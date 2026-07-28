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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))  # so `safety.corpus` resolves (repo convention)

from safety.corpus import SCHEMA  # noqa: E402

_TYPE_MAP = {"INTEGER": "integer", "REAL": "float", "TEXT": "string"}


# ── model + datasource builders (single-sourced from SCHEMA) ───────────────────────────────────
def build_org():
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
        subject_areas=[
            m.SubjectArea(name="sales", tables_defined=[_table(n, s) for n, s in SCHEMA.items()])
        ],
    )


def write_disk_model(root: Path) -> None:
    """Write the FILE-served model under `root` (an AGAMI_ARTIFACTS_DIR/<profile> dir)."""
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True, exist_ok=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]}
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
        for name, spec in SCHEMA.items():
            ddl = ", ".join(f"{c} {t}" for c, t in spec["columns"])
            con.execute(f"CREATE TABLE {name} ({ddl})")
            placeholders = ", ".join("?" for _ in spec["columns"])
            con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", spec["rows"])
        con.commit()
    finally:
        con.close()


def seed_db_model(url: str, ds: str = "acme") -> None:
    """Write the DB-served model into an app DB at `url` (the hosted path's model source)."""
    import model_store
    from store import Store

    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, ds, build_org())
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
