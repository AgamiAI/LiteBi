"""Every vector of the safety corpus, driven through the real `execute_sql` tool over HTTP.

This is the file that makes the corpus a regression gate rather than a list. Each vector goes in as
a `tools/call` on the authenticated `/mcp` endpoint and the serialized tool-edge body comes back,
so what is asserted is what a caller actually receives — not what a gate returns to its own caller
one frame in.

**A refused vector asserts its RULE and its REASON, never merely that it was refused.**
`status == "refused"` reads green while the gate that owns the rule never fires: the bracket-quoted
star is the standing example, where column scope can answer for the star ban and the statement is
still refused, by the wrong gate, for the wrong reason. So every refusal names the
`guardrail.RULE_*` symbol it expects and takes its `reason` from `guardrail.REASON_FOR_RULE`, which
keeps the contract's enum the only source of the pairing.

The governed vectors are the other half and they are not decoration: they are what stops a
tightening from buying safety by refusing valid SQL. Each asserts `ok`, the ABSENCE of a refusal,
and a receipt carrying every declared section — iterated off `guardrail.Receipt.SECTIONS` so a
section added to the contract is covered here the day it lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (TESTS_ROOT, Path(__file__).resolve().parent, REPO_ROOT / "packages" / "agami-core" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import guardrail  # noqa: E402
import harness  # noqa: E402
from safety.corpus import CASES  # noqa: E402


@pytest.fixture()
def file_path(tmp_path, monkeypatch):
    """The file-served model path: disk YAML + a SQLite warehouse, both built from `SCHEMA`."""
    yield harness.build_file_path(tmp_path, monkeypatch)
    harness.reset_injected_executor()


def _params():
    """One parameter per vector, carrying its own strict-xfail marker where this branch is red.

    The marker is applied by the PARAMETRIZER off `Case.red_on_main`, so the red set is a property
    of the corpus rather than a decoration a later edit can add to a vector that already passes.
    `strict` cuts both ways on purpose: when the owning gate lands, the vector flips green and this
    file fails until the marker is removed, so nobody has to re-read a spec to notice.
    """
    return [
        pytest.param(
            case,
            marks=pytest.mark.xfail(strict=True, reason="the gate that closes this has not landed"),
            id=case.id,
        )
        if case.red_on_main
        else pytest.param(case, id=case.id)
        for case in CASES
        if case.runs_on(harness.ENGINE)
    ]


@pytest.mark.parametrize("case", _params())
def test_the_chokepoint_gives_each_vector_its_own_rule_over_http(file_path, case, monkeypatch):
    """The whole corpus, one vector at a time, on the transport the hosted deployment serves."""
    if case.rule == guardrail.RULE_RESOURCE_LIMIT:
        # The deployment ceiling, lowered on the harness's server process for the two vectors that
        # exist to reach it. There is no per-call cap to lower any more, and lowering it for the
        # whole session would turn the governed vectors into availability refusals — they return
        # more rows than this ceiling by design.
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))

    body = harness.ROUTES["http"](case.sql)

    _assert_verdict(body, case)


def _assert_verdict(body: dict, case) -> None:
    """The two halves of the contract, asserted the same way for every vector."""
    if case.rule is None:
        assert body["status"] == "ok", body
        # Not "the refusal is empty" — the key is absent. A caller distinguishing the two would be
        # reading a shape the tool edge does not emit.
        assert "refusal" not in body, body
        receipt = body["receipt"]
        for section in guardrail.Receipt.SECTIONS:
            assert section in receipt, (section, receipt)
            assert set(receipt[section]) == {"items", "undetermined"}, (section, receipt)
        return

    assert body["status"] == "refused", body
    refusal = body["refusal"]
    assert refusal["rule"] == case.rule, body
    assert refusal["reason"] == guardrail.REASON_FOR_RULE[case.rule], body


# ---------------------------------------------------------------------------
# Keeping the corpus honest
# ---------------------------------------------------------------------------


def test_the_corpus_is_the_shape_the_coverage_claim_rests_on():
    """The parametrization above is only as good as the list it reads.

    Three facts a thinned corpus would quietly lose: the vector count, the fifteen governed vectors
    that carry the no-false-refusal half, and that every rule a vector expects is one the guardrail
    actually pins — a vector expecting an unpinned rule could never pass, and a vector expecting a
    misspelt one would xfail forever without anyone noticing.
    """
    assert len(CASES) == 56
    assert len([c for c in CASES if c.rule is None]) == 15
    expected_rules = {c.rule for c in CASES if c.rule is not None}
    assert expected_rules <= set(guardrail.REASON_FOR_RULE)
    assert len({c.id for c in CASES}) == len(CASES), "a duplicate id would silently drop a vector"


def test_only_the_measured_forms_are_red_and_the_dialect_quoted_ones_are_not():
    """A strict xfail that PASSES fails the build, so the red set is a claim about this branch and
    has to be exactly right.

    The dialect-quoted vectors are the ones this pins hardest. They were the corpus's largest
    expected-red group until the readability gate shipped, and marking them now would fail the
    build on four vectors that pass. The two that remain red both parse cleanly and still slip a
    source past the scope walk, which is the narrow tail the readability gate does not yet reach.
    """
    assert {c.note for c in CASES if c.red_on_main} == {"table-fn", "comma-join-values"}
    assert not any(c.red_on_main for c in CASES if c.cls == "quoting")
