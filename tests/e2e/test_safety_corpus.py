"""ACE-040 — the F9 safety regression corpus, end-to-end over BOTH surfaces (file-served model path).

Every attack class in `tests/safety/corpus.CASES` is driven through the REAL execute_sql tool on both
transports (stdio subprocess + in-process HTTP) and asserted against its expected Envelope. This is
the F9 done-bar for the controls it asserts: a regression in read-only, object-scope, fail-closed
scopability, or recon fails here on whichever surface it regressed; availability is asserted via the
row-cap arm (the statement-timeout arm is proven end-to-end in `tests/test_resource_limits.py`). The
read-only-role floor lives in `test_role_floor_pg.py` (Postgres, env-gated).

ACE-082 — per-engine EXECUTION coverage
=======================================
ACE-079 made the guard read every statement in the datasource's own dialect, and proved the VERDICT
on all eleven supported engines (`tests/test_guard_dialect_parsing.py`). That proof uses a spy
executor and no database: the vetted statement is re-parsed by sqlglot, never accepted by an engine.
This module adds the other half — the statement the guard vetted is a statement a REAL engine
accepts — and the table below is the single place that says how far that goes per engine.

| Engine     | Identifier quote | Coverage here                     | What is NOT proven                    |
|------------|------------------|-----------------------------------|---------------------------------------|
| SQLite     | `"` `` ` `` `[]` | real, in-process (file path)      | —                                     |
| PostgreSQL | `"`              | real server (`integration-pg`)    | —                                     |
| MySQL      | `` ` ``          | real server (`mysql:8`)           | —                                     |
| SQLServer  | `[]`             | real server (`mssql/server:2022`) | —                                     |
| DuckDB     | `"`              | real, in-process                  | —                                     |
| Redshift   | `"`              | verdict only (ACE-079)            | execution parity                      |
| Snowflake  | `"`              | verdict only (ACE-079)            | execution parity                      |
| BigQuery   | `` ` ``          | verdict only (ACE-079)            | execution parity                      |
| Databricks | `` ` ``          | verdict only (ACE-079)            | execution parity                      |
| Oracle     | `"`              | verdict only (ACE-079)            | execution parity                      |
| Trino      | `"`              | verdict only (ACE-079)            | execution parity                      |

The five executing engines cover all three identifier-quoting families — backtick (MySQL), bracket
(SQLServer) and double-quote (PostgreSQL/SQLite/DuckDB) — which is the axis ACE-079's defect class
turns on. The six verdict-only engines each share a quoting family with one that executes.

Two engines named in ACE-082 are deliberately absent, because reaching them would require changing
runtime source, which that spec forbids:
  - **BigQuery emulator** — `execute_sql._run_bigquery` builds `bigquery.Client(project=…)` with no
    `client_options` / `api_endpoint`, so it cannot be pointed at an emulator without a source
    change. (The emulator is also SQLite-backed, so it would prove parse/accept, not BigQuery
    semantics.)
  - **Databricks via local Spark** — `execute_sql._run_databricks` connects through the Databricks
    SQL connector to a workspace; a local `SparkSession` is not reachable through it. Testing it
    would mean asserting against a stub, which is what ACE-079 already does.
Both remain verdict-only above, and are recorded as residual risk in the spec rather than implied.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("sqlglot")
pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from safety.corpus import CASES  # noqa: E402


def assert_outcome(env: dict, expect: str, sql: str) -> None:
    """Assert the tool's Envelope matches the case's expected outcome — the single mapping used for
    every surface and every model path, so a surface/path can't silently diverge."""
    if expect == "ok":
        assert env["status"] == "ok", (sql, env)
        assert "refusal" not in env
        assert env["audit_id"]
    elif expect == "bounded":
        # Availability: a runaway result is bounded — EITHER a resource_limit refusal, OR an ok
        # Envelope flagged truncated with a row_cap in `applied` (capped + flagged, never silent).
        if env["status"] == "refused":
            assert env["refusal"]["kind"] == "resource_limit", (sql, env)
        else:
            assert env["status"] == "ok", (sql, env)
            assert env["data"]["truncated"] is True, (sql, env)
            assert any("row_cap" in a for a in env.get("applied", [])), (sql, env)
    else:
        # A refusal kind: the query is refused with exactly this kind, carries no data, is audited.
        assert env["status"] == "refused", (sql, env)
        assert env["refusal"]["kind"] == expect, (sql, env)
        assert "data" not in env
        assert env["audit_id"]


# The two paths read the same corpus but execute against different engines, and identifier
# quoting is engine-specific — so each path runs the cases that are valid SQL on its engine.
FILE_PATH_CASES = [c for c in CASES if c.runs_on("SQLite")]
DB_PATH_CASES = [c for c in CASES if c.runs_on("PostgreSQL")]


@pytest.mark.parametrize("case", FILE_PATH_CASES, ids=[c.id for c in FILE_PATH_CASES])
def test_safety_corpus_file_path(case, surface, file_safety_env):
    # File-served model (disk YAML) + SQLite datasource. Runs in the default (DB-free) test job.
    env = surface(case.sql, datasource="acme", max_rows=case.max_rows)
    assert_outcome(env, case.expect, case.sql)


# ── ACE-082: the same corpus, against real engines beyond Postgres/SQLite ─────────────────────
# One env fixture per executing engine. Each SKIPS without connection settings and FAILS instead
# when AGAMI_IT_<ENGINE>_REQUIRED is set, so the job that owns an engine's evidence cannot pass
# green while proving nothing. Pairing engine×case here (rather than skipping inside the test)
# keeps the ids exact, so a CI job selects its engine with `-k "engine and MySQL"`.
ENGINE_ENV_FIXTURES = {
    "MySQL": "mysql_safety_env",
    "SQLServer": "sqlserver_safety_env",
    "DuckDB": "duckdb_safety_env",
}
ENGINE_CASES = [(e, c) for e in ENGINE_ENV_FIXTURES for c in CASES if c.runs_on(e)]


@pytest.mark.parametrize(
    "engine,case", ENGINE_CASES, ids=[f"{e}:{c.id}" for e, c in ENGINE_CASES]
)
def test_safety_corpus_engine(engine, case, surface, request):
    # The verdict must be identical to the SQLite/Postgres paths AND the vetted statement must be
    # one this engine actually accepts — an "ok" case here executes for real, so a statement the
    # guard approved but the engine rejects fails as an executor error rather than passing.
    request.getfixturevalue(ENGINE_ENV_FIXTURES[engine])
    env = surface(case.sql, datasource="acme", max_rows=case.max_rows)
    assert_outcome(env, case.expect, case.sql)


@pytest.mark.parametrize("case", DB_PATH_CASES, ids=[c.id for c in DB_PATH_CASES])
def test_safety_corpus_db_path(case, surface, db_safety_env):
    # DB-served model (Postgres app DB) + Postgres datasource read as the read-only role. IDENTICAL
    # verdicts to the file path prove file/db parity (a control that reads the model can't behave
    # differently by source). Env-gated: skips unless a Postgres is reachable (the integration-pg job).
    env = surface(case.sql, datasource="acme", max_rows=case.max_rows)
    assert_outcome(env, case.expect, case.sql)
