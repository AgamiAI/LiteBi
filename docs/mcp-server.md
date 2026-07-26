# Using agami from other AI clients — the local MCP server (`agami serve`)

The agami plugin runs inside **Claude Code**. But your semantic model, examples,
and local SQL execution are just files + a script — so agami can also be served
to *any* AI client that launches a local **stdio** MCP server as a child process.
Today that means **Claude Code** (`claude mcp add`) and **Claude Desktop**
(`claude_desktop_config.json`).

This is the local mirror of Agami's hosted "Ask Agami" connector: the same tool
surface, but backed by your local `<artifacts_dir>/local` files and local DB execution instead
of a cloud registry. Going from this local server → the hosted team product is a
*backend swap*, not a new product.

```
your AI client  ──spawns──▶  python3 -m mcp_harness   (stdio child process)
   (Claude Code /                      │
    Claude Desktop)                    ├─ reads  <artifacts_dir>/<profile>/  (the semantic model + examples)
                                       └─ runs   python -m execute_sql  ──▶  your local DB
```

## What it exposes

Four tools, mirroring the hosted connector so the client experience is identical:

| Tool | What it does |
|---|---|
| `list_datasources` | Enumerate local profiles (credential sections) and whether each has a model. |
| `get_datasource_schema` | Return the semantic model: the subject-area index, full per-table detail for requested `dataset_names`, plus `datasource.md` / `USER_MEMORY.md`. |
| `get_prompt_examples` | Return the curated `examples.yaml` few-shot library. |
| `execute_sql` | Run **one read-only** `SELECT` / `WITH...SELECT` locally and return `{columns, rows, row_count, ...}`. DML/DDL/multi-statement are rejected. |

The NL→SQL *intelligence* stays on the client side (the model generates SQL from
the schema + examples these tools return) — exactly as with the hosted connector.

## Prerequisites

- You've already run `agami-connect` (so `<artifacts_dir>/local/credentials` and a model under
  `<artifacts_dir>/<profile>/` exist).
- The **agami-core package** installed in the Python you'll point the server at
  (it provides `mcp_harness` + `execute_sql`):

  ```bash
  pip install "agami-core[model]"                 # from PyPI
  # or, from a checkout (dev):  pip install -e "packages/agami-core[model]"
  ```

- A **Python driver** for your database, because the server executes via
  `execute_sql` (the Tier-3 Python path):

  ```bash
  pip install psycopg2-binary             # Postgres / Redshift
  pip install pymysql                     # MySQL
  pip install snowflake-connector-python  # Snowflake
  pip install google-cloud-bigquery       # BigQuery
  # SQLite needs nothing (stdlib)
  ```

## Connect it to Claude Code

```bash
claude mcp add agami -- /ABS/PATH/to/python3 -m mcp_harness
```

(where `/ABS/PATH/to/python3` is the interpreter that has agami-core installed — see Prerequisites.)
Optionally pin a profile/artifacts dir for this server with env vars:

```bash
claude mcp add agami \
  --env AGAMI_PROFILE=main \
  --env AGAMI_ARTIFACTS_DIR=$HOME/agami-artifacts \
  -- /ABS/PATH/to/python3 -m mcp_harness
```

## Connect it to Claude Desktop (macOS and Windows)

Claude Desktop is a **separate program from Claude Code** — installing the agami
plugin via the Claude Code marketplace does *not* register the server here.
Claude Desktop reads only its own `claude_desktop_config.json`. This works on both
**macOS and Windows**: the server is pure-stdlib Python and the setup helper writes
the correct config path per OS. It's been validated on the macOS app; the Windows
path is handled in code but is lightly tested (see the note at the end of this section).

### Recommended: one command

From Claude Code, just say *"set up agami for Claude Desktop"* (the **`agami-serve`**
skill), or run the helper directly:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_desktop_mcp.py" --dry-run   # preview the plan
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_desktop_mcp.py"             # apply it
```

It removes all three sharp edges of hand-editing:

- **auto-detects the right Python** — the interpreter that can actually import your
  DB driver (the GUI-PATH gotcha, solved);
- **installs the agami-core package** into that interpreter (non-editable, so the
  code lands in site-packages) and registers `python -m mcp_harness`, so the Desktop
  config survives plugin updates and keeps working even if the plugin is uninstalled;
- **safely merges** the entry into `claude_desktop_config.json` — timestamped
  backup, atomic write, every other key and MCP server preserved.

Then **fully quit** the app (Cmd+Q on macOS; quit from the system tray on Windows)
and reopen. Flags: `--profile NAME` (pin a profile), `--config PATH`
(target a different client's config file), `--python /abs/python3` (force an interpreter).
Re-run after a plugin update to reinstall.
The helper auto-resolves the macOS vs Windows config path for you.

### Manual (what the helper does under the hood)

If you'd rather edit by hand, mind these two gotchas — both silently produce
"no tools":

1. **Use an absolute path to a Python that has your DB driver.** Claude Desktop
   launches helpers with a *minimal PATH*, so a bare `python3`/`python` usually
   isn't found — and it must be the interpreter where `psycopg2` (etc.) is installed.
   Find it: `python3 -c 'import sys,psycopg2; print(sys.executable)'` (Windows:
   `python -c "import sys,psycopg2; print(sys.executable)"`). Example paths — macOS:
   `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`; Windows:
   `C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe`.
2. **Install agami-core into that Python and run it as a module.** The server is
   `python -m mcp_harness`, which needs the agami-core package importable in the
   interpreter from gotcha #1: `"/abs/python3" -m pip install "agami-core[model]"` (from a checkout, `-e "/path/to/packages/agami-core[model]"`).
   This is what the helper automates — and why it survives the plugin cache dir moving
   on each update (the code is installed, not referenced by a moving path).

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config). Its path is
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. `mcpServers` is a
**top-level** key (a sibling of `preferences`, not nested inside it):

```json
{
  "mcpServers": {
    "agami": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
      "args": ["-m", "mcp_harness"],
      "env": { "AGAMI_PROFILE": "main" }
    }
  }
}
```

**Fully quit** the app — Cmd+Q on macOS; quit from the system tray on Windows (not
just closing the window) — then reopen. It spawns the server as a child process
over stdin/stdout — **there is no URL and no port.** Logs:
`~/Library/Logs/Claude/mcp-server-agami.log` on macOS,
`%APPDATA%\Claude\logs\mcp-server-agami.log` on Windows.

> **Containment — confirmed working (verified 2026-06 on the macOS app).** A
> custom stdio MCP server runs with your full user access: it reads `<artifacts_dir>/local`
> and executes SQL against your DB from inside the Mac app, no directory-mount
> prompt required. (The "mount a directory" sandbox applies to Claude's *built-in*
> file tools, not custom MCP servers.) This is app-version-dependent, so if a
> future build changes it — `list_datasources` returns empty or `execute_sql`
> reports missing-credentials/`not_found` — fall back to **Claude Code** (the CLI),
> where it always runs with full local access.

> **Windows status.** The wiring is cross-platform: the helper writes
> `%APPDATA%\Claude\claude_desktop_config.json`, detects `python`/`python3`, and
> the server is pure stdlib. `execute_sql.py` skips the POSIX `chmod 600`
> credentials check on Windows (NTFS has no Unix mode bits; the file is guarded by
> your user profile's ACL instead). This path is validated on macOS but **not yet
> run end-to-end on a Windows box** — if you hit a snag there, please open an issue.

## Security model — no authentication, by design

A stdio MCP server has **no authentication**, and that is correct:

- The transport is a child process the client launches **as you**, communicating
  over OS pipes. The trust boundary is your OS user account — the server reads the
  same `<artifacts_dir>/local/credentials` you already have. There is nothing to authenticate
  *to*. (The MCP spec defines auth for the *HTTP* transport, not stdio.)
- It does **not** widen your attack surface beyond already having `psql` + a
  `.pgpass` on your laptop.
- **Read-only is enforced**: `execute_sql` rejects anything that isn't a single
  `SELECT` / `WITH...SELECT`. The gate (`sql_guard`) also blocks multi-statement SQL
  (including comment- and quoted-identifier-hidden bypasses), data-modifying CTEs,
  transaction-control / session-state / prepared statements, row-level locks, and
  dangerous server-side functions (`pg_read_file`, `lo_export`, `dblink`,
  `copy_program`, `pg_sleep`, …). It runs at the shared executor so the stdio server,
  the hosted HTTP server, the skills, and cron are all guarded identically (see
  `shared/sql-generation-rules.md → Safety Rules`).
- **It is stdio-only on purpose.** It never binds a network port. Doing so would
  create an *unauthenticated network listener* exposing query execution. If you
  need networked, multi-user serving with auth + RBAC + audit, that is the hosted
  Agami product — by design, not by omission.

## Local ↔ hosted: same interface, different backend

| | This local server (`agami serve`) | Hosted "Ask Agami" connector |
|---|---|---|
| Transport | stdio (local child process) | remote HTTPS (cloud-reachable) |
| Reachable from | Claude Code, Claude Desktop | Claude/Cowork/web/mobile, ChatGPT |
| Auth | none (OS user boundary) | OAuth/SSO + RBAC + audit |
| Model store | your local YAML files | shared multi-tenant registry |
| Evals | run `examples` in CI yourself | managed continuous evals + golden sets |

Because the tool surface matches, moving a team from local to hosted is a backend
swap — see [open-vs-hosted.md](open-vs-hosted.md).
