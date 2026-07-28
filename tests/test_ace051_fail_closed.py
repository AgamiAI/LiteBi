"""ACE-051 — the hosted safety guard resolves the model from the DB and FAILS CLOSED when no model
can be found: a served query never runs with the fan/chasm/scope/PII guards silently off. Locally
(no DB configured) a not-yet-built model is still a no-op.
"""

from __future__ import annotations

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
from semantic_model import models as m  # noqa: E402


def _org() -> m.Datasource:
    """A model declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Datasource(
        datasource="Shop",
        storage_connections=[m.StorageConnection(name="c", storage_type="SQLite")],
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
                        "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
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

    _, refusal, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert refusal is not None and refusal.kind == "model_unavailable"  # refused (returned, not run)
    assert capsys.readouterr().err == ""  # _model_safety returns the refusal, no stderr side-effect


def test_local_missing_model_is_noop(tmp_path, monkeypatch):
    # No DB configured → local path: a not-yet-built model legitimately means "no model" → no-op.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "empty"))

    sql, code, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None and sql == "SELECT id FROM orders"  # unchanged, guards inert


def test_db_sourced_model_enforces_guards(tmp_path, monkeypatch, capsys):
    # Model in the DB, NOTHING on disk → the guards run off the DB-sourced model.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))

    _, refusal, _ = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    assert refusal is not None and refusal.kind == "table_out_of_scope"  # undeclared table, DB model
    assert capsys.readouterr().err == ""

    sql, refusal, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert refusal is None  # a declared table with a named projection passes


def test_db_model_under_minted_org_resolves_for_guard(tmp_path, monkeypatch, capsys):
    # REGRESSION (ACE-051 org-scoping): a model seeded under a real MINTED org (not the 'local'
    # default) must resolve for the safety guard when the runtime org matches — the guard read has to
    # scope by org exactly like the serve path (tools._load_org). Before the fix, the guard's DB read
    # defaulted to model_store.DEFAULT_ORG ('local'), so a non-'local' model was invisible to the
    # guard even though it served fine → a false `model_unavailable` refusal on every query of a
    # hosted, DB-only deploy (no baked /artifacts). The other DB tests here can't catch this: they
    # seed AND read under the default 'local' org, so the scoping is a no-op.
    import model_store
    from store import Store

    ORG = "09c499f4fe4e4203b178e04e76564faa"  # a minted org id, like a real deployment's
    url = "sqlite://" + str(tmp_path / "model.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, "acme", _org(), org_id=ORG)  # seeded under the MINTED org
    s.close()

    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ORG_ID", ORG)  # runtime org == the seeded org (what _guard_org_id reads)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # DB is the ONLY source
    import tools

    tools.resolved_org_id.cache_clear()  # memoized; force a re-resolve under this test's env
    try:
        # The model resolves under the minted org → a declared table passes (NOT model_unavailable).
        _, refusal, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
        assert refusal is None, "guard-model must resolve under the minted org, not fail closed"
        # And the guards actually run off that DB-sourced model (undeclared table still refused).
        _, refusal2, _ = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
        assert refusal2 is not None and refusal2.kind == "table_out_of_scope"
        assert capsys.readouterr().err == ""
    finally:
        tools.resolved_org_id.cache_clear()  # don't leak the minted org into later tests


def test_disk_db_verdict_parity(tmp_path, monkeypatch, capsys):
    # The same model sourced from disk vs the DB must yield identical guard verdicts.
    monkeypatch.delenv("AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False)  # default enforce for the rows below
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
        _, code, _ = execute_sql._model_safety(sql, "acme", None)
        capsys.readouterr()
        return code

    # Query BOTH declared tables + an undeclared table + a bad column, so a lossy DB round-trip that
    # drops a table (customers) or mangles a column can't hide behind identical verdicts. The last two
    # rows are ACE-037's SC5: an unscopable query must produce the SAME fail-closed verdict on either
    # source — the scopability gate is path-agnostic (it reads the parsed tree + the resolved model,
    # never the datasource), so file-served and DB-served models refuse identically.
    for sql in (
        "SELECT id FROM sqlite_master",  # undeclared table → refuse (both)
        "SELECT id FROM orders",  # declared → allow (both)
        "SELECT id FROM customers",  # the OTHER declared table → allow only if it survived
        "SELECT nope FROM orders",  # undeclared column → refuse only if the column set survived
    ):
        assert verdict(hosted=True, sql=sql) == verdict(hosted=False, sql=sql), sql

    # The unscopable rows must both REFUSE (a Refusal, not None), not merely agree — pins the
    # fail-closed half of SC5 (a silent no-op on BOTH sources would satisfy equality alone).
    for sql in (
        "SELECT g FROM generate_series(1, 10) AS t(g)",  # table-function
        "SELECT x FROM (VALUES (1)) AS v(x)",  # VALUES source
    ):
        h, d = verdict(hosted=True, sql=sql), verdict(hosted=False, sql=sql)
        assert h is not None and h.kind == "unscopable_sql", sql
        assert d is not None and d.kind == "unscopable_sql", sql


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

    _, refusal, _ = execute_sql._model_safety("SELECT id FROM sqlite_master", "acme", None)
    assert refusal is not None and refusal.kind == "table_out_of_scope"  # disk model, NOT model_unavailable
    assert capsys.readouterr().err == ""


def test_hosted_db_load_error_falls_back_to_disk(tmp_path, monkeypatch):
    # A DB that errors on load must degrade to the disk model, not crash or fail open.
    _write_disk(tmp_path / "art" / "acme")
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "art"))

    sql, code, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert code is None  # disk model resolved + guards passed the declared query


def test_refusal_stderr_is_a_single_clean_json_object(tmp_path, monkeypatch, capsys):
    # A DB that ERRORS on load + no disk model, on hosted → fail closed. The load failure must NOT
    # write freeform diagnostics (which would precede the JSON refusal → mixed/unparseable stderr,
    # and could leak DB connection details). stderr must be exactly one JSON refusal object.
    monkeypatch.setenv("AGAMI_DB_URL", "postgres://user:pw@127.0.0.1:1/nope")  # unreachable → raises
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))  # no disk model either

    _, refusal, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert refusal is not None and refusal.kind == "model_unavailable"
    # The DB load error must not leak connection details into the refusal reason, and _model_safety
    # writes NOTHING to stderr — the JSON is emitted exactly once, by main()/execute_guarded.
    assert "127.0.0.1" not in refusal.reason and "pw" not in refusal.reason
    assert capsys.readouterr().err == ""


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

    _, refusal, _ = execute_sql._model_safety("SELECT id FROM orders", "acme", None)
    assert refusal is not None and refusal.kind == "model_unavailable"  # fail closed, no DB load
    assert capsys.readouterr().err == ""


# ── ACE-037: the scopability gate wired into _model_safety + the posture flag ─────────────────

# The differential corpus — queries that parse but don't scope (a source the scope walk can't
# resolve). Fixtures live here; the broad regression suite is ACE-040's.
_UNSCOPABLE_CORPUS = [
    "SELECT g FROM generate_series(1, 10) AS t(g)",  # table-function
    "SELECT a FROM ROWS FROM (generate_series(1, 3)) AS t(a)",  # ROWS FROM
    "SELECT x FROM (VALUES (1), (2)) AS v(x)",  # VALUES source
    "SELECT x FROM UNNEST(ARRAY[1, 2]) AS t(x)",  # UNNEST source
    "SELECT a FROM orders o, LATERAL (SELECT 1 AS a) l",  # LATERAL source
    "SELECT id FROM orders UNION SELECT g FROM generate_series(1, 3) AS t(g)",  # unscopable set-op arm
]


@pytest.mark.parametrize("sql", _UNSCOPABLE_CORPUS)
def test_unscopable_corpus_refused_under_enforce(tmp_path, monkeypatch, capsys, sql):
    # Every parse-but-don't-scope construct is refused (unscopable_sql) by _model_safety under the
    # default `enforce` posture — proving the gate is REACHED in the wired pass (no silent pass).
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.delenv("AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False)  # default = enforce

    _, refusal, _ = execute_sql._model_safety(sql, "acme", None)
    assert refusal is not None and refusal.kind == "unscopable_sql"  # refused, never executed
    assert capsys.readouterr().err == ""


def test_unscopable_allowed_and_logged_under_warn(tmp_path, monkeypatch, capsys):
    # `warn` is the staged-rollout escape hatch: log + allow, do NOT refuse.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.setenv("AGAMI_SQL_UNSCOPABLE_POSTURE", "warn")

    _, refusal, _ = execute_sql._model_safety("SELECT g FROM generate_series(1, 10) AS t(g)", "acme", None)
    assert refusal is None  # allowed (not refused)
    err = capsys.readouterr().err
    assert "unscopable SQL allowed" in err and "warn" in err
    assert '"unscopable_sql"' not in err  # no refusal emitted


@pytest.mark.parametrize("posture", ["off", "disable", "warm", "", "ENFORCE ", "0"])
def test_unknown_posture_value_fails_closed(tmp_path, monkeypatch, capsys, posture):
    # The posture flag's core safety invariant: ONLY exact `warn` allows; every other value
    # (typo, empty, garbage) must fail closed (refuse). Guards against a future refactor that flips
    # the default open.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.setenv("AGAMI_SQL_UNSCOPABLE_POSTURE", posture)

    _, refusal, _ = execute_sql._model_safety("SELECT g FROM generate_series(1, 3) AS t(g)", "acme", None)
    assert refusal is not None and refusal.kind == "unscopable_sql"
    assert capsys.readouterr().err == ""


def test_scopability_gate_runs_before_table_scope(tmp_path, monkeypatch, capsys):
    # A table-function is unscopable AND references no declared table; it must refuse as
    # `unscopable_sql` (the gate runs first), not fall through to a different verdict.
    url = "sqlite://" + str(tmp_path / "model.db")
    _seed_db(url, "acme")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk"))
    monkeypatch.delenv("AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False)

    _, refusal, _ = execute_sql._model_safety("SELECT g FROM generate_series(1, 3) AS t(g)", "acme", None)
    assert refusal is not None and refusal.kind == "unscopable_sql"
    assert capsys.readouterr().err == ""
