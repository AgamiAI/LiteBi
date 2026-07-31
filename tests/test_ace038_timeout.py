"""Per-statement timeout — config resolution and the deadline primitive.

`_resolve_timeout_s` answers "how long may one statement run", from `AGAMI_SQL_TIMEOUT_S`, the
`_timeout_override` ContextVar, and a 30s default; unlike the row cap it complains out loud when the
env value is present but unparseable. `_deadline` is the watchdog those seconds feed: it fires an
Event and calls a cancel callable when a block outlives its budget, and disarms cleanly when it does
not. Nothing in the engines calls either yet.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

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
