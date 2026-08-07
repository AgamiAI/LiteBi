---
name: agami-save-correction
description: "Saves a user correction so future queries learn from it. Always appends a (question, corrected_sql) pair to the subject area's example library under <artifacts_dir>/<profile>/prompt_examples/<area>/. Additionally, classifies the correction and — when applicable — applies a surgical edit to the semantic model itself (relationship fix, column metadata, or new metric) via the curation engine. Every model edit is validated before write; the validator is the binding gate, and a failed validation reverts. Shows the user a model diff for approval before any model mutation."
when_to_use: "Use when the user says 'save this as a correction', '/agami-save-correction', 'remember this', 'use this SQL next time', or after the user manually fixes a query result and wants future similar questions to use the fix. Also use when the demo query in agami-connect/SKILL.md gets a 'No' answer — that's a correction in disguise."
argument-hint: "[corrected SQL or NL feedback]"
---

# agami save-correction

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** Agami slash commands: `/agami-connect`, `/agami-query`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. (`/agami-model`'s Review tab absorbed the former `/agami-review`.) Never write the un-prefixed forms (`/save-correction`, `/init`, etc.) or colon forms (`/agami:save-correction`) — those don't exist. **`/agami-init` was folded into `/agami-connect` Phase 0a.** For chat replies, prefer natural language ("say 'save this as a correction'", "say 'remember this'") — the agami-save-correction skill's `when_to_use` matcher routes correctly.

You are recording a user correction. Goal: persist the fix so similar questions get better answers next time.

This skill does two things, in this order:

1. **Always**: append the `(question, corrected_sql)` pair to the subject area's example library at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`.
2. **When applicable**: surgically update the semantic model at `<artifacts_dir>/<profile>/` (a relationship/column/table edit, or a new metric) with the knowledge implied by the correction, **via the curation engine** (`semantic_model.cli curate`), which **validates** before write and reverts on failure. If the user's correction would break the model, refuse the model update (the example still gets saved).

For the model format: [`semantic_model/__init__.py`](../../../../packages/agami-core/src/semantic_model/__init__.py) (layout) + `packages/agami-core/src/semantic_model/models.py`. The curation engine is `packages/agami-core/src/semantic_model/curate.py`.
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-save-correction needs Write (examples + model edits) and Bash (EXPLAIN-validation) — both are blocked in plan mode.

**If plan mode is active and the user picks `Stay in plan mode` (or this skill is invoked under an active plan-mode context):** refuse and end the turn. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text (verbatim):

> I can't save corrections in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. The correction won't persist otherwise.

If plan mode is not active, skip this phase silently and go to Phase 1.

---

## Phase 1: Identify the correction

### 1a — resolve the active profile and artifacts_dir

Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `<artifacts_dir>/local/.config` → literal string `"default"` (legacy fallback).

Resolve `<artifacts_dir>` per [`shared/file-layout.md → Configuring artifacts_dir`](../../shared/file-layout.md#configuring-artifacts_dir): `AGAMI_ARTIFACTS_DIR` env var → `<artifacts_dir>/local/.config.artifacts_dir` → default `$HOME/agami-artifacts`. All examples / model / datasource.md paths in this skill resolve under `<artifacts_dir>/<profile>/`. USER_MEMORY.md is at `<artifacts_dir>/USER_MEMORY.md` (top-level, cross-database).

For v1.0 / v1.1 fallback paths (`<artifacts_dir>/local/<profile>.yaml`, `<artifacts_dir>/local/<profile>-examples.yaml`, `<artifacts_dir>/<profile>/`), only read; never write. Migration is agami-connect's job — this skill assumes the user has already migrated by the time they're saving corrections.

### 1b — find the most recent query

Read `<artifacts_dir>/local/query_log.jsonl` and take the last entry **whose `status` is `"ok"` or absent**. Need `question` and `sql`.

The log now records every execution, not only the successful ones: an entry with `"status": "refused"` (agami declined the statement) or `"status": "failed"` (the database rejected it) carries SQL that never returned a result, so attaching a correction to it would teach the example library from a query that never ran. Entries written before the log carried a `status` field have none — treat those as `ok`.

If there is no such entry: "I don't have a recent successful query to attach this correction to. Ask the question first, then save the correction." Stop.

### 1c — get the corrected SQL

Determine what the user gave:
- **They pasted SQL** (`$ARGUMENTS` looks like a SELECT, contains `FROM` / `JOIN` / `GROUP BY`) → use directly as the corrected SQL.
- **They described what's wrong** ("the join should be on customer_id, not user_id"; "amount is in cents") → regenerate SQL using the semantic model + the original question + their feedback as additional context. Same prompt assembly as `query-database` Phase 2b.
- **No arguments and no recent feedback** → ask: "Paste the corrected SQL, or tell me what's wrong with the result."

### 1d — EXPLAIN-validate the corrected SQL

Run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the cached database tool from `<artifacts_dir>/local/.config`. Same validate-then-save contract as `agami-connect/SKILL.md` Phase 5b:

- EXPLAIN succeeds → continue.
- EXPLAIN fails → route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface the one-line remediation. Do **not** save anything. Ask the user to fix the SQL and try again.

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):
- Refuse DDL/DML (DROP, DELETE, INSERT, UPDATE, ALTER, etc.).
- Refuse system-table queries unless the user explicitly asked about schema metadata.

---

## Phase 2: Always append to the examples library

Examples live per subject area at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`. Pick `<area>` = the subject area whose `tables_defined` includes the table(s) the corrected SQL references (run `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" areas "$ROOT"` and match, or `get_table_context` to confirm membership; if the SQL spans areas, use the area of the primary/driving table).

**Use the packaged writer — don't Read/Edit the YAML by hand or grep the source for its schema.** It creates the file if absent, appends, and **dedups by `question`** (a same-question correction replaces the earlier answer). One entry in a JSON array:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add-example "$ROOT" --area <area> --file /tmp/agami-correction-example.json
```
```json
[{"question": "<original NL question from query_log.jsonl>", "sql": "<corrected SQL>",
  "tables": ["..."], "source": "correction", "status": "confirmed", "created_at": "<ISO8601 UTC>"}]
```
Required: `question`, `sql`. Optional scope tags (improve ranking): `tables`, `columns`, `metric`.

This phase is **non-conditional** — every correction always lands in the examples library, even if Phase 5 (model update) declines or fails.

Surface: `✓ Correction appended to <artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml.`

---

## Phase 3: Classify the correction

Compare the original SQL (from `query_log.jsonl`) to the corrected SQL. Identify what the user was teaching us, then route the correction to the right destination. **Mis-routing is the most common failure** — early-adopter feedback included three real cases where corrections landed in the wrong file:

| Category | What the user did (wrong) | What should have happened |
|---|---|---|
| Per-example commentary ("order total can be negative on #12") | Captured in `examples.yaml` `notes[]` | Field description on `ORDERS.TOTAL` (`field_metadata`) |
| Status normalization (active / ACTIVE / 1 → "Active") | `datasource.md` | `agami.choice_field` on `CUSTOMERS.STATUS` (`field_metadata`) |
| "Format counts with commas in outputs" | Captured in a markdown file as prose | `user_preference` in `USER_MEMORY.md` AND `TO_CHAR(…)` / aliases IN the seed example's SQL |

The decision tree below corrects these failures. **Walk it top to bottom — first match wins.** Don't fall back to `org_context` as a default; it's the catch-all that produces the wrong outcome in practice.

### Decision tree (top to bottom — first match wins)

```
Is the correction about a single column's MEANING, UNIT, ENCODING, or VALUE NORMALIZATION?
   (e.g., "amount is in cents", "1 means active not true", "the order total can be
    negative for refunds", "active/ACTIVE/1 all map to 'Active'", "STATUS='1' means active")
   → field_metadata
       → if it's specifically about value → canonical-display mapping, write to
         agami.choice_field on that field.
       → if it's about how the column is interpreted (unit, sign convention,
         encoding), update agami.unit + the field description prose.

Else: is the correction about a JOIN — which columns connect two tables?
   (e.g., "join on customer_id, not user_id"; "products → categories via category_id")
   → relationship

Else: is the correction about what a WHOLE TABLE represents, not one column?
   (e.g., "`orders` includes cancelled rows too", "`metrics_daily` is materialized
    from `events`, use it for date ranges > 7 days")
   → table_metadata

Else: does the corrected SQL define a REUSABLE AGGREGATION that didn't exist?
   (e.g., "MRR = SUM(price) WHERE plan_type='subscription'", "active customers
    means is_active AND last_login > 30 days ago")
   → new_metric
       → if also touches the predicate side (active customers), consider whether
         the predicate alone deserves a named_filter — same Rule 1 sign-off.

Else: is the correction a DISPLAY / FORMATTING / DEFAULT-FILTER preference?
   **Classify it like everything else — don't reflexively ask. Route to the MOST
   SPECIFIC home, structured-model-first:**
   - It's the **currency/unit** of specific column(s) — "amounts are in INR → show ₹",
     "this is a percentage", "values are in days" → set that column's **`unit`** field
     (the ISO currency code, or `percent`/`cents`/`days`) via `cli curate`
     (`{op:edit, kind:table, area, name:<table>, column:<col>, field:unit, value:"INR"}`).
     The runtime + chart renderer format it **deterministically** (`units.py`). This IS
     the org-wide home — it's in the shared model.
   - Another column fact — "amount is in cents" (a scale fix), "this code maps to
     <label>" → `field_metadata`: `value_transform` (`amount/100.0`) or `choice_field`
     on that column. Also in the shared model, org-wide.
   - It's a default filter on a table — "exclude soft-deleted", "tenancy filter" →
     the table's `default_filters` (model), via `cli curate`.
   - It's a cross-cutting presentation convention not tied to one column — "present
     money with lakh/crore grouping" → `user_preference` → `USER_MEMORY.md` (it's a
     presentation rule, not domain meaning; datasource.md is narrative-only now).
   - It's a personal stylistic tic that would hold on ANY database — "I like top-10
     not top-5", "my date format" → `user_preference` → `USER_MEMORY.md`.
   Only when you genuinely can't tell personal vs org-wide → **AskUserQuestion** (the
   ambiguity fallback below). Default a currency/unit/data fact to the **model**, not a
   prose file.
   → In all cases, if it changes how SQL renders results (TO_CHAR, ROUND, a symbol),
     ALSO bake it into the affected seed example's SQL so future answers apply it.

Else: is the correction about a BUSINESS TERM specific to this database's domain?
   (e.g., "gold tier means lifetime spend > $10k" — used as a category in many
    queries; "MRR" — the abstract concept; "we don't track refunds, those live
    in Stripe" — what the data fundamentally doesn't include)
   → org_context. A term → `cli set-terminology` (the structured `key_terminology`
     glossary). A higher-level narrative ("we don't track refunds…", "who the users
     are") → a datasource.md prose line. See the `org_context` edit section for both.
       → org_context is for ABSTRACT business concepts not tied to one specific
         column. A correction tied to a specific column belongs in field_metadata,
         NOT here. Re-check the first rule of the tree before landing here.

Else: pure SQL syntax / typo with no domain knowledge implied
   (e.g., "missed the GROUP BY", "`customer_idx` is a typo of `customer_id`")
   → sql_fix
```

### Anti-patterns the LLM keeps producing (do NOT do these)

1. **Per-column rule → datasource.md.** "`CUSTOMERS.STATUS` values normalize to Active" is NOT domain context — it's a column-value mapping. Route to `field_metadata` (`choice_field`).
2. **Per-column rule → examples.yaml notes.** This skill never writes to `examples.yaml.notes[]` (that path lives in agami-connect Phase 6d). If you find yourself wanting to write "the order total can be negative" as a note on example #12, route it to `field_metadata` on the actual column instead — the lesson applies to every future query, not just to one example.
3. **Dumping a column-fact into a prose file (or USER_MEMORY).** "Amounts are in INR → show ₹" is a fact about the `amount` column → a `caveat`/`value_transform` on that column (org-wide, structured, in the shared model) — NOT a USER_MEMORY line and NOT a datasource.md prose rule. Route data-facts to the column/table; reserve the prose files for cross-cutting conventions (datasource.md) and personal tics (USER_MEMORY). Don't reflexively ask — classify; ask only when personal-vs-org is genuinely unclear.
4. **Display preference → prose without changing SQL.** If the correction is "always format like X," ALSO modify the seed example's SQL to demonstrate the formatting (so future answers actually apply it, not just describe it).

### Diff-based hints (look at SQL changes for classification clues)

- JOIN condition changed → likely `relationship`.
- Math applied to one column (`/100`, `* 100.0`, `CAST(...)`) → likely `field_metadata` (with a `unit` correction).
- `CASE WHEN col = 'X' THEN 'Y' ELSE 'Z' END` for a column's display value → likely `field_metadata` with `choice_field` update.
- New WHERE clause referencing a specific business term (e.g., `plan_type='subscription'`) AND new aggregation → likely `new_metric`.
- `TO_CHAR(...)`, `ROUND(...)`, `AS my_alias`, a currency symbol purely on the output side → a display rule. If it's about a specific column (a currency/unit) → that column's `caveat`/`value_transform` (model, org-wide). If it's a personal style tic → `user_preference`. ALSO update the seed example's SQL either way.
- Only structural / cosmetic SQL changes → `sql_fix`.

### When ambiguous, AskUserQuestion (use the rubric above as your option set)

> What kind of correction is this?
> - **A SQL fix** — the answer was wrong but the model is fine
> - **A column meaning change** — e.g., amount is in cents, status means something specific, Male/MALE/T all map to "Male"
> - **A join correction** — relationships in the model need updating
> - **A table meaning change** — the description / context for a whole table
> - **A new business metric** — let's add this as a reusable metric
> - **A display / formatting rule** — number formatting, currency/units, default filters (currency/units attach to the column; I only ask about scope if it's genuinely unclear)
> - **Domain context for this database** — abstract business concepts not tied to one column (e.g., "gold tier means lifetime spend > $10k")

The user's answer determines Phase 5 routing.

**Distinguishing `org_context` vs `user_preference` vs `field_metadata`:**
- If the rule is tied to a specific column → `field_metadata`. **Always check this first.**
- Else ask: "would this guidance apply if I connected to a different database?" If yes → `user_preference`. If no (it's specific to this domain) → `org_context`.

### Phase 4 — surface classification + destination BEFORE Phase 5 writes anything

After classifying, **always** surface the decision to the user in chat as a one-line summary with explicit reasoning, then proceed with the edit. The contract:

```
Classification: <kind>
  → routing to: <destination file + the specific field/section that'll change>
  → reasoning: <one sentence explaining which rule of the decision tree matched>
```

Concrete examples:

```
Classification: field_metadata
  → routing to: sales/ORDERS.yaml → fields["TOTAL"].description
  → reasoning: rule says "correction about a single column's meaning/encoding/sign convention" — you're teaching that this column can be negative because refunds carry a negative sign.

Classification: field_metadata
  → routing to: sales/CUSTOMERS.yaml → fields["STATUS"].agami.choice_field
  → reasoning: rule says "value normalization mapping (active/ACTIVE/1 → 'Active') belongs in choice_field" — not datasource.md.

Classification: user_preference + seed-example update
  → routing to: USER_MEMORY.md (the prose preference) AND examples.yaml example #N (TO_CHAR in SQL)
  → reasoning: "always format counts with commas" is a display preference (cross-DB), and the seed example needs the formatting baked into its SQL so future answers actually apply it.
```

The user can override before any file is written. If they say "no, that belongs in X instead," re-route to X and re-surface the new classification before proceeding.

---

## Phase 5: Apply surgical model edits (when applicable)

If the correction kind is `sql_fix`: **stop here**. Phase 2 already saved the example. Surface the closing message and skip to Phase 6.

For every other kind, you propose a model edit and run the validator BEFORE writing.

### 4a — propose the edit

Model edits go through the curation engine (`semantic_model.cli curate "$ROOT" --ops-file …`), which validates + commits + reverts on failure — you don't stage/validate/promote by hand. `ROOT="<artifacts_dir>/<profile>"`. Resolve the subject `<area>` for an affected table the same way as Phase 2 (the area whose `tables_defined` holds it). The new-metric case uses `cli add --kind metric` (curate's `--ops-file` edits existing entries; `add` creates them) — same validate + commit + revert guarantees.

**Fixing a column/table `description` marks it human-validated.** A correction that rewrites a `description` via a curate `edit` op (no `source:"ai"`) automatically sets `description_source: "human"` — so a description agami had inferred is now trusted and stops surfacing as an "assumption" in answer receipts. You don't set `description_source` yourself; the curate engine does it.

| Edit kind | How |
|---|---|
| `relationship` | `cli curate` `edit` op(s) on the relationship in `<area>` |
| `field_metadata` | `cli curate` `edit` op(s) on the column (kind: table, + `column`) |
| `table_metadata` | `cli curate` `edit` op(s) on the table |
| `new_metric` | Write a new `subject_areas/<area>/metrics/<name>.yaml`, then `cli validate "$ROOT"` |
| `org_context` | a term → `cli set-terminology` (structured `key_terminology`, validated); a narrative line → append to `datasource.md` (no validator) |
| `user_preference` | append to `USER_MEMORY.md` (no validator) |

#### `relationship` edit

Find the relationship in `<area>`'s `relationships.yaml` (by `from_table`/`to_table`). The corrected SQL's `ON` clause tells you the fix:
- **Different columns** → `edit` op setting `from_column` / `to_column` (or, for a CAST/compound join, set `on:` and null out `from_column`/`to_column` — the "approve with fix" shape).
- **Missing relationship** → write a new entry into `relationships.yaml` (unique by from/to), with the **required** `relationship:` cardinality inferred from the join (many_to_one unless the keys are both unique), then `cli validate`.
- **Spans two areas** → it's a `cross_subject_area_relationship` (org-level `cross_subject_area_relationships.yaml`), with `from_subject_area`/`to_subject_area` + `executable`.
- **Reverses direction** → ask before flipping. Never delete a relationship unless the user explicitly says "remove the relationship".

#### `table_metadata` edit

`edit` op(s) on the table: set `description` (what the table represents) or append a `caveats[]` entry (a usage note / quirk). Never change `grain`, `columns`, `source_type`, or relationships from a table-metadata correction — those are structural.

#### `field_metadata` edit

`edit` op(s) on the column (`kind: table`, `area`, `name: <table>`, `column: <col>`):
- Set `description` ("Order amount in cents.").
- A unit / data-quality note → append a `caveats[]` entry ("Amounts in cents; divide by 100 for dollars.").
- An enum mapping ("status `1` = active, `0` = inactive") → set `choice_field`.
- A cleaning rule ("strip the brackets") → set `value_transform` (must parse as SQL — the validator checks).
Never set a column's structural identity from a field-metadata correction.

#### `new_metric` edit

Write `subject_areas/<area>/metrics/<name>.yaml` (snake_case name, unique in the area):

```yaml
name: <derived_from_user_request>
calculation: <one-sentence prose intent — REQUIRED, never empty>
bindings:
  <storage_type>: <the aggregation SQL from the corrected SQL>
source_tables: [<tables it reads>]
other_names: [<the user's term + obvious variants>]
confidence: proposed
review_state: unreviewed
```

Reference columns plainly (`<table>.<column>`). Strip user-specific WHERE filters (`WHERE customer_id = 42`); keep only definitional ones (`WHERE plan='subscription'`). Then `cli validate "$ROOT"`. A `proposed`/`unreviewed` metric needs sign-off on the `/agami-model` Review tab before the runtime will use it (Rule 1) — tell the user.

#### `user_preference` edit

A `user_preference` correction does NOT touch the semantic model. It lands in `<artifacts_dir>/USER_MEMORY.md` (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)) — the **global** preferences file that applies across every database. Steps:

1. **Read** `<artifacts_dir>/USER_MEMORY.md` (it exists — `init` seeds it).
2. **Pick the right section** (`Default filters`, `Naming and synonyms`, `Display preferences`, or `Avoid`) based on the policy's nature. Add a new section if none of the four fits — keep this rare.
3. **Append the new bullet** under that section, in plain English (the user's wording, lightly cleaned). Don't paraphrase aggressively — preserve their voice.
4. **Show the user the diff** (per Phase 4b below) before writing.
5. **Strip nothing** — USER_MEMORY.md is intentionally free-form, not schema-validated. The validator (Phase 4c) is a no-op for `user_preference` corrections; the semantic model is unchanged.

The user's bullet should be self-contained — anyone reading USER_MEMORY.md should understand the policy without seeing the original conversation.

#### `org_context` edit

`org_context` splits by **what kind of fact it is** — each goes to its proper home (per [`shared/organization-context-format.md`](../../shared/organization-context-format.md)). The two homes are deliberately separate; never write one kind into the other.

- **A term's meaning** (the common case) — "gold tier" = lifetime spend > $10k, "MRR" = monthly recurring revenue, or an acronym → its expansion. This goes to the **structured glossary**, NOT a prose file — `set-terminology` merges it onto `key_terminology` (validated, committed), and it then surfaces in the derived domain context on every query automatically (no file to re-render, nothing for a human to clobber):
  ```bash
  printf '{"gold tier": "lifetime spend > $10k"}' > /tmp/agami-term.json
  bash "$AGAMI_PLUGIN_ROOT/scripts/sm" set-terminology "$ROOT" --file /tmp/agami-term.json
  ```
  The key is the term; the value is a **self-contained** definition (understandable without the original conversation). It merges by default — existing terms are never lost. **Never** hand-append `- "term" = definition` lines to datasource.md; that's the old prose home and is wrong now.
- **A higher-level narrative** — what the data represents, who the users are, what's *not* in this database. Append a sentence/paragraph to `<artifacts_dir>/<profile>/datasource.md` under `# About this database` (create it with the starter if missing — `cli org-draft "$ROOT" > "$ROOT/datasource.md"`). This file is the human narrative **only** — no `term = definition` lines, no model facts.
- **A cross-cutting display/formatting convention** (a currency symbol or number grouping everyone querying this DB should see) is a *presentation* preference, not domain meaning: route it to `user_preference` → `USER_MEMORY.md`, or — when it's really a fact about one column (units/currency) — to that column's `caveat`/`value_transform`. Do **not** invent a datasource.md "conventions" heading; the file is narrative-only.

**Show the user the diff** (Phase 4b) before writing. `set-terminology` is validated (reverts on failure); datasource.md prose is free-form (no validation).

#### `mixed` edit

Apply each individual edit as above. Show the user the combined diff in 4b before validating. If the mix includes a `user_preference` or `org_context`, those parts skip the validator (USER_MEMORY.md / datasource.md aren't validated); the semantic-model parts still go through the validator.

### 4b — show the diff to the user, get approval

Build a unified diff (or a compact "before / after" summary) of the proposed change against the existing target file. Name the file in the prompt so the user knows what they're approving. Show via AskUserQuestion:

> I want to update the target file in `<artifacts_dir>/<profile>/subject_areas/<area>/:
>
> ```
> [Relationship] orders_to_customers
> - from_columns: [user_id]
> + from_columns: [customer_id]
> ```
>
> Approve?
> - **Yes (Recommended)** — apply and validate
> - **No** — leave the model as-is, the example is still saved
> - **Edit first** — let me tweak before applying

For `org_context` / `user_preference` the file is `datasource.md` / `USER_MEMORY.md` instead of a schema yaml — same prompt shape, just a different filename.

Always include the validator step in 4c regardless of which option they pick (since "Yes" still has to validate, except for datasource.md / USER_MEMORY.md which aren't validated).

### 4c — apply with validation (the gate)

**For `relationship` / `field_metadata` / `table_metadata` edits** (existing entries): build the ops array, **write it with the Write tool** (never a heredoc / shell variable / `python3 -c` — JSON quotes and `null` break those), then apply via the curation engine — it validates the whole model, commits to the profile git repo, logs to `curation_log.jsonl`, and **reverts every change if validation fails**:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" --ops-file /tmp/agami-correction-ops.json
```

Stdout: `{applied, skipped, errors, validated, committed}`.
- `validated: true` → surface `✓ Model updated and validated.`
- `validated: false` → the engine already reverted; surface `errors` verbatim: "Your correction would break the model — here's what's wrong: …. The example is saved either way; the model wasn't updated."

**For `new_metric`** (creating an entry): use the packaged `add` command — don't hand-write the YAML. It validates the item + the whole tree, writes `subject_areas/<area>/metrics/<slug>.yaml`, reverts on failure, and commits. Put the one metric in a JSON array:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind metric --area <area> --file /tmp/agami-new-metric.json
```
Same `{applied, skipped, errors, validated, committed}` contract as above: `validated: false` → it already reverted, surface `errors`; `skipped` → the metric was structurally invalid (e.g. missing `calculation`), surface the reason.

There is no override path — a model that fails validation is never persisted; `<artifacts_dir>/<profile>/` is left unchanged. The example library still got the correction (Phase 2 already happened).

For `org_context` / `user_preference` corrections (datasource.md / USER_MEMORY.md only): no validator step. Write the file directly with `chmod 600`.

### 4d — confirmation

```
✓ Correction appended to <artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml
✓ Model updated in <artifacts_dir>/<profile>/subject_areas/<area>/:
    - relationship orders_to_customers from_columns: [user_id] → [customer_id]
✓ Validator passed.

Next time someone asks "<question>" or anything similar, I'll use the corrected SQL AND know the right join is on customer_id.
```

---

(Phase 6 — telemetry emission on correction — has been removed in the current 0.x line. The skill no longer reads `analytics_consent` and no longer appends to `.telemetry-queue.jsonl`. agami has no telemetry — see `docs/privacy.md`.)

---

## Edge cases

- **Empty examples file** — initialize it with the new entry as the only one.
- **`examples.yaml` missing** — invoke `agami-connect` first to seed, then append.
- **User pastes SQL referencing tables not in the model** — EXPLAIN-validate catches it (`table_not_found`); surface the remediation, don't save.
- **User saves a duplicate of an existing seed** — replace the seed (`source: correction`, fresh `created_at`).
- **Most-recent query is itself a correction** — that's fine, attach to it.
- **Validator fails on a model edit but the user really wants it saved** — they can hand-edit `<artifacts_dir>/<profile>/<schema>.yaml` directly and the next `query-database` will (try to) read it. The validator runs again from `connect verify` if they want to confirm. There is no "skip validation" path from this skill.
- **User says "actually undo my last correction"** — they hand-edit the YAML / Markdown files; this skill doesn't track an undo log in v1.
- **Edit affects datasets in two different schemas** — split into two separate edits, one per target file. The validator runs once per write (or once for the merged directory).

---

## Hard rules

1. **Phase 2 (examples append) always runs.** Even if the user later changes their mind on the model edit, the example is already saved.
2. **Phase 5 model writes are gated by the validator.** `cli curate` (edits existing entries) and `cli add` (creates a new metric/entity) are the only ways to write inside `<artifacts_dir>/<profile>/`, and both refuse / revert on a validation failure. No exceptions. datasource.md and USER_MEMORY.md edits skip the validator (free-form Markdown).
3. **Edits stay valid against the model.** Don't invent fields — the Pydantic models (`packages/agami-core/src/semantic_model/models.py`) forbid unknown keys, so an invalid edit is rejected by the validator. When you can't express a correction within the model shape, fall back to `sql_fix` (example only) and tell the user "I can save this as a few-shot example but it doesn't fit a model edit."
4. **Show the diff before mutating the model.** The user always gets to see and approve the proposed change.
