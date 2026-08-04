"""The shared guardrail contract — one `Refusal`, one `Envelope`, every surface.

Every SQL safety gate returns `Refusal | None`; every path through `execute_sql.execute_guarded`
returns an `Envelope`. So a caller never has to know WHICH gate fired to understand what happened,
and a decision we made is never confused with a failure the database reported.

**Stdlib only, dataclasses only, and 3.9-compatible.** Imported at runtime by `sql_guard`,
`execute_sql` and `semantic_model.runtime`, and vendored byte-identical into
`plugins/agami/lib/guardrail.py` for the marketplace layout (no pip, no package, no deps). It
therefore may not import pydantic, `contracts`, or anything outside the stdlib — pinned by the
clean-subprocess check in `tests/test_ports.py`. The 3.9 floor is separate and applies to this
module and the rest of the vendored slice only: the *package* requires 3.10+, but the plugin mirror
runs on whatever `python3` the user already has, which on stock macOS is 3.9.6. So no `match`, no
3.10-only dataclass keyword (`kw_only`, `slots`), and `X | None` only inside annotations, which
`from __future__ import annotations` keeps as lazy strings. Pinned by
`tests/test_plugin_lib_resolution.py::test_the_vendored_slice_stays_python_39_compatible`.
`Envelope.data` is an `ExecResult`, referenced under TYPE_CHECKING only: the same device
`ports.Executor` uses, for the same two reasons — dependency-freedom, and no runtime import cycle
(`execute_sql` imports THIS module).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, get_args

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
# The audit store cannot take the record, so the statement does not run (ACE-097, principle 7). A
# DEPLOYMENT-state rule, the sibling of `model_unavailable` rather than of the scope gates: neither
# says anything about the statement, and both mean this deployment cannot do a thing it promises. It
# is a rule and not a fourth REASON on purpose — `RefusalReason` stays closed at three, and the cost
# of a deployment-state refusal is one entry here plus one in `REASON_FOR_RULE`, which is a diff a
# reviewer reads.
#
# It is also the ONE refusal that is exempt from being recorded, by construction: writing a row to
# say the row could not be written is the same write failing twice. `tools._record_execution` skips
# it, and that exemption is the whole of the carve-out — every other outcome, refusals included,
# still writes.
RULE_AUDIT_UNAVAILABLE = "audit_unavailable"
# Produced by the **per-statement** timeout the executor imposes: a watchdog cancels a statement that
# outlives the configured budget, and `execute_sql.execute_guarded` turns that into this refusal. Its
# subject is the statement, which is what earns it a rule at all — "narrow the query" is a fix we can
# honestly name. The subprocess supervisor's kill is NOT that bound and must not borrow this rule: it
# stops a child that never returned, without knowing what the child was doing when it stopped, so it
# is a `failed`/`timeout` (see `FailureKind` below and contract §3). Every engine the executor speaks
# to is wired, with one recorded residual: BigQuery has no connection to cancel, so there the bound is
# server-side only and a client-side stall comes back as the executor never returning rather than as a
# statement we stopped.
RULE_RESOURCE_LIMIT = "resource_limit"
RULE_UNPARSEABLE = "unparseable"

# Metadata / recon functions: `version()`, `current_user`, `has_table_privilege(…)`, the register-type
# casts. Pinned `unsafe` by ACE-039, the gate that produces it. The reason was genuinely arguable —
# a recon call is a reach in one framing and a hazard in another — which is why it was left for the
# owning gate to settle in a reviewed diff rather than guessed at here. It is `unsafe` because the
# statement is not asking the model a question it could ask differently: a semantic model declares
# tables and columns and never declares functions, so there is no in-scope spelling of `version()`
# for a 4b `out_of_scope` refusal to point the caller toward.
RULE_RECON = "recon"
# Named by the contract, produced by a later gate — declared here so that work fills a constant
# rather than inventing a string. Deliberately absent from `REASON_FOR_RULE` below: its reason is the
# owning gate's call, and leaving it unpinned makes `refuse()` fail loudly rather than let that gate
# choose one silently.
RULE_ENGINE_MISMATCH = "engine_mismatch"
# `unscopable` is the third of these, and it is NOT a synonym for `unparseable`: an unparseable
# statement is one sqlglot cannot read at all, while an unscopable one parses perfectly and still
# cannot be checked — a table function, `ROWS FROM`, `VALUES`, `UNNEST`, or any source that is not an
# `exp.Table`, so the scope walk finds nothing to reject and would otherwise silently allow. It was
# missing from this module entirely, which is how a contract-named rule went zero-hit repo-wide
# without a test noticing; `test_ace035_guardrail_contract.py` now pins the vocabulary against the
# contract's list so neither direction of drift can recur. Left unpinned with the two above so the
# gate that produces it fills `REASON_FOR_RULE` in a reviewed diff (the contract already says that
# entry is `undetermined`, so that is a one-line change, not a decision to re-litigate).
RULE_UNSCOPABLE = "unscopable"

PRE_MODEL_RULES: frozenset[str] = frozenset(
    {RULE_READ_ONLY, RULE_RECON, RULE_AUDIT_UNAVAILABLE}
)
"""The rules decided BEFORE any semantic model is consulted, and the home for the next one.

All three gates run above the semantic-model pass — a write, a server-fingerprinting probe and an
unrecordable call are stopped without asking what the model declares — so a statement any of them
refuses never resolved a model at all. That is a fact about the DECISION rather than about the
deployment, and it has to be stated the same way whichever process decided it: the in-process path
knows no model was loaded because it holds the gate, while the fork path holds only the rule the
child sent back. Consulting this set is what
lets the two reach the same receipt, and it is what keeps the fork path from loading a model in order
to describe a refusal that never looked at one (see `RECEIPT_BEFORE_MODEL`).

A gate added above the model pass belongs here in the same diff that adds it. Leaving it out is not a
crash: it is a receipt that quietly claims no model could be resolved, which is a different fact and
an untrue one."""

REASON_FOR_RULE: dict[str, RefusalReason] = {
    RULE_READ_ONLY: "unsafe",
    RULE_TABLE_SCOPE: "out_of_scope",
    RULE_COLUMN_SCOPE: "out_of_scope",
    # `undetermined`, not `out_of_scope`: a star projection is not a reach, it is an inability to
    # decide whether there is one. Resolving `*` to a column list needs the catalog, which the guard
    # does not have, so whether the statement stays inside the declared surface cannot be decided —
    # which is what `undetermined` means. Filing it beside the two scope rules made the refusal
    # wrong about its own reason.
    RULE_SELECT_STAR: "undetermined",
    RULE_MODEL_UNAVAILABLE: "undetermined",
    # Same reason as its sibling above, and for the same argument: we did not determine that the
    # statement was safe or in scope, because we stopped before asking. Not `unsafe` — nothing about
    # the statement is in question — and not `out_of_scope`, which is a claim about the model.
    RULE_AUDIT_UNAVAILABLE: "undetermined",
    RULE_RECON: "unsafe",
    # A bound we imposed, not a property of the statement: neither unsafe nor out of scope — we
    # simply did not determine the answer within the bound. Pinned before it had a producer, so the
    # gate that imposes the per-statement timeout filled a constant rather than inventing one.
    RULE_RESOURCE_LIMIT: "undetermined",
    RULE_UNPARSEABLE: "undetermined",
    # Both pinned by the gate that finally produces them: the readability gate, which refuses a
    # statement the guard cannot read in the datasource's own grammar. They were declared above
    # without a reason so this diff would be the one that chose it, and the choice is the same for
    # both, on the same argument as their three siblings here — we did not determine the statement
    # was safe or in scope, because we could not read it well enough to ask.
    #
    # `unscopable` is the narrower of the two: the statement parsed, and still resolved to no named
    # table, so there is nothing for the scope walk to accept or reject. `engine_mismatch` says
    # nothing about the statement at all — the model declares one engine and the credentials connect
    # to another, so whichever grammar was used, one of them was wrong.
    RULE_UNSCOPABLE: "undetermined",
    RULE_ENGINE_MISMATCH: "undetermined",
}


@dataclass(frozen=True)
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
    "permission",
    "auth",
    "network",
    "dsn",
    "driver_missing",
    "timeout",
    "other",
]
"""Ten classified operational errors declared, six produced.

Produced today: `dsn`, `driver_missing`, `auth` and `syntax` from the executor's classified exit
codes, `other` from its catch-all, and `timeout` from the subprocess supervisor at the tool edge —
the bound that kills a forked executor which never returned.

**`timeout` is a failure, not a refusal, and whose decision it was is not the test on its own.** The
supervisor bound is ours, but it cannot attribute the kill to the STATEMENT: the child may have hung
in connect, credential resolution or model load, where "narrow the query" is the wrong fix. So an
unresponsive executor is `failed` / `timeout`, and its message says only that we stopped waiting
(guardrail contract §3). A **per-statement** timeout is the other case — its subject IS the
statement, so it is a refusal carrying `RULE_RESOURCE_LIMIT`, and the executor now imposes one: a
watchdog cancels the statement through the driver and the outcome leaves the chokepoint on the
refusal channel rather than this one. The two therefore coexist and stay distinguishable in the audit
trail, which is the whole point of splitting them. Driver-level connect/login timeouts fold into the
connect failure the executor already reports as `auth` (exit 4), because that is what the driver
raises.

`column_not_found`, `table_not_found`, `permission` and `network` are DECLARED BUT UNREACHABLE:
producing them means parsing driver text, and sanitizing driver text belongs to the error-hardening
slice. Declared now so those slices fill a member rather than widen the type.

**`permission` is a failure kind, not the `read_only` rule** (contract §3). `read_only` is §1's
verdict that we blocked a write; `permission` is the database refusing a READ to the connection's
role, and it stays distinct from `auth` because the credentials were accepted — the operator action
is a grant, not a re-credential. The two were one word until 2026-07-30, which left an audit row
unable to say which had happened. ACE-039 produces it."""

_FAILURE_KINDS: frozenset[str] = frozenset(get_args(FailureKind))


@dataclass(frozen=True)
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
class ReceiptSection:
    """One section of the receipt: what was established, and why the rest was not.

    The two fields are independent, which is the whole point. Four states, and the middle two are
    the ones the receipt exists to tell apart:

      * `items` set, `undetermined` None — established, here it is.
      * `items` empty, `undetermined` None — checked, and there was nothing to report.
      * `items` empty, `undetermined` set  — NOT checked, and here is why.
      * `items` set, `undetermined` set    — partly established, and here is what is missing.

    Before this, an unchecked section and a clean one were both the empty list, so silence read as
    clean. A section whose analysis has not shipped yet says what it did not establish, in a sentence
    a user can act on. It never names the spec that owns the gap: this repo is public, and an
    internal spec id resolves nowhere for the reader while disclosing unshipped work to everyone else.

    `items` is a tuple, not a list: this module's types are frozen, and a frozen dataclass holding a
    list is only shallowly immutable.
    """

    items: tuple[dict[str, Any], ...] = ()
    undetermined: str | None = None


@dataclass(frozen=True)
class Receipt:
    """What the statement did — every section declared, every gap named.

    Present on every status including `refused`, because a refused caller most needs the facts. Each
    section is always there: a consumer never has to distinguish "no joins" from "joins not checked",
    which is what `ReceiptSection.undetermined` answers.

    The sections are containers; the facts inside them belong to the specs that own each analysis
    (per-column metric match, per-join predicate, per-aggregate fan-out, per-table declared filters).
    A container whose analysis has not landed ships with `undetermined` stating what was not
    established, so the gap is visible at the point of use rather than inferred from an empty list.

    Everything here is derivable from the SQL and the model alone: no probe queries, no sampled
    values, no row contents. It is metadata and statement structure, and it must stay that way, or
    the receipt becomes a disclosure channel around the sensitive-column rules.
    """

    model_version: str | None = None
    columns: ReceiptSection = field(default_factory=ReceiptSection)
    tables: ReceiptSection = field(default_factory=ReceiptSection)
    joins: ReceiptSection = field(default_factory=ReceiptSection)
    aggregates: ReceiptSection = field(default_factory=ReceiptSection)
    assumptions: ReceiptSection = field(default_factory=ReceiptSection)

    SECTIONS: ClassVar[tuple[str, ...]] = (
        "columns",
        "tables",
        "joins",
        "aggregates",
        "assumptions",
    )
    """The section names, in order. Declared once so a caller that must touch every section (the
    undetermined-everything builders, the serializers, the tests) iterates this rather than
    re-listing them and drifting."""


# What a receipt says when it could not be built, one reason per cause — and the reasons live HERE
# rather than beside either builder, because there are two builders for the same four facts. The
# execution chokepoint assembles the receipt in-process; the tool edge assembles it again in the
# parent of a fork, since the child's Envelope is destroyed at the process boundary. Two copies of
# these sentences is two chances for the same statement to be described two ways, which is the defect
# this spec exists to remove one layer up. Each is a user-facing sentence, ending in a full stop:
# they surface next to the answer, not in a log.
RECEIPT_NO_RUNTIME = (
    "The semantic-model runtime is not available in this deployment, so nothing about the statement "
    "could be established."
)
RECEIPT_NO_MODEL = (
    "No semantic model could be resolved for this datasource, so nothing in the statement was "
    "checked."
)
RECEIPT_BUILD_FAILED = (
    "The receipt could not be assembled for this statement. The details are in the server log."
)
# The fourth is NOT a degradation at all, which is why it may not borrow any of the three above. A
# statement stopped by a `PRE_MODEL_RULES` gate was refused before anything asked the model a
# question: a model may well exist and resolve perfectly. Saying "no model could be resolved" there
# would report a deployment problem that is not happening.
RECEIPT_BEFORE_MODEL = (
    "The statement was refused before any semantic model was consulted, so nothing in it was checked "
    "against one."
)


def undetermined_receipt(reason: str, *, model_version: str | None = None) -> Receipt:
    """A receipt that establishes nothing, and gives every section the same reason why.

    What a caller gets when the receipt could not be built at all: no parser, no model, or a builder
    that raised. It is deliberately not `Receipt()` — five empty sections with no reason read as
    "checked, found nothing", which is the exact confusion `ReceiptSection.undetermined` exists to
    remove. A receipt that could not be built is a fact, not an absence.

    Iterates `Receipt.SECTIONS` rather than naming the five fields, so a section added to the type
    cannot be forgotten here and silently ship as clean. One `ReceiptSection` is shared by all five:
    it is frozen and its `items` is a tuple, so the aliasing gives a caller nothing to mutate.
    """
    section = ReceiptSection(undetermined=reason)
    return Receipt(model_version=model_version, **{name: section for name in Receipt.SECTIONS})


def receipt_from_assembled(assembled: dict[str, Any]) -> Receipt:
    """Map an assembled receipt dict — `semantic_model.runtime.assemble_receipt`'s output, or its
    refusal-bounded sibling's — onto the contract's `Receipt`.

    The assembler builds dicts and lists (it feeds a JSON chart template as well as this), the
    contract is frozen dataclasses, and this is the ONE place the two meet, so neither the executor
    nor the tool edge grows its own copy of the mapping. It lives in this module rather than next to
    the assembler because the vendored `execute_sql.py` needs it and cannot import the model stack.

    The sections are the assembled dict's own TOP-LEVEL keys, named exactly as this type names them.
    They were briefly nested under a `sections` key, which existed only so a parallel set of flat
    keys could sit beside them while both spellings shipped; with the flat keys deleted there is one
    spelling and no wrapper.

    `items` becomes a tuple because the field is one. `Receipt.SECTIONS` is indexed strictly: an
    assembled dict missing a section is a drifted builder, and every caller turns the resulting
    `KeyError` into an `undetermined` receipt rather than one that quietly lost a section.
    """
    return Receipt(
        model_version=assembled.get("model_version"),
        **{
            name: ReceiptSection(
                items=tuple(assembled[name]["items"]),
                undetermined=assembled[name]["undetermined"],
            )
            for name in Receipt.SECTIONS
        },
    )


# --- Envelope ---------------------------------------------------------------

Status = Literal["ok", "refused", "failed"]
"""Three statuses, two decisions: `ok` and `refused` are ours, `failed` is the database's."""

_STATUSES: frozenset[str] = frozenset(get_args(Status))


@dataclass(frozen=True)
class Envelope:
    """What the caller gets — one shape, every surface.

    Assembled in `execute_sql.execute_guarded`, after `executor.execute()`, so nothing can be
    smuggled past it.

    `data` is an `ExecResult` — native-typed rows. Serializing it, and enriching it with units,
    markdown and the trust receipt, stays one layer up in `tools`.

    `audit_id` is the `query_executions.id` of the row recording this execution — the same id, not a
    parallel one, so the answer and its audit trail are joined by construction.

    Field order is the contract's, verbatim, and `audit_id` carries a default only so that order
    survives: a field with no default may not follow one that has any. The empty string is not a
    valid id — `__post_init__` rejects it — so the mandatory-audit-id invariant is unchanged and the
    declared order still reads as the contract does.

    `kw_only=True` would express that more directly and was how this was first written, but the
    marketplace plugin mirror vendors this module for whatever `python3` the user already has (no
    pip, no package, no deps), and `dataclass(kw_only=…)` is 3.10+. It made the whole vendored slice
    unimportable on macOS-stock 3.9 — `sql_guard` and `execute_sql` both import this at module load —
    so the field order is kept the way that costs 3.9 nothing.
    """

    status: Status
    data: ExecResult | None = None
    refusal: Refusal | None = None
    failure: Failure | None = None
    receipt: Receipt = field(default_factory=Receipt)
    audit_id: str = ""

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
