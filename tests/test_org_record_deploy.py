"""F15 / ACE-068: derive the org record into the Postgres `organization` table.

The table is the one company-level row (keyed on org_id alone) and is written with an FK-SAFE UPSERT,
not the clear-then-insert the other serving writers use — because in the hosted stack org_membership /
license FK-reference it. These tests pin: the migration + columns; lossless round-trip; upsert-replaces;
the None-degradation contract; the FK-safety crux (a redeploy must NOT delete an FK-referenced row); the
hosted coexistence the whole shared-table decision rests on; and the end-to-end "one org row + two
datasource_model rows on one org_id" shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import model_deploy  # noqa: E402
import model_store as MS  # noqa: E402
import tools  # noqa: E402
from semantic_model import org_record as OR  # noqa: E402
from semantic_model.models import DisplayConventions, OrgRecord  # noqa: E402
from store import Store  # noqa: E402


def _store() -> Store:
    s = Store.connect("sqlite://")
    s.run_migrations()
    return s


def _full_record(org_id: str = "org1") -> OrgRecord:
    return OrgRecord(
        org_id=org_id,
        name="Acme",
        description="a demo company",
        fiscal_year_start_month=4,
        display_conventions=DisplayConventions(currency="USD", rounding=2, week_start="monday"),
        glossary={"ARR": "annual recurring revenue"},
    )


def test_write_then_load_round_trips_losslessly():
    s = _store()
    rec = _full_record()
    MS.write_organization_record(s, rec, org_id="org1")
    assert (
        MS.load_organization_record(s, "org1") == rec
    )  # incl. display_conventions + glossary in doc
    s.close()


def test_second_write_replaces_not_duplicates():
    s = _store()
    MS.write_organization_record(s, _full_record(), org_id="org1")
    changed = OrgRecord(org_id="org1", name="Acme", description="new desc")
    MS.write_organization_record(s, changed, org_id="org1")
    n = s.query("SELECT COUNT(*) AS c FROM organization WHERE org_id = ?", ("org1",))[0]["c"]
    assert n == 1  # a redeploy replaces the row, never appends
    assert MS.load_organization_record(s, "org1").description == "new desc"
    s.close()


def test_load_returns_none_when_absent():
    s = _store()
    assert MS.load_organization_record(s, "nope") is None  # the ACE-069 degradation contract
    s.close()


def test_upsert_is_fk_safe_against_a_referencing_membership():
    # THE CRUX. Mirror agami-hosted: a membership row FK-references organization(org_id). A DELETE-based
    # write would raise FOREIGN KEY constraint failed on redeploy; the UPSERT must not.
    s = _store()
    MS.write_organization_record(s, _full_record(), org_id="org1")
    s.execute(
        "CREATE TABLE org_membership (principal_id TEXT NOT NULL, "
        "org_id TEXT NOT NULL REFERENCES organization(org_id), PRIMARY KEY (principal_id, org_id))"
    )
    s.execute("INSERT INTO org_membership (principal_id, org_id) VALUES (?, ?)", ("bob", "org1"))
    s.commit()

    MS.write_organization_record(s, _full_record(), org_id="org1")  # redeploy — must not FK-crash
    assert (
        s.query("SELECT COUNT(*) AS c FROM org_membership WHERE org_id = ?", ("org1",))[0]["c"] == 1
    )
    s.close()


def test_coexists_with_a_hosted_shape_row():
    # Hosted onboards a tenant first: INSERT (org_id, org_name, created_at) — no `doc` (rides the DEFAULT).
    # Then core deploys: the upsert fills content but PRESERVES hosted's org_name + created_at.
    s = _store()
    s.execute(
        "INSERT INTO organization (org_id, org_name, created_at) VALUES (?, ?, ?)",
        ("org2", "Hosted Co", "2026-07-23T00:00:00Z"),
    )
    s.commit()
    MS.write_organization_record(
        s, OrgRecord(org_id="org2", name=None, description="core desc"), org_id="org2"
    )
    row = s.query(
        "SELECT org_name, created_at, description FROM organization WHERE org_id = ?", ("org2",)
    )[0]
    assert row["org_name"] == "Hosted Co"  # preserved (COALESCE keeps the existing non-null)
    assert row["created_at"] == "2026-07-23T00:00:00Z"  # core never touches it
    assert row["description"] == "core desc"  # content upserted
    s.close()


def _write_profile(root: Path, datasource: str) -> None:
    d = root / datasource
    d.mkdir(parents=True, exist_ok=True)
    (d / "org.yaml").write_text(
        f"organization: acme\nversion: 1\ndescription: {datasource} model.\n"
        "storage_connections:\n  - name: warehouse\n    storage_type: PostgreSQL\nsubject_areas: []\n"
    )


def test_deploy_writes_one_org_row_and_two_datasource_rows_on_one_org_id(tmp_path, monkeypatch):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    _write_profile(arts, "crm")
    _write_profile(arts, "erp")
    OR.write_org_record(arts, _full_record(org_id="deployorg"))  # the root organization.yaml
    OR.narrative_path(arts).write_text("# About Acme\nWe sell widgets.\n")  # company narrative → memory row

    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(arts))
    monkeypatch.setenv("AGAMI_ORG_ID", "deployorg")  # pin the resolved org for the assertion
    tools.resolved_org_id.cache_clear()

    assert model_deploy.main([]) == 0

    s = Store.connect("sqlite://" + str(tmp_path / "m.db"))
    org_rows = s.query("SELECT org_id FROM organization")
    ds_rows = s.query("SELECT org_id, datasource FROM datasource_model ORDER BY datasource")
    company_mem = s.query(
        "SELECT content FROM memory WHERE org_id = ? AND datasource = '' AND kind = 'organization'",
        ("deployorg",),
    )
    deployed_record = MS.load_organization_record(s, "deployorg")
    s.close()

    assert [r["org_id"] for r in org_rows] == ["deployorg"]  # exactly ONE org row
    assert company_mem and "widgets" in company_mem[0]["content"]  # company narrative → company-level row
    assert [(r["org_id"], r["datasource"]) for r in ds_rows] == [
        ("deployorg", "crm"),
        ("deployorg", "erp"),
    ]  # two datasource rows, same org_id
    # The deploy refreshed the datasource list from disk and derived it into the org row's doc.
    assert deployed_record.datasources == ["crm", "erp"]
