"""The F9 safety regression corpus (ACE-040) — the single, canonical source of the adversarial
attack vectors and the demo schema they run against.

One place defines: (a) the demo model/datasource schema (`SCHEMA`), from which the harness derives
BOTH the semantic model (the `Organization` the guards scope against) AND the physical datasource
(the SQLite/Postgres tables governed queries actually execute against), and (b) `CASES` — every
attack class mapped to its expected `Envelope` outcome. The end-to-end corpus test parametrizes
`CASES` across both surfaces (stdio + HTTP) and both model paths (file-served + DB-served); the
outcome is asserted the same way regardless.

`expect` is one of:
  - a refusal `kind` string (e.g. "permission", "table_out_of_scope") — the query is refused;
  - "bounded" — an availability control fired: EITHER a `resource_limit` refusal OR an ok Envelope
    flagged `data.truncated` with a `row_cap` in `applied` (a runaway result was capped, never silent);
  - "ok" — a governed query returns successfully with no false refusal.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── the demo schema — single-sourced; the harness builds the model AND the datasource from this ──
# table -> {"columns": [(name, sqlite_type), ...], "sensitive": {col, ...}, "rows": [tuple, ...]}
SCHEMA: dict[str, dict] = {
    "orders": {
        "columns": [
            ("id", "INTEGER"),
            ("customer_id", "INTEGER"),
            ("amount", "REAL"),
            ("status", "TEXT"),
        ],
        "sensitive": set(),
        "rows": [
            (1, 10, 100.0, "paid"),
            (2, 10, 50.0, "open"),
            (3, 20, 75.0, "paid"),
        ],
    },
    "customers": {
        "columns": [("id", "INTEGER"), ("name", "TEXT"), ("email", "TEXT"), ("region", "TEXT")],
        "sensitive": {"email"},  # PII: may be COUNTed/filtered/joined, never PROJECTED raw
        "rows": [
            (10, "Acme", "ops@acme.example", "west"),
            (20, "Globex", "hi@globex.example", "east"),
        ],
    },
}


@dataclass(frozen=True)
class Case:
    cls: str  # attack-class label (test id + grouping)
    sql: str
    expect: str  # a refusal kind, or "bounded", or "ok"
    note: str = ""
    max_rows: int | None = None  # per-call row cap (the availability row-cap case lowers it)

    @property
    def id(self) -> str:
        return f"{self.cls}:{self.note or self.sql[:40]}"


CASES: list[Case] = [
    # ── class 1: integrity / confinement → refused{permission} ──────────────────────────────────
    Case("integrity", "DELETE FROM orders", "permission", "delete"),
    Case("integrity", "UPDATE orders SET amount = 0", "permission", "update"),
    Case("integrity", "DROP TABLE orders", "permission", "drop"),
    Case("integrity", "INSERT INTO orders (id) VALUES (9)", "permission", "insert"),
    Case("integrity", "SELECT 1; DROP TABLE orders", "permission", "multi-statement"),
    Case("integrity", "SELECT pg_sleep(10)", "permission", "sleep-fn"),
    Case("integrity", "SELECT id FROM orders FOR UPDATE", "permission", "row-lock"),
    Case("integrity", "SELECT id INTO x FROM orders", "permission", "select-into"),
    # ── class 2a: object scope — undeclared table → table_out_of_scope ──────────────────────────
    Case("table_scope", "SELECT id FROM secret_table", "table_out_of_scope", "undeclared"),
    Case(
        "table_scope",
        "SELECT o.id FROM orders o JOIN secret_table s ON s.id = o.id",
        "table_out_of_scope",
        "join",
    ),
    Case(
        "table_scope",
        "SELECT id FROM orders UNION SELECT id FROM secret_table",
        "table_out_of_scope",
        "set-op-arm",
    ),
    Case(
        "table_scope",
        "SELECT id FROM orders EXCEPT SELECT id FROM secret_table",
        "table_out_of_scope",
        "except-arm",
    ),
    # ── class 2b: SELECT * → select_star (incl. hidden in a set-op arm) ──────────────────────────
    Case("select_star", "SELECT * FROM orders", "select_star", "star"),
    Case("select_star", "SELECT o.* FROM orders o", "select_star", "qualified-star"),
    Case(
        "select_star",
        "SELECT id FROM orders UNION SELECT * FROM customers",
        "select_star",
        "set-op-arm-star",
    ),
    # ── class 2c: undeclared column → column_out_of_scope ────────────────────────────────────────
    Case("column_scope", "SELECT bogus FROM orders", "column_out_of_scope", "undeclared-col"),
    Case(
        "column_scope",
        "SELECT id FROM orders UNION SELECT bogus FROM customers",
        "column_out_of_scope",
        "set-op-arm-col",
    ),
    # ── class 3: fail-closed scopability → unscopable_sql (under enforce, the default posture) ────
    Case("unscopable", "SELECT x FROM (VALUES (1), (2)) AS v(x)", "unscopable_sql", "values"),
    Case(
        "unscopable", "SELECT g FROM generate_series(1, 10) AS t(g)", "unscopable_sql", "table-fn"
    ),
    Case(
        "unscopable",
        "SELECT o.id FROM orders o, (VALUES (1)) AS v(x)",
        "unscopable_sql",
        "comma-join-values",
    ),
    # ── class 4: recon / metadata deny-list → recon ─────────────────────────────────────────────
    Case("recon", "SELECT version()", "recon", "version-fn"),
    Case("recon", "SELECT current_user", "recon", "current-user"),
    Case(
        "recon", "SELECT table_name FROM information_schema.tables", "recon", "information_schema"
    ),
    Case("recon", "SELECT relname FROM pg_catalog.pg_class", "recon", "pg_catalog"),
    # ── class 5: availability — a runaway result is bounded (row-cap truncate+flag OR timeout) ────
    # A capacity query returning more rows than the (test-lowered) cap must come back flagged, never
    # silently cut. Driven with a low AGAMI_SQL_MAX_ROWS by the harness. (A timeout variant is a
    # separate slow-marked case; the row-cap path is the deterministic availability proof.)
    Case("availability", "SELECT id FROM orders", "bounded", "row-cap-truncate", max_rows=1),
    # ── class 7: governed queries still pass → ok (no false refusals) ────────────────────────────
    Case("governed", "SELECT id, amount FROM orders", "ok", "projection"),
    Case("governed", "SELECT status, COUNT(id) AS n FROM orders GROUP BY status", "ok", "group-by"),
    Case(
        "governed",
        "SELECT c.name, COUNT(o.id) AS n FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        "ok",
        "join-aggregate",
    ),
    Case("governed", "SELECT COUNT(email) AS n FROM customers", "ok", "sensitive-in-count-ok"),
]
