"""Unit tests for semantic_model/models.py — happy + failure paths per model.

Guarded with importorskip: the v2 package needs pydantic (opt-in dependency,
needs pydantic (an optional dependency), so these skip cleanly on a
default install rather than erroring at import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from pydantic import ValidationError  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _col(name, type="integer", **kw):
    return m.Column(name=name, type=type, **kw)


# --- Column / ForeignKey ---


def test_column_happy():
    c = _col("x", "string", description="d", sensitive=True, caveats=["a quirk"])
    assert c.sensitive and c.caveats == ["a quirk"]


def test_column_empty_caveat_rejected():
    with pytest.raises(ValidationError):
        _col("x", caveats=[" "])


def test_foreign_key_simple_happy():
    fk = m.ForeignKey(table="t", column="c")
    assert not fk.is_polymorphic


def test_foreign_key_polymorphic_happy():
    fk = m.ForeignKey(discriminator_column="kind", target_tables=["a", "b"])
    assert fk.is_polymorphic


def test_foreign_key_simple_missing_column_rejected():
    with pytest.raises(ValidationError):
        m.ForeignKey(table="t")


def test_foreign_key_polymorphic_missing_targets_rejected():
    with pytest.raises(ValidationError):
        m.ForeignKey(discriminator_column="kind")


# --- Table ---


def test_table_happy_and_deep_flag():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"],
                description="one line", columns=[_col("id")])
    assert not t.is_deep
    deep = m.Table(name="w", schema="s", storage_connection="c", grain=["id"],
                   description="d", columns=[_col(f"c{i}") for i in range(30)],
                   column_groups={"g": [f"c{i}" for i in range(30)]})
    assert deep.is_deep


def test_table_sql_source_requires_sql():
    with pytest.raises(ValidationError):
        m.Table(name="v", source_type="sql", description="d")


def test_table_table_source_rejects_sql():
    with pytest.raises(ValidationError):
        m.Table(name="v", source_type="table", sql="SELECT 1", description="d")


def test_table_unknown_field_rejected():
    with pytest.raises(ValidationError):
        m.Table(name="v", description="d", bogus=1)


# --- Entity ---


def test_entity_happy_one_primary():
    e = m.Entity(name="E", maps_to=[m.EntityMapping(table="t", column="c", primary=True),
                                    m.EntityMapping(table="u", column="d")])
    assert e.name == "E"


def test_entity_two_primaries_rejected():
    with pytest.raises(ValidationError):
        m.Entity(name="E", maps_to=[m.EntityMapping(table="t", column="c", primary=True),
                                    m.EntityMapping(table="u", column="d", primary=True)])


# --- Metric ---


def test_metric_happy():
    mm = m.Metric(name="m", calculation="count of distinct x", bindings={"PostgreSQL": "COUNT(DISTINCT x)"})
    assert mm.confidence == "proposed"


def test_metric_empty_calculation_rejected():
    with pytest.raises(ValidationError):
        m.Metric(name="m", calculation="  ")


# --- Relationship ---


def test_relationship_simple_happy():
    r = m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                       relationship="many_to_one")
    assert r.relationship == "many_to_one"


def test_relationship_on_happy():
    r = m.Relationship(from_table="a", to_table="b", on="CAST(a.x AS STRING)=b.y",
                       relationship="one_to_one")
    assert r.on


def test_relationship_missing_cardinality_rejected():
    with pytest.raises(ValidationError):
        m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y")


def test_relationship_both_forms_rejected():
    with pytest.raises(ValidationError):
        m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                       on="a.x=b.y", relationship="one_to_one")


def test_relationship_neither_form_rejected():
    with pytest.raises(ValidationError):
        m.Relationship(from_table="a", to_table="b", relationship="one_to_one")


def test_relationship_partial_simple_rejected():
    with pytest.raises(ValidationError):
        m.Relationship(from_table="a", to_table="b", from_column="x", relationship="one_to_one")


def test_cross_relationship_requires_areas():
    with pytest.raises(ValidationError):
        m.CrossSubjectAreaRelationship(from_table="a", to_table="b", from_column="x",
                                       to_column="y", relationship="many_to_one")
    ok = m.CrossSubjectAreaRelationship(from_table="a", to_table="b", from_column="x",
                                        to_column="y", relationship="many_to_one",
                                        from_subject_area="A", to_subject_area="B")
    assert ok.from_subject_area == "A"


# --- Datasource ---


def test_org_happy_and_accessors():
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[_col("id")])
    org = m.Datasource(datasource="O",
                         storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                         subject_areas=[m.SubjectArea(name="sa", tables_defined=[t])])
    assert org.subject_area("sa").defined_table("t") is t
    assert org.storage_connection("c").storage_type == "PostgreSQL"


def test_org_bad_fiscal_month_rejected():
    with pytest.raises(ValidationError):
        m.Datasource(datasource="O", fiscal_year_start_month=13)
