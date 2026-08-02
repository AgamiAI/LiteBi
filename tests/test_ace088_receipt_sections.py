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
import os
import re
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
    assert "matched against the whole statement" in section["undetermined"]


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


def test_a_cte_that_shadows_a_declared_table_is_not_declared(org):
    """`declared` was resolved against a bare-name index, so a CTE named after a declared table
    reported `declared: true` and borrowed the real table's row estimate — a fact about a table this
    statement never read. `check_table_scope` has always subtracted CTE names; the receipt now
    subtracts the same set.

    It matters more from this slice on than it did before it: `declared` becomes user-facing on the
    refusal path, where it is the ONE model fact a refused caller is told.
    """
    shadowing = ("WITH orders AS (SELECT 1 AS id) SELECT id FROM orders")
    items = rt.assemble_receipt(org, shadowing)["sections"]["tables"]["items"]

    assert [i["ref"] for i in items] == ["orders"]
    assert items[0]["declared"] is False
    assert items[0]["qname"] is None
    assert (items[0]["rows"], items[0]["rows_as_of"]) == (None, None)


def test_tables_section_carries_the_models_row_estimate_not_a_count(org):
    orders = next(i for i in _sections(org, freshness="hourly")["tables"]["items"]
                  if i["qname"] == "public.orders")
    assert orders["declared"] is True
    assert (orders["rows"], orders["rows_as_of"]) == (ROW_ESTIMATE, ROWS_AS_OF)
    assert orders["freshness"] == "hourly"


def test_tables_section_says_declared_filters_are_not_accounted_for(org):
    section = _sections(org)["tables"]
    assert section["undetermined"] == rt.UNDETERMINED_TABLES
    assert "declared filters" in section["undetermined"]


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
    assert "not read out of the SQL" in section["undetermined"]


# --- aggregates (ACE-060 owns the gap) --------------------------------------


def test_aggregates_section_is_empty_but_declared(org):
    """`SUM(amount)` is aggregated over a joined statement, exactly the shape a fan-out would
    corrupt. Nothing checked it, and the section says so instead of looking clean."""
    section = _sections(org)["aggregates"]
    assert section["items"] == []
    assert section["undetermined"] == rt.UNDETERMINED_AGGREGATES
    assert "is not checked" in section["undetermined"]


def test_no_marker_ships_an_internal_spec_id_to_a_user(org):
    """This repo is public and these sentences surface next to the answer. An "ACE-NNN" resolves only
    in a private portfolio repo, so to the reader it is an unresolvable reference and to everyone
    else it is a list of work that has not shipped. Which spec owns each gap is a code comment beside
    the constant; the behavioural half is what a user can act on and is the half that ships."""
    markers = [
        rt.UNDETERMINED_COLUMNS, rt.UNDETERMINED_TABLES, rt.UNDETERMINED_JOINS,
        rt.UNDETERMINED_AGGREGATES, rt.UNDETERMINED_NO_PARSER, rt.UNDETERMINED_UNPARSEABLE,
        rt.UNDETERMINED_REFUSED, rt.UNDETERMINED_REFUSED_TABLES,
    ]
    for marker in markers:
        assert not re.search(r"\b[A-Z]{2,}-\d+\b", marker), marker
    # And every section of a real receipt, so a marker added later cannot dodge the list above.
    for name, section in _sections(org).items():
        assert not re.search(r"\b[A-Z]{2,}-\d+\b", section["undetermined"] or ""), name


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
        assert section["undetermined"] == rt.UNDETERMINED_UNPARSEABLE, name


def test_a_missing_parser_reports_every_section_undetermined(org, monkeypatch):
    """The other early return. The vendored plugin mirror runs on whatever python3 the user has, so
    a missing sqlglot is a real deployment, not a hypothetical."""
    monkeypatch.setattr(rt, "_HAVE_SQLGLOT", False)
    sections = _sections(org)
    assert all(
        s == {"items": [], "undetermined": rt.UNDETERMINED_NO_PARSER} for s in sections.values()
    )


def test_the_two_early_returns_do_not_share_one_reason(org, monkeypatch):
    """SC-4. A deployment with no parser and a statement this parser cannot read are different
    facts with different fixes — install sqlglot, or rewrite the statement — and a reader who is
    handed the same sentence for both cannot tell which one they have."""
    unparseable = _sections(org, ";;;")["tables"]["undetermined"]
    monkeypatch.setattr(rt, "_HAVE_SQLGLOT", False)
    no_parser = _sections(org)["tables"]["undetermined"]

    assert unparseable != no_parser
    assert "sqlglot" in no_parser and "sqlglot" not in unparseable


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


# --- determinism across processes -------------------------------------------


def _write_many_ai_columns_model(tmp_path):
    """Six AI-written columns, so the assumptions cap of three has to CHOOSE. With fewer than four
    every candidate survives and the ordering question never arises."""
    yaml = __import__("yaml")
    p = tmp_path / "many"
    (p / "datasources" / "c").mkdir(parents=True)
    (p / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (p / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "many", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (p / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (p / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "wide"}]}))
    (p / "subject_areas" / "s" / "tables" / "wide.yaml").write_text(yaml.safe_dump({
        "name": "wide", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "w",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]
        + [{"name": f"c{i}", "type": "string", "description_source": "ai_unknown"}
           for i in range(6)]}))
    return p


_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from semantic_model import loader as L
from semantic_model import runtime as rt
org = L.load_datasource(sys.argv[2])
r = rt.assemble_receipt(org, "SELECT c0, c1, c2, c3, c4, c5 FROM public.wide")
print(json.dumps([a["column"] for a in r["assumptions"]]))
"""


def test_the_assumptions_cap_picks_the_same_three_in_every_process(tmp_path):
    """REQ-022: the receipt is "the same for the same SQL and model version". The assumptions list is
    concatenated and then capped at three, so an unsorted walk of a set of column tuples lets
    string-hash randomization decide WHICH three a caller is shown. Same process, same seed, so this
    can only be caught across processes: four seeds, four runs, one answer."""
    import subprocess

    root = _write_many_ai_columns_model(tmp_path)
    pkg_src = str(REPO_ROOT / "packages" / "agami-core" / "src")
    seen = set()
    for seed in ("0", "1", "42", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, pkg_src, str(root)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())
    assert len(seen) == 1, f"the cap chose differently across hash seeds: {seen}"
    assert json.loads(seen.pop()) == ["public.wide.c0", "public.wide.c1", "public.wide.c2"]


def _assembled_tables(assembler, org, sql):
    """The `tables` section either assembler produces, so one test can drive both."""
    return assembler(org, sql)["sections"]["tables"]["items"]


@pytest.mark.parametrize("assembler", [rt.assemble_receipt, rt.assemble_refusal_receipt],
                         ids=["full", "bounded"])
def test_the_receipt_and_the_gate_agree_about_a_case_folded_table_name(org, assembler):
    """`check_table_scope` folds case, because Postgres and friends fold unquoted identifiers. The
    receipt's table index did not, so `FROM ORDERS` passed the gate and the receipt then reported the
    table as undeclared. That is the single fact SC-2 promises a refused caller, so the two have to
    give the same answer about the same statement.

    BOTH assemblers, because there are two and the fix reached one. `assemble_refusal_receipt` kept
    computing `declared` from the RAW name against an index keyed by `_tkey`, and it is the one that
    matters most: on a refusal, `declared` is the ONLY model fact the caller is given.
    `SELECT ref_no FROM ORDERS` refuses with `column_scope` — so the table gate resolved `ORDERS`
    fine — while its receipt said `{"ref": "ORDERS", "declared": false}`.
    """
    sql = "SELECT id FROM ORDERS"

    assert rt.check_table_scope(sql, org) is None, "the gate accepts the folded spelling"

    tables = _assembled_tables(assembler, org, sql)
    assert [t["ref"] for t in tables] == ["ORDERS"], "the reference is echoed as the caller wrote it"
    assert tables[0]["declared"] is True


def test_the_full_receipt_resolves_a_case_folded_reference_to_the_models_own_spelling(org):
    """The half only the full receipt has. The bounded one never resolves a name at all, which is
    the point of it."""
    tables = _sections(org, "SELECT id FROM ORDERS")["tables"]["items"]
    assert tables[0]["qname"] == "public.orders"


def test_a_case_folded_statement_does_not_deny_a_relationship_the_model_declares(org):
    """The sibling lookup, and the reason it is worse than the table one. The relationship walk
    compared the model's own names against the UNFOLDED set of names the statement wrote, so a folded
    statement reported both tables in scope AND no declared join between them. That pair is not an
    admitted gap — the section's marker is about the PREDICATE, not about existence — it is the
    receipt stating that the model declares no relationship where the model declares one."""
    folded = ("SELECT c.id, SUM(amount) AS total FROM ORDERS o "
              "JOIN CUSTOMERS c ON o.customer_id = c.id GROUP BY c.id")
    sections = _sections(org, folded)

    assert all(t["declared"] for t in sections["tables"]["items"]), "both tables are in scope"
    assert [j["name"] for j in sections["joins"]["items"]] == ["orders_to_customers"]
