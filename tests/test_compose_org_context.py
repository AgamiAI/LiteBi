"""F15 / ACE-069: two-level org-context composition (shared company block + per-datasource ontology).

Pins the headline behaviours: per-source vocabulary isolation (single-source), the company block rendered
EXACTLY ONCE for a federated (multi-source) question, and byte-identical graceful degradation to the
pre-F15 `compose_context` when there is no record (what keeps every old deployment working).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import org_draft as OD  # noqa: E402
from semantic_model.models import (  # noqa: E402
    DisplayConventions,
    Organization,
    OrgRecord,
    SubjectArea,
)


def _org(name: str, area: str, account_means: str) -> Organization:
    return Organization(
        organization=name,
        subject_areas=[SubjectArea(name=area, description=f"{area} area")],
        key_terminology={"Account": account_means},
    )


def _record() -> OrgRecord:
    return OrgRecord(
        org_id="o1",
        name="Acme Corp",
        description="A demo company.",
        fiscal_year_start_month=4,
        display_conventions=DisplayConventions(currency="USD", rounding=2, week_start="monday"),
        glossary={"ARR": "annual recurring revenue"},
    )


CRM = _org("crm", "Sales", "customer")
ERP = _org("erp", "Ledger", "GL account")


def test_single_source_carries_company_block_and_that_sources_vocabulary_only():
    a = OD.compose_org_context(_record(), [CRM])
    b = OD.compose_org_context(_record(), [ERP])
    # both carry the shared company block …
    assert "Acme Corp — company context" in a and "Acme Corp — company context" in b
    assert "ARR" in a and "ARR" in b  # …and the company glossary
    # … but each carries ONLY its own source vocabulary (per-source isolation, both directions).
    assert "customer" in a and "GL account" not in a
    assert "GL account" in b and "customer" not in b


def test_federated_renders_company_block_once_and_both_vocabularies():
    fed = OD.compose_org_context(_record(), [CRM, ERP])
    assert fed.count("Acme Corp — company context") == 1  # company context exactly once
    assert "crm — datasource context" in fed and "erp — datasource context" in fed
    assert "customer" in fed and "GL account" in fed  # both vocabularies present


def test_no_record_degrades_byte_identically_to_pre_f15_output():
    # The degradation contract: without a record, output equals today's per-profile compose_context.
    got = OD.compose_org_context(None, [CRM], source_narratives=["CRM-specific notes."])
    assert got == OD.compose_context("CRM-specific notes.", CRM)


def test_company_conventions_and_narrative_render_in_the_company_block():
    got = OD.compose_org_context(
        _record(), [CRM], company_narrative="We sell widgets.", source_narratives=["src note"]
    )
    assert "We sell widgets." in got  # company narrative
    assert "Fiscal year starts in month 4." in got  # structured conventions
    assert "Currency: USD." in got
    assert "Rounding: 2 decimal places." in got
    assert "Week starts on monday." in got
    assert "src note" in got  # source-specific narrative still shown, under the datasource heading
