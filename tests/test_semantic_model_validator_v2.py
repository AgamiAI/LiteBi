"""Unit tests for semantic_model/validator.py — one happy + one failure per rule.

Each test builds a minimal Datasource that isolates a single rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import validator as v  # noqa: E402


def _conn(name="c"):
    return m.StorageConnection(name=name, storage_type="PostgreSQL")


def _org(area, conn="c", **kw):
    return m.Datasource(datasource="O", storage_connections=[_conn(conn)],
                          subject_areas=[area], **kw)


def _codes(res):
    return {f.code for f in res.findings}


def _col(name, type="integer", **kw):
    return m.Column(name=name, type=type, **kw)


# --- sizing ---


def test_sizing_ok():
    tables = [m.Table(name=f"t{i}", schema="s", storage_connection="c", grain=["id"],
                      description="d", columns=[_col("id")]) for i in range(3)]
    refs = [m.TableRef(storage_connection="c", schema="s", table=t.name) for t in tables]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=tables)))
    assert "subject_area_too_large" not in _codes(res)


def test_sizing_error_over_30():
    tables = [m.Table(name=f"t{i}", schema="s", storage_connection="c", grain=["id"],
                      description="d", columns=[_col("id")]) for i in range(31)]
    refs = [m.TableRef(storage_connection="c", schema="s", table=t.name) for t in tables]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=tables)))
    assert "subject_area_too_large" in _codes(res)
    assert not res.ok


def test_sizing_warn_at_25():
    tables = [m.Table(name=f"t{i}", schema="s", storage_connection="c", grain=["id"],
                      description="d", columns=[_col("id")]) for i in range(26)]
    refs = [m.TableRef(storage_connection="c", schema="s", table=t.name) for t in tables]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=tables)))
    assert "subject_area_large" in _codes(res) and res.ok


# --- orphan table ref ---


def test_orphan_table_ref():
    t = m.Table(name="real", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id")])
    refs = [m.TableRef(storage_connection="c", schema="s", table="ghost")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "orphan_table_ref" in _codes(res)


def test_table_ref_resolves_org_wide():
    # TableRef in area B points at a table defined in area A (multi-membership).
    t = m.Table(name="shared", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id")])
    a = m.SubjectArea(name="A", tables=[m.TableRef(storage_connection="c", schema="s", table="shared")],
                      tables_defined=[t])
    b = m.SubjectArea(name="B", tables=[m.TableRef(storage_connection="c", schema="s", table="shared")],
                      tables_defined=[])
    org = m.Datasource(datasource="O", storage_connections=[_conn()], subject_areas=[a, b])
    res = v.validate(org)
    assert "orphan_table_ref" not in _codes(res)


# --- expose_column_groups ---


def test_expose_unknown_group():
    t = m.Table(name="w", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col(f"c{i}") for i in range(30)],
                column_groups={"g": [f"c{i}" for i in range(30)]})
    refs = [m.TableRef(storage_connection="c", schema="s", table="w", expose_column_groups=["nope"])]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "unknown_column_group" in _codes(res)


# --- deep-table column_groups ---


def test_deep_table_requires_groups():
    t = m.Table(name="w", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col(f"c{i}") for i in range(30)])
    refs = [m.TableRef(storage_connection="c", schema="s", table="w")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "deep_table_no_column_groups" in _codes(res)


def test_deep_table_orphan_columns():
    t = m.Table(name="w", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col(f"c{i}") for i in range(30)],
                column_groups={"g": [f"c{i}" for i in range(20)]})
    refs = [m.TableRef(storage_connection="c", schema="s", table="w")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "column_group_orphans" in _codes(res)


def test_column_group_missing_column():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id")], column_groups={"g": ["nonexistent"]})
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "column_group_missing_column" in _codes(res)


# --- default_filters ---


def test_default_filter_unknown_column():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id")], default_filters=["t.ghost IS NULL"])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "default_filter_unknown_column" in _codes(res)


def test_default_filter_known_column_ok():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id"), _col("deleted_at", "timestamp")],
                default_filters=["t.deleted_at IS NULL"])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "default_filter_unknown_column" not in _codes(res)


def test_default_filter_alias_placeholder_and_param_ok():
    # the canonical engine-emitted form uses the {alias} placeholder + :param bind
    # markers — neither is a column and must NOT trip the unknown-column check
    t = m.Table(name="orders", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id"), _col("deleted_at", "timestamp"), _col("tenant_id")],
                default_filters=["{alias}.deleted_at IS NULL", "{alias}.tenant_id = :tenant_id"])
    refs = [m.TableRef(storage_connection="c", schema="s", table="orders")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "default_filter_unknown_column" not in _codes(res)


# --- value_transform ---


def test_value_transform_unparseable():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("x", "string", value_transform="this is (((not sql")])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "value_transform_unparseable" in _codes(res)


def test_value_transform_ok():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("x", "string", value_transform="regexp_replace(x, '[\\[\\]]', '', 'g')")])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "value_transform_unparseable" not in _codes(res)


# --- choice_field type ---


def test_choice_field_type_mismatch():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("n", "integer", choice_field={"abc": "not an int"})])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "choice_field_type_mismatch" in _codes(res)


def test_choice_field_ok():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("b", "boolean", choice_field={"true": "yes", "false": "no"})])
    refs = [m.TableRef(storage_connection="c", schema="s", table="t")]
    res = v.validate(_org(m.SubjectArea(name="a", tables=refs, tables_defined=[t])))
    assert "choice_field_type_mismatch" not in _codes(res)


# --- FK type compatibility (Gap 3) ---


def _two_table_area(ftype, ttype, confidence="inferred"):
    a = m.Table(name="a", schema="s", storage_connection="c", grain=["x"], description="d",
                columns=[_col("x", ftype)])
    b = m.Table(name="b", schema="s", storage_connection="c", grain=["y"], description="d",
                columns=[_col("y", ttype)])
    rel = m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                         relationship="many_to_one", confidence=confidence)
    refs = [m.TableRef(storage_connection="c", schema="s", table="a"),
            m.TableRef(storage_connection="c", schema="s", table="b")]
    return m.SubjectArea(name="ar", tables=refs, tables_defined=[a, b], relationships=[rel]), rel


def test_fk_type_mismatch_warns_and_suggests_cast():
    sa, rel = _two_table_area("integer", "string")
    res = v.validate(_org(sa))
    assert "fk_type_mismatch" in _codes(res)
    finding = next(f for f in res.findings if f.code == "fk_type_mismatch")
    assert finding.suggestion and "CAST" in finding.suggestion
    assert v.recommended_confidence_cap(rel, sa) == "proposed"


def test_fk_type_mismatch_confirmed_is_error():
    sa, _ = _two_table_area("integer", "string", confidence="confirmed")
    res = v.validate(_org(sa))
    assert "fk_type_mismatch_confirmed" in _codes(res) and not res.ok


def test_fk_type_compatible_ok():
    sa, rel = _two_table_area("integer", "integer")
    res = v.validate(_org(sa))
    assert "fk_type_mismatch" not in _codes(res)
    assert v.recommended_confidence_cap(rel, sa) is None


def test_fk_on_expression_skips_type_check():
    a = m.Table(name="a", schema="s", storage_connection="c", grain=["x"], description="d",
                columns=[_col("x", "integer")])
    b = m.Table(name="b", schema="s", storage_connection="c", grain=["y"], description="d",
                columns=[_col("y", "string")])
    rel = m.Relationship(from_table="a", to_table="b", on="CAST(a.x AS STRING) = b.y",
                         relationship="many_to_one")
    refs = [m.TableRef(storage_connection="c", schema="s", table="a"),
            m.TableRef(storage_connection="c", schema="s", table="b")]
    sa = m.SubjectArea(name="ar", tables=refs, tables_defined=[a, b], relationships=[rel])
    res = v.validate(_org(sa))
    assert "fk_type_mismatch" not in _codes(res)


# --- trust-block parity ---


def test_trust_block_incomplete_when_approved():
    a = m.Table(name="a", schema="s", storage_connection="c", grain=["x"], description="d",
                columns=[_col("x")])
    b = m.Table(name="b", schema="s", storage_connection="c", grain=["y"], description="d",
                columns=[_col("y")])
    rel = m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                         relationship="many_to_one", review_state="approved")
    refs = [m.TableRef(storage_connection="c", schema="s", table="a"),
            m.TableRef(storage_connection="c", schema="s", table="b")]
    res = v.validate(_org(m.SubjectArea(name="ar", tables=refs, tables_defined=[a, b],
                                        relationships=[rel])))
    assert "trust_block_incomplete" in _codes(res)


# --- cross-area entity collision ---


def test_cross_area_entity_collision_warns():
    a = m.Table(name="accounts", schema="crm", storage_connection="c", grain=["id"],
                description="d", columns=[_col("id")])
    rr = m.Table(name="rev", schema="fin", storage_connection="c", grain=["id"],
                 description="d", columns=[_col("id")])
    area_a = m.SubjectArea(name="crm",
                           tables=[m.TableRef(storage_connection="c", schema="crm", table="accounts")],
                           tables_defined=[a],
                           entities=[m.Entity(name="Account",
                                              maps_to=[m.EntityMapping(table="accounts", column="id", primary=True)])])
    area_b = m.SubjectArea(name="finance",
                           tables=[m.TableRef(storage_connection="c", schema="fin", table="rev")],
                           tables_defined=[rr],
                           entities=[m.Entity(name="Account",
                                              maps_to=[m.EntityMapping(table="rev", column="id", primary=True)])])
    org = m.Datasource(datasource="O", storage_connections=[_conn()],
                         subject_areas=[area_a, area_b])
    res = v.validate(org)
    assert "cross_area_entity_collision" in _codes(res)
    # unifying it at org level clears the warning
    org.cross_subject_area_entities = [m.Entity(name="Account")]
    assert "cross_area_entity_collision" not in _codes(v.validate(org))


# --- executable parity on cross-area edges ---


def test_executable_mismatch_on_cross_edge():
    a = m.Table(name="a", schema="s", storage_connection="c1", grain=["x"], description="d",
                columns=[_col("x")])
    b = m.Table(name="b", schema="s", storage_connection="c2", grain=["y"], description="d",
                columns=[_col("y")])
    area_a = m.SubjectArea(name="A", tables=[m.TableRef(storage_connection="c1", schema="s", table="a")],
                           tables_defined=[a])
    area_b = m.SubjectArea(name="B", tables=[m.TableRef(storage_connection="c2", schema="s", table="b")],
                           tables_defined=[b])
    edge = m.CrossSubjectAreaRelationship(from_table="a", to_table="b", from_column="x", to_column="y",
                                          relationship="many_to_one", executable="same_engine",
                                          from_subject_area="A", to_subject_area="B")
    org = m.Datasource(datasource="O",
                         storage_connections=[_conn("c1"), _conn("c2")],
                         subject_areas=[area_a, area_b],
                         cross_subject_area_relationships=[edge])
    res = v.validate(org)
    assert "executable_mismatch" in _codes(res)
