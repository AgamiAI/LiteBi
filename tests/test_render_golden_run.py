"""The golden-run report: what it draws, and the two things it must and must not carry.

`render_golden_run.py` reads `shared/golden-run-template.html`, substitutes placeholders and
returns one self-contained HTML file. It renders a run that was already scored — it decides
nothing — so every assertion here is about what reached the page.

Two of them are the point of the file:

* **Both statements are on it.** The answer key and the generated statement, side by side, is the
  whole reason a report exists. The rule that keeps a key off a terminal is about stdout and about
  what a model reads; a gitignored file a person opens afterwards is neither.
* **No result row is on it.** The comparator already reduced them to a score, and a self-contained
  file full of rows is the kind of thing that gets pasted into a chat window.

The page builds its DOM from an embedded JSON payload, so — as in the sibling renderer's tests —
what is asserted is that payload and the template's own literal markup.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_golden_run import render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")
RENDERER = REPO_ROOT / "plugins" / "agami" / "scripts" / "render_golden_run.py"

# The template's own source. Several assertions below are over this text rather than over a rendered
# DOM, and that is the ceiling of what this repository tests rather than laziness: the page builds
# every element in JavaScript, there is no browser in this suite, and adding one to assert a heading
# exists would be a dependency out of all proportion to the claim. So the drawing code is pinned by
# its markup — that the call site is there, and in the right order — which is what a mutation of the
# renderer would take out. The sibling `test_render_examples_validation.py` has the same ceiling.
TEMPLATE = (REPO_ROOT / "plugins" / "agami" / "shared" / "golden-run-template.html").read_text(
    encoding="utf-8"
)

# Planted in the two statements so "both statements are rendered" is an assertion rather than an
# inspection, the way the eval helper's own tests plant them to assert the opposite about stdout.
GOLDEN_SENTINEL = "goldensentinelzzq"
GENERATED_SENTINEL = "generatedsentinelzzq"

# The presentation order the run itself writes down. Repeated here as fixture data only: the
# renderer reads the order out of the run rather than holding one of its own, which is what
# `test_the_sections_keep_the_order_the_run_wrote_them_in` pins.
SECTIONS = ("failure", "error", "unscored", "unconfirmed", "pass")


def _claims(generated: list[str], golden: list[str], status: str = "differs") -> dict[str, Any]:
    """A statement difference in the shape the run writes it — seven claims, tables first."""
    rest = [
        {"name": name, "status": "agrees", "generated": None, "golden": None}
        for name in (
            "filter_predicates",
            "date_window",
            "group_keys",
            "join_keys",
            "ordering",
            "limit",
        )
    ]
    return {
        "claims": [
            {"name": "tables", "status": status, "generated": generated, "golden": golden},
            *rest,
        ],
        "gates": [],
        "gated": False,
    }


def _item(**overrides: Any) -> dict[str, Any]:
    """One scored case, as the run's artifact carries it."""
    item = {
        "item_key": "orders-count",
        "question": "How many orders have been placed?",
        "expected_sql": f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM orders",
        "generated_sql": f"SELECT COUNT(id) AS {GENERATED_SENTINEL} FROM orders",
        "score": {
            "status": "scored",
            "accuracy": 1.0,
            "reason": "every row matched",
            "unmatched_golden_columns": [],
            "golden_row_count": 1,
            "generated_row_count": 1,
            "order_sensitive": False,
            "notes": [],
        },
        "claims": _claims(["orders"], ["orders"], status="agrees"),
        "confirmed": True,
        "passed": True,
        "gated": False,
        "section": "pass",
    }
    score = {**item["score"], **overrides.pop("score", {})}
    return {**item, **overrides, "score": score}


def _run(items: list[dict[str, Any]], **summary: Any) -> dict[str, Any]:
    """A whole run, with the counts a header reads and the section order it renders in."""
    counts = {
        "total": len(items),
        "passed": sum(1 for item in items if item["section"] == "pass"),
        "failed": sum(1 for item in items if item["section"] == "failure"),
        "unscored": sum(1 for item in items if item["section"] == "unscored"),
        "errored": sum(1 for item in items if item["section"] == "error"),
        "gating_failures": sum(1 for item in items if item["section"] == "failure"),
        "completed": True,
        "sections": {
            name: sum(1 for item in items if item["section"] == name) for name in SECTIONS
        },
    }
    return {
        "run_id": "0f1e2d3c4b5a",
        "profile": "demo",
        "dataset": "orders",
        "summary": {**counts, **summary},
        "items": items,
        "findings": [],
    }


def _payload(html: str) -> dict[str, Any]:
    """The JSON the page builds its DOM from, read back out of the rendered file."""
    match = re.search(r"const RUN = (\{.*?\});\n", html, re.S)
    assert match, "the rendered page no longer embeds a RUN payload"
    return json.loads(match.group(1).replace("<\\/", "</"))


def _mixed() -> list[dict[str, Any]]:
    """One case of each section — the run a report is actually opened for."""
    return [
        _item(
            item_key="customers-count",
            section="failure",
            passed=False,
            question="How many customers are on file?",
            expected_sql=f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM customers",
            generated_sql=f"SELECT COUNT(DISTINCT country) AS {GENERATED_SENTINEL} FROM orders",
            score={"status": "scored", "accuracy": 0.0, "reason": "no row matched"},
            claims=_claims(["orders"], ["customers"]),
        ),
        _item(
            item_key="products-count",
            section="error",
            passed=False,
            generated_sql="",
            question="How many products are listed?",
            score={
                "status": "error",
                "accuracy": None,
                "reason": "the generator returned no statement for this question",
            },
            claims=None,
        ),
        _item(
            item_key="unused-status",
            section="unscored",
            passed=False,
            question="How many orders are in a status nobody uses?",
            score={
                "status": "unscored",
                "accuracy": None,
                "reason": "both result sets are empty, so the comparison would check no value",
            },
        ),
        _item(
            item_key="payments-count",
            section="unconfirmed",
            confirmed=False,
            passed=False,
            question="How many payments have been taken?",
            score={"status": "scored", "accuracy": 0.0, "reason": "no row matched"},
        ),
        # The one case where the score and the verdict disagree on purpose: every row agreed, and
        # the statement still did not answer the question the dataset requires. It is the only path
        # on which a genuine 1.000 is printed beside "Did not reproduce the answer key".
        _item(
            item_key="orders-by-status-scoped",
            section="failure",
            passed=False,
            gated=True,
            question="How many orders are there, by status?",
            score={"status": "scored", "accuracy": 1.0, "reason": "every row matched"},
        ),
        _item(),
    ]


# --- The file itself ------------------------------------------------------


def test_no_placeholder_survives_rendering():
    """The template carries no literal `{{...}}` of its own, so a leftover is a substitution miss."""
    html = render(title="Golden run · orders · demo", profile="demo", run=_run(_mixed()))

    assert PLACEHOLDER_RE.findall(html) == []


def test_the_report_loads_nothing_from_the_network():
    """Self-contained, asserted over the rendered file rather than inherited from a sibling: of the
    four templates beside this one, the chart template loads its plotting library from a CDN.

    The footer's link to the product's own site is navigation a person clicks, not a subresource
    the page fetches, so it is allowed — every sibling template carries one."""
    html = render(title="x", profile="demo", run=_run(_mixed()))

    assert "<script src=" not in html
    assert 'rel="stylesheet"' not in html
    assert "url(http" not in html


def test_the_renderer_imports_only_the_standard_library():
    """The plugin's scripts run under whatever `python3` a user has, with no package installed —
    and this one has a second reason: it must not need a SQL parser. That is why the run writes the
    statement difference into its artifact instead of leaving it to be re-derived here."""
    tree = ast.parse(RENDERER.read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


# --- The two data rules ---------------------------------------------------


def test_both_statements_are_rendered_whatever_the_verdict():
    """The answer key and the generated statement, on the failing item and the passing one alike —
    the difference is a diagnostic, not a failure report. Rendering the key here is deliberate: the
    rule that keeps it off a terminal is about stdout and about what a model reads."""
    html = render(title="x", profile="demo", run=_run(_mixed()))

    items = {item["item_key"]: item for item in _payload(html)["items"]}
    for key in ("customers-count", "orders-count"):
        assert GOLDEN_SENTINEL in items[key]["expected_sql"]
        assert GENERATED_SENTINEL in items[key]["generated_sql"]
    assert GOLDEN_SENTINEL in html and GENERATED_SENTINEL in html


def test_no_result_row_reaches_the_report():
    """The other data rule. The page is built from a payload this renderer names field by field, so
    a row that turned up on the run — under any key, now or later — has no way through."""
    rows = [["Ada Lovelace", "1234"], ["Grace Hopper", "5678"]]
    item = _item()
    item["rows"] = rows
    item["row_preview"] = rows
    item["score"] = {**item["score"], "rows": rows}
    run = _run([item])
    run["rows"] = rows

    html = render(title="x", profile="demo", run=run)

    assert "Lovelace" not in html and "Hopper" not in html
    assert "rows" not in _payload(html)["items"][0]


def test_a_closing_script_tag_in_a_statement_cannot_end_the_block():
    """A question or a statement is somebody's text, and the payload lives inside a `<script>`. The
    escape is the sibling renderer's, and it matters more here because the payload IS SQL."""
    html = render(
        title="x",
        profile="demo",
        run=_run(
            [
                _item(
                    question="What breaks </script><img src=x> here?",
                    generated_sql="SELECT 1 -- </script>",
                )
            ]
        ),
    )

    assert "</script><img" not in html
    assert "<\\/script>" in html
    # …and the payload still parses, so the escape is reversible rather than lossy.
    assert _payload(html)["items"][0]["generated_sql"] == "SELECT 1 -- </script>"


# --- The header -----------------------------------------------------------


def test_the_header_reports_the_runs_own_counts():
    """Pass, fail, unscored and error come from the run's summary and are never recounted here — a
    report that recomputed a verdict would be a second place that decides what a run looks like."""
    run = _run(_mixed())
    # Deliberately not what the items add up to: the summary is the authority, so a renderer that
    # counted the items instead would disagree with it here.
    run["summary"]["passed"] = 41
    run["summary"]["failed"] = 42
    run["summary"]["unscored"] = 43
    run["summary"]["errored"] = 44

    summary = _payload(render(title="x", profile="demo", run=run))["summary"]

    assert summary["passed"] == 41 and summary["failed"] == 42
    assert summary["unscored"] == 43 and summary["errored"] == 44
    assert summary["total"] == 6
    # Exactly what the header draws, plus the two things no count says. A whitelist that carried a
    # name nothing on the page reads would not be doing the job the projection exists for.
    assert set(summary) == {
        "total",
        "passed",
        "failed",
        "unscored",
        "errored",
        "completed",
        "sections",
        "verified",
    }


def test_a_run_that_confirmed_nothing_does_not_read_as_a_clean_pass():
    """A dataset whose answer keys nobody has confirmed can score every case and gate on none of
    them, so a run of them must not read as green. Derived from the items — no item confirmed —
    rather than from a count, because no counter says it."""
    draft = _run([_item(item_key="payments-count", section="unconfirmed", confirmed=False)])
    signed_off = _run([_item()])

    drafted = render(title="x", profile="demo", run=draft)

    assert _payload(drafted)["summary"]["verified"] is False
    assert (
        _payload(render(title="x", profile="demo", run=signed_off))["summary"]["verified"] is True
    )
    # …and the page says what that means, in the words the skill already uses for it. Asserted over
    # the template rather than over the rendered file, because the banner's words are in the file
    # whether or not the banner is drawn: what is worth pinning is that the flag draws it.
    assert "if (!s.verified)" in TEMPLATE
    assert "rests on nothing" in TEMPLATE


def test_a_run_that_stopped_partway_says_so():
    """`completed` is not derivable from the counts: a run that broke on its second case and a run
    that finished clean both report no failure."""
    stopped = render(title="x", profile="demo", run=_run([_item()], completed=False))

    assert _payload(stopped)["summary"]["completed"] is False
    # Over the template, for the reason the banner above gives: the sentence ships in every report.
    assert "if (!s.completed)" in TEMPLATE
    assert "The run stopped partway. " in TEMPLATE


# --- Per item -------------------------------------------------------------


def test_every_item_carries_its_question_and_verdict():
    """The question, what the run decided, and why — for every case, in every section."""
    html = render(title="x", profile="demo", run=_run(_mixed()))

    items = _payload(html)["items"]
    assert len(items) == 6
    for item in items:
        assert item["question"] and item["status"] and item["reason"]
        assert item["section"] in SECTIONS
        assert set(item) >= {"passed", "confirmed", "gated", "accuracy"}
    assert "How many orders have been placed?" in html


def test_an_unscored_item_and_an_errored_item_are_told_apart():
    """Both carry `accuracy: null` and neither is a failure, and they are not the same thing: one
    produced no statement, the other produced two result sets with nothing to compare. Each keeps
    its own section and its own reason."""
    items = {
        item["item_key"]: item
        for item in _payload(render(title="x", profile="demo", run=_run(_mixed())))["items"]
    }

    assert items["products-count"]["section"] == "error"
    assert items["products-count"]["status"] == "error"
    assert "no statement" in items["products-count"]["reason"]
    assert items["unused-status"]["section"] == "unscored"
    assert items["unused-status"]["status"] == "unscored"
    assert "empty" in items["unused-status"]["reason"]
    assert items["products-count"]["accuracy"] is None
    assert items["unused-status"]["accuracy"] is None


def test_a_score_of_zero_is_not_a_score_of_nothing():
    """0.0 is a comparison that ran and found no agreement; None is a comparison that never ran.
    Collapsing them would report a wrong answer and an unrunnable case as the same thing."""
    items = {
        item["item_key"]: item
        for item in _payload(render(title="x", profile="demo", run=_run(_mixed())))["items"]
    }

    assert items["customers-count"]["accuracy"] == 0.0
    assert items["products-count"]["accuracy"] is None


def test_the_accuracy_is_shown_to_three_decimals():
    """The score is deliberately unrounded upstream — an item passes at exactly 1.0, and rounding
    there would hand the pass mark to a near miss. 3995 of 4000 rows is one such near miss, and
    presenting it to a person is this renderer's job."""
    near_miss = _item(
        passed=False, section="failure", score={"status": "scored", "accuracy": 3995 / 4000}
    )

    payload = _payload(render(title="x", profile="demo", run=_run([near_miss])))

    assert payload["items"][0]["accuracy"] == 0.999
    assert "0.99875" not in json.dumps(payload)


def test_a_near_miss_is_never_shown_as_a_perfect_score():
    """4002 of 4004 rows rounds to 1.000 at three decimals, and an item passes at exactly 1.0. So
    rounding alone would print a perfect-looking score beside the sentence saying the statement did
    not reproduce the answer key — the one confusion this report exists to remove. Only a real 1.0
    may read as 1.000."""
    near_miss = _item(
        passed=False, section="failure", score={"status": "scored", "accuracy": 4002 / 4004}
    )
    perfect = _item(passed=True, section="pass", score={"status": "scored", "accuracy": 1.0})

    payload = _payload(render(title="x", profile="demo", run=_run([near_miss, perfect])))

    assert payload["items"][0]["accuracy"] == 0.999, "a near miss must not reach the pass mark"
    assert payload["items"][1]["accuracy"] == 1.0, "a real pass still shows as one"


def test_the_table_set_delta_is_rendered_above_the_statements():
    """The table-set delta is usually the whole finding — "generated read `orders`, the answer key
    read `customers`" — so it is one line above the two statements rather than something to be
    spotted in them."""
    items = {
        item["item_key"]: item
        for item in _payload(render(title="x", profile="demo", run=_run(_mixed())))["items"]
    }

    assert items["customers-count"]["tables"] == {
        "name": "tables",
        "status": "differs",
        "generated": ["orders"],
        "golden": ["customers"],
    }
    assert items["orders-count"]["tables"]["status"] == "agrees"
    # A case that never produced a statement has no difference to show, and the page has to render
    # the absence rather than assume the claim is there.
    assert items["products-count"]["tables"] is None


def test_a_claim_that_is_not_a_list_of_names_cannot_reach_the_page():
    """Each side of the claim is joined into a sentence by the page. A bare string arriving where a
    list was promised makes that join undefined, throws, and — since the whole body is built by that
    script — leaves the report blank with no error on it. Nothing on this side of the handoff
    enforces the comparator's shape, so the projection coerces instead of trusting it."""
    run = _run(
        [
            _item(
                claims={
                    "claims": [
                        {
                            "name": "tables",
                            "status": "differs",
                            "generated": ["orders", ["nested", "list"], {"a": 1}, None, 7],
                            "golden": "customers",
                        }
                    ]
                }
            )
        ]
    )

    tables = _payload(render(title="x", profile="demo", run=run))["items"][0]["tables"]

    assert tables["generated"] == ["orders", "7"]
    assert tables["golden"] == []
    assert "nested" not in json.dumps(tables)


def test_a_gated_item_shows_a_perfect_score_beside_a_failing_verdict():
    """The one case where the score and the verdict disagree, and the reason the page never derives
    one from the other: every row agreed, so the accuracy really is 1.0, and the statement still did
    not write the filter the dataset requires. A renderer that recomputed `passed` from the accuracy
    would turn this run's only real failure into a pass."""
    items = {
        item["item_key"]: item
        for item in _payload(render(title="x", profile="demo", run=_run(_mixed())))["items"]
    }

    gated = items["orders-by-status-scoped"]
    assert gated["accuracy"] == 1.0 and gated["passed"] is False
    assert gated["gated"] is True and gated["section"] == "failure"
    # …and the page says which of the two it is, rather than leaving a reader to reconcile them.
    assert "if (item.gated)" in TEMPLATE
    assert "the dataset requires a filter this statement does not write" in TEMPLATE


def test_the_sections_keep_the_order_the_run_wrote_them_in():
    """The run and the skill share one presentation order, and its own comment says the report
    reads the same one so the two cannot disagree. So the renderer takes the order from the run
    instead of holding a second copy of it."""
    run = _run(_mixed())
    run["summary"]["sections"] = {
        "pass": 1,
        "failure": 1,
        "error": 1,
        "unscored": 1,
        "unconfirmed": 1,
    }

    payload = _payload(render(title="x", profile="demo", run=run))

    assert list(payload["summary"]["sections"]) == [
        "pass",
        "failure",
        "error",
        "unscored",
        "unconfirmed",
    ]


def test_a_case_under_a_section_the_summary_never_listed_still_reaches_the_page():
    """A report that silently described fewer cases than ran would be worse than a fallback nobody
    reaches. The runner's own summary always names every section, so this cannot come from it — but
    `--items-file` is a public argument taking JSON somebody else wrote, and a hand-edited run is a
    real input."""
    run = _run([_item(item_key="hand-edited", section="rewritten-by-hand")])
    run["summary"]["sections"] = {"pass": 1}

    payload = _payload(render(title="x", profile="demo", run=run))

    assert payload["items"][0]["section"] == "rewritten-by-hand"
    # And the page draws it under a heading of its own rather than filtering it away.
    assert 'el("h2", { text: "Other" })' in TEMPLATE


# --- The two sizes --------------------------------------------------------


def test_a_run_of_one_and_a_run_of_two_hundred_both_render():
    """The smallest dataset anybody writes and one large enough that the page has to stay usable."""
    one = render(title="x", profile="demo", run=_run([_item()]))
    assert len(_payload(one)["items"]) == 1

    many = [
        _item(
            item_key=f"case-{n:03d}",
            question=f"Question {n}?",
            section="failure" if n % 2 else "pass",
            passed=bool(n % 2 == 0),
        )
        for n in range(200)
    ]
    big = render(title="x", profile="demo", run=_run(many))

    payload = _payload(big)
    assert len(payload["items"]) == 200
    assert payload["summary"]["total"] == 200
    assert "Question 199?" in big


def test_a_run_with_no_items_still_renders():
    """An author who created the dataset and has not written a case yet. The reader calls that a
    dataset, so the run does — and a report of it must not be a stack trace."""
    html = render(title="x", profile="demo", run=_run([]))

    assert PLACEHOLDER_RE.findall(html) == []
    assert _payload(html)["items"] == []


# --- The substitutions ----------------------------------------------------


def test_the_title_the_profile_and_the_dataset_reach_the_page():
    """The profile is drawn from its own placeholder, so it is asserted there and carried in the
    payload nowhere — a second copy of it would be a value the page never reads."""
    html = render(title="Golden run · orders · demo", profile="demo", run=_run([_item()]))

    assert "Golden run · orders · demo" in html
    assert "profile <code>demo</code>" in html
    payload = _payload(html)
    assert payload["dataset"] == "orders"
    assert "profile" not in payload


def test_a_placeholder_written_into_a_question_is_not_substituted_into():
    """A question is free text and a statement is somebody's SQL, so either may contain the literal
    text of another placeholder. The run's JSON is substituted last for exactly this: substituted
    earlier, a later replace splices a stylesheet into the object literal, the JSON stops parsing,
    the script throws at load and the report renders blank with nothing on it saying why."""
    html = render(
        title="x",
        profile="demo",
        run=_run(
            [
                _item(
                    question="How many {{THEME_CSS}} orders?",
                    generated_sql="SELECT COUNT(*) FROM orders -- {{PROFILE}}",
                )
            ]
        ),
    )

    # The assertion is that this parses at all; the values are checked so it cannot pass by having
    # eaten the placeholders instead.
    item = _payload(html)["items"][0]
    assert item["question"] == "How many {{THEME_CSS}} orders?"
    assert item["generated_sql"] == "SELECT COUNT(*) FROM orders -- {{PROFILE}}"
    assert "profile <code>demo</code>" in html


def test_a_run_that_is_not_an_object_is_refused():
    """The `--items-file` handoff is JSON somebody else wrote, and a list is the sibling's shape —
    a renderer that half-read one would produce a page describing nothing."""
    with pytest.raises(ValueError, match="run"):
        render(title="x", profile="demo", run=[_item()])  # type: ignore[arg-type]


# --- What the template actually draws -------------------------------------
#
# Everything above reads the payload the page is built from. That is most of what there is to
# assert in a repository with no browser in its test suite — but on its own it is a ceiling worth
# naming: a template that drew an empty div would satisfy every one of those assertions, because
# the payload would still be embedded in the file. So the drawing code is pinned by its own markup:
# that each call site is present, and that the ones whose order carries meaning are in that order.
# It is not a substitute for a rendered DOM; it is the check that fails when the drawing is gutted.


def _css_rule(selector: str) -> str:
    """One CSS rule's declarations, by exact selector."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", TEMPLATE)
    assert match, f"{selector} is no longer a rule in the template"
    return match.group(1).strip()


def test_the_template_draws_both_statements_on_every_case():
    """The two statements side by side are the whole reason the page exists, and they are drawn in
    one place — a case built without them would still carry them in the payload and show neither."""
    assert 'statement("Answer key", item.expected_sql' in TEMPLATE
    assert 'statement("Generated", item.generated_sql' in TEMPLATE
    assert 'class: "statements"' in TEMPLATE


def test_the_template_draws_the_question_the_verdict_and_the_delta_for_a_case():
    """One case is a question, what the run decided about it, the table-set difference and the two
    statements. The delta is between the verdict and the statements on purpose: "generated read one
    table and the answer key read another" is usually the whole finding, so it is read before them
    rather than spotted inside them."""
    assert 'class: "question", text: item.question' in TEMPLATE
    assert "text: SECTION_LABEL[item.section] || item.section" in TEMPLATE

    verdict = TEMPLATE.index("verdictLine(item),")
    delta = TEMPLATE.index("deltaLine(item),")
    statements = TEMPLATE.index('el("div", { class: "statements" }')
    assert verdict < delta < statements


def test_the_page_draws_its_banners_its_counts_and_its_sections():
    """The three calls that put anything on the page at all. Without them the file still parses,
    still carries the whole run, and renders an empty box."""
    for call in ("renderBanners();", "renderCounts();", "renderSections();"):
        assert call in TEMPLATE


def test_an_unscored_case_and_an_errored_case_do_not_look_alike():
    """They are told apart in the payload above; this is the half of that claim a reader sees. Both
    carry a null accuracy and neither is a failure, so if their pills were styled the same the page
    would show one thing where the run recorded two."""
    unscored, errored = _css_rule(".pill.unscored"), _css_rule(".pill.error")

    assert unscored and errored
    assert unscored != errored


# ---------------------------------------------------------------------------
# A payload this renderer did not write
# ---------------------------------------------------------------------------
# `--items-file` is a handoff, and the shape on the far side of one is promised by a docstring
# rather than enforced by anything here. A run edited by hand, written by an older version, or
# produced by something else entirely must cost the field it broke and never the whole page: the
# report is what a person opens when a run has already gone wrong, so it failing too is the one
# failure with no recourse.


def test_a_non_dict_claims_block_costs_the_delta_line_and_not_the_page():
    items = _mixed()
    items[0]["claims"] = ["tables", "differs"]
    payload = _payload(render(title="x", profile="demo", run=_run(items)))
    assert payload["items"][0]["tables"] is None
    assert len(payload["items"]) == len(items)


def test_a_non_dict_first_claim_costs_the_delta_line_and_not_the_page():
    items = _mixed()
    items[0]["claims"] = {"claims": ["tables"]}
    payload = _payload(render(title="x", profile="demo", run=_run(items)))
    assert payload["items"][0]["tables"] is None


def test_a_non_numeric_accuracy_reads_as_unscored_rather_than_throwing():
    """`_shown` compares against 1.0, so a string here would raise and take the report with it."""
    items = _mixed()
    items[0]["score"] = {"status": "scored", "accuracy": "0.0", "reason": "hand-edited"}
    payload = _payload(render(title="x", profile="demo", run=_run(items)))
    assert payload["items"][0]["accuracy"] is None
    assert payload["items"][0]["reason"] == "hand-edited"


def test_a_non_dict_score_reads_as_unscored_rather_than_throwing():
    items = _mixed()
    items[0]["score"] = "scored"
    payload = _payload(render(title="x", profile="demo", run=_run(items)))
    assert payload["items"][0]["accuracy"] is None
    assert payload["items"][0]["status"] == ""


def test_a_run_that_omits_completed_is_not_banner_ed_as_stopped_partway():
    """Absent is not False. Only a run that SAYS it stopped is shown as one."""
    run = _run(_mixed())
    del run["summary"]["completed"]
    assert _payload(render(title="x", profile="demo", run=run))["summary"]["completed"] is True


def test_a_run_that_says_it_stopped_is_still_banner_ed():
    run = _run(_mixed(), completed=False)
    assert _payload(render(title="x", profile="demo", run=run))["summary"]["completed"] is False


def test_a_truthy_non_bool_confirmed_does_not_make_a_run_read_as_verified():
    """The string "False" is truthy, and a run nobody signed off must never render as verified."""
    items = _mixed()
    for item in items:
        item["confirmed"] = "False"
    payload = _payload(render(title="x", profile="demo", run=_run(items)))
    assert payload["summary"]["verified"] is False
