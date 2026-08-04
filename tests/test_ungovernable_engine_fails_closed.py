"""A statement that cannot be read in a known grammar is refused, not run blind.

The guards decide by reading the statement. If the datasource does not say which engine it runs on
there is no grammar to read it in, so no gate below can reach a verdict worth trusting — and a gate
that cannot object is indistinguishable from a gate with nothing to object to. The statement is
therefore refused before any database is touched, whatever the model happens to declare and
whichever gate would otherwise have run.

**Unconditional on purpose.** An earlier draft refused only when the model declared a `sensitive`
column, which would have made the guarantee depend on how diligently the model was authored. Both
spellings are asserted below.

The refusal has to be usable, which means the situations get different next moves: a statement the
caller can re-emit, and a datasource only an operator can fix. The second must not invite a retry —
an unactionable "try again" turns a configuration fault into a retry loop.

The last section covers the other direction of the same fault. The guard picks its grammar from the
MODEL's declared engine and the executor picks its driver from the CREDENTIALS, and nothing
reconciles the two: a mis-declared model has the guard vet a statement in a grammar the database
does not speak, which is the defect this whole slice exists to close, arriving by a different door.

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
import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

# 000-00-0000 is never issued, so no scanner or reader can mistake it for a real SSN.
SENSITIVE_VALUE = "000-00-0000"


def _org(*storage_types: str, sensitive: bool = True) -> "m.Datasource":
    customers = m.Table(
        name="customers",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="ssn", type="string", sensitive=sensitive),
        ],
    )
    orders = m.Table(
        name="orders",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name="id", type="integer")],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders])
    return m.Datasource(
        datasource="AcmeCorp",
        version=1,
        # The first connection is named "c" because that is what the tables above reference; extras
        # get suffixed names. A fixture whose tables point at a connection it never declares would
        # be internally inconsistent.
        #
        # Passing NO storage types is the exception, and deliberately so: it models a datasource
        # that declares no engine, which is the state these tests exist to refuse.
        # `Table.storage_connection` is required, so the table necessarily names a connection that
        # is not declared — that dangling reference IS the condition under test, not an oversight.
        storage_connections=[
            m.StorageConnection(name="c" if i == 0 else f"c{i}", storage_type=st)
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
    def _run(sql: str, org, spy, creds_type: str = "postgres", **kw):
        monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: org)
        # Credentials for the engine the model declares, by default — the guard also refuses a
        # model/credential engine mismatch, which would otherwise mask what most of these are
        # checking. The last section overrides it deliberately.
        monkeypatch.setattr(
            execute_sql,
            "_load_credentials",
            lambda p, org_id="local": {"type": creds_type, "path": ":memory:"},
        )
        return execute_sql.execute_guarded(sql, "acme", None, executor=spy, **kw)

    return _run


# --- the refusal is unconditional -------------------------------------------------


@pytest.mark.parametrize("sensitive", [True, False], ids=["pii-declared", "no-pii-declared"])
def test_no_declared_engine_refuses_regardless_of_declared_pii(guarded, sensitive):
    """Governance cannot be conditional on how diligently the model was authored."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org(sensitive=sensitive), spy)
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert spy.calls == [], "the executor ran against an engine the guard could not parse for"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM customers",  # would have passed every gate
        "SELECT id FROM undeclared",  # would have been refused by table scope
        "SELECT ssn FROM customers",  # reads a column the model marks sensitive
        "SELECT * FROM customers",  # would have been refused by the star ban
    ],
)
def test_no_declared_engine_refuses_whichever_gate_would_have_run(guarded, sql):
    spy = _SpyExecutor()
    env = guarded(sql, _org(), spy)
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert spy.calls == []


def test_disagreeing_engines_refuse(guarded):
    """One datasource resolves to one database, so two declared engines are ambiguous."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org("PostgreSQL", "MySQL"), spy)
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_MODEL_UNAVAILABLE
    assert spy.calls == []


def test_a_declared_engine_still_runs(guarded):
    """The fail-closed must not swallow the ordinary case."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org("PostgreSQL"), spy)
    assert env.status == "ok", env
    assert spy.calls, "a governable statement was refused"


# --- the refusals are distinguishable and actionable ------------------------------


def test_the_unmapped_engine_refusal_names_the_operator_action_and_invites_no_retry(guarded):
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org(), spy)
    remediation = env.refusal.remediation

    assert "storage_type" in remediation, "the operator is not told what to declare"
    # It may say "then retry" *after* the fix, but must not offer re-emitting the query as the
    # remedy — no rewrite of this statement helps.
    assert "re-emit" not in remediation.lower()


def test_the_unparseable_refusal_names_a_query_level_next_move():
    """Distinct from the above: this one the caller can act on by regenerating.

    Backticks are not PostgreSQL's identifier quote, so on a PostgreSQL datasource this statement
    does not parse at all — which is a fact about the statement rather than about the deployment.
    """
    refusal = rt.check_readable("SELECT `ssn` FROM `customers`", _org("PostgreSQL"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_UNPARSEABLE
    assert "re-emit" in refusal.remediation.lower()


def test_neither_refusal_echoes_the_statement_or_the_model(guarded):
    """A reason crosses the boundary between the model and the customer's database, so it carries
    the shape of the problem and never its contents."""
    spy = _SpyExecutor()
    env = guarded("SELECT ssn FROM customers", _org(), spy)
    text = f"{env.refusal.detail} {env.refusal.remediation}"
    for leaked in ("ssn", "customers", "AcmeCorp", "SELECT"):
        assert leaked not in text, f"the refusal echoed {leaked!r}"


# --- the dialect-independent backstop ---------------------------------------------


def test_a_read_that_attributes_no_table_is_refused():
    """The shape a mis-read produces, caught without relying on the dialect map being complete —
    so a quoting style nobody has mapped yet still fails closed.

    `unscopable` rather than `unparseable`: the statement parsed perfectly well. There is simply
    nothing in the result for the scope walk to accept or reject, which is what that rule names.
    """
    refusal = rt.check_readable("SELECT c.x FROM (VALUES (1)) AS c(x)", _org("PostgreSQL"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_UNSCOPABLE


def test_a_statement_that_reads_nothing_is_left_alone():
    """`SELECT 1` attributes no table because it reads nothing, not because it was mis-read."""
    assert rt.check_readable("SELECT 1", _org("PostgreSQL")) is None


def test_an_ordinary_read_is_unaffected():
    assert rt.check_readable("SELECT id FROM customers", _org("PostgreSQL")) is None


# --- the model's declared engine must be the one the credentials reach ------------


@pytest.mark.parametrize(
    ("declared", "creds_type"),
    [("PostgreSQL", "mysql"), ("MySQL", "postgres")],
    ids=["declared-postgres-connects-mysql", "declared-mysql-connects-postgres"],
)
def test_a_model_declaring_one_engine_against_another_refuses(guarded, declared, creds_type):
    """Both directions, because the fault is symmetric: whichever way round it is, the guard read
    the statement in a grammar the database does not speak."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org(declared), spy, creds_type=creds_type)
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_ENGINE_MISMATCH
    assert spy.calls == [], "a statement vetted in the wrong grammar reached the database"


def test_the_mismatch_refusal_points_at_the_deployment_and_names_no_engine(guarded):
    """It is the operator's to fix, and it must not name either engine: the caller sent SQL and is
    owed no fact about how the datasource is configured."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org("PostgreSQL"), spy, creds_type="mysql")
    text = f"{env.refusal.detail} {env.refusal.remediation}"

    assert "storage_type" in env.refusal.remediation
    assert "re-emit" not in text.lower(), "no rewrite of the query fixes a mis-declared datasource"
    for leaked in ("PostgreSQL", "postgres", "MySQL", "mysql", "AcmeCorp", "customers"):
        assert leaked not in text, f"the refusal named {leaked!r}"


def test_an_unmapped_credential_type_is_not_a_mismatch(guarded):
    """Silence here is deliberate. The executor rejects an unusable credential type itself, with a
    message about the credential; refusing here as well would relabel a plain configuration error
    as a governance refusal that misdescribes it."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org("PostgreSQL"), spy, creds_type="not-an-engine")
    assert env.status == "ok", env
    assert spy.calls, "an unmapped credential type was treated as a governance failure"


def test_a_matching_engine_still_runs(guarded):
    """The mismatch check must not swallow the ordinary case, including the aliases that ride the
    same wire protocol — `supabase` is hosted PostgreSQL, so it agrees with a PostgreSQL model."""
    spy = _SpyExecutor()
    env = guarded("SELECT id FROM customers", _org("PostgreSQL"), spy, creds_type="supabase")
    assert env.status == "ok", env
    assert spy.calls
