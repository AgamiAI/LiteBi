"""Integration guard: the per-statement timeout on a LIVE Postgres, asserted against the SERVER's
view of what happened rather than the client's.

The in-process tests prove the refusal; they cannot prove the cancel. A client that simply stopped
waiting — abandoned the statement and closed nothing — produces the identical Envelope while the
backend keeps scanning for nobody. So the assertion here is made from a SECOND connection: the
statement is observed running in `pg_stat_activity`, and after the refusal it is GONE from
`pg_stat_activity`. That is the difference between a bound on the caller and a bound on the
database, and it is the whole security value of the feature.

The second test proves the other half — that `SET LOCAL statement_timeout` really landed on the
session. It disarms our own watchdog and lets the statement run past the native bound; Postgres then
kills it itself, and says so in its own words ("canceling statement due to statement timeout", which
a client cancel never produces — that one reads "due to user request").

Opt-in: it **skips unless `AGAMI_IT_PG_PASSWORD` is set** (so normal CI, which has no Postgres, is
unaffected, and no test password lives in the source). To run it against the integration fixture:

    docker compose -f tests/integration/docker-compose.yml up -d postgres
    AGAMI_IT_PG_PASSWORD=<the fixture's POSTGRES_PASSWORD> \
        uv run pytest tests/test_postgres_timeout_integration.py

Host/port/user/db default to the fixture's values and can be overridden via the other AGAMI_IT_PG_*
vars, exactly as in `test_postgres_named_cursor_integration.py`.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402

PROFILE = "analytics"

# Long enough that the observer below reliably catches the statement mid-flight on a loaded machine,
# short enough that the whole file stays a few seconds. The resolver deals in whole seconds, so this
# is also the smallest useful step above the 1s floor the SQLite tests use.
_BUDGET_S = 3

# A statement with no termination in reach on any machine: a three-way cross join of ten thousand
# rows each, so a trillion nested-loop iterations of pure CPU. Sized that way on purpose — "bounded
# rather than hanging" is only proved if the unbounded run would take far longer than the assertion
# allows.
#
# The SHAPE matters as much as the size. One wide `generate_series(1, 100000000000)` looks like the
# simpler runaway and is wrong: Postgres materializes a function scan into a tuplestore, so that
# statement burns time by filling the server's temp tablespace, and a test that leaves the disk full
# has done more damage than the bug it was checking for. Three narrow series each materialize a few
# hundred kilobytes and never grow — the cost is entirely time, which is the only axis under test.
#
# `pg_sleep` would be the obvious runaway and is deliberately not used: the read-only guard blocks it
# by name (it is a DoS function), so it would never reach the executor. This does reach it — it opens
# with SELECT, names no physical table, projects no star, and calls nothing the guard denies.
_RUNAWAY_SQL = (
    "SELECT count(*) AS n FROM generate_series(1, 10000) AS a(x), "
    "generate_series(1, 10000) AS b(y), generate_series(1, 10000) AS c(z)"
)

# What the executor's work looks like from the server side. The Postgres path runs through a
# server-side cursor, so the backend's `query` is the `DECLARE "agami_bounded" …` and then the
# `FETCH FORWARD … FROM "agami_bounded"` that actually burns the CPU — the cursor name, not the
# SELECT text, is what is on the wire for most of the run.
_CURSOR_NAME = "agami_bounded"


def _pg_creds() -> dict[str, str]:
    # The password is env-only (no source-embedded test secret); host/port/user/db default to the
    # fixture's values. Override any of them via AGAMI_IT_PG_* to point at another Postgres.
    return {
        "type": "postgres",
        "host": os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
        "port": os.environ.get("AGAMI_IT_PG_PORT", "55432"),
        "user": os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
        "password": os.environ["AGAMI_IT_PG_PASSWORD"],
        "database": os.environ.get("AGAMI_IT_PG_DB", "shop"),
    }


@pytest.fixture
def pg_observer():
    """A live Postgres, plus the SECOND connection the assertions are made from.

    `autocommit` is not a detail: `pg_stat_activity` is served from a per-transaction snapshot of the
    backend-status array, so polling it inside one long transaction returns the same stale rows
    forever. Each poll has to be its own transaction to see the server's current state.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        pytest.skip("set AGAMI_IT_PG_PASSWORD to run the live-Postgres integration test")
    creds = _pg_creds()
    try:
        conn = psycopg2.connect(
            host=creds["host"], port=int(creds["port"]), user=creds["user"],
            password=creds["password"], dbname=creds["database"], connect_timeout=3,
        )
    except Exception as exc:  # no DB in this environment → skip, don't fail
        pytest.skip(f"no reachable Postgres for the integration test ({exc})")
    conn.autocommit = True
    try:
        yield conn, creds
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_overrides():
    # Both overrides are request-scoped ContextVars; isolate this file from whatever set them. The
    # budget below travels by env var rather than by ContextVar on purpose: the executor runs on a
    # thread this test starts, and a new thread begins with an empty context.
    execute_sql._timeout_override.set(None)
    execute_sql._max_rows_override.set(None)
    yield
    execute_sql._timeout_override.set(None)
    execute_sql._max_rows_override.set(None)


def _dsn(creds: dict[str, str]) -> str:
    quote = urllib.parse.quote
    return (
        f"postgresql://{quote(creds['user'])}:{quote(creds['password'])}"
        f"@{creds['host']}:{creds['port']}/{quote(creds['database'])}"
    )


def _our_backends(conn, creds: dict[str, str]) -> list[tuple]:
    """Every backend OTHER than this one whose work is the executor's — the server's own answer to
    "is that statement still there?". `pg_backend_pid()` excludes the observer, whose own query text
    contains the cursor name it is searching for."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pid, state, left(query, 120) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid() AND query LIKE %s",
            (creds["database"], f"%{_CURSOR_NAME}%"),
        )
        return cur.fetchall()


def _wait_until(predicate, limit_s: float, poll_s: float = 0.02) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def _guarded_on_a_thread(sql: str) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def call() -> None:
        try:
            outcome["env"] = execute_sql.execute_guarded(
                sql, PROFILE, None, executor=execute_sql.BUILTIN_EXECUTOR, no_safety=True
            )
        except BaseException as exc:  # the chokepoint is total; record a breach rather than hide it
            outcome["raised"] = exc

    worker = threading.Thread(target=call, name="agami-it-guarded", daemon=True)
    worker.start()
    return worker, outcome


def test_the_cancel_reaches_the_server_and_the_statement_is_gone(pg_observer, monkeypatch):
    """The headline, and the one criterion the client cannot self-certify.

    Three facts in order, from a connection the executor does not own: the statement WAS running on
    the server, the caller got the `resource_limit` refusal, and the statement is no longer there.
    The last one is polled rather than asserted instantaneously — a cancel is a request the backend
    acts on at its next check, not a synchronous return — but the window is two seconds, which a
    statement counting to a hundred billion would still be inside by many minutes.

    The detail assertion is not decoration. `execute_guarded` has TWO time bounds, and the outer one
    (`_execute_bounded`) is exactly the client-only abandon this test exists to rule out: it stops
    waiting, leaves the worker inside the driver, and cannot say the statement stopped. Its refusal
    says so in different words. Asserting the INNER wording is what proves the cancel is the thing
    that ended this, rather than the caller walking away.
    """
    conn, creds = pg_observer
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", _dsn(creds))
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(_BUDGET_S))

    started = time.monotonic()
    worker, outcome = _guarded_on_a_thread(_RUNAWAY_SQL)

    # 1. The server really is running it — otherwise "it is gone" proves nothing.
    assert _wait_until(lambda: bool(_our_backends(conn, creds)), limit_s=_BUDGET_S), (
        "the statement never appeared in pg_stat_activity; the test would prove nothing"
    )

    # 2. The caller gets the refusal, well inside the outer bound (budget + 10s).
    worker.join(timeout=_BUDGET_S + 30)
    assert not worker.is_alive(), "the executor never returned"
    assert "raised" not in outcome, outcome.get("raised")
    elapsed = time.monotonic() - started
    env = outcome["env"]
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert env.data is None
    assert f"{_BUDGET_S}s limit" in env.refusal.detail, env.refusal.detail
    assert "cancelled" in env.refusal.detail, env.refusal.detail  # the inner bound, not the abandon
    assert elapsed < _BUDGET_S + execute_sql._OUTER_BOUND_SKEW_S, f"{elapsed:.1f}s"

    # 3. The server's view: the statement is gone, not merely disowned. A backend left `idle in
    #    transaction` would still be listed here and would fail this assertion, so this covers the
    #    half-abandon (client stopped reading, session still holding the transaction open) as well
    #    as the outright runaway.
    assert _wait_until(lambda: not _our_backends(conn, creds), limit_s=2.0), (
        f"still on the server after the refusal: {_our_backends(conn, creds)}"
    )


def test_the_native_statement_timeout_is_really_set_on_the_session(pg_observer, monkeypatch):
    """`SET LOCAL statement_timeout` is the backstop for the case the watchdog cannot cover — our
    process dying mid-statement — so it has to be proven to have LANDED, not just to have been sent.

    It cannot be read back the ordinary way: `SHOW statement_timeout` is not a SELECT and
    `current_setting()` is a denied function, so neither reaches the executor, and the setting is
    transaction-scoped so it is gone by the time anything else could look. What is left is to let it
    fire. With our watchdog disarmed the statement runs on to the native bound, and the proof is
    Postgres's own wording: it says "statement timeout", where a client `conn.cancel()` says "user
    request". Only the server can produce the former.

    The elapsed floor pins the skew as well as the fact: the native bound is the budget PLUS five
    seconds, deliberately behind the watchdog so the watchdog always wins the race in normal
    operation and stays the sole classification signal.
    """
    conn, creds = pg_observer
    budget_s = 1
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", _dsn(creds))
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", str(budget_s))

    @contextlib.contextmanager
    def _disarmed(cancel, timeout_s):
        # The watchdog, minus the timer: the Event never fires, so nothing client-side stops this.
        yield threading.Event()

    monkeypatch.setattr(execute_sql, "_deadline", _disarmed)

    started = time.monotonic()
    worker, outcome = _guarded_on_a_thread(_RUNAWAY_SQL)
    worker.join(timeout=budget_s + execute_sql._OUTER_BOUND_SKEW_S + 30)
    assert not worker.is_alive(), "the executor never returned"
    elapsed = time.monotonic() - started

    env = outcome["env"]
    # A server-side kill is not our refusal: nothing set the flag, so it is an ordinary database
    # failure — which is exactly why the watchdog is set to fire first in normal operation.
    assert env.status == "failed", env
    assert "statement timeout" in env.failure.message.lower(), env.failure.message
    native_s = budget_s + execute_sql._NATIVE_BOUND_SKEW_S
    assert elapsed >= native_s - 0.5, f"killed after {elapsed:.1f}s, before the {native_s}s bound"
    assert _wait_until(lambda: not _our_backends(conn, creds), limit_s=2.0), (
        f"still on the server after the native timeout: {_our_backends(conn, creds)}"
    )
