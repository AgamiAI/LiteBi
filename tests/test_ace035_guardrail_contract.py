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
    ReceiptSection,
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


def test_receipt_fields_are_the_version_pin_and_the_five_sections_in_order():
    """ACE-035 declared this type empty and ACE-088 fills it. The tripwire that used to read
    `fields(Receipt) == ()` is replaced rather than deleted: it existed to stop an accidental field,
    and the same guard now works by pinning the intended ones."""
    assert [f.name for f in fields(Receipt)] == [
        "model_version",
        *Receipt.SECTIONS,
    ]


def test_every_section_defaults_to_declared_but_unestablished():
    """Every field carries a default, so `Receipt()` still satisfies `Envelope.receipt`'s
    `default_factory` and the Envelope's shape is unchanged."""
    r = Receipt()
    assert r.model_version is None
    for name in Receipt.SECTIONS:
        assert getattr(r, name) == ReceiptSection(items=(), undetermined=None)


def test_a_section_distinguishes_unchecked_from_clean():
    """The distinction the receipt exists for. An empty section with no reason means "checked, found
    nothing"; an empty section with a reason means "not checked". They must not compare equal."""
    clean = ReceiptSection()
    unchecked = ReceiptSection(undetermined="sqlglot unavailable")
    assert clean != unchecked
    assert clean.items == unchecked.items == ()
    assert clean.undetermined is None and unchecked.undetermined


def test_section_items_are_immutable():
    """A frozen dataclass holding a list is only shallowly immutable, so `items` is a tuple."""
    assert isinstance(ReceiptSection().items, tuple)
    with pytest.raises(FrozenInstanceError):
        ReceiptSection().items = ({"x": 1},)


# --- the two constructors ---------------------------------------------------


def test_an_undetermined_receipt_gives_every_section_the_same_reason():
    """The shape a caller gets when the receipt could not be built at all. Deliberately not
    `Receipt()`: five empty sections with no reason read as "checked, found nothing", and a receipt
    that could not be built is a fact rather than an absence."""
    receipt = guardrail.undetermined_receipt("no parser here", model_version="v1")

    assert receipt.model_version == "v1"
    for name in Receipt.SECTIONS:
        assert getattr(receipt, name) == ReceiptSection(items=(), undetermined="no parser here")
    assert receipt != Receipt()


def test_an_undetermined_receipt_covers_a_section_added_to_the_type():
    """It iterates `Receipt.SECTIONS` rather than naming the five fields, so a sixth section cannot
    be forgotten here and silently ship as clean. Asserted by counting rather than by reading the
    source, because "it iterates" is the property, not the spelling."""
    receipt = guardrail.undetermined_receipt("r")
    marked = [name for name in Receipt.SECTIONS if getattr(receipt, name).undetermined == "r"]
    assert len(marked) == len(Receipt.SECTIONS) == len(fields(Receipt)) - 1


def test_an_assembled_receipt_maps_onto_the_contract_with_tuple_items():
    """The one place the assembler's dicts-and-lists meet the contract's frozen dataclasses. `items`
    becomes a tuple because the field is one — a list would make the frozen type shallowly
    immutable."""
    assembled = {
        "model_version": "v2",
        **{name: {"items": [], "undetermined": f"{name} pending"} for name in Receipt.SECTIONS},
    }
    assembled["tables"] = {"items": [{"ref": "orders"}], "undetermined": None}

    receipt = guardrail.receipt_from_assembled(assembled)

    assert receipt.model_version == "v2"
    assert receipt.tables == ReceiptSection(items=({"ref": "orders"},), undetermined=None)
    assert isinstance(receipt.tables.items, tuple)
    assert receipt.joins.undetermined == "joins pending"


def test_an_assembled_receipt_missing_a_section_raises_rather_than_losing_it():
    """Strict indexing on purpose: a builder that drops a section is drifted, and every caller turns
    this `KeyError` into an `undetermined` receipt. Silently defaulting the section would ship a
    clean-looking one instead."""
    with pytest.raises(KeyError):
        guardrail.receipt_from_assembled({"columns": {"items": [], "undetermined": None}})


@pytest.mark.parametrize("cls", [Refusal, Failure, Receipt, ReceiptSection, Envelope])
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


# --- the rule vocabulary, against the contract ------------------------------

# The ten rules §1 of the guardrail contract names, transcribed as a literal. Deriving this set from
# `guardrail` — which is what the reason-for-rule test below does, legitimately, for a different
# question — cannot answer THIS one: a set compared against itself agrees with any drift. It agreed
# with `unscopable` being absent from the module entirely (zero hits repo-wide) while the contract
# spends a paragraph on why it is not the same rule as `unparseable`, and it would have agreed just
# as readily with an invented rule appearing. Transcribing the contract is the only version of this
# test that can fail.
CONTRACT_RULES = frozenset({
    "read_only",
    "recon",
    "resource_limit",
    "table_scope",
    "column_scope",
    "select_star",
    "unparseable",
    "unscopable",
    "model_unavailable",
    "engine_mismatch",
})

# Rules this module declares that the contract does NOT name. There are NONE, and that is the
# stronger form of this check. There was exactly one: `model_safety`, carried by the unconverted
# `_model_safety` branches so that every path out of `execute_guarded` could still return an
# Envelope. ACE-094 deleted those branches and the rule went with them, so every rule this module
# declares is now one the contract names. Anything appearing here again is a rule someone
# invented, which is the drift this test exists to catch.
LOCAL_ADDITIONS: frozenset[str] = frozenset()


def _declared_rules() -> set[str]:
    return {v for k, v in vars(guardrail).items() if k.startswith("RULE_") and isinstance(v, str)}


def test_the_declared_rules_are_exactly_the_contract():
    declared = _declared_rules()
    # Split into the two directions so a failure says which one happened rather than printing two
    # sets and leaving the reader to diff them.
    assert CONTRACT_RULES - declared == frozenset(), "contract rule(s) not declared in guardrail.py"
    assert declared - CONTRACT_RULES - LOCAL_ADDITIONS == frozenset(), "undeclared rule invented"
    assert declared == CONTRACT_RULES | LOCAL_ADDITIONS


# --- the pinned reason-for-rule table ---------------------------------------


def test_every_rule_this_slice_can_emit_has_a_pinned_reason():
    """`refuse()` raises KeyError on an unpinned rule, so `REASON_FOR_RULE` is the list a new gate
    must extend. Everything emittable today is pinned."""
    assert _declared_rules() - set(REASON_FOR_RULE) == {
        guardrail.RULE_ENGINE_MISMATCH,
        guardrail.RULE_UNSCOPABLE,
    }


@pytest.mark.parametrize("rule", ["engine_mismatch", "unscopable"])
def test_a_named_but_unproduced_rule_is_deliberately_unpinned(rule):
    """`engine_mismatch` and `unscopable` are named by the contract but produced by later slices.
    For the first the reason is genuinely that slice's call. `unscopable` is a different case: the
    contract already says `undetermined`, so leaving it out of `REASON_FOR_RULE` is not deferring a
    decision, it is making the owning gate write the one line the contract dictates in a diff a
    reviewer sees, alongside the gate that first emits it. Either way `refuse()` fails loudly rather
    than letting a slice pick a reason without pinning it here.

    `recon` LEFT this list in ACE-039, which is the slice that produces it. Its reason was the one
    genuinely arguable case — a recon call is a reach in one framing and a hazard in another — and
    it settled as `unsafe`, pinned in the table below."""
    assert rule not in REASON_FOR_RULE
    with pytest.raises(KeyError):
        refuse(rule, detail="d", remediation="r")


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        ("read_only", "unsafe"),
        ("table_scope", "out_of_scope"),
        ("column_scope", "out_of_scope"),
        # `undetermined`, not out_of_scope: resolving `*` to a column list needs the catalog, so
        # whether the projection stays inside the declared surface is not decidable from the SQL
        # and the model alone. The ban is a determinability refusal, not a reach.
        ("select_star", "undetermined"),
        ("model_unavailable", "undetermined"),
        ("resource_limit", "undetermined"),
        ("unparseable", "undetermined"),
        # Pinned by ACE-039, the gate that produces it. `unsafe` rather than `out_of_scope`
        # because a model declares tables and columns and never declares functions, so there is
        # no in-scope spelling of `version()` for an out-of-scope refusal to point toward.
        ("recon", "unsafe"),
    ],
)
def test_the_reason_for_each_rule_is_pinned(rule, reason):
    assert REASON_FOR_RULE[rule] == reason
    assert refuse(rule, detail="d", remediation="r").reason == reason


def test_refuse_rejects_a_rule_with_no_pinned_reason():
    with pytest.raises(KeyError):
        refuse("invented_rule", detail="d", remediation="r")


# --- failure kinds ----------------------------------------------------------


def test_failure_kinds_are_exactly_the_contract_ten():
    """Contract §3's list, verbatim. `permission` is in it and is NOT the `read_only` rule: §1's
    `read_only` is our verdict that we blocked a write, `permission` is the database refusing a read
    to the connection's role. Declared here though nothing produces it yet, so ACE-039 fills a member
    rather than widening the type — which is the whole reason the unreachable kinds are declared."""
    assert sorted(guardrail._FAILURE_KINDS) == [
        "auth",
        "column_not_found",
        "driver_missing",
        "dsn",
        "network",
        "other",
        "permission",
        "syntax",
        "table_not_found",
        "timeout",
    ]


def test_an_unknown_failure_kind_is_rejected():
    """The vector used to be `permission`, which the contract declares as a real kind (§3) — it read
    as invalid only because the type had dropped it. Using a member as the not-a-member example is
    how that omission stayed invisible, so this asks for a string the contract will never name."""
    with pytest.raises(ValueError, match="kind must be one of"):
        Failure(kind="not_a_declared_kind", message="m")


def test_permission_is_a_failure_kind_and_read_only_is_a_rule():
    """Contract §3's distinction, pinned as a type-level fact because it was collapsed once already.

    `read_only` is §1's RULE — our verdict that we blocked a write. `permission` is a failure KIND —
    the database refusing a read to the connection's role, which is also not `auth`, because the
    credentials were accepted and the fix is a grant. Nothing produces `permission` yet (ACE-039
    does), so without this the member could be dropped again and every test would stay green.
    """
    assert "permission" in guardrail._FAILURE_KINDS
    assert "permission" not in guardrail.REASON_FOR_RULE
    assert guardrail.RULE_READ_ONLY in guardrail.REASON_FOR_RULE
    assert guardrail.RULE_READ_ONLY not in guardrail._FAILURE_KINDS
    assert Failure(kind="permission", message="m").kind == "permission"


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
