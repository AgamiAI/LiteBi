"""Dependency gating for the env-gated Postgres integration tests — the one place that decides
whether a missing prerequisite is a SKIP or a FAILURE.

`AGAMI_IT_PG_REQUIRED` exists so the DB-backed job cannot pass while proving nothing: that job
carries the only evidence for the Postgres-served safety corpus (file/db parity) and the read-only
role floor, and pytest exits 0 when every test skips. `pytest.importorskip` defeated that — it skips
unconditionally, so a missing driver or a missing transport dependency turned the whole job into
skips and it exited green.

`importorfail` is the drop-in that respects the sentinel: identical to `importorskip` when the
sentinel is unset (so the DB-free job is unaffected), and a hard failure when it is set — a missing
prerequisite there means the run did not execute the proof it was there to execute.
"""

from __future__ import annotations

import importlib
import os

import pytest

SENTINEL = "AGAMI_IT_PG_REQUIRED"


def db_required() -> bool:
    """True when the caller declared this run MUST execute the DB-backed tests, not skip them."""
    return bool(os.environ.get(SENTINEL))


def unavailable(reason: str, *, module_level: bool = False):
    """Skip — or FAIL when the sentinel is set. Never returns (both raise).

    `module_level` must be True when called during collection rather than inside a test or fixture:
    a bare `pytest.skip` there is a collection ERROR, which would turn the DB-free job red for a
    dependency it is allowed to be missing."""
    if db_required():
        pytest.fail(reason)
    pytest.skip(reason, allow_module_level=module_level)


def importorfail(name: str):
    """`pytest.importorskip`, except a missing module FAILS when the sentinel is set.

    Called at module scope in the DB-backed test modules, so the failure surfaces as a collection
    error and the job exits non-zero instead of silently reporting a skip."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        unavailable(f"could not import {name!r}: {exc}", module_level=True)
