# Contributing to agami-core

Issues and PRs welcome at [github.com/AgamiAI/agami-core](https://github.com/AgamiAI/agami-core).

## Contributor License Agreement (CLA)

This project is **fair-code** (source-available) and is offered under a dual-license model: the [Agami Functional Use License](LICENSE) for the community, and a separate commercial license. For that model to hold, every external contribution has to come in with the rights that let us ship it under **both** licenses.

So before your first contribution can be merged, you sign a short **Contributor License Agreement** ([CLA.md](CLA.md)). In one sentence: you give Agami AI permission to license your contribution **on any terms — including the fair-code FUL and a commercial license** — and your contribution comes as is, without warranty or liability on your part. The full text is short; please read it — it includes a warranty/liability disclaimer.

**How to sign — no separate account, no form.** The first time you open a PR, our CLA bot comments on it with a link to the agreement and asks you to sign. You sign by replying with a single comment:

```
I have read the CLA Document and I hereby sign the CLA
```

The bot records your signature (stored in this repo) and flips the **CLA** status check to green; it stays green for all your future PRs. Until it's signed, the check blocks merge — that's the only thing it gates.

Maintainers and first-party (Agami AI) authors are allowlisted, so internal commits aren't gated; the CLA is for contributions from outside the organization.

## Running the checks locally

The same gate runs in CI on every PR — **ruff** (lint + format), the **test suite**, and
**gitleaks** (secret scan). To catch problems before you push, install the local hooks once.

**Prerequisite — install `uv` (the only thing you need globally):**

```bash
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# any platform, if you prefer a package manager:
#   brew install uv   ·   winget install astral-sh.uv   ·   pipx install uv
# docs: https://docs.astral.sh/uv/getting-started/installation/
```

Everything else rides on `uv` — there's nothing else to install globally. From the repo root, a
tiny cross-platform task runner (`dev.py`) wraps it all:

```bash
uv run dev.py setup     # once: wire the pre-commit hooks (ruff + gitleaks on commit, tests on push)
uv run dev.py check     # the whole gate locally — ruff + tests + gitleaks + conflict markers (same as CI)
uv run dev.py cover     # did the lines I changed get tested? (patch coverage)
```

`dev.py` shells out to `uvx` (which fetches ruff / pre-commit / pytest on demand), so it runs the
same on macOS, Linux, and Windows. Other tasks: `test`, `lint`, `fmt`. After `setup`, the hooks run
automatically — `ruff` + `gitleaks` on every **commit**, the test suite on every **push**. They're a
convenience (bypassable with `git commit --no-verify`); **CI is the real, unbypassable gate.**

Prefer the raw tools? `dev.py` is only a wrapper — the equivalents are below.

### Tests on their own

The suite imports the `agami-core` library, so install it editable with its `[model,server]` extras
(that pulls in `pydantic` / `pyyaml` / `sqlglot` plus the server deps; DB-driver tests skip cleanly
without a database). `uvx` wires it up for the run:

```bash
uvx --with pytest-cov --with-editable "packages/agami-core[model,server]" pytest tests/ -q
```

The privacy test (`tests/test_privacy_no_network.py`) is a contract: no shipped script may make a
network call — adding a network-egress primitive fails the build.

### Did I test the code I changed?

`uv run dev.py cover` reports coverage of **the lines your PR touched** (fails on changed lines that
no test exercises) — the quickest way to confirm a change is tested, regardless of overall coverage.
Under the hood that's:

```bash
uvx --with pytest-cov --with-editable "packages/agami-core[model,server]" \
  pytest tests/ -q --cov=plugins --cov=packages/agami-core/src --cov-report=xml
uvx diff-cover coverage.xml --compare-branch=origin/main
```

### End-to-end integration tests (Postgres + MySQL fixtures)

```bash
cd tests/integration
docker compose up -d
./test_postgres_e2e_cli.sh        # native CLI (psql)
./test_mysql_e2e_cli.sh
./test_postgres_e2e_duckdb.sh     # DuckDB (skipped if duckdb not on PATH)
docker compose down -v
```

## Version-bump discipline (read this before any release-shaped commit)

Claude Code's plugin marketplace caches each plugin **by version number**. The cache key for any user who installed `agami-core@1.1.0` is pinned to that version — Claude Code does not re-fetch source files until the version changes, even if the upstream `main` branch has moved on.

This has a real consequence: if we rename a skill, change file layouts, or remove a Bash invocation pattern from `.claude/settings.json` and **don't bump the version**, every user who installed an earlier version stays on the old code forever. They'll see stale slash commands, hit broken file paths, and have no obvious way to invalidate their cache short of deleting `~/.claude/plugins/cache/agami/agami-core/<old-version>/` by hand.

So: **bump the version on any commit that changes user-visible behavior.**

### When to bump what

The version lives in **four places across three files**. They must always match:

- `.claude-plugin/marketplace.json` — `metadata.version` and `plugins[0].version`
- `plugins/agami/.claude-plugin/plugin.json` — `version`
- `packages/agami-core/pyproject.toml` — `version`, the one published to PyPI. `release-pypi.yml`
  fails the publish when the release tag doesn't match it, so a bump that misses this file doesn't
  ship a wrong version — it doesn't ship at all.

Use semver:

| Change | Bump |
|---|---|
| Bug fix, doc-only change, internal refactor with no user-visible behavior shift | **patch** (1.1.0 → 1.1.1) |
| New feature, new skill, new database type, new optional flag — anything additive | **minor** (1.1.0 → 1.2.0) |
| Skill rename, file-layout change, removed flag, default behavior change, anything that breaks an existing install | **major** (1.1.0 → 2.0.0) |

Semver is the contract: a **major** bump is the signal that says "this will break your config — you'll need to reinstall / re-migrate". Don't fold a breaking change into a minor bump.

**The most common mistake** is renaming a skill (e.g., `init` → `agami-init`) or changing a file path (e.g., `<artifacts_dir>/local/<profile>/` → `<artifacts_dir>/<profile>/`) without bumping. Users installed at the old version see neither rename. Always bump on those changes.

### What "user-visible behavior" means

- Slash command name changed → bump
- File the skill writes moved → bump
- Allowlisted Bash command removed from `.claude/settings.json` → bump
- New required flag → bump
- New SKILL.md `when_to_use` trigger phrase added → patch (additive)
- New optional flag → patch (additive)
- Typo fix in a SKILL.md description → patch
- Test-only or scripts/README-only change → no bump needed

When in doubt, bump. Patch bumps are cheap; stale caches in the wild are not.

### Migrations are NOT a substitute for version bumps

We do auto-migrate on layout changes (e.g., v1.0 single-file → v1.1 directory → v1.2 split). That doesn't replace the version bump — it complements it. Without the bump, the new code that *does* the migration never reaches the user's machine. Bump first; migration code runs on next invocation.

## Files to touch on a release

When opening a PR that warrants a version bump, the checklist is:

1. `.claude-plugin/marketplace.json` — bump both `metadata.version` and `plugins[0].version` to the same string.
2. `plugins/agami/.claude-plugin/plugin.json` — bump `version`.
3. `packages/agami-core/pyproject.toml` — bump `version` to that same string.
4. Add a note to the PR description summarizing the user-visible change and which version it lands in.

Publishing is a **separate, deliberate step**: merging the bump ships nothing. `release-pypi.yml` and
`release-image.yml` both trigger on `release: published`, so PyPI and GHCR only move once a GitHub
release is cut with a `v<version>` tag matching `packages/agami-core/pyproject.toml`.

Record notable user-visible changes in [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog format) under the version that ships them.

## A community Discord will land soon

Once it's live, the link will appear here and in [`agami-connect/SKILL.md`](plugins/agami/skills/agami-connect/SKILL.md).
