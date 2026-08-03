"""Model-scoping reaches that the guard does not catch today, because it parses generically.

Every gate in the model-scoping family calls `sqlglot.parse_one(sql)` with no `dialect=`. Under
the generic dialect a backtick is not an identifier quote and neither is a bracket, so a statement
written for MySQL, BigQuery, Databricks or SQL Server parses into something that is not what the
statement says — usually nothing at all. A gate handed a tree with no tables and no columns finds
nothing to object to and passes.

    SELECT `ssn` FROM `customers`
      generic   tables=[]            cols=['`']
      mysql     tables=['customers'] cols=['ssn']

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

Owner of the fix: the dialect-aware guard parsing work (ACE-079), whose change is threading the
datasource dialect through every guard-path parse. Nothing in ACE-096 touches a parse call.
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
