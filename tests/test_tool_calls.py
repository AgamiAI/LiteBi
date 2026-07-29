"""The tool-call activity log — the recorder, the actor plumbing, and the end-to-end capture.

The gate test (`test_authenticated_mcp_call_logs_the_actor`) proves the authenticated user reaches the
tool dispatch and lands in the log — the one piece of new wiring (a contextvar set in the raw-ASGI
`/mcp` endpoint, since the MCP handler only gets `(name, arguments)`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
import tools  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "calls.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


# --- the recorder ------------------------------------------------------------


def _rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM tool_calls ORDER BY ts")
    s.close()
    return rows


def test_record_derives_success_rowcount_and_self_report(db):
    tools.record_tool_call(
        name="execute_sql",
        arguments={"datasource": "SALES_DATA", "sql": "SELECT 1", "user_question": "how many?",
                   "raw_query": "count", "thread_id": "t1"},
        result_text='{"row_count": 5}', execution_ms=84, actor="jordan@example.com",
    )
    (r,) = _rows(db)
    assert r["tool_name"] == "execute_sql" and r["actor"] == "jordan@example.com"
    assert r["datasource"] == "SALES_DATA" and r["sql"] == "SELECT 1" and r["row_count"] == 5
    assert r["success"] == 1 and r["execution_ms"] == 84
    assert r["user_question"] == "how many?" and r["agent_query"] == "count" and r["thread_id"] == "t1"


def test_record_marks_error_body_and_exception(db):
    tools.record_tool_call(name="execute_sql", arguments={"sql": "x"},
                           result_text='{"error": {"kind": "syntax"}}', execution_ms=3, actor="a")
    tools.record_tool_call(name="execute_sql", arguments={"sql": "x"}, result_text=None,
                           execution_ms=1, actor="a", raised=True)
    err_body, raised = _rows(db)
    assert err_body["success"] == 0 and err_body["error_kind"] == "syntax"
    assert raised["success"] == 0 and raised["error_kind"] == "exception"


def test_record_logs_every_tool_with_null_self_report(db):
    tools.record_tool_call(name="list_datasources", arguments={}, result_text="[]",
                           execution_ms=2, actor="a")
    (r,) = _rows(db)
    assert r["tool_name"] == "list_datasources" and r["user_question"] is None and r["thread_id"] is None


def test_record_tolerates_a_non_json_result(db):
    tools.record_tool_call(name="list_datasources", arguments={}, result_text="not json",
                           execution_ms=1, actor="a")
    (r,) = _rows(db)
    assert r["success"] == 1 and r["row_count"] is None  # unparseable result → defaults, no crash


def test_record_is_best_effort_and_never_raises(tmp_path, monkeypatch):
    # No datastore configured → falls back to the local jsonl; a broken record is swallowed.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setattr(tools, "TOOL_CALL_LOG", tmp_path / "tool_calls.jsonl")
    tools.record_tool_call(name="x", arguments={}, result_text="{}", execution_ms=1, actor=None)
    assert (tmp_path / "tool_calls.jsonl").exists()
    # An un-serializable argument must not surface — the recorder swallows everything.
    tools.record_tool_call(name="x", arguments={"sql": object()}, result_text=None, execution_ms=0, actor=None)


# --- the actor plumbing ------------------------------------------------------


class _P:
    subject = "jordan@example.com"


class _Auth:
    def validate_token(self, token):
        return _P() if token == "good" else None


def test_actor_from_scope_prefers_state_then_header():
    auth = _Auth()
    # principal already on scope state (the middleware path)
    assert mcp_http._actor_from_scope({"state": {"principal": _P()}}, auth) == "jordan@example.com"
    # fallback: re-validate the bearer from the scope headers
    scope = {"headers": [(b"authorization", b"Bearer good")]}
    assert mcp_http._actor_from_scope(scope, auth) == "jordan@example.com"
    # nothing usable → None
    assert mcp_http._actor_from_scope({"headers": [(b"authorization", b"Bearer bad")]}, auth) is None
    assert mcp_http._actor_from_scope({}, auth) is None


# --- end to end: the gate ----------------------------------------------------


def _mcp_tool_call(subject: str, name: str) -> None:
    """initialize → notifications/initialized → tools/call(name) over the authed HTTP transport."""
    from oauth_server import issue_jwt

    headers = {
        "Authorization": f"Bearer {issue_jwt(subject)}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.build_app()) as c:
        init = c.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        h2 = {**headers, **({"mcp-session-id": init.headers["mcp-session-id"]}
                            if init.headers.get("mcp-session-id") else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": name, "arguments": {}}})


def test_authenticated_mcp_call_logs_the_actor(db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    _mcp_tool_call("jordan@example.com", "list_datasources")
    rows = [r for r in _rows(db) if r["tool_name"] == "list_datasources"]
    assert rows and rows[0]["actor"] == "jordan@example.com"  # the actor reached the dispatch + the log


def test_a_raising_tool_is_still_logged_as_an_error(db, monkeypatch):
    # The capture hook records a tool that raises (and re-raises it) — failures are observable too.
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)

    def _boom(_args):
        raise RuntimeError("nope")

    monkeypatch.setitem(tools.TOOLS["list_datasources"], "handler", _boom)
    _mcp_tool_call("jordan@example.com", "list_datasources")
    rows = [r for r in _rows(db) if r["tool_name"] == "list_datasources"]
    assert rows and rows[0]["success"] == 0 and rows[0]["error_kind"] == "exception"


# --- the session plumbing ----------------------------------------------------


def test_session_from_scope_prefers_state_then_header():
    """Mirrors `_actor_from_scope`: state first, re-validate the bearer as a fallback, None otherwise."""

    class _PS:
        subject = "jordan@example.com"
        session_id = "sess-1"

    class _AuthS:
        def validate_token(self, token):
            return _PS() if token == "good" else None

    auth = _AuthS()
    assert mcp_http._session_from_scope({"state": {"principal": _PS()}}, auth) == "sess-1"
    scope = {"headers": [(b"authorization", b"Bearer good")]}
    assert mcp_http._session_from_scope(scope, auth) == "sess-1"
    assert (
        mcp_http._session_from_scope({"headers": [(b"authorization", b"Bearer bad")]}, auth) is None
    )
    assert mcp_http._session_from_scope({}, auth) is None


def test_session_from_scope_tolerates_a_principal_without_the_field():
    """`AuthProvider` is a @runtime_checkable Protocol — it checks method presence only, so a third-party
    provider may return any object exposing `.subject`. Reading the session must not explode on one."""
    auth = _Auth()  # its principal `_P` has `subject` and nothing else
    assert mcp_http._session_from_scope({"state": {"principal": _P()}}, auth) is None
    assert (
        mcp_http._session_from_scope({"headers": [(b"authorization", b"Bearer good")]}, auth)
        is None
    )


def test_current_session_id_is_none_outside_a_request():
    assert mcp_http.current_session_id() is None
