"""The shared guardrail contract, asserted field by field against the spec.

These are shape tests, not behavior tests — no gate is exercised here. They exist because every
later slice and every later gate encodes assumptions about these field names and these closed value
sets, and a silent widening is exactly the kind of change that passes review.
"""

from dataclasses import FrozenInstanceError, fields

import guardrail
import pytest
from guardrail import (
    REASON_FOR_RULE,
    Envelope,
    Failure,
    Receipt,
    Refusal,
    refuse,
)

OK_ID = "abc123"


def _refusal(**over):
    base = {
        "reason": "unsafe",
        "rule": guardrail.RULE_READ_ONLY,
        "detail": "d",
        "remediation": "r",
    }
    return Refusal(**{**base, **over})


# --- field exactness --------------------------------------------------------


def test_refusal_fields_are_exactly_the_contract_four_in_order():
    assert [f.name for f in fields(Refusal)] == ["reason", "rule", "detail", "remediation"]


def test_envelope_fields_are_exactly_the_contract_six_in_order():
    assert [f.name for f in fields(Envelope)] == [
        "status",
        "data",
        "refusal",
        "failure",
        "receipt",
        "audit_id",
    ]


def test_failure_fields_are_kind_and_message():
    assert [f.name for f in fields(Failure)] == ["kind", "message"]


def test_receipt_is_empty():
    """The field exists so the Envelope does not change shape when the receipt is filled in. If
    this test starts failing because someone added a field, they wanted `contracts.Receipt`."""
    assert fields(Receipt) == ()


@pytest.mark.parametrize("cls", [Refusal, Failure, Receipt, Envelope])
def test_contract_types_are_frozen(cls):
    assert cls.__dataclass_params__.frozen


def test_refusal_is_immutable_in_practice():
    r = _refusal()
    with pytest.raises(FrozenInstanceError):
        r.rule = "something_else"  # type: ignore[misc]


# --- reason is a closed three-value type ------------------------------------


def test_reason_has_exactly_three_members():
    assert sorted(guardrail._REASONS) == ["out_of_scope", "undetermined", "unsafe"]


def test_a_fourth_reason_is_rejected():
    """Closed by construction. A fourth reason cannot be introduced without editing the type, which
    a reviewer sees in the diff."""
    with pytest.raises(ValueError, match="reason must be one of"):
        _refusal(reason="data_protection")


@pytest.mark.parametrize("reason", ["unsafe", "out_of_scope", "undetermined"])
def test_each_declared_reason_constructs(reason):
    assert _refusal(reason=reason).reason == reason


# --- mandatory remediation, structurally ------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_an_empty_remediation_cannot_be_constructed(blank):
    """Not a lint — a construction error. This is what makes 'every refusal carries a remediation'
    true at every emit site rather than at the sampled ones a test happened to reach."""
    with pytest.raises(ValueError, match="remediation is mandatory"):
        _refusal(remediation=blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_detail_cannot_be_constructed(blank):
    with pytest.raises(ValueError, match="detail is mandatory"):
        _refusal(detail=blank)


def test_an_empty_rule_cannot_be_constructed():
    with pytest.raises(ValueError, match="rule is mandatory"):
        _refusal(rule="")


def test_the_error_names_the_rule_so_a_failure_is_locatable():
    with pytest.raises(ValueError, match="table_scope"):
        _refusal(rule=guardrail.RULE_TABLE_SCOPE, remediation="")


# --- the pinned reason-for-rule table ---------------------------------------


def test_every_rule_this_slice_can_emit_has_a_pinned_reason():
    """`refuse()` raises KeyError on an unpinned rule, so `REASON_FOR_RULE` is the list a new gate
    must extend. Everything emittable today is pinned."""
    declared = {
        v for k, v in vars(guardrail).items() if k.startswith("RULE_") and isinstance(v, str)
    }
    assert declared - set(REASON_FOR_RULE) == {guardrail.RULE_RECON, guardrail.RULE_ENGINE_MISMATCH}


@pytest.mark.parametrize("rule", ["recon", "engine_mismatch"])
def test_a_named_but_unproduced_rule_is_deliberately_unpinned(rule):
    """`recon` and `engine_mismatch` are named by the contract but produced by later slices, and
    their reason is genuinely those slices' call — `recon` could be `unsafe` or `out_of_scope`
    depending on how it is framed. So they are declared as constants (a later slice fills a symbol
    rather than inventing a string) but left out of `REASON_FOR_RULE`, which makes `refuse()` fail
    loudly rather than let that slice pick a reason without pinning it here first."""
    assert rule not in REASON_FOR_RULE
    with pytest.raises(KeyError):
        refuse(rule, detail="d", remediation="r")


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        ("read_only", "unsafe"),
        ("table_scope", "out_of_scope"),
        ("column_scope", "out_of_scope"),
        # Deliberately out_of_scope, not undetermined — a later slice reclassifies the SELECT * ban
        # as a determinability refusal, and emitting `undetermined` now would pre-empt it.
        ("select_star", "out_of_scope"),
        ("model_unavailable", "undetermined"),
        ("resource_limit", "undetermined"),
        ("unparseable", "undetermined"),
    ],
)
def test_the_reason_for_each_rule_is_pinned(rule, reason):
    assert REASON_FOR_RULE[rule] == reason
    assert refuse(rule, detail="d", remediation="r").reason == reason


def test_refuse_rejects_a_rule_with_no_pinned_reason():
    with pytest.raises(KeyError):
        refuse("invented_rule", detail="d", remediation="r")


# --- failure kinds ----------------------------------------------------------


def test_failure_kinds_are_exactly_the_contract_nine():
    assert sorted(guardrail._FAILURE_KINDS) == [
        "auth",
        "column_not_found",
        "driver_missing",
        "dsn",
        "network",
        "other",
        "syntax",
        "table_not_found",
        "timeout",
    ]


def test_an_unknown_failure_kind_is_rejected():
    with pytest.raises(ValueError, match="kind must be one of"):
        Failure(kind="permission", message="m")


# --- the Envelope's present-iff invariant -----------------------------------


def test_statuses_are_exactly_three():
    assert sorted(guardrail._STATUSES) == ["failed", "ok", "refused"]


def test_ok_requires_data():
    with pytest.raises(ValueError, match="requires its corresponding payload"):
        Envelope(status="ok", audit_id=OK_ID)


def test_refused_requires_a_refusal():
    with pytest.raises(ValueError, match="requires its corresponding payload"):
        Envelope(status="refused", audit_id=OK_ID)


def test_failed_requires_a_failure():
    with pytest.raises(ValueError, match="requires its corresponding payload"):
        Envelope(status="failed", audit_id=OK_ID)


def test_two_payloads_are_rejected():
    """A refusal that also carries rows would let a caller read data we decided not to return."""
    with pytest.raises(ValueError, match="exactly one of"):
        Envelope(
            status="refused",
            refusal=_refusal(),
            failure=Failure(kind="syntax", message="m"),
            audit_id=OK_ID,
        )


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValueError, match="status must be one of"):
        Envelope(status="warned", audit_id=OK_ID)


@pytest.mark.parametrize("blank", ["", "  "])
def test_audit_id_is_mandatory(blank):
    with pytest.raises(ValueError, match="audit_id is mandatory"):
        Envelope(status="refused", refusal=_refusal(), audit_id=blank)


# --- the receipt is present on all three statuses ---------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "data": object()},
        {"status": "refused", "refusal": _refusal()},
        {"status": "failed", "failure": Failure(kind="syntax", message="m")},
    ],
    ids=["ok", "refused", "failed"],
)
def test_receipt_is_present_on_every_status(payload):
    """Asserted on the type rather than only on the ok path, so the receipt work fills a field
    instead of changing the shape — including on a refusal, where a caller most needs the facts."""
    env = Envelope(audit_id=OK_ID, **payload)
    assert env.receipt == Receipt()


def test_each_envelope_gets_its_own_receipt_instance():
    """`default_factory`, not a shared default — a mutable-by-later-slice value must not be shared
    across envelopes."""
    a = Envelope(status="refused", refusal=_refusal(), audit_id=OK_ID)
    b = Envelope(status="refused", refusal=_refusal(), audit_id=OK_ID)
    assert a.receipt is not b.receipt
