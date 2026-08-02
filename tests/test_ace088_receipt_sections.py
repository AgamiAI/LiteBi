"""The five declared receipt sections: what each one establishes, and what it admits it did not.

ACE-088 reshapes `runtime.assemble_receipt` around `guardrail.Receipt.SECTIONS`: `columns`,
`tables`, `joins`, `aggregates`, `assumptions`, each `{items, undetermined}`. The property under
test is not "the sections exist" but that the four states of a section stay distinguishable. An
empty list with no marker means "checked, found nothing", and an empty list WITH one means "not
checked, and here is why". Before this, both were the empty list and silence read as clean.

In this slice the sections are ADDITIVE: the flat keys the chart template reads are untouched, and
the sections live under `receipt["sections"]` because `assumptions` names both a flat list and a
section and the two cannot share a dict. `test_the_flat_receipt_keys_are_untouched` is what makes
that guarantee a test rather than an intention.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import guardrail  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

# --- fixture ----------------------------------------------------------------
#
# `test_mcp_harness.py::_write_rich_model` verbatim, minus its `git init` (nothing here resolves a
# model version, which is the only thing that history feeds) and plus `performance_hints` on
# `orders`, which is what lets the tables section prove it carries the model's row ESTIMATE rather
# than counting rows itself. Copied rather than imported, for the reason
# `test_ace035_gate_verdict_parity.py` gives: the fixture is the spec of what each assertion below
# means, so it stays readable here and cannot be re-pointed at a different model by an edit to
# another test file.

ROWS_AS_OF = "2026-01-01T00:00:00Z"
ROW_ESTIMATE = 1200


def _write_rich_model(tmp_path):
    """A model with an UNREVIEWED metric + an UNREVIEWED join, the trust signals the
    receipt must surface. Returns the profile root."""
    yaml = __import__("yaml")
    p = tmp_path / "p"
    (p / "datasources" / "c").mkdir(parents=True)
    (p / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (p / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (p / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (p / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (p / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                                {"storage_connection": "c", "schema": "public", "table": "customers"}]}))
    (p / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "o",
        "performance_hints": {"estimated_row_count": ROW_ESTIMATE, "estimated_row_count_at": ROWS_AS_OF},
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "amount", "type": "decimal", "description": "net revenue",
                     "description_source": "ai_unvalidated"}]}))
    (p / "subject_areas" / "s" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "c",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    (p / "subject_areas" / "s" / "metrics" / "revenue.yaml").write_text(yaml.safe_dump({
        "name": "revenue", "calculation": "sum of order amount", "bindings": {"PostgreSQL": "SUM(amount)"},
        "source_tables": ["orders"], "confidence": "proposed", "review_state": "unreviewed"}))
    (p / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "orders", "from_column": "customer_id",
                           "to_table": "customers", "to_column": "id", "from_schema": "public",
                           "to_schema": "public", "relationship": "many_to_one",
                           "confidence": "inferred", "review_state": "unreviewed"}]}))
    return p


SQL = ("SELECT c.id, SUM(amount) AS total FROM orders o "
       "JOIN customers c ON o.customer_id = c.id GROUP BY c.id")

# The same table read twice under two scopes: once inside a CTE, once in the outer statement. The
# literal is deliberate: no section may echo it (see the data-sensitivity test).
CTE_SQL = ("WITH big AS (SELECT customer_id FROM public.orders WHERE amount > 4242) "
           "SELECT o.id FROM orders o JOIN big b ON o.customer_id = b.customer_id")
CTE_LITERAL = "4242"


@pytest.fixture()
def org(tmp_path):
    return L.load_datasource(_write_rich_model(tmp_path))


def _sections(org, sql=SQL, **kw):
    return rt.assemble_receipt(org, sql, **kw)["sections"]


# --- the container ----------------------------------------------------------


def test_every_declared_section_is_present_and_shaped(org):
    """Empty-but-declared, never absent: a consumer must not have to tell "no joins" from "joins
    not checked", and it cannot do that if the key is sometimes missing."""
    sections = _sections(org)
    assert tuple(sections) == guardrail.Receipt.SECTIONS
    for name, section in sections.items():
        assert set(section) == {"items", "undetermined"}, name
        assert isinstance(section["items"], list), name
        assert section["undetermined"] is None or isinstance(section["undetermined"], str), name


# --- columns (ACE-058 owns the gap) -----------------------------------------


def test_columns_section_lists_every_referenced_column_then_the_matched_metrics(org):
    section = _sections(org)["columns"]
    cols = [i["column"] for i in section["items"] if i["column"]]
    # Every column the statement references, resolved through the alias scope, not just the three
    # the assumptions filter keeps.
    assert cols == ["public.customers.id", "public.orders.amount", "public.orders.customer_id"]
    assert all(i["metric"] is None for i in section["items"] if i["column"])
    # The metric match is statement-level, so it is its own entry with no owning column.
    metric_entries = [i for i in section["items"] if i["metric"]]
    assert [i["column"] for i in metric_entries] == [None]
    assert metric_entries[0]["metric"]["name"] == "revenue"
    assert metric_entries[0]["metric"]["review_state"] == "unreviewed"


def test_columns_section_says_metric_attribution_is_not_per_column(org):
    section = _sections(org)["columns"]
    assert section["undetermined"] == rt.UNDETERMINED_COLUMNS
    assert "ACE-058" in section["undetermined"]


# --- tables (ACE-042 owns the gap) ------------------------------------------


def test_tables_section_is_one_entry_per_reference_not_per_table(org):
    """ACE-042 SC-2 needs a filter satisfied in a CTE and absent outside it reported accurately
    rather than credited to the whole statement. One entry per table cannot express that, so a
    table read twice under two scopes appears twice."""
    items = _sections(org, CTE_SQL)["tables"]
    orders = [i for i in items["items"] if i["qname"] == "public.orders"]
    assert len(orders) == 2
    # Same table, two references: distinguished by how each was written and aliased.
    assert {(i["ref"], i["alias"]) for i in orders} == {("orders", "o"), ("public.orders", None)}


def test_tables_section_marks_a_reference_the_model_does_not_declare(org):
    """The CTE name is a reference the statement makes and the model knows nothing about. It is
    reported as undeclared rather than dropped, because a dropped reference is an unchecked one."""
    cte = next(i for i in _sections(org, CTE_SQL)["tables"]["items"] if i["ref"] == "big")
    assert cte["declared"] is False
    assert cte["qname"] is None
    assert (cte["rows"], cte["rows_as_of"], cte["freshness"]) == (None, None, None)


def test_tables_section_carries_the_models_row_estimate_not_a_count(org):
    orders = next(i for i in _sections(org, freshness="hourly")["tables"]["items"]
                  if i["qname"] == "public.orders")
    assert orders["declared"] is True
    assert (orders["rows"], orders["rows_as_of"]) == (ROW_ESTIMATE, ROWS_AS_OF)
    assert orders["freshness"] == "hourly"


def test_tables_section_says_declared_filters_are_not_accounted_for(org):
    section = _sections(org)["tables"]
    assert section["undetermined"] == rt.UNDETERMINED_TABLES
    assert "ACE-042" in section["undetermined"]


# --- joins (ACE-059 owns the gap) -------------------------------------------


def test_joins_section_is_todays_relationships_unchanged(org):
    receipt = rt.assemble_receipt(org, SQL)
    section = receipt["sections"]["joins"]
    assert section["items"] == receipt["relationships"]
    assert [i["name"] for i in section["items"]] == ["orders_to_customers"]
    assert section["items"][0]["review_state"] == "unreviewed"


def test_joins_section_says_the_actual_predicate_was_not_read(org):
    section = _sections(org)["joins"]
    assert section["undetermined"] == rt.UNDETERMINED_JOINS
    assert "ACE-059" in section["undetermined"]


# --- aggregates (ACE-060 owns the gap) --------------------------------------


def test_aggregates_section_is_empty_but_declared(org):
    """`SUM(amount)` is aggregated over a joined statement, exactly the shape a fan-out would
    corrupt. Nothing checked it, and the section says so instead of looking clean."""
    section = _sections(org)["aggregates"]
    assert section["items"] == []
    assert section["undetermined"] == rt.UNDETERMINED_AGGREGATES
    assert "ACE-060" in section["undetermined"]


# --- assumptions (complete today) -------------------------------------------


def test_assumptions_section_carries_forward_unchanged_with_no_marker(org):
    """The one section that is complete, so the only one with a null marker."""
    receipt = rt.assemble_receipt(org, SQL)
    section = receipt["sections"]["assumptions"]
    assert section["undetermined"] is None
    assert section["items"] == receipt["assumptions"]
    assert [i["column"] for i in section["items"]] == ["public.orders.amount"]
    assert section["items"][0]["source"] == "ai_unvalidated"


# --- the four states --------------------------------------------------------


def test_an_unchecked_section_is_not_equal_to_a_clean_one(org):
    """The defect this spec exists to fix: before the marker, "checked, found nothing" and "not
    checked" were both `[]` and compared equal."""
    sections = _sections(org)
    checked_and_clean = {"items": [], "undetermined": None}
    assert sections["aggregates"]["items"] == checked_and_clean["items"]
    assert sections["aggregates"] != checked_and_clean
    # And the other two states are distinct from each other as well.
    established = sections["assumptions"]
    partly_established = sections["tables"]
    assert established["undetermined"] is None and established["items"]
    assert partly_established["undetermined"] is not None and partly_established["items"]
    assert established != partly_established


def test_an_unparseable_statement_reports_every_section_undetermined(org):
    """Never an empty receipt and never a missing one: a statement we could not read is a receipt
    that checked nothing, and every section has to say so."""
    sections = _sections(org, ";;;")
    assert tuple(sections) == guardrail.Receipt.SECTIONS
    for name, section in sections.items():
        assert section["items"] == [], name
        assert section["undetermined"] == rt.UNDETERMINED_UNPARSED, name


def test_a_missing_parser_reports_every_section_undetermined(org, monkeypatch):
    """The other early return. The vendored plugin mirror runs on whatever python3 the user has, so
    a missing sqlglot is a real deployment, not a hypothetical."""
    monkeypatch.setattr(rt, "_HAVE_SQLGLOT", False)
    sections = _sections(org)
    assert all(s == {"items": [], "undetermined": rt.UNDETERMINED_UNPARSED} for s in sections.values())


# --- determinism (REQ-022) --------------------------------------------------


def test_the_same_statement_and_model_produce_an_equal_receipt(org):
    """REQ-022. `ref_cols` is a set, so the columns section is sorted before it is emitted; without
    that, two calls could order the same facts differently."""
    first = rt.assemble_receipt(org, CTE_SQL, model_version="v1", freshness="hourly")
    second = rt.assemble_receipt(org, CTE_SQL, model_version="v1", freshness="hourly")
    assert first == second
    assert first["sections"] == second["sections"]


# --- data sensitivity -------------------------------------------------------


def _leaves(node):
    """Every scalar in a nested structure, so a test can assert on what a blob contains."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _leaves(value)
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value)
    else:
        yield node


def test_sections_carry_metadata_and_structure_only_never_values(org):
    """Data sensitivity: the receipt reports model metadata and statement structure, never sampled
    values or row contents, or it becomes a disclosure channel around the sensitive-column rules.

    Pinned two ways, because a prose rule does not survive an extension: the item shapes are closed
    (a field carrying values cannot appear unnoticed), and the only number anywhere in the sections
    is the row estimate the model itself declares.
    """
    sections = _sections(org, CTE_SQL, freshness="hourly")

    assert {frozenset(i) for i in sections["columns"]["items"]} == {frozenset({"column", "metric"})}
    assert {frozenset(i) for i in sections["tables"]["items"]} == {
        frozenset({"ref", "alias", "qname", "declared", "rows", "rows_as_of", "freshness"})}

    numbers = [v for v in _leaves(sections) if isinstance(v, int) and not isinstance(v, bool)]
    assert set(numbers) <= {ROW_ESTIMATE}
    # The statement's own literal is data-shaped; it reaches the receipt nowhere.
    assert CTE_LITERAL in CTE_SQL
    assert CTE_LITERAL not in json.dumps(sections)


# --- the additive guarantee -------------------------------------------------


def test_the_flat_receipt_keys_are_untouched(org):
    """The sections are additive in this slice. The chart template, `render_chart.py` and four test
    files still read the flat keys; a later PR deletes them and repoints those surfaces. Until then
    a vanished key is a silently missing trust banner on main."""
    receipt = rt.assemble_receipt(org, SQL, model_version="v1", applied_filters=["o.x IS NULL"])
    assert set(receipt) - {"sections"} == {
        "sql", "model_version", "tables_used", "relationships", "metrics",
        "named_filters", "assumptions", "warnings", "default_filters_applied",
    }
    assert receipt["sql"] == SQL
    assert receipt["model_version"] == "v1"
    assert [t["qname"] for t in receipt["tables_used"]] == ["public.customers", "public.orders"]
    assert [m["name"] for m in receipt["metrics"]] == ["revenue"]
    assert any("unreviewed join" in w for w in receipt["warnings"])
    assert receipt["named_filters"] == []
    assert receipt["default_filters_applied"] == ["o.x IS NULL"]
