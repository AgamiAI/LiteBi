"""ACE-071 — the guard battery is composed in exactly one place, in one fixed order.

The defect this spec closes was a second, silent composition: the package-less entry point ran no
gates at all and said nothing about it. The fix is a refusal on that path, and what keeps the fix
meaningful is that there is nowhere else for the battery to be assembled. A caller that reaches the
gates through its own sequence is a second composition whether or not it happens to be correct
today, and the next gate added to `_model_safety` would silently not be in it.

So this is a structural test over the package source rather than a behavioural one: every call of
every gate lives inside `_model_safety`, each appears exactly once, and they appear in the order the
battery depends on. It reads `packages/agami-core/src/` alone; `plugins/agami/lib/execute_sql.py` is
generated from it by `dev.py sync-lib` and held byte-identical by the drift gate, so a second scan
would assert the drift gate rather than this property.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
COMPOSER = PKG_SRC / "execute_sql.py"
COMPOSING_FUNCTION = "_model_safety"

# The five gates, in the order `_model_safety` runs them. The order is load-bearing rather than
# stylistic: the readability gate refuses a statement no gate below could parse, the scopability
# gate catches the readable statement whose reach still cannot be named, and each scope gate below
# degrades to ALLOW when it has nothing to read — so a battery run out of order reports clean on
# exactly the statements the first two gates exist to stop.
BATTERY = (
    "check_readable",
    "check_scopable",
    "check_table_scope",
    "check_no_select_star",
    "check_column_scope",
)


def _call_sites(tree: ast.AST) -> list[tuple[str, int]]:
    """Every call of a battery gate under `tree`, as (gate, line), in source order.

    An AST walk rather than a text search, because the two families of gate read differently in
    source and only one of them is greppable without false positives. The three scope gates are
    called off the `RT` alias, so `RT.check_table_scope(` is distinctive; the other two are resolved
    through `getattr(RT, ..., None)` and then called as bare names, and that bare name also appears
    in `semantic_model/runtime.py` as its own `def` and in several prose comments. A `Call` node is
    neither a definition nor English.

    Both the attribute form and the bare form are collected for all five names, so importing a gate
    directly and calling it somewhere else is caught as readily as reaching it through the alias.
    """
    sites: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BATTERY:
            sites.append((func.attr, node.lineno))
        elif isinstance(func, ast.Name) and func.id in BATTERY:
            sites.append((func.id, node.lineno))
    return sorted(sites, key=lambda site: site[1])


def _composing_function() -> ast.FunctionDef:
    """The `_model_safety` definition in `execute_sql.py` — the one place the battery is assembled."""
    module = ast.parse(COMPOSER.read_text(), filename=str(COMPOSER))
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == COMPOSING_FUNCTION:
            return node
    raise AssertionError(f"{COMPOSING_FUNCTION} is not defined in {COMPOSER}")


def test_the_guard_battery_is_called_only_from_the_one_composing_function() -> None:
    """No module in the package calls a gate outside `_model_safety`.

    A gate reached from anywhere else is a second battery: it can be assembled in the wrong order,
    miss a member, or run against a model the receipt was not built from, and none of those is
    visible from the outcome of a single query.
    """
    inside = _call_sites(_composing_function())

    for path in sorted(PKG_SRC.rglob("*.py")):
        sites = _call_sites(ast.parse(path.read_text(), filename=str(path)))
        expected = inside if path == COMPOSER else []
        assert sites == expected, f"{path.relative_to(REPO_ROOT)} calls a guard gate: {sites}"


def test_the_guard_battery_is_composed_once_in_a_fixed_order() -> None:
    """Each gate is called exactly once inside `_model_safety`, and in the battery's own order.

    One assertion covers both because both failures look the same from outside: a duplicated gate
    and a reordered one each produce a sequence that is not `BATTERY`, and a dropped gate produces a
    shorter one.
    """
    called = tuple(gate for gate, _ in _call_sites(_composing_function()))

    assert called == BATTERY
