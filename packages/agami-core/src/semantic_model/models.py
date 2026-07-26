"""Pydantic v2 models for the agami semantic-model-v2 hierarchy.

Hierarchy (see the design doc's "The new hierarchy" section):

    Organization
    ├─ description
    ├─ storage_connections[]        (physical — host, port, creds, dialect)
    ├─ subject_areas[]              (logical — the primary semantic unit)
    │  └─ SubjectArea
    │     ├─ tables[]               (TableRef into storage connections)
    │     ├─ tables_defined[]       (canonical Table definitions)
    │     ├─ entities[]
    │     ├─ metrics[]
    │     └─ relationships[]
    └─ cross_subject_area_relationships[]

Design intent baked into the types:

* **Provider portability** — declarative fields (default_filters, value_transform,
  caveats, value_pattern, sensitive, default_time_window) live on the model, not
  in prose the LLM must re-interpret.
* **Standard concepts** — tables, columns, entities, metrics, relationships,
  hierarchies, choice fields. No bespoke `rules[]` taxonomy.
* **Trust block parity** — every Relationship (intra- and cross-area) carries the
  same confidence / review_state / signed_off_* fields. No second-class shape.

These models are the *parse + structural-validation* layer. Cross-cutting
invariants that need the whole model in view (sizing, orphan refs, type-compat,
name collisions, …) live in `validator.py`, which consumes parsed models.

All models forbid unknown fields (`extra="forbid"`) so typos surface as errors
rather than being silently dropped — the same guarantee the legacy jsonschema
validator gives via `additionalProperties: false`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def bare_name(qualified: str) -> str:
    """A table name without its schema/area prefix: ``sales.orders`` -> ``orders``.
    Single source of truth so the several places that strip the prefix stay in lockstep."""
    return qualified.split(".")[-1]


# ---------------------------------------------------------------------------
# Shared enums / literals
# ---------------------------------------------------------------------------

# Simple, provider-neutral column types (mirrors the legacy agami.type set).
ColumnType = Literal[
    "string",
    "integer",
    "decimal",
    "float",
    "boolean",
    "date",
    "timestamp",
    "time",
    "json",
    "array",
    "uuid",
    "bytes",
]

Confidence = Literal["confirmed", "inferred", "proposed"]
ReviewState = Literal["unreviewed", "approved", "rejected", "stale", "not_applicable"]
# How a column may be aggregated when used as a MEASURE. Column-INTRINSIC only:
# semi-additivity (additive over some dimensions but not others — e.g. an account
# balance is summable across accounts but NOT across time) is a column×dimension
# property and lives on the metric as `non_additive_dimensions`, NOT here.
#   additive     → SUM is meaningful (amount, quantity, revenue). Semi-additive measures
#                  are ALSO `additive` here — the time exception is declared on the metric.
#   averageable  → AVG/MIN/MAX meaningful, SUM is NOT (unit_price, rate, ratio, score).
#   dimension    → not a measure; a grouping key (ids, codes, year, zip) — never aggregated.
#   unknown      → not yet classified (legacy models, or the heuristic was unsure). Never
#                  enforced against — the enforcement layer only acts on a definite class.
Aggregation = Literal["additive", "averageable", "dimension", "unknown"]
# How a SEMI-ADDITIVE metric is collapsed over a dimension it can't be summed across
# (almost always time): take the period-end (`last`) / period-start (`first`) value, or an
# `average`/`min`/`max`. None on a metric with `non_additive_dimensions` means "don't sum
# over them — refuse/warn rather than auto-collapse."
SemiAdditiveAgg = Literal["last", "first", "average", "min", "max"]
# Provenance of a table/column `description` (NOT a sign-off gate — advisory only).
#   None    → unknown / legacy; treated as trusted, never surfaced for confirmation
#   human   → written or edited by a person; trusted
#   ai_unvalidated → AI-generated, not yet confirmed in an accepted answer
#   ai_validated   → AI-generated, confirmed by a human (via a receipt or explicitly)
#   ai_unknown     → the AI looked at an opaque column (e.g. `xyz`, `v_1`) and could NOT
#                    determine its meaning. Description stays empty; flagged so a human can
#                    fill it in. The inverse of ai_unvalidated: "I don't know" vs "I guessed".
# Descriptions earn trust through USE: agami-query surfaces an `ai_unvalidated` description in
# the answer receipt when the query actually used that column, so the human confirms it in
# context instead of rubber-stamping a giant list. An `ai_unknown` column used in an answer
# is surfaced the same way ("I used `xyz` but don't know what it is — is this right?").
DescriptionSource = Literal["human", "ai_unvalidated", "ai_validated", "ai_unknown", "metadata"]
Cardinality = Literal["many_to_one", "one_to_many", "one_to_one"]
JoinType = Literal["INNER", "LEFT", "RIGHT", "FULL", "CROSS"]
Executable = Literal["same_engine", "split", "informational"]
SourceType = Literal["table", "sql"]
StorageType = Literal[
    "PostgreSQL",
    "MySQL",
    "Snowflake",
    "BigQuery",
    "Redshift",
    "SQLite",
    "DuckDB",
    "SQLServer",
    "Databricks",
    "Trino",
    "Oracle",
]
# Supabase is hosted PostgreSQL — it maps to storage_type="PostgreSQL", not a
# distinct value (same wire protocol, driver, and catalog).


class _Base(BaseModel):
    """Base config shared by every v2 model: forbid unknown keys, strip strings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Trust block — carried by relationships (and reusable elsewhere)
# ---------------------------------------------------------------------------


class TrustBlock(_Base):
    """Attribution + review state. Parity requirement: intra- and cross-area
    relationships both carry this. `signed_off_*` become required once a thing
    is `approved` (enforced in validator.py, which can see the whole model)."""

    confidence: Confidence = "proposed"
    review_state: ReviewState = "unreviewed"
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None
    # Provenance: set by the migration tool so re-runs are idempotent.
    migrated_from: Optional["MigratedFrom"] = None


class MigratedFrom(_Base):
    """Idempotency marker emitted by the migration tool. Each migrated item
    records where it came from so a second migration run is a no-op."""

    source_file: str
    source_line_hash: Optional[str] = None
    tool_version: Optional[str] = None


# ---------------------------------------------------------------------------
# Storage Connection (physical layer)
# ---------------------------------------------------------------------------


class StorageConnection(_Base):
    """Physical storage: where the data lives + how to reach it. No conventions,
    no rules, no entity definitions — purely physical. `storage_config` values
    are env-var *names* or references, never literal secrets."""

    name: str
    storage_type: StorageType
    storage_config: dict[str, Any] = Field(default_factory=dict)
    storage_type_override: Optional[str] = None
    entity_metadata_config: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Column
# ---------------------------------------------------------------------------


class ForeignKey(_Base):
    """FK target. Supports the standard simple form plus the polymorphic
    discriminator pattern (discriminator_column + target_tables + where)."""

    table: Optional[str] = None
    column: Optional[str] = None
    # Polymorphic FK: the value of `discriminator_column` selects which of
    # `target_tables` this row points at.
    discriminator_column: Optional[str] = None
    target_tables: Optional[list[str]] = None
    where: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "ForeignKey":
        is_polymorphic = self.discriminator_column is not None or self.target_tables is not None
        if is_polymorphic:
            if not self.target_tables:
                raise ValueError("polymorphic foreign_key requires non-empty target_tables")
        else:
            if not (self.table and self.column):
                raise ValueError("simple foreign_key requires both table and column")
        return self

    @property
    def is_polymorphic(self) -> bool:
        return self.discriminator_column is not None or bool(self.target_tables)


class DenormalizedFrom(_Base):
    """This column mirrors a value reachable via FK; the MCP can skip the join."""

    table: str
    column: str
    via: Optional[str] = None
    where: Optional[str] = None
    freshness: Optional[str] = None


class Column(_Base):
    name: str
    type: ColumnType
    description: str = ""
    # provenance of `description` — drives "earn trust through use" (advisory, not a gate)
    description_source: Optional[DescriptionSource] = None
    primary_key: bool = False
    foreign_key: Optional[ForeignKey] = None
    # enum semantics: maps stored value -> human meaning
    choice_field: Optional[dict[str, str]] = None
    # How this column may be aggregated as a measure (additive / averageable / dimension).
    # Set by an introspection heuristic, refined by the curator. Consumed by the query-time
    # enforcement layer (e.g. refuse SUM of an `averageable` price, or AVG of a `dimension`
    # id). `unknown` is never enforced against. See the `Aggregation` literal above.
    aggregation: Aggregation = "unknown"
    sensitive: bool = False
    # declarative cleaning/transform SQL (regexp_replace, TO_TIMESTAMP, …)
    value_transform: Optional[str] = None
    # Display unit for the column's values. A currency ISO code (INR/USD/EUR/…)
    # drives a deterministic symbol + grouping in the formatter/chart renderer;
    # other units (cents, percent, days, ms) surface as a label. This is the
    # structured home for the onboarding "what currency are these in?" answer —
    # beats a prose caveat the LLM has to re-interpret on every query.
    unit: Optional[str] = None
    # How a date/time value is ENCODED in storage, when the column type doesn't
    # already say it (sniffed at introspection). Drives deterministic human-readable
    # rendering: epoch_s/epoch_ms/epoch_us/epoch_ns (integer Unix time → UTC datetime),
    # yyyymmdd (integer 20240115 → date), iso8601 (string). None for native
    # date/timestamp columns (the DB already returns a readable value).
    date_format: Optional[str] = None
    # Timezone the column's timestamps are in, when known. Epoch is always "UTC";
    # a native TIMESTAMPTZ may carry an offset; a naive TIMESTAMP is "naive" (no tz
    # stored). Surfaced so answers can state it and SQL converts correctly.
    timezone: Optional[str] = None
    denormalized_from: Optional[DenormalizedFrom] = None
    caveats: list[str] = Field(default_factory=list)
    # curation/trust — structure is trusted by default (introspected); the curator
    # sets review_state='rejected' via /agami-model to exclude a column from the
    # runtime, or signs off enriched detail via /agami-model.
    confidence: Confidence = "confirmed"
    review_state: ReviewState = "approved"
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None

    @field_validator("caveats")
    @classmethod
    def _caveats_nonempty(cls, v: list[str]) -> list[str]:
        for c in v:
            if not c or not c.strip():
                raise ValueError("caveats entries must be non-empty strings")
        return v


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


class IndexHint(_Base):
    columns: list[str]
    is_unique: bool = False


class PerformanceHints(_Base):
    estimated_row_count: Optional[int] = None
    # ISO-8601 UTC timestamp of when `estimated_row_count` was last measured (introspection
    # time). Lets the trust receipt show "≈N rows (estimated as of <date>)" so a reader knows
    # the count is a point-in-time estimate, not a live COUNT(*).
    estimated_row_count_at: Optional[str] = None
    recommended_filters: list[str] = Field(default_factory=list)
    indexes: list[IndexHint] = Field(default_factory=list)


class Table(_Base):
    """Canonical table definition (lives in a subject area's tables_defined[])."""

    name: str
    schema_name: Optional[str] = Field(default=None, alias="schema")
    storage_connection: Optional[str] = None

    source_type: SourceType = "table"
    sql: Optional[str] = None  # required when source_type == "sql"

    # composite-key-aware primary key; the source of truth for grain.
    grain: list[str] = Field(default_factory=list)
    description: str = ""  # ONE line — what is this table
    # provenance of `description` — drives "earn trust through use" (advisory, not a gate)
    description_source: Optional[DescriptionSource] = None

    default_filters: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    parent_table: Optional[str] = None
    child_table: Optional[str] = None

    performance_hints: Optional[PerformanceHints] = None

    # logical column groupings; REQUIRED on deep tables (enforced in validator).
    column_groups: dict[str, list[str]] = Field(default_factory=dict)
    # OPTIONAL one-line gloss per column-group name (e.g. {"references": "FK columns linking
    # to other tables"}). Lets the explorer/MCP explain a group instead of showing a bare key.
    # Keys SHOULD be a subset of column_groups; extras are ignored, missing ones just render
    # without a description.
    column_group_descriptions: dict[str, str] = Field(default_factory=dict)

    # importer composition hook (v2; nothing consumes it in v1).
    inherits_columns_from: Optional[str] = None

    # curation/trust — structure trusted by default; reject via /agami-model to
    # exclude the whole table from the runtime.
    confidence: Confidence = "confirmed"
    review_state: ReviewState = "approved"
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None

    columns: list[Column] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    @model_validator(mode="after")
    def _source_shape(self) -> "Table":
        if self.source_type == "sql" and not (self.sql and self.sql.strip()):
            raise ValueError(f"table {self.name!r}: source_type='sql' requires a non-empty sql field")
        if self.source_type == "table" and self.sql:
            raise ValueError(f"table {self.name!r}: sql must be empty when source_type='table'")
        return self

    @field_validator("caveats")
    @classmethod
    def _caveats_nonempty(cls, v: list[str]) -> list[str]:
        for c in v:
            if not c or not c.strip():
                raise ValueError("caveats entries must be non-empty strings")
        return v

    # --- convenience accessors used by loader/validator ---

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    def get_column(self, name: str) -> Optional[Column]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def is_deep(self) -> bool:
        """Deep tables (>= ~30 columns) must declare column_groups."""
        return len(self.columns) >= DEEP_TABLE_COLUMN_THRESHOLD


DEEP_TABLE_COLUMN_THRESHOLD = 30


# ---------------------------------------------------------------------------
# TableRef (subject-area membership reference)
# ---------------------------------------------------------------------------


class TableRef(_Base):
    """A subject area references tables via TableRefs, not by duplicating them.
    `expose_column_groups` scopes which of the canonical table's column_groups
    are visible in *this* area (lets one wide table appear in several areas with
    different views). None => all columns visible."""

    storage_connection: str
    schema_name: Optional[str] = Field(default=None, alias="schema")
    table: str
    expose_column_groups: Optional[list[str]] = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class EntityMapping(_Base):
    table: str
    column: str
    primary: bool = False
    default_filter: Optional[str] = None


class Entity(_Base):
    name: str
    plural: Optional[str] = None
    other_names: list[str] = Field(default_factory=list)
    description: str = ""
    maps_to: list[EntityMapping] = Field(default_factory=list)
    # OPTIONAL display anchor — the single table to file this entity under in the explorer/MCP.
    # An entity maps to many tables by design (`maps_to`); this just names the one a reader
    # would consider its home (usually the primary mapping's table). None = list it un-anchored.
    primary_table: Optional[str] = None
    # declarative opaque-literal identification (provider-neutral regex)
    value_pattern: Optional[str] = None
    value_format_hint: Optional[str] = None
    parent_entity: Optional[str] = None
    key_components: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    # rare override: force a clarifying question even when resolution is confident
    clarification_strictness: Optional[Literal["normal", "high"]] = None
    # curation/trust — LLM-proposed entities default approved; reject to drop.
    confidence: Confidence = "confirmed"
    review_state: ReviewState = "approved"
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None

    @property
    def resolved_primary_table(self) -> Optional[str]:
        """Display anchor for the explorer/MCP: the explicit `primary_table` if set, else the
        primary `maps_to` table (or the first mapping). None only when it maps to nothing — so an
        entity gets filed under a table without anyone having to set `primary_table` by hand."""
        if self.primary_table:
            return self.primary_table
        for m in self.maps_to:
            if m.primary:
                return m.table
        return self.maps_to[0].table if self.maps_to else None

    @model_validator(mode="after")
    def _one_primary(self) -> "Entity":
        if self.maps_to:
            primaries = [m for m in self.maps_to if m.primary]
            if len(primaries) > 1:
                raise ValueError(
                    f"entity {self.name!r}: at most one maps_to entry may be primary "
                    f"(found {len(primaries)})"
                )
        return self


# ---------------------------------------------------------------------------
# Metric (unified across table / DS-overlay / org-overlay levels)
# ---------------------------------------------------------------------------


class Metric(_Base):
    name: str
    description: str = ""
    other_names: list[str] = Field(default_factory=list)
    # prose intent (provider-portable; never empty so the model isn't binding-only)
    calculation: str
    # per-storage_type SQL bindings, e.g. {"PostgreSQL": "COUNT(DISTINCT ...)"}
    bindings: dict[str, str] = Field(default_factory=dict)
    # display unit of the metric's output (e.g. a currency ISO code) so the
    # formatter renders results deterministically (₹1,23,456 not 123456)
    unit: Optional[str] = None
    source_tables: list[str] = Field(default_factory=list)
    base_metrics: list[str] = Field(default_factory=list)
    subject_areas: list[str] = Field(default_factory=list)
    # OPTIONAL display anchor — the single table this metric is primarily "about", so the
    # explorer/MCP can present it under that table. NOT ownership and NOT a scope change: a
    # metric still lives in its subject area and may join several `source_tables`; this just
    # picks the one a reader would file it under (usually source_tables[0]). None = no single
    # home (a genuinely cross-table metric); the UI then lists it un-anchored.
    primary_table: Optional[str] = None
    # Additivity (scorecard #3). A SEMI-ADDITIVE measure (account balance, inventory,
    # headcount, point-in-time subscribers) is summable across some dimensions but NOT
    # across others — almost always time. `non_additive_dimensions` names those (a column
    # name, or the shorthand "time" = any date/time grain in the query); `semi_additive_agg`
    # says how to collapse over them (period-end `last`, `average`, …). With dims set but no
    # agg, the enforcement layer (#4) refuses/warns instead of auto-collapsing. A
    # fully-additive metric leaves both empty.
    non_additive_dimensions: list[str] = Field(default_factory=list)
    semi_additive_agg: Optional[SemiAdditiveAgg] = None
    # Second-order statistic (scorecard #1, case b): an aggregate OF an aggregate at a finer
    # grain — AVG of a daily SUM, MAX of a monthly COUNT. The binding is `OUTERAGG({base})`
    # (e.g. "AVG({daily_revenue})") and `inner_grain` names the dimension(s) the BASE metric is
    # grouped by first. The engine deterministically synthesizes the CTE (compute the base at
    # `inner_grain`, then apply the outer aggregate) — illegal AVG(SUM(...)) is never emitted.
    # Empty for first-order / case-(a) metrics.
    inner_grain: list[str] = Field(default_factory=list)
    business_question: Optional[str] = None
    confidence: Confidence = "proposed"
    source: Optional[str] = None
    review_state: ReviewState = "unreviewed"
    # sign-off attribution (Rule 1: metrics require sign-off before runtime use)
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None

    @field_validator("calculation")
    @classmethod
    def _calc_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("metric calculation (prose intent) must be non-empty")
        return v

    @model_validator(mode="after")
    def _semi_additive_coherent(self) -> "Metric":
        # "how to collapse over a non-additive dimension" is meaningless without naming
        # the dimension(s). (The reverse is fine: dims without an agg = refuse to sum.)
        if self.semi_additive_agg and not self.non_additive_dimensions:
            raise ValueError(
                "semi_additive_agg is set but non_additive_dimensions is empty — name the "
                "dimension(s) the metric can't be summed over (e.g. [\"time\"])"
            )
        return self


# ---------------------------------------------------------------------------
# Relationship (intra-subject-area)
# ---------------------------------------------------------------------------


class Relationship(_Base):
    """A join edge. Either the simple FK form (from_column + to_column) OR the
    `on:` SQL-expression escape hatch — exactly one (enforced below). Carries a
    REQUIRED cardinality (the fan-trap detector consumes it) and the full trust
    block."""

    from_table: str
    to_table: str
    from_column: Optional[str] = None
    to_column: Optional[str] = None
    # Schema (namespace) each endpoint lives in. Stamped at introspection so a join that
    # spans two schemas of the same DB is visible AS cross-schema, and so two schemas that
    # each have a same-named table don't get silently conflated. None for schema-less DBs
    # (SQLite) or models written before schema-qualified relationships shipped.
    from_schema: Optional[str] = None
    to_schema: Optional[str] = None
    # SQL-expression escape hatch (CAST, compound, function-based joins).
    on: Optional[str] = None
    join_type: JoinType = "LEFT"
    # REQUIRED — no default. cardinality the planner needs.
    relationship: Cardinality
    executable: Executable = "same_engine"
    description: str = ""
    for_questions_about: list[str] = Field(default_factory=list)

    # trust block (flattened for ergonomic YAML authoring)
    confidence: Confidence = "proposed"
    review_state: ReviewState = "unreviewed"
    signed_off_by: Optional[str] = None
    signed_off_at: Optional[str] = None
    signed_off_role: Optional[str] = None
    migrated_from: Optional[MigratedFrom] = None

    @model_validator(mode="after")
    def _completeness(self) -> "Relationship":
        # Exactly one of (from_column + to_column) OR (on:).
        simple = self.from_column is not None and self.to_column is not None
        partial_simple = (self.from_column is not None) ^ (self.to_column is not None)
        has_on = self.on is not None and self.on.strip() != ""
        if partial_simple and not has_on:
            raise ValueError(
                f"relationship {self.from_table}->{self.to_table}: simple form requires "
                "BOTH from_column and to_column"
            )
        if simple and has_on:
            raise ValueError(
                f"relationship {self.from_table}->{self.to_table}: specify exactly one of "
                "(from_column + to_column) OR (on:), not both"
            )
        if not simple and not has_on:
            raise ValueError(
                f"relationship {self.from_table}->{self.to_table}: must specify either "
                "(from_column + to_column) OR (on:)"
            )
        return self

    @property
    def cross_schema(self) -> bool:
        """True when this edge joins two different schemas (both endpoints stamped).
        These are architectural claims worth a human glance — the Review tab badges them."""
        return bool(self.from_schema and self.to_schema and self.from_schema != self.to_schema)


class CrossSubjectAreaRelationship(Relationship):
    """Org-level edge between two subject areas. Same shape as Relationship plus
    the area endpoints. Trust-block parity is inherited (Gap 1)."""

    from_subject_area: str
    to_subject_area: str


# ---------------------------------------------------------------------------
# Subject Area (the primary semantic unit)
# ---------------------------------------------------------------------------


class SubjectArea(_Base):
    name: str
    description: str = ""
    default_time_window: Optional[str] = None
    tables: list[TableRef] = Field(default_factory=list)
    tables_defined: list[Table] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    def defined_table(self, name: str) -> Optional[Table]:
        for t in self.tables_defined:
            if t.name == name:
                return t
        return None


# ---------------------------------------------------------------------------
# Organization (top level)
# ---------------------------------------------------------------------------


class Organization(_Base):
    # Locally-minted, globally-unique deployment identity (F14 / ACE-056): a uuid4 hex minted ONCE at
    # agami-connect and persisted in org.yaml, then immutable. Optional/None so pre-F14 org.yaml files
    # (which have no org_id key) still load under the model's `extra="forbid"` policy. Never transmitted;
    # it only travels the day the operator exports their own DB — see F14's no-egress invariant.
    org_id: Optional[str] = None
    organization: str
    version: int = 1
    description: str = ""
    fiscal_year_start_month: int = 1
    storage_connections: list[StorageConnection] = Field(default_factory=list)
    subject_areas: list[SubjectArea] = Field(default_factory=list)
    cross_subject_area_relationships: list[CrossSubjectAreaRelationship] = Field(
        default_factory=list
    )
    # cross-cutting entities/metrics that unify multiple subject areas
    cross_subject_area_entities: list[Entity] = Field(default_factory=list)
    cross_subject_area_metrics: list[Metric] = Field(default_factory=list)
    # domain glossary: term -> one-line definition (e.g. "MRR": "monthly recurring revenue").
    # Enrichment fills this from decoded abbreviations + choice-field legends; org_draft
    # renders it into ORGANIZATION.md's "Key terminology", and it feeds NL→SQL as context.
    # The structured home means it survives an ORGANIZATION.md regeneration (a prose section
    # the LLM has to remember to write does not).
    key_terminology: dict[str, str] = Field(default_factory=dict)

    @field_validator("fiscal_year_start_month")
    @classmethod
    def _fy_month(cls, v: int) -> int:
        if not 1 <= v <= 12:
            raise ValueError("fiscal_year_start_month must be 1..12")
        return v

    def subject_area(self, name: str) -> Optional[SubjectArea]:
        for sa in self.subject_areas:
            if sa.name == name:
                return sa
        return None

    def storage_connection(self, name: str) -> Optional[StorageConnection]:
        for sc in self.storage_connections:
            if sc.name == name:
                return sc
        return None


# ---------------------------------------------------------------------------
# Organization record (F15 / ACE-067) — the deployment-level company record that
# sits ABOVE the per-datasource Organization models. One per artifacts dir.
# ---------------------------------------------------------------------------


class DisplayConventions(_Base):
    """Company-wide, machine-actionable rendering conventions the SQL/answer path can act on
    directly rather than parse out of prose. All optional; `notes` carries free-form conventions
    that don't fit a structured slot."""

    currency: Optional[str] = None
    rounding: Optional[int] = None
    week_start: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class OrgRecord(_Base):
    """The deployment-level company record (F15). Written ONCE at ``<artifacts_dir>/organization.yaml``
    and shared across every datasource under the deployment, so company-wide facts (name, description,
    fiscal year, display conventions, glossary) live in one place instead of drifting across each
    profile's ``org.yaml``. ``org_id`` is F14's deployment identity, relocated here from the per-profile
    ``org.yaml`` (minted once, immutable, deployment-scoped — the value is preserved, never re-minted).
    Every non-id field is optional so a bare record (id only) validates; company content is authored
    later by onboarding/curation."""

    org_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None
    display_conventions: DisplayConventions = Field(default_factory=DisplayConventions)
    # Company-wide glossary: term -> one-line definition. Same shape as Organization.key_terminology
    # (the established glossary type), but scoped to the COMPANY rather than a single datasource.
    glossary: dict[str, str] = Field(default_factory=dict)
    # The datasources (profile names) attached under this org. Auto-maintained: rebuilt from the profile
    # directories present on disk on every onboard/deploy, never hand-edited, so it cannot drift.
    datasources: list[str] = Field(default_factory=list)

    @field_validator("fiscal_year_start_month")
    @classmethod
    def _fy_month(cls, v: Optional[int]) -> Optional[int]:
        # Same 1..12 bound as Organization, but None is allowed (an unauthored record has no fiscal year).
        if v is not None and not 1 <= v <= 12:
            raise ValueError("fiscal_year_start_month must be 1..12")
        return v


# Resolve forward references (TrustBlock.migrated_from -> MigratedFrom).
TrustBlock.model_rebuild()


__all__ = [
    # enums
    "ColumnType",
    "Confidence",
    "ReviewState",
    "Cardinality",
    "JoinType",
    "Executable",
    "SourceType",
    "StorageType",
    # models
    "TrustBlock",
    "MigratedFrom",
    "StorageConnection",
    "ForeignKey",
    "DenormalizedFrom",
    "Column",
    "IndexHint",
    "PerformanceHints",
    "Table",
    "TableRef",
    "EntityMapping",
    "Entity",
    "Metric",
    "Relationship",
    "CrossSubjectAreaRelationship",
    "SubjectArea",
    "Organization",
    "DisplayConventions",
    "OrgRecord",
    # constants
    "DEEP_TABLE_COLUMN_THRESHOLD",
]
