"""ACE-051 — the hosted safety guard resolves the model from the DB and FAILS CLOSED when no model
can be found: a served query never runs with the fan/chasm/scope/PII guards silently off. Locally
(no DB configured) a not-yet-built model is still a no-op.

The branches these tests reach now emit the shared guardrail contract on stderr —
`{"refusal": {reason, rule, detail, remediation}}` — so the assertions read `["refusal"]["rule"]`
where they used to read `["error"]["kind"]`. What is being pinned is unchanged and, at every site,
slightly stronger: stderr must parse WHOLE as a single JSON object (a stray diagnostic line would
make the refusal unreadable to the parent, and could carry DB connection details), the object must
carry nothing but the refusal, the refusal must satisfy the contract's own invariants, and no DSN
or path text may appear anywhere in it.
"""

from __future__ import annotations

import json
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


def _sole_refusal(capsys) -> guardrail.Refusal:
    """The refusal `_model_safety` wrote to stderr, rebuilt through the contract.

    `json.loads` runs over the WHOLE stream rather than scanning it for a JSON-looking line: that
    is what makes a stray diagnostic a failure here instead of something a line-scanner skips past,
    and it is the property `tools._stderr_refusal`'s callers depend on. Rebuilding through
    `Refusal` re-checks the contract's own invariants (a known reason, a non-empty detail AND
    remediation) at the emit site, so a branch that shipped an unactionable refusal fails here.
    """
    err = capsys.readouterr().err.strip()
    payload = json.loads(err)
    assert set(payload) == {"refusal"}, err  # nothing rides alongside it
    return guardrail.Refusal(**payload["refusal"])


def _org() -> m.Datasource:
    """A model declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Datasource(
        datasource="Shop",
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
        yaml.safe_dump({"datasource": "Shop", "version": 1, "subject_areas": ["subject_areas/sales"]})
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

    _, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code == 1  # refused, not run
    assert _sole_refusal(capsys).rule == guardrail.RULE_MODEL_UNAVAILABLE


def test_local_missing_model_is_noop(tmp_path, monkeypatch):
    # No DB configured → local path: a not-yet-built model legitimately means "no model" → no-op.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "empty"))

    sql, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None and sql == "SELECT id FROM orders"  # unchanged, guards inert


def test_db_sourced_model_enforces_guards(tmp_path, monkeypatch, capsys):
    # Model in the DB, NOTHING on disk → the guards run off the DB-sourced model.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    _, code = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    assert code == 1  # undeclared table refused by the table-scope guard, sourced from the DB model
    assert _sole_refusal(capsys).rule == guardrail.RULE_TABLE_SCOPE

    sql, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None  # a declared table with a named projection passes


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
        _, code = execute_sql._model_safety(sql, "acme", None)
        capsys.readouterr()
        return code

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

    _, code = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    assert code == 1  # refused by the disk-sourced model, NOT model_unavailable
    assert _sole_refusal(capsys).rule == guardrail.RULE_TABLE_SCOPE


def test_hosted_db_load_error_falls_back_to_disk(tmp_path, monkeypatch):
    # A DB that errors on load must degrade to the disk model, not crash or fail open.
    _write_disk(tmp_path / "art" / "acme")
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))

    sql, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None  # disk model resolved + guards passed the declared query


def test_refusal_stderr_is_a_single_clean_json_object(tmp_path, monkeypatch, capsys):
    # A DB that ERRORS on load + no disk model, on hosted → fail closed. The load failure must NOT
    # write freeform diagnostics (which would precede the JSON refusal → mixed/unparseable stderr,
    # and could leak DB connection details). stderr must be exactly one JSON refusal object.
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable → raises
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # no disk model either

    _, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code == 1
    err = capsys.readouterr().err.strip()
    payload = json.loads(err)  # parses WHOLE → a single object, nothing before or after it
    assert set(payload) == {"refusal"}
    refusal = guardrail.Refusal(**payload["refusal"])  # and it satisfies the contract
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    # No connection details leaked — checked against the raw stream, so it covers the authored
    # remediation this branch gained as well as the detail it already had.
    assert "127.0.0.1" not in err and "pw" not in err
    assert str(tmp_path) not in err  # nor the resolved artifacts path we just probed


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

    _, code = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code == 1  # fail closed — no DB load is even attempted (we never resolve a model)
    assert _sole_refusal(capsys).rule == guardrail.RULE_MODEL_UNAVAILABLE


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
        _, code = execute_sql._model_safety("SELECT id FROM orders", "shop", None)
        assert code is None  # resolved under 'contoso': a declared table passes

        # ...and the guards genuinely ran off that model, rather than being inert.
        _, code = execute_sql._model_safety("SELECT id FROM sqlite_master", "shop", None)
        assert code == 1
        assert _sole_refusal(capsys).rule == guardrail.RULE_TABLE_SCOPE
    finally:
        tools._current_org_ctx.reset(token)
        tools.resolved_org_id.cache_clear()
