"""The StorageType -> sqlglot dialect map is complete, explicit, and actually parses.

The guards read a statement in the engine's own grammar; the map is what tells them which
grammar. Its failure mode is silent — an engine with no mapping would fall back to generic
parsing, where a backtick-quoted statement reads as no tables and no columns and every
model-scoping gate finds nothing to object to. These tests make that failure loud:

* completeness is asserted against the `StorageType` literal itself, not a hand-written
  list, so declaring a new engine without a dialect decision fails here;
* each mapping is exercised by parsing under it, so a name that sqlglot does not recognise
  fails here rather than at the first query on that engine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

import pytest

pytest.importorskip("pydantic")
sqlglot = pytest.importorskip("sqlglot")

from sqlglot.errors import ErrorLevel  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import sql_dialect as sd  # noqa: E402

STORAGE_TYPES = get_args(m.StorageType)


def _org(*storage_types: str) -> m.Datasource:
    """A datasource declaring one storage connection per engine named."""
    return m.Datasource(
        datasource="AcmeCorp",
        version=1,
        storage_connections=[
            m.StorageConnection(name=f"c{i}", storage_type=st)
            for i, st in enumerate(storage_types)
        ],
    )


# --- completeness ----------------------------------------------------------------


def test_every_declared_storage_type_has_a_dialect():
    """Adding a StorageType without a dialect decision must fail here.

    Asserted as set equality against the literal rather than as a length or a subset check:
    a subset check would pass for a newly declared engine, which is the exact regression
    this guards — that engine would silently parse generically.
    """
    assert set(sd.supported_storage_types()) == set(STORAGE_TYPES)
    assert sd.unmapped_storage_types() == ()


@pytest.mark.parametrize("storage_type", STORAGE_TYPES)
def test_each_storage_type_resolves_and_parses(storage_type):
    """The mapped name must be one sqlglot actually accepts."""
    dialect = sd.sqlglot_dialect(storage_type)
    tree = sqlglot.parse_one(
        "SELECT a FROM t", dialect=dialect, error_level=ErrorLevel.RAISE
    )
    assert tree is not None


def test_lowercasing_the_storage_type_is_not_the_mapping():
    """The reason the map is written by hand rather than derived.

    Two of the eleven engines have a sqlglot name that is not their lowercased
    StorageType, and they are the most common engine and the most divergent quoter.
    """
    assert sd.sqlglot_dialect("PostgreSQL") == "postgres"
    assert sd.sqlglot_dialect("SQLServer") == "tsql"
    for rejected in ("postgresql", "sqlserver"):
        with pytest.raises(ValueError):
            sqlglot.parse_one("SELECT 1", dialect=rejected)


def test_unknown_engine_raises_rather_than_defaulting():
    with pytest.raises(sd.DialectUnresolved):
        sd.sqlglot_dialect("Teradata")


# --- resolution from a datasource ------------------------------------------------


@pytest.mark.parametrize("storage_type", STORAGE_TYPES)
def test_single_connection_resolves(storage_type):
    assert sd.resolve_datasource_dialect(_org(storage_type)) == sd.sqlglot_dialect(
        storage_type
    )


def test_several_connections_on_one_engine_resolve():
    """Two hosts on the same engine is a legitimate model and must not refuse."""
    assert sd.resolve_datasource_dialect(_org("PostgreSQL", "PostgreSQL")) == "postgres"


def test_no_connection_is_unresolved():
    with pytest.raises(sd.DialectUnresolved):
        sd.resolve_datasource_dialect(_org())


def test_disagreeing_engines_are_unresolved():
    """A datasource runs against one database, so two declared engines are ambiguous."""
    with pytest.raises(sd.DialectUnresolved):
        sd.resolve_datasource_dialect(_org("PostgreSQL", "MySQL"))


def test_override_does_not_steer_the_dialect():
    """`storage_type_override` is free-form and unvalidated.

    Honouring it would let an arbitrary string change the grammar the guard reads a
    statement in, which is the same class of defect the explicit map closes.
    """
    org = _org("PostgreSQL")
    org.storage_connections[0].storage_type_override = "mysql"
    assert sd.resolve_datasource_dialect(org) == "postgres"


# --- the refusal reasons must be safe to surface ---------------------------------


def test_unresolved_reasons_carry_no_engine_names():
    """A refusal reason describes the shape of the problem, never model or data values."""
    with pytest.raises(sd.DialectUnresolved) as no_conn:
        sd.resolve_datasource_dialect(_org())
    with pytest.raises(sd.DialectUnresolved) as mixed:
        sd.resolve_datasource_dialect(_org("PostgreSQL", "MySQL"))

    for excinfo in (no_conn, mixed):
        reason = str(excinfo.value)
        assert "PostgreSQL" not in reason
        assert "MySQL" not in reason
        assert "AcmeCorp" not in reason
    # The ambiguous case still tells the operator how many engines were found.
    assert "2" in str(mixed.value)
