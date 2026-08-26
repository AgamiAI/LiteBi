# Invocation conventions — read this BEFORE telling the user to invoke a skill

agami ships eight skills, all prefixed `agami-` to avoid colliding with Claude Code's built-in slash commands (e.g. `/init`) and with other plugins:

| Skill | Slash command | Natural-language triggers (via `when_to_use`) |
|---|---|---|
| agami-connect | `/agami-connect` | "set up agami", "connect to my database", "introspect the schema", "reload the schema", "reintrospect", "add a new database" |
| agami-query | `/agami-query` | any data question — "how many", "show me", "top N", "trend over time", etc. |
| agami-model | `/agami-model` | "open the model explorer", "show me the model", "exclude a table", "remove this column" — AND review/sign-off: "open the review dashboard", "review my model", "sign off the metrics", "approve N", "walk the review queue" (its Review tab absorbed the former `/agami-review`) |
| agami-save-correction | `/agami-save-correction` | "save this as a correction", "remember this", "use this SQL next time" |
| agami-reconcile | `/agami-reconcile` | "reconcile against my dashboard", "verify these numbers against agami" |
| agami-eval | `/agami-eval` | "run the evals", "run the golden dataset", "score the model against my golden questions", "did anything regress?", "how accurate is agami on my data?" |
| agami-serve | `/agami-serve` | "set up agami for Claude Desktop", "use agami in the Claude app", "hook up the MCP server", "let me test what my end users would see" |
| agami-deploy | `/agami-deploy` | "deploy agami", "self-host agami", "set up the agami server for my team", "host agami on a VM" (early access — in testing) |

**`/agami-init` no longer exists** — its credential-setup flow was folded into `/agami-connect` Phase 0a. Users who haven't run setup before invoke `/agami-connect`; the skill detects missing credentials and runs the DB-type picker + writes `<artifacts_dir>/local/credentials.example` inline before introspecting.

## What works

| Form | Notes |
|---|---|
| `/agami-<skill>` (e.g. `/agami-connect`) | Works as a bare slash command across hosts. The `agami-` prefix is what makes them safe. |
| Plain natural language | Each skill's `when_to_use` carries trigger phrases. The model routes correctly without an explicit slash command. **Prefer this in chat** — it reads more naturally. |
| `@agami` at-mention (some hosts) | Some Claude Code hosts autocomplete `@agami` to the skill list. Don't assume it works on every host. |

## What does NOT work — never write these in user-facing text

The model often reaches for slash patterns it's seen on other plugins. None of these exist in users' installations:

- `/agami:connect`, `/agami:query-database`, etc. — colon-namespaced forms **do not exist**.
- `/init`, `/connect`, `/query-database`, etc. — bare forms without the `agami-` prefix. **`/init` collides with Claude Code's built-in `/init`** (which generates a CLAUDE.md), so we explicitly avoid it. The others are too generic and collide with other plugins.
- `/agami-init` — **deprecated and removed.** Its flow now lives at `/agami-connect` Phase 0a. If you see references in older docs, ignore them.
- `/agami-review` — **removed.** Its trust-review dashboard was folded into `/agami-model` (the Review tab). Use `/agami-model` (or `/agami-model review` to open straight on the sign-off queue). Ignore older references.
- `agami init`, `agami connect` (no slash, no @) — **do not exist** as commands.
- `@agami:init`, `@agami:connect` — colon-namespaced @-forms **do not exist**.

If you're tempted to write any of those, stop and re-read this doc.

## How to phrase guidance to the user

For most chat replies, **prefer natural-language phrasing over slash commands** — it reads better and the `when_to_use` matcher routes correctly.

| Instead of… | Say… |
|---|---|
| "Run `/agami-connect reintrospect`" | "Say 'reload the schema' and I'll re-introspect from your DB." |
| "Run `/agami-save-correction`" | "Say 'save this as a correction' and I'll add it to the examples library." |
| "Run `/agami-review`" (removed) | "Say 'open the review dashboard' to walk the sign-off queue." (routes to `/agami-model`'s Review tab) |
| "Type `/agami-query`" | Just answer the question directly. Slash commands are unnecessary. |

## When the model invents a new form

If you find yourself about to write `/init`, `/connect`, `/agami:connect`, `/agami connect`, or any other variation, stop. Either use the `agami-` prefix (e.g. `/agami-connect`) or use natural language. There is no third option.

## Passing JSON to `sm` (`--ops-file` / `--file`) — always write it with the Write tool

Every `sm` command that takes a JSON file (`curate --ops-file`, `add --file`,
`add-example --file`, `seed-examples --file`, `format-table --units`) expects a real
JSON file. **Create it with the Write tool — never a heredoc, a shell variable
(`printf '%s' "$OPS"`), or `python3 -c`.** JSON quotes get mangled by shell quoting,
and JSON `null` / `true` / `false` are not valid Python — pasting a review-items entry
(which has `null` fields) into `python3 -c` is what produces
`NameError: name 'null' is not defined`. The Write tool writes the bytes literally, so
quotes and `null` survive. Keep ops minimal (the locator + action), not whole items.
