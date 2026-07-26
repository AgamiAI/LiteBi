# `ORGANIZATION.md` — per-database human narrative

Free-form Markdown describing **what this database is about** in the human's own words: the company / product, what the data represents, who the users are. One file per profile at `<artifacts_dir>/<profile>/ORGANIZATION.md` — context is database-specific.

## Two levels: the company record vs the per-datasource narrative

A deployment can connect several databases under **one company**. Company-wide context is written **once** at the deployment level and shared by every datasource; each datasource keeps its **own** vocabulary. Two homes, joined at read time:

| Level | Where | Holds | Written |
|---|---|---|---|
| **Company** (the deployment) | `<artifacts_dir>/organization.yaml` (the `OrgRecord`) + `<artifacts_dir>/ORGANIZATION.md` (company narrative) | company `name`/`description`, `fiscal_year_start_month`, `display_conventions` (currency/rounding/week_start), the company-wide `glossary`, and an auto-maintained `datasources` list — plus the company narrative prose | `name`/`description` + narrative at first onboarding; `datasources` rebuilt automatically on each onboard/deploy; conventions/glossary edited via `/agami-model` |
| **Datasource** (each profile) | `<artifacts_dir>/<profile>/org.yaml` + `<artifacts_dir>/<profile>/ORGANIZATION.md` | that source's ontology (`key_terminology`, subject areas, …) and a **source-specific** narrative only | per profile |

`cli org-context` (local) and `get_datasource_schema` (served) both assemble these two levels: the **company block once**, then each datasource's per-database narrative + derived summary. A federated question spanning several datasources renders the company block **once** and both vocabularies. **With no company record, the output is just the per-database assembly** — no error, nothing to migrate.

### Content-routing rule — where each kind of context goes

- **Company-wide** (fiscal year, company glossary, a display convention true for the whole company, "who we are" prose) → the **company record** (`organization.yaml` + root `ORGANIZATION.md`).
- **Source-specific** (what THIS database means, a term that resolves differently here) → the **per-profile** files (`<artifacts_dir>/<profile>/org.yaml` `key_terminology` / `<artifacts_dir>/<profile>/ORGANIZATION.md`).
- **Per-column** units/encodings → the column's `field_metadata` in the structured model — **never** prose.
- **Personal / stylistic** (how *I* like results displayed) → `USER_MEMORY.md`.

The rest of this doc describes the per-datasource `ORGANIZATION.md`.

**It holds the human narrative ONLY.** The factual summary — subject areas, conventions, and the decoded domain **glossary** — is NOT stored here. That's derived from the structured model at read time and combined with the narrative when a reader needs the full context. The two homes stay separate so:

- a human editing their prose can never accidentally overwrite or delete the auto facts, and
- the glossary always reaches the LLM, even if the narrative file is empty or hand-mangled.

Local-only. Never sent in telemetry.

## The two parts of "domain context"

| Part | Where it lives | Who owns it |
|---|---|---|
| **Narrative** — what the company/product is, who the users are | `ORGANIZATION.md` (this file) | the human (free prose) |
| **Glossary** — term → meaning (e.g. `MRR` → "monthly recurring revenue") | `key_terminology` field on `org.yaml` (structured) | written by enrichment / `set-terminology`; surfaced read-only |
| **Summary** — shape, subject areas, conventions (units/currency) | derived from the model each time | computed, never stored |

`cli org-context "$ROOT"` assembles all three for a reader: the narrative (HTML comments stripped) + a `## Model summary (auto-generated from your schema)` block with the subject areas, conventions, and glossary. The model explorer shows the narrative as an **editable** field and the derived summary as a **read-only** field beneath it.

## Why it's separate from `USER_MEMORY.md`

| File | Scope | Examples |
|---|---|---|
| `<artifacts_dir>/USER_MEMORY.md` | **User preferences**, across every database | "default window: last 30 days", "show currency as EUR" |
| `<artifacts_dir>/<profile>/ORGANIZATION.md` | **Domain narrative** for this database | "We swap EV batteries at stations; a 'member' has an active plan." |

`ORGANIZATION.md` answers *what does this data mean*; `USER_MEMORY.md` answers *how should I display / filter results*.

## Format

Just Markdown. No required schema — free prose under `# About this database`. On the skip path the skill writes a tiny starter (a prompt comment, nothing else):

```markdown
# About this database

<!-- Describe what only you know: what the company/product is, who the users are,
     and what your key terms mean. agami already knows your schema — this is for the
     human context it can't infer. Leaving this as-is is fine; agami still works. -->
```

The user replaces the comment with real prose, or leaves it — agami works either way, because the summary + glossary come from the model regardless.

## Where each kind of fact goes

- **Narrative / "what we are"** → `ORGANIZATION.md` (this file).
- **A term's meaning** ("gold tier = lifetime spend > $10k", an acronym → its expansion) → the structured `key_terminology` field via `cli set-terminology`. It then renders into the derived summary automatically — no file edit needed.
- **A display/formatting convention** tied to a column (currency/unit) → the column's `caveat`/`value_transform` in the model. A genuinely cross-cutting presentation rule → `USER_MEMORY.md`.

## When the skill loads it

- **`query-database`** — Phase 1d.2 runs `cli org-context`, injecting the combined narrative + derived summary under `## Organization context`, ahead of `## User memory`.
- **`connect`** — enrichment uses the narrative (if any) as a domain prior for descriptions; writes the glossary to `key_terminology`; writes the starter on the skip path.

If there's no narrative and no model, the context is empty — never an error.

## Privacy

Local-only: never sent anywhere, read only by the agami skills running locally, `chmod 600` on creation. If the user shares `<artifacts_dir>/<profile>/` with a teammate, the file goes with the model — a deliberate user action.
