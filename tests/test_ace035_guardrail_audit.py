"""The audit trail is real: one `query_executions` row per `execute_sql` call, on every outcome,
keyed by the very `audit_id` the answer carried back.

Three claims, in order of how load-bearing they are:

  1. **Refusals are recorded at all.** The write used to hang off `_finalize_execution`, which only
     ever runs on success — so the trail held the queries that worked and silently dropped every
     decision we made against one. The outcomes most worth reviewing were exactly the ones a
     reviewer could not find. It now hangs off `_emit`, the single tool-edge serializer, so `ok`,
     `refused` and `failed` all land, on **both** transports.
  2. **`Envelope.audit_id` IS `query_executions.id`.** Not a parallel identifier and not a join key:
     the sink writes the caller's id as the row's primary key, so a caller can look up the record of
     its own execution. The sink used to mint a uuid inside the INSERT and discard it, which made
     the id unreferenceable the moment it existed.
  3. **Best-effort never means silent, and never means fragile.** A broken sink must not change the
     answer by one byte, must not be indistinguishable from a working one, and must not be able to
     break a query merely by failing to OPEN — the bug that shipped when `Store.from_env()` sat
     outside its try.

Both surfaces are driven for real — a `python -m mcp_harness` subprocess speaking JSON-RPC on stdin,
and `TestClient` over `create_app()`'s `/mcp` — rather than by calling the handler, because "the row
is written on both surfaces" is a claim about the transports, and the two run different execution
paths (the stdio default forks `python -m execute_sql`; `create_app`'s default adapters carry the
built-in executor and run in-process).
"""

from __future__ import annotations

import json
import logging
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

BASE_URL = "https://your-host.example.com"
SIGNING_SECRET = "x" * 40
PROFILE = "acme"
# The caller's own framing of the question, passed on every tool call this file makes. The audit
# row's `question` and `datasource` columns are the two a reviewer of a refusal reaches for first —
# "who asked what, against which datasource" — so every assertion here names a concrete value rather
# than accepting whatever the writer happened to pass.
QUESTION = "how many orders"


@pytest.fixture(autouse=True)
def _isolate():
    """`_INJECTED_EXECUTOR` is a process global and `_max_rows_override` a ContextVar; a test that
    injects an executor (or that `create_app` injects one for) must not leak it into the next."""
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """A one-table model on disk: `orders`, with columns `id` and `amount`.

    `amount` is declared here but deliberately ABSENT from the warehouse the fixture creates. That
    is how a statement gets past every gate and is then rejected by the database — the `failed`
    branch, reached for real rather than by stubbing the executor.
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
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "amount", "type": "integer"},
            ],
        })
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A complete single-datasource install: an app database to audit into, a semantic model on
    disk, and a real (sqlite) warehouse to execute against."""
    app_db = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()

    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.executemany("INSERT INTO orders (id) VALUES (?)", [(1,), (2,)])
    con.commit()
    con.close()
    dsn = f"sqlite:///{warehouse}"

    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", dsn)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    return SimpleNamespace(app_db=app_db, artifacts=artifacts, dsn=dsn)


def _rows(url: str) -> list[dict]:
    store = Store.connect(url)
    try:
        return store.query(
            "SELECT id, datasource, question, sql, sql_truncated, row_count, source, status, "
            "reason, rule FROM query_executions"
        )
    finally:
        store.close()


def _ids(url: str) -> set[str]:
    return {r["id"] for r in _rows(url)}


# ---------------------------------------------------------------------------
# The two transports, driven for real
# ---------------------------------------------------------------------------


def _stdio_execute_sql(sql: str) -> dict:
    """`python -m mcp_harness` over JSON-RPC on stdin — the transport Claude Desktop launches.

    The child inherits the fixture's environment, so it resolves the same app database, the same
    model and the same warehouse; nothing is stubbed on this path, including the executor fork the
    server itself performs.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "execute_sql", "arguments": {
             "sql": sql, "datasource": PROFILE, "raw_query": QUESTION}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=180, env={**os.environ},
    )
    replies = {
        m.get("id"): m
        for m in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }
    assert 2 in replies, proc.stderr
    return json.loads(replies[2]["result"]["content"][0]["text"])


def _http_execute_sql(sql: str) -> dict:
    """The authenticated HTTP transport: `TestClient` over `create_app()`'s `/mcp`.

    `create_app()`'s default adapters carry the built-in executor, so this surface runs execution
    IN-PROCESS — the other of the two execution paths. Same tool, same serializer, same audit write.

    That in-process coverage is a CONSEQUENCE of a default, not a property of this test, so the
    default is asserted rather than assumed: if `default_adapters` ever stopped carrying an
    executor, every test in this file would quietly start exercising the fork path a second time and
    all of them would still pass, leaving the in-process serializer unaudited with nothing to say
    so. (`tests/test_ace035_no_enumeration.py` gets the same coverage by driving the two routes
    explicitly; here the transports are the subject, so the assertion is the cheaper version.)
    """
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app()) as client:
        assert tools._INJECTED_EXECUTOR is not None, (
            "create_app() no longer injects an executor, so this surface is now the fork path — "
            "the in-process path this file believes it covers is uncovered"
        )
        init = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post("/mcp", headers=headers2,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = client.post("/mcp", headers=headers2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": {
                "sql": sql, "datasource": PROFILE, "raw_query": QUESTION}}})
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


# Each vector is one status, produced by a real statement against the fixture's model + warehouse:
#   ok             — a declared column that exists in the warehouse
#   refused (deep) — an undeclared column, which the column-scope gate stops after the model loads
#   refused (fast) — a mutation, stopped by the read-only gate at the tool edge before anything else
#   failed         — a column the MODEL declares but the warehouse does not have, so the database
#                    rejects it
# The two refusals are both here because they leave the tool edge from different places: the deep one
# comes back from `execute_guarded` (or, on the fork, from the child's stderr), the fast one never
# gets that far. They are the pair that used to disagree about what landed in the audit row.
_STATUSES = [
    ("ok", "SELECT id FROM orders", "ok", None),
    ("refused-column-scope", "SELECT nope FROM orders", "refused", guardrail.RULE_COLUMN_SCOPE),
    ("refused-read-only", "DELETE FROM orders", "refused", guardrail.RULE_READ_ONLY),
    # A third refusal, from a third place again: the recon gate sits at the chokepoint between the
    # other two, after the read-only gate and before the model pass, and it is the only refusal that
    # is NOT skippable by `--no-safety` yet is also not a write. Its row is what proves a refusal is
    # audited on the strength of being a refusal rather than of which gate produced it.
    ("refused-recon", "SELECT id, version() FROM orders", "refused", guardrail.RULE_RECON),
    ("failed", "SELECT amount FROM orders", "failed", None),
]
_STATUS_IDS = [label for label, _, _, _ in _STATUSES]


def _assert_recorded(url: str, body: dict, status: str, rule: str | None) -> None:
    """One row, whose primary key IS the `audit_id` the caller was handed, carrying the verdict —
    and carrying WHO asked WHAT against WHICH datasource.

    `datasource` and `question` are asserted here because `_rows` has always selected them and
    nothing ever checked them: the fork path (the stdio default) was passing neither on its refused
    and failed branches, so those rows landed with `datasource=''` and `question=NULL` while the
    identical outcome in-process recorded both. A refusal whose row cannot say which datasource it
    was aimed at is the one row shape a reviewer cannot use.
    """
    rows = _rows(url)
    assert len(rows) == 1, rows
    (row,) = rows
    assert body["status"] == status
    # The criterion's actual verb: the id on the answer EQUALS the id of the row recording it.
    assert row["id"] == body["audit_id"]
    assert row["status"] == status
    assert row["datasource"] == PROFILE
    assert row["question"] == QUESTION
    if rule is None:
        # Only a refusal is a decision of ours, so only a refusal has a rule to record. An `ok` or a
        # `failed` row leaving these set would mean the columns had drifted into free text.
        assert row["reason"] is None and row["rule"] is None
    else:
        # Asserted against the guardrail symbols, not against string literals: renaming a rule must
        # break the code that names it, not silently change what the audit trail says.
        assert row["rule"] == rule == body["refusal"]["rule"]
        assert row["reason"] == guardrail.REASON_FOR_RULE[rule] == body["refusal"]["reason"]


@pytest.mark.parametrize(("label", "sql", "status", "rule"), _STATUSES, ids=_STATUS_IDS)
def test_the_stdio_surface_records_a_row_for_every_status(env, label, sql, status, rule):
    _assert_recorded(env.app_db, _stdio_execute_sql(sql), status, rule)


@pytest.mark.parametrize(("label", "sql", "status", "rule"), _STATUSES, ids=_STATUS_IDS)
def test_the_http_surface_records_a_row_for_every_status(env, label, sql, status, rule):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("jwt")
    _assert_recorded(env.app_db, _http_execute_sql(sql), status, rule)


# ---------------------------------------------------------------------------
# Exactly one row per call — the regression guard for a future seventh return site
# ---------------------------------------------------------------------------


def test_exactly_one_row_per_call_on_every_branch(env, monkeypatch):
    """Every branch of `tool_execute_sql` writes ONE row, and it is the row that call's answer names.

    Six `_emit` call sites reach the serializer today. The write lives at the single point they all
    pass through, so this stays true for a seventh — but only if that seventh actually goes through
    `_emit`. Asserting the id SET grows by exactly the returned `audit_id` catches all three ways to
    get this wrong at once: no row, two rows, or a row keyed by an id nobody was given.
    """
    def _call(**args):
        return json.loads(tools.tool_execute_sql(args))

    branches = [
        # The argument-validation fast-fail: no profile resolved, no statement to speak of.
        ("malformed argument", lambda: _call(sql="   ")),
        # The read-only fast-fail at the tool edge, before a profile or a fork.
        ("read-only fast-fail", lambda: _call(sql="DELETE FROM orders", datasource=PROFILE)),
        # The forked executor, all three of its outcomes.
        ("fork / ok", lambda: _call(sql="SELECT id FROM orders", datasource=PROFILE)),
        ("fork / refused", lambda: _call(sql="SELECT nope FROM orders", datasource=PROFILE)),
        ("fork / failed", lambda: _call(sql="SELECT amount FROM orders", datasource=PROFILE)),
    ]
    for label, run in branches:
        before = _ids(env.app_db)
        body = run()
        assert _ids(env.app_db) - before == {body["audit_id"]}, label

    # ...and the in-process path, which returns through the same serializer with no fork at all.
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    for label, run in [
        ("in-process / ok", lambda: _call(sql="SELECT id FROM orders", datasource=PROFILE)),
        ("in-process / failed", lambda: _call(sql="SELECT amount FROM orders", datasource=PROFILE)),
    ]:
        before = _ids(env.app_db)
        body = run()
        assert _ids(env.app_db) - before == {body["audit_id"]}, label


@pytest.mark.parametrize(("label", "sql", "status"), [
    ("ok", "SELECT id FROM orders", "ok"),
    ("refused", "SELECT nope FROM orders", "refused"),
    ("failed", "SELECT amount FROM orders", "failed"),
], ids=["ok", "refused", "failed"])
def test_the_audit_row_carries_the_scoped_call_source(env, label, sql, status):
    """The recorded `source` is the one scoped for the call, on every outcome.

    AH-022 gave this row its provenance: it reads `current_call_source()`, so an embedder that
    dispatches handlers itself is distinguishable from transport traffic. That read lived in
    `_finalize_execution`; this spec moved the write to `_emit`, and a move is exactly where a
    hard-coded default creeps back in — the new writer only has to *say* `"mcp_server"` to look right.

    Scoped via the ContextVar is the only route this row has. `record_tool_call` also accepts an
    explicit `source=` that outranks the ContextVar, and this writer has no such parameter — so an
    embedder that passes the argument instead of scoping gets two rows that disagree. That asymmetry
    is AH-022's and predates this spec (it is on `main` verbatim); it is named here so the parity this
    test does pin is not mistaken for the stronger guarantee.

    Nothing else catches that. AH-022's own provenance test builds the record inline and stubs
    `_record_query`, so it pins the ContextVar's behaviour rather than the production write, and it
    passes whether or not the writer consults it. Verified: restoring the literal here leaves the
    whole suite green.

    Asserted on all three statuses because `_record_execution` runs on all three, and a refusal is
    the row a reviewer most needs to attribute to a caller.
    """
    scoped = "embedded"
    assert scoped != tools.DEFAULT_CALL_SOURCE  # or the assertion below proves nothing
    token = tools.set_call_source(scoped)
    try:
        body = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE,
                                                  "raw_query": QUESTION}))
    finally:
        tools.reset_call_source(token)
    assert body["status"] == status
    (row,) = [r for r in _rows(env.app_db) if r["id"] == body["audit_id"]]
    assert row["source"] == scoped

    # ...and unscoped it is the value it has always been, so the seam is opt-in rather than a
    # rename of the default.
    plain = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE,
                                               "raw_query": QUESTION}))
    (row,) = [r for r in _rows(env.app_db) if r["id"] == plain["audit_id"]]
    assert row["source"] == tools.DEFAULT_CALL_SOURCE


def test_the_forked_child_writes_no_row_of_its_own(env):
    """One query, one row — the child's id is neither published nor recorded.

    `execute_sql._envelope` mints an `audit_id` in the forked child too, and keeps it off the wire so
    the parent's is the one a caller sees. If the child ALSO wrote a row, every forked query would
    leave an orphan the caller could never name. Proved twice: one row after a forked query, and no
    row at all when the executor is run directly against the same configured database.
    """
    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))
    assert body["status"] == "ok"
    assert _ids(env.app_db) == {body["audit_id"]}

    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", PROFILE,
         "--sql", "SELECT id FROM orders"],
        capture_output=True, text=True, timeout=180, env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr
    assert _ids(env.app_db) == {body["audit_id"]}  # unchanged: the executor audits nothing


# ---------------------------------------------------------------------------
# `execute_guarded` is TOTAL — an escaping exception skips the chokepoint AND the audit row
# ---------------------------------------------------------------------------


class _ReturnsNothing:
    """A broken adapter: satisfies `ports.Executor` by shape, returns the wrong type."""

    def execute(self, vetted_sql, creds, *, profile):
        return None


class _RaisesItsOwnError(Exception):
    """Stand-in for a consumer's own exception type — a pooled executor's `PoolError`, an RBAC
    denial, a tunnel failure. `ports.Executor` never said an adapter may only raise
    `ExecutorError`, so this is a shape a hosted consumer is entitled to produce."""


class _RaisesForeign:
    def execute(self, vetted_sql, creds, *, profile):
        raise _RaisesItsOwnError("pool exhausted after 30s waiting for a connection")


def _break_model_safety(monkeypatch, _env):
    def _boom(sql, profile, area):
        raise AttributeError("'NoneType' object has no attribute 'tables'")

    monkeypatch.setattr(execute_sql, "_model_safety", _boom)
    return execute_sql.BUILTIN_EXECUTOR


def _break_credentials_file(monkeypatch, env):
    """A credentials file `configparser` cannot read — the empirically-confirmed traceback source.

    `MissingSectionHeaderError` is raised by `cfg.read(...)` inside `_load_credentials`, which is
    inside the try but was not an `ExecutorError`, so it escaped. Its message embeds the absolute
    path of the file, and the traceback embeds several more.

    Arranged on DISK and in the ENVIRONMENT rather than by monkeypatching a module attribute, which
    is what makes it the one vector that also reaches a forked child: the child re-resolves
    `CREDENTIALS_PATH` from `AGAMI_ARTIFACTS_DIR` and inherits the deleted DSN vars, so it hits the
    same unreadable file. (`execute_sql.CREDENTIALS_PATH` is still patched for the in-process route,
    because that module global was resolved at import time, before the fixture set the artifacts dir.)
    """
    import agami_paths

    creds = agami_paths.credentials_path()
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("this line has no [section] header\nuser = someone\n")
    creds.chmod(0o600)  # `_load_credentials` refuses a world-readable file before parsing it
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", creds)
    monkeypatch.delenv("DATASOURCE_URL__ACME", raising=False)  # force the file path
    monkeypatch.delenv("DATASOURCE_URL", raising=False)
    return execute_sql.BUILTIN_EXECUTOR


def _executor_returns_none(monkeypatch, _env):
    return _ReturnsNothing()


def _executor_raises_foreign(monkeypatch, _env):
    return _RaisesForeign()


# Each vector: a label, how to arrange the break, and the execution routes it can reach.
#
# `fork` is not an optional extra. `Failure.kind` crosses that boundary as an EXIT CODE and is
# rebuilt from it, and while the catch-all `other` had no code of its own it fell to the generic 2 —
# which the parent read back as `dsn`. So an internal break was reported to the caller as a
# datasource-configuration problem, on the default transport only, while the identical break
# in-process said `other`. Driving the vectors in-process alone is precisely why that survived.
#
# Two vectors are in-process ONLY, and not by omission: they inject a `ports.Executor`, and the fork
# path never consults one — it runs `python -m execute_sql`, whose executor is the built-in. There is
# no fork analog of "the adapter a consumer supplied misbehaved". `model_safety raises` is
# in-process only for a duller reason: it is a monkeypatch of a module attribute, which a child
# process does not inherit, and adding a production hook to reach it would be a worse trade than the
# coverage is worth. `unreadable credentials file` is arranged on disk, so it crosses — and it is the
# vector that actually produced the drift.
_ESCAPING = [
    ("model_safety raises", _break_model_safety, ("in_process",)),
    ("unreadable credentials file", _break_credentials_file, ("in_process", "fork")),
    ("executor returns None", _executor_returns_none, ("in_process",)),
    ("executor raises a foreign type", _executor_raises_foreign, ("in_process",)),
]
_ESCAPING_MATRIX = [
    (label, arrange, route) for label, arrange, routes in _ESCAPING for route in routes
]
_ESCAPING_IDS = [f"{label} / {route}" for label, _, route in _ESCAPING_MATRIX]


def _arrange_for_route(monkeypatch, env, arrange, route: str):
    """Apply a vector's break and put the tool edge on `route`.

    Every vector's `arrange` returns the executor its in-process form needs; the fork route drops it
    and clears the injection, which is what makes `tool_execute_sql` fork `python -m execute_sql`.
    """
    executor = arrange(monkeypatch, env)
    tools.set_injected_executor(executor if route == "in_process" else None)


def test_the_escaping_matrix_drives_both_routes(env):
    """The matrix is the claim. An `_ESCAPING` table that quietly lost its only `fork` row would run
    green while re-opening the exact hole above, so the route coverage is asserted, not assumed."""
    routes = {route for _, _, route in _ESCAPING_MATRIX}
    assert routes == {"in_process", "fork"}
    # Every vector must at least run in-process; `fork` is the one that can be legitimately absent.
    assert all("in_process" in routes_for for _, _, routes_for in _ESCAPING)


@pytest.mark.parametrize(("label", "arrange", "route"), _ESCAPING_MATRIX, ids=_ESCAPING_IDS)
def test_an_unanticipated_break_is_a_failed_envelope_with_an_audit_row(env, monkeypatch, label,
                                                                      arrange, route):
    """Four ways `execute_guarded` used to raise instead of returning, and what each cost.

    All four propagated out of the chokepoint: `_model_safety` sat outside the try entirely, the
    credentials file raises `configparser.Error` where only `ExecutorError` was caught, an injected
    executor's own exception type was never anticipated, and an executor returning `None` made
    `Envelope.__post_init__` raise on the way out. `tools._run_in_process` catches only `SystemExit`,
    so each escaped `tool_execute_sql` — and because the audit row is written by the serializer the
    exception skipped, the trail recorded nothing at all (verified: rows before and after, 0 and 0).

    The hosted path is where this matters most. `ports.Executor` exists so a consumer can inject a
    pooled / per-user-RBAC / SSH-tunnel executor, and nothing in its contract said it may only raise
    `ExecutorError` — so a pooled executor's `PoolError` walked straight past the chokepoint.

    `kind == "other"` is asserted on BOTH routes, which is the newer half. The fork rebuilds the kind
    from the child's exit code, and until `other` had a code of its own it was read back as `dsn`:
    the same break, reported as a datasource-configuration problem on one transport and as an
    internal error on the other.

    Asserted together, because they are one property: the caller gets a `failed` Envelope, and
    exactly one row lands, keyed by the id that Envelope carried.
    """
    _arrange_for_route(monkeypatch, env, arrange, route)

    before = _ids(env.app_db)
    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))

    assert body["status"] == "failed", body
    assert body["failure"]["kind"] == "other", body
    assert _ids(env.app_db) - before == {body["audit_id"]}, label
    (row,) = [r for r in _rows(env.app_db) if r["id"] == body["audit_id"]]
    assert row["status"] == "failed" and row["reason"] is None and row["rule"] is None


@pytest.mark.parametrize(("label", "arrange", "route"), _ESCAPING_MATRIX, ids=_ESCAPING_IDS)
def test_an_unanticipated_break_tells_the_caller_nothing(env, monkeypatch, label, arrange, route):
    """The generic message is not politeness, it is the containment.

    Nobody has read the text of an exception nobody anticipated. `MissingSectionHeaderError` alone
    carries the absolute path of the credentials file, and the traceback carries the path of every
    frame — which is what used to be relayed verbatim into `failure.message`, a field the caller is
    shown. So the caller gets the one fixed string, whichever route produced the break: in-process
    the chokepoint authors it, and across the fork the parent relays the child's copy of the same
    string (and would substitute its own for anything carrying a traceback).
    """
    _arrange_for_route(monkeypatch, env, arrange, route)

    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))

    assert body["failure"]["message"] == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    serialized = json.dumps(body)
    assert "Traceback" not in serialized
    assert str(env.artifacts) not in serialized  # no absolute path from any frame or message


@pytest.mark.parametrize(
    ("label", "arrange"),
    [(label, arrange) for label, arrange, _ in _ESCAPING],
    ids=[label for label, _, _ in _ESCAPING],
)
def test_an_unanticipated_break_tells_the_log_everything(env, monkeypatch, caplog, label, arrange):
    """The other half of the containment: what the caller is not told, an operator must be.

    In-process only, and that is the whole scope of the claim rather than a gap — `caplog` observes
    THIS process's logger, and on the fork route the break happens in the child, whose log is its own
    stderr. What the parent does with that stderr is a different property, pinned by
    `_child_failure_message`'s tests and by the sibling test above.
    """
    tools.set_injected_executor(arrange(monkeypatch, env))

    with caplog.at_level(logging.ERROR):
        json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                           "datasource": PROFILE}))

    logged = [r for r in caplog.records if r.name == "execute_sql" and r.levelname == "ERROR"]
    assert len(logged) == 1, [r.getMessage() for r in logged]
    assert logged[0].exc_info is not None  # the cause, not merely the fact


def test_a_process_teardown_still_escapes(env, monkeypatch):
    """`SystemExit` and `KeyboardInterrupt` are not `Exception` and must stay that way.

    A process being torn down is not a query outcome to report, and swallowing it into a `failed`
    Envelope would make Ctrl-C and a supervisor's shutdown look like a database problem. The
    catch-all above therefore has to be `except Exception`, never a bare `except`. (The narrower
    `SystemExit` net one layer up in `tools._run_in_process` is a different case — it is scoped to a
    driver's own deep `sys.exit` and is pinned by
    `tests/test_ah012_executor_seam.py::test_injected_executor_systemexit_is_caught_not_fatal`.)
    """
    class _Interrupted:
        def execute(self, vetted_sql, creds, *, profile):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute_sql.execute_guarded("SELECT id FROM orders", PROFILE, None,
                                    executor=_Interrupted())


# ---------------------------------------------------------------------------
# A broken sink: never changes the answer, never passes silently
# ---------------------------------------------------------------------------


def _refused_envelope() -> guardrail.Envelope:
    """A fixed Envelope with a fixed `audit_id`, so two `_emit` calls are byte-comparable."""
    return guardrail.Envelope(
        status="refused",
        refusal=guardrail.refuse(
            guardrail.RULE_READ_ONLY,
            detail="only a single read statement is allowed",
            remediation="Rewrite it as a SELECT.",
        ),
        audit_id="c0ffee00c0ffee00c0ffee00c0ffee00",
    )


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "tools"]


def test_a_sink_whose_write_raises_changes_nothing_and_says_so(env, caplog, monkeypatch):
    """The INSERT fails. The caller must not be able to tell from the answer — and an operator must."""
    healthy = tools._emit(_refused_envelope(), sql="DELETE FROM orders", execution_ms=None)
    assert len(_rows(env.app_db)) == 1

    import model_store

    def _boom(self, record):
        raise RuntimeError("the audit table is unreachable")

    monkeypatch.setattr(model_store.DbActivitySink, "record_query_execution", _boom)

    with caplog.at_level(logging.WARNING):
        broken = tools._emit(_refused_envelope(), sql="DELETE FROM orders", execution_ms=None)

    assert broken == healthy  # byte-identical answer
    assert len(_rows(env.app_db)) == 1  # nothing new landed
    assert [r.levelname for r in _warnings(caplog)] == ["WARNING"]
    assert _warnings(caplog)[0].exc_info is not None  # the cause is in the log, not just the fact


def test_a_store_that_cannot_even_be_opened_changes_nothing_and_says_so(env, caplog, monkeypatch):
    """The failure that used to escape: `Store.from_env()` raising.

    It sat OUTSIDE its try, so a malformed DSN or an uninstalled driver broke every query the server
    logged rather than every log it wrote — on the success path, where the answer was already
    computed. This is the test that proves the outer try covers store CONSTRUCTION, not just the
    write; the sibling test above would still pass with the old shape.
    """
    healthy = tools._emit(_refused_envelope(), sql="DELETE FROM orders", execution_ms=None)
    assert len(_rows(env.app_db)) == 1

    import store as store_module

    def _boom(cls):
        raise RuntimeError("unsupported AGAMI_DB_URL scheme")

    monkeypatch.setattr(store_module.Store, "from_env", classmethod(_boom))

    with caplog.at_level(logging.WARNING):
        broken = tools._emit(_refused_envelope(), sql="DELETE FROM orders", execution_ms=None)

    assert broken == healthy
    assert len(_rows(env.app_db)) == 1
    assert [r.levelname for r in _warnings(caplog)] == ["WARNING"]


# ---------------------------------------------------------------------------
# The timeout split — by who decided it
# ---------------------------------------------------------------------------


def test_killing_an_unresponsive_executor_is_a_failure_we_cannot_attribute(env, monkeypatch):
    """The supervisor bound produces `failed` / `timeout`, and the contract is what settles it.

    Guardrail contract §3, verbatim: *"The rule is about a per-statement timeout. The subprocess
    supervisor that kills an executor which never returned is also ours, but it cannot attribute the
    kill to the statement — the child may have hung in connect, credential resolution or model load,
    where 'narrow the query' is the wrong fix — so an unresponsive executor is `failed`."*

    So "whose decision was it?" is not the test on its own. Ours, yes — but a refusal must name a
    fix, and the only fix this branch could name ("narrow the time range, add a filter, aggregate")
    asserts something we did not observe: that the STATEMENT is what ran long. Point it at a child
    that hung resolving credentials and it is advice about the wrong subject, delivered with the
    authority of a guardrail decision. `failed` / `timeout` claims exactly what we know — we stopped
    waiting — and the value-free message says only that.

    `RULE_RESOURCE_LIMIT` belongs to the per-statement bound the executor now imposes, which IS a
    refusal because its subject is the statement. That the rule finally has a producer is precisely
    why this branch is worth holding: the two bounds now coexist, and the one that cannot say what it
    stopped must not drift into borrowing the vocabulary of the one that can.
    """
    def _timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 240))

    monkeypatch.setattr(tools.subprocess, "run", _timed_out)

    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE, "raw_query": QUESTION}))

    assert body["status"] == "failed"
    assert body["failure"]["kind"] == "timeout"
    assert "refusal" not in body
    # Value-free: it names no table, no column and no bound of the caller's statement, and it does
    # not pretend to know what the child was doing.
    message = body["failure"]["message"]
    assert "orders" not in message and "SELECT" not in message
    (row,) = _rows(env.app_db)
    assert row["id"] == body["audit_id"]
    assert row["status"] == "failed" and row["reason"] is None and row["rule"] is None
    # The row still says which datasource and which question — the branch used to pass neither.
    assert row["datasource"] == PROFILE and row["question"] == QUESTION


def test_the_supervisor_kill_does_not_borrow_the_resource_limit_refusal(env, monkeypatch):
    """The supervisor kill is where `RULE_RESOURCE_LIMIT` would creep in — so drive that branch and
    assert the rule is absent from what the caller gets, and from the row recording it.

    Renamed rather than retired: the rule DOES have a producer now (the executor's per-statement
    deadline), so "no gate produces it" is no longer the claim. The claim that survives is the sharper
    half and always was — a branch must not borrow this rule merely because the word "timeout" fits.
    The pin is on `REASON_FOR_RULE`, not on the absence, which is why this test needed no weakening
    when the producer landed: it asserts what this branch says, not what the contract contains.
    """
    assert guardrail.RULE_RESOURCE_LIMIT in guardrail.REASON_FOR_RULE

    def _timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 240))

    monkeypatch.setattr(tools.subprocess, "run", _timed_out)
    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))

    assert guardrail.RULE_RESOURCE_LIMIT not in json.dumps(body)
    (row,) = _rows(env.app_db)
    assert row["rule"] is None


def test_a_driver_timeout_is_the_databases_and_stays_a_failure(env):
    """The other side of the split, and it lands on a DIFFERENT kind.

    A connect/network timeout is the database's outcome rather than ours, and it arrives as the
    connect failure the executor already classifies — `auth`, exit 4 — because that is what the
    driver raises. So the two timeouts a deployment actually sees are distinguishable in the audit
    trail: `timeout` means we stopped waiting on our own child, `auth` means the connection did.
    """
    class _TimedOutDriver:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("SQLite connect failed: connection timed out", code=4)

    tools.set_injected_executor(_TimedOutDriver())
    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))

    assert body["status"] == "failed"
    assert body["failure"]["kind"] == "auth"
    (row,) = _rows(env.app_db)
    assert row["id"] == body["audit_id"]
    assert row["status"] == "failed" and row["reason"] is None and row["rule"] is None


# ---------------------------------------------------------------------------
# The stored statement is bounded — a refusal must not be a way to grow the store
# ---------------------------------------------------------------------------


def test_an_oversized_statement_is_refused_and_stored_bounded(env):
    """The cost of recording refusals, paid down.

    Before refusals were audited they wrote no row at all, so the size of the statement a caller
    sent did not matter. Now the read-only fast-fail records it — and the guard refuses a statement
    for BEING oversized, which means the one statement guaranteed to be enormous is also guaranteed
    to be written. An authenticated caller could grow the audit store without ever reaching the
    warehouse.

    The verdict columns are what make the row worth keeping, so those are unchanged; the blob is
    bounded, and `sql_truncated` says so, because a cut statement that does not admit it reads as the
    whole one.
    """
    oversized = "SELECT id FROM orders WHERE id IN (" + ",".join(["1"] * 250_000) + ")"
    assert len(oversized) > 500_000

    body = json.loads(tools.tool_execute_sql({"sql": oversized, "datasource": PROFILE,
                                              "raw_query": QUESTION}))

    assert body["status"] == "refused"
    assert body["refusal"]["rule"] == guardrail.RULE_READ_ONLY
    (row,) = _rows(env.app_db)
    assert row["id"] == body["audit_id"]
    assert len(row["sql"]) == tools.AUDIT_SQL_MAX_CHARS
    assert row["sql"] == oversized[:tools.AUDIT_SQL_MAX_CHARS]  # a prefix, not a summary
    assert row["sql_truncated"]
    assert row["rule"] == guardrail.RULE_READ_ONLY and row["datasource"] == PROFILE


def test_a_normal_statement_is_stored_whole_and_says_it_was_not_cut(env):
    """The bound must not be silently rewriting ordinary rows: a statement under it is stored
    verbatim and flagged as untruncated, so `sql_truncated` distinguishes something rather than
    being true of everything."""
    sql = "SELECT id FROM orders"
    body = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE,
                                              "raw_query": QUESTION}))

    assert body["status"] == "ok"
    (row,) = _rows(env.app_db)
    assert row["sql"] == sql
    assert not row["sql_truncated"]


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


def test_the_audit_columns_apply_to_a_fresh_store_and_re_running_is_a_no_op(tmp_path):
    """`ALTER TABLE ADD COLUMN` is not idempotent on either backend, so re-run safety has to come
    from the runner's `schema_migrations` ledger. A second `run_migrations()` returning nothing is
    what proves the ledger — not the DDL — is carrying it.

    Both audit migrations are asserted together: 014 added the verdict columns, 015 added
    `sql_truncated`, and they share the property and the failure mode.
    """
    url = "sqlite://" + str(tmp_path / "fresh.db")
    store = Store.connect(url)
    try:
        applied = store.run_migrations()
        assert "014_query_executions_guardrail.sql" in applied
        assert "015_query_executions_sql_truncated.sql" in applied
        columns = {r["name"] for r in store.query("PRAGMA table_info(query_executions)")}
        assert {"status", "reason", "rule", "sql_truncated"} <= columns
        assert store.run_migrations() == []  # a reboot is a no-op, not a duplicate-column error
    finally:
        store.close()
