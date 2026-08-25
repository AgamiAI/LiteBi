"""A result-set cell is compared through a canonical key, never as the driver handed it over.

Two result sets that say the same thing arrive as different Python objects: one driver returns
``Decimal('5.00')`` where another returns ``5``, one attaches UTC where another does not, and an
answer key read out of YAML is text where the live run is a ``datetime``. Comparing those raw is
not merely imprecise, it is wrong in ways that silently pass:

* ``True == 1 == 1.0 == Decimal(1)`` and all four share a hash, so a ``Counter`` collapses a
  boolean column onto an integer column of zeros and ones and reports them identical.
* ``float('nan') != float('nan')``, but dict's identity fast-path makes the *same* NaN object
  collapse anyway — so a row count would depend on whether the driver reused an object.
* ``Decimal('0.1') != 0.1``, so the same number read through two drivers disagrees.

These tests pin the canonical keys that close all three, and the deliberate choices made where
more than one answer was defensible. Synthetic values only — nothing here names real data.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import comparator as c  # noqa: E402

# --- numbers -------------------------------------------------------------------------------


def test_int_and_padded_decimal_share_a_key():
    # `unit_price` comes back as REAL from SQLite and as NUMERIC from Postgres; the answer key
    # would never match the run if the trailing zeros counted.
    assert c.canonical_cell(5) == c.canonical_cell(Decimal("5.00"))
    assert c.canonical_cell(5) == c.canonical_cell(5.0)


def test_decimal_and_float_of_the_same_number_share_a_key():
    # Decimal(0.1) is 0.1000000000000000055…; only Decimal(repr(0.1)) lands on Decimal('0.1').
    assert c.canonical_cell(Decimal("0.1")) == c.canonical_cell(0.1)


def test_normalize_survives_the_exponent_form():
    # Decimal('100').normalize() is Decimal('1E+2'), which is a different repr but the same value
    # and the same hash — so the key still compares equal to a plain 100.
    assert c.canonical_cell(100) == c.canonical_cell(Decimal("100.000"))
    assert hash(c.canonical_cell(100)) == hash(c.canonical_cell(Decimal("100.000")))


def test_a_number_is_tagged_num():
    assert c.canonical_cell(7)[0] == "num"
    assert c.canonical_cell(Decimal("-2.5"))[0] == "num"


def test_negative_zero_matches_zero():
    assert c.canonical_cell(-0.0) == c.canonical_cell(0)


def test_number_keys_are_immune_to_the_ambient_decimal_context():
    # The decimal context is process-global and any caller can lower its precision; a key that
    # moved with it would compare two identical runs as different.
    import decimal

    saved = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 3
        narrowed = c.canonical_cell(Decimal("1.23456789012345"))
    finally:
        decimal.getcontext().prec = saved
    assert narrowed == c.canonical_cell(Decimal("1.23456789012345"))


# --- non-finite numbers --------------------------------------------------------------------


def test_two_independent_nans_share_one_bucket():
    first, second = float("nan"), float("nan")
    assert first is not second and first != second
    assert c.canonical_cell(first) == c.canonical_cell(second)
    counted = Counter([c.canonical_cell(first), c.canonical_cell(second)])
    assert list(counted.values()) == [2]


def test_decimal_nan_matches_float_nan():
    assert c.canonical_cell(Decimal("NaN")) == c.canonical_cell(float("nan"))
    assert c.canonical_cell(float("nan"))[0] == "nan"


def test_infinities_keep_their_sign_and_stay_numbers():
    assert c.canonical_cell(float("inf"))[0] == "num"
    assert c.canonical_cell(float("inf")) != c.canonical_cell(float("-inf"))
    assert c.canonical_cell(float("inf")) == c.canonical_cell(Decimal("Infinity"))
    assert c.canonical_cell(float("-inf")) == c.canonical_cell(Decimal("-Infinity"))


def test_nan_is_not_infinity():
    assert c.canonical_cell(float("nan")) != c.canonical_cell(float("inf"))


# --- quantize ------------------------------------------------------------------------------


def test_quantize_buckets_a_twelfth_digit_difference_together():
    left = c.canonical_cell(Decimal("1.00000000001"), quantize=True)
    right = c.canonical_cell(Decimal("1.0"), quantize=True)
    assert left == right


def test_quantize_keeps_a_fifth_digit_difference_apart():
    left = c.canonical_cell(Decimal("1.0001"), quantize=True)
    right = c.canonical_cell(Decimal("1.0002"), quantize=True)
    assert left != right


def test_quantize_leaves_non_numbers_alone():
    for value in ("2025-01-01", "shipped", True, None, float("nan"), float("inf")):
        assert c.canonical_cell(value, quantize=True) == c.canonical_cell(value)


def test_quantize_is_off_by_default():
    assert c.canonical_cell(Decimal("1.00000000001")) != c.canonical_cell(Decimal("1.0"))


# --- booleans ------------------------------------------------------------------------------


def test_bool_and_int_do_not_collide():
    # The one that silently breaks a Counter: `is_active` as a real boolean against `is_active`
    # as the 0/1 integer SQLite stores must not read as the same column.
    assert c.canonical_cell(True) != c.canonical_cell(1)
    assert c.canonical_cell(False) != c.canonical_cell(0)
    assert Counter([c.canonical_cell(True), c.canonical_cell(1)]).total() == 2
    assert len(Counter([c.canonical_cell(True), c.canonical_cell(1)])) == 2


@pytest.mark.parametrize(
    "spelling, expected",
    [("t", True), ("T", True), ("true", True), ("TRUE", True), ("yes", True), ("Yes", True),
     ("f", False), ("false", False), ("FALSE", False), ("no", False), ("NO", False)],
)
def test_boolean_spellings_become_bools(spelling, expected):
    assert c.canonical_cell(spelling) == ("bool", expected)


def test_a_boolean_spelling_agrees_with_the_bool():
    assert c.canonical_cell("t") == c.canonical_cell(True)
    assert c.canonical_cell("no") == c.canonical_cell(False)
    assert c.canonical_cell("t") != c.canonical_cell("f")


def test_a_word_that_merely_starts_like_one_is_text():
    assert c.canonical_cell("nope") == ("text", "nope")
    assert c.canonical_cell("truest") == ("text", "truest")


# --- dates ---------------------------------------------------------------------------------


def test_date_datetime_and_iso_text_share_a_key():
    assert c.canonical_cell(date(2025, 1, 1)) == c.canonical_cell(datetime(2025, 1, 1))
    assert c.canonical_cell(date(2025, 1, 1)) == c.canonical_cell("2025-01-01")
    assert c.canonical_cell(date(2025, 1, 1))[0] == "date"


def test_a_datetime_with_a_time_is_not_the_bare_date():
    assert c.canonical_cell(datetime(2025, 1, 1, 9, 30)) != c.canonical_cell(date(2025, 1, 1))


def test_a_trailing_z_parses():
    # Python 3.10's fromisoformat raises on a trailing `Z`, and this repo still supports 3.10.
    assert c.canonical_cell("2025-01-01T00:00:00Z") == c.canonical_cell(date(2025, 1, 1))


def test_a_space_separated_timestamp_parses():
    # What SQLite stores in `placed_at`.
    assert c.canonical_cell("2025-03-04 09:30:00") == c.canonical_cell(datetime(2025, 3, 4, 9, 30))


def test_fractional_seconds_parse():
    assert c.canonical_cell("2025-03-04T09:30:00.500000") == c.canonical_cell(
        datetime(2025, 3, 4, 9, 30, 0, 500000)
    )


def test_an_aware_datetime_is_read_as_an_instant_in_utc():
    # The deliberate choice: an aware value is converted to UTC and its offset dropped, so a naive
    # datetime is read as a UTC wall clock. One driver attaches tzinfo to a timestamp column where
    # another does not, and an answer key authored as text carries no offset at all — splitting
    # those apart would fail every case that crosses a driver.
    aware = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert c.canonical_cell(aware) == c.canonical_cell(datetime(2025, 1, 1, 12, 0))
    shifted = datetime(2025, 1, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert c.canonical_cell(shifted) == c.canonical_cell(datetime(2025, 1, 1, 12, 0))


def test_an_aware_midnight_utc_matches_the_date():
    aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert c.canonical_cell(aware) == c.canonical_cell(date(2025, 1, 1))


@pytest.mark.parametrize(
    "not_a_date",
    ["01100170109835", "2025", "20250101", "2025-1-1", "x2025-01-01", "2025-01-01x",
     "2025-01-01T00:00:00Z ", "the 2025-01-01 order"],
)
def test_a_string_that_is_not_strictly_date_shaped_stays_text(not_a_date):
    # A zero-padded identifier is the case that matters: coerced to a date it would compare equal
    # to some other identifier that happens to round to the same day.
    assert c.canonical_cell(not_a_date) == ("text", not_a_date)


def test_a_date_shaped_string_that_is_not_a_real_day_stays_text():
    assert c.canonical_cell("2025-13-45") == ("text", "2025-13-45")


# --- text and nulls ------------------------------------------------------------------------


def test_null_is_not_the_empty_string():
    assert c.canonical_cell(None) == ("null", None)
    assert c.canonical_cell(None) != c.canonical_cell("")
    assert c.canonical_cell("") == ("text", "")


def test_text_is_neither_stripped_nor_case_folded():
    # A comparator that folded these would hide a real difference between two result sets.
    assert c.canonical_cell(" a ") != c.canonical_cell("a")
    assert c.canonical_cell("A") != c.canonical_cell("a")
    assert c.canonical_cell(" a ") == ("text", " a ")


def test_an_unrecognised_object_becomes_its_text():
    assert c.canonical_cell(b"paid") == ("text", str(b"paid"))
    assert c.canonical_cell(["web", "store"]) == ("text", str(["web", "store"]))


def test_a_numeric_string_is_not_coerced_to_a_number():
    # Coercion here would make a text column of order ids compare equal to a numeric one.
    assert c.canonical_cell("5") == ("text", "5")
    assert c.canonical_cell("5") != c.canonical_cell(5)


# --- rows ----------------------------------------------------------------------------------


def test_canonical_row_canonicalises_every_cell():
    row = c.canonical_row(("web", 5, None, date(2025, 1, 1)))
    assert row == (("text", "web"), ("num", Decimal("5")), ("null", None), ("date", "2025-01-01"))


def test_canonical_row_forwards_quantize():
    left = c.canonical_row((Decimal("1.00000000001"),), quantize=True)
    right = c.canonical_row((Decimal("1.0"),), quantize=True)
    assert left == right


def test_two_rows_that_agree_land_in_one_counter_bucket():
    counted = Counter([c.canonical_row(("web", 5)), c.canonical_row(("web", Decimal("5.00")))])
    assert list(counted.values()) == [2]


# --- the coarse type lattice ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [(None, None), (True, "bool"), ("t", "bool"), (5, "number"), (Decimal("1.5"), "number"),
     (float("nan"), "number"), (float("inf"), "number"), (date(2025, 1, 1), "date"),
     ("2025-01-01", "date"), (datetime(2025, 1, 1, 9, 30), "date"), ("shipped", "text"),
     ("", "text"), (b"paid", "text")],
)
def test_cell_type_over_every_tag(value, expected):
    assert c.cell_type(c.canonical_cell(value)) == expected


def test_a_null_contributes_no_type():
    assert c.cell_type(c.canonical_cell(None)) is None


# --- hashability ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, True, False, "t", 5, 5.0, Decimal("5.00"), float("nan"), float("inf"), float("-inf"),
     date(2025, 1, 1), datetime(2025, 1, 1, 9, 30), "2025-01-01", "", " a ", b"paid",
     ["web", "store"], {"channel": "web"}],
)
def test_every_canonical_cell_is_hashable(value):
    key = c.canonical_cell(value)
    assert isinstance(key, tuple) and len(key) == 2
    hash(key)
    assert Counter([key])[key] == 1
