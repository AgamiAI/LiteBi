"""Unit tests for semantic_model/golden.py — the golden-dataset reader.

Guarded with importorskip: the v2 package needs pydantic (an optional dependency),
so these skip cleanly on a default install rather than erroring at import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from pydantic import ValidationError  # noqa: E402
from semantic_model import golden as g  # noqa: E402

PROFILE = "demo"

# Every fixture is a question over the shipped sample store database, so nothing here names a
# real dataset, table or question.
QUERY = "How many orders have been placed?"
SQL = "SELECT COUNT(*) AS order_count FROM orders"


def _expected(**kw):
    return {"sql": SQL, "sql_confirmed": True, **kw}


def _case(item_id="orders-count", **kw):
    return {"id": item_id, "query": QUERY, "expected": _expected(), **kw}


def _golden_dir(tmp_path):
    d = tmp_path / PROFILE / "golden_datasets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(directory, filename, doc):
    import yaml

    (directory / filename).write_text(yaml.safe_dump(doc), encoding="utf-8")


def _good_file(directory):
    """A second, well-formed dataset — the one the adversarial cases assert still reads."""
    _write(directory, "orders.yaml", {"test_cases": [_case()]})


# --- the happy paths ---


def test_minimal_file_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [_case()]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and [d.name for d in datasets] == ["orders"]
    item = datasets[0].test_cases[0]
    assert item.query == QUERY and item.expected.sql == SQL and item.expected.sql_confirmed


def test_all_optional_fields_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "revenue.yaml", {
        "description": "Revenue questions over the sample store.",
        "category": "finance",
        "user_context": "An analyst who only ever asks about paid orders.",
        "test_cases": [{
            "id": "revenue-total",
            "query": "What is our total revenue?",
            "expected": {
                "sql": "SELECT ROUND(SUM(total_amount), 2) AS revenue FROM orders",
                "sql_confirmed": True,
                "tables_used": ["orders"],
                "chart_type": "bar",
                "data_shape": "single_value",
                "validation_notes": "Checked against the sample seed.",
            },
            "match": "values",
            "must_filter": ["status = 'paid'"],
            "recorded": {"columns": ["revenue"], "rows": [[1234.56]], "at": "2026-01-01T00:00:00Z"},
            "tags": ["revenue", "smoke"],
            "confirmed_by": {"method": "reviewed by hand", "at": "2026-01-02T00:00:00Z"},
        }],
    })
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok
    ds = datasets[0]
    assert ds.description.startswith("Revenue questions")
    assert ds.category == "finance" and ds.user_context.endswith("paid orders.")
    item = ds.test_cases[0]
    assert item.expected.tables_used == ["orders"] and item.expected.chart_type == "bar"
    assert item.expected.data_shape == "single_value"
    assert item.expected.validation_notes == "Checked against the sample seed."
    assert item.match == "values" and item.must_filter == ["status = 'paid'"]
    assert item.recorded.columns == ["revenue"] and item.recorded.rows == [[1234.56]]
    assert item.recorded.at == "2026-01-01T00:00:00Z"
    assert item.tags == ["revenue", "smoke"]
    assert item.confirmed_by.method == "reviewed by hand"
    assert item.confirmed_by.at == "2026-01-02T00:00:00Z"


def test_match_defaults_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [_case()]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and datasets[0].test_cases[0].match == "exact"


def test_tags_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [
        _case("orders-count", tags=["smoke", "counts"]),
        _case("orders-count-untagged"),
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok
    assert [i.tags for i in datasets[0].test_cases] == [["smoke", "counts"], []]


def test_must_filter_reaches_item(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_case(must_filter=["status = 'paid'", "region = 'north'"])]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok
    assert datasets[0].test_cases[0].must_filter == ["status = 'paid'", "region = 'north'"]


def test_unconfirmed_item_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [
        {"id": "orders-open", "query": QUERY, "expected": {"sql_confirmed": False}},
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok
    item = datasets[0].test_cases[0]
    assert item.expected.sql_confirmed is False and item.expected.sql is None


def test_item_key_is_authored_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [_case("orders-count")]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and datasets[0].test_cases[0].item_key == "orders-count"


def test_name_is_filename_stem(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"description": "Something else entirely.", "test_cases": [_case()]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and datasets[0].name == "orders"


# --- the refusals ---


def test_missing_sql_confirmed_rejected():
    with pytest.raises(ValidationError):
        g.GoldenExpected(sql=SQL)


def test_confirmed_without_sql_rejected():
    with pytest.raises(ValidationError):
        g.GoldenItem(id="orders-count", query=QUERY, expected=g.GoldenExpected(sql_confirmed=True))


def test_declared_name_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "renamed.yaml", {"name": "something-else", "test_cases": [_case()]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_invalid_dataset"]
    assert res.findings[0].locator == "renamed.yaml"
    assert [ds.name for ds in datasets] == ["orders"]


def test_unknown_match_level_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_case("orders-count", match="approximately")]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_invalid_case"]
    assert res.findings[0].locator == "orders.yaml[orders-count]"
    assert datasets[0].test_cases == []


def test_near_miss_field_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_case("orders-count", must_filters=["status = 'paid'"])]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_invalid_case"]
    assert "must_filters" in res.findings[0].message
    assert datasets[0].test_cases == []


def test_bad_case_named_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "revenue.yaml", {"test_cases": [
        {"id": "revenue-broken", "query": "What is our total revenue?", "expected": {}},
        _case("revenue-total"),
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_invalid_case"]
    assert res.findings[0].locator == "revenue.yaml[revenue-broken]"
    assert [ds.name for ds in datasets] == ["orders", "revenue"]
    assert [i.item_key for i in datasets[1].test_cases] == ["revenue-total"]
    assert len(datasets[0].test_cases) == 1


# --- the empty cases ---


def test_empty_directory_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _golden_dir(tmp_path)
    datasets, res = g.load_golden_datasets(PROFILE)
    assert datasets == [] and res.findings == []


def test_missing_directory_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    datasets, res = g.load_golden_datasets(PROFILE)
    assert datasets == [] and res.findings == []


# --- the adversarial corpus: every one asserts the other file in the directory still reads ---


def test_empty_file_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    (d / "blank.yaml").write_text("", encoding="utf-8")
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and [ds.name for ds in datasets] == ["blank", "orders"]
    assert datasets[0].test_cases == [] and len(datasets[1].test_cases) == 1


def test_absent_test_cases_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "empty.yaml", {"description": "No cases written yet."})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and [ds.name for ds in datasets] == ["empty", "orders"]
    assert datasets[0].test_cases == [] and len(datasets[1].test_cases) == 1


def test_list_root_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "listy.yaml", [_case()])
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_unreadable_file"]
    assert res.findings[0].locator == "listy.yaml"
    assert [ds.name for ds in datasets] == ["orders"]


def test_unparseable_yaml_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    (d / "broken.yaml").write_text("test_cases: [\n  - id: 'unclosed\n", encoding="utf-8")
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_unreadable_file"]
    assert [ds.name for ds in datasets] == ["orders"]


def test_non_utf8_file_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    (d / "latin.yaml").write_bytes(b"description: caf\xe9\ntest_cases: []\n")
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_unreadable_file"]
    assert res.findings[0].locator == "latin.yaml"
    assert [ds.name for ds in datasets] == ["orders"]


def test_yml_extension_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "strays.yml", {"test_cases": [_case()]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_misnamed_file"]
    assert res.findings[0].locator == "strays.yml"
    assert [ds.name for ds in datasets] == ["orders"]


def test_duplicate_ids_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "revenue.yaml", {"test_cases": [
        _case("revenue-total"),
        _case("revenue-total", query="What is our total revenue?"),
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_duplicate_item_key"]
    assert res.findings[0].locator == "revenue.yaml[revenue-total]"
    assert [ds.name for ds in datasets] == ["orders", "revenue"]
    # The first case of the pair is the one kept.
    assert [i.query for i in datasets[1].test_cases] == [QUERY]


def test_null_case_others_still_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "revenue.yaml", {"test_cases": [None, _case("revenue-total")]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_invalid_case"]
    # A case too broken to carry an id is named by its position instead.
    assert res.findings[0].locator == "revenue.yaml[0]"
    assert [ds.name for ds in datasets] == ["orders", "revenue"]
    assert [i.item_key for i in datasets[1].test_cases] == ["revenue-total"]


# --- the relativity lint: a question that moves against an answer key that does not ---

RELATIVE_QUERY = "How many orders last quarter?"
STATIC_QUERY = "How many orders were placed in 2024?"
FROZEN_SQL = (
    "SELECT COUNT(*) AS order_count FROM orders "
    "WHERE placed_at >= '2024-01-01' AND placed_at < '2024-04-01'"
)


def _window_case(query, sql, item_id="orders-window"):
    """A case whose question and answer key are both under the lint's nose.

    `sql` may be None, which is the unconfirmed shape: no answer key to inspect.
    """
    expected = {"sql": sql, "sql_confirmed": True} if sql else {"sql_confirmed": False}
    return {"id": item_id, "query": query, "expected": expected}


def test_relativity_lint_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_window_case(RELATIVE_QUERY, FROZEN_SQL)]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_relative_question_frozen_sql"]
    assert [f.severity for f in res.findings] == ["error"]
    assert res.findings[0].locator == "orders.yaml[orders-window]"
    # Reported, never dropped: the item is broken, so the run has to see it as a dataset fault
    # rather than as a model that failed.
    assert [ds.name for ds in datasets] == ["orders"]
    assert [i.item_key for i in datasets[0].test_cases] == ["orders-window"]


def test_relativity_lint_clean_when_anchored(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    # Carries a frozen literal too, so only the anchor can be what keeps the lint quiet.
    anchored = (
        "SELECT COUNT(*) AS order_count FROM orders "
        "WHERE placed_at >= CURRENT_DATE - INTERVAL '90 days' AND placed_at >= '2020-01-01'"
    )
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_window_case(RELATIVE_QUERY, anchored)]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and len(datasets[0].test_cases) == 1


@pytest.mark.parametrize("predicate", [
    "placed_at >= NOW() - INTERVAL '90 days'",
    "placed_at >= date('now', '-90 days')",
    "placed_at >= DATEADD(day, -90, GETDATE())",
    "placed_at >= SYSDATE - 90",
])
def test_relativity_lint_reads_every_anchor_spelling(tmp_path, monkeypatch, predicate):
    """The lint is not Postgres-only: each dialect's word for "now" has to count as an anchor."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    sql = f"SELECT COUNT(*) FROM orders WHERE {predicate} AND placed_at >= '2020-01-01'"
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_window_case(RELATIVE_QUERY, sql)]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and len(datasets[0].test_cases) == 1


def test_relativity_lint_silent_on_a_static_question(tmp_path, monkeypatch):
    """Frozen literals are the normal shape of a golden case; only a moving question rots them."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_window_case(STATIC_QUERY, FROZEN_SQL)]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and len(datasets[0].test_cases) == 1


def test_relativity_lint_silent_without_an_answer_key(tmp_path, monkeypatch):
    """An unconfirmed case has no SQL to inspect, which is legal and not something to report."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml",
           {"test_cases": [_window_case(RELATIVE_QUERY, None)]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and datasets[0].test_cases[0].expected.sql is None


def test_relativity_lint_silent_without_a_date_literal(tmp_path, monkeypatch):
    """No frozen literal means nothing has been pinned to a day, so there is nothing to rot."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    _write(_golden_dir(tmp_path), "orders.yaml", {"test_cases": [
        _window_case(RELATIVE_QUERY, "SELECT COUNT(*) AS order_count FROM orders"),
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.ok and len(datasets[0].test_cases) == 1


def test_relativity_lint_names_the_file_and_the_case(tmp_path, monkeypatch):
    """The message, not just the locator, has to be enough to find the case in the tree."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    d = _golden_dir(tmp_path)
    _good_file(d)
    _write(d, "revenue.yaml", {"test_cases": [
        _window_case("What did we bill in the past 30 days?", FROZEN_SQL, "revenue-recent"),
        _case("revenue-total"),
    ]})
    datasets, res = g.load_golden_datasets(PROFILE)
    assert [f.code for f in res.findings] == ["golden_relative_question_frozen_sql"]
    assert "revenue.yaml" in res.findings[0].message and "revenue-recent" in res.findings[0].message
    # The lint reports; the file and every case in it, flagged or not, still read.
    assert [ds.name for ds in datasets] == ["orders", "revenue"]
    assert [i.item_key for i in datasets[1].test_cases] == ["revenue-recent", "revenue-total"]


# --- the canonical authoring reference ---

SHAPE_DOC = REPO_ROOT / "plugins" / "agami" / "shared" / "golden-dataset-shape.md"
FORMAT_SPEC = REPO_ROOT / "docs" / "format-spec.md"


def _first_yaml_fence(text):
    """The doc's first ```yaml block, which is the complete example dataset. Only the first is
    parsed: a later fence would be an excerpt, not a file anyone could copy whole."""
    body = text.split("```yaml", 1)[1]
    return body.split("```", 1)[0]


def test_shape_doc_example_parses(tmp_path, monkeypatch):
    """The reference has to be a file the real reader accepts, or it teaches a shape that is
    refused the moment someone copies it."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    example = _first_yaml_fence(SHAPE_DOC.read_text(encoding="utf-8"))
    (_golden_dir(tmp_path) / "orders.yaml").write_text(example, encoding="utf-8")
    datasets, res = g.load_golden_datasets(PROFILE)
    assert res.findings == [] and res.ok
    assert [ds.name for ds in datasets] == ["orders"]
    # One minimal case and one exercising every optional field.
    assert len(datasets[0].test_cases) >= 2
    assert all(i.query and i.expected for i in datasets[0].test_cases)


def test_shape_doc_carries_hard_rule():
    """The rule that binds hardest here: a golden dataset is the questions and the answer key in
    one file, so globbing a sibling profile reads another tenant's data."""
    text = SHAPE_DOC.read_text(encoding="utf-8")
    assert "> **HARD RULE — never read another profile to learn a shape.**" in text
    assert "tenant-data leak" in text


def test_format_spec_lists_golden_datasets():
    lines = FORMAT_SPEC.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## `<artifacts_dir>/` — sharable"))
    end = next(i for i, ln in enumerate(lines[start + 1:], start + 1) if ln.startswith("## "))
    rows = [ln for ln in lines[start:end] if "golden_datasets/<name>.yaml" in ln]
    assert len(rows) == 1 and "User-authored" in rows[0]
