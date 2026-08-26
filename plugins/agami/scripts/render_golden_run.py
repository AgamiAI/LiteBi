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

# How precisely an accuracy is shown. The score itself is left unrounded where it is computed — an
# item passes at exactly 1.0, and rounding there would hand the pass mark to a near miss — so the
# rounding happens here instead, where it is presentation and no verdict rests on it.
_ACCURACY_DECIMALS = 3

# The largest accuracy that may be shown for an item that did not score exactly 1.0. Rounding alone
# is not enough: 4002/4004 rounds to 1.000, so a near miss would read as a perfect score printed
# beside the sentence saying it did not reproduce the answer key. That is the one confusion this
# report exists to remove, so a score short of the mark is shown short of the mark.
_NEARLY_ONE = 1.0 - 10.0 ** -_ACCURACY_DECIMALS

# The counts the header reads. Taken from the run's own summary and never recounted from the
# items: a report that recomputed one would be a second place that decides what a run looks like.
# Exactly the five tiles the page draws — a name here that nothing reads is a value reaching the
# file for no reason, which is the opposite of what a whitelist is for.
_SUMMARY_COUNTS = ("total", "passed", "failed", "unscored", "errored")

# What one side of the table-set claim may carry through. The page joins these into a sentence with
# `Array.join`, so anything else is a broken page rather than a wrong word.
_SCALAR = (str, int, float)


def _shown(accuracy: float) -> float:
    """One accuracy at the precision the page prints it, never rounded up to the pass mark.

    Only an accuracy of exactly 1.0 may be shown as 1.000, because that is the only value that
    passes. Anything short of it is held below, so the number beside a failure never looks like
    the number beside a pass.
    """
    if accuracy >= 1.0:
        return round(accuracy, _ACCURACY_DECIMALS)
    return min(round(accuracy, _ACCURACY_DECIMALS), _NEARLY_ONE)


def _names(value: Any) -> list[str]:
    """One side of the table-set claim, flattened to the strings the page prints.

    The claim is written by the statement comparator and its shape is promised by a docstring, not
    by anything on this side of the handoff. The page joins the list into a sentence, so a bare
    string arriving where a list was promised makes `.join` undefined, throws, and leaves the whole
    page blank — the same silent failure a broken payload causes. Anything that is not a scalar is
    dropped rather than rendered, because a nested structure would print its own punctuation.
    """
    if not isinstance(value, list):
        return []
    return [str(name) for name in value if isinstance(name, _SCALAR)]


def _tables_claim(item: dict) -> Optional[dict[str, Any]]:
    """The table-set difference, which is the one line the page puts above the two statements.

    "Generated read `orders`, the answer key read `customers`" is usually the whole finding. It is
    the first of the seven claims the statement comparator writes, and an item whose generation
    never produced a statement has no claims at all — so the absence is carried as None rather than
    faked as an agreement.
    """
    claims = (item.get("claims") or {}).get("claims") or []
    if not claims:
        return None
    claim = claims[0]
    return {
        # Carried rather than assumed: the page labels the line from the claim's own name, so a
        # run that one day writes its claims in another order labels it correctly instead of
        # calling something else "tables".
        "name": claim.get("name", ""),
        "status": claim.get("status", ""),
        "generated": _names(claim.get("generated")),
        "golden": _names(claim.get("golden")),
    }


def _item(item: dict) -> dict[str, Any]:
    """One case, reduced to what the page draws.

    Named field by field rather than passed through, and that is this function's whole job. A run's
    artifact is free to grow, and a page assembled by copying it would render whatever turned up —
    including, one day, the result rows this report promises not to show. What is not named here
    cannot reach the file.
    """
    score = item.get("score") or {}
    accuracy = score.get("accuracy")
    return {
        "item_key": item.get("item_key", ""),
        "question": item.get("question", ""),
        "section": item.get("section", ""),
        "confirmed": bool(item.get("confirmed")),
        "passed": bool(item.get("passed")),
        "gated": bool(item.get("gated")),
        "status": score.get("status", ""),
        # None means nothing was scored and 0.0 is a score an item earned, so the two are kept
        # apart here as carefully as they are where they were decided.
        "accuracy": None if accuracy is None else _shown(accuracy),
        "reason": score.get("reason", ""),
        "expected_sql": item.get("expected_sql", ""),
        "generated_sql": item.get("generated_sql", ""),
        "tables": _tables_claim(item),
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
        "completed": bool(summary.get("completed")),
        "sections": summary.get("sections") or {},
        "verified": any(item.get("confirmed") for item in items),
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
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{GENERATED_AT}}",
                 datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{PROFILE}}", profile or "")
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
        .replace("{{THEME_CSS}}", theme_css)
        .replace("{{RUN_JSON}}", run_json)
    )


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Render one golden run as a self-contained report.")
    p.add_argument("--title", required=True,
                   help="Report title (e.g., 'Golden run · orders · demo')")
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--items-file", required=True,
                   help="Path to the run's JSON artifact")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(os.path.expanduser(args.items_file)) as f:
        run = json.load(f)
    if not isinstance(run, dict):
        sys.stderr.write(
            f"--items-file must contain a JSON object, got {type(run).__name__}\n"
        )
        return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(
        title=args.title,
        profile=args.profile,
        run=run,
    ), encoding="utf-8")
    count = len(run.get("items") or [])
    print(f"Wrote {out_path} ({count} case{'s' if count != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
