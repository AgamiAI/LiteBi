# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

### Security

- **Closed a read-only-guard bypass via a welded quoted identifier.** A double-quoted
  identifier is self-delimiting in SQL on **both** ends, so `SELECT*FROM"pg_read_file"(…)`
  and `SELECT "x"INTO evil FROM t` are valid statements with no whitespace either side of
  the quote. The guard's lexer dropped the quote characters without re-supplying those
  boundaries, fusing neighbouring tokens into one (`FROM"pg_class"` → `FROMpg_class`,
  `"x"INTO` → `xINTO`) and destroying the word-boundary anchor every deny-list pattern
  matches on — so the gate stopped *seeing* the token rather than allowing it, and returned
  no rejection. Verified against PostgreSQL 16: the leading form reads a server-side file
  and the trailing form creates a table from `SELECT … INTO`, both while the guard passed
  them. Row locks (`FOR SHARE`) were reachable the same way. The lexer now re-supplies a
  separator on either side, when and only when the quote was actually separating two word
  characters — so a qualified name (`t."current_user"`) still neutralizes to one token,
  `t.current_user`, rather than being split. This restores an invariant the lexer already
  documented for comments and literals ("never empty"); the identifier branch was the one
  place not honouring it. Prior corpus cases all happened to carry a space before the
  quote, which is why this stayed invisible; the corpus now pins both weld directions and
  asserts the neutralized token structure directly, so neither a one-sided fix nor a
  blanket separator can pass.

## [0.5.0] — 2026-07-25

### Changed

- **Renamed the per-datasource model files** so their names say what they are, now that a
  company-wide `organization.yaml` exists at the artifacts root. In each profile,
  `org.yaml` → `datasource.yaml` and `ORGANIZATION.md` → `datasource.md`. The model root's
  display-name field `organization:` is likewise `datasource:`, and the served per-datasource
  memory row uses `kind='datasource'`. The company record (`organization.yaml`, `org_id`,
  tenancy) is unchanged. No on-disk migration is provided — re-run `agami-connect` (or rename
  the two files by hand) for any pre-existing profile.

## [0.4.5] — 2026-07-15

Hosted/self-hosted server hardening: a real-wheel packaging fix, a multi-tenancy seam, and an
append-only instructions hook. **The local plugin path is behaviour-preserving** — everything
resolves to a single `local` org and existing deploys are byte-identical by default.

### Fixed

- **Migrations and static assets now ship inside the wheel.** In a real (non-editable) `pip install`,
  `store.MIGRATIONS_DIR` resolved outside the package, so the server could boot on an **empty schema**
  with no error, and the missing `static/` dir made app construction fail. Migrations moved into the
  package (`packages/agami-core/src/migrations/core`), both are packaged as package-data, and `run_migrations`
  now **raises** on a missing core-migration root instead of silently applying nothing. (Editable
  checkouts and the `pip install -e` Docker deploy were unaffected — which is why CI stayed green.)

### Added

- **Multi-tenancy: `org_id` scoping across serving, runtime logs, and credentials.** One deployment
  can host many tenants whose datasources collide on name (e.g. `prod`). `org_id` (default `local`) is
  threaded through every serving/runtime read+write; a redeploy DELETE is org-scoped (one tenant's
  reseed can't wipe another's same-named datasource); per-tenant credential env vars
  (`<ORG>_DATASOURCE_URL[__<PROFILE>]`) with a **fail-closed** rule (a named tenant never falls back to
  the org-less DSN); and a resolver may raise `PermissionError` to refuse a caller (clean 403, not 500).
  A plain OSS/self-host deploy is unchanged — everything resolves to `local`.
- **Append-only `extra_instructions` seam on the HTTP composition root.** `build_server(...)` /
  `create_app(...)` accept `extra_instructions` so a consumer can add to the model-facing MCP
  instructions without forking core. **Append-only, never replace** — so a consumer can't silently drop
  a safety directive (e.g. the sensitive-column output rule); no-op and byte-identical by default.

## [0.4.4] — 2026-07-14

Onboarding fix for the examples-validation (NL→SQL) dashboard.

### Fixed

- **Examples-validation dashboard: Edit/Add-note no longer fires on every card.** Each example's
  interaction state was keyed on its display number `n`, which `sm seed-validate` assigns **per
  subject area** (1..k) — so a dashboard combining multiple areas carried duplicate `n`, and clicking
  **Edit** (or **Add note**) on one card opened every card that shared that number. It also made the
  "Generate feedback" block ambiguous (`edit N` could match more than one example). The renderer now
  assigns a **stable global `1..N`** in render order — the single numbering shared by the interaction
  key, the `#N` label, the feedback block, and the apply lookup — and normalizes the items file to
  match, so `edit N` resolves unambiguously.

## [0.4.3] — 2026-07-14

Documentation-only release. No behavior changes; the executor and skills from 0.4.2 are unchanged.

### Changed

- **`agami-core` PyPI page is now a readable landing page.** Reframed
  `packages/agami-core/README.md` (the PyPI `long_description`) to lead with the value proposition
  and clarify the plugin-vs-`pip` audiences, and trimmed the deep HTTP-server internals down to a
  summary plus links to `deploy/README.md` and `docs/`. Added `[project.urls]`
  (Homepage/Repository/Documentation/Issues) so PyPI shows sidebar navigation. Publishing this
  version is what refreshes the live PyPI page.

## [0.4.2] — 2026-07-14

Onboarding hardening for the public launch — fixes to the first-run `/agami-connect` path — plus a
documentation pass. No breaking changes; the executor internals from 0.4.1 are unchanged.

### Fixed

- **Seed validation no longer rejects every seed example.** The zero-row validation probe wrapped each
  seed as `SELECT * FROM (<sql>) WHERE 1=0`; its own `SELECT *` tripped the `SELECT *` ban and every seed
  was rejected regardless of its SQL. The probe now projects `SELECT 1` — it still parses and plans the
  inner query, but no longer trips the ban.
- **DuckDB readiness now requires `pytz`.** DuckDB needs `pytz` to materialize `TIMESTAMP WITH TIME ZONE`
  values; the driver probe only checked `import duckdb`, so an interpreter missing `pytz` scored as ready
  and then failed at query time on any `timestamptz` column. `pytz` is now part of the DuckDB probe.
- **Approve operations auto-stamp their timestamp.** An approve op without a `signed_off_at` recorded
  `null` and the validator rejected the whole batch. The timestamp is now stamped at the CLI boundary
  (where the clock is available), so sign-off batches apply cleanly.

### Added

- **Headless sign-off (`sm approve-queue`).** A no-browser path that reads the pending review queue
  (Rule 1 + Rule 2), builds a self-stamped approve op per item, and applies it (`--kind` to narrow,
  `--dry-run` to preview) — so onboarding can complete without opening the review dashboard.
- **The no-DB sample clears its own pre-seed gate.** The sample's silent build auto-approves its pre-seed
  queue as `signer=system` before seeding; real databases keep the human sign-off gate.

### Docs

- **Launch positioning.** The self-hosted team server (`/agami-deploy`) is labeled **Early access (in
  testing)** throughout; the free-vs-paid copy leads with the value the hosted cloud adds.
- **README.** A **Databases supported** section (all engines + how each executes), VS Code/Cursor install
  clarified as Manage-Plugins-UI (not the CLI slash-command form), and the sample-query copy made generic.
- **Guides.** Onboarding docs (`duckdb pytz` install, explicit render flags, the headless sign-off path),
  a plain-English trust-layer intro, an `/agami-serve` (Claude Desktop) usage section, and an accurate
  `migrations/core` README (the self-hosted server schema).

## [0.4.1] — 2026-07-12

The self-hosted HTTP server now runs SQL **in-process** by default — no per-query subprocess fork,
no CSV round-trip — behind a swappable execution seam. Plus a correctness fix for Postgres/Redshift.

### Added

- **Executor seam (`ports.Executor`).** `execute_sql` is split into a shared, un-bypassable *guarded
  envelope* (read-only guard → semantic-model safety → resolve datasource → execute) and a swappable
  **`Executor` port**. The built-in executor is the default and behaviour is unchanged; a consumer can
  inject a custom executor (e.g. pooled / per-user-RBAC) via `create_app(adapters=…)` **behind the same
  guard** — one execution implementation, never forked.

### Changed

- **The HTTP server executes queries in-process by default.** Previously every query forked
  `python -m execute_sql`; now the served path runs through the executor seam in-process — no fork, no
  CSV serialize/re-parse round-trip, native rows. The local stdio path and the `python -m execute_sql`
  CLI still fork (the throwaway-process isolation is kept for the single-user tool). Successful query
  results are identical to before.
- **The per-call row cap is request-scoped.** `--max-rows` now rides a `ContextVar` (was a module
  global), so concurrent in-process queries with different caps can't affect each other.

### Fixed

- **Postgres/Redshift queries returned 0 rows through the Python executor.** A psycopg2 server-side
  (named) cursor reports `description = None` until the first fetch, and the result collector read it
  *before* fetching — so it concluded "no result set" and returned empty. It now fetches first, then
  reads the description. SQLite/MySQL/etc. (client-side cursors) were unaffected. (Present since the
  server-side cursor was introduced for bounded transfer.)

### Performance

- **Tool handler runs off the event loop.** The heavy query handler is offloaded via `run_blocking`
  (completing the async-offload work), so one slow query no longer stalls the server's event loop.

## [0.4.0] — 2026-07-12

Runtime scalability & safety hardening: the server stays responsive and bounded as model size,
result size, and concurrent load grow. Behaviour-preserving unless a note says otherwise.

### Added

- **Bounded result sets.** A query now materialises at most a row cap instead of the whole result:
  `AGAMI_SQL_MAX_ROWS` (default 1000) is the deployment cap; a per-call cap is available via the
  executor's `--max-rows` (which can only lower it). Truncation is flagged (`result.truncated`) so a
  cut-off result is never presented as complete. The SQL is never rewritten (no injected `LIMIT`);
  Postgres uses a server-side cursor so the cap bounds transfer, not just what's written.
- **Multi-worker HTTP server.** The server can run with `--workers=N` (uvicorn import-string
  factory). MCP session state is already stateless (JWT + Postgres), so it scales horizontally.

### Changed

- **OAuth refresh-token storage is now configurable — default `overwrite`.**
  `AGAMI_REFRESH_TOKEN_MODE` selects `overwrite` (default: each refresh UPDATEs the session's single
  token row in place — one row per session, no growing heap of dead tokens) or `rotate` (the prior
  behaviour: insert-new + revoke-old, keeping OAuth 2.1 **stolen-token reuse detection**, plus a
  cleanup that prunes only already-expired revoked rows). **Upgrade note:** the new default
  `overwrite` trades away reuse detection — a replayed stolen refresh token simply fails to
  authenticate instead of revoking the whole family. A deployment that wants family-revocation must
  set `AGAMI_REFRESH_TOKEN_MODE=rotate`. Also: used/expired one-time authorization codes are now
  cleared at authorize, and the query/activity logs (`query_executions` / `tool_calls`) are
  explicitly **retained** — never deleted by any default path — with a new `idx_query_executions_ts`
  index to keep newest-first reads fast as history grows.
- **Hosted safety guard is now fail-closed and DB-backed.** On the hosted server the fan/chasm-trap,
  table/column-scope, SELECT-\* and PII guards resolve the semantic model from the database (not only
  the `/artifacts` disk mount) and **refuse** a query when no model can be resolved — instead of
  silently running it unguarded. The local single-player path is unchanged (a not-yet-built model is
  still fine, not an error).

### Performance (behaviour-preserving)

- **Per-process semantic-model cache + single SQL parse.** The model loads once per process and the
  SQL is parsed once per query, with the safety-guard indices built once and shared — down from a
  reload/re-parse per query on a long-lived server. Biggest latency win.
- **Blocking work runs off the event loop.** Password hashing (argon2), OIDC HTTP calls, and the
  per-call audit write no longer stall the async server — one slow login can't freeze all traffic.
- **Incremental model-authoring validation.** Curation/enrichment re-validates only the edited area,
  and snapshots read each file once, so authoring a large (many-area) model no longer grows
  super-linearly. Same verdicts and snapshots.
- **Faster schema discovery.** `get_datasource_schema` resolves tables via an O(1) index instead of
  re-scanning the model per table — byte-identical output, faster on wide models.

## [0.3.9] — 2026-07-10

### Added

- **Composition seams for downstream extension (no-op by default).**
  `mcp_http.create_app(extra_tools={}, adapters=None)` lets a downstream consumer add MCP tools and
  inject the `ports.py` adapters without forking or monkeypatching core, and `tools.register(...)`
  adds a tool to the shared registry with a duplicate-name guard. An existing deploy is unaffected —
  `create_app()` with no arguments behaves exactly as before, and `execute_sql`'s schema is unchanged.
  (#100)
- **Migration-overlay seam.** The store can layer additional migration roots on top of core's, so a
  downstream package can ship its own migrations alongside agami-core's (empty/duplicate namespaces
  and non-directory roots are rejected). No change for a default install. (#101)

## [0.3.8] — 2026-07-06

### Added

- **OAuth refresh tokens — no more hourly re-login on the self-hosted server.** The token
  endpoint now issues a `refresh_token` and supports the `refresh_token` grant (RFC 6749 §6),
  so a connected client (claude.ai) silently renews the short-lived access token instead of
  redoing the full login every hour. Refresh tokens **rotate** on each use with **reuse
  detection** (replaying a rotated/stolen token revokes the whole family), are stored **hashed,
  never in plaintext**, and are revocable. Access tokens stay short-lived (1h). Both lifetimes are
  now env-configurable (`AGAMI_ACCESS_TOKEN_TTL` / `AGAMI_REFRESH_TOKEN_TTL`, seconds) with the
  same defaults when unset (access 1h, refresh 30-day idle). No action needed on upgrade — the
  new `oauth_refresh_token` table migrates in automatically on boot.

## [0.3.7] — 2026-07-06

### Added

- **Read-only database user guidance.** `/agami-connect` and `/agami-deploy` now
  recommend connecting agami with a **read-only** database user — agami only ever
  runs read-only SELECT queries, so read access is all it needs. A new
  [readonly-grants.md](plugins/agami/shared/readonly-grants.md) ships copy-paste
  `CREATE USER` / `GRANT SELECT` SQL for every supported dialect (Postgres/Redshift,
  MySQL, Snowflake, SQL Server, Oracle, Databricks, Trino, BigQuery). Ask agami for
  "the read-only grant" to get the exact SQL for your database.

### Changed

- **Self-host compose caps container log growth.** Every service now uses the
  `json-file` driver with `max-size: 10m` / `max-file: 3` (≤30 MB per container), so
  a long-running deploy on a small VM can't silently fill the disk — no VM-side
  `daemon.json` step needed. Also silenced a harmless `CLOUDFLARE_TUNNEL_TOKEN … not
  set` warning on non-tunnel deploys.

### Fixed

- **`list_datasources` no longer reports empty on a self-hosted server.** On a
  served deployment the warehouse/model is reached through the store, and the local
  `credentials` file never ships to the container — but `list_datasources` was the
  one tool still reading only that file, so it always returned "No profiles found …
  run agami-connect", even while `get_datasource_schema` and `execute_sql` worked
  against the deployed model. Because clients are told to call it first, they'd
  conclude nothing was connected. It now enumerates the served models from the store
  (the same seam every other tool already uses), and only falls back to the
  credentials file for the local plugin.

### Security

- **Hardened the read-only `execute_sql` gate.** SQL execution now runs through a
  single guard (`sql_guard`) at the shared executor, so the stdio server, the hosted
  HTTP server, the skills, and cron are all protected identically (previously the
  check lived only on the MCP tool path; a direct `python -m execute_sql` call — used
  by the skills and cron — was unguarded). Beyond "must start with `SELECT`/`WITH`",
  it now rejects multi-statement SQL (including bypasses hidden in string literals,
  comments, or double-quoted identifiers), data-modifying CTEs, transaction-control /
  session-state / prepared statements, `SELECT ... INTO`, row-level locks, and
  dangerous server-side functions (`pg_read_file`, `lo_export`, `dblink`,
  `copy_program`, `pg_sleep`, advisory locks, `query_to_xml`, …). Legitimate analytics
  SQL is unaffected — a large false-positive corpus pins that. Enforcement is not
  bypassable via `--no-safety` (that flag only skips the semantic-model pass).
- **Closed a dollar-quote statement-stacking bypass in that gate.** A `'` inside a
  Postgres/Snowflake/DuckDB `$$…$$` (or `$tag$…$tag$`) string desynced the literal
  stripper and could smuggle a second statement (`SELECT $$'$$ ; DROP TABLE x -- '`)
  past the multi-statement check. The gate now neutralizes comments and string /
  dollar literals in a single lexer-faithful pass (first-opened construct wins),
  refuses dialect-ambiguous MySQL comment forms (a bare `--x` and executable
  `/*! … */` comments), and also blocks sequence writes (`setval`/`nextval`) and
  server/replication control
  (`pg_stat_reset*`, `pg_switch_wal`, `pg_drop_replication_slot`, …). The guard module
  is also now packaged in the built wheel (it was missing from `py-modules`, which
  would have broken `import sql_guard` in an installed/containerized deploy).

## [0.3.6] — 2026-07-04

### Changed

- **`/agami-deploy` is easier to find and safe to re-run.** The config file is now
  a **visible `agami.env`** (not a hidden `.env`), and the skill opens it for you.
  A **re-run upgrades in place, non-destructively**: your typed password/secret and
  DSN are kept, any setting new in a version is surfaced (e.g. `DATASOURCE_URL`),
  and the image tag bumps only when you pass one — so a model update is just
  re-run + restart, and a version upgrade tells you exactly what's new.
- **Multi-datasource deploys are an explicit choice.** With more than one model,
  the skill asks which to deploy (all or a subset) and names the per-datasource
  `DATASOURCE_URL__<NAME>` to set; dropping one on a re-run removes it cleanly.

## [0.3.5] — 2026-07-04

### Fixed

- **Self-host deploy no longer crash-loops on artifact permissions.** The team
  server runs as a non-root container user; the deploy now stages the model
  **world-readable**, so the boot-time model load can't fail `Permission denied`
  on `datasource.md` under a mismatched host owner.
- **claude.ai connects to a self-hosted server.** The `/mcp` endpoint no longer
  answers the bare (no-trailing-slash) URL with a `307` redirect that the MCP
  client won't follow — the server normalizes it internally, so `{base}/mcp`
  works on every deploy profile (including the Caddy-less Cloud Run one).

### Changed

- **Warehouse credentials come from the environment (`DATASOURCE_URL`), not a
  mounted file.** The executor resolves a connection DSN from
  `DATASOURCE_URL[__<datasource>]` env-first, falling back to the local
  `credentials` file — one code path, no fork. The self-host bundle now carries
  the DSN in `.env` and **ships no secret**: `local/` (credentials, `.pgpass`)
  is never staged, and a re-run purges any stale copy from an older bundle.

## [0.3.4] — 2026-07-03

### Fixed

- **Table-prune step of a real-DB onboarding no longer crashes on an installed
  build.** The `discover` pass (which renders the prune page where you pick which
  tables to model) failed with `ModuleNotFoundError` on a pip/marketplace install;
  it now resolves its renderer via the plugin root and works everywhere.

### Docs

- Refreshed for the current release: README slimmed (self-hosting moved to
  `docs/self-hosting.md`), the published PyPI install surfaced
  (`pip install "agami-core[model]"`), and the changelog backfilled.

## [0.3.3] — 2026-07-02

### Fixed

- **Marketplace-install reliability.** Credential promotion and the Claude Desktop
  setup (`/agami-serve`) no longer fail on a fresh marketplace install — they resolve
  the bundled library the same way every other script does, and install the model
  engine through the single `sm install` path.
- **Externally-managed Python + package shadowing.** The installer now works on an
  externally-managed interpreter (Homebrew / PEP 668) and can no longer be shadowed by
  a partially-installed package (the model CLI is verified from a neutral path).

### Changed

- **Sample "watch it build" opens the model explorer** when the build completes, and
  skips the prompts that don't apply to the curated sample (no table-prune / org /
  data-dictionary questions).
- **First-time setup no longer shows a placeholder profile name** — it reads as
  "first-time setup" until you name your profile.

## [0.3.2] — 2026-07-01

### Added

- **Published to PyPI.** `pip install "agami-core[model]"` (and `[server]`) installs
  the library from the index, and the plugin's model-build step uses it automatically.
  Published via GitHub trusted publishing (no stored token). The self-host server image
  is published to GHCR (`ghcr.io/agamiai/agami-core`) so a deploy pulls it — no clone,
  no build.

## [0.3.1] — 2026-07-01

### Fixed

- **Marketplace installs can query and build models with no dev checkout.** Bundled the
  stdlib query library into the plugin, so a marketplace install answers questions with
  no `pip install`; and the model-build step installs the engine from a source that
  exists in a marketplace layout (the published package, else git) instead of a
  dev-only path.

## [0.3.0] — 2026-06-24

### Added

- **No-database sample (`/agami-connect sample`).** Ships *Acme Store*, a small
  local SQLite dataset (commerce + subscriptions) with a ready-made, signed-off
  semantic model. Goes from install to a governed, receipted answer in under a
  minute — no connection, no credentials, nothing leaving the machine. The
  bootstrap (Phase 0s) offers a fast copy-the-model path and a "watch it build
  live" rebuild path. Builds deterministically via the `sqlite3` CLI or a pure
  Python-stdlib fallback (no install required).
- **Model snapshots / `model_version`.** A model write now stamps a content-hashed
  snapshot under `<profile>/.snapshots/<hash>/`, so every answer's receipt pins a
  real `model_version` (previously `null` for all profiles) and old answers stay
  reproducible. New `sm snapshot <root>` CLI.
- **Deterministic interaction spine.** The mechanical parts of the skills are now
  scripts that emit a uniform `{ok, data, anomalies, needs_judgment}` contract, so
  the agent only makes judgment calls on genuine ambiguity:
  `connect_resolve.py` (one call resolves profile / credentials / interpreter +
  next-phase decision — fixes choosing a Python that can't connect),
  `parse_prune_block.py` (fixes a shell word-split that mangled table lists),
  `parse_model_feedback.py` (the dashboard back-channel), `csv_to_sections.py`
  (charts/tables get their numbers from the result CSV, not the model), and the
  `sm receipt` / `sm curate-gate` subcommands.

### Changed

- **Renamed LiteBi → agami-core.** The install identity is now `agami-core@agami`
  (marketplace `agami`, plugin `agami-core`); the version bump is breaking, so
  existing `agami@litebi` installs must re-add the marketplace to upgrade
  (`/plugin marketplace add AgamiAI/agami-core` → `/plugin install agami-core@agami`).
- **Relicensed Apache-2.0 → fair-code (the Agami Functional Use License / FUL).**
  Internal/team use stays free; exposing the data or the MCP to people outside your
  organization now requires a commercial license. See [LICENSE](LICENSE) and
  [LICENSING.md](LICENSING.md).
- **Repositioned around the trust layer.** README, marketplace, and plugin
  metadata now lead with the governance/trust stance ("the trust layer between AI
  and your data") instead of natural-language querying. Dropped the "BI" framing.
- **Quickstart leads with the sample** — the fastest path to a first governed
  answer, with the real-database flow following it.

### Security

- **Engine-level PII enforcement.** Raw projection of a column marked `sensitive`
  is refused in the shared executor (`runtime.check_sensitive_projection`, wired
  into `execute_sql.py`), so the same rule protects the Claude Code skill **and**
  the local MCP server. Aggregates, filters, joins, and `GROUP BY` over sensitive
  columns are still allowed — only raw per-row output is blocked.

## [0.2.2] — baseline

First version tracked in this changelog. Earlier history lives in the git log.

- The local-first **trust layer**: confidence + review state on every join,
  metric, and entity; single-reviewer sign-off; per-answer receipts (SQL, tables,
  relationships, metric definitions, freshness); a review dashboard.
- Schema introspection into a provider-portable, git-native YAML semantic model.
- NL→SQL generation and **local execution** across Postgres, Supabase, Redshift,
  MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, and
  SQLite.
- Corrections with attribution, persisted to an `examples.yaml` few-shot library.
- An optional local **MCP server** (`agami serve`) for use from Claude Desktop and
  other clients — stdio, no auth, no network.
- Fan-trap / chasm-trap pre-flight that refuses to silently double-count.

[0.3.9]: https://github.com/AgamiAI/agami-core/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/AgamiAI/agami-core/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/AgamiAI/agami-core/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/AgamiAI/agami-core/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/AgamiAI/agami-core/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/AgamiAI/agami-core/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/AgamiAI/agami-core/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/AgamiAI/agami-core/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/AgamiAI/agami-core/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/AgamiAI/agami-core/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/AgamiAI/agami-core/releases/tag/v0.2.2
