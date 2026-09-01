"""The golden-dataset explorer: what the page says a dataset IS, before any run.

`render_golden_datasets.py` walks every golden dataset for a profile, reads the model the way the
model explorer does, and writes one self-contained HTML page. It decides nothing about a run — the
verdicts it shows were written down by AH-110 — so every assertion here is about what reached the
page and what could not.

The page builds its DOM from an embedded JSON payload, so — as in the sibling renderers' tests —
what is asserted is that payload and the template's own literal markup.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

from render_golden_datasets import build_payload, render  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")
TEMPLATE = (REPO_ROOT / "plugins" / "agami" / "shared" / "golden-datasets-template.html").read_text(
    encoding="utf-8"
)

PROFILE = "demo"


# --- The fixture ----------------------------------------------------------
#
# A synthetic profile, hand-built: three model tables of which one — `channels` — is touched by no
# confirmed answer key, so the coverage gap this page exists to show is deliberate rather than
# incidental. Two datasets, because a developer's question is about the answer key and not about
# one file.


def _model(root: Path) -> None:
    """The semantic model the explorer's own manifest builder reads."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [
                    {"storage_connection": "c", "schema": "SALES_DATA", "table": name}
                    for name in ("orders", "customers", "channels")
                ],
            }
        )
    )
    columns = {
        "orders": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "customer_id", "type": "integer"},
            {"name": "status", "type": "string"},
            {"name": "placed_at", "type": "timestamp"},
            {"name": "total", "type": "decimal"},
        ],
        "customers": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "name", "type": "string"},
        ],
        "channels": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "name", "type": "string"},
        ],
    }
    for name, cols in columns.items():
        (root / "subject_areas" / "sales" / "tables" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "schema": "SALES_DATA",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": f"the {name} table",
                    "columns": cols,
                }
            )
        )
    for metric, calculation in (
        ("order_count", "count of orders"),
        ("revenue_total", "sum of order totals"),
    ):
        (root / "subject_areas" / "sales" / "metrics" / f"{metric}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": metric,
                    "calculation": calculation,
                    "bindings": {"PostgreSQL": "COUNT(*)"},
                    "source_tables": ["orders"],
                }
            )
        )


# One statement per confirmed case, written out so a test can assert what coverage read from it.
ORDERS_COUNT_SQL = "SELECT COUNT(*) AS order_count FROM orders"
ORDERS_BY_CUSTOMER_SQL = (
    "SELECT c.name, COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status = 'paid' AND o.placed_at >= '2024-01-01' AND o.placed_at < '2025-01-01' "
    "GROUP BY c.name"
)
# The relativity lint's own shape: a question that slides with the calendar over an answer key
# pinned to a fixed date. AH-100 reports it and this page must report exactly what AH-100 does.
REVENUE_LAST_QUARTER_SQL = (
    "SELECT SUM(total) AS revenue FROM orders WHERE placed_at >= '2024-01-01'"
)


def _datasets(root: Path) -> None:
    """Two datasets: one carrying the well-formed cases, one carrying the relativity fault."""
    gdir = root / "golden_datasets"
    gdir.mkdir(parents=True)
    (gdir / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "description": "Order-volume questions over the synthetic store.",
                "category": "orders",
                "test_cases": [
                    {
                        "id": "orders-count",
                        "query": "How many orders have been placed?",
                        "expected": {"sql": ORDERS_COUNT_SQL, "sql_confirmed": True},
                        "tags": ["smoke"],
                        "recorded": {
                            "columns": ["order_count"],
                            "rows": [[812]],
                            "at": "2026-01-14T09:00:00Z",
                        },
                        "confirmed_by": {
                            "method": "reviewed against the seed by hand",
                            "at": "2026-01-14T09:00:00Z",
                        },
                    },
                    {
                        # Confirmed, and nobody signed for it — one of this page's own two lints.
                        "id": "orders-by-customer",
                        "query": "How many orders did each customer place in 2024?",
                        "expected": {"sql": ORDERS_BY_CUSTOMER_SQL, "sql_confirmed": True},
                        "match": "values",
                        "must_filter": ["status"],
                        "tags": ["orders"],
                        "recorded": {
                            "columns": ["name", "n"],
                            "rows": [["acme", 7]],
                            "at": "2026-01-14T09:00:00Z",
                        },
                    },
                    {
                        # The legal in-progress shape: no answer key, so it cannot gate a run.
                        "id": "orders-draft",
                        "query": "How many orders were refunded?",
                        "expected": {"sql_confirmed": False},
                    },
                ],
            }
        )
    )
    (gdir / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "description": "Revenue questions over the synthetic store.",
                "test_cases": [
                    {
                        "id": "revenue-last-quarter",
                        "query": "What was revenue last quarter?",
                        "expected": {"sql": REVENUE_LAST_QUARTER_SQL, "sql_confirmed": True},
                        "confirmed_by": {"method": "spot-checked against the seed"},
                    }
                ],
            }
        )
    )


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    """An artifacts dir holding one profile: the model above and the two datasets above."""
    root = tmp_path / PROFILE
    root.mkdir(parents=True)
    _model(root)
    _datasets(root)
    return tmp_path


def _payload(html: str) -> dict[str, Any]:
    """The JSON the page builds its DOM from, read back out of the rendered file."""
    match = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    assert match, "the rendered page no longer embeds a DATA payload"
    return json.loads(match.group(1).replace("<\\/", "</"))


def _rendered(artifacts: Path) -> str:
    """The page as the command writes it — the whole path, payload included."""
    return render(
        title="Golden datasets · demo",
        profile=PROFILE,
        payload=build_payload(PROFILE, artifacts),
    )


def _items(html: str) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in _payload(html)["items"]}


# --- SC1: every dataset on one page ---------------------------------------


def test_every_dataset_in_the_profile_renders_on_one_page(artifacts):
    """A developer's question is about the answer key, not about one file, so both datasets and
    every case in them are on the page — each item saying which file it came from."""
    payload = _payload(_rendered(artifacts))

    assert [d["name"] for d in payload["datasets"]] == ["orders", "revenue"]
    assert {item["id"] for item in payload["items"]} == {
        "orders-count",
        "orders-by-customer",
        "orders-draft",
        "revenue-last-quarter",
    }
    assert {item["dataset"] for item in payload["items"]} == {"orders", "revenue"}


def test_an_item_carries_the_fields_the_page_draws_and_nothing_else(artifacts):
    """The whitelist is the point: a dataset file is free to grow, and a page assembled by copying
    one would render whatever turned up — including the author's own recorded result rows."""
    item = _items(_rendered(artifacts))["orders-by-customer"]

    assert set(item) == {
        "id",
        "dataset",
        "question",
        "confirmed",
        "match",
        "tags",
        "must_filter",
        "has_recorded",
        "has_confirmed_by",
        "sql",
        "verdict",
    }
    assert item["match"] == "values"
    assert item["must_filter"] == ["status"]
    assert item["tags"] == ["orders"]
    assert item["has_recorded"] is True and item["has_confirmed_by"] is False


def test_no_recorded_result_row_reaches_the_page(artifacts):
    """`recorded` is the author's receipt — their own data — and it is reduced to a boolean. A
    self-contained file full of rows is the kind of thing that gets pasted into a chat window."""
    html = _rendered(artifacts)

    assert "812" not in html
    assert _items(html)["orders-count"]["has_recorded"] is True


# --- SC2: what can gate, separately from what exists ----------------------


def test_the_header_states_what_can_gate_apart_from_what_exists(artifacts):
    """A dataset of forty items with three confirmed must not read as forty items of coverage, so
    the two counts are carried separately and labelled apart on the page."""
    totals = _payload(_rendered(artifacts))["totals"]

    assert totals["total"] == 4
    assert totals["gating"] == 3
    assert totals["datasets"] == 2
    # …and the page draws them as two tiles saying which is which, rather than one number.
    assert "Items that can gate a run" in TEMPLATE
    assert "Items in the datasets" in TEMPLATE


def test_a_dataset_carries_its_own_two_counts(artifacts):
    """Per dataset as well as per profile: a profile that gates well overall can hold one file
    nobody has confirmed a case in."""
    datasets = {d["name"]: d for d in _payload(_rendered(artifacts))["datasets"]}

    assert datasets["orders"]["total"] == 3 and datasets["orders"]["gating"] == 2
    assert datasets["revenue"]["total"] == 1 and datasets["revenue"]["gating"] == 1


# --- SC6: the page renders standalone -------------------------------------


def test_the_page_renders_from_files_with_no_model_call(artifacts):
    """The whole path — read the datasets, read the model, write the file — runs against a fixture
    profile with no client, no credential and no connection. That is the criterion, and it is what
    lets this page be regenerated as often as somebody wants it."""
    html = _rendered(artifacts)

    assert "<html" in html and "Golden datasets · demo" in html
    assert "profile <code>demo</code>" in html


def test_the_page_loads_nothing_from_the_network(artifacts):
    """Self-contained, asserted over the rendered file: of the templates beside this one, the chart
    template loads its plotting library from a CDN. The footer's link to the product's own site is
    navigation a person clicks, not a subresource the page fetches."""
    html = _rendered(artifacts)

    assert "<script src=" not in html
    assert 'rel="stylesheet"' not in html
    assert "url(http" not in html


def test_no_placeholder_survives_rendering(artifacts):
    """The template carries no literal `{{...}}` of its own, so a leftover is a substitution miss."""
    assert PLACEHOLDER_RE.findall(_rendered(artifacts)) == []


# --- The payload is substituted last --------------------------------------


def test_a_placeholder_written_into_a_question_is_not_substituted_into(artifacts):
    """A question is free text and an answer key is somebody's SQL, so either may contain the
    literal text of another placeholder. The payload is substituted last for exactly this:
    substituted earlier, a later replace splices a stylesheet into the object literal, the JSON
    stops parsing, the script throws at load and — because the whole body is built by that script —
    the page renders blank with nothing on it saying why."""
    gdir = artifacts / PROFILE / "golden_datasets"
    (gdir / "placeholders.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "placeholder-question",
                        "query": "How many {{THEME_CSS}} orders?",
                        "expected": {
                            "sql": "SELECT COUNT(*) FROM orders -- {{PROFILE}}",
                            "sql_confirmed": True,
                        },
                    }
                ]
            }
        )
    )

    html = _rendered(artifacts)

    # The assertion is that this parses at all; the values are checked so it cannot pass by having
    # eaten the placeholders instead.
    item = _items(html)["placeholder-question"]
    assert item["question"] == "How many {{THEME_CSS}} orders?"
    assert item["sql"] == "SELECT COUNT(*) FROM orders -- {{PROFILE}}"
    assert "profile <code>demo</code>" in html


def test_a_closing_script_tag_in_a_statement_cannot_end_the_block(artifacts):
    """A question and an answer key both live inside a `<script>`, and the payload IS SQL."""
    (artifacts / PROFILE / "golden_datasets" / "escapes.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "escape-question",
                        "query": "What breaks </script><img src=x> here?",
                        "expected": {
                            "sql": "SELECT 1 -- </script>",
                            "sql_confirmed": True,
                        },
                    }
                ]
            }
        )
    )

    html = _rendered(artifacts)

    assert "</script><img" not in html
    assert "<\\/script>" in html
    # …and the payload still round-trips, so the escape is reversible rather than lossy.
    assert _items(html)["escape-question"]["sql"] == "SELECT 1 -- </script>"
