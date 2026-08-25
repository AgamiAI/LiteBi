# Golden dataset YAML shape — the canonical reference

This is the **sanctioned source of truth** for the shape of a golden-dataset
YAML — a file of questions whose answers are already agreed, used to gate a run.
The data below is **synthetic** (the shipped sample `store` database: orders,
customers, channels) — it exists to show the *structure*, never to be copied as
content.

> **HARD RULE — never read another profile to learn a shape.**
> Do **not** glob or read other profiles' artifacts (e.g.
> `find <artifacts_dir> -path '*/golden_datasets/*.yaml'`, or reading
> `<artifacts_dir>/<some-other-profile>/...`) to "copy the file shape." That
> crosses the profile boundary, and it crosses it *harder* here than anywhere
> else: a golden dataset is the business definitions **and the answer key in one
> file**, so a glob returns another customer's questions together with the SQL
> that correctly answers them. In a hosted / multi-tenant deployment that is a
> **tenant-data leak**; even locally it lifts another profile's *business
> definitions* (filters, calculation text, the questions they care about).
>
> You don't need to. Every field is below, and the reader validates each file
> against the `GoldenDataset` / `GoldenItem` Pydantic models — it names what it
> refused instead of guessing. Build from this reference (and the profile's own
> schema) and let the reader validate it. **This file is the authority; a
> sibling profile never is.**

## The dataset file

A dataset lives at `<artifacts_dir>/<profile>/golden_datasets/<name>.yaml` — the
committable half of the profile, beside `prompt_examples/`, so a team reviews
its answer key the way it reviews its few-shot library. Only `*.yaml` is read; a
`*.yml` stray is reported rather than silently skipped.

**The filename stem is the dataset's name.** `orders.yaml` is the dataset
`orders`, so the YAML must **not** declare a `name:` key — a second place to
declare it would disagree with the first and nothing on disk would say which
won. A file that declares one is refused.

Dataset-level fields: `description` (optional prose), `category` (optional —
free text, a way to group datasets), `user_context` (optional — who is asking,
and what they assume), `test_cases` (optional; a file with no cases yet is a
legal in-progress shape, not a fault).

A complete file — one minimal case, then one exercising every optional field:

```yaml
description: Order-volume questions over the sample store database.
category: orders
user_context: An operations analyst who only ever counts orders that were paid for.
test_cases:
  - id: orders-count
    query: How many orders have been placed?
    expected:
      sql: SELECT COUNT(*) AS order_count FROM orders
      sql_confirmed: true

  - id: orders-paid-by-channel-2024
    query: How many paid orders came through each channel in 2024?
    expected:
      sql: >
        SELECT channel, COUNT(*) AS order_count
        FROM orders
        WHERE status = 'paid'
          AND placed_at >= '2024-01-01' AND placed_at < '2025-01-01'
        GROUP BY channel
        ORDER BY order_count DESC
      sql_confirmed: true
      tables_used: [orders]
      chart_type: bar
      data_shape: category_value
      validation_notes: Counted at order grain, so nothing joins in to fan the count out.
    match: values
    must_filter: [status]
    recorded:
      columns: [channel, order_count]
      rows:
        - [web, 812]
        - [mobile, 517]
      at: '2026-01-14T09:00:00Z'
    tags: [orders, smoke]
    confirmed_by:
      method: reviewed against the sample seed by hand
      at: '2026-01-14T09:00:00Z'
```

## Test cases

Each entry under `test_cases` is one question and its answer key. `id`
(**required** — the author's own, and the key every stored result hangs off, so
it must not be renamed casually; an id repeated within one file is reported and
that second case is dropped, the first kept — so copy-pasting a case means
changing its id too) and `query` (**required** — the question as a user would
ask it). `expected` is **required** and holds the answer key:
`sql_confirmed` (**required**, boolean), `sql` (optional), `tables_used`,
`chart_type`, `data_shape`, `validation_notes` (all optional).

`sql_confirmed: true` **requires an `expected.sql`** and is refused without one.
A confirmed case is the only kind that can fail a run, so it is the only kind
that has to be comparable against something; with no SQL it would pass forever
and nobody would notice. An unconfirmed case with no SQL is legal — that is the
in-progress shape, and it simply cannot gate.

Alongside `expected`, an item takes five optional fields:

- `match` — how closely the run has to match the answer key, loosening left to
  right: `exact`, `values`, `shape`, `bounded`, `nonempty`. **Defaults to
  `exact`**, so an item that says nothing is held to the strictest reading. Any
  other word is refused.
- `must_filter` — the **column names** a correct statement has to filter on
  (`must_filter: [status]` — not the predicate, just the column). A statement
  that does not filter on every column listed fails. This is how a case gates
  *how* the answer was reached, not just what it came to.
- `recorded` — `columns` (a list of names), `rows` (a list of rows, each row
  itself a list of values in `columns` order) and `at`: what the author saw on
  the day. A receipt for a reviewer, **never the comparison target** — a run is
  judged against `expected`.
- `tags` — free text. `smoke` is a **convention** for the fast subset, not a
  keyword the reader knows or treats specially.
- `confirmed_by` — `method` (required within the block, free text) and `at`:
  who or what vouched for the answer key, and when.

**An unknown field is refused, not ignored** — and what it costs depends on
where it is. A typo'd `must_filters` **on a case** does not quietly become a
case with no filters that gates nothing: the reader drops that one case, names
the field, and everything else in the file still reads. A bad key at the **top
level of the file** — a typo'd `descriptoin:`, or a `description:` left with no
value while an edit is half-finished — is refused at the dataset, so the whole
file is dropped and every valid case in it goes with it. Re-read after editing
the top of a file.

**A relative question over a frozen answer key is reported as a dataset error.**
A question like *"how many orders last quarter?"* names a window that slides
forward on its own; SQL pinned to `placed_at >= '2024-01-01'` does not. The two
agree today and drift apart on their own. Either anchor the SQL to the current
date (`CURRENT_DATE - INTERVAL '90 days'`, `NOW()`, the dialect's spelling) or
rewrite the question to name the window it means, as
`orders-paid-by-channel-2024` above does. The item is still read — the case is
broken, not the model, and a runner that scored it as a failure would blame the
wrong thing.

## How to write them

Write the file by hand, one dataset per question area, and read it back before
relying on it — the reader reports per-file and per-case, so one bad case costs
one case rather than the suite:

```bash
python -c "from semantic_model.golden import load_golden_datasets; \
  d, r = load_golden_datasets('<profile>'); print(r.findings or 'clean', len(d))"
```

Findings name the file and the case (`orders.yaml[orders-count]`), so fix what
is named and re-read. Keep the ids stable: results already stored against an id
detach the moment it is renamed.
