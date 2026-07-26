#!/usr/bin/env python3
"""Cross-platform dev task runner for agami-core.

Run a task with:  uv run dev.py <task>   (works the same on macOS, Linux, Windows).

The ONLY prerequisite is `uv` (https://docs.astral.sh/uv/). Every task shells out to
`uvx`, which fetches ruff / pre-commit / pytest on demand — so there is nothing else to
install globally. These tasks mirror the CI gate (.github/workflows/ci.yml); CI is the
unbypassable version that runs on every PR.

Tasks:
  setup   wire the local pre-commit hooks (ruff + gitleaks on commit; tests on push)
  check   the full local gate: ruff lint + tests + gitleaks (what CI runs)
  test    just the test suite
  lint    just ruff (lint + format check)
  fmt     apply ruff's auto-formatter to the tree
  cover   patch coverage — are the lines you changed covered by a test?
  sync-lib  regenerate plugins/agami/lib/ (the vendored slice the plugin scripts import)
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

RUFF = ["uvx", "ruff@0.15.19"]
# The suite imports the agami-core library, so install it editable with the [model]
# extra (pydantic/pyyaml/sqlglot). DB drivers are omitted on purpose — those tests skip without a DB.
TEST_DEPS = ["--with", "pytest-cov", "--with-editable", "packages/agami-core[model,server]"]
TARGETS = ["plugins", "packages", "tests", "dev.py"]

_ROOT = Path(__file__).resolve().parent
# The plugin's runtime scripts import a small stdlib-only slice of the agami-core library. The
# marketplace ships only plugins/agami/ (no packages/, no pip install), so that slice is vendored —
# drift-checked — into plugins/agami/lib/ so the scripts resolve it there. Source of truth stays the
# package; `sync-lib` regenerates the copy and `check` fails on drift.
_LIB_SRC = _ROOT / "packages" / "agami-core" / "src"
_LIB_DST = _ROOT / "plugins" / "agami" / "lib"
# The module-load import closure of the 4 scripts (all stdlib-only). tests/test_plugin_lib_resolution.py
# fails if a script gains a module-level import that's missing here; a new *lazy* import from the package
# would also need adding to this list.
_VENDORED = ["agami_paths.py", "execute_sql.py", "sql_guard.py", "guardrail.py", "semantic_model/__init__.py", "semantic_model/units.py"]


def run(cmd: list[str], *, allow_fail: bool = False) -> int:
    """Echo and run a command; return its exit code (0 == ok)."""
    print(f"\n$ {' '.join(cmd)}")
    code = subprocess.run(cmd).returncode
    if code and not allow_fail:
        print(f"  -> failed (exit {code})")
    return code


def lint() -> int:
    rc = run([*RUFF, "check", *TARGETS])
    # format is informational for now (the tree has an unformatted backlog) — report, don't fail.
    run([*RUFF, "format", "--check", *TARGETS], allow_fail=True)
    return rc


def fmt() -> int:
    return run([*RUFF, "format", *TARGETS])


def test() -> int:
    return run(["uvx", *TEST_DEPS, "pytest", "tests/", "-q"])


def secrets() -> int:
    return run(["uvx", "pre-commit", "run", "gitleaks", "--all-files"])


def sync_lib() -> int:
    """Regenerate plugins/agami/lib/ from packages/agami-core/src (the vendored closure the scripts import)."""
    for rel in _VENDORED:
        dst = _LIB_DST / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_LIB_SRC / rel, dst)
        print(f"  synced {dst.relative_to(_ROOT)}")
    return 0


def _lib_drift() -> int:
    """Fail if plugins/agami/lib/ has drifted from packages/agami-core/src (someone edited the source)."""
    drifted = [
        rel
        for rel in _VENDORED
        if not (_LIB_DST / rel).exists() or not filecmp.cmp(_LIB_SRC / rel, _LIB_DST / rel, shallow=False)
    ]
    if drifted:
        print(f"\n$ lib drift check\n  ✗ plugins/agami/lib is stale: {', '.join(drifted)}"
              "\n    run: uv run dev.py sync-lib")
        return 1
    return 0


def check() -> int:
    """ruff lint + tests + gitleaks + the vendored-lib drift check — the same checks CI gates on."""
    rc = lint()
    rc |= test()
    rc |= secrets()
    rc |= _lib_drift()
    print("\n✓ all checks passed" if rc == 0 else "\n✗ some checks failed")
    return rc


def cover() -> int:
    """Coverage of the lines THIS branch changed (fails on untested changed lines)."""
    # Make sure origin/main exists locally (fresh clones / worktrees may not have it).
    run(["git", "fetch", "--quiet", "origin", "main"], allow_fail=True)
    rc = run(["uvx", *TEST_DEPS, "pytest", "tests/", "-q",
              "--cov=plugins", "--cov=packages/agami-core/src", "--cov-report=xml"])
    return rc or run(["uvx", "diff-cover", "coverage.xml", "--compare-branch=origin/main"])


def setup() -> int:
    """Install the local pre-commit hooks (convenience; CI is the real gate)."""
    return run(
        ["uvx", "pre-commit", "install", "--hook-type", "pre-commit", "--hook-type", "pre-push"]
    )


TASKS = {"setup": setup, "check": check, "test": test, "lint": lint, "fmt": fmt, "cover": cover,
         "sync-lib": sync_lib}


def main() -> int:
    if shutil.which("uvx") is None:
        print(
            "uv is required and was not found on PATH.\n"
            "  macOS / Linux : curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            '  Windows       : powershell -c "irm https://astral.sh/uv/install.ps1 | iex"\n'
            "  docs          : https://docs.astral.sh/uv/getting-started/installation/"
        )
        return 1
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    if task not in TASKS:
        print(f"usage: uv run dev.py [{' | '.join(TASKS)}]")
        return 2
    return TASKS[task]()


if __name__ == "__main__":
    # Normalize to a clean 0/1 (a signal-killed child returns a negative code).
    raise SystemExit(0 if main() == 0 else 1)
