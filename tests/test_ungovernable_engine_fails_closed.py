"""A statement that cannot be read in a known grammar is refused, not run blind.

The guards decide by reading the statement. If the datasource does not say which engine it
runs on there is no grammar to read it in, so no gate below can reach a verdict worth
trusting — and a gate that cannot object is indistinguishable from a gate with nothing to
object to. The statement is therefore refused before any database is touched, whatever the
model happens to declare and whichever gate would otherwise have run.

The refusal has to be usable, which means two different situations get two different next
moves: a statement the caller can re-emit, and a datasource only an operator can fix. The
second must not invite a retry — an unactionable "try again" turns a configuration fault
into a retry loop.

Synthetic, generic names only — `customers`, `orders`, `AcmeCorp`.
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
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

SENSITIVE_VALUE = "111-22-3333"


def _org(*storage_types: str, sensitive: bool = True) -> "m.Datasource":
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="ssn", type="string", sensitive=sensitive),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
        columns=[m.Column(name="id", type="integer")],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders])
    return m.Datasource(
        datasource="AcmeCorp",
        version=1,
        storage_connections=[
            m.StorageConnection(name=f"c{i}", storage_type=st)
            for i, st in enumerate(storage_types)
        ],
        subject_areas=[sa],
    )


class _SpyExecutor:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, vetted_sql, creds, *, profile):
        self.calls.append((vetted_sql, creds, profile))
        return execute_sql.ExecResult(columns=["ssn"], rows=[(SENSITIVE_VALUE,)])


@pytest.fixture
def guarded(monkeypatch):
    def _run(sql: str, org, spy, **kw):
        monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: org)
        monkeypatch.setattr(
            execute_sql, "_load_credentials",
            lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
        )
        return execute_sql.execute_guarded(sql, "acme", None, executor=spy, **kw)

    return _run


# --- the refusal is unconditional -------------------------------------------------


@pytest.mark.parametrize("sensitive", [True, False], ids=["pii-declared", "no-pii-declared"])
def test_no_declared_engine_refuses_regardless_of_declared_pii(guarded, sensitive):
    """Governance cannot be conditional on how diligently the model was authored."""
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT id FROM customers", _org(sensitive=sensitive), spy)
    assert ei.value.refusal.kind == "unscopable_sql"
    assert spy.calls == [], "the executor ran against an engine the guard could not parse for"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM customers",       # would have passed every gate
        "SELECT id FROM undeclared",      # would have been refused by table scope
        "SELECT ssn FROM customers",      # would have been refused or masked by the PII gate
        "SELECT * FROM customers",        # would have been refused by the star ban
    ],
)
def test_no_declared_engine_refuses_whichever_gate_would_have_run(guarded, sql):
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded(sql, _org(), spy)
    assert ei.value.refusal.kind == "unscopable_sql"
    assert spy.calls == []


def test_disagreeing_engines_refuse(guarded):
    """One datasource resolves to one database, so two declared engines are ambiguous."""
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT id FROM customers", _org("PostgreSQL", "MySQL"), spy)
    assert ei.value.refusal.kind == "unscopable_sql"
    assert spy.calls == []


def test_a_declared_engine_still_runs(guarded):
    """The fail-closed must not swallow the ordinary case."""
    spy = _SpyExecutor()
    guarded("SELECT id FROM customers", _org("PostgreSQL"), spy)
    assert spy.calls, "a governable statement was refused"


# --- the two refusals are distinguishable and actionable --------------------------


def test_the_unmapped_engine_refusal_names_the_operator_action_and_invites_no_retry(guarded):
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT id FROM customers", _org(), spy)
    remediation = ei.value.refusal.remediation

    assert "storage_type" in remediation, "the operator is not told what to declare"
    # It may say "then retry" *after* the fix, but must not offer re-emitting the query as
    # the remedy — no rewrite of this statement helps.
    assert "re-emit" not in remediation.lower()


def test_the_unparseable_refusal_names_a_query_level_next_move():
    """Distinct from the above: this one the caller can act on by regenerating."""
    verdict = rt.check_scopable("SELECT `ssn` FROM `customers`", _org("PostgreSQL"))
    assert verdict is not None
    assert "re-emit" in verdict.remediation.lower()


def test_neither_refusal_echoes_the_statement_or_the_model(guarded):
    """A reason crosses the boundary between the model and the customer's database, so it
    carries the shape of the problem and never its contents."""
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT ssn FROM customers", _org(), spy)
    text = f"{ei.value.refusal.reason} {ei.value.refusal.remediation}"
    for leaked in ("ssn", "customers", "AcmeCorp", "SELECT"):
        assert leaked not in text, f"the refusal echoed {leaked!r}"


# --- the dialect-independent backstop ---------------------------------------------


def test_a_read_that_attributes_no_table_is_refused():
    """The shape a mis-read produces, caught without relying on the dialect map being
    complete — so a quoting style nobody has mapped yet still fails closed."""
    org = _org("PostgreSQL")
    verdict = rt.check_scopable("SELECT x FROM (SELECT 1 AS x) t", org)
    assert verdict is not None


def test_a_statement_that_reads_nothing_is_left_alone():
    """`SELECT 1` attributes no table because it reads nothing, not because it was mis-read."""
    assert rt.check_scopable("SELECT 1", _org("PostgreSQL")) is None


def test_an_ordinary_read_is_unaffected():
    assert rt.check_scopable("SELECT id FROM customers", _org("PostgreSQL")) is None


# --- a declared row filter that cannot be applied refuses, rather than running unscoped ---


def _org_with_unusable_filter() -> "m.Datasource":
    org = _org("PostgreSQL")
    # A filter the parser cannot make sense of. Authoring like this is a mistake — the point
    # is what happens when the mistake exists, not that it is plausible.
    org.subject_areas[0].tables_defined[0].default_filters = ["{alias}.id >>>= ("]
    return org


def test_an_unusable_row_filter_refuses_instead_of_running_unfiltered(guarded):
    """The caller keeps the rewritten SQL only when the applied list is non-empty, so an
    unusable filter that returned the statement unchanged would be indistinguishable from
    'this query needed no filter' — and the query would run with the model's row scoping
    silently absent. That is a fail-open in a governance control."""
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT id FROM customers", _org_with_unusable_filter(), spy)

    assert ei.value.refusal.kind == "model_unavailable", (
        "the fault is in the model, not the query, so it must not be reported as the query's"
    )
    assert spy.calls == [], "a query ran without the row scoping its model declares"


def test_the_row_scoping_refusal_points_at_the_model_not_the_query(guarded):
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT id FROM customers", _org_with_unusable_filter(), spy)
    assert "default_filters" in ei.value.refusal.remediation


def test_a_usable_row_filter_still_applies(guarded):
    """The refusal must not swallow the ordinary row-scoping path."""
    org = _org("PostgreSQL")
    org.subject_areas[0].tables_defined[0].default_filters = ["{alias}.id > 0"]
    spy = _SpyExecutor()

    guarded("SELECT id FROM customers", org, spy)
    assert spy.calls, "a governable statement was refused"
    assert "id > 0" in spy.calls[0][0].replace('"', ""), "the row filter did not reach the executor"


# --- the refusal does not depend on the unscopable posture switch ------------------


def test_the_engine_refusal_holds_under_the_unscopable_warn_posture(guarded, monkeypatch):
    """`AGAMI_SQL_UNSCOPABLE_POSTURE=warn` is a staged-rollout hatch for queries that cannot
    be scoped. A datasource that cannot be parsed at all is a configuration fault, not a
    query to wave through, so this refusal sits ahead of that switch."""
    monkeypatch.setenv("AGAMI_SQL_UNSCOPABLE_POSTURE", "warn")
    spy = _SpyExecutor()
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT ssn FROM customers", _org(), spy)
    assert ei.value.refusal.kind == "unscopable_sql"
    assert spy.calls == [], "warn posture let a statement run that could not be parsed at all"
