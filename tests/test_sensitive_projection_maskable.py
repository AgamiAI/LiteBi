"""ACE-041 slice 1: the sensitive-projection guard must, in addition to its existing
refuse decision, expose per-offending-projection *traceability* that a later masking slice
consumes — whether the projection is a 1:1 image of a single OUTPUT column (MASKABLE, with its
0-based output index) or has the sensitive value buried in an expression / function / scalar
subquery / star expansion (MUST-REFUSE). This slice adds NO runtime behaviour change: a sensitive
projection is still refused exactly as today; the maskable/index info is latent, exercised here.

Fixture style mirrors tests/test_guard_context.py::_org (an inline Organization). Synthetic,
generic names only — `customers`, `orders`, `archived_customers`, `x` — no real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _org() -> "m.Datasource":
    """One org with two sensitive columns on `customers` (`ssn`, `email`), a second sensitive-`ssn`
    table (`archived_customers`) for set-operation arms, a non-sensitive `orders` for joins, and a
    table `x` that carries a column literally named `ssn` which is NOT sensitive (the same-name
    decoy)."""
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="dept", type="string"),
            m.Column(name="ssn", type="string", sensitive=True),
            m.Column(name="email", type="string", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="cust_id", type="integer"),
        ],
    )
    archived = m.Table(
        name="archived_customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="ssn", type="string", sensitive=True),
        ],
    )
    # `x.ssn` is a column literally named `ssn` on a DIFFERENT, non-sensitive table — the decoy the
    # guard must NOT flag when it is the resolved source.
    x = m.Table(
        name="x", schema="public", storage_connection="c", grain=["ssn"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="ssn", type="string"),
        ],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders, archived, x])
    return m.Datasource(datasource="AcmeCorp", version=1, subject_areas=[sa])


def _classify(sql: str) -> tuple["rt.SensitiveCheckResult", list, list]:
    """Run the guard and split its projections into (result, maskable[(index, column)], refuse[column])."""
    res = rt.check_sensitive_projection(sql, _org())
    maskable = sorted((p.output_index, p.column) for p in res.projections if p.maskable)
    refuse = sorted(p.column for p in res.projections if not p.maskable)
    return res, maskable, refuse


# ---------------------------------------------------------------------------
# Maskable: a bare col / t.col / either through a simple AS alias -> 1:1 output column.
# Each asserts the classification AND the exact 0-based output index.
# ---------------------------------------------------------------------------

MASKABLE_CASES = [
    ("SELECT ssn FROM customers", [(0, "customers.ssn")]),
    ("SELECT c.ssn FROM customers c", [(0, "customers.ssn")]),
    ("SELECT c.ssn AS taxid FROM customers c", [(0, "customers.ssn")]),
    ("SELECT id, ssn, name FROM customers", [(1, "customers.ssn")]),
    ("SELECT ssn, email FROM customers", [(0, "customers.ssn"), (1, "customers.email")]),
    ("SELECT ssn, ssn FROM customers", [(0, "customers.ssn"), (1, "customers.ssn")]),
    ("SELECT c.ssn FROM customers c JOIN orders o ON o.cust_id = c.id", [(0, "customers.ssn")]),
    ("SELECT ssn FROM customers ORDER BY ssn", [(0, "customers.ssn")]),
    ("SELECT ssn FROM customers LIMIT 5", [(0, "customers.ssn")]),
    # A derived table whose only real table in scope is `customers`, so the bare `ssn` resolves.
    ("SELECT ssn FROM (SELECT ssn FROM customers) t", [(0, "customers.ssn")]),
    # A CTE puts BOTH the cte name `t` and the physical `customers` into flat scope, so the bare
    # column can't be pinned to one physical table -> the ref falls back to the bare name, but the
    # PROJECTION is still a 1:1 image of output column 0, so it stays maskable at index 0.
    ("WITH t AS (SELECT ssn FROM customers) SELECT ssn FROM t", [(0, "ssn")]),
    # Every set-operation arm is walked; each arm's `ssn` is maskable at its own index 0.
    ("SELECT ssn FROM customers UNION ALL SELECT ssn FROM archived_customers",
        [(0, "archived_customers.ssn"), (0, "customers.ssn")]),
    ("SELECT ssn FROM customers INTERSECT SELECT ssn FROM archived_customers",
        [(0, "archived_customers.ssn"), (0, "customers.ssn")]),
    ("SELECT ssn FROM customers EXCEPT SELECT ssn FROM archived_customers",
        [(0, "archived_customers.ssn"), (0, "customers.ssn")]),
]


@pytest.mark.parametrize("sql, expected", MASKABLE_CASES)
def test_maskable_with_output_index(sql, expected):
    res, maskable, refuse = _classify(sql)
    # Runtime behaviour is unchanged: a sensitive projection is still refused.
    assert res.action == "refuse", sql
    # Nothing in these queries must-refuses — every offending projection is deterministically maskable.
    assert refuse == [], sql
    assert maskable == sorted(expected), sql


# ---------------------------------------------------------------------------
# Must-refuse: sensitive value buried in an expression / function / scalar subquery.
# These have NO deterministic 1:1 output column, so they are never maskable.
# ---------------------------------------------------------------------------

MUST_REFUSE_CASES = [
    ("SELECT UPPER(ssn) FROM customers", ["customers.ssn"]),
    ("SELECT ssn || '-x' FROM customers", ["customers.ssn"]),
    ("SELECT SUBSTR(ssn, 1, 3) FROM customers", ["customers.ssn"]),
    ("SELECT CASE WHEN id > 0 THEN ssn END FROM customers", ["customers.ssn"]),
    ("SELECT COALESCE(ssn, '') FROM customers", ["customers.ssn"]),
    ("SELECT (SELECT ssn FROM customers LIMIT 1) AS s", ["customers.ssn"]),
    # A sensitive column wrapped in an expression in ONE arm of a set operation makes the WHOLE
    # query must-refuse (no arm is maskable, so a later slice cannot mask its way to a safe answer).
    ("SELECT UPPER(ssn) FROM customers UNION ALL SELECT id FROM customers", ["customers.ssn"]),
]


@pytest.mark.parametrize("sql, expected", MUST_REFUSE_CASES)
def test_must_refuse_never_maskable(sql, expected):
    res, maskable, refuse = _classify(sql)
    assert res.action == "refuse", sql
    assert maskable == [], sql
    assert refuse == sorted(expected), sql


def test_star_is_must_refuse_for_every_sensitive_column():
    """`SELECT *` expands a whole table; its output columns can't be pinned to a single
    deterministic index, so each sensitive column is must-refuse (fail-closed), never maskable."""
    res, maskable, refuse = _classify("SELECT * FROM customers")
    assert res.action == "refuse"
    assert maskable == []
    assert refuse == ["customers.email", "customers.ssn"]


# ---------------------------------------------------------------------------
# No offending projection -> allow, and nothing to mask or refuse.
# ---------------------------------------------------------------------------

ALLOW_CASES = [
    "SELECT COUNT(ssn) FROM customers",
    "SELECT COUNT(DISTINCT ssn) FROM customers",
    "SELECT dept, COUNT(*) FROM customers WHERE ssn = :x GROUP BY dept",
    "SELECT COUNT(*) FROM customers c JOIN x ON c.ssn = x.ssn",
    "SELECT name FROM customers ORDER BY ssn",
]


@pytest.mark.parametrize("sql", ALLOW_CASES)
def test_allowed_queries_report_nothing(sql):
    res, maskable, refuse = _classify(sql)
    assert res.action == "allow", sql
    assert res.projections == [], sql
    assert maskable == [] and refuse == [], sql


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------

def test_schema_qualified_projection_is_maskable():
    res, maskable, refuse = _classify("SELECT public.customers.ssn FROM public.customers")
    assert res.action == "refuse"
    assert refuse == []
    assert maskable == [(0, "customers.ssn")]


def test_same_named_nonsensitive_column_on_other_table_not_flagged():
    """`x.ssn` is literally named `ssn` but is NOT sensitive, so projecting it is allowed."""
    res, maskable, refuse = _classify("SELECT ssn FROM x")
    assert res.action == "allow"
    assert res.projections == []


@pytest.mark.parametrize("sql", [
    'SELECT "SSN" FROM customers',   # quoted upper-case
    "SELECT SSN FROM customers",     # unquoted upper-case
    "SELECT Ssn FROM customers",     # mixed case
    "SELECT c.SSN FROM customers c", # qualified upper-case
])
def test_case_insensitive_sensitive_name_is_now_masked(sql):
    """ACE-041 slice 3 (behaviour CHANGE — was `allow` before this slice): the sensitive-name match
    is now CASE-INSENSITIVE, consistent with the table/column-scope gates (which fold). An upper /
    mixed / quoted spelling of a lower-case declared `ssn` therefore matches and is a maskable
    projection at output index 0 — this masks/refuses MORE, closing the `SELECT "SSN"` PII bypass.
    The declared ref (`customers.ssn`) names the model column regardless of the query's casing."""
    res, maskable, refuse = _classify(sql)
    assert res.action == "refuse", sql
    assert refuse == [], sql
    assert maskable == [(0, "customers.ssn")], sql


def test_case_insensitive_decoy_on_nonsensitive_table_still_not_flagged():
    """The case-fold must not over-reach: `x.ssn` is a same-named column on a NON-sensitive table, so
    an upper-case `SELECT SSN FROM x` is still allowed (it binds to `x`, which does not declare it
    sensitive)."""
    res, maskable, refuse = _classify("SELECT SSN FROM x")
    assert res.action == "allow"
    assert res.projections == []


def test_unparseable_sql_reports_nothing_and_never_maskable():
    """An unparseable statement degrades to allow (the guard's documented posture) with an empty
    projections list — so fail-closed's 'never maskable' holds (nothing is offered as maskable)."""
    res, maskable, refuse = _classify("NOT SQL AT ALL ;;;")
    assert res.action == "allow"
    assert res.projections == []


# ---------------------------------------------------------------------------
# Backwards-compat + ctx parity: the legacy fields are unchanged and the new
# maskable/index info is identical with or without a shared GuardContext.
# ---------------------------------------------------------------------------

def test_legacy_fields_unchanged():
    res = rt.check_sensitive_projection("SELECT ssn FROM customers", _org())
    assert res.action == "refuse"
    assert res.columns == ["customers.ssn"]
    assert "customers.ssn" in res.reason
    assert res.suggestion is not None


def test_maskable_index_is_ctx_invariant():
    org = _org()
    sql = "SELECT id, ssn, name FROM customers"
    ctx = rt.build_guard_context(sql, org)
    no_ctx = rt.check_sensitive_projection(sql, org)
    with_ctx = rt.check_sensitive_projection(sql, org, ctx=ctx)
    assert no_ctx.as_dict() == with_ctx.as_dict()
    assert [(p.output_index, p.column, p.maskable) for p in with_ctx.projections] == [
        (1, "customers.ssn", True)
    ]
