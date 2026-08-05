"""The surface dimension: the plugin's vendored library, held to the same verdicts as the package.

The plugin ships its own copy of the executor slice — `plugins/agami/lib/` — because a marketplace
install has no `packages/` sibling and nothing pip-installs the library. A user running a query
through the plugin is therefore running DIFFERENT FILES from the ones every other test in this
suite drives, and "both surfaces in sync" is the claim about what the two decide. Nothing proved it
end to end before this file.

**It is proved in two halves, and the split is structural rather than a convenience.**
`dev.py::_VENDORED` mirrors `agami_paths`, `execute_sql`, `guardrail`, `sql_guard` and the
`semantic_model` package init — and deliberately NOT `semantic_model/runtime.py`, which is where
`table_scope`, `column_scope`, `select_star` and `unscopable` live and which needs pydantic and
sqlglot the vendored path is designed not to require. Those four rules cannot fire on the plugin
surface at all, so a test asserting they do would be asserting something the design says is false.

  * **The contract, symbol for symbol.** `REASON_FOR_RULE`, `Receipt.SECTIONS`, `PRE_MODEL_RULES`
    and every `RULE_*` are compared across the two import roots as LOADED OBJECTS. The byte-level
    pin already exists — `tests/test_plugin_lib_resolution.py::test_vendored_lib_matches_source`
    compares the files — and this is the other end of it: identical bytes that fail to produce
    identical symbols would mean the module read differently, and identical symbols with drifted
    bytes are what that test catches. Neither replaces the other.
  * **The behaviour.** Below.

**ACE-071 (`c11d732`) changed what the behavioural half can honestly claim, and this file now
asserts the new contract rather than the one it was written against.** Until that commit the
vendored executor met the missing runtime with a shrug: `_model_safety` took its `except Exception`
arm, handed the statement back untouched, and the plugin ran it with table scope, the star ban and
column scope all off. ACE-071 made that arm fail closed. When a model is DECLARED for the profile —
`<artifacts>/<profile>/datasource.yaml` exists — and the guards cannot be imported, the executor
refuses `model_unavailable` / `undetermined` instead of executing. A modelless install stays inert
by design: nothing is declared, so nothing is undetermined.

So the behavioural half is no longer "the same verdict on the rules that reach the plugin".
Measured on this branch over all 84 file-engine corpus vectors, with a model declared — the state
any install past its first `agami-connect` is in:

  * **36 vectors decide identically**: the 31 `read_only` and the 5 `recon` ones. Their producer is
    the vendored `sql_guard` and it runs ABOVE `_model_safety` at the chokepoint, so the new branch
    cannot pre-empt them. That set is not hand-listed here; it is derived from the contract's own
    `PRE_MODEL_RULES` — see `STATEMENT_REACHABLE_RULES`.
  * **48 vectors now diverge, every one of them in the same direction.** Where the package answers
    `ok` (16) or refuses with a rule only the model pass can produce — `table_scope` 12,
    `select_star` 6, `column_scope` 5, `unscopable` 5, `unparseable` 2 — the vendored surface
    refuses `model_unavailable` and returns nothing.
  * **`resource_limit` moved from the first group to the second.** Its producer IS vendored, but it
    is produced in `execute_sql` AFTER `_model_safety` returns, so the new branch shadows it. It is
    not gone: `test_a_modelless_vendored_install_still_answers_and_still_bounds` reaches it on the
    same surface with the model taken away, which is what makes "shadowed" a measured statement
    rather than a guess about a dead gate.

**This is a narrower parity claim than the one this file made before, and stating it narrowly is
the honest option rather than a concession.** The two surfaces agreed on 54 vectors and now agree
on 36. What replaced the agreement is not silence: it is a stronger safety property asserted in its
place — on the 48 the vendored surface no longer executes AT ALL, where before it executed
unguarded. A test that kept the old wording would be reporting parity that no longer exists; a test
that only asserted "it refuses" would let the surface refuse for any reason at all. Both halves are
asserted below, by rule.

**Which root actually answered.** `_agami_lib.ensure_importable()` falls back to
`packages/agami-core/src` when the bundled `lib/` is missing or incomplete, silently and by design —
so a parity test that merely ran and passed could be the package answering under the plugin's name,
twice. Every call below runs under `python -S` (site-packages hidden, so an installed agami-core is
invisible) inside a copied marketplace cache that has no `packages/` sibling to fall back TO, and
`test_the_marketplace_layout_answers_from_the_bundled_lib` asserts the resolved module files by
path before any verdict is compared.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (
    TESTS_ROOT,
    Path(__file__).resolve().parent,
    REPO_ROOT / "packages" / "agami-core" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

# The model stack, required rather than skipped when a run declared it carries this half. See
# `test_safety_corpus.py` for the measured failure this replaces.
itdeps.importorfail("pydantic", "sqlglot", "yaml", sentinel=itdeps.E2E_REQUIRED)

import guardrail  # noqa: E402
import harness  # noqa: E402

from safety.corpus import CASES  # noqa: E402

SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
LIB = REPO_ROOT / "plugins" / "agami" / "lib"
SRC = REPO_ROOT / "packages" / "agami-core" / "src"

# `-S` disables site.py, so an installed (including editable) agami-core is off the path — the same
# "the package is not available" state a marketplace user's plain python3 is in. Copied from
# `tests/test_plugin_lib_resolution.py`, which established the pattern and the guard below.
_NOPKG = [sys.executable, "-S"]

# The rules a STATEMENT can still provoke on this surface once a model is declared — derived from
# the contract rather than re-listed, because the property that puts a rule here is exactly the one
# `PRE_MODEL_RULES` names: it was decided before the model pass was consulted, so ACE-071's branch
# inside that pass cannot pre-empt it. Today that resolves to `read_only` and `recon`, both from the
# vendored `sql_guard`.
#
# `audit_unavailable` is the third member and comes out because no statement can provoke it: it
# needs a served configuration, so it is asserted by
# `test_a_vendored_deployment_that_cannot_record_refuses` instead of by a corpus vector.
#
# `resource_limit` was here and is not any more. Its producer is vendored, but it is produced in
# `execute_sql` after `_model_safety` has already returned a refusal, so on this surface it is
# shadowed rather than reachable. Measured, both directions — see this module's docstring and
# `test_a_modelless_vendored_install_still_answers_and_still_bounds`.
STATEMENT_REACHABLE_RULES = guardrail.PRE_MODEL_RULES - {guardrail.RULE_AUDIT_UNAVAILABLE}

# Unsupported on purpose rather than merely unreachable, and the same value
# `test_safety_envelope.py` drives the package surface with, so the two surfaces are answering one
# question: the store raises on this scheme before it touches a driver or a socket. Its presence is
# also what makes the deployment SERVED as far as the gate is concerned.
BROKEN_DB_URL = "mysql://not-a-supported-scheme/agami"


def _package_hidden() -> bool:
    """Whether `-S` really hides the installed package here.

    Without this the whole file could pass vacuously in the other direction: an interpreter that
    still resolves `agami_paths` from site-packages would have the PACKAGE answer every call while
    the test reported the plugin surface green.
    """
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""}
    probe = subprocess.run([*_NOPKG, "-c", "import agami_paths"], env=env, capture_output=True)
    return probe.returncode != 0


def _require_a_package_less_interpreter() -> None:
    """Stop the fixture unless `-S` really hides the installed package here.

    A skip drops the ENTIRE vendored-surface dimension — every parity vector, the import-root proof,
    the pinned divergence — and takes the run's exit code with it, which is the same shape as every
    other hole this spec closes. Under `AGAMI_E2E_REQUIRED` an interpreter that will not hide its
    site-packages is a broken runner, not a tolerated one.
    """
    if not _package_hidden():
        itdeps.skip_or_fail(
            "cannot simulate a package-less interpreter here (-S does not hide agami-core)",
            itdeps.E2E_REQUIRED,
        )


def _marketplace_cache(tmp_path: Path) -> Path:
    """Copy `scripts/` + `lib/` into `tmp_path` with no `packages/` sibling to fall back to.

    `__pycache__` is left behind rather than copied, and that is load-bearing twice over: a copied
    cache would be stale against nothing in particular, and an EMPTY one turns "which root answered"
    into a fact on disk — see `test_the_executor_the_cli_ran_was_compiled_out_of_the_bundled_lib`.
    """
    root = tmp_path / "cache"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(SCRIPTS, root / "scripts", ignore=ignore)
    shutil.copytree(LIB, root / "lib", ignore=ignore)
    return root


def _child_env(tmp_path: Path, artifacts: Path, warehouse: Path) -> dict:
    """The environment the vendored child runs under.

    A deliberately minimal environment rather than the developer's own: the plugin path reads its
    configuration entirely from the environment, so inheriting a shell would let an unrelated
    `AGAMI_*` variable decide a verdict. `HOME` is redirected because `agami_paths.bootstrap()`
    writes an artifacts pointer under it — and because `_disk_model_root` falls back to
    `~/agami-artifacts`, so a developer with a real `acme` profile would otherwise decide the
    modelless fixture's answer from their own home directory.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "",
        "HOME": str(home),
        "AGAMI_ARTIFACTS_DIR": str(artifacts),
        f"DATASOURCE_URL__{harness.PROFILE.upper()}": f"sqlite:///{warehouse}",
    }


@pytest.fixture()
def marketplace(tmp_path, monkeypatch):
    """A marketplace cache — `scripts/` + `lib/`, no `packages/` sibling — over the corpus's own
    warehouse and model.

    Both surfaces are given the SAME inputs on purpose: the same seeded SQLite file, the same disk
    YAML, the same statement. Anything the two then disagree about is a property of the code, not of
    the fixture.

    The model is written even though the plugin surface cannot READ it — no loader, no runtime — and
    since ACE-071 that is the whole point rather than a fairness gesture. Its mere PRESENCE is what
    the vendored executor keys its fail-closed branch off: `_disk_model_root` asks whether
    `<artifacts>/<profile>/datasource.yaml` exists, and nothing more. So this fixture is a
    marketplace install past its first `agami-connect`, which is the state the divergence below is
    a claim about.
    """
    _require_a_package_less_interpreter()

    built = harness.build_file_path(tmp_path, monkeypatch)
    root = _marketplace_cache(tmp_path)
    env = _child_env(tmp_path, built.artifacts, built.warehouse)
    yield root, env
    harness.reset_injected_executor()


@pytest.fixture()
def bare_marketplace(tmp_path):
    """The same cache over the same warehouse, with NO model declared for the profile.

    ACE-071's carve-out, built rather than described: a local install between `pip install` and its
    first `agami-connect` has no declared surface, so nothing is out of scope and nothing is
    undetermined, and the vendored executor stays inert instead of refusing. The artifacts root
    exists and the profile directory does not, which is exactly what `_disk_model_root` answers None
    to.

    No `monkeypatch` and no `harness.build_file_path`: the only consumer of this fixture asks the
    vendored surface a question and never the package one, and the child is handed an explicit
    environment, so there is no parent state to arrange.
    """
    _require_a_package_less_interpreter()

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    warehouse = tmp_path / "warehouse.db"
    harness.seed_warehouse(warehouse)
    return _marketplace_cache(tmp_path), _child_env(tmp_path, artifacts, warehouse)


def vendored_verdict(root: Path, env: dict, sql: str, **overrides) -> tuple:
    """Run one statement through the VENDORED executor and read its verdict off the CLI wire.

    `lib/execute_sql.py` invoked as a script puts `lib/` at `sys.path[0]`, which is exactly what the
    documented `"$PY" -m execute_sql` invocation does from a marketplace cache — same root, same
    module, one fewer moving part than re-deriving the plugin's interpreter selection here.

    The wire is the CLI contract `execute_sql.main` documents and every plugin caller parses: one
    JSON object on stderr and exit 1 when refused, CSV on stdout and exit 0 when it ran. The tuple
    it returns is shaped like `harness.verdict`'s first three fields so the two surfaces compare
    directly.
    """
    proc = subprocess.run(
        [
            *_NOPKG,
            str(root / "lib" / "execute_sql.py"),
            "--profile",
            harness.PROFILE,
            "--area",
            harness.AREA,
            "--sql",
            sql,
        ],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        # Bounded for the same reason `harness.route_stdio` is, and to the same 180s: a child that
        # deadlocks or blocks on I/O would otherwise hang until the job-level timeout kills the
        # whole run, which reports as an infrastructure flake rather than as this vector stalling.
        timeout=180,
    )
    if proc.returncode == 0:
        return ("ok", None, None, proc.stdout)
    # A refusal is the ONLY thing this stream may carry, which is what makes parsing the whole of
    # it the right read rather than a lenient scan: `_write_refusal` documents stderr as a single
    # object, and a second line there would be a contract break worth failing on.
    try:
        refusal = json.loads(proc.stderr)["refusal"]
    except (ValueError, KeyError):
        return ("failed", proc.returncode, proc.stderr.strip(), proc.stdout)
    return ("refused", refusal["rule"], refusal["reason"], proc.stdout)


def package_verdict(sql: str) -> tuple:
    """The same statement on the PACKAGE surface, through the real tool edge.

    In-process rather than over a transport: the transports are already proved verdict-for-verdict
    against each other in `test_safety_stdio.py`, and what is under test here is the import root,
    so paying a subprocess to re-prove the transport dimension would measure the wrong axis.
    """
    body = harness.route_in_process(sql)
    refusal = body.get("refusal") or {}
    return (body["status"], refusal.get("rule") or None, refusal.get("reason") or None)


# ---------------------------------------------------------------------------
# Half one: the contract, symbol for symbol
# ---------------------------------------------------------------------------


def _load_vendored_guardrail():
    """Load `plugins/agami/lib/guardrail.py` under its own name, alongside the package's.

    By path rather than by manipulating `sys.path`, because both modules have to be live in this
    process at once and a path insertion would only ever give one of them the name `guardrail`.

    Registered in `sys.modules` BEFORE it is executed, and left there. `dataclasses` resolves a
    field's string annotation by looking its defining module up by name, so a module that executes
    while absent from the table raises on the first `@dataclass` it reaches — the loader normally
    does this registration and doing it by hand means doing all of it.
    """
    name = "vendored_guardrail"
    spec = importlib.util.spec_from_file_location(name, LIB / "guardrail.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_two_import_roots_expose_one_contract():
    """The rule vocabulary, its reason pairing and the receipt's sections are the same objects on
    both sides.

    This is the loaded-symbol end of a pin whose other end is the byte comparison in
    `tests/test_plugin_lib_resolution.py::test_vendored_lib_matches_source`, and the two catch
    different things. That one catches an edit to the package source that was never synced. This
    one catches the case bytes cannot speak to: a contract that is identical on disk and still
    reads differently once loaded — a rule added under an `if` the two interpreters take
    differently, or a `REASON_FOR_RULE` assembled at import time from something environmental.
    Asserting the rule SET as well as the mapping is what makes a rule added on one side only a
    failure here rather than a silent asymmetry the corpus would never reach.
    """
    vendored = _load_vendored_guardrail()

    assert vendored.REASON_FOR_RULE == guardrail.REASON_FOR_RULE
    assert vendored.Receipt.SECTIONS == guardrail.Receipt.SECTIONS
    assert vendored.PRE_MODEL_RULES == guardrail.PRE_MODEL_RULES

    rules = {name for name in dir(guardrail) if name.startswith("RULE_")}
    assert rules == {name for name in dir(vendored) if name.startswith("RULE_")}
    assert {name: getattr(vendored, name) for name in rules} == {
        name: getattr(guardrail, name) for name in rules
    }
    # Not a restatement of the first assertion: it pins that the mapping's KEYS are the whole rule
    # vocabulary, so a rule symbol that exists on both sides and is pinned to no reason on either
    # cannot pass here. `refuse()` raises `KeyError` on such a rule, which is a runtime failure on
    # whichever surface reaches it first.
    assert set(guardrail.REASON_FOR_RULE) == {getattr(guardrail, name) for name in rules}


# ---------------------------------------------------------------------------
# Which root answered
# ---------------------------------------------------------------------------


def test_the_marketplace_layout_answers_from_the_bundled_lib(marketplace):
    """Every module the plugin path resolves comes out of the bundled `lib/`, and none out of
    `packages/agami-core/src`.

    The check the rest of this file rests on. `ensure_importable()` tries an installed package
    first, then `<scripts>/../lib`, then the dev checkout's `packages/agami-core/src` — and the
    fallback is silent, so a bundled `lib/` that failed to resolve would hand every call below to
    the package source and the parity assertions would compare the package against itself and pass.
    `-S` closes the first candidate and a cache with no `packages/` sibling closes the third; this
    asserts what is left actually answered, by resolved file path.

    It also pins the absence the two-halves split rests on: `semantic_model.runtime` is not
    importable here, which is precisely why the scope rules are out of scope below.
    """
    root, env = marketplace
    probe = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(root / 'scripts')!r})\n"
        "import _agami_lib; _agami_lib.ensure_importable()\n"
        "import agami_paths, execute_sql, guardrail, sql_guard, semantic_model\n"
        "resolved = {m.__name__: m.__file__ for m in "
        "(agami_paths, execute_sql, guardrail, sql_guard, semantic_model)}\n"
        "try:\n"
        "    from semantic_model import runtime\n"
        "    resolved['semantic_model.runtime'] = runtime.__file__\n"
        "except ImportError:\n"
        "    pass\n"
        "print(json.dumps(resolved))\n"
    )
    proc = subprocess.run([*_NOPKG, "-c", probe], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    resolved = json.loads(proc.stdout)
    bundled = (root / "lib").resolve()
    for name, path in resolved.items():
        assert Path(path).resolve().is_relative_to(bundled), (name, path)
        assert not Path(path).resolve().is_relative_to(SRC.resolve()), (name, path)
    assert "semantic_model.runtime" not in resolved, resolved
    # Named individually as well, so a probe that quietly stopped importing one of them cannot make
    # the loop above vacuous.
    assert set(resolved) == {"agami_paths", "execute_sql", "guardrail", "sql_guard",
                             "semantic_model"}


def test_the_executor_the_cli_ran_was_compiled_out_of_the_bundled_lib(marketplace):
    """The same question as the test above, asked of the CLI invocation the parity vectors use, and
    answered by the filesystem instead of by a probe.

    The probe above establishes what `ensure_importable()` resolves; this establishes what the
    process that produced the verdicts actually imported, which is not quite the same statement.
    The two roots hold byte-identical files by construction — that is what the drift check
    guarantees — so no output can ever tell them apart, and the evidence has to be structural.
    Bytecode is: the interpreter writes a `.pyc` next to the source it imported, the fixture hands
    over a cache with no `__pycache__` at all, and the environment carries nothing that would
    suppress the write. A compiled `guardrail` and `sql_guard` under the cache is therefore a
    statement about which files were read, made by the interpreter that read them.
    """
    root, env = marketplace
    pycache = root / "lib" / "__pycache__"
    assert not pycache.exists(), "the fixture handed over a cache that was already compiled"

    status, rule, _reason, _stdout = vendored_verdict(root, env, "DELETE FROM orders")

    assert (status, rule) == ("refused", guardrail.RULE_READ_ONLY)
    for module in ("agami_paths", "guardrail", "sql_guard"):
        assert list(pycache.glob(f"{module}.*.pyc")), (module, sorted(pycache.glob("*")))


# ---------------------------------------------------------------------------
# Half two: the behaviour — what still agrees, and what now diverges
# ---------------------------------------------------------------------------


def _cases(predicate) -> list:
    """Corpus vectors selected by RULE rather than by hand.

    By rule and not by attack class: the class labels what a vector ATTEMPTS, and two of the `recon`
    class expect `table_scope` because a catalog relation is the model's job. A class-based
    selection would file those under the rules that still agree and assert a verdict this surface
    cannot produce.
    """
    return [
        pytest.param(case, id=case.id)
        for case in CASES
        if predicate(case.rule) and case.runs_on(harness.ENGINE)
    ]


@pytest.mark.parametrize("case", _cases(lambda rule: rule in STATEMENT_REACHABLE_RULES))
def test_the_vendored_executor_returns_the_same_verdict_as_the_package(marketplace, case):
    """Each vector decided ABOVE the model pass, decided twice — once by `plugins/agami/lib`, once
    by the package — and the two answers compared to each other AND to what the corpus says the
    rule must be.

    Comparing the two surfaces alone would be satisfied by both drifting together; comparing only
    against the corpus would not prove they are in sync. Both assertions, so neither hole is open.

    **This is narrower than it was, and deliberately so.** It used to cover the governed vectors and
    the two `resource_limit` ones as well — 54 vectors against today's 36. ACE-071 made the vendored
    executor refuse `model_unavailable` whenever a model is declared and the guards are not
    importable, which pre-empts every verdict the model pass or the executor would have reached, so
    on those 48 the two surfaces no longer produce the same answer and a test saying they do would
    be false. What replaced the claim is
    `test_the_vendored_executor_refuses_where_the_package_consults_the_model`, which asserts the
    divergence itself, by rule and in one direction only. Nothing was dropped to make a failure go
    away; the vectors moved to the assertion that is now true of them.

    What survives here survives for a structural reason rather than by luck. `sql_guard` is vendored
    AND runs above `_model_safety` at the chokepoint, so no branch inside the model pass can get in
    front of it — which is the same property `guardrail.PRE_MODEL_RULES` names and the reason
    `STATEMENT_REACHABLE_RULES` is derived from that constant instead of listed here.
    """
    root, env = marketplace

    status, rule, reason, stdout = vendored_verdict(root, env, case.sql)

    assert status == "refused", (status, rule, reason, stdout)
    assert rule == case.rule, (rule, case.rule)
    assert reason == guardrail.REASON_FOR_RULE[case.rule], reason
    assert not stdout, "a refused statement returned a result"

    assert (status, rule, reason) == package_verdict(case.sql)


@pytest.mark.parametrize("case", _cases(lambda rule: rule not in STATEMENT_REACHABLE_RULES))
def test_the_vendored_executor_refuses_where_the_package_consults_the_model(marketplace, case,
                                                                           monkeypatch):
    """Every vector the model pass or the executor would decide, on both surfaces: the package
    reaches the corpus's verdict, and the vendored surface refuses `model_unavailable` and hands
    back nothing.

    The 48 vectors this file used to either assert parity on (the 16 governed and the 2
    `resource_limit`) or leave out entirely (the 30 scope / `unscopable` / `unparseable` ones),
    now under one assertion because ACE-071 gave them one answer. Whatever the package decides —
    `ok`, `table_scope`, `select_star`, `column_scope`, `unscopable`, `unparseable`,
    `resource_limit` — the vendored surface never gets far enough to decide it, because
    `_model_safety` refuses first on a model it can see declared and cannot read.

    Both sides are asserted and that is the point. Asserting only the vendored half would pass on a
    surface that refused `model_unavailable` for every statement ever sent, including the writes
    the lexer is supposed to catch first; asserting only the package half would not be about this
    surface at all. Together they say: the divergence is exactly here, it runs in exactly one
    direction, and the safe side is the vendored one.

    `remediation` and `detail` are deliberately not compared anywhere in this file. The two surfaces
    are now giving advice about different things — "narrow the statement" against "use the
    interpreter that has the package" — and pinning the wording would pin the mirror to a model
    stack it exists to do without.
    """
    root, env = marketplace
    overrides = {}
    if case.rule == guardrail.RULE_RESOURCE_LIMIT:
        # The deployment ceiling, lowered for the two vectors that exist to reach it — on the child
        # through its environment and on the parent through `os.environ`, because there is no
        # per-call cap on either surface any more. The child no longer REACHES the ceiling, since
        # the model refusal comes first, and it is set anyway: this fixture's contract is that both
        # surfaces see identical inputs, so the divergence below is the code's and not the setup's.
        overrides["AGAMI_SQL_MAX_ROWS"] = str(harness.LOW_ROW_CAP)
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))

    status, rule, reason, stdout = vendored_verdict(root, env, case.sql, **overrides)

    assert (status, rule) == ("refused", guardrail.RULE_MODEL_UNAVAILABLE), (status, rule, stdout)
    assert reason == guardrail.REASON_FOR_RULE[guardrail.RULE_MODEL_UNAVAILABLE], reason
    # The half that makes this a security statement rather than a status code: a fail-closed refusal
    # that still printed the rows would be the old behaviour wearing a refusal's clothes.
    assert not stdout, "a refused statement returned a result"

    if case.rule is None:
        assert package_verdict(case.sql) == ("ok", None, None)
    else:
        assert package_verdict(case.sql) == (
            "refused", case.rule, guardrail.REASON_FOR_RULE[case.rule]
        )


def test_a_modelless_vendored_install_still_answers_and_still_bounds(bare_marketplace):
    """The control for everything above: with the model taken away, the same vendored surface runs
    the governed statement and still enforces the row ceiling.

    Two claims that only mean something together.

    **The refusals above are keyed to the model, not to a broken cache.** Every assertion in
    `test_the_vendored_executor_refuses_where_the_package_consults_the_model` would pass just as
    well if the copied `lib/` were unusable and the child refused everything it was handed. Here the
    only thing that changed is that `<artifacts>/<profile>/datasource.yaml` is absent, and the same
    statement comes back `ok` with rows — so the refusal is `_model_safety`'s declared-model branch
    and nothing else. This is also ACE-071's own inertness carve-out, asserted on the surface it was
    written for.

    **`resource_limit` is shadowed on this surface, not absent from it.** Its producer is vendored,
    it is simply reached after `_model_safety` has already refused. Take the model away and the same
    vector on the same surface refuses `resource_limit` again, which is the difference between a
    gate that is dead and a gate that is unreachable behind an earlier one — and the difference
    matters, because removing the model refusal must not silently leave the ceiling off.
    """
    root, env = bare_marketplace
    governed = next(case for case in CASES if case.rule is None)
    capped = next(case for case in CASES if case.rule == guardrail.RULE_RESOURCE_LIMIT)

    status, rule, reason, stdout = vendored_verdict(root, env, governed.sql)
    assert (status, rule, reason) == ("ok", None, None), (status, rule, reason, stdout)
    assert stdout.strip(), "a governed vector came back ok with no result"

    status, rule, reason, stdout = vendored_verdict(
        root, env, capped.sql, AGAMI_SQL_MAX_ROWS=str(harness.LOW_ROW_CAP)
    )
    assert (status, rule) == ("refused", guardrail.RULE_RESOURCE_LIMIT), (status, rule, stdout)
    assert reason == guardrail.REASON_FOR_RULE[guardrail.RULE_RESOURCE_LIMIT], reason
    assert not stdout, "a refused statement returned a result"


# ---------------------------------------------------------------------------
# The asymmetry, pinned rather than assumed
# ---------------------------------------------------------------------------


def test_a_vendored_deployment_that_cannot_record_refuses(marketplace, monkeypatch):
    """`audit_unavailable` is the third member of `PRE_MODEL_RULES` and the one no statement can
    provoke: it needs configuring rather than a statement, so it is asserted here rather than by a
    corpus vector.

    The statement is a governed one, so the refusal cannot be a scope gate wearing this rule's
    clothes — and on this surface there are no scope gates to mistake it for.

    A spy executor cannot cross the process boundary the way `test_safety_envelope.py` uses one, so
    "it did not execute" is asserted here in the only form the CLI exposes: the result emitter is
    the sole writer to stdout, so an empty stdout is a statement that returned nothing. The stronger
    did-not-execute proof is the package surface's and is already made there; this is the parity
    claim about the verdict.
    """
    root, env = marketplace
    governed = next(case for case in CASES if case.rule is None)

    status, rule, reason, stdout = vendored_verdict(
        root, env, governed.sql, AGAMI_DB_URL=BROKEN_DB_URL
    )

    assert (status, rule) == ("refused", guardrail.RULE_AUDIT_UNAVAILABLE), (status, rule, reason)
    assert reason == guardrail.REASON_FOR_RULE[guardrail.RULE_AUDIT_UNAVAILABLE]
    assert not stdout

    monkeypatch.setenv("AGAMI_DB_URL", BROKEN_DB_URL)
    assert package_verdict(governed.sql) == (status, rule, reason)


def test_a_served_vendored_deployment_refuses_before_it_can_miss_the_model(marketplace):
    """The audit probe still runs in front of the model check, and after ACE-071 that ordering is
    finally observable instead of merely structural.

    Both refusals are live on this surface now, which is what changed. `_audit_store_reachable`
    opens the store through `store.py`, not vendored, so a SERVED vendored deployment cannot record
    and refuses `audit_unavailable`; `_model_safety` cannot import the guards against a model it can
    see declared, so it refuses `model_unavailable`. `execute_guarded` runs the audit probe first,
    above even the read-only gate, so the served configuration gets `audit_unavailable` — including
    one whose store is perfectly healthy, asserted below with a real, openable SQLite store rather
    than the broken URL, because the broken one would reach the same verdict for the ordinary reason
    and prove nothing about the ordering.

    **The control is what makes this an ordering claim.** Before ACE-071 the model branch did not
    exist on this path, so "the audit probe pre-empts the model check" was satisfied by there being
    nothing to pre-empt. The second call below takes `AGAMI_DB_URL` away and nothing else — same
    cache, same declared model, same statement — and gets `model_unavailable`. So the model refusal
    is armed in both runs and the audit probe really is what comes first.

    Read as a security property it is the strongest possible fail-closed: the mirror cannot execute
    a statement in a served deployment at all, and since ACE-071 it cannot execute one against a
    declared model either.

    The store is CREATED and MIGRATED here, which it was not: the path was pointed at a file nothing
    had made, so "a real, openable SQLite store" was a claim the test did not arrange and the case it
    says it rules out — the broken URL reaching this verdict for the ordinary reason — was exactly
    the case being run. `test_safety_envelope.py::served` builds one the same way, and its existence
    is asserted before the verdict so a store that failed to appear cannot pass this quietly.
    """
    from store import Store

    root, env = marketplace
    healthy_store = Path(env["AGAMI_ARTIFACTS_DIR"]).parent / "app.db"
    store = Store.connect(f"sqlite://{healthy_store}")
    store.run_migrations()
    store.close()
    assert healthy_store.exists(), "the store this test calls healthy was never created"

    governed = next(case for case in CASES if case.rule is None)

    status, rule, _reason, stdout = vendored_verdict(
        root, env, governed.sql, AGAMI_DB_URL=f"sqlite://{healthy_store}"
    )

    assert (status, rule) == ("refused", guardrail.RULE_AUDIT_UNAVAILABLE), (status, rule, stdout)

    # The control: the same everything, served no longer. The refusal the probe got in front of.
    status, rule, _reason, stdout = vendored_verdict(root, env, governed.sql)

    assert (status, rule) == ("refused", guardrail.RULE_MODEL_UNAVAILABLE), (status, rule, stdout)


def test_the_scope_rules_have_no_producer_on_the_vendored_surface(marketplace):
    """The other half of the split, stated as behaviour instead of as a claim in a docstring — and
    the consequence of that missing producer is no longer the one this test was written to pin.

    The premise still holds: `semantic_model/runtime.py` is not vendored, so `table_scope`,
    `column_scope`, `select_star` and `unscopable` have no producer on this surface and cannot
    appear in any verdict it returns. That absence is asserted in two places already and is not
    restated here — `test_the_marketplace_layout_answers_from_the_bundled_lib` pins that
    `semantic_model.runtime` does not import in this layout, and
    `test_the_vendored_executor_refuses_where_the_package_consults_the_model` pins the rule each of
    the 28 scope vectors actually comes back with, which is `model_unavailable` and therefore never
    a scope rule. This test is about the CONSEQUENCE of that absence, on one vector, in full.

    **What changed is the consequence, and it changed in the best direction.** This test used to
    assert that `SELECT *` ran on the plugin surface and handed back every column the table has —
    true when it was written, and the loudest demonstration of what "the plugin path has no semantic
    model" meant. ACE-071 replaced that with a refusal: the executor already published
    `RECEIPT_NO_RUNTIME` for exactly this state, principle 4c makes undetermined a refusal, and it
    now acts on its own conclusion. So the assertion here is strictly stronger than the one it
    replaces — the statement does not run, and nothing comes back — and the package surface still
    refuses the same statement with `select_star`, which is what keeps this a measured divergence
    rather than a note about a surface nobody compared.

    **Two things could now close this split, and either one fails this test.**
    `semantic_model/runtime.py` joining `dev.py::_VENDORED` would give the four rules a producer
    here and the two halves would become one. So would the fail-closed branch going away — by
    vendoring the model stack, or by the branch being narrowed or removed — which would put the old
    fail-open behaviour back. The first is the split closing; the second is a regression wearing its
    clothes, and the rule asserted below is what tells them apart.
    """
    root, env = marketplace
    star = next(case for case in CASES if case.rule == guardrail.RULE_SELECT_STAR)

    status, rule, reason, stdout = vendored_verdict(root, env, star.sql)

    assert (status, rule) == ("refused", guardrail.RULE_MODEL_UNAVAILABLE), (status, rule, stdout)
    assert reason == guardrail.REASON_FOR_RULE[guardrail.RULE_MODEL_UNAVAILABLE], reason
    assert not stdout, "the statement ran and handed back columns nothing had scoped"
    # And the package surface refuses the very same statement WITH the rule this surface has no
    # producer for, which is what makes the lines above a measured divergence.
    assert package_verdict(star.sql) == (
        "refused",
        guardrail.RULE_SELECT_STAR,
        guardrail.REASON_FOR_RULE[guardrail.RULE_SELECT_STAR],
    )
