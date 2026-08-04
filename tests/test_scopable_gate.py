"""The scopability gate: a statement that parses perfectly and still cannot be checked is refused.

**This is the other half of a split the contract makes deliberately.** `unparseable` is a statement
sqlglot cannot read at all and belongs to `check_readable`; `unscopable` is one that reads fine and
offers the scope walk nothing to accept or reject — a table function, `ROWS FROM`, `VALUES`,
`UNNEST`, `LATERAL`. Collapsing them into one rule would make the remediation a guess, because
"re-emit the query" is useless advice to someone whose query parsed.

**Why neither existing gate catches these, which is the whole reason this file exists.** ACE-079's
readability backstop refuses a statement resolving to NO named table. A table function does not have
that shape: sqlglot parses it to an `exp.Table` whose *name* is empty, with the function in `.this`,
so `find(exp.Table) is None` sees a table and passes. `check_table_scope` then skips the very same
node — `if not name ... continue` is how it lets a CTE reference through — so the node no gate can
name is the node both gates decline to judge. And one declared table leading a comma-join satisfies
the backstop while every source after the comma goes unexamined.

Measured before this gate existed: of the eight constructs below, six reached the database with
`check_readable`, `check_table_scope` and `check_column_scope` all silent. The two that did refuse
did so incidentally, via the zero-named-table backstop, which is a backstop rather than a diagnosis.

**Every assertion names the rule and the reason.** A suite asserting only "something refused" would
have read green while the wrong gate fired for the wrong reason — the failure mode
`test_parse_fidelity_gaps.py` was written to prevent, kept here.
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


def _scope_org(engine: str = "PostgreSQL"):
    """orders(id, amount) + customers(id, name), on a declared engine.

    The engine is declared because the readability gate refuses a datasource that names none, and a
    statement refused there would never reach this gate — the assertions below would then be pinning
    ACE-079's refusal while reading as though they pinned this one.
    """
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type="integer") for c in cols])
    return m.Datasource(
        datasource="Shop",
        storage_connections=[m.StorageConnection(name="c", storage_type=engine)],
        subject_areas=[m.SubjectArea(name="sales",
                                     tables_defined=[_t("orders", ["id", "amount"]),
                                                     _t("customers", ["id", "name"])])])


def _assert_unscopable(refusal) -> None:
    """The refusal is 4c, carries the contract's rule, and names a fix without listing the model.

    `reason` is asserted against the enum's own mapping rather than a literal, so a future edit that
    re-pinned the rule to `unsafe` or `out_of_scope` fails here rather than passing a hard-coded
    string that was copied from the same edit.
    """
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_UNSCOPABLE
    assert refusal.reason == "undetermined"
    assert refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_UNSCOPABLE]
    assert refusal.remediation
    # Echo, never enumerate: a refusal that lists the declared tables is a schema-listing endpoint
    # reachable by one deliberately-wrong query.
    assert "orders" not in refusal.detail and "customers" not in refusal.detail
    assert "orders" not in refusal.remediation and "customers" not in refusal.remediation


# ===========================================================================
# The refuse-class
# ===========================================================================

UNSCOPABLE = [
    pytest.param("SELECT g FROM generate_series(1, 10) AS t(g)", id="table-function"),
    pytest.param("SELECT a FROM ROWS FROM (generate_series(1,3)) AS t(a)", id="rows-from"),
    pytest.param("SELECT x FROM (VALUES (1), (2)) AS v(x)", id="values"),
    pytest.param("SELECT x FROM ((VALUES (1), (2))) AS v(x)", id="values-wrapped-in-a-subquery"),
    pytest.param("SELECT x FROM UNNEST(ARRAY[1,2]) AS t(x)", id="unnest"),
    pytest.param("SELECT o.id FROM orders o, LATERAL (SELECT 1 AS a) l", id="lateral"),
]


@pytest.mark.parametrize("sql", UNSCOPABLE)
def test_an_unscopable_source_is_refused(sql):
    _assert_unscopable(rt.check_scopable(sql, _scope_org()))


def test_the_hive_lateral_view_spelling_is_refused_on_a_hive_family_engine():
    """`LATERAL VIEW` is Hive's spelling, so it is asserted on Databricks rather than on the
    PostgreSQL default.

    Both dialects happen to parse it today, so this passed either way — but on PostgreSQL it was
    passing because sqlglot is lenient about a construct that engine does not have, not because the
    guard read the statement in its own grammar. A vector whose premise is an accident tells you
    nothing the day the accident stops. It also exercises the half of the LATERAL sweep the Postgres
    spelling does not: sqlglot hangs `LATERAL VIEW` off the Select rather than under a From/Join.
    """
    sql = "SELECT x FROM orders LATERAL VIEW explode(arr) t AS x"
    _assert_unscopable(rt.check_scopable(sql, _scope_org("Databricks")))


def test_a_values_arm_of_a_set_operation_is_refused():
    """Found by review on this PR, and it executed against a real Postgres with all three gates
    silent — `check_scopable`, `check_readable` and `check_table_scope` each returned `None`.

    A parenthesized `VALUES` used as a set-operation arm is **not a FROM/JOIN source**: sqlglot
    hangs it off the `Union` beside the select, so a walk over `From`/`Join` sources never reaches
    it — while it contributes rows to the result exactly as an arm reading a table would. This is
    why `VALUES` is a whole-tree sweep rather than a case in the source walk.
    """
    _assert_unscopable(rt.check_scopable("SELECT id FROM orders UNION ALL (VALUES (1))",
                                         _scope_org()))


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT o.id FROM orders o JOIN (VALUES (1),(2)) AS v(x) ON true", id="join"),
    pytest.param("SELECT o.id FROM orders o CROSS JOIN (VALUES (1)) AS v(x)", id="cross-join"),
    pytest.param("SELECT o.id FROM orders o WHERE o.id IN (SELECT x FROM (VALUES (1)) AS v(x))",
                 id="in-subquery"),
    pytest.param("SELECT o.id FROM orders o JOIN (SELECT * FROM (VALUES (1)) AS v(x)) s ON true",
                 id="nested-in-a-derived-table"),
    pytest.param("WITH v AS (SELECT * FROM (VALUES (1)) AS t(x)) SELECT o.id FROM orders o "
                 "JOIN v ON true", id="cte-body"),
    pytest.param("SELECT o.id FROM orders o JOIN UNNEST(ARRAY[1,2]) AS u(x) ON true",
                 id="unnest-join"),
])
def test_every_placement_of_a_row_building_source_is_refused(sql):
    """One declared table plus a row-building source, in each place SQL lets you put one. The gate
    must not depend on which of them sqlglot chose to represent as a `Subquery` wrapper, which
    varies by shape and by version."""
    _assert_unscopable(rt.check_scopable(sql, _scope_org()))


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT o.id FROM orders o, (VALUES (1),(2)) AS v(x)", id="values"),
    pytest.param("SELECT o.id FROM orders o, UNNEST(ARRAY[1,2]) AS u(x)", id="unnest"),
    pytest.param("SELECT o.id FROM orders o, generate_series(1,10) AS t(g)", id="table-function"),
])
def test_a_declared_table_does_not_shield_a_later_comma_join_source(sql):
    """The leading source is `orders`, which is declared, and that is the point.

    This is where the readability backstop stops helping: the statement resolves to a named table,
    so `find(exp.Table) is None` is false and it passes. Whether the extra sources of a comma-join
    normalize into a `Join` or hang off `From.expressions` varies by sqlglot version, so the walk
    reads both and neither shape can hide a source behind a valid one.
    """
    _assert_unscopable(rt.check_scopable(sql, _scope_org()))


def test_an_unscopable_source_in_a_set_operation_arm_is_refused():
    """A table function hidden in the second `UNION` arm, behind a first arm that is entirely
    declared. The walk is whole-tree rather than per-select for exactly this: a gate that judged
    only the outermost select would return the right verdict for the wrong half of the statement."""
    sql = "SELECT id FROM orders UNION SELECT g FROM generate_series(1,3) AS t(g)"
    _assert_unscopable(rt.check_scopable(sql, _scope_org()))


def test_an_unscopable_source_nested_in_a_subquery_is_refused():
    """A derived subquery is a legal source, so the gate descends into one rather than accepting it
    on sight — otherwise wrapping any construct in parentheses would be the bypass."""
    sql = "SELECT s.g FROM (SELECT g FROM generate_series(1,3) AS t(g)) s"
    _assert_unscopable(rt.check_scopable(sql, _scope_org()))


# ===========================================================================
# The allow-list — the gate is silent on everything the scope walk CAN read
#
# A fail-closed gate is only worth having if it does not refuse valid SQL, and this half is the
# expensive half to get wrong: a false refusal here is a broken query on every deployment.
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT id FROM orders", id="plain"),
    pytest.param("SELECT o.id FROM orders o JOIN customers c ON c.id = o.id", id="join"),
    pytest.param("SELECT o.id FROM orders o, customers c", id="comma-join"),
    pytest.param("WITH t AS (SELECT id FROM orders) SELECT id FROM t", id="cte"),
    pytest.param("SELECT x FROM (SELECT id AS x FROM orders) s", id="derived-subquery"),
    pytest.param("SELECT x FROM (SELECT id AS x FROM (SELECT id FROM orders) i) s", id="nested"),
    pytest.param("SELECT id FROM orders UNION SELECT id FROM customers", id="set-operation"),
    pytest.param("SELECT 1", id="no-from"),
    pytest.param("SELECT id FROM orders WHERE id IN (SELECT id FROM customers)", id="in-subquery"),
])
def test_a_scopable_statement_is_allowed(sql):
    assert rt.check_scopable(sql, _scope_org()) is None


def test_an_undeclared_table_is_not_this_gate_s_refusal():
    """`secret` is undeclared, and that is a 4b reach for table scope to name — not a 4c
    cannot-determine. A gate that refused here would be taking the other gate's verdict and
    reporting the wrong reason for it, which is what the reason split exists to prevent."""
    assert rt.check_scopable("SELECT id FROM secret", _scope_org()) is None
    assert rt.check_table_scope("SELECT id FROM secret", _scope_org()) is not None


def test_the_gate_is_inert_when_the_model_declares_no_tables():
    """A deployment with no declared surface is not scoping anything, so there is nothing to be
    unable to scope against. Matches `check_table_scope`'s empty-model no-op rather than inventing
    a second posture for the same situation."""
    org = m.Datasource(datasource="Shop",
                       storage_connections=[m.StorageConnection(name="c",
                                                                storage_type="PostgreSQL")],
                       subject_areas=[])
    assert rt.check_scopable("SELECT g FROM generate_series(1,3) AS t(g)", org) is None


# ===========================================================================
# Structural properties
# ===========================================================================

@pytest.mark.parametrize("sql", [p.values[0] for p in UNSCOPABLE] + [
    "SELECT id FROM orders",
    "WITH t AS (SELECT id FROM orders) SELECT id FROM t",
])
def test_the_shared_context_and_standalone_paths_agree(sql):
    """Reusing `ctx.tree` must not change the verdict. A gate that re-parsed could disagree with the
    tree every other gate judged, which is the divergence this whole family exists to prevent."""
    org = _scope_org()
    ctx = rt.build_guard_context(sql, org)
    standalone, shared = rt.check_scopable(sql, org), rt.check_scopable(sql, org, ctx=ctx)
    assert (standalone is None) == (shared is None)
    if standalone is not None:
        assert (standalone.rule, standalone.detail) == (shared.rule, shared.detail)


def test_no_environment_variable_can_open_the_gate(monkeypatch):
    """Principle 4 admits no waiver, and the earlier design of this gate shipped one:
    `AGAMI_SQL_UNSCOPABLE_POSTURE=warn` logged and executed the statement anyway. It is asserted
    absent rather than merely deleted, because a fail-open reintroduced behind an env var is exactly
    the change that reads as an operational knob in review.
    """
    for name in ("AGAMI_SQL_UNSCOPABLE_POSTURE", "AGAMI_UNSCOPABLE_POSTURE",
                 "AGAMI_SQL_SCOPABLE_POSTURE"):
        for value in ("warn", "off", "disable", "0", "allow", "ignore"):
            monkeypatch.setenv(name, value)
            _assert_unscopable(
                rt.check_scopable("SELECT g FROM generate_series(1,3) AS t(g)", _scope_org()))


def test_the_rule_is_pinned_to_a_reason_the_contract_admits():
    """The refusal vocabulary is closed at three. Asserted against the enum rather than by grep, so
    a fourth reason cannot be introduced by this gate without failing here."""
    assert guardrail.REASON_FOR_RULE[guardrail.RULE_UNSCOPABLE] == "undetermined"
    assert guardrail.REASON_FOR_RULE[guardrail.RULE_UNSCOPABLE] in {
        "unsafe", "out_of_scope", "undetermined"}
