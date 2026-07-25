# Privacy

**The short version:** used the normal way — as a skill inside Claude Code, or the local stdio server in Claude Desktop — agami makes no network calls of its own and has no telemetry. Your credentials, semantic model, query results, charts, and exports all stay in local files on your machine. (The self-hosted team server is a network service you deliberately stand up for your org — a separate, opt-in deployment, covered near the end.)

But agami works *through* an AI assistant — Claude Code, or Claude Desktop / claude.ai via the local MCP server — and that assistant is what turns your question into SQL. So the assistant sends the model provider (Anthropic, under its terms) the things any AI assistant would need: your question, the part of your semantic model it's reasoning over, the SQL, and the results it shows you. That's inherent to using an LLM — not something agami adds. This page is precise about who sees what.

---

## The local path makes no network calls

When you use agami locally — the skill scripts plus the on-machine library (the SQL executor and the stdio MCP server) — there is no outbound network call of any kind: no telemetry, no analytics, no install ping. `tests/test_privacy_no_network.py` enforces this: it scans every shipped skill script and the local library modules and fails the build if one gains a `curl` / `requests.post` / socket call. (It deliberately excludes the two **server** modules, `mcp_http` and `oidc` — the self-hosted team server *is* a network service by design; that's the opt-in deploy covered below, not the local path.)

So none of this is ever collected, transmitted, or logged by the local agami path:

- Your credentials, database hostnames, IPs, ports, tokens
- File paths, environment variables, working-directory contents, git history
- Email addresses, machine IDs, hardware fingerprints, stack traces, error logs
- Any usage counts or events — there is nothing to opt out of, because there's nothing there

---

## What your AI client sends to the model

Because agami writes SQL *with* an LLM, your AI client passes the model what it needs to do that. When you run agami inside Claude Code / Claude Desktop / claude.ai, that means Anthropic's API receives:

- your natural-language question,
- the slice of your **semantic model** it's working from — table and column names, descriptions, and metric definitions (the governed model agami built, not your whole database),
- the SQL agami generates, and
- the result rows the assistant shows you.

This is how every AI coding or analysis assistant works; agami neither adds nor removes it. What agami *does* do is keep that surface as small and controlled as possible:

- **You decide what the model can see.** The semantic model is the only schema the LLM works from — never a live connection to your database. Exclude any table or column you don't want queried, and mark columns `sensitive` so their raw values are never projected (they can still be counted or filtered).
- **Execution stays on your machine.** agami runs the generated SQL against your database locally. Your **database credentials and connection never go to the model** — only the model context above and the rows you'd see anyway.
- **Want nothing to leave at all?** Point your client — or a [self-hosted deploy](open-vs-hosted.md) — at a local or self-hosted model, and even the question and model context stay on your own infrastructure.

---

## What agami keeps local

Every byte agami reads or writes stays on your machine:

- **Credentials** (`<artifacts_dir>/local/credentials`) — chmod 600
- **Auth files** (`<artifacts_dir>/local/.pgpass`, `.mysql.cnf`, `.snowsql.cnf`) — chmod 600, written by `setup_pgauth.py`
- **Config** (`<artifacts_dir>/local/.config`) — `active_profile`, `tool_paths`, `reviewer_email`, `reviewer_role` (the artifacts-dir location lives in the `~/.config/agami/path` pointer)
- **Semantic model** (`datasource.yaml` + the `subject_areas/<area>/` tree under `<artifacts_dir>/<profile>/`; default `<artifacts_dir>` is `~/agami-artifacts/`)
- **Examples library** (`<artifacts_dir>/<profile>/examples.yaml`)
- **Datasource context** (`<artifacts_dir>/<profile>/datasource.md`) — your description of what the database represents, domain terminology
- **User memory** (`<artifacts_dir>/USER_MEMORY.md`) — your cross-database preferences
- **Query results** (everything the assistant shows you)
- **Query log** (`<artifacts_dir>/local/query_log.jsonl`) — your personal record of every query you ran
- **Charts** (`<artifacts_dir>/local/charts/<profile>/<ts>.html`)
- **CSV exports** (`<artifacts_dir>/local/exports/<profile>/<ts>.csv`)
- **Review + model-explorer + examples-validation dashboards** (`<artifacts_dir>/local/{review,model,examples-validation}/<profile>/<ts>.html`)
- **Snapshots** (`<artifacts_dir>/<profile>/.snapshots/<hash>/`) — immutable copies of past model versions for reproducibility
- **Curation log** (`<artifacts_dir>/<profile>/curation_log.jsonl`) — append-only audit trail of review actions
- **Corrections** (`<artifacts_dir>/<profile>/corrections.jsonl`) — append-only history of saved corrections

The skill never reads files outside those paths, with one carve-out: your DB tool's auth config (`~/.pg_service.conf`, `~/.snowsql/config`, etc.) is read when `setup_pgauth.py` materializes the auth files on first connect, with your permission.

---

## The GitHub-star prompt is not a network call

After your first successful query, `agami-query` asks once, in chat, whether you'd like to star the repo. It's just a prompt — choosing **Yes** runs `open https://github.com/AgamiAI/agami-core` (or the platform equivalent), handing that URL to your browser; your browser does the rest. agami sends nothing and never learns whether you actually starred (a star is public anyway). **Maybe later** and **Already starred** dismiss it for good. The choice is recorded in `<artifacts_dir>/local/.optins` so it doesn't repeat; to see it again, `rm <artifacts_dir>/local/.optins`.

---

## The optional local MCP server keeps the same guarantee

`agami serve` (`python -m mcp_harness`, in `packages/agami-core/src/mcp_harness.py`) lets you use agami from Claude Desktop. It changes nothing about the posture above:

- It speaks the MCP **stdio** transport — a child process of your AI client, reading/writing OS pipes. It **never binds a network port** and makes **no network call** of its own. `tests/test_mcp_harness.py` enforces this (the source is asserted to contain no socket/http/urllib/requests primitives).
- It reads only the local paths listed above and executes SQL locally via `execute_sql.py`. Only the rows you'd see anyway are returned to your client. (What that client then sends to the model is the same as the section above — it's still an LLM assistant.)
- It has **no authentication** because it needs none: the trust boundary is your OS user account. (Networked, authenticated, multi-user serving is the [self-hosted team server](open-vs-hosted.md).)

---

## The self-hosted team server (opt-in) — Early access (in testing)

If you [deploy the team server](../deploy/README.md) so your org can share one model, that server *is* a network service by design — it serves your model to your team over HTTPS. (This path is **early access** — usable today, newer than the local single-player experience; [feedback via a GitHub issue](https://github.com/AgamiAI/agami-core/issues) is welcome.) Your data still stays in your environment: it holds only the semantic model (never a live database connection), runs SQL locally against your own warehouse, and is **zero-egress by default**. The one thing that reaches out is **single sign-on**, if you turn it on: to verify a login the server calls Google/Microsoft (the identity provider). Leave SSO off — the default — and it makes no outbound call of its own.

---

## No telemetry

agami has **no telemetry** — no usage counts, no events, no install ping, no opt-in or opt-out. Earlier 0.x builds kept a vestigial (never-deployed, never-invoked) telemetry sample client + endpoint in the repo as historical artifacts; those were removed entirely. There is no network call anywhere in agami's codebase, and `tests/test_privacy_no_network.py` fails the build if any script introduces one. If a future version ever added telemetry, it would be opt-in with a full privacy doc — never a silent re-enable.
