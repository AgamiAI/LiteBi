"""Regression tests for ``execute_sql._resolve_guard_model`` — the disk/DB model resolver the
model-safety guards depend on.

A merge once dropped this module's ``from semantic_model import loader as L``, so the disk branch's
``L.load_datasource(root)`` raised ``NameError``, was swallowed by the resolver's ``except Exception``,
and returned ``None`` for EVERY disk-served model — silently disabling all model-safety guards on the
local/disk path while the gate UNIT tests stayed green (only the e2e corpus, indirectly, caught it).
These pin the resolver directly (fast, no subprocess) so that class of silent failure fails loudly,
and so a genuine load failure is now LOGGED rather than indistinguishable from 'no model yet'.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402


def _write_disk_model(root: Path) -> None:
    """Materialize a real file-served model (declared tables + a sensitive column) via the e2e
    harness's canonical builder, so the assertion exercises the ACTUAL loader, not a stub."""
    spec = importlib.util.spec_from_file_location(
        "e2e_harness", REPO_ROOT / "tests" / "e2e" / "harness.py"
    )
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    h.write_disk_model(root)


def _local_env(monkeypatch, artifacts_dir: Path) -> None:
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)  # no DB -> local/disk path
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts_dir))


def test_resolve_guard_model_loads_a_disk_model(tmp_path, monkeypatch):
    # The regression: a valid disk model must RESOLVE to a populated Datasource — not silently None,
    # which would leave the scope / PII / trap guards inert on every disk-served query.
    art = tmp_path / "art"
    (art / "acme").mkdir(parents=True)
    _write_disk_model(art / "acme")
    _local_env(monkeypatch, art)

    org = execute_sql._resolve_guard_model("acme")

    assert org is not None, "a valid disk model must resolve (None here == guards silently off)"
    tables = {t.name for sa in org.subject_areas for t in sa.tables_defined}
    assert tables, "the resolved model must carry its declared tables so the scope guards can fire"


def test_resolve_guard_model_returns_none_when_absent(tmp_path, monkeypatch):
    _local_env(monkeypatch, tmp_path / "empty")  # no datasource.yaml anywhere -> genuine no-model
    assert execute_sql._resolve_guard_model("nope") is None


def test_resolve_guard_model_logs_a_load_failure_instead_of_silent_none(tmp_path, monkeypatch, caplog):
    # A genuine loader failure must (a) still degrade to None so the hosted path fails closed, AND
    # (b) be LOGGED — so a resolver bug is observable, not masquerading as 'no model yet' (the exact
    # trap the dropped-import bug fell into).
    art = tmp_path / "art"
    (art / "acme").mkdir(parents=True)
    _write_disk_model(art / "acme")  # datasource.yaml exists -> the disk branch IS entered
    _local_env(monkeypatch, art)

    from semantic_model import loader as L

    def _boom(*_a, **_k):
        raise RuntimeError("simulated loader failure")

    monkeypatch.setattr(L, "load_datasource", _boom)

    with caplog.at_level(logging.WARNING):
        assert execute_sql._resolve_guard_model("acme") is None  # still fails closed
    assert "failed to load" in caplog.text, "a real load failure must be logged, not silently swallowed"
