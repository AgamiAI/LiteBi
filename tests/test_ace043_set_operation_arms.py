"""ACE-043 — which ARM of a set operation a table reference was written in.

`TableRef.scope` used to answer "the main query, a named CTE, or a nested subquery", and for a set
operation that made every arm the same answer. Two arms of a UNION reading one table under one
alias were two IDENTICAL rows on the receipt: same name, same alias, same scope. A reader asked to
trust a per-reference filter accounting could not tell which arm each row was about, and when one
arm applied a declared filter and the other did not, could not tell which of the two the statement
had left unfiltered. The suffix is what makes those rows distinguishable: a 1-based `#<n>` appended
to the scope label when, and ONLY when, the reference's enclosing output select is one of TWO OR
MORE arms of a set operation.

A failure in this file means the receipt has started attributing a reference to the wrong arm, or
has started numbering something that is not a set operation. Both are silent in production: the
label still parses, the receipt still assembles, and the only thing wrong is that the number is
about a different piece of SQL than the one the caller sent.

Two orders are in play and they deliberately differ. `_table_references` returns references in
sqlglot's own traversal order, which reaches `A UNION B UNION C` as C, A, B. The ordinal is the
arm's position in the TEXT. That divergence is the whole value of the feature — a walk-order number
would be a fact about our parser rather than about the caller's statement — so the tests below
assert ORDERED LISTS of references and never a mapping keyed by bare table name. Keyed by name, the
three arms that make the distinction visible collapse into one entry and the assertion proves
nothing while still passing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import sqlglot  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from sqlglot import expressions as exp  # noqa: E402


def _parse(sql: str) -> "exp.Expression":
    return sqlglot.parse_one(sql, error_level="ignore")


def _refs(sql: str) -> list[tuple[str, str | None, str]]:
    """(bare name, alias, scope) per table REFERENCE, in the resolver's own walk order.

    An ordered list, deliberately not a dict keyed by the bare name. Every discriminating case in
    this file reads ONE table from several arms, and a name-keyed view folds those into a single
    entry — an assertion written that way holds just as well against an implementation that numbers
    nothing at all.
    """
    return [(r.bare, r.alias, r.scope) for r in rt._table_references(_parse(sql))]


# --- the guard: only a real set operation is numbered ------------------------


def test_an_ordinary_select_keeps_a_bare_main_scope():
    """One output query carries no ordinal, and neither does a single-arm CTE body.

    `_arm_suffixes` numbers whatever `_output_selects` hands it, and for a plain SELECT that is a
    ONE-element list. Drop the `len(arms) < 2` guard and that list gets numbered too: every ordinary
    statement in the product ships `main#1`, and every single-arm CTE body ships `cte:recent#1`.
    That is a contract change to every consumer of the receipt, bought for nothing — an ordinal
    exists to tell two arms apart and there is only ever one arm here.

    The guard is also what fixes the MEANING of an absent suffix. With it, absent means "not a set
    operation". Without it, absent would mean "we could not work out which arm", which is the one
    thing a receipt fact must never quietly turn into.
    """
    assert _refs("SELECT id FROM orders o") == [("orders", "o", "main")]
    assert _refs("WITH recent AS (SELECT id FROM orders) SELECT id FROM recent") == [
        ("recent", None, "main"),
        ("orders", None, "cte:recent"),
    ]


# --- the ordinal is the position in the SQL, not in the walk -----------------


def test_each_arm_of_a_set_operation_is_numbered_by_its_position_in_the_sql():
    """THREE arms over ONE table under three aliases, so the ordinal is all that tells them apart.

    Two arms would not discriminate, and that is why this test is written with three. The walk
    reaches a two-arm `A UNION B` as A then B, so a numbering taken from `find_all` order and a
    numbering taken from `_output_selects` order agree, and a walk-order implementation passes.
    sqlglot parses `A UNION B UNION C` LEFT-DEEP: the walk reaches C first, and the two orders come
    apart. Only a number taken from `_output_selects` recovers the order the caller actually wrote.

    Asserted as the ordered `(alias, scope)` list rather than through any helper that keys by bare
    table name. All three arms read `orders`; keyed by name they are one entry, and every claim
    below evaporates while the assertion still goes green.

    An off-by-one lands here too: `enumerate(arms, 1)` written `enumerate(arms)` shifts the whole
    receipt by one and every arm is then reported as the arm before it.
    """
    sql = "SELECT id FROM orders o UNION SELECT id FROM orders o2 UNION SELECT id FROM orders o3"
    assert [(alias, scope) for _, alias, scope in _refs(sql)] == [
        ("o3", "main#3"),
        ("o", "main#1"),
        ("o2", "main#2"),
    ]
    # The divergence spelled out, so a change that quietly aligned the two orders would fail here
    # rather than pass everywhere: the ordinals in WALK order are 3, 1, 2 and not 1, 2, 3.
    assert [scope for _, _, scope in _refs(sql)] != ["main#1", "main#2", "main#3"]


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_intersect_and_except_are_numbered_exactly_as_union_is(operator):
    """All four operators are one thing to sqlglot, and they have to stay one thing here.

    `A UNION B`, `A INTERSECT B` and `A EXCEPT B` parse to three different classes that share the
    base `exp.SetOperation`, and `exp.Union` is only ONE of the three. A guard written
    `isinstance(node, exp.Union)` is the spelling that reads natural — UNION is the operator anyone
    thinks of first — and it passes every UNION assertion in this file while silently leaving
    INTERSECT and EXCEPT arms unnumbered. In `_output_selects` the same narrowing is worse than
    unnumbered: an arm outside the guard is an arm the sensitive-projection and fan/chasm passes
    never scan at all.

    UNION ALL is here because `distinct` is an argument on the node rather than a separate operator,
    so it is the case a guard could plausibly treat differently by accident — and duplicate-
    preserving arms are exactly where two identical-looking rows on a receipt need telling apart.
    """
    sql = f"SELECT id FROM orders o {operator} SELECT id FROM orders o2"
    assert _refs(sql) == [("orders", "o", "main#1"), ("orders", "o2", "main#2")]


# --- one numbering rule, both scope families ---------------------------------


def test_a_set_operation_in_a_cte_body_numbers_that_cte_s_arms():
    """A UNION-ed CTE body is a set operation too, and it is numbered by the SAME mechanism.

    `_arm_suffixes` walks the statement root AND every CTE body, which is the design decision this
    test exists to hold still. Build the ordinal from `_output_selects(node)` alone — the obvious
    reading, since that is where the top-level arms come from — and a CTE body's arms get no
    ordinal, so a UNION-ed CTE reading one table twice is once again two identical receipt rows.
    Bolt on a second mechanism for CTE bodies instead and the two numberings are free to drift: a
    `#2` under `main` and a `#2` under `cte:recent` would mean different things and a reader could
    not tell which rule produced either.

    The single-arm body is asserted in the same test rather than a neighbouring one, because the
    guard and the numbering are the same claim seen from two sides: a CTE body is numbered when it
    has arms to number and bare when it does not.
    """
    union_body = (
        "WITH recent AS (SELECT id FROM orders UNION SELECT id FROM orders) "
        "SELECT r.id FROM recent r"
    )
    assert _refs(union_body) == [
        ("recent", "r", "main"),
        ("orders", None, "cte:recent#1"),
        ("orders", None, "cte:recent#2"),
    ]

    single_arm_body = "WITH recent AS (SELECT id FROM orders) SELECT r.id FROM recent r"
    assert _refs(single_arm_body) == [("recent", "r", "main"), ("orders", None, "cte:recent")]

    # Both numberings running in ONE statement, which is what says the mechanism is shared rather
    # than merely present twice: the outer UNION numbers `main`, the CTE body numbers `cte:recent`,
    # and neither borrows the other's positions.
    both = (
        "WITH recent AS (SELECT id FROM orders o1 UNION SELECT id FROM orders o2) "
        "SELECT id FROM recent r UNION SELECT id FROM orders o3"
    )
    assert _refs(both) == [
        ("recent", "r", "main#1"),
        ("orders", "o3", "main#2"),
        ("orders", "o1", "cte:recent#1"),
        ("orders", "o2", "cte:recent#2"),
    ]


# --- a nested reference is not an arm ----------------------------------------


def test_a_reference_nested_inside_an_arm_stays_an_unnumbered_subquery():
    """`subquery` is the label that says "we did not name this scope", and it takes no ordinal.

    Two mutations land here and they fail in opposite directions. Suffix `subquery` and the receipt
    claims to know which arm a scope it just admitted it could not name belongs to — a confident
    number attached to an explicit "unknown". Resolve a nested SELECT up to its ENCLOSING arm
    instead, so `t9` reads `main#1`, and a table the caller only reads inside a derived table is
    reported as one the arm reads directly, which is the same class of error as crediting the outer
    query with a filter that was satisfied inside a CTE.

    The second arm still numbers normally in the same statement, so this is a statement about the
    nested reference alone and not about the numbering having switched off.
    """
    sql = "SELECT a FROM (SELECT z AS a FROM t9) q UNION SELECT b FROM orders"
    assert _refs(sql) == [("orders", None, "main#2"), ("t9", None, "subquery")]


# The arm shapes the sweep below crosses with every set operator. Each shape names its own tables so
# a failure says which shape lost its scope. `a select from a derived table` is the negative
# control: it holds the one reference in the whole corpus that is GENUINELY nested, and the sweep's
# own filter has to exclude exactly it and nothing else.
ARM_SHAPES = {
    "a plain select": "SELECT id FROM t_plain",
    "a select with a WHERE": "SELECT id FROM t_where WHERE t_where.id > 0",
    "a select with a GROUP BY": "SELECT id FROM t_group GROUP BY id",
    "a parenthesized select": "(SELECT id FROM t_paren)",
    "a doubly-parenthesized select": "((SELECT id FROM t_paren_paren))",
    "a nested set operation": "(SELECT id FROM t_nest_a UNION SELECT id FROM t_nest_b)",
    "a VALUES arm": "VALUES (1)",
    # Bare `VALUES (1)` is normalized by sqlglot into a Select and so contributes one; PARENTHESIZED
    # it stays a Subquery wrapping Values and contributes NONE. That difference is invisible in the
    # SQL and is exactly the shape that renumbered every later arm before `_output_select_arms`.
    "a parenthesized VALUES arm": "(VALUES (1))",
    "a select with a join": "SELECT a.id FROM t_join_a a JOIN t_join_b b ON b.id = a.id",
    "a select from a derived table": "SELECT q.id FROM (SELECT id FROM t_derived) q",
}

# `t_other` is written as the LAST arm of every statement the sweep builds, so its ordinal must be
# that statement's arm COUNT. Pinning the exact number rather than "starts with main#" is what
# separates a true ordinal from a plausible one: an arm contributing no output SELECT still has to
# consume its position, and a shape whose leading arm is itself a set operation pushes `t_other` to
# three. Both are cases where a positional walk over the surviving SELECTs silently reports a
# different arm than the caller wrote.
EXPECTED_LAST_ARM_SCOPE = {
    "a plain select": "main#2",
    "a select with a WHERE": "main#2",
    "a select with a GROUP BY": "main#2",
    "a parenthesized select": "main#2",
    "a doubly-parenthesized select": "main#2",
    "a nested set operation": "main#3",
    "a VALUES arm": "main#2",
    "a parenthesized VALUES arm": "main#2",
    "a select with a join": "main#2",
    "a select from a derived table": "main#2",
}


def _is_nested(tbl: "exp.Table") -> bool:
    """True iff something between `tbl` and the statement root NESTS the query that reads it.

    Written as an ancestor walk rather than by asking `_output_selects`, so the test's idea of "this
    reference sits in an arm" cannot quietly become the implementation's idea of it — a filter
    derived from the code under test would exclude precisely the references that code got wrong.

    A `Subquery` or `Paren` that merely BRACKETS an arm is not a nesting: its own parent is the set
    operation, or another bracket around it. A derived table's `Subquery` hangs off a `FROM` or a
    `JOIN` instead, and that is a nesting. A second enclosing SELECT, a CTE, or a LATERAL is a
    nesting in every case.
    """
    seen_select = False
    node = tbl.parent
    while node is not None:
        if isinstance(node, exp.Select):
            if seen_select:
                return True
            seen_select = True
        elif isinstance(node, (exp.CTE, exp.Lateral)):
            return True
        elif isinstance(node, (exp.Subquery, exp.Paren)) and not isinstance(
            node.parent, (exp.SetOperation, exp.Subquery, exp.Paren)
        ):
            return True
        node = node.parent
    return not seen_select


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_no_reference_in_any_arm_of_a_set_operation_falls_through_to_subquery(operator):
    """The negative invariant: an arm that cannot be scoped does not occur.

    This stands in for a spec criterion rather than beside one. ACE-043 asked what the label should
    be for "an arm we could not scope", and the answer measured over 1156 parsed set-operation
    statements was that the case is UNREACHABLE: every arm resolves either to `main#<n>` or, inside
    a WITH, to `cte:<name>#<n>`. A runtime branch for it would have been dead code, and dead code
    that claims to handle a case is worse than no code, because it makes the case look handled. So
    the criterion became this sweep: the property is asserted, and nothing ships to serve it.

    That makes the mutation it catches a subtle one, because the thing it protects reads like
    unnecessary defensiveness. `_output_selects` recurses through `exp.Paren` and `exp.Subquery`
    before it reaches the SELECT inside, and a reader tidying that function would see the branch as
    a no-op — parentheses around a query, deleted. Delete it and `(SELECT …) UNION (SELECT …)` has
    no arms at all: both references fall through to `subquery`, the sensitive-projection and
    fan/chasm passes stop scanning either arm, and every assertion in this file that uses a bare
    `SELECT … UNION SELECT …` still passes.

    Nine arm shapes crossed with four operators, and the shapes are chosen for the ways an arm can
    be wrapped or complicated rather than for SQL coverage: brackets, double brackets, a set
    operation of its own, a VALUES arm that contributes no table at all, and the ordinary clauses
    that change a SELECT's children without changing what it is.
    """
    scoped: list[tuple[str, str, str]] = []
    nested: list[tuple[str, str, str]] = []
    for shape, arm in sorted(ARM_SHAPES.items()):
        sql = f"{arm} {operator} SELECT id FROM t_other"
        for ref, tbl in rt._reference_sites(_parse(sql)):
            (nested if _is_nested(tbl) else scoped).append((shape, tbl.name, ref.scope))

    # Every reference written DIRECTLY in an arm is scoped to an arm. Listed rather than counted, so
    # a failure names the shapes that broke instead of only saying that some number moved.
    assert [entry for entry in scoped if not entry[2].startswith("main#")] == []
    # And the sweep above is not vacuous. A `_is_nested` that returned True for everything would
    # empty `scoped` and leave the assertion passing over nothing: every shape has to have
    # contributed, and the count pins the references WITHIN a shape too — two apiece, three for the
    # nested set operation and for the join, one for the VALUES arm and one for the derived table.
    assert {entry[0] for entry in scoped} == set(ARM_SHAPES)
    assert len(scoped) == 19
    assert nested == [("a select from a derived table", "t_derived", "subquery")]
    # And the ordinals are the arms' real positions, not merely well-formed labels. Asserted last
    # because it is the strictest claim here: `startswith("main#")` above would accept `main#1` for
    # every reference in the corpus.
    assert {
        shape: scope for shape, table, scope in scoped if table == "t_other"
    } == EXPECTED_LAST_ARM_SCOPE


def test_an_arm_that_contributes_no_output_select_still_consumes_its_position():
    """A shifted ordinal is a FALSE receipt fact, not a weaker one, and this is where it came from.

    `(VALUES ('x', 0))` is ordinary SQL — appending a synthetic row to a result — and it parses to a
    `Subquery` wrapping `Values`, which contributes no output SELECT. Numbering the flattened list
    of surviving SELECTs therefore closed the gap and handed the THIRD arm the second arm's number:

        SELECT a FROM t1 UNION ALL (VALUES ('x', 0)) UNION ALL SELECT c FROM t3
          -> t3 read `main#2`

    Nothing about that label looks wrong. It parses, it is well-formed, it is in the documented
    vocabulary, and it points at a different piece of SQL than the caller wrote — which is the one
    failure mode a trust receipt cannot have, because a reader has no second source to check it
    against. The contract says the ordinal is the arm's position IN THE SQL; here it silently was
    not.

    The two-arm case is the same defect wearing a different hat: one surviving SELECT tripped the
    `len(arms) < 2` guard, so a set operation was labelled exactly like a plain SELECT and the
    docstring's promise that an absent suffix means "no suffix, never arm unknown" was false.

    Caught by `_output_select_arms` keeping one slot per arm AS WRITTEN, empty or not. The mutation
    this catches is flattening that back to `_output_selects` for tidiness — every other test in
    this file passes if you do, because every other statement here has an output SELECT in every
    arm.
    """
    three = "SELECT a FROM t1 UNION ALL (VALUES ('x', 0)) UNION ALL SELECT c FROM t3"
    assert [(r.bare, r.scope) for r in rt._table_references(_parse(three))] == [
        ("t3", "main#3"),
        ("t1", "main#1"),
    ]

    two = "SELECT a FROM t1 UNION ALL (VALUES ('x', 0))"
    assert [(r.bare, r.scope) for r in rt._table_references(_parse(two))] == [("t1", "main#1")]

    # The flattened view is unchanged for every other caller: it still holds only real SELECTs, so
    # the sensitive-projection and fan/chasm passes see exactly what they saw before.
    assert len(rt._output_selects(_parse(three))) == 2
    assert len(rt._output_select_arms(_parse(three))) == 3


# --- the suffix is separable from the label it is appended to ----------------


def test_a_hash_in_a_cte_name_cannot_be_mistaken_for_an_arm_ordinal():
    """The `#` in a caller-written CTE name never survives into the label, so the split stays sound.

    A consumer that wants the scope back without its ordinal splits on the LAST `#`, and that only
    works while `#` cannot appear in the label's other half. `_ECHO_UNSAFE` is what guarantees it:
    the CTE name goes through `_echo_name`, whose alphabet is `[A-Za-z0-9_.$*-]`, and `#` is outside
    it — so a name written `re#cent` reaches the label as `re?cent` and the only `#` left is ours.

    The mutation this catches is someone widening that alphabet. `#` looks harmless to allow: it is
    not whitespace, it is not a quote, and it is a legal identifier character in some engines. Allow
    it and `WITH "re#cent" AS (… UNION …)` produces `cte:re#cent#1`, which every consumer splitting
    from the right still parses — into the scope `cte:re#cent` and the ordinal `1`, correctly — but
    which a consumer splitting from the LEFT reads as the scope `cte:re` and the ordinal `cent#1`.
    The label is caller-influenced text either way, and the bound is the only reason it is not
    caller-CHOSEN text.
    """
    sql = (
        'WITH "re#cent" AS (SELECT id FROM orders o1 UNION SELECT id FROM orders o2) '
        'SELECT id FROM "re#cent"'
    )
    # `bare` is the raw parsed name and is bounded where the receipt renders it, not here; the
    # SCOPE is the field this test is about, and it is bounded before the suffix is composed on.
    assert _refs(sql) == [
        ("re#cent", None, "main"),
        ("orders", "o1", "cte:re?cent#1"),
        ("orders", "o2", "cte:re?cent#2"),
    ]

    # The consumer's own operation, spelled out: split off the ordinal and the scope comes back
    # whole. Spelled against `_echo_name` too, so the test fails if the bound is ever relaxed.
    label = _refs(sql)[1][2]
    assert label.rsplit("#", 1) == ["cte:re?cent", "1"]
    assert label.rsplit("#", 1)[0] == "cte:" + rt._echo_name("re#cent")
    assert rt._ECHO_UNSAFE.search("#") is not None


# --- the fact a shared `_output_selects` must keep reporting ------------------


def _sensitive_org() -> "m.Datasource":
    """One table with one `sensitive` column, which is the whole input this determination reads."""
    return m.Datasource(
        datasource="Shop",
        subject_areas=[
            m.SubjectArea(
                name="sales",
                tables_defined=[
                    m.Table(
                        name="customers",
                        schema="public",
                        storage_connection="c",
                        grain=["id"],
                        description="customers",
                        columns=[
                            m.Column(name="id", type="integer"),
                            m.Column(name="country", type="string"),
                            m.Column(name="ssn", type="string", sensitive=True),
                        ],
                    )
                ],
            )
        ],
    )


def test_a_sensitive_column_projected_in_a_non_leading_arm_is_still_reported():
    """A regression pin on the OTHER caller of `_output_selects`, which ACE-043 now shares.

    `_arm_suffixes` made `_output_selects` a shared helper with two consumers that want different
    things from it. The numbering only needs the arms COUNTED and ORDERED, and "return the leading
    arm and the count" is a plausible optimization for that — it would keep every assertion above
    green, because the leading arm is `#1` under either shape. `_projected_sensitive` iterates the
    same list to decide which columns the answer actually contains, and under that optimization it
    would inspect the first arm only: `SELECT id … UNION SELECT ssn …` reports nothing.

    What that costs is a REPORT, not a refusal, and this test is written against the report on
    purpose. The projection determination stopped being a gate — `projected_sensitive_columns`
    refuses nothing and the statement runs — so a test asserting that the second arm is BLOCKED
    fails against correct behaviour and would be "fixed" by weakening it. The failure being pinned
    is quieter than a refusal and worse for it: the rows come back holding the sensitive values and
    the receipt beside them says none were projected.

    Second AND third arm, because an implementation that kept only the first and last would pass a
    two-arm test.
    """
    org = _sensitive_org()

    second = "SELECT id FROM customers UNION SELECT ssn FROM customers"
    assert rt.projected_sensitive_columns(second, org) == ["customers.ssn"]

    third = (
        "SELECT id FROM customers UNION SELECT country FROM customers "
        "UNION SELECT ssn FROM customers"
    )
    assert rt.projected_sensitive_columns(third, org) == ["customers.ssn"]

    # The determination genuinely reads the arms rather than reporting the column for any statement
    # that names the table: the same three-arm shape projecting nothing sensitive reports nothing.
    clean = (
        "SELECT id FROM customers UNION SELECT country FROM customers "
        "UNION SELECT id FROM customers"
    )
    assert rt.projected_sensitive_columns(clean, org) == []
