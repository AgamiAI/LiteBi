"""Tier 1: structured enum decode (choice_field).

Introspection seeds a choice_field SKELETON {value: ""} on low-cardinality coded
columns (catalog mode too, not just probe) so the LLM can fill the labels;
`unlabeled_choice_fields` reports the ones still blank.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import validator as V  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402


# the sample query (SELECT ... LIMIT) → low-cardinality severity, varied free text
def _incident_sample(_sql):
    return [{"id": i, "severity": (i % 3) + 1,
             "short_description": f"issue number {i} with a long unique description",
             "caller_id": 1000 + i} for i in range(30)]


# Catalog runner: an incident-like table with a coded `severity` (1/2/3), a free-text
# `short_description`, a PK `id`, and an FK `caller_id`.
_catalog_runner = make_catalog_runner(
    tables=["incident"],
    columns={"incident": [
        col("id", "integer", nullable=False),
        col("severity", "integer"),
        col("short_description", "varchar"),
        col("caller_id", "integer"),
    ]},
    estimate=None,  # original runner had no reltuples branch — keep row-count unknown
    sample=_incident_sample,
)


def test_catalog_coded_column_gets_choice_skeleton(tmp_path):
    org, _ = I.introspect("sn", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    t = org.subject_areas[0].defined_table("incident")
    sev = t.get_column("severity")
    # severity (low-cardinality integer) → skeleton with the 3 codes, blank labels
    assert sev.choice_field == {"1": "", "2": "", "3": ""}
    # free-text, PK, and FK-named columns are NOT given a choice_field
    assert t.get_column("short_description").choice_field is None
    assert t.get_column("id").choice_field is None
    assert t.get_column("caller_id").choice_field is None


def test_unlabeled_choice_fields_reports_then_clears(tmp_path):
    org, _ = I.introspect("sn", "postgres", runner=_catalog_runner, artifacts_dir=tmp_path)
    root = tmp_path / "sn"
    rep = C.unlabeled_choice_fields(__import__("semantic_model.loader", fromlist=["load_datasource"]).load_datasource(root, include_rejected=True))
    assert rep["count"] == 1 and rep["ok"] is False
    assert rep["unlabeled"][0]["column"] == "severity"

    # fill the labels via the same structured edit op the enrichment uses
    area = org.subject_areas[0].name
    res = C.apply(root, [{"op": "edit", "kind": "table", "area": area, "name": "incident",
                          "column": "severity", "field": "choice_field",
                          "value": {"1": "High", "2": "Medium", "3": "Low"}}])
    assert not res.errors, res.errors
    org2 = __import__("semantic_model.loader", fromlist=["load_datasource"]).load_datasource(root)
    assert org2.subject_areas[0].defined_table("incident").get_column("severity").choice_field == {"1": "High", "2": "Medium", "3": "Low"}
    assert C.unlabeled_choice_fields(org2)["ok"] is True
