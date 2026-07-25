# Troubleshooting, permissions & uninstall

## Troubleshooting

| Symptom | Fix |
|---|---|
| `<artifacts_dir>/local/credentials must be chmod 600` | `chmod 600 <artifacts_dir>/local/credentials` |
| `psql: command not found` | `brew install postgresql` (or use DuckDB: `brew install duckdb`) |
| `mysql: command not found` | `brew install mysql` (or DuckDB) |
| `bq: command not found` | Install the [`gcloud` SDK](https://cloud.google.com/sdk/docs/install) and run `gcloud components install bq`. Or `pip install google-cloud-bigquery` for the Python path. |
| `snowsql` flag-guessing failures | Snowflake CLI is fussy about flag ordering; use the explicit invocation table in [connection-reference.md](../plugins/agami/shared/connection-reference.md). |
| `connection refused` on a remote DB | Check VPN / firewall, then connect with your native CLI (`psql -h ... -U ...` or `snowsql -a ... -u ...`) directly to confirm. |
| "I don't have a model for `<profile>`" | Tell agami "introspect my schema" or run `/agami-connect`. The skill picks up `AGAMI_PROFILE` automatically. |
| The generated SQL keeps using a column that doesn't exist | The model is stale. Run `/agami-connect reintrospect` — it preserves your hand-edits, refreshes from the DB, and surfaces any new entries in the review queue. See [When the database schema changes](usage.md#when-the-database-schema-changes-new-tables--new-columns--dropped-columns). |
| Just added new tables / columns / metrics to your DB | Run `/agami-connect reintrospect`. Same as above — hand-edits + sign-offs survive; new structure flows in; drifted entries flip to `stale`. |
| Query times out on a large table | Add a date filter or `LIMIT`; the skill flags HIGH-risk scans before running. |
| "agami warned that revenue isn't signed off" | The answer still came back — the warning just means the `revenue` metric is unreviewed. Open `/agami-model` (Review tab), check the metric (or fix its `calculation` if it's wrong), and approve it; the warning then disappears. |
| Validator rejects a hand-edited YAML | Read the error verbatim — it'll point at the exact line. Most common: a metric set to `approved` without `signed_off_by`/`signed_off_role`, or a non-empty `calculation`. |
| Want to switch profiles | `AGAMI_PROFILE=staging` then re-ask the question. |

If you hit a case not in the table, file an issue at
[github.com/AgamiAI/agami-core/issues](https://github.com/AgamiAI/agami-core/issues) with
the exact error, your DB type, and what the validator says
(`python3 -m semantic_model.cli validate ~/agami-artifacts/<profile>`).

## Reduce permission prompts (built in)

Claude Code prompts for permission the first time it runs a Bash command pattern.
agami ships its allowlist as part of the plugin's `.claude/settings.json` — when
you install agami via the marketplace, the host picks up these defaults
automatically. No copy-paste step needed.

The shipped allowlist covers the common agami invocation shapes: `psql` / `mysql`
/ `snowsql` with auth files, the bundled scripts (`execute_sql.py` /
`setup_pgauth.py` / `render_chart.py` / `build_duckdb_attach.py` / the
`semantic_model` package), `mkdir`/`chmod` on `<artifacts_dir>/local/` and
`~/agami-artifacts/`, `open` on chart files, and the GitHub-star ask URL. It does
NOT auto-allow arbitrary `psql` / `mysql` invocations against your DB — only the
wrapper scripts that read credentials safely.

To override per-user (e.g., to add commands you trust beyond agami), put them in
`~/.claude/settings.local.json` — Claude Code merges that on top of the shipped
allowlist. That file is gitignored; your additions stay private.

## Uninstalling

Removing the plugin via the Claude Code marketplace UI marks it disabled, but the
on-disk cache (and your data + settings) survive in case you reinstall later. To
fully clean up:

```bash
# 1. Optional: archive your tuned semantic model first (in case you come back)
tar czf ~/agami-backup-$(date +%Y%m%d).tar.gz ~/agami-artifacts   # model + local/ (credentials, config) — all in one folder now

# 2. Remove the plugin's on-disk cache (Claude Code doesn't auto-purge this)
rm -rf ~/.claude/plugins/cache/agami/agami-core
rm -rf ~/.claude/plugins/cache/agami-skills   # only if you also installed our earlier marketplace

# 3. Remove your data (only if you're sure you don't want it back)
#    Snapshot files are intentionally immutable — chmod first so rm can delete them.
chmod -R u+w ~/agami-artifacts 2>/dev/null
rm -rf ~/agami-artifacts                      # semantic model, examples, datasource.md, USER_MEMORY.md, .snapshots/, .git/
rm -rf <artifacts_dir>/local                  # credentials, .config, charts, exports, review + examples-validation dashboards

# 4. Restart Claude Code (full quit, not just close window)
```

If the slash commands `/agami-connect`, `/agami-query`, etc. still appear after
step 4, you have another cached copy at a different path.
`find ~/.claude -type d -name "agami-core"` will show every copy.
