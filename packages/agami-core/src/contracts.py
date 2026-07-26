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
# Errors: execute_sql returns the shared guardrail Envelope (status / refusal — see
# `guardrail.py`); the other tools return a plain `{"error": {kind, remediation}}` body. Neither
# needs a pydantic type here.
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
    ts: str
    profile: str
    sql: str
    row_count: int
    source: str
    question: str | None = None  # the user's NL question (may be absent)
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


class GuardrailAuditRecord(_Contract):
    """The verdict trail for one execute_sql call, keyed by the response Envelope's `audit_id`.
    Written best-effort at the shared executor chokepoint on every surface (see `guardrail_audit`)."""

    audit_id: str
    ts: str
    status: str  # 'ok' | 'refused'
    datasource: str | None = None
    refusal_kind: str | None = None
    sql: str | None = None
    row_count: int | None = None
    execution_ms: int | None = None
    correlation_id: str | None = None
    source: str | None = None
    error_detail: str | None = None  # RAW driver error — server-side audit only, NEVER in the Envelope
