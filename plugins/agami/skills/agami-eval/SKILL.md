---
name: agami-eval
description: "Runs the golden evaluation harness for a profile. Picks one golden dataset, regenerates SQL for every question in it, executes each statement against the org's own warehouse, scores the result against the confirmed answer key, and reports the verdicts — failures first, with the errored, the unscored and the unconfirmed each kept separate. Scoring is deterministic and happens in agami-core; the skill reports it and never re-judges it. Neither the answer key nor the generated statement is printed: both stay in a local JSON artifact the report points at."
when_to_use: "Use when the user says 'run the evals', 'run the golden dataset', 'score the model against my golden questions', 'did anything regress?', 'how accurate is agami on my data?', or '/agami-eval <dataset>' — any ask for evidence that the semantic model still answers its agreed questions correctly. Requires agami-connect to have been run first (needs a semantic model + working credentials) and at least one dataset under `<artifacts_dir>/<profile>/golden_datasets/`. This skill reads and scores only; it never writes or edits a dataset."
argument-hint: "[dataset-name]"
---

# agami eval

You are running the golden evaluation harness. Goal: take a dataset of questions whose answers are already agreed, have the model answer each one again from today's semantic model, execute both statements, and report where the two disagree. A dataset is the profile's own regression suite — the run says whether the model still answers the questions this team already signed off on, and each disagreement is a concrete thing to fix (a drifted metric, a missing filter, a join that changed shape).

This skill orchestrates:

1. **List** the profile's datasets, before anything expensive runs.
2. **Choose** one — the only one, the one named, or the one the user picks.
3. **Run** it via `scripts/run_golden_eval.py`, which generates, executes and scores every case.
4. **Present** the verdicts in the order the script emitted them, failures first.
5. **Point** at the run's JSON artifact for the drill-down the terminal deliberately withholds.

Spec for the deterministic half: [`scripts/run_golden_eval.py`](../../scripts/run_golden_eval.py) (dataset choice + schema rendering + the printed payload). The scoring itself is agami-core's.

## Conversation style

- **A run with failures is a SUCCESSFUL run** — the failures are the whole value. A 9/12 run is the run that just told this team three things about their model. Lead with what did not pass; never soften it, never bury it under the passes.
- **Tight loops.** This skill is a tool, not a tutorial. One question per turn, at most two sentences of prose between phases.
- **Don't paste SQL in chat.** The script prints none, and neither do you. Same hard rule as agami-query.

---

## Phase 0: Preflight

1. **Plan-mode check** per [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill needs Bash + Read + Write — refuse if locked in plan mode. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't run an eval in plan mode — every case executes live SQL and the run writes a report. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me with the dataset name."*
2. **Credentials present** — read `<artifacts_dir>/local/credentials` for the active profile. If missing, invoke `/agami-connect` to set up first; this skill needs a working DB connection.
3. **Model present** — `<artifacts_dir>/<profile>/datasource.yaml` must exist. If not, invoke `/agami-connect`. This skill needs an introspected model: the run hands the generator the model's own tables and columns.
4. **See what exists** — run `--list` (Phase 1) before anything else. It reads no credentials and runs no query, so it is the cheap way to find out there is nothing to run.

---

## Phase 1: Choose the dataset

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/run_golden_eval.py" --profile <profile> --list
```

The JSON names each dataset with its `total`, `confirmed` and `unconfirmed` counts, plus `datasets_dir` — the directory a dataset would live in — and any `findings` the reader raised while reading them. Three outcomes:

- **No datasets.** Say so in one line, name the path (`datasets_dir` from the payload, i.e. `<artifacts_dir>/<profile>/golden_datasets/<name>.yaml`), and stop. Authoring one is not this skill's job — point at [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md), which is the authority on every field. **Carry its hard rule as you say so: never read another profile to learn the shape.** A golden dataset is the business definitions and the answer key in one file, so globbing for a sibling's returns another tenant's questions together with the SQL that answers them. The reference has every field; a sibling profile is never the reference.
- **Exactly one.** Use it. No question — a bare invocation needs no argument, and the script picks it too.
- **Several.** Ask with `AskUserQuestion`, one option per dataset, each labeled with its confirmed / unconfirmed counts (`orders — 14 confirmed, 2 unconfirmed`) so the choice is informed by what can actually gate. If the user already named one in `$ARGUMENTS`, skip the question.

**A dataset with zero confirmed items gets one line before you run it:** *"`<name>` has no confirmed cases, so this run reports scores but its verdict rests on nothing. See [`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md) for confirming an answer key."* Run it anyway if they want — a report with no gate is still a report.

If `findings` is non-empty, keep it: those are dataset-level breakages and they belong in Phase 3c.

---

## Phase 2: Run

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/run_golden_eval.py" \
  --profile <profile> --dataset <dataset> \
  > /tmp/agami-eval-<ts>.json
```

Then `Read` the file. The payload goes to a file rather than straight into the transcript because a run of forty cases is a large object and you need to look at parts of it more than once.

A run costs one model call plus one query per case, so tell the user what they are waiting for before it starts: *"Running `<dataset>` — `<N>` cases, roughly `<N> × 10–30s`."*

`--timeout-s` bounds a single generation (default 120). Raise it only if items are coming back with *"the generator did not answer within the time this run allows"*.

**The script emits `items` already ordered** — failures, errors, unscored, unconfirmed, passes — and `summary.sections` counts the rows in each. Render them in the order received and use those counts; do not re-sort, re-group or recount.

---

## Phase 3: Present

### 3a — Summary line first

```
Ran <dataset> on <profile>: <sections.failure> failed, <sections.error> errored, <sections.unscored> unscored, <sections.unconfirmed> unconfirmed, <sections.pass> passed — run completed: <yes | no>.
```

Each placeholder names its own key under `summary.sections`; `completed` is `summary.completed`. The keys are spelled out because the payload also carries top-level `summary.failed` and `summary.errored` and **they are different numbers** — an unconfirmed miss counts in `failed` and is rendered under 3e, so a run can report `failed: 2` beside one failure row. **A verdict is not "zero failures".** `gating_failures` counts items that were *scored*, so a run in which every generation errored has zero of them and is not green — and a run that stopped partway reports only the cases it reached. Read `completed`, `gating_failures` and `errored` together, and if `completed` is false say so on this line and again below: *"The run stopped partway — `<N>` cases were never attempted, so this is not a clean result."*

### 3b — Failures (lead with what did not pass)

```markdown
### Failures

| Item | What went wrong | Gate |
|---|---|---|
| orders-paid-by-channel-2024 | 3 of 5 rows matched the answer key (accuracy 0.60) | — |
| orders-refunded-count | Right rows, but the statement never filtered on `status` | must_filter: status |
```

`Item` is `item_key`. `What went wrong` is `reason`, with `accuracy` and the two row counts where they add something. `Gate` is the entry from `gates` that fired, or `—` when the item simply disagreed with its answer key.

Under the table, one line of interpretation per failure — the same job the receipt does in reconcile:

> **orders-refunded-count** — the rows are right and the filter is missing, so the model reached the answer another way. That is the kind of failure that passes today and breaks on next quarter's data.

### 3c — Errors and dataset findings

Two different things, and they must not share a table.

```markdown
### Errors — the run could not produce an answer

| Item | What went wrong |
|---|---|
| products-count | The generator's answer did not carry a statement this run could read |
```

```markdown
### Dataset findings — the file, not the answer

| Locator | Finding |
|---|---|
| orders.yaml[orders-last-quarter] | A relative question over a frozen answer key: the window slides, the SQL does not. |
```

**A finding is not a scored failure.** A rotted or unreadable case is *broken*, not *wrong* — nothing was judged, and counting it as a miss would blame the model for the dataset. The locator is `<stem>.yaml[<case id>]`, which is what the author edits.

### 3d — Unscored

```markdown
### Unscored — nothing could be compared

| Item | Why |
|---|---|
| orders-in-review | Both result sets came back empty, so there was nothing to compare |
```

**Not a pass.** An item is unscored when the comparator had nothing to work with — most often both sides returning no rows, typically because the question's window has outrun the data. The answer was never checked, so say that rather than letting a reader count it as agreement.

### 3e — Unconfirmed (kept visibly apart)

```markdown
### Unconfirmed — reported, but they cannot gate

| Item | Score |
|---|---|
| payments-count | did not match the answer key (accuracy 0.00) |
```

These ran and **they can never gate a run** — nobody has confirmed their answer key, so failing a run on one would be gating on an unreviewed answer. Keep them under their own heading, after the failures, so a reader scanning for what broke never picks one up.

Most of them were also scored, but not all: an unconfirmed item can come back `unscored` like any other (both sides empty, say), and then `accuracy` is `null` rather than a number. Read `status` before you render a score — write *"nothing could be compared"* rather than the `(accuracy 0.00)` the template shows.

### 3f — Passes, then the drill-down

```markdown
### Passed

12 cases reproduced their answer key: orders-count, customers-active, revenue-by-month, …
```

One compact line — the passes build the case, the failures drive the conversation. Then:

```
Full run (both statements per case, side by side): <artifact>
Re-run one dataset after a fix, or open the artifact to see what the model wrote.
```

`<artifact>` is `artifact` from the payload. That file is where the answer key and the generated statement live; it is the drill-down, and reading it aloud is not.

End the turn.

---

## Hard rules

1. **Never paste SQL in chat.** The script prints no statement, and the artifact is a path you hand over, not a source to quote — **do not read it out of the artifact** to show the user what the model wrote. They open the file.
2. **Never write or edit a dataset.** Not to fix a rotted case, not to confirm an answer key, not to add the question a failure suggests. This skill reads and scores. Authoring is a separate job with its own reference ([`shared/golden-dataset-shape.md`](../../shared/golden-dataset-shape.md)); route the user there.
3. **Never re-judge a verdict.** The score is deterministic and it came from agami-core. Report it, interpret it, and do not argue with it — no "this is arguably right", no re-reading rows to overturn a failure. If the score looks wrong, the case or the model is what changes, not the report.
4. **A run with failures is a completed run, not an error.** Report it as a result. Conversely a run of nothing but errors is **not** green: a verdict reads `completed`, `gating_failures` and `errored` together, never `gating_failures` alone.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `--list` returns no datasets | One line: "This profile has no golden datasets yet — the first one goes in `<datasets_dir>/<name>.yaml`." Point at `shared/golden-dataset-shape.md` and stop. Don't write one. |
| The dataset has 0 confirmed items | Say the verdict will rest on nothing (Phase 1), then run if they want. Every case reports; none can gate. |
| `agami-eval: cannot run this profile — …no storage connection…` | The preflight stop: the model declares no storage connection, so no dialect can be resolved and nothing can execute. Route to `/agami-connect` to finish the profile. Nothing ran; this is not a failing eval. |
| `agami-eval: cannot read the semantic model…` / `…does not parse…` | The model is missing or broken, so the generator has no tables to write against. Route to `/agami-connect` to build or rebuild it. Nothing ran; this is not a failing eval, and don't try to repair the YAML from here. |
| `agami-eval: the verdicts are below, but…could not be written` | The run finished and its JSON artifact did not land — usually a permissions problem on `<artifacts_dir>/local/eval/<profile>/`. Report the verdicts normally, and say there is no drill-down file for this run rather than pointing at an empty `artifact`. |
| Every item says *"the generator command could not be started on this machine"* | The generator is the `claude` client and it is not on this PATH (or not on the PATH of whatever shell ran the script). Nothing was scored — report it as a broken run, not a red one. |
| `completed: false` | The run stopped partway: the cases after the stop were never attempted and are absent from `items`. Say so on the summary line, and don't compare the counts to a previous run. |
| Credentials missing | The run fails at execution, not generation, so items come back scored-as-errors in bulk. Check `<artifacts_dir>/local/credentials` per Phase 0.2 and re-invoke `/agami-connect` if it's absent. |
