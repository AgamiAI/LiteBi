"""ACE-042 — the guard no longer injects a table's declared `default_filters` into the statement.

`runtime.apply_default_filters` ANDed each in-scope table's declared filters into the SQL before
execution. Three things were wrong with that, in increasing order of severity.

It was **misclassified**: `default_filters` are business logic (`is_deleted = false`,
`status != 'test'`), a statement about what the org MEANS by a table, not a disclosure control.
Getting one wrong produces a wrong answer, not a leak.

It **injected**: Agami never authors or alters SQL, and the one carve-out that permitted this
transform rested on the misclassification above.

And it was **broken**. `_tables_in_scope` is `tree.find_all(exp.Table)`, which descends into CTEs
and subqueries, so an alias bound INSIDE a CTE was collected and its filter ANDed onto the OUTER
`WHERE`, where that alias does not exist. The database rejected the statement and the receipt
claimed the filter had been applied — Agami's own edit manufacturing a database failure.
`test_the_cte_case_reaches_the_driver_unchanged` is that defect, pinned.

**The window.** Between this and ACE-099 a declared filter is neither applied nor reported:
`SELECT COUNT(*) FROM orders` returns every row where it previously returned the undeleted ones.
That is accepted deliberately and is DISCLOSED rather than silent —
`test_the_window_is_stated_where_a_model_author_reads` pins the two places that say so.
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
