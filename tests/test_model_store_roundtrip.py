"""Serve the model from the DB — golden parity with the file loader (Slice C).

Two proofs: (1) writing then loading an Organization through the DB is lossless for every object
type; (2) a model loaded from YAML files, seeded to the DB, and re-loaded from a *fresh* connection
yields the identical Organization — and tools._load_org / get_datasource_schema serve from the DB
(files absent) when AGAMI_DB_URL is set.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import model_store  # noqa: E402
import tools  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model.models import Organization  # noqa: E402
from store import Store  # noqa: E402

FULL_ORG = {
    "organization": "acme",
    "version": 1,
    "description": "Acme Inc.",
    "fiscal_year_start_month": 4,
    "key_terminology": {"MRR": "monthly recurring revenue"},
    "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
    "subject_areas": [
        {
            "name": "sales",
            "description": "Orders + revenue",
            "default_time_window": "last_90_days",
            "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            "tables_defined": [
                {
                    "name": "orders",
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": "one row per order",
                    "columns": [
                        {"name": "id", "type": "integer", "primary_key": True},
                        {"name": "amount", "type": "decimal"},
                    ],
                    "performance_hints": {"estimated_row_count": 2_000_000},
                },
            ],
            "metrics": [
                {"name": "revenue", "calculation": "sum of amount", "other_names": ["sales"]}
            ],
            "entities": [{"name": "customer", "value_pattern": "^C[0-9]+$"}],
            "relationships": [
                {
                    "from_table": "orders",
                    "from_column": "customer_id",
                    "to_table": "customers",
                    "to_column": "id",
                    "relationship": "many_to_one",
                    "confidence": "inferred",
                    "review_state": "unreviewed",
                }
            ],
        }
    ],
}


def test_db_roundtrip_is_lossless_for_every_object_type():
    org = Organization.model_validate(FULL_ORG)
    s = Store.connect("sqlite://")
    s.run_migrations()
    model_store.write_organization(s, "main", org)
    rebuilt = model_store.load_organization(s, "main")
    assert rebuilt is not None
    assert rebuilt.model_dump(mode="json") == org.model_dump(mode="json")
    s.close()


def test_load_missing_datasource_returns_none():
    s = Store.connect("sqlite://")
    s.run_migrations()
    assert model_store.load_organization(s, "nope") is None
    s.close()


def test_reseed_replaces_rows():
    s = Store.connect("sqlite://")
    s.run_migrations()
    org = Organization.model_validate(FULL_ORG)
    model_store.write_organization(s, "main", org)
    model_store.write_organization(s, "main", org)  # idempotent re-seed, not a duplicate
    assert len(s.query("SELECT name FROM subject_area WHERE datasource='main'")) == 1
    s.close()


# --- file → DB parity + tools wiring ---------------------------------------


def _write_file_model(root):
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    a = root / "subject_areas" / "sales"
    (a / "tables").mkdir(parents=True)
    (a / "metrics").mkdir()
    (a / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "description": "Orders",
                "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            }
        )
    )
    (a / "tables" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "o",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal"},
                ],
            }
        )
    )
    (a / "metrics" / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "revenue",
                "calculation": "sum of amount",
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )
    (root / "org.yaml").write_text(
        yaml.safe_dump(
            {
                "organization": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )


def test_file_model_seeds_to_db_and_tools_serve_from_it(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _write_file_model(art / "main")
    file_org = L.load_organization(art / "main")

    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    model_store.write_organization(s, "main", file_org)
    s.commit()
    s.close()

    # a fresh connection (a "second instance") rebuilds the identical Organization
    s2 = Store.connect(db_url)
    db_org = model_store.load_organization(s2, "main")
    s2.close()
    assert db_org.model_dump(mode="json") == file_org.model_dump(mode="json")

    # tools._load_org serves from the DB when AGAMI_DB_URL is set
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    assert tools._load_org("main").model_dump(mode="json") == file_org.model_dump(mode="json")

    # get_datasource_schema's structured head is identical DB-served vs file-served
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    db_head = json.JSONDecoder().raw_decode(
        tools.tool_get_datasource_schema({"datasource": "main"})
    )[0]
    monkeypatch.delenv("AGAMI_DB_URL")
    file_head = json.JSONDecoder().raw_decode(
        tools.tool_get_datasource_schema({"datasource": "main"})
    )[0]
    assert db_head["mode"] == file_head["mode"]
    assert db_head["subject_areas"] == file_head["subject_areas"]
    assert db_head["metric_index"] == file_head["metric_index"]


def test_memory_and_model_version_round_trip():
    s = Store.connect("sqlite://")
    s.run_migrations()
    model_store.write_memory(
        s, "main", organization="# About\nAcme sells widgets.", user="prefer USD"
    )
    model_store.write_model_version(s, "main", "v-abc123", created_at="2026-06-25T00:00:00Z")
    assert model_store.load_memory(s, "main") == {
        "organization": "# About\nAcme sells widgets.",
        "user": "prefer USD",
    }
    assert model_store.newest_model_version(s, "main") == "v-abc123"
    s.close()


def test_tools_serve_memory_and_version_from_db_no_files(tmp_path, monkeypatch):
    # The spec's "no tool reads a file at runtime": domain context + the receipt version pin come
    # from the DB, with NO artifacts dir on disk.
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    model_store.write_memory(
        s, "main", organization="# Acme\nWidgets co.", user="exclude test users"
    )
    model_store.write_model_version(s, "main", "v-deadbeef")
    s.close()

    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "does-not-exist"))
    assert tools._model_version("main") == "v-deadbeef"
    org_md, user_md, _, _ = tools._context_sources("main", tools._current_org_id())
    assert "Widgets co." in org_md and user_md == "exclude test users"


def _seed_org(tmp_path, monkeypatch, org_dict) -> None:
    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    model_store.write_organization(s, "main", Organization.model_validate(org_dict))
    s.close()
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "none"))


def _schema_head(**args) -> dict:
    out = tools.tool_get_datasource_schema({"datasource": "main", **args})
    return json.JSONDecoder().raw_decode(out)[0]


def test_metric_name_collision_keeps_both_metrics(tmp_path, monkeypatch):
    # C2: two subject areas with a metric of the same name — both must survive in metric_index
    # (the never-hide contract), not silently collapse to one.
    _seed_org(
        tmp_path,
        monkeypatch,
        {
            "organization": "acme",
            "version": 1,
            "subject_areas": [
                {"name": "sales", "metrics": [{"name": "revenue", "calculation": "gross"}]},
                {"name": "finance", "metrics": [{"name": "revenue", "calculation": "net"}]},
            ],
        },
    )
    idx = _schema_head(mode="index")["metric_index"]
    assert sum(1 for k in idx if k == "revenue" or k.startswith("revenue (")) == 2


def test_index_floor_sheds_full_metrics_and_flags_truncated(tmp_path, monkeypatch):
    # C1/C3: when even `index` + the inline matched metrics blow the 60K budget, the full `metrics`
    # list is shed (metric_index still lists every metric) and `truncated` is set — never silent.
    metrics = [
        {"name": f"m{i}", "calculation": "c" * 500, "description": "short"} for i in range(200)
    ]
    _seed_org(
        tmp_path,
        monkeypatch,
        {
            "organization": "acme",
            "version": 1,
            "subject_areas": [{"name": "a", "metrics": metrics}],
        },
    )
    head = _schema_head(mode="full", query="m")  # "m" substring-matches every m<i> → all "strong"
    assert head["truncated"] is True
    assert head["metrics"] == []  # full detail shed at the floor
    assert len(head["metric_index"]) == 200  # but every metric is still listed by name
    assert len(json.dumps(head)) <= tools._SCHEMA_CHAR_BUDGET  # shedding brought it under budget
