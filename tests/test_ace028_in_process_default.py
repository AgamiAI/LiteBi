"""ACE-028 — in-process execution is the HTTP server's default (no fork), and the per-call row cap is
request-scoped so concurrent in-process queries can't stomp each other's cap.

Two things this pins:
  1. `default_adapters()` carries `BUILTIN_EXECUTOR`, so `create_app()` runs execution in-process; the
     stdio/CLI path (no injected executor) still forks.
  2. `_max_rows_override` is a `ContextVar` — set in one context does not leak into another, which is
     what makes it safe once concurrent requests share the server process.
"""

from __future__ import annotations

import contextvars
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    import tools

    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


# --- the row cap is request-scoped (ContextVar), not a shared global -----------------------------


def test_row_cap_is_isolated_per_context(monkeypatch):
    # Two independent contexts set different caps; neither sees the other's. This is the property that
    # makes in-process serving safe under concurrent requests (each runs in its own copied context).
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")

    def _cap_with_override(n: int | None) -> int:
        execute_sql._max_rows_override.set(n)
        return execute_sql._resolve_row_cap()

    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()
    cap_a = ctx_a.run(_cap_with_override, 10)
    cap_b = ctx_b.run(_cap_with_override, 500)

    assert cap_a == 10 and cap_b == 500  # each context kept its own cap
    assert execute_sql._max_rows_override.get() is None  # the outer context is untouched by either


def test_run_blocking_isolates_the_cap_across_worker_threads(monkeypatch):
    # The real offload path: two `run_blocking` calls (worker threads, each a copied context) set
    # different caps concurrently and must not see each other's. Guards the ACE-028 concurrency claim.
    anyio = pytest.importorskip("anyio")
    from async_offload import run_blocking

    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "1000")
    seen: dict[str, int] = {}

    def _work(key: str, cap: int):
        execute_sql._max_rows_override.set(cap)
        # a tiny bit of work so the two overlap; the ContextVar must stay this call's value throughout
        for _ in range(1000):
            pass
        seen[key] = execute_sql._resolve_row_cap()

    async def _call():
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_blocking, _work, "a", 10)
            tg.start_soon(run_blocking, _work, "b", 500)

    anyio.run(_call)
    assert seen == {"a": 10, "b": 500}  # no cross-request stomp


# --- the HTTP server defaults to in-process; stdio/CLI still forks --------------------------------


def test_default_adapters_carry_the_builtin_executor():
    import mcp_http

    assert mcp_http.default_adapters().executor is execute_sql.BUILTIN_EXECUTOR


def test_http_server_runs_in_process_by_default_no_fork(monkeypatch):
    # A default create_app() registers the built-in executor, so tool_execute_sql runs in-process —
    # NO subprocess fork.
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agami.example.test")
    import mcp_http
    import tools

    mcp_http.create_app()  # default adapters
    assert tools._INJECTED_EXECUTOR is execute_sql.BUILTIN_EXECUTOR

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(
        tools.subprocess, "run", lambda *a, **k: pytest.fail("HTTP default must not fork")
    )

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1 AS n", "datasource": "acme"}))
    assert out["data"]["columns"] == ["n"]  # ran in-process, no subprocess


def test_stdio_cli_path_still_forks_when_no_executor_injected(monkeypatch):
    # With no injected executor (the stdio/CLI default), tool_execute_sql shells the subprocess.
    import tools

    tools.set_injected_executor(None)
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "n\r\n1\r\n"
        stderr = ""

    def _fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(tools.subprocess, "run", _fake_run)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme"}))
    assert "-m" in captured["cmd"] and "execute_sql" in captured["cmd"]  # forked
    assert out["data"]["rows"] == [["1"]]


def test_in_process_default_matches_subprocess_result_envelope(monkeypatch, tmp_path):
    # Parity: the in-process default and the REAL subprocess fork return the same successful envelope
    # for the same query. Both resolve creds from the SAME file (the subprocess is a separate process,
    # so a `_load_credentials` monkeypatch wouldn't reach it) — the sqlite profile has no model, so the
    # safety pass is inert locally on both.
    import sqlite3

    import agami_paths
    import tools

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER, s TEXT)")
    con.executemany("INSERT INTO t (n, s) VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()

    # A real credentials file both paths read: the subprocess via AGAMI_ARTIFACTS_DIR, the in-process
    # module via its CREDENTIALS_PATH global (both point at the same file).
    art = tmp_path / "art"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text(f"[acme]\ntype = sqlite\npath = {db}\n")
    creds.chmod(0o600)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", creds)
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(agami_paths, "credentials_path", lambda: creds)
    args = {"sql": "SELECT n, s FROM t ORDER BY n", "datasource": "acme"}

    tools.set_injected_executor(None)  # subprocess fork through `python -m execute_sql`
    sub = json.loads(tools.tool_execute_sql(args))

    tools.set_injected_executor(
        execute_sql.BUILTIN_EXECUTOR
    )  # in-process, as the HTTP default does
    inproc = json.loads(tools.tool_execute_sql(args))

    assert "columns" in sub["data"], sub  # subprocess actually produced a result (not a creds error)
    for key in ("columns", "rows", "row_count", "truncated"):
        assert sub["data"][key] == inproc["data"][key]  # identical envelope whichever ran it
