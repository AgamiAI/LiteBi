"""End-to-end: execute_sql returns ONE response Envelope on BOTH surfaces (stdio + HTTP).

A safety violation → status=refused + refusal{kind} + no data; a clean query → status=ok + data +
audit_id. The full adversarial safety corpus + the read-only-DB-role floor live in
`test_safety_corpus.py` / `test_role_floor_pg.py`; this file locks the cross-surface Envelope shape
the shared contract promises. The stdio + HTTP drivers (and `presence_auth`) are the shared harness —
one copy — so this file and the corpus exercise the exact same two surfaces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

PKG_SRC = Path(__file__).resolve().parents[2] / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import harness  # noqa: E402
import tools  # noqa: E402

_stdio_execute_sql = harness.stdio_execute_sql
_http_execute_sql = harness.http_execute_sql


# --- the cross-surface contract ---------------------------------------------------------------


def _assert_refused(env: dict) -> None:
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "permission"
    assert env["refusal"]["reason"]  # a human reason is present
    assert "data" not in env  # a refusal carries no data
    assert env["audit_id"]  # references the recorded trail


def test_write_is_refused_with_one_envelope_over_stdio():
    _assert_refused(_stdio_execute_sql("DELETE FROM users"))


def test_write_is_refused_with_one_envelope_over_http(presence_auth):
    _assert_refused(_http_execute_sql("DELETE FROM users"))


def test_http_refusal_writes_a_guardrail_audit_row(presence_auth, tmp_path, monkeypatch):
    # The audit fires at the shared chokepoint on BOTH surfaces (unlike tool_calls, which only the HTTP
    # transport writes). Pin the actual claim on the HTTP transport: a refusal over HTTP writes ONE
    # guardrail_audit row the Envelope's audit_id points at — not merely that the Envelope carries an
    # audit_id. A regression that bypassed _finish on this surface would drop the row and be caught.
    from store import Store

    url = "sqlite://" + str(tmp_path / "audit.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()

    env = _http_execute_sql("DELETE FROM users")
    assert env["status"] == "refused" and env["audit_id"]

    s = Store.connect(url)
    rows = s.query("SELECT * FROM guardrail_audit")
    s.close()
    match = [r for r in rows if r["audit_id"] == env["audit_id"]]
    assert len(match) == 1  # exactly the row the Envelope points at, written over HTTP
    assert match[0]["status"] == "refused" and match[0]["refusal_kind"] == "permission"


def test_clean_query_returns_ok_envelope_over_http(presence_auth, monkeypatch):
    # A governed query needs no live DB here — fake the guarded executor so the test stays hermetic.
    # The HTTP transport runs execution IN-PROCESS by default (ACE-028), so fake execute_guarded (not
    # the subprocess); the point is the Envelope shape the transport hands back, not the driver.
    import execute_sql

    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        lambda *a, **k: execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False),
    )
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    monkeypatch.setattr(tools, "_resolve_receipt", lambda *a: None)

    env = _http_execute_sql("SELECT n FROM t")
    assert env["status"] == "ok"
    assert env["data"]["row_count"] == 1  # the ExecuteSqlResult payload rides under `data`
    assert env["data"]["columns"] == ["n"]
    assert env["audit_id"]
    assert "refusal" not in env
