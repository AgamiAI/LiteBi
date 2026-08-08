"""Regression: the `sm` launcher must install agami-core[model] in a **marketplace** layout —
`scripts/` + `lib/` with NO `packages/` sibling and no pip install — without crashing and without pointing
at the dev-only `packages/agami-core` path (the failure the real run-through hit).

We run `sm install` with a **fake `$PY` shim** (`AGAMI_PYTHON`) that logs every pip-install it's asked to
run and fails the bare index requirement `agami-core[model]` (simulating "not on PyPI yet") so we can
observe the git fallback — no real network install happens. Also asserts the dev checkout still uses the
editable install, and that the skill delegates to `sm install` (no hardcoded dev-path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SM = REPO / "plugins" / "agami" / "scripts" / "sm"
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
LIB = REPO / "plugins" / "agami" / "lib"
SKILL = REPO / "plugins" / "agami" / "skills" / "agami-connect" / "SKILL.md"
# The plugin version drives the cache-dir name + the pinned git ref sm derives — read it from the
# manifest so a version bump needs no edit here.
PLUGIN_VERSION = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())["metadata"]["version"]

# A fake interpreter: the import check fails until a successful install "lands" it (a marker file), each
# pip install is logged, and the bare index name fails so the git fallback is exercised — the `-e` (dev)
# and git requirements "succeed" and drop the marker so the post-install re-check passes.
_SHIM = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["SM_SHIM_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
marker = os.environ["SM_SHIM_INSTALLED"]
if args[:1] == ["-c"]:
    sys.exit(0 if os.path.exists(marker) else 1)  # `import semantic_model, …` works only once installed
if "pip" in args and "install" in args:
    joined = " ".join(args)
    if "agami-core[model]" in joined and "git+" not in joined and "-e" not in args:
        sys.exit(1)  # bare index name: pretend it's not on an index yet
    open(marker, "w").close()  # -e (dev) and git requirements install the package
    sys.exit(0)
sys.exit(0)
"""


# Externally-managed interpreter (PEP 668): refuse BOTH `--user` and plain pip; only
# `--break-system-packages` is allowed to install. Proves `sm` reaches that last-resort tier.
_SHIM_PEP668 = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["SM_SHIM_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
marker = os.environ["SM_SHIM_INSTALLED"]
if args[:1] == ["-c"]:
    sys.exit(0 if os.path.exists(marker) else 1)
if "pip" in args and "install" in args:
    if "--break-system-packages" in args:
        open(marker, "w").close()
        sys.exit(0)
    sys.exit(1)  # externally-managed: --user and plain both refused
sys.exit(0)
"""


# A library that IMPORTS fine but is older than the plugin — the case `_imports_ok` alone cannot see.
# The import probe (`-c`) always passes; the version probe (`python - <ver>`, script on stdin) fails
# until an `--upgrade` install lands the marker. Without the version gate this shim would make `sm`
# return immediately and log no install at all, which is exactly the silent staleness being fixed.
_SHIM_STALE = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["SM_SHIM_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
marker = os.environ["SM_SHIM_INSTALLED"]
if args[:1] == ["-c"]:
    sys.exit(0)                                    # the stale library imports perfectly well
if args[:1] == ["-"]:
    sys.exit(0 if os.path.exists(marker) else 1)   # ...but is below the floor until upgraded
if "pip" in args and "install" in args:
    if "--upgrade" not in args:
        sys.exit(1)      # pip would treat the present dist as satisfying a bare requirement
    open(marker, "w").close()
    sys.exit(0)
sys.exit(0)
"""


def _run_install(sm_path: Path, tmp_path: Path, shim_src: str = _SHIM):
    shim = tmp_path / "pyshim"
    shim.write_text(shim_src)
    shim.chmod(0o755)
    log = tmp_path / "pip.log"
    env = {
        **os.environ,
        "AGAMI_PYTHON": str(shim),
        "SM_SHIM_LOG": str(log),
        "SM_SHIM_INSTALLED": str(tmp_path / "installed.marker"),
        "AGAMI_ARTIFACTS_DIR": str(tmp_path / "art"),
    }
    r = subprocess.run(["bash", str(sm_path), "install"], env=env, capture_output=True, text=True)
    lines = log.read_text().splitlines() if log.exists() else []
    installs = [ln for ln in lines if "pip" in ln and "install" in ln]
    return r, installs


def test_marketplace_layout_installs_from_git_not_devpath(tmp_path):
    cache = tmp_path / "agami-core" / PLUGIN_VERSION  # the cache dir name is the version
    shutil.copytree(SCRIPTS, cache / "scripts")
    shutil.copytree(LIB, cache / "lib")  # NO packages/ sibling
    r, installs = _run_install(cache / "scripts" / "sm", tmp_path)

    assert r.returncode == 0, r.stderr  # no crash at the old PKG_DIR line
    assert installs, "sm attempted no install"
    # Never the dev-only editable path.
    assert not any("-e" in ln.split() for ln in installs), installs
    assert not any("/packages/agami-core" in ln for ln in installs), installs
    # Index tried first, git as the fallback, pinned to the cache's version.
    idx = next(i for i, ln in enumerate(installs) if "agami-core[model]" in ln and "git+" not in ln)
    git = next(i for i, ln in enumerate(installs) if "git+" in ln)
    assert idx < git, installs
    assert f"git+https://github.com/AgamiAI/agami-core@v{PLUGIN_VERSION}#subdirectory=packages/agami-core" in "\n".join(installs)


def test_dev_checkout_uses_editable(tmp_path):
    # The real repo has packages/agami-core → editable install wins; git never tried.
    r, installs = _run_install(SM, tmp_path)
    assert r.returncode == 0, r.stderr
    assert any("-e" in ln.split() and ln.rstrip().endswith("/packages/agami-core[model]") for ln in installs), installs


def test_skill_delegates_install_to_sm():
    txt = SKILL.read_text()
    assert "packages/agami-core[model]" not in txt  # no hardcoded dev-path install command
    assert 'scripts/sm" install' in txt


def test_pep668_reaches_break_system_packages(tmp_path):
    # An externally-managed interpreter refuses --user + plain; sm must fall through to the
    # --break-system-packages tier and still install. Dev layout → the `-e` requirement.
    r, installs = _run_install(SM, tmp_path, shim_src=_SHIM_PEP668)
    assert r.returncode == 0, r.stderr
    assert any("--break-system-packages" in ln for ln in installs), installs


def test_stale_library_is_upgraded_not_left_alone(tmp_path):
    # The regression this guards: the plugin's own files are refetched on a version bump, but the
    # pip-installed library is not — and `_agami_lib` prefers that installed package over the fresh
    # bundled `lib/`. An import-only readiness check passes, so new skills run against an old library
    # (0.6.0's render_chart.py rejects a pre-0.6.0 receipt and every charted query dies).
    cache = tmp_path / "agami-core" / PLUGIN_VERSION
    shutil.copytree(SCRIPTS, cache / "scripts")
    shutil.copytree(LIB, cache / "lib")
    r, installs = _run_install(cache / "scripts" / "sm", tmp_path, shim_src=_SHIM_STALE)

    assert r.returncode == 0, r.stderr
    assert installs, "a stale-but-importable library was left in place — no install attempted"
    assert all("--upgrade" in ln.split() for ln in installs), (
        f"every attempt must carry --upgrade or pip no-ops on the present dist: {installs}"
    )
    assert f"older than this plugin ({PLUGIN_VERSION})" in r.stderr, r.stderr


def test_current_library_is_left_alone(tmp_path):
    # The other half: a library at or above the floor must NOT provoke a reinstall on every invocation.
    # Same shim, but the marker exists from the start, so the version probe passes immediately.
    cache = tmp_path / "agami-core" / PLUGIN_VERSION
    shutil.copytree(SCRIPTS, cache / "scripts")
    shutil.copytree(LIB, cache / "lib")
    (tmp_path / "installed.marker").write_text("")
    r, installs = _run_install(cache / "scripts" / "sm", tmp_path, shim_src=_SHIM_STALE)

    assert r.returncode == 0, r.stderr
    assert installs == [], f"a current library must not be reinstalled: {installs}"


def test_version_floor_is_the_plugin_version_not_a_second_constant():
    # The floor must stay the plugin's own version. A hand-maintained constant is one a release can
    # forget to bump, which would silently retire the gate this test exists to protect.
    txt = SM.read_text()
    assert "_version_ok" in txt, "the version floor gate must exist"
    assert 'version("agami-core")' in txt, "the gate must read the INSTALLED distribution's version"
    assert "_imports_ok && _version_ok" in txt, "readiness must require both probes, not just imports"


def test_readiness_probes_cli_entrypoint_and_isolates_cwd():
    # the readiness probe must import the real entrypoint `semantic_model.cli` (not just the
    # package, which the bundled stub's __init__ would satisfy), and the CLI must run from a neutral cwd
    # so a `semantic_model` in the caller's cwd can't shadow the installed package.
    txt = SM.read_text()
    assert "import semantic_model.cli" in txt, "readiness probe must check the .cli entrypoint"
    assert "cd /" in txt, "the CLI/probe must run from a neutral cwd (path isolation)"
