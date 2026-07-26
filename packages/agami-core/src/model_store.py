"""Serve the semantic model from the DB — the read path + the deploy-time writer.

The model-loader seam is just "produce an `Organization`": the file adapter is
`semantic_model.loader.load_organization(root)`; this is the DB adapter, which rebuilds the
**identical** `Organization` from rows so every downstream tool (get_datasource_schema incl.
sizing, the receipt) is untouched. YAML stays the source of truth — `write_organization` seeds the
rows from a YAML-loaded `Organization` at deploy time (the `deploy_semantic_model.py` path).

Each object is stored as its key/structural columns + a `doc` (the object's `model_dump`), so the
rebuild is lossless without enumerating every pydantic field. The parent docs exclude their child
collections (those are their own rows); load re-attaches them.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from semantic_model.models import Organization, OrgRecord
from store import Store

# The per-datasource model tables write_organization clears before a re-seed (so a redeploy
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


def write_organization(
    store: Store, datasource: str, org: Organization, org_id: str = DEFAULT_ORG
) -> None:
    """(Re)seed the serving rows for `datasource` from a loaded Organization. Idempotent — clears
    the datasource's existing model rows first, so re-running the deploy reproduces the served model."""
    for tbl in _MODEL_TABLES:
        # Scoped by org as well as datasource. Without the org predicate one tenant's redeploy would
        # clear EVERY tenant's rows for a same-named datasource — and `prod` is the name everyone uses.
        store.execute(
            f"DELETE FROM {tbl} WHERE org_id = ? AND datasource = ?", (org_id, datasource)
        )

    org_doc = org.model_dump(mode="json", exclude={"subject_areas"})
    store.execute(
        "INSERT INTO datasource_model (org_id, datasource, description, doc) VALUES (?, ?, ?, ?)",
        (org_id, datasource, org.description or None, json.dumps(org_doc)),
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
        for r in sa.relationships:
            rdoc = r.model_dump(mode="json")
            name = f"{rdoc.get('from_table')}->{rdoc.get('to_table')}"
            store.execute(
                "INSERT INTO relationship (org_id, datasource, area, name, doc) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_id, datasource, sa.name, name, json.dumps(rdoc)),
            )
    store.commit()


def load_organization(
    store: Store, datasource: str, org_id: str = DEFAULT_ORG
) -> Organization | None:
    """Rebuild the Organization for `datasource` from rows, or None if it isn't seeded."""
    org_rows = store.query(
        "SELECT doc FROM datasource_model WHERE org_id = ? AND datasource = ?", (org_id, datasource)
    )
    if not org_rows:
        return None
    org_doc: dict[str, Any] = json.loads(org_rows[0]["doc"])

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

    org_doc["subject_areas"] = subject_areas
    return Organization.model_validate(org_doc)


def list_datasources(store: Store, org_id: str = DEFAULT_ORG) -> list[str]:
    """The datasources `org_id` has a served model for, sorted. The admin model view picks among
    these; one org can serve several."""
    rows = store.query(
        "SELECT datasource FROM datasource_model WHERE org_id = ? ORDER BY datasource", (org_id,)
    )
    return [r["datasource"] for r in rows]


def model_table_counts(store: Store, org_id: str = DEFAULT_ORG) -> dict[str, int]:
    """`{datasource: table_count}` for the org's served datasources, in ONE grouped query — so the
    datasource listing sizes itself without a per-datasource round trip (no N+1) and without
    rebuilding the whole Organization. A datasource with no modeled tables simply won't appear in
    the map; the caller defaults it to 0."""
    rows = store.query(
        "SELECT datasource, count(*) AS n FROM model_table WHERE org_id = ? GROUP BY datasource",
        (org_id,),
    )
    return {r["datasource"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# Memory (ORGANIZATION.md / USER_MEMORY.md) + model_version — served from the DB too, so a DB-only
# deploy reads NO files at runtime (get_datasource_schema's domain context + the receipt's version
# pin come from these tables, not disk).
# ---------------------------------------------------------------------------


# ORGANIZATION.md is per-datasource; USER_MEMORY.md is cross-datasource (mirroring the file layout:
# <artifacts_dir>/<profile>/ORGANIZATION.md vs <artifacts_dir>/USER_MEMORY.md), so it is stored once
# under this sentinel datasource rather than duplicated per datasource. It is still keyed by org — one
# row PER ORG, not one per install, or one tenant's user memory would be served to another's.
_GLOBAL_DATASOURCE = ""


def write_memory(
    store: Store,
    datasource: str,
    *,
    organization: str | None = None,
    user: str | None = None,
    org_id: str = DEFAULT_ORG,
) -> None:
    """Seed the domain-context docs. `organization` is per-datasource; `user` is cross-datasource but
    still per-org (the empty-datasource sentinel row). Pass either/both; each replaces its row."""
    if organization is not None:
        store.execute(
            "DELETE FROM memory WHERE org_id = ? AND datasource = ? AND kind = 'organization'",
            (org_id, datasource),
        )
        store.execute(
            "INSERT INTO memory (org_id, datasource, kind, content) "
            "VALUES (?, ?, 'organization', ?)",
            (org_id, datasource, organization),
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
    """{'organization': <per-datasource ORGANIZATION.md>, 'user': <the org's USER_MEMORY.md>} —
    missing keys absent."""
    out: dict[str, str] = {}
    for kind, ds in (("organization", datasource), ("user", _GLOBAL_DATASOURCE)):
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
    doc = json.dumps(
        {
            "fiscal_year_start_month": record.fiscal_year_start_month,
            "display_conventions": record.display_conventions.model_dump(mode="json"),
            "glossary": record.glossary,
            "datasources": record.datasources,
        }
    )
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
    return OrgRecord(
        org_id=org_id,
        name=row["org_name"],
        description=row["description"],
        fiscal_year_start_month=doc.get("fiscal_year_start_month"),
        display_conventions=doc.get("display_conventions") or {},
        glossary=doc.get("glossary") or {},
        datasources=doc.get("datasources") or [],
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


def write_examples(
    store: Store, datasource: str, examples: list[dict[str, Any]], org_id: str = DEFAULT_ORG
) -> None:
    """(Re)seed the prompt-example rows for a datasource. Each example is {area, question, sql, …};
    area None ⇒ the cross-subject-area bucket."""
    store.execute(
        "DELETE FROM prompt_example WHERE org_id = ? AND datasource = ?", (org_id, datasource)
    )
    for ex in examples:
        # Keep a stable id across re-seeds when the example carries one (so per-example identity
        # survives a redeploy); mint one only when absent.
        ex_id = str(ex.get("id") or uuid4().hex)
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
            "SELECT question, doc FROM prompt_example WHERE org_id = ? AND datasource = ? "
            "AND (area = ? OR area IS NULL)",
            (org_id, datasource, area),
        )
    else:
        rows = store.query(
            "SELECT question, doc FROM prompt_example WHERE org_id = ? AND datasource = ?",
            (org_id, datasource),
        )
    q = _tokens(query)
    if q:
        rows = sorted(rows, key=lambda r: len(q & _tokens(r["question"])), reverse=True)
    out: list[dict[str, Any]] = []
    used = 0
    for r in rows[:top_k]:
        doc = json.loads(r["doc"])
        size = len(json.dumps(doc))
        if out and used + size > char_budget:
            break
        out.append(doc)
        used += size
    return out


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
        self._store.execute(
            "INSERT INTO query_executions (id, ts, org_id, datasource, question, sql, row_count, "
            "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                record.ts,
                _record_org(record),
                record.profile,
                record.question,
                record.sql,
                record.row_count,
                record.source,
            ),
        )
        self._store.commit()

    def record_tool_call(self, record: Any) -> None:
        # `success` is a portable 0/1 (no boolean literal across SQLite/Postgres).
        self._store.execute(
            "INSERT INTO tool_calls (id, ts, org_id, actor, tool_name, datasource, sql, row_count, "
            "execution_ms, success, error_kind, source, user_question, agent_query, thread_id, "
            "correlation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self._store.commit()


# ---------------------------------------------------------------------------
# Read path — the admin activity views (read-only).
# ---------------------------------------------------------------------------

_TOOL_CALL_COLS = (
    "id, ts, actor, tool_name, datasource, sql, row_count, execution_ms, success, error_kind, "
    "user_question, agent_query, thread_id, correlation_id"
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
            }
        )
    return turns


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
        # Scope the group by the (audit-grade) actor as well as the (self-reported, untrusted) thread_id:
        # two different users colliding on one thread_id must NOT blend into a single, misattributed
        # conversation. A call with no thread_id stays its own singleton (grouped on its id).
        key = (r["actor"], r["thread_id"]) if r["thread_id"] else r["id"]
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
