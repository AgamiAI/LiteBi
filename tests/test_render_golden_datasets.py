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
                    # CUSTOMERS is spelled in a different case than every answer key writes it,
                    # so the fold in `_model_tables` is load-bearing: drop it and this table
                    # reads as untouched while a confirmed key demonstrably joins it.
                    for name in ("orders", "CUSTOMERS", "channels")
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
        "CUSTOMERS": [
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
    # Every `<` in the payload, not only the ones that begin a closing tag — see the sibling test
    # below for the case that forced the wider escape.
    assert "\\u003c/script>" in html
    # …and the payload still round-trips, so the escape is reversible rather than lossy.
    assert _items(html)["escape-question"]["sql"] == "SELECT 1 -- </script>"


def test_a_comment_opener_in_one_key_cannot_blank_the_page_via_a_tag_in_another(artifacts):
    """The escape covers every `<`, and this is the case that requires it.

    HTML's script-data tokenizer enters escaped state on an unbalanced `<!--` and double-escaped
    state on a later `<script`; in double-escaped state the template's own closing tag stops
    closing the element, so the page draws its chrome with no data and no error — which reads as a
    profile that has no datasets. The two halves need not be in the same item, and a SQL comment
    produces the `<!--` by accident."""
    (artifacts / PROFILE / "golden_datasets" / "tokenizer.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "opener",
                        "query": "legacy report",
                        "expected": {
                            "sql": "SELECT 1 -- <!-- legacy pipeline",
                            "sql_confirmed": True,
                        },
                    },
                    {
                        "id": "later-tag",
                        "query": "does the <script tag column parse?",
                        "expected": {"sql_confirmed": False},
                    },
                ]
            }
        )
    )

    html = _rendered(artifacts)

    # Neither sequence survives into the script block, so the tokenizer never leaves script-data
    # state and the closing tag still closes.
    assert "<!--" not in html.split("const DATA =", 1)[1]
    assert "<script" not in html.split("const DATA =", 1)[1]
    assert _items(html)["opener"]["sql"] == "SELECT 1 -- <!-- legacy pipeline"
    assert _items(html)["later-tag"]["question"] == "does the <script tag column parse?"


# --- SC3: what the dataset never tests ------------------------------------


def test_coverage_names_a_model_table_no_item_touches(artifacts):
    """The reason this page exists. A dataset of forty questions that never touches one table gives
    a false sense of coverage, and the model change that breaks that table passes cleanly — so the
    gap is computed rather than left to a reader to notice."""
    coverage = _payload(_rendered(artifacts))["coverage"]

    assert coverage["tables_untouched"] == ["channels"]
    assert coverage["tables_exercised"] == ["customers", "orders"]


def test_only_a_confirmed_answer_key_counts_as_coverage(artifacts):
    """An unconfirmed case cannot fail a run, so a table only it reads is a table nothing holds the
    model to. Counting it would report coverage that gates on nothing."""
    (artifacts / PROFILE / "golden_datasets" / "channels.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "channels-count",
                        "query": "How many channels are there?",
                        "expected": {
                            "sql": "SELECT COUNT(*) FROM channels",
                            "sql_confirmed": False,
                        },
                    }
                ]
            }
        )
    )

    coverage = _payload(_rendered(artifacts))["coverage"]

    assert coverage["tables_untouched"] == ["channels"]


def test_the_tables_are_read_as_claims_and_not_as_the_author_declared_them(artifacts):
    """Coverage reads `read_claims`, the one reader of a statement in this repository. What the
    author wrote under `expected.tables_used` is exactly the blind spot the tab exists to catch, so
    a case that declares a table its statement never reads must not close the gap."""
    (artifacts / PROFILE / "golden_datasets" / "declared.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "declared-channels",
                        "query": "How many orders have been placed, again?",
                        "expected": {
                            "sql": "SELECT COUNT(*) FROM orders",
                            "sql_confirmed": True,
                            "tables_used": ["channels"],
                        },
                    }
                ]
            }
        )
    )

    coverage = _payload(_rendered(artifacts))["coverage"]

    assert coverage["tables_untouched"] == ["channels"]


def test_an_answer_key_the_claim_reader_cannot_read_is_named_rather_than_counted(artifacts):
    """`read_claims` returns no tables for a statement it cannot read, and silently unioning that
    would turn the key's own tables into the gap — the page reporting that nothing holds a table a
    confirmed key demonstrably reads. That is the one direction this tab must not be wrong in, so
    the reason the reader hands back is carried onto the page beside the gap."""
    (artifacts / PROFILE / "golden_datasets" / "union.yaml").write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "ids-across-both",
                        "query": "Which ids appear in either table?",
                        "expected": {
                            "sql": "SELECT id FROM channels UNION ALL SELECT id FROM orders",
                            "sql_confirmed": True,
                        },
                    }
                ]
            }
        )
    )

    coverage = _payload(_rendered(artifacts))["coverage"]

    assert [(u["dataset"], u["id"]) for u in coverage["unreadable"]] == [
        ("union", "ids-across-both")
    ]
    assert coverage["unreadable"][0]["reason"]
    # Still reported as a gap, because it genuinely is not held: the point is that the page says
    # why rather than leaving the reader to believe a key was read when it was not.
    assert "channels" in coverage["tables_untouched"]


def test_a_readable_answer_key_leaves_the_unreadable_list_empty(artifacts):
    """The empty case is the ordinary one, and a caveat that is always on says nothing."""
    assert _payload(_rendered(artifacts))["coverage"]["unreadable"] == []


def test_the_model_table_fold_survives_a_key_that_spells_it_differently(artifacts):
    """The model declares CUSTOMERS and every answer key writes `customers`, because `read_claims`
    returns the folded name. Without the fold on the model side the set difference compares two
    spellings of one table and reports a gap that is not there."""
    coverage = _payload(_rendered(artifacts))["coverage"]

    assert "CUSTOMERS" not in coverage["tables_untouched"]
    assert coverage["tables_untouched"] == ["channels"]


def test_the_dialect_comes_from_the_model_rather_than_a_default(artifacts):
    """A statement read in the wrong grammar describes a different statement, and on some engines
    parses to no tables at all — which would report every table as untouched."""
    storage = artifacts / PROFILE / "datasources" / "c" / "storage.yaml"

    assert _payload(_rendered(artifacts))["coverage"]["dialect"] == "postgres"

    storage.write_text(yaml.safe_dump({"name": "c", "storage_type": "Snowflake"}))
    assert _payload(_rendered(artifacts))["coverage"]["dialect"] == "snowflake"


def test_metrics_are_reported_apart_from_the_tables(artifacts):
    """A metric is not one of the seven claims a statement is read into, so it is matched by name
    against the statement text — weaker evidence than a table claim, kept under its own key so the
    tab cannot present the two as the same thing."""
    coverage = _payload(_rendered(artifacts))["coverage"]

    assert coverage["metrics_named"] == ["order_count"]
    assert coverage["metrics_unnamed"] == ["revenue_total"]
    # …and the page says which of the two a reader is looking at.
    assert "matched by name against the answer key" in TEMPLATE


# --- SC5: the lint rows are the reader's own ------------------------------


def test_the_lint_rows_match_what_the_reader_reports(artifacts):
    """Both interpret the same file, and a page that disagreed with the validator would be worse
    than no page: a reader would fix what the page named and the reader would still refuse it."""
    from semantic_model.golden import load_golden_datasets

    _, res = load_golden_datasets(PROFILE, artifacts)
    rows = _payload(_rendered(artifacts))["lint"]

    assert res.findings, "the fixture is meant to carry a relativity fault"
    for finding in res.findings:
        assert {
            "severity": finding.severity,
            "code": finding.code,
            "message": finding.message,
            "locator": finding.locator or "",
        } in rows


def test_no_lint_row_carries_the_statement_it_is_about(artifacts):
    """`golden.py` deliberately keeps the answer key out of a finding, because a finding travels
    wherever its caller sends it. This page renders the key elsewhere; the lint rows have to keep
    matching what the reader reports, so they carry none of it."""
    rows = json.dumps(_payload(_rendered(artifacts))["lint"])

    for sql in (ORDERS_COUNT_SQL, ORDERS_BY_CUSTOMER_SQL, REVENUE_LAST_QUARTER_SQL):
        assert sql not in rows
    assert "SUM(total)" not in rows and "'2024-01-01'" not in rows


def test_the_relativity_fault_reaches_the_page(artifacts):
    """The one fault AH-100 reports on this fixture: a question asked against today over an answer
    key pinned to a fixed date. The two agree today and drift apart on their own."""
    codes = {row["code"]: row for row in _payload(_rendered(artifacts))["lint"]}

    assert codes["golden_relative_question_frozen_sql"]["severity"] == "error"
    assert codes["golden_relative_question_frozen_sql"]["locator"] == (
        "revenue.yaml[revenue-last-quarter]"
    )


def test_the_page_adds_its_own_two_derivations(artifacts):
    """A confirmed key nobody signed for, and a case with no receipt of what the answer looked like
    on the day. Neither is a fault the reader reports — both are warnings rather than errors,
    because the case still gates correctly."""
    rows = _payload(_rendered(artifacts))["lint"]
    by_code = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row["locator"])

    assert by_code["golden_confirmed_without_confirmed_by"] == ["orders.yaml[orders-by-customer]"]
    assert by_code["golden_no_recorded_receipt"] == [
        "orders.yaml[orders-draft]",
        "revenue.yaml[revenue-last-quarter]",
    ]
    assert {
        row["severity"] for row in rows if row["code"] != "golden_relative_question_frozen_sql"
    } == {"warning"}


# --- SC4: the last run's verdict, and no run at all -----------------------


def _outcome(item_key: str, **overrides: Any) -> dict[str, Any]:
    """One case as a run's artifact records it."""
    return {
        "item_key": item_key,
        "question": "recorded by the run",
        "expected_sql": "",
        "generated_sql": "",
        "confirmed": True,
        "passed": True,
        "gated": False,
        "score": {"status": "scored", "accuracy": 1.0, "reason": "every row matched"},
        "claims": None,
        "section": "pass",
        **overrides,
    }


def _write_run(artifacts: Path, stamp: str, dataset: str, items: list, selection=None) -> None:
    out = artifacts / "local" / "eval" / PROFILE
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stamp}.json").write_text(
        json.dumps(
            {
                "run_id": stamp,
                "profile": PROFILE,
                "dataset": dataset,
                "selection": selection,
                "summary": {"total": len(items), "completed": True},
                "items": items,
                "findings": [],
            }
        )
    )


def test_an_items_last_verdict_renders_when_a_run_left_one(artifacts):
    """After a run, the question is which cases stopped matching — so the verdict is on the item
    rather than one page away."""
    _write_run(
        artifacts,
        "20260901-101500",
        "orders",
        [
            _outcome("orders-count"),
            _outcome(
                "orders-by-customer",
                passed=False,
                gated=True,
                section="failure",
                score={"status": "scored", "accuracy": 0.5, "reason": "half the rows matched"},
            ),
        ],
    )

    items = _items(_rendered(artifacts))

    assert items["orders-count"]["verdict"] == {
        "passed": True,
        "gated": False,
        "confirmed": True,
        "section": "pass",
        "score": 1.0,
    }
    assert items["orders-by-customer"]["verdict"]["passed"] is False
    assert items["orders-by-customer"]["verdict"]["gated"] is True
    # A case the run never covered says nothing rather than saying it passed.
    assert items["orders-draft"]["verdict"] is None


def test_a_dataset_nobody_has_run_is_not_an_error_state(artifacts):
    """The normal starting state. Every verdict is absent, the page still renders, and it says so
    in its own words rather than leaving a column of blanks."""
    html = _rendered(artifacts)

    assert all(item["verdict"] is None for item in _payload(html)["items"])
    assert PLACEHOLDER_RE.findall(html) == []
    assert "No run has scored these cases yet" in TEMPLATE


def test_a_verdict_never_crosses_from_one_dataset_to_another(artifacts):
    """An item id is unique within its file and nothing makes it unique across a profile, so the
    verdicts are keyed per dataset — otherwise one dataset's run would answer for another's case."""
    _write_run(artifacts, "20260901-101500", "revenue", [_outcome("orders-count", passed=False)])

    items = _items(_rendered(artifacts))

    assert items["orders-count"]["verdict"] is None


def test_a_run_that_was_a_selection_is_not_read_as_the_last_run(artifacts):
    """AH-110's rule, mirrored: a `--tag` slice or a re-run of the failures describes only some of
    the dataset, so reading it would report a narrower run as the whole one."""
    _write_run(artifacts, "20260901-090000", "orders", [_outcome("orders-count")])
    _write_run(
        artifacts,
        "20260901-101500",
        "orders",
        [_outcome("orders-count", passed=False, section="failure")],
        selection="tag=smoke",
    )

    assert _items(_rendered(artifacts))["orders-count"]["verdict"]["passed"] is True


def test_an_unreadable_record_costs_that_record_and_not_the_page(artifacts):
    """A hand-edited artifact, or one written before the verdict fields existed. The page shows no
    verdict rather than a wrong one, and falls back to the newest record it can read."""
    _write_run(artifacts, "20260901-090000", "orders", [_outcome("orders-count")])
    out = artifacts / "local" / "eval" / PROFILE
    (out / "20260901-101500.json").write_text("{not json at all")

    assert _items(_rendered(artifacts))["orders-count"]["verdict"]["passed"] is True


def test_the_page_writes_nothing_a_rerun_would_read_as_a_run(artifacts, tmp_path):
    """The page lands in the directory AH-110 globs for `*.json` to find the previous run, so a
    manifest dropped beside it would be read back as a run record — of a run that never happened."""
    from render_golden_datasets import main

    out = artifacts / "local" / "eval" / PROFILE
    code = main(
        [
            "--profile",
            PROFILE,
            "--artifacts-dir",
            str(artifacts),
            "--out",
            str(out / "datasets-20260901-101500.html"),
        ]
    )

    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["datasets-20260901-101500.html"]


def test_the_page_is_refused_a_destination_outside_the_gitignored_half(artifacts, capsys):
    """This is the only rendered surface carrying confirmed answer keys in full, and what licenses
    that is where it lands. The committable half of the artifacts dir is one path component away,
    so the licence is enforced here rather than left to each caller to remember."""
    from render_golden_datasets import main

    code = main(
        [
            "--profile",
            PROFILE,
            "--artifacts-dir",
            str(artifacts),
            "--out",
            str(artifacts / PROFILE / "datasets.html"),
        ]
    )

    assert code == 2
    assert not (artifacts / PROFILE / "datasets.html").exists()
    assert "gitignored" in capsys.readouterr().err


# --- The back-channel, and the two things it may never do -----------------
#
# The queue lives in the browser, so what can be asserted here is the template's own markup: that
# each call site is there, and that the ones whose absence is the point are absent. It is not a
# substitute for a rendered DOM; it is the check that fails when the drawing is gutted — the same
# ceiling `test_render_golden_run.py` names.

QUEUEABLE = {
    "add-tag",
    "remove-tag",
    "set-match",
    "edit-question",
    "remove-item",
    "withdraw-confirmation",
}


def test_exactly_the_six_queueable_actions_exist():
    """The contract's list, and a seventh verb would be a change to what this page may ask for
    rather than an implementation detail — the parser on the other side accepts these six."""
    assert set(re.findall(r"queueOp\('([a-z-]+)'", TEMPLATE)) == QUEUEABLE


def test_the_page_never_offers_a_match_level_the_write_door_must_refuse():
    """`bounded` is the one level that is not a level on its own: an item is bounded together with
    the band it is held to, and this page carries no bounds and no way to type one. Offering it
    would queue a change refused every time, which is a control whose only use is a no-op."""
    levels = re.search(r"const MATCH_LEVELS = \[([^\]]*)\]", TEMPLATE).group(1)

    assert "bounded" not in levels
    assert set(re.findall(r"'([a-z]+)'", levels)) == {"exact", "values", "shape", "nonempty"}


def test_no_control_grants_confirmation():
    """The page may weaken a claim and may never strengthen one. Granting confirmation from a
    checkbox is forging ground truth and the easiest possible way to make a failing suite green:
    tick the box, and a statement nobody verified becomes what every future run is measured
    against. Withdrawing it needs no evidence, so that one is here."""
    assert "withdraw-confirmation" in TEMPLATE
    assert "sql_confirmed" not in TEMPLATE
    # …and the request is answered rather than ignored: an unconfirmed case says how confirming
    # actually happens, which is by running it and accepting the result.
    assert "run the case and accept the result" in TEMPLATE


def test_no_statement_is_editable_in_the_browser():
    """A SQL box beside a failing item is a one-click path to making the answer key agree with the
    bug. The key is drawn into a `pre`, and the page's one text area is the feedback block."""
    assert "el('pre', { text: item.sql })" in TEMPLATE
    assert TEMPLATE.count("<textarea") == 1
    assert 'id="modal-text"' in TEMPLATE


def test_the_feedback_block_names_its_profile_first_and_ends_with_done():
    """The model explorer's format, unchanged: the profile pins the target so the apply step never
    falls back to whichever profile happens to be active, and `done` closes the block. The ops ride
    under a bare `golden-ops:` header with the JSON array on the NEXT line."""
    header = TEMPLATE.index("lines.push('golden-ops:')")
    payload = TEMPLATE.index("lines.push(JSON.stringify(queue))")
    profile = TEMPLATE.index("lines.unshift('profile: '")
    done = TEMPLATE.index("lines.push('done')")

    assert header < payload < profile < done


def test_the_queue_is_folded_over_the_payload_and_never_persisted():
    """Nothing queued has happened yet: it is a proposal until Claude applies it through the write
    door. So the page folds the queue over the payload to draw a row, and stores it nowhere."""
    assert "function effItem(" in TEMPLATE
    assert "localStorage" not in TEMPLATE


def test_a_queued_row_can_be_undone_and_a_removal_still_shows_its_item():
    """Per-row Undo, because a queue you cannot walk back is one people stop using — and a queued
    removal keeps drawing the case, since agreeing to delete something you can no longer read is
    the append-only rule's whole objection."""
    assert "queue.splice(" in TEMPLATE and "Undo" in TEMPLATE
    assert "queued for removal" in TEMPLATE
