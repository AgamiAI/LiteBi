"""The shared guardrail contract: ``Verdict`` · ``policy`` · ``Envelope``.

Covers the type shapes (field names + serialization, incl. the ``action`` set) and that
``policy(safety, …, tier)`` returns ``reject`` for every tier and on ``certainty=uncertain``
(fail-closed).

Pure-stdlib module — no importorskip needed (unlike the model-backed gate tests).
"""

from __future__ import annotations

import pytest
from guardrail import (
    ACTIONS,
    CLASSES,
    REFUSAL_KINDS,
    Envelope,
    Refusal,
    Verdict,
    policy,
)

TIERS = ("oss", "saas", "enterprise")


def _safety(**over) -> Verdict:
    base = dict(
        cls="safety",
        rule="read_only",
        severity="high",
        certainty="provable",
        detail="write statements are not allowed",
        remediation="send a single read-only SELECT",
    )
    base.update(over)
    return Verdict(**base)


# ── type shapes + serialization ──────────────────────────────────────────────


def test_verdict_fields_and_serialization():
    v = _safety()
    # The dataclass attribute is `cls` (a Python keyword can't be a field), but the WIRE key is
    # `class` — the contract name. This is the pin that keeps serialization contract-faithful.
    assert v.cls == "safety"
    d = v.as_dict()
    assert d == {
        "class": "safety",
        "rule": "read_only",
        "severity": "high",
        "certainty": "provable",
        "detail": "write statements are not allowed",
        "remediation": "send a single read-only SELECT",
    }
    assert "rewritten_sql" not in d  # omitted unless the action is rewrite


def test_verdict_rewritten_sql_present_only_when_set():
    v = _safety(cls="governance", certainty="provable", rewritten_sql="SELECT 1")
    assert v.as_dict()["rewritten_sql"] == "SELECT 1"


def test_verdict_is_frozen():
    v = _safety()
    with pytest.raises(Exception):
        v.rule = "other"  # frozen dataclass — immutable verdicts


def test_action_and_class_sets_match_the_contract():
    # The `action` set and the class set are fixed by the shared contract.
    assert set(ACTIONS) == {"allow", "reject", "mask", "row_filter", "rewrite", "warn"}
    assert set(CLASSES) == {"safety", "data_protection", "governance"}


def test_every_emitted_refusal_kind_is_documented():
    # `Refusal.kind` is an open str, so this pins the kinds the code ACTUALLY emits (executor gates +
    # the tools layer + _classify_db_error) against the documented REFUSAL_KINDS set, so the vocabulary
    # can never silently drift from reality. Keep in sync with the emit sites if you add a kind.
    emitted = {
        # execute_sql._model_safety + main + tools.tool_execute_sql pre-check (guardrail refusals)
        "permission",
        "table_out_of_scope",
        "select_star",
        "column_out_of_scope",
        "unscopable_sql",
        "resource_limit",
        "recon",
        "model_unavailable",
        "preflight_refused",
        "sensitive_columns",
        # tools.tool_execute_sql + _classify_db_error (operational / execution failures)
        "other",
        "timeout",
        "dsn",
        "network",
        "driver_missing",
        "auth",
        "column_not_found",
        "table_not_found",
        "syntax",
    }
    assert emitted <= set(REFUSAL_KINDS), emitted - set(REFUSAL_KINDS)


def test_refusal_shape():
    r = Refusal(kind="permission", reason="write not allowed", remediation="use SELECT")
    assert r.as_dict() == {
        "kind": "permission",
        "reason": "write not allowed",
        "remediation": "use SELECT",
    }


def test_envelope_refused_shape():
    r = Refusal(kind="table_out_of_scope", reason="unknown table foo")
    env = Envelope.refused(r, audit_id="a1")
    assert env.status == "refused"
    d = env.as_dict()
    assert d["status"] == "refused"
    assert d["audit_id"] == "a1"
    assert d["refusal"] == {
        "kind": "table_out_of_scope",
        "reason": "unknown table foo",
        "remediation": "",
    }
    assert "data" not in d  # no data on a refusal
    assert d["applied"] == [] and d["warnings"] == []


def test_envelope_ok_shape_with_warnings():
    warn = _safety(cls="governance", certainty="heuristic", rule="ungoverned_metric")
    env = Envelope.ok(
        {"columns": ["n"], "rows": [[3]]},
        audit_id="a2",
        applied=[{"row_filter": "region = 'US'"}],
        warnings=[warn],
    )
    d = env.as_dict()
    assert d["status"] == "ok"
    assert d["data"] == {"columns": ["n"], "rows": [[3]]}
    assert d["applied"] == [{"row_filter": "region = 'US'"}]
    assert d["warnings"] == [warn.as_dict()]  # warnings serialize as a list of Verdict dicts
    assert "refusal" not in d
    assert d["audit_id"] == "a2"


# ── policy: the safety branch is absolute + fail-closed ──────────────────────


def test_policy_safety_rejects_every_tier():
    v = _safety()
    for tier in TIERS:
        assert policy(v, tier) == "reject"


def test_policy_safety_rejects_on_uncertainty():
    # Fail-closed on doubt: an `uncertain` safety verdict is still a reject, in every tier.
    v = _safety(certainty="uncertain")
    for tier in TIERS:
        assert policy(v, tier) == "reject"


def test_policy_default_tier_is_oss_and_safety_still_rejects():
    assert policy(_safety()) == "reject"  # default tier


def test_policy_unknown_class_raises():
    bad = Verdict(
        cls="bogus", rule="x", severity="low", certainty="provable", detail="", remediation=""
    )
    with pytest.raises(ValueError):
        policy(bad)


# ── policy stubs for the data-protection / governance branches (regression pins) ─


def test_policy_data_protection_stub_fails_closed():
    v = _safety(cls="data_protection", rule="sensitive_projection")
    # Stub branch: fail-closed until the data-protection gates fill mask/row_filter selection.
    assert policy(v) == "reject"


def test_policy_governance_stub_warns_by_default_enforces_at_enterprise():
    heuristic = _safety(cls="governance", certainty="heuristic", rule="ungoverned_metric")
    assert policy(heuristic, "oss") == "warn"
    assert policy(heuristic, "saas") == "warn"  # SaaS recommends — never blocks
    assert policy(heuristic, "enterprise") == "warn"  # heuristic never blocks
    provable = _safety(cls="governance", certainty="provable", rule="fan_trap")
    assert policy(provable, "oss") == "warn"  # non-enforcing tier
    assert policy(provable, "saas") == "warn"  # SaaS recommends, does not enforce
    assert policy(provable, "enterprise") == "reject"  # provable + enforcing tier
    rewrite = _safety(
        cls="governance", certainty="provable", rule="fan_trap", rewritten_sql="SELECT 1"
    )
    assert policy(rewrite, "enterprise") == "rewrite"
