# Format specs

agami's state splits across two directories. See [plugins/agami/shared/file-layout.md](../plugins/agami/shared/file-layout.md) for the full design rationale; this page is the per-file format reference.

## `<artifacts_dir>/local/` — secrets + per-user state — **NEVER commit**


| File                                             | Format                                                                                          | Owner                                             |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `<artifacts_dir>/local/credentials`                           | INI (chmod 600)                                                                                 | User-edited                                       |
| `<artifacts_dir>/local/.pgpass`, `.mysql.cnf`, `.snowsql.cnf` | Provider-native auth files (chmod 600)                                                          | Skill-written by `setup_pgauth.py`                |
| `<artifacts_dir>/local/.config`                  | JSON (chmod 600) — `active_profile`, `tool_paths`, `reviewer_email`, `reviewer_role` (artifacts-dir location is the `~/.config/agami/path` pointer) | Skill-managed                  |
| `<artifacts_dir>/local/.optins`                               | JSON (chmod 600) — GitHub-star ask response                                                     | Skill-managed                                     |
| `<artifacts_dir>/local/.duckdb_init_<id>.sql`                 | SQL (chmod 600, ephemeral)                                                                      | `build_duckdb_attach.py`, deleted after the query |
| `<artifacts_dir>/local/query_log.jsonl`                       | JSONL append-only                                                                               | Skill-written, never sent, personal record        |
| `<artifacts_dir>/local/charts/<ts>.html`                      | Chart.js HTML                                                                                   | Skill-written                                     |
| `<artifacts_dir>/local/exports/<ts>.csv`                      | RFC 4180 CSV                                                                                    | Skill-written                                     |


## `<artifacts_dir>/` — sharable, can be committed (default `~/agami-artifacts/`)


| File                                              | Format                                                                                                                                                        | Owner                                                                            |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `<artifacts_dir>/USER_MEMORY.md`                  | Free-form Markdown — cross-database preferences                                                                                                               | Seeded by `agami-connect` Phase 0a, edited by user or appended by `agami-save-correction` |
| `<artifacts_dir>/<profile>/datasource.yaml`              | Org description + storage-connection refs + subject-area refs + cross_subject_area_relationships                                                              | Skill-written, user-editable                                                     |
| `<artifacts_dir>/<profile>/datasources/<conn>/storage.yaml` | Storage connection (physical): storage_type + storage_config (env-var refs, never secrets)                                                         | Skill-written, user-editable                                                     |
| `<artifacts_dir>/<profile>/subject_areas/<area>/subject_area.yaml` | Subject area: description, default_time_window, TableRefs (with optional expose_column_groups)                                              | Skill-written, user-editable                                                     |
| `<artifacts_dir>/<profile>/subject_areas/<area>/tables/<table>.yaml` | Canonical Table: columns, grain, default_filters, caveats, column_groups, performance_hints                                              | Skill-written, user-editable                                                     |
| `<artifacts_dir>/<profile>/subject_areas/<area>/{entities,metrics}/<name>.yaml`, `relationships.yaml` | Entities, metrics, and the intra-area FK graph (join cardinality + trust block)             | Skill-written, user-editable                                                     |
| `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml` | NL→SQL few-shot library (scope-tagged)                                                                                        | Skill-written (seeds) + append-only via `agami-save-correction`                  |
| `<artifacts_dir>/<profile>/datasource.md`       | Free-form Markdown — per-database domain context                                                                                                              | Seeded by `agami-connect`, edited by user or appended by `agami-save-correction` |
| `<artifacts_dir>/local/cross_profile_relationships.yaml`       | Agami-bespoke YAML — declared JOIN paths across profiles for federation. **Lives in `<artifacts_dir>/local/` because it spans profiles** and isn't tied to one team's repo | User-edited (optional)                                                           |


`USER_MEMORY.md` is **distinct** from Claude Code's auto-memory at `~/.claude/projects/<workspace>/memory/MEMORY.md`. The auto-memory is host-managed and project-scoped; `USER_MEMORY.md` is agami-managed, lives in the artifacts dir, and persists across Claude Code hosts (CLI / VS Code extension / Cursor extension) the same way the rest of `<artifacts_dir>/` does.

`USER_MEMORY.md` covers **user preferences that apply across every database** (default time windows, display preferences, exclude rules). The per-database **`datasource.md`** at `<artifacts_dir>/<profile>/datasource.md` covers **domain knowledge for that specific database** (terminology, key metrics, what the data represents). See [plugins/agami/shared/organization-context-format.md](../plugins/agami/shared/organization-context-format.md).

`<profile>` matches the section name in `<artifacts_dir>/local/credentials` (default: `default`). One *directory* per profile under `<artifacts_dir>/`. The `agami-connect` skill auto-migrates v1.0 (single-file) and v1.1 (under `<artifacts_dir>/local/`) installs on first run after upgrade.

## Why the split

Three concrete wins (full design in [shared/file-layout.md](../plugins/agami/shared/file-layout.md)):

1. **Zero credential-leak risk on commit.** `<artifacts_dir>/local/` is gitignored by default; `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, datasource.md, and USER_MEMORY.md preferences.
3. **Power users override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session.

---

## 1. Credentials INI

See [plugins/agami/shared/credentials-format.md](../plugins/agami/shared/credentials-format.md). `chmod 600` is enforced by `agami-connect` Phase 0a.

```ini
[default]
type     = postgres
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword
```

---

## 2. Semantic model

The model is a **provider-portable, standard-concepts hierarchy** that any LLM can traverse to build reliable SQL against any backend:

```
Datasource (datasource.yaml)
├─ Storage Connections[]   (physical: host/creds/dialect — datasources/<conn>/storage.yaml)
└─ Subject Areas[]         (logical — the primary unit the LLM consumes; cap ~20-30 tables)
   ├─ tables[]             (TableRefs into storage connections; expose_column_groups scopes wide tables)
   ├─ tables_defined[]     (canonical Table: columns, grain, default_filters, caveats, column_groups)
   ├─ entities[]           (vocabulary → maps_to columns; value_pattern for opaque IDs)
   ├─ metrics[]            (calculation prose + per-dialect bindings)
   └─ relationships[]      (FK graph; REQUIRED join cardinality + trust block)
└─ cross_subject_area_relationships[]   (org-level edges spanning areas)
```

The Pydantic models in [`packages/agami-core/src/semantic_model/models.py`](../packages/agami-core/src/semantic_model/models.py) **are** the spec (they `forbid` unknown keys). Provider-portable declarative fields — `default_filters`, `value_transform`, `caveats`, `value_pattern`, `sensitive`, `default_time_window`, join `cardinality` — are applied generically by the MCP/runtime, so behavior is identical across LLMs.

Every write is gated by the validator at [`packages/agami-core/src/semantic_model/validator.py`](../packages/agami-core/src/semantic_model/validator.py) (driven via `python3 -m semantic_model.cli validate <root>`). **No model that fails validation is ever persisted.**

### Worked example — a minimal model on disk

```yaml
# datasource.yaml
datasource: shop
version: 1
description: E-commerce shop.
storage_connections:
  - { name: shop_postgres, ref: datasources/shop_postgres/storage.yaml }
subject_areas: [subject_areas/sales]
cross_subject_area_relationships: []
```
```yaml
# datasources/shop_postgres/storage.yaml
name: shop_postgres
storage_type: PostgreSQL
storage_config: { profile: shop, credentials_ref: "<artifacts_dir>/local/credentials" }
```
```yaml
# subject_areas/sales/subject_area.yaml
name: sales
description: Sales analytics across orders and customers.
tables:
  - { storage_connection: shop_postgres, schema: public, table: orders }
  - { storage_connection: shop_postgres, schema: public, table: customers }
```
```yaml
# subject_areas/sales/tables/orders.yaml
name: orders
schema: public
storage_connection: shop_postgres
grain: [id]
description: One row per order.
default_filters: ["{alias}.deleted_at IS NULL"]
columns:
  - { name: id, type: integer, primary_key: true }
  - { name: customer_id, type: integer, foreign_key: { table: customers, column: id } }
  - { name: status, type: string, choice_field: { pending: Pending, shipped: Shipped } }
  - { name: amount, type: decimal, caveats: ["Amount in USD; excludes refunds."] }
```
```yaml
# subject_areas/sales/relationships.yaml
relationships:
  - from_table: orders
    from_column: customer_id
    to_table: customers
    to_column: id
    relationship: many_to_one        # REQUIRED — fan-trap detector consumes this
    confidence: confirmed
    review_state: approved
    signed_off_by: agami_introspect
    signed_off_role: system
    signed_off_at: "2026-06-09T00:00:00Z"
```
```yaml
# subject_areas/sales/metrics/total_revenue.yaml
name: total_revenue
calculation: Total revenue across all orders (USD, excludes refunds).
bindings: { PostgreSQL: "SUM(orders.amount)" }
source_tables: [orders]
other_names: [revenue, gross sales]
confidence: proposed
review_state: unreviewed             # Rule 1: needs sign-off before the runtime uses it
```

### Validation rules

The validator combines Pydantic structural validation (required fields, enums, relationship completeness — exactly one of `from_column`+`to_column` OR `on:`, one primary per entity) with cross-cutting invariants:

1. **Relationship cardinality required** on every join; **trust-block parity** (`signed_off_*` required when `review_state: approved`).
2. **FK type-compatibility** — a simple-FK join with mismatched column types caps `confidence` at `proposed` and suggests a `CAST` in `on:` (BigQuery `INT64 = STRING` class of bug).
3. **Subject-area sizing** — warn at 25 tables, error at 30.
4. **Deep-table column_groups** — tables ≥ 30 columns must declare `column_groups`; no column may be orphaned; `expose_column_groups` must reference declared groups; every `TableRef.table` resolves to a `tables_defined` entry (org-wide, for multi-membership).
5. **default_filters** reference only existing columns; **value_transform** / **on:** parse with sqlglot; **caveats** non-empty; **choice_field** keys match the column type.
6. **Cross-area entity name-collision** → warning + a suggested cross-cutting unification.

The verdict is **binding**: `cli validate` exits 0 → ok, 1 → errors. `cli curate` and the introspection engine refuse / revert on a non-zero verdict.

---

## 3. Examples library YAML

Few-shot NL→SQL pairs loaded by `query-database` (most-recent 50). Appended to by the `agami-save-correction` skill.

```yaml
# <artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml

examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Top 5 customers by spend
    sql: |-
      SELECT c.name, SUM(o.amount) AS spend
      FROM customers c
      JOIN orders o ON o.customer_id = c.id
      GROUP BY c.id, c.name
      ORDER BY spend DESC
      LIMIT 5
    source: correction
    created_at: 2026-05-07T18:30:00Z
    confirmed: true
    confirmed_at: 2026-05-07T18:30:00Z
```


| Field          | Type    | Required | Description                                                                                       |
| -------------- | ------- | -------- | ------------------------------------------------------------------------------------------------- |
| `question`     | string  | yes      | NL question that triggers this example                                                            |
| `sql`          | string  | yes      | Reference SQL                                                                                     |
| `source`       | enum    | yes      | `seed` (auto-generated by `connect`) or `correction` (saved by the `agami-save-correction` skill) |
| `created_at`   | ISO8601 | yes      | When the example was added                                                                        |
| `confirmed`    | bool    | no       | `true` if the user explicitly confirmed it                                                        |
| `confirmed_at` | ISO8601 | no       | When confirmed                                                                                    |


**Why separate?** The semantic model carries structure + metrics, not NL→SQL examples. The examples library is its own scope-tagged file per subject area.

---

## 4. User memory (free-form Markdown)

`<artifacts_dir>/USER_MEMORY.md` (committable top-level, **not** under `local/` — it's cross-DB preferences worth sharing, not a secret) holds free-form preferences and policies that don't belong in the semantic model — default filters, domain vocabulary, display preferences, hard avoids. Every agami skill loads this file on each invocation and applies what's in it to SQL generation, formatting, and follow-up suggestions.

Seeded by `agami-connect` Phase 0a on first run with section hints (HTML comments). User edits by hand, OR the `agami-save-correction` skill appends a bullet when it classifies a correction as `user_preference` ("from now on, always exclude test users where email matches @example.com").

Full spec: [plugins/agami/shared/user-memory-format.md](../plugins/agami/shared/user-memory-format.md).

## 5. Internal state files

### `<artifacts_dir>/local/.config`

```json
{
  "schema_version": 1,
  "active_profile": "main",
  "artifacts_dir": "/Users/me/agami-artifacts",
  "reviewer_email": "you@example.com",
  "reviewer_role": "data_lead",
  "tier": "cli",
  "host": "claude-code-cli",
  "tool_paths": {
    "psql": "/opt/homebrew/bin/psql",
    "sqlite3": "/usr/bin/sqlite3"
  }
}
```

Written by `agami-connect` Phase 0a on first run; updated by `agami-model` (its Review tab) the first time the curator approves a Rule 1 item (the `reviewer_email` + `reviewer_role` get persisted so future sessions don't re-ask).

> **Note on `artifacts_dir`:** this field is recorded for reference (and read as a fallback signal that onboarding has already chosen a folder), but it is **not** the authoritative locator — `.config` lives *inside* `<artifacts_dir>/local/`, so it can't bootstrap its own location. The source of truth for finding `<artifacts_dir>` is, in order: `AGAMI_ARTIFACTS_DIR` → the `~/.config/agami/path` pointer → the default `~/agami-artifacts`.

### `<artifacts_dir>/local/.optins`

```json
{
  "schema_version": 1,
  "github_star_asked": true,
  "github_star_response": "yes_opened",
  "ts": "2026-05-07T18:30:00Z"
}
```

`github_star_response` is one of `yes_opened` (user clicked through to GitHub), `maybe_later`, or `already_starred`. Existence of the file is the never-re-prompt gate — we ask exactly once, after the user's first successful query.

### `<artifacts_dir>/local/query_log.jsonl`

```jsonl
{"ts":"2026-05-07T15:14:00Z","question":"how many orders shipped in May","sql":"SELECT ...","row_count":4,"execution_ms":250,"tier":"cli","risk":"LOW","error_kind":null,"chart_path":"/Users/me/.agami/charts/20260507-141500.html"}
```

Fields per line:


| Field            | Type             | Description                                                                                                                                                                     |
| ---------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ts`             | ISO8601 UTC      | When the query ran                                                                                                                                                              |
| `question`       | string           | The user's NL question                                                                                                                                                          |
| `sql`            | string           | The executed SQL                                                                                                                                                                |
| `row_count`      | integer          | Rows returned (post-filter, pre-truncation)                                                                                                                                     |
| `execution_ms`   | integer          | Wall-clock latency                                                                                                                                                              |
| `tier`           | enum             | Connection method that ran the query: `cli` (native CLI), `duckdb`, `python` (Python driver). Field name is `tier` for backward-compatibility with v1.0 logs.                   |
| `risk`           | enum             | `LOW` / `MEDIUM` / `HIGH` (large-table risk classifier)                                                                                                                         |
| `error_kind`     | enum or null     | Set when execution failed; one of the 9 classifier kinds                                                                                                                        |
| `chart_path`     | string or null   | Absolute path of the HTML report from Phase 4e, or null if the query returned a 1×1 scalar that didn't get a report. Read by `query-database`'s reopen-intent flow (Phase 2a.1) |
| `tables_used`    | array of strings | Qualified `<schema>.<table>` names the SQL FROMs/JOINs. For two-pass retrieval (Phase 2b large mode), this is what Pass 1 picked; for small mode, parsed from the SQL.          |
| `retrieval_mode` | enum             | `small` or `large` — which Phase 2b branch ran. Useful for tuning the 50-table threshold.                                                                                       |


**Local-only** — never sent. Records every query. Grep / aggregate it in your own tooling if you want personal analytics.

The MCP server writes to this same file when no database is configured, and its records carry the audit fields instead of the skill's rendering fields:

| Field    | Type           | Description                                                                                                                                              |
| -------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`     | string         | The execution's id — the same value the tool response returned as `audit_id`. When a database *is* configured this is `query_executions.id`.              |
| `profile`  | string       | The datasource the statement ran against                                                                                                                 |
| `source`   | string       | What wrote the record (`mcp_server`)                                                                                                                     |
| `status`   | enum         | `ok`, `refused` (agami declined the statement) or `failed` (the database rejected it). Absent on entries written before this field existed — read those as `ok`. |
| `reason`   | enum or null | Refusals only: `unsafe`, `out_of_scope` or `undetermined`                                                                                                |
| `rule`     | string or null | Refusals only: which gate fired (`read_only`, `table_scope`, `column_scope`, …)                                                                        |
| `sql_truncated` | boolean | The `sql` above was cut to the audit bound (8,000 characters) and is not the whole statement            |
| `org_id`   | string       | The tenant the query ran for (`local` on a single-tenant install)                                                                                        |

**Read the log filtered.** Anything treating "the last entry" as "the query you just ran" — the save-correction and reopen-chart flows both do — must skip entries whose `status` is not `ok`: a refused or failed statement never returned a result to correct or a chart to reopen.

---

## 6. Chart artifacts (`<artifacts_dir>/local/charts/<ts>.html`)

Self-contained Chart.js v4 HTML, rendered from [plugins/agami/shared/chart-template.html](../plugins/agami/shared/chart-template.html) with placeholders substituted (`{{TITLE}}`, `{{CHART_TYPE}}`, `{{LABELS}}`, `{{DATASETS}}`, `{{GENERATED_AT}}`, `{{SQL}}`). Open in any browser.

## 7. CSV exports (`<artifacts_dir>/local/exports/<ts>.csv`)

Standard RFC 4180 CSV. UTF-8, no BOM.