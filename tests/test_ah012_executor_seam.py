"""AH-012 — the executor seam. `execute_sql` is split into a single un-bypassable guarded envelope
(`execute_guarded`: read-only guard -> semantic-model safety -> resolve datasource ->
`executor.execute(vetted_sql)`) and a swappable `Executor` port. These tests pin the load-bearing
invariants: the guard runs BEFORE the executor, the executor only ever sees already-vetted SQL, and
there is no path to an executor around the guard (fail-closed, REQ-002/REQ-014).

On result fidelity: the built-in executor returns NATIVE-typed rows (`ExecResult`) — NULL as None,
ints as int — and the subprocess wire still serializes byte-identical CSV. The in-process TOOL edge
(`tools._run_in_process`) currently textualizes those rows to match the CSV wire so both execution
paths return identical JSON; native-typed rows at the MCP JSON edge are a deliberately deferred
decision (see the AH-012 spec), so the tool-edge tests assert the stringified form on purpose.
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
from guardrail import Refusal  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_seam_state():
    # Isolate every test from the process-global seam state: the _max_rows_override ContextVar
    # (ACE-028) and the injected executor (leaving one set would make a later subprocess-path test
    # run in-process instead).
    import tools

    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


class _SpyExecutor:
    """Records every call so a test can assert what SQL/creds/profile reached the connect-and-run
    step — and that it was reached at all (or not, when a guard refuses first)."""

    def __init__(self, result: execute_sql.ExecResult | None = None):
        self.calls: list[tuple[str, dict, str]] = []
        self._result = result or execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append((vetted_sql, creds, profile))
        return self._result


# --- the guard is un-bypassable: no path to the executor around it -----------------------------


def test_readonly_guard_refuses_before_the_executor_is_reached():
    # A write statement is refused by the hard read-only gate; the executor is NEVER constructed a
    # query for. This is the "no public path reaches an executor without the guard" invariant.
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        execute_sql.execute_guarded("DELETE FROM t", "acme", None, executor=spy)
    assert ei.value.code == 1
    assert ei.value.refusal.kind == "permission"  # GuardRefused carries the typed Refusal
    assert spy.calls == []  # executor never reached


def test_readonly_guard_still_fires_under_no_safety():
    # --no-safety skips ONLY the semantic-model pass, never the write/RCE/DoS read-only gate.
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        execute_sql.execute_guarded("DROP TABLE t", "acme", None, executor=spy, no_safety=True)
    assert ei.value.code == 1
    assert spy.calls == []


def test_model_safety_refusal_short_circuits_before_the_executor(monkeypatch):
    # A model-safety refusal short-circuits with its typed Refusal carried on the exception (so both
    # the subprocess and in-process paths build the same envelope); the executor must not run.
    monkeypatch.setattr(
        execute_sql, "_model_safety", lambda s, p, a: (s, Refusal("preflight_refused", "fan-trap"), None)
    )
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        execute_sql.execute_guarded("SELECT 1", "acme", None, executor=spy)
    assert ei.value.code == 1 and ei.value.refusal.kind == "preflight_refused"
    assert spy.calls == []


# --- the executor only ever receives already-vetted SQL ----------------------------------------


def test_executor_receives_vetted_sql_and_resolved_creds(monkeypatch):
    # The default_filters rewrite happens in the model pass; the executor sees the POST-guard SQL and
    # the resolved datasource creds — never raw user input, never an unresolved profile.
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(
        execute_sql, "_model_safety", lambda s, p, a: ("SELECT 1 AS c /*vetted*/", None, None)
    )
    spy = _SpyExecutor()

    result = execute_sql.execute_guarded("SELECT 1 AS c", "acme", "sales", executor=spy)

    assert spy.calls == [
        ("SELECT 1 AS c /*vetted*/", {"type": "sqlite", "path": ":memory:"}, "acme")
    ]
    assert result.rows == [(1,)]


def test_no_safety_bypasses_the_model_pass_but_still_runs(monkeypatch):
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )

    def _boom(*a, **k):
        raise AssertionError("_model_safety must be skipped when no_safety=True")

    monkeypatch.setattr(execute_sql, "_model_safety", _boom)
    spy = _SpyExecutor()

    result = execute_sql.execute_guarded(
        "SELECT 1 AS c", "acme", None, executor=spy, no_safety=True
    )

    assert (
        spy.calls[0][0] == "SELECT 1 AS c"
    )  # raw SQL passed straight to the executor, unrewritten
    assert result.rows == [(1,)]


# --- the built-in executor: native rows in, byte-identical CSV out ------------------------------


def test_builtin_executor_satisfies_the_executor_port():
    import ports

    assert isinstance(execute_sql.BUILTIN_EXECUTOR, ports.Executor)  # 5th port, by shape


def test_builtin_executor_returns_native_typed_rows_and_emits_identical_csv(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (n INTEGER, s TEXT)")
    con.executemany("INSERT INTO t (n, s) VALUES (?, ?)", [(1, "a"), (2, None)])
    con.commit()
    con.close()
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": str(db)},
    )

    result = execute_sql.execute_guarded(
        "SELECT n, s FROM t ORDER BY n",
        "acme",
        None,
        executor=execute_sql.BUILTIN_EXECUTOR,
        no_safety=True,
    )

    # Native fidelity (Sandeep's concern): ints stay ints, SQL NULL stays None — NOT "" and not "2".
    assert result.columns == ["n", "s"]
    assert result.rows == [(1, "a"), (2, None)]

    # The subprocess/CLI wire still serializes byte-identical CSV at the edge (NULL renders as an
    # empty field there — the ambiguity lives only in the text wire, not in the native rows).
    execute_sql._emit_result_csv(result)
    assert capsys.readouterr().out == "n,s\r\n1,a\r\n2,\r\n"


def test_collect_cursor_bounds_and_preserves_native_types(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2")

    class _Cur:
        description = [("n",), ("s",)]

        def fetchmany(self, k):
            return [(1, "a"), (2, None), (3, "c")][:k]

    r = execute_sql._collect_cursor(_Cur())
    assert r.columns == ["n", "s"]
    assert r.rows == [(1, "a"), (2, None)]  # cap 2, native None preserved
    assert r.truncated is True  # a (cap+1)th row was available


def test_builtin_executor_raises_executor_error_on_unknown_db():
    with pytest.raises(execute_sql.ExecutorError) as ei:
        execute_sql._builtin_execute("SELECT 1", {"type": "nosuchdb"}, profile="acme")
    assert ei.value.code == 2
    assert "Unsupported db type" in ei.value.msg


# --- Slice 2: injection through create_app + the in-process branch in tool_execute_sql ----------


@pytest.fixture(autouse=True)
def _reset_injected_executor():
    import tools

    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


def test_create_app_wires_injected_executor_and_defaults_to_builtin(monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agami.example.test")
    import mcp_http
    import tools
    from ports import Adapters

    base = mcp_http.default_adapters()
    fake = _SpyExecutor()
    adapters = Adapters(
        activity_sink=base.activity_sink,
        org_resolver=base.org_resolver,
        auth_provider=base.auth_provider,
        governance=base.governance,
        executor=fake,
    )
    mcp_http.create_app(adapters=adapters)
    assert tools._INJECTED_EXECUTOR is fake  # a consumer's explicit executor is wired from adapters

    # ACE-028: the default adapters now carry the built-in executor, so a plain HTTP server runs
    # in-process (the None-means-fork behaviour is still exercised via set_injected_executor(None)).
    mcp_http.create_app()
    assert tools._INJECTED_EXECUTOR is execute_sql.BUILTIN_EXECUTOR


def test_injected_executor_runs_in_process_with_vetted_sql_and_no_fork(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s + " /*vetted*/", None, None))
    # a fork here would be the REQ-002 violation the seam prevents — fail loudly if it happens.
    monkeypatch.setattr(
        tools.subprocess, "run", lambda *a, **k: pytest.fail("must not fork a subprocess")
    )

    fake = _SpyExecutor(
        result=execute_sql.ExecResult(columns=["n"], rows=[(1,), (2,)], truncated=False)
    )
    tools.set_injected_executor(fake)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme"}))

    assert fake.calls[0][0] == "SELECT n FROM t /*vetted*/"  # executor saw POST-guard SQL only
    assert out["data"]["columns"] == ["n"] and out["data"]["rows"] == [["1"], ["2"]] and out["data"]["row_count"] == 2


def test_injected_executor_is_unreachable_for_a_write(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        tools.subprocess, "run", lambda *a, **k: pytest.fail("must not fork a subprocess")
    )

    fake = _SpyExecutor()
    tools.set_injected_executor(fake)
    out = json.loads(tools.tool_execute_sql({"sql": "DELETE FROM t"}))

    assert out["refusal"]["kind"] == "permission"  # refused by the read-only guard
    assert fake.calls == []  # the injected executor was never reached — un-bypassable


def test_injected_executor_error_maps_to_the_same_envelope(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("Postgres connect failed: refused", code=4)

    tools.set_injected_executor(_Boom())
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["refusal"]["kind"] == tools._classify_exit(4)
    # Sanitized (ACE-039): the raw driver text never rides the reason — a value-free classified
    # message does; the raw goes to the server-side audit trail only. Parity with the subprocess wire.
    assert "connect failed" not in out["refusal"]["reason"] and "refused" not in out["refusal"]["reason"]
    assert out["refusal"]["reason"]  # a non-empty, value-free reason


def test_set_injected_executor_rejects_a_bad_shape():
    import tools

    class _NotAnExecutor:
        pass  # no .execute method

    with pytest.raises(TypeError):
        tools.set_injected_executor(_NotAnExecutor())
    assert tools._INJECTED_EXECUTOR is None  # rejected, nothing stored


def test_injected_executor_credential_error_is_sanitized_not_leaked(monkeypatch):
    # Parity with the subprocess path (ACE-039): a bad-profile ExecutorError is classified into a
    # VALUE-FREE refusal reason — the raw message (which can name a DSN / host / env var) never rides
    # the reason; it goes to the server-side audit trail only. This is why _load_credentials/_parse_dsn
    # raise ExecutorError (so the classifier can sanitize) rather than surfacing the raw text.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")

    def _bad(profile, org_id="local"):
        raise execute_sql.ExecutorError(
            "No warehouse credentials for profile [acme]. Set DATASOURCE_URL ...", code=2
        )

    monkeypatch.setattr(execute_sql, "_load_credentials", _bad)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None, None))
    tools.set_injected_executor(_SpyExecutor())

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "refused"
    assert "DATASOURCE_URL" not in out["refusal"]["reason"]  # value-free, not the raw config text
    assert out["refusal"]["reason"]  # a non-empty classified reason


def test_injected_executor_systemexit_is_caught_not_fatal(monkeypatch):
    # A deep sys.exit (a bad profile/DSN in _load_credentials/_parse_dsn) must NOT escape in-process
    # and kill the host — it becomes a fail-closed tool error.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")

    def _exit(*a, **k):
        raise SystemExit(2)

    monkeypatch.setattr(execute_sql, "execute_guarded", _exit)
    tools.set_injected_executor(_SpyExecutor())

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "refused"  # a refused envelope, not a process exit


def test_default_no_injected_executor_forks_the_subprocess(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "n\r\n1\r\n"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(tools.subprocess, "run", _fake_run)
    tools.set_injected_executor(None)  # the default

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme"}))

    assert "-m" in captured["cmd"] and "execute_sql" in captured["cmd"]  # forked the CLI executor
    assert out["data"]["rows"] == [["1"]]


def test_injected_executor_model_safety_refusal_returns_the_typed_refusal(monkeypatch):
    # In-process now surfaces the SAME typed model-safety Refusal the subprocess path does (the
    # detail rides GuardRefused, no longer lost to stderr) — full structured-refusal parity.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(
        execute_sql,
        "_model_safety",
        lambda s, p, a: (s, Refusal("table_out_of_scope", "table foo is not in the model"), None),
    )
    fake = _SpyExecutor()
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "refused" and out["refusal"]["kind"] == "table_out_of_scope"
    assert "not in the model" in out["refusal"]["reason"]  # the real detail, not a generic string
    assert fake.calls == []  # refused before the executor


def test_injected_executor_real_pii_gate_masks_a_maskable_projection(monkeypatch):
    # BEHAVIOUR CHANGE (ACE-041 slice 3, was refuse before this slice): a REAL sensitive-projection
    # gate firing on a REAL model for a *maskable* projection (a bare `email`, a clean 1:1 image of
    # output column 0) now MASKS rather than refuses — the query RUNS and the value is redacted, with
    # an `applied[{mask}]` note. This is the spec's data-protection behaviour, not a weakened safety
    # gate: an UNTRACEABLE projection still refuses (see test_..._untraceable below and the ACE-041
    # masking suite). Pins the whole real gate -> policy -> mask -> redacted-envelope path end-to-end.
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")
    import tools
    from semantic_model import models as m

    org = m.Datasource(
        datasource="o",
        version=1,
        subject_areas=[
            m.SubjectArea(
                name="area",
                description="d",
                tables_defined=[
                    m.Table(
                        name="users", schema="public", storage_connection="c", grain=["id"],
                        columns=[
                            m.Column(name="id", type="integer"),
                            m.Column(name="email", type="string", sensitive=True),
                        ],
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: org)  # real _model_safety runs
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})
    fake = _SpyExecutor(result=execute_sql.ExecResult(columns=["email"], rows=[("alice@x.io",)], truncated=False))
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT email FROM users", "datasource": "acme"}))

    assert out["status"] == "ok"  # NOT refused — the query ran behind the guard
    assert out["data"]["rows"] == [[execute_sql.REDACTION_TOKEN]]  # the raw email was redacted, not leaked
    assert "alice@x.io" not in json.dumps(out)  # no raw sensitive value anywhere in the envelope
    assert {"mask": "users.email"} in out["applied"]  # the applied[{mask}] note is recorded
    assert fake.calls  # the executor RAN (masking is post-execution), then the value was redacted


def test_injected_executor_real_pii_gate_refuses_an_untraceable_projection(monkeypatch):
    # The safety counterpart to the mask test: an UNTRACEABLE sensitive projection (the value buried
    # in an expression) still fails closed to a refused Envelope with kind "sensitive_columns", and
    # the executor never runs. Masking must NOT weaken this fail-closed refusal.
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")
    import tools
    from semantic_model import models as m

    org = m.Datasource(
        datasource="o",
        version=1,
        subject_areas=[
            m.SubjectArea(
                name="area",
                description="d",
                tables_defined=[
                    m.Table(
                        name="users", schema="public", storage_connection="c", grain=["id"],
                        columns=[
                            m.Column(name="id", type="integer"),
                            m.Column(name="email", type="string", sensitive=True),
                        ],
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: org)
    fake = _SpyExecutor()
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT UPPER(email) FROM users", "datasource": "acme"}))

    assert out["status"] == "refused"
    assert out["refusal"]["kind"] == "sensitive_columns"  # buried value → fail closed
    assert "data" not in out  # a refusal carries no data (no raw sensitive values leak)
    assert fake.calls == []  # refused BEFORE the executor ran


def test_injected_executor_textualizes_null_as_empty_at_the_tool_edge(monkeypatch):
    # The deferred-decision contract: at the MCP JSON edge the in-process path renders SQL NULL as
    # "" (matching the CSV wire), NOT "None". Pins the one coercion a future native-typed switch flips.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None, None))
    fake = _SpyExecutor(
        result=execute_sql.ExecResult(columns=["n", "s"], rows=[(1, None)], truncated=False)
    )
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT n, s FROM t", "datasource": "acme"}))

    assert out["data"]["rows"] == [["1", ""]]  # int -> "1", NULL -> "" (never "None")


def test_injected_executor_backstop_trims_to_max_rows(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None, None))
    fake = _SpyExecutor(
        result=execute_sql.ExecResult(columns=["n"], rows=[(1,), (2,), (3,)], truncated=False)
    )
    tools.set_injected_executor(fake)

    out = json.loads(
        tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme", "max_rows": 2})
    )

    assert out["data"]["rows"] == [["1"], ["2"]] and out["data"]["truncated"] is True


# --- main() (the subprocess CLI entry) translates the envelope's outcomes byte-identically --------


def _raise(exc):
    raise exc


def test_main_read_only_refusal_writes_json_and_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "DELETE FROM t"])

    rc = execute_sql.main()

    assert rc == 1
    assert json.loads(capsys.readouterr().err.strip())["refusal"]["kind"] == "permission"


def test_main_executor_error_writes_message_and_returns_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        lambda *a, **k: _raise(
            execute_sql.ExecutorError("Postgres connect failed: refused", code=4)
        ),
    )
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT 1"])

    rc = execute_sql.main()

    assert rc == 4
    assert capsys.readouterr().err.strip() == "Postgres connect failed: refused"


def test_main_success_serializes_result_to_stdout_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        lambda *a, **k: execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False),
    )
    monkeypatch.setattr(
        sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT n FROM t"]
    )

    rc = execute_sql.main()

    assert rc == 0
    assert capsys.readouterr().out == "n\r\n1\r\n"


# --- per-tenant env credentials: <ORG>_DATASOURCE_URL__<PROFILE>, fail-closed for named orgs -----


@pytest.fixture
def _clean_datasource_env(monkeypatch):
    import os

    for k in list(os.environ):
        if "DATASOURCE_URL" in k:
            monkeypatch.delenv(k, raising=False)
    yield


def test_single_tenant_local_keeps_the_orgless_fallback_chain(_clean_datasource_env, monkeypatch):
    # org 'local' (the single-tenant default) still resolves the historical org-less vars, in order.
    monkeypatch.setenv("DATASOURCE_URL", "generic")
    assert execute_sql._env_datasource_dsn("prod", "local") == "generic"
    monkeypatch.setenv("DATASOURCE_URL__PROD", "per-profile")
    assert execute_sql._env_datasource_dsn("prod", "local") == "per-profile"  # more specific wins
    monkeypatch.setenv("LOCAL_DATASOURCE_URL__PROD", "local-prefixed")
    assert (
        execute_sql._env_datasource_dsn("prod", "local") == "local-prefixed"
    )  # most specific wins


def test_a_named_tenant_uses_its_own_prefixed_var(_clean_datasource_env, monkeypatch):
    monkeypatch.setenv("ACME_DATASOURCE_URL__PROD", "acme-wh")
    monkeypatch.setenv("GLOBEX_DATASOURCE_URL__PROD", "globex-wh")
    assert execute_sql._env_datasource_dsn("prod", "acme") == "acme-wh"
    assert (
        execute_sql._env_datasource_dsn("prod", "globex") == "globex-wh"
    )  # same profile, diff org


def test_a_named_tenant_never_falls_back_to_the_shared_datasource_url(
    _clean_datasource_env, monkeypatch
):
    # The fail-closed rule: a forgotten tenant var must NOT silently point that tenant at the shared
    # warehouse. Only 'local' gets the org-less fallback.
    monkeypatch.setenv("DATASOURCE_URL", "shared")
    monkeypatch.setenv("DATASOURCE_URL__PROD", "shared-per-profile")
    assert execute_sql._env_datasource_dsn("prod", "acme") is None  # refuses, no shared fallback


def test_org_id_threads_from_execute_guarded_into_credential_resolution(
    _clean_datasource_env, monkeypatch
):
    # End-to-end through the guarded envelope: the org passed to execute_guarded picks the tenant's var.
    monkeypatch.setenv("ACME_DATASOURCE_URL__PROD", "sqlite:///:memory:")
    spy = _SpyExecutor()
    execute_sql.execute_guarded(
        "SELECT 1", "prod", None, executor=spy, org_id="acme", no_safety=True
    )
    assert spy.calls[0][1]["type"] == "sqlite"  # acme's DSN parsed and handed to the executor
