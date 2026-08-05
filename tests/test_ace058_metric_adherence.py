"""ACE-058 — the `columns` section says which declared metric each OUTPUT COLUMN computes.

The section used to match metrics against the whole STATEMENT by substring containment
(`_norm_sql`), which was wrong in three directions at once: a matched metric was an entry with no
owning column, a column that matched nothing was absent rather than reported, and containment
credited text that never reached the output — a binding inside a CTE body counted for the statement
that merely read the CTE.

What this slice pins:

  * the comparison is STRUCTURAL and normalized identically on both sides, so a table qualifier the
    statement wrote does not defeat a match while a `FILTER` clause the binding declares does;
  * a string literal's case is NOT folded, which is the `_norm_sql` defect stated as a test;
  * one binding per metric, the one declared for this deployment's engine;
  * a metric declaring no binding for this engine is not a candidate, while one whose binding will
    not parse is carried as unread — the two are different facts and only the second holds a column
    open.

The model is this battery's own. No governance fixture declares a metric with a `FILTER` binding or
a cross-area metric, and adding either to a shared fixture would move every receipt the ACE-088 /
094 / 060 batteries assert on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

# --- fixture ----------------------------------------------------------------

SIGNED_OFF = {"review_state": "approved", "signed_off_by": "you@example.com",
              "signed_off_role": "data_owner", "signed_off_at": "2026-01-01T00:00:00Z"}

# The binding SC-2 is written against. It differs from a bare `SUM(total_amount)` by a FILTER clause
# — the two select different rows, so reporting the bare form as this metric would bless a number
# the organisation's definition does not produce. Containment does exactly that, because the bare
# form is a substring of this one.
PAID_REVENUE = "SUM(total_amount) FILTER (WHERE status = 'paid')"


def _write_model(root: Path, *, storage_type: str = "PostgreSQL") -> None:
    """One area `s` (orders, customers) plus an org-level cross-area metric.

    The metrics are chosen so each one is the only way to reach a branch:

      * `paid_revenue` — signed off, FILTER binding: the positive match, SC-2's rejection, and the
        sign-off block a `matched` item carries;
      * `order_count` — `unreviewed`: sign-off is a FIELD on the item, not a filter on which
        metrics are considered, so this one has to match too;
      * `active_customers` — declared org-level as a CROSS-AREA metric, which a walk over
        `org.subject_areas` cannot see however correct its matching is;
      * `broken_metric` — a binding declared for this engine that will not parse, which is OUR gap;
      * `elsewhere_only` — a binding declared for a DIFFERENT engine only, which is the model's
        coverage rather than our gap. The two must not be confused.
    """
    yaml = __import__("yaml")
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "storage_type": storage_type}],
        "subject_areas": ["subject_areas/s"],
        "cross_subject_area_metrics": [{
            "name": "active_customers",
            "calculation": "Count of distinct customers with an order.",
            "bindings": {storage_type: "COUNT(DISTINCT customer_id)"},
            "source_tables": ["orders"], "confidence": "confirmed", **SIGNED_OFF}]}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s",
        "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                   for t in ("orders", "customers")]}))

    def _table(name: str, columns: list[dict]) -> None:
        (root / "subject_areas" / "s" / "tables" / f"{name}.yaml").write_text(yaml.safe_dump({
            "name": name, "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": name, "columns": columns}))

    _table("orders", [{"name": "id", "type": "integer", "primary_key": True},
                      {"name": "customer_id", "type": "integer"},
                      {"name": "status", "type": "string"},
                      {"name": "total_amount", "type": "decimal"}])
    _table("customers", [{"name": "id", "type": "integer", "primary_key": True},
                         {"name": "email", "type": "string", "sensitive": True}])

    def _metric(name: str, calculation: str, bindings: dict, **kw) -> None:
        doc = {"name": name, "calculation": calculation, "bindings": bindings,
               "source_tables": ["orders"], "description": name}
        doc.update(kw)
        (root / "subject_areas" / "s" / "metrics" / f"{name}.yaml").write_text(yaml.safe_dump(doc))

    _metric("paid_revenue", "Sum of order amounts for paid orders.",
            {storage_type: PAID_REVENUE},
            other_names=["revenue"], confidence="confirmed", **SIGNED_OFF)
    _metric("order_count", "Count of order rows.", {storage_type: "COUNT(*)"},
            confidence="proposed", review_state="unreviewed")
    _metric("broken_metric", "Deliberately unparseable.",
            {storage_type: "SUM(total_amount"}, confidence="proposed",
            review_state="unreviewed")
    _metric("elsewhere_only", "Declared for another engine only.",
            {"Snowflake": "SUM(total_amount)"}, confidence="proposed",
            review_state="unreviewed")


@pytest.fixture()
def org(tmp_path):
    root = tmp_path / "p"
    root.mkdir(parents=True)
    _write_model(root)
    return L.load_datasource(root)


def _candidates(org) -> dict:
    """The candidate list keyed by metric name — what the matcher gets to compare against."""
    return {c.metric.name: c
            for c in rt._metric_candidates(org, rt._storage_type_of(org), rt._dialect_of(org)[0])}


def _matches(sql_expr: str, binding: str, dialect: str = "postgres") -> bool:
    """Whether one written expression and one declared binding compare equal.

    Goes through the same two reductions the section uses rather than re-deriving them here: a test
    that normalized its own way would pass while the section failed.
    """
    import sqlglot

    written = sqlglot.parse_one(f"SELECT {sql_expr}", read=dialect).expressions[0]
    if isinstance(written, sqlglot.exp.Alias):
        written = written.this
    reduced = rt._reduced_binding(binding, dialect)
    return reduced is not None and rt._reduced_projection(written, dialect) == reduced


# --- SC-2: what the structural comparison rejects ---------------------------


def test_a_bare_sum_does_not_match_a_filtered_binding():
    """SC-2. The alias would agree and containment does agree; only the structure decides.

    `SUM(total_amount)` is a substring of the declared binding, which is exactly why the test this
    replaced reported it as the org's `revenue`. The two select different rows.
    """
    assert not _matches("SUM(total_amount)", PAID_REVENUE)


def test_a_string_literals_case_is_not_folded():
    """The other half of the `_norm_sql` defect: it lowercased the WHOLE string, literals included,
    so a declared `'paid'` matched a written `'PAID'`. Those select different rows in Postgres."""
    assert not _matches("SUM(total_amount) FILTER (WHERE status = 'PAID')", PAID_REVENUE)
    assert _matches("SUM(total_amount) FILTER (WHERE status = 'paid')", PAID_REVENUE)


def test_a_different_column_does_not_match():
    assert not _matches("SUM(id) FILTER (WHERE status = 'paid')", PAID_REVENUE)


# --- SC-3 / SC-4: what it accepts -------------------------------------------


def test_the_declared_binding_written_verbatim_matches():
    assert _matches(PAID_REVENUE, PAID_REVENUE)


def test_a_table_qualifier_does_not_defeat_a_match():
    """SC-4. A binding is written unqualified because the model author names a column on a declared
    table; a generated statement writes `o.total_amount`. Measured on the live testbed: this exact
    shape matched NOTHING under containment, on the query that most plainly uses the definition."""
    assert _matches("SUM(o.total_amount) FILTER (WHERE o.status = 'paid')", PAID_REVENUE)


def test_an_unquoted_identifiers_case_is_folded():
    """Unquoted identifiers fold case in Postgres and friends, so these are the same column."""
    assert _matches("SUM(O.TOTAL_AMOUNT) FILTER (WHERE O.STATUS = 'paid')", PAID_REVENUE)


def test_whitespace_and_line_breaks_do_not_decide_a_match():
    assert _matches("SUM(total_amount)\n  FILTER (WHERE status = 'paid')", PAID_REVENUE)


# --- which binding is read --------------------------------------------------


def test_only_this_engines_binding_is_a_candidate(org):
    """A metric declaring a binding for another engine only is absent from the candidate list.

    That is a fact about the MODEL's coverage, not a declaration we failed to read — so it is not a
    candidate and, per the caller's rule, does not hold any column open. The containment test this
    replaced tried every value in `bindings`, so a Snowflake binding could be reported as the
    definition a Postgres statement used.
    """
    cands = _candidates(org)
    assert "elsewhere_only" not in cands
    assert {"paid_revenue", "order_count", "broken_metric", "active_customers"} <= set(cands)


def test_a_cross_subject_area_metric_is_a_candidate(org):
    """SC-7. The walk this replaced read only each area's `metrics`, so a declared cross-area metric
    could never match however plainly the statement computed it."""
    assert _candidates(org)["active_customers"].area is None


def test_a_binding_that_will_not_parse_is_carried_as_unread(org):
    """Carried with `reduced is None` rather than dropped. Dropping it would make "no metric
    matched" reachable off our own failure to read the model, which is the defect ACE-059's review
    found three times over in the joins section."""
    cands = _candidates(org)
    assert cands["broken_metric"].reduced is None
    assert cands["paid_revenue"].reduced is not None


def test_an_unresolvable_engine_yields_no_candidates(tmp_path):
    """No declared connection means we do not know which engine's binding to read, and a guess is
    not available to a receipt. `resolve_datasource_dialect` refuses the same two shapes."""
    yaml = __import__("yaml")
    root = tmp_path / "noengine"
    root.mkdir(parents=True)
    _write_model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    doc["storage_connections"] = []
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))
    org = L.load_datasource(root)
    assert rt._storage_type_of(org) is None
    assert rt._metric_candidates(org, rt._storage_type_of(org), None) == []


# --- cost -------------------------------------------------------------------


def test_a_binding_is_reduced_once_and_not_once_per_request(org):
    """The cache is the cost guard, not a nicety. This runs on the `ok` path of every executed query
    against every metric the deployment declares, and each reduction is a sqlglot parse of text that
    has not changed since the process started. ACE-059 was measured at 10.6x `main` before its own
    reduction was cached."""
    rt._reduced_binding.cache_clear()
    for _ in range(5):
        rt._metric_candidates(org, rt._storage_type_of(org), rt._dialect_of(org)[0])
    info = rt._reduced_binding.cache_info()
    # Four metrics declare a binding for this engine; each is parsed once and read from the cache
    # every time after.
    assert info.misses == 4
    assert info.hits >= 16
