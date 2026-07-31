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
