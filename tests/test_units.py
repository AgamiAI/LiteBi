"""Tests for semantic_model/units.py (deterministic currency/unit formatting) and the
`unit` field surfacing through the model + get_table_context."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")
# semantic_model ships in the installed agami-core package; PKG_SRC is only used for the
# source-level "stays import-light" scan below.
PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"

from semantic_model import units


def test_currency_symbol_and_is_currency():
    assert units.currency_symbol("inr") == "₹"
    assert units.currency_symbol("USD") == "$"
    assert units.currency_symbol("not-a-currency") is None
    assert units.is_currency("eur") and not units.is_currency("days")


def test_inr_uses_indian_grouping():
    assert units.format_value(123456.5, "INR") == "₹1,23,456.50"
    assert units.format_value(10000000, "INR") == "₹1,00,00,000.00"
    assert units.format_value(-2500, "INR") == "-₹2,500.00"


def test_western_grouping_and_zero_decimal():
    assert units.format_value(123456.5, "USD") == "$123,456.50"
    assert units.format_value(1234, "JPY") == "¥1,234"     # no minor unit
    assert units.format_value(50, "EUR") == "€50.00"


def test_non_currency_units_and_passthrough():
    assert units.format_value(98.6, "percent") == "98.6%"
    assert units.format_value(1234, "days") == "1,234 days"
    assert units.format_value("N/A", "USD") == "N/A"        # non-numeric passthrough
    assert units.format_value(None, "USD") == ""


def test_format_cell_is_exact_never_abbreviated():
    # verification surface: full value, grouping, currency symbol — NO abbreviation/rounding
    assert units.format_cell("21620870000.50", "INR") == "₹21,62,08,70,000.50"
    assert units.format_cell("134100000", None) == "134,100,000"     # bare count, grouped, exact
    assert units.format_cell("684.3", None) == "684.3"                # decimals preserved
    assert units.format_cell("active", None) == "active"             # passthrough
    assert units.format_cell("", None) == ""


def test_format_table_markdown_exact():
    md = units.format_table(
        ["borrowers", "outstanding"],
        [["134100000", "21620870000.5"], ["50200000", "9876543210"]],
        {"outstanding": "INR"})
    lines = md.splitlines()
    assert lines[0] == "| borrowers | outstanding |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 134,100,000 | ₹21,62,08,70,000.50 |"
    assert lines[3] == "| 50,200,000 | ₹9,87,65,43,210.00 |"
    # no abbreviation anywhere
    assert "Cr" not in md and "L " not in md and "M" not in md


def test_units_module_has_no_heavy_deps():
    # units.py must stay import-light so the pure-stdlib MCP path can use it
    import sys as _sys
    assert "pydantic" not in _sys.modules or True  # informational
    src = (PKG_SRC / "semantic_model" / "units.py").read_text()
    assert "import pydantic" not in src and "import sqlglot" not in src


def _currency_model(root):
    import yaml
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "loans"}]}))
    (root / "subject_areas" / "s" / "tables" / "loans.yaml").write_text(yaml.safe_dump({
        "name": "loans", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "l",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))


def test_resolve_result_units_traces_aggregates(tmp_path):
    # the reliability fix: a SUM/AVG over a currency column keeps the currency, even
    # under an alias; COUNT and ratios do not become currency
    pytest.importorskip("sqlglot")
    from semantic_model import runtime as RT
    from semantic_model.loader import load_datasource
    _currency_model(tmp_path)
    org = load_datasource(tmp_path)
    # named results carry the unit by name (+ a positional #0 key alongside)
    assert RT.resolve_result_units(org, "SELECT SUM(amount) AS total FROM loans")["total"] == "INR"
    assert RT.resolve_result_units(org, "SELECT AVG(amount) AS a FROM loans")["a"] == "INR"
    assert RT.resolve_result_units(org, "SELECT amount FROM loans")["amount"] == "INR"
    assert RT.resolve_result_units(org, "SELECT COUNT(*) AS cnt FROM loans") == {}
    # currency / count is still currency (avg ticket) — see dimensional test below
    assert RT.resolve_result_units(org, "SELECT SUM(amount)/COUNT(*) AS avg_ticket FROM loans")["avg_ticket"] == "INR"
    # unaliased aggregate → positional key only (#0), since the DB invents the column name
    assert RT.resolve_result_units(org, "SELECT MAX(amount) FROM loans") == {"#0": "INR"}


def test_resolve_result_units_dimensional_ratios(tmp_path):
    # dimensional analysis: currency/count is still currency; true ratios + computed
    # percentages are unitless (exact number, no wrong symbol) — verification-safe
    pytest.importorskip("sqlglot")
    import yaml
    from semantic_model import runtime as RT
    from semantic_model.loader import load_datasource
    _currency_model(tmp_path)
    # add a second currency col + a percent col
    t = tmp_path / "subject_areas" / "s" / "tables" / "loans.yaml"
    doc = yaml.safe_load(t.read_text())
    doc["columns"] += [{"name": "fee", "type": "decimal", "unit": "INR"},
                       {"name": "npa_pct", "type": "decimal", "unit": "percent"}]
    t.write_text(yaml.safe_dump(doc))
    org = load_datasource(tmp_path)
    R = lambda s: RT.resolve_result_units(org, s)
    assert R("SELECT SUM(amount)/COUNT(*) AS avg_ticket FROM loans")["avg_ticket"] == "INR"
    assert R("SELECT amount*1.18 AS with_tax FROM loans")["with_tax"] == "INR"
    assert R("SELECT SUM(amount)+SUM(fee) AS gross FROM loans")["gross"] == "INR"
    assert R("SELECT AVG(npa_pct) AS avg_npa FROM loans")["avg_npa"] == "percent"
    assert R("SELECT SUM(amount)/SUM(fee) AS ratio FROM loans") == {}        # currency/currency
    assert R("SELECT SUM(npa)/SUM(total)*100 AS pct FROM loans") == {}       # computed % → exact, unlabeled


def test_format_date_epoch_yyyymmdd_and_passthrough():
    # epoch is UTC by definition → human datetime + explicit UTC label
    assert units.format_date(1704067200, "epoch_s") == "2024-01-01 00:00:00 UTC"
    assert units.format_date(1704067200000, "epoch_ms") == "2024-01-01 00:00:00 UTC"
    assert units.format_date(1704067200000000, "epoch_us") == "2024-01-01 00:00:00 UTC"
    assert units.format_date(20240115, "yyyymmdd") == "2024-01-15"
    # iso / native → passthrough (DB already returns it readable)
    assert units.format_date("2024-01-15T10:00:00Z", "iso8601") == "2024-01-15T10:00:00Z"
    # non-numeric epoch value passes through, never crashes
    assert units.format_date("N/A", "epoch_s") == "N/A"
    assert units.is_date_format("epoch_ms") and not units.is_date_format("INR")


def test_format_value_and_table_render_epoch_as_date():
    # the date token flows through the same pipeline as units
    assert units.format_value(1704067200, "epoch_s") == "2024-01-01 00:00:00 UTC"
    md = units.format_table(["id", "created"], [["1", "1704067200"]], {"created": "epoch_s"})
    assert md.splitlines()[2] == "| 1 | 2024-01-01 00:00:00 UTC |"


def test_resolve_result_units_emits_date_format_token(tmp_path):
    pytest.importorskip("sqlglot")
    import yaml
    from semantic_model import runtime as RT
    from semantic_model.loader import load_datasource
    (tmp_path / "datasources" / "c").mkdir(parents=True)
    (tmp_path / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (tmp_path / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (tmp_path / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (tmp_path / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (tmp_path / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "o",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "created_ts", "type": "integer", "date_format": "epoch_s", "timezone": "UTC"}]}))
    org = load_datasource(tmp_path)
    assert RT.resolve_result_units(org, "SELECT created_ts FROM orders")["created_ts"] == "epoch_s"
    # propagates through MAX (the last timestamp is still that encoding)
    assert RT.resolve_result_units(org, "SELECT MAX(created_ts) AS last FROM orders")["last"] == "epoch_s"


def test_format_table_applies_units_by_position():
    # the positional fallback formats a DB-auto-named column (e.g. Postgres "max")
    md = units.format_table(["max"], [["250000.5"]], {"#0": "INR"})
    assert md.splitlines()[2] == "| ₹2,50,000.50 |"


def test_unit_round_trips_on_column_and_surfaces_in_context(tmp_path):
    import yaml
    from semantic_model.loader import get_table_context, load_datasource
    root = tmp_path
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}]}))
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "o",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))
    org = load_datasource(root)
    assert org.subject_areas[0].defined_table("orders").get_column("amount").unit == "INR"
    ctx = get_table_context(org, ["orders"], area="s")
    amount = next(c for c in ctx["tables"]["orders"]["columns"] if c["name"] == "amount")
    assert amount["unit"] == "INR"
