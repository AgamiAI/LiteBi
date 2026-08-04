"""The invariants that hold around every verdict, whatever the verdict was.

`test_safety_corpus.py` asks what the chokepoint DECIDED. This file asks what it always carries: a
receipt, an audit id, an audit row, and nothing it should not carry. Those are the properties a
reviewer relies on after the fact, and they are the ones a per-rule test cannot see — a refusal
test that asserts only the refusal leaves half the contract unlocked, because a receipt or an
audit id could quietly stop being attached to a whole status and every rule assertion in the
corpus would stay green.

The corpus is reused as the population, not re-listed: every vector runs here too, so an invariant
claimed "on every outcome" is asserted against every outcome the corpus produces rather than
against a representative someone chose.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (
    TESTS_ROOT,
    Path(__file__).resolve().parent,
    REPO_ROOT / "packages" / "agami-core" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import guardrail  # noqa: E402
import harness  # noqa: E402
import tools  # noqa: E402

from safety.corpus import CASES  # noqa: E402


# A governed vector's own SQL, taken from the corpus rather than written again here: the tests
# below that need a statement every gate allows must use one the corpus agrees is allowed, or they
# would prove their point against a statement that was refused for an unrelated reason.
_GOVERNED_SQL = next(c.sql for c in CASES if c.rule is None)


@pytest.fixture()
def file_path(tmp_path, monkeypatch):
    """The file-served path, built by the slice-1 harness. Three lines of pytest glue around
    `harness.build_file_path`, which is where the model and the warehouse actually come from."""
    yield harness.build_file_path(tmp_path, monkeypatch)
    harness.reset_injected_executor()


def _vectors(predicate=None) -> list:
    """The corpus as parameters. No xfail markers: `red_on_main` is empty on this branch (the
    corpus asserts that itself), and every invariant here holds whichever rule fires anyway."""
    return [
        pytest.param(case, id=case.id)
        for case in CASES
        if case.runs_on(harness.ENGINE) and (predicate is None or predicate(case))
    ]


def _drive(case, monkeypatch, route=None) -> dict:
    """One vector over a route, with the deployment ceiling lowered for the vectors that need it.

    The availability vectors return more rows than the ceiling they are driven under, and every
    other vector needs the ceiling left alone — the governed ones return more rows than the lowered
    value on purpose, so a session-wide setting would turn them into availability refusals.
    """
    if case.rule == guardrail.RULE_RESOURCE_LIMIT:
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))
    return (route or harness.ROUTES["http"])(case.sql)


def _assert_receipt_is_whole(body: dict) -> None:
    """Every section the contract declares is present and shaped.

    Iterated off `guardrail.Receipt.SECTIONS` rather than re-listed, so a section added to the
    contract is covered here the day it lands instead of the day someone remembers this file.
    """
    receipt = body["receipt"]
    for section in guardrail.Receipt.SECTIONS:
        assert section in receipt, (section, receipt)
        assert set(receipt[section]) == {"items", "undetermined"}, (section, receipt)


# ---------------------------------------------------------------------------
# A receipt on refusals too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _vectors(lambda c: c.rule is not None))
def test_a_refused_vector_still_carries_a_whole_receipt(file_path, case, monkeypatch):
    """A refusal is an outcome, and an outcome owes an account of itself.

    `tools._emit` attaches the receipt outside the status branch, so this is structurally true
    today — which is the reason to pin it rather than a reason not to. The branch it sits outside
    of is one edit away from being the branch it sits inside, and the corpus's own rule assertions
    would not notice: they read `refusal`, never `receipt`.

    The refused status is asserted first because without it the receipt assertion is vacuous — an
    `ok` body carries a whole receipt too, so a vector that stopped being refused would pass this.
    Which rule refused it is `test_safety_corpus.py`'s subject, not this file's.
    """
    body = _drive(case, monkeypatch)

    assert body["status"] == "refused", body
    _assert_receipt_is_whole(body)


# ---------------------------------------------------------------------------
# An audit id on every vector, on all three statuses
# ---------------------------------------------------------------------------


def _failed_body(file_path, monkeypatch) -> dict:
    """The third status, provoked without touching a gate.

    The model on disk keeps declaring `orders`, so every gate passes and the statement is a
    governed one — then the warehouse it reaches has no such table and the executor fails. That is
    the shape a caller actually meets on `failed`: a decision to run, followed by a database that
    could not. Injecting a raising executor would produce the same status without the chokepoint
    ever having agreed to execute, which is a different path.
    """
    empty = file_path.warehouse.parent / "empty-warehouse.db"
    sqlite3.connect(empty).close()
    monkeypatch.setenv(f"DATASOURCE_URL__{harness.PROFILE.upper()}", f"sqlite:///{empty}")
    return harness.ROUTES["http"](_GOVERNED_SQL)


@pytest.mark.parametrize("case", _vectors())
def test_every_vector_comes_back_with_an_audit_id(file_path, case, monkeypatch):
    """The id a caller reads off its own answer is the primary key of its own audit row.

    Which makes it the whole of a caller's ability to find the record of what it just did. It is
    attached in the same place as the receipt and for the same reason, so it is asserted over the
    same population: every vector, on whichever status it produces.
    """
    body = _drive(case, monkeypatch)

    assert body["status"] in ("ok", "refused"), body
    assert body["audit_id"], body


def test_a_failed_execution_carries_an_audit_id_and_a_receipt_too(file_path, monkeypatch):
    """`failed` is the status the corpus cannot reach, and the one most likely to be forgotten.

    Every vector above lands on `ok` or `refused` — a corpus of adversarial statements has no
    reason to produce a database that breaks. So the third status gets its own vector here rather
    than being assumed to behave like the two that are covered.
    """
    body = _failed_body(file_path, monkeypatch)

    assert body["status"] == "failed", body
    assert body["audit_id"], body
    _assert_receipt_is_whole(body)


# ---------------------------------------------------------------------------
# The recording pair: refuse when we cannot record, and record everything else
# ---------------------------------------------------------------------------

# Unsupported on purpose rather than merely unreachable: the store raises on this scheme before it
# touches a driver or a socket, so "the store cannot be opened" is deterministic and instant. Its
# presence is also what makes this a SERVED deployment as far as the gate is concerned, which is
# the state under test.
BROKEN_DB_URL = "mysql://not-a-supported-scheme/agami"


class _SpyExecutor:
    """An executor that records being reached and then refuses to pretend it was not.

    Raising rather than returning an empty result is the point: if the gate regresses, the failure
    has to land on the call itself. A spy that returned quietly would let the test fail later, on
    an assertion about rows, which could be satisfied for an unrelated reason.
    """

    def __init__(self) -> None:
        self.called = False

    def execute(self, sql, creds, *, profile=None, **kwargs):
        self.called = True
        raise AssertionError("the executor was reached with the audit store unopenable")


def _emit_through_the_tool_edge(sql: str, executor) -> dict:
    """One call through the real tool edge with `executor` behind it.

    The transport routes cannot be used here: `create_app()` injects its own executor, so a spy
    handed to it never runs. What this exercises is the same serializer either transport reaches,
    which is where the audit row is written and the receipt attached.
    """
    tools.set_injected_executor(executor)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": harness.PROFILE}))


def test_a_deployment_that_cannot_record_refuses_and_never_executes(file_path, monkeypatch):
    """The half that is load-bearing is the second one.

    A test that only checked the refusal would pass with the statement still running: the audit
    write happens at the tool edge, AFTER execution, so a gate that merely reported the problem
    would report it about a statement that had already reached the customer's database. The spy is
    what makes "did not execute" a fact rather than an inference from the status.

    The statement is a governed one, so the refusal cannot be a gate refusal wearing this rule's
    clothes — every scope gate allows it, and it comes back refused anyway.
    """
    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    spy = _SpyExecutor()

    body = _emit_through_the_tool_edge(_GOVERNED_SQL, spy)

    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == guardrail.RULE_AUDIT_UNAVAILABLE, body
    assert body["refusal"]["reason"] == guardrail.REASON_FOR_RULE[
        guardrail.RULE_AUDIT_UNAVAILABLE
    ], body
    assert spy.called is False, "the statement ran on a deployment that could not record it"
