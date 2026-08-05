"""Dependency gating for the env-gated integration tests: the one place that decides whether a
missing prerequisite is a skip or a failure.

`pytest.importorskip` is the right tool locally — a developer with no Postgres driver should still
get a usable suite — but it skips unconditionally, and pytest exits 0 when every test skips. In a
job whose entire purpose is to execute a half of this corpus, a lost dependency would therefore
skip everything and report green: the run proves nothing and says so in a colour nobody reads. A
SENTINEL variable is how such a job declares "these tests MUST run", and `importorfail` is
`importorskip` until one is set.

**There are two sentinels because there are two jobs, and neither may speak for the other.** The
DB-backed half needs a database and a driver the file-path half is entitled to be without, so one
name covering both would either force Postgres onto the job that must run without it or leave the
job that runs on every PR ungoverned. Each guard therefore names the sentinel that governs it, and
the default is the DB one because that is where the pattern started.
"""

from __future__ import annotations

import importlib
import os
from typing import NoReturn

import pytest

# Set by the job that carries the DB-backed evidence: the served model, the Postgres warehouse and
# the read-only role floor. Its presence is the caller declaring that a skip here is a failed run,
# not a tolerated one.
REQUIRED = "AGAMI_IT_PG_REQUIRED"

# Set by the job that carries the FILE-path evidence — the half that runs on every PR against disk
# YAML and a SQLite file, with no database anywhere. It needs its own name for the reason above, and
# it needs to exist at all because without it that job passed having collected nothing: every corpus
# module opened with `pytest.importorskip`, so an unimportable `sqlglot` reduced the whole run to
# `4 passed, 6 skipped` and exit 0.
E2E_REQUIRED = "AGAMI_E2E_REQUIRED"


def skip_or_fail(reason: str, sentinel: str = REQUIRED, *, module_level: bool = False) -> NoReturn:
    """A prerequisite is absent: skip, or FAIL when `sentinel` says the run must carry it.

    The decision `importorfail` makes, factored out for the prerequisites that are not imports — a
    password that was never set, an interpreter that will not hide its site-packages. Those gates
    were written as bare `pytest.skip` calls and each was a way for a declared run to report green
    having proved nothing, which is the same hole in a different shape.

    `module_level` is the caller stating where it is: a bare `pytest.skip` raised while a module is
    being imported is a collection ERROR rather than a skip, so a module-scope guard has to say so.
    Inside a fixture or a test body the flag is wrong and the default is right.
    """
    if os.environ.get(sentinel):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason, allow_module_level=module_level)


def importorfail(*modules: str, sentinel: str = REQUIRED) -> None:
    """Require `modules`: skip when they are missing, or FAIL when `sentinel` is set.

    Returns nothing. Callers gate on the dependency being importable rather than using the module
    object, so handing one back for a variadic call would only raise the question of which one.

    Both outcomes are raised at module level, because the call site is a module-scope guard in a
    test file: a bare `pytest.skip` during collection is a collection ERROR, which would turn a job
    red for a dependency it is allowed to be without. Module scope is also what makes the sentinel's
    other half work — the missing dependency removes the items during COLLECTION, where the
    counting hook in `conftest.py` can see that they are gone, rather than skipping them one by one
    at call time with the count already taken.
    """
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            skip_or_fail(
                f"missing test dependency {name!r}: {exc}", sentinel, module_level=True
            )
