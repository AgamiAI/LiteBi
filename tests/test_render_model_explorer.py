"""Tests for plugins/agami/scripts/render_model_explorer.py.

build_manifest now reads the semantic model (subject areas → the explorer's
top-level groups, tables → tables, columns → fields), with include_rejected=True
so excluded entries show (toggleable in the UI). These tests build a small model
on disk and assert the manifest + render output.

(The legacy apply_model_exclusions.py tests were retired — the model explorer now
applies exclude/include via semantic_model.curate; see test_semantic_model_curate.py.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_model_explorer import build_manifest, render  # noqa: E402


@pytest.fixture
def profile_dir(tmp_path):
    """A small model: area 'sales' with two tables; customers excluded, and
    orders.customer_id excluded."""
    root = tmp_path / "test"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "test", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                   {"storage_connection": "c", "schema": "public", "table": "customers"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer", "review_state": "rejected"},
                    {"name": "total", "type": "decimal"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "customers", "review_state": "rejected",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "name", "type": "string"}],
    }))
    (root / "subject_areas" / "sales" / "metrics" / "order_count.yaml").write_text(yaml.safe_dump({
        "name": "order_count", "calculation": "count of orders",
        "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"]}))
    return root


def test_manifest_captures_areas_tables_fields(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["schemas"] == 1
    assert m["totals"]["tables"] == 2
    assert m["totals"]["fields"] == 5  # 3 in orders + 2 in customers


def test_manifest_excluded_table_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    customers = next(t for t in m["schemas"][0]["tables"] if t["name"] == "customers")
    assert customers["excluded"] is True and customers["review_state"] == "rejected"


def test_manifest_excluded_column_flagged(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["excluded"] is True and cid["review_state"] == "rejected"
    for f in orders["fields"]:
        if f["name"] != "customer_id":
            assert f["excluded"] is False


def test_manifest_totals_excluded_counts(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["excluded_tables"] == 1
    assert m["totals"]["excluded_fields"] == 1


def test_manifest_qnames_use_area(profile_dir):
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    assert orders["qname"] == "sales.orders"
    cid = next(f for f in orders["fields"] if f["name"] == "customer_id")
    assert cid["qname"] == "sales.orders.customer_id"


def test_manifest_collects_metrics(profile_dir):
    m = build_manifest(profile_dir, "test")
    assert m["totals"]["metrics"] == 1
    met = m["metrics"][0]
    assert met["name"] == "order_count"
    assert "calculation" in met and "bindings" in met  # full metric fields, not just a name


def test_manifest_surfaces_full_model(profile_dir):
    # the comprehensive explorer needs everything in the manifest, not just tables
    area = profile_dir / "subject_areas" / "sales"
    (area / "entities").mkdir(parents=True, exist_ok=True)
    (area / "entities" / "customer.yaml").write_text(yaml.safe_dump({
        "name": "customer", "plural": "customers",
        "maps_to": [{"table": "customers", "column": "id", "primary": True}],
        "confidence": "inferred", "review_state": "unreviewed"}))
    (profile_dir / "prompt_examples" / "sales").mkdir(parents=True, exist_ok=True)
    (profile_dir / "prompt_examples" / "sales" / "examples.yaml").write_text(yaml.safe_dump([
        {"question": "how many orders", "sql": "SELECT COUNT(*) FROM orders", "source": "seed"}]))
    (profile_dir / "datasource.md").write_text("# About\nA shop. MRR = monthly recurring revenue.")
    (area / "relationships.yaml").write_text(yaml.safe_dump({"relationships": [
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customers",
         "to_column": "id", "relationship": "many_to_one", "review_state": "approved",
         "confidence": "confirmed", "signed_off_by": "agami_introspect",
         "signed_off_role": "system", "signed_off_at": "t"}]}))
    m = build_manifest(profile_dir, "test")
    assert "MRR = monthly recurring revenue" in m["datasource_md"]
    assert any(e["name"] == "customer" and e["maps_to"] == ["customers.id"] for e in m["entities"])
    assert any(x["question"] == "how many orders" for x in m["examples"])
    # the fixture's orders→customers FK relationship is surfaced
    assert any(r["from_table"] == "orders" and r["to_table"] == "customers" for r in m["relationships"])


def test_render_embeds_manifest_and_substitutes(profile_dir):
    import re
    m = build_manifest(profile_dir, "test")
    html = render(title="Model explorer · test", profile="test", manifest=m)
    assert not re.search(r"\{\{[A-Z_]+\}\}", html)  # all placeholders substituted
    assert "orders" in html and "order_count" in html


def test_sensitive_column_surfaced(profile_dir):
    p = profile_dir / "subject_areas" / "sales" / "tables" / "orders.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["columns"].append({"name": "ssn", "type": "string", "sensitive": True})
    p.write_text(yaml.safe_dump(doc))
    m = build_manifest(profile_dir, "test")
    orders = next(t for t in m["schemas"][0]["tables"] if t["name"] == "orders")
    ssn = next(f for f in orders["fields"] if f["name"] == "ssn")
    assert ssn["sensitive"] is True


def test_manifest_carries_table_description_source_ai_unknown(tmp_path):
    # an opaque TABLE (agami couldn't describe it) must surface description_source in the manifest
    # so the explorer's "couldn't read" pile can include whole tables, not just columns.
    root = tmp_path / "p"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "xyz"}]}))
    (root / "subject_areas" / "s" / "tables" / "xyz.yaml").write_text(yaml.safe_dump({
        "name": "xyz", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "", "description_source": "ai_unknown",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    m = build_manifest(root, "p")
    t = m["schemas"][0]["tables"][0]
    assert t["description_source"] == "ai_unknown"
