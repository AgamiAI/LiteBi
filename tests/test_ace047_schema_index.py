"""ACE-047 — get_datasource_schema O(1) table lookups (Slice 1).

The name→table index must reproduce `_find_table` EXACTLY (area scoping, name-or-bare match,
first-in-scan-order on a clash, TableRef fallback), and the whole schema payload must stay
byte-identical whether the index is used or the old linear scan is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402


def _tbl(name: str, cols=("id",)) -> m.Table:
    return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                   description=f"{name} desc", columns=[m.Column(name=c, type="integer") for c in cols])


def _ref(table: str) -> m.TableRef:
    return m.TableRef(storage_connection="c", schema="public", table=table)


def _org_with_clash_and_fallback() -> m.Datasource:
    # alpha defines a table literally named "orders"; beta defines one named "sales.orders" (bare
    # "orders") — a name clash resolved by scan order. gamma REFERENCES orders (defined in alpha)
    # via a TableRef but doesn't define it — the multi-area fallback case.
    alpha = m.SubjectArea(name="alpha", tables=[_ref("orders")], tables_defined=[_tbl("orders")])
    beta = m.SubjectArea(name="beta", tables=[_ref("sales.orders")],
                         tables_defined=[_tbl("sales.orders")])
    gamma = m.SubjectArea(name="gamma", tables=[_ref("customers"), _ref("orders")],
                          tables_defined=[_tbl("customers")])
    return m.Datasource(datasource="O",
                          storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                          subject_areas=[alpha, beta, gamma])


def test_index_find_matches_linear_find_for_every_case():
    org = _org_with_clash_and_fallback()
    idx = L.build_table_index(org)
    # (name, area) matrix: exact names, bare/full, area-scoped, org-wide, the clash, the TableRef
    # fallback (customers-area referencing orders), and misses.
    cases = [
        ("orders", None), ("sales.orders", None), ("customers", None), ("missing", None),
        ("orders", "alpha"), ("orders", "beta"), ("orders", "gamma"),  # gamma = TableRef fallback
        ("sales.orders", "beta"), ("customers", "gamma"), ("customers", "alpha"),
        ("orders", "nosucharea"), ("", None), ("orders", ""),  # "" area is falsy → org-wide
    ]
    for name, area in cases:
        linear = L._find_table(org, name, area)                 # the old scan
        viaidx = L._find_table(org, name, area, index=idx)      # the O(1) path
        assert viaidx is linear, f"index diverged from linear for ({name!r}, {area!r})"


def test_index_matches_linear_with_duplicate_area_names():
    # Area names are NOT enforced unique. `_find_table` resolves an area via subject_area(), which
    # returns the FIRST area of that name — so the index's per-area maps must be first-wins too.
    a1 = m.SubjectArea(name="sales", tables=[_ref("only_first")], tables_defined=[_tbl("only_first")])
    a2 = m.SubjectArea(name="sales", tables=[_ref("only_second")], tables_defined=[_tbl("only_second")])
    org = m.Datasource(datasource="O",
                         storage_connections=[m.StorageConnection(name="c", storage_type="PostgreSQL")],
                         subject_areas=[a1, a2])
    idx = L.build_table_index(org)
    for name in ("only_first", "only_second", "missing"):
        assert L._find_table(org, name, "sales", index=idx) is L._find_table(org, name, "sales")


def test_collect_default_filters_index_matches_linear():
    org = _org_with_clash_and_fallback()
    idx = L.build_table_index(org)
    for area in (None, "alpha", "gamma"):
        base = L.collect_default_filters(org, ["orders", "customers"], area=area)
        withidx = L.collect_default_filters(org, ["orders", "customers"], area=area, index=idx)
        assert withidx == base


def test_get_table_context_byte_identical_with_and_without_index():
    org = _org_with_clash_and_fallback()
    idx = L.build_table_index(org)
    names = ["orders", "customers", "sales.orders"]
    for area in (None, "alpha", "gamma"):
        base = L.get_table_context(org, names, area=area)
        withidx = L.get_table_context(org, names, area=area, index=idx)
        assert json.dumps(withidx, default=str) == json.dumps(base, default=str)


# ---------------------------------------------------------------- tool-level byte identity


def _write_model(root: Path, n_areas: int, tables_per_area: int, *, wide: bool, big: bool) -> None:
    import yaml

    (root / "datasources" / "c").mkdir(parents=True, exist_ok=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    area_paths = []
    for i in range(n_areas):
        a = f"area{i}"
        (root / "subject_areas" / a / "tables").mkdir(parents=True)
        refs = []
        for j in range(tables_per_area):
            tname = f"t{i}_{j}"
            refs.append({"storage_connection": "c", "schema": "public", "table": tname})
            cols = [{"name": "id", "type": "integer", "primary_key": True}]
            for k in range(10 if wide else 1):
                cols.append({"name": f"col_{k}", "type": "decimal",
                             "description": ("d" * 300) if wide else "x"})
            tdoc = {"name": tname, "schema": "public", "storage_connection": "c", "grain": ["id"],
                    "description": f"table {tname}", "columns": cols}
            if big:
                tdoc["performance_hints"] = {"estimated_row_count": 2_000_000}
            (root / "subject_areas" / a / "tables" / f"{tname}.yaml").write_text(yaml.safe_dump(tdoc))
        (root / "subject_areas" / a / "subject_area.yaml").write_text(
            yaml.safe_dump({"name": a, "description": f"area {a}", "tables": refs}))
        area_paths.append(f"subject_areas/{a}")
    (root / "datasource.yaml").write_text(yaml.safe_dump(
        {"datasource": "acme", "version": 1,
         "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
         "subject_areas": area_paths}))


@pytest.mark.parametrize("build,args", [
    (dict(n_areas=3, tables_per_area=4, wide=True, big=True), {"mode": "full"}),      # full
    (dict(n_areas=30, tables_per_area=2, wide=False, big=False), {"mode": "auto"}),   # summary
    (dict(n_areas=60, tables_per_area=1, wide=False, big=False), {"mode": "auto"}),   # index
    (dict(n_areas=3, tables_per_area=8, wide=True, big=False), {"mode": "full"}),     # budget-downgrade
    (dict(n_areas=3, tables_per_area=4, wide=True, big=False), {"dataset_names": ["t0_0", "t1_1"]}),
])
def test_tool_output_byte_identical_with_and_without_index(monkeypatch, tmp_path, build, args):
    import tools

    art = tmp_path / "art"
    _write_model(art / "acme", **build)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    with_index = tools.tool_get_datasource_schema({"datasource": "acme", **args})
    # Force the old linear path everywhere by disabling the index, and compare byte-for-byte.
    monkeypatch.setattr(L, "build_table_index", lambda org: None)
    without_index = tools.tool_get_datasource_schema({"datasource": "acme", **args})

    assert with_index == without_index


def test_e2e_wide_model_all_lookups_via_index_and_linear_in_table_count(monkeypatch, tmp_path):
    # The done-bar: on a 300-table model, full-mode assembly resolves EVERY table through the O(1)
    # index (the linear scan is never entered), and the number of lookups is linear in the table
    # count — not the O(tables^2) rescan the index replaces. Counts work, not wall-time (non-flaky).
    import tools

    n_tables = 300
    art = tmp_path / "art"
    _write_model(art / "acme", n_areas=10, tables_per_area=30, wide=False, big=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    scan_calls = {"n": 0}
    real_find_table = L._find_table

    def counting_find_table(org, name, area=None, *, index=None):
        if index is None:
            scan_calls["n"] += 1  # a fall-through to the old linear scan
        return real_find_table(org, name, area, index=index)

    find_calls = {"n": 0}
    real_index_find = L.TableIndex.find

    def counting_index_find(self, name, area=None):
        find_calls["n"] += 1
        return real_index_find(self, name, area)

    monkeypatch.setattr(L, "_find_table", counting_find_table)
    monkeypatch.setattr(L.TableIndex, "find", counting_index_find)

    tools.tool_get_datasource_schema({"datasource": "acme", "mode": "full"})

    assert scan_calls["n"] == 0                    # no lookup fell back to the linear scan
    assert find_calls["n"] >= n_tables             # every table was resolved
    assert find_calls["n"] <= n_tables * 4         # a constant #lookups per table — LINEAR, not n^2
