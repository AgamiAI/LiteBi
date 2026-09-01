---
name: agami-save-golden
description: "Writes golden-dataset items for a profile through two doors. The import door turns a question bank — a CSV, or a table pasted into chat — into items written unconfirmed, after the parsed rows have been shown and agreed to. The save door writes one question, the statement that answered it and the result the person accepted, as a confirmed item with its receipt. The curation door applies the changes queued on the golden-dataset explorer page, which may weaken a claim and may never grant one. Every write goes through agami-core's writer, is re-read by the runner's own reader before it is kept, and is append-only: a write that would change an item that already exists stops and shows the before and the after. This skill writes only; it never runs or scores a dataset."
when_to_use: "Use when the user says 'save this as a golden question', 'add this to the golden dataset', 'import my question bank', 'turn this spreadsheet into a golden dataset', 'this answer is correct — remember it as ground truth', 'show me the golden datasets', 'what does this dataset not test', 'apply my queued changes', or '/agami-save-golden <dataset>' — any ask to record a question, or a bank of questions, that the model should be scored against later. Requires agami-connect to have been run first (needs a profile with a semantic model). To RUN a dataset and see the verdicts, use `/agami-eval` instead: that skill reads and scores, this one writes and never runs or scores."
argument-hint: "[dataset-name]"
---

# agami save-golden

You are writing to the answer key. Goal: get a question — or a bank of them — into the profile's golden dataset in the state it has actually earned, and never in a better one. A dataset is what `/agami-eval` gates on, so an item that claims to be confirmed when nobody looked at a result turns the whole suite green on nothing.

**This skill has three doors and they write different things.** Everything below is organized around them, because confusing them is the one failure that matters:

| Door | Input | What it writes | Can it gate a run? |
|---|---|---|---|
| **Import** | A CSV of questions, or a table pasted into chat | Items with `sql_confirmed: false` | No |
| **Save** | One question, the statement that answered it, the result the person accepted | One item with `sql_confirmed: true`, its statement and its receipt | Yes |

An imported item **has no statement until somebody runs it and saves the answer through the save door.** That is the intended shape, not a gap to close: an import is a list of the questions this team cares about, and a question with no verified answer is an honest in-progress case that reports and cannot gate.

**This skill never generates SQL for an imported question.** Not to be helpful, not "as a starting point", not even marked as a draft. Filling one in would fabricate ground truth, which is the one thing an answer key may not contain — the answer key is what everything else is measured against, so a statement nobody verified corrupts every future verdict rather than just one item. If the user wants an imported question answered, run it (`/agami-query`), let them look at the result, and come back through the save door with what they accepted.

Spec for the deterministic half: [`scripts/golden_author.py`](../../scripts/golden_author.py) (column matching, id derivation, the append-only funnel and the write-then-re-read gate). The file shape is [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md)'s.

## Conversation style

- **One question per turn.** This is a tool, not a tutorial. At most two sentences of prose between phases.
- **Show before you write.** Nothing reaches disk that the user has not seen rendered first — parsed rows as a table, a replacement as a before and an after.
- **Never volunteer SQL for a question that has none.** See the rule above; it is the reason this skill exists in the shape it does.

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-save-golden needs Write (the pasted-table CSV, the item JSON) and Bash (the helper) — both are blocked in plan mode.

**If plan mode is active and the user picks `Stay in plan mode` (or this skill is invoked under an active plan-mode context):** refuse and end the turn. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text (verbatim):

> I can't save a golden item in plan mode — every door here writes to the model tree. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me with the dataset name.

If plan mode is not active, skip this phase silently and go to Phase 0.

---

## Phase 0: Preflight

1. **Plan-mode check** — Phase −1 above. Do it first; a refusal halfway through a parse is a confusing partial state.
2. **Model present** — `<artifacts_dir>/<profile>/datasource.yaml` must exist. If not, invoke `/agami-connect`. A dataset belongs to a profile, and a profile with no model has nothing to be scored against.
3. **See what exists** — `ls <artifacts_dir>/<profile>/golden_datasets/`. It writes nothing, and it is how you find out whether this is a new dataset or an existing one you are appending to. A missing directory is ordinary: the first write creates it.
4. **Which dataset** — the one named in `$ARGUMENTS`, the only one that exists, or ask with `AskUserQuestion` listing what is there plus "a new one". The name is the **filename stem** (`orders` → `orders.yaml`); the file never declares its own name and the writer never puts one in. A stem is **one plain name** — letters, digits, dots, dashes and underscores, nothing else. `--profile` is held to the same rule. Both are joined straight into the path the writer truncates and rewrites, so anything with a separator or a `..` in it is refused with exit `2` before a single byte is read; never "helpfully" pass a path where a name is asked for.

**Never glob for a dataset outside this profile.** [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md) is the authority on every field and it carries the hard rule verbatim: **never read another profile** to learn the file shape. A golden dataset is the business definitions and the answer key in one file, so a glob across `<artifacts_dir>` returns another customer's questions together with the SQL that correctly answers them — a tenant-data leak in a hosted deployment and a lift of somebody's business definitions even locally. The reference has every field; a sibling profile never is the reference.

---

## Phase 1: Which door

Route on what the user brought, and say which door you are opening:

- **A file path, a spreadsheet, a pasted table, "here are our questions"** → the import door (Phase 2).
- **A question plus a statement plus a result they just accepted** → the save door (Phase 3).
- **Both** ("import these, and the first one I've already checked") → import first, then the save door for the one they verified. The save door replaces the imported item by id, which is the append-only path in Phase 4 and is exactly right here — say so and confirm it rather than treating it as a surprise.

---

## Phase 2: The import door

### 2a — Get a CSV

The parser reads **one format**, and that is deliberate: one parser is one place where a column can be misread.

- **A `.csv` path** — use it as given.
- **A `.xlsx` / `.xls` path** — refuse and say how to get past it. Do not try to read it, and do not guess at its contents:

  > I can't read `.xlsx` directly. Open it in Excel (or Numbers / Google Sheets) and **Save As → CSV UTF-8**, then re-invoke me with the `.csv` path.

- **A table pasted into chat** — write it out as a CSV with the **Write tool** and then parse that file. One parser, one code path, and the file is also the thing the user can fix and re-run. Per [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md): **never a heredoc, never `python3 -c`, never a shell variable** — quoting mangles the commas and quotes that are the whole point of a CSV. Write it to `/tmp/agami-golden-pasted-<ts>.csv` and tell the user where it went.

The sheet needs a header row with a **question column** — `question`, `query`, `nl question`, `prompt` or `ask` (case, underscores and hyphens all fold). Optional columns: `id`, `expected` / `expected value` / `answer`, `sql` / `statement`, `tags`. A header the contract does not know is left alone — matching is exact, never fuzzy, so an analyst's note column costs nothing.

### 2b — Parse (this writes nothing)

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" parse \
  --csv <path-to-csv> \
  > /tmp/agami-golden-parse-<ts>.json
```

Then `Read` the file. `parse` is inert **by construction** — it has no write in it at all, which is what makes "the rows are confirmed before anything is written" a fact about the software rather than a promise about how you behave.

The payload carries `columns` (the header as found), `rows`, `skipped` (each with its row number in the user's own sheet and why) and `summary`. Exit `2` means no column could be identified as the question; the stderr line names every header it read, which is the whole of what the user needs to rename one and re-invoke.

### 2c — Render the rows and get an explicit yes

**Show the rows as a markdown table.** Not a count, not a summary — the rows:

```markdown
| id | Question | Expected | SQL in the sheet? | Tags |
|---|---|---|---|---|
| how-many-orders-have-been-placed | How many orders have been placed? | 1,329 | — | orders, smoke |
| revenue-by-channel-2024 | What was revenue by channel in 2024? | — | yes (unverified) | revenue |
```

Ids are derived from the question when the sheet has none, so they are stable across re-imports — say that, because it is why re-running the same sheet lands on the duplicate path instead of doubling the dataset.

If `skipped` is non-empty, list every skipped row with its number and reason **before** asking. A question missing from a dataset is one nobody notices is missing.

Derived ids are unique by construction, but a sheet with its **own `id` column** can repeat one. The import refuses the whole batch and names the id, because an id is the key a result is stored under and the second row would otherwise overwrite the first inside one write. The table you just rendered is where to spot it — ask the user which row keeps the id.

Then ask, plainly: *"Import these `<N>` questions into `<dataset>`? They'll be written unconfirmed — none of them can gate a run until someone verifies an answer."* **Wait for an explicit yes.** Never skip this and never infer it from the user having handed you the file.

### 2d — Import

Pass the **same parse payload** to `--rows` — the file you just showed them, not a re-derivation of it:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" import \
  --profile <profile> --dataset <dataset> \
  --rows /tmp/agami-golden-parse-<ts>.json \
  > /tmp/agami-golden-import-<ts>.json
```

`--description "<prose>"` sets the dataset's description, and is worth passing on a new dataset. Every row is written `sql_confirmed: false` — including the rows whose sheet carried a statement, which is carried through as `expected.sql` but claims nothing. Nobody ran it.

Read the exit code before the payload (Phase 4). On `0`, report `summary.added` and where the file is, then say what is still missing: *"`<N>` questions are in `<dataset>`. None can gate a run yet — ask one of them, and say 'save this as a golden question' when the answer is right."*

---

## Phase 3: The save door

This is the only door that can produce an item able to gate a run, and it needs three things the user has actually got:

1. **The question**, as a user would ask it.
2. **The statement** that answered it — the SQL that ran, not one you are writing now.
3. **The result they accepted**, and how they checked it.

If any of the three is missing, say which one and stop. Most often this skill is invoked straight after `/agami-query` returned a result the user is happy with, and all three are in the conversation already; take them from there rather than asking again.

Write the item with the **Write tool** as JSON — the same rule as any JSON file this plugin passes to a script, and for the same reason ([`shared/invocation-conventions.md`](../../shared/invocation-conventions.md)):

```json
{
  "query": "How many paid orders came through each channel in 2024?",
  "sql": "SELECT channel, COUNT(*) AS order_count FROM orders WHERE status = 'paid' AND placed_at >= '2024-01-01' AND placed_at < '2025-01-01' GROUP BY channel",
  "recorded": { "columns": ["channel", "order_count"], "rows": [["web", 812], ["mobile", 517]] },
  "confirmed_by": { "method": "read on screen and accepted by the analyst who asked" },
  "tags": ["orders"]
}
```

- `id` is optional — derived from the question when absent, the same way the import door derives it, so saving an answer to an imported question lands on that item rather than beside it.
- `recorded` is the **receipt**: what the answer looked like on the day. It is never the comparison target; a run is judged against `expected`. Take the columns and rows from the result the user just looked at.
- `confirmed_by.method` is free text and is **required** — an answer key whose provenance is blank cannot be audited later, and the script refuses without it. Say how it was checked ("read on screen and accepted", "cross-checked against the finance close"), not who in a way that names a person.
- `match` is optional and **defaults to `exact`, which is almost always the right answer**. Column pairing is by value at every level — neither a column's name nor its position is ever consulted — so "the names or the order might differ" is never a reason to loosen. `values` forgives exactly two things: a floating-point tail, and an extra column the question did not ask for. Reach for it when the answer carries a real float, and **not for a count or a result whose columns the question already names** — over-answering is the most common way a generated statement is wrong, and `values` is the level that forgives it. `bounded` takes a `bounds` block, for an answer that legitimately moves — see [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md). `bounded` with no band is refused with exit `2`: a level with nothing to consult would keep passing forever.
- `must_filter` gates *how* the answer was reached. Add it when the question only means what it says with a filter in place (`must_filter: [status]`).

`match`, `bounds` and `must_filter` all reach the written item exactly as sent — which is why a replacement has to repeat whatever the existing item carried (Phase 4).

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" save \
  --profile <profile> --dataset <dataset> \
  --item /tmp/agami-golden-item-<ts>.json \
  > /tmp/agami-golden-save-<ts>.json
```

**A relative question over a frozen answer key is refused here**, with exit `2`: *"how many orders in the last 7 days?"* names a window that slides forward and SQL pinned to a fixed date does not. Either anchor the statement to the current date (`CURRENT_DATE - INTERVAL '7 days'`, the dialect's spelling) or rewrite the question to name the window it means (`…in Q1 2024?`), then re-invoke. Do not save it and plan to fix it later — the whole point of refusing at save time is that the one person who can still fix it is here.

---

## Phase 3b: The curation door — the explorer page's queued actions

The explorer page (`/agami-eval`, or rendered directly) shows a profile's datasets and lets the reader queue changes against them. It writes nothing itself; it hands back one block. This is the door that applies it.

**Render the page:**

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_golden_datasets.py" \
  --profile <profile> \
  --out "<artifacts_dir>/local/eval/<profile>/datasets-<ts>.html"
```

`--out` must land under `local/`, the gitignored half — the page carries every confirmed answer key in full, and that is only safe where it is not committed. The script refuses any other destination.

**When the user pastes the block back**, parse it before you read it:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/parse_golden_feedback.py" \
  --block-file /tmp/agami-golden-block-<ts>.txt \
  > /tmp/agami-golden-ops-<ts>.json
```

**Read `needs_judgment` first.** If it is `confirmation_cannot_be_granted`, the block tried to set an answer key from the page. Nothing in it applies — not the offending op and not its neighbours. Tell the user what it names and stop; confirming an item means running it and accepting the result through the save door above, never editing a page. `anomalies` is the softer list: ops that were dropped and the rest carried on.

**Then apply, one dataset at a time.** The block's `profile:` line names its target — use that, not the active profile, or a page rendered for one model applies to another:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" apply \
  --profile <the block's own profile> --dataset <stem> \
  --ops /tmp/agami-golden-ops-<ts>.json \
  > /tmp/agami-golden-apply-<ts>.json
```

Exit codes are Phase 4's, unchanged. On `1` the payload carries `needs_confirmation` for the edits **and `needs_confirmation_removals` for the deletions** — the latter has a `before` and no `after`, because the whole of what somebody is agreeing to is what disappears. **Render the removals' questions and answer keys, not their ids.** An id is a slug; nobody can decide from it.

**What may be queued, and the one thing that may not.** Six actions: add or remove a tag, set `match`, edit the question, remove the item, and withdraw confirmation. **Nothing here grants confirmation.** The page has no control for it, the parser refuses a block that asks for it, and this door knows no verb that does — three layers, because a queued action that could confirm an item is the cheapest possible way to make a failing suite green.

---

## Phase 4: Exit codes, and the append-only stop

**Read the exit code before the payload.** Both write doors share it:

- **`0`** — written. Report `added` / `replaced` and the path.
- **`1`** — **needs confirmation.** Nothing was written. Not a failure and not a success; a pipeline that treated it as either would be wrong in both directions. **This is the only thing that produces `1`** — every other outcome, expected or not, is `2` with a sentence on stderr — so a `1` always carries a `needs_confirmation` payload and is never a crash. If you ever see `1` with no such payload, stop and report it rather than re-running with `--confirm-replace`.
- **`2`** — cannot start. The stderr line (prefix `agami-save-golden:`) says why. Nothing was written, or a failed write was rolled back to the bytes that were there before.

On `1`, the payload carries `needs_confirmation`: a list of `{id, before, after}`. **Render both sides** — a markdown table or two fenced blocks per id, the existing item and the one that would take its place. "This id already exists" is not enough for anyone to decide with: the thing being overwritten is an answer key, and they have to see what they would lose.

**A replacement is wholesale** — the item that is written is the item you sent, so anything the existing one carries and yours does not (its `tags`, a `must_filter`, a `match`) is gone. Read the `before` and carry forward what still applies before you ask; that is also what the user is agreeing to when they look at the two sides.

Then ask, and only after an explicit yes re-run the **same command with `--confirm-replace` appended**:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" save \
  --profile <profile> --dataset <dataset> \
  --item /tmp/agami-golden-item-<ts>.json --confirm-replace \
  > /tmp/agami-golden-save-<ts>.json
```

**Never pass `--confirm-replace` pre-emptively.** Not on the first attempt, not because a re-import "is probably the same sheet", not to save a round trip. The flag means one thing — a person saw the before and the after and said yes — and passing it before that has happened makes the stop decorative. If the user says no, say the file is untouched and stop; it is byte-identical, not merely equivalent.

---

## Hard rules

1. **Never generate SQL for an imported question.** No drafts, no "here's a starting point", no filling in a blank `expected.sql` to make a dataset look finished. Ground truth that nobody verified is worse than a gap, because a gap reports itself and a fabrication does not. Run the question and come back through the save door.
2. **Never write `sql_confirmed: true` for an answer nobody looked at.** The save door is the only route to it, and the only thing behind that door is a person who read a result and accepted it. A statement that "looks right" is not one.
3. **Never skip the confirmation step.** The parse is shown as a table and agreed to before the import runs; a replacement is shown as a before and an after and agreed to before `--confirm-replace` is passed. Both are the point of the two-step, not a formality in front of it.
4. **Never read another profile.** Not to learn the file shape, not to copy a case, not to see "how other people tag these". [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md) is the authority and it has every field; a glob across profiles returns another tenant's questions together with the SQL that answers them.
5. **Never run or score a dataset from here.** No `/agami-eval`, no executing the statement to "check it first". This skill writes. If the user wants a verdict, hand them off: *"Say 'run the evals' to score `<dataset>`."*
6. **Never edit a dataset YAML by hand.** Every write goes through `golden_author.py`, which re-reads the file with the runner's own reader and rolls back if it would not read clean. A hand edit skips that gate, and the fault surfaces at the next run against the file rather than at the moment it was made.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| Exit `0` | Written. Report `added` / `replaced` and the path. After an import, say plainly that nothing can gate yet. |
| Exit `1` with `needs_confirmation` | Nothing was written. Render the before and the after for every id, ask, and re-run with `--confirm-replace` only on an explicit yes. On a no, say the file is untouched and stop. |
| Exit `2` | Cannot start. Read the `agami-save-golden:` line on stderr — it names the cause. Nothing was written; a rolled-back write left the previous bytes exactly as they were. |
| `agami-save-golden: this file is empty` / `no column here holds the question` / `this file has no header row` | The sheet's question column can't be identified. The refusal lists every header it read — quote it back, ask which column holds the question, and have them rename it (or add a header row) and re-invoke. Never guess at column 0: a bank of ids imported as questions fails every future run in a way that looks exactly like a model regression. |
| `agami-save-golden: N row(s) were skipped` on a successful parse | A warning, not a stop. List every entry in `skipped` with its row number and reason before asking for the import — a question silently missing from a dataset is the failure this line exists to prevent. |
| A `.xlsx` / `.xls` path | Refuse with the Save As → CSV UTF-8 instruction (Phase 2a). Do not attempt to read it and do not reconstruct its contents from memory. |
| `agami-save-golden: this item does not say how its answer was confirmed` | `confirmed_by.method` was blank. Ask how the result was checked and re-write the item JSON — provenance is most of what a receipt is for. |
| `agami-save-golden: '<name>' is not a usable dataset name` / `profile name` | The stem or the profile was a path, not a name. Ask for the plain name (`orders`, not `orders/2024` or `../orders`) and re-invoke. Nothing was read and nothing was written. |
| `agami-save-golden: dataset '<name>' names the file rather than the dataset` | The extension was typed too. The stem *is* the dataset's name, so pass `orders`, not `orders.yaml`. Re-invoke; nothing was written. |
| `agami-save-golden: this batch carries the id '<id>' twice` | The sheet's own `id` column repeats a key, so two questions would land under one. Nothing was written. Show the user the two rows and ask which keeps the id. |
| `agami-save-golden: this does not fit a golden case — …` | The item JSON is a shape the dataset reader refuses — most often `match: bounded` with no `bounds` block, or a `sql: null` on a save. The sentence names the field and the reason (never the value). Fix the item JSON and re-run. |
| `agami-save-golden: <path> does not exist` / `this file is not readable JSON` | The `--csv` / `--rows` / `--item` path is wrong or the file you wrote is truncated. Re-write it with the Write tool and re-run; nothing was written. |
| `agami-save-golden: <name>.yaml cannot be read as it stands` | The existing dataset has a fault that costs it a case, so nothing may be merged into it — a merge into a file the reader can't fully read would drop whatever it couldn't parse. Report the finding, point at [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md), and let the user fix the named case first. (A dataset that merely *reports* a relative question over a frozen answer key is not this: that finding drops nothing, and writing to the dataset still works.) |
| A relative question refused at save time | The window slides and the SQL doesn't. Anchor the statement to the current date, or rewrite the question to name its window, then re-invoke. Don't save it "for now". |
| `golden_author's write doors need agami-core and its model extra` | The plugin's interpreter is missing `agami-core[model]`. Route to `/agami-connect`, which sets the environment up; nothing was written. |
