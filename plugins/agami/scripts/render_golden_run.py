#!/usr/bin/env python3
"""
Golden-run report renderer.

Reads plugins/agami/shared/golden-run-template.html, substitutes placeholders, and writes a
self-contained HTML file — the fifth of these, after the chart, the model explorer, the
examples-validation queue and the prune view, and deliberately the same shape as the four.

It renders a run that was already scored and decides nothing: every verdict on the page was
written down by the run, including the difference between the two statements. That is also what
lets it be stdlib only — reading a claim difference would need a SQL parser, so the run writes one
into its artifact and this reads it back.

Two properties are the whole point of the page, and both are pinned by tests rather than by this
paragraph:

* **Both statements are on it**, the confirmed answer key beside the generated one. Reading them
  against each other is why a report exists. The rule that keeps an answer key out of the run's
  own output is about a terminal and about what a model's context holds; a gitignored file a
  person opens afterwards is neither.
* **No result row is on it.** The comparator already reduced them to a score and a difference, and
  a self-contained file full of rows is the kind of thing that gets pasted into a chat window when
  somebody asks for help with a failure.

Usage:

    python3 render_golden_run.py \\
        --title "Golden run · orders · demo" \\
        --profile demo \\
        --items-file /tmp/agami-golden-run.json \\
        --out <artifacts_dir>/local/eval/demo/20260826-141500.html
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "golden-run-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"

# The accuracy is deliberately NOT projected, and this is the note that keeps it that way.
#
# It reached the page as a headline number and told a reader nothing. Over a real fifteen-case run
# every value was exactly 1.0 or exactly 0.0, which is structural rather than luck: differing row
# counts, an unpaired column and an extra column each short-circuit to 0.0 before any overlap is
# computed, and `match_columns` pairs only on EQUAL value vectors, so a column that is wrong
# anywhere does not pair at all. The one route to a value in between — every column carrying the
# right set of values while the rows themselves do not line up — is real and rare.
#
# And on exactly that occasion the comparator already writes the same fact in words ("5 of the
# answer key's 15 rows matched"), which `reason` carries. So the number was redundant when it meant
# something and misleading the rest of the time: printed as "accuracy 1.000" beside a gated item
# that had reproduced its answer key perfectly, it read as a contradiction of the sentence next to
# it. What a reader can act on at 5-of-15 is WHICH ten rows differed, and the report cannot show
# that — rows never reach a run result by design.

# The counts the header reads. Taken from the run's own summary and never recounted from the
# items: a report that recomputed one would be a second place that decides what a run looks like.
# Exactly the five tiles the page draws — a name here that nothing reads is a value reaching the
# file for no reason, which is the opposite of what a whitelist is for.
_SUMMARY_COUNTS = ("total", "passed", "failed", "unscored", "errored")

# What one side of the table-set claim may carry through. The page joins these into a sentence with
# `Array.join`, so anything else is a broken page rather than a wrong word.
_SCALAR = (str, int, float)


def _run_stamp(now: Optional[datetime.datetime] = None) -> str:
    """When the run was rendered, as a date somebody would say out loud.

    It read `2026-09-06T11:47:38+00:00`, which is a sortable key rather than a sentence, and the
    page has no second place where the timestamp is a key. UTC is spelled out because the reader is
    not necessarily in it, and the day is not zero-padded because nobody says "06 September".
    """
    stamp = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{stamp.day} {stamp:%B %Y} at {stamp:%H:%M} UTC"


def _names(value: Any, *, ordinals: bool = False) -> list[str]:
    """The entries of one claim's side, flattened to the strings the page prints.

    The claim is written by the statement comparator and its shape is promised by a docstring, not
    by anything on this side of the handoff, so anything unexpected is dropped rather than rendered.

    An entry may itself be a short sequence, and that is not an edge case: `ordering` writes a
    column and a direction together, so `[["incident_count", "desc"]]` is the ordinary shape of an
    ordering difference. Dropping nested entries rendered the one claim that most often differs as
    an empty side — "generated none · answer key none" — which is worse than not drawing it at all.
    A sequence of scalars is joined with a space; anything deeper is still dropped, because it
    would print its own punctuation.
    """
    if not isinstance(value, list):
        return []
    flattened = []
    for name in value:
        if isinstance(name, _SCALAR):
            entry = _readable(str(name))
            flattened.append(_ordinal(entry) if ordinals else entry)
        elif isinstance(name, (list, tuple)):
            entry = _nested(name)
            if entry:
                flattened.append(entry)
    return flattened


def _nested(entry: Any) -> str:
    """One entry that is itself a sequence, of which two shapes reach this page.

    `ordering` writes a column and a direction (`["order_count", "desc"]`), and `join_keys` writes
    the two sides of an equality as a PAIR OF PAIRS —
    `[["customers", "id"], ["orders", "customer_id"]]` — so the nesting is two deep there. The
    earlier coercion dropped anything below the first level, which rendered the two claims most
    likely to differ as empty sides.
    """
    parts = []
    for part in entry:
        if isinstance(part, _SCALAR):
            parts.append(str(part))
        elif isinstance(part, (list, tuple)):
            # A qualified column arrives split into its parts rather than dotted.
            qualified = [str(inner) for inner in part if isinstance(inner, _SCALAR)]
            if qualified:
                parts.append(".".join(qualified))
    if len(parts) == 2 and all("." in part for part in parts):
        # A join key is an equality, and reads as one.
        return f"{parts[0]} = {parts[1]}"
    return " ".join(parts)


def _ordinal(entry: str) -> str:
    """`GROUP BY 1` reaches the claim as the bare string "1", which reads as a number.

    "grouped by 1" against "grouped by service_criticality" tells a reader nothing at all, and the
    ordinal cannot be resolved here — the claim carries no projection to resolve it against. So it
    is labelled rather than decoded, which is honest and readable where a bare digit is neither.
    Resolving it properly belongs to the claim reader, which does hold the select list.
    """
    return f"column {entry} of the select list" if entry.isdigit() else entry


# The infix spelling of each comparison the claim reader writes functionally. Its keys come from
# sqlglot's node names, so this is a lookup rather than a grammar: anything not here is left in the
# form it arrived in, which is already readable for a function call like `count(orders.id)`.
_INFIX = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "ilike": "ILIKE",
    "is": "IS",
    "in": "IN",
}


def _split(inner: str, *, expected: int) -> Optional[list[str]]:
    """The operands of one rendered call, or None if there is not exactly the expected number.

    Split rather than parsed, and only at depth zero and outside quotes: a literal is perfectly
    entitled to contain a comma or a bracket (`eq(customer.name, 'Smith, John')`), and a split that
    ignored that would cut a value in half and print the halves as two operands.
    """
    operands, depth, quoted, current = [], 0, False, []
    for char in inner:
        if quoted:
            current.append(char)
            if char == "'":
                quoted = False
            continue
        if char == "'":
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            operands.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if quoted or depth:
        return None
    operands.append("".join(current).strip())
    return operands if len(operands) == expected else None


def _readable(key: str) -> str:
    """One claim key as a person writes it: `gte(created, '2024-01-01')` → `created >= '2024-01-01'`.

    The functional form is deliberate WHERE IT IS WRITTEN and must stay: a claim rides on tool
    output the calling model reads as server-authored, and something that looked like SQL would be
    read as SQL. That reasoning is about a model's context. This report is a local page a person
    opens, built with `textContent` and rendering nothing — so the argument for the functional form
    does not reach it, while the cost of it does. `gte(created, '2024-01-01')` is a shape a reader
    has to decode before they can compare it to the one beside it.

    Best effort by design: only a known comparison with exactly two operands is rewritten, and
    anything else is returned untouched. A nested predicate is left alone rather than half-rewritten
    into something that reads like SQL and is not.
    """
    if not key.endswith(")"):
        return key
    head, _, rest = key.partition("(")
    inner = rest[:-1]
    name = head.lower()

    # A subquery's whole parse tree is rendered inline, so an `IN (SELECT …)` arrives as forty
    # characters of nested calls. What a reader needs from it is that a subquery is there; the
    # statements are printed underneath if they want the rest.
    if name == "subquery":
        return "(subquery)"

    if name == "between":
        parts = _split(inner, expected=3)
        if parts:
            return f"{parts[0]} BETWEEN {parts[1]} AND {parts[2]}"
        return key

    operator = _INFIX.get(name)
    if operator is None:
        return key
    operands = _split(inner, expected=2)
    if operands is None:
        return key
    operands = [_readable(operand) if operand.endswith(")") else operand for operand in operands]
    if any("(" in operand and not operand.startswith("(") for operand in operands):
        return key
    return f"{operands[0]} {operator} {operands[1]}"


def _window(value: dict) -> str:
    """A resolved date window as an interval a person reads at a glance.

    Half-open is the whole point of resolving one — three spellings of the same year agree, and a
    `BETWEEN` over a timestamp is caught for the off-by-one it is — so the brackets are printed
    rather than described. This is one of the two claims that can gate, which is why its shape is
    handled here instead of falling through to the generic rendering below.
    """
    column = value.get("column")
    start, end = value.get("start"), value.get("end")
    if not column or (start is None and end is None):
        return ""
    open_bracket = "[" if value.get("start_inclusive", True) else "("
    close_bracket = "]" if value.get("end_inclusive", False) else ")"
    return f"{column} in {open_bracket}{start}, {end}{close_bracket}"


def _side(value: Any, *, ordinals: bool = False) -> str:
    """One side of one claim, flattened to the string the page prints.

    Flattened HERE rather than in the template, because the sides are not one shape: a table set is
    a list, a resolved date window is an object, and a limit is a bare number. The page used to
    receive lists alone and call `Array.join` on them, so anything else would have thrown and left
    the whole report blank — the failure this function exists to make unrepresentable.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        return _window(value) or ", ".join(
            f"{key} {item}" for key, item in sorted(value.items()) if isinstance(item, _SCALAR)
        )
    if isinstance(value, list):
        return ", ".join(_names(value, ordinals=ordinals))
    if isinstance(value, _SCALAR):
        return str(value)
    return ""


def _claims(item: dict) -> list[dict[str, Any]]:
    """Every claim the statement comparator wrote, projected for the page.

    The page previously received `claims[0]` — the table set — and nothing else. That is reliably
    the claim that says the least: in a structural failure both statements almost always read the
    same tables, which is why the comparison got far enough to gate on something else at all. The
    claim that explains such a failure is `filter_predicates`, which was computed, written to the
    JSON artifact beside the page, and never rendered. A reader was left to diff two SQL statements
    by eye to recover a difference the run had already worked out.

    Every claim is carried and the page decides what to draw, so the ordering the comparator chose
    survives and a claim added later needs no change here.
    """
    block = item.get("claims")
    claims = block.get("claims") if isinstance(block, dict) else None
    if not isinstance(claims, list):
        return []
    projected = []
    for claim in claims:
        # The payload's shape is promised by a docstring on the far side of a `--items-file`
        # handoff, not by anything here, and a hand-edited or third-party run that breaks the
        # promise must cost one line rather than the whole page.
        if not isinstance(claim, dict):
            continue
        ordinals = claim.get("name") == "group_keys"
        projected.append(
            {
                # Carried rather than assumed: the page labels each line from the claim's own name,
                # so a run that one day writes its claims in another order labels them correctly
                # instead of calling something else "tables".
                "name": claim.get("name", ""),
                "status": claim.get("status", ""),
                # An ordinal is only an ordinal under `group_keys`. A `limit` of 100 is a row
                # count, and labelling it "column 100 of the select list" would be worse than the
                # bare digit this exists to fix.
                "generated": _side(claim.get("generated"), ordinals=ordinals),
                "golden": _side(claim.get("golden"), ordinals=ordinals),
            }
        )
    return projected


def _gates(item: dict) -> list[dict[str, str]]:
    """Which gate fired, and on what.

    The page used to print one hardcoded sentence — "the dataset requires a filter this statement
    does not write" — that named no column, so a reader was told *a* filter was missing and left to
    work out which. It was also written as though `must_filter` were the only gate; structure gates
    twice, and a differing date window rendered as a missing filter.

    The kind and the column are both on the item already. Neither carries a value from a result
    set: a `must_filter` column is a name the dataset's author wrote down, and the date-window gate
    names no boundary here.
    """
    block = item.get("claims")
    gates = block.get("gates") if isinstance(block, dict) else None
    if not isinstance(gates, list):
        return []
    return [
        {"kind": str(gate.get("kind", "")), "column": str(gate.get("column") or "")}
        for gate in gates
        if isinstance(gate, dict)
    ]


def _item(item: dict) -> dict[str, Any]:
    """One case, reduced to what the page draws.

    Named field by field rather than passed through, and that is this function's whole job. A run's
    artifact is free to grow, and a page assembled by copying it would render whatever turned up —
    including, one day, the result rows this report promises not to show. What is not named here
    cannot reach the file.
    """
    score = item.get("score")
    if not isinstance(score, dict):
        score = {}
    accuracy = score.get("accuracy")
    # Whether the rows agreed, kept as the one bit the verdict actually needs. `bool` is excluded
    # from the numeric check on purpose: it is an `int` subclass, and True is not an accuracy.
    # The value itself does not travel — see the note at the top of this file.
    reproduced = (
        not isinstance(accuracy, bool)
        and isinstance(accuracy, (int, float))
        and float(accuracy) == 1.0
    )
    return {
        "item_key": item.get("item_key", ""),
        "question": item.get("question", ""),
        "section": item.get("section", ""),
        "confirmed": bool(item.get("confirmed")),
        "passed": bool(item.get("passed")),
        "gated": bool(item.get("gated")),
        "status": score.get("status", ""),
        # Whether the two result sets agreed, which is NOT whether the item passed: a statement can
        # reproduce its answer key exactly and still fail a structural gate. The page said "did not
        # reproduce the answer key" for precisely that case, which was simply false, so the two
        # facts are carried separately and the verdict is built from both.
        "reproduced": reproduced,
        "reason": score.get("reason", ""),
        "expected_sql": item.get("expected_sql", ""),
        "generated_sql": item.get("generated_sql", ""),
        "claims": _claims(item),
        "gates": _gates(item),
    }


def _summary(run: dict, items: list) -> dict[str, Any]:
    """The header's numbers, plus the one thing no counter says.

    `verified` is whether the run confirmed anything at all: a dataset whose answer keys nobody has
    signed off scores every case and can gate on none of them, and a report of one must not read as
    green. No count carries that, so it is derived from the items — and from nothing else.

    `sections` is the presentation order, taken from the run rather than held here. The run's own
    comment says the report reads the same order so the two cannot disagree about what a run looks
    like, which is only true if there is one copy of it.
    """
    summary = run.get("summary") or {}
    return {
        **{name: summary.get(name) for name in _SUMMARY_COUNTS},
        # Absent is not False. A run that omits the field — an older artifact, a hand-edited one —
        # has not told us it stopped partway, and banner-ing one that finished is the worse error
        # of the two.
        "completed": bool(summary.get("completed", True)),
        "sections": summary.get("sections") or {},
        # `_item` has already narrowed `confirmed` to a real bool, so a truthy non-bool in the
        # payload (the string "False") cannot make a run read as verified.
        "verified": any(item.get("confirmed") is True for item in items),
    }


def render(
    *,
    title: str,
    profile: str,
    run: dict,
) -> str:
    """Render one golden run's report HTML.

    `run` is the run's artifact whole, rather than a bare list of items: the header reads `summary`
    and `completed`, neither of which lives on an item, and handing those over separately would let
    the two halves of the page describe different runs.
    """
    if not isinstance(run, dict):
        raise ValueError("run must be an object — the whole run, not its items")
    items = run.get("items") or []
    if not isinstance(items, list):
        raise ValueError("run['items'] must be a list")

    payload = {
        "run_id": run.get("run_id", ""),
        "dataset": run.get("dataset", ""),
        "summary": _summary(run, items),
        "items": [_item(item) for item in items],
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    logo_dark_svg = LOGO_DARK_PATH.read_text(encoding="utf-8")
    logo_light_svg = LOGO_LIGHT_PATH.read_text(encoding="utf-8")
    theme_css = (SHARED_DIR / "theme.css").read_text(encoding="utf-8")

    # Escape `</` so a `</script>` in a question or a statement can't terminate the <script> block
    # holding the run JSON (JS unescapes `<\/` → `</`). It matters more here than in the sibling
    # renderers, because the payload IS SQL.
    run_json = json.dumps(payload).replace("</", "<\\/")

    # The run's JSON goes in LAST, after every other placeholder. The order matters here in a way it
    # does not in the sibling renderers, and it is deliberate rather than incidental: this payload is
    # somebody's question and somebody's SQL, so a case asking "how many {{THEME_CSS}} orders?" would
    # otherwise have the stylesheet spliced into the object literal by a later replace. The JSON then
    # stops parsing, the script throws at load, and — because the whole body is built by that script
    # — the report renders blank with nothing anywhere saying why.
    return (
        template.replace("{{REPORT_TITLE}}", title)
        .replace("{{GENERATED_AT}}", _run_stamp())
        .replace("{{PROFILE}}", profile or "")
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
        .replace("{{THEME_CSS}}", theme_css)
        .replace("{{RUN_JSON}}", run_json)
    )


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Render one golden run as a self-contained report.")
    p.add_argument(
        "--title", required=True, help="Report title (e.g., 'Golden run · orders · demo')"
    )
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--items-file", required=True, help="Path to the run's JSON artifact")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(os.path.expanduser(args.items_file)) as f:
        run = json.load(f)
    if not isinstance(run, dict):
        sys.stderr.write(f"--items-file must contain a JSON object, got {type(run).__name__}\n")
        return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render(
            title=args.title,
            profile=args.profile,
            run=run,
        ),
        encoding="utf-8",
    )
    count = len(run.get("items") or [])
    print(f"Wrote {out_path} ({count} case{'s' if count != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
