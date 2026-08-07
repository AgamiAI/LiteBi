"""F16 / ACE-073: the cross-datasource metric as a first-class, validated model type.

A cross-datasource metric stitches per-key pieces from SEPARATE datasources into one number, lined up
on a shared key that a declared ACE-072 bridge resolves through (e.g. each account's CRM revenue next
to its ERP receivables, combined into "revenue at risk"). This spec only WRITES DOWN and CHECKS such a
metric — executing it is ACE-074. These tests pin: the model type (load / validate / round-trip + its
shape validator), the deployment-level validation pass (missing bridge, unknown datasource / grain
column, unknown alias in ``combine``, single-datasource all rejected; a good metric accepted), the
``federated`` executable (accepted on the metric, rejected on the join types), and the loader
(inline + sidecar merge, per-entry leniency, dedup-by-name).

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
    CrossDatasourceMetric,
    CrossDatasourceRelationship,
    Datasource,
    Relationship,
    StorageConnection,
    SubjectArea,
    SubMeasure,
    Table,
    TableRef,
)


def _datasource(name: str, schema: str, table: str, key: str) -> Datasource:
    """A minimal one-table datasource whose table carries `key` (the reconcile column) + `amount`."""
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


def _metric(**over) -> CrossDatasourceMetric:
    """A valid 2-piece metric: CRM revenue next to ERP receivables, reconciled on account_key."""
    base = dict(
        name="revenue_at_risk",
        calculation="each account's CRM revenue minus its ERP receivables",
        reconcile_on=["account_key"],
        combine="{crm_revenue} - {erp_ar}",
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"],
                       alias="crm_revenue"),
            SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"],
                       alias="erp_ar"),
        ],
    )
    base.update(over)
    return CrossDatasourceMetric(**base)


def _composite_datasource(name: str, schema: str, table: str) -> Datasource:
    """A one-table datasource carrying a COMPOSITE key (`region` + `account_key`) plus `amount`, for
    the composite-reconcile happy-path test."""
    tbl = Table(
        name=table,
        schema=schema,
        storage_connection="db",
        grain=["region", "account_key"],
        columns=[
            Column(name="region", type="string"),
            Column(name="account_key", type="string"),
            Column(name="amount", type="decimal"),
        ],
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


def _set_metrics(art: Path, metrics: list[CrossDatasourceMetric]) -> None:
    record = OR.load_org_record(art)
    OR.write_org_record(art, record.model_copy(update={"cross_datasource_metrics": metrics}))


def _set_bridges(art: Path, bridges: list[CrossDatasourceRelationship]) -> None:
    record = OR.load_org_record(art)
    OR.write_org_record(art, record.model_copy(update={"cross_datasource_relationships": bridges}))


# --------------------------------------------------------------------------- model


def test_metric_loads_validates_and_round_trips():
    m = _metric()
    assert m.executable == "federated"  # default: metric-only executable
    assert m.review_state == "unreviewed"  # trust-block default copied from the bridge
    assert m.source_datasources == ["acme_crm", "acme_erp"]  # computed, distinct, first-seen order
    # Lossless YAML round-trip through the model.
    reloaded = CrossDatasourceMetric(**yaml.safe_load(yaml.safe_dump(m.model_dump(mode="json"))))
    assert reloaded == m


def test_source_datasources_is_distinct_and_cannot_drift():
    # Three pieces across two datasources -> two distinct sources (a duplicate datasource is collapsed).
    m = _metric(
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="a"),
            SubMeasure(datasource="acme_crm", binding="COUNT(*)", grain=["account_key"], alias="b"),
            SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"], alias="c"),
        ],
        combine="{a} + {b} - {c}",
    )
    assert m.source_datasources == ["acme_crm", "acme_erp"]


def test_model_requires_federated():
    with pytest.raises(ValueError, match="federated"):
        _metric(executable="split")


def test_model_rejects_single_datasource():
    with pytest.raises(ValueError, match="distinct"):
        _metric(
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="a"),
                SubMeasure(datasource="acme_crm", binding="COUNT(*)", grain=["account_key"], alias="b"),
            ],
            combine="{a} - {b}",
        )


def test_model_rejects_too_few_pieces():
    with pytest.raises(ValueError, match=">= 2 sub_measures"):
        _metric(
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="a")
            ],
            combine="{a}",
        )


def test_model_rejects_empty_reconcile_on_and_bad_aliases():
    with pytest.raises(ValueError, match="reconcile_on"):
        _metric(reconcile_on=[])
    with pytest.raises(ValueError, match="unique"):
        _metric(
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="x"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"], alias="x"),
            ],
            combine="{x}",
        )
    with pytest.raises(ValueError, match="non-empty alias"):
        _metric(
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="x"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"], alias=" "),
            ],
            combine="{x}",
        )


def test_model_rejects_empty_calculation():
    with pytest.raises(ValueError, match="calculation"):
        _metric(calculation="   ")


def test_model_rejects_empty_combine():
    # A blank `combine` is a metric that combines nothing — rejected at construction (str_strip
    # turns "  " into "").
    with pytest.raises(ValueError, match="combine"):
        _metric(combine="   ")


def test_model_rejects_empty_grain():
    # Each piece must group by its local key column(s); an empty grain is rejected.
    with pytest.raises(ValueError, match="empty grain"):
        _metric(
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=[], alias="crm_revenue"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"], alias="erp_ar"),
            ],
        )


def test_model_rejects_grain_arity_mismatch():
    # A 2-part reconcile key needs a 2-column grain on every piece; a 1-column grain is rejected.
    with pytest.raises(ValueError, match="each piece groups by its local version"):
        _metric(
            reconcile_on=["region", "account_key"],
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)",
                           grain=["region", "account_key"], alias="crm_revenue"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)",
                           grain=["account_key"], alias="erp_ar"),  # only one key column
            ],
        )


def test_model_rejects_duplicate_reconcile_on():
    # A duplicated key component would set-match a shorter bridge key (set-equality) — reject at parse.
    with pytest.raises(ValueError, match="duplicate"):
        _metric(
            reconcile_on=["account_key", "account_key"],
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)",
                           grain=["account_key", "account_key"], alias="crm_revenue"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)",
                           grain=["account_key", "account_key"], alias="erp_ar"),
            ],
        )


def test_model_rejects_duplicate_grain():
    # reconcile_on is distinct, but a piece's grain duplicates a column — each key component distinct.
    with pytest.raises(ValueError, match="duplicate"):
        _metric(
            reconcile_on=["region", "account_key"],
            sub_measures=[
                SubMeasure(datasource="acme_crm", binding="SUM(amount)",
                           grain=["account_key", "account_key"], alias="crm_revenue"),
                SubMeasure(datasource="acme_erp", binding="SUM(amount)",
                           grain=["region", "account_key"], alias="erp_ar"),
            ],
        )


def test_federated_rejected_on_the_join_types():
    # `federated` is metric-only — a relationship or a bridge can't even be constructed with it.
    with pytest.raises(ValueError, match="federated"):
        Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                     relationship="many_to_one", executable="federated")
    with pytest.raises(ValueError, match="federated"):
        _bridge(executable="federated")


# --------------------------------------------------------------------------- deployment validation


def test_deployment_accepts_a_valid_metric(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])  # the metric needs a declared bridge on its reconcile key
    _set_metrics(art, [_metric()])
    res = validator.validate_deployment(art)
    assert res.ok, res.errors


def test_deployment_rejects_metric_with_no_bridge(tmp_path):
    # Fail-closed: a metric whose datasources aren't linked by any declared bridge on reconcile_on is
    # rejected. This ALSO exercises the fail-open fix — there are zero bridges, yet the metric is
    # still checked (a metric-but-no-bridge deployment must not skip metric validation).
    art = _deployment(tmp_path)
    _set_metrics(art, [_metric()])  # no bridges declared
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_no_bridge" for f in res.findings)


def test_deployment_rejects_unknown_datasource(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    _set_metrics(art, [_metric(
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="crm_revenue"),
            SubMeasure(datasource="acme_missing", binding="SUM(amount)", grain=["account_key"], alias="erp_ar"),
        ],
    )])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_endpoint_unresolved" for f in res.findings)


def test_deployment_rejects_unknown_grain_column(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    _set_metrics(art, [_metric(
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["not_a_column"], alias="crm_revenue"),
            SubMeasure(datasource="acme_erp", binding="SUM(amount)", grain=["account_key"], alias="erp_ar"),
        ],
    )])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_endpoint_unresolved"
               and "grain column" in f.message for f in res.findings)


def test_deployment_rejects_combine_with_unknown_alias(tmp_path):
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    _set_metrics(art, [_metric(combine="{crm_revenue} - {typo_alias}")])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_bad_combine" for f in res.findings)


def test_deployment_rejects_single_datasource_via_model_construct(tmp_path):
    # The model validator refuses a single-datasource metric, so it reaches the deployment only via a
    # hand-built object; model_construct bypasses validation to exercise the defensive check.
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    bad = CrossDatasourceMetric.model_construct(
        name="one_source",
        calculation="c",
        reconcile_on=["account_key"],
        combine="{a} - {b}",
        executable="federated",
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)", grain=["account_key"], alias="a"),
            SubMeasure(datasource="acme_crm", binding="COUNT(*)", grain=["account_key"], alias="b"),
        ],
    )
    res = validator.ValidationResult()
    validator._check_metric(bad, [_bridge()], {}, set(), res)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_single_source" for f in res.findings)


def test_deployment_metric_piece_on_unreadable_datasource(tmp_path):
    # A piece pointing at a datasource the record LISTS but whose model won't load reports the endpoint
    # unresolved with the "attached but ... failed to load" reason — distinct from a never-attached name
    # (same fail-closed split the bridge endpoint check makes).
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    _set_metrics(art, [_metric()])
    (art / "acme_erp" / "datasource.yaml").unlink()  # record still lists acme_erp, now unreadable
    res = validator.validate_deployment(art)
    assert not res.ok
    msgs = [f.message for f in res.findings if f.code == "cross_datasource_metric_endpoint_unresolved"]
    assert any("attached but its model failed to load" in m for m in msgs)


def test_deployment_bridge_must_match_reconcile_key(tmp_path):
    # A bridge exists between the two datasources, but on a DIFFERENT key than the metric reconciles
    # on — so no bridge links them on reconcile_on and the metric is rejected fail-closed.
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge(from_columns=["amount"], to_columns=["amount"])])
    _set_metrics(art, [_metric()])  # reconciles on account_key, not amount
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_no_bridge" for f in res.findings)


def test_deployment_rejects_disconnected_datasources(tmp_path):
    # PARTITIONED bridge set: bridges A-B and C-D over four datasources {A,B,C,D}. Every datasource is
    # bridged (degree >= 1), yet the graph splits into two islands that can't reconcile onto one key —
    # the connected-component check (not a degree test) catches this.
    for nm, sch, tbl in [("acme_a", "a", "ta"), ("acme_b", "b", "tb"),
                         ("acme_c", "c", "tc"), ("acme_d", "d", "td")]:
        build.write_tree(_datasource(nm, sch, tbl, "account_key"), tmp_path / nm)
    art = tmp_path
    _set_bridges(art, [
        _bridge(from_datasource="acme_a", to_datasource="acme_b",
                from_dataset="a.ta", to_dataset="b.tb"),
        _bridge(from_datasource="acme_c", to_datasource="acme_d",
                from_dataset="c.tc", to_dataset="d.td"),
    ])
    _set_metrics(art, [_metric(
        reconcile_on=["account_key"],
        combine="{a} + {b} + {c} + {d}",
        sub_measures=[
            SubMeasure(datasource="acme_a", binding="SUM(amount)", grain=["account_key"], alias="a"),
            SubMeasure(datasource="acme_b", binding="SUM(amount)", grain=["account_key"], alias="b"),
            SubMeasure(datasource="acme_c", binding="SUM(amount)", grain=["account_key"], alias="c"),
            SubMeasure(datasource="acme_d", binding="SUM(amount)", grain=["account_key"], alias="d"),
        ],
    )])
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_no_bridge" for f in res.findings)


def test_deployment_accepts_triangle_bridged_three_datasources(tmp_path):
    # Three datasources fully bridged in a triangle (A-B, B-C, A-C) on the reconcile key: they form ONE
    # component, so the metric is accepted. (The redundant third edge means a node is reached via two
    # paths — the component walk still terminates on one island.)
    for nm, sch, tbl in [("acme_a", "a", "ta"), ("acme_b", "b", "tb"), ("acme_c", "c", "tc")]:
        build.write_tree(_datasource(nm, sch, tbl, "account_key"), tmp_path / nm)
    art = tmp_path
    _set_bridges(art, [
        _bridge(from_datasource="acme_a", to_datasource="acme_b", from_dataset="a.ta", to_dataset="b.tb"),
        _bridge(from_datasource="acme_b", to_datasource="acme_c", from_dataset="b.tb", to_dataset="c.tc"),
        _bridge(from_datasource="acme_a", to_datasource="acme_c", from_dataset="a.ta", to_dataset="c.tc"),
    ])
    _set_metrics(art, [_metric(
        reconcile_on=["account_key"],
        combine="{a} + {b} + {c}",
        sub_measures=[
            SubMeasure(datasource="acme_a", binding="SUM(amount)", grain=["account_key"], alias="a"),
            SubMeasure(datasource="acme_b", binding="SUM(amount)", grain=["account_key"], alias="b"),
            SubMeasure(datasource="acme_c", binding="SUM(amount)", grain=["account_key"], alias="c"),
        ],
    )])
    res = validator.validate_deployment(art)
    assert res.ok, res.errors


def test_deployment_accepts_composite_reconcile_key(tmp_path):
    # Composite reconcile key (region + account_key): each piece groups by both columns, and a single
    # bridge naming the same two columns (in any order) links them. Proves _bridge_matches_key is a set
    # equality — the bridge's to_columns list the pair reversed and it still matches.
    build.write_tree(_composite_datasource("acme_crm", "crm", "accounts"), tmp_path / "acme_crm")
    build.write_tree(_composite_datasource("acme_erp", "erp", "customers"), tmp_path / "acme_erp")
    art = tmp_path
    _set_bridges(art, [_bridge(
        from_columns=["region", "account_key"],
        to_columns=["account_key", "region"],  # same set, reversed order
    )])
    _set_metrics(art, [_metric(
        reconcile_on=["region", "account_key"],
        combine="{crm_revenue} - {erp_ar}",
        sub_measures=[
            SubMeasure(datasource="acme_crm", binding="SUM(amount)",
                       grain=["region", "account_key"], alias="crm_revenue"),
            SubMeasure(datasource="acme_erp", binding="SUM(amount)",
                       grain=["region", "account_key"], alias="erp_ar"),
        ],
    )])
    res = validator.validate_deployment(art)
    assert res.ok, res.errors


def test_deployment_rejects_combine_ignoring_a_piece(tmp_path):
    # `combine` must reference EXACTLY the declared pieces: a formula that ignores a declared piece
    # (a sub-measure computed for nothing) is rejected, not just an unknown alias.
    art = _deployment(tmp_path)
    _set_bridges(art, [_bridge()])
    _set_metrics(art, [_metric(combine="{crm_revenue}")])  # erp_ar declared but never used
    res = validator.validate_deployment(art)
    assert not res.ok
    assert any(f.code == "cross_datasource_metric_bad_combine"
               and "never referenced" in f.message for f in res.findings)


def test_validate_cli_runs_the_deployment_metric_pass(tmp_path):
    from semantic_model import cli

    art = _deployment(tmp_path)
    _set_metrics(art, [_metric()])  # a metric with no bridge -> deployment pass fails
    # `sm validate <profile>` folds in the deployment pass — a broken metric fails the profile's
    # validate even though the profile itself is fine.
    assert cli.main(["validate", str(art / "acme_crm")]) == 1


# --------------------------------------------------------------------------- sidecar + leniency + dedup


def test_sidecar_metrics_merge_on_load(tmp_path):
    art = _deployment(tmp_path)
    (art / OR.METRICS_FILENAME).write_text(
        yaml.safe_dump({"metrics": [_metric().model_dump(mode="json")]}), encoding="utf-8"
    )
    record = OR.load_org_record(art)
    assert len(record.cross_datasource_metrics) == 1
    assert record.cross_datasource_metrics[0].name == "revenue_at_risk"


def test_bare_list_metric_sidecar_loads(tmp_path):
    art = _deployment(tmp_path)
    (art / OR.METRICS_FILENAME).write_text(
        yaml.safe_dump([_metric().model_dump(mode="json")]),  # bare list, no `metrics:` key
        encoding="utf-8",
    )
    record = OR.load_org_record(art)
    assert len(record.cross_datasource_metrics) == 1


def test_malformed_metric_entry_does_not_raise_and_valid_sibling_loads(tmp_path):
    # load_org_record is on runtime paths and lenient by contract — a malformed metric entry (single
    # datasource fails the model validator) is skipped, not propagated, and a valid sibling loads.
    art = _deployment(tmp_path)
    (art / OR.METRICS_FILENAME).write_text(
        yaml.safe_dump({"metrics": [
            {"name": "broken", "calculation": "c", "reconcile_on": ["account_key"], "combine": "{a}",
             "sub_measures": [{"datasource": "acme_crm", "binding": "x", "alias": "a"}]},  # < 2 pieces
            _metric().model_dump(mode="json"),  # valid sibling
        ]}),
        encoding="utf-8",
    )
    record = OR.load_org_record(art)  # must not raise
    assert len(record.cross_datasource_metrics) == 1
    assert record.cross_datasource_metrics[0].name == "revenue_at_risk"


def test_corrupt_metric_yaml_file_does_not_raise(tmp_path):
    # A syntactically corrupt metric FILE degrades to no metrics rather than propagating out of the
    # runtime-path load_org_record.
    art = _deployment(tmp_path)
    (art / OR.METRICS_FILENAME).write_text("metrics: [ : broken", encoding="utf-8")
    record = OR.load_org_record(art)  # must not raise
    assert record.cross_datasource_metrics == []


def test_scalar_metric_sidecar_yields_no_metrics(tmp_path):
    # A sidecar whose top-level YAML is neither a mapping nor a list (a bare scalar) yields no metrics
    # rather than raising — the "anything else -> []" fallthrough the bridge + metric entries share.
    art = _deployment(tmp_path)
    (art / OR.METRICS_FILENAME).write_text("just a scalar", encoding="utf-8")
    record = OR.load_org_record(art)  # must not raise
    assert record.cross_datasource_metrics == []


def test_metrics_dedup_by_name(tmp_path):
    # Metrics dedup by `name` (unlike anonymous bridges, which dedup by endpoint): an inline copy and a
    # sidecar copy of the same name collapse to one, and the inline copy (concatenated first) survives.
    art = _deployment(tmp_path)
    _set_metrics(art, [_metric(calculation="inline definition")])
    (art / OR.METRICS_FILENAME).write_text(
        yaml.safe_dump({"metrics": [_metric(calculation="sidecar definition").model_dump(mode="json")]}),
        encoding="utf-8",
    )
    survivors = OR.load_org_record(art).cross_datasource_metrics
    assert len(survivors) == 1
    assert survivors[0].calculation == "inline definition"  # inline wins (first occurrence kept)
