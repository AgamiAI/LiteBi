# Database error classifier

Friendly error routing for database-layer failures. Consumed wherever a SQL execution touches a live
database — `agami-connect` (introspection), `agami-query` (the query path), and `agami-save-correction`
(when an EXPLAIN of a corrected query fails).

The contract: every raw exception from any connection method (native CLI / Python driver / DuckDB) gets
classified before it reaches the user. A solo user staring at `psycopg2.errors.UndefinedColumn: column
"x" does not exist` has no support channel; the classifier turns it into one actionable line — e.g. "your
model references a column that's no longer in the live DB; re-introspect with `/agami-connect`."

This file is the single source of truth for the classification rules.

## Classification rules

When SQL execution raises an exception, match the exception class and message against this table **in
order**. First match wins. If no class matches, fall through to `kind: other` and surface the raw error.
Credentials live in `<artifacts_dir>/local/credentials` (per-profile `[section]`s).

| Class | Detection (any of) | Remediation |
|---|---|---|
| `auth` | psycopg2 `OperationalError` with "password authentication failed", "FATAL: password", "no pg_hba.conf entry"; mysql `Access denied`; snowflake `Incorrect username or password`; SF/HTTP 401; `KeyError` on a missing credential field (`PASSWORD`, `PWD`, `USER`, `TOKEN`, `KEY`) | `"Edit <artifacts_dir>/local/credentials — your <db> credentials may have rotated, a field may be missing, or the user may lack login permission. Re-run after fixing."` |
| `dsn` | "could not translate host name", "Name or service not known", "getaddrinfo ENOTFOUND", "Unknown MySQL server host"; mysql `Can't connect`; "no such file or directory" against a sqlite/duckdb path | `"Check <artifacts_dir>/local/credentials: the host/path for this profile doesn't resolve. Common causes: typo in hostname, VPN not connected, server moved. For local sqlite/duckdb, confirm the file path exists."` |
| `network` | "Connection refused", "timed out", "Connection reset by peer", `socket.timeout`, requests `ConnectTimeout` / `ReadTimeout`, snowflake `OperationalError` with "Could not connect", "SSL: WRONG_VERSION_NUMBER" | `"Network error reaching <db_host>. Common causes: VPN not connected, firewall blocking the port, server is down, SSL/TLS misconfiguration. Test with: nc -zv <host> <port> (or your usual reachability check)."` |
| `driver_missing` | `ModuleNotFoundError` / `ImportError` for `psycopg2`, `pymysql`, `pyodbc`, `snowflake.connector`, `google.cloud.bigquery`, `redshift_connector`, `duckdb`; native-CLI shell error "command not found: <psql\|mysql\|bq\|snowsql\|sqlite3>" | `"Driver missing: pip install <driver_pkg> (or install the CLI, e.g. brew install <cli_pkg>). See docs/credentials.md for the driver per dialect."` |
| `permission` | "permission denied for table", "permission denied for relation", "permission denied for schema", "INSUFFICIENT_PRIVILEGES" (Snowflake), MySQL `1142` `SELECT command denied to user`, BigQuery `Access Denied`, SF `INSUFFICIENT_ACCESS_OR_READONLY` | `"Your DB user can connect but cannot read <object>. Grant SELECT on <schema>.<table> (or GRANT USAGE on the schema), then re-run."` |
| `column_not_found` | psycopg2 `UndefinedColumn`; mysql `1054` `Unknown column`; snowflake `Invalid identifier`; BigQuery `Name <x> not found`; sqlite `no such column`; SF `INVALID_FIELD` | If the missing column **is** in the local semantic model YAML: `"Your model references <col> but it's no longer in the live database — schema drift. Re-introspect with /agami-connect to sync."` Otherwise: `"Generated SQL referenced a column that doesn't exist. Re-run the query — it'll auto-retry with corrected SQL."` |
| `table_not_found` | psycopg2 `UndefinedTable`; mysql `1146` `Table doesn't exist`; snowflake `Object <x> does not exist`; BigQuery `Table <x> not found`; sqlite `no such table` | If the missing table **is** in the local semantic model: `"Your model references <table> but it's no longer in the live database — schema drift. Re-introspect with /agami-connect to sync."` Otherwise: `"Generated SQL referenced a table that doesn't exist in this datasource. Re-run the query."` |
| `syntax` | psycopg2 `SyntaxError` (DB-side); mysql `1064` `You have an error in your SQL syntax`; snowflake `compilation error`; sqlite `near "X": syntax error` | `"SQL syntax error from the generator. Re-run the query — auto-retry usually fixes generator slips."` |
| `other` | anything else | `"<original error message>. For deeper per-datasource troubleshooting see the connection reference (plugins/agami/shared/connection-reference.md) and docs/troubleshooting.md."` |

**There is no `timeout` row, deliberately.** It used to detect psycopg2 `QueryCanceled`, MySQL 2013,
Snowflake `query was canceled` and explicit `statement_timeout` errors — every one of which is a
*cancellation signature*, and a deadline is now classified from the watchdog signal that fired it
rather than from whatever string the driver happened to return. `timeout` survives as a
`FailureKind` with exactly one producer, the subprocess supervisor stopping an executor that never
returned, which no rule in this table can see. A per-statement deadline is a `resource_limit`
**refusal**, not a classified error: it is a decision the server made, so it carries a remediation,
which is precisely what a failure does not.

An unattributable server-side cancellation therefore classifies as `other`. That is the honest
label — deleting the row without saying so would have let those errors fall through to `network`
(the "timed out" wording) or to `syntax` (the exit-code prior), and both read as a diagnosis this
table cannot actually make.

## Drift-aware column / table classification

The `column_not_found` and `table_not_found` cases need a follow-up step before remediation: **does the
missing object exist in the local semantic model?**

For column errors:
1. **Extract the qualified name when available.** Most drivers emit a qualified form somewhere in the
   error: psycopg2's `UndefinedColumn` includes "of relation `<schema>.<table>`" in its `diag` block;
   Snowflake's `Invalid identifier '<schema>.<table>.<column>'` is fully qualified; BigQuery's `Name
   <project>.<dataset>.<table>.<column> not found` is fully qualified. Parse the most-qualified form
   available (preferring `<schema>.<table>.<column>` > `<table>.<column>` > `<column>`).
2. **Match strategy by qualification level:**
   - **Fully qualified** (`<schema>.<table>.<column>`): exact match against the loaded model. If the
     schema-qualified table exists locally and contains the column, `drift_match: true`. If the table
     exists but lacks the column, that's the textbook drift signal — same `drift_match: true`. If the
     table doesn't exist locally, `drift_match: false`.
   - **Table-qualified** (`<table>.<column>`, no schema): find every loaded table whose unqualified name
     matches; check each for the column. One match → `drift_match: true` with that candidate. Multiple →
     `drift_match: true` with `drift_candidates: [...]`; the remediation asks which schema's table drifted.
   - **Bare column name**: scan all loaded tables for a `columns:` key matching the bare name. One match →
     that candidate; multiple → `drift_candidates: [...]` and prompt the user.
3. **Regex parse miss** (an unrecognized wording — e.g. a Postgres/MySQL fork like CockroachDB or AlloyDB):
   emit `kind: other` with `classifier_unmatched: true` rather than silently routing to a generic
   "auto-retry will fix" remediation — auto-retry won't fix drift.

For table errors: same flow — qualified matching uses `<schema>.<table>` exact match; unqualified falls
back to scanning all loaded tables. The `kind: other` + `classifier_unmatched: true` fallback applies.

## Output shape

The classifier returns:

```python
{
  # The vocabulary is owned by `guardrail.FailureKind` — this document does not declare its own.
  # Two surfaces read the same kinds and present them differently, which is the point: locally the
  # raw error is already in front of the user and a remediation is worth giving, while
  # `execute_sql`'s failure crosses the LLM boundary and carries `{kind, message}` with no
  # remediation and no value text. Detection rules are shared; presentation is per-surface.
  "kind": "auth | dsn | network | driver_missing | permission | column_not_found | table_not_found | syntax | timeout | other",
  "remediation": "<one-line user-facing remediation message>",
  "raw_message": "<original exception message, truncated to 500 chars>",
  "drift_match": True | False,    # set only on column_not_found / table_not_found
  "tier": "cli | python | duckdb",
  "datasource": "<datasource name>"
}
```

Callers use `kind` for control flow and `remediation` for the user-facing message. `raw_message` is
preserved so the query log captures the original.

## Where this is consumed

- **`agami-query`** — wraps each connection method's SQL call. On `auth` / `dsn` / `network` /
  `driver_missing` / `permission`, surface the remediation and stop. On `column_not_found` /
  `table_not_found`, run the drift-match step and emit the appropriate message. On `syntax` / `timeout`,
  the query path's auto-retry fires; the classifier just labels the failure.
- **`agami-connect`** — when an introspection query fails, classify it and surface the one-line
  remediation instead of a raw driver traceback.
- **`agami-save-correction`** — when the EXPLAIN of a user-corrected query fails, route it here, surface
  the one-line remediation, and don't save the correction until it EXPLAINs clean.
