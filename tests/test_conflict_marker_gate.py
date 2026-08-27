"""Guard: the conflict-marker gate actually gates.

`dev/check_conflict_markers.py` is a CI-blocking check whose only failure mode that
matters is the quiet one — reporting a pass over files it never read. Every case below
was a real silent bypass at some point in review, so each is a regression lock rather
than a hypothetical.

Two conventions here are deliberate:

* **Markers are built at runtime** (`"<" * 7`), never written as literals. A literal at
  column 0 in this file would be found by the gate scanning its own tree; indentation
  inside a triple-quoted string protects it only until someone reflows the file.
* **The script is driven as a subprocess with `cwd=` a throwaway git repo.** That scopes
  `git ls-files` to the fixture and keeps the real tree out of it. `git add -A` alone is
  enough — `git ls-files` reads the index, and committing would need a git identity that
  CI runners don't have.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "dev" / "check_conflict_markers.py"

# Built at runtime so this file never contains a marker at column 0. `git merge` writes a
# label after the run; a hand-resolved conflict often doesn't, so both forms are tested.
OPEN = "<" * 7
BASE = "|" * 7
SEP = "=" * 7
CLOSE = ">" * 7

CONFLICT = f"keep\n{OPEN} HEAD\nmine\n{SEP}\ntheirs\n{CLOSE} feature\n"


def run_gate(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def track(repo: Path, name: str, content: str | bytes = CONFLICT) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return path


# --- the core asymmetric rule -------------------------------------------------------


def test_clean_tree_passes(repo):
    track(repo, "a.py", "print('hello')\n")
    result = run_gate(repo)
    assert result.returncode == 0, result.stderr
    assert "1 tracked files scanned" in result.stdout  # the denominator, so "scanned nothing" can't read as a pass


def test_full_conflict_block_fails_and_names_every_line(repo):
    track(repo, "a.py")
    result = run_gate(repo)
    assert result.returncode == 1
    for lineno in (2, 4, 6):  # open, separator, close
        assert f"a.py:{lineno}" in result.stderr, result.stderr


@pytest.mark.parametrize("marker", [OPEN, CLOSE, f"{BASE} ancestor"])
def test_each_unambiguous_marker_alone_fails(repo, marker):
    """A partial resolution leaves one marker behind; each must fail on its own."""
    track(repo, "a.md", f"text\n{marker}\nmore\n")
    assert run_gate(repo).returncode == 1


def test_bare_separator_alone_is_a_setext_heading_not_a_conflict(repo):
    """The false-positive this gate's asymmetry exists to avoid."""
    track(repo, "doc.md", f"A Heading\n{SEP}\n\nprose\n")
    result = run_gate(repo)
    assert result.returncode == 0, result.stderr


def test_bare_separator_is_scoped_per_file_not_per_tree(repo):
    """Load-bearing: a conflict in one file must not indict a setext heading in another."""
    track(repo, "conflicted.py", CONFLICT)
    track(repo, "innocent.md", f"A Heading\n{SEP}\n\nprose\n")
    result = run_gate(repo)
    assert result.returncode == 1
    assert "conflicted.py" in result.stderr
    assert "innocent.md" not in result.stderr, result.stderr


def test_indented_marker_does_not_fire(repo):
    """Markers anchor at column 0 — this is what lets docs discuss them when indented."""
    track(repo, "a.md", f"text\n  {OPEN} HEAD\n  {CLOSE} other\n")
    assert run_gate(repo).returncode == 0


# --- the silent-bypass regressions --------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "café.py",  # non-ASCII filename: core.quotePath C-quotes it out of existence
        "docs/日本語/readme.md",  # non-ASCII *directory* hid its whole subtree
        "back\\slash.py",
        "q'uote.py",
    ],
)
def test_unusual_paths_are_still_scanned(repo, name):
    """Regression: `git ls-files` without `-z` returns an unopenable C-quoted string."""
    if name != os.fsdecode(os.fsencode(name)):
        pytest.skip("filesystem cannot represent this name")
    track(repo, name)
    result = run_gate(repo)
    assert result.returncode == 1, f"{name} was silently skipped:\n{result.stdout}"


def test_non_utf8_text_file_is_still_scanned(repo):
    """Regression: one latin-1 byte used to discard the whole file's scan."""
    track(repo, "legacy.py", CONFLICT.encode() + b"# author: Jos\xe9\n")
    result = run_gate(repo)
    assert result.returncode == 1, result.stdout
    assert "legacy.py" in result.stderr


def test_utf16_file_is_treated_as_binary_like_git_does(repo):
    """UTF-16 is skipped, and that is correct rather than a gap.

    The NUL-byte probe in `scan()` is deliberately the same rule git itself uses. Verified
    against a real `git merge` of two UTF-16 files: git declines the text merge and leaves
    our side in place, writing NO markers at all. So a UTF-16 file cannot carry a
    git-authored conflict, and skipping it costs nothing. Contrast `legacy.py` above —
    latin-1 has no NUL bytes, so git *does* text-merge it and *does* write markers.
    """
    track(repo, "win.md", CONFLICT.encode("utf-16"))
    assert run_gate(repo).returncode == 0


def test_unreadable_file_fails_rather_than_reporting_clean(repo):
    """A file we could not read is unknown, not clean — the two must not share an answer."""
    path = track(repo, "locked.py")
    path.chmod(0o000)
    try:
        result = run_gate(repo)
        assert result.returncode == 1
        assert "could not be read" in result.stderr, result.stderr
        assert "locked.py" in result.stderr
    finally:
        path.chmod(0o644)  # else tmp_path cleanup fails


def test_widened_conflict_marker_size_is_matched(repo):
    """`.gitattributes` can set conflict-marker-size; git then writes longer runs."""
    wide = f"keep\n{'<' * 20} HEAD\nmine\n{'=' * 20}\ntheirs\n{'>' * 20} other\n"
    track(repo, "a.txt", wide)
    assert run_gate(repo).returncode == 1


def test_binary_file_is_skipped_not_crashed(repo):
    track(repo, "blob.bin", b"\x00\x01\x02" + CONFLICT.encode())
    assert run_gate(repo).returncode == 0


def test_untracked_file_cannot_fail_the_build(repo):
    """Scratch files must not break the hook, or developers start using --no-verify."""
    track(repo, "tracked.py", "ok\n")
    (repo / "scratch.py").write_text(CONFLICT)  # deliberately not git-added
    assert run_gate(repo).returncode == 0


def test_run_from_subdirectory_still_scans_whole_tree(repo):
    """`git ls-files` is cwd-relative; a subdir run used to pass having scanned nothing."""
    track(repo, "top.py", CONFLICT)
    sub = repo / "sub"
    sub.mkdir(exist_ok=True)
    track(repo, "sub/clean.py", "ok\n")
    result = run_gate(sub)
    assert result.returncode == 1, result.stdout
    assert "top.py" in result.stderr


def test_empty_tree_refuses_to_report_a_pass(repo):
    result = run_gate(repo)
    assert result.returncode == 1
    assert "no tracked files" in result.stderr


def test_diff3_residue_after_removing_only_what_was_reported(repo):
    """The message says remove *every* marker; the base marker must be one of them.

    Reported lines used to omit `|||||||`, so a developer who deleted exactly what was
    printed shipped the base version's content under a gate that then went green.
    """
    track(repo, "a.md", f"keep\n{BASE} ancestor\nBASE CONTENT\n{SEP}\ntheirs\n")
    result = run_gate(repo)
    assert result.returncode == 1
    assert "a.md:2" in result.stderr, result.stderr


# --- the gate is actually wired up --------------------------------------------------


def test_ci_job_invokes_the_script():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = wf["jobs"]["hygiene"]["steps"]
    assert any("dev/check_conflict_markers.py" in s.get("run", "") for s in steps), steps


def test_precommit_hook_invokes_the_script():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = [h for r in cfg["repos"] for h in r["hooks"] if h["id"] == "conflict-markers"]
    assert hooks, "the conflict-markers hook is gone"
    assert "dev/check_conflict_markers.py" in hooks[0]["entry"]


def test_gate_passes_on_this_repo():
    """Self-scan. Also catches the tempting `startswith` -> `in` 'fix', which would make
    the script fail on its own docstring — a failure you cannot resolve by fixing code."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
