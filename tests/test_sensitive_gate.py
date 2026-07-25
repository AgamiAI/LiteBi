"""The agami-connect Phase 4 curate gate counts flagged PII via `sm sensitive` so
it can open the explorer when there's PII OR sign-offs pending."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate as C  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _org(cols):
    t = m.Table(name="users", schema="public", storage_connection="c", grain=["id"], columns=cols)
    sa = m.SubjectArea(name="area", description="d", tables_defined=[t])
    return m.Datasource(datasource="o", version=1, subject_areas=[sa])


def test_counts_flagged_pii():
    org = _org([m.Column(name="id", type="integer"),
                m.Column(name="email", type="string", sensitive=True),
                m.Column(name="phone", type="string", sensitive=True)])
    out = C.sensitive_columns(org)
    assert out["count"] == 2
    assert {c["column"] for c in out["columns"]} == {"email", "phone"}


def test_excluded_sensitive_not_counted():
    org = _org([m.Column(name="ssn", type="string", sensitive=True, review_state="rejected"),
                m.Column(name="email", type="string", sensitive=True)])
    assert C.sensitive_columns(org)["count"] == 1   # rejected one drops out


def test_clean_db_zero():
    org = _org([m.Column(name="id", type="integer"), m.Column(name="amount", type="decimal")])
    assert C.sensitive_columns(org)["count"] == 0


def test_sensitive_columns_under_excluded_table_not_counted():
    # a whole table excluded → its sensitive columns are off the runtime, so not counted
    t = m.Table(name="pii_dump", schema="public", storage_connection="c", grain=["id"],
                review_state="rejected",
                columns=[m.Column(name="email", type="string", sensitive=True),
                         m.Column(name="ssn", type="string", sensitive=True)])
    sa = m.SubjectArea(name="area", description="d", tables_defined=[t])
    org = m.Datasource(datasource="o", version=1, subject_areas=[sa])
    assert C.sensitive_columns(org)["count"] == 0


def test_suspected_tier_surfaces_missed_pii():
    # first_name in a non-PII-named table is NOT auto-flagged (table-gated weak rule), but the
    # suspected tier surfaces it so a reviewer can confirm. Already-sensitive cols don't repeat.
    org = _org([m.Column(name="id", type="integer"),
                m.Column(name="email", type="string", sensitive=True),   # flagged
                m.Column(name="first_name", type="string"),              # suspected, not flagged
                m.Column(name="gender", type="string"),                  # suspected (extra tokens)
                m.Column(name="total", type="decimal")])                 # not PII
    susp = C.suspected_sensitive_columns(org)
    names = {c["column"] for c in susp["columns"]}
    assert names == {"first_name", "gender"}
    assert "email" not in names   # already sensitive → not re-surfaced
    assert "total" not in names


def test_suspected_excludes_rejected():
    org = _org([m.Column(name="maiden_name", type="string", review_state="rejected"),
                m.Column(name="nationality", type="string")])
    assert {c["column"] for c in C.suspected_sensitive_columns(org)["columns"]} == {"nationality"}
