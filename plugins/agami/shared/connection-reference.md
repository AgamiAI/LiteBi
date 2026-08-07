# Database Connection Reference

How `agami` connects to your database. Used by the `connect`, `query-database`, and `agami-save-correction` skills.

## HARD RULES — read first

These are non-negotiable. Skills that read this document must follow them under every circumstance.

1. **Connect ONLY to the host/port/database/user/password in `<artifacts_dir>/local/credentials`** — the sole credential source; there is no env-var bypass. Never use `localhost` or any other host as a fallback. If the credentials say `host = remote-prod.example.com`, the only acceptable connection is to `remote-prod.example.com` — not also to `localhost` "to see if there's something there".
2. **Never ask the user for connection details in chat.** Credentials live in `<artifacts_dir>/local/credentials` only. If the file is missing, invoke agami-connect Phase 0a (which writes a `credentials.example` template the user edits). Never accept host / port / database / user / password values typed inline.
3. **Never scan or guess.** Tool detection is `which <tool>` and `python3 -c 'import <module>'`. Nothing else. No `pgrep`, `ps`, `lsof`, `find /`, `ls /Applications`, port scanning, or hostname guessing. Tool paths are cached in `<artifacts_dir>/local/.config.tool_paths` so subsequent skill invocations don't even re-probe — they read the cached path and use it.
4. **If the cached tool path is broken** (binary moved or uninstalled), surface the failure cleanly and offer to re-detect. Do not silently fall through to localhost-probing or any other discovery technique.
5. **NEVER put the password (or any credential field) in a Bash command line.** Hosts render Bash tool calls in their UI — anything in the command, env-var assignment, or stdin is visible to anyone scrolling the chat. Use the provider-native auth files written by `scripts/setup_pgauth.py`:
   - For postgres: `PGPASSFILE=<artifacts_dir>/local/.pgpass psql -h <host> -p <port> -U <user> -d <db> -c "$SQL" --csv`
   - For mysql: `mysql --defaults-file=<artifacts_dir>/local/.mysql.cnf --defaults-group-suffix=_<profile> -h <host> -P <port> <db> -e "$SQL" --batch --raw`
   These auth files are chmod 600 in `<artifacts_dir>/local/`. The visible Bash command contains NO password — psql / mysql read the password from the auth file silently. **Patterns that are FORBIDDEN**: `export PGPASSWORD='<literal>'`, `export MYSQL_PWD='<literal>'`, `psql -W <password>`, `mysql -p<password>`, or anything else where the password appears in the command, env assignment, or stdin.

## Contents
- Connection methods (psql / mysql / snowsql / sqlite3 → DuckDB → Python driver)
- Native CLI tool (`psql`, `mysql`, `snowsql`, `sqlite3`)
- DuckDB universal client
- Python driver via `execute_sql.py`
- Connection Defaults
- CLI Connection Commands
- Python Driver Fallback
- Reading Credentials
- System Schema Exclusions
- Security Rules

---

## Quick connection probes — `SELECT 1` per tier

Reference this table whenever a skill needs to verify a connection works. Don't guess flags — `execute_sql.py` has a tight CLI and made-up flags like `--format json` produce wasted-turn errors that look bad in chat.

| tier | EXACT invocation |
|---|---|
| `cli` postgres | `PGPASSFILE="<artifacts_dir>/local/.pgpass" psql -h <host> -U <user> -d <db> -c 'SELECT 1' --csv` |
| `cli` mysql | `mysql --defaults-file="<artifacts_dir>/local/.mysql.cnf" --defaults-group-suffix="_<profile>" -e 'SELECT 1' --batch` |
| `cli` snowflake | `snowsql --config "<artifacts_dir>/local/.snowsql.cnf" -c "<profile>" -q 'SELECT 1' -o output_format=csv -o friendly=false` |
| `python` bigquery | `AGAMI_PROFILE="<profile>" "$PY" -m execute_sql --sql 'SELECT 1'` (BigQuery is Python-only — there's no native CLI tier today) |
| `cli` sqlite | `sqlite3 -header -csv "<path>" 'SELECT 1'` — **always pass `-header`** so result CSVs carry column names (without it `format-table` treats row 1 as the header). |
| `duckdb` | `duckdb -init "$init_file" -c 'SELECT 1' --csv` |
| `python` | `AGAMI_PROFILE="<profile>" "$PY" -m execute_sql --sql 'SELECT 1'` |

**`python -m execute_sql` CLI surface** — exhaustive, read before invoking:

```
python3 execute_sql.py [-h] [--profile PROFILE] (--sql SQL | --sql-file SQL_FILE)
```

- **Output is RFC-4180 CSV on stdout, always.** No `--format` flag exists; don't pass one.
- **Either** `--sql 'SELECT 1'` (string) **or** `--sql-file /tmp/q.sql` (path). Positional SQL is rejected.
- `--profile` overrides `AGAMI_PROFILE`. If neither is set, defaults to `active_profile` from `<artifacts_dir>/local/.config`.
- Exit codes: `0` (success), `2` (config/usage), `3` (connect error), `4` (execution error), `5` (driver missing).

For Snowflake-specific liveness checks where `SELECT CURRENT_VERSION()` reads better than `SELECT 1`, just substitute the SQL — same flag shape. **Never** add flags not listed above.

---

## Connection methods

`agami` picks the first available connection method for your database type, falling through if it's not installed. If nothing is installed, it tells you exactly which tools are missing and the install command for each.

The order, from most-preferred to least-preferred:

| # | Method | When it's the right pick | Pros | Cons |
|---|--------|--------------------------|------|------|
| 1 | **Native CLI tool** (`psql`, `mysql`, `snowsql`, `sqlite3`) | Most common path on a developer laptop | Fast, idiomatic, no extra layer | Requires the per-DB CLI on `PATH` |
| 2 | **DuckDB universal client** | When you don't have / don't want the native CLI | Single binary install; handles Postgres / MySQL / SQLite / Parquet / CSV | Doesn't natively cover Snowflake, BigQuery (use the Python tier for those), SQL Server, Oracle, Databricks |
| 3 | **Python driver** (`psycopg2`, `pymysql`, `snowflake-connector-python`) | If you already have Python set up | Works in environments without the CLI | Adds a Python dependency |

`agami` runs entirely on your machine. There is no hosted server.

> **Internal note on `.config`.** The chosen method is recorded in `<artifacts_dir>/local/.config.tier` for compatibility with shipped installs — values are `cli` / `duckdb` / `python`. The field name stays as `tier`; user-facing prose calls these "the native CLI", "DuckDB", and "the Python driver".

### How agami picks a connection method

Detection runs **once**, in agami-connect Phase 0a's Phase 3. The result (chosen method + absolute paths of every detected tool) is persisted in `<artifacts_dir>/local/.config`. Every subsequent skill invocation reads the cached paths — they do NOT re-probe.

Init's selection pseudocode:

```text
db_type := credentials → type   (e.g., "postgres", "redshift", "snowflake")

# Detect every tool in parallel and cache the absolute path of each.
tool_paths := {
  psql:    which("psql")  || ls /opt/homebrew/Cellar/libpq/*/bin/psql /opt/homebrew/opt/libpq/bin/psql,
  mysql:   which("mysql") || ls /opt/homebrew/opt/mysql-client/bin/mysql,
  snowsql: which("snowsql"),
  sqlite3: which("sqlite3"),
  duckdb:  which("duckdb"),
  python3: $PY,    # AGAMI_PYTHON → existing .config → which("python3"); the DB driver is
                   # installed into THIS interpreter (0a.5) and execute_sql.py runs under it
}
# Probe driver imports in $PY (not bare python3 — they can differ):
tool_imports := {
  psycopg2:                    py_import_ok($PY, "psycopg2"),
  pymysql:                     py_import_ok($PY, "pymysql"),
  snowflake_connector_python:  py_import_ok($PY, "snowflake.connector"),
}

# Pick the first available method for db_type.
# postgres / redshift share psql + psycopg2 (Redshift speaks Postgres wire protocol).
# (Internal config field name stays `tier`; values "cli" / "duckdb" / "python".)
if db_type in {postgres, redshift} and tool_paths.psql:                  return tier=cli
if db_type == "mysql"    and tool_paths.mysql:                           return tier=cli
if db_type == "snowflake" and tool_paths.snowsql:                        return tier=cli
if db_type == "sqlite"   and tool_paths.sqlite3:                         return tier=cli
if db_type in {postgres, redshift, mysql, sqlite} and tool_paths.duckdb: return tier=duckdb
if db_type in {postgres, redshift} and tool_imports.psycopg2:            return tier=python
if db_type == "mysql"    and tool_imports.pymysql:                       return tier=python
if db_type == "snowflake" and tool_imports.snowflake_connector_python:   return tier=python
if db_type == "sqlite"   and tool_paths.python3:                         return tier=python  # stdlib

# Nothing worked.
offer_install()  # AskUserQuestion — never install silently
```

DuckDB's `postgres_scanner` extension can also scan Redshift over the wire (since Redshift is Postgres-protocol-compatible). DuckDB cannot scan Snowflake natively in v1.1.

Other skills look up the cached method (`.config.tier`) and `tool_paths.<tool>` from `<artifacts_dir>/local/.config` and use them directly. They do not re-run `which`. If the cached path no longer exists on disk (`! -x "$path"`), they offer to re-detect — they do NOT silently scan or fall back to localhost.

### When no tool is available

Surface a single, specific error that names exactly what's missing — never a generic "connection failed":

```
Couldn't find a tool to talk to your Postgres database:
  ✗ psql        not on PATH
  ✗ duckdb      not on PATH
  ✗ psycopg2    not importable

Pick one to install (any one is enough):
  a) psql       — `brew install postgresql`       (simplest — most common)
  b) DuckDB     — `brew install duckdb`           (single universal binary)
  c) psycopg2   — `pip install psycopg2-binary`   (Python driver path)

Reply with a/b/c or install manually.
```

---

## Native CLI tool (`psql`, `mysql`, `snowsql`, `sqlite3`)

The default and fastest path. Most Postgres users already have `psql`; most MySQL users have `mysql`; Snowflake users use `snowsql`; SQLite users use `sqlite3`. See **CLI Connection Commands** below for the canonical invocation per database.

---

## DuckDB universal client

DuckDB ships as a single binary (`brew install duckdb` / `apt install duckdb` / download from duckdb.org). It natively reads from:

- **PostgreSQL** (via the `postgres_scanner` extension — auto-installed on first use)
- **MySQL** (`mysql_scanner`)
- **SQLite** (built-in)
- **File sources**: Parquet, CSV, JSONL, Excel, Arrow, S3

It does **not** natively cover Snowflake, BigQuery, SQL Server, Oracle, or Databricks. For those, DuckDB is not a valid fallback — drop straight to the "no tool available" message.

### Offering DuckDB to the user

Only offer DuckDB when:
1. The database type is in `{postgres, mysql, sqlite, duckdb, file}`, AND
2. The native CLI for that database is not available.

Never install silently — prompt via **AskUserQuestion** with the install command specific to the user's OS. Respect a "no" answer and fall through to the no-tool-available error.

### Connecting from DuckDB to Postgres / MySQL

```bash
# Postgres
duckdb <<SQL
  INSTALL postgres_scanner; LOAD postgres_scanner;
  ATTACH 'host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER password=$PGPASSWORD' AS pg (TYPE POSTGRES);
  SELECT * FROM pg.public.<table> LIMIT 10;
SQL
```

```bash
# MySQL
duckdb <<SQL
  INSTALL mysql_scanner; LOAD mysql_scanner;
  ATTACH 'host=$MYSQL_HOST port=$MYSQL_PORT user=$MYSQL_USER password=$MYSQL_PWD database=$MYSQL_DATABASE' AS my (TYPE MYSQL);
  SELECT * FROM my.<table> LIMIT 10;
SQL
```

CSV output: append `-csv` or wrap in `COPY (<query>) TO '/dev/stdout' (FORMAT CSV)`.

---

## Python driver via `execute_sql.py`

Used when neither the native CLI nor DuckDB is available, but Python with the right driver is. The agami skill ships a runtime helper for this.

**Always invoke it with `$PY` — the interpreter recorded in `<artifacts_dir>/local/.config` — never bare `python3`.** Bare `python3` on `PATH` often differs from the one with the driver, which is the #1 cause of "driver not found" at query time. agami-connect already discovered the right interpreter (the one with the DB driver + model deps) and wrote it to `.config`; **read it from there — the user never sets an environment variable**:

```bash
PY="$(python3 -c 'import json,pathlib; print(json.loads(pathlib.Path("<artifacts_dir>/local/.config").expanduser().read_text()).get("tool_paths",{}).get("python3") or "")' 2>/dev/null)"
PY="${PY:-$(command -v python3 || command -v python)}"
```

(Same interpreter the `sm` wrapper and the introspection engine resolve to. Keeping them identical means the driver is present wherever a DB connection is opened.)

```bash
"$PY" -m execute_sql --profile <profile> --sql-file /tmp/agami-query.sql
```

`execute_sql.py` reads `<artifacts_dir>/local/credentials` itself, opens a connection via `psycopg2` / `pymysql` / `sqlite3` based on the profile's `type` field, runs the SQL, emits RFC 4180 CSV on stdout. Exit codes communicate the failure category (config, driver missing, connect error, execution error). See [`plugins/agami/scripts/README.md`](../scripts/README.md) for full usage.

Skills should always use `--sql-file` for non-trivial SQL. The `--sql` flag is fine for short statements; `--sql-file` avoids any shell-quoting issues for SQL containing single quotes, backticks, `$`, or backslashes.

The "Python Driver Fallback" section further down shows the inline `python3 -c '...'` form that does the same thing without the helper — useful only if `execute_sql.py` isn't bundled (e.g., a legacy install). Prefer `execute_sql.py`.

---

## Connection Defaults

| Database | Default Port | CLI Tool | Python Driver | SSL |
|---|---|---|---|---|
| PostgreSQL | 5432 | `psql` | `psycopg2` (`pip install psycopg2-binary`) | `prefer` (default) |
| **Redshift** | **5439** | `psql` (Redshift speaks Postgres wire protocol) | `psycopg2` | **`require`** (default for Redshift) |
| MySQL / MariaDB | 3306 | `mysql` | `pymysql` (`pip install pymysql`) | optional |
| **Snowflake** | 443 (HTTPS) | `snowsql` | `snowflake-connector-python` (`pip install snowflake-connector-python`) | TLS always (managed by client) |
| SQLite | N/A (file) | `sqlite3` | built-in `sqlite3` | n/a |
| DuckDB | N/A (file) | `duckdb` | built-in or `pip install duckdb pytz` | n/a |

Supported end-to-end: Postgres + Redshift + MySQL + Snowflake + SQLite + **BigQuery**. SQLite also works via DuckDB. Other databases (SQL Server, Oracle, Databricks, ClickHouse) are deferred.

> **Why `pytz` with DuckDB:** the DuckDB Python driver needs `pytz` to materialize `TIMESTAMP WITH TIME ZONE` values into Python — without it, any query selecting a timestamptz column fails at runtime (`Required module 'pytz' failed to import`). Install it alongside `duckdb`.

---

## CLI Connection Commands

### PostgreSQL

```bash
# Ensure the auth file exists for the active profile (idempotent, fast).
# Generates <artifacts_dir>/local/.pgpass from credentials. Bash command line contains
# NO password.
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Execute a query and return CSV. PGPASSFILE points at the auth file;
# psql reads the password silently. The bash command itself is password-free.
PGPASSFILE="<artifacts_dir>/local/.pgpass" PGSSLMODE="${sslmode:-prefer}" \
  psql -h "$host" -p "$port" -U "$user" -d "$database" -c "$SQL" --csv

# Execute from a file
PGPASSFILE="<artifacts_dir>/local/.pgpass" PGSSLMODE="${sslmode:-prefer}" \
  psql -h "$host" -p "$port" -U "$user" -d "$database" -f query.sql --csv
```

**Security / SSL**:
- **Never** pass the password on the command line, in `export PGPASSWORD='...'`, or via `-W <password>`. Always use `PGPASSFILE` pointing at `<artifacts_dir>/local/.pgpass` (chmod 600, generated by `scripts/setup_pgauth.py`). The visible Bash command must be password-free.
- Set `PGSSLMODE` from the credentials profile's `sslmode` field. Cloud Postgres providers (Supabase, Neon, RDS in many configs) **require** SSL — set `sslmode = require` in `<artifacts_dir>/local/credentials` or use a DSN with `?sslmode=require` and the parser will pick it up. Default is `prefer` which works for both SSL-required and non-SSL servers.

**Supabase pooler**: the SQLAlchemy-style DSN that Supabase shows (`postgresql+asyncpg://...`) is accepted as-is in the `url = ...` credentials field — see [`credentials-format.md → Supabase`](credentials-format.md). The `+asyncpg` driver suffix is stripped before connecting.

### Redshift

Redshift speaks the PostgreSQL wire protocol, so **psql works as-is**. The only differences from regular Postgres:

- Default port is **5439** (not 5432)
- SSL is **required** by default (`sslmode=require`)
- The hostname is the cluster's full DNS — `<cluster>.<region>.redshift.amazonaws.com` for provisioned clusters, or `<workgroup>.<account>.<region>.redshift-serverless.amazonaws.com` for Redshift Serverless.

`scripts/setup_pgauth.py` writes a `.pgpass` line for Redshift profiles the same as for postgres profiles — no special handling needed.

```bash
# Same invocation as postgres, just with the Redshift host/port and sslmode=require.
PGPASSFILE="<artifacts_dir>/local/.pgpass" PGSSLMODE="require" \
  psql -h "$host" -p 5439 -U "$user" -d "$database" -c "$SQL" --csv
```

`type = redshift` in the credentials profile (or a `redshift://` DSN) sets the right defaults. For the Python driver path the connection params are identical to postgres; agami's `execute_sql.py` routes `type=redshift` through the postgres execution path.

### Snowflake

Snowflake doesn't speak the Postgres wire protocol. It needs its own native CLI (`snowsql`) or the `snowflake-connector-python` Python driver.

#### Native CLI — `snowsql`

`scripts/setup_pgauth.py` writes a `<artifacts_dir>/local/.snowsql.cnf` config file with a `[connections.<profile>]` block per Snowflake profile in your credentials. The skill invokes snowsql with `--config` pointing at it:

```bash
# Ensure the snowsql config exists (idempotent).
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Run a query — snowsql reads the password from the config silently.
snowsql --config "<artifacts_dir>/local/.snowsql.cnf" -c "$PROFILE" \
        -q "$SQL" -o output_format=csv -o header=true -o friendly=false -o timing=false
```

The `-o output_format=csv -o header=true` flags produce parseable output. `-o friendly=false -o timing=false` strips the human-friendly banner and timing line so the CSV is clean.

Install snowsql: see <https://docs.snowflake.com/en/user-guide/snowsql-install-config>. macOS: download from Snowflake's website (Homebrew formula isn't official).

#### Python driver — `snowflake-connector-python`

```bash
pip install snowflake-connector-python
"$PY" -m execute_sql --profile "$PROFILE" --sql-file /tmp/agami-q.sql
```

`execute_sql.py` handles Snowflake natively when `type=snowflake`. Connection params: `account`, `user`, `password` (or `authenticator` for SSO), `warehouse`, `database`, `schema`, `role`. All optional except `account` and `user`; either `password` or `authenticator` is required.

#### Account identifier formats

Snowflake's `account` field is **not** a hostname. Examples:

- `xy12345` — short locator (legacy, AWS US-West-2)
- `xy12345.us-east-1` — locator + region (AWS)
- `xy12345.us-east-1.aws` — locator + region + cloud
- `myorg-myaccount` — newer org-account format (recommended by Snowflake)

The connector / snowsql appends `.snowflakecomputing.com` automatically. Use whatever your Snowflake admin gave you.

### BigQuery

BigQuery has no host:port and no native CLI tier in agami today. It uses Google's REST API via the `google-cloud-bigquery` Python library, authenticated by a service-account JSON key (recommended) or Application Default Credentials.

#### Python driver — `google-cloud-bigquery`

```bash
pip install google-cloud-bigquery

# Run a query — execute_sql.py loads the service-account JSON itself and
# never puts credentials on the Bash command line.
"$PY" -m execute_sql --profile "$PROFILE" --sql-file /tmp/agami-q.sql
```

`execute_sql.py` handles BigQuery natively when `type=bigquery`. Connection params (read from the credentials profile):

- `project` — required. The GCP project ID (e.g., `my-prod-project-12345`).
- `service_account_path` — recommended. Absolute path to the JSON key file. `chmod 600` it.
- `dataset` — optional. Sets the default dataset so SQL can use bare `table_name`.
- `location` — optional. BigQuery dataset location (`US`, `EU`, `asia-northeast1`, etc.).

If `service_account_path` is omitted, the BigQuery client falls back to Application Default Credentials (`gcloud auth application-default login`).

#### DSN form

```
bigquery://<project>[/<dataset>]?service_account=/abs/path/to/key.json&location=US
```

The `bq://` scheme also works. Query parameters `credentials_path` and `service_account` are equivalent.

#### Cost note

BigQuery bills per byte scanned (~$5 / TB). `INFORMATION_SCHEMA` queries are free, so introspection is cheap. Sample-row queries (for description generation, choice-field detection) ARE billed — the SKILL applies `LIMIT 100` aggressively and uses `TABLESAMPLE SYSTEM (1 PERCENT)` on tables > 1B rows. Runtime queries from `agami-query` are billed at whatever they scan; the row estimates on the receipt's `tables.items[]` give the user visibility.

#### Required IAM roles

The service account needs:
- **`roles/bigquery.dataViewer`** on every dataset to be queried
- **`roles/bigquery.jobUser`** on the project (to submit jobs)

These are sufficient for SELECT-only workloads, which is all agami issues.

### MySQL / MariaDB

```bash
# Ensure the auth file exists for the active profile (idempotent, fast).
# Generates <artifacts_dir>/local/.mysql.cnf with [client_<profile>] sections. Bash
# command line contains NO password.
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile "$PROFILE"

# Execute a query — mysql reads creds from the auth file via --defaults-file
# + --defaults-group-suffix=_<profile>. Visible bash command is password-free.
mysql --defaults-file="<artifacts_dir>/local/.mysql.cnf" \
      --defaults-group-suffix="_$PROFILE" \
      -h "$host" -P "$port" "$database" \
      -e "$SQL" --batch --raw

# CSV-like output
mysql --defaults-file="<artifacts_dir>/local/.mysql.cnf" \
      --defaults-group-suffix="_$PROFILE" \
      -h "$host" -P "$port" "$database" \
      -e "$SQL" --batch --raw | tr '\t' ','
```

**Security**: Never use `-p<password>`, `--password=...`, or `export MYSQL_PWD='...'` — all of those leak the password into Bash command-line / process listings / chat transcripts. Always use `--defaults-file=<artifacts_dir>/local/.mysql.cnf` (chmod 600, generated by `scripts/setup_pgauth.py`).

### SQLite
```bash
sqlite3 "$path" "$SQL" -csv -header
```

---

## Python Driver Fallback

When CLI tools are not available, use the bundled runtime helper:

```bash
# Single-line SQL via --sql
"$PY" -m execute_sql --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred)
"$PY" -m execute_sql --sql-file /tmp/agami-query.sql
```

`execute_sql.py` reads `<artifacts_dir>/local/credentials` itself (with chmod check) and connects via `psycopg2` / `pymysql` / `sqlite3`. The visible Bash command contains no credentials. SQL is passed via `--sql-file` (preferred for non-trivial queries) so single quotes, backticks, `$`, and `\` in the SQL don't get mangled by the shell.

The legacy inline `python3 -c '...'` pattern (with `export PGPASSWORD=...` / `export MYSQL_PWD=...` ahead of it) is **forbidden** — it puts the password in the visible Bash command line. Use `execute_sql.py` instead.

The DuckDB scanner approach currently has a similar weakness for cloud-credentialed databases — DuckDB's `ATTACH 'host=... password=...'` requires the password in the SQL string. For Supabase / Neon / RDS connections, prefer the native CLI (psql with `PGPASSFILE`) or the Python driver (`execute_sql.py`) over DuckDB.

---

## Reading Credentials

Credentials live in `<artifacts_dir>/local/credentials` (an INI-style file, `chmod 600`) — the only source. Format spec: [`credentials-format.md`](credentials-format.md).

### Reading the file

```bash
# Refuse to read if too permissive
perms=$(stat -c '%a' <artifacts_dir>/local/credentials 2>/dev/null || stat -f '%A' <artifacts_dir>/local/credentials)
case "$perms" in
  600|400) ;;
  *)
    echo "<artifacts_dir>/local/credentials must be chmod 600 (currently $perms). Run: chmod 600 <artifacts_dir>/local/credentials" >&2
    exit 1
    ;;
esac

# Parse a profile (default: [main])
profile="${AGAMI_PROFILE:-main}"
# Use awk or python to extract host/port/user/password/database/type for the named profile
```

(A profile may set `url = <DSN>` instead of per-field values — that DSN is parsed
the same way. There is no `AGAMI_DATABASE_URL` env-var override: credentials come
only from `<artifacts_dir>/local/credentials`, so the one path stays auditable and chmod-gated.)

---

## System Schema Exclusions

When introspecting databases, exclude system schemas:

| Database | Exclude |
|----------|---------|
| PostgreSQL | `pg_catalog`, `information_schema`, `pg_toast`, `pg_internal`, `pg_temp_*` |
| MySQL/MariaDB | `information_schema`, `mysql`, `performance_schema`, `sys` |
| SQLite | `sqlite_master`, `sqlite_sequence` (filter by name prefix) |

---

## Security Rules

- **NEVER** put passwords in any visible Bash command — not in `export PGPASSWORD='...'`, not in `mysql -p<password>`, not in stdin heredocs that interpolate the password. Hosts render Bash tool calls in their UI; the password leaks into the chat. Use the auth files generated by `scripts/setup_pgauth.py`:
  - psql: `PGPASSFILE=<artifacts_dir>/local/.pgpass psql -h <host> -p <port> -U <user> -d <db> -c "$SQL" --csv`
  - mysql: `mysql --defaults-file=<artifacts_dir>/local/.mysql.cnf --defaults-group-suffix=_<profile> -h <host> -P <port> <db> -e "$SQL" --batch --raw`
  - Python driver: `"$PY" -m execute_sql --sql-file ...` (reads creds internally; never echoes them)
- Use `--csv` or `--batch` output modes (not interactive) for predictable parsing
- **Result-set size policy** — chat preview shows the first **30 rows**; for results > 30 the full set auto-exports to `<artifacts_dir>/local/exports/<ts>.csv` alongside the HTML report (CSV opens natively in Excel / Numbers / Sheets). User can override per-query with "top N" or "limit N" framing.
- **NEVER** generate DDL or DML statements (`DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, etc.)
- Sanitize user input before including in SQL queries
- `<artifacts_dir>/local/credentials` must be `chmod 600`. The agami-connect Phase 0a enforces this; refuse to read otherwise.
