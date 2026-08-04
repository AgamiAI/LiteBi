"""The end-to-end harness the safety corpus runs on: the transports, and the file-served path.

Two things live here and nothing else.

**The routes.** A route takes `(sql, profile)` and returns the parsed tool-edge body — the JSON a
caller actually receives. `in_process` calls the tool directly with the built-in executor injected;
`http` goes through `mcp_http.create_app()`'s authenticated `/mcp` over `TestClient`, which is the
transport the hosted deployment serves. They are shaped after the four-route matrix in
`tests/test_ace035_no_enumeration.py` and deliberately copied rather than imported: a harness whose
transports can be re-pointed by an edit to another test file is not a harness, and that file's
routes are bound to its own model fixture.

**The file-served model path.** `AGAMI_ARTIFACTS_DIR` for the model and `DATASOURCE_URL__<PROFILE>`
for the warehouse — the two axes are orthogonal and this module wires the file/SQLite corner of
them. Both sides are derived from `safety.corpus.SCHEMA`, so the semantic model and the physical
tables cannot describe different columns; a governed vector that returns rows is returning the
rows this module seeded, against the model the gates read.

The model is written RICH rather than minimal — a declared relationship, an unreviewed metric, an
AI-written column description, a declared filter and a row estimate — because the governed vectors
assert that every receipt section is present, and a model that declares nothing produces sections
that are present but say nothing. The point of a receipt assertion is to reach the assembler's real
output, not its empty case.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (TESTS_ROOT, REPO_ROOT / "packages" / "agami-core" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import execute_sql  # noqa: E402
import tools  # noqa: E402

from safety.corpus import SCHEMA  # noqa: E402

PROFILE = "acme"
AREA = "sales"
# The engine the model DECLARES, and the one the warehouse actually is. They have to agree or the
# chokepoint refuses every vector with `engine_mismatch` before any gate under test runs.
ENGINE = "SQLite"

# The deployment ceiling the availability vectors are driven under, derived from the seed data so
# it cannot go stale: `orders` seeds three rows, so a ceiling one below that is over-run by the
# plain projection, and the cross join (three orders x two customers) over-runs it further.
LOW_ROW_CAP = len(SCHEMA["orders"]["rows"]) - 1

# A `TestClient` needs an issuer and a signing secret to mint the bearer the route carries. Both
# are fixture values for a throwaway in-process app; neither reaches a real deployment.
BASE_URL = "https://your-host.example.com"
SIGNING_SECRET = "x" * 40

# The seed data is written in the warehouse's own types; the model declares the semantic ones.
_MODEL_TYPE = {"INTEGER": "integer", "REAL": "decimal", "TEXT": "string"}

# The one declared filter, on `orders`. `{alias}` binds per table REFERENCE, so the same
# declaration reads `o.…` where the statement aliases and `orders.…` where it does not. It changes
# no statement — the injector that used to rewrite SQL is gone — it only gives the `tables` section
# something real to account for.
DECLARED_FILTER = "{alias}.customer_id IS NOT NULL"
ROW_ESTIMATE = 1200
ROWS_AS_OF = "2026-01-01T00:00:00Z"


def write_model(root: Path) -> None:
    """Write the disk YAML for `SCHEMA` under `root` (a profile directory)."""
    import yaml

    tables = root / "subject_areas" / AREA / "tables"
    tables.mkdir(parents=True)
    (root / "subject_areas" / AREA / "metrics").mkdir(parents=True)

    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "Shop",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": ENGINE}],
                "subject_areas": [f"subject_areas/{AREA}"],
            }
        )
    )
    (root / "subject_areas" / AREA / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": AREA,
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": name}
                    for name in SCHEMA
                ],
            }
        )
    )

    for name, spec in SCHEMA.items():
        columns = []
        for column, sqlite_type in spec["columns"]:
            declared: dict[str, object] = {"name": column, "type": _MODEL_TYPE[sqlite_type]}
            if column == "id":
                declared["primary_key"] = True
            if column in spec["sensitive"]:
                # A model FACT, and the reason it is declared here: a sensitive column is
                # projectable, so a vector reading one must come back `ok`. Declaring the flag is
                # what makes that a real assertion rather than one about a column nothing marked.
                declared["sensitive"] = True
            if column == "amount":
                # An AI-written meaning, so the `assumptions` section has something to carry.
                declared["description"] = "net revenue"
                declared["description_source"] = "ai_unvalidated"
            columns.append(declared)

        table: dict[str, object] = {
            "name": name,
            "schema": "public",
            "storage_connection": "c",
            "grain": ["id"],
            "description": name,
            "columns": columns,
        }
        if name == "orders":
            table["default_filters"] = [DECLARED_FILTER]
            table["performance_hints"] = {
                "estimated_row_count": ROW_ESTIMATE,
                "estimated_row_count_at": ROWS_AS_OF,
            }
        (tables / f"{name}.yaml").write_text(yaml.safe_dump(table))

    # An UNREVIEWED metric and an UNREVIEWED join: the two trust signals the receipt surfaces, and
    # what gives the `columns`, `joins` and `aggregates` sections real items to report on.
    (root / "subject_areas" / AREA / "metrics" / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "revenue",
                "calculation": "sum of order amount",
                "bindings": {ENGINE: "SUM(amount)"},
                "source_tables": ["orders"],
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )
    (root / "subject_areas" / AREA / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "from_table": "orders",
                        "from_column": "customer_id",
                        "to_table": "customers",
                        "to_column": "id",
                        "from_schema": "public",
                        "to_schema": "public",
                        "relationship": "many_to_one",
                        "confidence": "inferred",
                        "review_state": "unreviewed",
                    }
                ]
            }
        )
    )


def seed_warehouse(path: Path) -> None:
    """Create and seed the SQLite warehouse `SCHEMA` describes."""
    con = sqlite3.connect(path)
    try:
        for name, spec in SCHEMA.items():
            columns = ", ".join(
                f"{column} {sqlite_type}" for column, sqlite_type in spec["columns"]
            )
            con.execute(f"CREATE TABLE {name} ({columns})")
            placeholders = ", ".join("?" for _ in spec["columns"])
            con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", spec["rows"])
        con.commit()
    finally:
        con.close()


def build_file_path(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Wire the file-served model path and return where its two halves landed.

    Local, not hosted: `AGAMI_DB_URL` is removed, so the model is the one on disk and the gates
    read it. Setting it would flip `execute_sql._hosted()` and change what a missing model does,
    which is the DB-served path's own axis rather than a variation of this one.
    """
    artifacts = tmp_path / "artifacts"
    write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    seed_warehouse(warehouse)

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{warehouse}")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    for name in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID", "AGAMI_SQL_MAX_ROWS"):
        monkeypatch.delenv(name, raising=False)
    # Not a per-session budget: only the availability vectors want a tightened bound and they set
    # their own, so a stall on a loaded runner cannot turn an expected verdict into a timeout.
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)

    reset_injected_executor()
    return SimpleNamespace(artifacts=artifacts, warehouse=warehouse, profile=PROFILE)


def reset_injected_executor() -> None:
    """`tools._INJECTED_EXECUTOR` is a process global — both routes set it — so it must not leak
    between vectors."""
    tools.set_injected_executor(None)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def route_in_process(sql: str, profile: str = PROFILE) -> dict:
    """`execute_guarded` runs in THIS process and the gate's own object reaches the serializer."""
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": profile}))


def route_http(sql: str, profile: str = PROFILE) -> dict:
    """The HTTP transport, for real: `TestClient` over `create_app()`'s authenticated `/mcp`."""
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("jwt")
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app()) as client:
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post(
            "/mcp", headers=headers2, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        resp = client.post(
            "/mcp",
            headers=headers2,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": profile}},
            },
        )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


ROUTES = {
    "in_process": route_in_process,
    "http": route_http,
}
