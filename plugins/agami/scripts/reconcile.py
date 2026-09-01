#!/usr/bin/env python3
"""
Reconciliation helper for the agami-reconcile skill.

The skill drives the LLM (question generation, query execution, narration);
this helper handles the deterministic parts:

- CSV parsing (header / no-header, common dialects)
- Number-string parsing ($4.2M, ₹2.16Cr, "47,238,221.00", "42%", etc.)
- Diff logic with tolerance
- The tolerance band around an observed number, in a golden item's own bounds keys

Stdlib only.

Usage:

    # Parse a CSV and emit a normalized JSON list of {label, expected_value}:
    python3 reconcile.py parse --csv /path/to/dashboard-export.csv

    # Diff two numbers (expected vs actual) with optional tolerance:
    python3 reconcile.py diff --expected 47238221 --actual 47200000 --tolerance 0.01
    #   (--tolerance also takes a percentage: `--tolerance 1%` is the same band)

    # Band an observed number, ready to paste as a golden item's `bounds`:
    python3 reconcile.py band --value 47238221 --tolerance 0.01
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

# --- Number parsing -------------------------------------------------------

# Currency symbols and their codes that we strip from the front of a number.
CURRENCY_SYMBOLS = ("$", "€", "£", "¥", "₹", "₩", "₽", "₿")

# Magnitude suffixes. Indian numbering (Lakh / Crore) is included because
# dashboards from Indian deployments commonly use it.
SUFFIXES: dict[str, float] = {
    "k": 1_000,
    "K": 1_000,
    "m": 1_000_000,
    "M": 1_000_000,
    "b": 1_000_000_000,
    "B": 1_000_000_000,
    "bn": 1_000_000_000,
    "Bn": 1_000_000_000,
    "BN": 1_000_000_000,
    "L": 100_000,  # Lakh
    "l": 100_000,
    "Cr": 10_000_000,  # Crore
    "cr": 10_000_000,
    "CR": 10_000_000,
}


def parse_value(s: Any) -> float | None:
    """Parse a string-or-number into a float. Returns None if uninterpretable.

    Handles:
      "47238221"        -> 47238221.0
      "47,238,221.00"   -> 47238221.0
      "$4.2M"           -> 4200000.0
      "₹2.16Cr"         -> 21600000.0
      "42%"             -> 0.42  (percent → fraction)
      "12.4%"           -> 0.124
      "  148.95 "       -> 148.95
      "(123.45)"        -> -123.45  (accounting-style negative)
      "n/a", "—", ""    -> None
      None              -> None
    """
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    if not isinstance(s, str):
        return None

    raw = s.strip()
    if not raw:
        return None

    # Common null sentinels in dashboards.
    if raw.lower() in {"n/a", "na", "—", "-", "null", "none", "nil"}:
        return None

    is_percent = raw.endswith("%")
    if is_percent:
        raw = raw[:-1].strip()

    # Accounting parens for negatives: (123.45) → -123.45
    is_negative = False
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1].strip()

    # Strip leading currency symbols + ISO codes (USD / INR / EUR / etc.).
    for sym in CURRENCY_SYMBOLS:
        if raw.startswith(sym):
            raw = raw[len(sym) :].strip()
            break
    iso = re.match(r"^[A-Z]{3}\s+", raw)
    if iso:
        raw = raw[iso.end() :].strip()

    # Look for a magnitude suffix (longest-match: handle "Cr" before "C").
    suffix_multiplier = 1.0
    sorted_suffixes = sorted(SUFFIXES.keys(), key=len, reverse=True)
    for suf in sorted_suffixes:
        if raw.endswith(suf) and len(raw) > len(suf):
            head = raw[: -len(suf)].strip()
            # Only treat as a suffix if what's before is a clean number.
            if (
                re.fullmatch(r"-?\d+(\.\d+)?(\s*[,\s]\s*\d{3})*", head)
                or re.fullmatch(r"-?\d+(?:[,_]\d{3})*(?:\.\d+)?", head)
                or re.fullmatch(r"-?\d+(?:\.\d+)?", head)
            ):
                raw = head
                suffix_multiplier = SUFFIXES[suf]
                break

    # Strip thousands separators (commas, underscores, NBSP, regular spaces).
    raw = re.sub(r"[,_ ]", "", raw)
    raw = raw.replace(" ", "")

    try:
        n = float(raw)
    except ValueError:
        return None

    n *= suffix_multiplier
    if is_negative:
        n = -n
    if is_percent:
        n /= 100.0

    return n


# --- CSV parsing ----------------------------------------------------------


# Heuristic header detection. The first cell is the label and is *always*
# non-numeric (the metric name). The second cell is the value: if it parses
# as a number, the row is data; if not, the row is a header (with column names
# like "Value" or "Amount").
def _looks_like_header(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return parse_value(row[1]) is None


def parse_csv(path: str) -> list[dict]:
    """Parse a reconciliation CSV. Returns list of {label, expected_value, raw_value}.

    Accepts:
      - 2 columns (label, value) — most common
      - 3+ columns: first is label, second is value, the rest are appended to
        label as `(extra1, extra2)` for context.
      - With or without a header row (auto-detected).

    Skips rows where the value can't be parsed as a number; emits them with
    `expected_value: null` so the SKILL can surface them to the user.
    """
    rows: list[dict] = []
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {p}")

    with p.open(newline="") as f:
        reader = csv.reader(f)
        all_rows = [r for r in reader if r and any(c.strip() for c in r)]

    if not all_rows:
        return rows

    # Header detection on the first non-empty row.
    has_header = _looks_like_header(all_rows[0])
    data_rows = all_rows[1:] if has_header else all_rows

    for r in data_rows:
        if len(r) < 2:
            continue
        label = r[0].strip()
        raw_value = r[1].strip()
        extras = [c.strip() for c in r[2:] if c.strip()]
        if extras:
            label = f"{label} ({', '.join(extras)})"
        rows.append(
            {
                "label": label,
                "expected_value": parse_value(raw_value),
                "raw_value": raw_value,
            }
        )

    return rows


# --- Diff -----------------------------------------------------------------


def diff(
    expected: float | None,
    actual: float | None,
    *,
    tolerance: float = 0.01,
) -> dict:
    """Compare expected vs actual. `tolerance` is fractional (0.01 = ±1%).

    Returns:
      {
        "match":      bool,
        "delta":      float (actual - expected) or None,
        "delta_pct":  float ((actual - expected) / expected) or None,
        "reason":     "match" | "mismatch" | "missing_expected" | "missing_actual"
      }
    """
    if expected is None:
        return {"match": False, "delta": None, "delta_pct": None, "reason": "missing_expected"}
    if actual is None:
        return {"match": False, "delta": None, "delta_pct": None, "reason": "missing_actual"}

    delta = actual - expected
    delta_pct: float | None = None
    if expected != 0:
        delta_pct = delta / expected
        match = abs(delta_pct) <= tolerance
    else:
        # Expected is exactly 0 — exact match required (no relative tolerance possible).
        match = actual == 0

    return {
        "match": match,
        "delta": delta,
        "delta_pct": delta_pct,
        "reason": "match" if match else "mismatch",
    }


# --- Band -----------------------------------------------------------------


def band(value: float, *, tolerance: float = 0.01) -> dict:
    """The band a single observed number is allowed to land in, as `GoldenBounds` keys.

    `tolerance` is fractional (0.01 = ±1%), the same shape and default `diff` uses. The four
    keys are exactly the ones `semantic_model.golden.GoldenBounds` accepts, so a caller pastes
    the output into an item and does no arithmetic of its own.

    Returns:
      {"min_rows": 1, "max_rows": 1, "min_value": <float>, "max_value": <float>}
    """
    low, high = value * (1 - tolerance), value * (1 + tolerance)
    # A negative observation inverts the two — and GoldenBounds refuses a floor above its
    # ceiling, so the band would be rejected rather than merely read oddly. A zero value falls
    # out of this as a zero band, which matches diff's rule that a zero expected is only ever
    # matched exactly: there is no relative tolerance around nothing.
    return {
        "min_rows": 1,
        "max_rows": 1,
        "min_value": min(low, high),
        "max_value": max(low, high),
    }


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reconciliation helper for agami-reconcile.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="Parse a reconciliation CSV.")
    p_parse.add_argument("--csv", required=True)

    # `--tolerance` is read the way every other number on this CLI is read, rather than by
    # argparse's float: the skill's own prose says a tolerance out loud as `±1%` in a dozen places
    # and then tells the caller to pass the run's tolerance, so `1%` is the form that actually
    # arrives. `parse_value` folds it to 0.01 and leaves 0.01 alone, so the decimal form every
    # existing caller passes is untouched.
    p_diff = sub.add_parser("diff", help="Diff two numbers.")
    p_diff.add_argument("--expected", required=True)
    p_diff.add_argument("--actual", required=True)
    p_diff.add_argument("--tolerance", default="0.01")

    p_band = sub.add_parser("band", help="Band an observed number for a golden item's bounds.")
    p_band.add_argument("--value", required=True)
    p_band.add_argument("--tolerance", default="0.01")

    args = p.parse_args(argv)

    if args.cmd == "parse":
        rows = parse_csv(args.csv)
        print(json.dumps(rows, indent=2))
        return 0

    if args.cmd in ("diff", "band"):
        # Refused rather than defaulted. A tolerance nobody can read is not a reason to fall back
        # to ±1%: the caller asked for a band of some other width, and silently returning the
        # default one would answer a question they did not ask.
        tolerance = parse_value(args.tolerance)
        if tolerance is None or tolerance < 0:
            print(
                f"reconcile {args.cmd}: could not read a tolerance from --tolerance "
                f"{args.tolerance!r}; pass a fraction (0.01) or a percentage (1%).",
                file=sys.stderr,
            )
            return 2

    if args.cmd == "diff":
        e = parse_value(args.expected)
        a = parse_value(args.actual)
        result = diff(e, a, tolerance=tolerance)
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "band":
        value = parse_value(args.value)
        if value is None:
            # Refused rather than crashed, and specifically not with exit 1: the band feeds a
            # golden item, and 1 on that path is the save door's "needs confirmation" — a
            # traceback landing on it would read as a stop somebody could say yes to.
            print(
                f"reconcile band: could not read a number from --value {args.value!r}; "
                "pass the observed value as it was recorded.",
                file=sys.stderr,
            )
            return 2
        print(json.dumps(band(value, tolerance=tolerance), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
