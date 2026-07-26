"""ACE-038 — per-statement timeout (the availability guarantee).

The row-cap half shipped via ACE-044; this covers the timeout. The genuine-cancel proof runs
in-process against SQLite (a real recursive-CTE bomb is interrupted by the wall-clock watchdog and
returns a `resource_limit` refusal); the cloud engines' native mechanisms are asserted-by-config in
unit tests (CI has no live warehouses — same constraint as ACE-037's SC5).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

# A recursive CTE that counts to 2 billion — seconds of pure DB-side work, no memory growth (it
# aggregates, storing no rows), so a sub-second timeout must interrupt it mid-flight.
_BOMB = (
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 2000000000) "
    "SELECT count(*) FROM r"
)


# ── _resolve_timeout_s ───────────────────────────────────────────────────────


def test_timeout_default_is_30(monkeypatch):
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)
    assert execute_sql._resolve_timeout_s() == 30


@pytest.mark.parametrize("val,expected", [("5", 5), ("", 30), ("nope", 30), ("0", 30), ("-3", 30)])
def test_timeout_env_override_and_invalid_fall_back(monkeypatch, val, expected):
    # A missing / invalid / non-positive value falls back to the default — never "0 = unlimited".
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", val)
    assert execute_sql._resolve_timeout_s() == expected


def test_deadline_uses_the_passed_timeout_not_a_reread_env(monkeypatch):
    # Copilot review: the watchdog Timer must key off the timeout the caller already resolved (and
    # feeds to _deadline_hit), NOT a fresh env read — else a mid-flight change to AGAMI_SQL_TIMEOUT_S
    # makes the watchdog duration and the timeout classification diverge. Pin that _deadline schedules
    # the Timer with the PASSED value, not the (different) env value a re-read would pick up.
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "99")  # what a re-read inside _deadline would have used
    captured = {}

    class _FakeTimer:
        def __init__(self, interval, fn):
            captured["interval"] = interval

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(execute_sql.threading, "Timer", _FakeTimer)
    with execute_sql._deadline(lambda: None, timeout_s=7):
        pass
    assert captured["interval"] == 7  # the caller's resolved value, not 99 from the env


# ── the genuine cancel (SQLite, in-process) ──────────────────────────────────


def test_sqlite_runaway_is_killed_with_resource_limit(tmp_path, monkeypatch, capsys):
    # The watchdog genuinely cancels an in-flight query: a recursive-CTE bomb under a 1 s timeout is
    # interrupted and returns a resource_limit refusal — not hung, not a generic execution error.
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "1")
    db = tmp_path / "t.db"
    sqlite3.connect(str(db)).close()  # create the file

    code = execute_sql._execute_sqlite({"path": str(db)}, _BOMB)

    assert code == 1  # killed (refused), not 0 (success) or 5 (generic error)
    out = capsys.readouterr()
    refusal = json.loads(out.err.strip().splitlines()[-1])["refusal"]
    assert refusal["kind"] == "resource_limit"
    assert "timeout" in refusal["reason"]
    assert out.out == ""  # the cancelled aggregate wrote nothing to stdout — no partial data


def test_duckdb_runaway_is_killed_with_resource_limit(tmp_path, monkeypatch, capsys):
    # The other in-process engine: conn.interrupt() genuinely aborts a DuckDB runaway the same way.
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "1")
    db = tmp_path / "t.duckdb"
    duckdb.connect(str(db)).close()  # create the file so _execute_duckdb can open it read-only

    code = execute_sql._execute_duckdb({"path": str(db)}, _BOMB)

    assert code == 1
    out = capsys.readouterr()
    refusal = json.loads(out.err.strip().splitlines()[-1])["refusal"]
    assert refusal["kind"] == "resource_limit"


def test_sqlite_fast_query_still_returns(tmp_path, monkeypatch, capsys):
    # A quick query well under the timeout is unaffected — the watchdog is a bound, not a tax.
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (1), (2), (3)")
    con.commit()
    con.close()

    code = execute_sql._execute_sqlite({"path": str(db)}, "SELECT count(*) AS n FROM t")

    assert code == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["n", "3"]  # header + the real result
    assert '"resource_limit"' not in out.err


# ── native server-side timeouts (asserted-by-config) ─────────────────────────
#
# CI has no live warehouse, so each engine's native timeout is asserted by construction: the right
# param, in the right UNITS, is handed to the driver. This guards the ms-vs-s mix directly — a
# `_timeout_ms()`/`_resolve_timeout_s()` swap would make an engine's timeout 1000x wrong and silently
# disable the availability guarantee. (Mirrors the ACE-037 SC5 call — the runnable path is proven
# in-process, the cloud path asserted by config.)


class _RecCursor:
    """A DB-API cursor that records every executed statement and yields one row."""

    description = [("n",)]
    itersize = 0

    def __init__(self, log):
        self._log = log

    def execute(self, sql, *_a):
        self._log.append(sql)

    def fetchmany(self, _n):
        return [(1,)]

    def cancel(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _RecConn:
    """A DB-API connection over a shared statement log; `cursor(...)` ignores kwargs like `name`."""

    def __init__(self, log):
        self._log = log

    def cursor(self, *_a, **_k):
        return _RecCursor(self._log)

    def cancel(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _set_timeout_env(monkeypatch, timeout_s):
    if timeout_s:
        monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", timeout_s)
    else:
        monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)


@pytest.mark.parametrize("timeout_s,expected_ms", [("30", 30000), ("5", 5000), ("", 30000)])
def test_postgres_sets_statement_timeout_via_set_local(monkeypatch, timeout_s, expected_ms):
    log: list = []
    connect_kw: dict = {}
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda **kw: (connect_kw.update(kw), _RecConn(log))[1]
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    _set_timeout_env(monkeypatch, timeout_s)

    creds = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}
    code = execute_sql._execute_postgres(creds, "SELECT 1 AS n")

    assert code == 0
    # Genuine server-side abort, delivered per-transaction via SET LOCAL (ms)...
    assert f"SET LOCAL statement_timeout = {expected_ms}" in log
    # ...NOT the libpq `options` startup param a transaction-mode pooler (Supabase/PgBouncer) rejects.
    assert "options" not in connect_kw


def test_mysql_native_timeouts_and_client_read_timeout(monkeypatch, capsys):
    log: list = []
    connect_kw: dict = {}
    fake = types.ModuleType("pymysql")
    fake.connect = lambda **kw: (connect_kw.update(kw), _RecConn(log))[1]
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    creds = {"host": "h", "port": "3306", "user": "u", "password": "p", "database": "d"}
    code = execute_sql._execute_mysql(creds, "SELECT 1 AS n")

    assert code == 0
    # The reliable client bound (a close from the watchdog thread can't unblock a recv on Linux).
    assert connect_kw["read_timeout"] == 30 and connect_kw["write_timeout"] == 30
    assert "SET SESSION max_execution_time=30000" in log  # MySQL 5.7.8+ (ms)
    assert "SET SESSION max_statement_time=30" in log  # MariaDB (seconds)


def test_snowflake_native_statement_timeout(monkeypatch):
    connect_kw: dict = {}
    connector = types.ModuleType("snowflake.connector")
    connector.connect = lambda **kw: (connect_kw.update(kw), _RecConn([]))[1]
    pkg = types.ModuleType("snowflake")
    pkg.connector = connector
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    code = execute_sql._execute_snowflake(
        {"account": "a", "user": "u", "password": "p"}, "SELECT 1 AS n"
    )

    assert code == 0
    assert connect_kw["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 30}  # seconds


def test_bigquery_native_job_timeout(monkeypatch):
    cfg: dict = {}

    class _Job:
        def result(self, max_results=None):
            return types.SimpleNamespace(schema=[])

        def cancel(self):
            pass

    class _Client:
        def __init__(self, **_kw):
            pass

        def query(self, _sql, **_kw):
            return _Job()

    gcloud = types.ModuleType("google.cloud")
    gcloud.bigquery = types.SimpleNamespace(
        Client=_Client, QueryJobConfig=lambda **k: (cfg.update(k), object())[1]
    )
    goauth = types.ModuleType("google.oauth2")
    goauth.service_account = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "google.cloud", gcloud)
    monkeypatch.setitem(sys.modules, "google.oauth2", goauth)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    code = execute_sql._execute_bigquery({"project": "p"}, "SELECT 1 AS n")

    assert code == 0
    assert cfg["job_timeout_ms"] == 30000  # milliseconds


def test_sqlserver_native_query_timeout(monkeypatch):
    connect_kw: dict = {}
    fake = types.ModuleType("pymssql")
    fake.connect = lambda **kw: (connect_kw.update(kw), _RecConn([]))[1]
    monkeypatch.setitem(sys.modules, "pymssql", fake)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    code = execute_sql._execute_sqlserver(
        {"host": "h", "user": "u", "password": "p"}, "SELECT 1 AS n"
    )

    assert code == 0
    assert connect_kw["timeout"] == 30  # pymssql per-query timeout (seconds)


def test_oracle_native_call_timeout(monkeypatch):
    conn = _RecConn([])
    fake = types.ModuleType("oracledb")
    fake.connect = lambda **_kw: conn
    fake.makedsn = lambda *_a, **_k: "dsn"
    monkeypatch.setitem(sys.modules, "oracledb", fake)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    code = execute_sql._execute_oracle({"user": "u", "password": "p", "dsn": "d"}, "SELECT 1 AS n")

    assert code == 0
    assert conn.call_timeout == 30000  # oracledb round-trip timeout (ms)


def test_trino_native_query_max_run_time(monkeypatch):
    connect_kw: dict = {}
    fake = types.ModuleType("trino")
    fake.dbapi = types.SimpleNamespace(
        connect=lambda **kw: (connect_kw.update(kw), _RecConn([]))[1]
    )
    fake.auth = types.SimpleNamespace(BasicAuthentication=lambda *_a: object())
    monkeypatch.setitem(sys.modules, "trino", fake)
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "30")

    code = execute_sql._execute_trino({"host": "h", "user": "u"}, "SELECT 1 AS n")

    assert code == 0
    assert connect_kw["session_properties"] == {"query_max_run_time": "30s"}  # seconds-string


def test_postgres_timeout_returns_refusal_exit_even_when_cursor_close_raises(monkeypatch, capsys):
    # A cancelled Postgres query aborts the txn, so closing the server-side cursor raises. That close
    # must NOT mask _ResourceLimit — the executor must still return exit 1 with the resource_limit
    # refusal (not exit 5 / a generic error). Deterministic: the watchdog cancel unblocks execute().
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "1")
    cancelled = threading.Event()

    class _NamedCursor:
        description = [("n",)]
        itersize = 0

        def execute(self, _sql):
            if cancelled.wait(
                timeout=5
            ):  # blocks like a real slow query until the watchdog cancels
                raise RuntimeError("canceling statement due to statement timeout")

        def fetchmany(self, _n):
            return []

        def close(self):
            if cancelled.is_set():
                raise RuntimeError("current transaction is aborted")  # the masking error

    class _SetupCursor:
        def execute(self, _sql):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Conn:
        def cursor(self, name=None):
            return _NamedCursor() if name else _SetupCursor()

        def cancel(self):
            cancelled.set()  # the watchdog's pg_cancel — unblocks the blocked execute()

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    fake = types.ModuleType("psycopg2")
    fake.connect = lambda **_kw: _Conn()
    monkeypatch.setitem(sys.modules, "psycopg2", fake)

    creds = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}
    code = execute_sql._execute_postgres(creds, "SELECT 1 AS n")

    assert code == 1  # refused, NOT 5 — the cursor-close error did not mask _ResourceLimit
    refusal = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["refusal"]
    assert refusal["kind"] == "resource_limit"
