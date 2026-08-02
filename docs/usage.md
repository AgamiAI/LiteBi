# Usage guide

The first-run walkthrough and the common workflows. For setup, see the
[Quickstart](../README.md#quickstart-under-5-minutes); for credentials, see
[docs/credentials.md](credentials.md).

## First-run walkthrough

```
$ /agami-connect
[Phase 0: preflight — no credentials yet, running first-time setup]
> Pick your database: PostgreSQL · MySQL · Snowflake · BigQuery · Other
You: Snowflake
✓ Wrote <artifacts_dir>/local/credentials.example with a [main] section for Snowflake.
  Fill it in (account, user, password OR authenticator=externalbrowser,
  warehouse, role, database, schema) then save as <artifacts_dir>/local/credentials.
  Tip: agami only runs read-only queries — a read-only DB user is safest.
  Ask for "the read-only grant" to get the exact SQL for your database.

# After filling in the file:
$ /agami-connect
[Phase 0: preflight]
  ✓ <artifacts_dir>/local/credentials present (chmod 600)
  ✓ Tier detected: snowsql (Tier 2 — native CLI)

[Phase 1: introspect]
  ✓ 14 tables across 1 schema (ANALYTICS)
  ✓ 0 FK relationships declared (Snowflake — typical)
  ✓ 23 inferred relationships from column-name + unique-index match

[Phase 2c: trust spine]
  ✓ Confidence computed for every dataset, field, relationship
  ✓ 187 field descriptions auto-approved (DBA column comments / structural pattern match)
  ✓ 21 relationships auto-approved (unique-index + plural-pattern match)
  ⚠ 8 metric proposals stamped Rule 1 (need human sign-off)
  ⚠ 14 inferred relationships unreviewed (lazy — confirm as you query)

[Phase 3: validate + write]
  ✓ Validator passed (semantic-model schema + trust block)
  ✓ Wrote the model under ~/agami-artifacts/main/ (datasource.yaml + subject_areas/…)
  ✓ Snapshot pinned at .snapshots/45f0fefa2403/
  ✓ git init + initial commit

[Phase 4: Rule 1 sign-off — BEFORE seed generation]
  8 metric proposals need your sign-off — seeds will exercise these
  definitions, so signing them off first means the seeds inherit
  approved truth instead of LLM guesses.

  Opening Rule 1 review dashboard…
  <artifacts_dir>/local/review/main/20260511-204100.html

You (in dashboard): click Approve on 6 metrics by you@example.com role=data_lead,
                    Edit 1 (calculation tweak), Reject 1.
                    Generate feedback → paste back.

✓ Applied: 7 approved (1 with edit), 1 rejected. Rule 1 complete.

[Phase 4: seed examples]
  Generating 10–12 NL→SQL seed examples and EXPLAIN-validating each
  against the live database. Expect 1–3 minutes…
  [1/11] Top 5 customers by lifetime spend — EXPLAIN ✓
  ...
  ✓ Generated 11 seed examples (≥6 multi-table, ≥1 time-comparison shape)

[Phase 5: examples validation]
  Rendered dashboard: <artifacts_dir>/local/examples-validation/main/20260511-204500.html

You (in chat): validate 1, 3, 4, 5, 7 by you@example.com
               edit 8 sql>>>
               SELECT ...
               <<<
               note 4 >>>
               Format counts with commas
               <<<
               done

✓ Validation complete: 6 validated, 1 edited, 4 unreviewed (errors).

[Phase 7: trust-layer landing — Rule 1 already done]
  ✓ Rule 1 sign-off complete · 7 items approved earlier this session

  Optional polish (low-confidence Rule 2 entries — won't block):
  ⚠ 14 inferred relationships unreviewed (lazy — confirm as you query)
  ⚠ 23 field descriptions awaiting review

  Open the Rule 2 polish queue? (y / skip — they self-approve as you query)

You: skip

You: how many customers placed an order in the last 30 days?
```

The receipt panel on the answer shows the tables it touched, the relationships
used (with their confidence + review state), the columns and metric definitions
it leaned on, and the model version (`.snapshots/45f0fefa2403/`). The SQL is not
in that panel: each report section carries its own, next to the numbers it
produced. If a query touched an unreviewed join, the report carries a trust
banner pointing back at `/agami-model` (its Review tab); an unapproved metric
gets its own banner with Approve / Change buttons, which queue the decision for
Claude to apply.

The panel also states what it did **not** establish, section by section. An empty
section with such a note means "not checked", and an empty section without one
means "checked, nothing to report" — see
[the trust layer](trust-layer.md#every-answer-ships-a-receipt).

## Common workflows

### Ask a question

```
top 10 active customers by spend last 30 days
```

The skill loads your model + examples, generates SQL, runs it, returns a markdown
table AND a chart (by default — every result gets a chart unless the shape doesn't
lend itself to one). The receipt panel below the chart shows the SQL, the tables
touched (one row per reference), the relationships used (with confidence + review
state), the metrics invoked, the columns referenced, the assumptions agami made,
and the model snapshot hash — plus, per section, what it did not establish. If a touched table is large (> 1M rows) without a date filter, the
skill prompts you before running.

If the question relies on a definition with multiple candidates — e.g. you ask
"show me revenue" and there are three `revenue`-synonym metrics — the skill asks
you which one, instead of silently picking.

### Open the review dashboard

```
You: open the review dashboard
# or: /agami-model review
# (the Review tab lists everything needing sign-off; confident items have a one-click "Approve all")
```

Walk the cards. Each shows the inferred SQL + signal breakdown + an inline editable
textarea. Click Approve / Reject / Edit on the cards you want, then hit "Generate
feedback for Claude" at the bottom and paste back. agami applies each edit, runs
the validator, commits to `<artifacts_dir>/<profile>/.git/`, and re-renders to a
new timestamped HTML file.

### Browse the model + exclude tables / columns

```
You: open the model explorer
# or: /agami-model
# or: "remove the staging tables and PII columns from the model"
```

Renders a self-contained HTML browser of every subject area → table → field →
**metric → entity → join**. Dedicated tabs surface every metric (with its prose
`calculation` + per-dialect `bindings`), entity, and relationship so you can see
exactly what the model contains without reading YAML.

Live search across names + types + descriptions + metric prose + filter
predicates. Filter chips (All / Active / Excluded / Unreviewed / Queued for
change), per-table + per-column Exclude / Include buttons. Useful when:

- You want PII columns hidden from agami without changing access at the DB level.
- A re-introspect pulled in staging / archive tables you don't want considered.
- You want to scan field names across the whole schema (e.g. "where do we have
  `created_at` columns?").
- You want a single view of every metric definition the trust layer is enforcing.

Excluded entries flip `agami.review_state` to `rejected`. The runtime model loader
filters them out everywhere — they never appear in prompts, never get joined to,
never get aggregated. The YAML still has them, so you can re-include later. The
HTML is static and rendered by Python; **no LLM tokens are spent on the YAML walk**.

### Save a correction (with attribution)

```
You: top customers should rank by lifetime spend, not just last 30 days
[agami regenerates and shows the corrected query]

You: save this as a correction
[agami classifies the correction and shows you where it'll land:
 → routing to: examples.yaml example #N (SQL pattern fix)
 → reasoning: "the corrected SQL changes the ranking expression — this
   is a SQL pattern correction, not a per-column rule"
 Confirm or override?]
```

agami's save-correction classifier routes to one of five destinations based on
what the correction is actually fixing:

| What the correction fixes | Where it lands |
|---|---|
| SQL pattern (join columns, aggregation expression, filter shape) | `examples.yaml` as a new few-shot example |
| Per-column meaning, unit, sign convention, or value normalization (Male/MALE/T → "Male") | The column's `description` / `choice_field` / `caveats` in its table YAML |
| Cross-DB display preference (format counts with commas, default time window) | `USER_MEMORY.md` (+ updates the seed example's SQL to demonstrate the formatting) |
| Abstract business concept tied to this DB ("gold tier means lifetime spend > $10k") | `datasource.md` |
| Reusable aggregation that didn't exist before ("MRR = SUM(price) WHERE plan_type='subscription'") | New `metric` in the semantic model (sign-off required — Rule 1) |

The classifier surfaces its decision before writing, so you can override if it
picks wrong. The next answer that uses the correction surfaces its attribution in
the receipt: *"this answer was influenced by a correction from you@example.com on
2026-05-11: 'use lifetime spend not 30-day window.'"*

### Render a chart

Charts are produced by default for every query result. To request a specific shape:

```
You: make that a bar chart by customer
```

The skill writes `<artifacts_dir>/local/charts/<ts>.html` — self-contained
Chart.js, the trust receipt embedded as a collapsible panel. Supported: `bar`,
`line`, `pie`, `doughnut`, `scatter`. Tables paginate at 20 rows.

### Export to CSV

```
You: export this
```

Writes the full result (no row cap) to `<artifacts_dir>/local/exports/<ts>.csv`.

### Use agami from the Claude Desktop app (`/agami-serve`)

Everything above works inside Claude Code. To ask the same questions from the
**Claude Desktop app** (or any client that reads a `claude_desktop_config.json`),
wire up the local MCP server once:

```
You: /agami-serve
# or: "set up agami for Claude Desktop"
```

It auto-detects the Python interpreter that has your DB driver, installs the
agami-core package into it (so the wiring survives plugin updates), and merges an
`agami` entry into your Claude Desktop config — backing up the previous config and
preserving every other key. Then:

1. **Fully quit** Claude Desktop (Cmd+Q on macOS — not just closing the window) and reopen it.
2. Ask *"what datasources does agami see?"* — it should call `list_datasources`.

The local server is the **mirror of the hosted Agami connector**: the same tools
(`list_datasources`, `get_datasource_schema`, `get_prompt_examples`, `execute_sql`),
backed by your local model and local execution. It's stdio-only, read-only, and makes
no network call — nothing leaves your machine. Requires `/agami-connect` to have run
first (it needs credentials + a semantic model). Re-running `/agami-serve` is safe and
idempotent — do it after a plugin update to refresh the wiring. Full details, including
where to find the log if the server doesn't appear: [docs/mcp-server.md](mcp-server.md).

### Reconcile against a legacy dashboard

When you've inherited a number from a dashboard or a spreadsheet and want to verify
the model returns the same:

```
You: /agami-reconcile ~/Downloads/q3-revenue-by-region.csv
```

Parses the CSV (auto-detects headers + number formatting — currency, magnitude
suffixes, accounting parens, percentages), generates the matching NL question for
each row, runs it through agami, and shows a side-by-side diff. Matches are green;
mismatches drill into the receipt so you can see *why* the two numbers disagree
(typically a definitional disagreement, which is exactly what the trust layer is
for).

### Edit the semantic model by hand

Open the table YAML at
`~/agami-artifacts/<profile>/subject_areas/<area>/tables/<table>.yaml` (or the
area's `metrics/`, `entities/`, `relationships.yaml`). Add a description, refine a
metric's `calculation`/`bindings`, add `caveats`. Save. The next query picks it up
— no skill restart needed.

If you flip a `review_state` from `unreviewed` to `approved` by hand on a
relationship or metric, also set `signed_off_by`, `signed_off_at`, and
`signed_off_role` — the validator will reject the file otherwise.

Format reference: [`docs/format-spec.md`](format-spec.md) and the Pydantic models
at [`packages/agami-core/src/semantic_model/models.py`](../packages/agami-core/src/semantic_model/models.py).

### When the database schema changes (new tables / new columns / dropped columns)

Re-run `/agami-connect reintrospect` (or just `/agami-connect` and pick "Refresh
the schema"). agami doesn't watch your DB for drift automatically — you have to
kick the refresh yourself.

**What survives the re-introspect** (your hand-edits are not lost):
- Descriptions, `choice_field` maps, metric definitions, entities
- Trust-layer sign-offs (`signed_off_by` / `signed_off_at` / `signed_off_role`) on
  every unchanged entry
- The rule is: **the DB is canonical for structure (tables / columns / types / PK /
  FK); the YAML is canonical for meaning** (prose, business definitions, approvals)

**What happens to the new stuff**:
| Change | Behavior |
|---|---|
| **New tables** | Fresh trust blocks per Phase 2c.2. FK relationships auto-approve where the DB declares them; structural column-name patterns (`id`, `*_id`, `created_at`, `email`, ...) auto-approve via the dictionary. Anything else stays `unreviewed`. |
| **New columns on existing tables** | Same — pattern-matched columns auto-approve, others land `unreviewed`. |
| **New metric** | If Phase 4 detects any new Rule 1 candidates, the Rule 1 gate fires *before* Phase 5 regenerates seed examples. Sign them off, then seeds inherit approved definitions. |

**What happens to drift** (column type change, FK target shift):
- The entry's `agami.review_state` flips to `stale`. Prior `signed_off_*` is
  preserved for audit.
- At runtime, `agami-query` still answers but **warns** when a query touches a
  `stale` entry (schema drift) — surfaces as: *"This used X, marked stale (schema
  drift). Run /agami-connect to re-introspect, then /agami-model to reconcile."*

**What happens to removed tables / columns** (the lossy case):
- They drop out of the model on the next write. Hand-edits on them (descriptions,
  sign-offs) are lost.
- `.git/` in `<artifacts_dir>/<profile>/` keeps the history — `git log` and
  `git show <commit>:<path>` recover prior versions if needed.
- `.snapshots/` keeps prior model versions pinned, so old query receipts still
  resolve the entries they referenced.

**Workflow**:
```
git -C ~/agami-artifacts/<profile> log -5  # optional: see what's there
/agami-connect reintrospect
# Walk Phase 4 if a Rule 1 gate fires (new metrics need sign-off)
# Walk Phase 5 examples-validation (can skip with `done`)
# Walk Phase 7's Rule 2 polish panel if you want (or skip)
git -C ~/agami-artifacts/<profile> diff HEAD~1  # diff of what the schema change cost / added
```

**Known gap**: no automated drift detection yet. You have to know the schema
changed. A "drift inbox" feature (watch DB metadata, surface changes proactively)
is in the plan but unbuilt — the v1 contract is manual re-introspect.

### Snapshot reproducibility

Every introspect writes the canonical model to
`~/agami-artifacts/<profile>/.snapshots/<hash>/`. Every query records that hash in
its receipt. To reproduce an old answer exactly, `git checkout` the matching commit
in `<artifacts_dir>/<profile>/.git/` — the model that produced the original number
is byte-identical.

### Switch profiles (multi-database)

```bash
AGAMI_PROFILE=staging
```

Or in chat: *"switch to the staging profile"*. Per-profile artifacts live under
`~/agami-artifacts/<profile>/`; credentials live in the same
`<artifacts_dir>/local/credentials` file but under a different `[<profile>]`
section.
