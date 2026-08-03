"""ACE-038 (row-cap) + ACE-044 (per-call cap) — bound result sets at the single materialization
chokepoint: `fetchmany(cap + 1)`, never `fetchall`. The SQL is never modified (no injected LIMIT).
Effective cap = AGAMI_SQL_MAX_ROWS, default 1000.

**What ACE-087 changed here.** The bound itself is untouched and is still what these tests pin: the
fetch stops at cap+1, never `fetchall`, and the (cap+1)th row is what says "there was more". What
that signal now TRIGGERS moved out of this file — it used to trim to the cap and write a
`{"truncated": …}` marker to stderr, and it is now a `resource_limit` refusal built at
`execute_guarded`. The refusal is pinned in `test_ace087_result_bound.py`; the per-call cap these
tests also covered is gone with `--max-rows`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402


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


def test_the_fetch_stops_at_cap_plus_one_and_reports_the_overflow(monkeypatch):
    """The bound itself, which is the half of this file ACE-087 did not touch.

    `fetchmany(cap + 1)` and never `fetchall` is what keeps a huge result from being buffered whole,
    and it is asserted on the FETCH rather than on the returned rows: a `fetchall` that sliced
    afterwards would produce an identical `ExecResult` and cost the memory this exists to save.
    Detection still costs exactly one extra row — no counting query runs first."""
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")
    cur = _FakeCur(2, 10)  # 10 rows available, cap 3

    result = execute_sql._collect_cursor(cur)

    assert cur.fetchmany_args == [4] and not cur.fetchall_called  # fetchmany(cap+1), never fetchall
    assert result.truncated is True  # a (cap+1)th row existed
    assert len(result.rows) == 3  # what it holds; `execute_guarded` discards these and refuses


def test_a_result_within_the_cap_is_not_flagged(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5")
    cur = _FakeCur(1, 2)  # 2 rows, cap 5

    result = execute_sql._collect_cursor(cur)
    execute_sql._emit_result_csv(result)
    out = capsys.readouterr()

    assert result.truncated is False
    assert len(out.out.strip().splitlines()) == 1 + 2  # header + all rows
    assert out.err.strip() == ""  # nothing rides on stderr beside the rows


def test_exactly_cap_rows_is_complete_not_overflowing(monkeypatch, capsys):
    # The off-by-one boundary, and it matters more than it used to: a result of EXACTLY cap rows is
    # complete, and a `>= cap` regression here would now REFUSE it rather than merely mislabel it.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")
    cur = _FakeCur(1, 3)  # exactly cap rows

    result = execute_sql._collect_cursor(cur)
    execute_sql._emit_result_csv(result)
    out = capsys.readouterr()

    assert result.truncated is False  # exactly cap → complete
    assert len(out.out.strip().splitlines()) == 1 + 3  # header + all 3 rows written


def test_an_empty_result_writes_the_header_only(monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "5")
    cur = _FakeCur(2, 0)  # columns present, zero rows

    result = execute_sql._collect_cursor(cur)
    execute_sql._emit_result_csv(result)
    out = capsys.readouterr()

    assert result.truncated is False
    assert out.out.strip().splitlines() == ["c0,c1"]  # header only


def test_the_cap_comes_from_the_deployment_env_alone(monkeypatch):
    """The per-call half of this test went with `--max-rows` (ACE-087). What it asserted — that a
    caller could lower the cap for one call — is no longer a behaviour: the one thing a caller might
    know better than the operator is that it wants MORE rows, which a lowering-only override could
    never express. The env assertions below are ACE-038's and are unchanged."""
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    assert execute_sql._resolve_row_cap() == 1000  # env only
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "20")
    assert execute_sql._resolve_row_cap() == 20
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
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

    monkeypatch.setattr(
        execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": str(db)},
    )

    # Over the cap: refused, with none of the four rows it had in hand (ACE-087). This test asserted
    # `rc == 0` and a four-row CSV before, which is the behaviour the spec calls an arbitrary sample
    # presented as the answer.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "4")
    env = execute_sql.execute_guarded(
        "SELECT n FROM t ORDER BY n", "acme", None,
        executor=execute_sql.BUILTIN_EXECUTOR, no_safety=True,
    )

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    assert env.data is None

    # Under the cap the same statement runs untouched and returns every row it asked for. The cap
    # came from the bounded fetch, never a rewrite: no LIMIT is injected on either path, which
    # `test_ace093_byte_identity` pins independently.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "20")
    env = execute_sql.execute_guarded(
        "SELECT n FROM t ORDER BY n", "acme", None,
        executor=execute_sql.BUILTIN_EXECUTOR, no_safety=True,
    )

    assert env.status == "ok"
    assert env.data.rows == [(i,) for i in range(10)]


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

        def execute(self, sql, params=None):
            # `params` accepts the native server-side bound the engine now sets first
            # (`SET LOCAL statement_timeout = %s`, on its own client-side cursor). The caller's
            # statement runs last, so `seen["sql"]` still ends up holding it.
            seen["sql"] = sql

        def fetchmany(self, n):
            seen["fetchmany"] = n
            return self._rows[:n]

        def close(self):
            seen["closed"] = True

    class FakeConn:
        def cursor(self, name=None):
            return FakeCur(name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cancel(self):
            # The per-statement watchdog arms `connection.cancel` on this engine; never fired here.
            seen["cancelled"] = True

        def close(self):
            pass

    class FakePG:
        @staticmethod
        def connect(**kw):
            return FakeConn()

    monkeypatch.setitem(sys.modules, "psycopg2", FakePG)
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2")
    creds = {"host": "h", "port": "5432", "user": "u", "password": "p", "database": "d"}

    # Driven through `_run_postgres`, the shared connect-and-run. It was `_execute_postgres`, one of
    # the per-engine CSV wrappers ACE-087 deleted as a route around the chokepoint.
    result = execute_sql._run_postgres(creds, "SELECT n FROM t")
    assert result.truncated is True             # 3 rows available at cap 2
    assert seen["name"] == "agami_bounded"     # server-side cursor (bounds transfer, not just writes)
    assert seen["sql"] == "SELECT n FROM t"    # SQL verbatim — no injected LIMIT
    assert seen["fetchmany"] == 3              # cap(2)+1


def test_bigquery_bounds_and_reports_overflow_like_the_cursor_path(monkeypatch):
    # BigQuery has no DB-API cursor so it cannot go through `_collect_cursor`; it must apply the SAME
    # cap and set the SAME flag itself. That is exactly why the verdict reads `ExecResult.truncated`
    # at the chokepoint rather than living in `_collect_cursor` (ACE-087). Mock the google client and
    # drive a 10-row result at cap 4.
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

    result = execute_sql._run_bigquery({"project": "p"}, "SELECT n FROM t")

    assert result.truncated is True                       # 10 available at cap 4
    assert result.rows == [(i,) for i in range(4)]        # capped at 4, not all 10


def test_the_fork_command_carries_no_per_call_cap(monkeypatch):
    """The inverse of the assertion this replaces (ACE-087).

    It used to pin that a caller's `max_rows` reached the child as `--max-rows N`. Both ends of that
    are gone: the tool takes no such argument and the child's argparser has no such flag, so a
    parent that still appended one would make every forked call die on an unrecognized argument."""
    import tools

    captured: dict = {}

    class FakeProc:
        returncode = 0
        stdout = "n\n0\n1\n"
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    # There is no model behind this fake fork, so the receipt builder is stubbed. It may not return
    # `None` (ACE-088 SC-5 — a receipt that could not be built is an `undetermined` receipt, not an
    # absence).
    monkeypatch.setattr(
        tools, "_resolve_receipt",
        lambda *a, **kw: guardrail.undetermined_receipt("stubbed by the test"),
    )

    tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme"})

    assert "--max-rows" not in captured["cmd"]
