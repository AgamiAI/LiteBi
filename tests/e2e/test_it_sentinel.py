"""The sentinel that keeps the DB-backed job honest — proved by execution, not by reading it.

`AGAMI_IT_PG_REQUIRED` exists because pytest exits 0 when every test skips: the job that carries the
only evidence for the Postgres-served safety corpus and the read-only role floor could otherwise go
green having run nothing. The sentinel only earns that if a MISSING PREREQUISITE fails the run — and
`pytest.importorskip` (which skips before anything else is consulted) silently defeated it.

These tests drive the REAL call sites in a subprocess with a dependency made unimportable, and
assert the exit code both ways: non-zero with the sentinel set, zero (skips) without it. The second
half matters as much as the first — the DB-free job must keep skipping cleanly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import itdeps  # tests/e2e is on sys.path during collection (same directory as conftest)
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A pytest plugin (loaded via `-p`, so it runs before collection) that makes named modules
# unimportable — the cheapest faithful stand-in for "the environment is missing this dependency".
# It raises ModuleNotFoundError, exactly what a genuinely absent module raises: a plain ImportError
# would NOT be a faithful stand-in, because `pytest.importorskip` narrows on the exception type and
# would let it escape as an error — making the base behaviour look stricter than it really is.
_BLOCKER_PLUGIN = '''
import os
import sys

_BLOCKED = {n for n in os.environ.get("AGAMI_TEST_BLOCK_IMPORTS", "").split(",") if n}


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(f"blocked by the test harness: {name}", name=name)
        return None


for _mod in [m for m in sys.modules if m.split(".")[0] in _BLOCKED]:
    del sys.modules[_mod]
sys.meta_path.insert(0, _Blocker())
'''

# The DB-backed job's own invocation, mirrored from the workflow — the subprocess runs exactly this,
# so what is asserted is the JOB's exit code, not a single file's. Running one file in isolation
# would report pytest's "no tests collected" code (5) when its module skips, which reads as a failure
# for the wrong reason and hides that the OTHER file still exits the job 0.
JOB_ARGS = [
    "tests/e2e/test_safety_corpus.py",
    "tests/e2e/test_role_floor_pg.py",
    "-q",
    "-k",
    "db_path or role",
]

# The two prerequisites the job can lose: the driver the role floor + DB-served model need, and a
# transport dependency the corpus module needs. Losing either used to leave the job green.
BLOCKED_DEPS = ["psycopg2", "mcp"]
IDS = ["missing-db-driver", "missing-transport-dep"]


def _run_job_with_blocked_import(tmp_path: Path, blocked: str, sentinel: bool):
    """Run the DB-backed job in a subprocess with `blocked` unimportable; return the CompletedProcess.

    The inherited AGAMI_IT_PG_* config is stripped so the outcome depends only on the blocked import
    and the sentinel — never on whether the caller happened to have a database configured."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "agami_block_imports.py").write_text(_BLOCKER_PLUGIN)

    env = {k: v for k, v in os.environ.items() if not k.startswith("AGAMI_IT_PG_")}
    env["AGAMI_TEST_BLOCK_IMPORTS"] = blocked
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin_dir), env.get("PYTHONPATH", "")]))
    if sentinel:
        env[itdeps.SENTINEL] = "1"

    return subprocess.run(
        [sys.executable, "-m", "pytest", *JOB_ARGS,
         "-p", "agami_block_imports", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.parametrize("blocked", BLOCKED_DEPS, ids=IDS)
def test_missing_prerequisite_fails_the_job_when_the_sentinel_is_set(tmp_path, blocked):
    # THE POINT: with the sentinel set, an absent prerequisite must FAIL the job. Before the fix this
    # exited 0 with everything skipped — green, having proved nothing.
    proc = _run_job_with_blocked_import(tmp_path, blocked, sentinel=True)
    # Not just "non-zero": exit 5 is pytest's "no tests collected", which would be failing for the
    # wrong reason. The sentinel must produce a real failure.
    assert proc.returncode not in (0, 5), (
        f"the job exited {proc.returncode} with {blocked} unimportable and {itdeps.SENTINEL} set — "
        f"the sentinel did not bite:\n{proc.stdout}\n{proc.stderr}"
    )
    # …and it must fail BECAUSE of the blocked dependency. The two halves of the job fail
    # independently, so a non-zero exit alone can come from the other half while this one is still
    # skipping silently — naming the module is what pins the gap this test is about.
    assert blocked in proc.stdout, (
        f"the job failed, but not because {blocked} was missing — that half is still skipping "
        f"silently:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.mark.parametrize("blocked", BLOCKED_DEPS, ids=IDS)
def test_missing_prerequisite_still_skips_cleanly_without_the_sentinel(tmp_path, blocked):
    # The other half of the contract: the DB-free job (no sentinel) must be untouched — a missing
    # dependency there is expected and still skips, so this fix cannot turn that job red.
    proc = _run_job_with_blocked_import(tmp_path, blocked, sentinel=False)
    assert proc.returncode == 0, (
        f"the job failed without {itdeps.SENTINEL} set — the DB-free job must still skip:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "skipped" in proc.stdout


def test_importorfail_returns_the_module_when_it_is_importable(monkeypatch):
    monkeypatch.setenv(itdeps.SENTINEL, "1")
    assert itdeps.importorfail("json").dumps({"a": 1}) == '{"a": 1}'


def test_importorfail_skips_or_fails_on_the_sentinel(monkeypatch):
    # The two modes of the one helper, pinned side by side: same missing module, opposite outcomes.
    monkeypatch.delenv(itdeps.SENTINEL, raising=False)
    with pytest.raises(pytest.skip.Exception):
        itdeps.importorfail("agami_module_that_does_not_exist")

    monkeypatch.setenv(itdeps.SENTINEL, "1")
    with pytest.raises(pytest.fail.Exception):
        itdeps.importorfail("agami_module_that_does_not_exist")
