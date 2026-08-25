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


# --- is the result ordered? ------------------------------------------------------------------


def test_a_top_level_order_by_is_ordered():
    assert c.has_top_level_order_by("SELECT a FROM t ORDER BY a") == (True, None)


def test_a_subquery_order_by_is_not_ordered():
    # The inner ORDER BY orders the input to the outer SELECT, which is then free to return its
    # rows in any order. `find(exp.Order)` would read this as ordered; `args["order"]` does not.
    ordered, note = c.has_top_level_order_by("SELECT a FROM (SELECT a FROM t ORDER BY a) x")
    assert (ordered, note) == (False, None)


def test_a_window_order_by_is_not_ordered():
    sql = "SELECT a, ROW_NUMBER() OVER (ORDER BY a) FROM t"
    assert c.has_top_level_order_by(sql) == (False, None)


def test_a_cte_order_by_is_not_ordered():
    sql = "WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c"
    assert c.has_top_level_order_by(sql) == (False, None)


def test_an_aggregate_order_by_is_not_ordered():
    # `array_agg(a ORDER BY a)` orders what goes INTO one cell, not the rows that come out.
    assert c.has_top_level_order_by("SELECT array_agg(a ORDER BY a) FROM t") == (False, None)


def test_order_by_with_a_limit_is_ordered():
    assert c.has_top_level_order_by("SELECT a FROM t ORDER BY a LIMIT 10") == (True, None)


@pytest.mark.parametrize(
    "sql",
    ["SELECT a FROM t ORDER BY a;", "-- the answer key\nSELECT a FROM t ORDER BY a",
     "   \n\tSELECT a FROM t ORDER BY a", "select a from t order by a"],
)
def test_incidental_spelling_does_not_hide_the_ordering(sql):
    assert c.has_top_level_order_by(sql) == (True, None)


def test_a_union_ordered_as_a_whole_is_ordered():
    # The ORDER BY hangs off the Union node here, which `args["order"]` reads because it is asked
    # of whatever the top-level node turned out to be rather than of a Select.
    sql = "SELECT a FROM t UNION SELECT b FROM u ORDER BY 1"
    assert c.has_top_level_order_by(sql) == (True, None)


def test_a_union_arm_ordered_alone_is_not_ordered():
    # Ordering one arm is not a total order of the result the caller receives.
    sql = "SELECT a FROM t ORDER BY a UNION SELECT b FROM u"
    assert c.has_top_level_order_by(sql) == (False, None)


@pytest.mark.parametrize("sql", ["not sql at all", "SELECT FROM", "SELECT 'unterminated"])
def test_an_unreadable_statement_is_assumed_ordered(sql):
    # Deliberate: assuming UNORDERED would silently stop checking an ordering the author asked
    # for, and a visible false failure is recoverable where a silent weakening is not.
    ordered, note = c.has_top_level_order_by(sql)
    assert ordered is True
    assert note


@pytest.mark.parametrize("sql", [None, "", "   \n\t "])
def test_a_missing_statement_is_assumed_ordered(sql):
    # None is guarded separately from the parse: sqlglot raises TypeError on it, not a
    # SqlglotError, so it would escape the except clause.
    ordered, note = c.has_top_level_order_by(sql)
    assert ordered is True
    assert note


def test_a_backtick_statement_needs_its_dialect():
    # The dialect earns its place here: read with the generic grammar the same statement does not
    # parse at all, and the case would silently fall to the assumed-ordered note.
    sql = "SELECT `a` FROM `t` ORDER BY `a`"
    generic_ordered, generic_note = c.has_top_level_order_by(sql)
    assert generic_ordered is True
    assert generic_note
    assert c.has_top_level_order_by(sql, dialect="mysql") == (True, None)


def test_a_dialect_parse_still_reads_an_unordered_statement():
    # ...and the dialect path is not just returning True for everything.
    sql = "SELECT `a` FROM `t`"
    assert c.has_top_level_order_by(sql, dialect="mysql") == (False, None)


# --- matching columns by their values --------------------------------------------------------


def test_columns_in_a_different_order_match():
    pairing, unmatched = c.match_columns(
        ["channel", "orders"], [("web", 1), ("store", 2)],
        ["orders", "channel"], [(1, "web"), (2, "store")],
        ordered=True,
    )
    assert pairing == {0: 1, 1: 0}
    assert unmatched == ()


def test_columns_with_different_names_match():
    # A generated statement is free to alias the total; it still answered the question.
    pairing, unmatched = c.match_columns(
        ["orders"], [(1,), (2,)], ["order_count"], [(1,), (2,)], ordered=True
    )
    assert pairing == {0: 0}
    assert unmatched == ()


def test_a_golden_column_with_no_partner_is_named():
    pairing, unmatched = c.match_columns(
        ["channel", "revenue"], [("web", 10), ("store", 20)],
        ["channel"], [("web",), ("store",)],
        ordered=True,
    )
    assert pairing == {0: 0}
    assert unmatched == ("revenue",)


def test_identical_value_vectors_do_not_collapse_onto_one_partner():
    # The case a careless pairing gets wrong: two golden columns carrying the same values must
    # take two DIFFERENT partners, and the complete matching has to be found even though the
    # duplicate generated columns are not the first candidates encountered.
    golden_rows = [(1, "web", 1), (2, "store", 2)]
    generated_rows = [("web", 1, 1), ("store", 2, 2)]
    pairing, unmatched = c.match_columns(
        ["first", "channel", "second"], golden_rows,
        ["channel", "left", "right"], generated_rows,
        ordered=True,
    )
    assert unmatched == ()
    assert len(pairing) == 3
    # ...and no generated column was handed to two golden columns.
    assert len(set(pairing.values())) == 3
    assert pairing[1] == 0


def test_a_duplicated_golden_column_leaves_one_unmatched():
    # Two golden columns of the same values against one generated column: exactly one pairs, and
    # the other is named rather than quietly sharing the partner.
    pairing, unmatched = c.match_columns(
        ["a", "b"], [(1, 1), (2, 2)], ["only"], [(1,), (2,)], ordered=True
    )
    assert len(pairing) == 1
    assert len(unmatched) == 1
    assert unmatched[0] in ("a", "b")


def test_a_bool_column_does_not_match_an_int_column():
    # Slice 1's tags exist for this: `is_active` as a real boolean against the 0/1 SQLite stores
    # is a different answer, and raw equality would have called it a match.
    pairing, unmatched = c.match_columns(
        ["is_active"], [(True,), (False,)], ["is_active"], [(1,), (0,)], ordered=True
    )
    assert pairing == {}
    assert unmatched == ("is_active",)


def test_column_values_in_a_different_row_order_match_when_unordered():
    pairing, unmatched = c.match_columns(
        ["channel"], [("web",), ("store",)], ["channel"], [("store",), ("web",)], ordered=False
    )
    assert pairing == {0: 0}
    assert unmatched == ()


def test_column_values_in_a_different_row_order_do_not_match_when_ordered():
    pairing, unmatched = c.match_columns(
        ["channel"], [("web",), ("store",)], ["channel"], [("store",), ("web",)], ordered=True
    )
    assert pairing == {}
    assert unmatched == ("channel",)


def test_a_mixed_type_column_sorts_without_raising_when_unordered():
    # Sorting the raw cells here raises: a naive datetime does not compare with an aware one, and
    # a Decimal does not compare with a string. The sort key is derived from the canonical form.
    left = [(datetime(2025, 1, 1, 9, 30),), (None,), ("web",), (5,)]
    right = [(5,), ("web",), (datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc),), (None,)]
    pairing, unmatched = c.match_columns(["mixed"], left, ["mixed"], right, ordered=False)
    assert pairing == {0: 0}
    assert unmatched == ()


def test_matching_forwards_quantize():
    golden_rows = [(Decimal("1.00000000001"),)]
    generated_rows = [(Decimal("1.0"),)]
    assert c.match_columns(["v"], golden_rows, ["v"], generated_rows, ordered=True)[0] == {}
    pairing, _ = c.match_columns(
        ["v"], golden_rows, ["v"], generated_rows, ordered=True, quantize=True
    )
    assert pairing == {0: 0}


def test_matching_a_ragged_row_is_surfaced_as_this_module_s_error():
    with pytest.raises(c.RaggedRow):
        c.match_columns(["a", "b"], [(1,)], ["a", "b"], [(1, 2)], ordered=True)


# --- comparing the rows ----------------------------------------------------------------------


def test_rows_are_projected_onto_the_pairing():
    # The generated side carries an extra column and the matched one sits elsewhere; only the
    # paired columns are compared, in golden column order.
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [(99, 1, "web"), (99, 2, "store")]
    assert c.compare_rows(golden_rows, generated_rows, {0: 2, 1: 1}, ordered=True) == (2, 2, 2)


def test_row_order_is_irrelevant_when_unordered():
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [("store", 2), ("web", 1)]
    pairing = {0: 0, 1: 1}
    assert c.compare_rows(golden_rows, generated_rows, pairing, ordered=False) == (2, 2, 2)


def test_row_order_is_decisive_when_ordered():
    golden_rows = [("web", 1), ("store", 2)]
    generated_rows = [("store", 2), ("web", 1)]
    pairing = {0: 0, 1: 1}
    assert c.compare_rows(golden_rows, generated_rows, pairing, ordered=True) == (0, 2, 2)


def test_duplicate_rows_count_as_a_multiset_not_a_set():
    # A set would say these two agree once; they agree twice, and the duplicate is signal.
    pairing = {0: 0}
    assert c.compare_rows([("web",), ("web",)], [("web",), ("web",)], pairing, ordered=False) == (
        2, 2, 2,
    )


def test_a_duplicate_on_one_side_only_reduces_the_overlap():
    pairing = {0: 0}
    assert c.compare_rows([("web",), ("web",)], [("web",)], pairing, ordered=False) == (1, 2, 1)
    assert c.compare_rows([("web",)], [("web",), ("web",)], pairing, ordered=False) == (1, 1, 2)


def test_partial_overlap_returns_both_counts():
    golden_rows = [("web",), ("store",), ("kiosk",)]
    generated_rows = [("kiosk",), ("web",), ("phone",)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (2, 3, 3)


def test_a_shorter_generated_side_matches_only_where_it_reaches_when_ordered():
    golden_rows = [("web",), ("store",), ("kiosk",)]
    assert c.compare_rows(golden_rows, [("web",), ("store",)], {0: 0}, ordered=True) == (2, 3, 2)


def test_nan_rows_count_deterministically():
    # Two independently-produced NaNs are unequal and would never meet in a raw comparison; the
    # canonical bucket makes the count depend on the values rather than on object identity.
    golden_rows = [(float("nan"),), (float("nan"),)]
    generated_rows = [(float("nan"),), (float("nan"),)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (2, 2, 2)
    assert c.compare_rows(golden_rows, [(float("nan"),)], {0: 0}, ordered=False) == (1, 2, 1)


def test_comparing_forwards_quantize():
    golden_rows = [(Decimal("1.00000000001"),)]
    generated_rows = [(Decimal("1.0"),)]
    assert c.compare_rows(golden_rows, generated_rows, {0: 0}, ordered=False) == (0, 1, 1)
    assert c.compare_rows(
        golden_rows, generated_rows, {0: 0}, ordered=False, quantize=True
    ) == (1, 1, 1)


def test_comparing_a_ragged_row_is_surfaced_as_this_module_s_error():
    # `ExecResult` does not validate that a row is as wide as its column list, so a short row can
    # reach here; it must arrive as this module's own error rather than an IndexError from a
    # projection, which would say nothing about which side was malformed.
    with pytest.raises(c.RaggedRow):
        c.compare_rows([("web", 1)], [("web",)], {0: 0, 1: 1}, ordered=False)
