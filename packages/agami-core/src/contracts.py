"""Shared pydantic contracts for the 4 product tools.

These pin the **data shapes** the product tools exchange — `list_datasources`,
`get_datasource_schema`, `get_prompt_examples`, `execute_sql` — plus the `ActivitySink` log
records, so downstream consumers build against fixed shapes instead of inventing their own.

The trust `receipt` is deliberately NOT one of them. It rides every `execute_sql` body on all
three statuses, and it is typed by `guardrail.Receipt` — a frozen dataclass in the stdlib-only
module the plugin mirror vendors, which is the one place both the executor and the tool edge can
reach. A second pydantic spelling of it here would be a shape nothing validated against and a
second thing to keep in step.

Source of truth = the **existing** local tool I/O in `mcp_harness.py` (the JSON each tool emits).
The shapes are the local, **subject-area-primary** model shape.

Two stances make these contracts, not a rewrite:
  - `extra="allow"` — the local serving path is the source; a richer serving backend must still
    parse, and round-tripping must not silently drop fields. So unknown keys are kept.
  - Optional/permissive where the source is — these document the surface, they don't tighten it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    # Pin the known local shape, but keep (and re-emit) anything extra a richer payload carries.
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Requests (the tool inputSchemas in mcp_harness.TOOLS)
# ---------------------------------------------------------------------------


class ListDatasourcesRequest(_Contract):
    """`list_datasources` takes no arguments."""


class SchemaRequest(_Contract):
    datasource: str | None = None
    # The two DECLARED scopes. Never-hide applies within whichever the caller states: the whole
    # datasource when neither is given, that area, or those tables.
    area: str | None = None
    dataset_names: list[str] | None = None
    # Ranks which metrics get full detail. Does NOT scope — narrowing on a ranking hint would
    # deprive a caller who never declared a scope, and the match is lexical, so a poor match would
    # hide the very metric they could not name.
    query: str | None = None
    metric_names: list[str] | None = None  # selects detail; does not scope
    mode: str | None = None  # auto | full | summary | index


class PromptExamplesRequest(_Contract):
    datasource: str | None = None
    query: str | None = None
    area: str | None = None  # narrows to one subject area PLUS the cross-area bucket
    # Applied on the served path (defaults to 10, within a ~20k-char budget); the local file path
    # returns the curated YAML whole and ignores it. The comment here used to say the opposite.
    top_k: int | None = None


class ExecuteSqlRequest(_Contract):
    sql: str
    datasource: str | None = None
    area: str | None = None
    raw_query: str | None = None


# ---------------------------------------------------------------------------
# Errors — deliberately absent.
#
# `execute_sql` speaks the guardrail Envelope on every path: a decision of ours is
# `{"status": "refused", "refusal": {reason, rule, detail, remediation}}` and the database's is
# `{"status": "failed", "failure": {kind, message}}`. Both are stdlib dataclasses in `guardrail`,
# which is vendored into the plugin slice and therefore may not import pydantic — so re-declaring
# them here as contract models would be a second, drifting copy of a shape that already has one
# owner. The `{"error": {kind, remediation}}` models this section used to hold (`ToolError` /
# `ErrorResult`) are gone for that reason; the model-backed tools that still emit that older shape
# are the ones to convert next, not a reason to keep a contract for it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_datasources
# ---------------------------------------------------------------------------


class DatasourceInfo(_Contract):
    datasource: str
    description: str | None = None  # the model's own one-liner — what a router chooses on
    database_type: str | None = None
    table_count: int = 0
    # Local path only: there it is a real check (does `<profile>/datasource.yaml` exist?). The
    # served listing is built FROM the model rows, so it omits the field rather than send a
    # constant `True` dressed as a check.
    #
    # `None`, not `False`. Defaulting to False would make a consumer that validates a served entry
    # read `model_present is False` — "no model" — for a datasource that definitionally has one:
    # the same defect this field's removal was fixing, moved one layer down into the contract.
    # Absent must say "not asserted", which is the only thing absence can honestly mean here.
    model_present: bool | None = None
    is_active: bool = False


class ListDatasourcesResult(_Contract):
    datasources: list[DatasourceInfo] = Field(default_factory=list)
    active_datasource: str | None = None
    note: str | None = None  # present only when no profiles exist


# ---------------------------------------------------------------------------
# get_datasource_schema — the structured payload (the tool also appends Markdown
# domain context as text; a serving backend can emit this structured part as JSON).
# ---------------------------------------------------------------------------


class SubjectAreaTable(_Contract):
    """One table named on an area summary: enough to decide whether to ask for its full context."""

    name: str
    description: str | None = None


class SubjectAreaSummary(_Contract):
    name: str
    description: str | None = None
    default_time_window: str | None = None
    # `summary`/`full` carry the table list; `index` carries the count instead. Exactly one of
    # these is present per mode, so both are optional here.
    tables: list[SubjectAreaTable] = Field(default_factory=list)
    table_count: int | None = None


class CrossAreaRelationship(_Contract):
    # "from" is a Python keyword — alias the wire key.
    from_: str = Field(alias="from")
    to: str
    # The endpoint tables, which are what make one edge distinguishable from the eleven others
    # between the same pair of areas.
    from_table: str | None = None
    to_table: str | None = None


class DatasourceSchemaResult(_Contract):
    datasource: str
    organization: str | None = None
    mode: str | None = None
    requested_mode: str | None = None  # present when a scope was given and no downgrade applied
    # Pass 1 (index): subject areas + cross-area relationships.
    subject_areas: list[SubjectAreaSummary] | None = None
    cross_area_relationships: list[CrossAreaRelationship] | None = None
    # The never-hide net: every metric the model declares, and the tables big enough to matter.
    metric_index: dict[str, Any] | None = None
    large_tables: dict[str, int] | None = None  # {table: estimated_row_count}
    note: str | None = None
    truncated: bool | None = None
    # The scope this response was built for: {"level", "area", "tables"}. Declared rather than
    # ridden in on `extra="allow"` — never-hide is stated relative to a scope, so the scope has to
    # be part of the contract for the guarantee to be checkable by a consumer.
    scope: dict[str, Any] | None = None
    # Pass 2 (dataset_names): per-table context + relationships/metrics from get_table_context.
    # Kept loose — these come straight from the loader and carry many provenance fields.
    tables: dict[str, Any] | None = None
    relationships: list[dict[str, Any]] | None = None
    metrics: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# get_prompt_examples — structured payload (the tool returns Markdown text when
# examples exist; the no-examples case is this JSON).
# ---------------------------------------------------------------------------


class PromptExamplesResult(_Contract):
    examples: list[Any] = Field(default_factory=list)
    note: str | None = None


# ---------------------------------------------------------------------------
# execute_sql
# ---------------------------------------------------------------------------


class ExecuteSqlResult(_Contract):
    """The `ok` half of the enveloped `execute_sql` JSON — **documentation, not a construction site**.

    What the tool actually returns is one guardrail Envelope, serialized by `tools._emit`. On the
    `ok` path that body is these fields plus three the Envelope owns: `"status": "ok"`, `receipt`
    (the contract's `guardrail.Receipt`, on every status) and `audit_id`, the `query_executions.id`
    of the row recording the execution. On the other two paths the body is a `refusal` or a
    `failure` instead, and none of the fields below appear — so this model describes one branch of
    the wire, never the whole of it.

    A pydantic `Receipt` used to live in this module and hang off this model as `data.receipt`. It
    described the flat pre-section shape, nothing constructed or validated against it, and the
    receipt it purported to type moved onto the Envelope — where `guardrail` owns it as a frozen
    dataclass, in the stdlib-only module the plugin mirror can vendor. Two spellings of one thing,
    one of them unreachable, is worse than none; the Envelope's is the one that ships.

    Nothing in the shipped code constructs or validates against this either; it is `extra="allow"`
    and it is here so a reader (or a downstream consumer building against the surface) can see the
    successful shape in one place. `guardrail` deliberately does not import it — that module is
    stdlib-only.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    units: dict[str, str] = Field(default_factory=dict)
    markdown: str | None = None  # exact full numbers (currency symbol + grouping); render as-is
    sql: str | None = None
    execution_ms: int | None = None


# ---------------------------------------------------------------------------
# ActivitySink payloads (the existing _append_jsonl records in mcp_harness)
# ---------------------------------------------------------------------------


class QueryExecutionRecord(_Contract):
    """One execute_sql execution — written for **every** outcome, not only the successful ones.

    `id` is supplied by the caller rather than minted by the sink: it is the `audit_id` the guardrail
    Envelope handed back with the answer, and it becomes `query_executions.id`. One id, so a caller
    can find the row recording its own query without a join and without two ids to reconcile.

    `status` / `reason` / `rule` are server-observed like the fields above them, but nullable:
    `status` because rows written before the guardrail contract have no verdict, `reason` and `rule`
    because only a refusal has them (an `ok` or a `failed` row leaves both NULL).

    `sql` is BOUNDED by the writer (`tools.AUDIT_SQL_MAX_CHARS`), and `sql_truncated` says whether it
    had to be. Without the flag a cut statement reads as the whole one, and a reviewer re-running it
    would not reproduce the decision the row records."""

    id: str
    ts: str
    profile: str
    sql: str
    row_count: int
    source: str
    question: str | None = None  # the user's NL question (may be absent)
    status: str | None = None  # the Envelope's status: "ok" | "refused" | "failed"
    reason: str | None = None  # guardrail.RefusalReason — refusals only
    rule: str | None = None  # guardrail.RULE_* — refusals only
    sql_truncated: bool = False  # `sql` was cut to the audit bound
    # The RAW driver error, operator-only — never the caller's `failure.message`, which is the
    # classified value-free sentence. NULL is a claim rather than a gap: it means the chokepoint
    # holding the raw text and this recorder were not in one process, which on the forked stdio
    # surface is by design (the child sanitizes, the parent records, and the raw text never
    # crosses). Bounded by the writer — `tools.AUDIT_ERROR_DETAIL_MAX_CHARS`.
    error_detail: str | None = None
    org_id: str = "local"  # the tenant this ran for; defaults to the single-tenant org
    # The three that make the row re-derivable (ACE-098, principle 7). `detail` is the refusal's own
    # sentence — NULL on every non-refusal row, because only a refusal has one — and it is where
    # "which bound fired, and what it was set to" lives, since the statement timeout and the result
    # bound share one rule id. `receipt` is the `Receipt` as JSON, all five sections including their
    # `undetermined` markers: a section nobody checked has to keep saying so in the record too.
    # Bounded by the writer (`tools.AUDIT_DETAIL_MAX_CHARS`) like `sql` and `error_detail`.
    detail: str | None = None
    receipt: str | None = None
    # Duplicated inside `receipt` on purpose: a replay must be able to SELECT on the version, and a
    # value inside a JSON blob is not portably filterable across SQLite and Postgres. NULL means
    # either a pre-migration row or a call whose receipt could not pin a version at all.
    model_version: str | None = None


class ToolCallRecord(_Contract):
    """One MCP tool call. Audit-grade fields (server-observed) are required-ish; the self-report fields
    (user_question / agent_query / thread_id) are Claude-supplied and nullable (best-effort)."""

    ts: str
    tool_name: str
    source: str
    actor: str | None = None
    datasource: str | None = None
    sql: str | None = None
    row_count: int | None = None
    execution_ms: int | None = None
    success: bool = True
    error_kind: str | None = None
    # The gate's own sentences when this call was refused (018), bounded by the writer
    # (`tools.AUDIT_DETAIL_MAX_CHARS`). `error_kind` names the rule; these say what it fired on and
    # what to do instead. Value-free by `guardrail.Refusal`'s contract, which is what makes them
    # showable to a customer's administrator — unlike `QueryExecutionRecord.error_detail`, which is
    # the driver's own text and operator-only. NULL on every non-refusal.
    refusal_detail: str | None = None
    refusal_remediation: str | None = None
    user_question: str | None = None
    agent_query: str | None = None
    thread_id: str | None = None
    correlation_id: str | None = None  # the turn: one user question -> N agent sub-queries
    # **The conversation, decided by the server** (021). `thread_id` above answers the same question
    # and is the MODEL's answer, which cannot be relied on — asked in prose it arrives on a minority
    # of calls, made required it arrives on all of them and collides. This is computed from the
    # authenticated actor, their organization and the clock, so no caller can influence it. NULL on a
    # row written before 021 and on the local single-user path, which has no store to read a previous
    # call from.
    conversation_id: str | None = None
    # This call's execution, where it ran one (019) — `query_executions.id`, the same id `_emit` puts
    # on the envelope the caller receives. `correlation_id` identifies the turn, which may run several
    # statements; this identifies the statement. NULL wherever none ran.
    audit_id: str | None = None
    # What the agent says it based this query on (020): JSON `{"entries": [...], "truncated": bool}`,
    # each entry a `kind` from a closed set, a `ref`, and a one-sentence `why`. Bounded by the writer
    # (`tools.BASIS_MAX_ENTRIES` and the two length caps) — `truncated` says the stored value is not
    # what was sent. Self-reported like `user_question`/`agent_query` above, so it is evidence of a
    # claim and never a fact; nothing here checks `ref` against `sql`. NULL on every call that did
    # not send one, which is the ordinary case.
    basis: str | None = None
    org_id: str = "local"  # the tenant this call ran for; defaults to the single-tenant org
