"""Regression: the plugin's runtime scripts must resolve the agami-core library in a
**marketplace install** — `scripts/` + the bundled `lib/`, with NO `packages/` sibling and no pip
install — which is the exact layout that broke agami-connect (`import agami_paths` → ModuleNotFoundError).

The trick: the test suite installs the package, so a plain subprocess would import it from site-packages
and never exercise the bundled `lib/`. We run the scripts under `python -S` (site-packages disabled) so an
installed agami-core is invisible — faithfully simulating the marketplace "no package" env. A guard fixture
skips if `-S` fails to hide it, so the tests never pass vacuously.

Also guards the vendored `lib/` against drift from `packages/agami-core/src` (the source of truth).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "agami" / "scripts"
LIB = REPO / "plugins" / "agami" / "lib"
SRC = REPO / "packages" / "agami-core" / "src"
VENDORED = ["agami_paths.py", "execute_sql.py", "sql_guard.py", "guardrail.py", "semantic_model/__init__.py", "semantic_model/units.py"]

# `-S` disables site.py, so an installed (incl. editable) agami-core is not on the path — the same
# "the package isn't available" state a marketplace user's plain python3 is in.
_NOPKG = [sys.executable, "-S"]
_ENV = {**os.environ, "PYTHONPATH": ""}


def _package_hidden() -> bool:
    return subprocess.run([*_NOPKG, "-c", "import agami_paths"], env=_ENV, capture_output=True).returncode != 0


@pytest.fixture
def marketplace_cache(tmp_path):
    """A marketplace-like cache: scripts/ + lib/ with NO packages/ sibling. Skips if we can't hide the pkg."""
    if not _package_hidden():
        pytest.skip("cannot simulate a package-less interpreter here (-S does not hide agami-core)")
    root = tmp_path / "cache"
    shutil.copytree(SCRIPTS, root / "scripts")
    shutil.copytree(LIB, root / "lib")
    return root


def test_connect_resolve_runs_in_marketplace_layout(marketplace_cache):
    r = subprocess.run([*_NOPKG, str(marketplace_cache / "scripts" / "connect_resolve.py")],
                       env=_ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout)  # valid JSON → agami_paths resolved via the bundled lib, not site-packages


@pytest.mark.parametrize("mod", ["csv_to_sections", "setup_pgauth", "build_duckdb_attach"])
def test_scripts_import_in_marketplace_layout(marketplace_cache, mod):
    code = f"import sys; sys.path.insert(0, {str(marketplace_cache / 'scripts')!r}); import {mod}"
    r = subprocess.run([*_NOPKG, "-c", code], env=_ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_vendored_lib_matches_source():
    """The bundled lib/ is a drift-checked mirror; if this fails, run `uv run dev.py sync-lib`."""
    for rel in VENDORED:
        assert (LIB / rel).read_bytes() == (SRC / rel).read_bytes(), f"{rel} drifted from the package source"
