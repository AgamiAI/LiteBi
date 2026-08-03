"""Model-scoping reaches that the guard does not catch today, because of what its parse returns.

One root cause, two shapes. Every gate in the model-scoping family calls
`sqlglot.parse_one(sql, error_level="ignore")` with no `dialect=`, so the tree it judges can be
something other than what the statement says — and `error_level="ignore"` means it never raises
to tell anyone. A gate handed a tree with no tables and no columns finds nothing to object to
and passes.

**Shape 1, dialect quoting.** A backtick is not an identifier quote in the generic dialect, and
neither is a bracket, so a statement written for MySQL, BigQuery, Databricks or SQL Server is
misread:

    SELECT `ssn` FROM `customers`
      generic   tables=[]            cols=['`']
      mysql     tables=['customers'] cols=['ssn']

**Shape 2, silent truncation.** A construct the generic grammar cannot parse is not refused, it
is *dropped*, and what is left parses cleanly. Snowflake's nested semi-structured path is the
case found here — the FROM clause disappears:

    SELECT payload:cust.ssn FROM secret   ->   SELECT payload AS :cust

Every gate then judges a statement that reads no tables at all, so an undeclared table is
reached with nothing raised anywhere. This one is worse than shape 1: shape 1 needs a
non-default engine, and this needs only a two-level path.

**Every test here is `xfail(strict=True)`, and that is the deliverable.** ACE-096 specifies what
the three 4b gates refuse; this file is the part of that specification the code does not yet meet,
written as failing tests rather than as prose so the gap is visible in CI instead of in a document
nobody re-reads. `strict=True` is deliberate: when ACE-079's dialect threading lands, these xpass,
the build goes red, and whoever ports it deletes this file as the proof their port worked. A
non-strict marker would xpass in silence, which is the exact failure mode this portfolio keeps
legislating against — silence reading as clean.

**Do not add a marker to a case without measuring it first.** Two of the reaches below are already
refused, one of them by accident, and they are kept here without markers precisely so nobody
"fixes" them into the xfail list. See the contrast section at the end.

Owners of the fix, neither of them ACE-096: threading the datasource dialect through every
guard-path parse (ACE-079) closes shape 1, and refusing rather than degrading when a statement
cannot be parsed or scoped (ACE-037) closes shape 2 — a statement whose parse silently lost a
clause is exactly the "we could not determine" case that fails closed. Nothing in ACE-096 touches
a parse call or an `error_level`.
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

_WHY = ("the guard parses with sqlglot's generic dialect, where this quoting form is not an "
        "identifier quote — ACE-079's dialect threading is what turns this green")

_WHY_TRUNCATED = ("the generic grammar cannot parse a nested semi-structured path, so under "
                  "`error_level=\"ignore\"` it DROPS the FROM clause instead of raising: the gate "
                  "judges `SELECT payload AS :cust`, which reads no table at all. Closed by "
                  "ACE-037 (refuse rather than degrade when a statement cannot be scoped), or by "
                  "ACE-079 if the dialect makes the construct parse")


def _scope_org():
    """orders(id, amount, customer_id) + customers(id, name). `secret` is not declared."""
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"), ("customer_id", "integer")])
    customers = _t("customers", [("id", "integer"), ("name", "string")])
    return m.Datasource(datasource="Shop",
                        subject_areas=[m.SubjectArea(name="sales",
                                                     tables_defined=[orders, customers])])


# ===========================================================================
# Table scope — a reach to an undeclared table, quoted for a backtick or bracket engine
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT `secret`.`x` FROM `secret`",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY), id="backtick-qualified"),
    pytest.param("SELECT [x] FROM [secret]",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY), id="bracket"),
])
def test_undeclared_table_reached_through_dialect_quoting_is_refused(sql):
    refusal = rt.check_table_scope(sql, _scope_org())
    assert refusal is not None, f"undeclared table reached: {sql!r}"
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "secret" in refusal.detail


# ===========================================================================
# Column scope — a reach to an undeclared column on a declared table
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT `bogus` FROM orders",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY), id="backtick"),
])
def test_undeclared_column_reached_through_dialect_quoting_is_refused(sql):
    refusal = rt.check_column_scope(sql, _scope_org())
    assert refusal is not None, f"undeclared column reached: {sql!r}"
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert "bogus" in refusal.detail


# ===========================================================================
# The star ban — a qualified star whose qualifier is dialect-quoted
#
# Worse than a plain miss: the bracket form IS refused, but by column scope rather than the star
# ban, so a suite asserting only "refused" would read as green while the gate that owns the rule
# never fired. Both cases therefore assert the RULE, not just the refusal.
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT `o`.* FROM orders `o`",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY), id="backtick"),
    pytest.param("SELECT [o].* FROM orders [o]",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY + "; today column scope fires "
                                         "instead, which is the right verdict from the wrong gate"),
                 id="bracket"),
])
def test_qualified_star_with_a_dialect_quoted_qualifier_is_refused_by_the_star_ban(sql):
    refusal = rt.check_no_select_star(sql)
    assert refusal is not None, f"qualified star not seen: {sql!r}"
    assert refusal.rule == guardrail.RULE_SELECT_STAR


# ===========================================================================
# Shape 2 — a nested semi-structured path takes the FROM clause with it
#
# This is the one to fix first. It needs no exotic engine and no quoting trick: a two-level
# path on any Snowflake-shaped model reaches an undeclared TABLE with every gate silent. The
# adjacent single-level spellings (`payload:ssn`, `payload['ssn']`) parse correctly and ARE
# judged — they are the accepted column-granularity residual, pinned in
# test_column_scope_adversarial.py. These are not that residual; they are a hole.
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT payload:cust.ssn FROM secret",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY_TRUNCATED), id="projection"),
    pytest.param("SELECT c.data:a.b FROM secret c",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY_TRUNCATED), id="aliased"),
    pytest.param("SELECT id, payload:cust.ssn FROM secret",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY_TRUNCATED), id="alongside-a-real-column"),
])
def test_undeclared_table_survives_a_nested_path_truncation(sql):
    refusal = rt.check_table_scope(sql, _scope_org())
    assert refusal is not None, f"undeclared table reached through a dropped FROM: {sql!r}"
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT nope:cust.ssn FROM orders",
                 marks=pytest.mark.xfail(strict=True, reason=_WHY_TRUNCATED), id="undeclared-root"),
])
def test_undeclared_column_survives_a_nested_path_truncation(sql):
    """The column-scope half. `nope` is undeclared and the single-level `nope:ssn` IS refused —
    it is the second dot that takes the statement out of reach of the gate."""
    refusal = rt.check_column_scope(sql, _scope_org())
    assert refusal is not None, f"undeclared column reached through a dropped FROM: {sql!r}"
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE


def test_the_single_level_path_still_reaches_the_gate():
    """The contrast that makes the two above legible: one dot fewer and the gate works. Unmarked
    because it passes — it bounds the hole to the nested spelling rather than to path access."""
    refusal = rt.check_column_scope("SELECT nope:ssn FROM orders", _scope_org())
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE


# ===========================================================================
# Contrast — reaches that ALREADY hold, kept here unmarked so nobody adds a marker to them
# ===========================================================================

def test_a_bare_star_is_dialect_independent():
    """`*` is `*` in every grammar, so the star ban never depended on the quoting fix. Recording
    it here bounds the gap above: what dialect quoting hides is the QUALIFIER, not the star."""
    refusal = rt.check_no_select_star("SELECT * FROM `secret`")
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_SELECT_STAR


def test_a_backtick_quoted_table_is_refused_but_for_the_wrong_reason():
    """This one refuses today, and it must not be given a marker — it would xpass immediately.

    It is right by accident. The generic dialect reads `` SELECT id FROM `secret` `` as a table
    literally named `` ` ``, which is not in the declared set, so the gate fires having never seen
    `secret` at all. The proof is the detail: it echoes the sanitizer's placeholder rather than the
    name the caller wrote, so the caller is told a table they never typed is undeclared.

    The dialect port must revisit this rather than read a green tick: once the dialect is threaded
    the same statement refuses for the real reason and this assertion changes.
    """
    refusal = rt.check_table_scope("SELECT id FROM `secret`", _scope_org())
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "secret" not in refusal.detail, "if this fails the dialect is threaded — see the docstring"
    assert "?" in refusal.detail


def test_a_bracket_quoted_column_is_refused_today():
    """The bracket form of an undeclared COLUMN happens to survive the generic parse well enough
    for column scope to fire. Unmarked for the same reason as above: it would xpass."""
    refusal = rt.check_column_scope("SELECT [bogus] FROM orders", _scope_org())
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
