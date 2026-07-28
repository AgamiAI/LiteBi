"""The guards read a statement in the engine's own grammar, so quoting cannot hide it.

The model-scoping gates decide by inspecting a parsed tree. Parsed in the wrong grammar the
tree does not describe the statement, and a gate with nothing to object to allows it. On a
backtick-quoting engine that is not subtle: ``SELECT `ssn` FROM `customers``` parsed
generically yields *no tables and no columns*, so table scope, column scope and the
sensitive-projection gate all pass and the statement returns the column verbatim. The
bracket-quoting case is worse — the tree is confidently wrong rather than empty, naming a
column that is really a row-limit keyword.

These tests pin the verdict per engine, for the quoting each engine actually uses. They run
against the shared executor chokepoint with a recording stub, so a refusal is proved by the
executor never being reached, and an allowed statement is proved by what reaches it.

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
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

# Engines whose identifier quoting differs from the ANSI double quote. These are the ones a
# generic parse silently mis-reads; the double-quoting engines are the unaffected baseline.
BACKTICK_ENGINES = ["MySQL", "BigQuery", "Databricks"]
BRACKET_ENGINES = ["SQLServer"]
ANSI_ENGINES = ["PostgreSQL", "Snowflake", "Redshift", "DuckDB", "Oracle", "Trino", "SQLite"]
ALL_ENGINES = BACKTICK_ENGINES + BRACKET_ENGINES + ANSI_ENGINES


def _org(storage_type: str = "PostgreSQL") -> "m.Datasource":
    """`customers` declares a sensitive `ssn`; `orders` is an ordinary joinable table."""
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="ssn", type="string", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
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
    """The disclosure: the PII gate must not report `allow` because it saw no columns."""
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'ssn')} FROM {_quote(engine, 'customers')}"
    assert rt.check_sensitive_projection(sql, org).action != "allow", (
        f"{engine}: sensitive projection went unnoticed"
    )


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_receipt_attributes_the_table_under_native_quoting(engine):
    """A receipt that attributes nothing under-reports what the answer read."""
    org = _org(engine)
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'customers')}"
    receipt = rt.assemble_receipt(org, sql)
    attributed = [t["qname"].lower() for t in receipt.get("tables_used", [])]
    assert attributed == ["public.customers"], (
        f"{engine}: receipt attributed {receipt.get('tables_used')}"
    )


# --- the three divergences measured on the generic dialect ------------------------


def test_bigquery_dotted_path_in_one_backtick_pair():
    """Generically this parses to a table literally named '`', so scope has nothing to check."""
    org = _org("BigQuery")
    ctx = rt.build_guard_context("SELECT id FROM `proj.ds.customers`", org)
    names = {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)}
    assert names == {"customers"}
    assert rt.check_table_scope("SELECT id FROM `proj.ds.undeclared`", org) is not None


def test_sqlserver_top_with_bracket_identifiers():
    """Generically this yields zero tables *and* a phantom column named TOP."""
    org = _org("SQLServer")
    ctx = rt.build_guard_context("SELECT TOP 5 [ssn] FROM [customers]", org)
    assert {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)} == {"customers"}
    assert {c.name.lower() for c in ctx.tree.find_all(sqlglot.exp.Column)} == {"ssn"}
    assert rt.check_sensitive_projection("SELECT TOP 5 [ssn] FROM [customers]", org).action != "allow"


def test_mysql_hash_line_comment():
    """`#` starts a comment in MySQL; generically the parse truncates and attributes nothing."""
    org = _org("MySQL")
    sql = "SELECT ssn # trailing comment\nFROM customers"
    ctx = rt.build_guard_context(sql, org)
    assert {t.name for t in ctx.tree.find_all(sqlglot.exp.Table)} == {"customers"}
    assert rt.check_sensitive_projection(sql, org).action != "allow"


# --- unchanged behaviour for the engines this defect cannot reach -----------------


@pytest.mark.parametrize("engine", ANSI_ENGINES)
def test_unquoted_and_double_quoted_statements_are_unaffected(engine):
    org = _org(engine)
    assert rt.check_table_scope("SELECT id FROM customers", org) is None
    assert rt.check_column_scope("SELECT id FROM customers", org) is None
    assert rt.check_table_scope('SELECT "id" FROM "customers"', org) is None
    assert rt.check_column_scope('SELECT "id" FROM "customers"', org) is None


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_unquoted_statements_are_allowed_on_every_engine(engine):
    """No over-refusal: the plain form every engine accepts must stay allowed everywhere."""
    org = _org(engine)
    assert rt.check_table_scope("SELECT id FROM customers", org) is None
    assert rt.check_column_scope("SELECT id FROM customers", org) is None
    assert rt.check_no_select_star("SELECT id FROM customers") is None


# --- a parse that fails is reported, not silently truncated -----------------------


def test_parse_failure_is_reported_rather_than_truncated():
    """A string error level is inert — sqlglot compares against the enum, so a string level
    matches no branch and every collected error is discarded, leaving a truncated tree."""
    tree, why = rt._parse_reporting("SELECT `ssn` FROM `customers`", "postgres")
    assert tree is None and why


def test_lexical_failure_is_caught_too():
    """An unterminated literal raises TokenError regardless of error level, so a handler that
    catches only ParseError would crash instead of refusing."""
    tree, why = rt._parse_reporting("SELECT 'abc FROM customers", "postgres")
    assert tree is None and why


def test_parse_reports_both_error_classes_across_supported_sqlglot():
    from sqlglot.errors import ParseError, TokenError

    with pytest.raises(ParseError):
        sqlglot.parse_one("SELECT FROM WHERE FROM", dialect="postgres",
                          error_level=sqlglot.errors.ErrorLevel.RAISE)
    with pytest.raises(TokenError):
        sqlglot.parse_one("SELECT 'abc FROM t", dialect="postgres",
                          error_level=sqlglot.errors.ErrorLevel.RAISE)


# --- the same verdict with and without the shared context -------------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_ctx_and_standalone_agree(engine):
    """`ctx=None` must not be a cheaper, weaker path — the dialect reaches both."""
    org = _org(engine)
    for sql in (
        f"SELECT {_quote(engine, 'ssn')} FROM {_quote(engine, 'customers')}",
        f"SELECT {_quote(engine, 'nosuchcol')} FROM {_quote(engine, 'customers')}",
        f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'undeclared_table')}",
        "SELECT id FROM customers",
    ):
        ctx = rt.build_guard_context(sql, org)
        assert _verdict(rt.check_table_scope(sql, org)) == _verdict(
            rt.check_table_scope(sql, org, ctx)
        ), f"{engine}: table scope differs with ctx"
        assert _verdict(rt.check_column_scope(sql, org)) == _verdict(
            rt.check_column_scope(sql, org, ctx)
        ), f"{engine}: column scope differs with ctx"
        assert rt.check_sensitive_projection(sql, org).action == rt.check_sensitive_projection(
            sql, org, ctx
        ).action, f"{engine}: sensitive projection differs with ctx"
        assert _verdict(rt.check_no_select_star(sql, dialect=ctx.dialect)) == _verdict(
            rt.check_no_select_star(sql, ctx)
        ), f"{engine}: select-star differs with ctx"


def _verdict(v):
    return None if v is None else v.as_dict()


# --- the statement the guard vetted round-trips in the same grammar ---------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_row_scoping_regenerates_valid_sql_for_the_engine(engine):
    """`apply_default_filters` rewrites the statement the executor then runs, so it must emit
    in the engine's grammar and the result must re-parse there to the same tree."""
    org = _org(engine)
    org.subject_areas[0].tables_defined[0].default_filters = ["{alias}.id > 0"]
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'customers')}"

    out, applied = rt.apply_default_filters(sql, org)
    assert applied, f"{engine}: row scoping was not applied"

    dialect = rt._dialect_of(org)[0]
    reparsed, why = rt._parse_reporting(out, dialect)
    assert reparsed is not None, f"{engine}: regenerated SQL does not parse for its own engine ({why})"
    assert reparsed.sql(dialect=dialect) == out
    assert {t.name for t in reparsed.find_all(sqlglot.exp.Table)} == {"customers"}


# --- end to end, at the shared chokepoint ----------------------------------------


class _SpyExecutor:
    def __init__(self, result):
        self.calls: list[tuple] = []
        self._result = result

    def execute(self, vetted_sql, creds, *, profile):
        self.calls.append((vetted_sql, creds, profile))
        return self._result


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


@pytest.mark.parametrize("engine", BACKTICK_ENGINES + BRACKET_ENGINES)
def test_undeclared_table_never_reaches_the_executor(guarded, engine):
    """A refused statement is proved refused by the executor never being called."""
    org = _org(engine)
    spy = _SpyExecutor(execute_sql.ExecResult(columns=["id"], rows=[(1,)]))
    sql = f"SELECT {_quote(engine, 'id')} FROM {_quote(engine, 'undeclared_table')}"

    with pytest.raises(execute_sql.GuardRefused):
        guarded(sql, org, spy)
    assert spy.calls == [], f"{engine}: the executor ran a statement the model does not declare"


@pytest.mark.parametrize("engine", BACKTICK_ENGINES + BRACKET_ENGINES)
def test_sensitive_column_is_never_returned_raw(guarded, engine):
    """Masked or refused — never the value itself."""
    org = _org(engine)
    spy = _SpyExecutor(execute_sql.ExecResult(columns=["ssn"], rows=[("111-22-3333",)]))
    sql = f"SELECT {_quote(engine, 'ssn')} FROM {_quote(engine, 'customers')}"

    try:
        env = guarded(sql, org, spy)
    except execute_sql.GuardRefused:
        assert spy.calls == []
        return
    returned = [str(v) for row in env.rows for v in row]
    assert "111-22-3333" not in returned, f"{engine}: sensitive value returned verbatim"
