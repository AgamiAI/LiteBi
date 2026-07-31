"""Regression: the plugin's runtime scripts must resolve the agami-core library in a
**marketplace install** — `scripts/` + the bundled `lib/`, with NO `packages/` sibling and no pip
install — which is the exact layout that broke agami-connect (`import agami_paths` → ModuleNotFoundError).

The trick: the test suite installs the package, so a plain subprocess would import it from site-packages
and never exercise the bundled `lib/`. We run the scripts under `python -S` (site-packages disabled) so an
installed agami-core is invisible — faithfully simulating the marketplace "no package" env. A guard fixture
skips if `-S` fails to hide it, so the tests never pass vacuously.

Also guards the vendored `lib/` against drift from `packages/agami-core/src` (the source of truth),
and against the other half of "whatever python3 you have": the slice must still import on 3.9, the
stock macOS interpreter, even though the package itself requires 3.10+.
"""

from __future__ import annotations

import ast
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
VENDORED = ["agami_paths.py", "execute_sql.py", "guardrail.py", "sql_guard.py", "semantic_model/__init__.py", "semantic_model/units.py"]

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


# ---------------------------------------------------------------------------
# The oldest interpreter the vendored slice has to run on
# ---------------------------------------------------------------------------

# The plugin mirror is the "no pip, no package, no deps, whatever python3 you have" path, and on
# stock macOS that is 3.9.6. The *package* declares requires-python >= 3.10 and is unaffected — this
# floor applies to the vendored slice alone.
OLDEST_SUPPORTED = (3, 9)

# Keyword arguments `dataclasses.dataclass` only learned after OLDEST_SUPPORTED. They are perfectly
# valid SYNTAX, so no parser catches them; they fail at import time, on the user's machine, with
# `TypeError: dataclass() got an unexpected keyword argument`. `kw_only` is the one that actually
# regressed this slice — `guardrail.Envelope` used it to hold the contract's field order, and both
# `sql_guard` and `execute_sql` import `guardrail` at module load, so the whole slice stopped
# importing on 3.9 at once.
_TOO_NEW_DATACLASS_KWARGS = {"kw_only", "slots", "match_args", "weakref_slot"}


def _dataclass_decorators(tree: ast.AST):
    """Every `@dataclass(...)` decorator call in the module, however the name was imported."""
    for node in ast.walk(tree):
        for deco in getattr(node, "decorator_list", []):
            if isinstance(deco, ast.Call):
                fn = deco.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "dataclass":
                    yield deco


@pytest.mark.parametrize("rel", VENDORED)
def test_the_vendored_slice_stays_python_39_compatible(rel):
    """The vendored modules must still load on the oldest interpreter the plugin path can meet.

    Checked statically rather than by shelling out, because the suite's own interpreter is 3.12 and
    a 3.9 is not guaranteed to exist on a CI runner — `tests/test_ports.py` shells out to
    `sys.executable`, which is exactly why nothing in the suite could see this regression. Two
    distinct failure modes, because they fail at different times:

      * 3.10+ *grammar* (`match`, PEP-604 unions evaluated at runtime, parenthesized context
        managers) — `ast.parse(feature_version=…)` rejects it here.
      * 3.10+ *dataclass keywords* — legal syntax that raises `TypeError` at import. Nothing but an
        explicit check finds these, so they are named in a list.

    Not covered, and not worth the machinery: a 3.10+ stdlib call reached at runtime. The
    import-time surface is what broke and what a marketplace user hits first.
    """
    source = (LIB / rel).read_text()
    try:
        tree = ast.parse(source, filename=str(LIB / rel), feature_version=OLDEST_SUPPORTED)
    except SyntaxError as exc:  # pragma: no cover - only on a real regression
        pytest.fail(f"{rel} uses grammar newer than {'.'.join(map(str, OLDEST_SUPPORTED))}: {exc}")

    offenders = sorted(
        {
            kw.arg
            for deco in _dataclass_decorators(tree)
            for kw in deco.keywords
            if kw.arg in _TOO_NEW_DATACLASS_KWARGS
        }
    )
    assert not offenders, (
        f"{rel} passes {offenders} to @dataclass, which "
        f"python {'.'.join(map(str, OLDEST_SUPPORTED))} does not accept"
    )


def test_the_39_check_can_go_red():
    """A static check nobody has seen fail is a static check nobody should trust.

    Both arms, on synthetic sources: the grammar arm and the dataclass-keyword arm. Without this, a
    typo in the decorator matcher would leave the parametrized test above green forever.
    """
    with pytest.raises(SyntaxError):
        ast.parse("match x:\n    case 1: pass\n", feature_version=OLDEST_SUPPORTED)

    tree = ast.parse("import dataclasses\n@dataclasses.dataclass(frozen=True, kw_only=True)\nclass C:\n    x: int\n")
    assert [kw.arg for deco in _dataclass_decorators(tree) for kw in deco.keywords] == [
        "frozen", "kw_only",
    ]
