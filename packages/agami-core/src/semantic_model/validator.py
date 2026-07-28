"""Cross-cutting validator for the agami semantic-model-v2 hierarchy.

`models.py` handles per-object structural validation (required fields, enums,
relationship completeness, one-primary-per-entity, …). This module handles the
invariants that need the *whole* model in view — things a single object can't
check on its own.

Every rule from the design doc's "Validator" subsection — plus the cardinality and
BigQuery-gap additions — is implemented here:

  - Relationship cardinality required (handled in models; re-asserted here for the
    structural gate, verification check #17).
  - Relationship completeness: exactly one of (from/to_column) OR (on:)  [models].
  - FK type-compatibility on simple-FK joins  → caps confidence at `proposed`,
    emits a CAST suggestion (Gap 3).
  - Trust-block parity: signed_off_* required when review_state == approved.
  - Cross-area entity name-collision  → WARNING (not error).
  - Subject-area sizing: warn at >=25 tables, error at >30.
  - column_groups orphan check on deep tables (every column in >=1 group).
  - TableRef.expose_column_groups must reference declared column_groups.
  - Every TableRef.table resolves to a tables_defined entry.
  - default_filters reference only existing columns.
  - value_transform / on: parse with sqlglot.
  - caveats non-empty strings  [models].
  - executable on cross-area edges matches endpoint storage connections.
  - choice_field values match column type (key parseability).
  - metric calculation non-empty (backend-neutrality, check #4)  [models].

The validator NEVER mutates the input model. The FK type-mismatch rule reports
the *recommended* confidence cap + fix as a finding; callers (migration tool,
review dashboard) apply it. This keeps the validator a pure function.

Output: a `ValidationResult` with `errors` (deploy-blocking) and `warnings`
(advisory). `ok` is True iff there are no errors. Mirrors the legacy validator's
list[str] contract while adding structured findings for the review dashboard.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import sqlglot
    from sqlglot import exp as _sqlexp  # noqa: F401  (used indirectly)
    from sqlglot.errors import ErrorLevel as _ErrorLevel
    _HAVE_SQLGLOT = True
except ImportError:  # pragma: no cover - sqlglot is in requirements
    _HAVE_SQLGLOT = False

from .models import (
    CrossDatasourceRelationship,
    CrossSubjectAreaRelationship,
    Datasource,
    Entity,
    Relationship,
    SubjectArea,
    Table,
    bare_name,
)

# Sizing thresholds (design doc: warn at 25, error at 30).
SIZING_WARN = 25
SIZING_ERROR = 30

# Type coercion table — pairs considered compatible for a simple-FK join even
# when not byte-equal. Symmetric; checked both directions.
_COERCIBLE_PAIRS: set[frozenset[str]] = {
    frozenset({"integer", "decimal"}),
    frozenset({"integer", "float"}),
    frozenset({"decimal", "float"}),
    frozenset({"date", "timestamp"}),
    frozenset({"string", "uuid"}),
}


@dataclass
class Finding:
    """A single validator finding."""

    severity: str  # "error" | "warning"
    code: str
    message: str
    # Optional structured payload for the review dashboard (e.g. CAST suggestion).
    suggestion: Optional[str] = None
    locator: Optional[str] = None  # human path to the offending element


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def error(self, code: str, msg: str, **kw) -> None:
        self.findings.append(Finding("error", code, msg, **kw))

    def warn(self, code: str, msg: str, **kw) -> None:
        self.findings.append(Finding("warning", code, msg, **kw))


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


# An incremental-validation cache (ACE-046): per-area findings keyed by everything a per-area rule
# reads — the area's own content AND the org-wide table registry. An enrichment run passes one of
# these across edits so only the edited area's rules re-run; a normal `validate(org)` (cache=None)
# is unchanged. Because the key pins every input, a cache hit is byte-identical to recomputing.
# Key = (area name, area content sha, org-table-registry sha) → that area's findings.
ValidationCache = dict[tuple[str, str, str], "list[Finding]"]

# Hard ceiling so a long-lived process (e.g. the HTTP server) that validates many distinct models
# through one shared cache can't grow it without bound. Content-addressed, so evicting an entry only
# costs a cold recompute; we evict foreign-model areas first and never the current model's.
_CACHE_MAX = 1024


def _sig(obj) -> str:
    """Content hash of a model object (pydantic v2). Stable across loads of identical YAML."""
    return hashlib.sha256(obj.model_dump_json().encode("utf-8")).hexdigest()


def _org_tables_sig(org_tables: dict[str, Table]) -> str:
    """Signature of the org-wide table registry — the ONLY cross-area input the per-area rules read
    (`_check_table_refs_resolve` = table-name existence; `_check_expose_column_groups` = a referenced
    table's column_groups). Folding it into every area's key means any table add/remove/rename
    invalidates every cached area (a full revalidate), while a metric/entity/relationship edit —
    which never touches tables — leaves it stable, so only the edited area re-runs."""
    h = hashlib.sha256()
    for name in sorted(org_tables):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(org_tables[name].model_dump_json().encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _bound_cache(cache: ValidationCache, org: Datasource) -> None:
    """Cap total cache size for long-lived processes. Evict oldest entries for areas NOT in the
    current model first (insertion order); never evict the current model's areas, so a single large
    model doesn't thrash. Only triggers well past any real model's area count."""
    if len(cache) <= _CACHE_MAX:
        return
    current = {sa.name for sa in org.subject_areas}
    for k in [k for k in cache if k[0] not in current]:
        if len(cache) <= _CACHE_MAX:
            break
        del cache[k]


def _validate_area(
    sa: SubjectArea, org: Datasource, org_tables: dict[str, Table]
) -> list[Finding]:
    """Run one area's per-area rule battery into its own result and return just its findings, so a
    run cache can reuse them verbatim when the area (and the table registry) are unchanged."""
    ares = ValidationResult()
    # Build the area's {name: table} index once and share it across every check that needs it
    # (entity mappings + each relationship's FK-type check), instead of rebuilding it per
    # relationship — that per-rel rebuild was the O(R×T) cost this pass eliminates (ACE-046).
    defined = _all_tables(sa)
    _check_subject_area_sizing(sa, ares)
    _check_table_refs_resolve(sa, ares, org_tables)
    _check_expose_column_groups(sa, ares, org_tables)
    _check_deep_table_column_groups(sa, ares)
    _check_default_filters_columns(sa, ares)
    _check_value_transforms(sa, ares)
    _check_choice_fields(sa, ares)
    _check_entity_mappings(sa, ares, defined)
    for rel in sa.relationships:
        _check_relationship(rel, sa, org, ares, cross=False, defined=defined)
    return ares.findings


def validate(org: Datasource, *, cache: "ValidationCache | None" = None) -> ValidationResult:
    """Run every cross-cutting rule. Returns a ValidationResult (never raises on
    a model-level problem — those surface as the model failing to parse upstream).

    When `cache` is supplied (an enrichment run reusing one dict across edits), each area's
    per-area findings are memoized by (area name, area content, table-registry signature): only a
    changed area re-runs its rules. The cross-area + org-level rules always run fresh — they are
    cheap and can depend on any area. The result is identical to a full validate, because the key
    pins every input a per-area rule reads. Default `cache=None` validates everything, so all other
    callers are unchanged."""
    res = ValidationResult()

    _check_storage_connection_refs(org, res)
    # Canonical Table definitions resolve org-wide: a TableRef may point at a table
    # defined in another area (multi-membership without duplication — the design
    # doc's "defined once, TableRef'd from each subject area" pattern).
    org_tables = _org_tables(org)
    reg_sig = _org_tables_sig(org_tables) if cache is not None else ""
    for sa in org.subject_areas:
        if cache is not None:
            key = (sa.name, _sig(sa), reg_sig)
            findings = cache.get(key)
            if findings is None:
                findings = _validate_area(sa, org, org_tables)
                # Drop this area's entries at prior content/registry signatures before storing the
                # current one — keeps steady-state to ~one entry per area within a model.
                for stale in [k for k in cache if k[0] == sa.name and k != key]:
                    del cache[stale]
                cache[key] = findings
                _bound_cache(cache, org)
        else:
            findings = _validate_area(sa, org, org_tables)
        res.findings.extend(findings)

    for rel in org.cross_subject_area_relationships:
        _check_cross_relationship(rel, org, res)

    _check_cross_area_entity_collisions(org, res)
    _check_metric_backend_neutrality(org, res)
    _check_derived_metrics(org, res)
    _check_metric_binding_columns(org, res)

    return res


# ---------------------------------------------------------------------------
# Deployment-level pass (F16 / ACE-072): cross-datasource bridges
# ---------------------------------------------------------------------------


def validate_deployment(artifacts_dir: str | Path) -> ValidationResult:
    """Validate the deployment-level cross-datasource bridges on the ``OrgRecord``.

    A bridge links a key in one datasource to the same entity in another, so it can only be checked
    with EVERY datasource's model in view — more than the per-profile ``validate(org)`` sees. This
    loads the record (with its merged bridges) plus each attached datasource model and, for each
    bridge, rejects ``same_engine`` (structurally impossible across two engines) and any endpoint
    whose datasource / dataset (``schema.table``) / column doesn't resolve in that datasource's model.

    Returns an empty (``ok``) result when there is no record or no bridges — a pre-F16 deployment is a
    no-op, never an error."""
    from . import loader as _loader
    from . import org_record as _org_record

    res = ValidationResult()
    record = _org_record.load_org_record(artifacts_dir)
    if record is None or not record.cross_datasource_relationships:
        return res

    art = Path(artifacts_dir)
    models: dict[str, Datasource] = {}
    for name in record.datasources:
        try:
            models[name] = _loader.load_datasource(art / name)
        except Exception:
            # A profile listed on the record but unreadable on disk is the per-profile validate's
            # problem, not this pass's — skip it so an endpoint pointing at it just reports unresolved.
            continue

    for bridge in record.cross_datasource_relationships:
        _check_bridge(bridge, models, res)
    return res


def _bridge_label(bridge: CrossDatasourceRelationship) -> str:
    """Readable identity for a bridge in a finding message (bridges are usually anonymous —
    identified by their endpoints, not a name)."""
    return (f"{bridge.from_datasource}.{bridge.from_dataset} -> "
            f"{bridge.to_datasource}.{bridge.to_dataset}")


def _check_bridge(
    bridge: CrossDatasourceRelationship,
    models: dict[str, Datasource],
    res: ValidationResult,
) -> None:
    # same_engine is impossible across two datasources (two engines). The model validator already
    # refuses to construct one; re-assert here for a bridge that reached the deployment some other way.
    if bridge.executable == "same_engine":
        res.error(
            "cross_datasource_executable_mismatch",
            f"cross-datasource bridge {_bridge_label(bridge)}: executable='same_engine' is invalid "
            "across two datasources; use 'split' or 'informational'",
        )
        return
    _check_bridge_endpoint(
        res, bridge, models, bridge.from_datasource, bridge.from_dataset, bridge.from_columns)
    _check_bridge_endpoint(
        res, bridge, models, bridge.to_datasource, bridge.to_dataset, bridge.to_columns)


def _check_bridge_endpoint(
    res: ValidationResult,
    bridge: CrossDatasourceRelationship,
    models: dict[str, Datasource],
    datasource: str,
    dataset: str,
    columns: list[str],
) -> None:
    """Resolve one endpoint (datasource -> dataset -> column(s)) against the loaded models, emitting
    a ``cross_datasource_endpoint_unresolved`` error at the first level that doesn't resolve."""
    label = _bridge_label(bridge)
    model = models.get(datasource)
    if model is None:
        res.error(
            "cross_datasource_endpoint_unresolved",
            f"cross-datasource bridge {label}: datasource {datasource!r} is not an attached "
            "datasource of this deployment",
        )
        return
    table = _resolve_dataset(model, dataset)
    if table is None:
        res.error(
            "cross_datasource_endpoint_unresolved",
            f"cross-datasource bridge {label}: dataset {dataset!r} not found in datasource "
            f"{datasource!r}",
        )
        return
    present = {c.name for c in table.columns}
    missing = [c for c in columns if c not in present]
    if missing:
        res.error(
            "cross_datasource_endpoint_unresolved",
            f"cross-datasource bridge {label}: column(s) {missing} not found in dataset "
            f"{dataset!r} of datasource {datasource!r}",
        )


def _resolve_dataset(model: Datasource, dataset: str) -> Optional[Table]:
    """Find the ``Table`` a ``schema.table`` dataset names in a datasource's model. The schema half
    is matched leniently — a schema-less model (SQLite) or a bare ``table`` dataset resolves on the
    table name alone — so the bridge author isn't forced to schema-qualify when it's unambiguous."""
    target_schema, target_table = dataset.rsplit(".", 1) if "." in dataset else (None, dataset)
    for sa in model.subject_areas:
        for t in sa.tables_defined:
            if t.name != target_table:
                continue
            if target_schema is None or t.schema_name is None or t.schema_name == target_schema:
                return t
    return None


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def _all_tables(sa: SubjectArea) -> dict[str, Table]:
    return {t.name: t for t in sa.tables_defined}


def _check_storage_connection_refs(org: Datasource, res: ValidationResult) -> None:
    known = {sc.name for sc in org.storage_connections}
    for sa in org.subject_areas:
        for ref in sa.tables:
            if ref.storage_connection not in known:
                res.error(
                    "unknown_storage_connection",
                    f"subject area {sa.name!r}: TableRef {ref.table!r} references unknown "
                    f"storage_connection {ref.storage_connection!r}",
                    locator=f"{sa.name}.tables[{ref.table}]",
                )


def _check_subject_area_sizing(sa: SubjectArea, res: ValidationResult) -> None:
    n = len(sa.tables)
    if n > SIZING_ERROR:
        res.error(
            "subject_area_too_large",
            f"subject area {sa.name!r} has {n} tables (> {SIZING_ERROR}); split it into "
            "smaller areas (target ceiling ~20-30).",
            locator=sa.name,
        )
    elif n >= SIZING_WARN:
        res.warn(
            "subject_area_large",
            f"subject area {sa.name!r} has {n} tables (>= {SIZING_WARN}); approaching the "
            f"{SIZING_ERROR}-table ceiling. Consider splitting.",
            locator=sa.name,
        )


def _org_tables(org: Datasource) -> dict[str, Table]:
    """Every canonical Table across the org, keyed by name (org-wide resolution)."""
    out: dict[str, Table] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            out[t.name] = t
    return out


def _check_table_refs_resolve(
    sa: SubjectArea, res: ValidationResult, org_tables: dict[str, Table]
) -> None:
    for ref in sa.tables:
        if ref.table not in org_tables:
            res.error(
                "orphan_table_ref",
                f"subject area {sa.name!r}: TableRef {ref.table!r} has no matching canonical "
                "Table in any subject area's tables_defined[]",
                locator=f"{sa.name}.tables[{ref.table}]",
            )


def _check_expose_column_groups(
    sa: SubjectArea, res: ValidationResult, org_tables: dict[str, Table]
) -> None:
    for ref in sa.tables:
        if not ref.expose_column_groups:
            continue
        table = org_tables.get(ref.table)
        if table is None:
            continue  # already reported by orphan check
        for grp in ref.expose_column_groups:
            if grp not in table.column_groups:
                res.error(
                    "unknown_column_group",
                    f"subject area {sa.name!r}: TableRef {ref.table!r} exposes column_group "
                    f"{grp!r} which is not declared in the table's column_groups "
                    f"({sorted(table.column_groups)})",
                    locator=f"{sa.name}.tables[{ref.table}].expose_column_groups",
                )


def _check_deep_table_column_groups(sa: SubjectArea, res: ValidationResult) -> None:
    for table in sa.tables_defined:
        cols = table.column_names()
        # column_groups columns must exist on the table
        grouped: set[str] = set()
        for gname, gcols in table.column_groups.items():
            for c in gcols:
                if c not in cols:
                    res.error(
                        "column_group_missing_column",
                        f"table {table.name!r}: column_group {gname!r} lists column {c!r} "
                        "which does not exist on the table",
                        locator=f"{table.name}.column_groups.{gname}",
                    )
                grouped.add(c)
        # deep tables must declare column_groups AND have no orphan columns
        if table.is_deep:
            if not table.column_groups:
                res.error(
                    "deep_table_no_column_groups",
                    f"table {table.name!r} is deep ({len(table.columns)} columns) and must "
                    "declare column_groups",
                    locator=table.name,
                )
            else:
                orphans = sorted(cols - grouped)
                if orphans:
                    res.error(
                        "column_group_orphans",
                        f"table {table.name!r} (deep) has columns in no column_group: "
                        f"{orphans}",
                        locator=table.name,
                    )


def _check_default_filters_columns(sa: SubjectArea, res: ValidationResult) -> None:
    import re as _re

    for table in sa.tables_defined:
        cols = table.column_names()
        alias = bare_name(table.name)
        for flt in table.default_filters:
            # `{alias}` is the runtime table-alias placeholder, and `:param` are bind
            # markers the executor fills in — neither is a column. Resolve / strip them
            # before extracting column references (collect_default_filters does the same).
            resolved = flt.replace("{alias}", alias)
            resolved = _re.sub(r":\w+", "1", resolved)  # bind marker -> literal for parsing
            referenced = _columns_referenced(resolved, table.name)
            referenced -= {alias}  # the (now-substituted) table qualifier is not a column
            for col in referenced:
                if col not in cols:
                    res.error(
                        "default_filter_unknown_column",
                        f"table {table.name!r}: default_filter references column {col!r} "
                        f"not present on the table: {flt!r}",
                        locator=f"{table.name}.default_filters",
                    )


def _check_value_transforms(sa: SubjectArea, res: ValidationResult) -> None:
    for table in sa.tables_defined:
        for col in table.columns:
            if col.value_transform:
                err = _sqlparse_error(col.value_transform)
                if err:
                    res.error(
                        "value_transform_unparseable",
                        f"table {table.name!r} column {col.name!r}: value_transform does not "
                        f"parse as SQL ({err}): {col.value_transform!r}",
                        locator=f"{table.name}.{col.name}.value_transform",
                    )


def _check_choice_fields(sa: SubjectArea, res: ValidationResult) -> None:
    for table in sa.tables_defined:
        for col in table.columns:
            if not col.choice_field:
                continue
            for key in col.choice_field:
                if not _value_matches_type(key, col.type):
                    res.error(
                        "choice_field_type_mismatch",
                        f"table {table.name!r} column {col.name!r} ({col.type}): choice_field "
                        f"key {key!r} is not a valid {col.type} value",
                        locator=f"{table.name}.{col.name}.choice_field",
                    )


def _check_entity_mappings(
    sa: SubjectArea, res: ValidationResult, defined: dict[str, Table]
) -> None:
    for ent in sa.entities:
        for mp in ent.maps_to:
            table = defined.get(mp.table)
            if table is None:
                # entity may map to a table owned by another area / cross-cutting;
                # only warn when it's clearly within this area's namespace.
                continue
            if mp.column not in table.column_names():
                res.error(
                    "entity_mapping_unknown_column",
                    f"subject area {sa.name!r} entity {ent.name!r}: maps_to references "
                    f"{mp.table}.{mp.column} but that column does not exist",
                    locator=f"{sa.name}.entities[{ent.name}]",
                )


def _check_relationship(
    rel: Relationship,
    sa: SubjectArea,
    org: Datasource,
    res: ValidationResult,
    *,
    cross: bool,
    defined: dict[str, Table],
) -> None:
    # cardinality required (structural gate, check #17) — models enforces non-null,
    # re-assert the value domain here for a clear validator-level error too.
    if rel.relationship not in ("many_to_one", "one_to_many", "one_to_one"):
        res.error(
            "relationship_cardinality_invalid",
            f"relationship {rel.from_table}->{rel.to_table}: invalid cardinality "
            f"{rel.relationship!r}",
        )

    # trust-block parity: signed_off_* required when approved
    if rel.review_state == "approved":
        missing = [
            n
            for n in ("signed_off_by", "signed_off_at", "signed_off_role")
            if not getattr(rel, n)
        ]
        if missing:
            res.error(
                "trust_block_incomplete",
                f"relationship {rel.from_table}->{rel.to_table} is approved but missing "
                f"{missing}",
            )

    # on: must parse if present
    if rel.on:
        err = _sqlparse_error(rel.on, as_condition=True)
        if err:
            res.error(
                "relationship_on_unparseable",
                f"relationship {rel.from_table}->{rel.to_table}: on: does not parse "
                f"({err}): {rel.on!r}",
            )
        return  # user took explicit ownership; skip simple-form type check

    # FK type-compatibility on the simple form (Gap 3)
    _check_fk_type_compat(rel, res, defined)


def _check_cross_relationship(
    rel: CrossSubjectAreaRelationship, org: Datasource, res: ValidationResult
) -> None:
    sa_from = org.subject_area(rel.from_subject_area)
    sa_to = org.subject_area(rel.to_subject_area)
    if sa_from is None:
        res.error(
            "cross_rel_unknown_area",
            f"cross-area relationship references unknown from_subject_area "
            f"{rel.from_subject_area!r}",
        )
    if sa_to is None:
        res.error(
            "cross_rel_unknown_area",
            f"cross-area relationship references unknown to_subject_area "
            f"{rel.to_subject_area!r}",
        )

    # trust-block parity + on: parse + cardinality (reuse intra checks). The FK-type check resolves
    # against the FROM area's tables, exactly as before — the cross edge's to_table lives in another
    # area, so it stays unresolved here (unchanged behaviour).
    sa_for_rel = sa_from or SubjectArea(name="<unknown>")
    _check_relationship(rel, sa_for_rel, org, res, cross=True, defined=_all_tables(sa_for_rel))

    # executable must match the endpoints' storage connections
    conn_from = _table_connection(sa_from, rel.from_table) if sa_from else None
    conn_to = _table_connection(sa_to, rel.to_table) if sa_to else None
    if conn_from and conn_to:
        same = conn_from == conn_to
        if rel.executable == "same_engine" and not same:
            res.error(
                "executable_mismatch",
                f"cross-area relationship {rel.from_table}->{rel.to_table}: executable="
                f"'same_engine' but endpoints are on different storage connections "
                f"({conn_from} vs {conn_to}); use 'split' or 'informational'",
            )
        if rel.executable == "split" and same:
            res.warn(
                "executable_overcautious",
                f"cross-area relationship {rel.from_table}->{rel.to_table}: executable="
                f"'split' but both endpoints are on {conn_from}; 'same_engine' is achievable",
            )


def _check_cross_area_entity_collisions(org: Datasource, res: ValidationResult) -> None:
    """Two areas independently declaring the same entity name (or overlapping
    other_names) with maps_to into different connections/schemas → warning + a
    suggestion to unify in a cross-cutting area."""
    # index: lowercased name/alias -> list[(area, entity)]
    index: dict[str, list[tuple[str, Entity]]] = {}
    for sa in org.subject_areas:
        for ent in sa.entities:
            keys = {ent.name.lower()} | {n.lower() for n in ent.other_names}
            for k in keys:
                index.setdefault(k, []).append((sa.name, ent))

    # names already unified at the org level don't collide
    unified = {e.name.lower() for e in org.cross_subject_area_entities}
    for e in org.cross_subject_area_entities:
        unified |= {n.lower() for n in e.other_names}

    seen_pairs: set[tuple[str, str]] = set()
    for key, occurrences in index.items():
        if key in unified:
            continue
        if len(occurrences) < 2:
            continue
        # pairwise: collision only if the maps_to targets differ in connection/schema/table
        for i in range(len(occurrences)):
            for j in range(i + 1, len(occurrences)):
                (area_a, ent_a), (area_b, ent_b) = occurrences[i], occurrences[j]
                if area_a == area_b:
                    continue
                pair_key = tuple(sorted([area_a + "::" + ent_a.name, area_b + "::" + ent_b.name]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                tgt_a = _entity_targets(org, area_a, ent_a)
                tgt_b = _entity_targets(org, area_b, ent_b)
                if tgt_a and tgt_b and tgt_a != tgt_b:
                    res.warn(
                        "cross_area_entity_collision",
                        f"entity {ent_a.name!r} is declared independently in areas "
                        f"{area_a!r} (-> {sorted(tgt_a)}) and {area_b!r} (-> {sorted(tgt_b)}). "
                        "Consider declaring it ONCE in a cross-cutting subject area with "
                        "maps_to across both, so the runtime never has to ask which was meant.",
                        suggestion=(
                            f"cross_subject_area_entities:\n  - name: {ent_a.name}\n"
                            f"    maps_to: [<{area_a} mapping>, <{area_b} mapping>]"
                        ),
                    )


def _check_metric_backend_neutrality(org: Datasource, res: ValidationResult) -> None:
    """Verification check #4: every metric's `calculation` (prose intent) must be
    non-empty — no metric may depend ONLY on a SQL binding. (models enforces
    non-empty; here we also flag binding-only metrics with placeholder prose.)"""
    all_metrics = list(org.cross_subject_area_metrics)
    for sa in org.subject_areas:
        all_metrics.extend(sa.metrics)
    for met in all_metrics:
        if met.bindings and not met.calculation.strip():
            res.error(
                "metric_binding_only",
                f"metric {met.name!r} has SQL bindings but no prose calculation "
                "(backend-neutrality violation)",
            )


def _check_derived_metrics(org: Datasource, res: ValidationResult) -> None:
    """Scorecard #1: a derived metric (one composing others via {base} placeholders)
    must resolve — no cycles, no unknown bases, no illegal second-order nesting. A
    base over a disjoint grain is a warning (inline composition may be wrong; full
    grain attribution is #4)."""
    from . import derived as D

    idx = D.metric_index(org)
    all_metrics = list(org.cross_subject_area_metrics)
    for sa in org.subject_areas:
        all_metrics.extend(sa.metrics)
    for met in all_metrics:
        if not (D.is_derived(met) or D.is_second_order(met)):
            continue
        # resolve_metric_sql dispatches: second-order → CTE synthesis, else placeholder
        # expansion. Either failure mode (cycle, unknown base, bad shape, multi-table inner
        # synth, missing inner_grain) is deploy-blocking.
        for stype in (met.bindings or {}):
            try:
                D.resolve_metric_sql(met, stype, idx)
            except D.DerivedError as e:
                res.error("derived_metric", str(e))
        # grain sanity (first-order case-a only): a base sharing no source_table may be a
        # cross-grain ratio inline composition can't get right (the synthesizer handles the
        # second-order case explicitly, so skip the warning there).
        if D.is_second_order(met):
            continue
        for stype in (met.bindings or {}):
            for ref in D.binding_refs(met.bindings.get(stype)):
                base = idx.get(ref)
                if (base and met.source_tables and base.source_tables
                        and not (set(met.source_tables) & set(base.source_tables))):
                    res.warn(
                        "derived_metric_grain",
                        f"metric {met.name!r} composes {ref!r} but they share no source "
                        "table — verify they're at the same grain (cross-grain composition "
                        "needs the grain-attributed planner, #4)",
                    )


def _binding_column_refs(sql: str) -> set[str]:
    """Best-effort set of bare column names referenced in a binding expression (lower-cased).
    sqlglot-based; returns empty on parse failure or no sqlglot (never block on unparseable)."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:
        return set()
    try:
        tree = sqlglot.parse_one(sql, error_level="ignore")
    except Exception:
        return set()
    if tree is None:
        return set()
    return {c.name.lower() for c in tree.find_all(exp.Column) if c.name}


def _check_metric_binding_columns(org: Datasource, res: ValidationResult) -> None:
    """WARN when a metric's binding references a column that exists on NONE of its source_tables —
    catches a typo'd or renamed column (e.g. `SUM(cst)`) before it fails at query time, which is
    the one thing the hand-edited SQL snippet otherwise has no safety net for.

    Deliberately a warning, not a gate: it's a best-effort static check (sqlglot-parsed, skips
    derived metrics and unparseable bindings) and a denormalized / not-yet-listed source table
    could produce a false positive — so it surfaces the suspicion without blocking the write."""
    from . import derived as D
    cols_by_table: dict[str, set[str]] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            cols_by_table.setdefault(t.name.lower(), set()).update(c.name.lower() for c in t.columns)
    all_metrics = list(getattr(org, "cross_subject_area_metrics", []) or [])
    for sa in org.subject_areas:
        all_metrics.extend(sa.metrics)
    for met in all_metrics:
        if D.is_derived(met) or not met.source_tables:
            continue  # derived: columns live in the base; no source_tables: a different check
        allowed: set[str] = set()
        for tn in met.source_tables:
            allowed |= cols_by_table.get(tn.lower(), set())
        if not allowed:
            continue  # source tables aren't in the model (its own check) — don't double-flag
        for stype, sql in (met.bindings or {}).items():
            missing = sorted(r for r in _binding_column_refs(sql) if r not in allowed)
            if missing:
                res.warn(
                    "metric_binding_unknown_column",
                    f"metric {met.name!r} ({stype}) binding references column(s) {missing} not "
                    f"found on its source table(s) {met.source_tables} — check for a typo or a "
                    "missing source_table",
                )


# ---------------------------------------------------------------------------
# FK type compatibility (Gap 3)
# ---------------------------------------------------------------------------


def _check_fk_type_compat(
    rel: Relationship, res: ValidationResult, defined: dict[str, Table]
) -> None:
    from_t = defined.get(rel.from_table)
    to_t = defined.get(rel.to_table)
    if not (from_t and to_t and rel.from_column and rel.to_column):
        return
    from_c = from_t.get_column(rel.from_column)
    to_c = to_t.get_column(rel.to_column)
    if not (from_c and to_c):
        return
    if not _types_compatible(from_c.type, to_c.type):
        cast = (
            f'on: "CAST({rel.from_table}.{rel.from_column} AS {to_c.type}) '
            f'= {rel.to_table}.{rel.to_column}"'
        )
        sev_msg = (
            f"relationship {rel.from_table}.{rel.from_column} ({from_c.type}) = "
            f"{rel.to_table}.{rel.to_column} ({to_c.type}): incompatible join column types. "
            f"confidence is capped at 'proposed'; add a CAST in on: or reject."
        )
        # This is a finding that ALSO instructs the caller to cap confidence.
        if rel.confidence == "confirmed":
            res.error(
                "fk_type_mismatch_confirmed",
                sev_msg + " (currently marked 'confirmed' — not allowed on a type mismatch)",
                suggestion=cast,
            )
        else:
            res.warn(
                "fk_type_mismatch",
                sev_msg,
                suggestion=cast,
            )


def recommended_confidence_cap(rel: Relationship, sa: SubjectArea) -> Optional[str]:
    """Return 'proposed' if this relationship has a simple-FK type mismatch that
    should cap its confidence; else None. Used by the migration tool / dashboard
    to apply the cap (the validator itself never mutates)."""
    if rel.on:
        return None
    defined = _all_tables(sa)
    from_t = defined.get(rel.from_table)
    to_t = defined.get(rel.to_table)
    if not (from_t and to_t and rel.from_column and rel.to_column):
        return None
    from_c = from_t.get_column(rel.from_column)
    to_c = to_t.get_column(rel.to_column)
    if from_c and to_c and not _types_compatible(from_c.type, to_c.type):
        return "proposed"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _types_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    return frozenset({a, b}) in _COERCIBLE_PAIRS


def _entity_targets(org: Datasource, area_name: str, ent: Entity) -> set[str]:
    """Resolve an entity's maps_to into a set of 'connection.schema.table' strings."""
    sa = org.subject_area(area_name)
    out: set[str] = set()
    if sa is None:
        return out
    defined = _all_tables(sa)
    for mp in ent.maps_to:
        t = defined.get(mp.table)
        conn = t.storage_connection if t else "?"
        schema = (t.schema_name if t else None) or "?"
        out.add(f"{conn}.{schema}.{mp.table}")
    return out


def _table_connection(sa: Optional[SubjectArea], table_name: str) -> Optional[str]:
    if sa is None:
        return None
    # table_name in cross-area edges may be "schema.table"; match on suffix too.
    bare = bare_name(table_name)
    for ref in sa.tables:
        if ref.table == table_name or ref.table == bare:
            return ref.storage_connection
    return None


def _columns_referenced(expr: str, table_name: str) -> set[str]:
    """Best-effort extraction of column names referenced in a default_filter.

    Uses sqlglot when available; falls back to a conservative regex. Recognizes
    bare columns and `table.col` / `{alias}.col` forms. Bind params (`:tenant_id`)
    and the table's own name as a qualifier are ignored.
    """
    if _HAVE_SQLGLOT:
        try:
            tree = sqlglot.parse_one(expr, error_level="ignore")
            if tree is not None:
                cols: set[str] = set()
                for c in tree.find_all(sqlglot.exp.Column):
                    cols.add(c.name)
                if cols:
                    return cols
        except Exception:
            pass
    # regex fallback
    import re

    cols = set()
    # table.col or alias.col
    for m in re.finditer(r"(?:\{?\w+\}?)\.(\w+)", expr):
        cols.add(m.group(1))
    # bare identifiers that look like columns (exclude SQL keywords + funcs)
    return cols


def _sqlparse_error(expr: str, *, as_condition: bool = False) -> Optional[str]:
    """Return None if `expr` parses as SQL, else a short error string."""
    if not _HAVE_SQLGLOT:
        return None  # can't check; treat as ok (sqlglot optional, like legacy)
    candidate = f"SELECT 1 WHERE {expr}" if as_condition else f"SELECT {expr}"
    try:
        parsed = sqlglot.parse_one(candidate, error_level=_ErrorLevel.RAISE)
        return None if parsed is not None else "empty parse"
    except Exception as e:  # sqlglot.errors.ParseError and friends
        return str(e).splitlines()[0][:160]


def _value_matches_type(value: str, ctype: str) -> bool:
    """Is the string `value` a plausible literal of column type `ctype`?
    Used for choice_field keys (which are always YAML strings)."""
    v = value.strip()
    if ctype == "string" or ctype in ("json", "array", "uuid", "bytes", "time"):
        return True
    if ctype == "boolean":
        return v.lower() in ("true", "false", "0", "1", "t", "f", "yes", "no")
    if ctype == "integer":
        try:
            int(v)
            return True
        except ValueError:
            return False
    if ctype in ("decimal", "float"):
        try:
            float(v)
            return True
        except ValueError:
            return False
    if ctype in ("date", "timestamp"):
        # accept ISO-ish; choice_field on a date is unusual but allow YYYY...
        import re

        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", v))
    return True


def format_result(res: ValidationResult) -> str:
    """Human-readable summary (CLI output)."""
    lines = []
    for f in res.findings:
        tag = "ERROR" if f.severity == "error" else "warn "
        lines.append(f"[{tag}] {f.code}: {f.message}")
        if f.suggestion:
            lines.append(f"        suggestion: {f.suggestion}")
    if not lines:
        lines.append("OK — no findings.")
    summary = f"{len(res.errors)} error(s), {len(res.warnings)} warning(s)"
    lines.append(summary)
    return "\n".join(lines)


__all__ = [
    "Finding",
    "ValidationResult",
    "validate",
    "validate_deployment",
    "recommended_confidence_cap",
    "format_result",
    "SIZING_WARN",
    "SIZING_ERROR",
]
