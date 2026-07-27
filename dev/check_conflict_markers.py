#!/usr/bin/env python3
"""Fail if a merge-conflict marker survived into a tracked file.

Why this exists as its own check rather than pre-commit's `check-merge-conflict`:
that hook only inspects files **while a merge is in progress**, so a marker that is
committed and only noticed later — or one that arrives on a branch you merged — walks
straight past it. This runs over the tracked tree unconditionally, in CI, where it
cannot be skipped with `--no-verify`.

The matching rule is deliberately asymmetric, because two of the four markers a
conflict can leave behind are ambiguous:

  open / close (a `<` or `>` run)   never occur in legitimate content -> always an error.
  base-with-label (`|` run + text)  likewise -> always an error.
  a bare `=` run                    is also a Markdown setext heading underline (`Title`
                                    on one line, the rule under it), so flagging it
                                    unconditionally would false-positive on prose.
  a bare `|` run                    is also a degenerate Markdown table row of empty cells.

The two bare forms are reported only when the same file also carries an unambiguous
marker. That asymmetry is the point: a real conflict always leaves at least one
unambiguous marker, so the ambiguous ones become *attributable* rather than guessed at.

Three states, never two: markers -> fail; clean -> pass; unreadable -> fail loudly.
A file we could not read is *unknown*, not clean, and a gate must never collapse those
two into the same answer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# Marker runs are 7 characters by default, but `.gitattributes` can widen them via
# `conflict-marker-size`, so match a run of 7-or-more. The trailing label ("HEAD",
# "origin/main") is optional: git always writes one, hand-resolved conflicts often don't.
UNAMBIGUOUS = (
    re.compile(rb"^<{7,}(?: .*)?$"),  # ours
    re.compile(rb"^>{7,}(?: .*)?$"),  # theirs
    re.compile(rb"^\|{7,} .+$"),  # diff3/zdiff3 common ancestor, WITH a label
)
AMBIGUOUS = (
    re.compile(rb"^={7,}$"),  # also a Markdown setext heading underline
    re.compile(rb"^\|{7,}$"),  # also a Markdown table row of empty cells
)

# Suffixes we skip outright. This is a fast path, NOT the binary defence — the
# authoritative test is the NUL-byte probe in `scan()`, so an unlisted binary type is
# still handled correctly instead of being silently mis-scanned.
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".zip", ".gz", ".bz2", ".xz",
    ".7z", ".jar", ".class", ".so", ".dylib", ".dll", ".exe", ".pyc", ".whl",
    ".parquet", ".xlsx", ".docx", ".pptx", ".db", ".sqlite", ".mp4", ".mov",
)

BINARY_SNIFF_BYTES = 8192


def _git_bytes(*args: str) -> bytes:
    """Run a git command, surfacing git's own message rather than a bare exit code."""
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        sys.exit(f"✗ git {' '.join(args)} failed: {detail}")
    return proc.stdout


def repo_root() -> str:
    """The repo's top level, so a run from a subdirectory still scans the whole tree.

    `git ls-files` is cwd-relative: invoked from a clean subdirectory it lists only that
    subtree, and the gate would print a reassuring pass having scanned almost nothing.
    """
    return _git_bytes("rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()


def tracked_files() -> list[str]:
    """Every file git tracks, so an untracked scratch file cannot fail the build.

    `-z` is load-bearing: without it `core.quotePath` (on by default) C-quotes any path
    holding a non-ASCII byte, a control character, a quote or a backslash — turning
    `café.py` into the literal `"caf\\303\\251.py"`, which is not an openable path. The
    file would then be skipped silently, and a non-ASCII *directory* would hide its
    entire subtree.
    """
    raw = _git_bytes("ls-files", "-z")
    paths = [os.fsdecode(p) for p in raw.split(b"\0") if p]
    # An unmerged index lists a path once per stage; collapse while preserving order.
    unique = list(dict.fromkeys(paths))
    return [p for p in unique if not p.lower().endswith(SKIP_SUFFIXES)]


def scan(path: str) -> tuple[list[tuple[int, str]], str | None]:
    """Return (findings, error) for one file — exactly one of the two is meaningful.

    Matching runs on BYTES: the markers are pure ASCII, so decoding buys nothing and
    costs correctness. A single latin-1 or UTF-16 byte anywhere in an otherwise ordinary
    text file makes a whole-file strict decode raise, which would discard the scan.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc.strerror or exc}"
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return [], None  # genuinely binary — no text to match
    lines = [line.rstrip(b"\r") for line in data.split(b"\n")]
    hard = [
        (n, line) for n, line in enumerate(lines, 1) if any(p.match(line) for p in UNAMBIGUOUS)
    ]
    if not hard:
        # No unambiguous marker, so a bare `=` run here is a setext heading, not a conflict.
        return [], None
    soft = [(n, line) for n, line in enumerate(lines, 1) if any(p.match(line) for p in AMBIGUOUS)]
    return [(n, line.decode("utf-8", "replace")) for n, line in sorted(hard + soft)], None


def main() -> int:
    os.chdir(repo_root())
    paths = tracked_files()
    if not paths:
        print("✗ no tracked files to scan — refusing to report a pass", file=sys.stderr)
        return 1

    findings: list[tuple[str, int, str]] = []
    unreadable: list[tuple[str, str]] = []
    for path in paths:
        hits, error = scan(path)
        if error:
            unreadable.append((path, error))
        findings.extend((path, n, line) for n, line in hits)

    if not findings and not unreadable:
        print(f"✓ no merge-conflict markers ({len(paths)} tracked files scanned)")
        return 0

    if findings:
        print("✗ merge-conflict markers found in tracked files:\n", file=sys.stderr)
        for path, n, line in findings:
            print(f"  {path}:{n}: {line[:80]}", file=sys.stderr)
        print(
            "\nResolve the conflict and remove every marker before committing"
            " — a diff3/zdiff3 conflict leaves four, not three.",
            file=sys.stderr,
        )
    if unreadable:
        # Unknown, not clean. Fail rather than certify a file we never actually read.
        print("\n✗ tracked files could not be read, so they were not checked:\n", file=sys.stderr)
        for path, error in unreadable:
            print(f"  {path}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
