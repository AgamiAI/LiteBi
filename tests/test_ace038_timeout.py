"""Per-statement timeout — config resolution, the deadline primitive, and the refusal it produces.

`_resolve_timeout_s` answers "how long may one statement run", from `AGAMI_SQL_TIMEOUT_S`, the
`_timeout_override` ContextVar, and a 30s default; unlike the row cap it complains out loud when the
env value is present but unparseable. `_deadline` is the watchdog those seconds feed: it fires an
Event and calls a cancel callable when a block outlives its budget, and disarms cleanly when it does
not.

The second half of this file proves the whole contract end to end on ONE engine — SQLite, chosen
because it is in-process, needs no network, and `sqlite3.Connection.interrupt()` is a genuine cancel
rather than a polite request. A statement that outlives its budget is cancelled, unwinds on the
internal `_ResourceLimit` marker, and leaves `execute_guarded` as a `refused` Envelope carrying
`resource_limit` — with no partial data, a detail that quotes the configured budget, and a
remediation addressed to whoever can actually act on it.

**The classification is the FLAG, and only the flag.** A cancelled SQLite statement raises
`OperationalError("interrupted")`, so neither the error text nor the elapsed clock can be the test:
both are properties an ordinary database error can have by coincidence, and reading either one would
mean an unlucky query gets told to narrow itself when nothing timed out. `_deadline` sets its Event
*before* the cancel lands, so "did WE stop this?" is answerable without inference — and it is
asserted here in both directions.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402

# Short enough that the whole file stays sub-second, long enough that a loaded CI runner still
# schedules the timer thread before the assertion runs.
_TINY = 0.05


@pytest.fixture(autouse=True)
def _reset_override():
    # _timeout_override is a request-scoped ContextVar; isolate every test from it.
    execute_sql._timeout_override.set(None)
    yield
    execute_sql._timeout_override.set(None)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # The suite must not inherit an operator's real budget from the ambient environment.
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)


# --------------------------------------------------------------------------------------------
# _resolve_timeout_s
# --------------------------------------------------------------------------------------------


def test_an_absent_env_var_yields_the_default_budget():
    assert execute_sql._resolve_timeout_s() == 30
    assert execute_sql._DEFAULT_TIMEOUT_S == 30


@pytest.mark.parametrize("raw", ["1", "5", "45", "600"])
def test_a_valid_env_value_is_honoured(monkeypatch, raw):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    assert execute_sql._resolve_timeout_s() == int(raw)


def test_surrounding_whitespace_does_not_defeat_a_valid_value(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "  45  ")
    assert execute_sql._resolve_timeout_s() == 45


@pytest.mark.parametrize("raw", ["0", "00", "-5"])
def test_a_non_positive_value_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    assert execute_sql._resolve_timeout_s() == 30


@pytest.mark.parametrize("raw", ["6O", "30s", "45.5", "thirty", "1e3"])
def test_an_unparseable_value_falls_back_and_says_so(monkeypatch, caplog, raw):
    """The row cap falls back silently; this one must not. An operator who typed a capital O for a
    zero has to be able to find out why their budget is not what they configured."""
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        assert execute_sql._resolve_timeout_s() == 30

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, f"no warning emitted for the rejected value {raw!r}"
    assert any(raw in r.getMessage() for r in warnings), (
        f"the warning must name the rejected text {raw!r}; got {[r.getMessage() for r in warnings]}"
    )


@pytest.mark.parametrize("raw", ["", "45", "0", "-5"])
def test_a_parseable_or_absent_value_stays_quiet(monkeypatch, caplog, raw):
    """Only genuinely unparseable text warrants the warning. A deliberate `0` or `-5` is a value we
    understood and declined, not a typo, and warning on it would train operators to ignore the log."""
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        execute_sql._resolve_timeout_s()
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_context_var_takes_precedence_over_the_env(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "45")
    execute_sql._timeout_override.set(7)
    assert execute_sql._resolve_timeout_s() == 7


def test_the_context_var_takes_precedence_over_the_default():
    execute_sql._timeout_override.set(12)
    assert execute_sql._resolve_timeout_s() == 12


def test_the_context_var_may_raise_the_budget_as_well_as_lower_it(monkeypatch):
    """Unlike the row cap, which can only be tightened per call, the override wins outright."""
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "5")
    execute_sql._timeout_override.set(90)
    assert execute_sql._resolve_timeout_s() == 90


@pytest.mark.parametrize("override", [0, -1])
def test_a_non_positive_override_is_ignored(monkeypatch, override):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "45")
    execute_sql._timeout_override.set(override)
    assert execute_sql._resolve_timeout_s() == 45


# --------------------------------------------------------------------------------------------
# _deadline
# --------------------------------------------------------------------------------------------


class _RecordingCancel:
    """A stand-in for a driver's `cancel()`, recording the calls and the order relative to the flag."""

    def __init__(self, fails: bool = False):
        self.calls = 0
        self.fails = fails
        self.done = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        try:
            if self.fails:
                raise RuntimeError("driver refused to cancel from another thread")
        finally:
            self.done.set()


def test_an_overrunning_block_sets_the_event_and_cancels():
    cancel = _RecordingCancel()
    with execute_sql._deadline(cancel, _TINY) as fired:
        assert cancel.done.wait(2.0), "the watchdog never ran"
        # The flag must already be readable by the time the cancel lands, so a caller catching the
        # resulting driver error can attribute it to us rather than to the database.
        assert fired.is_set()
    assert cancel.calls == 1


def test_a_block_that_finishes_first_neither_fires_nor_cancels():
    cancel = _RecordingCancel()
    with execute_sql._deadline(cancel, 30) as fired:
        pass
    assert not fired.is_set()
    assert cancel.calls == 0


def test_the_timer_is_disarmed_on_exit_so_no_late_cancel_arrives():
    """The watchdog must not outlive its block: a cancel landing after the statement finished would
    kill whatever the connection is doing next."""
    cancel = _RecordingCancel()
    with execute_sql._deadline(cancel, _TINY) as fired:
        pass
    time.sleep(_TINY * 4)  # comfortably past when an un-disarmed timer would have fired
    assert cancel.calls == 0
    assert not fired.is_set()


def test_the_timer_is_disarmed_even_when_the_block_raises():
    cancel = _RecordingCancel()
    with pytest.raises(ValueError):
        with execute_sql._deadline(cancel, _TINY):
            raise ValueError("the statement blew up on its own")
    time.sleep(_TINY * 4)
    assert cancel.calls == 0


def test_a_cancel_that_raises_does_not_escape_the_timer_thread(caplog):
    """Some drivers raise when cancelled from a thread other than the one running the statement. That
    must be logged and swallowed: an exception escaping a timer thread is unhandleable by the caller
    and would be lost to threading's excepthook."""
    cancel = _RecordingCancel(fails=True)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        with execute_sql._deadline(cancel, _TINY) as fired:
            assert cancel.done.wait(2.0), "the watchdog never ran"
            time.sleep(_TINY)  # let `fire` finish handling the raised cancel before we leave

    assert fired.is_set()  # the timeout still counts as fired even though the cancel failed
    assert cancel.calls == 1
    assert any(
        r.levelno == logging.WARNING and "driver refused to cancel" in r.getMessage()
        for r in caplog.records
    ), f"the failed cancel was not logged; got {[r.getMessage() for r in caplog.records]}"


def test_the_watchdog_thread_is_a_daemon_and_does_not_hold_the_process_open():
    """A hung cancel must never keep the interpreter alive at shutdown."""
    live_before = {t.ident for t in threading.enumerate()}
    with execute_sql._deadline(_RecordingCancel(), 300) as fired:
        new = [t for t in threading.enumerate() if t.ident not in live_before]
        assert new, "no watchdog thread was started"
        assert all(t.daemon for t in new)
        assert not fired.is_set()


# --------------------------------------------------------------------------------------------
# _ResourceLimit
# --------------------------------------------------------------------------------------------


def test_the_resource_limit_marker_is_a_plain_exception():
    """It has to unwind an engine function like any other error so the transaction rolls back."""
    assert issubclass(execute_sql._ResourceLimit, Exception)
    with pytest.raises(execute_sql._ResourceLimit):
        raise execute_sql._ResourceLimit("statement exceeded its budget")


# --------------------------------------------------------------------------------------------
# The deadline, wired into SQLite — the refusal, end to end
# --------------------------------------------------------------------------------------------

PROFILE = "analytics"

# The smallest budget the resolver accepts, since it deals in whole seconds. Every end-to-end test
# below therefore costs about a second of wall clock, which is the price of driving a real cancel
# through a real driver rather than asserting on a stub.
_BUDGET_S = 1

# A recursive CTE with no physical table and no termination in reach: two billion iterations, which
# on this machine is minutes of pure CPU. Sized that way on purpose — "bounded rather than hanging"
# is only proved if the unbounded run would take far longer than the assertion allows.
#
# It also has to reach the executor, so it is written to pass every gate ahead of it: it opens with
# WITH…SELECT (read-only), names no physical table (`burn` is a CTE, which the table-scope gate
# excludes by construction), projects no star, and its one column binds to a CTE rather than to a
# declared table.
_RUNAWAY_SQL = (
    "WITH RECURSIVE burn(n) AS ("
    "SELECT 1 UNION ALL SELECT n + 1 FROM burn WHERE n < 2000000000"
    ") SELECT count(n) AS c FROM burn"
)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """A real SQLite warehouse reachable as profile `analytics`, and nothing else configured.

    `no_safety=True` on the direct calls below skips the semantic-model pass, so this fixture only
    has to satisfy credential resolution. The audit test further down needs the fuller install and
    builds it itself.
    """
    path = tmp_path / "warehouse.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.commit()
    con.close()
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{path}")
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    return path


def _guarded(sql: str) -> object:
    """`execute_guarded` over the built-in executor — the single chokepoint, driven directly."""
    return execute_sql.execute_guarded(
        sql, PROFILE, None, executor=execute_sql.BUILTIN_EXECUTOR, no_safety=True
    )


def test_a_runaway_statement_is_cancelled_rather_than_left_to_run(warehouse):
    """The headline: a statement that would run for minutes is stopped at its budget and comes back
    as a refusal naming the rule the contract reserves for a bound we imposed.

    The elapsed assertion is the one that would still fail if the deadline were never armed — without
    it a test that merely waited out the query would look identical and pass in several minutes.
    """
    execute_sql._timeout_override.set(_BUDGET_S)

    started = time.monotonic()
    env = _guarded(_RUNAWAY_SQL)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the statement ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    # Neither unsafe nor out of scope: we simply did not determine the answer within the bound.
    assert env.refusal.reason == "undetermined"
    assert env.refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT]


def test_a_cancelled_statement_yields_no_partial_data(warehouse):
    """Whatever rows the engine had gathered when the watchdog fired are not an answer.

    A truncated result presented as a result is the failure mode the bounded-fetch work already
    guards against on the row axis; on the time axis the answer is stronger — there is no data at
    all, and `Envelope.__post_init__` enforces that a refusal cannot carry any.
    """
    execute_sql._timeout_override.set(_BUDGET_S)

    env = _guarded(_RUNAWAY_SQL)

    assert env.status == "refused"
    assert env.data is None
    assert env.failure is None


class _ResourceLimitExecutor:
    """A `ports.Executor` that raises the internal marker, standing in for an engine whose watchdog
    fired. Used where the subject is the REFUSAL TEXT rather than the cancel, so the assertion does
    not have to pay a second of wall clock to reach it."""

    def execute(self, vetted_sql: str, creds: dict, *, profile: str):
        raise execute_sql._ResourceLimit("the statement outlived its per-statement budget")


def test_the_detail_quotes_the_configured_budget(warehouse):
    """A bound the caller cannot see is one it cannot plan around, so the number is in the detail.

    The configured value is not a data value: it is a deployment setting, and stating it discloses
    nothing about the database or its contents. Asserted against a distinctive budget rather than the
    default, so a hard-coded `30s` in the message cannot pass.
    """
    execute_sql._timeout_override.set(7)

    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_ResourceLimitExecutor(), no_safety=True,
    )

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert "7s" in env.refusal.detail, env.refusal.detail


def test_the_remediation_names_no_deployment_environment_variable(warehouse):
    """The remediation has to be addressed to whoever is reading it.

    On the served path that is an assistant holding a statement, with no shell, no deployment and no
    way to set an environment variable — so "raise AGAMI_SQL_TIMEOUT_S" is advice aimed past the
    caller at an operator who is not in the conversation, and it reads as a fix while being
    unfollowable. What is left has to be something that would make THIS statement executable.
    """
    execute_sql._timeout_override.set(_BUDGET_S)

    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_ResourceLimitExecutor(), no_safety=True,
    )

    authored = f"{env.refusal.detail} {env.refusal.remediation}"
    assert "AGAMI_SQL_TIMEOUT_S" not in authored, authored
    assert "AGAMI_" not in authored, authored  # no sibling deployment var either
    assert "environ" not in authored and "env var" not in authored.lower(), authored
    # And it is still actionable — it names something to change about the statement.
    assert env.refusal.remediation.strip()


# --------------------------------------------------------------------------------------------
# The clock covers the fetch, not just the execute
# --------------------------------------------------------------------------------------------


class _SlowFetchCursor:
    """A cursor whose `execute` returns at once and whose single `fetchmany` blocks until cancelled.

    That is the shape of a streaming / server-side cursor, where the scan happens on the PULL rather
    than on the call: `_collect_cursor` issues one `fetchmany(cap + 1)`, and on such a cursor that
    one call is the whole query. A deadline that stopped at `execute` would bound the cheap half.
    """

    def __init__(self, cancelled: threading.Event):
        self.description = [("c",)]
        self._cancelled = cancelled
        self.executed: str | None = None

    def execute(self, sql: str) -> None:
        self.executed = sql

    def fetchmany(self, n: int):
        # Bounded so a deadline that never reaches the fetch fails this test rather than hanging it.
        if not self._cancelled.wait(30):
            raise AssertionError("the fetch was never cancelled — the clock stopped at execute")
        raise sqlite3.OperationalError("interrupted")


class _FakeConnection:
    """The driver surface `_run_sqlite` uses: a cursor, a real `interrupt`, and a close."""

    def __init__(self, cursor_factory):
        self.cancelled = threading.Event()
        self.cursor_obj = cursor_factory(self.cancelled)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def interrupt(self) -> None:
        self.cancelled.set()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sqlite(monkeypatch):
    """Replace `sqlite3.connect` so a test can choose exactly where the time goes.

    A real slow query cannot separate execute from fetch — sqlite decides that — and driving one
    would also make these tests as slow as the thing they measure. `_run_sqlite` does its own
    `import sqlite3`, which resolves through `sys.modules`, so patching the module attribute is
    enough to reach it.
    """
    def _install(cursor_factory, *, connect_delay_s: float = 0.0):
        conn = _FakeConnection(cursor_factory)

        def _connect(path, *a, **kw):
            if connect_delay_s:
                time.sleep(connect_delay_s)
            return conn

        monkeypatch.setattr(sqlite3, "connect", _connect)
        return conn

    return _install


def test_a_slow_fetch_is_bounded_even_when_the_execute_returned_at_once(warehouse, fake_sqlite):
    """The clock covers the whole statement, fetch included — an explicit criterion, not a bonus.

    Bounding only `execute` would leave the common streaming shape unbounded: the driver returns
    immediately and the engine scans while the caller pulls. The cancel has to land on the fetch, and
    the refusal has to be the same one a slow execute produces.
    """
    execute_sql._timeout_override.set(_BUDGET_S)
    conn = fake_sqlite(_SlowFetchCursor)

    started = time.monotonic()
    env = _guarded("SELECT c FROM orders")
    elapsed = time.monotonic() - started

    assert conn.cursor_obj.executed == "SELECT c FROM orders"  # the execute really did run, and fast
    assert elapsed < 20, f"the fetch ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert conn.closed  # the connection is still released on the refusing path


# --------------------------------------------------------------------------------------------
# The classification is the flag alone — asserted in both directions
# --------------------------------------------------------------------------------------------
#
# `interrupt()` makes the in-flight statement raise `OperationalError("interrupted")`. That text is
# therefore NOT evidence of a timeout — a database is free to raise it for its own reasons, and an
# error that merely arrives late is not one we caused. Both vectors below carry that exact text with
# the flag unset, so anything keying on the message or on the clock would turn them green as
# refusals; only the flag distinguishes them.


class _ImmediateErrorCursor:
    """Raises the very error a cancel provokes, straight away and unprompted."""

    def __init__(self, cancelled: threading.Event):
        self.description = None
        self._cancelled = cancelled

    def execute(self, sql: str) -> None:
        raise sqlite3.OperationalError("interrupted")

    def fetchmany(self, n: int):  # pragma: no cover - execute raises first
        raise AssertionError("unreachable")


def test_a_database_error_with_the_flag_unset_is_a_failure_not_a_refusal(warehouse, fake_sqlite):
    """Direction (a): the watchdog never fired, so this is the database's outcome, not ours.

    A generous budget means the flag stays clear while the identical error text arrives. It must
    unwind as an `ExecutorError` and leave the chokepoint as `failed`/`syntax` — a refusal here would
    tell a caller to narrow a statement that never ran long at all.
    """
    execute_sql._timeout_override.set(300)
    fake_sqlite(_ImmediateErrorCursor)

    env = _guarded("SELECT c FROM orders")

    assert env.status == "failed"
    assert env.failure.kind == "syntax"
    assert env.refusal is None


@contextlib.contextmanager
def _deadline_that_never_fires(cancel, timeout_s):
    """A watchdog that does not fire, whatever the block does.

    Substituted so a block can genuinely outlive its budget with the flag clear — the one state a
    real `_deadline` cannot be put into, and the one that separates "the clock ran out" from "we
    cancelled it".
    """
    yield threading.Event()


class _LateErrorCursor:
    """Runs past the budget, then raises the error a cancel would have provoked."""

    def __init__(self, cancelled: threading.Event):
        self.description = None
        self._cancelled = cancelled

    def execute(self, sql: str) -> None:
        time.sleep(_BUDGET_S * 1.5)
        raise sqlite3.OperationalError("interrupted")

    def fetchmany(self, n: int):  # pragma: no cover - execute raises first
        raise AssertionError("unreachable")


def test_an_error_that_merely_arrives_late_is_still_a_failure(warehouse, fake_sqlite, monkeypatch):
    """Direction (b): elapsed time is not the classifier either.

    Everything a clock-based or text-based reading would need is present — the budget is exceeded and
    the message is literally "interrupted" — and the flag is clear. The verdict must still be
    `failed`, because we did not stop this statement; it stopped on its own, late.
    """
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_that_never_fires)
    execute_sql._timeout_override.set(_BUDGET_S)
    fake_sqlite(_LateErrorCursor)

    started = time.monotonic()
    env = _guarded("SELECT c FROM orders")
    elapsed = time.monotonic() - started

    assert elapsed > _BUDGET_S, "the statement did not actually outlive its budget"
    assert env.status == "failed"
    assert env.failure.kind == "syntax"
    assert env.refusal is None


# --------------------------------------------------------------------------------------------
# The refusal is recorded
# --------------------------------------------------------------------------------------------


@pytest.fixture
def audited(tmp_path, monkeypatch):
    """A complete single-datasource install: an app database to audit into, a semantic model on
    disk, and the same real warehouse.

    Fuller than the `warehouse` fixture because this one goes through the tool edge rather than
    straight to `execute_guarded`, so the model pass runs for real — and with an app database
    configured the executor reads that as the hosted signal and fails closed without a model.
    """
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")
    yaml = pytest.importorskip("yaml")
    from store import Store

    app_db = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()

    root = tmp_path / "artifacts" / PROFILE
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump(
        {"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump(
        {"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))

    path = tmp_path / "warehouse.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{path}")
    return SimpleNamespace(app_db=app_db)


def test_the_refusal_is_written_to_the_audit_trail(audited):
    """A decision we made against a caller's statement is exactly the row a reviewer comes looking
    for, so this outcome is audited like every other — and keyed by the id the caller was handed.

    Driven through the tool edge with the built-in executor injected, which is the in-process path
    the hosted server runs: one real cancel, one real serializer, one real sink.
    """
    import tools
    from store import Store

    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    execute_sql._timeout_override.set(_BUDGET_S)
    try:
        body = json.loads(tools.tool_execute_sql({"sql": _RUNAWAY_SQL, "datasource": PROFILE,
                                                  "raw_query": "how many"}))
    finally:
        tools.set_injected_executor(None)

    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == guardrail.RULE_RESOURCE_LIMIT, body

    store = Store.connect(audited.app_db)
    try:
        rows = store.query("SELECT id, status, reason, rule FROM query_executions")
    finally:
        store.close()

    assert len(rows) == 1, rows
    (row,) = rows
    assert row["id"] == body["audit_id"]  # the answer and its record name the same id
    assert row["status"] == "refused"
    assert row["rule"] == guardrail.RULE_RESOURCE_LIMIT
    assert row["reason"] == guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT]


# --------------------------------------------------------------------------------------------
# Every engine, under one table
# --------------------------------------------------------------------------------------------
#
# S2 proved the contract on SQLite. The other nine now run under the same deadline, and each names
# its OWN cancel — because there is no method a duck-typed probe could look for that is right
# everywhere. pymysql's connection has no `cancel()` at all and a probe would fall through to
# `close()`, which sends COM_QUIT down the socket the blocked statement owns; oracledb's connection
# DOES have `cancel()`, so a probe that tried `cancel` first would silently pick it for Oracle and
# never notice that Snowflake and Databricks put theirs on the cursor. The fakes below stand in for
# drivers this environment does not install, so the whole matrix runs on every machine.


def _module(name: str, **attrs: object):
    """A stand-in driver module. The engine functions do their own `import <driver>`, which resolves
    through `sys.modules`, so an entry there is enough to reach them."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class _RecordingCursor:
    """A DB-API cursor that logs every call, so a test can assert WHICH method a cancel reached."""

    def __init__(self, log: list, *, name: str | None = None, on_execute=None,
                 close_raises: bool = False):
        self.description = [("c",)]
        self.name = name
        self.itersize = 0
        self.sfqid = "01fake-0000-0000-0000-000000000000"  # Snowflake sets a query id at submission
        self._log = log
        self._on_execute = on_execute
        self._close_raises = close_raises

    def execute(self, sql: str, params=None) -> None:
        self._log.append(("execute", sql, params))
        # Only the caller's statement is armed to fail. Postgres runs `SET LOCAL statement_timeout`
        # on its own cursor first, and that one has to succeed — it runs before anything has gone
        # wrong, and a test about the timeout path must not accidentally break the setup for it.
        if self._on_execute is not None and not sql.startswith("SET "):
            raise self._on_execute()

    def fetchmany(self, n: int):
        self._log.append(("fetchmany", n))
        return [(1,)]

    def cancel(self) -> None:
        self._log.append(("cursor.cancel",))

    def abort_query(self, qid: str) -> bool:
        self._log.append(("cursor.abort_query", qid))
        return True

    def close(self) -> None:
        self._log.append(("cursor.close", self.name))
        if self._close_raises:
            raise RuntimeError("current transaction is aborted, commands ignored")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False


class _RecordingConnection:
    """Every cancel-shaped method a driver in this module might expose, each logging a DISTINCT
    marker — so asserting the log pins down exactly which one the engine chose."""

    def __init__(self, *, on_execute=None, named_close_raises: bool = False):
        self.log: list = []
        self.connect_kwargs: dict = {}
        self.job_config_kwargs: dict = {}
        self._on_execute = on_execute
        self._named_close_raises = named_close_raises
        self.cursors: list[_RecordingCursor] = []
        self.executed_cursor = None
        # pymssql's DB-API connection delegates its cancel to the `_mssql` connection it wraps.
        self._conn = SimpleNamespace(cancel=lambda: self.log.append(("mssql.cancel",)))

    def cursor(self, name: str | None = None, **kwargs):
        self.log.append(("cursor", name))
        cur = _RecordingCursor(
            self.log,
            name=name,
            on_execute=self._on_execute,
            close_raises=self._named_close_raises and name is not None,
        )
        self.cursors.append(cur)
        return cur

    # The per-driver cancels, each distinguishable in the log.
    def cancel(self) -> None:
        self.log.append(("conn.cancel",))

    def interrupt(self) -> None:
        self.log.append(("conn.interrupt",))

    def _force_close(self) -> None:
        self.log.append(("conn._force_close",))

    def kill(self, thread_id: int) -> None:  # pragma: no cover - present so a probe could find it
        self.log.append(("conn.kill",))

    def close(self) -> None:
        self.log.append(("conn.close",))

    # DuckDB's `execute` hands back the CONNECTION, so the connection is also a cursor there.
    def execute(self, sql: str, params=None):
        self.log.append(("execute", sql, params))
        self.executed_cursor = self
        if self._on_execute is not None:
            raise self._on_execute()
        return self

    @property
    def description(self):
        return [("c",)]

    def fetchmany(self, n: int):
        self.log.append(("fetchmany", n))
        return [(1,)]

    # `with conn` is the TRANSACTION on the Postgres path.
    def __enter__(self):
        self.log.append(("txn.enter",))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.log.append(("txn.exit", exc_type is not None))
        return False


class _FakeBigQueryResults:
    def __init__(self):
        self.schema = [SimpleNamespace(name="c")]

    def __iter__(self):
        return iter([(1,)])


def _install_simple(module_name: str, connect_attr: str = "connect"):
    """Installer for a driver reached as `import <module>` + `<module>.connect(...)`."""
    def _install(monkeypatch, *, on_execute=None, named_close_raises=False):
        conn = _RecordingConnection(on_execute=on_execute, named_close_raises=named_close_raises)

        def _connect(*args, **kwargs):
            conn.connect_kwargs = kwargs
            return conn

        monkeypatch.setitem(sys.modules, module_name, _module(module_name, **{connect_attr: _connect}))
        return conn

    return _install


def _install_sqlite(monkeypatch, *, on_execute=None, named_close_raises=False):
    conn = _RecordingConnection(on_execute=on_execute)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: conn)
    return conn


def _install_snowflake(monkeypatch, *, on_execute=None, named_close_raises=False):
    conn = _RecordingConnection(on_execute=on_execute)

    def _connect(**kwargs):
        conn.connect_kwargs = kwargs
        return conn

    connector = _module("snowflake.connector", connect=_connect)
    # `import snowflake.connector` short-circuits on `sys.modules` without the machinery ever setting
    # the attribute, so the parent module has to carry it explicitly.
    monkeypatch.setitem(sys.modules, "snowflake", _module("snowflake", connector=connector))
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    return conn


def _install_databricks(monkeypatch, *, on_execute=None, named_close_raises=False):
    conn = _RecordingConnection(on_execute=on_execute)

    def _connect(**kwargs):
        conn.connect_kwargs = kwargs
        return conn

    dbsql = _module("databricks.sql", connect=_connect)
    monkeypatch.setitem(sys.modules, "databricks", _module("databricks", sql=dbsql))
    monkeypatch.setitem(sys.modules, "databricks.sql", dbsql)
    return conn


def _install_trino(monkeypatch, *, on_execute=None, named_close_raises=False):
    conn = _RecordingConnection(on_execute=on_execute)

    def _connect(**kwargs):
        conn.connect_kwargs = kwargs
        return conn

    monkeypatch.setitem(
        sys.modules, "trino", _module("trino", dbapi=SimpleNamespace(connect=_connect)),
    )
    return conn


def _install_bigquery(monkeypatch, *, on_execute=None, named_close_raises=False):
    conn = _RecordingConnection(on_execute=on_execute)

    class _Job:
        def result(self, max_results=None):
            conn.log.append(("job.result", max_results))
            return _FakeBigQueryResults()

    class _Client:
        def __init__(self, **kwargs):
            conn.connect_kwargs = kwargs

        def query(self, sql, job_config=None):
            conn.log.append(("execute", sql, None))
            if on_execute is not None:
                raise on_execute()
            return _Job()

    def _job_config(**kwargs):
        conn.job_config_kwargs = kwargs
        return SimpleNamespace(**kwargs)

    bigquery = _module("google.cloud.bigquery", Client=_Client, QueryJobConfig=_job_config)
    oauth2 = _module("google.oauth2", service_account=_module("google.oauth2.service_account"))
    monkeypatch.setitem(sys.modules, "google", _module("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", _module("google.cloud", bigquery=bigquery))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", oauth2.service_account)
    return conn


class _EngineCase:
    """One engine's fake driver, its credentials, and the cancel it is required to reach."""

    def __init__(self, fn, install, creds: dict, cancel: str | None):
        self.fn = fn
        self.install = install
        self.creds = creds
        self.cancel = cancel  # the log marker the watchdog's cancel must produce; None = no watchdog

    def run(self, sql: str = "SELECT c FROM orders"):
        return self.fn(self.creds, sql)


_SQL_CREDS = {"host": "db.example", "port": "5432", "user": "u", "password": "p",
              "database": "shop"}

ENGINE_CASES = {
    "postgres": _EngineCase(
        execute_sql._run_postgres, _install_simple("psycopg2"), dict(_SQL_CREDS), "conn.cancel"),
    "mysql": _EngineCase(
        execute_sql._run_mysql, _install_simple("pymysql"), dict(_SQL_CREDS), "conn._force_close"),
    "snowflake": _EngineCase(
        execute_sql._run_snowflake, _install_snowflake,
        {"account": "acct", "user": "u", "password": "p"}, "cursor.abort_query"),
    "bigquery": _EngineCase(
        execute_sql._run_bigquery, _install_bigquery, {"project": "proj"}, None),
    "sqlite": _EngineCase(
        execute_sql._run_sqlite, _install_sqlite, {"path": "warehouse.db"}, "conn.interrupt"),
    "sqlserver": _EngineCase(
        execute_sql._run_sqlserver, _install_simple("pymssql"),
        {"host": "db.example", "user": "u", "password": "p"}, "mssql.cancel"),
    "oracle": _EngineCase(
        execute_sql._run_oracle, _install_simple("oracledb"),
        {"user": "u", "password": "p", "dsn": "db.example/shop"}, "conn.cancel"),
    "databricks": _EngineCase(
        execute_sql._run_databricks, _install_databricks,
        {"host": "db.example", "http_path": "/sql/1", "token": "t"}, "cursor.cancel"),
    "trino": _EngineCase(
        execute_sql._run_trino, _install_trino,
        {"host": "db.example", "user": "u"}, "cursor.cancel"),
    "duckdb": _EngineCase(
        execute_sql._run_duckdb, _install_simple("duckdb"), {"path": ":memory:"}, "conn.interrupt"),
}


def test_the_engine_table_covers_every_engine_this_module_has():
    """The guard that makes the table below a rule rather than a list. A new engine added without an
    `except _ResourceLimit: raise` would otherwise ship silently reporting its refusals as driver
    errors — this fails the moment a `_run_*` exists that the matrix does not name."""
    in_module = {n for n in dir(execute_sql) if n.startswith("_run_")}
    assert in_module == {case.fn.__name__ for case in ENGINE_CASES.values()}
    assert len(ENGINE_CASES) == 10


def _raise_marker():
    return execute_sql._ResourceLimit(execute_sql._OUTLIVED_BUDGET)


@pytest.mark.parametrize("engine", sorted(ENGINE_CASES))
def test_every_engine_re_raises_the_marker_instead_of_relabelling_it(engine, monkeypatch):
    """The highest-value assertion in the slice, and the one a copy-paste omission fails.

    Each engine ends in `except Exception as e: raise ExecutorError(..., code=5)`. Without an
    `except _ResourceLimit: raise` ahead of it, that catch-all swallows our own marker and the
    chokepoint reports a bound WE imposed as the database's failure — a `failed` envelope telling the
    caller their SQL is broken, when in fact it merely ran long.
    """
    case = ENGINE_CASES[engine]
    case.install(monkeypatch, on_execute=_raise_marker)

    with pytest.raises(execute_sql._ResourceLimit):
        case.run()


@pytest.mark.parametrize("engine", sorted(ENGINE_CASES))
def test_no_engine_mistakes_an_ordinary_driver_error_for_the_marker(engine, monkeypatch):
    """The other direction, on the same matrix: with the watchdog never fired, a driver error is the
    database's outcome and has to stay one. An engine that raised the marker here would tell every
    caller with a typo to narrow their query."""
    case = ENGINE_CASES[engine]
    case.install(monkeypatch, on_execute=lambda: RuntimeError("relation does not exist"))

    with pytest.raises(execute_sql.ExecutorError) as exc:
        case.run()
    assert exc.value.code == 5


@contextlib.contextmanager
def _deadline_already_fired(cancel, timeout_s):
    """A watchdog that has already fired by the time the block runs — the state a real one reaches
    only after a genuinely slow statement, reproduced here without paying for one."""
    fired = threading.Event()
    fired.set()
    yield fired


_WATCHDOG_ENGINES = sorted(e for e, c in ENGINE_CASES.items() if c.cancel is not None)


@pytest.mark.parametrize("engine", _WATCHDOG_ENGINES)
def test_a_driver_error_under_a_fired_watchdog_becomes_the_marker(engine, monkeypatch):
    """The conversion itself, on all nine engines that have a watchdog: the flag is set, so whatever
    the driver raised is the wreckage of OUR cancel and unwinds as the marker."""
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES[engine]
    case.install(monkeypatch, on_execute=lambda: RuntimeError("connection reset by peer"))

    with pytest.raises(execute_sql._ResourceLimit):
        case.run()


@pytest.mark.parametrize("engine", _WATCHDOG_ENGINES)
def test_a_cancel_that_lands_without_raising_is_still_a_refusal(engine, monkeypatch):
    """A cancel can land between the execute and the fetch, or just as the fetch returns, and leave
    nothing to raise. The budget still elapsed, so the outcome is still a refusal rather than a
    result gathered past it — which is why every engine re-reads the flag after the block."""
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES[engine]
    case.install(monkeypatch)

    with pytest.raises(execute_sql._ResourceLimit):
        case.run()


# --------------------------------------------------------------------------------------------
# The named cancel is the one that actually runs
# --------------------------------------------------------------------------------------------


class _CancelRecorder:
    """Stands in for `_deadline` and keeps the callable it was handed, so a test can invoke exactly
    what the watchdog would have invoked and see where it lands."""

    def __init__(self, log: list | None = None):
        self.cancels: list = []
        self.budgets: list = []
        # When handed the driver's own log, the arming and disarming are recorded IN it, so a test
        # can assert where the deadline sits relative to the calls it is supposed to bound.
        self._log = log

    @contextlib.contextmanager
    def __call__(self, cancel, timeout_s):
        self.cancels.append(cancel)
        self.budgets.append(timeout_s)
        if self._log is not None:
            self._log.append(("deadline.arm",))
        try:
            yield threading.Event()
        finally:
            if self._log is not None:
                self._log.append(("deadline.disarm",))


@pytest.mark.parametrize("engine", _WATCHDOG_ENGINES)
def test_each_engine_arms_its_own_named_cancel(engine, monkeypatch):
    """Not "a cancel ran" but "THE cancel ran". The fake connection exposes every cancel-shaped
    method any of these drivers has — `cancel`, `interrupt`, `_force_close`, `kill`, `close`, a
    cursor `cancel` and a cursor `abort_query` — each logging a distinct marker, so an engine that
    reached for the wrong one lands on the wrong marker and fails here."""
    case = ENGINE_CASES[engine]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()

    assert len(recorder.cancels) == 1, "exactly one deadline per call, resolved once"
    before = len(conn.log)
    recorder.cancels[0]()
    landed = [entry[0] for entry in conn.log[before:]]
    assert landed == [case.cancel], f"{engine} cancelled via {landed}, expected {[case.cancel]}"


def test_the_mysql_cancel_forces_the_socket_shut_and_never_sends_quit(monkeypatch):
    """Called out on its own because it is the case a duck-typed probe gets wrong and no test would
    notice. `pymysql.Connection` has no `cancel()`, so a probe falls through to `close()` — which
    writes COM_QUIT to the very socket the blocked statement owns and can block on it. Only
    `_force_close()` shuts the socket outright, which is what unblocks the statement."""
    case = ENGINE_CASES["mysql"]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()
    before = len(conn.log)
    recorder.cancels[0]()

    during_cancel = [entry[0] for entry in conn.log[before:]]
    assert during_cancel == ["conn._force_close"]
    assert "conn.close" not in during_cancel
    assert "conn.kill" not in during_cancel


def test_the_postgres_cancel_is_the_connections_own(monkeypatch):
    """psycopg2's `connection.cancel()` opens a second connection and sends the libpq cancel request,
    so it is safe to call while this thread is blocked inside the driver."""
    case = ENGINE_CASES["postgres"]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()
    before = len(conn.log)
    recorder.cancels[0]()

    assert [entry[0] for entry in conn.log[before:]] == ["conn.cancel"]


def test_the_snowflake_cancel_does_nothing_before_a_query_id_exists(monkeypatch):
    """The abort is addressed to a query id, and Snowflake only issues one at submission. A cancel
    firing in the sliver before that has nothing to abort, and must not raise on the way to finding
    out — the session parameter is what bounds the statement in that window."""
    case = ENGINE_CASES["snowflake"]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()
    conn.cursors[0].sfqid = None  # rewind to "submitted nothing yet"
    before = len(conn.log)
    recorder.cancels[0]()

    assert conn.log[before:] == []


@pytest.mark.parametrize("engine", _WATCHDOG_ENGINES)
def test_each_engine_arms_the_deadline_with_the_resolved_budget(engine, monkeypatch):
    """One resolution per call, so the budget the watchdog enforces and the number the refusal quotes
    cannot be two different values."""
    execute_sql._timeout_override.set(11)
    case = ENGINE_CASES[engine]
    case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()

    assert recorder.budgets == [11]


def test_duckdb_cancels_through_the_connection_even_though_the_cursor_is_the_connection(monkeypatch):
    """DuckDB's `execute` hands back the CONNECTION rather than a cursor, so `cur` and `conn` are the
    same object and a cursor-side cancel would be indistinguishable from a connection-side one by
    inspection. The cancel that works is `interrupt()`, and it has to be armed around the execute:
    on an in-process engine the execute is where the scan happens, not the fetch."""
    case = ENGINE_CASES["duckdb"]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder(conn.log)
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()

    assert conn.executed_cursor is conn  # the premise: execute returned the connection itself
    # The deadline is armed BEFORE the execute and covers the fetch as well — on an in-process
    # engine the execute is where the scan happens, so arming it after would bound nothing.
    kinds = [entry[0] for entry in conn.log]
    assert kinds[:1] == ["deadline.arm"]
    assert kinds.index("execute") < kinds.index("fetchmany") < kinds.index("deadline.disarm")
    before = len(conn.log)
    recorder.cancels[0]()
    assert [entry[0] for entry in conn.log[before:]] == ["conn.interrupt"]


# --------------------------------------------------------------------------------------------
# Postgres: the named cursor must not eat the marker
# --------------------------------------------------------------------------------------------


class _PostgresExecutor:
    """Runs the real `_run_postgres` against the fake driver, so the assertion is about the Envelope
    the chokepoint produces rather than about the exception the engine raises."""

    def __init__(self, creds: dict):
        self._creds = creds

    def execute(self, vetted_sql: str, creds: dict, *, profile: str):
        return execute_sql._run_postgres(self._creds, vetted_sql)


def test_the_postgres_marker_survives_a_named_cursor_close_that_raises(warehouse, monkeypatch):
    """The structural bug this slice had to fix before the deadline could work on Postgres at all.

    Closing a server-side cursor sends `CLOSE agami_bounded`. On the timeout path the transaction is
    already aborted, so that statement raises in turn — and with the cursor inside a `with`, it
    raises from `__exit__`, where a new exception REPLACES the one being propagated. The marker
    vanishes, the engine's own catch-all wraps the replacement in an `ExecutorError`, and Postgres
    alone reports every timeout as a failure. The fix is to close it by hand and swallow that.
    """
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES["postgres"]
    conn = case.install(
        monkeypatch,
        on_execute=lambda: RuntimeError("canceling statement due to user request"),
        named_close_raises=True,
    )

    env = execute_sql.execute_guarded(
        "SELECT c FROM orders", PROFILE, None,
        executor=_PostgresExecutor(case.creds), no_safety=True,
    )

    assert [e for e in conn.log if e[0] == "cursor.close"], "the named cursor was never closed"
    assert env.status == "refused", getattr(env, "failure", None)
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert conn.log[-1] == ("conn.close",)  # and the connection is still released


def test_the_postgres_transaction_still_rolls_back_when_the_deadline_fires(monkeypatch):
    """`with conn` stayed for a reason: it is the TRANSACTION, and leaving it by an exception rolls
    back. Taking the cursor out of its own `with` must not take that with it."""
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES["postgres"]
    conn = case.install(monkeypatch, on_execute=lambda: RuntimeError("canceling statement"))

    with pytest.raises(execute_sql._ResourceLimit):
        case.run()

    assert ("txn.enter",) in conn.log
    assert ("txn.exit", True) in conn.log  # exited WITH an exception in flight, i.e. rolled back


# --------------------------------------------------------------------------------------------
# The native server-side bounds — three engines, each one skew behind the watchdog
# --------------------------------------------------------------------------------------------

_BUDGET_FOR_NATIVE = 11  # distinctive, so a hard-coded 30 or 35 cannot pass


def test_the_skew_puts_the_native_bound_behind_the_watchdog():
    """The direction of the skew is the whole design. Ahead of the watchdog, a server-side kill would
    win the race, the flag would be clear, and the refusal would arrive as a database failure."""
    assert execute_sql._NATIVE_BOUND_SKEW_S > 0


def test_postgres_sets_the_native_bound_on_the_same_transaction_before_the_named_cursor(monkeypatch):
    """`SET LOCAL` is transaction-scoped, so it is worthless unless it runs on the transaction the
    statement will run in, and before it. Ordering is the assertion; the value is the other half."""
    execute_sql._timeout_override.set(_BUDGET_FOR_NATIVE)
    case = ENGINE_CASES["postgres"]
    conn = case.install(monkeypatch)

    case.run()

    kinds = [entry[0] for entry in conn.log]
    set_local = next(
        i for i, e in enumerate(conn.log)
        if e[0] == "execute" and e[1].startswith("SET LOCAL statement_timeout")
    )
    declared = conn.log.index(("cursor", "agami_bounded"))
    statement = next(
        i for i, e in enumerate(conn.log) if e[0] == "execute" and e[1] == "SELECT c FROM orders"
    )

    assert kinds.index("txn.enter") < set_local < declared < statement
    # Same transaction: nothing committed or rolled back between the setting and the statement.
    assert "txn.exit" not in kinds[:statement]
    # The setting is in milliseconds, and it sits one skew behind our own budget.
    assert conn.log[set_local][2] == ((_BUDGET_FOR_NATIVE + execute_sql._NATIVE_BOUND_SKEW_S) * 1000,)


def test_postgres_does_not_set_the_native_bound_through_the_connect_options(monkeypatch):
    """The libpq `options` startup parameter is the other way to set `statement_timeout`, and it is
    the wrong one here: a transaction-mode connection pooler can reject an unknown startup parameter,
    which breaks the connect outright rather than bounding the statement."""
    execute_sql._timeout_override.set(_BUDGET_FOR_NATIVE)
    case = ENGINE_CASES["postgres"]
    conn = case.install(monkeypatch)

    case.run()

    assert "options" not in conn.connect_kwargs


def test_snowflake_sets_its_statement_timeout_as_a_session_parameter(monkeypatch):
    execute_sql._timeout_override.set(_BUDGET_FOR_NATIVE)
    case = ENGINE_CASES["snowflake"]
    conn = case.install(monkeypatch)

    case.run()

    session = conn.connect_kwargs["session_parameters"]
    assert session["STATEMENT_TIMEOUT_IN_SECONDS"] == (
        _BUDGET_FOR_NATIVE + execute_sql._NATIVE_BOUND_SKEW_S
    )


def test_bigquery_sets_a_job_timeout_and_is_the_one_engine_with_no_cancel(monkeypatch):
    """BigQuery's native bound is the ONLY bound it has, and that is a recorded residual rather than
    an oversight: there is no connection to cancel, and the call that blocks is `job.result()`, which
    is reached only after `client.query()` returns — so at the instant a watchdog would fire there is
    nothing in hand to stop. A client-side stall here comes back `failed`, not `resource_limit`."""
    execute_sql._timeout_override.set(_BUDGET_FOR_NATIVE)
    case = ENGINE_CASES["bigquery"]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder()
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()

    assert conn.job_config_kwargs["job_timeout_ms"] == (
        _BUDGET_FOR_NATIVE + execute_sql._NATIVE_BOUND_SKEW_S
    ) * 1000
    assert recorder.cancels == [], "BigQuery arms no watchdog — see the residual above"


def test_bigquery_keeps_the_default_dataset_alongside_the_job_timeout(monkeypatch):
    """The job config used to be built only when a default dataset was configured. Now it is always
    built, and the pre-existing setting has to survive that."""
    case = ENGINE_CASES["bigquery"]
    conn = case.install(monkeypatch)

    execute_sql._run_bigquery({"project": "proj", "dataset": "shop"}, "SELECT c FROM orders")

    assert conn.job_config_kwargs["default_dataset"] == "proj.shop"
    assert "job_timeout_ms" in conn.job_config_kwargs


@pytest.mark.parametrize("engine", sorted(set(ENGINE_CASES) - {"postgres", "snowflake", "bigquery"}))
def test_no_other_engine_grows_a_native_bound(engine, monkeypatch):
    """Exactly three engines get one. The rest are bounded by the watchdog alone, and inventing a
    per-engine session setting for them would be a second, unasserted timeout to keep in step."""
    case = ENGINE_CASES[engine]
    conn = case.install(monkeypatch)

    case.run()

    statements = " ".join(e[1] for e in conn.log if e[0] == "execute" and isinstance(e[1], str))
    assert "timeout" not in statements.lower()
    assert "session_parameters" not in conn.connect_kwargs


# --------------------------------------------------------------------------------------------
# A real in-process bomb, on a real DuckDB
# --------------------------------------------------------------------------------------------


def test_a_cartesian_bomb_is_bounded_on_a_real_duckdb(tmp_path):
    """The fakes above prove the wiring; this proves the cancel. DuckDB is in-process like SQLite, so
    a genuine `interrupt()` can be driven end to end with no network and no fixture warehouse — and a
    cross join of two ten-million-row ranges is 10^14 rows, far beyond what the budget allows, so a
    run that returns in time can only have been stopped."""
    duckdb = pytest.importorskip("duckdb", reason="duckdb is not installed in this environment")

    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.close()

    execute_sql._timeout_override.set(_BUDGET_S)
    started = time.monotonic()
    with pytest.raises(execute_sql._ResourceLimit):
        execute_sql._run_duckdb(
            {"path": str(path)},
            "SELECT count(*) AS c FROM range(10000000) a, range(10000000) b",
        )
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the statement ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
