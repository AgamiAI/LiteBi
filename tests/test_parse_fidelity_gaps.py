"""Model-scoping reaches that the guard used to miss, because of what its parse returned.

**These were the gaps. They are now the regression suite.** Every case below was
`xfail(strict=True)` when this file was written, recording a reach that the gates let through; each
one is now a plain assertion. The marker was strict precisely so that closing the hole would turn
the build red and force whoever closed it to come here and say so.

One root cause, two shapes. Every gate in the model-scoping family called
`sqlglot.parse_one(sql, error_level="ignore")` with no `dialect=`, so the tree it judged could be
something other than what the statement said — and `error_level="ignore"` meant it never raised to
tell anyone. A gate handed a tree with no tables and no columns found nothing to object to and
passed.

**Shape 1, dialect quoting.** A backtick is not an identifier quote in the generic dialect, and
neither is a bracket, so a statement written for MySQL, BigQuery, Databricks or SQL Server was
misread:

    SELECT `ssn` FROM `customers`
      generic   tables=[]            cols=['`']
      mysql     tables=['customers'] cols=['ssn']

**Shape 2, silent truncation.** A construct the generic grammar could not parse was not refused, it
was *dropped*, and what was left parsed cleanly. Snowflake's nested semi-structured path was the
case found here — the FROM clause disappeared:

    SELECT payload:cust.ssn FROM secret   ->   SELECT payload AS :cust

Every gate then judged a statement that read no tables at all, so an undeclared table was reached
with nothing raised anywhere.

**Both shapes are closed by the same change, and the second one is the surprise.** Shape 1 was
always going to be closed by threading the datasource's dialect. Shape 2 was expected to need
ACE-037 (refuse rather than degrade when a statement cannot be scoped) — but this file's own
original note allowed for the other outcome, "or by ACE-079 if the dialect makes the construct
parse", and that is what happened: under the `snowflake` dialect sqlglot parses `payload:cust.ssn`
correctly and keeps the FROM, so table scope sees `secret` and refuses it. Nothing here now depends
on the unscopable-statement work.

**What the fixtures had to gain, and why it is the point rather than a detail.** Each test now
declares the engine its quoting belongs to, because that declaration is what the guard reads the
statement in. A model that declares no engine at all is refused outright by the readability gate —
so "which engine is this" stopped being a thing the guard could shrug at, which is the whole of the
fix. `_scope_org` takes it as an argument for that reason.

**Do not weaken a case into 'refused' without its rule.** Several of these were refused before the
fix for the WRONG reason — a bracket-quoted column that column scope happened to catch, a
backtick-quoted table read as a table literally named `` ` `` — and a suite asserting only that
something refused would have read green throughout. Every assertion below names the gate.
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


def _scope_org(engine: str):
    """orders(id, amount, customer_id) + customers(id, name). `secret` is not declared.

    `engine` is what the guard reads every statement here in. It is a parameter rather than a
    constant because the reaches below are each written in one engine's own quoting, and reading a
    MySQL statement in SQL Server's grammar is the defect this file exists to pin, not a fixture
    detail.
    """
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"), ("customer_id", "integer")])
    customers = _t("customers", [("id", "integer"), ("name", "string")])
    return m.Datasource(datasource="Shop",
                        storage_connections=[m.StorageConnection(name="c", storage_type=engine)],
                        subject_areas=[m.SubjectArea(name="sales",
                                                     tables_defined=[orders, customers])])


# ===========================================================================
# Table scope — a reach to an undeclared table, quoted for a backtick or bracket engine
# ===========================================================================

@pytest.mark.parametrize(("engine", "sql"), [
    pytest.param("MySQL", "SELECT `secret`.`x` FROM `secret`", id="backtick-qualified"),
    pytest.param("SQLServer", "SELECT [x] FROM [secret]", id="bracket"),
])
def test_undeclared_table_reached_through_dialect_quoting_is_refused(engine, sql):
    refusal = rt.check_table_scope(sql, _scope_org(engine))
    assert refusal is not None, f"undeclared table reached: {sql!r}"
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "secret" in refusal.detail


# ===========================================================================
# Column scope — a reach to an undeclared column on a declared table
# ===========================================================================

@pytest.mark.parametrize(("engine", "sql"), [
    pytest.param("MySQL", "SELECT `bogus` FROM orders", id="backtick"),
])
def test_undeclared_column_reached_through_dialect_quoting_is_refused(engine, sql):
    refusal = rt.check_column_scope(sql, _scope_org(engine))
    assert refusal is not None, f"undeclared column reached: {sql!r}"
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert "bogus" in refusal.detail


# ===========================================================================
# The star ban — a qualified star whose qualifier is dialect-quoted
#
# Worse than a plain miss before the fix: the bracket form WAS refused, but by column scope rather
# than the star ban, so a suite asserting only "refused" would have read as green while the gate
# that owns the rule never fired. Both cases therefore assert the RULE, not just the refusal.
#
# This gate is the one that never receives `org`, so it is handed the dialect directly. That is the
# whole reason it takes a `dialect=` keyword: without it a standalone call would read a
# backtick-quoted projection generically and miss the star, which is this case.
# ===========================================================================

@pytest.mark.parametrize(("engine", "sql"), [
    pytest.param("MySQL", "SELECT `o`.* FROM orders `o`", id="backtick"),
    pytest.param("SQLServer", "SELECT [o].* FROM orders [o]", id="bracket"),
])
def test_qualified_star_with_a_dialect_quoted_qualifier_is_refused_by_the_star_ban(engine, sql):
    org = _scope_org(engine)
    refusal = rt.check_no_select_star(sql, dialect=rt._dialect_of(org)[0])
    assert refusal is not None, f"qualified star not seen: {sql!r}"
    assert refusal.rule == guardrail.RULE_SELECT_STAR


# ===========================================================================
# Shape 2 — a nested semi-structured path used to take the FROM clause with it
#
# It needed no exotic engine and no quoting trick: a two-level path on any Snowflake-shaped model
# reached an undeclared TABLE with every gate silent. The adjacent single-level spellings
# (`payload:ssn`, `payload['ssn']`) always parsed correctly and were judged — they are the accepted
# column-granularity residual, pinned in test_column_scope_adversarial.py. These were not that
# residual; they were a hole, and reading the statement in Snowflake's own grammar closes it.
# ===========================================================================

@pytest.mark.parametrize("sql", [
    pytest.param("SELECT payload:cust.ssn FROM secret", id="projection"),
    pytest.param("SELECT c.data:a.b FROM secret c", id="aliased"),
    pytest.param("SELECT id, payload:cust.ssn FROM secret", id="alongside-a-real-column"),
])
def test_undeclared_table_survives_a_nested_path_truncation(sql):
    refusal = rt.check_table_scope(sql, _scope_org("Snowflake"))
    assert refusal is not None, f"undeclared table reached through a dropped FROM: {sql!r}"
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT nope:cust.ssn FROM orders", id="undeclared-root"),
])
def test_undeclared_column_survives_a_nested_path_truncation(sql):
    """The column-scope half. `nope` is undeclared and the single-level `nope:ssn` was always
    refused — it was the second dot that took the statement out of reach of the gate."""
    refusal = rt.check_column_scope(sql, _scope_org("Snowflake"))
    assert refusal is not None, f"undeclared column reached through a dropped FROM: {sql!r}"
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE


def test_the_single_level_path_still_reaches_the_gate():
    """The contrast that made the two above legible: one dot fewer and the gate worked. It still
    does, which is what says the fix closed the hole without changing the residual beside it."""
    refusal = rt.check_column_scope("SELECT nope:ssn FROM orders", _scope_org("Snowflake"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE


# ===========================================================================
# Contrast — what the fix changed about cases that already refused
#
# These three were unmarked while the rest were xfail, kept to stop anyone marking a case that
# would xpass immediately. Two of them refused for reasons that were right by accident, and the
# original note said the dialect port must revisit them rather than read a green tick. This is that
# revisit.
# ===========================================================================

def test_a_bare_star_is_dialect_independent():
    """`*` is `*` in every grammar, so the star ban never depended on the quoting fix — but the
    STATEMENT still has to be readable, and that is the part this now pins.

    On MySQL the whole statement parses and the star ban fires, as it always did. The
    no-engine-declared spelling is not a weaker version of the same thing: it is refused before any
    gate runs, because a datasource that does not say which grammar its SQL is in cannot be
    governed at all. Both are correct outcomes and neither is a silent pass, which is what this
    case exists to say.
    """
    org = _scope_org("MySQL")
    refusal = rt.check_no_select_star("SELECT * FROM `secret`", dialect=rt._dialect_of(org)[0])
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_SELECT_STAR


def test_a_backtick_quoted_table_now_refuses_for_the_real_reason():
    """This one refused before the fix, and for the wrong reason.

    The generic dialect read `` SELECT id FROM `secret` `` as a table literally named `` ` ``, which
    is not in the declared set, so the gate fired having never seen `secret` at all. The proof was
    in the detail: it echoed the sanitizer's placeholder rather than the name the caller wrote, so
    the caller was told a table they never typed was undeclared.

    Now the statement is read in MySQL's grammar, the gate sees `secret`, and the detail names the
    caller's own identifier — which is what makes the refusal actionable rather than merely correct.
    """
    refusal = rt.check_table_scope("SELECT id FROM `secret`", _scope_org("MySQL"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert "secret" in refusal.detail, "the gate should now see the name the caller wrote"
    assert "?" not in refusal.detail, "the sanitizer placeholder was the wrong-reason tell"


def test_a_bracket_quoted_column_is_refused_on_its_own_engine():
    """The bracket form of an undeclared COLUMN happened to survive the generic parse well enough
    for column scope to fire. It still refuses on SQL Server, where the brackets are what the
    statement actually means, so the accident has been replaced by the real reading."""
    refusal = rt.check_column_scope("SELECT [bogus] FROM orders", _scope_org("SQLServer"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE


def test_a_datasource_that_declares_no_engine_is_refused_outright():
    """The other half of the contrast above, and the reason every fixture in this file gained an
    engine: without one there is no grammar to read the statement in, so the readability gate
    refuses before table scope, column scope or the star ban is consulted.

    It is `model_unavailable` rather than a scope rule because nothing is wrong with the statement.
    The remediation is the operator's, and it must not invite a retry — re-emitting the query
    cannot fix a datasource that has not said what it runs on.
    """
    org = m.Datasource(datasource="Shop", subject_areas=[])
    refusal = rt.check_readable("SELECT id FROM orders", org)
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert refusal.reason == "undetermined"
    assert "retry" not in refusal.remediation.lower()
