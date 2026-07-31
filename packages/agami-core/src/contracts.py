"""Shared pydantic contracts for the 4 product tools.

These pin the **data shapes** the product tools exchange — `list_datasources`,
`get_datasource_schema`, `get_prompt_examples`, `execute_sql` (incl. the trust `receipt`)
— plus the `ActivitySink` log records, so downstream consumers build against
fixed shapes instead of inventing their own.

Source of truth = the **existing** local tool I/O in `mcp_harness.py` (the JSON each tool emits)
and `semantic_model/runtime.assemble_receipt`. The shapes are the local, **subject-area-primary**
model shape.

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
    dataset_names: list[str] | None = None
    query: str | None = None  # context only; not used for ranking locally


class PromptExamplesRequest(_Contract):
    datasource: str | None = None
    query: str | None = None
    top_k: int | None = None  # accepted for parity; not applied locally


class ExecuteSqlRequest(_Contract):
    sql: str
    datasource: str | None = None
    area: str | None = None
    raw_query: str | None = None
    max_rows: int | None = None


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
    database_type: str | None = None
    table_count: int = 0
    model_present: bool = False
    is_active: bool = False


class ListDatasourcesResult(_Contract):
    datasources: list[DatasourceInfo] = Field(default_factory=list)
    active_datasource: str | None = None
    note: str | None = None  # present only when no profiles exist


# ---------------------------------------------------------------------------
# get_datasource_schema — the structured payload (the tool also appends Markdown
# domain context as text; a serving backend can emit this structured part as JSON).
# ---------------------------------------------------------------------------


class SubjectAreaSummary(_Contract):
    name: str
    description: str | None = None
    default_time_window: str | None = None
    tables: list[str] = Field(default_factory=list)


class CrossAreaRelationship(_Contract):
    # "from" is a Python keyword — alias the wire key.
    from_: str = Field(alias="from")
    to: str
    for_questions_about: str | None = None


class DatasourceSchemaResult(_Contract):
    datasource: str
    organization: str | None = None
    # Pass 1 (index): subject areas + cross-area relationships.
    subject_areas: list[SubjectAreaSummary] | None = None
    cross_area_relationships: list[CrossAreaRelationship] | None = None
    note: str | None = None
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
# execute_sql — incl. the trust receipt (runtime.assemble_receipt)
# ---------------------------------------------------------------------------


class TableUsed(_Contract):
    qname: str
    rows: int | None = None
    rows_as_of: str | None = None
    freshness: str | None = None


class Receipt(_Contract):
    """The trust receipt — deterministic provenance for an answer (no LLM).

    tables_used / relationships / metrics / named_filters / assumptions / warnings, plus the
    SQL and model_version. relationships and metrics carry many sign-off/review fields straight
    from the model, so they stay loose (dicts) — the shape is owned by assemble_receipt.
    """

    sql: str | None = None
    model_version: str | None = None
    tables_used: list[TableUsed] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    named_filters: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[Any] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExecuteSqlResult(_Contract):
    """The `ok` half of the enveloped `execute_sql` JSON — **documentation, not a construction site**.

    What the tool actually returns is one guardrail Envelope, serialized by `tools._emit`. On the
    `ok` path that body is these fields plus two the Envelope owns: `"status": "ok"` and `audit_id`,
    the `query_executions.id` of the row recording the execution. On the other two paths the body is
    a `refusal` or a `failure` instead, and none of the fields below appear — so this model describes
    one branch of the wire, never the whole of it.

    Nothing in the shipped code constructs or validates against this; it is `extra="allow"` and it is
    here so a reader (or a downstream consumer building against the surface) can see the successful
    shape in one place. `guardrail` deliberately does not import it — that module is stdlib-only.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    units: dict[str, str] = Field(default_factory=dict)
    markdown: str | None = None  # exact full numbers (currency symbol + grouping); render as-is
    sql: str | None = None
    execution_ms: int | None = None
    receipt: Receipt | None = None


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
    because only a refusal has them (an `ok` or a `failed` row leaves both NULL)."""

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
    org_id: str = "local"  # the tenant this ran for; defaults to the single-tenant org


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
    user_question: str | None = None
    agent_query: str | None = None
    thread_id: str | None = None
    correlation_id: str | None = None  # the turn: one user question -> N agent sub-queries
    org_id: str = "local"  # the tenant this call ran for; defaults to the single-tenant org
