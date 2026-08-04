"""The corpus over the stdio transport, and the verdict it owes: the same one HTTP gave.

Enforcement lives at one chokepoint, so a transport is supposed to be a way of *reaching* it and
nothing more. This file is what makes that a tested claim rather than an architectural intention.
Each vector is driven twice — once through `python -m mcp_harness` speaking JSON-RPC on stdin, once
through the authenticated `/mcp` endpoint — and the two decisions have to be the same decision. A
gate that fired on one transport and not the other would be a hole in the shape a reviewer is least
likely to look for, because the per-gate unit tests all pass and the HTTP corpus is entirely green.

**The subset is deliberate, and it is bounded from both sides.** The stdio route spawns a process
per call, so the whole corpus over both transports is ~112 spawns on the critical path of every PR.
`safety.corpus.STDIO_SUBSET` therefore carries one vector per distinct rule plus the read-only class
whole — the largest attack surface, whose vectors take genuinely different paths into the same gate
(bare keyword, comment-hidden separator, CTE body). Widening it to all 56 buys a slower gate and no
new rule; narrowing it drops a rule's only stdio evidence. The shape test below pins both ends.

The red set is empty on this branch and `test_safety_corpus.py` asserts it stays that way, so no
xfail machinery is duplicated here; a vector that went red would fail there first and loudest.
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
for _path in (
    TESTS_ROOT,
    Path(__file__).resolve().parent,
    REPO_ROOT / "packages" / "agami-core" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import guardrail  # noqa: E402
import harness  # noqa: E402

from safety.corpus import CASES, STDIO_SUBSET  # noqa: E402


@pytest.fixture()
def file_path(tmp_path, monkeypatch):
    """The file-served model path: disk YAML + a SQLite warehouse, both built from `SCHEMA`.

    The stdio child inherits it through the environment — `monkeypatch.setenv` writes the real
    `os.environ` and the route passes it down — so both transports read the same model and the same
    warehouse, which is what makes comparing their verdicts mean anything.
    """
    yield harness.build_file_path(tmp_path, monkeypatch)
    harness.reset_injected_executor()


def _verdict(body: dict) -> tuple:
    """The DECISION, stripped of everything a transport is free to differ on.

    `audit_id` is per call (the two runs are two executions and each writes its own row),
    `execution_ms` is a clock, and `sql` and `markdown` are echoes. What must not differ is the
    status, the rule and reason when refused, and the answer's shape when not — a transport that
    dropped or truncated rows shows up in `row_count`. The row payload itself is deliberately left
    out: it would pin an ordering the engine never promised, which is flakiness rather than
    evidence.
    """
    refusal = body.get("refusal") or {}
    return (
        body["status"],
        refusal.get("rule"),
        refusal.get("reason"),
        tuple(body["columns"]) if "columns" in body else None,
        body.get("row_count"),
    )


@pytest.mark.parametrize(
    "case",
    [pytest.param(case, id=case.id) for case in STDIO_SUBSET if case.runs_on(harness.ENGINE)],
)
def test_a_vector_gets_the_same_verdict_over_stdio_as_it_does_over_http(
    file_path, case, monkeypatch
):
    """One vector, two transports, one verdict — and that verdict is the one the corpus pins."""
    if case.rule == guardrail.RULE_RESOURCE_LIMIT:
        # The deployment ceiling, lowered on both server processes: `monkeypatch.setenv` reaches the
        # in-process app directly and the stdio child by inheritance.
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))

    over_stdio = harness.ROUTES["stdio"](case.sql)
    over_http = harness.ROUTES["http"](case.sql)

    # The assertion this whole dimension exists for.
    assert _verdict(over_stdio) == _verdict(over_http), (over_stdio, over_http)

    # And the shared verdict is the expected one, so two transports cannot pass by agreeing on the
    # wrong answer. Asserted on the rule and its reason, never on `status` alone: a refusal by the
    # wrong gate reads green under a status check and is a different security posture.
    if case.rule is None:
        assert over_stdio["status"] == "ok", over_stdio
        assert "refusal" not in over_stdio, over_stdio
        return
    assert over_stdio["status"] == "refused", over_stdio
    assert over_stdio["refusal"]["rule"] == case.rule, over_stdio
    assert over_stdio["refusal"]["reason"] == guardrail.REASON_FOR_RULE[case.rule], over_stdio


def test_the_stdio_subset_is_bounded_from_both_sides():
    """The subset is a claim about coverage, so it needs an assertion or it is a comment.

    Both bounds matter. Missing a rule means that rule has no stdio evidence at all; growing to the
    whole corpus means the bound was quietly abandoned and every PR pays ~112 spawns for it.
    """
    assert {case.rule for case in STDIO_SUBSET} == {case.rule for case in CASES}
    # The read-only class rides along whole — the vectors reach the same gate by different routes.
    read_only = guardrail.RULE_READ_ONLY
    assert [c.id for c in STDIO_SUBSET if c.rule == read_only] == [
        c.id for c in CASES if c.rule == read_only
    ]
    # One vector per remaining rule, and no more.
    assert len([c for c in STDIO_SUBSET if c.rule != read_only]) == len(
        {c.rule for c in CASES if c.rule != read_only}
    )
    assert len(STDIO_SUBSET) < len(CASES)
