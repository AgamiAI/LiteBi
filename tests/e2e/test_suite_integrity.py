"""The tests that check the SUITE rather than the chokepoint.

Everything else in this directory asks what `execute_sql` decided. These ask whether the run asking
is capable of finding out — and each closes a hole that was open, not a hypothetical one.

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
  * **The role the suite tests has to be the role operators get.**
    `tests/integration/fixtures/postgres-readonly-grants.sql` is a second copy of the recipe shipped
    in `plugins/agami/shared/readonly-grants.md`, and its own header says so — but nothing enforced
    it. The role is the primary, non-bypassable integrity control, so a fixture that drifts turns
    the whole DB half green on a role no operator has: a false certification of exactly the kind
    this spec exists to prevent. This diffs the two.

This file must not skip when there is no database, because the required `lint + test` job is where
it earns its keep: that job runs `pytest tests/`, has no Postgres, and is the check branch
protection requires. So it takes no database fixture and imports `harness` lazily, inside the one
test that needs a transport.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
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

# The two copies of the read-only role: the one operators are handed, and the one the suite runs.
RECIPE = REPO_ROOT / "plugins" / "agami" / "shared" / "readonly-grants.md"
GRANTS_FIXTURE = REPO_ROOT / "tests" / "integration" / "fixtures" / "postgres-readonly-grants.sql"

# The heading whose first fenced block is the required baseline. Everything below keys off it rather
# than off a line number, so reordering the document is not reported as drift.
POSTGRES_HEADING = "## PostgreSQL / Redshift"

# The recipe's `<…>` placeholders and the concrete values the fixture fills them with for the
# throwaway container. Applied to the FIXTURE, so the comparison happens in the recipe's own
# vocabulary and a failure reads as one shipped line against another rather than as a diff the
# reader has to substitute in their head.
#
# Plain string replacement, longest first, and the password matched inside its quotes: `agami_ro_pw`
# starts with the role name and `agami_test` contains none of the others, but ordering the pairs is
# cheaper than relying on that staying true.
FIXTURE_VALUES: tuple[tuple[str, str], ...] = (
    ("'agami_ro_pw'", "'<password>'"),
    ("agami_test", "<owner>"),
    ("corpus", "<db>"),
    ("public", "<schema>"),
)

# What the fixture adds that the recipe has no counterpart for: the corpus needs a database of its
# own (the safety corpus and the CLI smoke scripts both declare `orders`, and one would have to
# give), and psql has to be told to enter it before the schema-scoped grants mean anything.
#
# Named one by one on purpose. "The fixture may carry extra statements" would be a shorter rule and
# a useless one — it would swallow a deleted `GRANT SELECT` just as happily as it swallows these.
FIXTURE_SCAFFOLDING = (
    "CREATE DATABASE corpus OWNER agami_test;",
    "\\connect corpus",
)

# The two lines the fixture deliberately does NOT copy, quoted from the recipe verbatim. They live
# in a blockquote that calls itself optional; the fixture's header explains that running them would
# prove a floor stronger than the one an operator who follows the recipe actually has.
OPTIONAL_MARKER = "Optional hardening"
OPTIONAL_HARDENING = (
    "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
    "REVOKE TEMP ON DATABASE <db> FROM PUBLIC;",
)


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


def _strip_sql_comment(line: str) -> str:
    """`line` up to its first `--`, ignoring one that falls inside a quoted literal.

    The quote tracking is not decoration. The only literal in either file today is the fixture
    password, so a naive split would be right — until someone picks a password with a `--` in it,
    which is precisely the character class a password generator reaches for. The test would then
    silently compare a truncated statement.
    """
    quoted = False
    for index, char in enumerate(line):
        if char == "'":
            quoted = not quoted
        elif not quoted and line.startswith("--", index):
            return line[:index]
    return line


def _statements(sql: str) -> list[str]:
    """`sql` as one normalized statement per element: comments gone, whitespace collapsed.

    Accumulated until a `;` rather than assumed to be one statement per line, so reflowing a long
    `ALTER DEFAULT PRIVILEGES` over two lines is a formatting choice and not drift. psql
    meta-commands (`\\connect`) carry no terminator and are line-scoped, so they close themselves.
    """
    statements: list[str] = []
    pending = ""
    for raw in sql.splitlines():
        line = _strip_sql_comment(raw).strip()
        if not line:
            continue
        if not pending and line.startswith("\\"):
            statements.append(" ".join(line.split()))
            continue
        pending = f"{pending} {line}" if pending else line
        if pending.endswith(";"):
            statements.append(" ".join(pending.split()))
            pending = ""
    if pending:
        statements.append(" ".join(pending.split()))
    return statements


def _postgres_section() -> list[str]:
    """The recipe's lines under `## PostgreSQL / Redshift`, stopping at the next heading.

    Bounded rather than open-ended so that deleting the PostgreSQL block cannot quietly promote
    MySQL's into its place — the block lookup below would find nothing and say so.
    """
    lines = RECIPE.read_text().splitlines()
    assert POSTGRES_HEADING in lines, f"{RECIPE} no longer has a {POSTGRES_HEADING!r} heading"
    start = lines.index(POSTGRES_HEADING) + 1
    for offset, line in enumerate(lines[start:]):
        if line.startswith("## "):
            return lines[start : start + offset]
    return lines[start:]


def _required_block(section: list[str]) -> str:
    """The first UNINDENTED ```sql fence in `section`: the required baseline, and only it.

    Column zero is the whole distinction. The optional-hardening statements are fenced too, but
    inside a blockquote, so their fence line begins `> ` and is passed over here structurally rather
    than by matching on the text of the lines this test exists to police.
    """
    body: list[str] = []
    inside = False
    for line in section:
        if line.startswith("```"):
            if inside:
                return "\n".join(body)
            assert line.strip() == "```sql", f"expected a ```sql fence, found {line!r}"
            inside = True
        elif inside:
            body.append(line)
    raise AssertionError(f"no closed ```sql block under {POSTGRES_HEADING!r} in {RECIPE}")


def _optional_hardening_quote(section: list[str]) -> list[str]:
    """The contiguous blockquote that introduces itself as optional hardening.

    Found by its run of `>` lines rather than by searching for the REVOKEs, because what is being
    asserted is WHERE they live: promoted into the required recipe they would no longer be in this
    run, and the fixture — which omits them on purpose — would have to be updated to match.
    """
    run: list[str] = []
    for line in [*section, ""]:
        if line.startswith(">"):
            run.append(line)
            continue
        if any(OPTIONAL_MARKER in quoted for quoted in run):
            return run
        run = []
    raise AssertionError(
        f"{RECIPE} no longer carries a blockquote introducing itself as {OPTIONAL_MARKER!r} under "
        f"{POSTGRES_HEADING!r}"
    )


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
    back.

    Tokenize rather than searching for `" -k "`. `-k` binds its argument three ways — `-k expr`,
    `-kexpr` and `-k"expr"` — and only the first contains that substring, so the cheap spelling of
    this check would have waved through two of the three. A test against a substring match, defeated
    by a spelling, is the very shape of the bug it is here to prevent.
    """
    workflow = _workflow()
    for name in (FILE_PATH_JOB, DB_PATH_JOB):
        commands = _run_commands(workflow["jobs"][name])
        assert any("pytest tests/e2e" in command for command in commands), name
        for command in commands:
            offenders = [tok for tok in shlex.split(command) if tok.startswith("-k")]
            assert not offenders, (name, offenders, command)


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


def test_the_grants_fixture_is_the_shipped_postgres_recipe():
    """The fixture's header says it is a copy of the shipped recipe. This is what makes that true.

    The read-only role is the primary, non-bypassable integrity control — the one that holds when
    the app-layer guard does not — so every DB-backed claim in this directory is a claim about the
    role an operator following `readonly-grants.md` ends up with. The fixture is a SECOND copy of
    that recipe, and until now the only thing keeping the two in step was a comment asking nicely.
    Widen one `GRANT SELECT` to `GRANT ALL`, or drop the `ALTER DEFAULT PRIVILEGES` line, and the
    whole safety suite goes green on a role nobody has.

    A pure text comparison with no database anywhere, which is why it belongs in this file: the
    required `lint + test` job runs it, and that job has no Postgres.
    """
    recipe = _statements(_required_block(_postgres_section()))
    assert recipe, f"the {POSTGRES_HEADING!r} block in {RECIPE} parsed to no statements"

    fixture = _statements(GRANTS_FIXTURE.read_text())
    for scaffolding in FIXTURE_SCAFFOLDING:
        assert fixture.count(scaffolding) == 1, (
            f"{GRANTS_FIXTURE.name} no longer carries {scaffolding!r} exactly once, so the "
            f"scaffolding exclusion in FIXTURE_SCAFFOLDING is stale and would hide real drift"
        )
        fixture.remove(scaffolding)

    for value, placeholder in FIXTURE_VALUES:
        fixture = [statement.replace(value, placeholder) for statement in fixture]

    diff = "\n".join(
        difflib.unified_diff(
            recipe,
            fixture,
            fromfile=f"{RECIPE.name} ({POSTGRES_HEADING})",
            tofile=GRANTS_FIXTURE.name,
            lineterm="",
        )
    )
    assert fixture == recipe, (
        "the read-only role the suite tests is no longer the role the recipe ships. Update whichever"
        " of the two is behind — a fixture that has drifted certifies a role no operator has:\n"
        f"{diff}"
    )


def test_the_optional_hardening_stays_out_of_the_required_recipe():
    """The one asymmetry above is deliberate, and this is what keeps it deliberate.

    The fixture omits the recipe's two optional `REVOKE`s because running them would prove a floor
    STRONGER than the shipped baseline, which is the "proving something about a role no operator
    has" failure wearing its politest face. That omission is only correct while the doc still
    presents those lines as optional. Promote them into the required block and this fails here,
    naming the statement, so the fixture gets updated rather than quietly under-testing the floor.
    """
    section = _postgres_section()
    required = _statements(_required_block(section))
    quote = _optional_hardening_quote(section)

    for statement in OPTIONAL_HARDENING:
        assert statement not in required, (
            f"{statement!r} has been promoted into the required {POSTGRES_HEADING!r} block. "
            f"{GRANTS_FIXTURE.name} omits it on purpose and must be updated to run it too"
        )
        hosts = [line for line in section if statement in line]
        assert len(hosts) == 1, (
            f"expected {statement!r} exactly once under {POSTGRES_HEADING!r}, found {hosts}"
        )
        assert hosts[0] in quote, (
            f"{statement!r} has left the {OPTIONAL_MARKER!r} blockquote. If it is now required, "
            f"{GRANTS_FIXTURE.name} has to run it; if not, put it back where it reads as optional"
        )
