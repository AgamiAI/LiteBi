"""`model_deploy` loads the local YAML model into serving Postgres (idempotent, fail-closed).

Builds a minimal NEUTRAL artifacts fixture in a tmp dir and runs the real loader → real DB writers (no
mocking), then asserts the served model round-trips, re-running is idempotent, and the error paths exit non-zero.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import model_deploy  # noqa: E402
import model_store  # noqa: E402
from store import Store  # noqa: E402


def _write_model(root: Path, datasource: str, org_name: str = "acme") -> None:
    """A minimal but real v2 profile dir: org + one subject area + a table + examples + datasource.md."""
    d = root / datasource
    (d / "subject_areas" / "Catalog" / "tables").mkdir(parents=True, exist_ok=True)
    (d / "prompt_examples" / "Catalog").mkdir(parents=True, exist_ok=True)
    (d / "datasource.yaml").write_text(
        f"datasource: {org_name}\nversion: 1\ndescription: A neutral demo model.\n"
        "storage_connections:\n  - name: warehouse\n    storage_type: PostgreSQL\n"
        "subject_areas:\n  - Catalog\n"
    )
    (d / "subject_areas" / "Catalog" / "subject_area.yaml").write_text(
        "name: Catalog\ndescription: Products and pricing.\n"
    )
    (d / "subject_areas" / "Catalog" / "tables" / "products.yaml").write_text(
        "name: products\ndescription: Master product catalog.\n"
        "columns:\n  - name: id\n    type: uuid\n    primary_key: true\n  - name: sku\n    type: string\n"
    )
    (d / "prompt_examples" / "Catalog" / "examples.yaml").write_text(
        "examples:\n  - question: how many products?\n    sql: SELECT COUNT(*) FROM products\n"
    )
    (d / "datasource.md").write_text("# Acme\nNeutral demo domain notes.\n")


def _store(tmp_path: Path) -> Store:
    s = Store.connect("sqlite://" + str(tmp_path / "m.db"))
    s.run_migrations()
    return s


def _count(store: Store, table: str, datasource: str) -> int:
    return store.query(f"SELECT COUNT(*) AS n FROM {table} WHERE datasource = ?", (datasource,))[0]["n"]


def test_deploy_loads_model_and_round_trips(tmp_path):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    store = _store(tmp_path)
    loaded = model_deploy.deploy_models(store, arts)
    org = model_store.load_datasource(store, "demo")
    store.close()
    assert loaded == ["demo"]
    assert org is not None and org.datasource == "acme"
    assert [sa.name for sa in org.subject_areas] == ["Catalog"]
    assert [t.name for sa in org.subject_areas for t in sa.tables_defined] == ["products"]


def test_idempotent_reload_has_no_duplicates(tmp_path):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    store = _store(tmp_path)
    model_deploy.deploy_models(store, arts)
    first = (_count(store, "subject_area", "demo"), _count(store, "model_table", "demo"),
             _count(store, "prompt_example", "demo"))
    model_deploy.deploy_models(store, arts)  # re-run (the model-update-on-restart path)
    second = (_count(store, "subject_area", "demo"), _count(store, "model_table", "demo"),
              _count(store, "prompt_example", "demo"))
    store.close()
    assert first == second == (1, 1, 1)  # clear-then-insert → stable, no dupes


def test_examples_memory_and_version_are_loaded(tmp_path):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    store = _store(tmp_path)
    model_deploy.deploy_models(store, arts)
    examples = store.query("SELECT question FROM prompt_example WHERE datasource = ?", ("demo",))
    memory = store.query(
        "SELECT content FROM memory WHERE datasource = ? AND kind = 'datasource'", ("demo",)
    )
    version = store.query("SELECT version FROM model_version WHERE datasource = ?", ("demo",))
    store.close()
    assert any("how many products" in e["question"] for e in examples)
    assert memory and "Neutral demo domain" in memory[0]["content"]
    assert version  # a model_version row was written


def test_user_memory_is_loaded_from_the_artifacts_root(tmp_path, monkeypatch):
    # USER_MEMORY.md is install-global — it lives at the artifacts ROOT (not per profile) and writes one
    # shared row. main() handles it once (deploy_one does not).
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    (arts / "USER_MEMORY.md").write_text("# Preferences\nPrefer fiscal-year over calendar.\n")
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    assert model_deploy.main([]) == 0
    store = Store.connect("sqlite://" + str(tmp_path / "m.db"))
    user_rows = store.query("SELECT content FROM memory WHERE kind = 'user'")
    store.close()
    assert user_rows and "fiscal-year" in user_rows[0]["content"]  # the global user row was written


def test_redeploy_clears_removed_examples(tmp_path):
    # A redeploy after removing the examples file must clear the stale rows (write_examples always runs).
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    store = _store(tmp_path)
    model_deploy.deploy_models(store, arts)
    assert _count(store, "prompt_example", "demo") == 1
    (arts / "demo" / "prompt_examples" / "Catalog" / "examples.yaml").unlink()  # remove the examples
    model_deploy.deploy_models(store, arts)
    after = _count(store, "prompt_example", "demo")
    store.close()
    assert after == 0  # stale examples cleared, not left behind


def test_main_invalid_yaml_exits_one_not_traceback(tmp_path, monkeypatch):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    (arts / "demo" / "datasource.yaml").write_text("datasource: [unterminated\n")  # invalid YAML
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    assert model_deploy.main([]) == 1  # caught → clean exit, not an uncaught traceback


def test_multiple_datasources_each_load(tmp_path):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo", org_name="acme")
    _write_model(arts, "demo2", org_name="beta")
    # a non-model subdir (no datasource.yaml) must be skipped, not error
    (arts / "local").mkdir(parents=True, exist_ok=True)
    (arts / "local" / "credentials").write_text("[demo]\nhost=localhost\n")
    store = _store(tmp_path)
    loaded = model_deploy.deploy_models(store, arts)
    store.close()
    assert loaded == ["demo", "demo2"]  # both models, 'local' skipped


def test_main_deploys_all_then_a_named_datasource(tmp_path, monkeypatch):
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    assert model_deploy.main([]) == 0  # deploy every model under the dir (migrates first, then loads)
    assert model_deploy.main(["demo"]) == 0  # deploy a named datasource
    store = Store.connect("sqlite://" + str(tmp_path / "m.db"))
    org = model_store.load_datasource(store, "demo")
    store.close()
    assert org is not None and org.datasource == "acme"


def test_main_errors_when_no_model_found(tmp_path, monkeypatch):
    arts = tmp_path / "artifacts"
    arts.mkdir()  # exists but holds no model
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    assert model_deploy.main([]) == 1  # nothing to deploy → fail-closed


def test_malformed_examples_file_is_skipped_not_fatal(tmp_path):
    # A malformed examples file (a non-dict item) must not abort the deploy — the model still loads,
    # that area's examples are just skipped (examples are best-effort).
    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    (arts / "demo" / "prompt_examples" / "Catalog" / "examples.yaml").write_text(
        "examples:\n  - just a bare string\n"  # invalid: a scalar where a mapping is expected
    )
    store = _store(tmp_path)
    loaded = model_deploy.deploy_models(store, arts)  # must not raise
    org = model_store.load_datasource(store, "demo")
    n_examples = _count(store, "prompt_example", "demo")
    store.close()
    assert loaded == ["demo"] and org is not None  # the model deployed despite the bad examples file
    assert n_examples == 0  # the malformed area's examples were skipped, not partially written


def test_main_errors_when_artifacts_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "does-not-exist"))
    assert model_deploy.main([]) == 1  # clean exit, not an uncaught FileNotFoundError


def test_main_errors_when_no_database(monkeypatch):
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    assert model_deploy.main([]) == 2  # fail-closed, non-zero


def test_main_errors_naming_a_missing_datasource(tmp_path, monkeypatch):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    rc = model_deploy.main(["nope"])  # no nope/datasource.yaml
    assert rc == 1


# --- F14 / ACE-056 + ACE-057: minted org_id stamping + backfill --------------------------------

def test_deploy_stamps_minted_org_id(tmp_path, monkeypatch):
    # A model whose datasource.yaml carries a minted org_id: deploy resolves it (via _default_org ->
    # tools.resolved_org_id over the artifacts dir) and stamps serving rows with it, not 'local'.
    import tools

    arts = tmp_path / "artifacts"
    _write_model(arts, "demo")
    p = arts / "demo" / "datasource.yaml"
    p.write_text("org_id: mintedcafe\n" + p.read_text())
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()

    store = _store(tmp_path)
    model_deploy.deploy_one(store, "demo", arts / "demo")  # org_id=None -> resolves the minted id
    orgs = {r["org_id"] for r in store.query("SELECT DISTINCT org_id FROM datasource_model")}
    store.close()
    assert orgs == {"mintedcafe"}


def test_backfill_moves_local_rows_idempotently(tmp_path):
    store = _store(tmp_path)
    store.execute("INSERT INTO users (id, username, password_hash, created) VALUES ('u1','a@x','h','t')")
    store.execute("INSERT INTO datasource_model (org_id, datasource, doc) VALUES ('local','ds','{}')")
    store.execute("INSERT INTO tool_calls (id, ts, org_id, tool_name) VALUES ('t1','t','local','execute_sql')")
    store.commit()

    model_deploy._backfill_org_id(store, "the-uuid")

    def orgs(t):
        return {r["org_id"] for r in store.query(f"SELECT org_id FROM {t}")}

    assert orgs("users") == {"the-uuid"}
    assert orgs("datasource_model") == {"the-uuid"}
    assert orgs("tool_calls") == {"the-uuid"}
    # idempotent: re-run matches zero 'local' rows, nothing changes
    model_deploy._backfill_org_id(store, "the-uuid")
    assert orgs("users") == {"the-uuid"}
    store.close()


def test_backfill_noop_when_target_is_local(tmp_path):
    # A pre-F14 / un-minted deployment resolves 'local' -> the backfill must leave 'local' rows alone.
    store = _store(tmp_path)
    store.execute("INSERT INTO datasource_model (org_id, datasource, doc) VALUES ('local','ds','{}')")
    store.commit()
    model_deploy._backfill_org_id(store, "local")
    assert {r["org_id"] for r in store.query("SELECT org_id FROM datasource_model")} == {"local"}
    store.close()
