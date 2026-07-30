"""The read-only gate speaks the guardrail contract — proved over the WHOLE reject corpus.

`tests/test_sql_guard.py` proves *whether* each vector is rejected. This file proves *what the
caller gets* when it is: a `Refusal` carrying the read-only rule, the pinned reason, a non-empty
detail, and a remediation that is one of the gate's static, per-rejection fixes.

Two properties are worth stating explicitly, because both are easy to satisfy in form and lose in
substance:

  1. **Exhaustive, not sampled.** The corpus is imported from `test_sql_guard` rather than copied,
     so a vector added there is held to the contract here on the next run — the two cannot drift.

  2. **Per-rejection, not generic.** Every remediation the corpus produces must be one of the
     `_REMEDIATION` entries, and every `_REMEDIATION` entry must be produced by the corpus. A single
     catch-all fix would satisfy "remediation is mandatory" while telling the caller nothing, and
     an entry no vector reaches is a fix nobody can act on. Requiring the two sets to match exactly
     is what makes the mandatory-remediation rule mean something.

Membership in that static set is also the value-free proof: a remediation built by interpolating
what IS allowed would enumerate the declared surface, and no static string can.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import tools
from guardrail import RULE_READ_ONLY
from sql_guard import _MAX_SQL_CHARS, _REMEDIATION, check_read_only

from test_sql_guard import REJECT_CORPUS, over_length_payload

# The over-length payload is generated rather than a literal, so it is not in any of the corpus
# lists; append it here so the length-cap rejection is covered like every other one.
FULL_CORPUS = [*REJECT_CORPUS, over_length_payload()]


def test_corpus_is_the_whole_reject_surface() -> None:
    """A floor on the corpus size, so a refactor that accidentally empties a list is loud.

    Not an exact count — the corpus is meant to grow — but far enough below today's size that only
    real loss trips it."""
    assert len(FULL_CORPUS) > 120, len(FULL_CORPUS)


@pytest.mark.parametrize("sql", FULL_CORPUS, ids=lambda s: repr(s)[:60])
def test_every_rejection_is_a_read_only_refusal(sql: object) -> None:
    refusal = check_read_only(sql)  # type: ignore[arg-type]  # the corpus includes None on purpose
    assert refusal is not None, f"Expected rejection: {sql!r}"
    assert refusal.rule == RULE_READ_ONLY
    assert refusal.reason == "unsafe"
    assert refusal.detail.strip()
    assert refusal.remediation.strip()
    # Static prose from the table — never assembled from the statement, so it cannot leak a value
    # or enumerate what would have been allowed.
    assert refusal.remediation in set(_REMEDIATION.values()), refusal.remediation


def test_every_remediation_is_reachable_and_none_is_generic() -> None:
    produced = {check_read_only(sql).remediation for sql in FULL_CORPUS}  # type: ignore[union-attr]
    assert produced == set(_REMEDIATION.values())
    # The table's values are distinct, so the set equality above also proves the gate hands back a
    # different fix per rejection rather than one string wearing nine names.
    assert len(set(_REMEDIATION.values())) == len(_REMEDIATION)


def test_length_cap_remediation_tracks_the_cap() -> None:
    """The remediation quotes the cap, so it must be derived from `_MAX_SQL_CHARS` rather than
    typed in — otherwise raising the cap would leave the caller acting on a stale number."""
    assert f"{_MAX_SQL_CHARS:,}" in _REMEDIATION["too_long"]
    refusal = check_read_only(over_length_payload())
    assert refusal is not None and refusal.remediation == _REMEDIATION["too_long"]


# ---------------------------------------------------------------------------
# The wire: the forked executor's stderr, and the parent's reconstruction of it.
# ---------------------------------------------------------------------------


def _run_executor(sql: str, tmp_path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "nonexistent", "--sql", sql],
        capture_output=True,
        text=True,
        timeout=60,
        # Isolate the artifacts dir so bootstrap() never touches the real home dir.
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(tmp_path)},
    )


def test_refusal_stderr_is_exactly_one_json_object(tmp_path) -> None:
    """One object, one line, nothing else. The parent parses this stream, so a stray diagnostic
    line around the refusal would make it unreadable rather than merely noisy."""
    proc = _run_executor("DROP TABLE secrets", tmp_path)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.endswith("\n") and proc.stderr.count("\n") == 1
    payload = json.loads(proc.stderr)  # parses WHOLE → a single object, not a line among many
    assert set(payload) == {"refusal"}
    assert set(payload["refusal"]) == {"reason", "rule", "detail", "remediation"}
    assert payload["refusal"]["rule"] == RULE_READ_ONLY
    assert payload["refusal"]["reason"] == "unsafe"


def test_parent_reconstructs_the_child_refusal(tmp_path) -> None:
    """The refusal survives the process boundary as a refusal — same rule, same remediation as the
    in-process gate produced — instead of arriving as raw stderr text in an error field."""
    proc = _run_executor("SELECT pg_read_file('/etc/passwd')", tmp_path)

    rebuilt = tools._stderr_refusal(proc.returncode, proc.stderr)
    assert rebuilt is not None
    direct = check_read_only("SELECT pg_read_file('/etc/passwd')")
    assert direct is not None
    assert rebuilt == {
        "reason": direct.reason,
        "rule": direct.rule,
        "detail": direct.detail,
        "remediation": direct.remediation,
    }


def test_parent_leaves_a_non_refusal_exit_alone() -> None:
    """Exit 1 is not exclusively the read-only guard's — the semantic-model branches still exit 1
    after writing today's `{"error": …}` line. Only a payload carrying `refusal` is claimed, so the
    unconverted branches keep reaching the generic error path untouched."""
    assert tools._stderr_refusal(1, '{"error": {"kind": "table_out_of_scope"}}') is None
    assert tools._stderr_refusal(1, "Postgres connect failed: refused") is None
    assert tools._stderr_refusal(1, "") is None
    # A refusal shape on a non-guard exit code is not a refusal either.
    assert tools._stderr_refusal(4, '{"refusal": {"reason": "unsafe", "rule": "read_only", '
                                    '"detail": "d", "remediation": "r"}}') is None


def test_parent_rejects_a_malformed_refusal() -> None:
    """Rebuilding through `Refusal` re-checks the contract on this side of the boundary, so a child
    that emitted an unknown reason or an empty remediation falls back to the generic error path
    rather than being relayed as a valid refusal."""
    bad_reason = '{"refusal": {"reason": "nope", "rule": "read_only", "detail": "d", "remediation": "r"}}'
    empty_fix = '{"refusal": {"reason": "unsafe", "rule": "read_only", "detail": "d", "remediation": " "}}'
    extra_field = ('{"refusal": {"reason": "unsafe", "rule": "read_only", "detail": "d", '
                   '"remediation": "r", "surprise": 1}}')
    for line in (bad_reason, empty_fix, extra_field):
        assert tools._stderr_refusal(1, line) is None, line
