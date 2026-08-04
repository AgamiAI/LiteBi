"""The guards read a statement in the engine's own grammar, so quoting cannot hide it.

The model-scoping gates decide by inspecting a parsed tree. Parsed in the wrong grammar the tree
does not describe the statement, and a gate with nothing to object to allows it. On a
backtick-quoting engine that is not subtle: ``SELECT `ssn` FROM `customers``` parsed generically
yields *no tables and no columns*, so table scope, column scope and the star ban all pass, and the
statement reads whatever it likes. The bracket-quoting case is worse — the tree is confidently
wrong rather than empty, naming a column that is really a row-limit keyword.

These tests pin the verdict per engine, for the quoting each engine actually uses. Every member of
`StorageType` appears, including the engines where the defect cannot occur, because a wrong map
entry shows up as a wrong verdict rather than as a crash. None of them needs a database: every gate
reaches its verdict before `_load_credentials` is called, so the whole matrix runs on the parse.

**Not covered here, deliberately.** The branch version of this file also asserted that the guard's
*regenerated* SQL was emitted in the engine's grammar — the row-filter injection and the fan-join
rewrite. Neither exists any more: ACE-042 deleted the filter injection and ACE-093 deleted the
rewrite and pinned byte-identity of the executed statement, so nothing in the guard path
regenerates SQL and there is nothing to emit. `tests/test_ace093_byte_identity.py` holds that
property now, and it is the stronger one — byte-identity implies tree-equivalence and does not
depend on sqlglot's generator agreeing with its parser.

Synthetic, generic names only — `customers`, `orders`, `AcmeCorp`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

# Engines whose identifier quoting differs from the ANSI double quote. These are the ones a generic
# parse silently mis-reads; the double-quoting engines are the unaffected baseline.
BACKTICK_ENGINES = ["MySQL", "BigQuery", "Databricks"]
BRACKET_ENGINES = ["SQLServer"]
ANSI_ENGINES = ["PostgreSQL", "Snowflake", "Redshift", "DuckDB", "Oracle", "Trino", "SQLite"]
ALL_ENGINES = BACKTICK_ENGINES + BRACKET_ENGINES + ANSI_ENGINES


def test_the_matrix_covers_every_declared_engine():
    """The lists above are hand-written, so they can drift from the type. Asserted against
    `StorageType` itself, which is what makes a twelfth engine fail here rather than go untested."""
    from typing import get_args

    assert set(ALL_ENGINES) == set(get_args(m.StorageType))


def _org(storage_type: str = "PostgreSQL") -> "m.Datasource":
    """`customers` declares a sensitive `ssn`; `orders` is an ordinary joinable table."""
    customers = m.Table(
        name="customers",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="ssn", type="string", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="cust_id", type="integer")],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders])
    return m.Datasource(
        datasource="AcmeCorp",
        version=1,
        storage_connections=[m.StorageConnection(name="c", storage_type=storage_type)],
        subject_areas=[sa],
    )


def _quote(engine: str, name: str) -> str:
    if engine in BACKTICK_ENGINES:
        return f"`{name}`"
    if engine in BRACKET_ENGINES:
        return f"[{name}]"
    return f'"{name}"'


class _SpyExecutor:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, vetted_sql, creds, *, profile):
        self.calls.append((vetted_sql, creds, profile))
        return execute_sql.ExecResult(columns=["id"], rows=[(1,)])


@pytest.fixture
def guarded(monkeypatch):
    """Drive the real chokepoint with a recording stub, so a refusal is proved by the executor
    never being reached and an allowed statement by what reaches it."""

    def _run(sql: str, org, spy):
        monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: org)
        engine = org.storage_connections[0].storage_type
        creds_type = {"SQLServer": "sqlserver", "PostgreSQL": "postgres"}.get(
            engine, engine.lower()
        )
        monkeypatch.setattr(
            execute_sql,
            "_load_credentials",
            lambda p, org_id="local": {"type": creds_type, "path": ":memory:"},
        )
        return execute_sql.execute_guarded(sql, "acme", None, executor=spy)

    return _run


# --- the tree the gates inspect actually describes the statement ------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_tables_and_columns_are_attributed_under_native_quoting(engine):
    """The defect in one assertion: parsed generically this yields no tables at all."""
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'ssn')} FROM {_quote(engine, 'customers')}"
    ctx = rt.build_guard_context(sql, org)

    assert ctx is not None and ctx.tree is not None, f"{engine}: statement did not parse"
    tables = {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)}
    assert tables == {"customers"}, f"{engine}: attributed {tables} instead of the real table"


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_undeclared_table_is_refused_under_native_quoting(engine):
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'undeclared_table')}"
    assert rt.check_table_scope(sql, org) is not None, f"{engine}: undeclared table allowed"


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_undeclared_column_is_refused_under_native_quoting(engine):
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'nosuchcol')} FROM {_quote(engine, 'customers')}"
    assert rt.check_column_scope(sql, org) is not None, f"{engine}: undeclared column allowed"


@pytest.mark.parametrize("engine", BACKTICK_ENGINES + BRACKET_ENGINES)
def test_sensitive_column_is_seen_under_native_quoting(engine):
    """Not a refusal on main — ACE-094 moved this analysis onto the receipt as a fact. It is still
    the disclosure that matters: read generically the gate sees no columns, so it reports nothing
    projected and the answer arrives looking clean."""
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'ssn')} FROM {_quote(engine, 'customers')}"
    assert rt.projected_sensitive_columns(sql, org) == ["customers.ssn"], (
        f"{engine}: sensitive projection went unnoticed"
    )


@pytest.mark.parametrize("engine", BACKTICK_ENGINES + BRACKET_ENGINES)
def test_sensitive_column_is_seen_when_qualified_by_a_quoted_alias(engine):
    """A qualified column resolves through a different branch than a bare one, so the aliased form
    is its own case rather than a restatement of the one above."""
    org = _org(engine)
    c, ssn, customers = _quote(engine, "c"), _quote(engine, "ssn"), _quote(engine, "customers")
    sql = f"SELECT {c}.{ssn} FROM {customers} {c}"
    assert rt.projected_sensitive_columns(sql, org) == ["customers.ssn"], (
        f"{engine}: sensitive projection went unnoticed when qualified by a quoted alias"
    )


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_receipt_attributes_the_table_under_native_quoting(engine):
    """A receipt that attributes nothing under-reports what the answer read."""
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'customers')}"
    receipt = rt.assemble_receipt(org, sql)
    attributed = [t["qname"].lower() for t in receipt["tables"]["items"]]
    assert attributed == ["public.customers"], f"{engine}: receipt attributed {attributed}"


# --- the three divergences measured on the generic dialect ------------------------


def test_bigquery_dotted_path_in_one_backtick_pair():
    """Generically this parses to a table literally named '`', so scope has nothing to check."""
    org = _org("BigQuery")
    ctx = rt.build_guard_context("SELECT id FROM `proj.ds.customers`", org)
    assert ctx is not None and ctx.tree is not None
    names = {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)}
    assert names == {"customers"}, f"attributed {names}"


def test_sqlserver_top_with_bracket_identifiers():
    """Worse than the empty tree: generically this attributes a column named `TOP`, so the gate
    reports a confident wrong answer rather than nothing."""
    org = _org("SQLServer")
    ctx = rt.build_guard_context("SELECT TOP 5 [ssn] FROM [customers]", org)
    assert ctx is not None and ctx.tree is not None
    assert {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)} == {"customers"}
    cols = {c.name.lower() for c in ctx.tree.find_all(sqlglot.exp.Column)}
    assert "ssn" in cols and "top" not in cols, f"attributed {cols}"


def test_mysql_hash_line_comment():
    """`#` starts a comment in MySQL and does not in the generic grammar, so generically the FROM
    clause is lost with the rest of the line."""
    org = _org("MySQL")
    ctx = rt.build_guard_context("SELECT id # note\nFROM customers", org)
    assert ctx is not None and ctx.tree is not None
    assert {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)} == {"customers"}


# --- unchanged behaviour for the engines this defect cannot reach -----------------


@pytest.mark.parametrize("engine", ANSI_ENGINES)
def test_double_quoted_statements_are_unaffected(engine):
    org = _org(engine)
    assert rt.check_table_scope('SELECT "id" FROM "customers"', org) is None
    assert rt.check_column_scope('SELECT "id" FROM "customers"', org) is None


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_unquoted_statements_are_allowed_on_every_engine(engine):
    """The ordinary case, on all eleven. A map entry naming a grammar sqlglot rejects, or one
    pointing at the wrong engine, shows up here as a refusal of a statement nothing is wrong with."""
    org = _org(engine)
    assert rt.check_table_scope("SELECT id FROM customers", org) is None
    assert rt.check_column_scope("SELECT id FROM customers", org) is None
    assert rt.check_readable("SELECT id FROM customers", org) is None


# --- constructs that do NOT diverge today, pinned so an upgrade cannot regress them ---


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        ("Snowflake", "SELECT id FROM customers QUALIFY ROW_NUMBER() OVER (ORDER BY id) = 1"),
        ("Oracle", "SELECT id FROM customers ORDER BY id FETCH FIRST 1 ROWS ONLY"),
        ("BigQuery", "SELECT id FROM customers WHERE id IN UNNEST([1, 2])"),
        ("Trino", "SELECT id FROM customers WHERE id = 1"),
        ("Databricks", "SELECT id FROM customers TABLESAMPLE (1 PERCENT)"),
    ],
)
def test_a_non_diverging_construct_stays_readable(engine, sql):
    """These parse identically with and without the dialect today, which is exactly why nobody
    would re-check them after a dependency bump. Pinning them is what makes this a regression
    matrix rather than a snapshot of one afternoon's findings."""
    org = _org(engine)
    assert rt.check_readable(sql, org) is None, f"{engine}: {sql!r} became unreadable"


# --- a parse that fails is reported, not silently truncated -----------------------


def test_parse_failure_is_reported_rather_than_truncated():
    """The error level must be the enum. As a string it matches no branch in sqlglot's own
    `check_errors`, so every collected error is discarded and the caller gets a truncated tree."""
    tree, why = rt._parse_reporting("SELECT id FROM customers WHERE (((", "postgres")
    assert tree is None
    assert why, "a failed parse reported no reason"


def test_lexical_failure_is_caught_too():
    """`TokenError` is a different class from `ParseError` and is raised regardless of the error
    level, so a handler catching only the latter would crash instead of refusing."""
    tree, why = rt._parse_reporting("SELECT 'unterminated FROM customers", "postgres")
    assert tree is None
    assert why


def test_a_wrong_grammar_statement_is_refused_not_truncated():
    """Backticks on PostgreSQL: the composition of both halves. Without the enum this truncates
    silently and the gates see nothing; with it the statement is refused."""
    refusal = rt.check_readable("SELECT `ssn` FROM `customers`", _org("PostgreSQL"))
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_UNPARSEABLE


# --- the same verdict with and without the shared context -------------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_ctx_and_standalone_agree(engine):
    """ACE-045's parity property, extended to the dialect: a gate given the shared context returns
    the same verdict as one that resolves its own grammar."""
    org = _org(engine)
    for sql in (
        f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'customers')}",
        f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'undeclared_table')}",
        f"SELECT {_quote(engine, 'nosuchcol')} FROM {_quote(engine, 'customers')}",
    ):
        ctx = rt.build_guard_context(sql, org)
        assert rt.check_table_scope(sql, org) == rt.check_table_scope(sql, org, ctx=ctx), sql
        assert rt.check_column_scope(sql, org) == rt.check_column_scope(sql, org, ctx=ctx), sql
        assert rt.check_readable(sql, org) == rt.check_readable(sql, org, ctx=ctx), sql


# --- a double-quoted token on a backtick engine is ambiguous, so it is refused -----


@pytest.mark.parametrize("engine", BACKTICK_ENGINES)
def test_double_quoted_projection_is_refused_on_backtick_engines(engine):
    """A regression this change would otherwise have INTRODUCED.

    sqlglot reads `"ssn"` as a string literal under these grammars, so the projected sensitive
    column resolves to no column at all and the scope gates find nothing — where the generic parse
    being replaced read it as an identifier and caught it. MySQL under ANSI_QUOTES (and the
    Spark-family equivalent) really does treat it as an identifier, so the statement's meaning
    depends on server configuration the guard cannot see. Refused rather than guessed.
    """
    org = _org(engine)
    refusal = rt.check_readable('SELECT "ssn" FROM customers', org)
    assert refusal is not None, f"{engine}: an ambiguous double-quoted token was allowed"
    assert refusal.rule == guardrail.RULE_UNPARSEABLE
    assert "re-emit" in refusal.remediation.lower()


@pytest.mark.parametrize("engine", BACKTICK_ENGINES)
def test_a_genuine_string_literal_is_unaffected(engine):
    """The other side of the ambiguity: a single-quoted literal is a literal under both readings,
    so nothing about it is in doubt and it must not be caught by the check above."""
    org = _org(engine)
    assert rt.check_readable("SELECT id FROM customers WHERE name = 'acme'", org) is None


@pytest.mark.parametrize("engine", ANSI_ENGINES)
def test_double_quoting_is_not_ambiguous_on_an_ansi_engine(engine):
    """Where the double quote IS the identifier quote there is no second reading to worry about."""
    org = _org(engine)
    assert rt.check_readable('SELECT "ssn" FROM "customers"', org) is None


# --- the verdict at the chokepoint, with a recording stub -------------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_an_undeclared_table_never_reaches_the_executor(guarded, engine):
    """Asserted through `execute_guarded` rather than the gate in isolation, because the
    chokepoint is where the guarantee has to hold."""
    spy = _SpyExecutor()
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'undeclared_table')}"
    env = guarded(sql, org, spy)

    assert env.status == "refused", f"{engine}: {env}"
    assert spy.calls == [], f"{engine}: an undeclared table reached the database"


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_a_governed_statement_reaches_the_executor_unaltered(guarded, engine):
    """The other half: the matrix must not prove its point by refusing everything. What reaches
    the stub is the caller's own bytes, which is ACE-093's property holding under every grammar."""
    spy = _SpyExecutor()
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'customers')}"
    env = guarded(sql, org, spy)

    assert env.status == "ok", f"{engine}: {env}"
    assert spy.calls and spy.calls[0][0] == sql, f"{engine}: the executor got {spy.calls}"
