---
name: agami-model
description: "The single dashboard for the active profile's semantic model — browse, curate, AND sign off the trust layer in one surface. Browse every subject area, table, field, metric, entity, and join with live search; edit descriptions/metrics/entities/joins; exclude tables/columns you don't want queried; add new metrics; edit datasource.md. Its **Review tab** is the trust-layer sign-off queue: approve / reject the AI-proposed metrics (Rule 1 — a query using an unsigned metric still answers but carries a warning until it's approved), entities, and inferred joins (Rule 2 — lazy, usable while unreviewed). Every action is queued, submitted back to Claude as one feedback block, applied via the curation engine, and gated by the validator before it touches the YAML. (This skill absorbed the former `/agami-review`.)"
when_to_use: "Use for BOTH model curation and trust review. Curation: 'open the model explorer', 'show me the model', 'browse my tables', 'exclude a table', 'remove this column', 'I don't want PII columns', 'add a metric', 'edit datasource.md', '/agami-model'. Review / sign-off: 'open the review dashboard', 'review my model', 'what needs review', 'sign off the metrics', 'approve the metrics', 'walk the review queue', '/agami-review' (now folded in here) — or after agami-connect's Phase 4 sign-off gate or Phase 7 summary prompts to review or inspect the model. Also use when the user replies to a previously-rendered dashboard with a back-channel block (exclude tables: … / curate-ops: … / new-metrics: … / done)."
argument-hint: "[review | preseed | rule1] — open on the sign-off queue; no arg opens the explorer on Tables"
---

# agami model

You are running the unified **model + trust** surface — one dashboard to browse the live semantic model, curate it (exclude / edit / add), AND sign off the trust layer (approve / reject metrics, entities, joins). It replaces the separate model-explorer and review-dashboard surfaces.

This skill orchestrates:

1. **Render** — invoke `render_model_explorer.py` to walk every YAML and write a self-contained HTML artifact at `<artifacts_dir>/local/model/<profile>/<ts>.html`. The Python script does the YAML reading — **no LLM tokens spent on the walk**. The dashboard has tabs: **Datasource · Review · Subject areas · Tables · Metrics · Entities · Joins · Examples · Queued**. The **Review** tab is the sign-off queue (the old `/agami-review`); pass `--initial-tab review` to open on it.
2. **Open + wait** — auto-open the file, end the turn, wait for the user to come back with a "Generate feedback for Claude" block (exclude/include, approve/reject, edits, new metrics, org edit).
3. **Apply** — for each batch, run `semantic_model.cli curate` with an ops JSON. The engine flips review_state / stamps sign-off, runs the validator, reverts via git on failure, appends to `curation_log.jsonl`, and commits.
4. **Re-render** — render to a new timestamped file and re-open. Wait for the next batch.

Trust-spine semantics — three actions on the same `review_state` field:
- **Exclude / Reject** → `rejected`. The loader drops rejected tables, columns, metrics, entities, and relationships entirely (`include_rejected=False` at runtime) — never in prompts, never joined, never aggregated. ("Exclude" is the verb for tables/columns; "Reject" for metrics/entities/joins — same op.)
- **Include** → back to `unreviewed`.
- **Approve** → `approved` + a sign-off stamp (`signed_off_by`/`_at`/`_role`). **Rule 1** (metrics) — a query that uses an unsigned metric still answers, and the metric arrives on the answer's receipt carrying its `review_state`, which is what raises the report's approve/change banner until it's approved; **Rule 2** (entities, inferred joins) is usable while unreviewed and self-approves through use, with an unreviewed join raising the report's trust banner. Only `rejected` entries are dropped from the runtime entirely. Approving requires the curator's email + role (Phase 0) — the validator rejects an approved entry with no sign-off stamp.

## Conversation style

- **Tight loops.** The dashboard is the surface; the chat is just the input channel.
- **Don't restate the dashboard in chat.** A successful apply gets a one-line ack with the count and the new file path.
- **Qualified names everywhere.** Tables are `<area>.<table>`. Columns are `<area>.<table>.<column>` (area = subject area). No bare names.

---

## Phase 0: Preflight

- **Plan-mode check** — this skill writes YAMLs. If plan mode is active, refuse: *"I can't apply model edits in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. (You can still inspect a previously-rendered dashboard at `<artifacts_dir>/local/model/<profile>/<ts>.html`.)"* **Do NOT write a plan file. Do NOT call `ExitPlanMode`.**
- **Resolve `<profile>` and `<artifacts_dir>`** via the standard chain (`AGAMI_PROFILE` → `<artifacts_dir>/local/.config.active_profile` → `default`; `AGAMI_ARTIFACTS_DIR` → `.config.artifacts_dir` → `~/agami-artifacts`).
  - **A pasted feedback block names its own target.** When applying a block from the dashboard's "Generate feedback for Claude", its first line is `profile: <name>` — that dashboard was rendered for THAT model, so **use it and override the active-profile default.** It prevents applying one dashboard's approvals to whatever happens to be the active profile (a *different* model). If `<artifacts_dir>/<profile>/datasource.yaml` exists for the named profile, target it directly — don't fall back to `active_profile` or hunt with `find`.
- **If `<artifacts_dir>/<profile>/datasource.yaml` doesn't exist**, invoke `agami-connect` and stop — there's no model to explore yet.
- **Verify Python + PyYAML are importable** (the renderer + applier both depend on PyYAML): `python3 -c 'import yaml'`. If not, surface the install hint and stop.

**Scope / initial tab.** Look at `$ARGUMENTS`:
- `review`, `preseed`, or `rule1` → the user wants the sign-off queue: render with `--initial-tab review` so the dashboard opens on the Review tab. (`preseed`/`rule1` come from `/agami-connect`'s Phase 4 gate — the Review tab already groups Rule 1 metrics under "Needs your eyes", so no separate scope filter is needed; the user signs those off there.)
- otherwise → no `--initial-tab` (opens on Tables).

**Resolve the curator's identity** (needed to stamp sign-off on Approve ops, and for `curation_log.jsonl` + git commits). The **primary path is the dashboard's footer** — the user types their email + picks a role there, and it rides back on the `signed-off-by:` feedback line (Phase 2). So you usually don't ask at all. Only if a batch arrives with approvals but **no** `signed-off-by:` line, fall back to `<artifacts_dir>/local/.config` (`reviewer_email`/`reviewer_role`); and only if those are absent too, ask once — both at once:
> To sign off entries I need your email and role. Reply like: `you@company.com / data_lead`
>
> Roles: `CFO`, `CTO`, `Data Lead`, `Engineer`, `Analyst`, or type your own.
>
> I'll save these to `<artifacts_dir>/local/.config` so I don't ask again.

Parse on `/`, trim, validate email against `\S+@\S+\.\S+`; accept any non-empty role string (≤ 40 chars). Re-prompt only on a bad email. **Persist** by merging `reviewer_email`/`reviewer_role` into `<artifacts_dir>/local/.config` (preserve existing keys), then `chmod 600 <artifacts_dir>/local/.config`. **Do NOT infer the email from any source** — not git config / env / credentials, **and not the Claude Code login / session email** (the host exposes it, but using it produces a silently-wrong audit trail). The sign-off identity must be typed by the user; don't even pre-fill the domain. Exclude/edit-only sessions don't need the role — only resolve it lazily when an Approve is in the batch.

---

## Phase 1: Render the explorer

```bash
ts=$(date -u +%Y%m%d-%H%M%S)
# Per-profile subdir so multi-profile users can tell renders apart.
mkdir -p <artifacts_dir>/local/model/"$profile"
out="<artifacts_dir>/local/model/$profile/$ts.html"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_model_explorer.py" \
  --profile "$profile" \
  --artifacts-dir "$artifacts_dir" \
  ${initial_tab:+--initial-tab "$initial_tab"} \
  --out "$out"
```

Set `initial_tab=review` when `$ARGUMENTS` was `review`/`preseed`/`rule1` (Phase 0) to force the sign-off queue. **Leave it empty for a plain `/agami-model`** — the renderer then uses its **smart `auto` default**: it opens on **Review** when anything needs sign-off (unreviewed metrics/entities/joins, or columns agami couldn't read), and on **Tables** when the model is all clean (so the user never lands on an empty Review tab). `$AGAMI_PLUGIN_ROOT` is the plugin dir under `~/.claude/plugins/cache/<marketplace>/agami/`. If the env var isn't set, fall back to the conventional install paths described in [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).

**Surface in chat (single block, no padding):**

```
Model dashboard rendered — <N> schema(s) · <M> tables · <K> fields · <R> to review.
<artifacts_dir>/local/model/<profile>/<ts>.html

Tabs: Datasource · Review · Subject areas · Tables · Metrics · Entities ·
Joins · Examples · Queued. Live search + status filters per tab.
• Review tab — the sign-off queue: Approve / Reject the metrics (must be
  signed off before queries use them), entities, and inferred joins, with a
  one-click "Approve all" for the confident ones.
• Tables/Metrics/… — browse + edit; Exclude tables/columns you don't want
  queried (a column offers "exclude all N named <col>" to drop it everywhere);
  add metrics; edit datasource.md.
Click through, hit "Generate feedback for Claude" at the bottom, paste back here.

You can also type commands directly:
  exclude tables:  <area>.<table>, <area>.<table>
  exclude columns: <area>.<table>.<column>, <area>.<table>.<column>
  curate-ops: [{"op":"approve","kind":"metric","area":"...","name":"...","at":"<UTC ISO>"}, ...]
  done
```

**Auto-open with the standard multi-command fallback chain:**

```bash
( command -v open    >/dev/null 2>&1 && open "$out" ) || \
( command -v xdg-open >/dev/null 2>&1 && xdg-open "$out" ) || \
( command -v start    >/dev/null 2>&1 && start "$out" ) || \
( command -v cmd      >/dev/null 2>&1 && cmd /c start "" "$out" ) || \
echo "agami: couldn't auto-open the explorer — open manually: $out"
```

End the turn. Wait for the user.

---

## Phase 2: Parse the chat back-channel

The user pastes the dashboard's "Generate feedback" block. **Don't hand-parse it** — pipe it to the parser, which handles the whole grammar (the `profile:` line, the `exclude/include tables|columns:` lists, and the `curate-ops` / `new-metrics` / `new-examples` / `example-edits` / `key-terminology` / `datasource-md` JSON blocks) and translates the exclude/include lists into curate ops:

```bash
parse_model_feedback.py --block-file <pasted>
```

It prints `{data, anomalies, needs_judgment}`:
- `data.profile` — the dashboard's target model. **Use it, overriding the active-profile default** (a block was rendered for THAT model; never apply it to whatever's active).
- `data.ops` — the merged curate ops array (exclude/include for tables/columns, translated, **plus** the `curate-ops` approve/reject/edit verbatim). Apply in one `sm curate "$ROOT" --ops-file <json>` call.
- `data.new_metrics_by_area` — `{area: [metric…]}` → one `sm add "$ROOT" --kind metric --area <area> --file <json>` per area.
- `data.examples_by_area` — edited + new NL→SQL examples `{area: [example…]}` → one `sm add-example "$ROOT" --area <area> --file <json>` per area (dedups by question).
- `data.key_terminology` — the complete glossary object → `sm set-terminology "$ROOT" --file <json> --replace`.
- `data.datasource_md` — the full datasource.md text (already decoded) → **Write** it to `<artifacts_dir>/<profile>/datasource.md` (overwrite; human narrative only).
- `data.signer` / `data.role` — the curator identity for `--signer`/`--role` on every approve. **Persist** them into `<artifacts_dir>/local/.config` (`reviewer_email`/`reviewer_role`; preserve other keys, `chmod 600`) so future sessions don't re-ask; if absent on an approve-bearing batch, fall back to `.config`, then the Phase 0 ask.

**If `needs_judgment` is set, stop and ask** — `malformed_targets` (a table without its `<area>.` prefix → *"Tables need a schema prefix — `sales.STG_LEADS`, not `STG_LEADS`. Did you mean that?"*) or `unparseable_json` (re-copy that block from the dashboard). Don't apply a partial block.

**Rule 1 guard (judgment):** before applying an `approve` on a metric, confirm its `calculation` is non-empty — for an introspected metric with an empty calculation, ask the user to fill it via an `edit` first (the dashboard always has one for user-authored metrics). Show the user a one-line summary of what each block changed before applying.

---

## Phase 3: Apply via the curation engine

`data.ops` from the parser is already the merged curation ops array — exclude/include translated + the `curate-ops` approve/reject/edit verbatim. Apply it in one `sm curate` call (shape, for reference):

```json
[
  {"op": "exclude", "kind": "table", "area": "sales", "name": "STG_LEADS"},
  {"op": "exclude", "kind": "table", "area": "sales", "name": "CUSTOMERS", "column": "EMAIL"},
  {"op": "approve", "kind": "metric", "area": "sales", "name": "total_revenue", "at": "2026-06-10T17:51:41Z"},
  {"op": "reject",  "kind": "entity", "area": "sales", "name": "region"},
  {"op": "approve", "kind": "relationship", "area": "sales", "name": "orders->customers", "at": "2026-06-10T17:51:41Z"}
]
```

(Column exclusions use `kind: table` + a `column` field — the engine flips the column inside that table's YAML.) **Write the ops array to the file with the Write tool — never a heredoc, a shell variable (`printf "$OPS"`), or `python3 -c` (JSON quotes/`null` break those).** Then apply the whole batch atomically — the engine flips `review_state` / stamps sign-off, **validates the model, commits, logs to `curation_log.jsonl`, and reverts everything on validation failure**:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" \
  --ops-file /tmp/agami-model-ops.json \
  --signer "${reviewer_email}" --role "${reviewer_role}"
```

`ROOT="<artifacts_dir>/<profile>"`. `--signer`/`--role` come from Phase 0 (resolve them before applying if the batch has any `approve` op — the validator rejects an approved entry with no sign-off stamp). Stdout is JSON: `{applied, skipped, errors, validated, committed}`.

**Success** (`validated: true`):
- Ack with the counts that apply, e.g. *"✓ Applied: approved 4, rejected 1, 2 columns excluded, 1 edit. Re-rendering…"*
- List any `skipped[]` (bad target / not found) on their own lines.
- Continue to Phase 4 (re-render).

**Failure** (`validated: false`):
- The engine already reverted via git. Surface: *"⚠ Validator rejected the batch — all changes reverted. No files were modified."* Show `errors` verbatim. Stop; don't re-render.

Excluded entries vanish from the runtime model (the loader drops `review_state: rejected`) but stay visible in the explorer (which loads with `include_rejected=True`) so the user can re-include them.

---

## Phase 4: Re-render

Every successful apply produces a new explorer file at a new timestamp.

**Delete the previous timestamped file from this profile's dir before writing the new one** (`rm -f "<artifacts_dir>/local/model/$profile/$prev_ts.html"`). Track `$prev_ts` across re-renders. The auto-open of the new file is the refresh signal; old files just accumulate and confuse the user about which tab is current.

```bash
new_ts=$(date -u +%Y%m%d-%H%M%S)
new_out="<artifacts_dir>/local/model/$profile/$new_ts.html"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_model_explorer.py" \
  --profile "$profile" --artifacts-dir "$artifacts_dir" --out "$new_out"
```

Auto-open the new path. Surface the new file path in the chat ack:

```
✓ Applied: 2 tables excluded, 2 columns excluded. Re-rendered.
<artifacts_dir>/local/model/<profile>/<new-ts>.html
(Previous tab is stale and can be closed.)
```

End the turn. The user comes back with the next batch.

---

## Phase 5: Closing

When the user types `done` AND no further commands are in the message, surface:

```
✓ Model exploration session ended. Final state:
  <X> tables excluded · <Y> columns excluded across the profile.

The runtime semantic model now skips these entries — they will not
appear in any prompt or join. To restore them later, run /agami-model
and queue `include tables: ...` / `include columns: ...`.
```

Then end the turn. The skill is one-shot per invocation — re-enter via the slash command or natural-language phrase to start another session.

---

## Edge cases

| Case | Behavior |
|---|---|
| User types `exclude tables: schema.table` for a table that's already excluded | The applier still runs (idempotent flip to rejected with the curator's name on the curation log). No-op for the YAML, but the audit trail records the re-confirm. |
| User types `include tables: schema.table` for a table that's already unreviewed | Same — idempotent flip; entry stays unreviewed; curator name recorded. |
| User types `exclude columns: schema.table.col` and the column doesn't exist | The applier returns it in `skipped[]` with reason "field not found on dataset". Surface the skip; continue with the rest. |
| User types `exclude tables: schema.table.col` (3 segments — looks like a column) | The grammar is positional; agami treats the `exclude tables:` prefix as authoritative. The applier will fail to find a table with that 3-segment name and return `skipped` with reason "table yaml not found". Surface clearly. |
| Validator fails on a partially-applied batch | The applier reverts ALL YAML changes via `git checkout -- .` before returning. `applied` counts are zeroed in the response (defense against partial-apply confusion). The user sees `validator_output` verbatim and can fix + retry. |
| User has the profile dir but no `.git/` (legacy from before Phase 3e) | The applier skips the git commit + revert steps silently. Curation log still gets appended. The user loses revert-on-validator-fail safety — surface a one-liner suggesting `git init` in the profile dir. |
| The model has 5000+ fields | The renderer handles it (JSON manifest is ~hundreds of KB at the high end). Client-side search is instant up to the millions of DOM nodes mark. If it gets sluggish, file an issue. |

---

## Sign-off without a browser (`sm approve-queue`)

This dashboard is HTML — it assumes a browser. On a **headless machine** (no browser, e.g. a remote/SSH-only host), sign off the whole pending queue from the CLI instead. `sm approve-queue` reads the same queue this dashboard's Review tab shows (Rule 1 metrics/named-filters + Rule 2 joins/entities), stamps each item, and applies it in one call:

```bash
ROOT="<artifacts_dir>/<profile>"
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" approve-queue "$ROOT" --signer you@example.com --role owner
# --kind metric|entity|relationship  → narrow to one type (repeatable)
# --dry-run                          → print the approve ops without applying
```

- **`--signer` and `--role` are required** — an approve must record *who* signed off (the validator rejects an approved entry with no sign-off stamp). The command self-stamps the `at` timestamp, so you don't build ops by hand.
- Afterward `sm review-queue "$ROOT"` shows `counts.total: 0` and `sm curate-gate "$ROOT"` drops `preseed_count` to 0.
- **PII does not block seeding.** `curate-gate` may still report `should_open_explorer: true` purely because sensitive columns remain queryable — that's **advisory** (your call to exclude or keep them), **not** a gate on seed generation. Only unreviewed **pre-seed** items (metrics/entities) block `seed-examples`; approving the queue clears that.

---

## What the runtime does with `rejected` entries

The trust spine has always read `agami.review_state`. The model loader in [`plugins/agami/skills/agami-query/SKILL.md → Phase 1c`](../agami-query/SKILL.md#1c--index-the-model-for-fast-access) filters entries with `review_state: rejected` out of `datasets_by_name`, `datasets_by_qname`, `fields_by_qname`, and `relationships_by_endpoints`. Rejected entries:

- **Never appear in the schema context** the SQL generator sees (Phase 2b).
- **Are not joinable** — the join-path picker skips relationships whose endpoints reference a rejected dataset.
- **Dropped from the runtime** — a *rejected* metric or named filter is excluded entirely; a query that would have referenced one gets a "metric not found" path, not an error. (An *unreviewed* metric is different — it's still used, and arrives on the receipt carrying `review_state: unreviewed`, which is what puts it in the report's approve/change banner.)
- **Stay in the YAML** for audit. The user can `include` them later without re-introspect.

Reject (on the Review tab or any metric/entity/join card) and Exclude (on tables/columns) are the same `rejected` operation — this dashboard is the one surface for both the bulk tables-as-units curation and the per-entry sign-off.

---

## Examples

### Excluding PII columns

```
You: open the model explorer

[skill renders <artifacts_dir>/local/model/main/20260512-101500.html and opens it]

You (in dashboard): search "ssn" → toggle Exclude on every SSN/EMAIL
                    field → click Generate feedback → paste back in chat:

exclude columns: sales.CUSTOMERS.SSN,
                 sales.CUSTOMERS.EMAIL,
                 sales.STG_LEADS.SSN
done

[skill applies, validator passes, commits, re-renders]

agami: ✓ Applied: 3 columns excluded. Re-rendered.
       <artifacts_dir>/local/model/main/20260512-101830.html
```

### Removing staging tables

```
You: I never want agami to use the staging tables

[skill checks datasource.md / table names for `_stg` suffix; if
 unsure, asks: "I see CUSTOMER_SCORE_STG — anything else? Click
 Exclude on the table cards in the dashboard."]

[user excludes via dashboard, pastes back]
exclude tables: sales.CUSTOMER_SCORE_STG
done

agami: ✓ Applied: 1 table excluded.
```
