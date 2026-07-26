"""ACE-038 (row-cap) + ACE-044 (per-call cap) — bound result sets at the single materialization
chokepoint: `fetchmany(cap + 1)`, never `fetchall`, truncate at the cap and flag it. The SQL is
never modified (no injected LIMIT). Effective cap = min(--max-rows, AGAMI_SQL_MAX_ROWS, 1000).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_override():
    # _max_rows_override is a request-scoped ContextVar (ACE-028); isolate every test from it.
    execute_sql._max_rows_override.set(None)
    yield
    execute_sql._max_rows_override.set(None)


class _FakeCur:
    """A cursor that would return `nrows` rows; records how the sink pulls them."""

    def __init__(self, ncols: int, nrows: int):
        self.description = [(f"c{i}",) for i in range(ncols)]
        self._rows = [tuple(range(ncols)) for _ in range(nrows)]
        self.fetchmany_args: list[int] = []
        self.fetchall_called = False

    def fetchmany(self, n: int):
        self.fetchmany_args.append(n)
        return self._rows[:n]

    def fetchall(self):
        self.fetchall_called = True
        return self._rows


class _NamedCur:
    """Mimics a psycopg2 **server-side (named) cursor**: `description` is None until the first fetch,
    then reports the columns. The Postgres/Redshift path (`cursor(name="agami_bounded")`, ACE-038)
    behaves exactly this way — the previous `_FakeCur` set `description` at construction and so never
    reproduced it, masking a bug where every real Postgres row was dropped."""

    def __init__(self, columns: list[str], rows: list[tuple]):
        self._columns = columns
        self._rows = rows
        self.description = None  # None until the first fetch, like a real named cursor

    def fetchmany(self, n: int):
        self.description = [(c,) for c in self._columns]  # populated only after the first fetch
        return self._rows[:n]


def test_collect_cursor_reads_description_after_fetch_for_named_cursors(monkeypatch):
    # Regression: a server-side named cursor reports description=None until the first fetch, so
    # `_collect_cursor` must fetch FIRST. Under the pre-fix (read-before-fetch) this returned empty
    # columns/rows — i.e. every Postgres/Redshift query silently returned 0 rows.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    cur = _NamedCur(["name", "region"], [("Alice", "NA"), ("Bob", "EU")])

    result = execute_sql._collect_cursor(cur)

    assert result.columns == ["name", "region"]  # would be [] before the fix
    assert result.rows == [("Alice", "NA"), ("Bob", "EU")]
    assert result.truncated is False


def test_sink_bounds_at_cap_and_flags_truncation(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")
    cur = _FakeCur(2, 10)  # 10 rows available, cap 3
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert len(out.out.strip().splitlines()) == 1 + 3  # header + exactly cap rows written
    assert cur.fetchmany_args == [4] and not cur.fetchall_called  # fetchmany(cap+1), never fetchall
    assert json.loads(out.err.strip())["truncated"]["row_cap"] == 3


def test_sink_no_flag_when_result_is_within_cap(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5")
    cur = _FakeCur(1, 2)  # 2 rows, cap 5
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert len(out.out.strip().splitlines()) == 1 + 2  # header + all rows
    assert out.err.strip() == ""  # not truncated → no flag


def test_sink_exactly_cap_rows_is_complete_not_truncated(monkeypatch, capsys):
    # The off-by-one boundary: a result of EXACTLY cap rows is complete — it must NOT flag truncation
    # (only a (cap+1)th row does). Guards `len(rows) > cap` against a `>= cap` regression.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")
    cur = _FakeCur(1, 3)  # exactly cap rows
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert len(out.out.strip().splitlines()) == 1 + 3  # header + all 3 rows written
    assert out.err.strip() == ""  # exactly cap → NOT truncated


def test_sink_empty_result_writes_header_only_no_flag(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5")
    cur = _FakeCur(2, 0)  # columns present, zero rows
    execute_sql._write_cursor_csv(cur)
    out = capsys.readouterr()

    assert out.out.strip().splitlines() == ["c0,c1"]  # header only
    assert out.err.strip() == ""  # no rows → no truncation flag


def test_effective_cap_is_min_of_env_and_per_call(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    assert execute_sql._resolve_row_cap() == 1000  # env only
    execute_sql._max_rows_override.set(50)
    assert execute_sql._resolve_row_cap() == 50  # per-call is smaller → wins
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "20")
    assert execute_sql._resolve_row_cap() == 20  # env smaller than per-call → wins
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
    execute_sql._max_rows_override.set(None)
    assert execute_sql._resolve_row_cap() == 1000  # missing env → default
    # The env is the operator's DEPLOYMENT cap, not a hard 1000 ceiling — it may raise the default.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5000")
    assert execute_sql._resolve_row_cap() == 5000
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "0")  # invalid → falls back to default, never 0
    assert execute_sql._resolve_row_cap() == 1000


def test_sqlite_end_to_end_caps_without_rewriting_sql(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER)")
    con.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(10)])
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "4")
    rc = execute_sql._execute_sqlite({"path": str(db)}, "SELECT n FROM t ORDER BY n")
    out = capsys.readouterr()

    assert rc == 0
    lines = out.out.strip().splitlines()
    assert lines[0] == "n" and lines[1:] == ["0", "1", "2", "3"]  # first 4 by the query's own ORDER BY
    assert json.loads(out.err.strip())["truncated"]["row_cap"] == 4
    # The cap came from the bounded fetch, not a rewrite: the SQL passed to sqlite is the caller's
    # verbatim (no LIMIT) — _write_cursor_csv never sees or edits the SQL.


def test_postgres_uses_a_server_side_named_cursor(monkeypatch, capsys):
    # Postgres needs a NAMED (server-side) cursor so the cap bounds transfer — psycopg2's default
    # cursor buffers the whole result. Inject a fake psycopg2 and assert the cursor is named + the
    # SQL is passed verbatim + the bounded fetchmany(cap+1) is used.
    seen: dict = {}

    class FakeCur:
        def __init__(self, name):
            seen["name"] = name
            self.description = [("n",)]
            self.itersize = None
            self._rows = [(i,) for i in range(3)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            seen["sql"] = sql

        def fetchmany(self, n):
            seen["fetchmany"] = n
            return self._rows[:n]

    class FakeConn:
        def cursor(self, name=None):
            return FakeCur(name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cancel(self):  # the per-statement watchdog's cancel primitive (no-op here — never fires)
            pass

        def close(self):
            pass

    class FakePG:
        @staticmethod
        def connect(**kw):
            return FakeConn()

    monkeypatch.setitem(sys.modules, "psycopg2", FakePG)
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2")
    creds = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}

    rc = execute_sql._execute_postgres(creds, "SELECT n FROM t")
    assert rc == 0
    assert seen["name"] == "agami_bounded"     # server-side cursor (bounds transfer, not just writes)
    assert seen["sql"] == "SELECT n FROM t"    # SQL verbatim — no injected LIMIT
    assert seen["fetchmany"] == 3              # cap(2)+1


def test_bigquery_bounds_and_flags_like_the_sink(monkeypatch, capsys):
    # BigQuery has no DB-API cursor so it can't use _write_cursor_csv; it must apply the SAME cap +
    # truncation flag itself. Mock the google client and drive a 10-row result at cap 4.
    import types

    class _Field:
        def __init__(self, name):
            self.name = name

    class _Res:
        schema = [_Field("n")]

        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    class _Job:
        def __init__(self, total):
            self._total = total

        def result(self, max_results=None):
            k = self._total if max_results is None else min(self._total, max_results)
            return _Res([[i] for i in range(k)])

    class _Client:
        def __init__(self, **kw):
            pass

        def query(self, sql, **kw):
            return _Job(10)  # 10 rows available

    gcloud = types.ModuleType("google.cloud")
    gcloud.bigquery = types.SimpleNamespace(Client=_Client, QueryJobConfig=lambda **k: object())
    goauth = types.ModuleType("google.oauth2")
    goauth.service_account = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "google.cloud", gcloud)
    monkeypatch.setitem(sys.modules, "google.oauth2", goauth)
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "4")

    rc = execute_sql._execute_bigquery({"project": "p"}, "SELECT n FROM t")
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip().splitlines() == ["n", "0", "1", "2", "3"]  # capped at 4, not all 10
    assert json.loads(out.err.strip())["truncated"]["row_cap"] == 4


def test_executor_truncated_parses_the_stderr_flag():
    import tools

    assert tools._executor_truncated('{"truncated": {"row_cap": 1000}}') is True
    # mixed with other notices on stderr
    assert tools._executor_truncated('[agami] applied default_filters: x\n{"truncated": {"row_cap": 5}}') is True
    assert tools._executor_truncated('[agami] applied default_filters: x') is False
    assert tools._executor_truncated("") is False
    assert tools._executor_truncated(None) is False
    assert tools._executor_truncated('{"error": {"kind": "permission"}}') is False  # not a truncation


def test_tool_execute_sql_passes_max_rows_and_surfaces_truncation(monkeypatch):
    import tools

    captured: dict = {}

    class FakeProc:
        returncode = 0
        stdout = "n\n0\n1\n"
        stderr = '{"truncated": {"row_cap": 2}}'

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    monkeypatch.setattr(tools, "_resolve_receipt", lambda *a: None)

    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme", "max_rows": 2}))
    cmd = captured["cmd"]
    assert "--max-rows" in cmd and cmd[cmd.index("--max-rows") + 1] == "2"  # capped at the source
    assert resp["data"]["truncated"] is True  # executor's flag surfaced into the Envelope's data
    assert resp["applied"] == [{"row_cap": resp["data"]["row_count"]}]  # bound noted in `applied`
