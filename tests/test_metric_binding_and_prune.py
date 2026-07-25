"""Metric-binding column validation + column_group pruning on per-column exclusion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _org(metric):
    t = m.Table(name="orders", schema="s", storage_connection="c", grain=["id"],
                description="o", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="cost", type="decimal")])
    sa = m.SubjectArea(name="s", tables_defined=[t], metrics=[metric])
    return m.Organization(datasource="t", subject_areas=[sa])


# --- binding column check ---------------------------------------------------

def test_binding_column_check_flags_unknown_column():
    bad = m.Metric(name="orders_total", calculation="total", source_tables=["orders"],
                   bindings={"PostgreSQL": "SUM(cst)"})   # typo: cst not on orders
    res = V.ValidationResult()
    V._check_metric_binding_columns(_org(bad), res)
    assert any("cst" in w for w in res.warnings)


def test_binding_column_check_ok_for_real_column():
    good = m.Metric(name="orders_total", calculation="total", source_tables=["orders"],
                    bindings={"PostgreSQL": "SUM(cost)"})
    res = V.ValidationResult()
    V._check_metric_binding_columns(_org(good), res)
    assert not res.warnings


def test_binding_column_check_ignores_count_star():
    cnt = m.Metric(name="orders_count", calculation="count", source_tables=["orders"],
                   bindings={"PostgreSQL": "COUNT(*)"})
    res = V.ValidationResult()
    V._check_metric_binding_columns(_org(cnt), res)
    assert not res.warnings


def test_binding_column_check_catches_excluded_column_metric():
    # the moot-metric case: a rate on a column that was later excluded (no longer on the table)
    moot = m.Metric(name="orders_active_rate", calculation="rate", source_tables=["orders"],
                    bindings={"PostgreSQL": "AVG(CASE WHEN is_active THEN 1.0 ELSE 0.0 END)"})
    res = V.ValidationResult()
    V._check_metric_binding_columns(_org(moot), res)   # is_active isn't on orders
    assert any("is_active" in w for w in res.warnings)


# --- column_group pruning on exclusion --------------------------------------

def test_prune_column_groups_drops_excluded_columns_and_empty_groups():
    cols = [m.Column(name="id", type="integer", primary_key=True),
            m.Column(name="a", type="string"), m.Column(name="b", type="string")]
    t = m.Table(name="w", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=cols, column_groups={"identity": ["id"], "grp": ["a", "b"]},
                column_group_descriptions={"identity": "x", "grp": "y"})
    # simulate the loader having dropped excluded columns a, b
    t.columns = [c for c in t.columns if c.name == "id"]
    L._prune_column_groups(t)
    assert t.column_groups == {"identity": ["id"]}      # 'grp' emptied → removed
    assert t.column_group_descriptions == {"identity": "x"}


def test_reconcile_table_ref_exposes_narrows_and_clears():
    t = m.Table(name="w", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[m.Column(name="id", type="integer", primary_key=True),
                         m.Column(name="a", type="string")],
                column_groups={"identity": ["id"], "grp": ["a"]})
    # exposes a now-missing group 'flags' alongside a real one → narrowed to the real one
    narrow = m.TableRef(storage_connection="c", schema="s", table="w",
                        expose_column_groups=["identity", "flags"])
    # exposes only-real groups that cover ALL surviving groups → cleared (means "expose all")
    clearit = m.TableRef(storage_connection="c", schema="s", table="w",
                         expose_column_groups=["identity", "grp", "flags"])
    L._reconcile_table_ref_exposes([narrow, clearit], [t])
    assert narrow.expose_column_groups == ["identity"]
    assert clearit.expose_column_groups is None
