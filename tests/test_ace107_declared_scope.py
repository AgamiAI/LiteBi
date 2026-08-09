"""ACE-107 — `get_datasource_schema` answers within the scope the caller DECLARED.

Two independent defects, one cause: the tool did not treat a declared scope as meaning anything.

  * It **discarded** what a table-scoped call had already resolved. `_table_contexts` asked
    `get_table_context` for relationships and metrics and then kept only `ctx["tables"]`, so the
    call whose whole purpose is per-table detail returned columns and no way to join them — while
    the tool's own description promised both. A client could only write the join if a human had
    put it in the prose narrative.
  * It sent the **whole metric catalogue on every call**, under a never-hide guarantee written
    for the verbosity ladder (`full -> summary -> index` under a char budget), where the client
    asked for everything and silently got less. A scoped call is the opposite situation.

The guarantee is now scope-relative: **within the scope you declared, nothing is hidden** — the
scope's own metrics, plus the cross-area bucket. Scope is DECLARED (`area`, `dataset_names`) and
never inferred: `query` ranks and `metric_names` selects detail, and narrowing on either would
silently deprive a caller who never said "I am only working here".

The model this was found on has **zero** metrics with an empty `source_tables` and **zero**
cross-area metrics naming a scoped table, so neither edge is reachable by testing against a real
model. Both are built synthetically here — that is the whole reason this file has its own
fixture rather than borrowing the sizing suite's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import tools  # noqa: E402

# Synthetic throughout — agami-core is public (spec `## Data sensitivity`).
SALES, PEOPLE = "sales", "people"


def _write_model(root: Path) -> None:
    """Two areas, and one metric per interesting shape.

    | metric              | area   | source_tables | why it exists                                 |
    |---------------------|--------|---------------|-----------------------------------------------|
    | `order_count`       | sales  | [orders]      | plainly in scope for `orders`                 |
    | `return_rate`       | sales  | [returns]     | same area, DIFFERENT table — must be excluded |
    | `sales_health`      | sales  | []            | undeclared scope cannot exclude               |
    | `headcount`         | people | [users]       | another area entirely                         |
    | `revenue_per_user`  | cross  | [orders]      | cross-area, names a scoped table              |
    | `company_wide`      | cross  | []            | cross-area, names nothing                     |
    """
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )

    def _area(name: str, tables: list[str], metrics: list[dict]) -> None:
        adir = root / "subject_areas" / name
        (adir / "tables").mkdir(parents=True)
        (adir / "metrics").mkdir(parents=True)
        for t in tables:
            (adir / "tables" / f"{t}.yaml").write_text(yaml.safe_dump({
                "name": t, "schema": "public", "storage_connection": "c", "grain": ["id"],
                "description": f"{t} table",
                "columns": [{"name": "id", "type": "integer", "primary_key": True},
                            {"name": "amount", "type": "decimal"}]}))
        for m in metrics:
            (adir / "metrics" / f"{m['name']}.yaml").write_text(yaml.safe_dump(m))
        (adir / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": name, "description": f"{name} area",
            "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                       for t in tables]}))

    def _metric(name: str, source_tables: list[str]) -> dict:
        return {"name": name, "calculation": f"the {name}", "description": f"the {name}",
                "bindings": {"PostgreSQL": f"SUM({name})"}, "source_tables": source_tables,
                "confidence": "proposed", "review_state": "unreviewed"}

    _area(SALES, ["orders", "returns"],
          [_metric("order_count", ["orders"]),
           _metric("return_rate", ["returns"]),
           _metric("sales_health", [])])
    _area(PEOPLE, ["users"], [_metric("headcount", ["users"])])

    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": [f"subject_areas/{SALES}", f"subject_areas/{PEOPLE}"],
        "cross_subject_area_metrics": [_metric("revenue_per_user", ["orders"]),
                                       _metric("company_wide", [])],
        "cross_subject_area_relationships": [{
            "from_table": "orders", "to_table": "users", "from_column": "id", "to_column": "id",
            "join_type": "LEFT", "relationship": "many_to_one", "confidence": "proposed",
            "review_state": "unreviewed",
            "from_subject_area": SALES, "to_subject_area": PEOPLE}]}))


ALL_METRICS = {"order_count", "return_rate", "sales_health", "headcount",
               "revenue_per_user", "company_wide"}
CROSS_AREA = {"revenue_per_user", "company_wide"}


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _write_model(art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return "acme"


def _head(profile: str, **args) -> dict:
    """The JSON head of a call (the appended markdown context follows it)."""
    out = tools.tool_get_datasource_schema({"datasource": profile, **args})
    return json.JSONDecoder().raw_decode(out)[0]


def _raw(profile: str, **args) -> str:
    return tools.tool_get_datasource_schema({"datasource": profile, **args})


# --- A4: never-hide, per tier, as SET EQUALITY --------------------------------------------------
#
# Set equality and not a count, deliberately: a count passes while the wrong metrics are in scope,
# and "the wrong metrics" is the failure this guarantee exists to prevent.


def test_datasource_scope_hides_nothing(profile):
    """Nothing declared — the tier the original never-hide net was written for. Unchanged."""
    assert set(_head(profile)["metric_index"]) == ALL_METRICS


def test_area_scope_is_that_area_plus_the_cross_area_bucket(profile):
    """A metric belonging to no single area is out of scope nowhere.

    `get_prompt_examples` already answers this same question the same way — it scopes as
    `area = ? OR area IS NULL`, documented as "cross-area examples still included". Excluding a
    cross-area metric that applies would be the hide this rule forbids.
    """
    got = set(_head(profile, area=SALES)["metric_index"])
    assert got == {"order_count", "return_rate", "sales_health"} | CROSS_AREA
    assert "headcount" not in got, "another area's metric is not in scope"


def test_table_scope_is_the_tables_metrics_plus_the_cross_area_bucket(profile):
    got = set(_head(profile, dataset_names=["orders"])["metric_index"])
    assert got == {"order_count", "sales_health"} | CROSS_AREA
    assert "return_rate" not in got, "same area, different table — not in scope"
    assert "headcount" not in got


# --- A5: the two shapes no real model in hand contains ------------------------------------------


def test_a_metric_declaring_no_source_tables_is_in_scope_for_its_areas_tables(profile):
    """An UNDECLARED scope cannot be used to exclude.

    `loader._metrics_for` gets this right and the rule must preserve it: a metric that names no
    source tables might apply to any table in its area, and dropping it would be a hide justified
    by the absence of a declaration.
    """
    assert "sales_health" in _head(profile, dataset_names=["orders"])["metric_index"]
    assert "sales_health" in _head(profile, dataset_names=["returns"])["metric_index"]


def test_a_cross_area_metric_naming_a_scoped_table_is_in_scope(profile):
    """The hole in the obvious helper. `loader._metrics_for` never walks
    `cross_subject_area_metrics` at all, so reusing it as a never-hide filter would silently drop
    this metric — a real hide, and the exact outcome the guarantee exists to prevent."""
    assert "revenue_per_user" in _head(profile, dataset_names=["orders"])["metric_index"]


def test_a_schema_qualified_source_table_still_matches(profile, tmp_path, monkeypatch):
    """Match through `bare_name`, never a bare case-fold.

    A metric declaring `public.orders` must be in scope for a caller naming `orders`. The
    equivalent fold mismatch in the receipt's metric guard shipped and had to be corrected in
    review, so this is a known failure mode in this codebase rather than a hypothetical.
    """
    import yaml

    mfile = tmp_path / "art" / "acme" / "subject_areas" / SALES / "metrics" / "order_count.yaml"
    doc = yaml.safe_load(mfile.read_text())
    doc["source_tables"] = ["public.orders"]
    mfile.write_text(yaml.safe_dump(doc))
    assert "order_count" in _head(profile, dataset_names=["orders"])["metric_index"]


# --- A14: scope is DECLARED, never inferred -----------------------------------------------------


def test_query_ranks_but_does_not_narrow(profile):
    """`query` is a ranking hint. Narrowing on it would deprive a caller who never declared a
    scope — and worse, it is a fuzzy lexical match, so a poor match would hide exactly the metric
    the caller could not name."""
    assert set(_head(profile, query="order count")["metric_index"]) == ALL_METRICS


def test_metric_names_selects_detail_but_does_not_narrow(profile):
    """Naming a metric asks for its detail. It does not say "I am only working here", and
    inferring that would silently stop the caller seeing the rest of the datasource."""
    head = _head(profile, metric_names=["order_count"])
    assert set(head["metric_index"]) == ALL_METRICS
    assert [m["name"] for m in head["metrics"]] == ["order_count"], "detail IS selected"


# --- A1/A2: the call stops discarding what it resolved ------------------------------------------


def test_a_table_scoped_call_returns_the_joins_for_those_tables(profile):
    """The functional bug. Without this the client has columns and no way to join them, and has
    to fall back on whatever a human wrote into the prose narrative."""
    rels = _head(profile, dataset_names=["orders"])["relationships"]
    assert rels, "relationships were computed and discarded"
    edge = next(r for r in rels if r["to_table"] == "users")
    assert edge["from_column"] and edge["to_column"], "the join keys, not just the endpoints"
    assert edge["relationship"] == "many_to_one", "cardinality reaches the caller"


def test_a_table_scoped_call_returns_metrics_with_this_engines_binding(profile):
    """Projected through `_metric_full`, NOT shipped as the loader's raw dump.

    `loader._metrics_for` returns `model_dump()`s carrying the whole per-dialect `bindings` dict;
    emitting that would contradict the single-`binding` projection four sizing tests pin, and
    would ship every dialect to a client writing for one.
    """
    metrics = _head(profile, dataset_names=["orders"])["metrics"]
    by_name = {m["name"]: m for m in metrics}
    assert "order_count" in by_name
    assert by_name["order_count"]["binding"] == "SUM(order_count)"
    assert all("bindings" not in m for m in metrics), "the per-dialect dict must not leak"


# --- A7: the rule governs each block WHERE THAT BLOCK IS EMITTED --------------------------------


def test_area_scope_narrows_the_area_map_and_the_edge_list(profile):
    head = _head(profile, area=SALES)
    assert [a["name"] for a in head["subject_areas"]] == [SALES]
    assert all(e["from"] == SALES or e["to"] == SALES
               for e in head["cross_area_relationships"])


def test_table_scope_does_not_start_emitting_the_area_map(profile):
    """These two blocks are absent on the scoped branch today and stay absent.

    A caller that named its tables is not asking for the area map, and `relationships` now answers
    "how do I join these" better than the org-level edge list, which carries only endpoints.
    Emitting them would grow the response this change exists to shrink.
    """
    head = _head(profile, dataset_names=["orders"])
    assert "subject_areas" not in head
    assert "cross_area_relationships" not in head


# --- A8: the scope is legible ------------------------------------------------------------------


def test_the_response_states_the_scope_it_resolved(profile):
    """A guarantee that is relative to a scope is only honest if the scope is visible."""
    assert _head(profile)["scope"]["level"] == "datasource"
    area = _head(profile, area=SALES)["scope"]
    assert (area["level"], area["area"]) == ("area", SALES)
    table = _head(profile, dataset_names=["orders"])["scope"]
    assert (table["level"], table["tables"]) == ("table", ["orders"])


def test_the_scope_echo_is_a_declared_contract_field(profile):
    """`_Contract` is `extra="allow"`, so an undeclared echo would round-trip silently and the
    lossless payload test could not catch it. It has to be declared to be a contract."""
    from contracts import DatasourceSchemaResult

    assert "scope" in DatasourceSchemaResult.model_fields


# --- A13: the response is materially smaller ---------------------------------------------------


def test_a_table_scoped_response_carries_only_the_in_scope_catalogue(profile):
    """The size claim, stated as the thing that actually changed.

    A scoped response was ALREADY smaller than an unscoped one before this change — it omitted
    `subject_areas` and the metric detail block — so comparing two lengths passes on reverted code
    and measures nothing. What is new is that the metric catalogue itself shrank to the scope, so
    that is what this asserts: strictly fewer entries, and specifically not the whole model.
    """
    scoped = _head(profile, dataset_names=["orders"])["metric_index"]
    full = _head(profile)["metric_index"]
    assert set(scoped) < set(full), "the catalogue must be a proper subset, not the whole model"
    assert len(_raw(profile, dataset_names=["orders"])) < len(_raw(profile))


# --- findings from the review panel, each with the repro that produced it ------------------------


def test_a_referencing_areas_unsourced_metric_is_in_scope_for_a_shared_table(tmp_path, monkeypatch):
    """A table is DEFINED in one area but may be REFERENCED by others through a `TableRef`.

    Reading only `tables_defined` drops a referencing area's metric that declares no
    `source_tables` — and by this change's own rule such a metric "might apply to any table in its
    area". That is a hide inside the declared scope, which is the worst outcome here. Found by the
    review panel; no fixture in the suite had a shared table, so nothing caught it.
    """
    import yaml

    art = tmp_path / "art"
    root = art / "acme"
    _write_model(root)
    # `sales` now REFERENCES people's `users` without defining it.
    sa = root / "subject_areas" / SALES / "subject_area.yaml"
    doc = yaml.safe_load(sa.read_text())
    doc["tables"].append({"storage_connection": "c", "schema": "public", "table": "users"})
    sa.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    got = set(_head("acme", dataset_names=["users"])["metric_index"])
    assert "sales_health" in got, "the referencing area's un-sourced metric was hidden"
    assert "headcount" in got, "the defining area's metric is still in scope"


def test_an_unknown_area_is_an_error_not_an_empty_model(profile):
    """"Nothing in your scope is hidden" is vacuously true of a scope that does not exist, and an
    empty model reads to an agent as "this datasource has none" — after which it invents table
    names. Every neighbouring surface names the miss instead."""
    out = json.loads(tools.tool_get_datasource_schema({"datasource": profile, "area": "salez"}))
    assert out["error"]["kind"] == "not_found"
    assert SALES in out["error"]["remediation"], "say which areas do exist"


def test_a_cross_area_edge_is_returned_once_when_the_scope_spans_both_its_areas(profile):
    """`_relationships_among` returns a cross-area edge for EITHER endpoint, and the tables are
    resolved one group per area, so an edge spanning the scope arrived once per group."""
    rels = _head(profile, dataset_names=["orders", "users"])["relationships"]
    keys = [(r.get("from_table"), r.get("to_table"), r.get("from_column")) for r in rels]
    assert len(keys) == len(set(keys)), f"duplicate edges: {keys}"


def test_area_scope_sizes_verbosity_by_the_areas_in_scope(tmp_path, monkeypatch):
    """`mode=auto` sized by the whole datasource meant an area-scoped call on a wide model started
    at the `index` tier — which lists no tables at all — so the scope narrowed the content while
    defeating the point of asking for it. The ladder and the budget are unchanged; only the count
    fed to the selector is scope-aware."""
    import yaml

    art = tmp_path / "art"
    root = art / "acme"
    _write_model(root)
    doc = yaml.safe_load((root / "datasource.yaml").read_text())
    # Pad past the summary threshold so an unscoped `auto` would land on `index`.
    for i in range(60):
        name = f"filler{i}"
        adir = root / "subject_areas" / name
        (adir / "tables").mkdir(parents=True)
        (adir / "tables" / f"f{i}.yaml").write_text(yaml.safe_dump({
            "name": f"f{i}", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "filler", "columns": [{"name": "id", "type": "integer",
                                                  "primary_key": True}]}))
        (adir / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": name, "description": "filler area",
            "tables": [{"storage_connection": "c", "schema": "public", "table": f"f{i}"}]}))
        doc["subject_areas"].append(f"subject_areas/{name}")
    (root / "datasource.yaml").write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    assert _head("acme")["mode"] == "index", "unscoped, this model is index-tier"
    scoped = _head("acme", area=SALES)
    assert scoped["mode"] == "full", "one area is a small payload — size it that way"
    assert scoped["subject_areas"][0]["tables"], "the tier that lists tables actually lists them"
