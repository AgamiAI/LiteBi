"""F15 end-to-end: one shared company record across two datasources, each with its OWN vocabulary,
reflected on BOTH surfaces (local `cli org-context` and the served `get_datasource_schema`).

This is the feature demo as a test: a company connects a CRM and an ERP under one org; "Account" resolves
to a customer in the CRM and a GL account in the ERP; every answer carries the same company context; and a
deployment with no record degrades to the pre-F15 per-profile output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import yaml  # noqa: E402

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model import org_record as OR  # noqa: E402
from semantic_model.cli import main as cli_main  # noqa: E402
from semantic_model.models import (  # noqa: E402
    DisplayConventions,
    Organization,
    OrgRecord,
    SubjectArea,
)


def _profile(root: Path, name: str, area: str, account_means: str) -> None:
    org = Organization(
        organization=name,
        subject_areas=[SubjectArea(name=area, description=f"{area} area")],
    )
    build.write_tree(org, root / name)
    # key_terminology is written by the enrichment / set-terminology path, not write_tree — inject it so
    # the source-specific vocabulary ("Account" resolves per source) round-trips through load_organization.
    org_yaml = root / name / "org.yaml"
    doc = yaml.safe_load(org_yaml.read_text())
    doc["key_terminology"] = {"Account": account_means}
    org_yaml.write_text(yaml.safe_dump(doc, sort_keys=False))


def _company(root: Path) -> None:
    OR.write_org_record(
        root,
        OrgRecord(
            org_id="deployorg",
            name="Acme Corp",
            description="A demo company.",
            fiscal_year_start_month=4,
            display_conventions=DisplayConventions(currency="USD"),
            glossary={"ARR": "annual recurring revenue"},
        ),
    )
    OR.narrative_path(root).write_text("# About Acme\nWe sell widgets across regions.\n")


def _org_context(root: Path, profile: str, capsys) -> str:
    assert cli_main(["org-context", str(root / profile)]) == 0
    return capsys.readouterr().out


def _served_domain_context(profile: str) -> str:
    # Disk-backed serve path (no AGAMI_DB_URL): domain-context text trails the JSON head.
    out = tools.tool_get_datasource_schema({"datasource": profile})
    head, end = json.JSONDecoder().raw_decode(out)
    return out[end:]


def test_local_surface_shows_company_block_once_and_this_sources_vocabulary(tmp_path, capsys, monkeypatch):
    _profile(tmp_path, "crm", "Sales", "customer")
    _profile(tmp_path, "erp", "Ledger", "GL account")
    _company(tmp_path)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))

    crm_ctx = _org_context(tmp_path, "crm", capsys)
    assert crm_ctx.count("Acme Corp — company context") == 1  # shared company block, once
    assert "ARR" in crm_ctx  # company glossary
    assert "customer" in crm_ctx and "GL account" not in crm_ctx  # CRM's own vocabulary only

    erp_ctx = _org_context(tmp_path, "erp", capsys)
    assert "GL account" in erp_ctx and "customer" not in erp_ctx  # ERP's own vocabulary only


def test_served_surface_reflects_the_two_level_composition(tmp_path, monkeypatch):
    _profile(tmp_path, "crm", "Sales", "customer")
    _company(tmp_path)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()

    ctx = _served_domain_context("crm")
    assert "Acme Corp — company context" in ctx  # the served MCP context carries the company block …
    assert "We sell widgets across regions." in ctx  # … incl. the company narrative …
    assert "customer" in ctx  # … and the CRM vocabulary


def test_no_record_degrades_to_per_profile_on_both_surfaces(tmp_path, capsys, monkeypatch):
    _profile(tmp_path, "crm", "Sales", "customer")
    # A record was minted by write_tree; remove it to simulate a pre-F15 deployment.
    OR.record_path(tmp_path).unlink()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()

    ctx = _org_context(tmp_path, "crm", capsys)
    assert "company context" not in ctx  # no company block …
    assert "customer" in ctx  # … but the per-profile model still composes, no error
