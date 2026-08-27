"""A connection the server has closed must be reopened — and never mid-transaction.

**Why this exists.** `Store` opened one Postgres connection and held it for the life of the process.
A deployment that sits idle for an hour has that connection reaped from the server side, and nothing
told the process: `execute` called `conn.cursor()` with no liveness check and there was no reconnect
path anywhere. The instance was then poisoned for as long as it lived, answering every request — not
only the data-heavy ones — with a 500 in about three milliseconds, having attempted no round trip at
all. Observed four times in four days on a pilot deployment, one to two hours each.

**The two exceptions are not interchangeable.** `OperationalError` comes out of `cursor.execute` the
first time a statement meets a closed socket; `InterfaceError` comes out of `conn.cursor()` on every
call after that, once the driver has marked the connection closed. Handling only the second recovers
from everything except the failure that begins each outage.

**And the refusal matters as much as the retry.** Callers write two rows that must land together — a
role change and its audit line. If the connection dies between them the first is already lost, and
reconnecting silently would let the second commit alone: an audit line for a grant that never
happened. So the reconnect is allowed between statements and refused inside a transaction.

No database and no driver: `psycopg2` is stubbed, because the engine that fails is the one the suite
does not run on, and a test that needed a real reaped connection would run nowhere.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from store import Store  # noqa: E402

URL = "postgresql://someone@example.test/db"


class _InterfaceError(Exception):
    """Stands in for `psycopg2.InterfaceError` — the driver refusing a closed connection."""


class _OperationalError(Exception):
    """Stands in for `psycopg2.OperationalError` — the server closing it under a live statement."""


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.description = [("ok",)]

    def execute(self, sql, params=None):
        if self._conn.die_on_execute:
            self._conn.die_on_execute = False
            self._conn.closed = True
            raise _OperationalError("server closed the connection unexpectedly")
        self._conn.statements.append(sql)

    def fetchall(self):
        return [(1,)]


class _Conn:
    """A connection that can be told to die once, either way it dies in production."""

    def __init__(self, *, dead: bool = False, die_on_execute: bool = False) -> None:
        self.closed = dead
        self.die_on_execute = die_on_execute
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        if self.closed:
            raise _InterfaceError("connection already closed")
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """A stub driver, and a record of every reconnect attempted through it."""
    opened: list[_Conn] = []
    module = types.ModuleType("psycopg2")
    module.InterfaceError = _InterfaceError
    module.OperationalError = _OperationalError

    def connect(url):
        conn = _Conn()
        opened.append(conn)
        return conn

    module.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    return opened


def _store(conn: _Conn, *, url: str = URL) -> Store:
    return Store(conn, "postgres", url=url)


# --- the connection comes back ------------------------------------------------------------------


def test_a_connection_closed_by_the_server_is_reopened_and_the_statement_runs(fake_psycopg2):
    """`InterfaceError` from `cursor()` — every request after the first failure of an outage."""
    store = _store(_Conn(dead=True))

    assert store.query("SELECT 1 AS ok") == [{"ok": 1}]
    assert len(fake_psycopg2) == 1, "the connection was not reopened"


def test_a_connection_that_dies_under_a_live_statement_is_reopened(fake_psycopg2):
    """`OperationalError` from `execute()` — the failure that OPENS each outage. A fix that caught
    only the other one would leave this unhandled, and this is the one that happens first."""
    store = _store(_Conn(die_on_execute=True))

    assert store.query("SELECT 1 AS ok") == [{"ok": 1}]
    assert len(fake_psycopg2) == 1


def test_the_statement_reaches_the_new_connection_not_the_dead_one(fake_psycopg2):
    store = _store(_Conn(dead=True))
    store.execute("SELECT 'after the reconnect'")

    assert fake_psycopg2[0].statements == ["SELECT 'after the reconnect'"]


def test_a_reconnect_is_attempted_once_not_in_a_loop(fake_psycopg2, monkeypatch):
    """A provider that is genuinely down must surface as an error, not spin."""
    module = sys.modules["psycopg2"]
    monkeypatch.setattr(module, "connect", lambda url: _Conn(dead=True))
    store = _store(_Conn(dead=True))

    with pytest.raises(_InterfaceError):
        store.execute("SELECT 1")


def test_a_connection_that_cannot_be_reopened_raises_the_original_failure(fake_psycopg2, monkeypatch):
    """The database being unreachable is a real error and must not be disguised as anything else."""
    module = sys.modules["psycopg2"]

    def refuse(url):
        raise _OperationalError("could not connect to server")

    monkeypatch.setattr(module, "connect", refuse)
    store = _store(_Conn(dead=True))

    with pytest.raises(_InterfaceError):
        store.execute("SELECT 1")


# --- and never mid-transaction --------------------------------------------------------------------


def test_a_dead_connection_mid_transaction_refuses_rather_than_reconnecting(fake_psycopg2):
    """**The property this whole guard exists for.**

    Two rows that must land together: a role change and the audit line naming who granted it. The
    first statement has run and is uncommitted; the connection then dies. Reconnecting here would
    let the SECOND statement commit alone — an audit line for a grant that never happened, which is
    exactly what the audit line exists to make impossible. The caller has to see the failure.
    """
    conn = _Conn()
    store = _store(conn)
    store.execute("UPDATE org_membership SET role = 'admin'")  # first half, uncommitted
    conn.closed = True  # the server hangs up between the two

    with pytest.raises(_InterfaceError):
        store.execute("INSERT INTO role_change_audit VALUES ('...')")

    assert fake_psycopg2 == [], "reconnected mid-transaction; the audit line would commit alone"


def test_committing_ends_the_transaction_so_the_next_statement_may_reconnect(fake_psycopg2):
    conn = _Conn()
    store = _store(conn)
    store.execute("INSERT INTO t VALUES (1)")
    store.commit()
    conn.closed = True

    store.execute("SELECT 1")
    assert len(fake_psycopg2) == 1


def test_rolling_back_ends_it_too(fake_psycopg2):
    """Otherwise a connection abandoned by a failed transaction could never reconnect again."""
    conn = _Conn()
    store = _store(conn)
    store.execute("INSERT INTO t VALUES (1)")
    store.rollback()
    conn.closed = True

    store.execute("SELECT 1")
    assert len(fake_psycopg2) == 1
    assert conn.rollbacks == 1


def test_a_statement_that_died_does_not_leave_a_transaction_marked_open(fake_psycopg2):
    """The `OperationalError` path: the statement never ran, so nothing is uncommitted, so the NEXT
    call must still be free to reconnect. Marking the transaction before the statement succeeded
    would wedge the store closed after one failure."""
    store = _store(_Conn(die_on_execute=True))
    store.execute("SELECT 1")  # dies, reconnects, succeeds
    fake_psycopg2[0].closed = True

    store.execute("SELECT 2")
    assert len(fake_psycopg2) == 2


# --- everything else is untouched -------------------------------------------------------------


def test_sqlite_never_tries_to_reconnect(tmp_path):
    """SQLite has no reaped-connection problem, and a Store built by hand has no url to reopen from.
    Both must behave exactly as they did."""
    store = Store.connect("sqlite://" + str(tmp_path / "plain.db"))
    try:
        store.execute("CREATE TABLE t (name TEXT)")
        store.execute("INSERT INTO t (name) VALUES (?)", ("acme",))
        store.commit()
        assert store.query("SELECT name FROM t") == [{"name": "acme"}]
    finally:
        store.close()


def test_a_store_with_no_url_raises_rather_than_reconnecting(fake_psycopg2):
    """Nothing to reopen from, so the failure is the answer."""
    store = _store(_Conn(dead=True), url="")

    with pytest.raises(_InterfaceError):
        store.execute("SELECT 1")
    assert fake_psycopg2 == []


# --- the case the fix exists for, which the first version of it did not handle -------------------


def test_a_run_of_reads_that_never_commits_can_still_reconnect(fake_psycopg2):
    """**This is the failing production path, and it nearly went unfixed.**

    The piece that resolves a caller's org reads five tables in a row and never commits — there is
    nothing to commit. An earlier version of this guard marked the transaction on *any* statement,
    which meant that caller reconnected once and was then refused for the life of the process. The
    fix would have shipped, looked right, and left the outage exactly where it was.

    A read leaves nothing uncommitted, so it must not close the door behind it.
    """
    conn = _Conn()
    store = _store(conn)
    for table in ("org_membership", "organization", "super_admin", "org_role", "org_default"):
        store.query(f"SELECT * FROM {table}")  # no commit, ever
    conn.closed = True  # an hour later, the server hangs up

    assert store.query("SELECT * FROM org_membership") == [{"ok": 1}]
    assert len(fake_psycopg2) == 1, "a read-only caller could not reconnect"


def test_an_uncommitted_write_still_closes_the_door(fake_psycopg2):
    """The other half. Reads staying open must not weaken the guard for writes."""
    conn = _Conn()
    store = _store(conn)
    store.query("SELECT * FROM org_membership")  # harmless
    store.execute("UPDATE org_membership SET role = 'admin'")  # not harmless
    conn.closed = True

    with pytest.raises(_InterfaceError):
        store.execute("INSERT INTO role_change_audit VALUES ('...')")
    assert fake_psycopg2 == []


@pytest.mark.parametrize(
    "sql, is_read",
    [
        ("SELECT 1", True),
        ("  select 1", True),
        ("\n  SELECT\n  *\n  FROM t", True),
        ("INSERT INTO t VALUES (1)", False),
        ("UPDATE t SET a = 1", False),
        ("DELETE FROM t", False),
        ("CREATE TABLE t (a TEXT)", False),
        # A CTE is usually a read and Postgres lets it write, so it is treated as a write. Being
        # wrong in this direction costs a refused reconnect; the other direction costs a lost row.
        ("WITH x AS (SELECT 1) SELECT * FROM x", False),
        ("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x", False),
        # A word boundary, not a prefix: an identifier that merely starts with those six letters
        # is not a query, and reading it as one is the direction that loses data.
        ("SELECTED", False),
        ("SELECT_ALL()", False),
        ("SELECT*FROM t", True),
        ("SELECT(1)", True),
    ],
)
def test_which_statements_count_as_reads(sql, is_read):
    from store import _is_read

    assert _is_read(sql) is is_read
