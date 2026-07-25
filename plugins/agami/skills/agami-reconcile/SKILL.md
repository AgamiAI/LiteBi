---
name: agami-reconcile
description: "Reconciles known (label, expected_value) numbers from an existing dashboard against agami's answers. Input can be a SCREENSHOT of a Metabase / Power BI / Tableau / Looker dashboard (Claude's vision extracts the pairs), a CSV, or numbers pasted inline — the user doesn't need to know which; they can just ask. For each pair, the skill generates a matching NL question, runs it through the active profile's semantic model, diffs actual vs expected, and surfaces matches in green and mismatches in red with drill-down receipts. The strongest onboarding demo for a skeptical data engineer — either we agree with their numbers (trust earned via evidence) or we surface a real definitional disagreement (trust earned via transparency)."
when_to_use: "Use when the user says 'reconcile against this dashboard', 'do these numbers match?', 'validate against my Tableau export', '/agami-reconcile <csv>', drops a screenshot of a BI dashboard (Metabase/Power BI/Tableau/Looker/spreadsheet) and asks agami to reproduce the numbers, or pastes a CSV / table of known numbers. Requires agami-connect to have been run first (need a semantic model + examples library). A high-leverage validation surface for a skeptical data team — reproduce their dashboard numbers, or surface the definitional gap."
argument-hint: "<screenshot | path-to-csv | pasted numbers>"
---

# agami reconcile

You are running the reconciliation harness. Goal: take labeled numbers from an existing dashboard (Tableau / Looker / Mode / Metabase / Power BI / spreadsheet) — most often a **screenshot**, sometimes a CSV or pasted list — and prove agami can reproduce each number. When numbers match, that's evidence the semantic model is right. When they don't, the receipt drill-down explains why — typically a definitional disagreement (gross vs net, refunds in vs out, FX rate at booking vs reporting date) — which is exactly the trust signal that makes a DE relax.

This skill orchestrates:

1. **Extract** the `(label, expected_value)` pairs from the input — a dashboard screenshot (via vision, confirmed with the user), a CSV, or a pasted list. Number parsing is always deterministic (`reconcile.py`).
2. **Generate a matching NL question** for each label.
3. **Run** each question through the same NL→SQL→execute pipeline as agami-query.
4. **Diff** actual vs expected with a tolerance.
5. **Present** a markdown table with per-row status; for mismatches, render the full receipt as a drill-down so the user can find the definitional disagreement.

Spec for the deterministic helpers: [`scripts/reconcile.py`](../../scripts/reconcile.py) (CSV parser + number normalization + diff with tolerance).

## Conversation style

- **Tight loops.** This skill is a tool, not a tutorial. One question per turn, max two sentences of prose between phases.
- **Surface mismatches loud.** A reconcile run with 9/12 matches and 3 mismatches is a SUCCESSFUL run — the mismatches are the value. Lead with what didn't match.
- **Don't paste raw SQL in chat.** The receipt has it. Same hard rule as agami-query.

---

## Phase 0: Preflight

Same checks as agami-query / agami-connect:

1. **Plan-mode check** per [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill needs Bash + Read + Write — refuse if locked in plan mode. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't reconcile in plan mode — each row runs a live query and writes a receipt. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me with the CSV path."*
2. **Credentials present** — read `<artifacts_dir>/local/credentials` for the active profile. If missing, invoke `/agami-connect` to set up first; this skill needs a working DB connection.
3. **Model present** — `<artifacts_dir>/<profile>/datasource.yaml` must exist. If not, invoke `/agami-connect`. This skill needs an introspected model to generate questions against.
4. **Input — accept any of three shapes; the user needn't know which.** Detect what they gave:
   - **A screenshot / image** of a dashboard (Metabase, Power BI, Tableau, Looker, a spreadsheet) — the common case. Go to Phase 1's **vision branch**.
   - **A CSV** — a path in `$ARGUMENTS`, or pasted inline (write inline CSV to `/tmp/agami-reconcile-<ts>.csv`). Go to Phase 1's **CSV branch**.
   - **Numbers pasted inline** as a list/table — treat as inline CSV.
   If they gave nothing (or just asked "can you check my dashboard?"), ask once, welcoming all three: *"Show me the numbers you want to check against — easiest is a **screenshot of your dashboard** (Metabase, Power BI, Tableau, a spreadsheet — whatever you have), but a CSV or a pasted list of `label: value` works too."* Don't make them figure out an export format.
5. **If they gave a file path, validate it exists.** If not, surface the error and stop.

---

## Phase 1: Extract the (label, value) pairs

Whatever the input shape, the goal is the same normalized rows JSON. **Number parsing is always deterministic — it goes through `reconcile.py`, never the LLM eyeballing a value** (a misread expected number would manufacture a false mismatch on a verification surface).

### Vision branch — a dashboard screenshot

1. **Read the image** and extract every labeled number you can see — KPI tiles, table cells, chart value labels — as `(label, raw_value)` pairs. Keep the label the user would recognize ("Total Revenue", "Active Users — Apr"), and the value **exactly as shown, verbatim** (`$4.2M`, `₹2.16Cr`, `42%`, `1,234`) — don't convert it; the normalizer does that.
2. **Write the pairs as a 2-column CSV** (Write tool — never a heredoc/`python3 -c`) to `/tmp/agami-reconcile-<ts>.csv`, then run the SAME normalizer as the CSV branch (below) so value parsing stays deterministic.
3. **Confirm before reconciling — vision can misread.** Show the extracted pairs as a small table and ask the user to fix any misread label/number: *"I read these N numbers off your screenshot — correct anything I got wrong, then I'll reconcile."* This confirm step is **mandatory**: a wrong expected-value isn't a model bug but it reads like one. If a tile is ambiguous or partly cut off, say so and skip it rather than guess.

### CSV branch — a CSV path or inline-pasted CSV

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" parse --csv "<csv_path>" \
  > /tmp/agami-reconcile-rows-<ts>.json
```

The helper (used by **both** branches) handles:
- Header detection (with-or-without first-row column names)
- 2-column or 3+ column inputs (3rd onward are appended to the label as context)
- Currency symbols / magnitude suffixes / accounting parens / percent (`$4.2M`, `₹2.16Cr`, `(123.45)`, `42%`)
- Null sentinels (`n/a`, `—`, blank)

Read the JSON. Each row is `{label, expected_value, raw_value}`. Discard rows where `expected_value` is null (unparseable) — surface a one-liner: *"Skipped 2 rows where the value couldn't be parsed: 'X', 'Y'."*

Surface to the user:
> Parsed `<N>` rows from `<the screenshot / csv_path>`. Reconciling now — typically `<N> × 5–15s` per row depending on query latency.

---

## Phase 2: Generate questions + execute

For each row in the parsed list:

### 2a — Generate the NL question

Use the LLM to translate `label` (+ context if present) into the most natural English question whose answer should be `expected_value`. Examples:

| label | question |
|---|---|
| `Q3 2025 Revenue` | "What was total revenue in Q3 2025?" |
| `Active customers (Apr 2026)` | "How many active customers did we have in April 2026?" |
| `Pipeline value (open opps)` | "What's the total pipeline value across open opportunities?" |
| `Mean order size last 30 days` | "What's the average order size over the last 30 days?" |

The semantic model + examples library are loaded; let the LLM pick the right subject areas / entities / metrics that resolve the labeled term to a concrete query.

### 2b — Run via the agami-query pipeline

Invoke the same SQL-generation + execution path agami-query uses (Phases 2 + 3 of that skill — see [`agami-query/SKILL.md`](../agami-query/SKILL.md)). Capture:

- The generated SQL
- The result (should be a single scalar, or a single row)
- The full chart-template HTML report (so the user can drill in for mismatches)
- The trust receipt (with confidence, signed-off-by, etc.)

If the SQL fails OR the result isn't a single scalar (e.g., the LLM-generated question returned a multi-row table), capture an error: `Could not extract a single scalar from the result.` These rows show up as `error` status in the report.

### 2c — Diff

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" diff \
  --expected "<expected_value>" \
  --actual "<actual_value_from_query>" \
  --tolerance 0.01
```

Default tolerance: ±1%. The user can override with `tolerance=N%` in their original ask (e.g., "reconcile with 5% tolerance"). Tolerance applies to numeric comparisons; for text values (rare), use exact match.

Capture: `match` (bool), `delta`, `delta_pct`.

### 2d — Build the row record

Per row:

```json
{
  "label":        "<from CSV>",
  "question":     "<LLM-generated NL question>",
  "expected":     <number>,
  "actual":       <number or null if errored>,
  "delta_pct":    <signed fraction or null>,
  "match":        true | false,
  "status":       "match" | "mismatch" | "error",
  "report_path":  "<artifacts_dir>/local/charts/<profile>/<ts>.html",  // the full chart report for this query
  "error":        "<message if status=error, else null>"
}
```

Append all records to `/tmp/agami-reconcile-results-<ts>.jsonl` so the user can inspect later.

---

## Phase 3: Present

### 3a — Summary line first

```
Reconciled <N> numbers: <M> match (within ±1%), <K> mismatch, <E> error.
```

### 3b — Mismatches table (lead with what didn't match)

Render the mismatches as a markdown table BEFORE the matches:

```markdown
### Mismatches

| Label | Expected | Got | Δ | Drill-down |
|---|---:|---:|---:|---|
| Q3 2025 Revenue          | $4,200,000 | $3,890,000 | -7.4% | <artifacts_dir>/local/charts/&lt;profile&gt;/...html |
| Active customers (Apr)   | 12,450     | 11,920     | -4.3% | <artifacts_dir>/local/charts/&lt;profile&gt;/...html |
```

Cell formatting:
- Numbers carry the same currency / magnitude suffix as the input where unambiguous (echo the user's `raw_value` for `Expected`, format `Got` with the same shape).
- `Δ` is the signed percent (red wins emphasized in chat by ✗ prefix if Markdown rendering allows; otherwise plain text).
- `Drill-down` links to the chart-template HTML for that query. Open these to see the full receipt — that's where the definitional disagreement lives.

For each mismatch row, surface a one-line interpretation under the table:

> **Q3 2025 Revenue** — agami reports $3.89M, your dashboard says $4.2M (-7.4%). Open the receipt; the metric `revenue` here is *gross of refunds in USD at invoice date*. If your dashboard nets refunds, that's the gap.

This is where the trust win lands. The DE doesn't have to chase the disagreement — the receipt + your interpretation does it for them.

### 3c — Errors block (if any)

```markdown
### Errors

| Label | What went wrong |
|---|---|
| Pipeline value (open opps) | Could not extract a single scalar — the question returned 47 rows. Try rephrasing or check that the metric exists in the model. |
```

### 3d — Matches summary (last, compact)

```markdown
### Matches (within ±1%)

7 numbers reproduced cleanly: Q3 2025 Orders, MoM growth, Avg order size, Top customer, Customer count by region, Refund rate, Pipeline count.
```

Don't dump every match's drill-down — they're not interesting. The matches build the case; the mismatches drive the conversation.

### 3e — Closing prompt

```
Re-run with `tolerance=5%` to see softer matches, or open any drill-down to find the definitional gap.
```

End the turn. The user typically:
- Opens a mismatch's drill-down, finds the definitional gap, says *"the dashboard is gross-of-refunds; can we update the metric?"* — chain into `/agami-save-correction` to update the metric definition.
- Asks `tolerance=5%` to widen the matches.
- Asks for a different CSV.

---

## Hard rules

1. **No automatic question generation for ambiguous labels.** If the label is too short or too vague (e.g., `Total`, `Number`, `Value`), surface to the user: *"Row 5's label is just 'Total' — too ambiguous to translate to a question. Skipping. Add more context to the CSV (e.g., `Total Revenue Q3` instead of `Total`) and re-run."* Don't guess.
2. **Receipt is non-optional.** Every per-row run MUST produce a chart-template HTML report with the trust receipt — that's what the drill-down link points at, and it's what makes mismatches actionable. If the underlying query path can't produce a receipt (legacy pre-trust-layer model), refuse with: *"This profile pre-dates the trust-layer launch. Re-run `/agami-connect` to enable receipts, then retry."*
3. **Don't write to the semantic model from this skill.** Reconcile reads + diffs; it never mutates. If a definitional disagreement surfaces and the user wants to update the metric, route them through `/agami-save-correction`.
4. **CSV stays local.** Don't upload, don't summarize-and-send. The reconcile run produces local artifacts (`/tmp/agami-reconcile-results-*.jsonl` + the per-query chart HTML) and nothing leaves the machine.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `<csv>` doesn't exist | Refuse with one line: "File not found: `<path>`." |
| CSV has 0 parseable rows | Refuse: "No rows with parseable numeric values. Common cause: the value column has formatting like `$1,234.56 (USD)` — try simplifying to `1234.56`." |
| Every row errors out | Surface a meta-error: "All <N> rows errored — likely a model-coverage problem (the questions don't map to your schema). Run `/agami-connect reintrospect` if your schema changed; check the model has the relevant tables." |
| Single mismatch but huge delta (> 100%) | Note in the interpretation: "The delta is large enough to suggest a unit mismatch (cents vs dollars, count vs percentage) rather than a definition gap. Check `agami.unit` on the relevant field." |
| User pastes inline CSV instead of a path | Accept it. Write to `/tmp/agami-reconcile-pasted-<ts>.csv` and proceed. |
| Screenshot is blurry / a value is cut off / can't read a tile | Don't guess the number. Extract what's legible, and tell the user which tiles you skipped: "Couldn't read 'Pipeline value' clearly — re-snip it or type that one in." |
| User says "reconcile my dashboard" but attaches nothing | Ask for the screenshot (or CSV / pasted numbers) per Phase 0.4 — don't proceed without the expected numbers. |

---

## Hard rule for screenshots

The screenshot is an **image of numbers**, and a misread expected value reads exactly like a model bug. So: (1) the value is parsed by `reconcile.py`, never by eyeballing; (2) the extracted `(label, value)` table is **always confirmed with the user before any query runs** (Phase 1 vision branch). The image stays local — same as the CSV (Hard rule #4); it's never uploaded or summarized off-machine.

---

## Roadmap (not in v1)

- **Tableau / Looker / Mode export parsing** — parse `.twb` / `.twbx` / JSON exports directly (today a screenshot of any of them already works via the vision branch).
- **Recurring reconcile runs** — wire into `agami test` so the golden-test suite includes reconciliation against a pinned dashboard.
