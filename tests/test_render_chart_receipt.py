"""
Regression tests for the trust-receipt panel in render_chart.py.

These exercise the {{RECEIPT_JSON}} placeholder, the derived unreviewed-join banner, and the
`undetermined` markers. The legacy (no-receipt) path must still render — backward compatibility is
critical until every caller is migrated.

The receipts below are the shape `semantic_model.runtime.assemble_receipt` emits: five sections,
each `{"items": [...], "undetermined": "<sentence>" | null}`, beside the `model_version` pin.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_chart import RECEIPT_SECTIONS, TEMPLATE_PATH, render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# --- what an assertion in this file is allowed to be made of --------------------------------
#
# The output is one HTML file: the template's own text, plus the receipt serialized into it. So an
# assertion here proves one of exactly two things, and it has to be built to prove the one it
# claims:
#
#   * "the receipt reached the page" — assert a value that exists ONLY in the receipt. Every such
#     value below is a NONCE, pinned by `test_the_receipt_derived_assertions_use_values_the_template
#     _never_contains`. The tests used to assert `orders_to_customers` (in the RECEIPT_JSON doc
#     comment), `unreviewed` (in the CSS and in `approvalPhrase`) and `trust-warning-banner` (a CSS
#     rule) — all five assertions in the unreviewed-join test passed against `receipt=None`, so
#     nothing in the suite could tell a rendered banner from a stylesheet that still defines one.
#
#   * "the page still draws it" — assert the CONSTRUCTION SITE, the `el(...)` call or the string
#     literal that exists only where the element is built. A class name lives in the stylesheet
#     whether or not anything constructs the element; deleting the block that draws it leaves the
#     class name behind and takes the construction site with it.
#
# There is no JS runtime in this suite, so no assertion here can prove what the DOM ends up looking
# like. These two together are what is provable from the artifact, and they are what the two
# user-facing things this change could break silently — the banner and the markers — are guarded by.

# The real sentence `assemble_receipt` ships on the aggregates section, which is empty and declared
# on purpose. Copied verbatim rather than approximated: the point of the assertion below is that the
# sentence the assembler wrote is the sentence the reader sees. It doubles as the marker nonce — no
# marker sentence is template text.
AGGREGATES_MARKER = (
    "Whether a join multiplies the rows an aggregate is computed from is not checked, so this "
    "section is declared and empty rather than clean."
)

# A relationship named nothing in this repo names, so its presence in the output is the receipt's
# doing and nothing else's. Synthetic, like every other fixture value here.
NONCE_JOIN = "shipments_to_carriers"
NONCE_FROM_TO = "shipments → carriers"
# The spelling the PAGE carries: the receipt is embedded with `json.dumps`, which escapes non-ASCII,
# so the arrow lands as its \u escape. Asserting the source spelling would fail for a reason that
# has nothing to do with what this file is testing.
NONCE_FROM_TO_ENCODED = "shipments \\u2192 carriers"
# Serialized with `json.dumps`' spacing, which the template's own hand-written JSON examples do not
# use — so even a field name that appears in the doc comment discriminates in this spelling.
NONCE_REVIEW_STATE = '"review_state": "unreviewed"'
NONCE_AD_HOC_ORIGIN = '"origin": "ad_hoc"'
NONCE_EMPTY_SECTION = '"assumptions": {"items": [], "undetermined": null}'

# Construction sites. Each of these exists exactly once in the template, inside the block that
# builds the thing it names, and vanishes with that block.
BANNER_CONSTRUCTION = "el('div', { class: 'trust-warning-banner' })"
BANNER_DERIVATION = "const unreviewed = unreviewedJoins();"
BANNER_CTA = "open the review queue"
MARKER_CONSTRUCTION = "el('div', { class: 'receipt-undetermined' })"
MARKER_LABEL = "el('span', { class: 'lbl', text: 'Not established' })"
MARKER_READ = "const marker = markerOf(name);"
EMPTY_STATE_CONSTRUCTION = "text: 'Checked, and there was nothing to report.'"


def _section(items=None, undetermined=None) -> dict:
    return {"items": list(items or []), "undetermined": undetermined}


def _chart_section() -> dict:
    return {
        "title": "Top customers",
        "insights": "Carol Chen leads.",
        "chart_type": "bar",
        "labels": ["Carol Chen", "Dave Davis"],
        "datasets": [{"label": "Spend", "data": [148.95, 93.96]}],
        "table_headers": ["Customer", "Spend"],
        "table_rows": [["Carol Chen", "$148.95"], ["Dave Davis", "$93.96"]],
        "sql": "SELECT name, SUM(amount) FROM orders GROUP BY name",
    }


def _receipt(**overrides) -> dict:
    """A complete receipt with every section present, which is now the minimum the renderer takes."""
    base = {"model_version": "abc123def456"}
    base.update({name: _section() for name in RECEIPT_SECTIONS})
    base["aggregates"] = _section(undetermined=AGGREGATES_MARKER)
    base.update(overrides)
    return base


def _receipt_clean() -> dict:
    return _receipt(
        columns=_section([
            {"column": "public.orders.amount", "metric": None},
            {"column": None, "metric": {
                "name": "total_spend", "area": "sales",
                "definition_prose": "Sum of completed-order amounts in USD.",
                "expression": "SUM(amount)", "confidence": "confirmed",
                "review_state": "approved", "origin": "model",
                "signed_off_by": "you@example.com", "signed_off_role": "cfo",
                "signed_off_at": "2026-03-15T10:00:00Z",
            }},
        ]),
        tables=_section([
            {"ref": "orders", "alias": None, "qname": "public.orders", "declared": True,
             "rows": 12000, "rows_as_of": "2026-05-01",
             "freshness": "2026-05-09T23:00:00Z (nightly batch)"},
        ]),
        joins=_section([
            {"name": "orders_to_customers", "from_to": "orders → customers",
             "cardinality": "many_to_one", "confidence": "confirmed",
             "review_state": "approved", "origin": "fk",
             "signed_off_by": "you@example.com", "signed_off_role": "data_lead",
             "signed_off_at": "2026-03-15T10:00:00Z", "cross_schema": False,
             "on": "orders.customer_id = customers.id"},
        ]),
    )


def _receipt_with_unreviewed_join() -> dict:
    return _receipt(
        model_version="abc123",
        tables=_section([
            {"ref": "shipments", "alias": "s", "qname": "public.shipments", "declared": True,
             "rows": 1000, "rows_as_of": None, "freshness": None},
        ]),
        joins=_section([
            {"name": NONCE_JOIN, "from_to": NONCE_FROM_TO,
             "cardinality": "many_to_one", "confidence": "inferred",
             "review_state": "unreviewed", "origin": "introspect_heuristic",
             "signed_off_by": None, "signed_off_role": None, "signed_off_at": None,
             "cross_schema": False, "on": "shipments.carrier_id = carriers.id"},
        ]),
    )


def _receipt_with_ad_hoc_metric() -> dict:
    """A metric agami composed for this one answer: no review state to speak of, and no confidence
    label either, because the model never declared it."""
    return _receipt(
        columns=_section([
            {"column": None, "metric": {
                "name": "median_transit_days", "area": "logistics",
                "definition_prose": "Median days from dispatch to delivery.",
                "expression": "PERCENTILE_CONT(0.5)", "confidence": None,
                "review_state": "unreviewed", "origin": "ad_hoc",
                "signed_off_by": None, "signed_off_role": None, "signed_off_at": None,
            }},
        ]),
    )


# --- happy paths ----------------------------------------------------------

def test_render_with_receipt_substitutes_payload():
    html = render(
        title="Test report",
        summary="A test.",
        sections=[_chart_section()],
        receipt=_receipt_clean(),
    )
    # The receipt JSON ends up inline in the JS. Check key pieces.
    assert "model_version" in html
    assert "abc123def456" in html
    assert "you@example.com" in html


def test_render_with_unreviewed_join_carries_the_banner_inputs():
    """The banner is DERIVED from the joins section, so what has to reach the page is the join and
    its review state — not a pre-rendered warning sentence, which the receipt no longer carries.

    Both halves are asserted with values that can only come from where they claim to come from: the
    join's name and endpoints are nonces, and the banner is checked at its construction site rather
    than by its class name. Every assertion this test used to make was satisfied by static template
    text and passed against `receipt=None` — including the class name, which is a CSS rule, and
    `unreviewedJoins`, which is a function definition that exists whether or not anything calls it.
    """
    html = render(
        title="Test",
        summary="",
        sections=[_chart_section()],
        receipt=_receipt_with_unreviewed_join(),
    )
    # The receipt reached the page: nothing in the template contains these.
    assert NONCE_JOIN in html
    assert NONCE_FROM_TO_ENCODED in html
    assert NONCE_REVIEW_STATE in html
    # And the page still builds the banner out of them. Deleting the banner block — which is what
    # nearly happened when the old `receipt.warnings` key went away and the `Array.isArray` guard
    # silently stopped matching — takes all three of these with it.
    assert BANNER_CONSTRUCTION in html
    assert BANNER_DERIVATION in html
    assert BANNER_CTA in html


def test_the_undetermined_marker_reaches_the_rendered_page():
    """A section's marker is the difference between "checked, found nothing" and "not checked", and
    it is worth nothing if it stops at the JSON. `aggregates` ships empty WITH a marker today, so a
    correct rendering visibly says aggregates were not checked.

    Same two halves: the sentence is the assembler's own and appears nowhere in the template, and
    the drawing of it is asserted where it is drawn. `markerOf`, `receipt-undetermined` and
    `Not established` on their own are a function definition, a CSS rule and a string in a block
    this test could not tell you still existed.
    """
    html = render(
        title="Test",
        summary="",
        sections=[_chart_section()],
        receipt=_receipt_clean(),
    )
    assert AGGREGATES_MARKER in html
    # ...and the panel reads it rather than only carrying it: the renderer draws the marker under
    # every section, labelled, from `markerOf`.
    assert MARKER_READ in html
    assert MARKER_CONSTRUCTION in html
    assert MARKER_LABEL in html


def test_the_receipt_derived_assertions_use_values_the_template_never_contains():
    """What keeps the two tests above honest as the template changes.

    A receipt-derived assertion only discriminates while its value is absent from the template. The
    values these tests started with were not: `orders_to_customers` is in the RECEIPT_JSON doc
    comment and `unreviewed` is in both the CSS and `approvalPhrase`, so the assertions held with no
    receipt at all. Pin the property rather than the vigilance — a future edit that puts one of
    these strings into the template fails here, not silently three tests over.
    """
    template = TEMPLATE_PATH.read_text()
    for nonce in (AGGREGATES_MARKER, NONCE_JOIN, NONCE_FROM_TO, NONCE_FROM_TO_ENCODED,
                  NONCE_REVIEW_STATE, NONCE_AD_HOC_ORIGIN, NONCE_EMPTY_SECTION):
        assert nonce not in template, f"{nonce!r} is template text, so it proves nothing"


def test_the_construction_sites_the_assertions_stand_on_are_each_unique():
    """The other half. A construction-site assertion proves the block still exists only while the
    string it names appears exactly once — at the site. Two occurrences and deleting the block no
    longer fails the test."""
    template = TEMPLATE_PATH.read_text()
    for site in (BANNER_CONSTRUCTION, BANNER_DERIVATION, BANNER_CTA, MARKER_CONSTRUCTION,
                 MARKER_LABEL, MARKER_READ, EMPTY_STATE_CONSTRUCTION):
        assert template.count(site) == 1, f"{site!r} appears {template.count(site)}x, not once"


# --- the two display bugs fixed in this change ----------------------------

def test_an_unreviewed_entry_reads_its_confidence_label_not_a_question_mark():
    """Regression for the display bug that reached every user who opened the panel.

    `approvalPhrase`'s unreviewed branch was
    `typeof entry.confidence === 'number' ? entry.confidence.toFixed(2) : '?'`, and `confidence` is
    a LABEL — confirmed / inferred / proposed — so the numeric test never matched and every
    unreviewed relationship and metric read "unreviewed (confidence ?)".

    The suite has no JS runtime, so the assertion is on the phrase at its construction site in the
    rendered page, plus the entry it is computed from. Reverting the branch fails both halves.
    """
    html = render(title="Test", summary="", sections=[_chart_section()],
                  receipt=_receipt_with_unreviewed_join())

    assert "'unreviewed (confidence ' + entry.confidence + ')'" in html
    # The exact expression the bug returned. Asserting the absence of the numeric test itself would
    # match the comment above the branch, which explains the bug and is worth keeping.
    assert "'unreviewed (confidence ' + c + ')'" not in html
    # The label the phrase reads is on the page, so the phrase has something to say.
    assert '"confidence": "inferred"' in html


def test_an_entry_with_no_confidence_at_all_says_so_rather_than_inventing_a_label():
    """The other arm of the same branch. A metric agami composed for this one answer has no
    confidence label, because the model never declared it — so the phrase says it is not signed off
    instead of naming a confidence the model does not have."""
    html = render(title="Test", summary="", sections=[_chart_section()],
                  receipt=_receipt_with_ad_hoc_metric())

    assert "'unreviewed, not signed off'" in html
    assert '"confidence": null' in html


def test_a_metric_calculated_for_this_answer_says_that_in_both_places_it_appears():
    """`origin: "ad_hoc"` is a metric that is not in the model at all, which is a different thing
    from one the user has not got round to approving. Both surfaces that describe it say so: the
    provenance phrase and the approval banner's badge."""
    html = render(title="Test", summary="", sections=[_chart_section()],
                  receipt=_receipt_with_ad_hoc_metric())

    assert NONCE_AD_HOC_ORIGIN in html
    assert "'calculated for this answer, not yet in your model'" in html
    assert "text: 'calculated on the fly'" in html
    # And the banner that carries that badge is built from the metric this receipt supplied.
    assert "median_transit_days" in html


def test_the_named_filters_block_no_producer_ever_filled_is_gone():
    """The second display bug in the same change: the panel drew a "Named filters applied" heading
    from `receipt.named_filters`, a key nothing in the repo has ever emitted. The receipt has no
    such key now, and the block that read it is deleted rather than left guarded."""
    html = render(title="Test", summary="", sections=[_chart_section()], receipt=_receipt_clean())

    # The read and the heading, not the bare word: the doc comment says the key does not exist, and
    # that sentence is the documentation of this very deletion.
    assert "receipt.named_filters" not in html
    assert "Named filters applied" not in html


# --- what the panel says about what it is showing -------------------------

def test_the_join_predicate_is_labelled_as_the_declared_one():
    """The joins list drew `r.on` as a bare `<code>` immediately above the section's own marker,
    which says the predicate the statement actually joined on was NOT read out of the SQL. `r.on` is
    the model's DECLARED predicate, so unlabelled, under that sentence, a reader takes it for the
    condition that ran."""
    html = render(title="Test", summary="", sections=[_chart_section()], receipt=_receipt_clean())

    assert "text: 'declared predicate: '" in html
    assert '"on": "orders.customer_id = customers.id"' in html


def test_the_assumptions_blurb_is_drawn_only_when_there_is_something_to_explain():
    """`renderSection` appended the blurb before the empty-state branch, so an answer with zero
    assumptions drew "These column meanings come from agami, not from you…" immediately followed by
    "Checked, and there was nothing to report." The blurb belongs to the items it describes."""
    html = render(title="x", summary="", sections=[_chart_section()], receipt=_receipt())

    non_empty_arm = html[html.index("if (list.length) {"):html.index("} else if (!marker) {")]
    assert "if (blurb)" in non_empty_arm, "the blurb is drawn outside the arm that has items"


def test_the_trust_banners_second_sentence_counts_like_its_first():
    """One count, two sentences: the first pluralized and the second did not, so two unreviewed
    joins produced "used 2 joins … the join it leaned on is just not confirmed"."""
    html = render(title="Test", summary="", sections=[_chart_section()],
                  receipt=_receipt_with_unreviewed_join())

    assert "(n === 1 ? ' it leaned on is' : 's it leaned on are')" in html


def test_the_renderers_section_tuple_matches_the_contract_type():
    """`RECEIPT_SECTIONS` is a copy of `guardrail.Receipt.SECTIONS`, kept because the renderer is a
    stdlib-only template substitution and will not import a module to read one tuple. A copy nothing
    compares is a copy that drifts: without this, a sixth section added to the type would leave
    `render_chart.py` validating five and quietly accepting a receipt that had lost the new one."""
    guardrail = pytest.importorskip(
        "guardrail",  # the package under test; present in CI, absent only in a bare checkout
        reason="agami-core is not importable, so there is no source tuple to compare against",
    )
    assert RECEIPT_SECTIONS == guardrail.Receipt.SECTIONS


def test_render_legacy_no_receipt_still_works():
    """Backward compat: callers without a receipt still get a clean report."""
    html = render(
        title="Test",
        summary="No receipt path",
        sections=[_chart_section()],
        receipt=None,
    )
    # Receipt is null in the JS — the template's `if (receipt)` guard skips
    # the receipt panel entirely.
    assert "const receipt = null" in html


def test_render_no_unsubstituted_placeholders():
    html = render(
        title="Test",
        summary="x",
        sections=[_chart_section()],
        receipt=_receipt_clean(),
    )
    # The template's HTML doc-comment block contains literal {{...}}
    # placeholders as documentation. Filter those out by looking at lines
    # outside HTML comments.
    in_comment = False
    live_placeholders = []
    for line in html.splitlines():
        if "<!--" in line:
            in_comment = True
        if not in_comment:
            live_placeholders.extend(PLACEHOLDER_RE.findall(line))
        if "-->" in line:
            in_comment = False
    assert live_placeholders == [], f"unsubstituted: {live_placeholders}"


# --- validation guards ----------------------------------------------------

def test_render_rejects_non_dict_receipt():
    with pytest.raises(ValueError, match="receipt"):
        render(title="x", summary="", sections=[_chart_section()], receipt="not a dict")


@pytest.mark.parametrize("missing", RECEIPT_SECTIONS)
def test_render_rejects_a_receipt_missing_a_section(missing):
    """A missing section fails LOUDLY. The panel cannot draw a section it was never given, and a
    silently-skipped one reads to a user exactly like a section that found nothing — which is the
    ambiguity the five-section shape exists to remove."""
    bad = _receipt_clean()
    del bad[missing]
    # Escaped: `match` is a regex and the `.` in `receipt.tables` would otherwise match any
    # character, so the assertion would pass against a message naming a different section.
    with pytest.raises(ValueError, match=re.escape(f"receipt.{missing}")):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


def test_render_rejects_a_section_that_is_not_an_object():
    bad = _receipt_clean()
    bad["tables"] = [{"ref": "orders"}]        # the OLD flat-array spelling
    with pytest.raises(ValueError, match="receipt.tables"):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


def test_render_rejects_non_list_items():
    bad = _receipt_clean()
    bad["tables"] = {"items": "should be a list", "undetermined": None}
    with pytest.raises(ValueError, match="receipt.tables.items"):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


def test_render_rejects_a_section_with_no_undetermined_key():
    """Null is the positive claim "this section is complete"; an absent key claims nothing."""
    bad = _receipt_clean()
    bad["joins"] = {"items": []}
    with pytest.raises(ValueError, match="receipt.joins.undetermined"):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


def test_render_rejects_a_non_string_undetermined():
    bad = _receipt_clean()
    bad["joins"] = {"items": [], "undetermined": 17}
    with pytest.raises(ValueError, match="receipt.joins.undetermined"):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


def test_render_rejects_bad_model_version_type():
    bad = _receipt_clean()
    bad["model_version"] = 12345  # int, not str
    with pytest.raises(ValueError, match="model_version"):
        render(title="x", summary="", sections=[_chart_section()], receipt=bad)


# --- an all-empty receipt is allowed, as long as every section is there ----

def test_render_with_every_section_empty():
    """A receipt whose sections are all present and all empty still renders — each section states
    its own emptiness, which is a fact, unlike an absent section.

    `'"aggregates"'` was the old assertion here, and the template's doc comment contains it, so this
    passed against `receipt=None` too. An empty section is proved by the serialized section, and the
    sentence a reader gets for it by the string literal that only exists where it is built.
    """
    html = render(title="x", summary="", sections=[_chart_section()], receipt=_receipt())
    assert NONCE_EMPTY_SECTION in html
    assert AGGREGATES_MARKER in html
    assert EMPTY_STATE_CONSTRUCTION in html
