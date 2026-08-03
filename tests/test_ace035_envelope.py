"""One `Envelope` per path, assembled at one point, with no per-surface fork.

`tests/test_ace035_read_only_refusal.py` proves the read-only gate speaks the contract, and
`tests/test_ace035_gate_verdict_parity.py` proves the three scope gates do. This file proves the
thing those two cannot: that the ONE object each gate produced is what a caller actually receives,
on **both** execution paths, unchanged.

The property that motivates the whole contract is the parity test at the bottom. Before this slice
the in-process path collapsed every semantic-model gate into a single
`{"kind": "permission", "remediation": "…see server logs…"}` while the forked path relayed the real
rule — so the same statement got two different answers depending on a deployment detail the user
cannot see. That is not a formatting difference: a caller told "permission" cannot tell a
hallucinated column from a table it is not allowed to touch, and cannot fix either one.

Three things are pinned here, in order of how load-bearing they are:

  1. **Parity.** The same out-of-scope statement, run in-process and through the fork, yields the
     same `rule` and the same `remediation` — and specifically the gate's own rule, not a generic
     stand-in.
  2. **Totality.** Every path out of `execute_guarded` returns an `Envelope`; nothing is raised for
     a caller to interpret, and the refusal-carrying exception no longer exists anywhere in the
     shipped source.
  3. **Shape.** Each of the three statuses has one tool-edge JSON shape, produced by one serializer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402


class _SpyExecutor:
    """Records what reached the connect-and-run step — or that nothing did, when a gate refused."""

    def __init__(self, result: execute_sql.ExecResult | None = None):
        self.calls: list[tuple[str, dict, str]] = []
        self._result = result or execute_sql.ExecResult(columns=["c"], rows=[(1,)], truncated=False)

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append((vetted_sql, creds, profile))
        return self._result


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # `_max_rows_override` is a request-scoped ContextVar and `_INJECTED_EXECUTOR` a process global;
    # isolate both, and make sure a stray inherited AGAMI_DB_URL can't flip a test onto the hosted
    # branch.
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


# ---------------------------------------------------------------------------
# Totality — every path returns an Envelope, and nothing raises a refusal
# ---------------------------------------------------------------------------


def _sqlite_creds(p, org_id="local"):
    return {"type": "sqlite", "path": ":memory:"}


def test_every_outcome_of_execute_guarded_is_an_envelope(monkeypatch):
    """All four outcomes, asserted to be `Envelope`s of the right status in one place.

    Asserting the TYPE (not just the fields) is the point: the signature is `-> Envelope` with no
    union, so a future branch that returns an exit code, a tuple, or None to save a line is caught
    here rather than at whichever caller happens to unpack it first.

    There were five. The fifth was a `_model_safety` returning a bare exit code, answered with the
    interim `model_safety` rule; ACE-094 deleted the two branches that could return one, so the
    verdict type is `Refusal | None` and there is no int for this to exercise."""
    monkeypatch.setattr(execute_sql, "_load_credentials", _sqlite_creds)

    def _guarded(sql="SELECT 1", **kw):
        return execute_sql.execute_guarded(sql, "acme", None, executor=_SpyExecutor(), **kw)

    # 1. the read-only gate refuses
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    read_only = _guarded("DELETE FROM t")

    # 2. a converted model-safety gate returns its Refusal
    scope_refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE, detail="undeclared table", remediation="Ask about `orders`."
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, scope_refusal))
    scoped = _guarded()

    # 3. the executor raises
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("Postgres connect failed: refused", code=4)

    failed = execute_sql.execute_guarded("SELECT 1", "acme", None, executor=_Boom())

    # 4. the statement ran
    ok = _guarded()

    outcomes = {
        "read_only": (read_only, "refused"),
        "scope_gate": (scoped, "refused"),
        "executor_error": (failed, "failed"),
        "success": (ok, "ok"),
    }
    for name, (env, status) in outcomes.items():
        assert isinstance(env, guardrail.Envelope), name
        assert env.status == status, name
        assert env.audit_id.strip(), name
        # `Envelope.__post_init__` already enforces present-iff; re-stating it here is what makes a
        # weakened contract visible from the caller's side too.
        assert sum(x is not None for x in (env.data, env.refusal, env.failure)) == 1, name

    # Every outcome gets its OWN audit id — a shared or reused id would silently join two answers
    # to one audit row when the row lands.
    ids = [env.audit_id for env, _ in outcomes.values()]
    assert len(set(ids)) == len(ids)


def test_a_credentials_failure_is_a_failed_envelope_not_an_escaping_exception(monkeypatch):
    """`_load_credentials` sits INSIDE the try on purpose.

    A bad profile is an operational failure, not a governance decision, and it carries a detailed
    remediation the caller can act on. Letting it escape would put a second transport back next to
    the Envelope — the exact thing that let the two paths drift.
    """
    def _bad(profile, org_id="local"):
        raise execute_sql.ExecutorError(
            "No warehouse credentials for profile [acme]. Set DATASOURCE_URL ...", code=2
        )

    monkeypatch.setattr(execute_sql, "_load_credentials", _bad)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    spy = _SpyExecutor()

    env = execute_sql.execute_guarded("SELECT 1", "acme", None, executor=spy)

    assert env.status == "failed" and env.failure.kind == "dsn"
    assert "DATASOURCE_URL" in env.failure.message
    assert spy.calls == []  # never reached the executor


def test_the_refusal_carrying_exception_is_gone_from_the_shipped_source():
    """A refusal is RETURNED, never raised — proved by absence, over the whole shipped tree.

    Keeping both transports alive is how the fork path and the in-process path drifted into
    different answers in the first place, so the type must not exist to be reached for. Scanned
    across the package source AND the vendored plugin slice, because the marketplace layout ships
    only the latter and a copy resurrected there would be just as reachable.
    """
    roots = [PKG_SRC, REPO_ROOT / "plugins"]
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{i}"
        for root in roots
        for path in sorted(root.rglob("*.py"))
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "GuardRefused" in line
    ]
    assert hits == [], hits


# ---------------------------------------------------------------------------
# Shape — one serializer, three tool-edge shapes
# ---------------------------------------------------------------------------


@pytest.fixture
def _emitted(monkeypatch):
    """Capture every `Envelope` that reaches the tool edge's single serializer.

    Spying on `_emit` rather than on the JSON is deliberate: it proves the funnel exists (a return
    that bypassed it would leave this list short) as well as what went through it.
    """
    seen: list[guardrail.Envelope] = []
    real = tools._emit

    def _spy(env, **kw):
        seen.append(env)
        return real(env, **kw)

    monkeypatch.setattr(tools, "_emit", _spy)
    return seen


def _tool_out(sql: str, **args) -> dict:
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": "acme", **args}))


def test_tool_edge_refused_shape(monkeypatch, _emitted):
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")

    out = _tool_out("DELETE FROM t")

    # No `execution_ms`: nothing was executed, so a duration would be a fabricated field. The
    # `receipt` key is new in ACE-088: a refused caller most needs the facts, so the receipt is on
    # the refused wire from that slice (bounded to the caller's own identifiers).
    assert set(out) == {"status", "refusal", "sql", "audit_id", "receipt"}
    assert set(out["receipt"]) == {"model_version", *guardrail.Receipt.SECTIONS}
    assert out["status"] == "refused"
    assert set(out["refusal"]) == {"reason", "rule", "detail", "remediation"}
    assert out["refusal"]["rule"] == guardrail.RULE_READ_ONLY
    assert out["audit_id"].strip()
    assert len(_emitted) == 1  # exactly one trip through the serializer


def test_tool_edge_failed_shape(monkeypatch, _emitted):
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_load_credentials", _sqlite_creds)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("SQLite execution error: no such column", code=5)

    tools.set_injected_executor(_Boom())
    out = _tool_out("SELECT nope FROM t")

    # `receipt` is new in ACE-088 — see the refused shape above.
    assert set(out) == {"status", "failure", "sql", "execution_ms", "audit_id", "receipt"}
    assert out["status"] == "failed"
    assert set(out["failure"]) == {"kind", "message"}
    # Was `syntax`, which was just the exit-5 prior showing through: nothing read the text, so
    # every code-5 error got the same label. ACE-039 classifies it, and "no such column" is a
    # missing column rather than a syntax error — a distinction the caller can act on.
    assert out["failure"]["kind"] == "column_not_found"
    assert "no such column" not in out["failure"]["message"]
    assert len(_emitted) == 1


def test_tool_edge_ok_shape_keeps_the_frozen_payload_and_adds_status_and_audit_id(
    monkeypatch, _emitted
):
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_load_credentials", _sqlite_creds)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    tools.set_injected_executor(
        _SpyExecutor(execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False))
    )

    out = _tool_out("SELECT n FROM t")

    assert out["status"] == "ok" and out["audit_id"].strip()
    # `_finalize_execution`'s payload is frozen and merged whole — every key it owns survives.
    assert {"columns", "rows", "row_count", "truncated", "units", "markdown", "sql",
            "execution_ms", "receipt"} <= set(out)
    assert out["columns"] == ["n"] and out["rows"] == [["1"]]
    # The nested `receipt` is the FLAT trust receipt `_finalize_execution` nests, NOT the typed
    # `Envelope.receipt` — two shapes of the same facts while both spellings exist, and the ok body
    # keeps this one until the PR that deletes it flips ok onto the other.
    assert "receipt" in out
    assert len(_emitted) == 1


def test_tool_edge_omits_fields_it_has_nothing_to_say_about(_emitted):
    # A malformed argument, before a profile is even resolved: no `sql`, no duration. Those keys are
    # ABSENT rather than explicitly null, so a client never has to distinguish "no value" from
    # "value is null".
    out = json.loads(tools.tool_execute_sql({"sql": "   "}))

    # A receipt is still present, and it SAYS it has nothing rather than looking clean: there was
    # no statement, so there is nothing a receipt could be about.
    assert set(out) == {"status", "failure", "audit_id", "receipt"}
    assert out["receipt"]["tables"] == {"items": [], "undetermined": tools.RECEIPT_NO_STATEMENT}
    assert out["status"] == "failed" and out["failure"]["kind"] == "other"
    assert len(_emitted) == 1


def test_the_envelope_receipt_is_present_on_all_three_statuses(monkeypatch, _emitted):
    """`Envelope.receipt` exists on `ok`, `refused` AND `failed`.

    The field was on the Envelope from the start so the shape would not change when it filled;
    ACE-088 filled it. It is present on `refused` in particular, because a refused caller most needs
    the facts — bounded there to the identifiers its own statement wrote.

    This test pins the FIELD on every status. What each one contains is
    `tests/test_ace088_receipt_placement.py`.
    """
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_load_credentials", _sqlite_creds)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    class _Boom:
        def execute(self, vetted_sql, creds, *, profile):
            raise execute_sql.ExecutorError("boom", code=5)

    tools.set_injected_executor(_SpyExecutor())
    _tool_out("SELECT n FROM t")          # ok
    _tool_out("DELETE FROM t")            # refused
    tools.set_injected_executor(_Boom())
    _tool_out("SELECT n FROM t")          # failed

    assert [e.status for e in _emitted] == ["ok", "refused", "failed"]
    for env in _emitted:
        assert isinstance(env.receipt, guardrail.Receipt), env.status


# ---------------------------------------------------------------------------
# Parity — the property the whole contract exists for
# ---------------------------------------------------------------------------


def _write_disk_model(root: Path) -> None:
    """A two-table model on disk: `orders` and `customers`, one `id` column each."""
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]}
        )
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    for name in ("orders", "customers"):
        (root / "subject_areas" / "sales" / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump({
                "name": name, "schema": "public", "storage_connection": "c", "grain": ["id"],
                "description": name,
                "columns": [{"name": "id", "type": "integer", "primary_key": True}],
            })
        )


# Each vector is an out-of-scope statement the model above refuses, paired with the rule that must
# reach the caller. Three gates rather than one, so a path that happened to relay ONE rule correctly
# (or hard-coded it) does not pass.
_OUT_OF_SCOPE = [
    ("SELECT id FROM sqlite_master", guardrail.RULE_TABLE_SCOPE),
    ("SELECT * FROM orders", guardrail.RULE_SELECT_STAR),
    ("SELECT nope FROM orders", guardrail.RULE_COLUMN_SCOPE),
]


@pytest.mark.parametrize(("sql", "rule"), _OUT_OF_SCOPE, ids=[r for _, r in _OUT_OF_SCOPE])
def test_in_process_and_forked_refusals_are_the_same_refusal(tmp_path, monkeypatch, sql, rule):
    """The same out-of-scope statement, run both ways, yields the same rule and the same fix.

    This is the test the slice exists for. Before it, the in-process branch replaced whatever the
    gate decided with one generic string, so these two assertions could not both hold — and the
    caller's experience depended on whether an executor happened to be injected.

    Both halves are real: the in-process half runs the guard in this process behind an injected
    executor, and the forked half actually spawns `python -m execute_sql` and reads its stderr.
    """
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")

    artifacts = tmp_path / "art"
    _write_disk_model(artifacts / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")

    # In-process: an injected executor, so `execute_guarded` runs here. It must never be reached.
    spy = _SpyExecutor()
    tools.set_injected_executor(spy)
    in_process = _tool_out(sql)
    assert spy.calls == []

    # Forked: no injected executor, so `tool_execute_sql` spawns the CLI and rebuilds the refusal
    # its child wrote to stderr.
    tools.set_injected_executor(None)
    forked = _tool_out(sql)

    assert in_process["status"] == forked["status"] == "refused"
    assert in_process["refusal"]["rule"] == forked["refusal"]["rule"] == rule
    assert in_process["refusal"]["remediation"] == forked["refusal"]["remediation"]
    # The reason and the detail travel too, so the whole contract object is identical — not just
    # the two fields named above.
    assert in_process["refusal"] == forked["refusal"]
    # This used to also assert the rule was not the interim `model_safety` stand-in — the guard
    # against both paths regressing to the same useless answer. That constant is gone: ACE-094
    # deleted the two branches it stood in for, so there is no generic rule left to regress to.
    assert in_process["refusal"]["rule"] in guardrail.REASON_FOR_RULE
    # The receipt is the second half of the same property (ACE-088), and the harder half: the two
    # paths build it in two different PROCESSES. The child's Envelope is destroyed at the process
    # boundary, so the parent assembles the fork's receipt from the same model and the same version
    # pin — and if either side drifts, the same statement comes back described two ways.
    assert in_process["receipt"] == forked["receipt"]
    assert [i["ref"] for i in in_process["receipt"]["tables"]["items"]]


def _write_sensitive_model(root: Path) -> None:
    """A one-table model whose `email` column is flagged `sensitive`.

    Projecting it used to trip `check_sensitive_projection` and refuse. ACE-094 deleted that gate,
    so the flag is a description the receipt reports and the statement runs."""
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]}
        )
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(
        yaml.safe_dump({
            "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "customers",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "email", "type": "string", "sensitive": True},
            ],
        })
    )


def test_a_sensitive_projection_is_no_longer_refused_on_either_path(tmp_path, monkeypatch):
    """The sensitive-column branch does not refuse, and the two paths still agree.

    This was `test_an_unconverted_branch_also_refuses_identically_on_both_paths`, and it measured
    the convergence of the two branches that handed back a bare exit code with no rule attached:
    both paths said `model_safety`, pointing at a server log the caller could not read. ACE-094
    deleted both branches, so there is no unconverted branch left to converge and no interim rule.

    What is worth measuring now is the opposite property with the same parity. This fixture wires no
    warehouse, so both paths get as far as loading credentials and fail there — which is the
    evidence: a refusal returns long before credentials are touched, so reaching a `dsn` failure
    proves nothing refused the statement. That the two paths agree on it is the property this file
    exists for.
    """
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")

    artifacts = tmp_path / "art"
    _write_sensitive_model(artifacts / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    sql = "SELECT email FROM customers"

    tools.set_injected_executor(_SpyExecutor())
    in_process = _tool_out(sql)
    tools.set_injected_executor(None)
    forked = _tool_out(sql)

    assert in_process["status"] == forked["status"] == "failed", (in_process, forked)
    assert in_process["failure"]["kind"] == forked["failure"]["kind"] == "dsn"
    assert "refusal" not in in_process and "refusal" not in forked

    # And the forked stream is one JSON object again: the `{"error": {"kind": "sensitive_columns"}}`
    # diagnostic that used to precede the refusal line is gone with the branch that wrote it.
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "acme", "--sql", sql],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(artifacts)},
    )
    assert "sensitive_columns" not in proc.stderr, proc.stderr


def test_the_forked_child_keeps_its_audit_id_off_the_wire(tmp_path):
    """The child mints an id it does not publish; the parent mints the one that gets recorded.

    Two ids for one query would be two audit trails, so the wire shape stays exactly S2's — a
    refusal object and nothing else — and the id a caller sees is the parent's.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "acme", "--sql", "DROP TABLE t"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(tmp_path)},
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stderr)
    assert set(payload) == {"refusal"}
    assert set(payload["refusal"]) == {"reason", "rule", "detail", "remediation"}
    assert "audit_id" not in proc.stderr
