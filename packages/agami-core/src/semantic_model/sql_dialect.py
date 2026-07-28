"""Resolve a datasource's declared engine to the sqlglot dialect the guard parses with.

The model-scoping guards read a statement by parsing it. If they parse it in the wrong
grammar the tree does not describe the statement: on a backtick-quoting engine
``SELECT `ssn` FROM `customers``` parses to *no tables and no columns* under sqlglot's
generic dialect, so a gate looking for undeclared tables or sensitive columns finds
nothing to object to and allows the statement. The guard must therefore read every
statement in the same grammar the executor will run it in.

Two properties this module exists to guarantee:

* **The map is explicit.** ``StorageType.lower()`` is *not* a sqlglot dialect name —
  ``PostgreSQL`` -> ``postgresql`` and ``SQLServer`` -> ``sqlserver`` both raise, while the
  other nine happen to work. A derived map would fail on the most common engine and on the
  one whose quoting diverges most, so every engine is written out by hand.
* **An unmapped engine is detectable.** Resolution raises rather than falling back to the
  generic dialect, because falling back is precisely the failure this module prevents. A
  new ``StorageType`` added without a dialect decision fails the map's test rather than
  silently reverting the guards to generic parsing for that engine.

Dialect resolution never parses SQL, so it does not disturb the guard battery's
parse-exactly-once property.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from .models import StorageType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import Datasource


class DialectUnresolved(Exception):
    """The engine a statement would run on cannot be determined.

    The message is value-free — it describes the shape of the problem and never carries
    statement text, data values, schema/column names or driver output — so a caller may
    use it verbatim as a refusal reason.
    """


# StorageType -> sqlglot dialect name. Written out per engine on purpose: see the module
# docstring for why lowercasing the StorageType is wrong. Verified against sqlglot 30.10.0.
_SQLGLOT_DIALECT: dict[str, str] = {
    "PostgreSQL": "postgres",  # NOT "postgresql" — sqlglot rejects that spelling
    "MySQL": "mysql",
    "Snowflake": "snowflake",
    "BigQuery": "bigquery",
    "Redshift": "redshift",
    "SQLite": "sqlite",
    "DuckDB": "duckdb",
    "SQLServer": "tsql",  # NOT "sqlserver" — sqlglot names T-SQL "tsql"
    "Databricks": "databricks",
    "Trino": "trino",
    "Oracle": "oracle",
}


def sqlglot_dialect(storage_type: str) -> str:
    """Map one declared engine to its sqlglot dialect name.

    Raises DialectUnresolved when the engine has no mapping, so that a `StorageType`
    added without a dialect decision is caught rather than parsed generically.
    """
    dialect = _SQLGLOT_DIALECT.get(storage_type)
    if dialect is None:
        raise DialectUnresolved(
            "the datasource declares a storage engine this build cannot parse SQL for"
        )
    return dialect


def resolve_datasource_dialect(org: "Datasource") -> str:
    """Resolve the sqlglot dialect for every statement run against `org`.

    A datasource resolves to a single live database — credentials are looked up once per
    (organization, profile) and carry no connection name — so all of its declared storage
    connections must agree on the engine for the guard to know which grammar applies.

    Raises DialectUnresolved when no connection is declared, or when the declared
    connections name more than one engine.
    """
    engines = {sc.storage_type for sc in org.storage_connections}
    if not engines:
        raise DialectUnresolved(
            "the datasource declares no storage connection, so the engine its SQL would "
            "run on is unknown"
        )
    if len(engines) > 1:
        # Not reachable through a normal deployment today (one credential per datasource
        # means a second engine has no way to be queried), but a model can express it, so
        # it is refused rather than resolved by picking one arbitrarily.
        raise DialectUnresolved(
            f"the datasource declares {len(engines)} different storage engines, so the "
            "engine its SQL would run on is ambiguous"
        )
    return sqlglot_dialect(engines.pop())


def supported_storage_types() -> tuple[str, ...]:
    """Every engine with an explicit dialect mapping."""
    return tuple(_SQLGLOT_DIALECT)


def unmapped_storage_types() -> tuple[str, ...]:
    """Declared `StorageType` members that have no dialect mapping.

    Empty by construction; the map's test asserts it stays empty so that adding an engine
    without a dialect decision fails rather than degrading that engine to generic parsing.
    """
    return tuple(t for t in get_args(StorageType) if t not in _SQLGLOT_DIALECT)


__all__ = [
    "DialectUnresolved",
    "resolve_datasource_dialect",
    "sqlglot_dialect",
    "supported_storage_types",
    "unmapped_storage_types",
]
