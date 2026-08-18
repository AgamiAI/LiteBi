"""ACE-042 — the guard no longer injects a table's declared `default_filters` into the statement.

`runtime.apply_default_filters` ANDed each in-scope table's declared filters into the SQL before
execution. Three things were wrong with that, in increasing order of severity.

It was **misclassified**: `default_filters` are business logic (`is_deleted = false`,
`status != 'test'`), a statement about what the org MEANS by a table, not a disclosure control.
Getting one wrong produces a wrong answer, not a leak.

It **injected**: Agami never authors or alters SQL, and the one carve-out that permitted this
transform rested on the misclassification above.

And it was **broken**. The scope it injected against was one flat `tree.find_all(exp.Table)` walk of
the whole statement, which descends into CTEs and subqueries, so an alias bound INSIDE a CTE was
collected and its filter ANDed onto the OUTER `WHERE`, where that alias does not exist. The database
rejected the statement and the receipt claimed the filter had been applied — Agami's own edit
manufacturing a database failure. `test_the_cte_case_reaches_the_driver_unchanged` is that defect,
pinned. The walk now resolves each reference to the query scope that wrote it, which is what lets
the same fact be REPORTED per reference instead of injected across all of them.

**The window is closed.** Between this spec and ACE-099 a declared filter was neither applied nor
reported: `SELECT COUNT(*) FROM orders` returned every row where it previously returned the
undeleted ones, and nothing on the answer said so. The first half is permanent — nothing applies a
declared filter for a caller, and re-adding an injector is what
`test_the_injector_is_gone_not_wrapped` fails the build on. The second half ended when ACE-099 put
the determination on the receipt, per table reference, as
`tables.items[].filters`. So the surfaces a model author reads no longer announce a gap; they point
at the report. `test_the_surfaces_say_filters_are_unapplied_and_reported` pins both halves at once,
which is the only way to pin them: a surface that dropped "not applied" would be as wrong as one
that still claims nothing is reported.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

PROFILE = "acme"


def _write_model(root: Path) -> None:
    """`orders`, declaring a soft-delete `default_filters` entry that used to be injected.

    A real model on disk rather than a stub: `_model_safety` resolves its own model, and the
    property under test is what that whole pass does to the statement, not what one helper returns.
    """
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "default_filters": ["{alias}.is_deleted = false"],
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "total", "type": "decimal"},
                {"name": "created_at", "type": "timestamp"},
                {"name": "is_deleted", "type": "boolean"},
            ],
        })
    )


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """`_model_safety` bound to a local model that declares a filter. Returns (sql, verdict)."""
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))

    def _run(sql: str):
        return execute_sql._model_safety(sql, PROFILE, "sales")

    return _run


# --- the deletion itself ----------------------------------------------------



def _instruction_variants() -> tuple[str, str]:
    """The client-facing instructions as BOTH deployments serve them (local, hosted).

    The opening privacy sentence branches on `_hosted()`, so asserting against the module constant
    would check these properties on one path only — and the hosted preamble is new text that has to
    satisfy them too.
    """
    import os

    import tools

    saved = {k: os.environ.get(k) for k in ("AGAMI_DB_URL", "APP_DATABASE_URL")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        local = tools.server_instructions()
        os.environ["AGAMI_DB_URL"] = "sqlite:///tmp/variants.db"
        hosted = tools.server_instructions()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    return local, hosted


def test_the_injector_is_gone_not_wrapped():
    """Deleted, not left as an empty shell. A surviving `-> list[str]` reporter would be ACE-099's
    job a wave early, built on the same `find_all(exp.Table)` scoping ACE-099 exists to replace."""
    assert not hasattr(rt, "apply_default_filters")
    assert "apply_default_filters" not in rt.__all__


def test_the_safety_pass_does_not_rebind_the_statement(guarded, capsys):
    """A declared filter no longer reaches the SQL, and nothing is written about it either."""
    sql = "SELECT SUM(orders.total) AS total FROM orders"
    out, verdict = guarded(sql)
    assert verdict is None
    assert out == sql
    assert "is_deleted" not in out
    assert capsys.readouterr().err == ""


def test_the_cte_case_reaches_the_driver_unchanged(guarded):
    """The reproduction, verbatim from the spec.

    `orders` is aliased `o` INSIDE the CTE. The old walk collected that alias and ANDed
    `(o.is_deleted = FALSE)` onto the OUTER `WHERE`, where `o` is not bound, producing a statement
    the database rejects. Nothing is added now, so the statement the caller wrote is the statement
    the driver gets.
    """
    sql = ("WITH recent AS (SELECT id, total FROM orders o WHERE o.created_at > '2026-01-01') "
           "SELECT SUM(total) FROM recent")
    out, verdict = guarded(sql)
    assert verdict is None
    assert out == sql
    assert "is_deleted" not in out
    assert out.upper().count("WHERE") == 1  # the caller's own, inside the CTE


def test_asking_about_the_rows_a_filter_excludes_is_not_refused(guarded):
    """A statement that OMITS a declared filter runs. Declared filters are business logic, and a
    faithfulness finding is never a refusal (REQ-022) — "how many orders were deleted?" is a
    legitimate question, not a policy breach."""
    sql = "SELECT COUNT(orders.id) AS n FROM orders WHERE orders.is_deleted = true"
    out, verdict = guarded(sql)
    assert verdict is None
    assert out == sql


# --- what those surfaces say now --------------------------------------------

# The phrases the window notices used to end on. Each was true while the report did not exist and
# is false now, and each shipped on a surface a model or a user reads. Matched case-insensitively
# because they were written in three different casings across the surfaces they stood on.
RETIRED_CLAIMS = (
    "not yet reported",
    "are not yet reported",
    "nothing reports whether you did",
    "currently has no producer",
    "default_filters_applied",
)


def test_the_surfaces_say_filters_are_unapplied_and_reported():
    """SC-6, after the report landed. The two halves are one assertion on purpose.

    Nothing applies a declared filter to a caller's statement — that is permanent, and the surfaces
    still say it, because a model that stops writing the filter in gets a wrong answer. What ended
    is the second half: the receipt now decides, per table reference, which declared filters the
    statement satisfied, so a surface that merely dropped the old sentence would leave the model
    unaware the data it should read exists.

    Pinned on the tool description the model reads before every query, the server instructions, and
    the model field a model author is already looking at when they declare a filter. Each states the
    BEHAVIOUR; spec ids live in the adjacent comments, never in the string, because these ship to
    every client where an id resolves to nothing.
    """
    import tools
    from semantic_model import models as m

    for surface in (tools.TOOLS["execute_sql"]["description"], *_instruction_variants()):
        assert "default_filters" in surface
        # Still not applied for you.
        assert "NOT applied" in surface
        # And the report exists, named where the reader can go and find it.
        assert "tables.items[].filters" in surface
        assert "omitted" in surface

    field = m.Table.model_fields["default_filters"]
    assert field.description is not None
    assert "not applied" in field.description
    assert "reports" in field.description
    assert "per table reference" in field.description


def test_no_shipped_surface_still_says_the_report_does_not_exist():
    """The sweep the notice deletion is worth nothing without.

    Six surfaces carried the same window sentence in six spellings, and four of them had no grep
    anchor because the anchor comment WAS the shipped text. So the guard is on the retired claims
    themselves, over every surface a model or a user reads: the two tool strings, the model field,
    every SKILL.md, every shared template, and the docs tree.

    `default_filters_applied` is in the list for a second reason — it was a receipt key with no
    producer, and the fact it named now lives per reference on the `tables` section. A surface
    naming the flat key would be documenting a channel that no longer exists.
    """
    import tools
    from semantic_model import models as m

    surfaces: list[tuple[str, str]] = [
        ("tools.TOOLS['execute_sql'].description", tools.TOOLS["execute_sql"]["description"]),
        *[(f"tools.server_instructions() [{m}]", v)
          for m, v in zip(("local", "hosted"), _instruction_variants())],
        ("Table.default_filters.description", m.Table.model_fields["default_filters"].description),
    ]
    trees = [
        (REPO_ROOT / "plugins" / "agami" / "skills", "*.md"),
        (REPO_ROOT / "plugins" / "agami" / "shared", "*.html"),
        (REPO_ROOT / "docs", "*.md"),
    ]
    for root, pattern in trees:
        surfaces.extend(
            (str(path.relative_to(REPO_ROOT)), path.read_text()) for path in root.rglob(pattern)
        )

    offenders = [
        f"{name}: {claim!r}"
        for name, text in surfaces
        for claim in RETIRED_CLAIMS
        if claim.lower() in text.lower()
    ]
    assert not offenders, f"surfaces still deny the declared-filter report: {offenders}"


def test_no_notice_leaks_a_spec_id_to_a_client():
    """The window notices are the first thing in this codebase to describe an unlanded change on a
    surface an end user can see. Spec ids are an internal tracker's vocabulary — they belong in the
    comment that anchors the deletion, never in the payload a model could relay to a user."""
    import re

    import tools
    from semantic_model import models as m

    spec_id = re.compile(r"\b(?:ACE|AH|REQ)-\d+", re.IGNORECASE)
    for surface in (tools.TOOLS["execute_sql"]["description"],
                    *_instruction_variants(),
                    m.Table.model_fields["default_filters"].description):
        assert not spec_id.search(surface), surface

    # Every description at any depth, not just the top-level properties: a property whose `items`
    # carry their own object schema puts descriptions a level down, and those ship to the client
    # exactly like the rest. Sweeping only the top level meant the guard's reach stopped short of
    # part of the surface it exists to protect.
    def _descriptions(node):
        if isinstance(node, dict):
            if isinstance(node.get("description"), str):
                yield node["description"]
            for v in node.values():
                yield from _descriptions(v)
        elif isinstance(node, list):
            for v in node:
                yield from _descriptions(v)

    for spec in tools.TOOLS.values():
        for description in _descriptions(spec["inputSchema"]):
            assert not spec_id.search(description), description


def test_no_shipped_markdown_surface_carries_a_spec_id():
    """The same rule, for the surfaces that are *entirely* payload.

    A `#` comment in `tools.py` is a fair place to park the deletion anchor — Python strips it and
    no client ever sees it. A `<!-- … -->` in a SKILL.md is not the same thing: the skill file IS
    the prompt, loaded verbatim, so an HTML comment reaches the model exactly like the prose around
    it. The first version of this slice put the anchor there and it was wrong for that reason.

    `main` carries zero spec ids across these trees. If you are here because this failed, that is a
    deliberate contract change: say why in the spec's `## Decisions`, not just in the diff.
    """
    import re

    spec_id = re.compile(r"\b(?:ACE|AH|REQ)-\d+")
    roots = [REPO_ROOT / "plugins" / "agami" / "skills",
             REPO_ROOT / "plugins" / "agami" / "shared",
             REPO_ROOT / "docs"]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for root in roots
        for path in root.rglob("*.md")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if spec_id.search(line)
    ]
    assert not offenders, f"spec ids on shipped surfaces: {offenders}"
