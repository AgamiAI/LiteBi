"""Canonical keys for the cells of a result set, so two result sets can be compared at all.

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
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Context, Decimal
from typing import Any, Optional

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


__all__ = ["canonical_cell", "canonical_row", "cell_type"]
