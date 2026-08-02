#!/usr/bin/env python3
"""
HTML report renderer for agami-query.

Reads plugins/agami/shared/chart-template.html, substitutes every placeholder
(including the inline agami logos from shared/agami-logo-{dark,light}.svg),
and writes a self-contained HTML file containing one or more sections. Each
section has its own chart + table + insight + SQL — but they all live in
the same file. Stdlib only.

The agami-query SKILL invokes this script in Phase 4e instead of
doing template substitution through the LLM's Read + Write tools — that
path costs ~30KB of token I/O per query (template + two SVG logos) and is
the dominant slowness in chart rendering. Calling this script keeps the
LLM's job to producing a small JSON sections file, and the cheap shell
substitution lives here.

Usage (single section — backwards compatible with the old chart):

    python render_chart.py \\
        --title "Top customers" \\
        --summary "Carol Chen leads at $148.95, ahead of the next customer by 3x." \\
        --section '{
          "title": "Top customers by spend",
          "insights": "Carol Chen leads at $148.95.",
          "chart_type": "bar",
          "labels": ["Carol Chen","Dave Davis","Bob Brown"],
          "datasets": [{"label":"Spend","data":[148.95,93.96,45.0]}],
          "table_headers": ["Customer","Spend"],
          "table_rows": [["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]],
          "sql": "SELECT c.name, SUM(...) FROM ..."
        }' \\
        --out <artifacts_dir>/local/charts/single.html

Usage (multi-section narrative):

    python render_chart.py \\
        --title "How is the business doing?" \\
        --summary "Revenue up 12% QoQ; Carol Chen is the top customer; pending orders growing." \\
        --sections-file my-report.json \\
        --out <artifacts_dir>/local/charts/q1-review.html

The file `my-report.json` is a JSON array of section objects, see the
SECTIONS_JSON schema documented in shared/chart-template.html.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

VALID_TYPES = {"bar", "line", "pie", "doughnut", "scatter"}

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "chart-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"   # dark text — for light backgrounds
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"  # light text — for dark backgrounds


def _validate_section(sec: dict, idx: int) -> None:
    if not isinstance(sec, dict):
        raise ValueError(f"section {idx}: must be an object")
    if "title" not in sec or not isinstance(sec["title"], str):
        raise ValueError(f"section {idx}: 'title' (string) is required")
    ct = sec.get("chart_type")
    if ct is not None and ct not in VALID_TYPES:
        raise ValueError(
            f"section {idx}: chart_type must be one of {sorted(VALID_TYPES)} or null, got {ct!r}"
        )
    headers = sec.get("table_headers")
    rows = sec.get("table_rows")
    if headers is not None and not isinstance(headers, list):
        raise ValueError(f"section {idx}: table_headers must be a list")
    if rows is not None and not isinstance(rows, list):
        raise ValueError(f"section {idx}: table_rows must be a list of lists")


def _format_sql(sql: str) -> str:
    """Pretty-print a SQL string for display in the chart's SQL section.

    Tries sqlglot first (best results — proper indentation, keyword case,
    line breaks at clause boundaries). Falls back to a small heuristic
    formatter if sqlglot isn't installed: insert newlines before common
    top-level clause keywords. Either way, returns a multi-line string
    that's readable when wrapped in <pre>.

    The original SQL passes through unchanged if it's already multi-line
    (heuristic: contains a newline) — assume the caller knew what they
    were doing.
    """
    if not isinstance(sql, str) or not sql.strip():
        return sql
    if "\n" in sql:
        return sql

    try:
        import sqlglot
        # pretty=True formats with indentation; dialect=None means generic ANSI
        return sqlglot.transpile(sql, pretty=True)[0]
    except Exception:
        pass

    # Heuristic fallback: break before every top-level clause keyword.
    # Not as pretty as sqlglot but still much better than one line.
    import re
    keywords = [
        "SELECT", "FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY",
        "LIMIT", "OFFSET", "UNION ALL", "UNION", "INTERSECT", "EXCEPT",
        "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "OUTER JOIN",
        "FULL JOIN", "CROSS JOIN", "JOIN",
        "WITH", "ON",
    ]
    out = sql
    for kw in keywords:
        out = re.sub(rf"\s+{kw}\s+", f"\n{kw} ", out, flags=re.IGNORECASE)
    return out.strip()


# The five sections a receipt carries, named exactly as `semantic_model.runtime.assemble_receipt`
# emits them and as the contract type declares them. Re-listed here rather than imported because
# this script is stdlib-only and ships inside the plugin, with no path back to the package.
RECEIPT_SECTIONS = ("columns", "tables", "joins", "aggregates", "assumptions")


def _validate_receipt(receipt: dict) -> None:
    """Validate the trust-receipt shape. The fields are documented in chart-template.html →
    RECEIPT_JSON schema. Receipts are optional — callers that don't have one pass None / omit
    --receipt-file — but a receipt that IS passed must carry all five sections, each with its
    `items` list and its `undetermined` marker.

    A MISSING section is an error here, not a tolerated omission. The previous check was
    `if arr_key in receipt`, so a receipt that had silently lost a key validated clean and the
    panel simply drew nothing for it: the reader then could not tell a section that found nothing
    from a section that was never reported. That is the "absent versus empty" ambiguity the whole
    receipt shape exists to kill, and letting it back in at the last hop before the page would undo
    it for the one surface a user actually reads.

    `undetermined` must be PRESENT even when it is null: null is the positive claim "this section is
    complete", and a key that is simply absent claims nothing.
    """
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a JSON object")
    for name in RECEIPT_SECTIONS:
        if name not in receipt:
            raise ValueError(
                f"receipt.{name} is missing: every section must be present, because a section that "
                f"is absent and a section that is empty are different facts"
            )
        section = receipt[name]
        if not isinstance(section, dict):
            raise ValueError(
                f"receipt.{name} must be an object with 'items' and 'undetermined'"
            )
        if not isinstance(section.get("items"), list):
            raise ValueError(f"receipt.{name}.items must be a list")
        if "undetermined" not in section:
            raise ValueError(
                f"receipt.{name}.undetermined is missing: pass null when the section is complete"
            )
        if not isinstance(section["undetermined"], (str, type(None))):
            raise ValueError(f"receipt.{name}.undetermined must be a string or null")
    if "model_version" in receipt and not isinstance(receipt["model_version"], (str, type(None))):
        raise ValueError("receipt.model_version must be a string or null")


def render(
    *,
    title: str,
    summary: str,
    sections: list,
    receipt: dict | None = None,
) -> str:
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")
    for i, sec in enumerate(sections):
        _validate_section(sec, i)
    if receipt is not None:
        _validate_receipt(receipt)

    # Format SQL in every section before serializing.
    sections = [
        {**sec, "sql": _format_sql(sec["sql"])} if isinstance(sec.get("sql"), str) else sec
        for sec in sections
    ]

    template = TEMPLATE_PATH.read_text()
    logo_dark_svg = LOGO_DARK_PATH.read_text()
    logo_light_svg = LOGO_LIGHT_PATH.read_text()
    theme_css = (SHARED_DIR / "theme.css").read_text()

    # JSON embeds carry user/model text (SQL, insights, descriptions). Escape `</` so a
    # `</script>` can't terminate the <script> block (JS unescapes `<\/` → `</`). The
    # template's doc comment has no real `{{…}}` tokens, so a user `-->` can't close it.
    def j(obj):
        return json.dumps(obj).replace("</", "<\\/")

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{REPORT_TITLE_JSON}}", j(title))
        .replace("{{REPORT_SUMMARY_JSON}}", j(summary or ""))
        .replace("{{GENERATED_AT}}", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{SECTIONS_JSON}}", j(sections))
        # `null` for receipt-less reports — the template's JS checks `if (receipt)` and skips.
        .replace("{{RECEIPT_JSON}}", j(receipt))
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
        .replace("{{THEME_CSS}}", theme_css)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True, help="Report title (the user's question)")
    p.add_argument("--summary", default="", help="1-3 sentence executive summary across all sections")

    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--section", action="append", help="JSON object for a single section. Repeat for multiple.")
    src.add_argument("--sections-file", help="Path to a JSON file containing a list of section objects.")

    p.add_argument(
        "--receipt-file",
        help="Path to a JSON file with the trust receipt object: `model_version` plus the five "
             "sections (columns, tables, joins, aggregates, assumptions), each an object with an "
             "`items` list and an `undetermined` marker saying what the section did not establish "
             "(null when it is complete). Produced by `sm receipt`. Optional — when omitted, the "
             "report renders without a receipt panel.",
    )

    p.add_argument("--out", required=True)
    args = p.parse_args()

    if args.sections_file:
        with open(os.path.expanduser(args.sections_file)) as f:
            sections = json.load(f)
        if not isinstance(sections, list):
            sys.stderr.write(f"--sections-file must contain a JSON array, got {type(sections).__name__}\n")
            return 1
    else:
        sections = [json.loads(s) for s in args.section]

    receipt = None
    if args.receipt_file:
        with open(os.path.expanduser(args.receipt_file)) as f:
            receipt = json.load(f)
        if not isinstance(receipt, dict):
            sys.stderr.write(f"--receipt-file must contain a JSON object, got {type(receipt).__name__}\n")
            return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(title=args.title, summary=args.summary, sections=sections, receipt=receipt))
    print(f"Wrote {out_path} ({len(sections)} section{'s' if len(sections) != 1 else ''}"
          f"{', with trust receipt' if receipt else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
