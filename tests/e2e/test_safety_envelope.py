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
