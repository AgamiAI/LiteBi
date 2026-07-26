"""Load the local YAML semantic model into the serving Postgres — the deploy's `load model` step.

The hosted server serves the model **from the database** (`tools._serve_model` reads the DB when one is
configured), so a deploy has to push the YAML model into Postgres. The read→write primitives already exist
and are idempotent; this is the orchestrator that wires them for every datasource under the artifacts dir.
Run by the deploy entrypoint and re-run on restart to pick up an edited model.

    AGAMI_DB_URL=postgresql://…  AGAMI_ARTIFACTS_DIR=/…/agami-artifacts  python -m model_deploy [datasource …]
"""

from __future__ import annotations

import sys
from pathlib import Path

import agami_paths
import model_store
from store import Store


def _default_org() -> str:
    """The org to deploy under when the caller names none. A CLI has no request, so it calls the SAME
    resolver the server's read path uses (`tools.resolved_org_id`: AGAMI_ORG_ID -> the minted uuid in
    org.yaml -> 'local') — the two MUST agree, or the model is written under one org and read under
    another and the server sees no model (F14 / ACE-056)."""
    from tools import resolved_org_id  # lazy: keeps the deploy CLI's import surface small

    return resolved_org_id()


# Every tenant-scoped table carrying `org_id` — the serving model (9), the append-only runtime logs (2),
# and the user roster (1, added by 012). The backfill moves legacy 'local' rows across all of them.
_BACKFILL_TABLES = (
    "datasource_model", "subject_area", "model_table", "metric", "entity",
    "relationship", "prompt_example", "memory", "model_version",  # serving
    "query_executions", "tool_calls",                             # runtime (append-only)
    "users",                                                      # auth roster
)


def _backfill_org_id(store: Store, org_id: str) -> None:
    """Move rows written under the legacy 'local' sentinel onto the resolved minted `org_id`
    (F14 / ACE-057). Runs once at boot, right after migrations, so an EXISTING deployment that ran
    under 'local' before this feature adopts its minted id instead of orphaning those rows.

    Why an UPDATE-move and not a re-seed: `model_store.write_organization`'s redeploy DELETE is scoped
    to (org_id, datasource), so re-deploying under a NEW org_id would leave the old 'local' serving
    rows behind (doubled); the runtime tables are append-only and can only be corrected by an UPDATE.
    Idempotent + safe: a no-op when the target is still 'local' (a pre-F14 / un-minted deployment), and
    `WHERE org_id='local'` matches zero rows once moved, so re-runs do nothing. Never touches
    `username`, so the users UNIQUE index can't trip."""
    if org_id == "local":
        return
    for tbl in _BACKFILL_TABLES:
        store.execute(f"UPDATE {tbl} SET org_id = ? WHERE org_id = 'local'", (org_id,))
    store.commit()


def deploy_one(store: Store, datasource: str, profile_dir: Path, org_id: str | None = None) -> None:
    """Load one datasource's per-datasource model (org + examples + ORGANIZATION.md + version) from
    `profile_dir` into the store. The install-global `USER_MEMORY.md` is handled once per run, separately
    (`_deploy_user_memory`) — it lives at the artifacts ROOT, not per profile.

    Reads + parses **everything first, then writes** — so a malformed model fails *before* any DB write
    rather than half-way through. Each model_store writer is individually idempotent (clear-then-insert), so
    a clean re-run fully overwrites a datasource; the entrypoint fail-closes on a non-zero exit, so a partial
    write from a rare mid-write DB error is never served and self-heals on the next deploy. (The writers commit
    individually, so this isn't one transaction — load-then-write is the practical guard.)"""
    from semantic_model import loader
    from semantic_model.snapshot import newest_version

    org_id = org_id if org_id is not None else _default_org()
    # --- read + parse everything first (where malformed input fails, before any write) ---
    org = loader.load_organization(profile_dir)
    # Examples live per subject area (prompt_examples/<area>/examples.yaml); tag each with its area so the
    # served row carries it (write_examples reads ex["area"]). A malformed examples file for one area is
    # skipped with a warning, not fatal — examples are best-effort few-shots, not the model itself, and a bad
    # one shouldn't block the team's model from deploying.
    examples: list[dict] = []
    for sa in org.subject_areas:
        try:
            area_examples = loader.list_prompt_examples(profile_dir, sa.name)
        except Exception as e:  # noqa: BLE001 — a bad examples file mustn't abort the model deploy
            print(f"model_deploy: skipping malformed examples for area {sa.name!r}: {e}", file=sys.stderr)
            continue
        examples.extend({**ex, "area": ex.get("area") or sa.name} for ex in area_examples if isinstance(ex, dict))
    org_md = profile_dir / "ORGANIZATION.md"
    org_text = org_md.read_text() if org_md.exists() else None
    version = newest_version(profile_dir) or "deployed"

    # --- then write (version last, so its presence marks a completed deploy) ---
    model_store.write_organization(store, datasource, org, org_id=org_id)
    # Always write examples (even []) so a redeploy after REMOVING examples actually clears the stale rows —
    # write_examples is clear-then-insert, so an empty list replaces the datasource's examples with none.
    model_store.write_examples(store, datasource, examples, org_id=org_id)
    model_store.write_memory(
        store, datasource, organization=org_text, org_id=org_id
    )  # per-datasource
    model_store.write_model_version(store, datasource, version, org_id=org_id)
    store.commit()


def _deploy_user_memory(store: Store, artifacts_dir: Path, org_id: str | None = None) -> None:
    """USER_MEMORY.md is **cross-datasource** (one row per org, keyed by the global sentinel inside
    write_memory) and lives at the artifacts ROOT — not per profile — matching how the server reads it
    (`tools._context_sources` → `artifacts/USER_MEMORY.md`). Written once per run; absent ⇒ nothing to do."""
    f = artifacts_dir / "USER_MEMORY.md"
    if f.exists():
        org_id = org_id if org_id is not None else _default_org()
        model_store.write_memory(store, "", user=f.read_text(), org_id=org_id)  # global user row
        store.commit()


def _deploy_org_record(store: Store, artifacts_dir: Path, org_id: str | None = None) -> None:
    """Derive the deployment-level org record (F15 / ACE-067's `<artifacts_dir>/organization.yaml`) into
    the one `organization` row — company-wide context shared across every datasource, written ONCE per
    run at the artifacts ROOT (like USER_MEMORY.md), never per datasource. Absent record ⇒ nothing to do
    (a pre-F15 deployment; composition degrades to per-profile). No tenant-row backfill — F14/ACE-057
    already stamped `org_id`; this only upserts the single org row (FK-safe, see write_organization_record)."""
    from semantic_model import org_record as OR  # lazy: keeps the deploy CLI's import surface small

    record = OR.load_org_record(artifacts_dir)
    if record is not None:
        # Refresh the datasource list from disk so the derived row reflects what's actually deployed.
        # Only when a record already exists — an absent record still degrades (no mint at deploy time).
        record = OR.refresh_datasources(artifacts_dir) or record
        org_id = org_id if org_id is not None else _default_org()
        model_store.write_organization_record(store, record, org_id=org_id)
        # The company NARRATIVE is prose, not structured — it lives in a company-level `memory` row
        # (datasource='' sentinel, like USER_MEMORY.md) so the served two-level context can read it.
        narrative = OR.narrative_path(artifacts_dir)
        if narrative.exists():
            model_store.write_memory(store, "", organization=narrative.read_text(), org_id=org_id)


def deploy_models(store: Store, artifacts_dir: Path, org_id: str | None = None) -> list[str]:
    """Load every datasource model under `artifacts_dir` (a *directory* with an `org.yaml`) into the store.
    Returns the datasources loaded. The `local/` dir (gitignored secrets/state) and any non-directory or
    org.yaml-less entry are skipped — `local/` explicitly, so a stray `local/org.yaml` can't deploy from the
    secrets dir."""
    org_id = org_id if org_id is not None else _default_org()
    loaded: list[str] = []
    for prof in sorted(
        p
        for p in artifacts_dir.iterdir()
        if p.is_dir() and p.name != agami_paths.LOCAL_SUBDIR and (p / "org.yaml").exists()
    ):
        deploy_one(store, prof.name, prof, org_id=org_id)
        loaded.append(prof.name)
    return loaded


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    store = Store.from_env()
    if store is None:
        print("model_deploy: no database configured (set AGAMI_DB_URL).", file=sys.stderr)
        return 2
    # Ensure the model tables exist before writing — this runs in the deploy entrypoint *before* the
    # server (whose lifespan auto-migrates) is up, so the schema may not be applied yet.
    # run_migrations is idempotent, so the later lifespan pass is a harmless no-op.
    store.run_migrations()
    # Move any legacy 'local' rows onto this deployment's minted org_id BEFORE deploying — so the
    # redeploy's (org_id, datasource)-scoped overwrite lines up with the just-moved serving rows
    # instead of orphaning them. No-op on a fresh or un-minted ('local') deployment.
    _backfill_org_id(store, _default_org())
    artifacts_dir = agami_paths.artifacts_dir()
    if not artifacts_dir.is_dir():  # clean exit, not an uncaught FileNotFoundError from iterdir()
        store.close()
        print(f"model_deploy: artifacts dir not found: {artifacts_dir}", file=sys.stderr)
        return 1
    try:
        if args:  # deploy only the named datasources
            loaded: list[str] = []
            for ds in args:
                prof = artifacts_dir / ds
                if not (prof / "org.yaml").exists():
                    print(
                        f"model_deploy: no model for datasource {ds!r} at {prof}/org.yaml",
                        file=sys.stderr,
                    )
                    return 1
                deploy_one(store, ds, prof)
                loaded.append(ds)
        else:  # deploy every model under the artifacts dir
            loaded = deploy_models(store, artifacts_dir)
            if not loaded:
                print(
                    f"model_deploy: no model found under {artifacts_dir} "
                    "(a datasource is a subdir with an org.yaml).",
                    file=sys.stderr,
                )
                return 1
        _deploy_user_memory(store, artifacts_dir)  # install-global USER_MEMORY.md, once
        _deploy_org_record(store, artifacts_dir)  # deployment-level company record, once (F15)
    except Exception as e:  # noqa: BLE001 — any load/write failure is a clean fail-closed exit, not a traceback
        print(f"model_deploy: failed: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"model_deploy: loaded model for: {', '.join(loaded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
