"""The F9 safety regression corpus — the adversarial vectors and the demo schema they run against.

One place defines two things:

  * `SCHEMA` — the demo model/datasource, from which the harness derives BOTH the semantic model
    (the `Datasource` the gates scope against) AND the physical warehouse (the SQLite/Postgres
    tables the governed queries actually execute against). Single-sourced so the model and the
    warehouse cannot describe different tables.
  * `CASES` — every attack class mapped to the outcome the chokepoint must produce for it.

`Case.rule` holds a `guardrail.RULE_*` **symbol**, never a string literal, and `None` means the
vector is governed and must come back `ok`. `reason` is deliberately NOT stored: it is asserted as
`guardrail.REASON_FOR_RULE[case.rule]`, so the contract's own enum stays the single source and a
vector cannot pin a reason the enum disagrees with.

There is no `max_rows` field. The per-call row cap was deleted with the truncate-and-flag arm, so
the availability class is driven by the deployment ceiling `AGAMI_SQL_MAX_ROWS` that the harness
sets on the server process. Porting the field would have reintroduced a knob that no longer exists.

`red_on_main` marks a vector whose expected outcome this branch does not produce yet. Those are
`xfail(strict=True)` at the driver, so they flip green on their own when the owning gate lands and
a premature "fix" cannot hide in them. Each one was MEASURED — run through the four model gates on
this branch — rather than assumed by group; the two that are marked are the only two that pass
through all four unrefused.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import guardrail  # noqa: E402

# --- the demo schema — the harness builds the model AND the warehouse from this ---------------
#
# table -> {"columns": [(name, sqlite_type), ...], "sensitive": {col, ...}, "rows": [tuple, ...]}
# The seed rows are load-bearing: the governed vectors have to return real data for `ok` to mean
# anything, and the availability vectors need more rows than the harness's lowered ceiling.
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
        # Sensitive is a model FACT, not a gate: a flagged column may be counted, filtered, joined
        # AND projected. The sensitive-projection refusal was deleted (it was an access policy, and
        # this layer holds none), so no vector below may expect a refusal for reading it.
        "sensitive": {"email"},
        "rows": [
            (10, "Acme", "ops@acme.example", "west"),
            (20, "Globex", "hi@globex.example", "east"),
        ],
    },
}


@dataclass(frozen=True)
class Case:
    """One adversarial vector and the verdict the chokepoint owes it."""

    cls: str  # attack-class label — what the vector ATTEMPTS, which is not always what refuses it
    sql: str
    rule: str | None  # a `guardrail.RULE_*` symbol, or None for a governed vector
    note: str = ""
    # Engines this case is meaningful on, or None for every engine. Identifier quoting is
    # engine-specific — a backtick is an identifier quote on MySQL and SQLite but not valid SQL on
    # Postgres — so a quoting case pinned to one engine would assert a different verdict on the
    # other. Naming the engine keeps the case honest instead of letting it pass on whichever path
    # happens to accept it.
    engines: tuple[str, ...] | None = None
    # True when this branch does NOT produce `rule` for this vector yet. Measured per vector, never
    # assumed by group: a strict xfail that passes is a build failure, so a blanket marker over a
    # class whose members already behave would fail the build on the ones that work.
    red_on_main: bool = False

    def runs_on(self, engine: str) -> bool:
        return self.engines is None or engine in self.engines

    @property
    def id(self) -> str:
        """The pytest test id. Selection is by MARKER, never by an `-k` substring over this, so
        the label is free to say whatever reads best."""
        return f"{self.cls}:{self.note or self.sql[:40]}"


CASES: list[Case] = [
    # ── class 1: integrity / confinement → read_only ────────────────────────────────────────────
    # `permission` was the prior vocabulary here and it is now a `Failure` kind — the DATABASE
    # refusing a read to our role — so none of these may borrow it. Our decision to block a write
    # is `read_only`.
    Case("integrity", "DELETE FROM orders", guardrail.RULE_READ_ONLY, "delete"),
    Case("integrity", "UPDATE orders SET amount = 0", guardrail.RULE_READ_ONLY, "update"),
    Case("integrity", "DROP TABLE orders", guardrail.RULE_READ_ONLY, "drop"),
    Case("integrity", "INSERT INTO orders (id) VALUES (9)", guardrail.RULE_READ_ONLY, "insert"),
    Case("integrity", "SELECT 1; DROP TABLE orders", guardrail.RULE_READ_ONLY, "multi-statement"),
    Case("integrity", "SELECT pg_sleep(10)", guardrail.RULE_READ_ONLY, "sleep-fn"),
    Case("integrity", "SELECT id FROM orders FOR UPDATE", guardrail.RULE_READ_ONLY, "row-lock"),
    Case("integrity", "SELECT id INTO x FROM orders", guardrail.RULE_READ_ONLY, "select-into"),
    # ── class 2a: object scope — an undeclared table → table_scope ──────────────────────────────
    Case("table_scope", "SELECT id FROM secret_table", guardrail.RULE_TABLE_SCOPE, "undeclared"),
    Case(
        "table_scope",
        "SELECT o.id FROM orders o JOIN secret_table s ON s.id = o.id",
        guardrail.RULE_TABLE_SCOPE,
        "join",
    ),
    Case(
        "table_scope",
        "SELECT id FROM orders UNION SELECT id FROM secret_table",
        guardrail.RULE_TABLE_SCOPE,
        "set-op-arm",
    ),
    Case(
        "table_scope",
        "SELECT id FROM orders EXCEPT SELECT id FROM secret_table",
        guardrail.RULE_TABLE_SCOPE,
        "except-arm",
    ),
    # ── class 2b: SELECT * → select_star (incl. hidden in a set-op arm) ─────────────────────────
    # `undetermined`, not `out_of_scope`: a star is not a reach, it is an inability to decide
    # whether there is one, because resolving `*` to a column list needs a catalog the guard does
    # not have.
    Case("select_star", "SELECT * FROM orders", guardrail.RULE_SELECT_STAR, "star"),
    Case("select_star", "SELECT o.* FROM orders o", guardrail.RULE_SELECT_STAR, "qualified-star"),
    Case(
        "select_star",
        "SELECT id FROM orders UNION SELECT * FROM customers",
        guardrail.RULE_SELECT_STAR,
        "set-op-arm-star",
    ),
    # ── class 2c: an undeclared column → column_scope ───────────────────────────────────────────
    Case(
        "column_scope",
        "SELECT bogus FROM orders",
        guardrail.RULE_COLUMN_SCOPE,
        "undeclared-col",
    ),
    Case(
        "column_scope",
        "SELECT id FROM orders UNION SELECT bogus FROM customers",
        guardrail.RULE_COLUMN_SCOPE,
        "set-op-arm-col",
    ),
    # ── class 3: fail-closed scopability → unscopable ───────────────────────────────────────────
    # The rule has TWO producers, deliberately: `runtime.check_readable` refuses a statement that
    # resolves to no named table, and `runtime.check_scopable` refuses a FROM/JOIN source the scope
    # walk cannot read. All three vectors are green; the table function and the comma-joined VALUES
    # were red until the second producer landed, and the strict markers are what caught it.
    Case(
        "unscopable",
        "SELECT x FROM (VALUES (1), (2)) AS v(x)",
        guardrail.RULE_UNSCOPABLE,
        "values",
    ),
    Case(
        "unscopable",
        "SELECT g FROM generate_series(1, 10) AS t(g)",
        guardrail.RULE_UNSCOPABLE,
        "table-fn",
    ),
    Case(
        "unscopable",
        "SELECT o.id FROM orders o, (VALUES (1)) AS v(x)",
        guardrail.RULE_UNSCOPABLE,
        "comma-join-values",
    ),
    # ── class 4: recon / metadata ───────────────────────────────────────────────────────────────
    # The attack class is one thing and the rule that stops it is two, deliberately. The recon
    # deny-list is FUNCTIONS only; a catalog RELATION is a table the model does not declare, which
    # is the model's job to refuse. Both vectors stay in this class because the class is what the
    # caller was attempting, and moving them would leave the class claiming coverage it lost.
    Case("recon", "SELECT version()", guardrail.RULE_RECON, "version-fn"),
    Case("recon", "SELECT current_user", guardrail.RULE_RECON, "current-user"),
    Case(
        "recon",
        "SELECT table_name FROM information_schema.tables",
        guardrail.RULE_TABLE_SCOPE,
        "information_schema",
    ),
    Case(
        "recon",
        "SELECT relname FROM pg_catalog.pg_class",
        guardrail.RULE_TABLE_SCOPE,
        "pg_catalog",
    ),
    # ── class 5: availability — a result over the deployment ceiling → resource_limit ───────────
    # The truncate-and-flag arm is deleted: an over-ceiling result is REFUSED and carries no data,
    # never trimmed and flagged. Both vectors are driven by `AGAMI_SQL_MAX_ROWS` lowered on the
    # harness's server process, because there is no per-call cap to lower any more.
    Case("availability", "SELECT id FROM orders", guardrail.RULE_RESOURCE_LIMIT, "row-cap"),
    # A runaway SHAPE, not just a low ceiling: a cross join multiplies rows out of the caller's
    # control, and the bound applied at the shared executor is what keeps the result finite.
    Case(
        "availability",
        "SELECT o.id FROM orders o CROSS JOIN customers c",
        guardrail.RULE_RESOURCE_LIMIT,
        "cross-join-runaway",
    ),
    # ── class 7: governed queries still pass → ok (no false refusals) ───────────────────────────
    Case("governed", "SELECT id, amount FROM orders", None, "projection"),
    Case("governed", "SELECT status, COUNT(id) AS n FROM orders GROUP BY status", None, "group-by"),
    Case(
        "governed",
        "SELECT c.name, COUNT(o.id) AS n FROM customers c JOIN orders o "
        "ON o.customer_id = c.id GROUP BY c.name",
        None,
        "join-aggregate",
    ),
    Case("governed", "SELECT COUNT(email) AS n FROM customers", None, "sensitive-in-count-ok"),
    # ── class 8: the same verdicts when the statement is written in the engine's own quoting ────
    # A statement read in the wrong grammar does not describe itself: under a generic parse a
    # backtick-quoted statement resolves to no tables and no columns, so every scope gate finds
    # nothing to object to and allows it. These run through the full harness because that is the
    # only place the guarantee is proved end to end, with the engine declared.
    #
    # SQLite accepts backticks and brackets as identifier quoting; Postgres does not, where the
    # same text is not valid SQL at all — so those are pinned to the engine they mean something on.
    Case(
        "quoting",
        "SELECT `id` FROM `undeclared`",
        guardrail.RULE_TABLE_SCOPE,
        "backtick-undeclared-table",
        engines=("SQLite",),
    ),
    Case(
        "quoting",
        "SELECT `nosuchcol` FROM `orders`",
        guardrail.RULE_COLUMN_SCOPE,
        "backtick-undeclared-col",
        engines=("SQLite",),
    ),
    Case(
        "quoting",
        "SELECT `email` FROM `customers`",
        None,
        "backtick-sensitive-is-seen",
        engines=("SQLite",),
    ),
    Case(
        "quoting",
        "SELECT * FROM `orders`",
        guardrail.RULE_SELECT_STAR,
        "backtick-star",
        engines=("SQLite",),
    ),
    Case(
        "quoting",
        "SELECT [id] FROM [undeclared]",
        guardrail.RULE_TABLE_SCOPE,
        "bracket-undeclared-table",
        engines=("SQLite",),
    ),
    # Valid identifier quoting on both engines, so it runs everywhere.
    Case(
        "quoting",
        'SELECT "id" FROM "undeclared"',
        guardrail.RULE_TABLE_SCOPE,
        "quoted-undeclared-table",
    ),
    # The read-only lexer is a separate code path from the dialect-aware parse; a write stays
    # refused whatever the quoting, on every engine.
    Case("quoting", "DELETE FROM `orders`", guardrail.RULE_READ_ONLY, "backtick-delete"),
    # Ordinary shapes a governed query takes. These carry no attack — they exist so that a change
    # which tightens parsing has to prove it did not start refusing valid SQL. Without them the "no
    # false refusal" side of the corpus rests on four statements, and a tightening that broke
    # ordinary queries could still pass.
    Case("governed", 'SELECT "id", "amount" FROM "orders"', None, "quoted-identifiers"),
    Case("governed", "SELECT o.id, o.amount FROM orders o", None, "aliased-table"),
    Case("governed", "SELECT id FROM orders WHERE status = 'paid'", None, "where-literal"),
    Case("governed", "SELECT id, amount FROM orders ORDER BY amount DESC", None, "order-by"),
    Case("governed", "SELECT DISTINCT status FROM orders", None, "distinct"),
    Case(
        "governed",
        "SELECT status, COUNT(id) AS n FROM orders GROUP BY status HAVING COUNT(id) > 0",
        None,
        "having",
    ),
    Case(
        "governed",
        "WITH paid AS (SELECT id, amount FROM orders WHERE status = 'paid') "
        "SELECT COUNT(id) AS n FROM paid",
        None,
        "cte",
    ),
    Case(
        "governed",
        "SELECT id FROM orders WHERE customer_id IN "
        "(SELECT id FROM customers WHERE region = 'west')",
        None,
        "subquery-in-where",
    ),
    Case(
        "governed",
        "SELECT id, CASE WHEN amount > 60 THEN 'big' ELSE 'small' END AS bucket FROM orders",
        None,
        "case-expression",
    ),
    Case("governed", "SELECT id FROM orders UNION ALL SELECT id FROM customers", None, "union-all"),
    # ── class 9: reaching past the datasource entirely → read_only ──────────────────────────────
    # Each is a different way to get somewhere the caller's SELECT was never supposed to reach: the
    # database server's filesystem, another server, the session/transaction the connection is
    # pooled in, a statement a comment hides from a naive splitter, or a write smuggled inside a
    # CTE that opens with the allowed keyword.
    Case("file_fn", "SELECT pg_read_file('/etc/passwd')", guardrail.RULE_READ_ONLY, "read-file"),
    Case("file_fn", "SELECT lo_import('/etc/passwd')", guardrail.RULE_READ_ONLY, "lo-import"),
    Case(
        "network_fn",
        "SELECT dblink('dbname=x', 'SELECT 1')",
        guardrail.RULE_READ_ONLY,
        "remote-query",
    ),
    Case(
        "network_fn",
        "SELECT dblink_exec('dbname=x', 'SELECT 1')",
        guardrail.RULE_READ_ONLY,
        "remote-exec",
    ),
    # TCL escapes the read-only transaction; a session SET corrupts state that outlives the call on
    # a pooled connection. Neither is an analytics primitive.
    Case("session", "SELECT id FROM orders; COMMIT", guardrail.RULE_READ_ONLY, "tcl-commit"),
    Case("session", "SET ROLE postgres", guardrail.RULE_READ_ONLY, "escalate-via-set"),
    # The line comment swallows the statement separator, so a splitter that trusts `;` sees ONE
    # statement — the keyword check reads what is actually there and still refuses.
    Case(
        "comment",
        "SELECT id FROM orders -- ;\nDROP TABLE orders",
        guardrail.RULE_READ_ONLY,
        "line-comment-hides-separator",
    ),
    # Data-modifying CTEs: the statement opens with WITH (allowed) and the write hides in the body.
    Case(
        "cte_write",
        "WITH t AS (DELETE FROM orders RETURNING id) SELECT id FROM t",
        guardrail.RULE_READ_ONLY,
        "cte-delete",
    ),
    Case(
        "cte_write",
        "WITH t AS (INSERT INTO orders (id) VALUES (9) RETURNING id) SELECT id FROM t",
        guardrail.RULE_READ_ONLY,
        "cte-insert",
    ),
]


# The two engines the corpus is driven on. They are named HERE, next to the `engines` field they are
# compared against, so a vector pinned to one engine and a path that claims to be that engine are
# spelling the same string — and so the DB-path count below can be derived rather than restated.
FILE_PATH_ENGINE = "SQLite"
DB_PATH_ENGINE = "PostgreSQL"

# The number of vectors the DB-backed run must collect, and the ONLY place that number exists.
#
# It is read by the parametrizer that produces those vectors AND by the session hook that refuses to
# let the run finish short, so the two cannot disagree — and neither can be edited into agreement
# with a thinned corpus, because both read the corpus. That is the whole point: the job this
# replaces selected its work with `pytest -k "db_path or role"`, a substring match on the node id,
# so renaming a test dropped 102 of 108 vectors and the job still exited 0. A count derived from
# `CASES` cannot be renamed out of existence — it can only be wrong, loudly.
EXPECTED_DB_VECTORS = len([case for case in CASES if case.runs_on(DB_PATH_ENGINE)])


# The stdio bound, derived from `CASES` so it cannot be restated wrong. The stdio route spawns a
# process per call, so driving the whole corpus on it is ~56 subprocess spawns on the critical path
# of every PR; what stdio is there to prove is that the chokepoint's verdict does not depend on the
# transport, and one vector per distinct rule proves exactly that. The read-only class is carried
# whole because it is the largest attack surface and its vectors take different paths INTO the same
# gate (keyword, comment-hidden separator, CTE body), which is a difference a single representative
# would hide. `None` counts as a distinct rule, so a governed vector rides along and the subset
# cannot pass by refusing everything.
STDIO_SUBSET: list[Case] = [
    case
    for index, case in enumerate(CASES)
    if case.rule == guardrail.RULE_READ_ONLY
    or index == next(i for i, c in enumerate(CASES) if c.rule == case.rule)
]
