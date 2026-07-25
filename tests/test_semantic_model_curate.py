"""Unit tests for semantic_model/curate.py — review queue, model tree, and the
apply (exclude/include/approve/reject) write path with validation gating."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# case/whitespace-insensitive star projection — mirrors runtime.check_no_select_star (AST-based),
# so the test isn't fooled by `SELECT  *` / `select *` the way a plain substring check would be.
_STAR_PROJECTION = re.compile(r"(?i)\bselect\s*\*")

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import curate, loader  # noqa: E402


def _write_model(root: Path, *, git: bool = True) -> None:
    """A tiny two-table model with a metric + a relationship, written to disk."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "shop", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                   {"storage_connection": "c", "schema": "public", "table": "customers"}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "ssn", "type": "string", "sensitive": True}],
    }))
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "customers",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
    }))
    (root / "subject_areas" / "sales" / "metrics" / "order_count.yaml").write_text(yaml.safe_dump({
        "name": "order_count", "calculation": "count of orders",
        "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"],
        "confidence": "proposed", "review_state": "unreviewed",
    }))
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "orders", "from_column": "customer_id",
                           "to_table": "customers", "to_column": "id",
                           "relationship": "many_to_one", "confidence": "inferred",
                           "review_state": "unreviewed"}],
    }))
    if git:
        subprocess.run(["git", "-C", str(root), "init", "-q"])
        subprocess.run(["git", "-C", str(root), "add", "-A"])
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "init"])


def test_review_queue_partitions_rule1_rule2(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_organization(tmp_path)
    q = curate.review_queue(org)
    assert q["counts"]["rule_1"] == 1  # the proposed metric
    assert q["counts"]["rule_2"] == 1  # the inferred relationship
    assert q["rule_1"][0]["kind"] == "metric"
    assert q["rule_2"][0]["kind"] == "relationship"


def test_model_tree_shows_columns_and_state(tmp_path):
    _write_model(tmp_path, git=False)
    org = loader.load_organization(tmp_path, include_rejected=True)
    tree = curate.model_tree(org)
    orders = next(t for t in tree["subject_areas"][0]["tables"] if t["table"] == "orders")
    assert orders["review_state"] == "approved"
    ssn = next(c for c in orders["columns"] if c["name"] == "ssn")
    assert ssn["sensitive"] is True


def test_exclude_table_hides_from_runtime(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table",
                                   "area": "sales", "name": "orders"}])
    assert res.validated and res.applied
    runtime = loader.load_organization(tmp_path)  # drops rejected
    assert not any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)
    # still visible with include_rejected (so the explorer can toggle it back)
    full = loader.load_organization(tmp_path, include_rejected=True)
    assert any(t.name == "orders" for sa in full.subject_areas for t in sa.tables_defined)


def test_include_restores(tmp_path):
    _write_model(tmp_path)
    curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales", "name": "orders"}])
    curate.apply(tmp_path, [{"op": "include", "kind": "table", "area": "sales", "name": "orders"}])
    runtime = loader.load_organization(tmp_path)
    assert any(t.name == "orders" for sa in runtime.subject_areas for t in sa.tables_defined)


def test_exclude_column(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "exclude", "kind": "table", "area": "sales",
                                   "name": "orders", "column": "ssn"}])
    assert res.validated
    orders = loader.load_organization(tmp_path).subject_areas[0].defined_table("orders")
    assert not any(c.name == "ssn" for c in orders.columns)


def test_approve_metric_records_signoff(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                   "name": "order_count", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    org = loader.load_organization(tmp_path)
    mm = org.subject_areas[0].metrics[0]
    assert mm.review_state == "approved" and mm.signed_off_by == "reviewer@example.com"
    # no longer in the Rule 1 queue
    assert curate.review_queue(org)["counts"]["rule_1"] == 0


def test_approve_relationship(tmp_path):
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "approve", "kind": "relationship", "area": "sales",
                                   "name": "orders->customers", "at": "2026-06-09T00:00:00Z"}],
                       signer="reviewer@example.com", role="cto")
    assert res.validated
    rel = loader.load_organization(tmp_path).subject_areas[0].relationships[0]
    assert rel.review_state == "approved" and rel.signed_off_by == "reviewer@example.com"


def test_edit_relationship_on_clause(tmp_path):
    _write_model(tmp_path)
    # the "approve with fix" flow: set on: to a CAST, drop the simple columns
    res = curate.apply(tmp_path, [
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "on", "value": "CAST(orders.customer_id AS TEXT) = customers.id"},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "from_column", "value": None},
        {"op": "edit", "kind": "relationship", "area": "sales", "name": "orders->customers",
         "field": "to_column", "value": None},
    ])
    assert res.validated, res.errors
    rel = loader.load_organization(tmp_path).subject_areas[0].relationships[0]
    assert rel.on and "CAST" in rel.on


def test_approve_resolves_slugged_metric_name(tmp_path):
    """Regression: a multi-word metric name is stored slugged (`total event sales` →
    total_event_sales.yaml). `apply(approve)` must resolve the slug — looking for a literal
    `total event sales.yaml` was caught into res.skipped, so the approval silently no-op'd."""
    _write_model(tmp_path)
    curate.write_items(tmp_path, "sales", "metric", [
        {"name": "total event sales", "calculation": "count of event sales",
         "bindings": {"PostgreSQL": "COUNT(*)"}, "source_tables": ["orders"],
         "confidence": "inferred", "review_state": "unreviewed"}])
    assert (tmp_path / "subject_areas" / "sales" / "metrics" / "total_event_sales.yaml").exists()

    res = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                   "name": "total event sales", "at": "2026-06-12T00:00:00Z"}],
                       signer="x@y.com", role="cto")
    assert res.applied and not res.skipped, res.skipped
    m2 = next(x for x in loader.load_organization(tmp_path).subject_areas[0].metrics
              if x.name == "total event sales")
    assert m2.review_state == "approved"

    # a genuinely-missing metric now fails with a CLEAR reason (not a cryptic FileNotFoundError)
    res2 = curate.apply(tmp_path, [{"op": "approve", "kind": "metric", "area": "sales",
                                    "name": "no such metric"}])
    assert res2.skipped and "no metric named" in res2.skipped[0]["reason"]


def test_edit_op_auto_resolves_area_when_omitted(tmp_path):
    """No `area` on the op -> curate resolves which area owns the table by name (so callers
    never hand-maintain a table->area map / generator script)."""
    _write_model(tmp_path)
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                   "field": "description", "value": "all orders", "source": "ai"}])
    assert res.validated and res.applied and not res.skipped, res.skipped
    orders = loader.load_organization(tmp_path).subject_areas[0].defined_table("orders")
    assert orders.description == "all orders"
    assert orders.description_source == "ai_unvalidated"  # source:"ai" stamped


def test_edit_op_ambiguous_area_errors_clearly(tmp_path):
    """A bare table name that exists in two schema areas can't be auto-resolved -> the op is
    skipped with a clear 'pass an explicit area' reason rather than editing the wrong one."""
    yaml = __import__("yaml")
    _write_model(tmp_path)
    # add a second area 'crm' that ALSO has an `orders` table (the cross-schema collision shape)
    crm = tmp_path / "subject_areas" / "crm" / "tables"
    crm.mkdir(parents=True)
    (tmp_path / "subject_areas" / "crm" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "crm", "tables": [{"storage_connection": "c", "schema": "crm", "table": "orders"}]}))
    (crm / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "crm", "storage_connection": "c", "grain": ["id"],
        "description": "crm orders", "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    res = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "name": "orders",
                                   "field": "description", "value": "x", "source": "ai"}])
    assert res.skipped and "multiple areas" in res.skipped[0]["reason"]
    # with an explicit area it resolves fine
    res2 = curate.apply(tmp_path, [{"op": "edit", "kind": "table", "area": "crm", "name": "orders",
                                    "field": "description", "value": "crm only", "source": "ai"}])
    assert res2.applied and not res2.skipped, res2.skipped


def test_approve_cross_area_relationship_writes_org_yaml(tmp_path):
    """A cross-schema/cross-area join lives in datasource.yaml (not an area's relationships.yaml),
    so approving it must update datasource.yaml via the org-level fallback."""
    root = tmp_path
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    for area, tbl in [("billing", "invoices"), ("crm", "customers")]:
        (root / "subject_areas" / area / "tables").mkdir(parents=True)
        (root / "subject_areas" / area / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": area, "tables": [{"storage_connection": "c", "schema": area, "table": tbl}]}))
        (root / "subject_areas" / area / "tables" / f"{tbl}.yaml").write_text(yaml.safe_dump({
            "name": tbl, "schema": area, "storage_connection": "c", "grain": ["id"], "description": tbl,
            "columns": [{"name": "id", "type": "integer", "primary_key": True},
                        {"name": "customer_id", "type": "integer"}]}))
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "shop", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/billing", "subject_areas/crm"],
        "cross_subject_area_relationships": [{
            "from_table": "invoices", "from_column": "customer_id", "to_table": "customers", "to_column": "id",
            "from_subject_area": "billing", "to_subject_area": "crm", "from_schema": "billing", "to_schema": "crm",
            "relationship": "many_to_one", "confidence": "inferred", "review_state": "unreviewed"}]}))
    subprocess.run(["git", "-C", str(root), "init", "-q"])
    subprocess.run(["git", "-C", str(root), "add", "-A"])
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "i"])

    res = curate.apply(root, [{"op": "approve", "kind": "relationship", "area": "billing",
                               "name": "invoices->customers", "at": "2026-06-12T00:00:00Z"}],
                       signer="reviewer@example.com", role="data_lead")
    assert res.validated and res.applied and not res.skipped, res.skipped
    o = yaml.safe_load((root / "datasource.yaml").read_text())
    cr = o["cross_subject_area_relationships"][0]
    assert cr["review_state"] == "approved" and cr["signed_off_by"] == "reviewer@example.com"

    # editing a cross-area join's field also persists to datasource.yaml (the UI edit path) —
    # not just approve/reject. So cross joins are as editable as regular joins.
    res2 = curate.apply(root, [{"op": "edit", "kind": "relationship", "area": "billing",
                                "name": "invoices->customers", "field": "description",
                                "value": "invoice to its CRM account"}])
    assert res2.validated and res2.applied and not res2.skipped, res2.skipped
    cr2 = yaml.safe_load((root / "datasource.yaml").read_text())["cross_subject_area_relationships"][0]
    assert cr2["description"] == "invoice to its CRM account"


def test_validate_seeds_probe_is_star_free(tmp_path):
    """Regression (ACE-062 A1): the zero-row validation wrapper must not project `SELECT *`.
    The executor's SELECT-* ban walks every SELECT in the tree, so a `SELECT *` wrapper would
    trip the ban on itself and reject every seed. We simulate that ban with a runner that
    raises on any star in the probe, and assert a legit named-column seed still passes."""
    seen_probes: list[str] = []

    def runner(sql: str) -> None:
        seen_probes.append(sql)
        # mimic runtime.check_no_select_star: a projected star anywhere is refused
        if _STAR_PROJECTION.search(sql):
            raise RuntimeError('{"kind": "select_star"}')

    seed = {"question": "how many orders?", "sql": "SELECT COUNT(id) AS n FROM orders"}
    passing, rejected = curate.validate_seeds([seed], runner)

    assert not rejected, rejected
    assert len(passing) == 1 and passing[0]["source"] == "seed"
    assert seen_probes and not _STAR_PROJECTION.search(seen_probes[0])  # wrapper is star-free
