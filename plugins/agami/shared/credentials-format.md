# Credentials Format — `<artifacts_dir>/local/credentials`

`agami` reads database connection details from `<artifacts_dir>/local/credentials` (an INI-style file, `chmod 600`). Same pattern as `~/.aws/credentials`, `~/.dbt/profiles.yml`, `~/.pgpass`. The agami-connect Phase 0a writes `<artifacts_dir>/local/credentials.example` for you to fill in **in place** — when you come back, agami moves it to `<artifacts_dir>/local/credentials` and `chmod 600`s it for you (no manual save or chmod).

## Profile names

Each `[section]` is a named profile. **There's no magic `[default]` profile name** — you pick a name when `init` first runs (typical choices: `main`, `production`, `staging`, or anything specific to that database like `supabase` or the company name). The chosen name is written to `<artifacts_dir>/local/.config.active_profile` and used automatically for every subsequent skill invocation.

Switching between profiles in a single session: `AGAMI_PROFILE=staging` overrides the active profile for that one shell.

Resolution order when a skill needs to know which profile to use:

1. `AGAMI_PROFILE` env var (per-session override)
2. `active_profile` field in `<artifacts_dir>/local/.config` (set by `init`)
3. The literal string `"default"` (legacy fallback for users who set up before `active_profile` existed; users who clone the repo today don't hit this path)

## HARD RULES — for skills that read this doc

1. **The file is the only source of credentials.** Never accept host / port / database / user / password values typed into chat by the user, even "as a one-off". The user enters credentials by editing `<artifacts_dir>/local/credentials`.
2. **If the file is missing, invoke agami-connect Phase 0a.** Init writes `credentials.example`, sets `<artifacts_dir>/local/` permissions, and tells the user to fill it in. The user edits the template in place and comes back; agami promotes it (`mv` → `<artifacts_dir>/local/credentials`, `chmod 600`) — no manual save/chmod. Never ask "where's your database?" — that's what credentials are for.
3. **Connect ONLY to the host/port in the file.** Never substitute `localhost` as a fallback. Never probe for a "running database nearby" — if the credentials file says `host = remote-prod.example.com`, that's the only acceptable target.

## Use a read-only user (recommended)

agami only ever runs read-only SELECT queries, so the `user` you put here only needs read access. Connecting a **read-only database user** is the safest posture (especially against production) and never limits what agami can do. Copy-paste `CREATE USER` / `GRANT SELECT` SQL for every dialect is in [`readonly-grants.md`](readonly-grants.md).

## Format

```ini
# <artifacts_dir>/local/credentials
# Each [section] is a named profile. `init` asks you what to call your
# first profile (main / production / staging / a custom name). The active
# profile is recorded in <artifacts_dir>/local/.config.active_profile and used by every
# skill invocation. Override per session with AGAMI_PROFILE=<name>.

[main]
type     = postgres        # postgres | mysql | sqlite
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword

# Add additional profiles below — switch via AGAMI_PROFILE=staging
# [staging]
# type     = postgres
# host     = staging-db.example.com
# port     = 5432
# database = mydb
# user     = readonly
# password = ...
```

### MySQL example

```ini
[main]
type     = mysql
host     = 127.0.0.1
port     = 3306
database = analytics
user     = analyst
password = secret
```

### SQLite example

```ini
[main]
type = sqlite
path = /Users/me/data/local.db
```

### Paste a full DSN (`url = ...`)

If you have a connection string from your database provider — Supabase, Neon, RDS, Railway, etc. — paste it directly into a `url = ...` field and skip the per-field setup. The skill parses it into host / port / user / password / database internally.

```ini
[main]
url = postgresql://user:pass@host:5432/dbname
```

`url` accepts every variation listed below. `+driver` suffixes (used by SQLAlchemy / asyncpg / psycopg2) are stripped — you can paste your app's DSN as-is.

### Supabase

The pooler-mode DSN Supabase shows you (under Project Settings → Database → Connection pooling) usually looks like:

```
postgresql://postgres.<project_ref>:<password>@aws-1-<region>.pooler.supabase.com:5432/postgres
```

Drop it into the `url` field. Optionally add `sslmode = require` (Supabase requires SSL; the default `prefer` works too):

```ini
[main]
url     = postgresql://postgres.projectref:<your-password>@aws-1-us-east-1.pooler.supabase.com:5432/postgres
sslmode = require
```

If your DSN comes from an app that uses asyncpg or psycopg2 (e.g. `postgresql+asyncpg://...`), paste it as-is — the `+asyncpg` / `+psycopg2` / `+psycopg` driver suffix is recognized and stripped.

### Neon, Railway, Render, RDS, etc.

Same `url = …` shortcut. If the provider's URL has `?sslmode=require` (or other query params), they're parsed and merged into the connection settings — no extra fields needed.

```ini
[main]
url = postgresql://user:pass@ep-cool-darkness.us-east-2.aws.neon.tech/neondb?sslmode=require
```

### Redshift

Per-field form:

```ini
[main]
type     = redshift
host     = my-cluster.abc123.us-west-2.redshift.amazonaws.com
port     = 5439
database = analytics
user     = readonly
password = ...
sslmode  = require
```

Or DSN form (port 5439 and `sslmode=require` are auto-applied for `redshift://`):

```ini
[main]
url = redshift://readonly:pass@my-cluster.abc123.us-west-2.redshift.amazonaws.com:5439/analytics
```

Redshift Serverless uses a different host: `<workgroup>.<account>.<region>.redshift-serverless.amazonaws.com`.

### Snowflake

Per-field form (recommended — Snowflake has more required parameters than other DBs):

```ini
[main]
type      = snowflake
account   = xy12345.us-east-1.aws
user      = myuser
password  = mypassword
warehouse = COMPUTE_WH
database  = ANALYTICS
schema    = PUBLIC
role      = ANALYST_ROLE
```

DSN form (path is `/database/schema`; query params carry the rest):

```ini
[main]
url = snowflake://myuser:mypass@xy12345.us-east-1.aws/ANALYTICS/PUBLIC?warehouse=COMPUTE_WH&role=ANALYST_ROLE
```

For SSO, replace `password` with `authenticator = externalbrowser` (or your specific SAML provider value):

```ini
[main]
type          = snowflake
account       = xy12345.us-east-1.aws
user          = myuser@example.com
authenticator = externalbrowser
warehouse     = COMPUTE_WH
database      = ANALYTICS
```

Account identifier formats Snowflake accepts:

- `xy12345` — short locator (legacy AWS US-West-2)
- `xy12345.us-east-1` — locator + region (AWS)
- `xy12345.us-east-1.aws` — locator + region + cloud
- `myorg-myaccount` — newer org-account format (recommended by Snowflake)

The connector / snowsql appends `.snowflakecomputing.com` automatically — don't include it in the `account` field yourself.

## Required fields per `type`

| `type` | Required fields | Notes |
|---|---|---|
| `postgres` | `host`, `port`, `database`, `user`, `password` | |
| `redshift` | `host`, `port`, `database`, `user`, `password` | Same shape as Postgres. Default port 5439. SSL required (`sslmode = require` is the default). |
| `mysql` | `host`, `port`, `database`, `user`, `password` | |
| `snowflake` | `account`, `user`, `password` (or `authenticator`) | `host`/`port` not used. Optional: `warehouse`, `database`, `schema`, `role`. |
| `bigquery` | `project` | Auth: `service_account_path` (JSON key) or ADC. `host`/`port` not used. Optional: `dataset`, `location`. |
| `sqlite` | `path` | Absolute path to the `.db` file. |
| `duckdb` | `path` | Absolute path to the `.duckdb` file (or `:memory:`). |
| `sqlserver` | `host`, `user`, `password` | Default port 1433. Optional: `database`. Driver: `pymssql`. (`mssql` is an accepted alias.) |
| `oracle` | `user`, `password`, and either `dsn` or (`host` + `service_name`) | Default port 1521. Driver: `oracledb` (thin mode — no Oracle client libs needed). |
| `databricks` | `host`, `http_path`, `token` | SQL warehouse. Optional: `catalog`. Driver: `databricks-sql-connector`. |
| `trino` | `host`, `user` | Default port 8080. Optional: `catalog`, `schema`, `password` (HTTPS + basic auth). `presto` is an accepted alias. |

`supabase` is hosted PostgreSQL — use `type = postgres` (the Supabase pooler host is detected automatically).

Optional in all profiles: `schema` (default `public` for Postgres / `PUBLIC` for Snowflake), `sslmode` (Postgres / Redshift), `ssl` (MySQL).

**Or just use `url = ...`** instead of all individual fields — see the "Paste a full DSN" section below.

## File permissions (enforced)

The skill **refuses** to read `<artifacts_dir>/local/credentials` unless `chmod 600` (or stricter, e.g., `400`):

```
<artifacts_dir>/local/credentials must be chmod 600 (currently 644).
Run: chmod 600 <artifacts_dir>/local/credentials
```

The agami-connect Phase 0a sets the right permissions automatically when it writes the file. If you create it by hand, run `chmod 600 <artifacts_dir>/local/credentials` afterwards.

## Profile selection

The active profile is the one `init` recorded in `<artifacts_dir>/local/.config.active_profile` when you first set up. Override per shell session with:

```bash
AGAMI_PROFILE=staging   # uses [staging] section
```

To permanently change the active profile, edit `<artifacts_dir>/local/.config` and update `active_profile` (or re-run `/agami-connect` and pick again).

The skill writes the semantic model under `<artifacts_dir>/<profile>/` — one directory per profile, so multiple databases live side by side. `<artifacts_dir>` defaults to `~/agami-artifacts/` and is configurable per [`shared/file-layout.md`](file-layout.md).

## What the file does NOT contain

- No semantic model (that's `datasource.yaml` + the `subject_areas/<area>/` tree under `<artifacts_dir>/<profile>/`)
- No example queries (those live in `<artifacts_dir>/<profile>/examples.yaml`)
- No charts, exports, or query log

If a `credentials` line begins with `#` or `;`, it's treated as a comment.
