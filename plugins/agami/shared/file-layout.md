# File layout — what lives where

Everything agami stores lives under **one folder the user chooses** — the *artifacts dir*. Inside it, the **committable model** sits next to a gitignored **`local/`** subfolder that holds secrets + per-user state, so you can check the model into a team repo without ever risking a credential leak, and there's no separate `~/.agami` to hunt for.

The only thing outside the folder is a one-line, non-sensitive **pointer** at `~/.config/agami/path` recording the folder's location (so a fresh shell can find it). `AGAMI_ARTIFACTS_DIR` overrides it.

## `<artifacts_dir>/local/` — secrets + per-user ephemeral state — **NEVER committed** (gitignored)

Everything in here is either a secret, an auth file derived from a secret, or per-user data that wouldn't make sense to share. It's the consolidated replacement for the old `~/.agami/`.

| Path (under `<artifacts_dir>/local/`) | What it is |
|---|---|
| `credentials` | INI file, chmod 600 — the only place credentials live |
| `.pgpass`, `.mysql.cnf`, `.snowsql.cnf` | Provider-native auth files materialized from credentials |
| `.config` | JSON — `active_profile`, `tool_paths`, `reviewer_email`, `reviewer_role` (NOT `artifacts_dir` — that's the pointer's job now) |
| `.optins` | JSON — GitHub-star ask state |
| `query_log.jsonl` | Personal record of every query you ran |
| `charts/<profile>/<ts>.html` | Per-query HTML reports |
| `exports/<profile>/<ts>.csv` | Per-query CSV exports |
| `{review,model,examples-validation,eval}/<profile>/<ts>.html` | Per-profile dashboards. The `eval` kind also holds a JSON run artifact per run — the answer key and the generated statement, side by side |
| `serve/`, `tunnels/` | The copied MCP server; SSH tunnel scripts |
| `.duckdb_init_*.sql` | Ephemeral, chmod-600 — federation init files, deleted after the query |

**Convention:** `<artifacts_dir>/.gitignore` excludes `local/` (agami writes this automatically). `local/` is **not** a dotfolder — it's visible/discoverable on purpose; the `.gitignore` (not hiding) is what keeps it out of commits.

## `<artifacts_dir>/` — the committable model — sharable

Everything else in here is non-secret and team-useful. The default location is `~/agami-artifacts/`, but the user picks it during `agami-connect (Phase 0a)` (see "Configuring the artifacts dir" below). The semantic model is a small tree of YAML files under `<artifacts_dir>/<profile>/` — `datasource.yaml` at the root plus a `subject_areas/<area>/` directory per subject area.

| Path | What it is |
|---|---|
| `USER_MEMORY.md` | Top-level — cross-database preferences (default filters, currency, display rules). Power users committing this share their tuning with the team. |
| `<profile>/datasource.yaml` | Org root: description, the `key_terminology` glossary, storage-connection + subject-area references, and cross-area relationships / entities / metrics |
| `<profile>/datasources/<connection>/storage.yaml` | Physical connection metadata (storage type + config; **no secrets** — references, not values) |
| `<profile>/subject_areas/<area>/subject_area.yaml` | Subject-area definition: name + the tables it exposes (TableRefs, with optional column-group scoping) |
| `<profile>/subject_areas/<area>/tables/<table>.yaml` | Canonical table (one per file): columns + types, primary-key grain, foreign keys, `column_groups`, choice fields, caveats, performance hints |
| `<profile>/subject_areas/<area>/metrics/<slug>.yaml`, `entities/<slug>.yaml` | One metric / entity per file |
| `<profile>/subject_areas/<area>/relationships.yaml` | In-area join edges (cardinality + trust block) |
| `<profile>/prompt_examples/<area>/examples.yaml` | Per-area NL→SQL few-shot library |
| `<profile>/golden_datasets/<name>.yaml` | The profile's golden datasets — each case a question, its answer key, and how strictly to compare. **Written by `/agami-save-golden`** (or by hand): a bank of questions imports unconfirmed, and an answer somebody verified is saved confirmed with its receipt. Append-only — a write that would change an item already there stops and shows the before and the after. `/agami-eval` reads these and never writes one |
| `<profile>/datasource.md` | Per-profile human narrative (the model-derived summary + glossary are assembled at read time, not stored here) |
| `<profile>/.snapshots/<hash>/` | Pinned model snapshots — an answer reproduces against the hash it ran on |

**Why a tree of small files rather than one big YAML:** the query path and the explorer lazy-load only what a question (or view) needs — one table's file, not a 1000-table monolith. Git diffs stay small (editing one table's metadata touches one small file), and the model never has to be parsed all at once. Relationships, metrics, and entities live at the **area** level, not inside a table file.

**Convention:** to share with a team, point `artifacts_dir` at a subdirectory of a git-tracked repo. Example: `~/code/myteam/data-stack/agami/` — checked in alongside dbt models, etc.

## Configuring `artifacts_dir`

The location is set once during `agami-connect (Phase 0a)` via an `AskUserQuestion`. The choice persists in the **pointer** at `~/.config/agami/path` (a one-line file holding the path) — NOT in `.config`, because `.config` itself now lives *inside* the artifacts dir (`<artifacts_dir>/local/.config`), so it can't also be where the location is stored.

### Resolution order (every skill + script follows the same chain)

1. **`AGAMI_ARTIFACTS_DIR` env var** — highest priority, for "this session only" overrides.
2. **`~/.config/agami/path` pointer** — the one line written by `agami-connect (Phase 0a)`, persists across sessions.
3. **Default**: `$HOME/agami-artifacts`.

The secrets/state dir is always `<artifacts_dir>/local/`; credentials are `<artifacts_dir>/local/credentials`; the config is `<artifacts_dir>/local/.config`.

### Resolving in bash

```bash
artifacts_dir="${AGAMI_ARTIFACTS_DIR:-}"
if [ -z "$artifacts_dir" ] && [ -f "$HOME/.config/agami/path" ]; then
  artifacts_dir="$(tr -d '\n' < "$HOME/.config/agami/path")"
fi
artifacts_dir="${artifacts_dir:-$HOME/agami-artifacts}"
local_dir="$artifacts_dir/local"   # credentials, .config, dashboards, logs
```

### Resolving in Python

Scripts import `agami_paths` (the single source of truth): `agami_paths.artifacts_dir()`, `.local_dir()`, `.credentials_path()`, `.config_path()`, etc. The semantic-model tools (`sm …` / `semantic_model.cli`) take the profile root (`<artifacts_dir>/<profile>/`) as a positional argument.

## Permissions

- `<artifacts_dir>/local/` is `chmod 700`. Files inside (`credentials`, `.pgpass`, …) are `chmod 600`.
- The rest of `<artifacts_dir>/` is `chmod 755` by default (sharable). Model files are `chmod 644`. **Don't `chmod 600` the artifacts dir** — that prevents teammates from reading a checked-in copy.

## Migration from a legacy (v1) profile

Older profiles used a per-schema layout (`index.yaml` + `<schema>/_schema.yaml` + per-table files at the profile root). On the first introspect after upgrade, the engine **auto-detects** any such legacy artifacts at the profile root, **moves them into `.legacy_backup/`** (so nothing is silently clobbered and the old model is recoverable), and writes the v2 tree (`datasource.yaml` + `subject_areas/…`) in their place. It's one-shot per profile and surfaces a one-liner when it happens. `USER_MEMORY.md` and credentials are untouched.

## Why this split exists

Three concrete wins:

1. **Zero credential-leak risk on commit.** Before, accidentally `git add <artifacts_dir>/local/` would commit the password. Now `<artifacts_dir>/local/` is gitignored by default and `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, datasource.md, and USER_MEMORY.md preferences. One command.
3. **Power users can override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session without touching the global config.
