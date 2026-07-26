"""Model-version snapshots (semantic_model/snapshot.py).

The answer receipt's `model_version` = the newest dir name under
`<profile>/.snapshots/`. Nothing wrote that dir, so model_version was null for
every profile. These tests cover the writer: a content hash that's stable +
change-sensitive, idempotent dir creation, pruning, the reader contract
(mcp_harness._model_version), and that introspect actually stamps one.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import introspect as I  # noqa: E402
from semantic_model import snapshot as S  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402


def _mini_model(root: Path) -> None:
    (root / "subject_areas" / "a").mkdir(parents=True, exist_ok=True)
    (root / "datasource.yaml").write_text("datasource: t\nversion: 1\n", encoding="utf-8")
    (root / "subject_areas" / "a" / "subject_area.yaml").write_text("name: a\n", encoding="utf-8")


def test_hash_is_stable_and_change_sensitive(tmp_path):
    r = tmp_path / "m"
    _mini_model(r)
    h1 = S.compute_model_hash(r)
    assert len(h1) == 12
    assert S.compute_model_hash(r) == h1  # stable for identical content
    (r / "datasource.yaml").write_text("datasource: t\nversion: 2\n", encoding="utf-8")
    assert S.compute_model_hash(r) != h1  # changes when the model changes


def test_hash_ignores_machine_state(tmp_path):
    r = tmp_path / "m"
    _mini_model(r)
    h1 = S.compute_model_hash(r)
    S.write_snapshot(r)  # creates .snapshots/
    (r / ".introspect").mkdir()
    (r / ".introspect" / "progress.log").write_text("noise", encoding="utf-8")
    (r / "curation_log.jsonl").write_text('{"op":"x"}\n', encoding="utf-8")
    assert S.compute_model_hash(r) == h1  # .snapshots/.introspect/curation_log excluded


def test_write_snapshot_idempotent(tmp_path):
    r = tmp_path / "prof"
    _mini_model(r)
    h = S.write_snapshot(r)
    assert h and (r / ".snapshots" / h / "manifest.json").exists()
    assert S.write_snapshot(r) == h  # unchanged model → same hash
    dirs = [p for p in (r / ".snapshots").iterdir() if p.is_dir()]
    assert len(dirs) == 1  # no duplicate dir


def test_write_snapshot_noop_without_model(tmp_path):
    assert S.write_snapshot(tmp_path / "empty") is None  # no datasource.yaml → no snapshot


def test_prune_keeps_last_N(tmp_path):
    r = tmp_path / "prof"
    _mini_model(r)
    for i in range(S.KEEP + 5):
        (r / "datasource.yaml").write_text(f"datasource: t\nversion: {i}\n", encoding="utf-8")
        S.write_snapshot(r)
        time.sleep(0.005)  # distinct mtimes so prune order is well-defined
    dirs = [p for p in (r / ".snapshots").iterdir() if p.is_dir()]
    assert len(dirs) == S.KEEP


def test_model_version_reader_contract(tmp_path, monkeypatch):
    """tools._model_version returns the newest snapshot hash — and after a
    model change, the NEW hash (newest-by-mtime wins)."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    r = tmp_path / "prof"
    _mini_model(r)
    h1 = S.write_snapshot(r)
    import tools
    assert tools._model_version("prof") == h1
    time.sleep(0.02)
    (r / "datasource.yaml").write_text("datasource: t\nversion: 99\n", encoding="utf-8")
    h2 = S.write_snapshot(r)
    assert h2 != h1
    assert tools._model_version("prof") == h2


def test_introspect_stamps_a_snapshot(tmp_path):
    """The real introspect path (build.write_tree) stamps a model_version, so a
    freshly-introspected profile is never model_version: null."""
    runner = make_catalog_runner(
        tables=["customers", "orders"],
        columns={
            "customers": [col("id", "integer", nullable=False), col("email", "varchar")],
            "orders": [col("id", "integer", nullable=False), col("customer_id", "integer")],
        },
        fks=[{"from_table": "orders", "from_column": "customer_id",
              "to_table": "customers", "to_column": "id"}],
    )
    I.introspect("shop", "postgres", runner=runner, artifacts_dir=tmp_path)  # dry_run=False → writes
    snaps = tmp_path / "shop" / ".snapshots"
    dirs = [p for p in snaps.iterdir() if p.is_dir()] if snaps.exists() else []
    assert len(dirs) == 1
    assert (dirs[0] / "manifest.json").exists()
    assert dirs[0].name == S.compute_model_hash(tmp_path / "shop")
