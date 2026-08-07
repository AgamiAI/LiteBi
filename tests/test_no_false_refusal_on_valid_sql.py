"""Tightening the parse must not start refusing valid SQL.

Reading every statement in the engine's own grammar, and refusing one that does not parse, buys
nothing if it also refuses queries that were fine. sqlglot re-implements each engine's grammar in
Python, so wherever its grammar is short of the real one a valid statement now refuses where it used
to be read as a truncated tree.

This measures that, from the two sources main actually carries: the SQL seeded with the sample
model, and a battery of the shapes a governed analytics query is made of, parsed under every
declared engine.

**What this does and does not establish.** Both sources are SQL written by hand and committed to
this repo. Neither is the SQL a language model generates against a real semantic model, which is the
population that actually matters for over-refusal and which nothing here samples. A zero means the
parser did not reject the shapes we already knew to write down. The other half of the measurement is
the rest of the suite: every existing test that expects a statement to be allowed is a
false-refusal check too, and they all had to stay green.

The branch version of this file also read `tests/safety/corpus.py`. That corpus is not on main — it
arrives with the safety-regression-corpus work, which is unported — so it is deliberately not stood
up here; doing that inside this change would absorb another slice. The per-engine battery below
replaces it and covers more engines than it did.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402
from semantic_model import sql_dialect as sd  # noqa: E402

# The sample model declares SQLite, so its seeded SQL is written for SQLite. Parsing each statement
# under the engine it was written for is the whole point — a statement is only valid with respect to
# some engine.
SAMPLE_DIALECT = "sqlite"

SAMPLE_EXAMPLES = sorted(
    (REPO_ROOT / "plugins" / "agami" / "samples").rglob("prompt_examples/*/examples.yaml")
)


def _seeded_example_sql() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in SAMPLE_EXAMPLES:
        for entry in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            sql = (entry or {}).get("sql")
            if sql:
                out.append((str(path.relative_to(REPO_ROOT)), sql))
    return out


SEEDED_SQL = _seeded_example_sql()

# The shapes a governed analytics query is built from. Engine-neutral on purpose: each one is run
# against every declared engine, so a map entry pointing at a grammar that cannot read ordinary SQL
# shows up here rather than at a customer's first query.
GOVERNED_SHAPES = {
    "projection": "SELECT id, name FROM customers",
    "qualified projection": "SELECT c.id, c.name FROM customers c",
    "filter": "SELECT id FROM customers WHERE name = 'acme' AND id > 10",
    "in-list": "SELECT id FROM customers WHERE id IN (1, 2, 3)",
    "aggregate": "SELECT count(id) AS n FROM customers",
    "group by + having": (
        "SELECT name, count(id) AS n FROM customers GROUP BY name HAVING count(id) > 1"
    ),
    "inner join": "SELECT c.id, o.id FROM customers c JOIN orders o ON o.cust_id = c.id",
    "left join": "SELECT c.id, o.id FROM customers c LEFT JOIN orders o ON o.cust_id = c.id",
    "cte": "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    "two ctes": (
        "WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM customers) "
        "SELECT a.id FROM a JOIN b ON b.id = a.id"
    ),
    "subquery in from": "SELECT t.id FROM (SELECT id FROM customers) t",
    "subquery in where": "SELECT id FROM orders WHERE cust_id IN (SELECT id FROM customers)",
    # Bare `UNION` is deliberately NOT here — it is not valid on every engine (see
    # `test_bigquery_requires_a_qualified_union`), and a battery asserting "valid everywhere" has to
    # hold only statements that are.
    "union all": "SELECT id FROM customers UNION ALL SELECT id FROM orders",
    "union distinct": "SELECT id FROM customers UNION DISTINCT SELECT id FROM orders",
    "order + limit": "SELECT id FROM customers ORDER BY id DESC LIMIT 10",
    "case expression": "SELECT CASE WHEN id > 1 THEN 'a' ELSE 'b' END AS bucket FROM customers",
    "window function": "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM customers",
    "distinct": "SELECT DISTINCT name FROM customers",
    "arithmetic": "SELECT id * 2 AS doubled FROM customers",
    "cast": "SELECT CAST(id AS VARCHAR) AS s FROM customers",
    "coalesce": "SELECT coalesce(name, 'unknown') AS n FROM customers",
    "line comment": "-- why this exists\nSELECT id FROM customers",
    "block comment": "SELECT /* inline */ id FROM customers",
}

ALL_ENGINES = list(get_args(m.StorageType))


def test_the_seeded_examples_were_found():
    """Guards against the glob silently matching nothing and the tests below passing empty."""
    assert SEEDED_SQL, "no seeded example SQL found to check"


def test_there_is_enough_valid_sql_to_be_evidence():
    """A delta of zero over a handful of statements would not mean much.

    Not a style rule — it is what makes the tests below able to fail. If the valid-SQL set shrinks
    back to a token few, over-refusal stops being measured.
    """
    total = len(SEEDED_SQL) + len(GOVERNED_SHAPES) * len(ALL_ENGINES)
    assert len(SEEDED_SQL) >= 12, f"only {len(SEEDED_SQL)} seeded statements"
    assert total >= 200, f"only {total} statement/engine pairs measured"


@pytest.mark.parametrize("path,sql", SEEDED_SQL, ids=[str(i) for i in range(len(SEEDED_SQL))])
def test_seeded_example_sql_still_parses(path, sql):
    tree, why = rt._parse_reporting(sql, SAMPLE_DIALECT)
    assert tree is not None, f"seeded example in {path} now refuses ({why}): {sql}"


@pytest.mark.parametrize("engine", ALL_ENGINES)
@pytest.mark.parametrize("shape", sorted(GOVERNED_SHAPES), ids=lambda s: s.replace(" ", "-"))
def test_a_governed_shape_parses_on_every_engine(shape, engine):
    sql = GOVERNED_SHAPES[shape]
    dialect = sd.sqlglot_dialect(engine)
    tree, why = rt._parse_reporting(sql, dialect)
    assert tree is not None, f"{engine}: {shape} now refuses ({why}): {sql}"


def test_bigquery_requires_a_qualified_union():
    """The one statement the battery above had to drop, and it is evidence rather than an exception.

    `SELECT ... UNION SELECT ...` parses on ten engines and is refused on BigQuery, because BigQuery
    genuinely requires `UNION ALL` or `UNION DISTINCT` — bare `UNION` is a syntax error there. So the
    refusal is not over-refusal: it is the guard declining to vet a statement the database would
    have rejected anyway, which is what reading the statement in the engine's own grammar means.
    Under the generic parse this was accepted and passed to a database that would not run it.
    """
    bare_union = "SELECT id FROM customers UNION SELECT id FROM orders"

    assert rt._parse_reporting(bare_union, sd.sqlglot_dialect("BigQuery"))[0] is None
    for engine in ("PostgreSQL", "MySQL", "SQLite", "Snowflake", "SQLServer"):
        tree, why = rt._parse_reporting(bare_union, sd.sqlglot_dialect(engine))
        assert tree is not None, f"{engine} rejected a bare UNION ({why})"


def test_the_measured_refusal_delta_is_zero():
    """The number reported in the pull request, computed rather than asserted by hand."""
    checked = [(SAMPLE_DIALECT, s) for _, s in SEEDED_SQL]
    checked += [(sd.sqlglot_dialect(e), s) for e in ALL_ENGINES for s in GOVERNED_SHAPES.values()]
    refused = [s for d, s in checked if rt._parse_reporting(s, d)[0] is None]
    assert not refused, f"{len(refused)}/{len(checked)} valid statements refused: {refused[:5]}"
