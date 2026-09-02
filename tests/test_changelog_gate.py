"""The changelog gate: a PR that changes what we ship must say so in CHANGELOG.md.

The gate's whole value is being narrow enough that nobody learns to waive it on reflex, so these
tests pin both directions — what it catches AND what it deliberately lets through. A gate that
fired on a test-only change would be waived by habit within a week, and then the release it exists
to protect would ship with no notes anyway.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "dev" / "changelog_gate.py"

sys.path.insert(0, str(REPO / "dev"))

from changelog_gate import CHANGELOG, unreleased_paths  # noqa: E402

# --- what it catches -------------------------------------------------------------------------


def test_a_plugin_change_without_a_changelog_entry_is_caught():
    assert unreleased_paths(["plugins/agami/skills/agami-eval/SKILL.md"]) == [
        "plugins/agami/skills/agami-eval/SKILL.md"
    ]


def test_a_library_change_without_a_changelog_entry_is_caught():
    assert unreleased_paths(["packages/agami-core/src/semantic_model/golden.py"]) == [
        "packages/agami-core/src/semantic_model/golden.py"
    ]


def test_a_dependency_change_is_caught():
    # A cap or a new dep changes what a user's install resolves to, so it is user-visible even
    # though no line of our own code moved.
    assert unreleased_paths(["packages/agami-core/pyproject.toml"]) == [
        "packages/agami-core/pyproject.toml"
    ]


def test_every_offender_is_reported_not_just_the_first():
    # The failure names what tripped it; reporting one of five would send the author round again.
    paths = [
        "packages/agami-core/src/tools.py",
        "plugins/agami/scripts/sm",
        "tests/test_tools.py",
        "docs/self-hosting.md",
    ]
    assert unreleased_paths(paths) == [
        "packages/agami-core/src/tools.py",
        "plugins/agami/scripts/sm",
    ]


def test_the_real_070_diff_would_have_been_caught():
    # The case the gate was written for: 0.7.0's shipped surface, with CHANGELOG.md absent.
    paths = [
        "packages/agami-core/src/semantic_model/comparator.py",
        "packages/agami-core/src/semantic_model/golden_run.py",
        "plugins/agami/scripts/run_golden_eval.py",
        "plugins/agami/skills/agami-save-golden/SKILL.md",
    ]
    assert len(unreleased_paths(paths)) == 4


# --- what it deliberately lets through -------------------------------------------------------


def test_a_changelog_entry_satisfies_the_gate():
    assert unreleased_paths(["plugins/agami/scripts/sm", CHANGELOG]) == []


def test_a_test_only_change_is_not_gated():
    assert unreleased_paths(["tests/test_tools.py"]) == []


def test_a_packages_own_tests_dir_is_not_gated():
    # A package that grows its own tests/ must not trip a gate meant for shipped code.
    assert unreleased_paths(["packages/agami-core/tests/test_internal.py"]) == []


def test_docs_ci_and_dev_tooling_are_not_gated():
    # None of these reach a user's machine on an install, so gating them would be the noise that
    # teaches people to waive the label without reading.
    assert (
        unreleased_paths(
            [
                "docs/self-hosting.md",
                "README.md",
                "CONTRIBUTING.md",
                ".github/workflows/ci.yml",
                "dev.py",
                "dev/changelog_gate.py",
            ]
        )
        == []
    )


def test_an_empty_diff_passes():
    assert unreleased_paths([]) == []


# --- the CLI ---------------------------------------------------------------------------------


def _run(paths: list[str]):
    return subprocess.run(
        [sys.executable, str(GATE)],
        input="\n".join(paths),
        capture_output=True,
        text=True,
    )


def test_cli_exits_1_and_names_the_offender():
    r = _run(["plugins/agami/scripts/sm"])
    assert r.returncode == 1
    assert "plugins/agami/scripts/sm" in r.stdout
    # The failure must say how to clear it, including the waiver — a gate whose message doesn't
    # name its own escape hatch gets cleared by deleting the job.
    assert "no-changelog" in r.stdout


def test_cli_exits_0_when_the_changelog_moved():
    r = _run(["plugins/agami/scripts/sm", CHANGELOG])
    assert r.returncode == 0


def test_cli_ignores_blank_lines():
    # `git diff --name-only` output piped through a shell can carry a trailing newline.
    r = _run(["plugins/agami/scripts/sm", "", "  ", CHANGELOG])
    assert r.returncode == 0
