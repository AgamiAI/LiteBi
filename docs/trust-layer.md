# The trust layer (in depth)

Most AI data agents quietly pick a join, quietly pick a definition of "revenue",
and quietly return a number. agami makes every one of those decisions **auditable**:
each one is either derived from something your database already guarantees, or
approved by a named person — and every answer carries a receipt showing which.

This page is the deep dive. For the summary, see the [README](../README.md#the-trust-layer).

## How it works, in three moves

1. **Every join, metric, and entity carries a trust block** — a confidence label and a
   review state (detailed below). Things the database vouches for (declared foreign keys,
   self-evident structural column names) auto-approve; anything the model *inferred*
   stays `unreviewed` until a human signs it off.
2. **You sign off what needs eyes, in one queue.** The Review tab lists the unreviewed
   entries. **Metrics** must be signed off before the runtime treats them as truth
   (Rule 1); **joins and entities** are usable-but-flagged until confirmed (Rule 2).
3. **Every answer ships a receipt** — the exact SQL, the joins and metrics it used with
   their review state, and the model snapshot it pinned. If any unreviewed entry was
   used, the receipt says so, plainly. And where an analysis has not shipped yet, the
   receipt says *that* plainly too, rather than leaving the section empty and letting
   silence read as clean.

There is **no threshold to tune.** The trust layer is driven by review *state*
(`unreviewed` / `approved` / `rejected` / `stale`), not a numeric score you configure —
so "governed" means a person or the database vouched for it, not "the confidence was
above 0.8".

## Every entry carries a confidence + a review state

`agami-connect` writes each join, metric, and entity with a flat **trust block** —
a confidence label, a review state, and (once approved) a sign-off identity. No
vendor blobs, no numeric scores to tune:

```yaml
# a relationship in subject_areas/<area>/relationships.yaml
- from_table: orders
  to_table: customers
  from_column: customer_id
  to_column: id
  relationship: many_to_one
  confidence: confirmed        # confirmed | inferred | proposed
  review_state: approved       # unreviewed | approved | rejected | stale | not_applicable
  signed_off_by: null          # set when a human approves
  signed_off_at: null
  signed_off_role: null        # cfo | cto | data_lead | engineer | analyst | other
```

A **metric** carries the same block plus its definition — prose `calculation` +
per-dialect `bindings` — so an answer can show exactly what "revenue" means and
who vouched for it.

Auto-approve collapses the queue to what actually needs human eyes:
- A **DB-declared foreign key** → relationship `confidence: confirmed`,
  `review_state: approved` (the database already vouches for it).
- A **probe-inferred** join (name + value overlap, no declared FK) →
  `confidence: proposed` / `inferred`, `review_state: unreviewed`.
- A column with a **self-evident structural name** (`id`, `*_id`, `created_at`,
  `email`, `status`, `is_*`/`has_*` flags…) needs no description and is never
  queued.

Everything inferred stays `unreviewed` and surfaces in the Review tab.

## Rule 1 vs Rule 2 — and the hybrid review order

- **Rule 1 — metrics** (always queue): a metric must be signed off — a
  `signed_off_by` email AND a `signed_off_role` AND a non-empty `calculation` —
  before the runtime treats it as truth. Highest blast radius: one bad metric
  skews every report that uses it. The validator enforces all three before a
  metric can be `approved`.
- **Rule 2 — joins & entities** (lazy): usable while `unreviewed`; they
  self-approve as you query and surface on the answer's receipt until confirmed. No
  threshold to tune — it's review *state*, not a number.

At runtime, `agami-query` still **answers** questions that use `unreviewed`
metrics, joins, or entities — but every unreviewed entry it relied on arrives on
the receipt carrying its own `review_state`, and the report reads that state to
raise its banners: an unreviewed **join** raises the trust banner (with a pointer
to the Review tab), an unapproved **metric** raises the approve/change banner,
whose buttons write your decision straight back into the model. Nothing is
silently trusted; nothing is hard-blocked. Only `rejected` (excluded) entries are
dropped entirely — those never appear in an answer.

**Hybrid review order in `/agami-connect`**: Phase 4 surfaces a Rule 1 sign-off
gate *before* seed examples are generated (Phase 5). Reason: seed SQL exercises
metric definitions; signing them off first means the seeds inherit approved truth
instead of LLM guesses. Rule 2 polish (low-confidence joins / field descriptions)
stays in Phase 7's optional collapsed panel — it self-approves as the user queries
and never blocks the path to first answer.

## The review queue (a tab of the model dashboard)

`/agami-model review` (or "open the review dashboard") opens the model dashboard
on its **Review** tab — the trust-layer sign-off queue. The queue splits into
**Needs your eyes** (Rule 1 metrics, low-confidence or drifted entries) and
**Looks right (confident)** (FK-derived joins, clearly-defined entries) with a
one-click "Approve all". Each card shows:

- The inferred SQL fragment / definition / mapping
- Its confidence + review state
- An inline editable textarea for the description / `calculation`
- Per-card Approve / Reject / Edit buttons + group-level "Approve all"

Approving stamps the curator's email + role (resolved once and saved). Click
through the queue, hit "Generate feedback for Claude" at the bottom, paste back
into chat. agami applies each edit, runs the validator, commits the result to
`<artifacts_dir>/<profile>/.git/`, and re-renders.

## Every answer ships a receipt

The receipt is **five sections** — columns, tables, joins, aggregates, assumptions —
and every one of them is always present. Each carries what it established (`items`)
and, separately, a plain sentence saying what it did **not** (`undetermined`). Those
are two different facts, and keeping them apart is the whole point:

| `items` | `undetermined` | what it means |
| --- | --- | --- |
| set | null | established, here it is |
| empty | null | checked, and there was nothing to report |
| empty | set | **not checked**, and here is why |
| set | set | partly established, and here is what is missing |

Before this, an unchecked section and a clean one were both the empty list, so
silence read as clean. Today what a section could not establish says so where you
read the answer, and it says it in the caller's own numbers rather than naming
anything. A `COUNT(*)` whose source tables cannot be resolved is the live example:
the aggregates section reports that whether a join multiplies it is **not
established**, rather than leaving it off a list you would read as "no problem".
The same line goes null the moment there is genuinely nothing left to say, which
is what makes it worth reading.

Every `agami-query` answer includes a "Provenance for this answer" panel drawing
all of it:

- Tables touched — **one row per reference**, not per table, so a table read twice
  is listed twice; each with the name as written, the name the model resolved it
  to, and a row estimate. A reference the model does not declare (a CTE, say) says
  so instead of showing blanks
- Joins this query made — **one row per join the statement wrote**, each with the
  condition it joined on and whether your model declares a relationship for it. A
  join that matched one carries that relationship's cardinality, confidence +
  review state; one that matched none carries no sign-off trail, because there is
  no declaration for a signature to be about
- Metric definitions invoked, with author + sign-off date
- The columns the statement referenced
- Source-data freshness per table (when the DB exposes it)
- The assumptions agami made: the AI-written column meanings the answer leaned on
- Each section's "not established" sentence, where it has one
- Model snapshot hash (so the answer is reproducible from
  `<artifacts_dir>/<profile>/.snapshots/<hash>/`)

The statement itself is not in this panel: each report section carries its own
SQL, under that section's own disclosure, next to the numbers it produced.

Above the report, two banners: one if any join it used is unreviewed, one if any
metric it used is unapproved. The metric banner's Approve / Change buttons queue
your decision; your choice goes back to Claude to apply. Nothing lands in the
model until you send it.

## Examples validation

Phase 5 of `agami-connect` generates 10–12 NL→SQL seed examples that each satisfy
one of five **analytical shapes**: aggregation with a measure, segmentation, time
comparison, filtered top-N with context, or cohort / retention. Plain row-listing
is disqualified. Each seed is EXPLAIN-validated against the live DB, then surfaced
in an examples-validation dashboard
(`<artifacts_dir>/local/examples-validation/<ts>.html`) — same per-card pattern as
the review dashboard, with Validate / Reject / Edit / Add note buttons + an inline
"Add example" affordance.
