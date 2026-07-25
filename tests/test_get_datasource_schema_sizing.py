"""Smart get_datasource_schema — adaptive sizing by subject-area + char budget + metric ranking.

The server (not the client) decides payload verbosity so one tool call fits the context window:
`mode=auto` picks full/summary/index by subject-area count; a ~60K-char budget downgrades even a
forced `full`; a `query` lexically ranks metrics. All decidable on a synthetic model here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

import tools  # noqa: E402


def _write_model(
    root: Path,
    n_areas: int,
    tables_per_area: int = 1,
    metrics: dict[str, list[str]] | None = None,
    wide: bool = False,
    big_rows: bool = False,
) -> None:
    """Write a synthetic on-disk model: `n_areas` subject areas, each with `tables_per_area`
    tables. `metrics` maps area-index → metric names. `wide` pads columns to inflate full-mode
    size; `big_rows` marks tables ≥1M rows (for large_tables)."""
    import yaml

    (root / "datasources" / "c").mkdir(parents=True, exist_ok=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    area_paths: list[str] = []
    for i in range(n_areas):
        a = f"area{i}"
        adir = root / "subject_areas" / a
        (adir / "tables").mkdir(parents=True)
        refs = []
        for j in range(tables_per_area):
            tname = f"t{i}_{j}"
            refs.append({"storage_connection": "c", "schema": "public", "table": tname})
            cols = [{"name": "id", "type": "integer", "primary_key": True}]
            n_cols = 10 if wide else 1
            for k in range(n_cols):
                cols.append(
                    {
                        "name": f"col_{k}",
                        "type": "decimal",
                        "description": ("d" * 300) if wide else "x",
                    }
                )
            tdoc = {
                "name": tname,
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": f"table {tname} description",
                "columns": cols,
            }
            if big_rows:
                tdoc["performance_hints"] = {"estimated_row_count": 2_000_000}
            (adir / "tables" / f"{tname}.yaml").write_text(yaml.safe_dump(tdoc))
        (adir / "subject_area.yaml").write_text(
            yaml.safe_dump({"name": a, "description": f"area {a} description", "tables": refs})
        )
        names = (metrics or {}).get(i, [])
        if names:
            (adir / "metrics").mkdir()
            for mn in names:
                (adir / "metrics" / f"{mn}.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "name": mn,
                            "calculation": "sum of amount",
                            "confidence": "proposed",
                            "review_state": "unreviewed",
                            "description": mn.replace("_", " "),
                        }
                    )
                )
        area_paths.append(f"subject_areas/{a}")
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": area_paths,
            }
        )
    )


def _schema(profile: str, **args) -> dict:
    """Call the tool and parse the leading JSON head (domain-context text may follow it)."""
    out = tools.tool_get_datasource_schema({"datasource": profile, **args})
    return json.JSONDecoder().raw_decode(out)[0]


def _run(monkeypatch, tmp_path, **build):
    art = tmp_path / "art"
    _write_model(art / "acme", **build)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return "acme"


def test_auto_full_for_small_model(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=3)
    head = _schema(prof, mode="auto")
    assert head["mode"] == "full"
    assert head["requested_mode"] == "auto"
    assert "metric_index" in head and "large_tables" in head  # always present


def test_auto_summary_for_medium_model(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=20)  # 13..50 areas -> summary
    head = _schema(prof, mode="auto")
    assert head["mode"] == "summary"
    # summary carries the table list (name + one-line description), not full columns
    assert head["subject_areas"][0]["tables"][0]["name"]
    assert "tables" not in head  # the full per-table context blob is omitted


def test_auto_index_for_large_model(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=60)  # >50 areas -> index
    head = _schema(prof, mode="auto")
    assert head["mode"] == "index"
    assert head["subject_areas"][0] == {
        "name": "area0",
        "description": "area area0 description",
        "table_count": 1,
    }


def test_char_budget_downgrades_forced_full(monkeypatch, tmp_path):
    # 30 wide areas: full (all tables' get_table_context) blows the 60K budget, so even an
    # explicit mode=full must downgrade and flag truncated.
    prof = _run(monkeypatch, tmp_path, n_areas=30, tables_per_area=2, wide=True)
    head = _schema(prof, mode="full")
    assert head["requested_mode"] == "full"
    assert head["mode"] != "full" and head.get("truncated") is True
    assert len(json.dumps(head)) <= tools._SCHEMA_CHAR_BUDGET


def test_query_ranks_and_limits_metrics(monkeypatch, tmp_path):
    prof = _run(
        monkeypatch,
        tmp_path,
        n_areas=2,
        metrics={0: ["revenue", "gross_revenue"], 1: ["customer_count", "churn_rate"]},
    )
    head = _schema(prof, mode="index", query="revenue")
    returned = {m["name"] for m in head["metrics"]}
    assert "revenue" in returned and "gross_revenue" in returned  # matched in full
    assert "customer_count" not in returned and "churn_rate" not in returned  # filtered out
    # but the never-hide net lists every metric by name
    assert set(head["metric_index"]) == {"revenue", "gross_revenue", "customer_count", "churn_rate"}


def test_large_tables_flagged_in_every_mode(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=60, big_rows=True)  # index mode
    head = _schema(prof, mode="auto")
    assert head["mode"] == "index"
    assert head["large_tables"].get("t0_0") == 2_000_000


def test_dataset_names_full_detail_no_downgrade(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=3, wide=True)
    head = _schema(prof, dataset_names=["t0_0"])
    assert head["mode"] == "full" and "truncated" not in head  # explicit scope respected
    assert "t0_0" in head["tables"]
