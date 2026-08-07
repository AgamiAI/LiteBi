"""ACE-051 — the hosted safety guard resolves the model from the DB and FAILS CLOSED when no model
can be found: a served query never runs with the fan/chasm/scope/PII guards silently off. Locally
(no DB configured) a not-yet-built model is still a no-op.

The branches these tests reach speak the shared guardrail contract, and now **return** it rather
than writing it: `_model_safety` hands back `Refusal | int | None`, and `execute_guarded` puts the
`Refusal` in the Envelope. So the assertions read the returned verdict where they used to read an
exit code plus a stderr line.

What is being pinned is unchanged and strictly stronger. The old rule was "stderr must parse WHOLE
as a single JSON object" — a stray diagnostic line would make the refusal unreadable to the parent,
and could carry DB connection details. These branches now write **nothing at all**, which is that
rule's limit case, so `_silent` asserts an empty stream. The wire itself is still exercised
end-to-end, one process boundary out, by `test_hosted_refusal_stderr_is_a_single_clean_json_object`
below: it runs the real CLI and parses its whole stderr, which is where a caller actually reads it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _silent(capsys) -> None:
    """Assert a converted branch wrote nothing.

    A converted gate returns its `Refusal`; the ONE writer is `main`. Anything on stderr from here
    would be a diagnostic that precedes the refusal on the wire — exactly what would make the
    stream unparseable to the parent, and exactly where a DB connection string would leak.
    """
    captured = capsys.readouterr()
    assert captured.err == "", captured.err
    assert captured.out == "", captured.out


def _refusal(verdict) -> guardrail.Refusal:
    """The verdict, asserted to be a real contract `Refusal` rather than the interim bare int the
    four unconverted branches still return."""
    assert isinstance(verdict, guardrail.Refusal), verdict
    return verdict


def _org() -> m.Datasource:
    """A model declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Datasource(
        datasource="Shop",
        # Declared because the guard reads every statement in this engine's grammar and refuses a
        # datasource naming none. It must match `_write_disk`'s declaration, or the two sources
        # would resolve different grammars and the disk/DB parity assertion below would be
        # measuring that rather than what it means to.
        storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[_t("orders"), _t("customers")])],
    )


def _seed_db(url: str, ds: str = "acme") -> None:
    import model_store
    from store import Store

    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, ds, _org())
    s.close()


def _write_disk(root: Path) -> None:
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    for t in ("orders", "customers"):
        (root / "subject_areas" / "sales" / "tables" / f"{t}.yaml").write_text(
            yaml.safe_dump({"name": t, "schema": "public", "storage_connection": "c", "grain": ["id"],
                            "description": t,
                            "columns": [{"name": "id", "type": "integer", "primary_key": True}]})
        )


def test_hosted_fail_closed_refuses_when_no_model(tmp_path, monkeypatch, capsys):
    # Hosted (DB configured) but NO model resolvable (DB migrated-but-empty + no disk) → refuse,
    # never run the query with the guards silently off.
    from store import Store

    url = "sqlite://" + str(tmp_path / "empty.db")
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_artifacts"))

    _, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert _refusal(verdict).rule == guardrail.RULE_MODEL_UNAVAILABLE  # refused, not run
    _silent(capsys)


def test_local_missing_model_is_noop(tmp_path, monkeypatch):
    # No DB configured → local path: a not-yet-built model legitimately means "no model" → no-op.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "empty"))

    sql, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert verdict is None and sql == "SELECT id FROM orders"  # unchanged, guards inert


def test_db_sourced_model_enforces_guards(tmp_path, monkeypatch, capsys):
    # Model in the DB, NOTHING on disk → the guards run off the DB-sourced model.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    _, verdict = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    # Undeclared table refused by the table-scope guard, sourced from the DB model.
    assert _refusal(verdict).rule == guardrail.RULE_TABLE_SCOPE
    _silent(capsys)

    sql, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert verdict is None  # a declared table with a named projection passes


def test_disk_db_verdict_parity(tmp_path, monkeypatch, capsys):
    # The same model sourced from disk vs the DB must yield identical guard verdicts.
    _write_disk(tmp_path / "art" / "acme")
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")

    def verdict(hosted: bool, sql: str):
        if hosted:
            monkeypatch.setenv("AGAMI_DB_URL", url)
            monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # DB is the only source
        else:
            monkeypatch.delenv("AGAMI_DB_URL", raising=False)
            monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))  # disk is the only source
        _, out = execute_sql._model_safety(sql, "acme", None)
        capsys.readouterr()
        # Compare the whole verdict, not just refuse-vs-allow: with the contract object returned
        # rather than serialized away, a DB round-trip that changed WHICH rule fired (or the
        # identifiers echoed in its detail) is now visible here too.
        return out

    # Query BOTH declared tables + an undeclared table + a bad column, so a lossy DB round-trip that
    # drops a table (customers) or mangles a column can't hide behind identical verdicts.
    for sql in (
        "SELECT id FROM sqlite_master",   # undeclared table → refuse (both)
        "SELECT id FROM orders",          # declared → allow (both)
        "SELECT id FROM customers",       # the OTHER declared table → allow only if it survived
        "SELECT nope FROM orders",        # undeclared column → refuse only if the column set survived
    ):
        assert verdict(hosted=True, sql=sql) == verdict(hosted=False, sql=sql), sql


def test_hosted_falls_back_to_disk_when_db_has_no_model(tmp_path, monkeypatch, capsys):
    # Hosted, DB configured but EMPTY, yet a disk model exists → guards run off disk (not fail-closed).
    from store import Store

    url = "sqlite://" + str(tmp_path / "empty.db")
    s = Store.connect(url)
    s.run_migrations()  # migrated, no model
    s.close()
    _write_disk(tmp_path / "art" / "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))

    _, verdict = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    # Refused by the disk-sourced model, NOT model_unavailable.
    assert _refusal(verdict).rule == guardrail.RULE_TABLE_SCOPE
    _silent(capsys)


def test_hosted_db_load_error_falls_back_to_disk(tmp_path, monkeypatch):
    # A DB that errors on load must degrade to the disk model, not crash or fail open.
    _write_disk(tmp_path / "art" / "acme")
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))

    sql, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert verdict is None  # disk model resolved + guards passed the declared query


def test_a_db_load_failure_leaks_nothing_into_the_refusal(tmp_path, monkeypatch, capsys):
    # A DB that ERRORS on load + no disk model, on hosted → fail closed. The load failure must NOT
    # produce freeform diagnostics (which on the wire would precede the JSON refusal → mixed,
    # unparseable stderr) and must not carry DB connection details into the refusal it returns.
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable → raises
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # no disk model either

    _, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)

    refusal = _refusal(verdict)
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    _silent(capsys)  # nothing written at all — the limit case of "one clean object"
    # No connection details leaked — checked against the whole serialized refusal, so it covers the
    # authored remediation this branch gained as well as the detail it already had.
    text = json.dumps({"refusal": {"reason": refusal.reason, "rule": refusal.rule,
                                   "detail": refusal.detail, "remediation": refusal.remediation}})
    assert "127.0.0.1" not in text and "pw" not in text
    assert str(tmp_path) not in text  # nor the resolved artifacts path we just probed


def test_hosted_refusal_stderr_is_a_single_clean_json_object(tmp_path):
    """The wire, one process boundary out: the fail-closed refusal reaches a real caller as exactly
    one JSON object on stderr.

    `_model_safety` no longer writes, so the single-clean-object property is now `main`'s to keep —
    and this is where it must be pinned, because this is where a caller actually reads it. The
    child is given a REACHABLE but empty app database and no disk model, so the only thing missing
    is the model.

    It used to be given an unreachable one, which was the same fail-closed condition until ACE-097:
    an unreachable app DB is now BOTH "no model source" and "no audit store", and the audit gate
    runs above the model pass, so the child refused `audit_unavailable` and this test's subject
    became unreachable. Isolating the cause is what keeps it testing what it says — the sibling
    in-process tests above call `_model_safety` directly and never met the new gate, which is why
    only this one moved.
    """
    app_db = tmp_path / "app.db"
    env = {
        **os.environ,
        "AGAMI_DB_URL": f"sqlite://{app_db}",
        "AGAMI_ARTIFACTS_DIR": str(tmp_path / "no_disk"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "acme", "--sql", "SELECT id FROM orders"],
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.count("\n") == 1  # one line, so no diagnostic rode alongside it
    payload = json.loads(proc.stderr)  # parses WHOLE → a single object
    assert set(payload) == {"refusal"}
    refusal = guardrail.Refusal(**payload["refusal"])  # and it satisfies the contract
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert "127.0.0.1" not in proc.stderr and "pw" not in proc.stderr
    assert str(tmp_path) not in proc.stderr


def test_hosted_fail_closed_when_model_package_unimportable(tmp_path, monkeypatch, capsys):
    # Even the model PACKAGE being unavailable must fail closed on hosted — the guards can't run at
    # all, which is the same "can't guarantee safety" condition as a missing model. Force the very
    # first `from semantic_model import runtime` to raise, so the except branch is what's exercised.
    import builtins

    real_import = builtins.__import__

    def boom(name, _globals=None, _locals=None, fromlist=(), level=0):
        if name == "semantic_model" and fromlist and "runtime" in fromlist:
            raise ImportError("forced: semantic_model.runtime unavailable")
        return real_import(name, _globals, _locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "x.db"))

    _, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    # Fail closed — no DB load is even attempted (we never resolve a model).
    assert _refusal(verdict).rule == guardrail.RULE_MODEL_UNAVAILABLE
    _silent(capsys)


# ── ACE-037: the third member of the family — no parser, and the scopability gate ──

def test_hosted_fail_closed_when_the_sql_parser_is_unavailable(tmp_path, monkeypatch, capsys):
    """Every gate opens with `if not _HAVE_SQLGLOT: return None`, so without sqlglot a served
    deployment resolves a model, reports itself guarded, and runs the statement with table scope,
    column scope, the star ban and the readability gate all silently inert.

    The sibling of the two branches above and pinned to the same rule: a deployment-state fact that
    says nothing about the statement, so no re-emission fixes it. The model here resolves fine —
    that is the point, since a missing model would refuse for the other reason and the assertion
    would pass while measuring nothing.
    """
    from semantic_model import runtime as RT

    url = "sqlite://" + str(tmp_path / "seeded.db")
    _seed_db(url)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.setattr(RT, "_HAVE_SQLGLOT", False)

    sql, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert _refusal(verdict).rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert verdict.reason == "undetermined"
    assert sql == "SELECT id FROM orders"  # refused, and never rewritten on the way out
    _silent(capsys)


def test_local_missing_sql_parser_is_noop(tmp_path, monkeypatch):
    """The local twin, for the third time and for the same reason: a bare install legitimately has
    no sqlglot, and refusing there would break every local user to close a hole that only exists on
    a served path."""
    from semantic_model import runtime as RT

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))
    _write_disk(tmp_path / "art" / "acme")
    monkeypatch.setattr(RT, "_HAVE_SQLGLOT", False)

    sql, verdict = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert verdict is None and sql == "SELECT id FROM orders"


_UNSCOPABLE_CORPUS = [
    "SELECT g FROM generate_series(1, 10) AS t(g)",                     # table function
    "SELECT a FROM ROWS FROM (generate_series(1,3)) AS t(a)",           # ROWS FROM
    "SELECT x FROM (VALUES (1), (2)) AS v(x)",                          # VALUES source
    "SELECT x FROM UNNEST(ARRAY[1,2]) AS t(x)",                         # UNNEST source
    "SELECT o.id FROM orders o, LATERAL (SELECT 1 AS a) l",             # LATERAL source
    "SELECT o.id FROM orders o, (VALUES (1),(2)) AS v(x)",              # shielded by a declared table
    "SELECT o.id FROM orders o, generate_series(1,10) AS t(g)",         # ditto, table function
    "SELECT id FROM orders UNION SELECT g FROM generate_series(1,3) AS t(g)",  # hidden in an arm
    "SELECT id FROM orders UNION ALL (VALUES (1))",                     # an arm that is not a source
]


@pytest.mark.parametrize("sql", _UNSCOPABLE_CORPUS)
def test_the_unscopable_corpus_is_refused_at_the_chokepoint(tmp_path, monkeypatch, capsys, sql):
    """The gate is wired, not merely written.

    `test_scopable_gate.py` calls the gate directly; this asserts each construct is refused where it
    matters — the one pass every surface goes through. Six of these eight reached the database
    before this gate existed, with the readability gate and both scope gates silent, so a unit test
    alone would have proved a function nobody called.
    """
    url = "sqlite://" + str(tmp_path / "seeded.db")
    _seed_db(url)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    out_sql, verdict = execute_sql._model_safety(sql, "acme", None)
    refusal = _refusal(verdict)
    assert refusal.rule == guardrail.RULE_UNSCOPABLE
    assert refusal.reason == "undetermined"
    assert out_sql == sql  # refused, never rewritten
    _silent(capsys)


def test_the_scopability_gate_runs_after_readability_and_before_table_scope(tmp_path, monkeypatch,
                                                                            capsys):
    """Ordering, asserted by the verdicts three statements get rather than by reading the source.

    An unreadable statement must still be `unparseable` (the gate above owns it), an undeclared
    table must still be `table_scope` (the gate below owns it), and only the readable-but-unscopable
    one is this gate's. A gate inserted in the wrong place returns the right refusal for the wrong
    statement, which no single-case test would catch.
    """
    url = "sqlite://" + str(tmp_path / "seeded.db")
    _seed_db(url)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    _, unreadable = execute_sql._model_safety("SELECT FROM WHERE ((", "acme", None)
    assert _refusal(unreadable).rule == guardrail.RULE_UNPARSEABLE

    _, unscopable = execute_sql._model_safety(
        "SELECT o.id FROM orders o, generate_series(1,3) AS t(g)", "acme", None)
    assert _refusal(unscopable).rule == guardrail.RULE_UNSCOPABLE

    _, out_of_scope = execute_sql._model_safety("SELECT id FROM secret", "acme", None)
    assert _refusal(out_of_scope).rule == guardrail.RULE_TABLE_SCOPE
    _silent(capsys)


def test_db_model_resolves_under_the_requests_org(tmp_path, monkeypatch, capsys):
    """The guard must look up the model under the REQUEST's org, not the 'local' sentinel.

    Regression: `_resolve_guard_model` called `load_datasource(store, profile)`, taking the
    `org_id='local'` default, while `model_deploy._default_org()` — since F14/F15 — stamps rows with the
    deployment's *resolved* id. `_default_org`'s docstring states the contract the two must keep: "the
    model is written under one org and read under another and the server sees no model." On a
    multi-tenant server every tenant's rows live under its own id, so the 'local' read missed for ALL of
    them and the fail-closed rule above refused every query.

    The artifacts dir is pointed at nothing on purpose: the disk fallback is exactly what masked this on
    single-tenant deployments for a whole release, so it must not be available to rescue the assertion.
    """
    import model_store
    import tools
    from store import Store

    url = "sqlite://" + str(tmp_path / "mt.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, "shop", _org(), org_id="contoso")  # a NAMED tenant, never 'local'
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)

    tools.resolved_org_id.cache_clear()
    token = tools._current_org_ctx.set("contoso")  # what the multi-tenant resolver set for this request
    try:
        _, verdict = execute_sql._model_safety("SELECT id FROM orders", "shop", None)
        assert verdict is None  # resolved under 'contoso': a declared table passes

        # ...and the guards genuinely ran off that model, rather than being inert.
        _, verdict = execute_sql._model_safety("SELECT id FROM sqlite_master", "shop", None)
        assert _refusal(verdict).rule == guardrail.RULE_TABLE_SCOPE
        _silent(capsys)
    finally:
        tools._current_org_ctx.reset(token)
        tools.resolved_org_id.cache_clear()
