"""The surface dimension: the plugin's vendored library, held to the same verdicts as the package.

The plugin ships its own copy of the executor slice — `plugins/agami/lib/` — because a marketplace
install has no `packages/` sibling and nothing pip-installs the library. A user running a query
through the plugin is therefore running DIFFERENT FILES from the ones every other test in this
suite drives, and "both surfaces in sync" is the claim that the two nevertheless decide the same
thing. Nothing proved it end to end before this file.

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
  * **The behaviour, on the rules that can reach the plugin.** `read_only`, `recon` and
    `resource_limit` come from the vendored modules, so the corpus vectors expecting them are
    driven through the vendored executor and must produce the same rule and reason. The governed
    vectors ride along, because a surface that allowed everything would pass a refusal-only parity
    test while proving the exact opposite of what it claims.

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

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (
    TESTS_ROOT,
    Path(__file__).resolve().parent,
    REPO_ROOT / "packages" / "agami-core" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import guardrail  # noqa: E402
import harness  # noqa: E402
import tools  # noqa: E402

from safety.corpus import CASES  # noqa: E402

SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
LIB = REPO_ROOT / "plugins" / "agami" / "lib"
SRC = REPO_ROOT / "packages" / "agami-core" / "src"

# `-S` disables site.py, so an installed (including editable) agami-core is off the path — the same
# "the package is not available" state a marketplace user's plain python3 is in. Copied from
# `tests/test_plugin_lib_resolution.py`, which established the pattern and the guard below.
_NOPKG = [sys.executable, "-S"]

# The rules whose producers are vendored, so the plugin surface can actually reach them:
# `sql_guard` supplies the first two and `execute_sql` the third. The four scope rules are not
# here because `semantic_model/runtime.py` is not vendored — see this module's docstring.
REACHABLE_RULES = frozenset(
    {guardrail.RULE_READ_ONLY, guardrail.RULE_RECON, guardrail.RULE_RESOURCE_LIMIT}
)

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


@pytest.fixture()
def marketplace(tmp_path, monkeypatch):
    """A marketplace cache — `scripts/` + `lib/`, no `packages/` sibling — over the corpus's own
    warehouse and model.

    Both surfaces are given the SAME inputs on purpose: the same seeded SQLite file, the same disk
    YAML, the same statement. Anything the two then disagree about is a property of the code, not of
    the fixture. (The model is unreadable from the plugin surface — no loader, no runtime — and it
    is written anyway, so the comparison is not quietly arranged in the vendored surface's favour.)
    """
    if not _package_hidden():
        pytest.skip("cannot simulate a package-less interpreter here (-S does not hide agami-core)")

    built = harness.build_file_path(tmp_path, monkeypatch)

    # `__pycache__` is left behind rather than copied, and that is load-bearing twice over: a copied
    # cache would be stale against nothing in particular, and an EMPTY one turns "which root
    # answered" into a fact on disk — see
    # `test_the_executor_the_cli_ran_was_compiled_out_of_the_bundled_lib`.
    root = tmp_path / "cache"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(SCRIPTS, root / "scripts", ignore=ignore)
    shutil.copytree(LIB, root / "lib", ignore=ignore)

    # A deliberately minimal environment rather than the developer's own: the plugin path reads its
    # configuration entirely from the environment, so inheriting a shell would let an unrelated
    # `AGAMI_*` variable decide a verdict. `HOME` is redirected because `agami_paths.bootstrap()`
    # writes an artifacts pointer under it.
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "",
        "HOME": str(home),
        "AGAMI_ARTIFACTS_DIR": str(built.artifacts),
        f"DATASOURCE_URL__{harness.PROFILE.upper()}": f"sqlite:///{built.warehouse}",
    }
    yield root, env
    harness.reset_injected_executor()


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
# Half two: the behaviour, on the rules that reach the plugin surface
# ---------------------------------------------------------------------------


def _reachable_cases() -> list:
    """The corpus vectors this surface can answer, selected by RULE rather than by hand.

    By rule and not by attack class: the class labels what a vector attempts, and two of the `recon`
    class expect `table_scope` because a catalog relation is the model's job — those belong to the
    half this surface cannot reach, and a class-based selection would drag them in and assert a
    verdict the design says is impossible. The governed vectors come along because a surface that
    had simply stopped refusing would satisfy every refusal assertion in this file.
    """
    return [
        pytest.param(case, id=case.id)
        for case in CASES
        if (case.rule in REACHABLE_RULES or case.rule is None)
        and case.runs_on(harness.ENGINE)
    ]


@pytest.mark.parametrize("case", _reachable_cases())
def test_the_vendored_executor_returns_the_same_verdict_as_the_package(marketplace, case,
                                                                      monkeypatch):
    """Each reachable vector, decided twice — once by `plugins/agami/lib`, once by the package —
    and the two answers compared to each other AND to what the corpus says the rule must be.

    Comparing the two surfaces alone would be satisfied by both drifting together; comparing only
    against the corpus would not prove they are in sync. Both assertions, so neither hole is open.

    `remediation` is deliberately not compared. `resource_limit`'s wording depends on the
    statement's SHAPE, which `semantic_model.runtime.statement_shape` computes and the vendored
    mirror structurally cannot — so the plugin gets the shape-neutral sentence by design. That is a
    difference in advice, not in verdict, and pinning it here would pin the mirror to a model stack
    it exists to do without.
    """
    root, env = marketplace
    overrides = {}
    if case.rule == guardrail.RULE_RESOURCE_LIMIT:
        # The deployment ceiling, lowered for the two vectors that exist to reach it — on the child
        # through its environment and on the parent through `os.environ`, because there is no
        # per-call cap on either surface any more.
        overrides["AGAMI_SQL_MAX_ROWS"] = str(harness.LOW_ROW_CAP)
        monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))

    status, rule, reason, stdout = vendored_verdict(root, env, case.sql, **overrides)

    if case.rule is None:
        assert (status, rule, reason) == ("ok", None, None), (status, rule, reason, stdout)
        # The anti-vacuity half of the governed vectors: `ok` with nothing on stdout would mean the
        # statement was allowed and returned nothing, which no governed vector here does.
        assert stdout.strip(), "a governed vector came back ok with no result"
    else:
        assert status == "refused", (status, rule, reason, stdout)
        assert rule == case.rule, (rule, case.rule)
        assert reason == guardrail.REASON_FOR_RULE[case.rule], reason
        assert not stdout, "a refused statement returned a result"

    tools.set_injected_executor(None)
    assert (status, rule, reason) == package_verdict(case.sql)


# ---------------------------------------------------------------------------
# The asymmetry, pinned rather than assumed
# ---------------------------------------------------------------------------


def test_a_vendored_deployment_that_cannot_record_refuses(marketplace, monkeypatch):
    """`audit_unavailable` is the fourth rule both surfaces produce, and it needs configuring rather
    than a statement: no corpus vector can provoke it.

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
    tools.set_injected_executor(None)
    assert package_verdict(governed.sql) == (status, rule, reason)


def test_a_served_vendored_deployment_refuses_before_it_can_miss_the_model(marketplace):
    """The one place the two surfaces genuinely diverge, pinned so a change to `_VENDORED` has to
    come past it.

    `model_unavailable` is produced by a vendored module, so it looks reachable from here — and it
    is not. Both it and `audit_unavailable` require a SERVED deployment, the audit probe runs first,
    and that probe opens the store through `store.py`, which is not vendored either. So on the
    plugin surface every served configuration refuses with `audit_unavailable` before the model is
    ever looked for, INCLUDING one whose store is perfectly healthy — asserted below with a real,
    openable SQLite store rather than the broken URL, because the broken one would reach the same
    verdict for the ordinary reason and prove nothing about this.

    Read as a security property it is the strongest possible fail-closed: the mirror cannot execute
    a statement in a served deployment at all. Read as a parity claim it is the boundary of one —
    `model_unavailable` is asserted on the package surface, where it can happen, and asserting it
    here would be asserting something structurally impossible.
    """
    root, env = marketplace
    healthy_store = Path(env["AGAMI_ARTIFACTS_DIR"]).parent / "app.db"
    governed = next(case for case in CASES if case.rule is None)

    status, rule, _reason, stdout = vendored_verdict(
        root, env, governed.sql, AGAMI_DB_URL=f"sqlite://{healthy_store}"
    )

    assert (status, rule) == ("refused", guardrail.RULE_AUDIT_UNAVAILABLE), (status, rule, stdout)


def test_the_scope_rules_have_no_producer_on_the_vendored_surface(marketplace):
    """The other half of the split, stated as behaviour instead of as a claim in a docstring.

    `select_star` is the demonstration because its failure mode is the loudest: on the package
    surface `SELECT *` is refused because the guard cannot resolve the star to a column list, and
    on the plugin surface the same statement runs and hands back every column the table has. That
    is not a defect to fix here; it is what "the plugin path has no semantic model" MEANS, and the
    plugin is a local single-user tool reading a database the user already owns.

    Pinned because the day `semantic_model/runtime.py` joins `_VENDORED` this test fails, which is
    the notification that the two halves have become one and this file's split can go.
    """
    root, env = marketplace
    star = next(case for case in CASES if case.rule == guardrail.RULE_SELECT_STAR)

    status, rule, _reason, stdout = vendored_verdict(root, env, star.sql)

    assert (status, rule) == ("ok", None), (status, rule)
    assert "amount" in stdout.splitlines()[0], stdout
    # And the package surface refuses the very same statement, which is what makes the line above a
    # measured asymmetry rather than a note about a surface nobody compared.
    tools.set_injected_executor(None)
    assert package_verdict(star.sql) == (
        "refused",
        guardrail.RULE_SELECT_STAR,
        guardrail.REASON_FOR_RULE[guardrail.RULE_SELECT_STAR],
    )
