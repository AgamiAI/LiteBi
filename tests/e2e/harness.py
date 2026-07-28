"""Shared e2e harness for the F9 safety corpus (ACE-040): the two transport drivers (stdio + HTTP)
and the builders that materialize the demo model + datasource from `tests/safety/corpus.SCHEMA`.

Kept as a plain importable module (not conftest) so both `test_safety_corpus.py` and the existing
`test_safety_envelope.py` can share ONE copy of the drivers — the "both surfaces in sync" proof lives
in one place, not duplicated per file.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))  # so `safety.corpus` resolves (repo convention)

from safety.corpus import SCHEMA  # noqa: E402

_TYPE_MAP = {"INTEGER": "integer", "REAL": "float", "TEXT": "string"}


# ── the physical-fixture seam (ACE-082) ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EngineFixture:
    """How the single-sourced `SCHEMA` is materialized physically on ONE engine.

    The corpus never forks: the cases, the schema and the expected verdicts are shared. The only
    thing that legitimately varies per engine is the physical fixture — how each column type is
    spelled in DDL, which parameter placeholder the driver takes, and whether DROP needs a
    CASCADE. Naming exactly those three here keeps every engine on one seeding code path, so
    adding an engine is a row in `ENGINE_FIXTURES` rather than a second seeder that can drift.
    """

    storage_type: str  # what the semantic model declares (m.StorageConnection.storage_type)
    credential_type: str  # what the executor dispatches on (`type` in the DSN / credentials file)
    ddl_types: dict[str, str] = field(default_factory=dict)
    placeholder: str = "?"
    drop_suffix: str = ""  # Postgres needs CASCADE; the others reject the keyword


ENGINE_FIXTURES: dict[str, EngineFixture] = {
    "SQLite": EngineFixture(
        "SQLite", "sqlite", {"INTEGER": "INTEGER", "REAL": "REAL", "TEXT": "TEXT"}, "?"
    ),
    "PostgreSQL": EngineFixture(
        "PostgreSQL",
        "postgres",
        {"INTEGER": "integer", "REAL": "double precision", "TEXT": "text"},
        "%s",
        " CASCADE",
    ),
    "MySQL": EngineFixture(
        "MySQL", "mysql", {"INTEGER": "int", "REAL": "double", "TEXT": "varchar(255)"}, "%s"
    ),
    "SQLServer": EngineFixture(
        "SQLServer", "sqlserver", {"INTEGER": "int", "REAL": "float", "TEXT": "nvarchar(255)"}, "%s"
    ),
    "DuckDB": EngineFixture(
        "DuckDB", "duckdb", {"INTEGER": "INTEGER", "REAL": "DOUBLE", "TEXT": "VARCHAR"}, "?"
    ),
}


def seed_schema(execute, engine: str, *, drop_first: bool = False) -> None:
    """Materialize every `SCHEMA` table on `engine`, through the caller's `execute(sql, params=None)`.

    The adapter is passed in rather than a connection, so this function imports no driver and is
    the single place that turns `SCHEMA` into physical tables — on every engine, including the two
    that were previously seeded by hand (SQLite here, Postgres inline in `conftest.db_safety_env`).
    """
    fx = ENGINE_FIXTURES[engine]
    for name, spec in SCHEMA.items():
        if drop_first:
            execute(f"DROP TABLE IF EXISTS {name}{fx.drop_suffix}")
        ddl = ", ".join(f"{c} {fx.ddl_types[t]}" for c, t in spec["columns"])
        execute(f"CREATE TABLE {name} ({ddl})")
        placeholders = ", ".join(fx.placeholder for _ in spec["columns"])
        for row in spec["rows"]:
            execute(f"INSERT INTO {name} VALUES ({placeholders})", row)


# ── model + datasource builders (single-sourced from SCHEMA) ───────────────────────────────────
def build_org(storage_type: str = "SQLite"):
    """The semantic model the guards scope against — built from SCHEMA (names + sensitive flags)."""
    from semantic_model import models as m

    def _table(name: str, spec: dict):
        cols = [
            m.Column(name=c, type=_TYPE_MAP.get(t, "string"), sensitive=(c in spec["sensitive"]))
            for c, t in spec["columns"]
        ]
        return m.Table(
            name=name,
            schema="public",
            storage_connection="c",
            grain=["id"],
            description=name,
            columns=cols,
        )

    return m.Datasource(
        datasource="Shop",
        version=1,
        # The declared engine is what the guards parse every statement in, and it is now
        # reconciled against the credentials the executor connects with — so it must name the
        # engine that actually runs the SQL. The file-served corpus runs SQLite (`seed_sqlite`);
        # the DB-served corpus in the Postgres job runs Postgres.
        storage_connections=[m.StorageConnection(name="c", storage_type=storage_type)],
        subject_areas=[
            m.SubjectArea(name="sales", tables_defined=[_table(n, s) for n, s in SCHEMA.items()])
        ],
    )


def write_disk_model(root: Path, storage_type: str = "SQLite") -> None:
    """Write the FILE-served model under `root` (an AGAMI_ARTIFACTS_DIR/<profile> dir)."""
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True, exist_ok=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "Shop",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": storage_type}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": t} for t in SCHEMA
                ],
            }
        )
    )
    for name, spec in SCHEMA.items():
        cols = []
        for cname, ctype in spec["columns"]:
            col = {"name": cname, "type": _TYPE_MAP.get(ctype, "string")}
            if cname == "id":
                col["primary_key"] = True
            if cname in spec["sensitive"]:
                col["sensitive"] = True
            cols.append(col)
        (root / "subject_areas" / "sales" / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": name,
                    "columns": cols,
                }
            )
        )


def seed_sqlite(path: Path) -> None:
    """Create + seed the physical SQLite datasource governed queries execute against."""
    con = sqlite3.connect(str(path))
    try:
        seed_schema(lambda sql, params=None: con.execute(sql, params or ()), "SQLite")
        con.commit()
    finally:
        con.close()


def seed_duckdb(path: Path) -> None:
    """Create + seed the physical DuckDB datasource. Seeded through a WRITE connection here because
    the executor deliberately opens DuckDB `read_only=True` — the fixture must not need the executor
    to be writable to exist."""
    import duckdb  # type: ignore

    con = duckdb.connect(str(path))
    try:
        seed_schema(lambda sql, params=None: con.execute(sql, params or []), "DuckDB")
    finally:
        con.close()


def seed_db_model(url: str, ds: str = "acme", storage_type: str = "SQLite") -> None:
    """Write the DB-served model into an app DB at `url` (the hosted path's model source)."""
    import model_store
    from store import Store

    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, ds, build_org(storage_type))
    s.close()


# ── transport drivers: each returns the execute_sql tool's parsed Envelope ─────────────────────
def _tool_args(sql: str, datasource: str | None, max_rows: int | None) -> dict:
    args: dict = {"sql": sql}
    if datasource:
        args["datasource"] = datasource
    if max_rows is not None:
        args["max_rows"] = max_rows
    return args


def stdio_execute_sql(sql: str, datasource: str | None = None, max_rows: int | None = None) -> dict:
    """Drive execute_sql over the real stdio server (a subprocess), return the tool's Envelope."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": _tool_args(sql, datasource, max_rows)},
        },
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
    )
    by_id = {m.get("id"): m for m in (json.loads(x) for x in proc.stdout.splitlines() if x.strip())}
    return json.loads(by_id[2]["result"]["content"][0]["text"])


def http_execute_sql(sql: str, datasource: str | None = None, max_rows: int | None = None) -> dict:
    """Drive execute_sql over the real HTTP transport (in-process TestClient), return the Envelope."""
    import mcp_http
    from starlette.testclient import TestClient

    headers = {
        "Authorization": "Bearer present",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.build_app()) as c:
        init = c.post(
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
        sid = init.headers.get("mcp-session-id")
        h2 = {**headers, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = c.post(
            "/mcp",
            headers=h2,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "execute_sql",
                    "arguments": _tool_args(sql, datasource, max_rows),
                },
            },
        )
    rpc = json.loads(re.search(r"\{.*\}", r.text, re.DOTALL).group(0))
    return json.loads(rpc["result"]["content"][0]["text"])


SURFACES = {"stdio": stdio_execute_sql, "http": http_execute_sql}
