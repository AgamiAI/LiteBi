"""Coverage gate: universal _on/_count self-evident + preset-declared system columns."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate  # noqa: E402
from semantic_model import models as m  # noqa: E402


def test_universal_timestamp_and_count_suffixes_self_evident():
    # _on (Rails/Django/ServiceNow) and _count are universally self-evident — no preset needed
    assert curate._SELF_EVIDENT_NAME_RE.match("created_on")
    assert curate._SELF_EVIDENT_NAME_RE.match("closed_on")
    assert curate._SELF_EVIDENT_NAME_RE.match("login_count")
    # a genuinely meaningful column is still NOT self-evident
    assert not curate._SELF_EVIDENT_NAME_RE.match("approval_status_reason")


def _incident_cols():
    return [m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="sys_created_on", type="timestamp"),   # _on → universal
            m.Column(name="sys_mod_count", type="integer"),      # _count → universal
            m.Column(name="sys_domain", type="string"),          # sys_ → preset only
            m.Column(name="biz_widget", type="string")]          # genuinely meaningful → flagged


def _org(tables):
    return m.Organization(datasource="o",
                          subject_areas=[m.SubjectArea(name="s", tables_defined=tables)])


def test_preset_system_columns_self_evident_only_when_detected():
    inc = m.Table(name="incident", schema="s", storage_connection="c", grain=["id"],
                  description="i", columns=_incident_cols())
    sysdict = m.Table(name="sys_dictionary", schema="s", storage_connection="c", grain=["id"],
                      description="d", columns=[
                          m.Column(name="id", type="integer", primary_key=True),
                          m.Column(name="element", type="string", description="field name")])

    # WITH the ServiceNow signature (sys_dictionary present): sys_domain is self-evident,
    # only biz_widget remains a meaningful blank.
    cov = curate.column_coverage(_org([inc, sysdict]))
    row = next(t for t in cov["tables"] if t["table"] == "incident")
    assert row["blank_meaningful_columns"] == ["biz_widget"]

    # WITHOUT the signature (no preset): sys_domain is no longer covered → flagged too.
    cov2 = curate.column_coverage(_org([inc]))
    row2 = next(t for t in cov2["tables"] if t["table"] == "incident")
    assert set(row2["blank_meaningful_columns"]) == {"sys_domain", "biz_widget"}
