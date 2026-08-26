<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="plugins/agami/shared/agami-logo-light.svg">
    <img src="plugins/agami/shared/agami-logo-dark.svg" alt="Agami logo" width="240">
  </picture>
</p>

<p align="center"><strong>The trust layer between AI and your data. Local. Private. Yours.</strong></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-fair--code-blue.svg" alt="License: fair-code"></a>
  <a href="https://github.com/AgamiAI/agami-core/releases"><img src="https://img.shields.io/github/v/tag/AgamiAI/agami-core?label=version&sort=semver&color=blue" alt="Version"></a>
  <a href="#get-started-in-2-minutes"><img src="https://img.shields.io/badge/try%20the%20sample-no%20database%20needed-brightgreen" alt="Try the sample"></a>
</p>







**agami-core** is the trust layer between an AI agent and your database — a governed, self-improving semantic model that enforces correct joins, signed-off metric definitions, and a full audit receipt on every answer. Same question, same answer, every time. It builds itself from your live schema, messy or clean. The rules live in the model, not the prompt.

Real enterprise schemas have thousands of tables, mostly undocumented, with columns named `col_47` and three definitions of "revenue". That's the problem this solves.

## Get started in 2 minutes

In the **Claude Code CLI**:

```bash
/plugin marketplace add AgamiAI/agami-core
/plugin install agami-core@agami
/agami-connect sample          # zero setup: no database, no credentials
who are the top 5 customers by total spend?
```

> **In VS Code or Cursor**, the two `/plugin …` commands above don't work from the chat input —
> type `/plugin` to open the **Manage Plugins** dialog and add the marketplace + install the plugin
> there instead (see [Install](#install)). The `/agami-connect` line and everything after it work the
> same everywhere.

That last line returns a **governed answer with a receipt** (the exact SQL, the joins used, the model version) — and the governance runs *before* the query, so the kind of silent join mistake an ungoverned agent would hand back as a confident wrong number never reaches you. Two ways to go from here:

- 🧪 **No database?** The commands above answer from a built-in sample — nothing leaves your machine.
- 🗄️ **Have a database?** Run `/agami-connect` to introspect it into a governed semantic model, then just ask questions.

Each path is walked through in full below ([Quickstart](#quickstart-under-5-minutes) · [Install](#install)).


## What you get

- ✅ **Governed answers, not guesses** — every join is FK-derived or human-approved; every metric is signed off (name + role) before the runtime trusts it.
- 🧾 **A receipt on every answer** — the literal SQL, the tables touched, the relationships used, and the model version it pinned. Reproducible, auditable.
- 🔒 **Local and private** — runs inside Claude Code via Bash/Read/Write. Credentials, schema, and results never touch a server we operate.
- 🧩 **A portable semantic model** — plain, git-native YAML you own (subject areas, tables, entities, metrics, relationships). No lock-in.
- 🗄️ **Works with your database** — Postgres · Supabase · Redshift · MySQL · Snowflake · BigQuery · SQL Server · Oracle · Databricks · Trino · DuckDB · SQLite.
- 🛠️ **Zero infra to start** — no backend, no proxy. If you have a DB CLI you have everything; an optional local MCP server lets Claude Desktop use the same model.
- 👥 **Shareable with your team** *(early access — in testing)* — self-host [one governed server](#self-hosted-team-server--early-access-in-testing) that your whole team and business users query from their own Claude over a URL, still zero-egress. The team layer is newer than the local path; we're validating it with early users.

## Quickstart (under 5 minutes)

### Fastest path — the built-in sample (no database, no credentials)

agami ships with **Acme Store**, a small local SQLite dataset (commerce +
subscriptions) and a ready-made, signed-off semantic model. You get a governed
answer — with a full receipt — in under a minute, and nothing leaves your machine.

```bash
# 1. Install the plugin (see Install below for the per-host steps).

# 2. Try the sample — no connection needed:
/agami-connect sample      # or just say "I don't have a database" / "try the sample"

# 3. Ask a question — see governance ENFORCED:
who are the top 5 customers by total spend?

# 4. Now watch the model get BUILT — see governance MADE:
/agami-connect reintrospect
```

**Step 3 is where the governance fires.** The sample question is deliberately one
an ungoverned agent gets *confidently wrong* — a multi-table join where a naive
agent silently returns a bad number. agami's pre-flight catches it and returns the
correct answer *with a join receipt* — the trust layer, enforced on your very first
query.

**Step 4 is the real point.** Natural-language querying is everywhere; what's rare
is *where the trust comes from*. `reintrospect` re-derives the sample's model from
the live schema — introspect → infer + confidence-score every join → gate metric
sign-off → re-validate examples — so you watch the governed model take shape rather
than just consuming it. To see it built **from a blank slate** (full LLM
enrichment, descriptions and all), run `/agami-connect sample` and pick **"build
the model from scratch so I can see it work"** the first time. Either way it takes
a few minutes — it's the demo of *how* the receipts you saw in step 3 are earned.
Full flow: [docs/usage.md](docs/usage.md).

### Your own database

Connecting your database is a short back-and-forth with agami — you fill in one
template, and it handles the rest:

1. **Start setup.** Run `/agami-connect` (or just say *"connect to my database"*).
  agami asks which database you use, has you name the connection, and writes a
   credentials template for you to fill in.
2. **Fill in the template.** Open the file agami points you to, add your connection
  details, and save it — leave the filename as-is. agami only ever runs **read-only**
   queries, so a read-only database user is all it needs; ask agami for *"the read-only
   grant"* and it gives you the exact SQL to create one.
3. **Say *"introspect my database"*** (or just *"continue"*). agami takes it from there:
  it locks the file down for you (no manual `chmod`), reads your schema, builds the
   semantic model, and seeds a few validated example queries. You don't move or rename
   anything.
4. **Ask a question** — *"how many orders did we ship last month?"*

That's the whole setup. Along the way agami scores its confidence in every join and
entity, auto-approves the clear ones (real foreign keys, well-named columns), and asks
you to sign off the judgment calls. Full walkthrough: [docs/usage.md](docs/usage.md).
Connection details for each database: [docs/credentials.md](docs/credentials.md).

## Databases supported

agami runs your SQL **locally**, with whatever's already on your machine — a native database CLI, the
DuckDB universal binary, or a Python driver — picking the best option available. No agami-operated
server ever sees your data.


| Database                      | How agami runs the SQL                                  |
| ----------------------------- | ------------------------------------------------------- |
| **PostgreSQL** · **Supabase** | `psql` CLI · DuckDB · `psycopg2`                        |
| **Redshift**                  | `psql` · `psycopg2` (speaks the Postgres wire protocol) |
| **MySQL** / **MariaDB**       | `mysql` CLI · DuckDB · `pymysql`                        |
| **Snowflake**                 | `snowsql` CLI · `snowflake-connector-python`            |
| **BigQuery**                  | `bq` CLI · `google-cloud-bigquery`                      |
| **SQL Server** / Azure SQL    | `pymssql`                                               |
| **Oracle**                    | `python-oracledb` (thin mode — no client libs)          |
| **Databricks**                | `databricks-sql-connector`                              |
| **Trino** / Presto            | `trino`                                                 |
| **DuckDB**                    | `duckdb` binary or module                               |
| **SQLite**                    | `sqlite3` (Python stdlib — nothing to install)          |


agami picks the first method available for your database: a **native CLI** if one's on your `PATH`
(nothing to `pip install`), then the **DuckDB** binary where it applies (Postgres · MySQL · SQLite),
then the **Python driver**. Per-database connection fields and the read-only-grant SQL for every dialect
are in [docs/credentials.md](docs/credentials.md).

## Install

> **Platform note.** Validated on **macOS and Linux**. On **Windows** the skills
> need **[Git for Windows](https://git-scm.com/downloads/win)** (Claude Code uses
> Git Bash for its Bash tool); Windows is not yet validated end-to-end. The
> optional local MCP server is pure-stdlib Python and is cross-platform.

The same plugin works across Claude Code CLI, VS Code, and Cursor.

**Claude Code CLI** — in the Claude Code prompt:

```
/plugin marketplace add AgamiAI/agami-core
/plugin install agami-core@agami
```

Verify with `/plugin list` → you should see `agami-core@agami` installed.

**VS Code / Cursor** — the slash-command install form above is **CLI-only**; in the extension you
install through the UI. Install the **Claude Code** extension, type `/plugin` in the chat to open the
**Manage Plugins** dialog, paste `AgamiAI/agami-core` into the marketplace input and click **Add**, then
switch to the **Plugins** tab and click **Install** on `agami-core`.

Per-host walkthroughs: [docs/install/](docs/install/).

## The trust layer

Most AI data agents quietly pick a join, quietly pick a definition of "revenue",
and quietly return a number. agami makes every one of those decisions auditable:

- **Confidence + review state on every entry.** Each join, metric, and entity
carries a flat trust block (`confidence`, `review_state`, and a sign-off
identity once approved) — no vendor blobs, no scores to tune. DB-declared FKs
and structural column names auto-approve; everything inferred stays
`unreviewed` and surfaces in the Review tab.
- **Metrics must be signed off (Rule 1).** A metric needs an approver email, a
role, and a non-empty `calculation` before the runtime treats it as truth —
one bad metric skews every report that uses it. Joins & entities are lazy
(Rule 2): usable while unreviewed, flagged on the receipt until confirmed.
- **Every answer ships a receipt** — the literal SQL, tables + row counts,
relationships used (with confidence/state), metric definitions with author +
date, data freshness, and the model snapshot hash. A warning banner appears if
any unreviewed entry was used. Nothing is silently trusted.
- **Snapshot-pinned + git-native.** The model is YAML under
`~/agami-artifacts/<profile>/`, `git init`'d on first introspect. Every answer
records the model snapshot hash, so old answers reproduce exactly and schema
drift flips affected entries to `stale` instead of silently changing the number.

Full mechanics — the trust block, Rule 1/Rule 2, the review queue, the receipt,
examples validation: **[docs/trust-layer.md](docs/trust-layer.md)**.

## Skills (slash commands)

Natural-language phrasing routes to each skill automatically — "open the review
dashboard" / "save this as a correction" / "introspect my schema" all work without
typing the slash command.


| Command                                       | What it does                                                                                                                                                                                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/agami-connect`                              | One-stop setup + introspect: detect/collect credentials, introspect the live DB into the semantic model (tables, columns, PK grain, FK relationships, sensitive-column flags), layer LLM enrichment, generate EXPLAIN-validated seed examples. Validator-gated; `git init` + snapshots. Also `/agami-connect sample` for the no-DB sample.                                     |
| `/agami-query`                                | Answers a natural-language question: picks examples + relationships, generates and runs SQL, formats the result + chart, and surfaces a provenance receipt. Flags any unreviewed entry it relied on. (Usually you don't type this — plain language routes here.)                                                                                                               |
| `/agami-model`                                | One dashboard to **browse, curate, and sign off** the model: every table/field/metric/entity/join, per-table/column Exclude/Include, edits, new metrics. The **Review** tab is the trust-layer sign-off queue. Open it on the queue with `/agami-model review`.                                                                                                                |
| `/agami-save-correction`                      | Records a correction and routes it to the right home (SQL example, column metadata, display preference, business concept, or a new metric), showing its classification before writing. Attribution surfaces on future answers it influences.                                                                                                                                   |
| `/agami-reconcile`                            | Point it at an existing dashboard — a **screenshot** (Metabase / Power BI / Tableau / Looker) or a CSV of known numbers; it generates each question, runs it through agami, and shows a side-by-side diff with tolerances. Validate the model against numbers you already trust.                                                                                               |
| `/agami-eval`                                 | Run a **golden dataset** through the agent loop — every question regenerated, executed and scored against its confirmed answer key — and see, failures first, what stopped matching.                                                                                                                                                                                           |
| `/agami-serve`                                | Use agami from the **Claude Desktop** app: wires up the optional local MCP server (same tools as the hosted connector, backed by your local model + execution — stdio, read-only, no network). See [docs/mcp-server.md](docs/mcp-server.md).                                                                                                                                   |
| `/agami-deploy` *(early access — in testing)* | **Deploy a shared team server.** Writes a ready-to-run Docker bundle (the published image + HTTPS + OAuth + an admin console) so a team can stand up one governed server their Claude connects to. Business users query it over a URL — no local setup. Newer than the local path — we're validating it with early users ([details + how to give feedback](deploy/README.md)). |


## Why not just use Claude or a bare MCP server?

You can query a database from Claude Code or a bare MCP server today — one prompt, one shot. That works for exploration. It breaks for anything you need to trust.


|                         | Raw agent / bare MCP       | agami-core                                  |
| ----------------------- | -------------------------- | ------------------------------------------- |
| "Revenue" definition    | Re-derived every query     | Signed off by name + role, pinned           |
| Join correctness        | Guessed from schema hints  | FK-derived or human-approved, pre-flighted  |
| Fan trap / double-count | Silent wrong answer        | Caught before SQL runs                      |
| Audit trail             | None                       | Full receipt: SQL, model snapshot, approver |
| Schema drift            | Silent number change       | Stale flag on affected entries              |
| Sensitive columns       | Prompt-dependent           | Flagged in model, enforced at runtime       |
| Context persistence     | Re-explained every session | Built once, improves with corrections       |


If you're exploring a database you already understand, a bare MCP is fine. If you need repeatable, governed answers you can stake a decision on, that's what this is for.

## Privacy

agami runs entirely locally — credentials (`chmod 600`), the semantic model, charts,
exports, and dashboards all live under `~/agami-artifacts/`, and the skill never
reads files outside those paths (except your DB tool's auth config, set up on first
connect with your permission). Details: [docs/privacy.md](docs/privacy.md).

## Fair-code vs hosted

agami is **fair-code** (source-available). Everything here — introspection, the portable
semantic model, NL→SQL + local execution, the trust layer, corrections, and self-hosting for
your own organization — is **free** (serving people outside your organization needs a commercial
license). The hosted cloud adds the org-scale layer on top: advanced governance and access
controls (**RBAC**, audit, enterprise **SSO** with SAML/SCIM), a shared multi-tenant model
registry, and continuous evals. What's free vs paid, in full:
[docs/open-vs-hosted.md](docs/open-vs-hosted.md).

## Self-hosted team server — Early access (in testing)

> 🧪 **Early access.** This team layer is **usable today**, but it's newer than the local single-player
> path and we're still ironing it out with early users — expect the occasional rough edge. Please send
> feedback or report anything broken via a **[GitHub issue](https://github.com/AgamiAI/agami-core/issues)**.
> The local experience above is the stable, generally-available path.

The local setup is single-player. To put your **whole team — and non-technical business users — on
one governed model**, deploy agami's **HTTP MCP server** to your own host. Everyone then connects
their own Claude (claude.ai as a custom connector, or the desktop app) to **one HTTPS URL** and asks
questions in plain language. They install nothing, touch no credentials, and only ever see the model
you've governed. An admin console (`/admin`) controls who's allowed in; the warehouse stays behind a
read-only user, and **no data leaves your environment**.

The paved path is one command in Claude:

```bash
/agami-deploy      # writes a ready-to-run Docker bundle: published image + HTTPS (Caddy) + OAuth + admin
```

It gathers your hostname and admin identity, writes `docker-compose.yml` + a filled `agami.env`
(referencing the published `ghcr.io/agamiai/agami-core` image — no clone, no build), and either runs
`docker compose up` locally or prints the exact VM steps and the shareable `/mcp` URL.

**The full step-by-step — VM, DNS/TLS, and every variant (Cloudflare Tunnel, managed Postgres,
Cloud Run) — is in [deploy/README.md](deploy/README.md).** (Prefer to wire it up by hand instead of
using the bundle? The [manual install + environment-variable reference](docs/self-hosting.md) covers
that.) Admins sign in with a password; teammates get per-user access to `/mcp`.

It's cloud-neutral (a VM + Postgres, or a serverless platform + managed Postgres), configured entirely
by environment variables, and **LLM-free + zero-egress by default**. Self-hosting this for people
**inside your organization is free**; exposing data to people outside it is the paid line
([fair-code vs hosted](docs/open-vs-hosted.md)).

## Documentation

- [Quickstart & usage](docs/usage.md) — first-run walkthrough + common workflows
- [Credentials](docs/credentials.md) — every dialect, the connection-method picker
- [The trust layer](docs/trust-layer.md) — confidence, sign-off, receipts, snapshots
- [Format spec](docs/format-spec.md) — the semantic-model layout + a worked example
- [MCP server](docs/mcp-server.md) — use agami from Claude Desktop
- [Deploy a shared team server](deploy/README.md) *(early access — in testing)* — the Docker bundle, step-by-step (VM, DNS/TLS, variants)
- [Self-hosting reference](docs/self-hosting.md) — manual (non-Docker) install + the environment-variable reference
- [Troubleshooting & uninstall](docs/troubleshooting.md)
- [Fair-code vs hosted](docs/open-vs-hosted.md) · [Privacy](docs/privacy.md)

## Contributing

Issues + PRs welcome at
[github.com/AgamiAI/agami-core](https://github.com/AgamiAI/agami-core). See
[CONTRIBUTING.md](CONTRIBUTING.md) for test commands and the **version-bump
discipline** — every user-visible change needs a version bump in
`.claude-plugin/marketplace.json` (twice) and
`plugins/agami/.claude-plugin/plugin.json`, or existing installs stay on the cached
old version. Notable changes: [CHANGELOG.md](CHANGELOG.md).

Before pushing, run the checks locally with **`uv run dev.py check`** (ruff + the test
suite + gitleaks — the same gate CI runs on every PR). One-time setup and the full
command list are in [CONTRIBUTING.md](CONTRIBUTING.md); the coding + customer-safety
conventions are in [CLAUDE.md](CLAUDE.md). CI gates every PR regardless.

**If agami is useful to you, a ⭐ on the repo genuinely helps others find it.**

## License

**fair-code** (source-available) — the Agami Functional Use License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

What's free vs. paid (internal use vs. external exposure): [LICENSING.md](LICENSING.md).

Built by [Agami AI](https://agami.ai).