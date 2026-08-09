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
                            "bindings": {"PostgreSQL": f"SUM({mn})"},
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


# --- the contract, checked against the REAL payload ------------------------------------------
#
# `tests/test_contracts.py` validates hand-written samples. A sample is written to agree with the
# contract, so that pair can never disagree and never caught anything: `SubjectAreaSummary.tables`
# was declared `list[str]` while the tool sent `[{name, description}]`, and
# `CrossAreaRelationship.for_questions_about` was declared `str | None` while the model's field is
# a `list[str]` — both shipped, both invisible. These tests point the contract at what
# `tool_get_datasource_schema` actually emits, in each mode, which is the only version of this
# check that can fail.


@pytest.mark.parametrize("mode", ["index", "summary", "full"])
def test_the_real_payload_validates_against_the_published_contract(monkeypatch, tmp_path, mode):
    pytest.importorskip("pydantic")
    from contracts import DatasourceSchemaResult

    prof = _run(monkeypatch, tmp_path, n_areas=2, tables_per_area=2,
                metrics={0: ["revenue"], 1: ["churn_rate"]})
    head = _schema(prof, mode=mode)
    parsed = DatasourceSchemaResult.model_validate(head)
    # Lossless: every key the tool sent survives the contract. `extra="allow"` would let an
    # undeclared key ride along silently, so compare the round-trip to the payload itself.
    assert parsed.model_dump(by_alias=True, exclude_unset=True) == head


def test_the_area_summary_names_its_tables_rather_than_listing_bare_strings(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=2, tables_per_area=2)
    head = _schema(prof, mode="summary")
    tables = head["subject_areas"][0]["tables"]
    assert [t["name"] for t in tables] == ["t0_0", "t0_1"]
    assert all(t["description"] for t in tables), "the description is why these are objects"


def test_the_index_mode_area_carries_a_count_not_a_table_list(monkeypatch, tmp_path):
    prof = _run(monkeypatch, tmp_path, n_areas=60, tables_per_area=2)  # 60 areas -> index
    head = _schema(prof, mode="auto")
    assert head["mode"] == "index"
    area = head["subject_areas"][0]
    assert area["table_count"] == 2 and "tables" not in area


def test_a_returned_metric_carries_the_binding_the_instructions_tell_you_to_reuse(
    monkeypatch, tmp_path
):
    """The server instructions AND this tool's own description both say to use a metric's
    `calculation`/`bindings` VERBATIM — and `bindings` was not among the keys the payload sent, on
    the very call those instructions describe. The agent was told in capitals to reuse a field it
    never received, so it hand-rolled from the prose `calculation` instead. That also cost the
    receipt a true match: hand-rolled SQL does not reduce to the declared binding, so the output
    column read `unmatched` rather than naming the metric it computes.
    """
    prof = _run(monkeypatch, tmp_path, n_areas=2, metrics={0: ["revenue"]})
    head = _schema(prof, mode="full")
    revenue = next(m for m in head["metrics"] if m["name"] == "revenue")
    assert revenue["binding"] == "SUM(revenue)"


def test_only_this_deployments_engine_binding_is_sent(monkeypatch, tmp_path):
    """ONE binding, not the per-dialect dict. On an unscoped `full` call the metric block is every
    metric the model declares, so shipping every dialect would multiply the largest block in the
    payload for a client that writes for exactly one warehouse."""
    prof = _run(monkeypatch, tmp_path, n_areas=2, metrics={0: ["revenue"]})
    head = _schema(prof, mode="full")
    revenue = next(m for m in head["metrics"] if m["name"] == "revenue")
    assert "bindings" not in revenue, "the whole per-dialect dict must not ship"
    assert isinstance(revenue["binding"], str)


def test_a_metric_with_no_binding_for_this_engine_omits_the_key(monkeypatch, tmp_path):
    """Absent, not null. A metric declaring nothing for this deployment's engine has no `binding`
    key at all — `"binding": null` would read as "declared as nothing" rather than "not declared
    here", and the contract makes the field optional precisely so absence can say that."""
    import yaml

    prof = _run(monkeypatch, tmp_path, n_areas=2, metrics={0: ["revenue"]})
    mfile = tmp_path / "art" / "acme" / "subject_areas" / "area0" / "metrics" / "revenue.yaml"
    doc = yaml.safe_load(mfile.read_text())
    doc["bindings"] = {"Snowflake": "SUM(revenue)"}  # another engine only
    mfile.write_text(yaml.safe_dump(doc))

    head = _schema(prof, mode="full")
    revenue = next(m for m in head["metrics"] if m["name"] == "revenue")
    assert "binding" not in revenue
    assert revenue["calculation"], "the rest of the metric still comes through"


def test_an_unresolvable_engine_sends_no_binding_rather_than_the_wrong_one(monkeypatch, tmp_path):
    """Two connections declaring different engines means we do not know which dialect the caller
    writes for. Same rule `runtime._storage_type_of` applies — not knowing yields nothing rather
    than a guess, because a Snowflake binding handed to a Postgres caller is worse than none."""
    import yaml

    prof = _run(monkeypatch, tmp_path, n_areas=2, metrics={0: ["revenue"]})
    root = tmp_path / "art" / "acme"
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    doc["storage_connections"] = [
        {"name": "c", "storage_type": "PostgreSQL"},
        {"name": "d", "storage_type": "Snowflake"},
    ]
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))

    head = _schema(prof, mode="full")
    revenue = next(m for m in head["metrics"] if m["name"] == "revenue")
    assert "binding" not in revenue
