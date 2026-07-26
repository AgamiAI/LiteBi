"""
Tests for plugins/agami/scripts/prepare_deploy.py — the `/agami-deploy` bundle scaffolder.

It copies the carried bundle templates, stages the model artifacts, and writes an `agami.env` file with the
NON-SECRET values only. The contract these tests pin: a complete bundle is produced, the right
COMPOSE_PROFILES land per toggle, no password ever passes through the helper, an existing `agami.env` is
preserved, and the generated `agami.env` passes deploy_preflight.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import deploy_preflight  # noqa: E402
import prepare_deploy  # noqa: E402


def _artifacts(tmp_path: Path) -> Path:
    """A minimal local artifacts dir: a `local/credentials` (chmod 600) + one model profile."""
    art = tmp_path / "agami-artifacts"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text("[demo]\nhost = db.example\n", encoding="utf-8")
    creds.chmod(0o600)
    (art / "demo").mkdir()
    (art / "demo" / "datasource.yaml").write_text("name: demo\n", encoding="utf-8")
    return art


def _args(target: Path, artifacts: Path, **over) -> argparse.Namespace:
    base = dict(
        target=str(target),
        artifacts_dir=str(artifacts),
        public_base_url="https://agami.acme.example",
        admin_email="you@example.com",
        admin_first="Alex",
        admin_last="Kim",
        profiles="bundled-db,edge",
        image_tag=None,  # match the CLI default (None → fresh builds pin 'latest'; a re-run leaves the pin)
        datasources=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_prepares_a_complete_bundle(tmp_path):
    target = tmp_path / "bundle"
    status, code = prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    assert code == 0 and status.startswith("PREPARED ")

    # The carried templates are all present + the image-based compose pulls (no build context).
    for name in ("docker-compose.yml", "Caddyfile", "deploy.sh", "README.md", "agami.env"):
        assert (target / name).exists(), name
    compose = (target / "docker-compose.yml").read_text()
    assert "image: ghcr.io/agamiai/agami-core:" in compose
    assert "build:" not in compose

    # The MODEL is staged so the bundle is self-contained + shippable...
    assert (target / "artifacts" / "demo" / "datasource.yaml").exists()
    # ...but no secret travels: `local/` (credentials + .pgpass) is excluded entirely (creds come from
    # DATASOURCE_URL in agami.env now), so no 600-mode file lands in a shippable bundle or a mounted volume.
    assert not (target / "artifacts" / "local").exists()
    assert list(target.glob("artifacts/**/credentials")) == []


def test_config_is_visible_agami_env_wired_through_compose_and_deploy(tmp_path):
    """The editable config is `agami.env` (visible in Finder), NOT a hidden `.env`; and the shipped compose
    + deploy.sh reference it (compose won't auto-load a non-`.env` name, so it goes via env_file/--env-file)."""
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    assert (target / "agami.env").exists()
    assert not (target / ".env").exists()
    assert "env_file: agami.env" in (target / "docker-compose.yml").read_text()
    assert "--env-file agami.env" in (target / "deploy.sh").read_text()


def test_staged_compose_caps_container_logs(tmp_path):
    """The shipped compose caps per-container log growth — the default json-file driver is unbounded and
    would slowly fill a small VM disk on a long-running deploy."""
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    compose = (target / "docker-compose.yml").read_text()
    assert "logging:" in compose and 'max-size: "10m"' in compose and 'max-file: "3"' in compose


def test_local_secrets_are_never_staged(tmp_path):
    """`local/` — credentials AND .pgpass — must not enter a shippable bundle (the field tester had to
    chown both to the container uid; with them gone, neither exists to fix)."""
    art = _artifacts(tmp_path)
    pgpass = art / "local" / ".pgpass"
    pgpass.write_text("db.example:5432:*:u:p\n", encoding="utf-8")
    pgpass.chmod(0o600)
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, art))
    assert not (target / "artifacts" / "local").exists()
    assert list(target.glob("artifacts/**/credentials")) == []
    assert list(target.glob("artifacts/**/.pgpass")) == []


def test_rerun_purges_a_stale_local_from_an_older_bundle(tmp_path):
    """A bundle made by an OLDER prepare_deploy staged `local/` (with credentials). Re-running must
    delete that stale secret — `ignore=` only skips copying, and `dirs_exist_ok` merges, so without an
    explicit purge the old credentials file would linger in the mounted volume."""
    art = _artifacts(tmp_path)
    target = tmp_path / "bundle"
    stale = target / "artifacts" / "local"
    stale.mkdir(parents=True)
    (stale / "credentials").write_text("[demo]\nhost = stale\n", encoding="utf-8")
    prepare_deploy.prepare(_args(target, art))
    assert not (target / "artifacts" / "local").exists()


def test_staged_model_is_world_readable(tmp_path):
    """The container runs as a different uid and mounts the model read-only — every staged dir must be
    world-traversable and every file world-readable, else the boot-time load crashes (issue #1's fix)."""
    art = _artifacts(tmp_path)
    # Simulate a restrictive owner-only source (umask 077); the widening must repair it in the bundle.
    (art / "demo" / "datasource.yaml").chmod(0o600)
    (art / "demo").chmod(0o700)
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, art))
    prof = target / "artifacts" / "demo"
    assert stat.S_IMODE(prof.stat().st_mode) & 0o005 == 0o005          # dir: others r-x (traversable)
    assert stat.S_IMODE((prof / "datasource.yaml").stat().st_mode) & 0o004     # file: others readable


def test_widen_does_not_chmod_through_a_dir_symlink(tmp_path):
    """The widening must never follow a directory symlink out of the bundle and chmod external files
    (rglob('**') would on Python ≤3.12; os.walk(followlinks=False) does not)."""
    art = _artifacts(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0o600)
    # A directory symlink inside the model pointing at the external dir (copied into the bundle as a link).
    (art / "demo" / "link").symlink_to(external, target_is_directory=True)
    prepare_deploy.prepare(_args(tmp_path / "bundle", art))
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600  # untouched — not widened through the symlink


def test_env_ships_a_datasource_url_hint_left_unset(tmp_path):
    """The warehouse DSN is a Phase-2 hand-off (a credential): agami.env carries a commented hint, and the
    helper never sets it to a value (same discipline as APP_DATABASE_URL / the admin password)."""
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    env = (target / "agami.env").read_text()
    assert "# DATASOURCE_URL=" in env       # the hint ships
    # No line assigns DATASOURCE_URL a live value (commented hint only) — checked per-line so it holds
    # even at the start of the file, and doesn't false-match the `DATASOURCE_URL__<NAME>` form.
    assert not any(ln.lstrip().startswith("DATASOURCE_URL=") for ln in env.splitlines())


def test_env_has_non_secrets_and_a_blank_password(tmp_path):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    env = (target / "agami.env").read_text()
    assert "PUBLIC_BASE_URL=https://agami.acme.example" in env
    assert "AGAMI_ADMIN_USERNAME=you@example.com" in env
    assert "AGAMI_ADMIN_FIRST_NAME=Alex" in env
    assert "AGAMI_ADMIN_LAST_NAME=Kim" in env
    # The password is left for the user to type into the file — never filled by the helper.
    assert "AGAMI_ADMIN_PASSWORD=\n" in env or env.rstrip().endswith("AGAMI_ADMIN_PASSWORD=")
    # And no signing secret yet — deploy_preflight generates that.
    assert "AGAMI_SIGNING_SECRET=" not in env


def test_env_is_chmod_600(tmp_path):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    mode = stat.S_IMODE((target / "agami.env").stat().st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.parametrize(
    "profiles",
    ["bundled-db,edge", "bundled-db,tunnel", "edge"],
)
def test_tier2_toggles_set_profiles(tmp_path, profiles):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path), profiles=profiles))
    env = (target / "agami.env").read_text()
    assert f"COMPOSE_PROFILES={profiles}" in env
    # APP_DATABASE_URL is a credential — the helper never writes it (the user edits agami.env by hand); the
    # external-DB case ships it only as the commented template hint.
    assert "\nAPP_DATABASE_URL=" not in env


def test_set_key_uncomments_a_hint_and_avoids_duplicates_and_prefixes():
    # A commented hint line is uncommented + set (no duplicate APP_DATABASE_URL appended).
    out = prepare_deploy._set_key("# APP_DATABASE_URL=hint\nOTHER=1\n", "APP_DATABASE_URL", "real")
    assert out.count("APP_DATABASE_URL=") == 1
    assert "APP_DATABASE_URL=real" in out and "# APP_DATABASE_URL=hint" not in out
    # A prefix key must not match a longer one.
    out2 = prepare_deploy._set_key("AGAMI_IMAGE_TAG_X=keep\n", "AGAMI_IMAGE_TAG", "latest")
    assert "AGAMI_IMAGE_TAG_X=keep" in out2 and "AGAMI_IMAGE_TAG=latest" in out2


def test_helper_takes_no_password_argument(tmp_path):
    """Criterion: a secret never travels on the command line. Passing --password must be rejected."""
    with pytest.raises(SystemExit):
        prepare_deploy.main(
            [
                "--target", str(tmp_path / "b"),
                "--artifacts-dir", str(_artifacts(tmp_path)),
                "--public-base-url", "https://h.example",
                "--admin-email", "you@example.com",
                "--admin-first", "A", "--admin-last", "K",
                "--password", "hunter2",  # not a real option — argparse must error out
            ]
        )


def test_rerun_upgrades_in_place_and_preserves_typed_secrets(tmp_path):
    """A re-run over an existing bundle UPGRADES non-destructively: the typed password + generated secret
    values are preserved (line endings normalize to LF), and the status is UPGRADED (not a fresh PREPARED)."""
    target = tmp_path / "bundle"
    art = _artifacts(tmp_path)
    prepare_deploy.prepare(_args(target, art))
    env_path = target / "agami.env"
    env_path.write_text("AGAMI_ADMIN_PASSWORD=typed-by-user\nAGAMI_SIGNING_SECRET=deadbeef\n", encoding="utf-8")
    status, code = prepare_deploy.prepare(_args(target, art))
    assert code == 0 and status.startswith("UPGRADED ")
    kept = env_path.read_text()
    assert "AGAMI_ADMIN_PASSWORD=typed-by-user" in kept
    assert "AGAMI_SIGNING_SECRET=deadbeef" in kept


def test_main_prints_status_and_returns_zero(tmp_path, capsys):
    code = prepare_deploy.main(
        [
            "--target", str(tmp_path / "b"),
            "--artifacts-dir", str(_artifacts(tmp_path)),
            "--public-base-url", "https://h.example",
            "--admin-email", "you@example.com",
            "--admin-first", "A", "--admin-last", "K",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.startswith("PREPARED ")


def test_no_artifacts_is_a_clean_error(tmp_path):
    target = tmp_path / "bundle"
    empty = tmp_path / "empty-artifacts"
    empty.mkdir()
    status, code = prepare_deploy.prepare(_args(target, empty))
    assert code == 1 and status.startswith("ERROR ") and "artifacts" in status


def test_target_inside_artifacts_is_rejected(tmp_path):
    """Else copytree(artifacts -> target/artifacts) would recurse into the bundle it just created."""
    art = _artifacts(tmp_path)
    status, code = prepare_deploy.prepare(_args(art / "nested-bundle", art))
    assert code == 1 and status.startswith("ERROR ") and "must not be inside" in status


def test_generated_env_passes_deploy_preflight(tmp_path):
    """The agami.env (once a password is set) satisfies deploy_preflight: secret generated, host derived."""
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    env_path = target / "agami.env"
    # The user supplies the password by editing the file (here, simulated).
    env_path.write_text(
        env_path.read_text().replace("AGAMI_ADMIN_PASSWORD=", "AGAMI_ADMIN_PASSWORD=a-strong-pw"),
        encoding="utf-8",
    )
    errors = deploy_preflight.prepare_env(env_path)
    assert errors == [], errors
    after = env_path.read_text()
    assert "AGAMI_SIGNING_SECRET=" in after
    assert "AGAMI_PUBLIC_HOST=agami.acme.example" in after


# --- ACE-031: upgrade-aware re-run (_merge_env) + multi-datasource selection ---

_TEMPLATE = (
    "COMPOSE_PROFILES=bundled-db,edge\n"
    "AGAMI_IMAGE_TAG=latest\n"
    "PUBLIC_BASE_URL=https://agami.your-domain.example\n"
    "AGAMI_ADMIN_PASSWORD=\n"
    "# DATASOURCE_URL=postgresql://<user>:<password>@your-warehouse.example:5432/analytics?sslmode=require\n"
    "# APP_DATABASE_URL=postgresql://user@your-managed-pg.example:5432/agami?sslmode=require\n"
)


def test_merge_preserves_secrets_and_surfaces_new_keys():
    existing = (
        "AGAMI_ADMIN_PASSWORD=typed-by-user\n"
        "AGAMI_SIGNING_SECRET=deadbeef\n"
        "PUBLIC_BASE_URL=https://me.example\n"
    )
    merged, new_keys = prepare_deploy._merge_env(existing, _TEMPLATE, None)
    # Existing values are byte-preserved — including a password even though the template ships it blank.
    assert "AGAMI_ADMIN_PASSWORD=typed-by-user" in merged
    assert "AGAMI_SIGNING_SECRET=deadbeef" in merged
    assert "PUBLIC_BASE_URL=https://me.example" in merged
    assert merged.count("PUBLIC_BASE_URL=") == 1  # not re-appended
    # Keys new in this version are appended + reported; an already-present key is neither.
    assert "DATASOURCE_URL" in new_keys and "COMPOSE_PROFILES" in new_keys
    assert "# DATASOURCE_URL=" in merged
    assert "AGAMI_ADMIN_PASSWORD" not in new_keys and "PUBLIC_BASE_URL" not in new_keys


def test_merge_recognizes_an_indented_commented_key_and_wont_duplicate():
    """An indented `  # KEY=…` in the existing file must be seen as present, else the template's KEY would
    be re-appended (a duplicate). Guards the _env_key strip order."""
    existing = "  # DATASOURCE_URL=postgresql://<user>:<password>@h/db\nAGAMI_ADMIN_PASSWORD=x\n"
    merged, new_keys = prepare_deploy._merge_env(existing, _TEMPLATE, None)
    assert "DATASOURCE_URL" not in new_keys        # already present (indented comment) → not re-added
    assert merged.count("DATASOURCE_URL=") == 1


def test_merge_bumps_image_tag_only_when_passed():
    existing = "AGAMI_IMAGE_TAG=0.3.4\nAGAMI_ADMIN_PASSWORD=x\n"
    # A model-only re-stage passes no tag → the pin is left alone (no silent version change).
    kept, _ = prepare_deploy._merge_env(existing, _TEMPLATE, None)
    assert "AGAMI_IMAGE_TAG=0.3.4" in kept
    # An upgrade passes the new tag → bumped in place, no duplicate.
    bumped, _ = prepare_deploy._merge_env(existing, _TEMPLATE, "0.3.5")
    assert "AGAMI_IMAGE_TAG=0.3.5" in bumped and "AGAMI_IMAGE_TAG=0.3.4" not in bumped
    assert bumped.count("AGAMI_IMAGE_TAG=") == 1


def test_merge_normalizes_crlf_to_lf():
    """A CRLF existing file (Windows) must not yield mixed newlines after a merge; values are unchanged."""
    existing = "AGAMI_ADMIN_PASSWORD=typed\r\nAGAMI_SIGNING_SECRET=deadbeef\r\n"
    merged, _ = prepare_deploy._merge_env(existing, _TEMPLATE, "0.3.5")
    assert "\r" not in merged
    assert "AGAMI_ADMIN_PASSWORD=typed" in merged and "AGAMI_SIGNING_SECRET=deadbeef" in merged


def test_merge_reports_no_new_keys_when_file_has_them_all():
    merged, new_keys = prepare_deploy._merge_env(_TEMPLATE, _TEMPLATE, None)
    assert new_keys == []
    assert "added on upgrade" not in merged  # nothing appended


def test_selective_datasources_stages_only_chosen(tmp_path):
    """`--datasources` stages only the named models; others drop, install-global files always stay."""
    art = _artifacts(tmp_path)  # has model `demo`
    (art / "ops").mkdir()
    (art / "ops" / "datasource.yaml").write_text("name: ops\n", encoding="utf-8")
    (art / "USER_MEMORY.md").write_text("hi\n", encoding="utf-8")
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, art, datasources="demo"))
    assert (target / "artifacts" / "demo" / "datasource.yaml").exists()
    assert not (target / "artifacts" / "ops").exists()        # not chosen → dropped
    assert (target / "artifacts" / "USER_MEMORY.md").exists()  # install-global → always staged


def test_unknown_datasource_name_warns_but_does_not_fail(tmp_path, capsys):
    """A typo'd --datasources name warns to stderr (deploying a server silently missing a datasource is bad)
    but the run still succeeds for the valid ones."""
    art = _artifacts(tmp_path)  # model `demo`
    target = tmp_path / "bundle"
    status, code = prepare_deploy.prepare(_args(target, art, datasources="demo,nope"))
    assert code == 0 and status.startswith("PREPARED ")
    assert (target / "artifacts" / "demo" / "datasource.yaml").exists()
    assert "nope" in capsys.readouterr().err


def test_rerun_with_datasources_drops_a_previously_staged_model(tmp_path):
    """Dropping a datasource on a re-run must actually remove it from the bundle (copytree merges; it won't
    delete on its own) — else the server would keep serving a model the operator meant to drop."""
    art = _artifacts(tmp_path)  # model `demo`
    (art / "ops").mkdir()
    (art / "ops" / "datasource.yaml").write_text("name: ops\n", encoding="utf-8")
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, art))                       # first run stages all → demo + ops
    assert (target / "artifacts" / "ops").exists()
    prepare_deploy.prepare(_args(target, art, datasources="demo"))   # re-run: only demo
    assert (target / "artifacts" / "demo").exists()
    assert not (target / "artifacts" / "ops").exists()               # stale ops purged


def test_rerun_drops_a_symlinked_datasource_without_touching_its_target(tmp_path):
    """A dropped datasource that was staged as a symlink must be unlinked (rmtree raises on a symlink), and
    its target — which may live outside the bundle — must be left intact."""
    art = _artifacts(tmp_path)  # model `demo`
    external = tmp_path / "external-ops"
    external.mkdir()
    (external / "datasource.yaml").write_text("name: ops\n", encoding="utf-8")
    (art / "ops").symlink_to(external, target_is_directory=True)  # a symlinked model in the artifacts dir
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, art))                       # stages demo + the ops symlink
    status, code = prepare_deploy.prepare(_args(target, art, datasources="demo"))  # drop ops on re-run
    assert code == 0 and status.startswith("UPGRADED ")
    assert not (target / "artifacts" / "ops").exists()               # the staged symlink is gone
    assert (external / "datasource.yaml").exists()                          # its target is untouched


def test_all_unknown_datasources_fails_fast(tmp_path):
    """If NONE of the requested datasources exist, error out — don't build a modelless bundle that only
    breaks later at runtime."""
    art = _artifacts(tmp_path)  # model `demo`
    status, code = prepare_deploy.prepare(_args(tmp_path / "bundle", art, datasources="nope,alsonope"))
    assert code == 1 and status.startswith("ERROR ") and "matched no model" in status
