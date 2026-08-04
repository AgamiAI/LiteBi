"""What the DB-backed half of the corpus shares: the seeded Postgres warehouse, and the sentinels
that refuse to let a run claiming to carry evidence finish without it.

**The sentinels are the part worth reading.** A test suite reports what it ran, and a job that
selects its work badly reports green for work it never selected. That is not hypothetical here: the
job this replaces picked its DB-backed vectors with `pytest -k "db_path or role"` — a substring
match on the node id — so a rename dropped 102 of 108 vectors and the job still exited 0 with
nothing to show it.

Three layers close it, and each one exists because the layer above it can be satisfied by a run that
proved nothing:

  * the vectors carry MARKERS applied by their parametrizers, so selection is a property of the
    corpus rather than a string a rename can miss. `db_path` is the corpus on the served model and
    the Postgres warehouse; `role_floor` is the database refusing a write with the application out
    of the path — a separate marker and a separate count, because it is a separate claim and folding
    it into the vector total would let one shrink while the other grew.
  * `AGAMI_IT_PG_REQUIRED` declares "this run must carry the DB-backed evidence", and with it set
    `pytest_collection_modifyitems` counts the marked items and ENDS the session unless each count
    is exactly what its constant says. That catches everything that removes items: a module-level
    skip for a missing password or driver, a deleted file, a thinned parametrization, a `-k` or
    `--deselect` that misses.
  * and `pytest_sessionfinish` counts what actually RAN, because the hook above cannot. Collection
    is not execution: a `@pytest.mark.skip` on the vector, a `pytest.skip()` in its body, or a skip
    raised from the `pg_warehouse` fixture all leave the item collected and the count perfectly
    satisfied while zero vectors execute. So the marked items must also PASS, all of them, with
    nothing skipped and nothing xfailed.

`AGAMI_E2E_REQUIRED` is the same declaration for the half that needs no database — the half that
runs on every PR. It has no per-marker count to compare against (the file-path drivers parametrize
off the corpus directly), so what it asserts is the other half of the same property: under it,
nothing in this directory may skip or xfail at run time. A dependency that vanished removes its
module during collection instead, which is `itdeps.importorfail`'s job.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = Path(__file__).resolve().parent
for _path in (TESTS_ROOT, E2E_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

from safety.corpus import EXPECTED_DB_VECTORS  # noqa: E402

DB_PATH_MARKER = "db_path"
ROLE_FLOOR_MARKER = "role_floor"

# The five ways to change or destroy data that `test_role_floor_pg.py::WRITES` issues at the raw
# connection. A literal here rather than an import from that file, and deliberately so: the number
# has to survive the file being DELETED, which is one of the ways the role floor went missing before.
# A constant computed from the thing it is checking cannot fail when the thing is gone.
#
# It is therefore a second number, and the only defence against two numbers drifting is that they
# cannot drift QUIETLY: adding a sixth write class turns the DB job red until this line is updated,
# with a message that names both counts. That is the intended cost, not an oversight.
EXPECTED_ROLE_FLOOR_VECTORS = 5

# Marker -> how many items carrying it a declared run must collect AND pass.
_GUARDED = {
    DB_PATH_MARKER: EXPECTED_DB_VECTORS,
    ROLE_FLOOR_MARKER: EXPECTED_ROLE_FLOOR_VECTORS,
}

# Tallied by `pytest_runtest_logreport` below. `passed` counts call-phase passes; `not_run` counts
# every other way an item that was collected failed to produce a verdict — skipped at setup, skipped
# in its body, xfailed, xpassed.
_RAN: dict[str, dict[str, int]] = {marker: {"passed": 0, "not_run": 0} for marker in _GUARDED}
_RAN_ANY = {"passed": 0, "not_run": 0}


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
    it. Counted off the markers, compared against the constants above, and never a `-k`.

    `trylast` is load-bearing and was measured, not assumed. `-k`, `-m` and `--deselect` are all
    implemented as `pytest_collection_modifyitems` hooks in pytest's own plugins, and a conftest
    registers LATER than those, so by default this hook runs BEFORE them and sees the unfiltered
    list. A sentinel positioned there counts the vectors a run was going to drop and reports itself
    satisfied — which is the very failure it exists to catch, reproduced inside the fix. Running last
    puts the count after every deselection, where the number is what the session will actually
    collect.

    Collect, not execute — which is why this hook is only the first half. `pytest_sessionfinish`
    below is the other.
    """
    if not os.environ.get(itdeps.REQUIRED):
        return
    for marker, expected in _GUARDED.items():
        collected = [item for item in items if item.get_closest_marker(marker)]
        if len(collected) != expected:
            pytest.exit(
                f"{itdeps.REQUIRED} is set, so this run must carry the DB-backed safety corpus, "
                f"but it collected {len(collected)} items marked {marker!r} and {expected} are "
                f"required. A run that cannot execute the evidence must not report green.",
                returncode=1,
            )


def _in_this_directory(report) -> bool:
    """Whether the report belongs to a test in THIS directory.

    A conftest's hooks are registered for the whole session rather than for its own subtree, so
    without this the no-skips rule below would be a claim about every test in the repository. That
    happens to be true of the jobs that set the sentinel — both run `pytest tests/e2e` — and it
    would be a trap for the first person to set it on a wider run.
    """
    path = getattr(report, "path", None) or str(report.fspath)
    try:
        return Path(path).resolve().is_relative_to(E2E_DIR)
    except (OSError, ValueError):
        return False


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report) -> None:
    """Tally what each item actually did, so the session hook can compare execution to collection.

    `report.keywords` carries the item's own markers, so the tally is keyed off the same marker the
    parametrizer applied and the collection hook counted — one selection mechanism end to end.

    `wasxfail` is checked before `passed`, because an xpassed item reports `passed` and asserted
    nothing anyone declared: both halves of xfail belong in `not_run`.
    """
    if hasattr(report, "wasxfail") or report.skipped:
        outcome = "not_run"
    elif report.when == "call" and report.passed:
        outcome = "passed"
    else:
        # Setup/teardown passes carry no verdict, and failures already fail the session on their own.
        return
    if _in_this_directory(report):
        _RAN_ANY[outcome] += 1
    for marker in _GUARDED:
        if marker in report.keywords:
            _RAN[marker][outcome] += 1


def pytest_sessionfinish(session, exitstatus) -> None:
    """The half `pytest_collection_modifyitems` structurally cannot do: require that the evidence
    RAN.

    Everything the collection count survives, this catches — a `@pytest.mark.skip` or `skipif` on the
    parametrized test, a `pytest.skip()` in its body, a skip raised from the `pg_warehouse` fixture.
    Each leaves the item collected, so the count matches exactly and zero vectors execute.

    Failures are deliberately not re-reported here; a failed test already fails the session, and this
    hook is about the outcomes that do not.
    """
    problems: list[str] = []

    if os.environ.get(itdeps.REQUIRED):
        for marker, expected in _GUARDED.items():
            tally = _RAN[marker]
            if tally["passed"] != expected or tally["not_run"]:
                problems.append(
                    f"{expected} items marked {marker!r} must pass; {tally['passed']} passed and "
                    f"{tally['not_run']} were skipped or xfailed"
                )

    if os.environ.get(itdeps.E2E_REQUIRED):
        if _RAN_ANY["not_run"]:
            problems.append(
                f"{_RAN_ANY['not_run']} tests in this directory were skipped or xfailed, and a run "
                f"that set {itdeps.E2E_REQUIRED} declared that none would be"
            )
        if not _RAN_ANY["passed"]:
            problems.append("no test in this directory ran at all")

    if not problems:
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    lines = ["a run that declared it carries this evidence did not execute it:"]
    lines += [f"  - {problem}" for problem in problems]
    if reporter is None:
        print("\n".join(lines))
    else:
        reporter.write_sep("=", "safety corpus sentinel", red=True, bold=True)
        for line in lines:
            reporter.write_line(line)
    session.exitstatus = 1
