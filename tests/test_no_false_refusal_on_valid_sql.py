"""Tightening the parse must not start refusing valid SQL.

Reading every statement in the engine's own grammar, and refusing one that does not parse,
buys nothing if it also refuses queries that were fine. sqlglot re-implements each engine's
grammar in Python, so wherever its grammar is short of the real one a valid statement now
refuses where it used to be read as a truncated tree.

This measures that: every statement the repo carries as *valid* is parsed under its
declared engine with errors raised, and the count that fails must be zero.

What this does and does not establish. The corpus and the seeded examples are SQL written
by hand and committed to this repo. They are not the SQL a language model generates against
a real semantic model, which is the population that actually matters for over-refusal, and
which nothing here samples. A zero here means the parser did not reject the shapes we
already knew to write down.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from semantic_model import runtime as rt  # noqa: E402
from semantic_model.sql_dialect import sqlglot_dialect  # noqa: E402

from safety import corpus  # noqa: E402

# Parsing each statement under the engine it is written for is the whole point — a statement is
# only valid with respect to some engine. The corpus now carries statements written for engines
# other than SQLite (`SELECT TOP n [col]` is valid T-SQL and is not SQLite at all), so a case
# pinned to engines is checked under EACH of them, and only an unpinned case — one claimed to be
# valid everywhere — falls back to the default. Checking those under one fixed dialect would
# either fail on valid SQL or, worse, quietly stop covering the engines the corpus added.
DEFAULT_CORPUS_ENGINE = "SQLite"
CORPUS_DIALECT = sqlglot_dialect(DEFAULT_CORPUS_ENGINE)  # the seeded sample model declares SQLite

VALID_CORPUS_SQL = [c for c in corpus.CASES if c.expect == "ok"]

# (case, engine) pairs — the unit actually being measured, since one case can be valid on several.
VALID_CORPUS_PAIRS = [
    (c, e) for c in VALID_CORPUS_SQL for e in (c.engines or (DEFAULT_CORPUS_ENGINE,))
]

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


def test_the_corpus_carries_enough_valid_sql_to_be_evidence():
    """A delta of zero over a handful of statements would not mean much.

    This is not a style rule — it is what makes the two tests below able to fail. If the
    valid-SQL set shrinks back to a token few, over-refusal stops being measured.
    """
    assert len(VALID_CORPUS_SQL) >= 12, (
        f"only {len(VALID_CORPUS_SQL)} valid statements in the corpus; a zero refusal delta "
        "over so few proves almost nothing about over-refusal"
    )


def test_the_seeded_examples_were_found():
    """Guards against the glob silently matching nothing and the test below passing empty."""
    assert SEEDED_SQL, "no seeded example SQL found to check"


@pytest.mark.parametrize(
    "case,engine", VALID_CORPUS_PAIRS, ids=[f"{c.id}@{e}" for c, e in VALID_CORPUS_PAIRS]
)
def test_valid_corpus_sql_still_parses(case, engine):
    tree, why = rt._parse_reporting(case.sql, sqlglot_dialect(engine))
    assert tree is not None, f"valid corpus SQL now refuses on {engine} ({why}): {case.sql}"


@pytest.mark.parametrize(
    "path,sql", SEEDED_SQL, ids=[f"{i}" for i in range(len(SEEDED_SQL))]
)
def test_seeded_example_sql_still_parses(path, sql):
    tree, why = rt._parse_reporting(sql, CORPUS_DIALECT)
    assert tree is not None, f"seeded example in {path} now refuses ({why}): {sql}"


def test_the_measured_refusal_delta_is_zero():
    """The number reported in the pull request, computed rather than asserted by hand."""
    checked = [(c.sql, sqlglot_dialect(e)) for c, e in VALID_CORPUS_PAIRS] + [
        (s, CORPUS_DIALECT) for _, s in SEEDED_SQL
    ]
    refused = [s for s, dialect in checked if rt._parse_reporting(s, dialect)[0] is None]
    assert not refused, f"{len(refused)}/{len(checked)} valid statements refused: {refused}"
