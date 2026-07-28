"""Every SQL parse on the guard path states its grammar and reports its failures.

Two mistakes are invisible at review and silently disable a gate, so they are asserted
mechanically instead of trusted to a reader:

* **A parse with no dialect.** The generic grammar matches no real engine. A statement in
  the engine's own quoting parses to something that is not what it says — often to nothing
  at all — and a gate inspecting that tree finds nothing to object to.
* **An error level passed as a string.** sqlglot compares the level against `ErrorLevel`
  members, so a string matches no branch and every collected parse error is discarded,
  leaving a truncated tree. This one is especially hard to spot: `error_level="raise"`
  reads like hardening and does nothing at all. Only the enum works.

The scan is over the AST rather than the text, because a regex cannot tell
`error_level=ErrorLevel.RAISE` from `error_level="RAISE"` inside a multi-line call, and
cannot attribute a call to the function that contains it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = [
    REPO_ROOT / "packages" / "agami-core" / "src",
    REPO_ROOT / "plugins" / "agami" / "lib",
    REPO_ROOT / "plugins" / "agami" / "scripts",
]

# Parses that are deliberately not on the guard path, keyed by "<relpath>::<function>" so an
# edit that moves code does not silently invalidate the entry the way a line number would.
# A parse reaching a caller's SQL does NOT belong here — it belongs behind the guard's own
# parse helper, which supplies both the dialect and the error level.
# Every sqlglot entry point that turns text into a tree. Naming only `parse_one` would let a
# guard-path `sqlglot.parse(...)` or `transpile(...)` land without a grammar.
#
# These three names are distinctive enough to match however they are called. `parse` is not —
# this repo has two unrelated local `parse()` helpers, and any object can have a `.parse()` —
# so it counts only as `sqlglot.parse`. A scanner that cries wolf gets exempted into uselessness.
_UNAMBIGUOUS_PARSE_CALLS = frozenset({"parse_one", "transpile", "maybe_parse"})


def _receiver_is_sqlglot(node: ast.expr) -> bool:
    """True for `sqlglot.parse(...)` and `x.sqlglot.parse(...)`, false for any other receiver."""
    if isinstance(node, ast.Name):
        return node.id == "sqlglot"
    if isinstance(node, ast.Attribute):
        return node.attr == "sqlglot"
    return False

_EXEMPT: dict[str, str] = {
    "packages/agami-core/src/semantic_model/validator.py::_columns_referenced": (
        "reads model-authored SQL at validation time, not a caller's statement; its signature "
        "does not receive the model, so it has no declared engine to parse for"
    ),
    "plugins/agami/scripts/render_chart.py::_format_sql": (
        "pretty-prints SQL for display only; the result is never parsed for a verdict nor "
        "handed to an executor"
    ),
    "packages/agami-core/src/semantic_model/validator.py::_binding_column_refs": (
        "reads a model-authored metric binding at validation time, not a caller's statement"
    ),
    "packages/agami-core/src/semantic_model/validator.py::_sqlparse_error": (
        "already passes the ErrorLevel enum; it has no datasource engine to parse for because "
        "it validates the model itself"
    ),
    "packages/agami-core/src/semantic_model/derived.py::_has_nested_aggregate": (
        "inspects a model-authored metric fragment at validation time, not a caller's statement"
    ),
}


def _iter_parse_calls():
    """Yield (relpath, enclosing function name, ast.Call) for every `parse_one` call.

    Each call is attributed to the innermost function that contains it, so a nested helper
    is named in its own right rather than folded into its parent.
    """
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relpath = str(path.relative_to(REPO_ROOT))
            yield from _walk(tree, "<module>", relpath)


def _walk(node, owner: str, relpath: str):
    """Descend once, carrying the innermost enclosing function name."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _walk(child, child.name, relpath)
            continue
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                # `parse` is common enough that matching every `x.parse(...)` would flag
                # unrelated parsers, so it counts only when the receiver is sqlglot itself.
                if func.attr in _UNAMBIGUOUS_PARSE_CALLS or (
                    func.attr == "parse" and _receiver_is_sqlglot(func.value)
                ):
                    yield relpath, owner, child
            elif isinstance(func, ast.Name) and func.id in _UNAMBIGUOUS_PARSE_CALLS:
                yield relpath, owner, child
        yield from _walk(child, owner, relpath)


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


ALL_CALLS = list(_iter_parse_calls())


@pytest.mark.parametrize(
    "source,expected",
    [
        ("sqlglot.parse(sql)", True),
        ("sqlglot.parse_one(sql)", True),
        ("parse_one(sql)", True),
        ("sqlglot.transpile(sql)", True),
        # A local helper, or any other object's parser, is not a SQL parse we govern. Flagging
        # these would demand arguments they do not take, and an exemption list grown to silence
        # false positives is how a scanner stops being read.
        ("parse(text)", False),
        ("argparser.parse(text)", False),
        ("self.parse(text)", False),
        ("json.loads(text)", False),
    ],
)
def test_the_scan_matches_sql_parses_and_not_look_alikes(source, expected, tmp_path):
    path = tmp_path / "probe.py"
    path.write_text(f"def f(sql, text):\n    return {source}\n", encoding="utf-8")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = list(_walk(tree, "<module>", "probe.py"))
    assert bool(found) is expected, f"{source} -> {'matched' if found else 'not matched'}"


def test_the_scan_actually_found_the_parses():
    """A scan that silently matches nothing would pass every assertion below."""
    assert len(ALL_CALLS) >= 4


@pytest.mark.parametrize(
    "relpath,owner,call",
    [pytest.param(r, o, c, id=f"{r}::{o}") for r, o, c in ALL_CALLS],
)
def test_guard_path_parses_state_dialect_and_error_level(relpath, owner, call):
    if f"{relpath}::{owner}" in _EXEMPT:
        pytest.skip("declared not to be on the guard path")

    error_level = _kwarg(call, "error_level")
    assert error_level is not None, (
        f"{relpath}::{owner} parses without an error level. sqlglot's default raises, but "
        "state the posture rather than inherit it."
    )
    assert not isinstance(error_level, ast.Constant), (
        f"{relpath}::{owner} passes a *string* error level. sqlglot compares the level "
        "against ErrorLevel members, so a string matches no branch and every parse error is "
        "silently discarded — use ErrorLevel.RAISE."
    )
    assert isinstance(error_level, ast.Attribute) and error_level.attr == "RAISE", (
        f"{relpath}::{owner} must pass ErrorLevel.RAISE so a statement that does not parse "
        "is refused rather than read as a truncated tree."
    )
    assert _kwarg(call, "dialect") is not None, (
        f"{relpath}::{owner} parses without a dialect. The generic grammar matches no engine, "
        "so the resulting tree can describe a different statement than the one given."
    )


def test_no_exemption_is_stale():
    """An exemption for code that no longer exists hides a real regression behind a skip."""
    live = {f"{r}::{o}" for r, o, _ in ALL_CALLS}
    assert not (set(_EXEMPT) - live), f"exemptions no longer matching any parse: {set(_EXEMPT) - live}"


def test_every_exemption_gives_a_reason():
    for key, reason in _EXEMPT.items():
        assert reason.strip(), f"{key} is exempt without a written reason"


def test_the_internal_parse_helpers_are_always_given_a_grammar():
    """`_parse_sql` / `_parse_reporting` default `dialect` to None so standalone callers keep
    working, which means a guard that forgets the argument parses generically and the AST rule
    above cannot see it — it only watches sqlglot's own entry points. This watches ours."""
    bare: list[str] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relpath = str(path.relative_to(REPO_ROOT))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else None
                )
                if name not in ("_parse_sql", "_parse_reporting"):
                    continue
                # The grammar may be given positionally or by keyword; either states it.
                if len(node.args) < 2 and not any(k.arg == "dialect" for k in node.keywords):
                    bare.append(f"{relpath}:{node.lineno}")
    assert not bare, (
        "these calls parse without a grammar, so the statement is read in one no engine uses: "
        + ", ".join(bare)
    )


def test_the_guard_battery_parses_in_exactly_one_place():
    """Funnelling every guard parse through one helper is what makes the rule above hold as
    code moves: a new gate reuses the helper instead of re-deriving the arguments."""
    runtime_parses = [
        (r, o) for r, o, _ in ALL_CALLS
        if r.endswith("semantic_model/runtime.py")
    ]
    assert [o for _, o in runtime_parses] == ["_parse_reporting"], (
        f"the guard battery should parse only through its helper; found {runtime_parses}"
    )
