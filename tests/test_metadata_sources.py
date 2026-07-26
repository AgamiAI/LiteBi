"""Tests for metadata_sources — enrich a model from in-DB metadata/lookup tables.

The pure transforms (rows -> curate ops / reference specs) test without a database. One
integration test runs the generated ops through `curate.apply` to confirm they land and that a
dictionary-sourced description is stamped with the authoritative `metadata` provenance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402
from semantic_model import metadata_sources as M  # noqa: E402

# --- canned ServiceNow-shaped source rows -----------------------------------

SYS_CHOICE = [
    {"name": "incident", "element": "severity", "value": "1", "label": "High"},
    {"name": "incident", "element": "severity", "value": "2", "label": "Medium"},
    {"name": "incident", "element": "severity", "value": "3", "label": "Low"},
    {"name": "incident", "element": "state", "value": "1", "label": "New"},
    {"name": "incident", "element": "state", "value": "", "label": "blank-value-skip"},
    {"name": "incident", "element": "note", "value": "x", "label": ""},   # blank label → skip
]

SYS_DICTIONARY = [
    {"name": "incident", "element": "severity", "column_label": "Severity",
     "comments": "Severity level of the incident impact.", "internal_type": "integer", "reference": ""},
    {"name": "incident", "element": "short_description", "column_label": "Short description",
     "comments": "", "internal_type": "string", "reference": ""},          # falls back to label
    {"name": "incident", "element": "caller_id", "column_label": "Caller",
     "comments": "Person who reported it.", "internal_type": "reference", "reference": "sys_user"},
    {"name": "incident", "element": "blankcol", "column_label": "", "comments": "",
     "internal_type": "string", "reference": ""},                          # no text → no op
]


# --- pure transforms --------------------------------------------------------

def test_choice_field_ops_groups_and_skips_blanks():
    ops = M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                             value_col="value", label_col="label")
    by = {(o["name"], o["column"]): o["value"] for o in ops}
    assert by[("incident", "severity")] == {"1": "High", "2": "Medium", "3": "Low"}
    assert by[("incident", "state")] == {"1": "New"}          # blank-value row dropped
    assert ("incident", "note") not in by                     # blank-label column → no op
    assert all(o["op"] == "edit" and o["field"] == "choice_field" for o in ops)


def test_choice_field_ops_valid_filter_restricts_to_model_columns():
    ops = M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                             value_col="value", label_col="label",
                             valid={("incident", "severity")})
    assert {(o["name"], o["column"]) for o in ops} == {("incident", "severity")}


def test_description_ops_prefers_comment_then_label_with_metadata_source():
    ops = M.description_ops(SYS_DICTIONARY, table_col="name", column_col="element",
                            label_col="column_label", comment_col="comments")
    by = {(o["name"], o["column"]): o for o in ops}
    assert by[("incident", "severity")]["value"] == "Severity level of the incident impact."
    assert by[("incident", "short_description")]["value"] == "Short description"  # label fallback
    assert ("incident", "blankcol") not in by                 # no text → no op
    assert all(o["source"] == "metadata" for o in ops)        # authoritative provenance


def test_reference_declarations_keyed_by_field_not_table():
    # keyed by the FIELD name (so inherited fields resolve for children), reference rows only
    decls = M.reference_declarations(SYS_DICTIONARY, column_col="element",
                                     type_col="internal_type", reference_col="reference")
    assert decls == {"caller_id": "sys_user"}   # the one reference row; non-reference rows ignored


def test_reference_declarations_drops_conflicting_targets():
    rows = [
        {"element": "owner", "internal_type": "reference", "reference": "sys_user"},
        {"element": "owner", "internal_type": "reference", "reference": "sys_user_group"},  # conflict
        {"element": "company", "internal_type": "reference", "reference": "core_company"},
    ]
    decls = M.reference_declarations(rows, column_col="element",
                                     type_col="internal_type", reference_col="reference")
    assert decls == {"company": "core_company"}   # ambiguous `owner` dropped


def test_descriptions_by_element_propagates_parent_declarations():
    # ServiceNow declares shared fields under the PARENT table (task); the description must be
    # keyed by element so it reaches the inheriting children (incident/problem).
    rows = [
        {"element": "priority", "name": "task", "column_label": "Priority", "comments": ""},
        {"element": "business_impact", "name": "task", "column_label": "Business impact", "comments": ""},
        {"element": "state", "name": "task", "column_label": "State", "comments": ""},
        {"element": "state", "name": "sys_user", "column_label": "Active flag", "comments": ""},  # conflict
    ]
    by_el = M.descriptions_by_element(rows, column_col="element",
                                      label_col="column_label", comment_col="comments")
    assert by_el == {"priority": "Priority", "business_impact": "Business impact"}  # `state` dropped (conflict)


def test_detect_preset_and_usable_sources():
    assert M.detect_preset(["incident", "sys_choice", "sys_user"]) == "servicenow"
    assert M.detect_preset(["orders", "customers"]) is None
    # only sys_choice present → only the choice role is usable
    usable = M.usable_sources("servicenow", ["incident", "sys_choice"])
    assert set(usable) == {"choice"}
    assert M.usable_sources("servicenow", ["incident", "sys_dictionary"]).keys() == {"dictionary"}


# --- integration: ops actually apply, metadata provenance persists ----------

def _incident_model(root: Path) -> None:
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "itsm" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "sn", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/itsm"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "Redshift"}))
    (root / "subject_areas" / "itsm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "itsm",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "incident"}],
    }))
    (root / "subject_areas" / "itsm" / "tables" / "incident.yaml").write_text(yaml.safe_dump({
        "name": "incident", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "ServiceNow incident records",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "severity", "type": "integer"},
                    {"name": "short_description", "type": "string"}],
    }))


def test_generated_ops_apply_and_stamp_metadata(tmp_path):
    _incident_model(tmp_path)
    valid = {("incident", "severity"), ("incident", "short_description")}
    ops = (M.choice_field_ops(SYS_CHOICE, table_col="name", column_col="element",
                              value_col="value", label_col="label", valid=valid)
           + M.description_ops(SYS_DICTIONARY, table_col="name", column_col="element",
                               label_col="column_label", comment_col="comments", valid=valid))
    res = curate.apply(tmp_path, ops)
    assert res.validated and not res.errors, res.as_dict()
    assert len(res.applied) == 3

    org = loader.load_datasource(tmp_path)
    incident = next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == "incident")
    cols = {c.name: c for c in incident.columns}
    assert cols["severity"].choice_field == {"1": "High", "2": "Medium", "3": "Low"}
    assert cols["severity"].description == "Severity level of the incident impact."
    assert cols["severity"].description_source == "metadata"          # authoritative, not a guess
    assert cols["short_description"].description == "Short description"


def test_inheritance_inherited_reference_applies_to_all_children():
    # THE fix: `assignment_group → sys_user_group` is declared ONCE (as on base `task`), but the
    # COLUMN exists on both children — so both incident AND problem must get the join.
    from semantic_model import cli
    from semantic_model import models as mm
    decls = M.reference_declarations(
        [{"element": "assignment_group", "internal_type": "reference", "reference": "sys_user_group"}],
        column_col="element", type_col="internal_type", reference_col="reference")

    def tbl(name, extra):
        cols = [mm.Column(name="id", type="integer", primary_key=True)]
        cols += [mm.Column(name=c, type="string") for c in extra]
        return mm.Table(name=name, schema="public", storage_connection="c", grain=["id"], columns=cols)
    itsm = mm.SubjectArea(name="itsm", description="d",
                          tables_defined=[tbl("incident", ["assignment_group"]),
                                          tbl("problem", ["assignment_group"])])
    sysa = mm.SubjectArea(name="sys", description="d", tables_defined=[tbl("sys_user_group", [])])
    org = mm.Datasource(datasource="o", version=1, subject_areas=[itsm, sysa])
    specs = [{"from_table": t.name, "from_column": c.name, "to_table": decls[c.name.lower()]}
             for sa in org.subject_areas for t in sa.tables_defined for c in t.columns
             if c.name.lower() in decls]
    intra, cross, skipped = cli._route_references(org, specs)
    assert {r["from_table"] for r in cross} == {"incident", "problem"}   # both children joined
    assert all(r["to_table"] == "sys_user_group" for r in cross)


def test_route_references_intra_cross_and_skips_unmodelled():
    from semantic_model import cli
    from semantic_model import models as mm

    def tbl(name):
        return mm.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                        columns=[mm.Column(name="id", type="integer", primary_key=True)])
    itsm = mm.SubjectArea(name="itsm", description="d", tables_defined=[tbl("incident"), tbl("problem")])
    sysa = mm.SubjectArea(name="sys", description="d", tables_defined=[tbl("sys_user")])
    org = mm.Datasource(datasource="o", version=1, subject_areas=[itsm, sysa])
    specs = [
        {"from_table": "incident", "from_column": "problem_id", "to_table": "problem"},   # intra (itsm)
        {"from_table": "incident", "from_column": "caller_id", "to_table": "sys_user"},    # cross (itsm→sys)
        {"from_table": "problem", "from_column": "parent", "to_table": "problem"},         # SELF-ref — kept
        {"from_table": "incident", "from_column": "x", "to_table": "nope"},                # target unmodelled → skip+count
    ]
    intra, cross, skipped = cli._route_references(org, specs)
    itsm_edges = {r["from_column"]: r["to_table"] for r in intra.get("itsm", [])}
    assert itsm_edges == {"problem_id": "problem", "parent": "problem"}   # intra + self-ref both kept
    assert "from_subject_area" not in next(r for r in intra["itsm"] if r["from_column"] == "problem_id")
    assert len(cross) == 1
    assert cross[0]["from_subject_area"] == "itsm" and cross[0]["to_subject_area"] == "sys"
    assert cross[0]["to_column"] == "id"
    assert skipped == 1   # the `nope` target wasn't modelled → counted, not silent


def _servicenow_model(root: Path) -> None:
    """`incident` (+ metadata tables) in the itsm area; `sys_user` in a SEPARATE sys area — so the
    dictionary's caller_id→sys_user reference is a CROSS-area edge, as it is in real ServiceNow."""
    (root / "datasources" / "c").mkdir(parents=True)
    for area in ("itsm", "sys"):
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "sn", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/itsm", "subject_areas/sys"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "Redshift"}))
    (root / "subject_areas" / "itsm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "itsm",
        "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                   for t in ("incident", "sys_choice", "sys_dictionary")],
    }))
    (root / "subject_areas" / "sys" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sys",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "sys_user"}],
    }))
    (root / "subject_areas" / "itsm" / "tables" / "incident.yaml").write_text(yaml.safe_dump({
        "name": "incident", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "incidents",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "severity", "type": "integer"},
                    {"name": "short_description", "type": "string"},
                    {"name": "caller_id", "type": "integer"}],
    }))
    (root / "subject_areas" / "sys" / "tables" / "sys_user.yaml").write_text(yaml.safe_dump({
        "name": "sys_user", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "users",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    for meta in ("sys_choice", "sys_dictionary"):
        (root / "subject_areas" / "itsm" / "tables" / f"{meta}.yaml").write_text(yaml.safe_dump({
            "name": meta, "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": meta,
            "columns": [{"name": "id", "type": "integer", "primary_key": True}],
        }))


def test_cmd_enrich_metadata_applies_from_canned_db(tmp_path, monkeypatch):
    _servicenow_model(tmp_path)
    from semantic_model import introspect as INTRO

    def fake_factory(profile, python=None):
        def run(sql):
            s = sql.lower()
            if "sys_choice" in s:
                return SYS_CHOICE
            if "sys_dictionary" in s:
                return SYS_DICTIONARY
            if "matched" in s:                 # the value-overlap verification probe
                return [{"matched": "5"}]
            return []
        return run

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", fake_factory)
    from semantic_model import cli

    ns = cli.build_parser().parse_args(
        ["enrich-metadata", str(tmp_path), "--profile", "servicenow", "--db-type", "redshift"])
    assert ns.func(ns) == 0

    org = loader.load_datasource(tmp_path)
    incident = next(t for sa in org.subject_areas for t in sa.tables_defined if t.name == "incident")
    cols = {c.name: c for c in incident.columns}
    assert cols["severity"].choice_field == {"1": "High", "2": "Medium", "3": "Low"}
    assert cols["severity"].description_source == "metadata"
    # caller_id→sys_user (cross-area, NOT <x>_id name-matchable) — written AND overlap-verified,
    # so it's confirmed + system-approved rather than left unreviewed.
    xrels = org.cross_subject_area_relationships
    edge = next((r for r in xrels if r.from_table == "incident" and r.to_table == "sys_user"), None)
    assert edge is not None and edge.from_column == "caller_id"
    assert edge.from_subject_area == "itsm" and edge.to_subject_area == "sys"
    assert edge.review_state == "approved" and edge.confidence == "confirmed"
    assert edge.signed_off_role == "system"


def test_known_reference_graph_is_config():
    g = M.known_reference_graph("servicenow")
    assert g["caller_id"] == "sys_user" and g["assignment_group"] == "sys_user_group"
    assert M.known_reference_graph(None) == {} and M.known_reference_graph("nope") == {}


def test_enrich_metadata_drops_unverified_preset_reference(tmp_path, monkeypatch):
    # overlap probe returns NO match → the preset's standard caller_id→sys_user does NOT hold in
    # this export, so it must be DROPPED, never blindly trusted.
    _servicenow_model(tmp_path)
    from semantic_model import introspect as INTRO

    def fake_factory(profile, python=None):
        def run(sql):
            s = sql.lower()
            if "sys_choice" in s:
                return SYS_CHOICE
            if "sys_dictionary" in s:
                return SYS_DICTIONARY
            if "matched" in s:
                return [{"matched": "0"}]          # NO overlap
            return []
        return run

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", fake_factory)
    from semantic_model import cli
    ns = cli.build_parser().parse_args(
        ["enrich-metadata", str(tmp_path), "--profile", "servicenow", "--db-type", "redshift"])
    assert ns.func(ns) == 0
    org = loader.load_datasource(tmp_path)
    assert not [r for r in org.cross_subject_area_relationships if r.to_table == "sys_user"]


def test_verified_references_unverified_when_table_too_big():
    # the #7 guard: a FROM table above the row threshold must NOT be overlap-scanned — the ref is
    # kept UNVERIFIED (inferred/unreviewed), not dropped, not probed.
    from semantic_model import cli
    from semantic_model import dialects as D
    from semantic_model import introspect as INTRO
    from semantic_model import models as mm
    big = mm.PerformanceHints(estimated_row_count=INTRO._OVERLAP_PROBE_MAX_ROWS + 1)

    def tbl(name, cols, perf=None):
        return mm.Table(name=name, schema="public", storage_connection="c", grain=["sys_id"],
                        performance_hints=perf,
                        columns=[mm.Column(name="sys_id", type="integer", primary_key=True)]
                                + [mm.Column(name=c, type="integer") for c in cols])
    itsm = mm.SubjectArea(name="itsm", description="d", tables_defined=[tbl("incident", ["caller_id"], big)])
    sysa = mm.SubjectArea(name="sys", description="d", tables_defined=[tbl("sys_user", [])])
    org = mm.Datasource(datasource="o", version=1, subject_areas=[itsm, sysa])
    probed = {"n": 0}

    def runner(sql):
        if "matched" in sql:
            probed["n"] += 1
            return [{"matched": "5"}]
        return []
    intra, cross, skipped, unverified = cli._build_verified_references(
        org, runner, D.get_dialect("redshift"), {"caller_id": "sys_user"})
    assert probed["n"] == 0          # too big → not scanned
    assert unverified == 1 and len(cross) == 1
    assert cross[0]["review_state"] == "unreviewed" and cross[0]["confidence"] == "inferred"
