#!/usr/bin/env python3
"""Scaffold an `/agami-deploy` bundle on the user's machine: copy the carried templates, stage the
model artifacts, and write `agami.env` with the NON-SECRET values the skill gathered.

This helper never touches a secret: the admin password is typed by the user into the file afterwards
(the agami-connect hand-off pattern), `deploy_preflight` generates the signing secret, and an external
`APP_DATABASE_URL` (itself a credential) is likewise edited into `agami.env` by the user — never passed
here on the command line. On a re-run over an EXISTING bundle it upgrades non-destructively: every value the
user typed is kept, keys new in this version are appended (so an upgrade surfaces e.g. `DATASOURCE_URL`), and
the image tag bumps only if one was passed.

Stdout is a single status line (first token machine-readable); the skill reads it and acts. Stdlib only.

  PREPARED <target>                    fresh bundle written; user must set the secrets next          [0]
  UPGRADED <target> new_keys=<a,b,…>   existing bundle upgraded in place; new_keys = keys just added [0]
  ERROR <message>                      templates missing / artifacts missing / write failed          [1]
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

# The files copied verbatim into the bundle. `agami.env` is generated from `agami.env.example` (below),
# never copied as-is, so the user gets a real config rather than a template full of placeholders.
_VERBATIM = ("docker-compose.yml", "Caddyfile", "deploy.sh", "README.md")

# The carried templates live next to the skill (scripts/ -> agami/ -> skills/agami-deploy/bundle/).
_BUNDLE_SRC = Path(__file__).resolve().parents[1] / "skills" / "agami-deploy" / "bundle"


def _set_key(text: str, key: str, value: str) -> str:
    """Set `KEY=value`, replacing the first matching line — whether it ships uncommented (`KEY=...`)
    or commented as a hint (`# KEY=...`) — so e.g. `--app-database-url` uncomments the template's hint
    rather than appending a confusing duplicate. Append only if the key is absent entirely. The `=`
    guard prevents a prefix key (`AGAMI_IMAGE_TAG`) from matching a longer one (`AGAMI_IMAGE_TAG_X`)."""
    out, replaced = [], False
    for line in text.splitlines():
        if not replaced and line.lstrip("#").lstrip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _build_env(example: str, args: argparse.Namespace) -> str:
    """The `agami.env` for a fresh bundle: the non-secret answers written onto the template. The admin
    password line is left blank (the user types it); the signing secret is left for deploy_preflight."""
    text = example if example.endswith("\n") else example + "\n"
    text = _set_key(text, "COMPOSE_PROFILES", args.profiles)
    text = _set_key(text, "AGAMI_IMAGE_TAG", getattr(args, "image_tag", None) or "latest")
    text = _set_key(text, "PUBLIC_BASE_URL", args.public_base_url)
    text = _set_key(text, "AGAMI_ADMIN_USERNAME", args.admin_email)
    text = _set_key(text, "AGAMI_ADMIN_FIRST_NAME", args.admin_first)
    text = _set_key(text, "AGAMI_ADMIN_LAST_NAME", args.admin_last)
    # External/managed Postgres (APP_DATABASE_URL) is a credential, so it is NOT set here — the template
    # ships it commented and the user edits it into agami.env by hand (the same hand-off as the password).
    return text


def _widen_one(path: Path) -> None:
    """`a+rX` on a single path: add read for all, plus execute/traverse for a directory (or a file that
    already has an execute bit). Add-only — never removes a bit, so a read-only snapshot stays readable
    (perms only widen, never narrow). A symlink is skipped so a link's target (possibly outside the bundle) is untouched."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return
    mode = stat.S_IMODE(st.st_mode)
    add = 0o444  # a+r
    if stat.S_ISDIR(st.st_mode) or (mode & 0o111):  # a+X: dirs, or files already carrying an execute bit
        add |= 0o111
    path.chmod(mode | add)


def _grant_world_read(root: Path) -> None:
    """`chmod -R a+rX` over the staged model. The deployed container runs as uid 10001, not the operator
    who owns these files, and mounts them read-only — without this the boot-time model load fails
    "Permission denied" on datasource.md and the container crash-loops. Only non-secret model files
    reach here (`local/` is excluded from staging).

    Uses `os.walk(followlinks=False)` — NOT `rglob("**")`, which follows directory symlinks on Python
    ≤3.12 and could chmod files *outside* the bundle — and it streams rather than materializing every
    path, so a large model doesn't build a giant list."""
    _widen_one(root)
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False (default): never enters a symlinked dir
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            _widen_one(base / name)


def _env_key(line: str) -> str | None:
    """The KEY of an `agami.env` line (`KEY=…` or `# KEY=…`), or None for prose/blank. A key is
    UPPER_SNAKE (letters/digits/underscore), so a comment sentence that happens to contain `=`
    (e.g. `?sslmode=require`) isn't mistaken for a setting."""
    # Whitespace FIRST, then the comment marker(s): an INDENTED commented key ("  # KEY=…") must still be
    # recognized, else _merge_env would think it's missing and re-append it (a duplicate).
    s = line.lstrip().lstrip("#").lstrip()
    key = s.split("=", 1)[0].strip() if "=" in s else ""
    return key if key and all(c.isupper() or c.isdigit() or c == "_" for c in key) else None


def _merge_env(existing: str, template: str, image_tag: str | None) -> tuple[str, list[str]]:
    """Non-destructive upgrade of an existing `agami.env`: **preserve every existing value** (never touch a
    typed password / generated secret), **append any template key not already present** (as the template's
    own line — a commented hint or a value, so a key new in this version like `DATASOURCE_URL` shows up), and
    — only when `image_tag` is given — bump the non-secret `AGAMI_IMAGE_TAG`. The output is LF-normalized
    (docker's `env_file` + the generated file use LF; a value's content is unchanged by that). Returns
    (merged_text, new_keys)."""
    present = {k for line in existing.splitlines() if (k := _env_key(line))}
    merged = existing if existing.endswith("\n") else existing + "\n"
    new_lines: list[str] = []
    new_keys: list[str] = []
    for line in template.splitlines():
        k = _env_key(line)
        if k and k not in present and k not in new_keys:
            new_lines.append(line)
            new_keys.append(k)
    if new_lines:
        merged += (
            "\n# --- added on upgrade by /agami-deploy (new in this version — fill any you need) ---\n"
            + "\n".join(new_lines)
            + "\n"
        )
    if image_tag is not None:
        merged = _set_key(merged, "AGAMI_IMAGE_TAG", image_tag)
    # Normalize to LF so appending LF lines to a CRLF file (Windows) can't produce mixed newlines.
    return merged.replace("\r\n", "\n").replace("\r", "\n"), new_keys


def _stage_ignore(artifacts: Path, datasources: list[str] | None):
    """A `copytree` ignore callable: always drop `local/` (secrets never ship), and — when `datasources`
    is given — also drop any TOP-LEVEL profile dir (a dir with an `datasource.yaml`) not in the chosen set.
    Install-global files (e.g. `USER_MEMORY.md`) and non-profile entries are always kept. Default
    (`datasources is None`) stages every model, preserving prior behavior."""
    chosen = set(datasources) if datasources else None

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        drop: set[str] = set()
        if Path(dirpath) == artifacts:  # only prune at the artifacts root
            drop.add("local")
            if chosen is not None:
                for name in names:
                    d = artifacts / name
                    if name not in chosen and d.is_dir() and (d / "datasource.yaml").is_file():
                        drop.add(name)
        return drop

    return _ignore


def prepare(args: argparse.Namespace) -> tuple[str, int]:
    """Returns (status_line, exit_code). The status line's first token is machine-readable."""
    target = Path(args.target).expanduser().resolve()
    artifacts = Path(args.artifacts_dir).expanduser().resolve()

    if not _BUNDLE_SRC.is_dir():
        return f"ERROR carried bundle templates not found at {_BUNDLE_SRC}", 1
    if not (artifacts / "local").is_dir():
        # `local/` marks a real agami-artifacts dir (i.e. /agami-connect has run) — it is the precondition,
        # NOT something we stage (creds now travel in agami.env via DATASOURCE_URL; the model is staged below).
        return f"ERROR no agami-artifacts at {artifacts} (run /agami-connect first)", 1
    if target == artifacts or target.is_relative_to(artifacts) or artifacts.is_relative_to(target):
        # Else the copytree(artifacts -> target/artifacts) would recurse into the bundle it just created.
        return f"ERROR --target must not be inside --artifacts-dir (or vice versa): {target}", 1

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in _VERBATIM:
            shutil.copy2(_BUNDLE_SRC / name, target / name)
        (target / "deploy.sh").chmod(0o755)

        # Stage the MODEL only — never a secret. `local/` (credentials + .pgpass) is excluded: the
        # deployed server reads warehouse creds from DATASOURCE_URL in agami.env, so no 600-mode file is
        # copied into a shippable bundle or mounted into the container (the uid-mismatch crash this
        # replaces). symlinks=True keeps links as links; dirs_exist_ok so a re-run refreshes in place.
        staged = target / "artifacts"
        # `dirs_exist_ok=True` merges into an existing bundle, and `ignore` only skips COPYING `local/` —
        # it does NOT delete a `local/` a PRIOR (older) prepare_deploy staged. Purge it explicitly so a
        # re-run over an old bundle can't keep a stale credentials file in the mounted volume. Fail-CLOSED:
        # NOT `ignore_errors` — a failed purge (a perms/read-only issue) must surface as an ERROR, never
        # silently leave the secret behind. A missing `local/` is fine (nothing to purge).
        stale_local = staged / "local"
        if stale_local.is_symlink():
            stale_local.unlink()  # rmtree() raises on a symlink — drop the link, never its target
        elif stale_local.exists():
            shutil.rmtree(stale_local)
        datasources = getattr(args, "datasources", None)
        dslist = [s.strip() for s in datasources.split(",") if s.strip()] if datasources else None
        if dslist:
            available = {d.name for d in artifacts.iterdir() if d.is_dir() and (d / "datasource.yaml").is_file()}
            unknown = [d for d in dslist if d not in available]
            # Fail fast if NONE of the requested datasources exist — a modelless bundle would only break
            # later at runtime. If SOME are unknown, warn (stderr keeps the stdout status line clean) and
            # stage the valid ones.
            if not any(d in available for d in dslist):
                return f"ERROR --datasources matched no model in {artifacts}: {', '.join(dslist)}", 1
            if unknown:
                sys.stderr.write(f"warning: --datasources not found (staged nothing for them): {', '.join(unknown)}\n")
            # On a re-run, drop any previously-staged model NOT in the chosen set — copytree(dirs_exist_ok)
            # merges and won't delete, so without this a dropped datasource would linger and still be served.
            chosen = set(dslist)
            if staged.exists():
                for d in staged.iterdir():
                    if d.name in chosen or not (d / "datasource.yaml").is_file():
                        continue  # keep chosen models + non-model entries (e.g. USER_MEMORY.md)
                    if d.is_symlink():
                        d.unlink()  # a symlinked model: drop the link, never rmtree its target
                    else:
                        shutil.rmtree(d)
        shutil.copytree(
            artifacts, staged, symlinks=True, dirs_exist_ok=True,
            ignore=_stage_ignore(artifacts, dslist),  # drops `local/`, and non-chosen models when dslist set
        )
        # The model is non-secret, but the container runs as a different uid than the file owner — widen
        # the staged copy to world-readable/traversable so the read-only mount is readable regardless.
        _grant_world_read(staged)

        # The operator-editable config is `agami.env` — a visible name (a dot-file like `.env` is hidden in
        # Finder, and this is the one file the user must open). docker-compose reads it via `--env-file` in
        # deploy.sh + the `env_file:` directive, since it no longer auto-loads by the `.env` name.
        example = (_BUNDLE_SRC / "agami.env.example").read_text(encoding="utf-8")
        env_path = target / "agami.env"
        if env_path.exists():
            # Upgrade-aware, NON-DESTRUCTIVE: keep the operator's typed password / generated secret, append
            # any key new in this version (so an upgrade surfaces e.g. DATASOURCE_URL), and bump the image
            # tag only if one was passed (a model-only re-stage passes none, so the pin is left alone).
            merged, new_keys = _merge_env(
                env_path.read_text(encoding="utf-8"), example, getattr(args, "image_tag", None)
            )
            env_path.write_text(merged, encoding="utf-8")
            env_path.chmod(0o600)  # reassert 600 in case an editor/umask loosened it
            return f"UPGRADED {target} new_keys={','.join(new_keys)}", 0
        env_path.write_text(_build_env(example, args), encoding="utf-8")
        env_path.chmod(0o600)
    except OSError as e:
        return f"ERROR {e}", 1

    return f"PREPARED {target}", 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scaffold an /agami-deploy bundle (non-secret values only).")
    p.add_argument("--target", required=True, help="bundle output directory")
    p.add_argument("--artifacts-dir", required=True, help="local agami-artifacts dir (model + credentials)")
    p.add_argument("--public-base-url", required=True, help="https://<hostname> teammates connect to")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-first", required=True)
    p.add_argument("--admin-last", required=True)
    p.add_argument("--profiles", default="bundled-db,edge", help="COMPOSE_PROFILES (default: single-server)")
    p.add_argument("--image-tag", default=None,
                   help="ghcr.io/agamiai/agami-core tag (fresh: defaults to 'latest'; on a re-run, set it to "
                        "bump the version — omit to leave an existing pin alone)")
    p.add_argument("--datasources", default=None,
                   help="comma-separated datasource ids to stage (default: every model in the artifacts dir)")
    # Deliberately NO --password / --app-database-url / secret args: a credential never travels on the
    # command line (it would leak into chat logs / shell history). The user edits those into agami.env.
    args = p.parse_args(argv)

    status, code = prepare(args)
    print(status)
    return code


if __name__ == "__main__":
    sys.exit(main())
