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

from render_chart import RECEIPT_SECTIONS, render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# The real sentence `assemble_receipt` ships on the aggregates section, which is empty and declared
# on purpose. Copied verbatim rather than approximated: the point of the assertion below is that the
# sentence the assembler wrote is the sentence the reader sees.
AGGREGATES_MARKER = (
    "Whether a join multiplies the rows an aggregate is computed from is not checked, so this "
    "section is declared and empty rather than clean."
)


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
            {"ref": "orders", "alias": "o", "qname": "public.orders", "declared": True,
             "rows": 1000, "rows_as_of": None, "freshness": None},
        ]),
        joins=_section([
            {"name": "orders_to_customers", "from_to": "orders → customers",
             "cardinality": "many_to_one", "confidence": "inferred",
             "review_state": "unreviewed", "origin": "introspect_heuristic",
             "signed_off_by": None, "signed_off_role": None, "signed_off_at": None,
             "cross_schema": False, "on": "orders.customer_id = customers.id"},
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
    its review state — not a pre-rendered warning sentence, which the receipt no longer carries."""
    html = render(
        title="Test",
        summary="",
        sections=[_chart_section()],
        receipt=_receipt_with_unreviewed_join(),
    )
    assert "orders_to_customers" in html
    assert "unreviewed" in html
    # The CSS class is in the template — confirm the banner styling is wired. Deleting the banner
    # (which is exactly what happened when the old `receipt.warnings` key went away and the
    # `Array.isArray` guard silently stopped matching) fails here.
    assert "trust-warning-banner" in html
    # And the derivation itself, so the banner cannot be re-pointed back at a key that is gone.
    assert "unreviewedJoins" in html
    # The call to action the pre-rendered warning list used to end with, re-authored in the template.
    assert "open the review queue" in html


def test_the_undetermined_marker_reaches_the_rendered_page():
    """A section's marker is the difference between "checked, found nothing" and "not checked", and
    it is worth nothing if it stops at the JSON. `aggregates` ships empty WITH a marker today, so a
    correct rendering visibly says aggregates were not checked."""
    html = render(
        title="Test",
        summary="",
        sections=[_chart_section()],
        receipt=_receipt_clean(),
    )
    assert AGGREGATES_MARKER in html
    # ...and the panel reads it rather than only carrying it: the renderer draws the marker under
    # every section, labelled, from `markerOf`.
    assert "markerOf" in html
    assert "receipt-undetermined" in html
    assert "Not established" in html


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
    with pytest.raises(ValueError, match=f"receipt.{missing}"):
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
    its own emptiness, which is a fact, unlike an absent section."""
    html = render(title="x", summary="", sections=[_chart_section()], receipt=_receipt())
    assert '"aggregates"' in html
    assert AGGREGATES_MARKER in html
