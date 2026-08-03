"""Phase A — deterministic render piping.

csv_to_sections.py builds the render sections from the result CSV (numbers piped,
not LLM-transcribed); `sm receipt` (cli.cmd_receipt) assembles the trust receipt
from the SQL. These guard that the numbers a chart/table shows come from the data,
and the provenance comes from parsing the SQL — not from the model re-typing.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import csv_to_sections as C  # noqa: E402


def _csv(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# csv_to_sections — numbers come from the CSV
# ---------------------------------------------------------------------------

def test_table_rows_formatted_datasets_raw(tmp_path):
    csvf = _csv(tmp_path / "r.csv", "customer,total_spend\nAvery,121560.94\nBlake,58425.20\n")
    sec = C._build_section(
        {"title": "T", "csv_file": str(csvf), "units": {"total_spend": "USD"}, "value_cols": [1]}, 0, [])
    # table cells formatted EXACTLY via units (currency + grouping), no abbreviation
    assert sec["table_rows"] == [["Avery", "$121,560.94"], ["Blake", "$58,425.20"]]
    # datasets carry RAW numbers parsed from the CSV (not the formatted string)
    assert sec["datasets"][0]["data"] == [121560.94, 58425.2]
    assert sec["labels"] == ["Avery", "Blake"]
    assert sec["unit"] == "USD"


def test_raw_numbers_are_exact_no_drift(tmp_path):
    """The whole point: a big/awkward value reaches datasets EXACTLY (the kind a
    hand-transcription would drift on)."""
    csvf = _csv(tmp_path / "r.csv", "name,rev\nA,2162087.5\nB,1490000\n")
    sec = C._build_section({"title": "T", "csv_file": str(csvf), "units": {"rev": "INR"}, "value_cols": [1]}, 0, [])
    assert sec["datasets"][0]["data"] == [2162087.5, 1490000]
    assert sec["table_rows"][0][1].startswith("₹")  # INR symbol, Indian grouping


def test_non_numeric_value_col_is_anomaly_not_a_crash(tmp_path):
    csvf = _csv(tmp_path / "r.csv", "name,status\nA,active\nB,paused\n")
    anom: list = []
    sec = C._build_section({"title": "T", "csv_file": str(csvf), "value_cols": [1]}, 0, anom)
    assert sec["datasets"] == []  # status isn't chartable
    assert any(a["kind"] == "non_numeric_value_col" for a in anom)
    assert sec["table_rows"] == [["A", "active"], ["B", "paused"]]  # table still rendered


def test_sql_read_verbatim_from_file(tmp_path):
    csvf = _csv(tmp_path / "r.csv", "a,b\n1,2\n")
    sqlf = _csv(tmp_path / "q.sql", "SELECT a, b FROM t  -- kept exactly\n")
    sec = C._build_section({"title": "T", "csv_file": str(csvf), "sql_file": str(sqlf)}, 0, [])
    assert sec["sql"] == "SELECT a, b FROM t  -- kept exactly"


def test_default_value_cols_are_all_non_label(tmp_path):
    csvf = _csv(tmp_path / "r.csv", "month,orders,revenue\n2026-01,10,100\n2026-02,20,250\n")
    sec = C._build_section({"title": "T", "csv_file": str(csvf), "label_col": 0,
                            "units": {"revenue": "USD"}}, 0, [])
    labels = {d["label"] for d in sec["datasets"]}
    assert labels == {"orders", "revenue"}
    assert sec.get("unit") is None  # mixed units across value cols → no section unit key


def test_main_multi_section_writes_array(tmp_path):
    a = _csv(tmp_path / "a.csv", "k,v\nx,1\n")
    b = _csv(tmp_path / "b.csv", "k,v\ny,2\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps([
        {"title": "A", "csv_file": str(a), "value_cols": [1]},
        {"title": "B", "csv_file": str(b), "value_cols": [1]},
    ]), encoding="utf-8")
    out = tmp_path / "sections.json"
    rc = C.main(["--spec", str(spec), "--out", str(out)])
    assert rc == 0
    sections = json.loads(out.read_text())
    assert [s["title"] for s in sections] == ["A", "B"]
    assert sections[0]["datasets"][0]["data"] == [1]


# ---------------------------------------------------------------------------
# sm receipt — provenance comes from parsing the SQL, not the LLM
# ---------------------------------------------------------------------------

def test_receipt_from_sql(tmp_path, capsys):
    from semantic_model import cli
    from semantic_model import introspect as I

    from catalog_helpers import col, make_catalog_runner

    runner = make_catalog_runner(
        tables=["customers", "orders"],
        columns={"customers": [col("id", "integer", nullable=False), col("email", "varchar")],
                 "orders": [col("id", "integer", nullable=False), col("customer_id", "integer")]},
        fks=[{"from_table": "orders", "from_column": "customer_id",
              "to_table": "customers", "to_column": "id"}])
    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path)

    args = types.SimpleNamespace(
        root=str(tmp_path / "shop"), sql_file=None, freshness=None,
        sql="SELECT c.id, COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY c.id")
    cli.cmd_receipt(args)
    receipt = json.loads(capsys.readouterr().out)
    # The five sections are top-level keys beside the version pin — the same shape the MCP server
    # emits, so `sm receipt` and the server describe one statement one way.
    assert {t["qname"] for t in receipt["tables"]["items"]} == {"public.customers", "public.orders"}
    assert any("customers" in r["from_to"] for r in receipt["joins"]["items"])
