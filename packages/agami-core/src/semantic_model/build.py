"""Shared model-assembly + on-disk writer for building the semantic model.

These are the dialect-agnostic, source-agnostic pieces used by the introspection
engine (`introspect.py`): infer grain/cardinality, derive column_groups on deep
tables, flag sensitive columns, propose a subject-area split, extract cross-area
edges, and write the canonical on-disk tree. Kept here (not in introspect.py) so
the assembly logic is independently testable and reusable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import (
    DEEP_TABLE_COLUMN_THRESHOLD,
    Column,
    CrossSubjectAreaRelationship,
    Organization,
    Relationship,
    SubjectArea,
    Table,
    TableRef,
    bare_name,
)

# Strongly-PII column-name patterns — sensitive regardless of table.
STRONG_PII_RE = re.compile(
    r"(dob|birth|phone|mobile|email|address|aadhaar|aadhar|\bpan\b|\bssn\b|passport|"
    r"pincode|\bzip\b|account_?no|card_?no|salary|income)",
    re.IGNORECASE,
)
# Weakly-PII patterns — only sensitive inside a PII-ish table (a "name" column on
# a product table is not PII; on a person table it is).
WEAK_PII_RE = re.compile(r"(\bname\b|first_?name|last_?name|full_?name|surname)", re.IGNORECASE)
# Broader "might be PII" tokens for the SUSPECTED tier — a review aid only, never auto-flags.
# General privacy concepts (not a customer's schema): identity, contact, location, demographics.
_EXTRA_PII_RE = re.compile(
    r"(gender|\bsex\b|ip_?address|\bip\b|latitude|longitude|\bgeo\b|national_?id|tax_?id|"
    r"licen[cs]e|user_?name|nationality|ethnicit|religion|marital|emergency_?contact|"
    r"next_?of_kin|date_?of_?birth|maiden)", re.IGNORECASE)


def suspected_pii(column_name: str) -> bool:
    """A BROADER 'might be PII' heuristic than `detect_sensitive` — surfaces columns the strict
    flag may have MISSED (e.g. `first_name` in a non-PII-named table like `sys_user`, which the
    table-gated weak rule skips), so a reviewer can confirm. A review aid; never auto-marks
    sensitive. General privacy vocabulary, not vendor-specific."""
    return bool(STRONG_PII_RE.search(column_name) or WEAK_PII_RE.search(column_name)
                or _EXTRA_PII_RE.search(column_name))
# Back-compat alias (some callers import SENSITIVE_RE).
SENSITIVE_RE = STRONG_PII_RE

# sizing thresholds mirror validator.SIZING_WARN
SINGLE_AREA_MAX = 24


# Numeric columns whose NAME indicates a monetary value, so curation can offer a currency
# `unit` without callers hand-rolling (and mis-rolling) a regex. Tokens match on word
# boundaries (`_` or string edges), so a bare `count` never matches inside `discount`:
# `discount_amount` / `member_discount` ARE money; `order_count` / `discount_rate` are not.
_MONEY_RE = re.compile(
    r"(^|_)(amount|amt|price|cost|fee|revenue|sales|salary|wage|income|payment|"
    r"charge|balance|deposit|withdrawal|refund|discount|subtotal|total|spend|spent|"
    r"budget|invoice|mrr|arr|gmv|ltv|aov|paid|due|owed|credit|debit)s?(_|$)",
    re.IGNORECASE,
)
# Tokens that flip a money-ish name back to NON-money (it's a rate / count / id / score / …).
_MONEY_NEGATIVE_RE = re.compile(
    r"(^|_)(rate|pct|percent|percentage|ratio|count|cnt|qty|quantity|num|number|id|"
    r"flag|year|age|day|month|score|rank|code|status)s?(_|$)",
    re.IGNORECASE,
)


def detect_money_column(column_name: str) -> bool:
    """A column whose name looks monetary (amount/price/revenue/discount/…) and is NOT a
    rate/count/id/score — so `discount_amount` is money while `discount_rate` and
    `order_count` are not. Name-only; the caller restricts this to numeric columns."""
    return bool(_MONEY_RE.search(column_name)) and not _MONEY_NEGATIVE_RE.search(column_name)


# --- aggregation class (the Phase-1 column-intrinsic measure semantics) ------

# Only these column types can be MEASURES at all; everything else (string, date,
# timestamp, boolean, json, …) is a grouping dimension.
_NUMERIC_TYPES = frozenset({"integer", "decimal", "float"})

# Identifier / code / calendar / flag tokens → the column is a DIMENSION, never a measure.
# Deliberately conservative (omit ambiguous num/number/day/week/month) — a miss falls to a
# safe class downstream, a false dimension would wrongly block a real measure.
_DIMENSION_RE = re.compile(
    r"(^|_)(id|guid|uuid|key|code|cd|no|zip|zipcode|pincode|pin|year|fiscal|quarter|"
    r"status|state|type|category|flag|version|is|has)s?(_|$)",
    re.IGNORECASE,
)
# Rate / ratio / per-unit / index tokens → AVERAGEABLE (AVG/MIN/MAX ok, SUM is meaningless).
# Checked BEFORE additive so `avg_balance`, `discount_rate`, `cost_per_unit`, `unit_price`
# resolve to averageable rather than being summed.
_AVERAGEABLE_RE = re.compile(
    r"(^|_)(rate|ratio|pct|percent|percentage|avg|average|mean|median|per|price|"
    r"score|rating|temperature|temp|index|margin|share|utilization|util|occupancy)s?(_|$)",
    re.IGNORECASE,
)
# Quantity / money / volume tokens → ADDITIVE (SUM is meaningful). Includes semi-additive
# STOCKS (balance, inventory, headcount) — they ARE summable; the time exception is declared
# on the metric as `non_additive_dimensions` (Phase 2 / #3), not here.
_ADDITIVE_RE = re.compile(
    r"(^|_)(amount|amt|total|subtotal|sum|qty|quantity|count|cnt|revenue|sales|cost|"
    r"spend|spent|value|volume|units|gmv|charge|fee|tax|discount|paid|due|gross|net|"
    r"profit|income|expense|balance|deposit|withdrawal|refund|credit|debit|payment|"
    r"inventory|stock|headcount|distance|weight|duration|bytes|size)s?(_|$)",
    re.IGNORECASE,
)


def classify_aggregation(column_name: str, column_type: str, is_key: bool = False) -> str:
    """Heuristic column-intrinsic aggregation class — one of additive / averageable /
    dimension / unknown (see models.Aggregation). Name + type only; advisory, refined by
    the curator, and `unknown` is never enforced against. Order matters: keys and
    non-numeric types are dimensions; rate-ish names are averageable; money/quantity names
    are additive; anything else stays `unknown` (safe — no enforcement)."""
    if is_key:
        return "dimension"
    if column_type not in _NUMERIC_TYPES:
        return "dimension"
    if _DIMENSION_RE.search(column_name):
        return "dimension"
    if _AVERAGEABLE_RE.search(column_name):
        return "averageable"
    if detect_money_column(column_name) or _ADDITIVE_RE.search(column_name):
        return "additive"
    return "unknown"


# Universal coded-FORMAT columns whose meaning is an objective standard, not a guess — recognized
# from NAME signal + VALUE shape together (both required, so it's confident, never inferred from a
# name alone). Deterministic; fills a description the LLM would otherwise leave `ai_unknown`, on
# ANY database (no platform assumptions). General world facts, not vendor-specific.
_CCY_NAME_RE = re.compile(r"(^|_)(currency|ccy|curr)(_|$)", re.IGNORECASE)
_COUNTRY_NAME_RE = re.compile(r"(^|_)(country|nation)(_code)?(_|$)", re.IGNORECASE)
_LOCALE_NAME_RE = re.compile(r"(^|_)(locale|lang|language)(_|$)", re.IGNORECASE)
_TZ_NAME_RE = re.compile(r"(^|_)(timezone|tz|time_?zone)(_|$)", re.IGNORECASE)
_3UP_RE = re.compile(r"^[A-Z]{3}$")
_2UP_RE = re.compile(r"^[A-Z]{2}$")
_IANA_TZ_RE = re.compile(r"^[A-Za-z]+/[A-Za-z_]+")
_LOCALE_VAL_RE = re.compile(r"^[a-z]{2}([_-][A-Za-z]{2,4})?$")


def canonical_description(column_name: str, values: list) -> Optional[str]:
    """A deterministic description for a UNIVERSAL coded format (currency / country / timezone /
    locale), recognized from the column NAME plus the SHAPE of its sampled values — both required,
    so it never guesses from a name alone. Returns None when there's no confident match. General
    world standards, applicable to any database."""
    vals = [str(v).strip() for v in values if str(v).strip() != ""][:50]
    if not vals:
        return None

    def most(rx) -> bool:  # ≥80% of sampled values fit the shape (tolerate a few odd rows)
        return sum(1 for v in vals if rx.match(v)) >= max(1, int(0.8 * len(vals)))

    if _CCY_NAME_RE.search(column_name) and most(_3UP_RE):
        return "ISO 4217 currency code (3-letter, e.g. USD, EUR, INR)."
    if _COUNTRY_NAME_RE.search(column_name) and most(_2UP_RE):
        return "ISO 3166-1 alpha-2 country code (2-letter, e.g. US, IN, GB)."
    if _TZ_NAME_RE.search(column_name) and most(_IANA_TZ_RE):
        return "IANA time-zone identifier (e.g. America/New_York, Asia/Kolkata)."
    if _LOCALE_NAME_RE.search(column_name) and most(_LOCALE_VAL_RE):
        return "Language / locale code (e.g. en, en_US, pt-BR)."
    return None


def detect_sensitive(table_name: str, column_name: str) -> bool:
    """Strongly-PII column names (email/phone/dob/ssn/address/…) are sensitive
    regardless of table. Weakly-PII names (name/first_name/…) are sensitive only
    inside a PII-ish table, to avoid flagging e.g. a product `name`."""
    if STRONG_PII_RE.search(column_name):
        return True
    table_pii = table_name.upper() == "PII" or bool(STRONG_PII_RE.search(table_name)) \
        or bool(WEAK_PII_RE.search(table_name))
    return bool(WEAK_PII_RE.search(column_name)) and table_pii


# Role groups — assigned from STRUCTURAL column signals (PK, foreign_key, choice_field,
# aggregation class, type) plus a couple of universal name patterns. General, not vendor-
# specific. A wide table's columns are mostly references / coded values / flags / measures /
# timestamps, so naming those roles collapses the long tail of single-prefix columns that the
# old prefix-only grouping dumped into `misc`. Order = priority; each column lands in exactly
# ONE group (the explorer relies on one-group-per-column). Roles that match nothing fall
# through to prefix-token grouping, then to `misc`.
_FLAG_COL_RE = re.compile(r"^(is_|has_).+|.+_flag$", re.IGNORECASE)
# Audit / lifecycle bookkeeping: who/when a row was touched. Suffix-anchored on `_at/_on/_by`
# (and the created/updated/modified/deleted prefixes) so it catches `created_at`/`opened_by`/
# `closed_on` WITHOUT grabbing business dates like `due_date`/`order_date` (those fall to the
# `dates` role instead).
_AUDIT_COL_RE = re.compile(r"_(at|on|by)$|^(created|updated|modified|deleted)(_|$)", re.IGNORECASE)

# One-line gloss per RECOGNIZED role group. Prefix-token groups (and any unknown key) get a
# generated line from `column_group_descriptions`. No vendor specifics — these describe a role.
_ROLE_GROUP_DESCRIPTIONS: dict[str, str] = {
    "identity": "Primary keys and unique identifiers for this table.",
    "references": "Foreign-key columns that link to other tables.",
    "codes": "Coded / enumerated values drawn from a fixed set of choices.",
    "flags": "Boolean indicator columns (true/false conditions).",
    "audit": "Bookkeeping of when a row was created or changed, and by whom.",
    "measures": "Numeric columns that can be aggregated (summed or averaged).",
    "dates": "Date / time columns other than audit timestamps.",
    "misc": "Columns with no shared prefix or recognized role.",
}


def _role_of(c: Column, reference_columns: set[str]) -> Optional[str]:
    """The role group for a column from its structural signals, or None to defer to prefix
    grouping. First match wins (so a coded FK files under `references`, a boolean flag under
    `flags`, etc.) — keeps every column in exactly one group. `reference_columns` (column names
    that are the FROM side of a join) lets the caller mark references even though the join lives
    on a Relationship, not on `column.foreign_key` — which the introspection pipeline never sets."""
    if c.primary_key or c.name.upper() == "ID":
        return "identity"
    if c.foreign_key is not None or c.name in reference_columns:
        return "references"
    if c.choice_field:
        return "codes"
    if c.type == "boolean" or _FLAG_COL_RE.match(c.name):
        return "flags"
    if _AUDIT_COL_RE.search(c.name):
        return "audit"
    if c.aggregation in ("additive", "averageable"):
        return "measures"
    if c.type in ("timestamp", "date"):
        return "dates"
    return None


def derive_column_groups(
    columns: list[Column], *, reference_columns: Optional[set[str]] = None
) -> dict[str, list[str]]:
    """Group deep-table columns ROLE-FIRST (references / codes / flags / audit / measures /
    dates / identity from structural signals), then by name prefix for whatever's left, then
    fold remaining single-prefix columns into `misc`. Every column lands in exactly one group
    (no orphans — the validator enforces this on deep tables). General, not vendor-specific.

    `reference_columns` (optional) names the columns that are the FROM side of a join — passed
    in once relationships are known so FK columns group under `references` instead of scattering
    through `misc` (joins live on Relationship objects, not on `column.foreign_key`)."""
    ref_cols = reference_columns or set()
    role_groups: dict[str, list[str]] = defaultdict(list)
    prefix_groups: dict[str, list[str]] = defaultdict(list)
    for c in columns:
        role = _role_of(c, ref_cols)
        if role is not None:
            role_groups[role].append(c.name)
        else:
            prefix_groups[c.name.split("_")[0].lower()].append(c.name)
    final: dict[str, list[str]] = dict(role_groups)
    misc: list[str] = []
    for g, cols in prefix_groups.items():
        if len(cols) == 1:
            misc.extend(cols)
        else:
            final[g] = cols
    if misc:
        final.setdefault("misc", []).extend(misc)
    return final


def column_group_descriptions(groups: dict[str, list[str]]) -> dict[str, str]:
    """One-line gloss per group name: recognized role groups get a fixed description, prefix-
    token groups get a generated 'Columns named <token>…' line. General; the result is stored
    on the table so the explorer/MCP can explain a group instead of showing a bare key."""
    out: dict[str, str] = {}
    for g in groups:
        out[g] = _ROLE_GROUP_DESCRIPTIONS.get(g) or f"Columns named {g}* (grouped by prefix)."
    return out


def maybe_column_groups(
    columns: list[Column], *, reference_columns: Optional[set[str]] = None
) -> dict[str, list[str]]:
    """column_groups only on deep tables; narrow tables get none."""
    if len(columns) >= DEEP_TABLE_COLUMN_THRESHOLD:
        return derive_column_groups(columns, reference_columns=reference_columns)
    return {}


def infer_cardinality(
    from_table: str,
    to_table: str,
    from_cols: list[str],
    to_cols: list[str],
    grain_by_table: dict[str, set[str]],
) -> str:
    """Infer join cardinality from declared/inferred grain.

    to-side == to PK & from-side == from PK -> one_to_one
    to-side == to PK (unique), from not      -> many_to_one
    from-side == from PK, to not             -> one_to_many
    else                                     -> many_to_one (default; review)
    """
    from_pk = grain_by_table.get(from_table, set())
    to_pk = grain_by_table.get(to_table, set())
    from_is_pk = bool(from_pk) and set(from_cols) == from_pk
    to_is_pk = bool(to_pk) and set(to_cols) == to_pk
    if from_is_pk and to_is_pk:
        return "one_to_one"
    if to_is_pk and not from_is_pk:
        return "many_to_one"
    if from_is_pk and not to_is_pk:
        return "one_to_many"
    return "many_to_one"


# ---------------------------------------------------------------------------
# Subject-area proposal (prefix-family clustering; one table per area)
# ---------------------------------------------------------------------------


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def cluster_by_family(names: list[str]) -> dict[str, str]:
    """Map each table name -> an area key, grouping by first-token (singularized)
    then merging prefix-families (one key being a prefix of another)."""
    raw_key: dict[str, str] = {}
    for name in names:
        bare = bare_name(name)
        token = _singularize(re.split(r"[_\s]", bare)[0].lower())
        raw_key[name] = token or "misc"

    canonical: dict[str, str] = {}
    for k in sorted(set(raw_key.values()), key=len):
        target = k
        for root in canonical.values():
            if k.startswith(root) or root.startswith(k):
                target = min(root, k, key=len)
                break
        canonical[k] = target

    def resolve(k: str) -> str:
        seen: set[str] = set()
        while canonical.get(k, k) != k and k not in seen:
            seen.add(k)
            k = canonical[k]
        return k

    return {name: resolve(raw_key[name]) for name in names}


def make_table_ref(conn: str, table: Table) -> TableRef:
    expose = list(table.column_groups.keys()) if table.column_groups else None
    return TableRef(
        storage_connection=conn,
        schema=table.schema_name,
        table=table.name,
        expose_column_groups=expose,
    )


def _area_key(schema: str) -> str:
    """A filesystem-safe area name from a schema name."""
    return re.sub(r"[^a-z0-9_]+", "_", schema.lower()).strip("_") or "misc"


def _rel_in_area(r: Relationship, keys: set, bare: set) -> bool:
    """Is this relationship internal to an area? Match endpoints by (schema, table) when the
    relationship carries schemas — so a join from billing.products and one from crm.products
    don't both match an area just because the bare name `products` is present. Schemaless
    relationships (SQLite / legacy) fall back to bare-name membership."""
    def here(table: str, schema) -> bool:
        return (schema, table) in keys if schema is not None else table in bare
    return (here(bare_name(r.from_table), r.from_schema)
            and here(bare_name(r.to_table), r.to_schema))


def make_area(name: str, tables: list[Table], rels: list[Relationship], conn: str) -> SubjectArea:
    table_names = {t.name for t in tables}
    keys = {(t.schema_name, t.name) for t in tables}
    area_rels = [r for r in rels if _rel_in_area(r, keys, table_names)]
    return SubjectArea(
        name=name,
        description=f"Auto-proposed subject area covering: {', '.join(sorted(table_names))}.",
        tables=[make_table_ref(conn, t) for t in tables],
        tables_defined=tables,
        relationships=area_rels,
    )


def propose_subject_areas(
    tables: list[Table], rels: list[Relationship], conn: str, profile: str
) -> tuple[list[SubjectArea], list[str]]:
    """Return (areas, notes).

    A DB spanning **2+ schemas** is split **one area per schema** — the schemas are the
    natural domains, and (critically) this keeps same-named tables in different schemas
    (e.g. `billing.products` vs `crm.products`) in separate area dirs so neither is lost to
    a bare-name write collision. A single-schema DB keeps the old behavior: one area when
    small, else prefix-family clustering.
    """
    notes: list[str] = []
    schemas = sorted({t.schema_name for t in tables if t.schema_name})
    if len(schemas) >= 2:
        by_area: dict[str, list[Table]] = defaultdict(list)
        for t in tables:
            by_area[_area_key(t.schema_name) if t.schema_name else profile.lower()].append(t)
        areas = [make_area(a, members, rels, conn) for a, members in sorted(by_area.items())]
        notes.append(
            f"{len(tables)} tables across {len(schemas)} schemas -> {len(areas)} subject areas "
            "(one per schema); cross-schema joins become cross_subject_area_relationships."
        )
        return areas, notes
    if len(tables) <= SINGLE_AREA_MAX:
        notes.append(f"{len(tables)} tables -> single subject area {profile.lower()!r}")
        return [make_area(profile.lower(), tables, rels, conn)], notes
    mapping = cluster_by_family([t.name for t in tables])
    by_area = defaultdict(list)
    for t in tables:
        by_area[mapping[t.name]].append(t)
    areas = [make_area(a, members, rels, conn) for a, members in sorted(by_area.items())]
    notes.append(
        f"{len(tables)} tables -> {len(areas)} areas by prefix-family (one owner each); "
        "cross-area joins become cross_subject_area_relationships. PROPOSAL — review."
    )
    return areas, notes


def extract_cross_area_relationships(
    areas: list[SubjectArea], rels: list[Relationship]
) -> list[CrossSubjectAreaRelationship]:
    # Key by (schema, name) so two same-named tables in different schemas resolve to their
    # own area; keep a bare-name fallback for schemaless rels / tables.
    area_of: dict[tuple, str] = {}
    bare_of: dict[str, str] = {}
    for sa in areas:
        for t in sa.tables_defined:
            area_of[(t.schema_name, t.name)] = sa.name
            bare_of.setdefault(t.name, sa.name)
    intra_ids = {id(r) for sa in areas for r in sa.relationships}
    out: list[CrossSubjectAreaRelationship] = []
    for r in rels:
        if id(r) in intra_ids:
            continue
        ft, tt = bare_name(r.from_table), bare_name(r.to_table)
        fa = area_of.get((r.from_schema, ft)) or bare_of.get(ft)
        ta = area_of.get((r.to_schema, tt)) or bare_of.get(tt)
        if fa and ta and fa != ta:
            data = r.model_dump(exclude_none=True, by_alias=True)
            data.update(from_subject_area=fa, to_subject_area=ta, executable="same_engine")
            data.setdefault("for_questions_about", [])
            out.append(CrossSubjectAreaRelationship(**data))
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def _model_dump(model) -> dict:
    return model.model_dump(exclude_none=True, by_alias=True)


@dataclass
class WriteReport:
    out_dir: str
    dry_run: bool
    files_written: list[str] = field(default_factory=list)


def ensure_org_id(out: Path, existing: Optional[str] = None, *, dry_run: bool = False) -> str:
    """Idempotent, deployment-scoped resolution of the org identity (F14 / ACE-056; relocated by
    F15 / ACE-067). Returns, in order: ``existing`` (an id the caller already resolved); the org_id
    already persisted at ``out/org.yaml`` (preserved across re-introspect — the immutability guarantee);
    else the DEPLOYMENT record's id (``organization.yaml`` at ``out.parent``), minting the record on
    first use.

    F15 relocated the id's home from the profiles up into the one root record, which is now the
    deployment-scoped consistency mechanism — so the old "adopt a sibling profile's id" scan is GONE:
    a second profile reads the same id straight from the record instead of scanning siblings. Pure-local
    (a uuid4 minted on-box, no coordinator — the only option under F14's no-egress invariant). On
    ``dry_run`` nothing is persisted: a preview reads an existing record or surfaces a throwaway id."""
    from uuid import uuid4  # local generation only — no egress (F14 invariant)

    from . import loader, org_record

    resolved = existing or loader.load_org_id(out)
    if resolved:
        return resolved
    if dry_run:
        # Preview: never mint/persist a record. Use an existing one if present, else a throwaway id.
        record = org_record.load_org_record(out.parent)
        return record.org_id if record is not None else uuid4().hex
    return org_record.ensure_org_record(out.parent).org_id


def write_tree(
    org: Organization,
    out: Path,
    *,
    examples_by_area: Optional[dict[str, list[dict]]] = None,
    dry_run: bool = False,
) -> WriteReport:
    """Write the canonical on-disk tree (org.yaml + datasources + subject_areas +
    optional prompt_examples). Returns the list of files (would-be on dry_run)."""
    examples_by_area = examples_by_area or {}
    rep = WriteReport(out_dir=str(out), dry_run=dry_run)

    def write(rel: str, content: str) -> None:
        rep.files_written.append(rel)
        if dry_run:
            return
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # F14/F15: the deployment identity is the first key. `ensure_org_id` reads the *previous* org.yaml
    # (about to be overwritten) so a re-introspect/curate/snapshot preserves the minted id rather than
    # re-minting — the immutability guarantee — and otherwise takes it from the deployment record
    # (minting the record on first use). Resolved once, honoring dry_run so a preview persists nothing.
    org_id = ensure_org_id(out, org.org_id, dry_run=dry_run)
    write("org.yaml", _dump({
        "org_id": org_id,
        "organization": org.organization,
        "version": org.version,
        "description": org.description,
        "fiscal_year_start_month": org.fiscal_year_start_month,
        "storage_connections": [
            {"name": sc.name, "ref": f"datasources/{sc.name}/storage.yaml"}
            for sc in org.storage_connections
        ],
        "subject_areas": [f"subject_areas/{sa.name}" for sa in org.subject_areas],
        "cross_subject_area_relationships": [
            _model_dump(r) for r in org.cross_subject_area_relationships
        ],
    }))
    for sc in org.storage_connections:
        write(f"datasources/{sc.name}/storage.yaml", _dump(_model_dump(sc)))
    for sa in org.subject_areas:
        base = f"subject_areas/{sa.name}"
        write(f"{base}/subject_area.yaml", _dump({
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "tables": [_model_dump(tr) for tr in sa.tables],
        }))
        for t in sa.tables_defined:
            write(f"{base}/tables/{t.name}.yaml", _dump(_model_dump(t)))
        for e in sa.entities:
            write(f"{base}/entities/{e.name}.yaml", _dump(_model_dump(e)))
        for mm in sa.metrics:
            write(f"{base}/metrics/{mm.name}.yaml", _dump(_model_dump(mm)))
        if sa.relationships:
            write(f"{base}/relationships.yaml",
                  _dump({"relationships": [_model_dump(r) for r in sa.relationships]}))
        if sa.name in examples_by_area:
            write(f"prompt_examples/{sa.name}/examples.yaml",
                  _dump({"examples": examples_by_area[sa.name]}))
    if not dry_run:
        # Stamp the model_version pin for the freshly-written model (best-effort).
        from . import org_record, snapshot
        snapshot.write_snapshot(out)
        # Keep the deployment record's datasource list in sync with what's actually on disk — this
        # profile was just written, so rebuild the list from the profile dirs (auto-maintained, no drift).
        org_record.refresh_datasources(out.parent)
    return rep


# Structural patterns for DERIVED metrics — general (English start/end + flag vocabulary), not
# vendor-specific. A flag is a flag and two timestamps are two timestamps on any schema.
_START_NAME_RE = re.compile(
    r"(^|_)(opened|created|started|start|begin|requested|received|logged|reported)(_|$)", re.I)
_END_NAME_RE = re.compile(
    r"(^|_)(closed|resolved|ended|end|finished|completed|fulfilled|approved|delivered)(_|$)", re.I)
_FLAG_NAME_RE = re.compile(r"^(is_|has_).+|.+_flag$", re.I)
_TS_TYPES = {"timestamp", "date"}


# A "trivial" measure is a single bare aggregate over ONE column (or COUNT(*)) — there's no
# interpretive choice in it, so it's as trustworthy as the column's aggregation class (which is
# already confirmed structure). These auto-approve with a system sign-off, like an enforced FK
# join, instead of cluttering the review queue. Anything with a CASE (a flag RATE) or a
# multi-column / function expression (a DURATION's start/end pairing) is NOT trivial — that
# involves a heuristic the engine could get wrong, so it stays proposed for a human glance.
_TRIVIAL_BINDING_RE = re.compile(
    r"^\s*(COUNT\(\s*\*\s*\)|(?:SUM|AVG)\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\))\s*$", re.IGNORECASE)


def _is_trivial_measure(binding_sql: str) -> bool:
    """True for COUNT(*) / SUM(col) / AVG(col) — a single bare aggregate, no CASE/arithmetic/
    function. These carry no judgment beyond the column's (already-confirmed) aggregation class."""
    return bool(_TRIVIAL_BINDING_RE.match(binding_sql or ""))


def suggest_metrics(table: Table, dialect, *, max_per_table: int = 10,
                    now: Optional[str] = None) -> list[dict]:
    """Per-table reusable measures inferred STRUCTURALLY — all general, no vendor patterns:
      • count of rows;
      • SUM of `additive` columns, AVG of `averageable` columns (gated on aggregation class —
        never SUM an id, never AVG a status code);
      • a RATE for each boolean / `is_*`/`has_*`/`*_flag` column (the `made_sla` / `reopen`
        pattern — fraction true);
      • an AVG DURATION when a clear start+end timestamp pair exists (the `avg_resolution_time`
        pattern — `AVG(end − start)` via the dialect's day-difference form).
    Returns proposed/unreviewed Metric dicts for curate.write_items; the user signs them off in
    bulk in the explorer. Count is always kept; the rest are capped at max_per_table.

    Columns agami couldn't read (`description_source == "ai_unknown"`) are SKIPPED — we don't
    propose a metric on a column we can't explain (its prose would just restate opaque SQL).
    Re-run after such a column is described and it becomes eligible (write_items is incremental,
    so no duplicates)."""
    t = table.name
    st = dialect.name
    out: list[dict] = [{"name": f"{t}_count", "calculation": f"Number of {t} records",
                        "bindings": {st: "COUNT(*)"}, "source_tables": [t]}]
    ts_cols: list[str] = []
    for c in table.columns:
        if c.primary_key:
            continue
        if c.description_source == "ai_unknown":  # opaque column → don't propose a metric on it
            continue
        is_bool = c.type == "boolean"
        is_int_flag = c.type == "integer" and bool(_FLAG_NAME_RE.match(c.name))
        if is_bool or is_int_flag:
            cond = c.name if is_bool else f"{c.name} <> 0"
            out.append({"name": f"{t}_{c.name}_rate",
                        "calculation": f"Fraction of {t} where {c.name} is true",
                        "bindings": {st: f"AVG(CASE WHEN {cond} THEN 1.0 ELSE 0.0 END)"},
                        "source_tables": [t]})
            continue
        if c.aggregation == "additive":
            m = {"name": f"{t}_total_{c.name}", "calculation": f"Total {c.name} across {t}",
                 "bindings": {st: f"SUM({c.name})"}, "source_tables": [t]}
            if c.unit:  # SUM(col) carries the column's unit — USD column → USD total
                m["unit"] = c.unit
            out.append(m)
        elif c.aggregation == "averageable":
            m = {"name": f"{t}_avg_{c.name}", "calculation": f"Average {c.name} in {t}",
                 "bindings": {st: f"AVG({c.name})"}, "source_tables": [t]}
            if c.unit:  # AVG(col) is in the same unit as the column
                m["unit"] = c.unit
            out.append(m)
        if c.type in _TS_TYPES:
            ts_cols.append(c.name)
    starts = [c for c in ts_cols if _START_NAME_RE.search(c)]
    ends = [c for c in ts_cols if _END_NAME_RE.search(c)]
    if starts and ends and starts[0] != ends[0]:
        s, e = starts[0], ends[0]
        out.append({"name": f"{t}_avg_duration_days",
                    "calculation": f"Average days from {s} to {e} in {t}",
                    "bindings": {st: f"AVG({dialect.duration_days_expr(s, e)})"},
                    "source_tables": [t], "unit": "days"})
    out = out[: max(1, max_per_table)]
    for m in out:
        binding = next(iter(m.get("bindings", {}).values()), "")
        if _is_trivial_measure(binding):
            # mechanically sound (COUNT(*) / SUM(col) / AVG(col)) — auto-approve with a system
            # sign-off so it skips the review queue, exactly like an enforced FK join.
            m["confidence"] = "confirmed"
            m["review_state"] = "approved"
            m["signed_off_by"] = "agami_suggest"
            m["signed_off_role"] = "system"
            if now:
                m["signed_off_at"] = now
        else:
            # a flag RATE or a DURATION pair — heuristic choice the engine could get wrong.
            m["confidence"] = "proposed"
            m["review_state"] = "unreviewed"
        m["primary_table"] = t  # every suggested metric is single-table — anchor it there
    return out


__all__ = [
    "SENSITIVE_RE", "detect_sensitive", "detect_money_column",
    "derive_column_groups", "column_group_descriptions", "maybe_column_groups",
    "infer_cardinality", "cluster_by_family", "make_area", "make_table_ref",
    "propose_subject_areas", "extract_cross_area_relationships",
    "suggest_metrics", "suspected_pii", "canonical_description",
    "write_tree", "WriteReport",
]
