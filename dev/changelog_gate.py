#!/usr/bin/env python3
"""Fail a PR that changes what we ship without saying so in CHANGELOG.md.

The gate exists because of what 0.7.0 cost. Ninety commits landed between v0.6.9 and v0.7.0 and not
one carried a changelog entry, so a minor release — two new skills, four new library modules, a new
CI exit contract — arrived with an EMPTY `[Unreleased]` section and no notes at all. Nothing told a
user that `/agami-eval` or `/agami-save-golden` existed. Reconstructing that from nine merged PRs at
release time is both slow and worse: the PR bodies were excellent and the release notes still came
out with a flag that does not exist in them, because a summary written weeks later describes what
someone remembers building rather than what shipped.

Writing the entry in the PR that ships the change is the only time the author has the context to
write it correctly, and this gate is what makes that the path of least resistance.

Stdlib only, and a pure function underneath the CLI: CI hands it the changed paths on stdin, and
`tests/test_changelog_gate.py` calls `unreleased_paths` directly.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

CHANGELOG = "CHANGELOG.md"

# What "we ship" means, as prefixes. `plugins/` is the marketplace plugin — skills, scripts, shared
# templates — and `packages/` is the pip-installable library. A change under either reaches a user's
# machine on the next install; everything else in the tree (tests, docs, dev tooling, workflows)
# does not, and is deliberately NOT gated. Keeping the watched set this narrow is what stops the
# gate becoming noise a contributor learns to bypass on reflex.
SHIPPED_PREFIXES = ("plugins/", "packages/")

# The label that waives the gate. Deliberately a label rather than a magic string in the title or
# body: a label is a separate, visible act that survives a force-push and shows up in the PR's
# timeline, so waiving is auditable rather than invisible.
WAIVER_LABEL = "no-changelog"


def _is_test(path: str) -> bool:
    """Tests ship to nobody. `tests/` is the repo's suite; `/tests/` catches a package's own."""
    return path.startswith("tests/") or "/tests/" in path


def unreleased_paths(paths: Iterable[str]) -> list[str]:
    """The changed paths that reach a user with no changelog entry to explain them.

    Empty means the gate passes — either nothing shipped changed, or CHANGELOG.md moved with it.
    Returns the offending paths (sorted) rather than a bool so the CI failure can name them; being
    told *which* file tripped the gate is the difference between fixing it and re-running it.
    """
    paths = list(paths)
    if CHANGELOG in paths:
        return []
    return sorted(p for p in paths if p.startswith(SHIPPED_PREFIXES) and not _is_test(p))


def main() -> int:
    changed = [line.strip() for line in sys.stdin if line.strip()]
    offenders = unreleased_paths(changed)
    if not offenders:
        print("✓ changelog gate: nothing shipped changed, or CHANGELOG.md moved with it")
        return 0

    shown = offenders[:20]
    print("✗ changelog gate: these change what we ship, but CHANGELOG.md was not touched:\n")
    for p in shown:
        print(f"    {p}")
    if len(offenders) > len(shown):
        print(f"    … and {len(offenders) - len(shown)} more")
    print(
        f"\nAdd an entry under ## [Unreleased] in {CHANGELOG} describing the user-visible change.\n"
        f"If there genuinely isn't one — an internal refactor, a comment, a typo in a docstring —\n"
        f"apply the `{WAIVER_LABEL}` label to the PR and this check will pass."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
