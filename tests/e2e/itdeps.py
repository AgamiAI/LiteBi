"""Dependency gating for the env-gated integration tests: the one place that decides whether a
missing prerequisite is a skip or a failure.

`pytest.importorskip` is the right tool locally — a developer with no Postgres driver should still
get a usable suite — but it skips unconditionally, and pytest exits 0 when every test skips. In a
job whose entire purpose is to execute the DB-backed half of this corpus, a lost driver would
therefore skip everything and report green: the run proves nothing and says so in a colour nobody
reads. `AGAMI_IT_PG_REQUIRED` is how that job declares "these tests MUST run", and `importorfail`
is `importorskip` until it is set.
"""

from __future__ import annotations

import importlib
import os

import pytest

# Set by the job that carries the DB-backed evidence. Its presence is the caller declaring that a
# skip here is a failed run, not a tolerated one.
REQUIRED = "AGAMI_IT_PG_REQUIRED"


def importorfail(*modules: str) -> None:
    """Require `modules`: skip when they are missing, or FAIL when `REQUIRED` is set.

    Returns nothing. Callers gate on the dependency being importable rather than using the module
    object, so handing one back for a variadic call would only raise the question of which one.

    Both outcomes are raised at module level, because the call site is a module-scope guard in a
    DB-backed test file: a bare `pytest.skip` during collection is a collection ERROR, which would
    turn the DB-free job red for a dependency it is allowed to be without.
    """
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            reason = f"missing test dependency {name!r}: {exc}"
            if os.environ.get(REQUIRED):
                pytest.fail(reason, pytrace=False)
            pytest.skip(reason, allow_module_level=True)
