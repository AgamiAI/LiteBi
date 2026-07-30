"""The tool-call activity log — the recorder, the actor plumbing, and the end-to-end capture.

The gate test (`test_authenticated_mcp_call_logs_the_actor`) proves the authenticated user reaches the
tool dispatch and lands in the log — the one piece of new wiring (a contextvar set in the raw-ASGI
`/mcp` endpoint, since the MCP handler only gets `(name, arguments)`).
"""

from __future__ import annotations

import itertools
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


def test_record_marks_a_guardrail_failure_body_unsuccessful(db):
    """`execute_sql` speaks the guardrail Envelope now, so a failed query arrives as
    `{"status": "failed", "failure": {kind, message}}` rather than `{"error": {kind, remediation}}`.

    The sink must read BOTH shapes: reading only the old one would log every failed query as a
    success, which is a silent hole in the audit rather than a formatting change. (The other tools
    still return the `{"error": …}` shape, pinned above.)"""
    tools.record_tool_call(
        name="execute_sql", arguments={"sql": "SELECT nope FROM t"},
        result_text='{"status": "failed", "failure": {"kind": "syntax", "message": "no column"}}',
        execution_ms=3, actor="a",
    )
    (r,) = _rows(db)
    assert r["success"] == 0 and r["error_kind"] == "syntax"


def test_record_leaves_a_refusal_marked_successful(db):
    """A refusal is the server working correctly, so it stays `success=1` — exactly as it did
    before the envelope, when a refusal body simply carried no `error` key. Reclassifying refusals
    as failures would be a policy change, and it is not this slice's to make."""
    tools.record_tool_call(
        name="execute_sql", arguments={"sql": "DELETE FROM t"},
        result_text='{"status": "refused", "refusal": {"reason": "unsafe", "rule": "read_only", '
                    '"detail": "d", "remediation": "r"}}',
        execution_ms=1, actor="a",
    )
    (r,) = _rows(db)
    assert r["success"] == 1 and r["error_kind"] is None


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


def test_session_from_scope_normalizes_a_malformed_session_id():
    """Mint and read must agree on what "no session" means. A third-party AuthProvider's principal may
    carry a blank or non-string `session_id`; letting it through would put an unexpected type into the
    contextvar and, from there, into consumers using it as a key."""

    class _AuthBad:
        def __init__(self, value):
            self.value = value

        def validate_token(self, token):
            return type("P", (), {"subject": "j@example.com", "session_id": self.value})()

    for bad in ("", "   ", 42, None, object()):
        auth = _AuthBad(bad)
        principal = auth.validate_token("good")
        assert mcp_http._session_from_scope({"state": {"principal": principal}}, auth) is None, bad
        scope = {"headers": [(b"authorization", b"Bearer good")]}
        assert mcp_http._session_from_scope(scope, auth) is None, bad

    # ...and a well-formed one still comes through untouched.
    ok = _AuthBad("sess-1")
    assert (
        mcp_http._session_from_scope({"state": {"principal": ok.validate_token("x")}}, ok)
        == "sess-1"
    )


def test_the_fallback_validates_the_bearer_exactly_once():
    """The subject and the session are read off ONE principal. Deriving them from two independent
    `validate_token` calls would double the work whenever the middleware's scope state didn't propagate —
    a second signature check, or a second lookup for a DB-backed provider — and would let the two values
    describe different callers if a provider's validation isn't deterministic."""
    calls = {"n": 0}

    class _Counting:
        def validate_token(self, token):
            calls["n"] += 1
            return type("P", (), {"subject": "j@example.com", "session_id": "s-1"})()

    auth = _Counting()
    scope = {"headers": [(b"authorization", b"Bearer good")]}   # no scope state -> the fallback path
    principal = mcp_http._principal_from_scope(scope, auth)
    assert calls["n"] == 1
    assert getattr(principal, "subject", None) == "j@example.com"
    assert mcp_http._normalized_session(principal) == "s-1"

    # ...and when the middleware DID propagate, the bearer is never re-validated at all.
    calls["n"] = 0
    p = type("P", (), {"subject": "j@example.com", "session_id": "s-2"})()
    assert mcp_http._principal_from_scope({"state": {"principal": p}}, auth) is p
    assert calls["n"] == 0


# --- the override seam -------------------------------------------------------
#
# `record_tool_call` is also called by embedders that dispatch tool handlers themselves rather than
# through this package's transport. Such a caller observes the grouping ids and the outcome directly,
# so it states them instead of having them read out of the model's arguments or parsed back out of a
# result body. Every override defaults to None, meaning "derive it the way you always have" — the
# tests below pin both halves: that the overrides win when given, and that nothing moves when they
# are absent.


def test_the_default_path_is_unchanged_when_no_override_is_passed(db):
    """The regression that protects every existing caller: same call, same row as before the seam."""
    tools.record_tool_call(
        name="execute_sql",
        arguments={"datasource": "SALES_DATA", "sql": "SELECT 1", "user_question": "how many?",
                   "raw_query": "count", "thread_id": "t1", "correlation_id": "c1"},
        result_text='{"row_count": 5}', execution_ms=84, actor="jordan@example.com",
    )
    (r,) = _rows(db)
    assert r["source"] == tools.DEFAULT_CALL_SOURCE
    # every self-report still read straight out of the model's arguments
    assert r["user_question"] == "how many?" and r["thread_id"] == "t1"
    assert r["correlation_id"] == "c1" and r["agent_query"] == "count"
    # ...and the outcome still derived from the result body
    assert r["success"] == 1 and r["row_count"] == 5 and r["error_kind"] is None


def test_stated_ids_beat_the_model_s_self_report(db):
    """A caller that observed the question and minted the ids outranks whatever the model wrote into
    its own tool arguments — otherwise the model could choose how its calls are grouped."""
    tools.record_tool_call(
        name="execute_sql",
        arguments={"sql": "SELECT 1", "user_question": "a drifted sub-question",
                   "raw_query": "count", "thread_id": "model-made-this-up", "correlation_id": "and-this"},
        result_text="{}", execution_ms=1, actor="a",
        source="embedded", thread_id="th-1", correlation_id="co-1", user_question="how many widgets?",
    )
    (r,) = _rows(db)
    assert r["source"] == "embedded"
    assert r["thread_id"] == "th-1" and r["correlation_id"] == "co-1"
    assert r["user_question"] == "how many widgets?"
    # agent_query is deliberately NOT overridable — it is the model's framing, and that is the point
    assert r["agent_query"] == "count"


def test_a_stated_outcome_beats_the_derived_one(db):
    """The reason the outcome is overridable at all: a caller may have classified the result already
    and have no body left to hand over. Without this the row would default to success — the one
    direction an audit log must never fail in."""
    tools.record_tool_call(
        name="a_tool", arguments={}, result_text=None, execution_ms=1, actor="a",
        success=False, error_kind="bad_request", row_count=3,
    )
    (r,) = _rows(db)
    assert r["success"] == 0 and r["error_kind"] == "bad_request" and r["row_count"] == 3


def test_a_stated_outcome_replaces_the_derived_one_wholesale(db):
    """The outcome trio is applied as a GROUP, not field by field. Stating success on a body that
    parses as an error must not leave the derived `error_kind` stranded beside it — that would write a
    row saying "succeeded, syntax error", which is worse than either alone."""
    tools.record_tool_call(
        name="a_tool", arguments={}, result_text='{"error": {"kind": "syntax"}}',
        execution_ms=1, actor="a", success=True,
    )
    (r,) = _rows(db)
    assert r["success"] == 1 and r["error_kind"] is None


@pytest.mark.parametrize(
    ("overrides", "expected_success", "expected_error_kind"),
    [
        # An error_kind with no explicit flag IS a statement of failure. Defaulting it to success is
        # how the "succeeded, syntax error" row came back the first time this was fixed.
        ({"error_kind": "syntax"}, 0, "syntax"),
        # ...and a success can never carry one, however the caller phrases it.
        ({"success": True, "error_kind": "syntax"}, 1, None),
        ({"success": False, "error_kind": "syntax"}, 0, "syntax"),
        ({"success": False}, 0, None),
        ({"success": True}, 1, None),
        ({"row_count": 5}, 1, None),
        ({"row_count": 0}, 1, None),  # a falsy value still counts as stated
    ],
)
def test_no_combination_of_outcome_overrides_writes_an_incoherent_row(
    db, overrides, expected_success, expected_error_kind
):
    """Every way a caller can state the outcome, checked for coherence rather than field by field.

    The failure this guards is not hypothetical: the grouped-override logic shipped once with
    `error_kind` alone defaulting the row to success, which is precisely the contradiction the group
    was introduced to remove."""
    tools.record_tool_call(
        name="a_tool", arguments={}, result_text=None, execution_ms=1, actor="a", **overrides
    )
    (r,) = _rows(db)
    assert (r["success"], r["error_kind"]) == (expected_success, expected_error_kind)
    assert not (r["success"] == 1 and r["error_kind"]), "a successful row must carry no error kind"


@pytest.mark.parametrize(
    ("overrides", "expected_error_kind"),
    [
        ({}, "exception"),
        ({"row_count": 5}, "exception"),  # an innocuous argument must not erase the exception
        ({"success": True}, "exception"),
        ({"success": True, "error_kind": None}, "exception"),
        ({"error_kind": "timeout"}, "timeout"),  # a MORE specific kind is still welcome
        ({"success": False, "error_kind": "timeout"}, "timeout"),
        # A caller contradicting themselves loses the success claim but keeps the diagnosis: there is
        # no reason for `raised` to also discard the specific kind and fall back to the generic one.
        ({"success": True, "error_kind": "timeout"}, "timeout"),
        ({"success": True, "error_kind": "timeout", "row_count": 5}, "timeout"),
    ],
)
def test_a_call_that_raised_stays_a_failure_whatever_the_caller_passes(
    db, overrides, expected_error_kind
):
    """`raised` is not a classification on offer — it is a fact this function was told about what the
    tool actually did. No override may turn a call that threw into a successful one, or an exception
    could be logged as a success and vanish from every error view."""
    tools.record_tool_call(
        name="a_tool", arguments={}, result_text=None, execution_ms=1, actor="a",
        raised=True, **overrides,
    )
    (r,) = _rows(db)
    assert r["success"] == 0, f"a raised call logged as a success: {overrides}"
    assert r["error_kind"] == expected_error_kind


def test_a_stated_tenant_is_not_re_read_from_the_process_context(db, monkeypatch):
    """The tenant is otherwise stamped downstream by re-reading this process's context, whose fallback
    is the deployment-wide org. A caller that read it where the work was actually scoped states it."""
    monkeypatch.setattr(tools, "_current_org_id", lambda: "the-wrong-tenant")
    tools.record_tool_call(
        name="a_tool", arguments={}, result_text="{}", execution_ms=1, actor="a", org_id="acme"
    )
    (r,) = _rows(db)
    assert r["org_id"] == "acme"


def test_the_call_source_contextvar_scopes_rows_and_resets(db):
    """The source can also be scoped rather than passed, for the tool logging a caller does not reach
    (a handler that records its own execution). It must not leak past the scope."""
    token = tools.set_call_source("embedded")
    try:
        tools.record_tool_call(name="a_tool", arguments={}, result_text="{}", execution_ms=1, actor="a")
        assert tools.current_call_source() == "embedded"
    finally:
        tools.reset_call_source(token)
    assert tools.current_call_source() == tools.DEFAULT_CALL_SOURCE
    tools.record_tool_call(name="b_tool", arguments={}, result_text="{}", execution_ms=1, actor="a")
    scoped, after = _rows(db)
    assert scoped["source"] == "embedded"
    assert after["source"] == tools.DEFAULT_CALL_SOURCE


def test_an_explicit_source_beats_the_contextvar(db):
    token = tools.set_call_source("embedded")
    try:
        tools.record_tool_call(name="a_tool", arguments={}, result_text="{}", execution_ms=1,
                               actor="a", source="explicit")
    finally:
        tools.reset_call_source(token)
    (r,) = _rows(db)
    assert r["source"] == "explicit"


def test_the_query_log_carries_the_same_source_as_the_tool_call_log(db, monkeypatch, tmp_path):
    """`execute_sql` writes a SECOND log row of its own, from a depth no parameter of the caller's
    reaches. If it kept a hard-coded source while the tool-call row took a scoped one, the two logs
    would disagree about what drove one execution — so it reads the same scope. Unset, it is the value
    it has always been."""
    seen = []
    monkeypatch.setattr(tools, "_record_query", lambda rec: seen.append(rec))

    def _log_a_query():
        tools._record_query({"ts": "t", "profile": "p", "sql": "SELECT 1", "row_count": 0,
                             "source": tools.current_call_source()})

    _log_a_query()
    token = tools.set_call_source("embedded")
    try:
        _log_a_query()
    finally:
        tools.reset_call_source(token)
    _log_a_query()
    assert [r["source"] for r in seen] == [tools.DEFAULT_CALL_SOURCE, "embedded",
                                           tools.DEFAULT_CALL_SOURCE]


def test_no_combination_of_outcome_arguments_can_write_a_dishonest_row(db):
    """The whole override space, checked for invariants rather than for expected values.

    Three separate review rounds found defects in this block, every one a combination the in-repo
    caller never produces: an `error_kind` alone defaulting to success, `raised` flipped by an
    unrelated `row_count`, a stated kind discarded by a contradictory success. Each was fixed with a
    test for that case, and the next case slipped through the same way — because the matrix was
    written from the cases already thought of.

    So this asserts the three properties that must hold for EVERY input instead:
      1. a successful row never carries an error kind;
      2. a call that raised is never logged as a success;
      3. an explicitly stated error kind is never silently replaced on a failed row;
      4. naming an error kind, without also claiming success, produces a FAILED row.

    Property 4 is the one that matters most and was the last to be written down. The other three are
    all satisfied by turning a stated error into `success=1, error_kind=NULL` — which is perfectly
    coherent, and is exactly the first bug found here. Coherence alone is not honesty: a row can
    contradict itself, or it can quietly agree with itself about the wrong thing.
    """
    cases = list(
        itertools.product(
            [False, True],                     # raised
            [None, True, False],               # success
            [None, "timeout"],                 # error_kind
            [None, 0, 5],                      # row_count
            [None, "{}", '{"error": {"kind": "syntax"}}', "not json", '{"row_count": 3}'],
        )
    )
    store = Store.connect(db)
    try:
        for raised, success, kind, rows, body in cases:
            overrides: dict[str, object] = {}
            if success is not None:
                overrides["success"] = success
            if kind is not None:
                overrides["error_kind"] = kind
            if rows is not None:
                overrides["row_count"] = rows
            tools.record_tool_call(
                name="a_tool", arguments={}, result_text=body, execution_ms=1, actor="a",
                raised=raised, **overrides,
            )
            r = store.query(
                "SELECT success, error_kind FROM tool_calls ORDER BY rowid DESC LIMIT 1"
            )[0]
            case = f"raised={raised} success={success} kind={kind!r} rows={rows} body={body!r}"
            assert not (r["success"] == 1 and r["error_kind"]), f"succeeded WITH an error: {case}"
            assert not (raised and r["success"] == 1), f"a raise logged as success: {case}"
            if kind is not None and r["success"] == 0:
                assert r["error_kind"] == kind, f"stated kind dropped: {case}"
            if kind is not None and success is not True:
                assert r["success"] == 0, f"a stated error kind logged as a success: {case}"
    finally:
        store.close()
