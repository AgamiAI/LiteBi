# migrations/core

SQL schema migrations for the **self-hosted agami server** — the control-plane database behind the
HTTP MCP server (`/agami-deploy`). This is **not** used by the local single-player path
(`/agami-connect`, `/agami-serve`), which is file-based YAML under `~/agami-artifacts/` and needs no
database.

> 🧪 **Early access — in testing.** The self-hosted team server is newer than the local path and still
> being hardened with early users, so this schema can change between releases. See the
> [early-access note in `deploy/README.md`](../../deploy/README.md).

## What's here

Ordered `NNN_*.sql` files, each one forward-only:

- **`001_serving.sql`** — the semantic model served from the DB instead of local YAML (org → datasource
  → subject-area → tables/metrics/entities/relationships).
- **`002_runtime.sql`** … **`015_query_executions_sql_truncated.sql`** — runtime tables: users,
  OAuth/OIDC identity, per-user access, the organization record, and the query-execution / tool-call
  audit log (including each execution's guardrail verdict — `ok` / `refused` / `failed`, plus the rule
  when we refused, and whether the recorded statement had to be cut to the audit bound).

The DDL is deliberately **portable** — `TEXT`/`INTEGER` only, app-minted keys (no `SERIAL`/`JSONB`) — so
the same files run on **SQLite** (tests and small self-hosts) and **Postgres** (production).

## How they're applied

The server applies these on startup, idempotently, in filename order — the runner records each applied
id in a `schema_migrations` table and skips ones already applied, so a reboot is a safe no-op. You don't
run them by hand; standing up the server (`docker compose up` from the `/agami-deploy` bundle) migrates
the database for you. The runner lives in
[`packages/agami-core/src/store.py`](../../packages/agami-core/src/store.py).

## Adding a migration

Add the next `NNN_<slug>.sql` (zero-padded, one higher than the last). Keep it forward-only and portable
(SQLite **and** Postgres). Never edit an already-shipped migration — a released file has run on real
databases; changing it makes the applied history diverge from the files.
