# Self-hosting: manual install & configuration reference

> 🧪 **Early access (in testing).** The self-hosted team server is usable today but newer than the local
> single-player path — see the [early-access note in deploy/README.md](../deploy/README.md) and please
> send feedback via a [GitHub issue](https://github.com/AgamiAI/agami-core/issues).

> **Deploying agami? Start with [deploy/README.md](../deploy/README.md).** The `/agami-deploy` skill
> writes a ready-to-run Docker bundle there (docker-compose + Caddy auto-TLS + a filled `.env`) from the
> published image `ghcr.io/agamiai/agami-core` — no clone, no build. **This page is the reference** for
> the pieces that bundle sets for you: the manual (non-Docker) `pip install` path, and the full list of
> environment variables — handy for a serverless platform or a hand-rolled setup.

agami's **HTTP MCP server** lets a team point their Claude at one shared, governed model. It's
**cloud-neutral** — a VM + Postgres, or a stateless platform (Cloud Run / Container Apps) + managed
Postgres. No GCP service is required to boot: no Cloud SQL connector, no Secret Manager, no Cloud
Logging (a regression test enforces this).

## Install

The server lives in the published `agami-core` package's `[server]` extra:

```bash
pip install "agami-core[server]"
```

## Run

Both steps read `AGAMI_DB_URL` (the store) — set it in the environment or inline as shown:

```bash
export AGAMI_DB_URL=postgresql://user:pass@host:5432/agami

# 1. migrate the store + load the model into Postgres (idempotent, fail-closed)
python -m model_deploy

# 2. serve (also re-migrates + seeds the admin on startup; both idempotent)
PUBLIC_BASE_URL=https://your-host python -m mcp_http
```

All serving state lives in Postgres — a fresh instance with only `AGAMI_DB_URL` serves identically,
so the server survives restarts and stateless platforms. The serving path is **LLM-free and
zero-egress by default**: the client is the brain, `execute_sql` runs SQL against your own database,
and the other tools just read the model. Nothing leaves your environment.

## Configuration (environment variables)

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGAMI_DB_URL` | yes (server) | The **store** (agami's own metadata DB — the server migrates + writes it on boot): `postgresql://…` in prod, `sqlite://…` for a small/local run. `APP_DATABASE_URL` is accepted as an alias for the cloud-platform convention (`AGAMI_DB_URL` wins if both are set). Unset ⇒ the local file path. This is **not** your datasource — keep its user **read-write**. |
| `DATASOURCE_URL` | to query (or file creds) | The **datasource** agami runs generated SQL against, as a DSN. The env-DSN channel accepts the `postgres` / `postgresql`, `redshift`, `mysql` / `mariadb`, `snowflake`, `bigquery` / `bq`, and `sqlite` schemes; engines without a DSN scheme (SQL Server, Oracle, Databricks, Trino, DuckDB) connect via the file `credentials` channel instead. Whichever channel you use, it **must point at a read-only (SELECT-only) role** — that role is the primary, non-bypassable safety boundary; see [read-only grants](../plugins/agami/shared/readonly-grants.md) for the per-engine recipe and the app-role vs operator-role split. A **different** database from the store above; never point it at owner/admin creds. Several datasources ⇒ one `DATASOURCE_URL__<TOKEN>` per datasource (token = the datasource id upper-cased, non-alphanumerics → `_`). |
| `PUBLIC_BASE_URL` | yes (server) | Backs OAuth/MCP discovery + the `WWW-Authenticate` resource URL. Set it explicitly — it can't be auto-detected behind a proxy/LB; the server fails fast at startup if it's missing. |
| `AGAMI_ORG_ID` | no | The single configured org id (default `local`). The server is single-tenant by default. |
| `AGAMI_SIGNING_SECRET` | yes (auth) | ≥32-byte secret the server signs its session JWTs with — this is what turns on **real per-user login** (admin password + per-user `/mcp` OAuth). **Required for any internet-facing deploy;** the `/agami-deploy` bundle generates and persists it for you. Unset ⇒ a local bearer-presence default only (single-user, not per-user). |

So on a real deploy (signing secret set — the bundle does this for you), admins sign in to `/admin` with a
**username and password**, and each team member authenticates individually to `/mcp`.

### Optional: single sign-on (Google / Microsoft)

Instead of a password, the admin can sign in with **one** Google or Microsoft account. Set
`AGAMI_ADMIN_PROVIDER=google` (or `microsoft`) and that provider's OIDC client — for Google,
`AGAMI_OIDC_GOOGLE_CLIENT_ID` + `AGAMI_OIDC_GOOGLE_CLIENT_SECRET`; for Microsoft,
`AGAMI_OIDC_MICROSOFT_CLIENT_ID` + `AGAMI_OIDC_MICROSOFT_CLIENT_SECRET` plus `AGAMI_OIDC_MICROSOFT_TENANT`
pinned to a single tenant id. It's off unless configured, and this basic
single-provider login is **free**. Per-org (multi-tenant) SSO, SAML, and SCIM are hosted **Enterprise SSO**
— see [what's free vs hosted](open-vs-hosted.md).

> **Serverless note:** on a managed-container platform (Cloud Run, ECS/Fargate, Container Apps) the server
> runs under a cloud identity — scope it to a dedicated least-privilege one, not the platform default. See
> [Runtime identity on serverless platforms](../deploy/README.md#runtime-identity-on-serverless-platforms).

The serving path is **LLM-free and zero-egress by default**: the client is the brain, `execute_sql` runs SQL
against your own database, and nothing leaves your environment. (The one exception is single sign-on, if you
turn it on — the server calls Google/Microsoft to verify a login.)
