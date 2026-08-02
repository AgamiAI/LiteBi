"""AH-012 — the executor seam. `execute_sql` is split into a single un-bypassable guarded envelope
(`execute_guarded`: read-only guard -> semantic-model safety -> resolve datasource ->
`executor.execute(vetted_sql)`) and a swappable `Executor` port. These tests pin the load-bearing
invariants: the guard runs BEFORE the executor, the executor only ever sees already-vetted SQL, and
there is no path to an executor around the guard (fail-closed, REQ-002/REQ-014).

`execute_guarded` now returns an `Envelope` on **every** path and raises nothing for a caller to
interpret — the refusal-carrying exception is gone. The assertions below therefore read the
envelope's `status` / `refusal` / `failure` / `data` where they used to read an exception's
attributes; what is being pinned is unchanged, and one property is new: the outcome is a value the
type system carries, so the two callers (the subprocess `main` and the in-process MCP handler)
cannot render it differently.

On result fidelity: the built-in executor returns NATIVE-typed rows (`ExecResult`) — NULL as None,
ints as int — and the subprocess wire still serializes byte-identical CSV. The TOOL edge textualizes
those rows to match the CSV wire so both execution paths return identical JSON; native-typed rows at
the MCP JSON edge are a deliberately deferred decision (see the AH-012 spec), so the tool-edge tests
assert the stringified form on purpose.
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
import guardrail  # noqa: E402


def _refusal(rule: str = guardrail.RULE_TABLE_SCOPE) -> guardrail.Refusal:
    """A contract-valid `Refusal` for a `_model_safety` monkeypatch to hand back."""
    return guardrail.refuse(rule, detail="out of scope", remediation="Ask about a declared table.")


@pytest.fixture(autouse=True)
def _reset_override():
    # _max_rows_override is a request-scoped ContextVar (ACE-028); isolate every test from it.
    execute_sql._max_rows_override.set(None)
    yield
    execute_sql._max_rows_override.set(None)


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
    env = execute_sql.execute_guarded("DELETE FROM t", "acme", None, executor=spy)
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_READ_ONLY
    assert spy.calls == []  # executor never reached


def test_readonly_guard_still_fires_under_no_safety():
    # --no-safety skips ONLY the semantic-model pass, never the write/RCE/DoS read-only gate.
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded(
        "DROP TABLE t", "acme", None, executor=spy, no_safety=True
    )
    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_READ_ONLY
    assert spy.calls == []


def test_model_safety_refusal_short_circuits_before_the_executor(monkeypatch):
    # A converted gate hands back the contract object; the envelope relays it VERBATIM (same rule,
    # same remediation — no re-authoring at this layer) and the executor must not run.
    refusal = _refusal(guardrail.RULE_COLUMN_SCOPE)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, refusal))
    spy = _SpyExecutor()

    env = execute_sql.execute_guarded("SELECT 1", "acme", None, executor=spy)

    assert env.status == "refused"
    assert env.refusal is refusal  # relayed, not rebuilt
    assert spy.calls == []


def test_unconverted_model_safety_branch_still_yields_a_refusal_envelope(monkeypatch):
    # The four branches NOT converted in this slice still return a bare exit code after writing
    # their own diagnostic to the server log. They must still produce an Envelope — that is the
    # whole reason the interim `model_safety` rule exists — carrying a remediation that points at
    # the log rather than inventing a rule the gate never chose.
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, 1))
    spy = _SpyExecutor()

    env = execute_sql.execute_guarded("SELECT 1", "acme", None, executor=spy)

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_MODEL_SAFETY
    assert env.refusal.reason == "undetermined"
    assert "server log" in env.refusal.remediation
    assert spy.calls == []


# --- the executor only ever receives already-vetted SQL ----------------------------------------


def test_executor_receives_vetted_sql_and_resolved_creds(monkeypatch):
    # The fan/chasm auto-rewrite happens in the model pass; the executor sees the POST-guard SQL and
    # the resolved datasource creds — never raw user input, never an unresolved profile.
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(
        execute_sql, "_model_safety", lambda s, p, a: ("SELECT 1 AS c /*vetted*/", None)
    )
    spy = _SpyExecutor()

    env = execute_sql.execute_guarded("SELECT 1 AS c", "acme", "sales", executor=spy)

    assert spy.calls == [
        ("SELECT 1 AS c /*vetted*/", {"type": "sqlite", "path": ":memory:"}, "acme")
    ]
    assert env.status == "ok" and env.data.rows == [(1,)]


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

    env = execute_sql.execute_guarded(
        "SELECT 1 AS c", "acme", None, executor=spy, no_safety=True
    )

    assert (
        spy.calls[0][0] == "SELECT 1 AS c"
    )  # raw SQL passed straight to the executor, unrewritten
    assert env.status == "ok" and env.data.rows == [(1,)]


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

    env = execute_sql.execute_guarded(
        "SELECT n, s FROM t ORDER BY n",
        "acme",
        None,
        executor=execute_sql.BUILTIN_EXECUTOR,
        no_safety=True,
    )

    # Native fidelity: ints stay ints, SQL NULL stays None — NOT "" and not "2".
    assert env.status == "ok"
    assert env.data.columns == ["n", "s"]
    assert env.data.rows == [(1, "a"), (2, None)]

    # The subprocess/CLI wire still serializes byte-identical CSV at the edge (NULL renders as an
    # empty field there — the ambiguity lives only in the text wire, not in the native rows).
    execute_sql._emit_result_csv(env.data)
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
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s + " /*vetted*/", None))
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
    assert out["columns"] == ["n"] and out["rows"] == [["1"], ["2"]] and out["row_count"] == 2


def test_injected_executor_is_unreachable_for_a_write(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        tools.subprocess, "run", lambda *a, **k: pytest.fail("must not fork a subprocess")
    )

    fake = _SpyExecutor()
    tools.set_injected_executor(fake)
    out = json.loads(tools.tool_execute_sql({"sql": "DELETE FROM t"}))

    assert out["status"] == "refused"  # refused by the read-only guard
    assert out["refusal"]["rule"] == guardrail.RULE_READ_ONLY
    assert fake.calls == []  # the injected executor was never reached — un-bypassable


def test_injected_executor_error_maps_to_the_same_envelope(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("Postgres connect failed: refused", code=4)

    tools.set_injected_executor(_Boom())
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "failed"
    assert out["failure"]["kind"] == tools._classify_exit(4)
    # The message is now the fixed value-free sentence for the kind, not the driver's text
    # (ACE-039). Code 4 carries the driver's own exception, so relaying it was the enumeration
    # channel; the KIND still round-trips, which is what this test is actually about.
    assert out["failure"]["message"] == execute_sql._ERROR_MESSAGES["auth"]
    assert "connect failed" not in out["failure"]["message"]


def test_set_injected_executor_rejects_a_bad_shape():
    import tools

    class _NotAnExecutor:
        pass  # no .execute method

    with pytest.raises(TypeError):
        tools.set_injected_executor(_NotAnExecutor())
    assert tools._INJECTED_EXECUTOR is None  # rejected, nothing stored


def test_injected_executor_credential_error_surfaces_detailed_remediation(monkeypatch):
    # Parity with the subprocess path: a bad-profile ExecutorError carries its detailed message, so
    # the in-process tool envelope surfaces the SAME text the CLI stderr would (not a generic
    # string). This is why `_load_credentials` raises instead of sys.exit, and why it sits INSIDE
    # `execute_guarded`'s try — a credentials problem is a `failed`/`dsn`, and the detail now
    # arrives on `failure.message`.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")

    def _bad(profile, org_id="local"):
        raise execute_sql.ExecutorError(
            "No warehouse credentials for profile [acme]. Set DATASOURCE_URL ...", code=2
        )

    monkeypatch.setattr(execute_sql, "_load_credentials", _bad)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    tools.set_injected_executor(_SpyExecutor())

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "failed" and out["failure"]["kind"] == "dsn"
    assert "DATASOURCE_URL" in out["failure"]["message"]  # detailed, not the generic net string


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

    # A `failed` Envelope, not a process exit — and classified, not a bare "something broke".
    assert out["status"] == "failed" and out["failure"]["kind"] == "dsn"


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
    assert out["rows"] == [["1"]]


@pytest.mark.parametrize(
    ("verdict", "expected_rule"),
    [
        # A CONVERTED gate: the rule the gate itself chose reaches the caller intact. Before this
        # slice the in-process path collapsed every one of these to `{"kind": "permission"}`.
        (_refusal(guardrail.RULE_TABLE_SCOPE), guardrail.RULE_TABLE_SCOPE),
        (_refusal(guardrail.RULE_COLUMN_SCOPE), guardrail.RULE_COLUMN_SCOPE),
        # An UNCONVERTED branch: still a bare exit code, so the interim rule is the honest answer.
        (1, guardrail.RULE_MODEL_SAFETY),
    ],
    ids=["table_scope", "column_scope", "unconverted_int"],
)
def test_injected_executor_model_safety_refusal_carries_the_rule(
    monkeypatch, verdict, expected_rule
):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, verdict))  # refuse
    fake = _SpyExecutor()
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "refused"
    assert out["refusal"]["rule"] == expected_rule
    assert out["refusal"]["remediation"].strip()
    assert fake.calls == []  # refused before the executor


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
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    fake = _SpyExecutor(
        result=execute_sql.ExecResult(columns=["n", "s"], rows=[(1, None)], truncated=False)
    )
    tools.set_injected_executor(fake)

    out = json.loads(tools.tool_execute_sql({"sql": "SELECT n, s FROM t", "datasource": "acme"}))

    assert out["rows"] == [["1", ""]]  # int -> "1", NULL -> "" (never "None")


def test_injected_executor_backstop_trims_to_max_rows(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    fake = _SpyExecutor(
        result=execute_sql.ExecResult(columns=["n"], rows=[(1,), (2,), (3,)], truncated=False)
    )
    tools.set_injected_executor(fake)

    out = json.loads(
        tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "acme", "max_rows": 2})
    )

    assert out["rows"] == [["1"], ["2"]] and out["truncated"] is True


# --- main() (the subprocess CLI entry) translates the envelope's outcomes byte-identically --------
#
# `main` is a three-way switch on `env.status` and the only place this module prints. The bytes and
# exit codes below are the pre-envelope ones, unchanged — the CLI contract is read by the
# agami-query skill's error classifier and by `semantic_model.cli`, so the rewrite is invisible to
# both.


def _envelope_returning(env):
    """A `execute_guarded` stand-in that returns a fixed Envelope, whatever it is called with."""
    return lambda *a, **k: env


def test_main_read_only_refusal_writes_json_and_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "DELETE FROM t"])

    rc = execute_sql.main()

    assert rc == 1
    err = capsys.readouterr().err
    # Parses WHOLE, so it is a single object and nothing rides alongside it.
    assert json.loads(err)["refusal"]["rule"] == guardrail.RULE_READ_ONLY
    assert err.count("\n") == 1


def test_main_failed_writes_bare_message_and_the_classified_exit_code(
    tmp_path, monkeypatch, capsys
):
    # A `failed` Envelope renders as today's bare stderr line (NOT JSON) plus the exit code its
    # `Failure.kind` maps back to.
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        _envelope_returning(
            execute_sql._envelope(
                "failed",
                failure=guardrail.Failure(kind="auth", message="Postgres connect failed: refused"),
                # Mandatory on this constructor: an outcome with no receipt to hand must return one
                # that SAYS so, rather than the empty-and-silent default a consumer reads as
                # "checked, found nothing".
                receipt=guardrail.undetermined_receipt(guardrail.RECEIPT_NO_MODEL),
            )
        ),
    )
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT 1"])

    rc = execute_sql.main()

    assert rc == 4
    assert capsys.readouterr().err == "Postgres connect failed: refused\n"


def test_an_executor_error_becomes_the_failed_envelope_main_renders(tmp_path, monkeypatch, capsys):
    # The other half of the previous test: an `ExecutorError` raised by the executor is what
    # produces that Envelope, so the pair proves the exception -> Envelope -> stderr/exit-code chain
    # end to end rather than only its second half.
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("Postgres connect failed: refused", code=4)

    monkeypatch.setattr(execute_sql, "BUILTIN_EXECUTOR", _Boom())
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT 1"])

    rc = execute_sql.main()

    assert rc == 4
    # `main` still writes the bare message and returns the classified code — the CLI contract is
    # unchanged, and that matters because the parent relays this stream to the caller. What changed
    # is WHICH message reaches it: code 4 carries the driver's own exception, so it is classified
    # and replaced with the fixed sentence rather than relayed (ACE-039).
    assert capsys.readouterr().err == execute_sql._ERROR_MESSAGES["auth"] + "\n"


@pytest.mark.parametrize("code", sorted(execute_sql.EXIT_TO_FAILURE_KIND))
def test_every_produced_exit_code_round_trips_through_the_failure_kind(code):
    # `main` reconstructs the exit code from `Failure.kind`, which is lossy in general (nine kinds,
    # five codes). It must be LOSSLESS for every code the executor actually produces, or a rewrite
    # that only looks byte-identical would still change what a caller's `$?` sees. `other` is in this
    # set because it has its own code (6): while it fell to the generic 2, the round trip was lossy
    # for the catch-all and the parent read an internal break back as `dsn`.
    kind = execute_sql.EXIT_TO_FAILURE_KIND[code]
    assert execute_sql.FAILURE_KIND_TO_EXIT[kind] == code


def test_main_success_serializes_result_to_stdout_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        execute_sql,
        "execute_guarded",
        _envelope_returning(
            execute_sql._envelope(
                "ok", data=execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False),
                receipt=guardrail.undetermined_receipt(guardrail.RECEIPT_NO_MODEL),
            )
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT n FROM t"]
    )

    rc = execute_sql.main()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "n\r\n1\r\n"
    assert captured.err == ""  # a successful run says nothing on stderr


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
