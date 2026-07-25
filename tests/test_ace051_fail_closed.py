"""ACE-051 — the hosted safety guard resolves the model from the DB and FAILS CLOSED when no model
can be found: a served query never runs with the fan/chasm/scope/PII guards silently off. Locally
(no DB configured) a not-yet-built model is still a no-op.
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
from semantic_model import models as m  # noqa: E402


def _org() -> m.Organization:
    """A model declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Organization(
        datasource="Shop",
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[_t("orders"), _t("customers")])],
    )


def _seed_db(url: str, ds: str = "acme") -> None:
    import model_store
    from store import Store

    s = Store.connect(url)
    s.run_migrations()
    model_store.write_organization(s, ds, _org())
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
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "model_unavailable"


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
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "table_out_of_scope"

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
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "table_out_of_scope"


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
    assert json.loads(err)["error"]["kind"] == "model_unavailable"  # parses whole → single object
    assert "127.0.0.1" not in err and "pw" not in err  # no connection details leaked


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
    assert json.loads(capsys.readouterr().err.strip())["error"]["kind"] == "model_unavailable"
