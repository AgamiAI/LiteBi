"""Tests for semantic_model/org_draft.py — the factual datasource.md draft, and the
render_model_explorer fallback that uses it so the file is never blank."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _model(root):
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (root / "subject_areas" / "s" / "entities").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales", "description": "orders & customers",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "one row per order",
        "performance_hints": {"estimated_row_count": 1234567},
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))
    (root / "subject_areas" / "s" / "metrics" / "total_revenue.yaml").write_text(yaml.safe_dump({
        "name": "total_revenue", "calculation": "sum of order amounts", "unit": "INR",
        "other_names": ["revenue"], "confidence": "inferred", "review_state": "unreviewed"}))
    (root / "subject_areas" / "s" / "entities" / "customer.yaml").write_text(yaml.safe_dump({
        "name": "customer", "plural": "customers",
        "maps_to": [{"table": "orders", "column": "id", "primary": True}],
        "other_names": ["client"], "confidence": "inferred", "review_state": "unreviewed"}))


def test_derived_context_is_pure_summary(tmp_path):
    from semantic_model import org_draft
    from semantic_model.loader import load_organization
    _model(tmp_path)
    d = org_draft.derived_context(load_organization(tmp_path))
    # SUMMARY shape: counts + subject areas + conventions + glossary — NOT a model dump
    assert "**acme** — 1 table across 1 subject area" in d
    assert "### Subject areas" in d and "sales" in d and "orders & customers" in d
    assert "1 metric and 1 entity are defined" in d
    assert "### Conventions" in d and "INR" in d
    assert "<!--" not in d                  # no human-only comment scaffolding in derived facts
    assert "total_revenue" not in d         # metric counted, not enumerated
    assert "one row per order" not in d and "1,234,567" not in d   # tables not dumped


def test_derived_context_can_exclude_curated_glossary(tmp_path):
    # the explorer renders the curated glossary as an EDITABLE panel, so it asks derived_context
    # to omit those terms — but the derived ENUM legends (from choice_field) still show read-only.
    from semantic_model import curate, org_draft
    from semantic_model.loader import load_organization
    _model(tmp_path)
    p = tmp_path / "subject_areas" / "s" / "tables" / "orders.yaml"
    d = yaml.safe_load(p.read_text())
    d["columns"].append({"name": "status", "type": "string", "choice_field": {"P": "pending"}})
    p.write_text(yaml.safe_dump(d))
    curate.set_key_terminology(tmp_path, {"MRR": "monthly recurring revenue"})
    org = load_organization(tmp_path)
    full = org_draft.derived_context(org)                                   # LLM: curated + enum
    explorer = org_draft.derived_context(org, with_curated_glossary=False)  # read-only: enum only
    assert "MRR" in full and "monthly recurring revenue" in full
    assert "MRR" not in explorer                                            # curated glossary excluded
    assert "orders.status" in explorer and "pending" in explorer           # enum legend still shown


def test_explorer_exposes_glossary_as_editable_field(tmp_path):
    from render_model_explorer import build_manifest
    from semantic_model import curate
    _model(tmp_path)
    curate.set_key_terminology(tmp_path, {"MRR": "monthly recurring revenue"})
    m = build_manifest(tmp_path, "acme")
    assert m["key_terminology"] == {"MRR": "monthly recurring revenue"}     # editable structured field
    assert "MRR" not in m["derived_context_md"]                            # not duplicated in the read-only block


def test_compose_keeps_human_narrative_and_derived_facts_separate(tmp_path):
    from semantic_model import org_draft
    from semantic_model.loader import load_organization
    _model(tmp_path)
    human = "# About this database\n\nWe are a lending startup. MRR = monthly recurring revenue.\n"
    md = org_draft.compose_context(human, load_organization(tmp_path))
    # the human's words are preserved verbatim...
    assert "We are a lending startup." in md and "MRR = monthly recurring revenue." in md
    # ...and the derived facts sit under their OWN heading, never mixed into the prose
    assert "## Model summary (auto-generated from your schema)" in md
    assert "### Subject areas" in md and "INR" in md
    # empty human → derived only; empty model-less call → empty
    assert "Model summary" in org_draft.compose_context("", load_organization(tmp_path))


def test_key_terminology_seeded_from_glossary_and_enums(tmp_path):
    # the section is no longer a bare prompt: curated glossary terms + auto-derived enum
    # legends from choice_field columns both render.
    from semantic_model import curate, org_draft
    from semantic_model.loader import load_organization
    _model(tmp_path)
    p = tmp_path / "subject_areas" / "s" / "tables" / "orders.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["columns"].append({"name": "status", "type": "string",
                           "choice_field": {"P": "pending", "S": "shipped"}})
    p.write_text(yaml.safe_dump(doc))
    res = curate.set_key_terminology(tmp_path, {"MRR": "monthly recurring revenue", "ARR": "annual recurring revenue"})
    assert res.validated and res.applied
    md = org_draft.draft_organization_md(load_organization(tmp_path))
    assert "**MRR** — monthly recurring revenue" in md
    assert "**ARR** — annual recurring revenue" in md
    assert "orders.status" in md and "`P` = pending" in md   # auto enum legend
    assert "only you can fill this in" not in md             # bare placeholder is gone


def test_set_key_terminology_merges_then_replaces(tmp_path):
    from semantic_model import curate
    from semantic_model.loader import load_organization
    _model(tmp_path)
    assert curate.set_key_terminology(tmp_path, {"MRR": "monthly recurring revenue"}).validated
    curate.set_key_terminology(tmp_path, {"churn": "no order in 90 days"})             # merge (default)
    assert load_organization(tmp_path).key_terminology == {
        "MRR": "monthly recurring revenue", "churn": "no order in 90 days"}
    curate.set_key_terminology(tmp_path, {"gold tier": "lifetime spend over 10k"}, merge=False)  # replace
    assert load_organization(tmp_path).key_terminology == {"gold tier": "lifetime spend over 10k"}


def test_explorer_org_md_is_human_only_derived_is_a_separate_field(tmp_path):
    from render_model_explorer import build_manifest
    _model(tmp_path)
    # no datasource.md → editable field is the human STARTER (prompt only, NO facts)...
    m = build_manifest(tmp_path, "acme")
    assert "About this database" in m["organization_md"]
    assert "Subject areas" not in m["organization_md"]      # facts never in the editable file
    # ...and the model-derived facts live in their own read-only field
    assert "Subject areas" in m["derived_context_md"] and "sales" in m["derived_context_md"]
    # a comments-only file is still "blank" → starter; facts stay separate
    (tmp_path / "datasource.md").write_text("<!-- nothing here yet -->\n")
    m2 = build_manifest(tmp_path, "acme")
    assert "About this database" in m2["organization_md"] and "Subject areas" in m2["derived_context_md"]
    # a real human file is left as-is in the editable field; facts still separate
    (tmp_path / "datasource.md").write_text("# About\nWe are a lending startup.")
    m3 = build_manifest(tmp_path, "acme")
    assert m3["organization_md"] == "# About\nWe are a lending startup."
    assert "Subject areas" in m3["derived_context_md"]
