#!/usr/bin/env python3
"""Fail if a merge-conflict marker survived into a tracked file.

Why this exists as its own check rather than pre-commit's `check-merge-conflict`:
that hook only inspects files **while a merge is in progress**, so a marker that is
committed and only noticed later — or one that arrives on a branch you merged — walks
straight past it. This runs over the tracked tree unconditionally, in CI, where it
cannot be skipped with `--no-verify`.

The matching rule is deliberately asymmetric, because one of the three markers is
ambiguous:

  `<<<<<<< ` and `>>>>>>> `  never occur in legitimate content -> always an error.
  `=======`                  is a valid Markdown setext heading underline
                             (`Title` on one line, `=======` under it), so flagging it
                             unconditionally would false-positive on ordinary prose.
                             It is only reported when the same file also carries one of
                             the unambiguous markers.

That asymmetry is the point: a real conflict always leaves at least one unambiguous
marker, and the ambiguous one is then attributable rather than guessed at.
"""

from __future__ import annotations

import subprocess
import sys

# The two markers that are never legitimate content, and the ambiguous third.
UNAMBIGUOUS = ("<<<<<<< ", ">>>>>>> ")
AMBIGUOUS = "======="

# Binary-ish suffixes we should not try to decode.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".gz")


def tracked_files() -> list[str]:
    """Every file git tracks, so an untracked scratch file cannot fail the build."""
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if p and not p.endswith(SKIP_SUFFIXES)]


def scan(path: str) -> list[tuple[int, str]]:
    """Return (line_number, line) for each conflict marker in one file."""
    try:
        text = open(path, encoding="utf-8", errors="strict").read()
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable — nothing meaningful to match
    lines = text.splitlines()
    hard = [
        (n, line)
        for n, line in enumerate(lines, 1)
        if any(line.startswith(m) for m in UNAMBIGUOUS)
    ]
    if not hard:
        return []  # no unambiguous marker -> a bare `=======` is a setext heading, not a conflict
    soft = [(n, line) for n, line in enumerate(lines, 1) if line.rstrip() == AMBIGUOUS]
    return sorted(hard + soft)


def main() -> int:
    findings = [(p, n, line) for p in tracked_files() for n, line in scan(p)]
    if not findings:
        print("✓ no merge-conflict markers in tracked files")
        return 0
    print("✗ merge-conflict markers found in tracked files:\n", file=sys.stderr)
    for path, n, line in findings:
        print(f"  {path}:{n}: {line[:80]}", file=sys.stderr)
    print(
        "\nResolve the conflict and remove every marker before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
