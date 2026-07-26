"""ACE-040 — the F9 safety regression corpus, end-to-end over BOTH surfaces (file-served model path).

Every attack class in `tests/safety/corpus.CASES` is driven through the REAL execute_sql tool on both
transports (stdio subprocess + in-process HTTP) and asserted against its expected Envelope. This is
the F9 done-bar for the controls it asserts: a regression in read-only, object-scope, fail-closed
scopability, or recon fails here on whichever surface it regressed; availability is asserted via the
row-cap arm (the statement-timeout arm is proven end-to-end in `tests/test_resource_limits.py`). The
read-only-role floor lives in `test_role_floor_pg.py` (Postgres, env-gated).
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


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_safety_corpus_file_path(case, surface, file_safety_env):
    # File-served model (disk YAML) + SQLite datasource. Runs in the default (DB-free) test job.
    env = surface(case.sql, datasource="acme", max_rows=case.max_rows)
    assert_outcome(env, case.expect, case.sql)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_safety_corpus_db_path(case, surface, db_safety_env):
    # DB-served model (Postgres app DB) + Postgres datasource read as the read-only role. IDENTICAL
    # verdicts to the file path prove file/db parity (a control that reads the model can't behave
    # differently by source). Env-gated: skips unless a Postgres is reachable (the integration-pg job).
    env = surface(case.sql, datasource="acme", max_rows=case.max_rows)
    assert_outcome(env, case.expect, case.sql)
