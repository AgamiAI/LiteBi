"""Shared pytest fixtures for agami-core tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))


@pytest.fixture(autouse=True)
def _reset_org_cache():
    """The per-process semantic-model cache (ACE-045) is module-global state; isolate every test from it
    (and from a leaked current-org) so one test's cached model never bleeds into the next."""
    try:
        import tools
    except Exception:
        yield
        return
    tools._ORG_CACHE.clear()
    tools._current_org_ctx.set(None)
    tools.resolved_org_id.cache_clear()  # F14: memoized org-id resolver; clear so env/profile changes take
    yield
    tools._ORG_CACHE.clear()
    tools._current_org_ctx.set(None)
    tools.resolved_org_id.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_query_log(tmp_path_factory, monkeypatch):
    """Keep the suite's audit writes out of the developer's own artifacts directory.

    `tools._emit` records one query-execution row for EVERY outcome now, and with no database
    configured that record is appended to `tools.QUERY_LOG` — a module-level constant resolved at
    import time from the real artifacts dir, so a test setting `AGAMI_ARTIFACTS_DIR` afterwards does
    not move it. Left alone, running the tests would append to the developer's own
    `query_log.jsonl`. Redirect it per test; a test that asserts on the jsonl points it at a path of
    its own and this fixture is then simply overwritten."""
    try:
        import tools
    except Exception:
        yield
        return
    monkeypatch.setattr(tools, "QUERY_LOG", tmp_path_factory.mktemp("qlog") / "query_log.jsonl")
    yield


@pytest.fixture(autouse=True)
def _restore_raw_logger():
    """`execute_sql.main()` silences `_RAW_LOG` for the lifetime of the process it owns, and a test
    that calls it in-process owns the whole session instead.

    The silencing is right in production: the CLI child's stderr is a wire carrying exactly one JSON
    object, so a diagnostic line there makes the refusal unparseable. But it is a permanent mutation
    of a module-level logger (a NullHandler plus `propagate = False`) with no restore, and
    `propagate = False` is precisely what stops a record from reaching `caplog`. So every test that
    runs AFTER an in-process `main()` sees a logger that can no longer be asserted on, and a test
    proving the operator gets the cause of a failure passes or fails on file ordering alone.

    Restored around every test rather than in the callers: there are five of them across two files
    today, the next one will not know it is the fourth thing to trip this, and the failure it causes
    lands in someone else's test.
    """
    try:
        import execute_sql
    except Exception:
        yield
        return
    log = execute_sql._RAW_LOG
    handlers, propagate = list(log.handlers), log.propagate
    yield
    log.handlers, log.propagate = handlers, propagate


@pytest.fixture(autouse=True)
def _reset_validation_cache():
    """The incremental-curation-validation cache (ACE-046) is module-global too; clear it around
    each test so one test's cached per-area findings can't bleed into the next."""
    try:
        from semantic_model import curate
    except Exception:
        yield
        return
    curate._VALIDATION_CACHE.clear()
    yield
    curate._VALIDATION_CACHE.clear()
