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
import logging
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
    """Exit 1 is not exclusively the read-only guard's, and the parser claims a stream only when it
    finds a payload carrying `refusal` — never on the code alone.

    That is what lets a diagnostic-only stream fall through to the generic error path, and it is
    also why the four unconverted branches survive the envelope rewrite: on the wire their
    `{"error": …}` line is now FOLLOWED by `main`'s refusal line, and the line scan reads the
    refusal past the diagnostic rather than choking on the mixed stream. (That end-to-end shape is
    pinned in tests/test_ace035_envelope.py; here we pin the parser's half of it.)"""
    # `sensitive_columns` is one of the four `_model_safety` branches deliberately left on the old
    # shape (the scope gates and both model_unavailable sites have since moved to `{"refusal": …}`),
    # so it is a live example of the fall-through rather than a historical one.
    assert tools._stderr_refusal(1, '{"error": {"kind": "sensitive_columns"}}') is None
    assert tools._stderr_refusal(1, "Postgres connect failed: refused") is None
    assert tools._stderr_refusal(1, "") is None
    # A refusal shape on a non-guard exit code is not a refusal either.
    assert tools._stderr_refusal(4, '{"refusal": {"reason": "unsafe", "rule": "read_only", '
                                    '"detail": "d", "remediation": "r"}}') is None


def test_parent_rejects_a_malformed_refusal() -> None:
    """Rebuilding through `Refusal` re-checks the contract on this side of the boundary, so a child
    that emitted an unknown reason or an empty remediation falls back to the generic error path
    rather than being relayed as a valid refusal.

    `non_string_detail` is the case that used to escape rather than fall back: `__post_init__`
    validates every field by calling `.strip()`, so a non-string raises `AttributeError`, which was
    outside the caught set. The fallback this parser documents therefore did not exist for a third
    of the ways the payload can be wrong — unreachable today, since only our own child writes that
    stream, but the promise is the reason the parser rebuilds at all.
    """
    bad_reason = '{"refusal": {"reason": "nope", "rule": "read_only", "detail": "d", "remediation": "r"}}'
    empty_fix = '{"refusal": {"reason": "unsafe", "rule": "read_only", "detail": "d", "remediation": " "}}'
    extra_field = ('{"refusal": {"reason": "unsafe", "rule": "read_only", "detail": "d", '
                   '"remediation": "r", "surprise": 1}}')
    non_string_detail = ('{"refusal": {"reason": "unsafe", "rule": "read_only", "detail": 3, '
                         '"remediation": "r"}}')
    non_string_rule = ('{"refusal": {"reason": "unsafe", "rule": null, "detail": "d", '
                       '"remediation": "r"}}')
    for line in (bad_reason, empty_fix, extra_field, non_string_detail, non_string_rule):
        assert tools._stderr_refusal(1, line) is None, line


# ---------------------------------------------------------------------------
# The other half of the wire: what a NON-refusal exit puts in `failure.message`
# ---------------------------------------------------------------------------


def test_the_childs_own_config_diagnostic_is_relayed() -> None:
    """An AUTHORED exit carries text the child wrote and the caller needs.

    Exit 2 and 3 are the codes whose message this module composed — a missing driver, or the
    credential remediation naming the env var to set. They contain nothing the database said, so
    relaying them is what keeps the two execution paths saying the same thing: the in-process path
    surfaces the same string from `ExecutorError.msg` (pinned in tests/test_ah012_executor_seam.py).

    Codes 4 and 5 USED to be asserted here too, on the reading that they were equally the child's
    own. ACE-039 separated them: those carry the driver's exception, so the child replaces them with
    a fixed sentence and the parent rebuilds that sentence from the exit code rather than relaying
    the stream. That case now lives in `test_the_sanitized_band_is_rebuilt_not_relayed` below.
    """
    detailed = ("No warehouse credentials for profile [acme]. Set DATASOURCE_URL "
                "(or DATASOURCE_URL__ACME) in the environment.")
    assert tools._child_failure_message(2, detailed) == detailed
    driver_missing = "pymysql not installed. Run: pip install pymysql"
    assert tools._child_failure_message(3, driver_missing) == driver_missing


@pytest.mark.parametrize("code", [4, 5, 7, 8, 9, 10])
def test_the_sanitized_band_is_rebuilt_not_relayed(code: int) -> None:
    """For a driver-originated failure the parent reconstructs; it never reads the child's stream.

    The child's stderr is SHARED — the model-safety pass writes its own notices there before the
    failure line — so relaying it handed the caller text it never sent. Since the child derives its
    message from the kind, the exit code alone is enough to rebuild the identical sentence, which
    makes the stream irrelevant to the answer rather than merely filtered.
    """
    import execute_sql

    kind = execute_sql.EXIT_TO_FAILURE_KIND[code]
    noisy = (f"[agami] auto-corrected fan_out: ran rewritten SQL. joined t on tenant_shard\n"
             f"{execute_sql._ERROR_MESSAGES[kind]}")
    assert tools._child_failure_message(code, noisy) == execute_sql._ERROR_MESSAGES[kind]
    assert "tenant" not in tools._child_failure_message(code, noisy)


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        # A Python-level crash in the child: exit 1 with no refusal object on the stream. The
        # concrete case — a credentials file with no section header — used to put this whole thing,
        # absolute paths and all, into a field the caller is shown.
        (1, 'Traceback (most recent call last):\n'
            '  File "/Users/someone/agami/execute_sql.py", line 266, in _load_credentials\n'
            '    cfg.read(CREDENTIALS_PATH)\n'
            'configparser.MissingSectionHeaderError: File contains no section headers.\n'
            "file: '/Users/someone/agami-artifacts/local/credentials', line: 1"),
        # A signal, or any code outside the CLI's table: the child never classified anything.
        (-11, "Segmentation fault"),
        (70, "something upstream decided this"),
        # Belt and braces: a traceback on a classified code is still not relayed.
        (2, 'Traceback (most recent call last):\n  File "/private/x.py", line 1, in <module>\n'
            "ValueError: boom"),
    ],
    ids=["python crash", "signal", "unknown code", "traceback on a classified code"],
)
def test_an_unstructured_child_stream_never_reaches_the_caller(code, stderr, caplog) -> None:
    """Anything the child did not classify is replaced, and logged instead.

    `failure.message` is shown to the caller, and a traceback in it discloses the absolute path of
    every frame — which is a real disclosure, not a formatting complaint. The rule is structural
    (was this an exit code the child produces from a `Failure`?) with a traceback check behind it,
    rather than a scrub of the text, because scrubbing text you did not author is a guess.
    """
    from execute_sql import UNEXPECTED_FAILURE_MESSAGE

    with caplog.at_level(logging.ERROR):
        message = tools._child_failure_message(code, stderr)

    assert message == UNEXPECTED_FAILURE_MESSAGE
    assert "Traceback" not in message and "/" not in message
    # Suppressed for the caller, kept for the operator — the raw stream is what makes a child that
    # died unclassifiably debuggable at all.
    logged = [r for r in caplog.records if r.name == "tools" and r.levelname == "ERROR"]
    assert len(logged) == 1 and stderr in logged[0].getMessage()


def test_a_silent_child_still_gets_a_message() -> None:
    """`Failure.message` has no meaningful empty value, and an empty stream is exactly the case
    where there is nothing to relay. Nothing is logged either — there is no raw text to keep."""
    from execute_sql import UNEXPECTED_FAILURE_MESSAGE

    assert tools._child_failure_message(2, "") == UNEXPECTED_FAILURE_MESSAGE
    assert tools._child_failure_message(1, None) == UNEXPECTED_FAILURE_MESSAGE
