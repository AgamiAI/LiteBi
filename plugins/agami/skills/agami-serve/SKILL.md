---
name: agami-serve
description: "Wires the local agami MCP server (python -m mcp_harness) into the Claude Desktop app in one step, so you can ask your database questions from Claude Desktop — not just inside Claude Code. Auto-detects the right Python interpreter (the one with your DB driver), installs the agami-core package into it so the registration survives plugin updates, and safely merges the entry into claude_desktop_config.json (backup + atomic write, preserving every other key). The local server is the mirror of the hosted Agami connector — same tools, local backend — so this is also how a developer feels the exact experience their business end-users would get."
when_to_use: "Use when the user says 'set up agami for Claude Desktop', 'use agami in the Claude app', 'hook up the MCP server', 'let me test what my end users would see', '/agami-serve', or otherwise wants agami available outside Claude Code. Requires agami-connect to have run first (needs credentials + a semantic model). NOT needed to use agami inside Claude Code — the skills already work there."
---

# agami serve — wire the local MCP server into Claude Desktop

You are setting up the local agami **MCP server** so the user can query their
database from the **Claude Desktop app** (or any client that reads a
`claude_desktop_config.json`-style file). This is the local mirror of the hosted
"Ask Agami" connector: same tool surface (`list_datasources`,
`get_datasource_schema`, `get_prompt_examples`, `execute_sql`),
but backed by the user's local model + local execution. Everything stays on the
machine; the server is stdio-only, has no auth, and makes no network call.

The heavy lifting is done by a deterministic helper —
[`scripts/setup_desktop_mcp.py`](../../scripts/setup_desktop_mcp.py). This skill's
job is to run it, read its output, and handle the two things that need a human:
a missing DB driver, and the app restart.

## Conversation style

- **Tight.** This is a one-shot setup tool, not a tutorial. Run the helper, report what it did, give the restart line.
- **Honest about surfaces.** Claude Desktop is the target here; if the user is already in Claude Code, remind them the skills already work there — the MCP server is for *other* clients.

---

## Phase −1: Plan-mode preflight

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill needs Bash (to run the helper, which writes files). If plan mode is active, refuse with: *"I can't wire up Claude Desktop in plan mode — it installs the agami-core package and writes your desktop config. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me."* **DO NOT write a plan file. DO NOT call `ExitPlanMode`.**

## Phase 0: Preflight

1. **Credentials present** — `<artifacts_dir>/local/credentials` must exist for the active profile. If missing, invoke `/agami-connect` first; the server needs a connection to execute against.
2. **Model present** — `<artifacts_dir>/<profile>/datasource.yaml` must exist (so the server has a semantic model to serve). If not, invoke `/agami-connect`.
3. **Profile** — default to the active profile. If `$ARGUMENTS` names a profile, pass it through as `--profile <name>`.

## Phase 1: Run the setup helper

Show the plan first with `--dry-run`, then do it for real:

```bash
# 1. Preview (writes nothing)
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_desktop_mcp.py" --dry-run

# 2. Apply
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_desktop_mcp.py"
```

Pass `--profile <name>` if the user named one. (Installation goes through `sm install`,
which does an editable install automatically when run from a dev checkout — no flag needed.)

## Phase 2: Handle the two human cases

- **Driver missing (exit code 3).** The helper couldn't find a Python that can
  import the DB driver. Relay its suggested `pip install ...` line, offer to run
  it, then re-run the helper. Example: `python3 -m pip install psycopg2-binary`.
- **Wrote successfully.** Tell the user, in one or two lines:
  1. **Fully quit** the Claude Desktop app (Cmd+Q on macOS — not just close the window), then reopen it.
  2. In Claude Desktop, ask *"What datasources does agami see?"* — it should call `list_datasources` and return their profiles.
  3. If it doesn't appear, the log is at `~/Library/Logs/Claude/mcp-server-agami.log`.

## Phase 3: Orient them (one short paragraph)

Close with: this is the same toolset their business end-users would use through
the hosted Agami connector — the only difference is the backend (their local
files vs a shared, governed, always-on cloud). So what they just felt in Claude
Desktop *is* the end-user experience. If they want it for a team (one shared
model, evals, governance, reachable from web/mobile/Cowork), that's the hosted
product — see `docs/open-vs-hosted.md` in the repo
(https://github.com/AgamiAI/agami-core/blob/main/docs/open-vs-hosted.md).

## Notes

- The setup is **idempotent** — re-running updates the `agami` entry in place and
  re-installs the agami-core package. Re-run after a plugin update to refresh it.
- It **never** clobbers other config: every other key and MCP server is preserved,
  and the previous config is backed up to a timestamped `.bak-<epoch>` file.
- It does **not** touch credentials and adds **no** network surface (see
  `docs/privacy.md` in the repo).
