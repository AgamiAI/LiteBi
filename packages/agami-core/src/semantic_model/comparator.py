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

``compare_result_sets`` is the one way in. It is TOTAL: a malformed result, an unreadable
statement or a band that cannot be applied all come back as a score with an error status, never as
an exception, so one bad item costs that item and not the run — the same posture
``execute_guarded`` takes and the same one ``golden.py`` takes when it reads a dataset. And what
it hands back carries verdicts, counts and column NAMES only. Never a cell, and never the answer
key's SQL: the payload being judged here is result data, and a score travels further than the run
that produced it.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal
from typing import Any, Literal, NamedTuple, Optional

import sqlglot
from execute_sql import ExecResult
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from .golden import GoldenBounds, MatchLevel

# Every NaN is one bucket. The value is a plain string rather than a NaN, because a Decimal or
# float NaN as a dict key compares unequal to itself and would open a fresh bucket per cell.
_NAN_CELL: tuple[str, Any] = ("nan", "nan")

# Both contexts are explicit because the ambient decimal context is process-global and any caller
# can narrow its precision; a key that moved with it would compare two identical runs as different.
# The normalising precision is far above anything a database numeric carries, so it only ever
# strips trailing zeros. Note that it also turns Decimal('100') into Decimal('1E+2') — a different
# repr, but an equal value with an equal hash, which is all a key needs.
_NORMALIZE_CTX = Context(prec=60)
# A rounding BUCKET at nine significant digits, and deliberately not a tolerance: two numbers land
# on one key when they round alike, so two that straddle a bucket edge key apart however close they
# are. That is not a defect to be tightened later — a tolerance is not an equivalence relation, and
# a Counter needs one, so a bucket is the only form this forgiveness can take at all.
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
    # A whole number is never rounded, whatever it was spelled as. It carries no floating-point
    # tail to forgive, and rounding one would put every id or count above ~1e9 in a bucket with its
    # neighbours — two genuinely different ids would then pass `values` as the same answer. The
    # test is on the VALUE and not on the Python type, because the same id arrives as an int from
    # one driver and a Decimal from another, and exempting only one of them would fail two
    # identical numbers.
    if quantize and dec != dec.to_integral_value():
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

    With `quantize`, a number that is not a whole number is additionally rounded to nine
    significant digits. That is a BUCKET and not a tolerance: two values land on one key when they
    round alike, so two straddling a bucket edge stay apart however close they are. Nothing else is
    affected.
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
_UNREADABLE = "the statement could not be read, so an ordering was assumed"
_NOT_ONE_QUERY = (
    "the statement is not a single query whose ordering could be read, so an ordering was assumed"
)


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

    Only a query node can carry a top-level order, and plenty of statements do not parse to one:
    a trailing comment or a second statement wraps the whole thing in a `Block`, and a construct
    sqlglot has no grammar for (EXPLAIN, say) falls back to a `Command` — with a WARNING rather
    than an error, so the unparsed net below never fires for it. Asking `args['order']` of a node
    that has no such argument always answers None, so every one of those would read as cleanly
    unordered; they take the assumed-ordered path instead.
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
    except Exception:
        # The rest of what a parse can do, and none of it is a SqlglotError: an unknown dialect
        # raises ValueError, and a deeply nested statement a RecursionError. This function is
        # called outside the scoring call's own totality net, so anything escaping here escapes
        # that call's "never raises" contract too.
        return True, _UNREADABLE
    # A parenthesised statement parses to a `Subquery` wrapper. The ORDER BY sits on the node
    # inside it, or on the wrapper when it was written outside the parentheses — both order the
    # rows the caller receives, so the wrapper is asked before it is unwrapped.
    while isinstance(tree, exp.Subquery):
        if tree.args.get("order") is not None:
            return True, None
        tree = tree.this
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        return True, _NOT_ONE_QUERY
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
    # Greedy, and deliberately NOT a maximum-matching algorithm. A golden column pairs with a
    # generated one only when their value vectors are equal, and equality is transitive: the
    # candidate sets are equivalence classes, so two golden columns either compete for exactly the
    # same partners or for none of the same. Partners inside one class are interchangeable, so
    # taking the first unclaimed one can never strand a later column that had an option of its own
    # — there is no augmenting path to find, and adding one back would be dead weight.
    unclaimed: dict[tuple[tuple[str, Any], ...], list[int]] = {}
    for index, vector in enumerate(generated_vectors):
        unclaimed.setdefault(vector, []).append(index)
    pairing: dict[int, int] = {}
    for index, vector in enumerate(golden_vectors):
        partners = unclaimed.get(vector)
        if partners:
            pairing[index] = partners.pop(0)
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


@dataclass(frozen=True)
class ItemScore:
    """One item's verdict: how it was judged and how far the two sides agreed.

    A returned value and never an authored file, so a frozen dataclass like ``ExecResult`` and
    ``Envelope`` rather than a pydantic model — ``golden.py``'s models are the other case, parsing
    what a person wrote down.

    ``accuracy`` is None exactly when nothing was scored. 0.0 is a score an item can legitimately
    earn — a comparison that ran and found no agreement — and collapsing the two would report a
    wrong answer and an unrunnable case as the same thing.

    An item passes at exactly 1.0, and there is no second threshold: the match level already says
    how loose the comparison is, and a fractional pass mark on top of it would loosen every level
    again, invisibly.
    """

    status: Literal["scored", "unscored", "error"]
    accuracy: Optional[float]
    reason: str
    unmatched_golden_columns: tuple[str, ...] = ()
    golden_row_count: Optional[int] = None
    generated_row_count: Optional[int] = None
    order_sensitive: Optional[bool] = None
    notes: tuple[str, ...] = ()


class _Verdict(NamedTuple):
    """What one level decided, before the counts and ordering every score carries are attached."""

    status: Literal["scored", "unscored", "error"]
    accuracy: Optional[float]
    reason: str
    unmatched: tuple[str, ...] = ()


# Deliberately NOT a pass. Two empty results agree about nothing: no value was compared, and
# scoring that as a full match is how a statement that returns nothing at all gates like a right
# one. It applies only to the levels that consult the answer key — `nonempty` and `bounded` have
# no answer key by design, so an empty golden side is their NORMAL shape, and dropping the pair
# there would excuse the returned-nothing failure those two levels exist to catch.
_BOTH_EMPTY = "both result sets are empty, so the comparison would check no value"
_KEYED_LEVELS = ("exact", "values", "shape")


def _accuracy(overlap: int, row_count: int) -> float:
    """The share of the rows the two sides agreed on.

    One denominator, because a row-count difference is decided before any column is paired and the
    two sides are the same height by the time this is reached. The value is the raw share and is
    NOT rounded: an item passes at exactly 1.0, and rounding would hand the pass mark to a near
    miss — 4002 of 4004 rows is 0.99950…, which rounds up. Three-decimal presentation is the
    report renderer's business, not the score's.
    """
    return overlap / row_count


def _score_values(
    golden: ExecResult,
    generated: ExecResult,
    *,
    ordered: bool,
    loose: bool,
) -> _Verdict:
    """Pair the columns by value, then score how far the rows agree over the pairing.

    `loose` is the `values` level: it forgives a floating-point tail and an extra column the
    question did not ask for. One flag rather than two, because no level asks for one and not the
    other.
    """
    if len(golden.rows) != len(generated.rows):
        # Before any pairing, because a column vector's LENGTH is the row count: when the counts
        # differ no column can pair, and reporting that through the unmatched-column branch below
        # tells a reader a column is absent when every one of them is present.
        return _Verdict(
            "scored", 0.0,
            f"the answer key has {len(golden.rows)} rows and the generated result "
            f"{len(generated.rows)}",
        )
    pairing, unmatched = match_columns(
        golden.columns, golden.rows, generated.columns, generated.rows,
        ordered=ordered, quantize=loose,
    )
    if unmatched:
        # Not a partial answer. The question asked for that column and the result does not carry
        # it, so how well the remaining columns overlap says nothing about whether it is right.
        return _Verdict(
            "scored", 0.0, "no generated column carries the values of: " + ", ".join(unmatched),
            unmatched,
        )
    if not loose and len(generated.columns) > len(golden.columns):
        extra = len(generated.columns) - len(golden.columns)
        return _Verdict(
            "scored", 0.0, f"the generated result carries {extra} column(s) the answer key does not"
        )
    overlap, golden_count, generated_count = compare_rows(
        golden.rows, generated.rows, pairing, ordered=ordered, quantize=loose
    )
    accuracy = _accuracy(overlap, golden_count)
    if accuracy == 1.0:
        return _Verdict("scored", 1.0, "")
    return _Verdict(
        "scored", accuracy,
        f"{overlap} of the answer key's {golden_count} rows matched, "
        f"out of {generated_count} the generated statement returned",
    )


def _column_types(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[frozenset[str]]:
    """The coarse types present in each column. Reuses the vector build so a ragged row is caught
    here too, and orders the cells because a set of types does not care."""
    vectors = _column_vectors(columns, rows, ordered=True, quantize=False)
    return [
        frozenset(tag for tag in (cell_type(cell) for cell in vector) if tag is not None)
        for vector in vectors
    ]


def _score_shape(golden: ExecResult, generated: ExecResult) -> _Verdict:
    """Counts and the coarse type lattice, never a value.

    Columns are compared POSITIONALLY here, which the value levels never do — with the values off
    limits there is nothing to pair them on, and pairing by type would call any two text columns
    interchangeable.
    """
    if len(golden.rows) != len(generated.rows):
        return _Verdict(
            "scored", 0.0,
            f"the answer key has {len(golden.rows)} rows and the generated result "
            f"{len(generated.rows)}",
        )
    if len(golden.columns) != len(generated.columns):
        return _Verdict(
            "scored", 0.0,
            f"the answer key has {len(golden.columns)} columns and the generated result "
            f"{len(generated.columns)}",
        )
    pairs = zip(golden.columns, _column_types(golden.columns, golden.rows),
                _column_types(generated.columns, generated.rows))
    for name, golden_types, generated_types in pairs:
        # An empty set is an all-NULL column, or a result with no rows at all, and it constrains
        # nothing: a NULL is the absence of a value, not a value of some type.
        if golden_types and generated_types and golden_types != generated_types:
            return _Verdict(
                "scored", 0.0, f"column {name!r} does not carry the type the answer key does"
            )
    return _Verdict("scored", 1.0, "")


def _score_bounds(generated: ExecResult, bounds: Optional[GoldenBounds]) -> _Verdict:
    """Judge the generated result against the authored band. Row bounds count the GENERATED rows —
    a bounded item has no answer key to count against, which is why it is bounded."""
    if bounds is None:
        return _Verdict(
            "error", None,
            "a bounded item is judged against a band, and no bounds were given to judge it with",
        )
    row_band = bounds.min_rows is not None or bounds.max_rows is not None
    if bounds.min_value is not None or bounds.max_value is not None:
        if not generated.rows:
            # An empty result is a WRONG answer here and not an unjudgeable one: catching a
            # statement that returned nothing is half of why a band gets authored at all.
            return _Verdict("scored", 0.0, "the generated statement returned no rows")
        number = _single_number(generated)
        if number is None:
            # The value band asks about one number and there is not one to ask about. When the
            # author also wrote a row band, that band judges this result perfectly well, so it is
            # preferred over reporting an item nobody can ever judge; without one, an error is all
            # that is left — the case cannot be decided, and calling it wrong would blame the run.
            if not row_band:
                return _Verdict(
                    "error", None,
                    "a value band judges a single numeric cell, and the generated result is not "
                    "one",
                )
        # Both edges are INCLUSIVE: an author writing `max_value: 10` means ten is an acceptable
        # answer. Compared through str, because Decimal(float) expands the binary float in full
        # and would put an edge of 0.1 a hair away from where the author wrote it.
        elif bounds.min_value is not None and number < Decimal(str(bounds.min_value)):
            return _Verdict("scored", 0.0, "the generated value is below the band")
        elif bounds.max_value is not None and number > Decimal(str(bounds.max_value)):
            return _Verdict("scored", 0.0, "the generated value is above the band")
    count = len(generated.rows)
    if bounds.min_rows is not None and count < bounds.min_rows:
        return _Verdict("scored", 0.0, f"the generated result has {count} rows, below the band")
    if bounds.max_rows is not None and count > bounds.max_rows:
        return _Verdict("scored", 0.0, f"the generated result has {count} rows, above the band")
    return _Verdict("scored", 1.0, "")


def _single_number(generated: ExecResult) -> Optional[Decimal]:
    """The one number a result carries, or None when it does not carry exactly one."""
    if len(generated.rows) != 1 or len(generated.columns) != 1 or len(generated.rows[0]) != 1:
        return None
    tag, value = canonical_cell(generated.rows[0][0])
    # A NaN is tagged apart from `num`, so it never reaches a band comparison that it would answer
    # False to in both directions.
    return value if tag == "num" else None


def _judge(
    golden: ExecResult,
    generated: ExecResult,
    match: MatchLevel,
    bounds: Optional[GoldenBounds],
    ordered: bool,
) -> _Verdict:
    """Dispatch to the level the author asked for, loosening left to right."""
    if match in _KEYED_LEVELS and not golden.rows and not generated.rows:
        return _Verdict("unscored", None, _BOTH_EMPTY)
    if match in ("exact", "values"):
        # `values` forgives a floating-point tail and an extra column the question did not ask
        # for; `exact` forgives neither, which is the only difference between the two.
        return _score_values(golden, generated, ordered=ordered, loose=match == "values")
    if match == "shape":
        return _score_shape(golden, generated)
    if match == "bounded":
        return _score_bounds(generated, bounds)
    if match == "nonempty":
        if generated.rows:
            return _Verdict("scored", 1.0, "")
        return _Verdict("scored", 0.0, "the generated statement returned no rows")
    return _Verdict("error", None, f"{match!r} is not a match level this comparison knows")


def compare_result_sets(
    golden: ExecResult,
    generated: ExecResult,
    *,
    match: MatchLevel = "exact",
    golden_sql: Optional[str] = None,
    bounds: Optional[GoldenBounds] = None,
    dialect: Optional[str] = None,
) -> ItemScore:
    """Score one generated result against its answer key. Never raises.

    `golden_sql` is read for one thing only — whether the author ordered the rows — and the
    generated statement is deliberately not a parameter: the ordering that has to hold is the one
    the ANSWER KEY asked for, so a generated statement that drops the ORDER BY is still judged
    against it rather than excused by it.
    """
    ordered, note = has_top_level_order_by(golden_sql, dialect=dialect)
    try:
        verdict = _judge(golden, generated, match, bounds, ordered)
    except RaggedRow as exc:
        # Its message counts cells and names no value, so it can be reported as it stands.
        verdict = _Verdict("error", None, str(exc))
    except Exception as exc:
        # The totality net. The exception TYPE only and never its message: an arbitrary error
        # quotes the value that broke it, and this score travels.
        verdict = _Verdict(
            "error", None, f"the comparison failed with an unexpected {type(exc).__name__}"
        )
    return ItemScore(
        status=verdict.status,
        accuracy=verdict.accuracy,
        reason=verdict.reason,
        unmatched_golden_columns=verdict.unmatched,
        golden_row_count=len(golden.rows),
        generated_row_count=len(generated.rows),
        order_sensitive=ordered,
        notes=(note,) if note else (),
    )


# The scoring call and the value it hands back, and nothing else. The rest of this module is how
# the two are built rather than what a caller is invited to reach for; `MatchLevel` and
# `GoldenBounds` stay out because they belong to `golden`, which is where a caller should take them
# from rather than through here.
__all__ = ["ItemScore", "compare_result_sets"]
