"""Deciding whether two result sets say the same thing.

Canonical keys for the cells first, so two result sets can be compared at all.

A comparison asks whether the answer key's rows and the rows a generated statement produced say
the same thing. Doing that on the raw cells does not work, because the Python objects a driver
hands back are not the values a reader means:

* ``True == 1 == 1.0 == Decimal(1)`` and all four hash alike, so ``Counter`` collapses them into
  one bucket. A boolean column would compare equal to an integer column of zeros and ones, and
  the comparison would report a match it never checked.
* ``float('nan') != float('nan')``, yet dict's identity fast-path collapses the *same* NaN object
  anyway — so a row count would depend on whether the driver happened to reuse an object.
* ``Decimal('0.1') != 0.1``, because one is a decimal and the other the nearest binary float. The
  same number read through two drivers would disagree.

So every cell is first turned into a ``(type_tag, value)`` tuple: hashable, safely usable as a
``Counter`` key, and carrying the type distinction the raw value throws away. Values are
canonicalised only where two spellings genuinely mean one thing — a padded decimal, a date written
as text, a driver's ``'t'`` for true. Text itself is left exactly as it came: stripping or
case-folding would hide a real difference between two result sets, which is the one thing a
comparator must never do.

On top of those keys sits the comparison itself, in three steps that are deliberately separate:
whether the answer key asked for an ordering at all, which generated column answers which golden
one, and how far the rows agree once the columns are paired. Column identity is decided by VALUES
and never by name or position — a generated statement is free to alias a total and to select it
second — and rows are compared as a multiset unless the author ordered them, because duplicates
are signal and order usually is not.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Context, Decimal
from typing import Any, Optional

import sqlglot
from sqlglot.errors import ErrorLevel, SqlglotError

# Every NaN is one bucket. The value is a plain string rather than a NaN, because a Decimal or
# float NaN as a dict key compares unequal to itself and would open a fresh bucket per cell.
_NAN_CELL: tuple[str, Any] = ("nan", "nan")

# Both contexts are explicit because the ambient decimal context is process-global and any caller
# can narrow its precision; a key that moved with it would compare two identical runs as different.
# The normalising precision is far above anything a database numeric carries, so it only ever
# strips trailing zeros. Note that it also turns Decimal('100') into Decimal('1E+2') — a different
# repr, but an equal value with an equal hash, which is all a key needs.
_NORMALIZE_CTX = Context(prec=60)
# A relative 1e-9 tolerance expressed as a hashable bucket: round to nine significant digits and
# two numbers that agree to that precision land on the same key.
_QUANTIZE_CTX = Context(prec=9)

# What a driver or an author writes for a boolean. Postgres' text protocol emits `t`/`f`, other
# exports write the words out; none of them means the string it looks like.
_BOOL_TEXT = {"t": True, "true": True, "yes": True, "f": False, "false": False, "no": False}

# Strict, and matched with `fullmatch` so it is anchored at both ends. This pattern is the whole
# discriminator between a date and an identifier. A zero-padded account or order id is digits and
# nothing else, and coercing one into a date would make it compare equal to a different id that
# happened to land on the same day — so a bare year and a bare number are refused, exactly as
# `golden.py` refuses a loose date pattern, and for the same reason.
# The offset is limited to `±HH:MM` and the fraction to six digits because that is what
# `fromisoformat` accepts on Python 3.10, which this package still supports.
_DATE_TEXT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?"
)

# The coarse lattice a later, looser comparison judges a column by. A NULL contributes no type at
# all — it is the absence of a value, not a value of some type — and a NaN is still a number.
_CELL_TYPES: dict[str, Optional[str]] = {
    "null": None,
    "bool": "bool",
    "num": "number",
    "nan": "number",
    "date": "date",
    "text": "text",
}


def _canonical_number(value: int | float | Decimal, quantize: bool) -> tuple[str, Any]:
    """Key a numeric as a normalised Decimal, so spelling and storage type stop mattering."""
    if isinstance(value, float):
        if math.isnan(value):
            return _NAN_CELL
        # `Decimal(repr(x))` and not `Decimal(x)`: the latter expands the binary float in full, so
        # 0.1 becomes 0.1000000000000000055… and never matches a driver's Decimal('0.1').
        dec = Decimal(repr(value))
    elif isinstance(value, Decimal):
        if value.is_nan():
            return _NAN_CELL
        dec = value
    else:
        dec = Decimal(value)
    if not dec.is_finite():
        # An infinity is an ordinary comparable value and keeps its sign; normalising or rounding
        # it is meaningless, and quantizing it would raise.
        return ("num", dec)
    if quantize:
        dec = _QUANTIZE_CTX.plus(dec)
    return ("num", dec.normalize(context=_NORMALIZE_CTX))


def _canonical_datetime(value: datetime) -> tuple[str, Any]:
    """Key a datetime as ISO text, read as an instant in UTC.

    An aware value is converted to UTC and its offset dropped, which means a naive datetime is read
    as a UTC wall clock. That is deliberate: one driver attaches tzinfo to a timestamp column where
    another does not, and an answer key authored as text carries no offset at all, so keeping the
    offset in the key would fail every case whose two sides came through different readers.

    Midnight collapses to the bare date so that a date column read as a datetime still matches the
    date the author wrote down.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if value.time() == datetime.min.time():
        return ("date", value.date().isoformat())
    return ("date", value.isoformat())


def _canonical_text(value: str) -> tuple[str, Any]:
    """Key a string, recognising only the two shapes that are unambiguously not text."""
    spelled = _BOOL_TEXT.get(value.lower())
    if spelled is not None:
        return ("bool", spelled)
    if _DATE_TEXT_RE.fullmatch(value):
        try:
            # Python 3.10's `fromisoformat` raises on a trailing `Z`, so rewrite it to the offset
            # it stands for before parsing.
            return _canonical_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            # Date-shaped but not a real day, such as '2025-13-45'. It is text after all.
            return ("text", value)
    return ("text", value)


def canonical_cell(value: Any, *, quantize: bool = False) -> tuple[str, Any]:
    """Turn one result-set cell into a hashable ``(type_tag, value)`` key.

    With `quantize`, a number is additionally rounded to nine significant digits — a relative 1e-9
    tolerance expressed as a bucket rather than as a comparison. Nothing else is affected.
    """
    if value is None:
        return ("null", None)
    # Before the numeric branch, because bool is a subclass of int and the whole point of tagging
    # is that True must not key the same as 1.
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value, quantize)
    # Before `date`, because datetime is a subclass of it.
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, str):
        return _canonical_text(value)
    return ("text", str(value))


def canonical_row(row: Sequence[Any], *, quantize: bool = False) -> tuple[tuple[str, Any], ...]:
    """Canonicalise a whole row, so the row itself is hashable and countable."""
    return tuple(canonical_cell(cell, quantize=quantize) for cell in row)


def cell_type(canon: tuple[str, Any]) -> Optional[str]:
    """The coarse type of a canonical cell, or None for a null, which contributes no type."""
    return _CELL_TYPES[canon[0]]


class RaggedRow(ValueError):
    """A row whose width disagrees with the columns it arrived with.

    ``ExecResult`` does not check that every row is as wide as its column list — that is
    convention, not a validated invariant — so a comparison can be handed a short row. It is
    raised as this module's own type so the caller can report the case as an error: an
    ``IndexError`` escaping a projection says nothing about which side was malformed.
    """


# Why an unreadable statement is read as ORDERED. The permissive reading is the dangerous one:
# assuming unordered would silently stop checking an ordering the author asked for, and every case
# whose statement did not parse would quietly pass a weaker test than the one it declares. A
# visible false failure is recoverable; a silent weakening is not.
_NO_STATEMENT = "no statement was available to read, so an ordering was assumed"
_UNPARSED = "the statement could not be parsed as SQL, so an ordering was assumed"


def has_top_level_order_by(
    sql: Optional[str], *, dialect: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """Whether `sql` orders the rows it returns, and why the answer had to be assumed if it was.

    Read off the top-level node's own `order` argument, NOT with a search for an `Order` anywhere
    in the tree: a subquery's ORDER BY, a CTE's, an `OVER (ORDER BY …)` and an `array_agg(x ORDER
    BY x)` all order something other than the result, and a search finds every one of them. Asking
    the top node keeps the union cases right in both directions too — an ORDER BY after a UNION
    hangs off the `Union` node and is a total order, one inside a single arm is not.

    The dialect is threaded through because a generic parse does not merely lose detail on a
    backtick- or bracket-quoting engine: it raises, and the case would fall to the assumed note.
    """
    if sql is None or not sql.strip():
        return True, _NO_STATEMENT
    try:
        # ErrorLevel.RAISE as the enum, never the string: sqlglot compares the level against enum
        # members, so a string matches no branch, every error is dropped and the tree is silently
        # truncated — which here would read a broken statement as cleanly unordered.
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except SqlglotError:
        # ParseError and TokenError both derive from this; a `None` sql raises TypeError instead,
        # which is why it is guarded above rather than caught here.
        return True, _UNPARSED
    return tree.args.get("order") is not None, None


def _sort_key(canon: tuple[str, Any]) -> tuple[str, str]:
    """Order canonical cells for an unordered comparison — never the raw values.

    Sorting raw cells raises: a naive datetime does not compare with an aware one, and a Decimal
    does not compare with a string. The tag leads so the type classes never interleave, and the
    value is ordered as its text, which is injective within a tag — two cells share this key only
    when they are the same cell, so the sort is total and the result deterministic.
    """
    return (canon[0], str(canon[1]))


def _column_vectors(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    quantize: bool,
) -> list[tuple[tuple[str, Any], ...]]:
    """One comparable vector per column: its cells in row order, or sorted when order is not part
    of the answer."""
    canonical = []
    for row in rows:
        if len(row) != len(columns):
            raise RaggedRow(f"a row of {len(row)} cells arrived with {len(columns)} columns")
        canonical.append(canonical_row(row, quantize=quantize))
    vectors = []
    for index in range(len(columns)):
        cells = [row[index] for row in canonical]
        if not ordered:
            cells.sort(key=_sort_key)
        vectors.append(tuple(cells))
    return vectors


def _augment(
    golden: int, candidates: Sequence[Sequence[int]], taken: dict[int, int], seen: set[int]
) -> bool:
    """Kuhn's augmenting step: place `golden`, displacing an earlier pairing that can move aside.

    Two columns carrying identical values are interchangeable, so pairing them in whatever order
    they happen to appear can strand a later column whose only candidate is already spoken for;
    the augmenting path undoes the earlier choice instead of failing. Column counts are single
    digits, so this loop is the right algorithm and Hopcroft–Karp would buy nothing.
    """
    for generated in candidates[golden]:
        if generated in seen:
            continue
        seen.add(generated)
        if generated not in taken or _augment(taken[generated], candidates, taken, seen):
            taken[generated] = golden
            return True
    return False


def match_columns(
    golden_columns: Sequence[str],
    golden_rows: Sequence[Sequence[Any]],
    generated_columns: Sequence[str],
    generated_rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    quantize: bool = False,
) -> tuple[dict[int, int], tuple[str, ...]]:
    """Pair golden columns with the generated columns carrying the same values.

    Returns the golden-index → generated-index pairing and the golden column names that found no
    partner. Neither a column's NAME nor its position is ever consulted: a generated statement
    that aliases the total and selects it second still answered the question, and a statement that
    reused the golden name for a different value did not.
    """
    golden_vectors = _column_vectors(
        golden_columns, golden_rows, ordered=ordered, quantize=quantize
    )
    generated_vectors = _column_vectors(
        generated_columns, generated_rows, ordered=ordered, quantize=quantize
    )
    candidates = [
        [index for index, other in enumerate(generated_vectors) if other == vector]
        for vector in golden_vectors
    ]
    taken: dict[int, int] = {}
    for golden in range(len(golden_vectors)):
        _augment(golden, candidates, taken, set())
    pairing = {golden: generated for generated, golden in taken.items()}
    unmatched = tuple(name for index, name in enumerate(golden_columns) if index not in pairing)
    return pairing, unmatched


def _project(
    row: Sequence[Any], indices: Sequence[int], quantize: bool
) -> tuple[tuple[str, Any], ...]:
    """The paired cells of one row, canonicalised, in golden column order."""
    try:
        cells = [row[index] for index in indices]
    except IndexError:
        raise RaggedRow(f"a row of {len(row)} cells is too short for the matched columns") from None
    return canonical_row(cells, quantize=quantize)


def compare_rows(
    golden_rows: Sequence[Sequence[Any]],
    generated_rows: Sequence[Sequence[Any]],
    pairing: dict[int, int],
    *,
    ordered: bool,
    quantize: bool = False,
) -> tuple[int, int, int]:
    """How far the two sides agree over their paired columns, as (overlap, golden, generated).

    Ordered results are compared position by position, because the ordering is part of what the
    author asked for. Unordered ones are compared as multisets and not as sets: a row returned
    twice where the answer key has it once is a different answer, usually a join that fanned out,
    and a set comparison is exactly the one that hides it.
    """
    golden_indices = sorted(pairing)
    generated_indices = [pairing[index] for index in golden_indices]
    golden = [_project(row, golden_indices, quantize) for row in golden_rows]
    generated = [_project(row, generated_indices, quantize) for row in generated_rows]
    if ordered:
        overlap = sum(1 for left, right in zip(golden, generated) if left == right)
    else:
        overlap = sum((Counter(golden) & Counter(generated)).values())
    return overlap, len(golden_rows), len(generated_rows)


__all__ = [
    "RaggedRow",
    "canonical_cell",
    "canonical_row",
    "cell_type",
    "compare_rows",
    "has_top_level_order_by",
    "match_columns",
]
