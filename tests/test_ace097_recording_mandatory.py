"""Recording is mandatory: a served deployment that cannot write the audit row does not execute.

Principle 7 says every call is recorded, executed or refused. That is only true if a call which
cannot be recorded does not happen — and the ordering is what makes this a real change rather than a
tidier `except`. The audit write runs at the tool edge, AFTER the statement: `tools._record_execution`
is called from `tools._emit`, the serializer. So closing the swallows alone can only turn a lost
record into an exception on a statement that already reached the customer's database.

This file pins the half that runs BEFORE execution:

  1. **The statement never runs.** With the store unopenable, `execute_guarded` returns
     `refused` / `audit_unavailable` and the executor is never called — asserted for the in-process
     path with a spy, and for the subprocess path by the warehouse file never coming into existence.
  2. **Local is untouched.** `governance-principles.md` scopes the principles to the served
     deployment, and locally there is no store to reach. A laptop with no `AGAMI_DB_URL` still
     answers.
  3. **The refusal is shaped like its sibling.** `audit_unavailable` is a deployment-state rule, so
     it reads `undetermined`, carries a value-free detail, and gets the before-the-model receipt —
     the gate runs above the semantic-model pass, so claiming "no model could be resolved" would be
     a different fact and an untrue one.

The store is broken with an UNSUPPORTED SCHEME rather than an unreachable host: `Store.connect`
raises on it deterministically, with no driver installed and no network wait, and it is one of the
two causes `_record_query`'s own docstring names (a malformed DSN, an uninstalled driver).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

PROFILE = "acme"
QUESTION = "how many orders"
# Unsupported on purpose: `Store.connect` raises `ValueError` for it before touching a driver or a
# socket, so "the store cannot be opened" is deterministic and instant on both processes. `_hosted()`
# only asks whether the variable is set, so this is a served deployment as far as the gate is
# concerned — which is the state under test.
BROKEN_DB_URL = "mysql://not-a-supported-scheme/agami"


@pytest.fixture(autouse=True)
def _isolate():
    """`_INJECTED_EXECUTOR` is a process global; a test that injects one must not leak it."""
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """A one-table model on disk, so the gates below the audit check have something to run against.

    It has to be a REAL model: the point of several tests here is that the audit gate fires before
    the model pass, and a deployment with no model at all would refuse for `model_unavailable`
    instead, which would pass the assertion for the wrong reason.
    """
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "columns": [{"name": "id", "type": "integer", "primary_key": True}],
        })
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A served install whose audit store is healthy, plus the knobs to break it.

    `warehouse` deliberately does NOT exist yet. sqlite creates a database file on connect, so its
    continued absence is a filesystem-level proof that nothing tried to execute — the one assertion
    that works across a process boundary, where a spy executor cannot reach.
    """
    app_db = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()

    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"

    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    return SimpleNamespace(app_db=app_db, artifacts=artifacts, warehouse=warehouse)


def _seed_warehouse(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.executemany("INSERT INTO orders (id) VALUES (?)", [(1,), (2,)])
    con.commit()
    con.close()


class _SpyExecutor:
    """An executor that records being asked and refuses to do anything else.

    Raising rather than returning an empty result is deliberate: if the gate under test regresses,
    the test must fail on the call itself rather than on a downstream assertion about rows, which
    could be satisfied for an unrelated reason.
    """

    def __init__(self) -> None:
        self.called = False

    def execute(self, sql, creds, *, profile=None, **kwargs):
        self.called = True
        raise AssertionError("the executor was reached with the audit store unopenable")


# ---------------------------------------------------------------------------
# 1. The statement does not run
# ---------------------------------------------------------------------------


def test_in_process_the_statement_never_reaches_the_executor(env, monkeypatch):
    """The in-process path: `execute_guarded` refuses and the executor is never called."""
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    spy = _SpyExecutor()

    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=spy
    )

    assert envelope.status == "refused"
    assert envelope.refusal is not None
    assert envelope.refusal.rule == guardrail.RULE_AUDIT_UNAVAILABLE
    assert envelope.refusal.reason == "undetermined"
    assert spy.called is False


def test_in_process_a_write_is_refused_for_the_audit_gate_not_the_read_only_one(env, monkeypatch):
    """The gate sits ABOVE `check_read_only`, and this pins that ordering rather than assuming it.

    Principle 7 records every call "whether it executed or was refused", so a gate that only stopped
    executions would leave the refusals unrecorded too — and the outcomes most worth reviewing are
    exactly the ones a reviewer could not find. Nothing is weakened by the ordering: both are
    refusals, the statement runs either way never, and the read-only gate is unreachable only in the
    state where nothing is reachable.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    spy = _SpyExecutor()

    envelope = execute_sql.execute_guarded(
        "DROP TABLE orders", PROFILE, "sales", executor=spy
    )

    assert envelope.status == "refused"
    assert envelope.refusal.rule == guardrail.RULE_AUDIT_UNAVAILABLE
    assert spy.called is False


def test_no_safety_cannot_opt_out_of_being_recorded(env, monkeypatch):
    """`no_safety` skips the semantic-model pass and nothing else.

    A caller able to turn off the audit guarantee by passing a flag is the hole this spec closes, so
    the check sits outside that branch.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    spy = _SpyExecutor()

    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=spy, no_safety=True
    )

    assert envelope.refusal.rule == guardrail.RULE_AUDIT_UNAVAILABLE
    assert spy.called is False


def test_the_subprocess_path_never_opens_the_warehouse(env, monkeypatch):
    """The fork path, proved from the filesystem rather than from a spy.

    `python -m execute_sql` runs the chokepoint in a child, where a monkeypatched spy cannot reach.
    sqlite creates its database file on connect, so a warehouse file that still does not exist after
    the call is proof the executor was never entered — the same claim as the in-process test, made
    the only way it can be made across a process boundary.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    assert not env.warehouse.exists(), "fixture invariant: the warehouse must not exist yet"

    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", PROFILE, "--area", "sales",
         "--sql", "SELECT id FROM orders"],
        capture_output=True, text=True, timeout=180, env={**os.environ},
    )

    refusal = json.loads(proc.stderr)["refusal"]
    assert refusal["rule"] == guardrail.RULE_AUDIT_UNAVAILABLE
    assert refusal["reason"] == "undetermined"
    assert not env.warehouse.exists(), "the executor connected to the warehouse despite the refusal"


# ---------------------------------------------------------------------------
# 2. Local is untouched
# ---------------------------------------------------------------------------


def test_local_still_answers_with_no_store_at_all(env, monkeypatch):
    """No `AGAMI_DB_URL` is the local path, and it keeps best-effort recording.

    The principles describe the served deployment; locally there is no store to reach and
    `_record_query` writes jsonl. A read-only artifacts directory must not stop a laptop answering.
    """
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    _seed_warehouse(env.warehouse)

    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=execute_sql.BUILTIN_EXECUTOR
    )

    assert envelope.status == "ok"


def test_local_never_opens_a_store_to_ask(monkeypatch):
    """The local no-op returns before touching `Store`, so the check costs a laptop nothing."""
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    import store as store_module

    def _boom(cls):
        raise AssertionError("the local path opened a store to check availability")

    monkeypatch.setattr(store_module.Store, "from_env", classmethod(_boom))
    assert execute_sql._audit_store_reachable() is True


# ---------------------------------------------------------------------------
# 3. The refusal's shape
# ---------------------------------------------------------------------------


def test_a_healthy_store_is_reachable_and_the_call_proceeds(env):
    """The positive control. Without it, every assertion above passes on a gate that always refuses."""
    _seed_warehouse(env.warehouse)

    assert execute_sql._audit_store_reachable() is True
    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=execute_sql.BUILTIN_EXECUTOR
    )
    assert envelope.status == "ok"


def test_the_refusal_says_nothing_about_where_the_store_lives(env, monkeypatch):
    """`Refusal.detail` and `remediation` are value-free — never a DSN, a host, a path or a scheme.

    The rule the two `model_unavailable` branches already follow, for the same reason: a refusal is
    the one outcome a caller can provoke on purpose, so it must not become a way to read the
    deployment's configuration back out.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)

    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=_SpyExecutor()
    )
    text = f"{envelope.refusal.detail} {envelope.refusal.remediation}"

    for leak in ("mysql", "not-a-supported-scheme", str(env.artifacts), env.app_db):
        assert leak not in text
    assert envelope.refusal.remediation.strip(), "a refusal the caller cannot act on is a dead end"


def test_the_audit_unavailable_refusal_itself_writes_no_row(env, monkeypatch):
    """The exemption, and the reason it is load-bearing rather than tidy.

    This refusal means the store could not be opened. Writing a row to say so is the same write
    failing a second time — and on the served path a failing write now RAISES, so without the
    exemption the clean fail-closed refusal would arrive at the caller as an unhandled exception
    from the serializer. `_emit` returning normally here is the whole assertion.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=_SpyExecutor()
    )
    assert envelope.refusal.rule == guardrail.RULE_AUDIT_UNAVAILABLE

    body = tools._emit(envelope, sql="SELECT id FROM orders", execution_ms=None, profile=PROFILE)

    assert json.loads(body)["status"] == "refused"


def test_the_tool_call_row_is_exempt_too_or_the_refusal_never_arrives(env, monkeypatch):
    """The exemption has to cover BOTH writes, and this is the test that says why.

    Exempting only the query row looks complete from the in-process path a unit test usually drives.
    It is not: the HTTP transport writes a tool-call row in a `finally` for EVERY call, so the row
    this exemption exists to avoid was written anyway, failed against the unreachable store, and
    raised — replacing the clean fail-closed refusal with a transport error and losing the
    remediation naming what the operator must restore. On the served surface, where they most need
    it.

    Found by driving a real server whose audit store died under it (testbed step 11b), not here.
    This is that finding, brought back as a unit test.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=_SpyExecutor()
    )
    body = tools._emit(envelope, sql="SELECT id FROM orders", execution_ms=None, profile=PROFILE)

    # The transport's own call, verbatim in shape: it hands over the serialized body and lets the
    # recorder classify it. Must not raise, or the refusal above never reaches the caller.
    tools.record_tool_call(
        name="execute_sql", arguments={"sql": "SELECT id FROM orders"}, result_text=body,
        execution_ms=1, actor=None,
    )

    assert json.loads(body)["refusal"]["rule"] == guardrail.RULE_AUDIT_UNAVAILABLE


def test_every_other_refusal_still_records(env):
    """The exemption is exactly one rule wide, asserted rather than assumed.

    An exemption keyed on "it is a refusal" would have quietly stopped recording the outcomes
    principle 7 most wants kept, and this file would still be green.
    """
    _seed_warehouse(env.warehouse)

    envelope = execute_sql.execute_guarded(
        "DROP TABLE orders", PROFILE, "sales", executor=execute_sql.BUILTIN_EXECUTOR
    )
    assert envelope.refusal.rule == guardrail.RULE_READ_ONLY

    tools._emit(envelope, sql="DROP TABLE orders", execution_ms=None, profile=PROFILE)

    store = Store.connect(env.app_db)
    try:
        rows = store.query("SELECT id, status, rule FROM query_executions")
    finally:
        store.close()
    assert [(r["status"], r["rule"]) for r in rows] == [("refused", guardrail.RULE_READ_ONLY)]


# ---------------------------------------------------------------------------
# 4. The two swallows on the tool-call path
# ---------------------------------------------------------------------------


def _jsonrpc_result(text: str) -> dict:
    """The `result` object out of a JSON-RPC reply, whether it arrived plain or SSE-framed.

    The streamable-HTTP transport may negotiate either, and which one it picks is not this test's
    subject — so the frame is stripped here rather than asserted on.
    """
    payload = text.strip()
    if payload.startswith("event:") or payload.startswith("data:"):
        payload = next(
            line[len("data:"):].strip()
            for line in payload.splitlines()
            if line.startswith("data:")
        )
    return json.loads(payload)["result"]


def _break_the_tool_call_sink(monkeypatch) -> None:
    import model_store

    def _boom(self, record):
        raise RuntimeError("the tool_calls table is unreachable")

    monkeypatch.setattr(model_store.DbActivitySink, "record_tool_call", _boom)


def test_the_recorder_no_longer_swallows_its_own_failure(env, monkeypatch):
    """The first of the two masking swallows: `_record_tool_call`'s `except Exception: pass`.

    Asserted on behaviour rather than by grepping for a bare `except`. A grep would have passed
    before this spec started — there is no bare `except:` anywhere in the package, and never was.
    Both swallows were `except Exception:`, which a grep for the bare form does not see.
    """
    _break_the_tool_call_sink(monkeypatch)

    with pytest.raises(RuntimeError):
        tools.record_tool_call(
            name="execute_sql", arguments={"sql": "SELECT 1"}, result_text=None,
            execution_ms=1, actor=None,
        )


def test_the_transport_no_longer_swallows_it_either(env, monkeypatch):
    """The second: the `except Exception: pass` around the call, in the HTTP transport's `finally`.

    The two are why neither had been closed. Removing this one alone changes nothing, because
    `_record_tool_call` swallowed internally and never raised anything for it to catch; removing
    that one alone changes nothing, because this caught what it then raised. Each made the other
    unobservable. So this drives the real transport with the sink broken and asserts the failure
    reaches the wire — which is only true once BOTH are gone.
    """
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://your-host.example.com")
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", "x" * 40)
    _seed_warehouse(env.warehouse)
    _break_the_tool_call_sink(monkeypatch)

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app(), raise_server_exceptions=False) as client:
        init = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post("/mcp", headers=headers2,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = client.post("/mcp", headers=headers2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": {
                "sql": "SELECT id FROM orders", "datasource": PROFILE}}})

    result = _jsonrpc_result(reply.text)

    # `isError` is read as a parsed boolean, not by scanning the body for "error". The substring is
    # present either way — `"isError":false` contains it — so a text search passes whether or not
    # the swallow is back, which is the one thing this test exists to tell apart. Caught by
    # restoring the swallow and watching this test stay green.
    assert result["isError"] is True, (
        "the tool call returned an answer with its audit row lost — a swallow is back"
    )
    assert "unreachable" in result["content"][0]["text"]


def test_locally_the_tool_call_recorder_is_still_best_effort(env, monkeypatch, caplog):
    """The local half of the same split, so the swallow's removal does not reach a laptop."""
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    def _boom(path, record):
        raise OSError("the log directory is read-only")

    monkeypatch.setattr(tools, "_append_jsonl", _boom)

    with caplog.at_level("WARNING", logger="tools"):
        tools.record_tool_call(
            name="execute_sql", arguments={"sql": "SELECT 1"}, result_text=None,
            execution_ms=1, actor=None,
        )  # must NOT raise

    assert [r.levelname for r in caplog.records if r.name == "tools"] == ["WARNING"]


def test_the_refusal_carries_the_before_the_model_receipt(env, monkeypatch):
    """It is in `PRE_MODEL_RULES`, so its receipt says the model was never consulted.

    The gate runs above the semantic-model pass, and the deployment's model resolves perfectly a
    line later — so the generic "no model could be resolved" marker would be a different fact and an
    untrue one. `PRE_MODEL_RULES`'s own docstring asks for this in the diff that adds the gate.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)

    envelope = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, "sales", executor=_SpyExecutor()
    )

    assert guardrail.RULE_AUDIT_UNAVAILABLE in guardrail.PRE_MODEL_RULES
    assert envelope.receipt.columns.undetermined == guardrail.RECEIPT_BEFORE_MODEL
