"""Per-statement timeout — config resolution, the deadline primitive, and the refusal it produces.

`_resolve_timeout_s` answers "how long may one statement run", from `AGAMI_SQL_TIMEOUT_S` and a 30s
default and from nothing else — one configuration surface, so the parent and the forked child reach
the same number. Unlike the row cap it complains out loud whenever the budget it returns is not the
one the operator wrote, whether the text was unreadable or merely declined. `_deadline` is the
watchdog those seconds feed: it fires an Event and calls a cancel callable when a block outlives its
budget, and disarms cleanly when it does not — where "cleanly" means the disarm is a JOIN, so a
watchdog that loses the race lands nowhere at all.

The second half of this file proves the whole contract end to end on ONE engine — SQLite, chosen
because it is in-process, needs no network, and `sqlite3.Connection.interrupt()` is a genuine cancel
rather than a polite request. A statement that outlives its budget is cancelled, unwinds on the
internal `_ResourceLimit` marker, and leaves `execute_guarded` as a `refused` Envelope carrying
`resource_limit` — with no partial data, a detail that quotes the configured budget, and a
remediation addressed to whoever can actually act on it.

What no test here can show is that the cancel reached a SERVER: SQLite is in-process, and on a
client/server engine an abandoned statement produces the identical Envelope while the backend keeps
running. That assertion needs a live database and a second connection, and lives in
`test_postgres_timeout_integration.py`, which skips unless one is configured.

**The classification is the FLAG, and only the flag.** A cancelled SQLite statement raises
`OperationalError("interrupted")`, so neither the error text nor the elapsed clock can be the test:
both are properties an ordinary database error can have by coincidence, and reading either one would
mean an unlucky query gets told to narrow itself when nothing timed out. `_deadline` sets its Event
*before* the cancel lands, so "did WE stop this?" is answerable without inference — and it is
asserted here in both directions.

The last section is the OUTER bound, which is what makes the limit apply to every executor rather
than to the built-in one alone. An injected executor (the hosted connection-reuse path) carries none
of the engine watchdogs, and even the built-in BigQuery path has no cancel to arm — so
`execute_guarded` bounds `executor.execute` itself, on a worker thread it can stop waiting for. The
four bounds are one ordered family (watchdog < native < outer < supervisor) resolved from one budget,
and the order is asserted as a single fact so no future change can let them drift apart.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from contextvars import ContextVar
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
def _reset_carriers():
    # `_guard_shape` is request-scoped; isolate every test from it. It matters more than it looks,
    # because the outer bound copies the caller's whole context into its worker, so a value left set
    # by one test is visible to the next test's executor as well. (The row cap used to need the same
    # treatment; it is the deployment's env var alone now — ACE-087.)
    execute_sql._guard_shape.set(None)
    yield
    execute_sql._guard_shape.set(None)


@pytest.fixture(autouse=True)
def _drain_abandoned_workers():
    """Wait for any worker a test abandoned to finish, so its slot is back before the next test runs.

    The counter is process-wide and released by the abandoned worker itself, which returns shortly
    after the test releases it. Draining here rather than zeroing the counter keeps the accounting
    honest: setting it to 0 while a worker was still running would let that worker decrement past
    zero and hand a later test a cap it has not actually got.
    """
    yield
    deadline = time.monotonic() + 5
    while execute_sql._abandoned_workers and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # The suite must not inherit an operator's real budget from the ambient environment.
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)


@pytest.fixture(autouse=True)
def _no_injected_executor():
    """`_INJECTED_EXECUTOR` is a process global, and the tests below install one both directly and
    (via `create_app`) as a side effect of building the HTTP app. Reset it around every test so
    neither leaks into the next one and quietly moves it onto the other execution path."""
    try:
        import tools
    except Exception:
        yield
        return
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


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


# `²` and `①` are the cases `str.isdigit()` waves through and `int()` then refuses. They are not a
# curiosity: the resolver is called at the fork path's supervisor bound, outside any handler, so a
# ValueError raised out of it escapes the tool edge as a traceback rather than as a budget.
@pytest.mark.parametrize("raw", ["6O", "30s", "45.5", "thirty", "1e3", "²", "①"])
def test_an_unreadable_value_falls_back_and_says_so(monkeypatch, caplog, raw):
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


@pytest.mark.parametrize("raw", ["0", "00", "-5", "-0"])
def test_a_value_we_read_and_declined_says_so_too(monkeypatch, caplog, raw):
    """The warning is about the OUTCOME, not about the parse.

    `-5` reads perfectly and is then declined, so it used to slip past silently while `6O` warned —
    and a deployment quietly running 30s when its operator wrote something else is precisely the
    invisible degradation the warning exists against. Whether we could read the text is not the
    question the operator has; whether they got what they asked for is.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        assert execute_sql._resolve_timeout_s() == 30

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, f"no warning emitted for the declined value {raw!r}"
    assert any(raw in r.getMessage() for r in warnings), (
        f"the warning must name the declined text {raw!r}; got {[r.getMessage() for r in warnings]}"
    )


@pytest.mark.parametrize("raw", ["", "45", "045", "  45  ", "600"])
def test_a_value_the_operator_actually_got_stays_quiet(monkeypatch, caplog, raw):
    """The other direction, so the warning cannot become noise. A leading zero or surrounding
    whitespace still yields exactly the number that was written, and warning on it would train
    operators to ignore the log."""
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", raw)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        execute_sql._resolve_timeout_s()
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_budget_has_exactly_one_configuration_surface():
    """The environment is the only source, and that is load-bearing rather than tidy.

    A request-scoped override outranks the environment in THIS process and cannot cross a fork: the
    child re-resolves from `os.environ` alone. So a parent that derived the supervisor's bound from
    an override would compute a bound BELOW the budget the child actually enforces, the supervisor
    would fire first, and the ordered family the whole design rests on would invert — turning a
    precise refusal into a `failed`/`timeout` that names nothing. Asserted structurally, because the
    hazard is the existence of a second surface, not any particular value in one.
    """
    context_vars = {
        name for name, value in vars(execute_sql).items() if isinstance(value, ContextVar)
    }
    # `_last_error_detail` (ACE-039) is a ContextVar but not a CONFIGURATION surface: it carries the
    # raw driver text OUT of a failed call for the audit row, is written after the budget has already
    # been resolved and spent, and is never read to compute a bound. The hazard this test names is a
    # second INPUT to the budget that the fork cannot carry; an output carrier is not one.
    #
    # `_guard_model` (ACE-088) is excluded on the same test, not by exception: it carries the model
    # the safety pass already resolved ACROSS to the receipt builder a few lines later, inside one
    # call. Nothing reads it to compute a bound, and it is cleared at the entry to every call, so it
    # cannot outlive the call that set it — let alone reach a child.
    #
    # `_last_outcome` (ACE-098) is the third, and it is the same kind as the first: an OUTPUT
    # carrier. It holds the classified verdict of a call that has already finished, so the transport
    # can record why it failed without re-parsing the body it is about to return. Written after the
    # budget is resolved and spent, never read to compute a bound, and cleared at the entry to every
    # call. It also cannot cross the fork, which is the hazard here — and does not need to: the
    # parent sets it from the Envelope it rebuilds on its own side.
    #
    # `_guard_shape` (ACE-087) passes the same test for the fourth time: it carries the shape the
    # safety pass read off the statement across to the refusal builder, within one call, and is
    # cleared at entry beside `_guard_model`. It is read only to choose the WORDING of a refusal
    # that has already been decided — never to compute a bound, and never before one is spent.
    #
    # `_pass_posture` (ACE-101) is the fifth, and it is the only one that needed reading twice before
    # being allowed through, because on its face it is exactly the hazard: a request-scoped value that
    # outranks the environment. The difference is that it is not an INPUT anyone configures and it is
    # not invisible across the fork. It holds the answer this process already computed from the
    # environment, so it can never disagree with what the environment says at the moment it was
    # pinned; and `tools._pass_child_env` writes that same answer into the child's environment, so the
    # fork carries it EXPLICITLY rather than losing it. It exists for the inverse of this test's
    # concern: parent and child reading the environment at two different moments is the defect, and
    # pinning is the fix. It is never read to compute a bound.
    context_vars -= {
        "_last_error_detail",
        "_guard_model",
        "_last_outcome",
        "_guard_shape",
        "_pass_posture",
    }
    assert context_vars == set(), (
        "a second, higher-precedence configuration surface for the budget cannot cross the fork; "
        f"found {sorted(context_vars)}"
    )


def test_the_supervisor_bound_exceeds_the_budget_a_real_child_resolves(monkeypatch):
    """The inversion, driven across the actual process boundary rather than reasoned about.

    The parent computes the supervisor's bound; a real forked interpreter, inheriting this
    environment exactly as `subprocess.run` gives it one, resolves its own budget. The first must
    exceed the second, or the outermost bound fires before the innermost.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "300")
    parent_bound = execute_sql._resolve_timeout_s() + execute_sql._SUPERVISOR_SKEW_S

    child = subprocess.run(
        [sys.executable, "-c", "import execute_sql; print(execute_sql._resolve_timeout_s())"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(PKG_SRC)},
    )
    assert child.returncode == 0, child.stderr
    child_budget = int(child.stdout.strip())

    assert child_budget == 300, child.stdout
    assert parent_bound > child_budget, (
        f"the supervisor stops waiting at {parent_bound}s while the child is still inside a "
        f"{child_budget}s budget"
    )


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


class _ManualTimer:
    """A `threading.Timer` stand-in whose `cancel()` does nothing and whose callback the test fires.

    Not a caricature of the real one — a faithful model of its worst case. `Timer.cancel()` only sets
    the timer's internal `finished` Event, so a timer thread that has ALREADY passed its own
    `if not self.finished.is_set()` check runs the callback regardless. This makes that interleaving
    — disarm first, `fire` second — happen on every run rather than once in a thousand.
    """

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        pass


def test_a_watchdog_that_loses_the_race_to_the_disarm_lands_nowhere(monkeypatch):
    """The disarm has to be a JOIN, not a request, and `Timer.cancel()` is only ever a request.

    Two things go wrong when a `fire` that lost the race still runs. Every engine re-reads the flag
    once the block has exited — "checked after the watchdog is disarmed, so the flag is final" — and
    a flag set afterwards makes a completed statement look like a refusal. And the cancel itself
    lands on a connection the engine has moved on from; on a pooled one that is somebody else's
    statement being killed by our watchdog.
    """
    timers: list = []

    def _timer(interval, function):
        timers.append(_ManualTimer(interval, function))
        return timers[-1]

    monkeypatch.setattr(execute_sql.threading, "Timer", _timer)
    cancel = _RecordingCancel()

    with execute_sql._deadline(cancel, _TINY) as fired:
        pass

    assert timers and timers[0].started, "no watchdog was armed"
    timers[0].function()  # the timer thread had already committed; `fire` runs anyway

    assert cancel.calls == 0, "a cancel landed after the block the deadline was bounding had exited"
    assert not fired.is_set(), "the flag every engine re-reads after the block was not final"


def test_a_watchdog_that_fires_while_the_timer_is_being_disarmed_lands_nowhere(monkeypatch):
    """The narrowest interleaving of the same race: `fire` lands DURING the disarm itself.

    `Timer.cancel()` is not a join, so the disarm has two steps that are not one atomic act: stand the
    timer down, and claim the race under the lock. Do them in that order and the gap between them is
    a window in which an already-expired `fire` takes the lock first, sets the flag, and marks a
    statement that in fact completed. Claiming the race first closes it. This timer fires its
    callback from inside `cancel()`, which puts a `fire` in exactly that gap on every run.
    """
    cancel = _RecordingCancel()

    class _FiresWhileBeingCancelled(_ManualTimer):
        def cancel(self) -> None:
            self.function()

    monkeypatch.setattr(
        execute_sql.threading, "Timer", lambda interval, function: _FiresWhileBeingCancelled(interval, function)
    )

    with execute_sql._deadline(cancel, _TINY) as fired:
        pass

    assert cancel.calls == 0, "a cancel landed in the gap between standing the timer down and disarming"
    assert not fired.is_set(), "a statement that completed was flagged by a watchdog firing mid-disarm"


def test_the_disarm_waits_for_a_cancel_already_in_flight():
    """The other half of the same guarantee: a `fire` that WON the race is finished with before the
    block is allowed to return.

    Otherwise "no late cancel arrives" is only true of the cancels that had not started yet, and a
    driver cancel that takes a moment still reaches into whatever the connection does next.
    """
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def cancel() -> None:
        entered.set()
        release.wait(5)

    def run() -> None:
        with execute_sql._deadline(cancel, _TINY):
            assert entered.wait(5), "the watchdog never ran"
        exited.set()

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert entered.wait(5), "the watchdog never ran"

    assert not exited.wait(0.3), "the block returned while its own cancel was still running"
    release.set()
    runner.join(5)
    assert exited.is_set()


def _wait_for_warning(caplog, needle: str, timeout_s: float = 5.0) -> bool:
    """Poll the captured records for a warning containing `needle`, up to a deadline."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(needle in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING):
            return True
        time.sleep(0.005)
    return False


def test_a_cancel_that_raises_does_not_escape_the_timer_thread(caplog):
    """Some drivers raise when cancelled from a thread other than the one running the statement. That
    must be logged and swallowed: an exception escaping a timer thread is unhandleable by the caller
    and would be lost to threading's excepthook.

    Polled rather than slept on. `cancel.done` is set in a `finally` that runs BEFORE the raise
    reaches `fire`'s handler, so nothing this test can wait on is synchronized with the log write,
    and a fixed sleep is a wager on how the runner happened to schedule two threads.
    """
    cancel = _RecordingCancel(fails=True)
    with caplog.at_level(logging.WARNING, logger=execute_sql._LOG.name):
        with execute_sql._deadline(cancel, _TINY) as fired:
            assert cancel.done.wait(2.0), "the watchdog never ran"
        assert _wait_for_warning(caplog, "driver refused to cancel"), (
            f"the failed cancel was not logged; got {[r.getMessage() for r in caplog.records]}"
        )

    assert fired.is_set()  # the timeout still counts as fired even though the cancel failed
    assert cancel.calls == 1


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


def test_a_runaway_statement_is_cancelled_rather_than_left_to_run(warehouse, monkeypatch):
    """The headline: a statement that would run for minutes is stopped at its budget and comes back
    as a refusal naming the rule the contract reserves for a bound we imposed.

    The elapsed assertion is the one that would still fail if the deadline were never armed — without
    it a test that merely waited out the query would look identical and pass in several minutes.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

    started = time.monotonic()
    env = _guarded(_RUNAWAY_SQL)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the statement ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    # Neither unsafe nor out of scope: we simply did not determine the answer within the bound.
    assert env.refusal.reason == "undetermined"
    assert env.refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT]


def test_a_cancelled_statement_yields_no_partial_data(warehouse, monkeypatch):
    """Whatever rows the engine had gathered when the watchdog fired are not an answer.

    A truncated result presented as a result is the failure mode the bounded-fetch work already
    guards against on the row axis; on the time axis the answer is stronger — there is no data at
    all, and `Envelope.__post_init__` enforces that a refusal cannot carry any.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

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


def test_the_detail_quotes_the_configured_budget(warehouse, monkeypatch):
    """A bound the caller cannot see is one it cannot plan around, so the number is in the detail.

    The configured value is not a data value: it is a deployment setting, and stating it discloses
    nothing about the database or its contents. Asserted against a distinctive budget rather than the
    default, so a hard-coded `30s` in the message cannot pass.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(7))

    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_ResourceLimitExecutor(), no_safety=True,
    )

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert "7s" in env.refusal.detail, env.refusal.detail


def test_the_remediation_names_no_deployment_environment_variable(warehouse, monkeypatch):
    """The remediation has to be addressed to whoever is reading it.

    On the served path that is an assistant holding a statement, with no shell, no deployment and no
    way to set an environment variable — so "raise AGAMI_SQL_TIMEOUT_S" is advice aimed past the
    caller at an operator who is not in the conversation, and it reads as a fix while being
    unfollowable. What is left has to be something that would make THIS statement executable.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

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


def test_a_slow_fetch_is_bounded_even_when_the_execute_returned_at_once(
    warehouse, fake_sqlite, monkeypatch
):
    """The clock covers the whole statement, fetch included — an explicit criterion, not a bonus.

    Bounding only `execute` would leave the common streaming shape unbounded: the driver returns
    immediately and the engine scans while the caller pulls. The cancel has to land on the fetch, and
    the refusal has to be the same one a slow execute produces.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
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


def test_a_database_error_with_the_flag_unset_is_a_failure_not_a_refusal(
    warehouse, fake_sqlite, monkeypatch
):
    """Direction (a): the watchdog never fired, so this is the database's outcome, not ours.

    A generous budget means the flag stays clear while the identical error text arrives. It must
    unwind as an `ExecutorError` and leave the chokepoint as `failed`/`syntax` — a refusal here would
    tell a caller to narrow a statement that never ran long at all.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(300))
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
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
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

# A neutral hostname and a throwaway secret, only so the HTTP surface below can mint a token.
_BASE_URL = "https://your-host.example.com"
_SIGNING_SECRET = "x" * 40


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
        {"datasource": "Shop", "version": 1,
         "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
         "subject_areas": ["subject_areas/sales"]}))
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
    # The HTTP surface needs both to mint and verify its bearer token; the stdio surface ignores
    # them. Set here so one install serves both transports.
    monkeypatch.setenv("PUBLIC_BASE_URL", _BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", _SIGNING_SECRET)
    return SimpleNamespace(app_db=app_db)


def test_the_refusal_is_written_to_the_audit_trail(audited, monkeypatch):
    """A decision we made against a caller's statement is exactly the row a reviewer comes looking
    for, so this outcome is audited like every other — and keyed by the id the caller was handed.

    Driven through the tool edge with the built-in executor injected, which is the in-process path
    the hosted server runs: one real cancel, one real serializer, one real sink.
    """
    import tools
    from store import Store

    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
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
# The outer bound — the limit reaches every executor, not only the built-in one
# --------------------------------------------------------------------------------------------
#
# The engine watchdogs live INSIDE the built-in executor, so an injected one — which is what the
# hosted connection-reuse path supplies — ran with no per-statement bound at all, and BigQuery has no
# watchdog even on the built-in path. `execute_guarded` therefore bounds `executor.execute` itself.
# That layer holds no connection and can cancel nothing; it runs the call on a daemon worker and
# stops WAITING, which is why it must be the outer one and the watchdog must always win.


class _BlockingExecutor:
    """A `ports.Executor` whose `execute` never returns on its own — the shape the outer bound
    exists for. Nothing about it is cancellable from outside, which is exactly the point: an
    arbitrary injected executor exposes one method and no handle on the work behind it."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls += 1
        self.entered.set()
        # Released by the test, so the worker this deliberately leaks does not outlive the test that
        # made it. A real one would sit here until its driver call returned on its own.
        self.release.wait(10)
        return execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)


def test_an_injected_executor_that_never_returns_is_still_bounded(warehouse, monkeypatch):
    """The headline for this layer: an executor with no watchdog of its own, no cancel and no
    intention of returning still yields a `resource_limit` refusal at a bound we set.

    Without the outer layer this call blocks in the executor and comes back `ok` with its rows, so
    the assertion is on the WHOLE outcome — that it returned at all, in time, and as a refusal.

    The skew is patched to zero so the bound under test is the 1s budget rather than 11s of wall
    clock. The +10 value it normally carries is not skipped: it is pinned by the ordering test below,
    which needs no clock at all.
    """
    monkeypatch.setattr(execute_sql, "_OUTER_BOUND_SKEW_S", 0)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
    executor = _BlockingExecutor()

    started = time.monotonic()
    try:
        env = execute_sql.execute_guarded(
            "SELECT id FROM orders", PROFILE, None, executor=executor, no_safety=True,
        )
        elapsed = time.monotonic() - started
    finally:
        executor.release.set()

    assert executor.entered.is_set()  # the executor really ran; this is not a gate refusing early
    assert elapsed < 10, f"the call waited {elapsed:.1f}s on a {_BUDGET_S}s bound"
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    # A bound WE imposed is a refusal, not a failure — and it carries no data, like every refusal.
    assert env.data is None and env.failure is None


def test_the_outer_refusal_says_what_actually_happened(warehouse, monkeypatch):
    """It must not claim the statement was cancelled. Nothing was: the worker is still inside the
    driver call, may still hold a connection, and the statement may still be running on the server.
    The sentence the caller reads names what we observed — the executor did not come back — and
    quotes the bound that actually elapsed rather than the inner budget."""
    monkeypatch.setattr(execute_sql, "_OUTER_BOUND_SKEW_S", 0)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
    executor = _BlockingExecutor()

    try:
        env = execute_sql.execute_guarded(
            "SELECT id FROM orders", PROFILE, None, executor=executor, no_safety=True,
        )
    finally:
        executor.release.set()

    assert "cancelled" not in env.refusal.detail, env.refusal.detail
    assert "did not return" in env.refusal.detail, env.refusal.detail
    # The remediation is the same one the inner bound gives: the fix that makes THIS statement
    # runnable, addressed to a caller with no shell and no deployment.
    authored = f"{env.refusal.detail} {env.refusal.remediation}"
    assert "AGAMI_" not in authored, authored


def _guarded_with(executor) -> object:
    return execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=executor, no_safety=True,
    )


def _drain(deadline_s: float = 5.0) -> None:
    """Wait for the abandoned workers to return and give their slots back."""
    deadline = time.monotonic() + deadline_s
    while execute_sql._abandoned_workers and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_abandoned_workers_are_capped_and_the_cap_releases(warehouse, monkeypatch):
    """The cost of the outer bound is an abandoned worker, and it has to have a ceiling.

    Before this layer existed, a call this slow blocked the host's own worker thread — so the thread
    limiter capped how much abandoned work could exist and pushed back on everything behind it.
    Returning at the bound frees that slot, and on the injected path (no inner watchdog, so EVERY
    slow statement abandons one) the abandonments would accumulate at the rate callers arrive, each
    still holding a pooled connection and a running statement, until the datasource belonged to work
    nobody is waiting for.

    So at the cap the chokepoint refuses BEFORE starting one more — fast, fail-closed, and saying the
    executor is saturated rather than blaming a statement that never ran. And the slot comes back
    when the abandoned worker finally returns, which is the moment the leak actually ends.
    """
    monkeypatch.setattr(execute_sql, "_OUTER_BOUND_SKEW_S", 0)
    monkeypatch.setattr(execute_sql, "_MAX_ABANDONED_WORKERS", 2)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
    executor = _BlockingExecutor()

    try:
        for n in range(2):
            env = _guarded_with(executor)
            assert env.status == "refused", (n, env)
            assert "did not return" in env.refusal.detail, (n, env.refusal.detail)
        assert execute_sql._abandoned_workers == 2

        started = time.monotonic()
        env = _guarded_with(executor)
        elapsed = time.monotonic() - started

        assert elapsed < _BUDGET_S, f"the capped call still waited out a bound ({elapsed:.2f}s)"
        assert env.status == "refused"
        assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
        assert "saturated" in env.refusal.detail, env.refusal.detail
        # It must not blame a statement that never ran, and it must not claim a cancel.
        assert "cancelled" not in env.refusal.detail, env.refusal.detail
        assert executor.calls == 2, "the refused call started leak N+1 anyway"
    finally:
        executor.release.set()

    _drain()
    assert execute_sql._abandoned_workers == 0, "the slots never came back"
    assert _guarded_with(executor).status == "ok"  # and the executor is usable again


def test_the_inner_refusal_is_the_one_the_caller_gets(warehouse, monkeypatch):
    """When both layers are armed the INNER one wins, and the outer neither overwrites its refusal
    nor mints a second.

    That ordering is what buys the caller a precise answer: only the watchdog holds the connection
    it cancelled, so only it can say the STATEMENT was stopped. A real cancel is driven here (no
    stub, no patched skew) with the outer bound left at its real +10, so the outer layer is genuinely
    armed and genuinely loses the race. The detail is the assertion, because it is the one part of
    the envelope the two layers word differently.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

    started = time.monotonic()
    env = _guarded(_RUNAWAY_SQL)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the statement ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert f"{_BUDGET_S}s" in env.refusal.detail, env.refusal.detail  # the inner budget
    assert "cancelled" in env.refusal.detail, env.refusal.detail  # the inner sentence
    assert "did not return" not in env.refusal.detail, env.refusal.detail  # not the outer one


def test_the_four_bounds_are_ordered_from_one_resolved_budget(monkeypatch):
    """One fact, asserted once: the four time bounds are derived from a single resolved budget and
    stand in a fixed order — watchdog < native (+5) < outer (+10) < supervisor (+60).

    Asserted together rather than one skew per test because the ORDER is the property, and four
    separate assertions would each stay green while the family drifted apart. Each layer behind the
    watchdog is a backstop for something the layer in front cannot see, and every inversion trades a
    precise refusal for a vaguer answer to the same event: a native kill reads as a database error,
    an outer expiry cannot say the statement stopped, and a supervisor kill cannot say what hung.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "45")  # distinctive: a hard-coded 30 cannot pass
    timeout_s = execute_sql._resolve_timeout_s()

    watchdog = timeout_s
    native = timeout_s + execute_sql._NATIVE_BOUND_SKEW_S
    outer = timeout_s + execute_sql._OUTER_BOUND_SKEW_S
    supervisor = timeout_s + execute_sql._SUPERVISOR_SKEW_S

    assert watchdog < native < outer < supervisor
    assert (watchdog, native, outer, supervisor) == (45, 50, 55, 105)


class _RaisingExecutor:
    """A `ports.Executor` that raises a given exception from inside the worker thread."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        raise self._exc


def test_an_executor_error_keeps_its_type_across_the_worker(warehouse):
    """The subtle half of running the call off-thread: an exception raised in the worker has to reach
    the caller with its ORIGINAL type, or every handler in `execute_guarded` stops matching.

    `ExecutorError` is the one that would fail loudest — its handler is what turns a driver error
    into a classified `failed` envelope carrying the message this module authored. Caught as anything
    else it would land in the catch-all and the caller would get the generic string instead, so the
    relayed message is asserted rather than just the status.
    """
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_RaisingExecutor(execute_sql.ExecutorError("no such column: nope", code=5)),
        no_safety=True,
    )

    assert env.status == "failed"
    # The classified branch, i.e. `except ExecutorError` matched. Was `syntax` before ACE-039, which
    # was the exit-5 prior showing through rather than a read of the text; `SELECT c FROM orders`
    # against a warehouse with no `c` is a missing column.
    assert env.failure.kind == "column_not_found"
    # The message is the classified sentence, not the driver's text (ACE-039). What this test is
    # actually about survives unchanged: the exception kept its TYPE across the worker thread, which
    # is what `except ExecutorError` matching at all proves — had it been re-wrapped, the catch-all
    # would have produced `other` and the generic unexpected-failure message instead.
    assert env.failure.message == execute_sql._ERROR_MESSAGES["column_not_found"]
    assert "nope" not in env.failure.message
    assert env.failure.message != execute_sql.UNEXPECTED_FAILURE_MESSAGE


def test_a_plain_exception_still_reaches_the_catch_all_across_the_worker(warehouse):
    """And the other direction: an exception nobody classified is still an unanticipated break, told
    to the caller as the one fixed string. A worker that swallowed it would leave the call looking
    like it produced nothing at all."""
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_RaisingExecutor(RuntimeError("a driver detail nobody has vetted")),
        no_safety=True,
    )

    assert env.status == "failed"
    assert env.failure.kind == "other"
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    assert "driver detail" not in env.failure.message  # the raw text goes to the log, not the caller


class _ContextProbeExecutor:
    """Records the thread it ran on, the request-scoped row cap it read there, and the budget."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.seen_timeout: object = "not read"

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.thread = threading.current_thread()
        self.seen_timeout = execute_sql._resolve_timeout_s()
        return execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)


def test_the_executor_really_runs_off_thread_and_reads_the_same_budget(warehouse, monkeypatch):
    """The budget rides the environment, which a new thread inherits for free, and it must resolve
    to the same number inside the worker as outside it.

    The thread identity is asserted first, because it is what makes the rest mean anything: if the
    call ever stopped running off-thread, the budget assertion would pass trivially.

    This test used to also assert the per-call row cap reached the worker through `copy_context()`.
    That cap is gone (ACE-087) and, with it, the last thing OF OURS that is read inside the worker.
    The copy is still load-bearing and is witnessed by
    `test_the_callers_request_context_reaches_the_executor` below, at the layer that performs it.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "7")
    probe = _ContextProbeExecutor()

    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=probe, no_safety=True,
    )

    assert env.status == "ok"
    assert probe.thread is not threading.current_thread()  # it really ran on a worker
    assert probe.seen_timeout == 7


def test_the_callers_request_context_reaches_the_executor(monkeypatch):
    """`_execute_bounded` runs the executor on a worker thread, and a new thread starts with an
    EMPTY context. The copy is what lets a consumer's injected executor — the pooled /
    per-user-RBAC seam — pick its connection from the request it is actually serving.

    Asserted on `_execute_bounded` directly rather than through `execute_guarded`, because the
    chokepoint deliberately clears our own carriers on entry: any var set before calling it reads
    as None inside by design, so the copy could be deleted with an end-to-end test still green.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")
    probe_var: ContextVar[str | None] = ContextVar("probe_request_scope", default=None)
    probe_var.set("this-request")
    seen: dict[str, object] = {}

    class _Reader:
        def execute(self, vetted_sql, creds, *, profile):
            seen["thread"] = threading.current_thread()
            seen["value"] = probe_var.get()
            return execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)

    execute_sql._execute_bounded(_Reader(), "SELECT 1", {}, profile="acme")

    assert seen["thread"] is not threading.current_thread()  # it really ran off-thread
    assert seen["value"] == "this-request"  # ...and still saw the caller's context


# --------------------------------------------------------------------------------------------
# The supervisor's bound, derived rather than fixed
# --------------------------------------------------------------------------------------------


def test_the_supervisor_bound_is_derived_from_the_statement_budget(warehouse, monkeypatch):
    """The fork path's supervisor was a hardcoded 240s, which quietly became the FIRST bound to fire
    on any deployment configuring a larger statement budget — turning a refusal that could name the
    statement into a `failed`/`timeout` that names nothing. It is now `budget + 60`, from the same
    resolver every inner layer reads.

    Asserted at a RAISED budget, which is the case the fixed number got wrong: at 300s the supervisor
    must be 360, and 240 would have killed the child a full minute before its own statement bound.
    The computed value is read off the `subprocess.run` call rather than waited out, for the obvious
    reason.
    """
    import tools

    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "300")
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: PROFILE)

    class _Proc:
        returncode = 0
        stdout = "id\r\n1\r\n"
        stderr = ""

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(tools.subprocess, "run", _fake_run)
    tools.set_injected_executor(None)  # the fork path, which is the one with a supervisor

    tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE})

    assert captured["timeout"] == 300 + execute_sql._SUPERVISOR_SKEW_S == 360
    assert captured["timeout"] > 300, "the supervisor must fire AFTER the statement bound, not before"
    assert captured["timeout"] != 240, "the fixed bound this replaces"
    # And nothing was passed that would stop the child inheriting the same configuration.
    #
    # This asserted `"env" not in captured` until ACE-101, and the property it was defending is
    # unchanged: the child must reach the identical timeout by re-resolving it from an environment
    # this side did not disturb. What changed is that an env IS now passed, so "no env at all" stopped
    # being a way to state that property and started being a proxy for it. Asserted directly instead:
    # the child's environment is this process's, differing in exactly one key, and that key is not a
    # budget input. A second difference of any kind fails here, which is strictly more than the old
    # assertion caught (it could not have seen a change made by adding `env=` and then mutating it).
    child_env = captured["env"]
    differing = {
        key
        for key in set(child_env) | set(os.environ)
        if child_env.get(key) != os.environ.get(key)
    }
    # A subset rather than equality: the pinned key matches the environment whenever the environment
    # already spells the same posture, which is the ordinary case and the case this suite runs in. The
    # property being defended is that NOTHING ELSE differs.
    assert differing <= {"AGAMI_GOVERNANCE_ENFORCED"}, differing


def test_the_supervisors_verdict_is_unchanged(warehouse, monkeypatch):
    """Only the DURATION moved. A child that never came back is still a `failed`/`timeout` and never
    a `resource_limit`: the bound is ours, but all it tells us is that the child stopped responding —
    it may have hung in connect, in credential resolution or in loading the model, and on any of
    those "narrow the query" points at the wrong thing."""
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: PROFILE)

    def _timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(tools.subprocess, "run", _timed_out)
    tools.set_injected_executor(None)

    body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders",
                                              "datasource": PROFILE}))

    assert body["status"] == "failed"
    assert body["failure"]["kind"] == "timeout"


# --------------------------------------------------------------------------------------------
# Both surfaces refuse identically
# --------------------------------------------------------------------------------------------


def _stdio_refusal(sql: str) -> dict:
    """`python -m mcp_harness` over JSON-RPC on stdin — the transport a desktop client launches, and
    the one that forks `python -m execute_sql`. The child inherits this process's environment, so it
    resolves the same budget, model and warehouse; nothing on this path is stubbed."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": PROFILE,
                                                         "raw_query": "how many"}}},
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


def _http_refusal(sql: str) -> dict:
    """The authenticated HTTP transport, which runs execution IN-PROCESS through `create_app()`'s
    default adapters — the other of the two execution paths."""
    import mcp_http
    import tools
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app()) as client:
        assert tools._INJECTED_EXECUTOR is not None, (
            "create_app() no longer injects an executor, so this surface is now the fork path and "
            "the in-process path this test believes it covers is uncovered"
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
            "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": PROFILE,
                                                            "raw_query": "how many"}}})
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


def _audit_rows(app_db: str) -> list:
    from store import Store

    store = Store.connect(app_db)
    try:
        return store.query("SELECT id, status, reason, rule FROM query_executions")
    finally:
        store.close()


def test_both_surfaces_refuse_a_runaway_statement_identically(audited, monkeypatch):
    """The criterion in one test: the limit applies the same on stdio and on HTTP, no executor
    escapes it, and BOTH leave an audit row.

    The two transports run DIFFERENT execution paths — stdio forks a child and reads its stderr back,
    HTTP runs the built-in executor in-process behind the outer bound — so "identically" is asserted
    on the whole refusal, not merely on the status. A difference in any field would mean the caller's
    answer depends on which door it came through.

    The audit half is asserted here rather than only at the in-process tool edge for the same reason:
    the row is written by the serializer, and the fork path reaches it having reconstructed the
    refusal from a child's stderr. "A timeout refusal is recorded on both surfaces" is a claim about
    both, and one surface's row does not evidence the other's.
    """
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("jwt")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

    stdio = _stdio_refusal(_RUNAWAY_SQL)
    after_stdio = _audit_rows(audited.app_db)
    http = _http_refusal(_RUNAWAY_SQL)
    after_http = _audit_rows(audited.app_db)

    assert stdio["status"] == http["status"] == "refused", (stdio, http)
    assert stdio["refusal"]["rule"] == guardrail.RULE_RESOURCE_LIMIT
    assert stdio["refusal"] == http["refusal"]

    # One row per call, keyed by the id that call handed back — counted after each transport so a
    # single row could not stand in for both.
    assert len(after_stdio) == 1, after_stdio
    assert len(after_http) == 2, after_http
    assert {r["id"] for r in after_http} == {stdio["audit_id"], http["audit_id"]}
    assert {r["status"] for r in after_http} == {"refused"}
    assert {r["rule"] for r in after_http} == {guardrail.RULE_RESOURCE_LIMIT}
    assert {r["reason"] for r in after_http} == {
        guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT]
    }


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


def test_the_cli_renders_the_marker_as_a_refusal_not_a_traceback(tmp_path, monkeypatch, capsys):
    """A marker raised inside an engine reaches the CLI wire as a refusal — one JSON object on
    stderr and exit 1 — rather than as a traceback.

    This was asserted on `_execute_sqlite`, one of the per-engine CSV wrappers, which were the SECOND
    entry into the engine functions and caught only `ExecutorError` until this test was written.
    ACE-087 deleted that whole family: nothing in production reached it, and it trimmed results with
    no refusal, so it was a route around the chokepoint that looked like a route through it. With one
    entry left there is no longer a second renderer to disagree with, and the assertion moves to the
    entry that survived.
    """
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES["sqlite"]
    case.install(monkeypatch, on_execute=lambda: RuntimeError("interrupted"))
    # `main` routes through `execute_guarded`, which resolves credentials itself; the engine case
    # carries only its driver-specific keys, so the dispatch key is added here.
    creds = {**case.creds, "type": "sqlite"}
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": creds)
    monkeypatch.setattr(
        sys, "argv",
        ["execute_sql", "--profile", "acme", "--sql", "SELECT c FROM orders", "--no-safety"],
    )

    code = execute_sql.main()

    assert code == 1
    err = capsys.readouterr().err
    body = json.loads(err)  # parses WHOLE, so nothing rides alongside it
    assert body["refusal"]["rule"] == guardrail.RULE_RESOURCE_LIMIT
    assert body["refusal"]["reason"] == guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT]


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


@pytest.mark.parametrize("engine", _WATCHDOG_ENGINES)
def test_the_deadline_covers_the_fetch_and_not_only_the_execute(engine, monkeypatch):
    """An explicit criterion, and until now pinned on two engines out of nine.

    `_collect_cursor` pulls `cap + 1` rows in a single `fetchmany`, and on a cursor that streams its
    result THAT is where the scan happens — a clock stopping at `execute` would bound the cheap half
    of the work and leave the expensive half unbounded. Only SQLite (through a fake slow-fetch
    cursor) and DuckDB (through this ordering assertion) actually held the line; on the other seven,
    moving the fetch out of the deadline's block left the whole suite green. The ordering IS the
    criterion, so it is asserted the same way on every engine that arms a watchdog.
    """
    case = ENGINE_CASES[engine]
    conn = case.install(monkeypatch)
    recorder = _CancelRecorder(conn.log)
    monkeypatch.setattr(execute_sql, "_deadline", recorder)

    case.run()

    kinds = [entry[0] for entry in conn.log]
    # By value, not by first occurrence: Postgres runs `SET LOCAL statement_timeout` on its own
    # cursor before the deadline is armed, and that is an `execute` too.
    statement = next(
        i for i, e in enumerate(conn.log) if e[0] == "execute" and e[1] == "SELECT c FROM orders"
    )
    assert (
        kinds.index("deadline.arm") < statement < kinds.index("fetchmany")
        < kinds.index("deadline.disarm")
    ), conn.log


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
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(11))
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
# Nothing in a `finally` may eat the marker
# --------------------------------------------------------------------------------------------


class _EngineExecutor:
    """Runs one engine's real `_run_<db>` against its fake driver, so the assertion is about the
    Envelope the chokepoint produces rather than about the exception the engine raises."""

    def __init__(self, case: "_EngineCase"):
        self._case = case

    def execute(self, vetted_sql: str, creds: dict, *, profile: str):
        return self._case.fn(self._case.creds, vetted_sql)


# The three engines whose `finally` closed the connection unguarded. The other seven already wrapped
# it; these were the ones a refusal could still die in.
@pytest.mark.parametrize("engine", ["mysql", "postgres", "sqlite"])
def test_a_connection_close_that_raises_does_not_eat_the_refusal(engine, warehouse, monkeypatch):
    """The same structural bug as the named cursor above, one line further down.

    An exception raised inside a `finally` REPLACES the one propagating through it, and by the time
    the `finally` runs the engine's `except _ResourceLimit: raise` has already re-raised the marker.
    So a `close()` that throws destroys it, the catch-all one layer out wraps the replacement, and a
    bound WE imposed reaches the caller as an unclassified server break telling them nothing.

    MySQL is the most exposed and the reason this is not theoretical: its cancel is `_force_close()`,
    which deliberately destroys the socket — so on exactly the timeout path, `close()` is being asked
    to write COM_QUIT to a socket that is already gone.
    """
    monkeypatch.setattr(execute_sql, "_deadline", _deadline_already_fired)
    case = ENGINE_CASES[engine]
    conn = case.install(monkeypatch, on_execute=lambda: RuntimeError("connection reset by peer"))

    def _close_raises() -> None:
        conn.log.append(("conn.close",))
        raise RuntimeError("the socket the cancel destroyed is not there to close")

    monkeypatch.setattr(conn, "close", _close_raises)

    env = execute_sql.execute_guarded(
        "SELECT c FROM orders", PROFILE, None, executor=_EngineExecutor(case), no_safety=True,
    )

    assert ("conn.close",) in conn.log, "the connection was never closed"
    assert env.status == "refused", getattr(env, "failure", None)
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert env.failure is None


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
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_FOR_NATIVE))
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
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_FOR_NATIVE))
    case = ENGINE_CASES["postgres"]
    conn = case.install(monkeypatch)

    case.run()

    assert "options" not in conn.connect_kwargs


def test_snowflake_sets_its_statement_timeout_as_a_session_parameter(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_FOR_NATIVE))
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
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_FOR_NATIVE))
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


def test_a_cartesian_bomb_is_bounded_on_a_real_duckdb(tmp_path, monkeypatch):
    """The fakes above prove the wiring; this proves the cancel. DuckDB is in-process like SQLite, so
    a genuine `interrupt()` can be driven end to end with no network and no fixture warehouse — and a
    cross join of two ten-million-row ranges is 10^14 rows, far beyond what the budget allows, so a
    run that returns in time can only have been stopped."""
    duckdb = pytest.importorskip("duckdb", reason="duckdb is not installed in this environment")

    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.close()

    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))
    started = time.monotonic()
    with pytest.raises(execute_sql._ResourceLimit):
        execute_sql._run_duckdb(
            {"path": str(path)},
            "SELECT count(*) AS c FROM range(10000000) a, range(10000000) b",
        )
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"the statement ran {elapsed:.1f}s against a {_BUDGET_S}s budget"
