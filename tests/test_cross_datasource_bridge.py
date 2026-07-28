"""F16 / ACE-072: the cross-datasource bridge as a first-class, validated model type.

A bridge records that a key in ONE datasource is the same entity as a key in ANOTHER (e.g.
``accounts.account_key`` in a CRM = ``customers.account_key`` in an ERP). Before this it lived only
as a skill-side file — unvalidated, and lost on a machine re-onboard. These tests pin: the model
type (load / validate / round-trip + its endpoint validator), the deployment-level validation pass
(same_engine and unresolved endpoints rejected, a good bridge accepted), and the legacy migration
(the old file is surfaced idempotently on load).

Synthetic fixtures only (``acme`` datasources keyed on ``account_key``); no network is touched
(file I/O + pydantic only), so ``tests/test_privacy_no_network.py`` stays the separate egress gate.
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

import yaml  # noqa: E402
from semantic_model import build, validator  # noqa: E402
from semantic_model import org_record as OR  # noqa: E402
from semantic_model.models import (  # noqa: E402
    Column,
    CrossDatasourceRelationship,
    Datasource,
    StorageConnection,
    SubjectArea,
    Table,
    TableRef,
)


def _datasource(name: str, schema: str, table: str, key: str) -> Datasource:
    """A minimal one-table datasource whose table carries `key` (the bridge join column)."""
    tbl = Table(
        name=table,
        schema=schema,
        storage_connection="db",
        grain=[key],
        columns=[Column(name=key, type="string"), Column(name="amount", type="decimal")],
    )
    sa = SubjectArea(
        name="core",
        tables_defined=[tbl],
        tables=[TableRef(table=table, schema=schema, storage_connection="db")],
    )
    return Datasource(
        datasource=name,
        storage_connections=[StorageConnection(name="db", storage_type="PostgreSQL")],
        subject_areas=[sa],
    )


def _bridge(**over) -> CrossDatasourceRelationship:
    base = dict(
        from_datasource="acme_crm",
        to_datasource="acme_erp",
        from_dataset="crm.accounts",
        to_dataset="erp.customers",
        from_columns=["account_key"],
        to_columns=["account_key"],
    )
    base.update(over)
    return CrossDatasourceRelationship(**base)


def _deployment(tmp_path: Path) -> Path:
    """A 2-datasource deployment on disk (acme_crm + acme_erp, both keyed on account_key), with an
    auto-maintained OrgRecord. Returns the artifacts dir."""
    build.write_tree(
        _datasource("acme_crm", "crm", "accounts", "account_key"), tmp_path / "acme_crm"
    )
    build.write_tree(
        _datasource("acme_erp", "erp", "customers", "account_key"), tmp_path / "acme_erp"
    )
    return tmp_path


def _set_bridges(art: Path, bridges: list[CrossDatasourceRelationship]) -> None:
    record = OR.load_org_record(art)
    OR.write_org_record(art, record.model_copy(update={"cross_datasource_relationships": bridges}))


# --------------------------------------------------------------------------- model


def test_bridge_loads_validates_and_round_trips():
    b = _bridge(description="same customer across CRM and ERP")
    assert b.executable == "split"  # default: never same_engine
    assert b.review_state == "unreviewed"  # trust-block default copied from Relationship
    # Lossless YAML round-trip through the model.
    reloaded = CrossDatasourceRelationship(
        **yaml.safe_load(yaml.safe_dump(b.model_dump(mode="json")))
    )
    assert reloaded == b


def test_endpoint_key_ignores_description_and_trust_fields():
    # De-dup identity is the endpoints only — two edges that differ just in prose OR in a trust
    # field (review_state here) collapse to one, so a re-load never keeps both.
    assert _bridge(description="a").endpoint_key == _bridge(description="b").endpoint_key
    assert (
        _bridge(review_state="unreviewed").endpoint_key
        == _bridge(review_state="approved", signed_off_by="you@example.com",
                   signed_off_at="2026-01-01", signed_off_role="admin").endpoint_key
    )


def test_model_rejects_same_engine():
    with pytest.raises(ValueError, match="same_engine"):
        _bridge(executable="same_engine")


def test_model_rejects_empty_and_mismatched_columns():
    with pytest.raises(ValueError, match="non-empty"):
        _bridge(from_columns=[], to_columns=[])
    with pytest.raises(ValueError, match="equal length"):
        _bridge(from_columns=["account_key"], to_columns=["account_key", "region"])


# --------------------------------------------------------------------------- deployment validation


def test_deployment_accepts_a_valid_bridge(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    res = validator.validate_deployment(art)
    assert res.ok, res.errors


def test_deployment_no_record_is_a_noop(tmp_path):
    # A pre-F16 layout (no organization.yaml) validates as an empty, ok result — never an error.
    res = validator.validate_deployment(tmp_path)
    assert res.ok and not res.findings


def test_deployment_rejects_unknown_datasource(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge(to_datasource="acme_missing")])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_endpoint_unresolved" for f in res.findings)


def test_deployment_rejects_unknown_dataset(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge(from_dataset="crm.nonexistent")])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_endpoint_unresolved" for f in res.findings)


def test_deployment_rejects_unknown_column(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge(to_columns=["not_a_column"])])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_endpoint_unresolved" for f in res.findings)


def test_deployment_tolerates_an_unreadable_profile(tmp_path):
    # A profile still listed on the record but broken on disk (its datasource.yaml gone) is skipped,
    # not crashed on — the per-profile validate owns that failure. A bridge into it then reports the
    # endpoint unresolved rather than raising.
    art = _deployment(tmp_path)
    (art / "acme_erp" / "datasource.yaml").unlink()  # record still lists acme_erp
    _set_bridges(art, [_bridge()])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_endpoint_unresolved" for f in res.findings)


def test_deployment_distinguishes_failed_load_from_unattached(tmp_path):
    # Two ways an endpoint's datasource can be unresolvable, same fail-closed code but distinct text:
    #   (a) a datasource the record LISTS but whose model won't load -> "attached but ... failed to load"
    #   (b) a name that was never attached at all -> "not an attached datasource"
    art = _deployment(tmp_path)
    (art / "acme_erp" / "datasource.yaml").unlink()  # record still lists acme_erp, now unreadable
    _set_bridges(art, [_bridge(), _bridge(to_datasource="acme_missing")])
    res = validator.validate_deployment(art)
    assert not res.ok
    msgs = [f.message for f in res.findings if f.code == "cross_datasource_endpoint_unresolved"]
    assert any("attached but its model failed to load" in m for m in msgs)
    assert any("is not an attached datasource" in m for m in msgs)


def _table(name: str, schema, key: str = "account_key") -> Table:
    return Table(
        name=name,
        schema=schema,
        storage_connection="db",
        grain=[key],
        columns=[Column(name=key, type="string")],
    )


def test_resolve_dataset_prefers_exact_schema_over_lenient():
    # Two same-named tables — one schema-qualified `crm`, one schema-less (SQLite-style). A
    # `crm.accounts` dataset must bind the exact-schema table, not the lenient (schema=None) one.
    exact = _table("accounts", "crm")
    lenient = _table("accounts", None)
    model = Datasource(
        datasource="acme",
        storage_connections=[StorageConnection(name="db", storage_type="PostgreSQL")],
        subject_areas=[
            SubjectArea(name="a", tables_defined=[lenient],
                        tables=[TableRef(table="accounts", schema=None, storage_connection="db")]),
            SubjectArea(name="b", tables_defined=[exact],
                        tables=[TableRef(table="accounts", schema="crm", storage_connection="db")]),
        ],
    )
    assert validator._resolve_dataset(model, "crm.accounts") is exact
    # Keep the leniency: a bare table (no schema) still resolves on the name alone.
    assert validator._resolve_dataset(model, "accounts") is not None


def test_resolve_dataset_lenient_when_model_is_schema_less():
    # A schema-less model (SQLite) resolves a schema-qualified dataset on the table name alone.
    model = Datasource(
        datasource="acme",
        storage_connections=[StorageConnection(name="db", storage_type="SQLite")],
        subject_areas=[SubjectArea(
            name="a", tables_defined=[_table("accounts", None)],
            tables=[TableRef(table="accounts", schema=None, storage_connection="db")])],
    )
    assert validator._resolve_dataset(model, "crm.accounts") is not None


def test_deployment_rejects_same_engine_bridge(tmp_path):
    # same_engine can't be CONSTRUCTED (the model validator refuses), so it reaches the deployment
    # only via a hand-built object; model_construct bypasses validation to exercise that branch.
    from semantic_model import loader

    art = _deployment(tmp_path)
    models = {n: loader.load_datasource(art / n) for n in ("acme_crm", "acme_erp")}
    bad = CrossDatasourceRelationship.model_construct(
        from_datasource="acme_crm",
        to_datasource="acme_erp",
        from_dataset="crm.accounts",
        to_dataset="erp.customers",
        from_columns=["account_key"],
        to_columns=["account_key"],
        executable="same_engine",
    )
    res = validator.ValidationResult()
    validator._check_bridge(bad, models, set(), res)
    assert not res.ok
    assert any(f.code == "cross_datasource_executable_mismatch" for f in res.findings)


def test_validate_cli_runs_the_deployment_pass(tmp_path):
    from semantic_model import cli

    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge(from_columns=["not_a_column"])])
    # `sm validate <profile>` now folds in the deployment pass — a broken bridge fails the profile's
    # validate even though the profile itself is fine.
    assert cli.main(["validate", str(art / "acme_crm")]) == 1


# --------------------------------------------------------------------------- sidecar + legacy migration


def test_sidecar_bridges_merge_on_load(tmp_path):
    art = _deployment(tmp_path)
    (art / OR.BRIDGES_FILENAME).write_text(
        yaml.safe_dump({"relationships": [_bridge().model_dump(mode="json")]}), encoding="utf-8"
    )
    record = OR.load_org_record(art)
    assert len(record.cross_datasource_relationships) == 1
    assert record.cross_datasource_relationships[0].from_dataset == "crm.accounts"


def test_malformed_sidecar_entry_does_not_raise_and_valid_sibling_loads(tmp_path):
    # load_org_record is on runtime paths and is lenient by contract — a malformed sidecar entry
    # (empty columns fail the model validator) must be skipped, not propagated, and a valid sibling
    # in the same file still loads.
    art = _deployment(tmp_path)
    (art / OR.BRIDGES_FILENAME).write_text(
        yaml.safe_dump(
            {"relationships": [
                {"from_datasource": "acme_crm", "to_datasource": "acme_erp",
                 "from_dataset": "crm.accounts", "to_dataset": "erp.customers",
                 "from_columns": [], "to_columns": []},  # malformed: empty columns
                _bridge().model_dump(mode="json"),        # valid sibling
            ]}
        ),
        encoding="utf-8",
    )
    record = OR.load_org_record(art)  # must not raise
    assert len(record.cross_datasource_relationships) == 1
    assert record.cross_datasource_relationships[0].from_dataset == "crm.accounts"


def test_malformed_legacy_entry_does_not_raise_and_valid_sibling_loads(tmp_path):
    # Same leniency for the legacy migration: an entry missing a required key (KeyError in the
    # migrator) is skipped, and its valid sibling in the same file still migrates.
    art = _deployment(tmp_path)
    legacy = art / "local" / "cross_profile_relationships.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        yaml.safe_dump(
            {"relationships": [
                {"from_profile": "acme_crm", "to_profile": "acme_erp"},  # malformed: missing datasets/columns
                {"from_profile": "acme_crm", "to_profile": "acme_erp",
                 "from_dataset": "crm.accounts", "to_dataset": "erp.customers",
                 "from_columns": ["account_key"], "to_columns": ["account_key"]},  # valid sibling
            ]}
        ),
        encoding="utf-8",
    )
    record = OR.load_org_record(art)  # must not raise
    assert len(record.cross_datasource_relationships) == 1
    assert record.cross_datasource_relationships[0].from_datasource == "acme_crm"


def test_legacy_file_migrates_idempotently(tmp_path):
    art = _deployment(tmp_path)
    legacy = art / "local" / "cross_profile_relationships.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "name": "customer_bridge",
                        "from_profile": "acme_crm",
                        "to_profile": "acme_erp",
                        "from_dataset": "crm.accounts",
                        "to_dataset": "erp.customers",
                        "from_columns": ["account_key"],
                        "to_columns": ["account_key"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    record = OR.load_org_record(art)
    assert len(record.cross_datasource_relationships) == 1
    migrated = record.cross_datasource_relationships[0]
    assert migrated.from_datasource == "acme_crm"  # from_profile -> from_datasource
    assert migrated.executable == "split" and migrated.review_state == "unreviewed"
    assert migrated.name == "customer_bridge"
    assert migrated.migrated_from is not None  # provenance stamped so a future write is idempotent

    # Re-loading (or an inline copy of the same edge) never duplicates — deduped by endpoint.
    assert len(OR.load_org_record(art).cross_datasource_relationships) == 1
    _set_bridges(
        art, [migrated.model_copy(update={"migrated_from": None})]
    )  # same endpoints, inline
    survivors = OR.load_org_record(art).cross_datasource_relationships
    assert len(survivors) == 1
    # Precedence, not just count: the inline edge (migrated_from is None) is concatenated ahead of
    # the legacy one, and the merge keeps the FIRST occurrence — so the inline copy is the survivor.
    assert survivors[0].migrated_from is None
