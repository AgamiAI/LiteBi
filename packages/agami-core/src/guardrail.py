"""Shared guardrail contract — ``Verdict``, ``policy``, and the response ``Envelope``.

The one result shape for the three SQL-execution guardrails: **safety** (reject) ·
**data-protection** (mask) · **governance** (warn). Every gate produces a ``Verdict``; one
``policy`` maps a verdict to an action by class + deployment tier; and every surface returns one
``Envelope``. Each guardrail builds to these types and never defines its own result shape.

**Stdlib-only, dataclasses only — keep it that way.** This module is vendored into the
marketplace plugin (``plugins/agami/lib/``) and imported by ``ports`` (which must stay
dependency-free), so it must never grow a third-party import. The vendored-purity guard depends
on this staying pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── Controlled vocabularies ──────────────────────────────────────────────────
Class = Literal["safety", "data_protection", "governance"]
Severity = Literal["high", "medium", "low"]
Certainty = Literal["provable", "heuristic", "uncertain"]
Action = Literal["allow", "reject", "mask", "row_filter", "rewrite", "warn"]
Status = Literal["ok", "refused"]
Tier = Literal["oss", "saas", "enterprise"]

# Value sets exposed for validation + tests (the `action` set and the class set are stable).
CLASSES: tuple[str, ...] = ("safety", "data_protection", "governance")
ACTIONS: tuple[str, ...] = ("allow", "reject", "mask", "row_filter", "rewrite", "warn")

# Every refusal kind the system emits, grouped by origin. `Refusal.kind` is an open ``str`` —
# operational failures (`syntax` / `auth` / `driver_missing`) come from the DB driver, not a fixed
# guardrail vocabulary — so this documents the known set (and a test pins the emit sites against it),
# rather than a `Literal` that nothing at runtime (no mypy in CI) would actually enforce. This is the
# canonical vocabulary for the whole safe-SQL feature: a few kinds are FORWARD-DECLARED here for the
# gates that land in the dependent slices on top of this contract (`unscopable_sql` — fail-closed
# scoping; `resource_limit` — the statement timeout; `recon` — the metadata deny-list), so the set is
# defined once, in the base, and the emit-site test only asserts emitted ⊆ documented (never the
# reverse, which would fail here until those slices land).
REFUSAL_KINDS: tuple[str, ...] = (
    # safety — integrity / confinement / object-scope / availability / fail-closed
    "permission",
    "table_out_of_scope",
    "column_out_of_scope",
    "select_star",
    "unscopable_sql",  # forward-declared: emitted by the fail-closed-scoping slice
    "resource_limit",  # forward-declared: emitted by the per-statement-timeout slice
    "recon",  # forward-declared: emitted by the recon deny-list slice
    "model_unavailable",
    # data-protection / governance — emitted by those gates
    "sensitive_columns",
    "preflight_refused",
    # operational / execution failures — from the executor + DB driver (classified from stderr;
    # `permission` reuses the safety kind above). The rich column/table/network kinds mirror
    # db_error_classifier.md.
    "timeout",
    "dsn",
    "network",
    "driver_missing",
    "auth",
    "column_not_found",
    "table_not_found",
    "syntax",
    "other",
)


@dataclass(frozen=True)
class Verdict:
    """What a single gate returns.

    ``cls`` is serialized as ``class`` (a Python keyword can't be a field name). ``certainty``
    is the axis ``policy`` keys on: **safety** emits ``uncertain`` ⇒ reject (fail-closed on
    doubt); **governance** emits ``heuristic`` ⇒ warn (undecidable) or ``provable`` ⇒
    reject/rewrite at an enforcing tier. ``severity`` is load-bearing only on the provable
    governance path. ``rewritten_sql`` is present only when the action is ``rewrite``.
    """

    cls: Class
    rule: str
    severity: Severity
    certainty: Certainty
    detail: str
    remediation: str
    rewritten_sql: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "class": self.cls,
            "rule": self.rule,
            "severity": self.severity,
            "certainty": self.certainty,
            "detail": self.detail,
            "remediation": self.remediation,
        }
        if self.rewritten_sql is not None:
            d["rewritten_sql"] = self.rewritten_sql
        return d


def safety_verdict(rule: str, detail: str, remediation: str) -> Verdict:
    """Build a safety-class ``Verdict``. Safety findings are always ``provable`` + ``high`` — a
    safety gate that fires is deterministic and always rejects. ``rule`` names the gate (e.g.
    ``read_only``, ``table_scope``, ``column_scope``, ``no_select_star``)."""
    return Verdict(
        cls="safety",
        rule=rule,
        severity="high",
        certainty="provable",
        detail=detail,
        remediation=remediation,
    )


@dataclass(frozen=True)
class Refusal:
    """The ``refusal`` block of a refused ``Envelope``: why the query was rejected, with a
    remediation.

    ``reason`` is ALWAYS value-free — it never carries raw SQL or raw DB driver text (schema / column
    / value names, a DSN), which must not cross the boundary between the model and the customer's
    database. A **guardrail** refusal (a safety or model gate) reasons from the model; an
    **operational** failure (a DB syntax / connection error surfaced by the executor) is classified
    into a fixed value-free reason, with the raw driver text captured separately for the server-side
    audit trail only. (The operational-error sanitization is wired by the recon/error-hardening
    slice; the tool layer classifies via ``_classify_db_error`` on both surfaces.)"""

    kind: str  # one of REFUSAL_KINDS — an open str (operational kinds come from the DB driver)
    reason: str
    remediation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "remediation": self.remediation}


@dataclass(frozen=True)
class Envelope:
    """The one shape every surface returns.

    ``data`` is absent when refused; ``applied`` records transforms/bounds actually applied — today
    only the fetch bound ``{row_cap: N}`` is wired; ``{mask: col}`` / ``{row_filter: expr}`` /
    ``{rewrite: reason}`` land with the data-protection / governance gates. ``warnings`` are
    governance annotations (``Verdict``s); ``refusal`` is present iff ``status == 'refused'``;
    ``audit_id`` references the recorded verdict trail.
    """

    status: Status
    audit_id: str = ""
    data: Any | None = None
    applied: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[Verdict] = field(default_factory=list)
    refusal: Refusal | None = None

    @classmethod
    def refused(
        cls,
        refusal: Refusal,
        *,
        audit_id: str = "",
        warnings: list[Verdict] | None = None,
    ) -> Envelope:
        return cls(status="refused", audit_id=audit_id, refusal=refusal, warnings=warnings or [])

    @classmethod
    def ok(
        cls,
        data: Any,
        *,
        audit_id: str = "",
        applied: list[dict[str, Any]] | None = None,
        warnings: list[Verdict] | None = None,
    ) -> Envelope:
        return cls(
            status="ok",
            audit_id=audit_id,
            data=data,
            applied=applied or [],
            warnings=warnings or [],
        )

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "audit_id": self.audit_id}
        if self.data is not None:
            d["data"] = self.data
        d["applied"] = list(self.applied)
        d["warnings"] = [w.as_dict() for w in self.warnings]
        if self.refusal is not None:
            d["refusal"] = self.refusal.as_dict()
        return d


def policy(verdict: Verdict, tier: Tier = "oss") -> Action:
    """Map a verdict to an action by class + deployment tier.

    The class is read from ``verdict.cls`` (it already lives on the verdict, so it is not passed
    as a separate argument).

    - **safety** → always ``reject``, every tier; ``certainty == 'uncertain'`` also ⇒ ``reject``
      (fail-closed on doubt). Fully implemented here.
    - **data_protection** → ``mask`` / ``row_filter`` where the gate names one, else ``reject``
      (fail-closed). *Stub:* the data-protection gates fill this branch in later work; until then
      it fails closed.
    - **governance** → graded by (severity, certainty, tier): ``reject``/``rewrite`` only for a
      ``provable`` verdict under an enforcing tier, else ``warn`` (OSS warns · SaaS recommends ·
      Enterprise enforces). *Stub:* the governance gates fill this branch in later work.
    """
    if verdict.cls == "safety":
        # Safety is absolute: reject in every tier; uncertainty is already a reject (fail-closed).
        return "reject"
    if verdict.cls == "data_protection":
        # Stub — fill mask/row_filter selection later. Fail closed until then (the safe default).
        return "reject"
    if verdict.cls == "governance":
        # Stub — the graded (severity, certainty, tier) logic is filled in later.
        if tier == "enterprise" and verdict.certainty == "provable":
            return "rewrite" if verdict.rewritten_sql is not None else "reject"
        return "warn"
    raise ValueError(f"unknown verdict class: {verdict.cls!r}")
