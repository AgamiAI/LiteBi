"""The shared guardrail contract — one `Refusal`, one `Envelope`, every surface.

Every SQL safety gate returns `Refusal | None`; every path through `execute_sql.execute_guarded`
returns an `Envelope`. So a caller never has to know WHICH gate fired to understand what happened,
and a decision we made is never confused with a failure the database reported.

**Stdlib only, dataclasses only.** Imported at runtime by `sql_guard`, `execute_sql` and
`semantic_model.runtime`, and vendored byte-identical into `plugins/agami/lib/guardrail.py` for the
marketplace layout (no pip, no package, no deps). It therefore may not import pydantic, `contracts`,
or anything outside the stdlib — pinned by the clean-subprocess check in `tests/test_ports.py`.
`Envelope.data` is an `ExecResult`, referenced under TYPE_CHECKING only: the same device
`ports.Executor` uses, for the same two reasons — dependency-freedom, and no runtime import cycle
(`execute_sql` imports THIS module).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    # Type-checkers only — kept out of the runtime import graph. With `from __future__ import
    # annotations` the field annotation below stays a lazy string, so nothing here is resolved at
    # import or at construction time. `ExecResult` is the executor's result type and lives in
    # `execute_sql` rather than here, because `execute_sql` ships in the stdlib-lean plugin mirror
    # and cannot depend on this module's placement. Corollary: never call
    # `typing.get_type_hints(Envelope)` at runtime — it would try to import `execute_sql`.
    from execute_sql import ExecResult


# --- Refusal ----------------------------------------------------------------

RefusalReason = Literal["unsafe", "out_of_scope", "undetermined"]
"""The three reasons Agami refuses — and only three. Closed on purpose: a fourth requires editing
this line, which a reviewer sees in the diff. A *correctness* finding has no member here at all, by
construction rather than by convention."""

_REASONS: frozenset[str] = frozenset(get_args(RefusalReason))

# Rule ids. The set is open (a later gate adds one), but every value is a module-level constant so a
# regression corpus asserts on a symbol rather than a string literal.
RULE_READ_ONLY = "read_only"
RULE_TABLE_SCOPE = "table_scope"
RULE_COLUMN_SCOPE = "column_scope"
RULE_SELECT_STAR = "select_star"
RULE_MODEL_UNAVAILABLE = "model_unavailable"
RULE_RESOURCE_LIMIT = "resource_limit"
RULE_UNPARSEABLE = "unparseable"

# Named by the contract, produced by a later gate — declared here so that work fills a constant
# rather than inventing a string. Deliberately absent from `REASON_FOR_RULE` below: their reason is
# the owning gate's call (`recon` is arguably `unsafe` or `out_of_scope` depending on framing), and
# leaving them unpinned makes `refuse()` fail loudly rather than let that gate choose one silently.
RULE_RECON = "recon"
RULE_ENGINE_MISMATCH = "engine_mismatch"

# Interim. `_model_safety`'s fan/chasm pre-flight and sensitive-column branches are not converted in
# this slice (they become receipt facts when the mutation branches are subtracted), but every path
# out of `execute_guarded` must still return an Envelope. This is the rule those two branches carry
# until then. Delete it — and this comment — with them.
RULE_MODEL_SAFETY = "model_safety"

REASON_FOR_RULE: dict[str, RefusalReason] = {
    RULE_READ_ONLY: "unsafe",
    RULE_TABLE_SCOPE: "out_of_scope",
    RULE_COLUMN_SCOPE: "out_of_scope",
    # Deliberately NOT `undetermined`: the `SELECT *` ban is reclassified as a determinability
    # refusal in a later slice, and emitting `undetermined` now would silently pre-empt it.
    RULE_SELECT_STAR: "out_of_scope",
    RULE_MODEL_UNAVAILABLE: "undetermined",
    # A bound we imposed, not a property of the statement: neither unsafe nor out of scope — we
    # simply did not determine the answer within the bound.
    RULE_RESOURCE_LIMIT: "undetermined",
    RULE_UNPARSEABLE: "undetermined",
    RULE_MODEL_SAFETY: "undetermined",
}


@dataclass(frozen=True, kw_only=True)
class Refusal:
    """What a gate returns when it stops a statement.

    A gate returns `Refusal | None`; `None` means the gate is satisfied. There is no "allow" object,
    because there is nothing to grade.

    `detail` and `remediation` are always value-free: never raw SQL, never raw driver text, never a
    data value. An identifier the caller put in its own statement may be **echoed** back — that
    discloses nothing it did not already have. The declared surface is never **enumerated**: a
    refusal that lists the alternatives is a schema-listing endpoint.
    """

    reason: RefusalReason
    rule: str
    detail: str
    remediation: str

    def __post_init__(self) -> None:
        if self.reason not in _REASONS:
            raise ValueError(f"reason must be one of {sorted(_REASONS)}; got {self.reason!r}")
        if not self.rule.strip():
            raise ValueError("rule is mandatory")
        if not self.detail.strip():
            raise ValueError(f"detail is mandatory (rule={self.rule!r})")
        # A refusal the caller cannot act on is a dead end rather than a step in a conversation, so
        # an empty remediation is a CONSTRUCTION error, not a lint. That is what makes "every
        # refusal carries a remediation" true at every emit site rather than at the sampled ones a
        # test happened to reach.
        if not self.remediation.strip():
            raise ValueError(f"remediation is mandatory (rule={self.rule!r})")


def refuse(rule: str, *, detail: str, remediation: str) -> Refusal:
    """Build a `Refusal`, taking `reason` from `REASON_FOR_RULE` so no gate picks its own.

    A rule with no pinned reason raises `KeyError` on purpose: whoever introduces a rule pins its
    reason here, in one diff a reviewer reads.
    """
    return Refusal(reason=REASON_FOR_RULE[rule], rule=rule, detail=detail, remediation=remediation)


# --- Failure — the database's outcome, not ours -----------------------------

FailureKind = Literal[
    "syntax",
    "column_not_found",
    "table_not_found",
    "auth",
    "network",
    "dsn",
    "driver_missing",
    "timeout",
    "other",
]
"""The nine classified operational errors. This slice produces `dsn`, `driver_missing`, `auth`,
`syntax` and `other`, from the executor's exit codes. `column_not_found`, `table_not_found` and
`network` are DECLARED BUT UNREACHABLE: producing them means parsing driver text, and sanitizing
driver text belongs to the error-hardening slice. `timeout` is likewise unproduced today — the
supervisor bound we impose is a `resource_limit` REFUSAL, because the decision was ours; a real
per-statement database timeout arrives with the timeout slice. Declared now so those slices fill a
member rather than widen the type."""

_FAILURE_KINDS: frozenset[str] = frozenset(get_args(FailureKind))


@dataclass(frozen=True, kw_only=True)
class Failure:
    """The database rejecting a statement we let through, or the connection breaking — not a third
    thing we chose.

    `message` is the value-free form; raw driver text is captured server-side only. Keeping this out
    of `Refusal` is load-bearing: the remediation differs completely, because a refusal is our
    decision and always names its fix, while a failure can only be relayed.
    """

    kind: FailureKind
    message: str

    def __post_init__(self) -> None:
        if self.kind not in _FAILURE_KINDS:
            raise ValueError(f"kind must be one of {sorted(_FAILURE_KINDS)}; got {self.kind!r}")


# --- Receipt ----------------------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    """What the statement did — deliberately EMPTY.

    The field exists on `Envelope` from the start so the Envelope does not change shape later; its
    contents are owned by the receipt work and are empty until then. Present on every status
    including `refused`, because a refused caller most needs the facts.

    **This is not `contracts.Receipt`.** That one is a populated pydantic model — the trust receipt
    `semantic_model.runtime.assemble_receipt` builds and `tools._finalize_execution` nests inside
    the result payload, carrying tables_used / relationships / metrics / named_filters / assumptions
    / warnings. This one is the stdlib-only stub on the guardrail Envelope; this module may not
    import pydantic, so the two cannot be one type until the receipt is lifted to the top level. If
    you are reaching for content, you want `contracts.Receipt`.
    """


# --- Envelope ---------------------------------------------------------------

Status = Literal["ok", "refused", "failed"]
"""Three statuses, two decisions: `ok` and `refused` are ours, `failed` is the database's."""

_STATUSES: frozenset[str] = frozenset(get_args(Status))


@dataclass(frozen=True, kw_only=True)
class Envelope:
    """What the caller gets — one shape, every surface.

    Assembled in `execute_sql.execute_guarded`, after `executor.execute()`, so nothing can be
    smuggled past it.

    `data` is an `ExecResult` — native-typed rows. Serializing it, and enriching it with units,
    markdown and the trust receipt, stays one layer up in `tools`.

    `audit_id` is the `query_executions.id` of the row recording this execution — the same id, not a
    parallel one, so the answer and its audit trail are joined by construction.

    Field order is the contract's, verbatim. `kw_only=True` is what allows that: without it the two
    fields carrying no default would have to be adjacent, and the declared order would drift from
    the contract for a reason no reader could see.
    """

    status: Status
    data: ExecResult | None = None
    refusal: Refusal | None = None
    failure: Failure | None = None
    receipt: Receipt = field(default_factory=Receipt)
    audit_id: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {sorted(_STATUSES)}; got {self.status!r}")
        # The contract's "present iff", enforced rather than documented. The fork path and the
        # in-process path construct this independently, and this check is what keeps the two routes
        # from drifting into different shapes.
        payload = {"ok": self.data, "refused": self.refusal, "failed": self.failure}[self.status]
        if payload is None:
            raise ValueError(f"status={self.status!r} requires its corresponding payload field")
        if sum(x is not None for x in (self.data, self.refusal, self.failure)) != 1:
            raise ValueError("exactly one of data / refusal / failure may be set")
        if not self.audit_id.strip():
            raise ValueError("audit_id is mandatory — it references the recorded trail")
