"""The tests that check the SUITE rather than the chokepoint.

Everything else in this directory asks what `execute_sql` decided. These two ask whether the run
asking is capable of finding out — and both close a hole that was open, not a hypothetical one.

  * **The workflow has to arm the sentinels.** Every other layer of this spec was made structural
    precisely so that coverage could not be dropped by an edit: the markers replaced a `-k`, the
    counts replaced a hope, `importorfail` replaced `importorskip`. And all of it hung off one line
    of YAML. Deleting `AGAMI_IT_PG_REQUIRED` from the DB job, or blanking it, puts `importorfail`
    and both halves of the sentinel to sleep together; taking the password with it drops the whole
    DB half to a module-level skip and an exit code of 0. Nothing in the repository noticed. This
    reads the workflow and asserts the jobs are armed.
  * **The stdio child has to be THIS checkout.** `route_stdio` spawns a real process, and a child
    inherits none of the parent's `sys.path`. Without `PYTHONPATH` it resolved `mcp_harness` and
    `execute_sql` from whatever `agami-core` was pip-installed — on the machine this was found on,
    a different worktree's copy. The transport-parity claim was then comparing two branches rather
    than two transports.

This file must not skip when there is no database, because the required `lint + test` job is where
it earns its keep: that job runs `pytest tests/`, has no Postgres, and is the check branch
protection requires. So it takes no database fixture and imports `harness` lazily, inside the one
test that needs a transport.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (TESTS_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

# The workflow is YAML and reading it as anything else would be a parser nobody asked for. Required
# rather than skipped under the file-path sentinel; in `lint + test` the extras install it anyway.
itdeps.importorfail("yaml", sentinel=itdeps.E2E_REQUIRED)

import yaml  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The three jobs this spec's evidence lives in, and what each one is for.
LINT_AND_TEST = "lint-and-test"
FILE_PATH_JOB = "safety-corpus-file-path"
DB_PATH_JOB = "safety-corpus-db-path"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _exported(value) -> str:
    """What GitHub Actions actually puts in the environment for a workflow `env:` value.

    YAML types have to be collapsed the way the runner collapses them, or this test would pass on
    `AGAMI_IT_PG_REQUIRED: false` — which reaches Python as the string `"false"` and is, to
    `os.environ.get`, perfectly truthy.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _armed(env: dict, name: str) -> bool:
    """Whether `name` is set to something that arms a sentinel rather than disarming it.

    `itdeps` and `conftest.py` both gate on `os.environ.get(...)` being truthy, so any non-empty
    value arms them. The two spellings excluded below cannot in fact disarm anything — they are
    excluded because a workflow that reads as disabled and behaves as enabled is its own kind of
    trap, and the next person to edit this file should not have to know that.
    """
    value = _exported(env.get(name))
    return bool(value) and value.lower() not in {"false", "0", "no", "off"}


def _steps(job: dict) -> list:
    return job.get("steps") or []


def _run_commands(job: dict) -> list[str]:
    return [step["run"] for step in _steps(job) if "run" in step]


def test_the_db_job_arms_the_sentinel_and_carries_a_password():
    """The single-line silent disable, closed.

    `AGAMI_IT_PG_REQUIRED` is what turns a lost driver into a failure and what arms the collection
    and session counts; `AGAMI_IT_PG_PASSWORD` is what lets the DB modules import at all. Delete
    either and the DB half of this corpus skips at module level and the job exits 0, with every
    structural guard underneath it still perfectly in place and entirely dormant.
    """
    job = _workflow()["jobs"][DB_PATH_JOB]
    env = job.get("env") or {}

    assert _armed(env, itdeps.REQUIRED), env
    assert _exported(env.get("AGAMI_IT_PG_PASSWORD")), env


def test_the_file_path_job_arms_its_own_sentinel():
    """The half that runs on every PR declares itself too.

    Its sentinel is a different variable on purpose — this job has no Postgres and must never be
    made to demand one — and without it the job passes on `4 passed, 6 skipped` the moment a model
    dependency stops importing.
    """
    job = _workflow()["jobs"][FILE_PATH_JOB]
    env = job.get("env") or {}

    assert _armed(env, itdeps.E2E_REQUIRED), env


def test_neither_safety_job_may_be_made_advisory():
    """`continue-on-error` is the other one-line disable: the job still runs, still goes red, and
    stops failing the build. A required check that cannot fail is not a check."""
    workflow = _workflow()
    for name in (FILE_PATH_JOB, DB_PATH_JOB):
        job = workflow["jobs"][name]
        assert "continue-on-error" not in job, name
        for step in _steps(job):
            assert "continue-on-error" not in step, (name, step.get("name"))


def test_both_safety_jobs_still_select_their_work_by_path():
    """The regression that started all of this: the retired job selected with
    `pytest -k "db_path or role"`, a substring match on the node id, and a rename dropped 102 of 108
    vectors while it still exited 0. Selection is a path plus a marker now, and `-k` must not come
    back."""
    workflow = _workflow()
    for name in (FILE_PATH_JOB, DB_PATH_JOB):
        commands = _run_commands(workflow["jobs"][name])
        assert any("pytest tests/e2e" in command for command in commands), name
        for command in commands:
            assert " -k " not in command, (name, command)


def test_this_file_runs_inside_the_required_job():
    """Self-referential on purpose, and it has to be.

    The two jobs above are the ones whose arming this file checks, so a check living only inside
    them could be switched off by the very edit it exists to catch. `lint + test` is the check
    branch protection requires and it runs the whole tree, which is what puts this file somewhere
    the workflow cannot disarm.
    """
    job = _workflow()["jobs"][LINT_AND_TEST]

    assert "continue-on-error" not in job
    assert any("pytest tests/" in command for command in _run_commands(job)), job


def test_the_stdio_child_imports_the_checkout_under_test():
    """`route_stdio`'s child resolves the executor out of THIS source tree, not a pip-installed one.

    Two assertions because either alone can pass for the wrong reason. The environment is inspected
    directly, so deleting the `PYTHONPATH` line fails this everywhere — including on a runner where
    the installed package happens to BE this checkout, which is the configuration that would
    otherwise hide the bug forever. Then the child is actually spawned and asked what it resolved,
    because an environment variable is a claim and `__file__` is the answer.
    """
    import harness

    env = harness.stdio_child_env()
    assert str(harness.SRC) in env["PYTHONPATH"].split(os.pathsep), env["PYTHONPATH"]

    probe = (
        "import json, execute_sql, guardrail, mcp_harness, sql_guard, tools\n"
        "print(json.dumps({m.__name__: m.__file__ for m in "
        "(execute_sql, guardrail, mcp_harness, sql_guard, tools)}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, proc.stderr

    resolved = json.loads(proc.stdout)
    for name, path in resolved.items():
        assert Path(path).resolve().is_relative_to(harness.SRC.resolve()), (name, path)
