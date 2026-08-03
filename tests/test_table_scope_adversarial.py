"""Adversarial suite for the table-scope guard.

Each case is an attempt to reach a table the model does not declare by a route other than
naming it in a bare `FROM`. `test_table_scope_gate.py` is the happy path plus the documented
degrades; a green happy-path suite does NOT prove the gate holds against evasion — these do.
The column-scope and star gates have had this since #93; table scope had nothing, which is
what ACE-096 found.

Cases fall into three groups, mirroring `test_column_scope_adversarial.py`:

  * scope evasion               -> must refuse (rule: table_scope)
  * documented accepted allow   -> asserts *allow*, so a future narrowing of the boundary is
                                   a conscious, test-breaking decision
  * upstream-owned              -> asserts a different layer already catches it

**These lock, they do not close.** Every refusal below already holds on the base commit, and
that is the point: this gate is the whole of principle 4b's table half and had no statement of
intended behaviour for a reviewer to check a change against. The one route that genuinely does
NOT hold today is dialect-specific quoting, and it lives in `test_dialect_quoting_gaps.py` as
expected-fail rather than here, so the two kinds of case are never confused for each other.

The gate returns `guardrail.Refusal | None`, so "must refuse" reads `is not None` and "accepted
allow" reads `is None`. The rule id is asserted on every refusal: an evasion caught by the WRONG
gate would otherwise pass as if this one held.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _scope_org():
    """Org declaring orders(id, amount, customer_id) + customers(id, name). `secret` is not
    declared and is the table every evasion below is trying to reach."""
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"), ("customer_id", "integer")])
    customers = _t("customers", [("id", "integer"), ("name", "string")])
    return m.Datasource(datasource="Shop",
                        subject_areas=[m.SubjectArea(name="sales",
                                                     tables_defined=[orders, customers])])


def _assert_tables_refused(refusal, *tables: str) -> None:
    """The refusal names exactly `tables` and nothing else.

    Exact rather than substring, for the same reason the column-scope helper is: an `in` check
    would also pass for a refusal that had listed the tables the model DOES declare, which is
    precisely the enumeration the contract forbids.
    """
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert refusal.detail == ("query references table(s) not in the semantic model: "
                              + ", ".join(tables)
                              + " — only tables declared in the model may be queried.")


# ===========================================================================
# Scope evasion -> must refuse
# ===========================================================================

# Alias indirection: the undeclared table is named once and referenced by a short alias
# everywhere after, so a walk that judged the REFERENCES rather than the sources would miss it.
ALIAS_INDIRECTION = [
    ("SELECT s.id FROM secret s", "secret"),
    ("SELECT s.id FROM secret AS s", "secret"),
    ("SELECT o.id FROM orders o JOIN secret s ON o.id = s.id", "secret"),
    ("SELECT o.id FROM orders o LEFT JOIN secret AS s ON o.id = s.id", "secret"),
]


@pytest.mark.parametrize("sql,offender", ALIAS_INDIRECTION)
def test_alias_indirection_refused(sql, offender):
    _assert_tables_refused(rt.check_table_scope(sql, _scope_org()), offender)


# Nesting: the reach is one or more levels down, where an outer-query-only walk would not look.
NESTED_SOURCES = [
    ("WITH t AS (SELECT id FROM secret) SELECT id FROM t", "secret"),
    ("WITH a AS (SELECT 1 AS id), b AS (SELECT id FROM secret) SELECT a.id FROM a, b", "secret"),
    ("SELECT id FROM (SELECT id FROM secret) x", "secret"),
    ("SELECT id FROM orders WHERE id IN (SELECT id FROM secret)", "secret"),
    ("SELECT (SELECT MAX(id) FROM secret) AS m FROM orders", "secret"),
    ("SELECT id FROM (SELECT id FROM (SELECT id FROM secret) y) x", "secret"),
]


@pytest.mark.parametrize("sql,offender", NESTED_SOURCES)
def test_undeclared_table_nested_in_any_source_refused(sql, offender):
    _assert_tables_refused(rt.check_table_scope(sql, _scope_org()), offender)


# Set operations: an arm that is not the first one. This is the shape that bypassed the gate
# before #93 — `parse_one` yields `exp.Union`, which is not an `exp.Select`, so a walk gated on
# "is a SELECT" saw nothing to check.
SET_OPERATION_ARMS = [
    ("SELECT id FROM orders UNION SELECT id FROM secret", "secret"),
    ("SELECT id FROM orders UNION ALL SELECT id FROM secret", "secret"),
    ("SELECT id FROM orders INTERSECT SELECT id FROM secret", "secret"),
    ("SELECT id FROM orders EXCEPT SELECT id FROM secret", "secret"),
    ("SELECT id FROM orders UNION SELECT id FROM customers UNION SELECT id FROM secret", "secret"),
]


@pytest.mark.parametrize("sql,offender", SET_OPERATION_ARMS)
def test_undeclared_table_in_a_set_operation_arm_refused(sql, offender):
    _assert_tables_refused(rt.check_table_scope(sql, _scope_org()), offender)


def test_postgres_quoted_identifier_refused():
    # A double-quoted identifier is the one quoting form the generic dialect reads correctly,
    # so quoting an undeclared name does not hide it. The backtick and bracket forms DO hide it
    # today — those are in test_dialect_quoting_gaps.py, not here.
    _assert_tables_refused(rt.check_table_scope('SELECT id FROM "secret"', _scope_org()), "secret")


def test_case_variation_does_not_evade():
    # Unquoted identifiers fold case, so the match is case-insensitive in both directions —
    # an undeclared table cannot be smuggled past by shouting it.
    _assert_tables_refused(rt.check_table_scope("SELECT id FROM SECRET", _scope_org()), "SECRET")
    _assert_tables_refused(rt.check_table_scope("SELECT id FROM SeCrEt", _scope_org()), "SeCrEt")


def test_schema_qualification_does_not_evade():
    # Matching is on the bare table name, so prefixing a schema does not create a new name.
    _assert_tables_refused(
        rt.check_table_scope("SELECT id FROM private.secret", _scope_org()), "secret")


def test_every_undeclared_table_is_named_not_just_the_first():
    # A caller fixing one name at a time would otherwise need as many round trips as it had
    # undeclared tables, and would reasonably read the first refusal as the whole problem.
    _assert_tables_refused(
        rt.check_table_scope(
            "SELECT a.id FROM secret a JOIN vault b ON a.id = b.id", _scope_org()),
        "secret", "vault")


def test_the_declared_surface_is_never_enumerated():
    # The refusal echoes what the caller sent and nothing else. A refusal that named the
    # alternatives would be a schema-listing endpoint for anyone who could send one bad query.
    refusal = rt.check_table_scope("SELECT id FROM secret", _scope_org())
    assert refusal is not None
    for declared in ("orders", "customers"):
        assert declared not in refusal.detail
        assert declared not in refusal.remediation


# ===========================================================================
# Documented accepted allows -> the intended boundary
# ===========================================================================

def test_cte_name_shadowing_a_declared_table_is_allowed():
    # A CTE name is not a table: it names a result the statement defines for itself. Subtracting
    # the WITH-bound names is what keeps a legitimate `WITH orders AS (...)` from being read as a
    # reach, and the body of that CTE is still walked (see the nested cases above).
    assert rt.check_table_scope(
        "WITH orders AS (SELECT 1 AS id) SELECT id FROM orders", _scope_org()) is None


def test_derived_table_alias_is_not_a_table():
    assert rt.check_table_scope(
        "SELECT x.id FROM (SELECT id FROM orders) x", _scope_org()) is None


def test_a_model_declaring_no_tables_allows():
    # Nothing to scope against. Stated as a test because it is a fail-open, and a fail-open that
    # nobody wrote down is indistinguishable from a gate that stopped working.
    empty = m.Datasource(datasource="Empty", subject_areas=[])
    assert rt.check_table_scope("SELECT id FROM anything", empty) is None


# ===========================================================================
# Upstream-owned -> a different layer catches it
# ===========================================================================

def test_multistatement_stacking_caught_by_read_only_guard():
    # `SELECT id FROM orders; SELECT id FROM secret` never reaches this gate — the read-only
    # guard refuses a second statement first. Asserted here so the absence of a table-scope
    # refusal for it is a recorded division of labour rather than a hole.
    import sql_guard
    assert sql_guard.check_read_only("SELECT id FROM orders; SELECT id FROM secret") is not None
