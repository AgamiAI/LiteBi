"""Serve the semantic model from the DB — the read path + the deploy-time writer.

The model-loader seam is just "produce a `Datasource`": the file adapter is
`semantic_model.loader.load_datasource(root)`; this is the DB adapter, which rebuilds the
**identical** `Datasource` from rows so every downstream tool (get_datasource_schema incl.
sizing, the receipt) is untouched. YAML stays the source of truth — `write_datasource` seeds the
rows from a YAML-loaded `Datasource` at deploy time (the `deploy_semantic_model.py` path).

Each object is stored as its key/structural columns + a `doc` (the object's `model_dump`), so the
rebuild is lossless without enumerating every pydantic field. The parent docs exclude their child
collections (those are their own rows); load re-attaches them.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from semantic_model.models import Datasource, OrgRecord
from store import Store

# The per-datasource model tables write_datasource clears before a re-seed (so a redeploy
# reproduces the served model rather than appending duplicates / hitting PK conflicts). Must stay in
# sync with migrations/core/001_serving.sql's serving tables; examples/memory/model_version are
# re-seeded by their own writers, so they're not in this list.
_MODEL_TABLES = (
    "relationship",
    "entity",
    "metric",
    "model_table",
    "subject_area",
    "datasource_model",
)

# The org a row belongs to when nobody says otherwise — what SingleTenantOrgResolver resolves to, and
# the DEFAULT baked into every serving table's `org_id` column, so a single-tenant caller reads and
# writes exactly the rows it always did. A caller that FORGETS to pass an org lands here too: an org
# no real tenant reads, so the bug loses a row rather than leaking one.
DEFAULT_ORG = "local"


def _est_rows(table_doc: dict[str, Any]) -> int | None:
    ph = table_doc.get("performance_hints")
    return ph.get("estimated_row_count") if isinstance(ph, dict) else None


def _relationship_key(position: int, rel_doc: dict[str, Any]) -> str:
    """The `relationship.name` for one join — unique within its subject area BY CONSTRUCTION.

    `relationship` is PRIMARY KEY (org_id, datasource, area, name), and a duplicate name does not
    lose one edge: the INSERT raises part-way through `write_datasource`, nothing commits, and the
    deployment serves NO model at all. So the only acceptable property here is that a collision
    cannot happen, for any model that validates.

    **Hence the ordinal**, rather than a name assembled from whichever fields look discriminating.
    That was the previous approach twice over — first the table pair alone, which collides for a
    self-join written two ways (`manager_id` and `mentor_id` on one employee table), then the pair
    plus columns, which still collides for two `on:`-expression joins (both columns are None there)
    and for same-named tables in two schemas. Each fix enumerated the cases its author had in mind
    and the next shape found the gap. A position in a list cannot repeat.

    The rest is for the human reading the table, and carries no uniqueness burden: schema-qualified
    endpoints, with the columns when the simple form is used. The `on:` expression is deliberately
    NOT appended — it would add unbounded length to a primary key to describe an edge whose
    definition is already in `doc`.

    Nothing READS this. No query selects a relationship by name, and `load_datasource` rebuilds each
    one from `doc` — so the format is not a contract, and a redeploy replaces a datasource's rows
    wholesale (the DELETE in `write_datasource`), leaving nothing keyed the old way to migrate.
    """

    def endpoint(schema: Any, table: Any, column: Any) -> str:
        qualified = f"{schema}.{table}" if schema else f"{table}"
        return f"{qualified}.{column}" if column else qualified

    frm = endpoint(
        rel_doc.get("from_schema"), rel_doc.get("from_table"), rel_doc.get("from_column")
    )
    to = endpoint(rel_doc.get("to_schema"), rel_doc.get("to_table"), rel_doc.get("to_column"))
    return f"{position}:{frm}->{to}"


def write_datasource(
    store: Store, datasource: str, org: Datasource, org_id: str = DEFAULT_ORG
) -> None:
    """(Re)seed the serving rows for `datasource` from a loaded Datasource. Idempotent — clears
    the datasource's existing model rows first, so re-running the deploy reproduces the served model."""
    for tbl in _MODEL_TABLES:
        # Scoped by org as well as datasource. Without the org predicate one tenant's redeploy would
        # clear EVERY tenant's rows for a same-named datasource — and `prod` is the name everyone uses.
        store.execute(
            f"DELETE FROM {tbl} WHERE org_id = ? AND datasource = ?", (org_id, datasource)
        )

    ds_doc = org.model_dump(mode="json", exclude={"subject_areas"})
    store.execute(
        "INSERT INTO datasource_model (org_id, datasource, description, doc) VALUES (?, ?, ?, ?)",
        (org_id, datasource, org.description or None, json.dumps(ds_doc)),
    )

    for sa in org.subject_areas:
        sa_doc = sa.model_dump(
            mode="json", exclude={"tables_defined", "metrics", "entities", "relationships"}
        )
        store.execute(
            "INSERT INTO subject_area (org_id, datasource, name, description, default_time_window, "
            "table_count, doc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                org_id,
                datasource,
                sa.name,
                sa.description or None,
                sa.default_time_window,
                len(sa.tables_defined),
                json.dumps(sa_doc),
            ),
        )
        for t in sa.tables_defined:
            tdoc = t.model_dump(mode="json")
            store.execute(
                "INSERT INTO model_table (org_id, datasource, area, name, est_row_count, doc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org_id, datasource, sa.name, t.name, _est_rows(tdoc), json.dumps(tdoc)),
            )
        for m in sa.metrics:
            store.execute(
                "INSERT INTO metric (org_id, datasource, area, name, doc) VALUES (?, ?, ?, ?, ?)",
                (org_id, datasource, sa.name, m.name, json.dumps(m.model_dump(mode="json"))),
            )
        for e in sa.entities:
            edoc = e.model_dump(mode="json")
            store.execute(
                "INSERT INTO entity (org_id, datasource, area, name, value_pattern, doc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org_id, datasource, sa.name, e.name, edoc.get("value_pattern"), json.dumps(edoc)),
            )
        for position, r in enumerate(sa.relationships):
            rdoc = r.model_dump(mode="json")
            store.execute(
                "INSERT INTO relationship (org_id, datasource, area, name, doc) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_id, datasource, sa.name, _relationship_key(position, rdoc), json.dumps(rdoc)),
            )
    store.commit()


def load_datasource(store: Store, datasource: str, org_id: str = DEFAULT_ORG) -> Datasource | None:
    """Rebuild the Datasource for `datasource` from rows, or None if it isn't seeded."""
    org_rows = store.query(
        "SELECT doc FROM datasource_model WHERE org_id = ? AND datasource = ?", (org_id, datasource)
    )
    if not org_rows:
        return None
    ds_doc: dict[str, Any] = json.loads(org_rows[0]["doc"])

    subject_areas = []
    for sa_row in store.query(
        "SELECT name, doc FROM subject_area WHERE org_id = ? AND datasource = ? ORDER BY name",
        (org_id, datasource),
    ):
        sa_doc: dict[str, Any] = json.loads(sa_row["doc"])
        area = sa_row["name"]
        for field, table in (
            ("tables_defined", "model_table"),
            ("metrics", "metric"),
            ("entities", "entity"),
            ("relationships", "relationship"),
        ):
            sa_doc[field] = [
                json.loads(r["doc"])
                for r in store.query(
                    f"SELECT doc FROM {table} WHERE org_id = ? AND datasource = ? AND area = ? "
                    "ORDER BY name",
                    (org_id, datasource, area),
                )
            ]
        subject_areas.append(sa_doc)

    ds_doc["subject_areas"] = subject_areas
    return Datasource.model_validate(ds_doc)


def list_datasources(store: Store, org_id: str = DEFAULT_ORG) -> list[str]:
    """The datasources `org_id` has a served model for, sorted. The admin model view picks among
    these; one org can serve several."""
    rows = store.query(
        "SELECT datasource FROM datasource_model WHERE org_id = ? ORDER BY datasource", (org_id,)
    )
    return [r["datasource"] for r in rows]


def model_descriptions(store: Store, org_id: str = DEFAULT_ORG) -> dict[str, str]:
    """`{datasource: description}` for the org's served datasources, in ONE grouped query.

    `description` is a promoted column on `datasource_model` — written beside the doc at deploy
    time — so this is a column read, not a JSON decode of the org blob. Same no-N+1 shape as
    `model_table_counts`, and separate from `list_datasources` so that function's `list[str]`
    contract (the admin model view reads it) is unchanged.

    A datasource whose model declares no description simply won't appear in the map; the caller
    leaves the field off rather than sending an empty string, which would read as "described as
    nothing" instead of "not described".
    """
    rows = store.query(
        "SELECT datasource, description FROM datasource_model WHERE org_id = ?", (org_id,)
    )
    return {r["datasource"]: r["description"] for r in rows if (r["description"] or "").strip()}


def model_table_counts(store: Store, org_id: str = DEFAULT_ORG) -> dict[str, int]:
    """`{datasource: table_count}` for the org's served datasources, in ONE grouped query — so the
    datasource listing sizes itself without a per-datasource round trip (no N+1) and without
    rebuilding the whole Datasource. A datasource with no modeled tables simply won't appear in
    the map; the caller defaults it to 0."""
    rows = store.query(
        "SELECT datasource, count(*) AS n FROM model_table WHERE org_id = ? GROUP BY datasource",
        (org_id,),
    )
    return {r["datasource"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# Memory (datasource.md / USER_MEMORY.md) + model_version — served from the DB too, so a DB-only
# deploy reads NO files at runtime (get_datasource_schema's domain context + the receipt's version
# pin come from these tables, not disk).
# ---------------------------------------------------------------------------


# datasource.md is per-datasource; USER_MEMORY.md is cross-datasource (mirroring the file layout:
# <artifacts_dir>/<profile>/datasource.md vs <artifacts_dir>/USER_MEMORY.md), so it is stored once
# under this sentinel datasource rather than duplicated per datasource. It is still keyed by org — one
# row PER ORG, not one per install, or one tenant's user memory would be served to another's.
_GLOBAL_DATASOURCE = ""


def write_memory(
    store: Store,
    datasource: str,
    *,
    datasource_doc: str | None = None,
    user: str | None = None,
    org_id: str = DEFAULT_ORG,
) -> None:
    """Seed the domain-context docs. `datasource_doc` is per-datasource; `user` is cross-datasource but
    still per-org (the empty-datasource sentinel row). Pass either/both; each replaces its row."""
    if datasource_doc is not None:
        store.execute(
            "DELETE FROM memory WHERE org_id = ? AND datasource = ? AND kind = 'datasource'",
            (org_id, datasource),
        )
        store.execute(
            "INSERT INTO memory (org_id, datasource, kind, content) VALUES (?, ?, 'datasource', ?)",
            (org_id, datasource, datasource_doc),
        )
    if user is not None:
        store.execute(
            "DELETE FROM memory WHERE org_id = ? AND datasource = ? AND kind = 'user'",
            (org_id, _GLOBAL_DATASOURCE),
        )
        store.execute(
            "INSERT INTO memory (org_id, datasource, kind, content) VALUES (?, ?, 'user', ?)",
            (org_id, _GLOBAL_DATASOURCE, user),
        )
    store.commit()


def load_memory(store: Store, datasource: str, org_id: str = DEFAULT_ORG) -> dict[str, str]:
    """{'datasource': <per-datasource datasource.md>, 'user': <the org's USER_MEMORY.md>} —
    missing keys absent."""
    out: dict[str, str] = {}
    for kind, ds in (("datasource", datasource), ("user", _GLOBAL_DATASOURCE)):
        rows = store.query(
            "SELECT content FROM memory WHERE org_id = ? AND datasource = ? AND kind = ?",
            (org_id, ds, kind),
        )
        if rows:
            out[kind] = rows[0]["content"]
    return out


def write_model_version(
    store: Store,
    datasource: str,
    version: str,
    created_at: str | None = None,
    org_id: str = DEFAULT_ORG,
) -> None:
    """Record a model version (the snapshot content hash the receipt pins). Idempotent per version."""
    store.execute(
        "DELETE FROM model_version WHERE org_id = ? AND datasource = ? AND version = ?",
        (org_id, datasource, version),
    )
    store.execute(
        "INSERT INTO model_version (org_id, datasource, version, created_at) VALUES (?, ?, ?, ?)",
        (org_id, datasource, version, created_at),
    )
    store.commit()


def write_organization_record(store: Store, record: OrgRecord, org_id: str = DEFAULT_ORG) -> None:
    """Derive the deployment's `OrgRecord` (ACE-067) into the `organization` table — the one company-level
    row (org_id, name, description, doc), keyed on org alone.

    FK-SAFE UPSERT, deliberately NOT the clear-then-insert (`DELETE`+`INSERT`) every other writer here
    uses: in the hosted stack `org_membership.org_id` and `license.org_id` hold foreign keys to this row,
    so a `DELETE` on redeploy would violate them (and FK enforcement is ON — store.py). `ON CONFLICT DO
    UPDATE` rewrites only the content columns and preserves any hosted-owned `org_name`/`created_at`, so
    hosted onboarding (which INSERTs the row first) and core deploy (which upserts content) coexist on one
    row. Portable across SQLite (>=3.24) and Postgres. Idempotent — a redeploy replaces, never duplicates."""
    # Lossless: dump EVERY non-columned field into `doc` so any OrgRecord bucket (cross-datasource
    # relationships/metrics, and whatever is added later) round-trips without editing this writer —
    # the module's own stated contract. org_id/name/description are their own columns; the rest rides
    # the JSON blob. (The prior hand-listed dict silently dropped new buckets on deploy — ACE-072's
    # bridge among them.)
    doc = json.dumps(record.model_dump(mode="json", exclude={"org_id", "name", "description"}))
    store.execute(
        "INSERT INTO organization (org_id, org_name, description, doc) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (org_id) DO UPDATE SET "
        "org_name = COALESCE(organization.org_name, excluded.org_name), "
        "description = excluded.description, "
        "doc = excluded.doc",
        (org_id, record.name, record.description, doc),
    )
    store.commit()


def load_organization_record(store: Store, org_id: str = DEFAULT_ORG) -> OrgRecord | None:
    """Rebuild the `OrgRecord` for `org_id` from the `organization` row, or `None` when no row exists —
    the graceful-degradation contract ACE-069's composition relies on. A hosted-only row (tenant onboarded
    but no model deployed yet) has `doc='{}'` and rebuilds to a bare record carrying just the name."""
    rows = store.query(
        "SELECT org_name, description, doc FROM organization WHERE org_id = ?", (org_id,)
    )
    if not rows:
        return None
    row = rows[0]
    doc = json.loads(row["doc"]) if row["doc"] else {}
    # Lossless rebuild: the columned fields + every field the writer put in `doc` (the same
    # `model_validate` path the on-disk loader uses), so new buckets ride back automatically.
    return OrgRecord.model_validate(
        {"org_id": org_id, "name": row["org_name"], "description": row["description"], **doc}
    )


def newest_model_version(store: Store, datasource: str, org_id: str = DEFAULT_ORG) -> str | None:
    """The newest recorded version for a datasource (what the receipt pins), or None."""
    rows = store.query(
        "SELECT version FROM model_version WHERE org_id = ? AND datasource = ? "
        "ORDER BY created_at DESC, version DESC",
        (org_id, datasource),
    )
    return rows[0]["version"] if rows else None


# ---------------------------------------------------------------------------
# Prompt examples — write at deploy; serve scoped + ranked + capped at query time.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
_EXAMPLES_CHAR_BUDGET = 20_000


def _tokens(s: str | None) -> set[str]:
    return set(_WORD_RE.findall((s or "").lower()))


def example_id(ex: dict[str, Any]) -> str:
    """The example's identity, derived from its own content: `question` + `sql`, 12 hex characters —
    the construction `compute_model_hash` uses (`snapshot.py::_hash_and_manifest`).

    Derived rather than minted because a minted id changes on every deploy, and derived rather than
    authored because authoring gives the id two homes that can disagree. Each part is NUL-*terminated*
    rather than joined, so ("ab", "c") and ("a", "bc") cannot collapse onto one id.

    `area` is excluded deliberately: it is not carried in the example file — the deploy injects it
    from the subject-area directory name — so hashing it would tie the id to a value resolved outside
    the example. Two examples sharing question and sql are the same example.

    Byte-exact, no normalization: a normalizer is a second thing that can disagree between the two
    things it exists to hold in agreement. So a reworded question or a corrected query is a different
    example, which is the intended property; metadata edits leave the id alone.
    """
    h = hashlib.sha256()
    for part in (str(ex.get("question") or ""), str(ex.get("sql") or "")):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:12]


def write_examples(
    store: Store, datasource: str, examples: list[dict[str, Any]], org_id: str = DEFAULT_ORG
) -> None:
    """(Re)seed the prompt-example rows for a datasource. Each example is {area, question, sql, …};
    area None ⇒ the cross-subject-area bucket."""
    store.execute(
        "DELETE FROM prompt_example WHERE org_id = ? AND datasource = ?", (org_id, datasource)
    )
    # Since ids are derived from content, the same example filed under two subject areas now resolves
    # to one id — and `area` is not in the primary key, so the second INSERT would raise and abort the
    # deploy. First wins, which also keeps the surviving row's `area` stable across re-seeds. It also
    # only serves the area it won under, so an example deliberately filed twice is no longer returned
    # for the second one. Two examples carrying the *same authored* id skip here too, where they used
    # to take the deploy down. The rows above were just deleted, so a within-this-call repeat is the
    # only collision reachable.
    seen: set[str] = set()
    for ex in examples:
        # Keep a stable id across re-seeds when the example carries one (so per-example identity
        # survives a redeploy); derive one from its content when absent.
        #
        # Absent means absent, not falsy: an unquoted `id: 0` in YAML parses to an int, and
        # `ex.get("id") or ...` would derive an id for an example that carries one. An empty string
        # is treated as absent, since it names nothing.
        authored = ex.get("id")
        ex_id = str(authored) if authored is not None and str(authored) != "" else example_id(ex)
        if ex_id in seen:
            continue
        seen.add(ex_id)
        store.execute(
            "INSERT INTO prompt_example (org_id, datasource, area, id, question, doc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (org_id, datasource, ex.get("area"), ex_id, ex.get("question", ""), json.dumps(ex)),
        )
    store.commit()


def select_examples(
    store: Store,
    datasource: str,
    query: str | None = None,
    area: str | None = None,
    top_k: int = 10,
    char_budget: int = _EXAMPLES_CHAR_BUDGET,
    org_id: str = DEFAULT_ORG,
) -> list[dict[str, Any]]:
    """Scope to the org's datasource (+ area, plus the cross-area bucket), rank by word-overlap on
    the question, and cap to top-K within a char budget — so a large library never floods the
    context. No embeddings (that tier is deploy-time + off by default)."""
    if area:
        rows = store.query(
            "SELECT id, question, doc FROM prompt_example WHERE org_id = ? AND datasource = ? "
            "AND (area = ? OR area IS NULL)",
            (org_id, datasource, area),
        )
    else:
        rows = store.query(
            "SELECT id, question, doc FROM prompt_example WHERE org_id = ? AND datasource = ?",
            (org_id, datasource),
        )
    q = _tokens(query)
    if q:
        rows = sorted(rows, key=lambda r: len(q & _tokens(r["question"])), reverse=True)
    out: list[dict[str, Any]] = []
    used = 0
    for r in rows[:top_k]:
        # The column wins over any `id` inside `doc` — they agree by construction, but the column is
        # the identity `example_by_id` resolves. Merged before measuring, so the budget accounts for
        # the dict actually returned.
        doc = {**json.loads(r["doc"]), "id": r["id"]}
        size = len(json.dumps(doc))
        if out and used + size > char_budget:
            break
        out.append(doc)
        used += size
    return out


def example_by_id(
    store: Store, *, org_id: str = DEFAULT_ORG, datasource: str, example_id: str
) -> dict[str, Any] | None:
    """The one example carrying `example_id`, in the shape `select_examples` returns it, or None when
    no such example is seeded for this org and datasource.

    The derivation is one-way, so a caller holding an id has no way back to the example it names
    without this. Scoped like every other read here: an id identifies an example *within* an org's
    datasource, even though the same curated example imported elsewhere derives the same characters.
    """
    rows = store.query(
        "SELECT id, doc FROM prompt_example WHERE org_id = ? AND datasource = ? AND id = ?",
        (org_id, datasource, example_id),
    )
    if not rows:
        return None
    return {**json.loads(rows[0]["doc"]), "id": rows[0]["id"]}


# ---------------------------------------------------------------------------
# Runtime write path — the DB-backed ActivitySink (conforms to ports.ActivitySink by shape).
# ---------------------------------------------------------------------------


def _record_org(record: Any) -> str:
    """The org a log row belongs to, read off the record. Duck-typed rather than a method parameter so
    the `ports.ActivitySink` Protocol shape is unchanged — a record without an org lands on the
    default (an org no other tenant reads), so a missing org loses the log row, never leaks it."""
    return getattr(record, "org_id", None) or DEFAULT_ORG


class DbActivitySink:
    """Write `query_executions` + `tool_calls` to the DB (one class, any backend the Store opens —
    not a Postgres/SQLite pair). Conforms structurally to the `ports.ActivitySink` Protocol; the
    server's single execute_sql chokepoint logs one row per query through it."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record_query_execution(self, record: Any) -> None:
        # The row's key is the caller's `record.id`, NOT a uuid minted here. It is the `audit_id` the
        # guardrail Envelope already handed back with the answer, so the caller can look up the row
        # recording its own query. Minting one here (as this did) discarded it inside the INSERT,
        # which made the id unreferenceable the moment it existed.
        self._store.execute(
            "INSERT INTO query_executions (id, ts, org_id, datasource, question, sql, row_count, "
            "source, status, reason, rule, sql_truncated, error_detail, detail, receipt, "
            "model_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.ts,
                _record_org(record),
                record.profile,
                record.question,
                record.sql,
                record.row_count,
                record.source,
                record.status,
                record.reason,
                record.rule,
                # A portable 0/1 rather than a boolean literal — the same reason `record_tool_call`
                # below writes `success` that way (SQLite has no boolean type).
                1 if getattr(record, "sql_truncated", False) else 0,
                # The raw driver error, operator-only. NULL on the forked surface, where the
                # chokepoint holding it and this recorder are different processes (ACE-039).
                getattr(record, "error_detail", None),
                # The three that make the row re-derivable (ACE-098). `getattr` with a default like
                # the two above, so a caller still on the older record shape writes NULLs rather
                # than raising — the same tolerance `error_detail` and `sql_truncated` already have.
                getattr(record, "detail", None),
                getattr(record, "receipt", None),
                getattr(record, "model_version", None),
            ),
        )
        self._store.commit()

    def record_tool_call(self, record: Any) -> None:
        # `success` is a portable 0/1 (no boolean literal across SQLite/Postgres).
        self._store.execute(
            "INSERT INTO tool_calls (id, ts, org_id, actor, tool_name, datasource, sql, row_count, "
            "execution_ms, success, error_kind, source, user_question, agent_query, thread_id, "
            "correlation_id, refusal_detail, refusal_remediation, audit_id, basis, "
            "conversation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record.ts,
                _record_org(record),
                record.actor,
                record.tool_name,
                record.datasource,
                record.sql,
                record.row_count,
                record.execution_ms,
                1 if record.success else 0,
                record.error_kind,
                record.source,
                record.user_question,
                record.agent_query,
                record.thread_id,
                record.correlation_id,
                # The gate's own two sentences (018). `getattr` with a default, like the execution
                # row's later columns above: an embedder still on the older record shape writes NULLs
                # rather than raising, and NULL is the ordinary value here anyway — only a refusal
                # has them.
                getattr(record, "refusal_detail", None),
                getattr(record, "refusal_remediation", None),
                # This call's execution (019), `getattr`-guarded like the two above so an embedder on
                # an older record shape writes NULL rather than raising. NULL is ordinary here too:
                # those are NULL on every non-refusal, this on every call that ran no statement.
                getattr(record, "audit_id", None),
                # The agent's account of what it based the query on (020), already bounded and
                # serialized by the writer. `getattr`-guarded like the three above, and NULL is
                # ordinary here too: the argument is optional and most calls omit it.
                getattr(record, "basis", None),
                # The conversation the server decided this call belongs to (021). `getattr`-guarded
                # like the four above so an embedder on an older record shape writes NULL rather
                # than raising — and NULL is a real value here: the local single-user path has no
                # store to read a previous call from, so it derives nothing.
                getattr(record, "conversation_id", None),
            ),
        )
        self._store.commit()


# ---------------------------------------------------------------------------
# Read path — the admin activity views (read-only).
# ---------------------------------------------------------------------------

_TOOL_CALL_COLS = (
    "id, ts, actor, tool_name, datasource, sql, row_count, execution_ms, success, error_kind, "
    "user_question, agent_query, thread_id, correlation_id, source, "
    # The conversation the SERVER decided this call belongs to (021), which is what `list_sessions`
    # groups by now. `thread_id` stays selected beside it: it is still recorded, a consumer may still
    # read it, and on a row written before 021 it is the only grouping there is.
    "refusal_detail, refusal_remediation, basis, conversation_id"
)


def _group_turns(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a conversation's calls into **turns** by the self-reported `correlation_id` (one user
    question -> the N calls the agent made answering it). A call with no `correlation_id` is its own
    singleton turn (so the view degrades to the per-call list). The turn's `question` is the **earliest**
    call's `user_question` — Claude drifts `user_question` onto later refinements, so the first is the
    reliable one. Turns keep newest-first order (calls arrive ts-DESC); each turn's calls read
    chronologically."""
    turns_map: dict[Any, list[dict[str, Any]]] = {}
    turn_order: list[Any] = []
    for c in calls:
        ck = c["correlation_id"] or c["id"]  # no correlation_id -> singleton keyed on the row id
        if ck not in turns_map:
            turns_map[ck] = []
            turn_order.append(ck)
        turns_map[ck].append(c)
    turns: list[dict[str, Any]] = []
    for ck in turn_order:
        tc = sorted(turns_map[ck], key=lambda x: x["ts"])  # chronological within the turn
        # The turn's question = the EARLIEST call that actually reported one. Not literally tc[0]: the
        # setup calls that now fold into a turn (list_datasources, get_datasource_schema) carry no
        # user_question, so they'd mask the real question that the execute_sql did report. Earliest-with-
        # one stays drift-proof (a later call's drifted question can't win over the first real one).
        question = next((c["user_question"] for c in tc if c.get("user_question")), None)
        turns.append(
            {
                "question": question,
                "started": tc[0]["ts"],
                "calls": tc,
                "question_self_reported": _question_is_self_reported(tc),
            }
        )
    return turns


def _question_is_self_reported(calls: list[dict[str, Any]]) -> bool:
    """Whether a turn's `question` is the model's own claim rather than something the caller observed.

    On the transport path the question is copied out of the tool arguments the model wrote, so it is a
    self-report; a caller that dispatches handlers itself can state it authoritatively instead, and says
    so by recording a different `source`. **Fails toward self-reported:** an unset source (rows written
    before the column existed) and a turn whose calls disagree both count as self-reported, because the
    marker signals *lower* trust and the honest thing under uncertainty is to keep showing it.

    "Disagree" means the calls do not all carry the SAME source — not merely that one of them is the
    default. A turn mixing two different non-default sources is just as ambiguous about who captured
    the question, and dropping the marker there would overstate the trust rather than understate it.
    An empty turn has no evidence at all, so it keeps the marker too."""
    from tools import DEFAULT_CALL_SOURCE  # lazy: keeps the stdlib-lean base install importable

    sources = {(c.get("source") or DEFAULT_CALL_SOURCE) for c in calls}
    # Only one case drops the marker: every call agreeing on a single, non-default source.
    return len(sources) != 1 or DEFAULT_CALL_SOURCE in sources


def list_sessions(
    store: Store, *, limit: int = 500, org_id: str = DEFAULT_ORG
) -> list[dict[str, Any]]:
    """Group the org's tool calls into conversations for the best-effort Activity view: same `thread_id`
    = one conversation; a call with no `thread_id` (Claude didn't self-report) becomes its own singleton
    (grouped on its id), so the view degrades gracefully to ungrouped — and stays **audit-complete**:
    every call appears, query or not (the non-query tools — list_datasources, get_datasource_schema, …
    — fold into the conversation alongside the execute_sql calls). Grouping + aggregation are done in
    Python (portable, no dialect-specific GROUP BY)."""
    rows = store.query(
        f"SELECT {_TOOL_CALL_COLS} FROM tool_calls WHERE org_id = ? ORDER BY ts DESC LIMIT ?",
        (org_id, limit),
    )
    sessions: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for r in rows:
        # **`conversation_id` first, because it is the only one of the three that is a FACT.** It is
        # decided by the server from the authenticated actor and the clock (021), so it cannot be
        # influenced, cannot collide between two people, and does not depend on the model having
        # remembered anything. `thread_id` is the model's answer to the same question and was
        # measured colliding across two days of real traffic — two conversations arriving as `t1`
        # and blending into one row here.
        #
        # It stays as the fallback rather than being dropped: every row written before 021 has no
        # conversation, and the old grouping is the only one those have. The actor is still paired
        # with it there for the reason the original comment gives — two people colliding on one
        # self-reported id must not blend — and a call with neither stays its own singleton, which is
        # what keeps this view audit-complete.
        conversation = r["conversation_id"] if "conversation_id" in r.keys() else None
        if conversation:
            key = ("conversation", conversation)
        elif r["thread_id"]:
            key = (r["actor"], r["thread_id"])
        else:
            key = r["id"]
        s = sessions.get(key)
        if s is None:
            s = {
                "key": key,
                "thread_id": r["thread_id"],
                "actor": r["actor"],
                "datasource": r["datasource"],
                "calls": [],
            }
            sessions[key] = s
            order.append(key)
        s["calls"].append(r)
    out: list[dict[str, Any]] = []
    for key in order:
        s = sessions[key]
        cs = s["calls"]
        ts_all = [c["ts"] for c in cs]
        ms = [c["execution_ms"] for c in cs if c["execution_ms"] is not None]
        s["started"] = min(ts_all)
        s["last_activity"] = max(ts_all)
        # A conversation — or even one turn — can touch SEVERAL datasources: the user switches mid-session,
        # or asks something spanning two (the agent runs one execute_sql per datasource). Surface the full
        # distinct set in first-seen (chronological) order, not just one; `cs` is ts-DESC, so walk it
        # reversed. Empty when only datasource-less calls (e.g. list_datasources) ran.
        seen_ds: list[str] = []
        for c in reversed(cs):
            if c["datasource"] and c["datasource"] not in seen_ds:
                seen_ds.append(c["datasource"])
        s["datasources"] = seen_ds
        s["call_count"] = len(cs)
        s["error_count"] = sum(1 for c in cs if not c["success"])
        s["avg_ms"] = round(sum(ms) / len(ms)) if ms else None  # over calls that recorded latency
        s["turns"] = _group_turns(cs)  # the within-conversation turn level
        out.append(s)
    return out
