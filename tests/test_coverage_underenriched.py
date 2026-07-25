"""The coverage gate now also catches UNDER-enrichment: a table the column pass
touched but which still has a wall of blank columns with non-self-evident names
(skipped meaningful columns like `bptype`), not just fully-blank tables."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _t(name, cols):
    return m.Table(name=name, schema="public", storage_connection="c", grain=["id"], columns=cols)


def _col(name, desc=""):
    return m.Column(name=name, type="string", description=desc)


def _org(tables):
    sa = m.SubjectArea(name="a", description="d", tables_defined=tables)
    return m.Organization(datasource="o", version=1, subject_areas=[sa])


def test_self_evident_blanks_are_fine():
    # id / *_id / *_date / created_* / *_by left blank is allowed; one described col → enriched
    t = _t("vehicles", [_col("id"), _col("dealer_id"), _col("created_date"),
                        _col("modified_by"), _col("make", "manufacturer of the vehicle")])
    cov = C.column_coverage(_org([t]))
    assert cov["ok"] is True
    assert cov["under_enriched_tables"] == []


def test_skipped_meaningful_columns_flag_under_enriched():
    # many non-self-evident blanks (bptype, vehicle_phase, …) → under-enriched, ok False
    t = _t("vehicles", [_col("id"), _col("make", "manufacturer"),
                        _col("bptype"), _col("dispensingunittype"), _col("vehicle_phase"),
                        _col("payment_status"), _col("supported_partners")])
    cov = C.column_coverage(_org([t]))
    assert cov["ok"] is False
    assert "vehicles" in cov["under_enriched_tables"]
    row = next(r for r in cov["tables"] if r["table"] == "vehicles")
    assert row["meaningful_blank"] == 5
    assert "bptype" in row["blank_meaningful_columns"]


def test_a_couple_meaningful_blanks_tolerated():
    t = _t("vehicles", [_col("id"), _col("make", "mfr"), _col("color", "paint color"),
                        _col("bptype"), _col("vehicle_phase")])   # only 2 meaningful blanks
    cov = C.column_coverage(_org([t]))
    assert cov["ok"] is True   # within tolerance


def test_ai_unknown_counts_as_handled_not_blank():
    t = _t("vehicles", [_col("id"), _col("make", "mfr"),
                        m.Column(name="bptype", type="string", description="", description_source="ai_unknown"),
                        m.Column(name="xfield", type="string", description="", description_source="ai_unknown"),
                        m.Column(name="yfield", type="string", description="", description_source="ai_unknown")])
    cov = C.column_coverage(_org([t]))
    assert cov["ok"] is True   # ai_unknown is a handled state, not a skipped blank
