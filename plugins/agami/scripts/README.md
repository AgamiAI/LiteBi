# Python helpers (skill-only)

The agami **library** — the unified executor (`execute_sql`), the MCP `TOOLS` harness
(`mcp_harness`), the `semantic_model` package, and `agami_paths` — lives in an installable
package at [`packages/agami-core/`](../../../packages/agami-core). Install it once:

```bash
pip install -e "packages/agami-core[model]"   # [model] pulls pydantic/sqlglot/pyyaml
```

This `scripts/` directory now holds **skill-only** helpers (rendering, setup/connect, reconcile,
parsing, the `sm` launcher) — they *import* the package; they are not library code.

## The runtime executor (now in the package)

The agami skill runs the Python-driver tier as a module against the installed package:

```bash
# Single-line SQL via --sql
python3 -m execute_sql --profile default --sql "SELECT COUNT(*) FROM orders"

# Multi-line / quote-heavy SQL via --sql-file (preferred for anything non-trivial)
python3 -m execute_sql --profile default --sql-file /tmp/agami-query.sql
```

It reads `<artifacts_dir>/local/credentials`, runs ONE SQL statement, and emits RFC 4180 CSV on
stdout (postgres / mysql / sqlite / …). Exit codes are documented in the module docstring; the
skill routes non-zero exits through the DB error classifier in `../shared/db_error_classifier.md`.

The local stdio MCP server (`agami serve`) is `python3 -m mcp_harness` — wired into Claude Desktop
by `setup_desktop_mcp.py` (below), which installs the package into the chosen interpreter and
registers `python -m mcp_harness`. See [`docs/mcp-server.md`](../../../docs/mcp-server.md).

## Skill-only helpers

| Script | What it does | Requires |
|---|---|---|
| `setup_desktop_mcp.py` | One-command wiring of the local MCP server into the Claude Desktop app (used by the `agami-serve` skill). Auto-detects the right Python interpreter, installs agami-core into it via `sm install`, and safely merges the entry into `claude_desktop_config.json` (timestamped backup + atomic write, preserving every other key). `--dry-run` previews. | stdlib (delegates install to `sm`) |
| `render_chart.py` | Substitutes `chart-template.html` placeholders programmatically. Used by the agami-query SKILL to produce HTML reports (Phase 4e). | stdlib only |
| `run_golden_eval.py` | Runs one golden dataset for a profile (used by the `agami-eval` skill): lists the profile's datasets with `--list`, else regenerates SQL per question, executes and scores each case, prints the verdicts failures-first as JSON, and writes the run artifact. Neither statement reaches stdout. | the agami-core package with the `model` extra (imports `semantic_model`) |
| `connect_resolve.py` · `setup_pgauth.py` · `promote_credentials.py` · `reconcile.py` · `parse_*.py` · `csv_to_sections.py` · `sm` (CLI launcher) · `build_duckdb_attach.py` (retired) | Connect/auth, model reconcile, parsing, and the `sm` launcher for `python -m semantic_model.cli`. | stdlib; some import the agami-core package |

## Install dependencies (only if you want the Python driver path)

```bash
pip install psycopg2-binary pymysql
```

That's it. No `agami` package on PyPI. The skill itself is just markdown + reference docs.

## Running a script directly

```bash
# Render a chart (single section)
python render_chart.py \
  --title "Top customers" \
  --summary "" \
  --section '{"title":"Top customers by spend","insights":"Carol Chen leads at $148.95.","chart_type":"bar","labels":["Carol Chen","Dave Davis","Bob Brown"],"datasets":[{"label":"Spend","data":[148.95,93.96,45.0]}],"table_headers":["Customer","Spend"],"table_rows":[["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]]}' \
  --out <artifacts_dir>/local/charts/top-customers.html
```

These helpers work without any other agami code installed — they read `<artifacts_dir>/local/credentials` and `<artifacts_dir>/local/.config` themselves.
