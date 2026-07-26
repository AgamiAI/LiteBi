"""Entity display anchor (resolved_primary_table) + curate subject_area description edit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate  # noqa: E402
from semantic_model import models as m  # noqa: E402


def test_entity_resolved_primary_table_prefers_explicit_then_primary_then_first():
    e = m.Entity(name="assignee", primary_table="sys_user",
                 maps_to=[m.EntityMapping(table="incident", column="assigned_to")])
    assert e.resolved_primary_table == "sys_user"                      # explicit wins
    e2 = m.Entity(name="assignee", maps_to=[
        m.EntityMapping(table="incident", column="assigned_to"),
        m.EntityMapping(table="sys_user", column="sys_id", primary=True)])
    assert e2.resolved_primary_table == "sys_user"                     # primary mapping
    e3 = m.Entity(name="x", maps_to=[m.EntityMapping(table="a", column="c")])
    assert e3.resolved_primary_table == "a"                            # first mapping
    assert m.Entity(name="none").resolved_primary_table is None        # maps to nothing


def _area_model(tmp_path):
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
        "name": "s", "description": "Auto-proposed subject area covering: orders.",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders", "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    return root


def test_curate_edits_subject_area_description(tmp_path):
    root = _area_model(tmp_path)
    res = curate.apply(root, [{"op": "edit", "kind": "subject_area", "area": "s", "name": "s",
                               "field": "description", "value": "Customer orders and their line items."}])
    assert res.validated and res.applied, res.errors
    doc = yaml.safe_load((root / "subject_areas" / "s" / "subject_area.yaml").read_text())
    assert doc["description"] == "Customer orders and their line items."


def test_curate_subject_area_rejects_non_edit(tmp_path):
    root = _area_model(tmp_path)
    res = curate.apply(root, [{"op": "approve", "kind": "subject_area", "area": "s", "name": "s"}])
    assert res.skipped and not res.applied   # approve/reject don't apply to a subject area
