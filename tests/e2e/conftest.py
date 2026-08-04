"""What the DB-backed half of the corpus shares: the seeded Postgres warehouse, and the sentinel
that refuses to let a run claiming to carry that half finish without it.

**The sentinel is the part worth reading.** A test suite reports what it ran, and a job that selects
its work badly reports green for work it never selected. That is not hypothetical here: the job this
replaces picked its DB-backed vectors with `pytest -k "db_path or role"` — a substring match on the
node id — so a rename dropped 102 of 108 vectors and the job still exited 0 with nothing to show it.

Two changes close it, and they only work together:

  * the vectors carry a MARKER applied by the parametrizer, so selection is a property of the
    corpus rather than a string a rename can miss;
  * `AGAMI_IT_PG_REQUIRED` declares "this run must carry the DB-backed evidence", and with it set
    the hook below counts the marked items and ENDS the session unless the count is exactly
    `safety.corpus.EXPECTED_DB_VECTORS` — one constant, computed from `CASES` itself and read by
    both the parametrizer and the hook, so there is no second number to keep in step.

It bites on more than a rename. A module-level skip — no Postgres password, no driver — removes the
items during collection rather than reporting them skipped, so the count falls to zero and the run
that declared it needed the evidence fails instead of passing empty-handed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent
for _path in (TESTS_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

from safety.corpus import EXPECTED_DB_VECTORS  # noqa: E402

DB_PATH_MARKER = "db_path"


@pytest.fixture(scope="session")
def pg_warehouse() -> None:
    """The Postgres tables `safety.corpus.SCHEMA` describes, created once for the session.

    Session-scoped because it is the same warehouse for every vector and rebuilding it per test
    would pay a connect-and-reseed for each of fifty-odd calls. It is also what the role-floor test
    reads, so the two files agree on what `orders` is by construction rather than by coincidence.
    """
    import harness

    harness.seed_postgres()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items) -> None:
    """End the session when a run that declared it carries the DB-backed evidence has not collected
    it. Counted off the marker, compared against the corpus's own constant, and never a `-k`.

    `trylast` is load-bearing and was measured, not assumed. `-k`, `-m` and `--deselect` are all
    implemented as `pytest_collection_modifyitems` hooks in pytest's own plugins, and a conftest
    registers LATER than those, so by default this hook runs BEFORE them and sees the unfiltered
    list. A sentinel positioned there counts the vectors a run was going to drop and reports itself
    satisfied — which is the very failure it exists to catch, reproduced inside the fix. Running last
    puts the count after every deselection, where the number is what the session will actually
    execute.
    """
    if not os.environ.get(itdeps.REQUIRED):
        return
    collected = [item for item in items if item.get_closest_marker(DB_PATH_MARKER)]
    if len(collected) != EXPECTED_DB_VECTORS:
        pytest.exit(
            f"{itdeps.REQUIRED} is set, so this run must carry the DB-backed safety corpus, but it "
            f"collected {len(collected)} vectors marked {DB_PATH_MARKER!r} and the corpus has "
            f"{EXPECTED_DB_VECTORS}. A run that cannot execute the evidence must not report green.",
            returncode=1,
        )
