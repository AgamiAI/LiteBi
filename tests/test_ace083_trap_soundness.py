"""ACE-083 — the shapes a soundness fix may never report clean, pinned before any fix lands.

ACE-083 makes the multiplication report say what a join actually multiplies. Three of its four
corrections are LOOSENINGS: an aggregate a duplication cannot move stops being called a trap, a
many-side column that is not on the value path stops attributing the fan, and a reference the
statement cannot see stops entering the alias map. Each one narrows what gets reported, and the
whole existing battery guards the other direction — every shipped assertion says "this IS reported"
and not one of them says "this is still not reported clean".

That asymmetry is the hazard. A loosening that goes one step too far turns a genuinely inflated
number into `not_multiplied`, which is a positive claim on the receipt that the number is sound.
Nothing on `main` fails when that happens. So the reverse corpus lands FIRST, in its own file,
before a line of production code moves.

Almost every member of the reverse corpus passes on the base implementation, and that is the point:
those are not the tests for the new behaviour, they are the tests for the behaviour that must
survive it. Three members do fail on base — the unaliased `VALUES` list and the two set operations
whose CTE is bound above the arms — and each of those was a false clean measured on base while this
slice was being built, so they are corpus members and regression tests at once. The batteries below
the corpus are the per-slice tests of what each correction actually changed, and they land beside it
rather than in files of their own so that a loosening and the pins guarding it are read together.

The corpus rows carry the ITEMS each statement is known to inflate rather than only the SQL, because
the property asserted over every aggregate in a statement can only hold members every one of whose
arms is inflated. That shut out the mixed statements — one sound number beside one unsound one —
that most real multi-aggregate SQL is, and a rule that suppresses per STATEMENT rather than per ITEM
is invisible until one of those is in the corpus.

Slice 2 (S1) is here: an aggregate a duplication cannot move keeps the multiplication and loses the
word trap, and the marker sentence that reported the detector's old shortcoming is deleted.

Slice 3 (S2) is here: the fan is attributed by where a column sits on the aggregate's VALUE path
rather than by its presence anywhere inside the aggregate.

Slices 4 and 5 (S3) are here, and they are ONE commit rather than two. The scope filter and the
grain plumbing were planned as separate slices; they cannot be, and the corpus is what proved it.
The filter stops a reference written inside a CTE body from entering the outer query's map, which
is correct and is half of S3 — but `CTE_LAUNDERED_FAN` reported `multiplied` at HEAD only BECAUSE
of that leak, and with the leak gone and no resolution for what `oi` stands for, a genuinely
inflated statement read `not_multiplied`. The intermediate state fails the corpus's own criterion,
so it is not a legal place to stop.

The last battery in this file is the one two independent re-reviews arrived at from opposite ends,
and it is one root cause rather than several. `_value_sources` and `_grain_preserving_source` were
both DENYLISTS with an unsafe default: one enumerated the alternation node types and let every other
node union its children, the other rejected three `exp.Select` arguments and accepted the rest, and
in both an unanticipated shape landed on the side that CLEARS a finding. Seven statements were
measured `not_multiplied` through those two holes — four of them ordinary Snowflake / Redshift /
Oracle analytics SQL — and they are corpus members above rather than a corpus of their own, because
the property they violate is the one this file already asserts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

from sqlglot import exp, parse_one  # noqa: E402

# `rt` is imported FROM the fixture's module rather than beside it, and that is load-bearing.
# `test_semantic_model_runtime` puts `plugins/agami/scripts` on `sys.path`, while the ACE-060 and
# ACE-099 batteries put `packages/agami-core/src` there. Importing `semantic_model.runtime` down a
# different path than the fixture came down yields a second module object with its own
# `MULTIPLIED` / `NOT_MULTIPLIED` string objects and its own detector — so an assertion here could
# compare a status produced by one copy against a constant defined by the other. One import.
from test_semantic_model_runtime import _sales_org, m, rt  # noqa: E402

# Where the subprocess probe below finds the SAME source `rt` was imported from, derived from the
# module object rather than rebuilt from the repo root. The header above explains why two copies of
# `semantic_model` reached down two `sys.path` entries is a real hazard in this file; a determinism
# test that compared a child process running one copy against a parent running the other would be
# measuring the wrong difference.
PKG_SRC = Path(rt.__file__).resolve().parents[1]

# The one-to-many the whole corpus is built on: `orders` is the ONE side, `order_items` the MANY,
# and the model declares that edge. Written once so "the same fan join in every member" is a fact
# of the code rather than of the typing.
FAN_JOIN = "FROM orders JOIN order_items ON order_items.order_id = orders.id"

# 1. The plain fan. The shape every other member is a disguise of.
PLAIN_FAN = f"SELECT SUM(orders.total_amount) {FAN_JOIN}"

# 2. The measure wrapped in arithmetic. The value path is still a one-side column; scaling every
#    duplicated row scales the inflated total.
SCALED_FAN = f"SELECT SUM(orders.total_amount * 1.1) {FAN_JOIN}"

# 3. The many-side column is the CASE predicate, not the value. A value-path rule that attributes
#    the fan to `order_items` here would clear a total that is still summed once per item.
CONDITIONED_FAN = (
    f"SELECT SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount END) {FAN_JOIN}"
)

# 4. The many-side column is the ordering arm. Same trap as 3, on the branch that reads an
#    `exp.Order` rather than an `exp.Case`, and the concatenation genuinely repeats each status.
ORDERED_FAN = f"SELECT STRING_AGG(orders.status, ',' ORDER BY order_items.id) {FAN_JOIN}"

# 5. The many side behind a derived table. `d` is an `exp.Subquery` and binds no table name of its
#    own, so a scope filter that drops what it cannot bind drops `order_items` — and the fan with
#    it. This is the member that measures the false clean the filter itself creates.
DERIVED_TABLE_FAN = (
    "SELECT SUM(orders.total_amount) FROM orders "
    "JOIN (SELECT order_id FROM order_items) d ON d.order_id = orders.id"
)

# 6. The many side behind a CTE that changes nothing. `oi` is one grain-preserving hop from
#    `order_items`, so the join still multiplies `orders` exactly as member 1 does.
CTE_LAUNDERED_FAN = (
    "WITH oi AS (SELECT * FROM order_items) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)

# 7. The join is inside the CTE body, so the outer statement joins nothing and the rows behind
#    `SUM(j.total_amount)` were multiplied where the walk does not look. Nothing about this one is
#    determinable, which is a different answer from clean and must stay a different answer.
JOINED_CTE_BODY = (
    "WITH j AS (SELECT o.id AS id, o.total_amount AS total_amount FROM orders o "
    "JOIN order_items i ON i.order_id = o.id) SELECT SUM(j.total_amount) FROM j"
)

# 8. The rows come from a source with no name at all. A `VALUES` list in a comma join multiplies
#    every order by the number of tuples in it, and there is no alias to hang the fact on. Added
#    when the scope filter's own alias guard was measured: the guard has to drop the wrapper of a
#    parenthesized table, and dropping this one with it would have been a false clean.
UNALIASED_VALUES_FAN = "SELECT SUM(orders.total_amount) FROM orders, (VALUES (1), (2))"

# 9. Members 6 and 1, wrapped in a set operation. A WITH sits ABOVE the arms and binds its name for
#    all of them, so an arm looking for the CTE inside itself finds nothing and the laundered fan
#    goes unseen — the same statement, one keyword further out, reading clean.
UNION_ARM_CTE_FAN = f"{CTE_LAUNDERED_FAN} UNION ALL {PLAIN_FAN}"

# 10. The same set operation with ONE inflated arm and one honest one, which is what most real
#     multi-aggregate SQL looks like. It could not be a member while the property ran over every
#     item in the statement — that constraint is why member 9 had to be built out of two inflated
#     arms — and it is the shape that says the laundered arm is found without the clean arm being
#     dragged in with it.
MIXED_SET_OPERATION = f"{CTE_LAUNDERED_FAN} UNION ALL SELECT SUM(orders.revenue) FROM orders"

# 11, 12. The same fan spelled two ways nothing else here spells it. The edge comes off the model's
#     declared relationship rather than off the JOIN keyword, so neither of these should differ from
#     member 1 — and until now nothing said so, which is what makes a rule keyed on `exp.Join`
#     cheap to write by accident.
COMMA_JOIN_FAN = ("SELECT SUM(orders.total_amount) FROM orders, order_items "
                  "WHERE order_items.order_id = orders.id")
LEFT_JOIN_FAN = ("SELECT SUM(orders.total_amount) FROM orders "
                 "LEFT JOIN order_items ON order_items.order_id = orders.id")

# 13. Member 5 turned around: the derived table is on the ONE side and the many side is the plain
#     declared table. `t` binds a name the `exp.Table` walk never sees, so the measure's own source
#     is what goes unresolved here rather than the fanning one.
DERIVED_TABLE_ON_THE_ONE_SIDE = ("SELECT SUM(t.total_amount) FROM (SELECT * FROM orders) t "
                                 "JOIN order_items ON order_items.order_id = t.id")

# 14-17. A ONE-side column off the value path. Members 3 and 4 move the MANY side around; these move
#     the one side, and that is the only direction that can manufacture a clean answer — a value
#     path narrowed until it holds no one-side column stops the aggregate reading the one side at
#     all, so the fan loop never reaches it and a total the join really does inflate reports sound.
#     Each was measured `not_multiplied` while the value path decided `sources`.
ONE_SIDE_IN_A_CASE_PREDICATE = (
    f"SELECT SUM(CASE WHEN orders.status = 'shipped' THEN 1 ELSE 0 END) {FAN_JOIN}"
)
ONE_SIDE_AS_A_SIMPLE_CASE_OPERAND = (
    f"SELECT SUM(CASE orders.status WHEN 'shipped' THEN 1 ELSE 0 END) {FAN_JOIN}"
)
ONE_SIDE_IN_A_CONDITIONAL_COUNT = (
    f"SELECT COUNT(CASE WHEN orders.status = 'shipped' THEN 1 END) {FAN_JOIN}"
)
BRANCHES_DISAGREEING_ABOUT_THE_GRAIN = (
    f"SELECT SUM(CASE WHEN orders.flag THEN orders.total_amount "
    f"ELSE order_items.quantity END) {FAN_JOIN}"
)

# 18-21. The same defect reached through a node that is not an `exp.Case`. Which sqlglot node type
#     spells a conditional is exactly what the value-path rule is scoped by, and members 1-9 vary it
#     not at all: `IF` is an `exp.If` that RENDERS back as a CASE, `NULLIF` and `COALESCE` are
#     functions whose arguments are not all values, and a `GROUP_CONCAT` separator is punctuation.
#     For a node that only combines values, contributing its columns by default is honest; for one
#     that mixes a predicate with its values it contributes the predicate that clears the fan.
IF_SPELLED_CONDITIONAL_FAN = (
    f"SELECT SUM(IF(order_items.quantity > 0, orders.total_amount, 0)) {FAN_JOIN}"
)
NULLIF_COMPARAND_FAN = f"SELECT SUM(NULLIF(orders.total_amount, order_items.quantity)) {FAN_JOIN}"
COALESCE_ALTERNATION_FAN = (
    f"SELECT SUM(COALESCE(orders.total_amount, order_items.quantity)) {FAN_JOIN}"
)
SEPARATOR_OFF_THE_MANY_SIDE_FAN = (
    f"SELECT STRING_AGG(orders.status, order_items.product_id) {FAN_JOIN}"
)

# Two fans off ONE dimension, the other thing members 1-9 never vary: how many many-sides a measure
# table has. Every one of them is a single declared one-to-many, so a rule that suppresses per
# STATEMENT and a rule that suppresses per EDGE are indistinguishable on all nine.
TWO_FAN_JOIN = ("FROM customers JOIN orders ON orders.customer_id = customers.id "
                "JOIN tickets ON tickets.customer_id = customers.id")

# 22. The value is a product of `customers` and `tickets` columns, so the tickets join is the grain
#     the value was defined at and multiplies nothing, while the orders join duplicates it. Exactly
#     one of the two edges is a reason to say nothing, and suppressing the whole finding because one
#     of them is the value's own grain reported this statement clean.
TWO_FANS_ONE_ON_THE_VALUE_PATH = f"SELECT SUM(customers.id * tickets.id) {TWO_FAN_JOIN}"

# 23. Two independent measures over one shared dimension, one of them wrapped in a CASE that puts
#     its only column in the predicate. `agg_sources` is derived across every site in the SELECT, so
#     a narrowing at one aggregate is not local: with two measures needed for a shared dimension to
#     be a chasm at all, one wrapped aggregate took the finding away from the other one too.
#     `SUM(tickets.id)` is not inside that CASE and never was.
CHASM_WITH_ONE_WRAPPED_AGGREGATE = (
    f"SELECT SUM(CASE WHEN orders.status = 'x' THEN 1 ELSE 0 END), SUM(tickets.id) {TWO_FAN_JOIN}"
)

# 24. Grouped FINER than the join key: one row per (order, product), joined on the order alone. The
#     CTE is genuinely the many side of its own edge and the fan is real.
CTE_GRAIN_BELOW_JOIN_KEY = (
    "WITH oi AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
    "GROUP BY order_id, product_id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)
# 25. Member 6 one hop further out. Two grain-preserving CTEs in a chain are still the same rows, so
#     the resolution has to be transitive or it answers correctly only at depth one.
TRANSITIVE_CTE = (
    "WITH a AS (SELECT * FROM order_items), b AS (SELECT * FROM a) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN b ON b.order_id = orders.id"
)
# 26. `_cte_names` and `_model_table_index` fold case; `_alias_map` preserves it. Every comparison
#     the resolution makes therefore has to be explicit about which side it is on, and this is the
#     shape that catches one that is not.
CASE_FOLDED_CTE = (
    "WITH OI AS (SELECT * FROM order_items) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN OI ON OI.order_id = orders.id"
)

# 27, 28. Two arms, each declaring its OWN `x` over a different table. Only the first arm joins, and
#     only the first arm's `x` is the one side of that join. Written in both orders because the
#     defect was order-dependent: read off the root, whichever arm's body the walk reached last won
#     the folded name for both.
SAME_NAME_PER_ARM = (
    "(WITH x AS (SELECT * FROM orders) SELECT SUM(x.total_amount) FROM x "
    "JOIN order_items oi ON oi.order_id = x.id) UNION ALL "
    "(WITH x AS (SELECT * FROM order_items) SELECT SUM(x.quantity) FROM x)"
)
SAME_NAME_PER_ARM_SWAPPED = (
    "(WITH x AS (SELECT * FROM order_items) SELECT SUM(x.quantity) FROM x) UNION ALL "
    "(WITH x AS (SELECT * FROM orders) SELECT SUM(x.total_amount) FROM x "
    "JOIN order_items oi ON oi.order_id = x.id)"
)

# 29. A `WITH` inside a `WHERE … IN (…)` subquery. It binds `order_items` for that subquery and for
#     nothing else, and the outer statement's `order_items` is the real declared table it joins.
NESTED_WITH_SHADOWING_A_REAL_TABLE = (
    "SELECT SUM(orders.total_amount) FROM orders "
    "JOIN order_items ON order_items.order_id = orders.id "
    "WHERE orders.id IN (WITH order_items AS (SELECT id FROM customers) "
    "SELECT id FROM order_items)"
)

# 30-33. The alternation spelled as a FUNCTION rather than as a keyword, which is where the same
#     defect class was found one node type over. `GREATEST` and `LEAST` carry `COALESCE`'s
#     `this` + `expressions` layout; `NVL2`'s three arguments are byte-identical to `IF`'s; `DECODE`
#     parses to a node of its own, `exp.DecodeCase`. Every one of them returns ONE of its arguments,
#     so a rule that unioned their operands put a many-side column on the value path and cleared the
#     fan. All four are ordinary Snowflake / Redshift / Oracle analytics SQL rather than contrived
#     shapes, and all four were measured `not_multiplied` against a base that said `multiplied`.
GREATEST_ALTERNATION_FAN = (
    f"SELECT SUM(GREATEST(orders.total_amount, order_items.quantity)) {FAN_JOIN}"
)
LEAST_ALTERNATION_FAN = f"SELECT SUM(LEAST(orders.total_amount, order_items.quantity)) {FAN_JOIN}"
NVL2_ALTERNATION_FAN = f"SELECT SUM(NVL2(order_items.quantity, orders.total_amount, 0)) {FAN_JOIN}"
DECODE_ALTERNATION_FAN = (
    f"SELECT SUM(DECODE(order_items.product_id, 1, orders.total_amount, 0)) {FAN_JOIN}"
)

# 34-36. A CTE body that multiplies its OWN rows with no JOIN, no GROUP BY and no DISTINCT in it.
#     `laterals`, `connect` and `match` are arguments of `exp.Select` that the grain-preserving
#     guard's three-name denylist never read, so each body resolved as though it handed back
#     `orders` row for row and the outer aggregate reported `not_multiplied` — against a base that
#     said `undetermined`, which is a false clean created by this branch. A LATERAL VIEW emits one
#     row per exploded element, an UNPIVOT one row per unpivoted column, and a CONNECT BY one row
#     per path through the hierarchy.
LATERAL_VIEW_CTE = (
    "WITH o AS (SELECT * FROM orders LATERAL VIEW EXPLODE(orders.status) t AS s) "
    "SELECT SUM(o.total_amount) FROM o"
)
UNPIVOT_CTE = (
    "WITH o AS (SELECT * FROM orders UNPIVOT (v FOR k IN (total_amount, revenue))) "
    "SELECT SUM(o.total_amount) FROM o"
)
CONNECT_BY_CTE = (
    "WITH o AS (SELECT * FROM orders CONNECT BY PRIOR id = customer_id) "
    "SELECT SUM(o.total_amount) FROM o"
)

# The same three constructs written STRAIGHT INTO the statement rather than into a CTE body, plus
# the PIVOT that escaped only by accident. `laterals`, `connect` and `match` are siblings of `from_`
# on `exp.Select` and `pivots` rides the `exp.Table`, so a binding walk over `exp.From` and
# `exp.Join` reached none of them and the alias map looked like a single table. These are corpus
# members of the ungated `cmd_preflight` / `cmd_prepare` surface — see the test that runs them —
# and the false clean on them is PRE-EXISTING rather than created here.
LATERAL_VIEW_SOURCE = (
    "SELECT SUM(orders.total_amount) FROM orders LATERAL VIEW EXPLODE(orders.status) t AS tag"
)
UNPIVOT_SOURCE = (
    "SELECT SUM(o.total_amount) FROM orders o UNPIVOT (v FOR k IN (total_amount, revenue))"
)
CONNECT_BY_SOURCE = "SELECT SUM(orders.total_amount) FROM orders CONNECT BY PRIOR id = customer_id"
PIVOT_SOURCE = (
    "SELECT SUM(p.total_amount) FROM orders PIVOT (SUM(total_amount) FOR status IN ('a')) p"
)
# `MATCH_RECOGNIZE` does not parse on sqlglot's default grammar, so it cannot be driven through
# `pre_flight_check` against `_sales_org`, which declares no dialect. It is asserted on the binding
# walk directly, off a tree parsed with a dialect that does speak it.
MATCH_RECOGNIZE_SOURCE = (
    "SELECT SUM(orders.total_amount) FROM orders MATCH_RECOGNIZE ("
    "PARTITION BY customer_id ORDER BY id MEASURES id AS m ONE ROW PER MATCH "
    "PATTERN (a+) DEFINE a AS a.id > 0)"
)

# A grain-changing CTE that RENAMES its grain column on the way out. `_group_by_grain` reads the
# body's input names and the join key is the body's output name, so the comparison was between two
# spellings of one column and reported a fan on a CTE that is one row per join key. Over-reporting,
# so it is not a corpus member; a false positive on legitimate SQL all the same.
CTE_RENAMING_ITS_GRAIN = (
    "WITH x AS (SELECT id AS order_id FROM customers GROUP BY id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN x ON x.order_id = orders.id"
)

# The corpus, named so the later slices can re-run it unchanged rather than restating it. Labels are
# what a failure prints, so they name the disguise and not the SQL.
#
# The third field is the ITEMS the statement is known to inflate, by the label the receipt gives
# them. The property used to run over every aggregate in the statement, which forced every member to
# be inflated in all of its arms — member 9 is built out of two inflated arms for exactly that
# reason — and so shut the corpus out of the mixed statements that are most of real multi-aggregate
# SQL. Naming the items instead lets a member carry an honestly clean aggregate beside an inflated
# one, and members 10, 23, 27 and 28 all do.
INFLATED_SHAPES = [
    ("plain fan", PLAIN_FAN, ["SUM(orders.total_amount)"]),
    ("measure scaled by a literal", SCALED_FAN, ["SUM(orders.total_amount * 1.1)"]),
    ("many side in the CASE predicate", CONDITIONED_FAN,
     ["SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount END)"]),
    ("many side in the ORDER BY arm", ORDERED_FAN,
     ["GROUP_CONCAT(orders.status ORDER BY order_items.id, ',')"]),
    ("many side behind a derived table", DERIVED_TABLE_FAN, ["SUM(orders.total_amount)"]),
    ("many side behind a CTE", CTE_LAUNDERED_FAN, ["SUM(orders.total_amount)"]),
    ("join inside the CTE body", JOINED_CTE_BODY, ["SUM(j.total_amount)"]),
    ("rows from an unaliased VALUES list", UNALIASED_VALUES_FAN, ["SUM(orders.total_amount)"]),
    ("a laundered fan inside a set-operation arm", UNION_ARM_CTE_FAN,
     ["SUM(orders.total_amount)"]),
    ("a laundered fan beside an honestly clean arm", MIXED_SET_OPERATION,
     ["SUM(orders.total_amount)"]),
    ("a comma-join fan", COMMA_JOIN_FAN, ["SUM(orders.total_amount)"]),
    ("a LEFT JOIN fan", LEFT_JOIN_FAN, ["SUM(orders.total_amount)"]),
    ("the one side behind a derived table", DERIVED_TABLE_ON_THE_ONE_SIDE,
     ["SUM(t.total_amount)"]),
    ("one side in a searched CASE predicate", ONE_SIDE_IN_A_CASE_PREDICATE,
     ["SUM(CASE WHEN orders.status = 'shipped' THEN 1 ELSE 0 END)"]),
    ("one side as a simple CASE operand", ONE_SIDE_AS_A_SIMPLE_CASE_OPERAND,
     ["SUM(CASE orders.status WHEN 'shipped' THEN 1 ELSE 0 END)"]),
    ("one side in a conditional COUNT", ONE_SIDE_IN_A_CONDITIONAL_COUNT,
     ["COUNT(CASE WHEN orders.status = 'shipped' THEN 1 END)"]),
    ("branches that disagree about the grain", BRANCHES_DISAGREEING_ABOUT_THE_GRAIN,
     ["SUM(CASE WHEN orders.flag THEN orders.total_amount ELSE order_items.quantity END)"]),
    ("the conditional spelled IF", IF_SPELLED_CONDITIONAL_FAN,
     ["SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount ELSE 0 END)"]),
    ("a NULLIF comparand off the many side", NULLIF_COMPARAND_FAN,
     ["SUM(NULLIF(orders.total_amount, order_items.quantity))"]),
    ("a COALESCE alternation", COALESCE_ALTERNATION_FAN,
     ["SUM(COALESCE(orders.total_amount, order_items.quantity))"]),
    ("a GROUP_CONCAT separator off the many side", SEPARATOR_OFF_THE_MANY_SIDE_FAN,
     ["GROUP_CONCAT(orders.status, order_items.product_id)"]),
    ("two fans, one of them the value's own grain", TWO_FANS_ONE_ON_THE_VALUE_PATH,
     ["SUM(customers.id * tickets.id)"]),
    ("a chasm pair with one wrapped aggregate", CHASM_WITH_ONE_WRAPPED_AGGREGATE,
     ["SUM(CASE WHEN orders.status = 'x' THEN 1 ELSE 0 END)", "SUM(tickets.id)"]),
    ("a CTE grain finer than the join key", CTE_GRAIN_BELOW_JOIN_KEY,
     ["SUM(orders.total_amount)"]),
    ("a chain of grain-preserving CTEs", TRANSITIVE_CTE, ["SUM(orders.total_amount)"]),
    ("a CTE name spelled in another case", CASE_FOLDED_CTE, ["SUM(orders.total_amount)"]),
    ("the same CTE name declared per arm", SAME_NAME_PER_ARM, ["SUM(x.total_amount)"]),
    ("the same CTE name declared per arm, swapped", SAME_NAME_PER_ARM_SWAPPED,
     ["SUM(x.total_amount)"]),
    ("a WITH bound inside a WHERE subquery", NESTED_WITH_SHADOWING_A_REAL_TABLE,
     ["SUM(orders.total_amount)"]),
    ("an alternation spelled GREATEST", GREATEST_ALTERNATION_FAN,
     ["SUM(GREATEST(orders.total_amount, order_items.quantity))"]),
    ("an alternation spelled LEAST", LEAST_ALTERNATION_FAN,
     ["SUM(LEAST(orders.total_amount, order_items.quantity))"]),
    ("an alternation spelled NVL2", NVL2_ALTERNATION_FAN,
     ["SUM(NVL2(order_items.quantity, orders.total_amount, 0))"]),
    ("an alternation spelled DECODE", DECODE_ALTERNATION_FAN,
     ["SUM(DECODE(order_items.product_id, 1, orders.total_amount, 0))"]),
    ("a CTE body that explodes a column", LATERAL_VIEW_CTE, ["SUM(o.total_amount)"]),
    ("a CTE body that unpivots", UNPIVOT_CTE, ["SUM(o.total_amount)"]),
    ("a CTE body that walks a hierarchy", CONNECT_BY_CTE, ["SUM(o.total_amount)"]),
]

# A conditional count and a conditional sum over the many side. Both are honestly clean: one row
# per order item is exactly what they count, so the join multiplies nothing they read.
CONDITIONAL_COUNT = f"SELECT COUNT(CASE WHEN order_items.quantity > 0 THEN 1 END) {FAN_JOIN}"
CONDITIONAL_SUM = (
    f"SELECT SUM(CASE WHEN order_items.quantity > 0 THEN 1 ELSE 0 END) {FAN_JOIN}"
)

FAN_EDGE = "orders (1) <- order_items (N)"

# --- S2: the value path ----------------------------------------------------
#
# The statement S2 is about. Its value is a product computed once per `order_items` row, so the
# duplication the join performs is the grain the value was already at.
VALUE_AT_MANY_GRAIN = f"SELECT SUM(order_items.quantity * orders.total_amount) {FAN_JOIN}"

# The same one-side measure with the many-side column moved OUT of the aggregate entirely, into a
# `FILTER (WHERE …)` clause. Structurally different from every other member here — see the test.
FILTERED_FAN = (
    f"SELECT SUM(orders.total_amount) FILTER (WHERE order_items.quantity > 0) {FAN_JOIN}"
)

# One statement carrying all three statuses and both risk labels, for the cross-process pin. Four
# aggregates over one fan: a value already at the many side's grain, a one-side measure the join
# inflates, a one-side measure the duplication cannot move, and one that names no column at all.
EVERY_STATUS_SQL = (
    "SELECT SUM(i.quantity * o.total_amount), SUM(o.total_amount), MIN(o.total_amount), COUNT(*) "
    "FROM orders o JOIN order_items i ON i.order_id = o.id"
)
# And what it actually reports, written out. Three tests consume `EVERY_STATUS_SQL` for the breadth
# the comment above claims, and every one of them compares the analysis against itself: the
# cross-process probe compares four children against the parent, the both-surfaces test compares
# `assemble_receipt` against `pre_flight_check`, and the internal-consistency property asks only
# that `joins` agrees with `status`. All three would still pass if all four aggregates degraded to
# `undetermined` together, and the comment describing the statement would silently become false
# while the tests resting on it went on passing. This is the one assertion that says what it is.
EVERY_STATUS_ITEMS = [
    ("SUM(i.quantity * o.total_amount)", rt.NOT_MULTIPLIED, []),
    ("SUM(o.total_amount)", rt.MULTIPLIED, ["fan_trap"]),
    ("MIN(o.total_amount)", rt.MULTIPLIED, ["fan_out_invariant"]),
    ("COUNT(*)", rt.UNDETERMINED, []),
]

# The scope map every `_value_sources` case below is resolved through. Identity, because these
# statements qualify their columns with the table's own name and the question under test is which
# POSITION a column sits in, never which alias it was written under.
VALUE_SCOPE = {"orders": "orders", "order_items": "order_items"}

# Every branch of `_value_sources`, exercised on the function directly. `expected` is a set of TABLE
# names rather than a list of columns, because the grain of a value is a fact about tables: two
# columns of one table are one grain, and `CASE WHEN p THEN orders.total_amount ELSE orders.revenue
# END` is at `orders` grain on both branches while sharing no column between them.
VALUE_SOURCE_CASES = [
    ("a bare column", "orders.total_amount", {"orders"}),
    ("arithmetic, the generic branch",
     "SUM(order_items.quantity * orders.total_amount)",
     {"order_items", "orders"}),
    ("a searched CASE, whose WHEN is a predicate",
     "SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount END)",
     {"orders"}),
    ("a searched CASE with an ELSE, which is a value",
     "SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount ELSE orders.revenue END)",
     {"orders"}),
    ("a simple CASE, whose operand is a predicate input",
     "SUM(CASE orders.status WHEN 'x' THEN order_items.quantity END)",
     {"order_items"}),
    ("a CASE inside a CASE branch",
     "SUM(CASE WHEN order_items.quantity > 0 "
     "THEN CASE WHEN orders.flag THEN orders.total_amount END END)",
     {"orders"}),
    ("an ordering arm, which is neither predicate nor value",
     "STRING_AGG(orders.status, ',' ORDER BY order_items.id)",
     {"orders"}),
    ("DISTINCT, which the generic branch reaches with no case of its own",
     "SUM(DISTINCT orders.total_amount)",
     {"orders"}),
    ("no column at all", "COUNT(*)", set()),
    # The branches added because the union reading of an alternation cleared a real fan.
    ("a CASE whose branches disagree about the grain",
     "SUM(CASE WHEN orders.flag THEN orders.total_amount ELSE order_items.quantity END)",
     set()),
    ("a CASE whose branches agree about the grain",
     "SUM(CASE WHEN orders.flag THEN order_items.quantity ELSE order_items.id END)",
     {"order_items"}),
    ("an alternation nested inside arithmetic, which distributes",
     "SUM(order_items.quantity * CASE WHEN orders.flag THEN orders.total_amount "
     "ELSE orders.revenue END)",
     {"order_items", "orders"}),
    ("IF, which is not an exp.Case and whose condition is a predicate",
     "SUM(IF(order_items.quantity > 0, orders.total_amount, 0))",
     set()),
    ("IF with no ELSE arm at all",
     "SUM(IF(orders.flag, order_items.quantity))",
     {"order_items"}),
    ("NULLIF, whose second argument is a comparand",
     "SUM(NULLIF(orders.total_amount, order_items.quantity))",
     {"orders"}),
    ("a GROUP_CONCAT separator, which is punctuation and not a value",
     "STRING_AGG(orders.status, order_items.product_id)",
     {"orders"}),
    ("COALESCE, an alternation spelled as a function",
     "SUM(COALESCE(orders.total_amount, order_items.quantity))",
     set()),
    # The branches added because the union default cleared four more real fans. Each of these
    # returns ONE of its arguments and so intersects them, exactly as COALESCE above does.
    ("GREATEST, which carries COALESCE's layout",
     "SUM(GREATEST(orders.total_amount, order_items.quantity))",
     set()),
    ("LEAST, the same node shape the other way up",
     "SUM(LEAST(orders.total_amount, order_items.quantity))",
     set()),
    ("NVL2, whose three arguments are IF's",
     "SUM(NVL2(order_items.quantity, orders.total_amount, 0))",
     set()),
    ("DECODE, a simple CASE with the commas moved",
     "SUM(DECODE(order_items.product_id, 1, orders.total_amount, 0))",
     set()),
    ("DECODE with no default arm, whose implicit result is NULL",
     "SUM(DECODE(order_items.product_id, 1, orders.total_amount))",
     {"orders"}),
    ("DECODE with two search arms and no default",
     "SUM(DECODE(order_items.product_id, 1, orders.total_amount, 2, orders.revenue))",
     {"orders"}),
    ("an alternation whose arms are two columns of one table",
     "SUM(GREATEST(orders.total_amount, orders.revenue))",
     {"orders"}),
    # The fail-closed default, which is the polarity this round inverted. Neither of these is a node
    # `_value_operands` enumerates, and neither contributes anything: an empty value path suppresses
    # no edge, so the fan is reported.
    ("a scalar function nobody enumerated",
     "SUM(ROUND(orders.total_amount, order_items.quantity))",
     set()),
    ("a function sqlglot does not know at all",
     "SUM(SOME_UDF(orders.total_amount))",
     set()),
]

# --- S1: the aggregates a duplication cannot move --------------------------
#
# Six spellings of one property. Duplicating a row does not move a minimum, a maximum, an aggregate
# over the distinct values it was handed, or a fold over booleans. All six sit on the ONE side of
# the same fan as `PLAIN_FAN`, so the multiplication behind them is identical and the only thing
# that differs is whether the number moved with it.
DISTINCT_COUNT_OVER_FAN = f"SELECT COUNT(DISTINCT orders.id) {FAN_JOIN}"
MIN_OVER_FAN = f"SELECT MIN(orders.total_amount) {FAN_JOIN}"
MAX_OVER_FAN = f"SELECT MAX(orders.total_amount) {FAN_JOIN}"
# The spelling the predicate is most easily got wrong on: sqlglot parses this to
# `exp.Sum(this=exp.Distinct(...))` and leaves `args["distinct"]` at `None`, so a check written
# against the arg alone sees no DISTINCT here at all.
DISTINCT_SUM_OVER_FAN = f"SELECT SUM(DISTINCT orders.total_amount) {FAN_JOIN}"
# The boolean folds, which are `exp.LogicalOr` / `exp.LogicalAnd` whichever dialect wrote them and
# whatever it called them. They are echoed on the receipt as `LOGICAL_OR` / `LOGICAL_AND`.
BOOL_OR_OVER_FAN = f"SELECT BOOL_OR(orders.total_amount > 0) {FAN_JOIN}"
BOOL_AND_OVER_FAN = f"SELECT BOOL_AND(orders.total_amount > 0) {FAN_JOIN}"

# And four over the same fan that a duplication does move. `PLAIN_FAN` is the SUM.
AVG_OVER_FAN = f"SELECT AVG(orders.total_amount) {FAN_JOIN}"
COUNT_COLUMN_OVER_FAN = f"SELECT COUNT(orders.id) {FAN_JOIN}"
# Echoed as the sqlglot-normalized `GROUP_CONCAT(...)`, not as the `STRING_AGG` that was written.
STRING_AGG_OVER_FAN = f"SELECT STRING_AGG(orders.status, ',') {FAN_JOIN}"

# `COUNT(*)` names no column, so ACE-060's rule resolves it to no table and it settles as
# `undetermined` before any fan is considered.
COUNT_STAR_OVER_FAN = f"SELECT COUNT(*) {FAN_JOIN}"

# The same two properties over a CHASM rather than over a fan. A cross-product cannot move a minimum
# or a distinct count either, and the split above does not reach here — see the test.
CHASM_OVER_INVARIANT_MEASURES = (
    "SELECT c.id, MIN(o.revenue), COUNT(DISTINCT t.id) FROM customers c "
    "LEFT JOIN orders o ON o.customer_id = c.id "
    "LEFT JOIN tickets t ON t.customer_id = c.id GROUP BY c.id"
)

# --- the marker, after the fan-immune clause is deleted --------------------
#
# Four statements, one per state `_aggregates_marker` can be in that this spec could have disturbed,
# each carrying an aggregate a duplication cannot move so that the deleted clause would fire if it
# were still there. The fifth clause (the cap's "further aggregate(s) are not listed") is pinned by
# `test_ace060_trap_free_aggregates.py::test_the_cap_counts_aggregates_and_says_so`.
MARKER_NULL = "SELECT MIN(orders.total_amount) FROM orders"
MARKER_NESTED_SCOPE = (
    "WITH x AS (SELECT SUM(orders.total_amount) AS t FROM orders) SELECT MAX(x.t) FROM x"
)
MARKER_FILTER_OR_SORT = (
    "SELECT orders.id, MAX(orders.total_amount) FROM orders GROUP BY orders.id "
    "HAVING SUM(orders.total_amount) > 1"
)
MARKER_UNRESOLVED = (
    "SELECT COUNT(*), MIN(orders.total_amount) FROM customers "
    "JOIN orders ON orders.customer_id = customers.id"
)

# The sentence this slice deleted, quoted by the fragment that identifies it rather than in full, so
# that a reworded survivor cannot accidentally re-satisfy the absence assertions below.
DELETED_MARKER_PHRASE = "MIN, MAX and COUNT(DISTINCT)"


def _reports(sql: str) -> list["rt.AggregateReport"]:
    """Every aggregate report for `sql` against the sales model, with the analysis proved to run.

    `unchecked` is asserted here rather than in each test because an empty aggregate list is also
    what an unparseable statement returns, and a corpus that silently stopped parsing would pass
    every "is not reported clean" assertion below by computing nothing at all.
    """
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, pf.unchecked
    assert pf.aggregates, sql
    return pf.aggregates


@pytest.mark.parametrize("label,sql,inflated", INFLATED_SHAPES)
def test_no_known_inflated_shape_is_ever_reported_clean(label, sql, inflated):
    """A17 — for every shape a join is known to inflate, the inflated items say so.

    The assertion is a PROPERTY over the corpus, not a per-case expected value, and that choice is
    the test. Three of ACE-083's four corrections are loosenings; each one is correct in a direction
    nothing had asserted, and the failure mode they share is the same one: a shape that used to
    report `multiplied` quietly starts reporting `not_multiplied`. Pinning each member's exact
    status would freeze answers this spec is deliberately changing (a member may legitimately move
    between `multiplied` and `undetermined` as the scope and grain work lands) and would still not
    say the one thing that must hold across all of them.

    `multiplied` and `undetermined` are both acceptable answers here. What is not acceptable is the
    positive claim that a number a join inflated is sound: `undetermined` declines to answer, and a
    reader can act on that, while `not_multiplied` is a receipt asserting something false.

    The property runs over the NAMED items rather than over every aggregate in the statement, and
    that is what lets a member be a mixed statement. Over-everything, a member could only be added
    if all of its arms were inflated, so the corpus held nine disguises of one topology and not one
    statement in which a sound number sits beside an unsound one — which is most real
    multi-aggregate SQL, and is where a rule that suppresses per STATEMENT rather than per ITEM
    hides. Naming the items costs one thing, an expected label per row, and the first assertion is
    what pays for it: a label this file gets wrong selects nothing, and a row that asserts a
    property over an empty selection is a row that asserts nothing at all.
    """
    reports = _reports(sql)
    named = [a for a in reports if a.aggregate in inflated]
    assert sorted({a.aggregate for a in named}) == sorted(set(inflated)), (
        label, [a.aggregate for a in reports])
    assert all(a.status != rt.NOT_MULTIPLIED for a in named), (
        label, [(a.aggregate, a.status) for a in named])


@pytest.mark.parametrize("label,sql", [
    ("the CASE predicate", CONDITIONED_FAN),
    ("the ORDER BY arm", ORDERED_FAN),
])
def test_a_many_side_column_off_the_value_path_still_reports_the_multiplication(label, sql):
    """A8, A9 — a many-side column that is not the value does not clear the aggregate.

    These are the regression the value-path rule must not cause. That rule attributes the fan by
    the columns the aggregate's VALUE is computed from, so that
    `SUM(order_items.quantity * orders.total_amount)` — already at the many side's grain — stops
    being called a fan trap. Both statements here put a many-side column inside the aggregate
    without putting it on the value path: one is a filter on which rows contribute, the other is
    the order the values are concatenated in. In both the summed and concatenated value is a
    one-side column, once per order item, and the join multiplies it.

    Asserted on `status` and `joins` rather than on the finding's `risk`, because the risk label is
    exactly what a later slice splits: an aggregate a duplication cannot move keeps the
    multiplication and loses the word trap. What may never move is that the report says the rows
    were multiplied, and names the edge that did it.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)


@pytest.mark.parametrize("label,sql", [
    ("COUNT over a conditional literal", CONDITIONAL_COUNT),
    ("SUM over a conditional literal", CONDITIONAL_SUM),
])
def test_a_conditional_count_over_the_many_side_still_says_it_is_clean(label, sql):
    """These two are honestly clean, and a value-path rule applied too widely would lose that.

    Each counts order items, one row per item, which is precisely the grain the join produces. No
    fan fires because the only table either reads is the many side, and `not_multiplied` is the
    true answer.

    They are pinned because they are the measured cost of one tempting simplification. The value
    path also decides `sources`; if it were made to decide `resolved` as well, both of these would
    flip to `undetermined`, because their value path is a bare literal and "no columns on the value
    path" is indistinguishable from "no columns at all" to a `bool(cols)` test. That degradation
    breaks no existing assertion and would surface only as two receipts that stopped answering a
    question they used to answer correctly. This is what catches it.

    Unreachable before this spec: `_sales_org` declared no tables, so ACE-060's `visible` set was
    empty and no aggregate on this model could report `not_multiplied` at all.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], (label, reports)
    assert [f.risk for f in reports[0].findings] == [], (label, reports[0].findings)


def _every_risk(sql: str) -> list[str]:
    """Every risk label the pre-flight produced for `sql`, from BOTH channels it reports on.

    `PreFlightResult.findings` is the flat list the CLI reads and `.aggregates[].findings` is the
    per-aggregate roster the receipt reads. They are meant to be projections of one analysis, so a
    test that read only one of them would pass while the other still said `fan_trap` at whichever
    surface it did not check.
    """
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, pf.unchecked
    return [f.risk for f in pf.findings] + [f.risk for a in pf.aggregates for f in a.findings]


# --- S1: the fan is reported, and it is not called a trap -------------------


@pytest.mark.parametrize("label,sql", [
    ("COUNT(DISTINCT)", DISTINCT_COUNT_OVER_FAN),
    ("MIN", MIN_OVER_FAN),
    ("MAX", MAX_OVER_FAN),
    ("SUM(DISTINCT)", DISTINCT_SUM_OVER_FAN),
    ("BOOL_OR", BOOL_OR_OVER_FAN),
    ("BOOL_AND", BOOL_AND_OVER_FAN),
])
def test_an_aggregate_a_duplication_cannot_move_reports_the_fan_without_calling_it_a_trap(
    label, sql,
):
    """A1-A5. The multiplication survives; the word `trap` does not.

    The rows behind a `MAX` over the one side of a fan really are duplicated, so the report keeps
    saying `multiplied` and keeps naming the edge that did it. Suppressing the finding instead would
    make the item say `not_multiplied`, which is the positive claim that the rows were not
    duplicated, and they were. What was false is only the label: `fan_trap` names a defect, and
    there is no defect in a number a duplication cannot move. So `fan_out_invariant` is a second
    derivable property of the same aggregate reported alongside the first fact, not a reason to drop
    it — which is why it is a member of `_MULTIPLYING_RISKS` and `status` is untouched.

    `joins` is asserted because it is what makes the finding actionable at all: a reader told the
    rows were multiplied and not told by what has been given a fact they cannot check. It comes off
    the same shared `triggering_joins` the trap branch builds, so the two labels describe one edge.

    The `BOOL_OR` / `BOOL_AND` cases are the `exp.LogicalOr` / `exp.LogicalAnd` arms of the
    predicate, and `SUM(DISTINCT)` is the arm that only `isinstance(agg.this, exp.Distinct)`
    reaches. All three are TYPE tests: ACE-079 reads each statement in its engine's own dialect, so
    the written function name is whatever that dialect spells the fold, and a name allowlist would
    be wrong the first time the two differ.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)
    assert [f.risk for f in reports[0].findings] == ["fan_out_invariant"], (label, reports)
    assert "fan_trap" not in _every_risk(sql), (label, _every_risk(sql))


@pytest.mark.parametrize("label,sql", [
    ("SUM", PLAIN_FAN),
    ("AVG", AVG_OVER_FAN),
    ("COUNT of a column", COUNT_COLUMN_OVER_FAN),
    ("STRING_AGG", STRING_AGG_OVER_FAN),
])
def test_an_aggregate_a_duplication_moves_carries_no_invariance(label, sql):
    """A6. The other half of the split, and the half that must not move at all.

    Each of these four returns a different number when its rows are duplicated: the sum and the
    average shift, the count counts line items instead of orders, and the concatenation repeats
    every status once per item. Whatever widened the invariance predicate, it may not reach them —
    an aggregate wrongly labelled `fan_out_invariant` tells a reader the number is the same either
    way, which is the one false statement this split makes possible and the reason the negative
    assertion is here rather than left implicit in the positive one above.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert [f.risk for f in reports[0].findings] == ["fan_trap"], (label, reports)
    assert "fan_out_invariant" not in _every_risk(sql), (label, _every_risk(sql))


def test_count_star_over_a_fan_stays_undetermined_and_claims_no_invariance():
    """A6. `COUNT(*)` names no column, so there is nothing to be invariant about.

    ACE-060's rule is that an aggregate whose reads the analysis could not attribute to a table says
    it could not tell, rather than claiming to be clean. `COUNT(*)` is that case exactly, and it is
    reached before any fan is considered — no source table means no measure table, so the fan branch
    never looks at it and the invariance predicate is never asked.

    It is pinned because `COUNT(*)` parses to `exp.Count(this=exp.Star())`, one node type away from
    the `exp.Count(this=exp.Distinct())` the predicate does accept. A widening that stopped
    distinguishing them would attach `fan_out_invariant` here, and because that risk is a member of
    `_MULTIPLYING_RISKS` the status would flip from `undetermined` to `multiplied` — turning "we
    could not tell" into an assertion, over a join that genuinely does multiply what it counts.
    """
    reports = _reports(COUNT_STAR_OVER_FAN)
    assert [(a.aggregate, a.status) for a in reports] == [("COUNT(*)", rt.UNDETERMINED)], reports
    assert reports[0].joins == [], reports[0].joins
    assert _every_risk(COUNT_STAR_OVER_FAN) == [], _every_risk(COUNT_STAR_OVER_FAN)


# --- A21: what the marker says once the detector no longer has that gap -----


@pytest.mark.parametrize("label,sql,phrase", [
    ("nothing left unsaid", MARKER_NULL, None),
    ("an aggregate in a CTE body", MARKER_NESTED_SCOPE, "CTE or a subquery"),
    ("an aggregate in HAVING", MARKER_FILTER_OR_SORT, "HAVING or ORDER BY"),
    ("an aggregate that resolved to no table", MARKER_UNRESOLVED, "could not be resolved"),
])
def test_the_marker_stops_calling_an_invariant_aggregate_a_fan_out_risk(label, sql, phrase):
    """A21. The deleted clause is gone; the four it stood among are unchanged.

    It said "MIN, MAX and COUNT(DISTINCT) are counted as fan-out risks although a fan-out cannot
    change what they return" — a true statement about a shortcoming in the DETECTOR, which is why
    ACE-060 put it on the marker rather than on an item. This slice removed the shortcoming: those
    aggregates now carry `fan_out_invariant` on the item itself, where the reader is already
    looking. Left in place the sentence would report a gap the analysis no longer has, and by the
    four-state contract that costs the section its null marker, which is the only way it can make
    the positive claim "established, here it is".

    Nothing else moves, and the other clauses are pinned here rather than assumed because they were
    deleted from a shared tuple: each of the three remaining conditional clauses is still true of
    the statements it fires on. An aggregate inside a CTE or a subquery is still not reported, one
    in `HAVING` or `ORDER BY` is still not reported, and one that resolved to no table still says
    so. Every statement here carries a `MIN` or a `MAX` in its output list, so the deleted clause
    would fire on all four if it survived — including the null case, which is the state it made
    unreachable for every statement containing one.
    """
    marker = rt.assemble_receipt(_sales_org(), sql)["aggregates"]["undetermined"]
    assert (marker is None) == (phrase is None), (label, marker)
    assert phrase is None or phrase in marker, (label, marker)
    assert DELETED_MARKER_PHRASE not in (marker or ""), (label, marker)


# --- A22: a fact about correctness is still not a refusal -------------------


def test_no_correctness_finding_can_become_a_refusal():
    """A22. `fan_out_invariant` is not a refusal reason, and the type is what makes it so.

    Asserted against `guardrail.RefusalReason` itself rather than by searching the tree for the
    string, because the question is not whether some path happens to refuse today. It is whether one
    could: the reason vocabulary is a closed three-member `Literal`, so a correctness finding has no
    member to become, and adding a fourth means editing that one line in a diff a reviewer reads.
    ACE-094 made a multiplication a fact rather than a refusal, and this slice adds a member to the
    fact vocabulary — the assertion is that the new member landed on the side of that line it was
    meant to.

    `rt.guardrail` and not a fresh import: it is the module object `runtime` itself resolved, so
    this cannot pass against a second copy of `guardrail` reached down a different `sys.path` entry
    than the detector came down.

    The disjointness check covers `_MULTIPLYING_RISKS` whole rather than the new member alone, so
    the next risk added to it is held to the same rule without anyone remembering to come back here.
    """
    reasons = get_args(rt.guardrail.RefusalReason)
    assert reasons == ("unsafe", "out_of_scope", "undetermined"), reasons
    assert set(rt._MULTIPLYING_RISKS).isdisjoint(reasons), rt._MULTIPLYING_RISKS
    assert "fan_out_invariant" in rt._MULTIPLYING_RISKS, rt._MULTIPLYING_RISKS
    # And end to end: the statement that produces it produces a finding, not a refusal.
    pf = rt.pre_flight_check(MIN_OVER_FAN, _sales_org())
    assert {f.risk for f in pf.findings} == {"fan_out_invariant"}, pf.findings
    assert pf.unchecked is None, pf.unchecked


# --- S2: the fan is attributed by position on the value path ----------------


def test_a_value_at_many_side_grain_is_not_multiplied_by_the_fan():
    """A7. The join duplicates the rows, and the value was already one per duplicated row.

    `SUM(order_items.quantity * orders.total_amount)` computes one product per `order_items` row.
    `orders.total_amount` appears in it as a scalar co-factor: the join hands the expression the
    same amount once per item, and multiplying each item's quantity by it and summing is the same
    number the statement asked for. The duplication IS the grain the value is defined at, so there
    is nothing for the fan to inflate.

    The old rule attributed the fan by SYNTACTIC presence — every table with a column anywhere
    inside the aggregate — so this reported `fan_trap` on the strength of `orders` being named. That
    is a false statement on the receipt about a correct statement, and the cost of it is the one
    that matters: a reader who is told a sound number was inflated learns to discount the report.

    `joins` is asserted empty as well as the status, because the two are separable. A report that
    said `not_multiplied` while still naming the edge would be internally contradictory, and it is
    the second half of that pair that a rule attributing by presence would leave behind.
    """
    reports = _reports(VALUE_AT_MANY_GRAIN)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], reports
    assert reports[0].joins == [], reports[0].joins
    assert [f.risk for f in reports[0].findings] == [], reports[0].findings


def test_a_filter_clause_predicate_is_outside_the_aggregate_the_analysis_reads():
    """`_value_columns` has no `exp.Filter` branch because the parse makes one unnecessary.

    `SUM(orders.total_amount) FILTER (WHERE order_items.quantity > 0)` parses to
    `Filter(this=Sum(...), expression=Where(...))` — the `Filter` is the PARENT of the aggregate, so
    `order_items.quantity` is not in the `Sum` subtree at all. `_select_aggregates` collects
    `exp.AggFunc` nodes, so the aggregate that reaches `_value_columns` is the bare `Sum` and the
    predicate is structurally invisible to it. A branch for `exp.Filter` would be dead code.

    That is a fact about sqlglot's tree and not about this module, which is exactly why it is pinned
    here rather than trusted. If a future sqlglot re-parented the predicate under the aggregate, or
    if this layer started reading `exp.Filter` as the aggregate node, the predicate's many-side
    column would silently join the value path and clear a genuinely inflated total. The measured
    behaviour is asserted through the analysis so that either change fails: the summed value is a
    one-side amount, once per surviving order item, and the join multiplies it.
    """
    agg = parse_one(FILTERED_FAN).find(exp.AggFunc)
    assert isinstance(agg, exp.Sum), agg
    assert isinstance(agg.parent, exp.Filter), agg.parent
    assert [c.sql() for c in agg.find_all(exp.Column)] == ["orders.total_amount"], agg

    reports = _reports(FILTERED_FAN)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


@pytest.mark.parametrize("label,expression,expected", VALUE_SOURCE_CASES)
def test_the_value_path_holds_only_the_tables_the_result_is_built_from(label, expression, expected):
    """A7. Every branch of `_value_sources`, asserted on the function rather than through a receipt.

    The analysis reaches most of these branches, but it reaches them in combination and reports one
    status per statement, so a branch that dropped a table the value does use and a branch that kept
    one it does not can cancel out in the answer. Called directly, each shape says which tables it
    thinks the value is built from, and a wrong one is wrong visibly.

    The guard branches are the reason this is a unit test at all. A simple `CASE`'s operand
    (`CASE orders.status WHEN 'x' THEN …`) is an input to the branch CHOICE, not to the result, so
    it belongs with the `WHEN` predicates and not with the `THEN` values — and it lives under
    `Case.this`, which the generic branch would have taken. The nested case proves the recursion
    applies the same rule at depth rather than only at the top. `DISTINCT` is the opposite check:
    it carries a genuine value column under `args["expressions"]`, so the generic branch must reach
    it and no case may intercept it.

    The alternation cases are what an earlier reading of this function got wrong. Reading a `CASE`
    as the UNION of its branches puts a many-side column on the value path whenever ANY branch
    carries one, so a value that is a one-side amount on the rows where the predicate holds reads as
    though it were at many-side grain throughout — and the fan that really does duplicate those rows
    is cleared. `IF` is the same defect with a sharper edge, because sqlglot parses `IF` / `IIF` /
    `IFF` to `exp.If` rather than to `exp.Case` and then RENDERS it back as `CASE WHEN … END`, so
    without a case of its own the two spellings produce a byte-identical receipt label carrying
    opposite statuses.
    """
    node = parse_one(f"SELECT {expression} FROM orders").expressions[0]
    assert rt._value_sources(node, VALUE_SCOPE) == expected, label


@pytest.mark.parametrize("dialect", [None, "bigquery", "duckdb", "mysql", "tsql", "snowflake"])
def test_the_conditional_function_parses_to_exp_if_on_every_dialect_this_layer_speaks(dialect):
    """`exp.If`'s case exists because of a PARSE fact, so the parse fact is what is pinned.

    `IF` is spelled `IIF` on T-SQL and `IFF` on Snowflake, and every one of them lands on `exp.If`
    rather than on `exp.Case`. A future sqlglot that folded the conditional into `exp.Case` would
    make the `exp.If` branch dead rather than wrong, and one that introduced a THIRD node for it
    would silently drop back to the generic branch — which reads the condition as a value and clears
    the fan. That is the direction this asserts against, so it asserts on the node type directly.
    """
    spelling = {"tsql": "IIF", "snowflake": "IFF"}.get(dialect, "IF")
    sql = f"SELECT SUM({spelling}(order_items.quantity > 0, orders.total_amount, 0)) FROM orders"
    node = parse_one(sql, read=dialect).expressions[0]
    assert isinstance(node.this, exp.If), (dialect, repr(node))
    assert rt._value_sources(node, VALUE_SCOPE) == set(), dialect


@pytest.mark.parametrize("label, sql", [
    ("COALESCE", "SELECT SUM(COALESCE(orders.total_amount, 0)) FROM orders"),
    ("IF", "SELECT SUM(IF(orders.flag, orders.total_amount, 0)) FROM orders"),
    ("GREATEST", "SELECT SUM(GREATEST(orders.total_amount, orders.revenue)) FROM orders"),
    ("LEAST", "SELECT SUM(LEAST(orders.total_amount, orders.revenue)) FROM orders"),
])
def test_an_alternation_is_attributed_on_a_sqlglot_that_never_heard_of_decode(label, sql, monkeypatch):
    """The dispatch may not read a late-added class by attribute, only through `_exp_nodes`.

    `_exp_nodes` exists because the package pins `sqlglot>=20` and `exp.Nvl2` / `exp.DecodeCase`
    are later additions, and it is what keeps the module importable against a sqlglot that reads
    every statement here perfectly well. A bare `exp.DecodeCase` inside `_value_operands` defeats
    it completely, because the attribute is read when the LINE runs rather than at import: the
    `DECODE` arm sits above the ternary and the leading-argument arms, so on such a version every
    `COALESCE`, `IF`, `GREATEST` and `LEAST` raises `AttributeError` on the way past it.

    That is not a crash the caller sees. `execute_sql._receipt_for` catches it and returns
    `RECEIPT_BUILD_FAILED`, so the statement executes and answers while the trust layer silently
    disappears — the same shape `_MAX_CTE_CHAIN` and the iterative `_value_sources` walk exist for.
    `COALESCE` over an aggregate is ordinary SQL, so this was not an exotic configuration.

    Deleting the attribute is the honest simulation: the module-level tuples resolved at import on
    the real sqlglot, and what is under test is whether the dispatch reaches for the name again.
    """
    monkeypatch.delattr(exp, "DecodeCase", raising=False)
    reports = _reports(sql)
    assert [(r.aggregate, r.status) for r in reports] == [
        (reports[0].aggregate, rt.NOT_MULTIPLIED)], label


def test_the_value_path_of_something_that_is_not_an_expression_is_empty():
    """The guard that lets the generic branch iterate `node.args` without inspecting what it holds.

    Argument values are not all nodes: `Count(big_int=True)` and `Ordered(nulls_first=True)` carry
    bools, and an absent optional argument is `None`. The alternative to this guard is a type check
    at every call site inside the walk, which is the same test written four times.

    An alternation with no branch at all is the same guard on the other arm. `frozenset.intersection`
    with no argument at all is a `TypeError`, not an empty set, so a `CASE` node carrying neither an
    `ifs` list nor a `default` has to be answered before the fold rather than inside it.
    """
    assert rt._value_sources(None, VALUE_SCOPE) == frozenset()
    assert rt._value_sources(True, VALUE_SCOPE) == frozenset()
    assert rt._value_sources(exp.Case(), VALUE_SCOPE) == frozenset()
    # `DECODE` needs at least an operand, a search value and a result before any slot means
    # anything, and sqlglot parses the two-argument spelling to `exp.Decode` — a charset decode,
    # an entirely different function — so this arity is reachable only by construction. Answered
    # anyway rather than guessed at, because the alternative is reading a SEARCH value as a result.
    short_decode = exp.DecodeCase(
        expressions=[exp.column("product_id", "order_items"), exp.column("total_amount", "orders")])
    assert rt._decode_arms(short_decode) == []
    assert rt._value_sources(short_decode, VALUE_SCOPE) == frozenset()


# --- the direction the corpus reaches only through members 14-21 ------------
#
# The exact edge each of those members has to name. The corpus property says only that a named item
# is not reported clean, which is what lets a member move between `multiplied` and `undetermined` as
# the scope and grain work lands; these seven have no such freedom, because the whole reason they
# are in the corpus is that a value path narrowed until it holds no one-side column reports them
# sound. Each was measured `not_multiplied` while the value path decided `sources`, and `multiplied`
# on the base the spec is written against.

ONE_SIDE_OFF_THE_VALUE_PATH = [
    ("a searched CASE testing a one-side column", ONE_SIDE_IN_A_CASE_PREDICATE),
    ("a simple CASE whose operand is a one-side column", ONE_SIDE_AS_A_SIMPLE_CASE_OPERAND),
    ("a conditional COUNT over a one-side predicate", ONE_SIDE_IN_A_CONDITIONAL_COUNT),
    ("branches that disagree about the grain", BRANCHES_DISAGREEING_ABOUT_THE_GRAIN),
    ("IF, which sqlglot renders back as a CASE", IF_SPELLED_CONDITIONAL_FAN),
    ("NULLIF, whose second argument is a comparand", NULLIF_COMPARAND_FAN),
    ("COALESCE, an alternation spelled as a function", COALESCE_ALTERNATION_FAN),
    ("a GROUP_CONCAT separator read off the many side", SEPARATOR_OFF_THE_MANY_SIDE_FAN),
]


@pytest.mark.parametrize("label,sql", ONE_SIDE_OFF_THE_VALUE_PATH)
def test_a_one_side_column_off_the_value_path_still_reports_the_multiplication(label, sql):
    """The value path decides WHICH EDGE is suppressed, and nothing else about the aggregate.

    `sources` answers three questions and only one of them is a value-path question. "Which
    many-side table's duplication is this value's own grain" is; "which table is the measure on the
    one side" and "which tables does this aggregate read, for the chasm rule" are not. Narrowing
    `sources` to the value path answered all three with the narrow answer, and for a value built
    from no column at all — `THEN 1 ELSE 0` — the narrow answer is that the aggregate reads nothing,
    so the fan loop skipped it and the receipt asserted the total was sound.

    The last three members are the same defect reached through a node the value walk had no case
    for, where the GENERIC branch contributed the condition's columns and put the MANY side on the
    value path instead. That is the inverted polarity: for a node that only combines values,
    contributing by default is honest, but for one that mixes a predicate with its values it is
    contributing the predicate that clears the fan.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)


def test_only_the_edge_that_is_not_the_values_own_grain_is_suppressed():
    """A fan is suppressed PER EDGE, because `many_tables` can hold more than one.

    `SUM(customers.id * tickets.id)` sits on the one side of two fans off `customers`. The tickets
    join is the grain the product was already defined at and multiplies nothing; the orders join
    duplicates every one of those products. Suppressing the whole finding because ONE edge is the
    value's grain reported the statement clean, and reporting both edges names a join that did not
    move the number. Only the `orders` edge is true, so only the `orders` edge is named.
    """
    reports = _reports(TWO_FANS_ONE_ON_THE_VALUE_PATH)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == ["customers (1) <- orders (N)"], reports[0].joins


def test_one_wrapped_aggregate_does_not_clear_the_chasm_for_the_others():
    """The chasm reads the UNION of every site's sources, so a narrowing there is not local.

    `agg_sources` is derived across all the sites in one SELECT. When the value path decided
    `sources`, a single aggregate whose value is a literal emptied its own contribution — and with
    two measures needed for a shared dimension to be a chasm at all, one wrapped aggregate took the
    finding away from every OTHER aggregate in the statement too. `SUM(tickets.id)` is not inside
    that CASE and never was; its rows are still crossed against every order of the same customer.
    """
    reports = _reports(CHASM_WITH_ONE_WRAPPED_AGGREGATE)
    assert [a.status for a in reports] == [rt.MULTIPLIED, rt.MULTIPLIED], reports
    assert all("chasm_trap" in [f.risk for f in a.findings] for a in reports), reports


def test_the_statement_that_carries_every_status_carries_the_ones_its_comment_claims():
    """`EVERY_STATUS_SQL` is a premise three other tests rest on, so it is pinned as one.

    It is described as "one statement carrying all three statuses and both risk labels", and three
    tests use it for exactly that breadth. Not one of them can tell whether the breadth is still
    there: each compares the analysis against another rendering of the same analysis, or against a
    shape-agnostic invariant. A change that collapsed all four aggregates to `undetermined` would
    leave every one of them green and leave the comment above the constant describing a statement
    this file no longer produces.

    The findings' risks are pinned beside the statuses because the claim is "both risk labels", and
    `multiplied` is reachable through either. A `MIN` that started reporting `fan_trap` would keep
    the status column identical and lose half of what this statement is here to cover.
    """
    assert [(a.aggregate, a.status, [f.risk for f in a.findings])
            for a in _reports(EVERY_STATUS_SQL)] == EVERY_STATUS_ITEMS


# --- A24: the same report in every process ---------------------------------
#
# The statements the probe runs, one per SET the analysis iterates. Iteration order is only
# observable when there is more than one element to order, and `EVERY_STATUS_SQL` — which was the
# whole of this test — has one fanning source and one many-side table, so every set it produces is
# a singleton and its answer is the same whether the code sorts or not. Measured: with all three
# `sorted()` calls in `_preflight_select` deleted, `EVERY_STATUS_SQL` is byte-identical across hash
# seeds and the entire suite stays green.
#
# Each of the three below makes exactly one of those sets hold two elements, and each was measured
# to produce two different receipts across the seeds below when its own `sorted()` is removed:
#
#   `agg_sources` in the chasm branch  -> the `srcs` list inside the reason, and `joins`
#   `agg_sources` in the fan loop      -> the ORDER of two findings on one aggregate
#   `many_tables` inside the fan loop  -> the order of two edges inside one finding
#
# Two aggregate sources over a shared dimension. `_shared_dimension` walks a set to pick the
# dimension and the chasm reason interpolates the sources, so this is the shape that orders them.
CHASM_OVER_TWO_MEASURES = (
    "SELECT c.id, SUM(o.revenue), COUNT(t.id) FROM customers c "
    "LEFT JOIN orders o ON o.customer_id = c.id "
    "LEFT JOIN tickets t ON t.customer_id = c.id GROUP BY c.id"
)
# One measure on the one side of TWO fans off it. `many_tables` holds `orders` and `tickets`, and
# both are named inside a single finding, so their order is the receipt's order.
TWO_MANY_SIDES_OFF_ONE_MEASURE = f"SELECT SUM(customers.id) {TWO_FAN_JOIN}"
# One aggregate reading two measure tables, each the one side of a fan the value path does not
# suppress. `customers` fans out to `tickets` and `orders` fans out to `order_items`, so this
# aggregate carries TWO findings and the fan loop's iteration over `agg_sources` orders them.
TWO_MEASURE_TABLES_ONE_AGGREGATE = (
    "SELECT SUM(orders.total_amount * customers.id) FROM customers "
    "JOIN orders ON orders.customer_id = customers.id "
    "JOIN tickets ON tickets.customer_id = customers.id "
    "JOIN order_items ON order_items.order_id = orders.id"
)

DETERMINISM_SHAPES = [
    ("three statuses and both risk labels", EVERY_STATUS_SQL),
    ("two aggregate sources over one shared dimension", CHASM_OVER_TWO_MEASURES),
    ("one measure with two many sides", TWO_MANY_SIDES_OFF_ONE_MEASURE),
    ("one aggregate reading two measure tables", TWO_MEASURE_TABLES_ONE_AGGREGATE),
]

# Every statement in ONE child per seed rather than one child per statement: the property is about
# the process, and a child that analysed all four under one seed is the same evidence at a quarter
# of the process cost.
_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from semantic_model import models as m
from semantic_model import runtime as rt
org = m.Datasource.model_validate_json(sys.argv[3])
print(json.dumps([[a.as_dict() for a in rt.pre_flight_check(sql, org).aggregates]
                  for sql in json.loads(sys.argv[2])]))
"""


def test_the_aggregate_report_is_the_same_in_every_process():
    """A24 / REQ-022: the receipt is "the same for the same SQL and model version".

    That is a claim about PROCESSES, so nothing inside one can check it. The hazards it names are
    invisible under a fixed seed: `sources` is a `frozenset` and `many_tables` is a `set`, the fan
    branch iterates a sorted copy of one of them and intersects the other, and `_shared_dimension`
    picks its dimension by walking a set. Any answer derived from one of those is stable within an
    interpreter and free to differ in the next. Eight seeds, eight processes, one answer.

    The whole serialized item list is compared rather than the statuses alone, and it is NOT sorted:
    the aggregates are reported in the order the statement wrote them, so a re-ordered list is as
    much a difference as a flipped status and sorting here would hide exactly one of the two modes
    this exists to catch.

    `DETERMINISM_SHAPES` and not one statement, and that is the whole of what this test is. A set
    with one element has one iteration order, so a statement that produces only singletons cannot
    distinguish a `sorted()` from its absence — and `EVERY_STATUS_SQL`, which stood here alone,
    produces nothing else. Each of the other three makes one of the three sets hold two elements;
    the comment above them says which set each reaches and what moves when it is left unordered.

    The model crosses the process boundary as JSON rather than as a path, because this fixture is
    built in Python and has no profile on disk; `model_validate_json` of `model_dump_json` is the
    same object by construction, so the child analyses the model the parent declared.
    """
    payload = _sales_org().model_dump_json()
    statements = json.dumps([sql for _, sql in DETERMINISM_SHAPES])
    seen = set()
    for seed in ("0", "1", "2", "3", "7", "42", "1234", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(PKG_SRC), statements, payload],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())

    assert len(seen) == 1, f"the aggregate report differed across hash seeds: {seen}"
    # And it is the answer this process gets, so eight children agreeing on something wrong would
    # still fail rather than agree quietly.
    assert json.loads(seen.pop()) == [
        [a.as_dict() for a in _reports(sql)] for _, sql in DETERMINISM_SHAPES
    ]


# --- S3: what a SELECT can see, and what a CTE reference stands for ---------
#
# The two statements S3 names. Both were wrong at HEAD and wrong in opposite directions, which is
# why one fix could not have been enough on its own.
#
# The first names a join only the CTE BODY takes. `oi` groups `order_items` to one row per order,
# so the outer join is one-to-one and nothing is multiplied — but `order_items` leaked out of the
# body into the outer scope map and the report claimed a fan over a table the statement never
# joined to.
GRAIN_CHANGING_CTE = (
    "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)
# The second MISSES the join it does take. `o` hands back the rows of `orders` unchanged and the
# outer query joins that to `order_items`, which is the plain fan wearing one disguise — but `o`
# resolved to the string `'o'`, a name the model never declared, so nothing was found at all.
GRAIN_PRESERVING_CTE = (
    "WITH o AS (SELECT * FROM orders) SELECT SUM(o.total_amount) FROM o "
    "JOIN order_items ON order_items.order_id = o.id"
)
# The other three CTE shapes this section reasons about — `TRANSITIVE_CTE`,
# `CTE_GRAIN_BELOW_JOIN_KEY` and `CASE_FOLDED_CTE` — are corpus members and are declared with the
# corpus above, so that every statement the property runs over is defined in one place.

# The derived-table shape, with its aggregate's column UNQUALIFIED. Two independent mechanisms
# would each have to fail for this to read clean: without the derived binding the outer scope is
# the single table `orders`, so `total_amount` resolves by being the only candidate.
DERIVED_TABLE_UNQUALIFIED = (
    "SELECT SUM(total_amount) FROM orders "
    "JOIN (SELECT order_id FROM order_items) d ON d.order_id = orders.id"
)

# A parenthesized named table. `Subquery(this=Table)` binds no name of its own — the `exp.Table`
# arm already bound `orders` — so this reads one declared table and must stay clean.
PARENTHESIZED_TABLE = "SELECT SUM(orders.total_amount) FROM (orders)"

# The two shapes `check_scopable` refuses at the guarded chokepoint and `sm prepare` does not.
VALUES_SOURCE = (
    "SELECT SUM(orders.total_amount) FROM orders "
    "JOIN (VALUES (1), (2)) AS v(order_id) ON v.order_id = orders.id"
)
LATERAL_SOURCE = "SELECT SUM(o.total_amount) FROM orders o, LATERAL (SELECT 1) l"
# The third one, and the reason the derived binding is a denylist. `UNNEST` is an `exp.Unnest`, a
# node type an allowlist of Subquery / Lateral / Values does not hold, so it never entered the map
# at all — and a source absent from the map is a source the analysis reads as not being there.
UNNEST_SOURCE = "SELECT SUM(orders.total_amount) FROM orders, UNNEST([1, 2]) AS t(x)"

# Every way `_grain_preserving_source` and `_cte_edge` can decline, one row per guard. Each body
# below differs from a grain-preserving one in exactly one respect, so a guard that stopped firing
# would show up as one row rather than as a general collapse.
UNREADABLE_CTE_BODIES = [
    ("the body is a set operation, not a SELECT",
     "WITH u AS (SELECT order_id FROM order_items UNION SELECT id FROM orders) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN u ON u.order_id = orders.id"),
    ("the body is DISTINCT, which collapses rows",
     "WITH d AS (SELECT DISTINCT order_id FROM order_items) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN d ON d.order_id = orders.id"),
    ("the body aggregates without grouping",
     "WITH s AS (SELECT SUM(quantity) AS order_id FROM order_items) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN s ON s.order_id = orders.id"),
    ("the body takes a join of its own",
     "WITH j AS (SELECT o.id AS id FROM orders o JOIN order_items i ON i.order_id = o.id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN j ON j.id = orders.id"),
    ("the body has no FROM at all",
     "WITH n AS (SELECT 1 AS order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN n ON n.order_id = orders.id"),
    ("the body reads more than one table",
     "WITH t AS (SELECT order_id FROM order_items WHERE order_id IN (SELECT id FROM orders)) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN t ON t.order_id = orders.id"),
    ("the body reads a table the model does not declare",
     "WITH x AS (SELECT * FROM shipments) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN x ON x.order_id = orders.id"),
    ("two CTEs read each other, so the walk would not terminate",
     "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM a) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN a ON a.order_id = orders.id"),
    ("the join key is composite",
     "WITH oi AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
     "GROUP BY order_id) SELECT SUM(orders.total_amount) FROM orders "
     "JOIN oi ON oi.order_id = orders.id AND oi.product_id = orders.id"),
    ("there is no join key, because it is a comma join",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders, oi"),
    ("the join predicate is an inequality, not an equality",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id > orders.id"),
    ("the far side of the join key is a literal, not a column",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = 1"),
    ("the far side resolves to nothing in scope",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = zzz.id"),
    ("the grain is an expression with no column name to compare",
     "WITH oi AS (SELECT SUM(quantity) q FROM order_items GROUP BY order_id + 1) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.q = orders.id"),
    ("the unreadable body is joined from inside a set-operation arm",
     "WITH oi AS (SELECT i.order_id AS order_id FROM order_items i "
     "JOIN orders o ON o.id = i.order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id "
     "UNION ALL SELECT SUM(orders.revenue) FROM orders JOIN oi ON oi.order_id = orders.id"),
]

# Every shape whose multiplication answer this spec decides, gathered for the one property that
# holds across all of them. Not a new corpus: `INFLATED_SHAPES` is about the answer being wrong in
# one direction, and this is about the answer being INTERNALLY consistent whatever it says.
ALL_ANALYSED_SHAPES = [
    (label, sql) for label, sql, _inflated in INFLATED_SHAPES
] + UNREADABLE_CTE_BODIES + [
    ("a value already at the many side's grain", VALUE_AT_MANY_GRAIN),
    ("a conditional count over the many side", CONDITIONAL_COUNT),
    ("a conditional sum over the many side", CONDITIONAL_SUM),
    ("an aggregate a duplication cannot move", MIN_OVER_FAN),
    ("a DISTINCT count over a fan", DISTINCT_COUNT_OVER_FAN),
    ("a boolean fold over a fan", BOOL_OR_OVER_FAN),
    ("COUNT(*) over a fan", COUNT_STAR_OVER_FAN),
    ("a FILTER clause predicate", FILTERED_FAN),
    ("three statuses and both risk labels at once", EVERY_STATUS_SQL),
    ("a grain-changing CTE", GRAIN_CHANGING_CTE),
    ("a grain-preserving CTE", GRAIN_PRESERVING_CTE),
    ("a derived table with an unqualified measure", DERIVED_TABLE_UNQUALIFIED),
    ("a parenthesized named table", PARENTHESIZED_TABLE),
    ("a VALUES source", VALUES_SOURCE),
    ("a LATERAL source", LATERAL_SOURCE),
    ("a chasm over two independent measures", CHASM_OVER_TWO_MEASURES),
    ("a chasm whose measures a duplication cannot move", CHASM_OVER_INVARIANT_MEASURES),
    ("a LATERAL VIEW beside the FROM clause", LATERAL_VIEW_SOURCE),
    ("an UNPIVOT on the table itself", UNPIVOT_SOURCE),
    ("a CONNECT BY beside the FROM clause", CONNECT_BY_SOURCE),
    ("a PIVOT on the table itself", PIVOT_SOURCE),
    ("a CTE that renames its grain column", CTE_RENAMING_ITS_GRAIN),
]


def _parse(sql: str):
    """One parse, so every assertion on the resolver reads the tree the analysis reads."""
    return parse_one(sql)


def _no_grain_org() -> "m.Datasource":
    """`orders` with `grain: []` — the one thing `_sales_org` cannot express and must not learn.

    Every `_sales_org` table declares a non-empty grain, which is what makes its assertions mean
    what they say; weakening one of them to reach this branch would silently change what several
    other tests are testing. So the empty-grain case gets its own two-table model, cut down to the
    single edge the assertion is about.
    """
    tables = [
        m.Table(name="orders", schema="public", storage_connection="c", grain=[],
                description="orders",
                columns=[m.Column(name="id", type="integer"),
                         m.Column(name="total_amount", type="decimal")]),
        m.Table(name="order_items", schema="public", storage_connection="c",
                grain=["order_id", "product_id"], description="order items",
                columns=[m.Column(name="order_id", type="integer"),
                         m.Column(name="product_id", type="integer"),
                         m.Column(name="quantity", type="integer")]),
    ]
    rels = [m.Relationship(from_table="order_items", to_table="orders", from_column="order_id",
                           to_column="id", relationship="many_to_one")]
    return m.Datasource(datasource="Shop",
                        subject_areas=[m.SubjectArea(name="sales", tables_defined=tables,
                                                     relationships=rels)])


def _averageable_org() -> "m.Datasource":
    """One table with one `averageable` column — the smallest model `bad_aggregation` fires on.

    `_sales_org` deliberately leaves every `Column.aggregation` at `unknown` so that the exact
    risk-list assertions elsewhere stay about the fan/chasm detector. This model exists only to
    hold `_check_aggregation_semantics` still while the scope map underneath it changes.
    """
    t = m.Table(name="facts", schema="public", storage_connection="c", grain=["id"],
                description="facts",
                columns=[m.Column(name="id", type="integer", aggregation="dimension"),
                         m.Column(name="unit_price", type="decimal", aggregation="averageable")])
    return m.Datasource(datasource="F",
                        subject_areas=[m.SubjectArea(name="a", description="d",
                                                     tables_defined=[t])])


def test_a_grain_changing_cte_is_not_the_table_it_reads():
    """A10. The join a CTE BODY takes is not a join the statement takes.

    `oi` groups `order_items` to one row per order. The outer statement joins `orders` to that, one
    order to one row, and `SUM(orders.total_amount)` is exactly the sum of the orders. Nothing is
    multiplied and the receipt may say so.

    What it said before was `multiplied`, naming `orders (1) <- order_items (N)` — an edge that
    appears nowhere in the outer query. `order_items` is read once, inside the CTE body, under a
    `GROUP BY` that is the whole point of writing the CTE. Reporting it is not an over-cautious
    answer, it is a false one: it names a join the reader can look for and not find, and it does so
    on a statement that is correct.

    Both halves are asserted because they fail independently. Abstaining would fix the naming and
    lose the answer; resolving the CTE without checking its grain would keep the answer and keep
    naming the wrong edge. `joins` is asserted over EVERY item rather than the first, since the
    criterion is that no item names `order_items` — a second aggregate would be a second chance to
    get it wrong.
    """
    reports = _reports(GRAIN_CHANGING_CTE)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], reports
    assert [j for a in reports for j in a.joins] == [], reports
    assert not any("order_items" in j for a in reports for j in a.joins), reports
    assert [f.risk for a in reports for f in a.findings] == [], reports


def test_a_grain_preserving_cte_carries_the_join_it_launders():
    """A11. A CTE that hands back a table's rows unchanged IS that table, for this question.

    `WITH o AS (SELECT * FROM orders)` produces exactly the rows of `orders`, so joining `o` to
    `order_items` multiplies `SUM(o.total_amount)` precisely as joining `orders` would. The
    statement is the plain fan with one indirection, and the fan is real.

    It reported `undetermined` before, and that was not a cautious answer either — it was the
    detector failing to resolve `o` to anything and then having nothing to say. The edge is asserted
    by name: `order_items` and not `o`, because a reader who is told their number was multiplied
    and handed back the alias they invented has been told something they already knew.
    """
    reports = _reports(GRAIN_PRESERVING_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins
    assert [f.risk for f in reports[0].findings] == ["fan_trap"], reports[0].findings


def test_a_grain_preserving_cte_resolves_through_another_one():
    """A13. Two grain-preserving hops are still the same rows, so the resolution is transitive.

    `b` reads `a` and `a` reads `order_items`, neither changing a thing. A resolver that stopped at
    the first hop would find `b` bound to `a`, which is not a declared table either, and fall back
    to the same `undetermined` the single-hop case used to give — correct-looking on the corpus,
    which only asks that nothing read clean, and wrong on the one assertion that matters here.

    The cycle guard makes this safe rather than unbounded: `seen` is what stops `a` reading `b`
    reading `a` from recursing forever, and it is exercised by its own row in the guard table below.

    The resolved SCOPE MAP is what the assertion rests on, and the status and the edge alone cannot
    stand in for it. Base `439ecd1` produces this exact status and this exact edge on this exact
    statement — by LEAKING `order_items` out of the CTE body into `_alias_map`, which is the defect
    S3 exists to remove. The two implementations are byte-identical at the receipt and opposite
    underneath it, so a test that reads only the receipt passes on the code it was written to fail
    on. `b` resolving to `order_items` is the fact that is new: one entry per name the outer FROM/
    JOIN clauses bind, `b` bound to the table two grain-preserving hops away, and no `a` in the map
    at all, because `a` is a name only the CTE body ever writes.
    """
    tree = _parse(TRANSITIVE_CTE)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "b": "order_items"}, scope
    # No DERIVED edge: `b` IS `order_items`, so the model's own declared relationship is the one the
    # fan is read off. A derived edge here would mean the resolution fell back to treating the CTE
    # as an entity of its own, which is the answer A14's shape gets and not this one's.
    assert derived == [], derived

    reports = _reports(TRANSITIVE_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


def test_a_cte_grain_that_does_not_cover_the_join_key_still_fans():
    """A14. Grouped finer than the join key, the CTE is the many side of its own edge.

    `oi` is one row per (order, product) and the join uses `order_id` alone, so an order with three
    products meets three rows and `SUM(orders.total_amount)` triples. That is a fan, and it is a fan
    over a source the model never declared — which is exactly why the analysis has to DERIVE the
    edge rather than abstain. Abstaining would report `undetermined` on a statement whose answer is
    known and wrong.

    The derived edge is asserted, not just the status. `many_to_one` from the CTE to `orders` is the
    claim the report rests on, and `infer_cardinality` read it off the two grains — the CTE's
    `(order_id, product_id)` against the join key `order_id`. A status assertion alone would pass
    just as well if the fan came from somewhere else entirely.

    The edge names `oi` because that is the only name this source has. It is the one place a join
    on the receipt names an alias, and it is honest: there is no table to name.
    """
    tree = _parse(CTE_GRAIN_BELOW_JOIN_KEY)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "oi": "oi"}, scope
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", "many_to_one")], derived

    reports = _reports(CTE_GRAIN_BELOW_JOIN_KEY)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == ["orders (1) <- oi (N)"], reports[0].joins


def test_a_cte_grain_that_covers_the_join_key_is_not_multiplied():
    """A15. `not_multiplied` BECAUSE the grain covers the join key, not because we declined.

    `oi` is one row per order and the join is on `order_id`, so each order meets exactly one row.
    The derived edge is `one_to_one`, and that is the assertion: `undetermined` and `not_multiplied`
    are different answers, and so are two routes to `not_multiplied`. A resolver that bound every
    grain-changing CTE to nothing would also produce no fan here, and the item would then say
    `undetermined` — so the status alone cannot tell the working rule from the abstaining one.

    Same SQL as A10, read one layer down. A10 asserts what the receipt says; this asserts why.
    """
    tree = _parse(GRAIN_CHANGING_CTE)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "oi": "oi"}, scope
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", "one_to_one")], derived


@pytest.mark.parametrize("label,on,expected", [
    ("the CTE is written on the left", "oi.order_id = orders.id", "one_to_one"),
    ("the CTE is written on the right", "orders.id = oi.order_id", "one_to_one"),
])
def test_the_join_key_is_read_in_either_orientation(label, on, expected):
    """Which side of the equality the CTE sits on is the author's typing, not a fact about the join.

    `a = b` and `b = a` are the same predicate, and sqlglot keeps the two apart in the tree, so the
    orientation has to be resolved rather than assumed. Getting it wrong is not a crash: the pair
    would simply never be found, `_cte_edge` would see no join key, and the statement would report
    `undetermined` — a receipt that declines to answer half the statements it can answer, on a
    difference nobody writing SQL considers a difference.

    The derived edge is compared rather than the status, because both orientations of THIS statement
    happen to end at `not_multiplied` — one by deriving a `one_to_one` and one by abstaining — and
    only the edge tells those apart.
    """
    sql = ("WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
           f"SELECT SUM(orders.total_amount) FROM orders JOIN oi ON {on}")
    tree = _parse(sql)
    _scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", expected)], label
    assert [a.status for a in _reports(sql)] == [rt.NOT_MULTIPLIED], label


@pytest.mark.parametrize("label,sql", UNREADABLE_CTE_BODIES)
def test_a_cte_body_the_analysis_cannot_read_is_undetermined(label, sql):
    """A16. Every way the resolution can decline ends in `undetermined`, never in a clean answer.

    Each row differs from a resolvable statement in exactly one respect, and each of those respects
    is a way the rows behind the aggregate could differ from what the outer query appears to join.
    A set-operation body sums its arms; `DISTINCT` and an aggregate collapse rows; a join inside the
    body multiplies where the outer walk does not look; a body reading two tables or none, or a
    table the model does not declare, resolves to nothing that can be reasoned about; and two CTEs
    reading each other would not terminate at all.

    The last six are the edge rather than the body: a composite or absent join key is not the single
    -column cardinality rule's to state, an inequality and a literal are not a key, a far side that
    resolves to nothing has no grain to compare against, and a grain written as an expression has no
    column name to compare a join key to.

    They share one assertion because they share one contract. This layer is allowed to say a number
    was multiplied, or that it was not, or that it could not tell — and every one of these is the
    third. What none of them may be is absent or clean: `not_multiplied` on any row here would be
    the receipt asserting a statement is sound on the strength of a body it could not read.

    Asserted over the status SET rather than an exact list because the last row is a set operation
    and produces one item per arm. Every item still has to be `undetermined` and every item still
    has to name no join, so nothing is loosened for the single-select rows: one item whose status
    must be `undetermined` is the same assertion either way.
    """
    reports = _reports(sql)
    assert {a.status for a in reports} == {rt.UNDETERMINED}, (label, reports)
    assert [j for a in reports for j in a.joins] == [], (label, reports)


def test_an_undeclared_grain_is_undetermined_rather_than_a_default_cardinality():
    """A26. An EMPTY declared grain is not a grain, and the guard sits before the inference.

    `infer_cardinality` tests `bool(from_pk)` and `bool(to_pk)`, so a table declared `grain: []` and
    a table with no grain at all are the same thing to it, and both fall through to its
    `many_to_one` default. That default is right for a model-authoring tool proposing an edge for
    review. It is wrong here, where the answer lands on a receipt as a fact: the cardinality would
    be one nobody declared.

    Measured, and this is why the guard is placed before the call rather than after: with the guard
    removed this same statement reports `not_multiplied`. The inference makes the CTE the ONE side
    of a `one_to_many` — its own grain covers the join key, the far side's does not — so no fan
    fires and the item claims the number is clean. A false clean out of a cardinality nobody
    declared is the exact failure this spec exists to remove.

    Its own model, because the branch is unreachable on `_sales_org` and weakening a fixture to
    reach a branch changes what every other test on that fixture is asserting.
    """
    pf = rt.pre_flight_check(GRAIN_CHANGING_CTE, _no_grain_org())
    assert pf.unchecked is None, pf.unchecked
    assert [(a.aggregate, a.status) for a in pf.aggregates] == [
        ("SUM(orders.total_amount)", rt.UNDETERMINED)], pf.aggregates


def test_a_cte_name_is_resolved_case_insensitively_but_reported_as_written():
    """The fold hazard, pinned in both directions on one statement.

    `_cte_names` lowercases and `_model_table_index` is keyed folded, while `_alias_map` preserves
    exactly what the statement wrote. So `WITH OI AS (…) … JOIN OI` has to fold on the way IN — to
    recognise `OI` as a CTE and to look up its source's grain — and preserve on the way OUT, because
    the derived edge has to name the source the way the scope map holds it or the detector matches
    nothing at all.

    A comparison that folded on neither side would leave this reading clean, which is the direction
    that matters: `OI` is `order_items` under another spelling and the fan is real.

    The map is asserted for the same reason A13's is: this status and this edge are byte-identical
    to what base `439ecd1` produced, where `order_items` reached the outer map by leaking out of the
    CTE body rather than by `OI` being resolved to it. `{"OI": "order_items"}` is the whole claim —
    the key preserved exactly as the statement wrote it, the value the folded table it stands for —
    and it is the only place the fold's two directions are both visible at once.
    """
    tree = _parse(CASE_FOLDED_CTE)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "OI": "order_items"}, scope
    assert derived == [], derived

    reports = _reports(CASE_FOLDED_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


# --- A12: the map a caller may reason about joins from ----------------------


@pytest.mark.parametrize("label,sql,scopes,expected", [
    ("a reference inside a CTE body is not in the outer scope", CTE_LAUNDERED_FAN,
     ["cte:oi", "main", "main"], {"orders": "orders", "oi": "oi"}),
    ("a reference inside a derived table is not in the outer scope", DERIVED_TABLE_FAN,
     ["main", "subquery"], {"orders": "orders", "d": ""}),
    ("both arms of a set operation are the main query",
     "SELECT SUM(orders.total_amount) FROM orders "
     "UNION SELECT SUM(order_items.quantity) FROM order_items",
     ["main#1", "main#2"], {"orders": "orders", "order_items": "order_items"}),
])
def test_the_scope_filtered_alias_map_keeps_only_references_the_select_can_see(
    label, sql, scopes, expected,
):
    """A12. Asserted on the function, because the filter is a claim about the map and not about a
    receipt.

    Three shapes, one per thing the filter has to get right. A reference scoped `cte:oi` is written
    in a body whose rows the outer query only sees through the CTE, so it is not a table the outer
    FROM/JOIN clauses bind; a reference scoped `subquery` is the same case one construct over; and
    an arm of a set operation IS the main query, which is why the family is compared after the arm
    ordinal is stripped rather than before.

    Each case asserts the scopes its references actually carry BEFORE asserting the map, so a case
    proves what it claims rather than passing because the shape it names never arose. The third one
    needs it most: `main#1` and `main#2` are two different scope STRINGS, equal to `"main"` only
    once the ordinal is split off the right, and a filter written as a bare `scope == "main"` would
    drop both arms and leave an empty map — which no assertion on the map alone tells apart from a
    filter that is simply too keen.
    """
    tree = _parse(sql)
    assert sorted(r.scope for r in rt._table_references(tree)) == sorted(scopes), label
    assert rt._alias_map(tree, in_scope_only=True) == expected, label


def test_the_arm_ordinal_is_split_off_the_right_and_a_cte_name_can_carry_no_hash():
    """`_scope_family` splits from the RIGHT, and both halves of that choice are pinned.

    The ordinal `_arm_suffixes` appends is ours and always last, while everything to its left can
    hold a CTE name, which is caller-written text. Splitting from the left would read `cte:a#b` as
    the family `cte:a` — a scope that is not the main query either way, so nothing observable moves
    today, but the rule would be about the wrong thing.

    That no CTE name can actually carry a `#` is a fact about `_echo_name` and not about this
    function, so it is measured rather than assumed: the label a reference carries is echoed, and
    `#` is not a character that survives echoing. The right-side split is simply the form that stays
    correct without depending on it.
    """
    assert rt._scope_family("main") == "main"
    assert rt._scope_family("main#2") == "main"
    assert rt._scope_family("cte:recent#1") == "cte:recent"
    assert rt._scope_family("subquery") == "subquery"
    assert rt._echo_name("a#b") == "a?b"

    tree = _parse("SELECT SUM(orders.total_amount) FROM orders "
                  "UNION SELECT SUM(order_items.quantity) FROM order_items")
    assert sorted(r.scope for r in rt._table_references(tree)) == ["main#1", "main#2"]


@pytest.mark.parametrize("label,sql,expected", [
    ("an aliased derived table in a JOIN", DERIVED_TABLE_FAN,
     {"orders": "orders", "order_items": "order_items"}),
    ("an unaliased VALUES list in a comma join", UNALIASED_VALUES_FAN, {"orders": "orders"}),
])
def test_the_default_alias_map_is_unchanged_by_the_derived_binding(label, sql, expected):
    """A12b. The default path is bit-identical, on the shapes that could have moved.

    `tests/test_ace099_resolver_parity.py` pins the default against the helper it replaced, and it
    stays unedited — but it covers CTE and subquery shapes, none of which bind a FROM/JOIN derived
    source. Those are precisely the shapes the new walk added a node type for, so they are precisely
    the ones where a default that quietly picked up the binding would go unnoticed.

    Three callers read this map on the default and two of them would change what they DISCLOSE if
    the filter or the binding reached them: `_projected_sensitive` refuses, and `assemble_receipt`
    builds the receipt's `joins` and `tables` sections. So the assertion is equality with the exact
    map, plus the absence of the two keys the binding would have added.
    """
    tree = _parse(sql)
    assert rt._alias_map(tree) == expected, label
    assert "" not in rt._alias_map(tree), label
    assert "d" not in rt._alias_map(tree), label


# --- the false clean the filter itself creates ------------------------------


@pytest.mark.parametrize("label,sql", [
    ("the aggregate's column is qualified", DERIVED_TABLE_FAN),
    ("the aggregate's column is unqualified", DERIVED_TABLE_UNQUALIFIED),
])
def test_an_alias_bound_to_nothing_is_undetermined_rather_than_clean(label, sql):
    """The proof that the derived binding had to ship in the same commit as the scope filter.

    `d` is an `exp.Subquery`, so it never entered the alias map at all, and `order_items` reached
    the outer map only by leaking out of `d`'s body — which is the single reason this shape reported
    `multiplied` before. Land the filter alone and the leak stops, the outer scope is the one table
    `orders`, `SUM(orders.total_amount)` resolves perfectly, and a statement whose rows a join
    genuinely multiplied reports `not_multiplied`. That is the receipt asserting something false,
    and no test on `main` would have failed.

    The second row is the same shape with the column unqualified, and it needs both halves to fail
    before it reads clean: with `d` bound, the scope holds two entries and `_resolve_col_table`
    declines to attribute an unqualified column at all; with `d` absent, `orders` is the only
    candidate and the column resolves cleanly to it. Either mechanism alone would have caught this
    one, so it is the row that says the two are not the same mechanism.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)
    assert [j for a in reports for j in a.joins] == [], (label, reports)


def test_a_parenthesized_table_is_the_table_and_not_a_source_of_its_own():
    """`FROM (orders)` binds no name, so the scope-completeness rule must not fire on it.

    sqlglot parses it to `Subquery(this=Table)` with an empty alias — structurally the same node
    kind as a derived table, and semantically just a bracket around a table name the `exp.Table`
    arm has already bound. Binding it too would put an empty key in the map, and by the
    scope-completeness conjunct that one entry turns EVERY aggregate in the SELECT `undetermined`.

    Measured both ways while the guard was being placed: without the discriminator this statement
    reports `undetermined`, with it `not_multiplied`. The distinction is `.this` being an
    `exp.Table` and nothing else — an unaliased `VALUES`, `LATERAL` or derived `SELECT` in the same
    position introduces rows nothing can name and MUST bind, which is the corpus member above.
    """
    reports = _reports(PARENTHESIZED_TABLE)
    assert [(a.aggregate, a.status) for a in reports] == [
        ("SUM(orders.total_amount)", rt.NOT_MULTIPLIED)], reports
    assert rt._alias_map(_parse(PARENTHESIZED_TABLE), in_scope_only=True) == {"orders": "orders"}


@pytest.mark.parametrize("label,sql", [
    ("a VALUES list", VALUES_SOURCE),
    ("a LATERAL", LATERAL_SOURCE),
    ("an UNNEST", UNNEST_SOURCE),
])
def test_a_source_the_guarded_path_refuses_is_still_answered_honestly_on_the_prepare_surface(
    label, sql,
):
    """Why the derived binding arm is not dead code.

    `check_scopable` (ACE-037) refuses both of these source kinds at the `execute_guarded`
    chokepoint, so no query that RUNS ever reaches the multiplication analysis carrying one. That is
    asserted here rather than assumed, because it is the premise of the whole question.

    But `cmd_preflight` and `cmd_prepare` in `semantic_model/cli.py` call `pre_flight_check`
    directly, with no gate battery at all — `cmd_prepare`'s own docstring says it is not a gate and
    never was — and the query skill runs `sm prepare` on every tier. On that surface both of these
    reported `not_multiplied` over a source that can produce many rows per order. A reader of a
    prepare receipt has no way to know a gate they never invoked would have refused the statement;
    what they have is the sentence in front of them, and it said the number was sound.

    So the analysis answers honestly on both surfaces, and the binding exists for the one that has
    no gate in front of it. `pre_flight_check` is called directly here for the same reason: driving
    this through `execute_guarded` would measure the refusal and never reach the claim.

    `UNNEST` is the member that proves the binding has to be a DENYLIST. It is an `exp.Unnest`, and
    an allowlist naming `Subquery`, `Lateral` and `Values` simply did not hold it: the source never
    entered the map, the scope-completeness conjunct saw nothing missing, and the statement read
    `not_multiplied` over rows an unnest can multiply. A source kind nobody anticipated has to
    default to `undetermined`, which only binding by exclusion gives.
    """
    assert rt.check_scopable(sql, _sales_org()) is not None, label
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, (label, pf.unchecked)
    assert [a.status for a in pf.aggregates] == [rt.UNDETERMINED], (label, pf.aggregates)


# --- what the narrowed scope map costs the semantic checks ------------------


@pytest.mark.parametrize("label,sql", [
    ("qualified", "SELECT SUM(facts.unit_price) FROM facts"),
    ("unqualified, one table in scope", "SELECT SUM(unit_price) FROM facts"),
    ("unqualified, through a CTE",
     "WITH c AS (SELECT unit_price FROM facts) SELECT SUM(unit_price) FROM c"),
    ("qualified by a CTE alias — GAINED by the grain resolution",
     "WITH c AS (SELECT unit_price FROM facts) SELECT SUM(c.unit_price) FROM c"),
    ("qualified by a SELECT * CTE alias — GAINED by the grain resolution",
     "WITH c AS (SELECT * FROM facts) SELECT SUM(c.unit_price) FROM c"),
])
def test_an_aggregation_class_violation_still_fires_on_the_statement_that_makes_it(label, sql):
    """`_check_aggregation_semantics` reads the same map, so its floor is pinned here.

    It resolves the summed column's table through the scope map `_preflight_select` hands it, and
    that map changed twice over: narrowed to what this SELECT's own FROM/JOIN clauses bind, then
    widened again where a grain-preserving CTE resolves back to the table it hands through. The net
    was measured against base `439ecd1` over eight spellings, and it is NOT the loss of two that an
    earlier mid-build measurement recorded — that reading was taken before the grain resolution
    landed and is superseded by this one.

    ONE finding is lost: `SUM(unit_price) FROM (SELECT unit_price FROM facts) f`, which fired only
    by reading `facts` out of a derived body the outer statement cannot see. Its status moved from
    `not_multiplied` to `undetermined` in the same change, so it trades a finding for a killed false
    clean rather than simply going quiet.

    TWO are gained, and both are the resolution working: `SUM(c.unit_price)` over a CTE resolves `c`
    back to `facts`, where before it resolved to the undeclared name `c` and the check had nothing
    to look up. Those are the last three rows here — pinned as gains rather than mentioned, because
    a narrowing that quietly took them away again would otherwise look like the loss it is not.

    Both plain spellings stay, and they take different paths through the resolver: the qualified one
    through the map, the unqualified one through the single-in-scope-table fallback, which reads the
    map's VALUES — the half the filter changed.
    """
    pf = rt.pre_flight_check(sql, _averageable_org())
    assert [f.risk for f in pf.findings] == ["bad_aggregation"], (label, pf.findings)
    assert "unit_price" in pf.findings[0].reason, (label, pf.findings[0].reason)


def test_the_one_aggregation_class_finding_this_spec_gives_up_is_the_one_that_read_a_hidden_scope():
    """The other side of the differential above, so the narrowing is bounded rather than open-ended.

    This is the single `bad_aggregation` that base `439ecd1` reported and this spec does not. It
    fired because `facts` leaked out of the derived table's body into the outer scope map, which is
    precisely the leak S3 removes: a column inside a derived body is not a reference of the outer
    statement, and the outer statement here reads one source, `f`, whose columns the analysis cannot
    attribute to any declared table.

    Asserted together with the status, because the two halves are one trade. Base called this
    statement `not_multiplied` — a positive claim that a number computed over an unreadable derived
    source is sound — and it now declines to answer. Losing a true finding is a real cost; losing it
    alongside a false clean is the exchange this spec makes, and stating only one half of it would
    misrepresent the change in either direction.
    """
    sql = "SELECT SUM(unit_price) FROM (SELECT unit_price FROM facts) f"
    pf = rt.pre_flight_check(sql, _averageable_org())
    assert [f.risk for f in pf.findings] == [], pf.findings
    assert [a.status for a in pf.aggregates] == [rt.UNDETERMINED], pf.aggregates


def test_the_chasm_still_reports_both_of_the_aggregates_it_inflates():
    """The chasm reads `table_set` off the same map the filter narrowed, so it is re-pinned here.

    `tests/test_semantic_model_runtime.py::test_chasm_trap_is_reported` is the gate and stays
    unedited; this asserts the same property from the file that changed the map, so a narrowing that
    cost the chasm one of its two items fails beside the change that caused it rather than in
    another file. Both aggregates are inflated by the cross-product and both must say so — a chasm
    reported on one of the two numbers it ruins is a receipt that reads as half a fact.
    """
    reports = _reports(CHASM_OVER_TWO_MEASURES)
    assert [(a.aggregate, a.status) for a in reports] == [
        ("SUM(o.revenue)", rt.MULTIPLIED), ("COUNT(t.id)", rt.MULTIPLIED)], reports
    assert [f.risk for a in reports for f in a.findings] == ["chasm_trap", "chasm_trap"], reports


# --- A25: an item that answers, names its evidence -------------------------


def test_every_item_either_names_its_joins_or_declines_to_answer():
    """A25. Whatever an item says, it says it consistently — over every shape this spec decides.

    Two directions, and each is a way a receipt can be internally contradictory rather than merely
    wrong. A `multiplied` item with an empty `joins` tells a reader their number was inflated and
    gives them nothing to look at, which is a fact they cannot check and cannot act on. A
    `not_multiplied` or `undetermined` item that names a join asserts the opposite of what its own
    status says, and a reader has no way to know which half to believe.

    An internal loop rather than a parametrize, deliberately: the property is about the whole set,
    the set is assembled from constants defined for other tests, and a failure has to print the SQL
    it came from or the label alone would not locate it. Collecting every offender before asserting
    means one run reports all of them instead of stopping at the first.

    `status` is checked against the three constants as well. The vocabulary is closed, and an item
    carrying anything else is a fourth thing this layer is not allowed to say.
    """
    offenders: list[tuple[str, str, str, list[str]]] = []
    for label, sql in ALL_ANALYSED_SHAPES:
        for item in _reports(sql):
            assert item.status in (rt.MULTIPLIED, rt.NOT_MULTIPLIED, rt.UNDETERMINED), (label, item)
            names_joins = bool(item.joins)
            if names_joins != (item.status == rt.MULTIPLIED):
                offenders.append((label, item.aggregate, item.status, item.joins))
    assert offenders == [], offenders


# --- A20: the two surfaces report the same aggregates ----------------------


@pytest.mark.parametrize("label,sql,expected", [
    ("an invariant aggregate over a fan", MIN_OVER_FAN,
     [("MIN(orders.total_amount)", rt.MULTIPLIED, ["fan_out_invariant"])]),
    ("a value already at the many side's grain", VALUE_AT_MANY_GRAIN,
     [("SUM(order_items.quantity * orders.total_amount)", rt.NOT_MULTIPLIED, [])]),
    ("a grain-preserving CTE the analysis resolves", GRAIN_PRESERVING_CTE,
     [("SUM(o.total_amount)", rt.MULTIPLIED, ["fan_trap"])]),
    ("a grain-changing CTE whose edge is derived", GRAIN_CHANGING_CTE,
     [("SUM(orders.total_amount)", rt.NOT_MULTIPLIED, [])]),
    ("a derived table bound to nothing", DERIVED_TABLE_FAN,
     [("SUM(orders.total_amount)", rt.UNDETERMINED, [])]),
    ("three statuses and both risk labels at once", EVERY_STATUS_SQL, EVERY_STATUS_ITEMS),
])
def test_the_preflight_and_the_receipt_agree_about_the_aggregates(label, sql, expected):
    """A20. Both surfaces read one analysis, so the two renderings must be byte-equal.

    `pre_flight_check` parses and analyses; `assemble_receipt` analyses a tree it already holds.
    They are meant to be two doors onto one answer, and the way that stops being true is silent — a
    caller reading a receipt and a caller reading a pre-flight result would simply disagree about
    the same statement, with nothing failing anywhere.

    ACE-060 already asserts this, and its file stays unedited — but its shapes all predate this
    spec, so none of them exercises an invariance label, a value-path attribution, a resolved CTE or
    an alias bound to nothing. These six do, one per mechanism this spec added, on `_sales_org` so
    that no fixture ACE-060 depends on has to move.

    The comparison is the serialized item list against `as_dict()`, not a status-by-status walk:
    `joins`, `findings` and the aggregate's own label are all part of what the two surfaces have to
    agree about, and a spot check of statuses would pass while a reason string differed.

    And each row is ANCHORED, because equality between the two surfaces is `f(x) == f(x)` on its
    own. Both entry points call `_aggregate_reports` on one tree and differ only in whether
    `visible` is handed in — computed identically either way — and in the receipt's
    `_RECEIPT_MAX_REFS` cap, which none of these six reaches. So the agreement half of this test
    passes with the production change fully reverted, and it would pass just as well if every one of
    these statements degraded to a single `undetermined` item. The expected list is what makes each
    row say which mechanism it is here for. The cap, which is the one place the two surfaces really
    do diverge, is the test below.
    """
    org = _sales_org()
    items = rt.assemble_receipt(org, sql)["aggregates"]["items"]
    reports = rt.pre_flight_check(sql, org).aggregates
    assert items == [a.as_dict() for a in reports], label
    assert [(a.aggregate, a.status, [f.risk for f in a.findings]) for a in reports] == expected, (
        label, reports)


def test_the_receipt_bounds_the_item_list_the_preflight_returns_whole():
    """A20's one real divergence: the receipt is capped and the pre-flight result is not.

    Every other difference between the two entry points is arranged away — one analysis, one tree,
    one `visible` set — and this one is deliberate. A receipt is tool output bounded at every
    section, so `_RECEIPT_MAX_REFS` truncates the items and COUNTS what it dropped on the marker,
    because a truncated list under a silent marker is a positive claim of completeness.
    `PreFlightResult` is an in-process object with no such bound, and a caller reading it gets all
    of them.

    So the two surfaces agree UP TO the cap and not beyond it, and that is the contract this asserts
    rather than a bug it reports. It matters because the agreement test above cannot see it: none of
    its six shapes comes near 50 aggregates, so an equality that holds only below the cap and a
    equality that holds everywhere are the same test there. A reader who takes A20 to mean the two
    surfaces are interchangeable at any size is reading something no test said.

    The real constant is used rather than a monkeypatched one. ACE-060's
    `test_the_cap_counts_aggregates_and_says_so` already pins the counting behaviour at a patched
    cap of 2; what is unpinned is what the OTHER surface does at the same statement, and patching
    the constant would leave the shipped 50 uncovered on the only question this test is about.
    """
    org = _sales_org()
    over = rt._RECEIPT_MAX_REFS + 5
    sql = (f"SELECT {', '.join(f'SUM(orders.total_amount) AS a{i}' for i in range(over))} "
           f"{FAN_JOIN}")
    section = rt.assemble_receipt(org, sql)["aggregates"]
    reports = rt.pre_flight_check(sql, org).aggregates

    assert len(reports) == over, len(reports)
    assert len(section["items"]) == rt._RECEIPT_MAX_REFS, len(section["items"])
    assert section["items"] == [a.as_dict() for a in reports][:rt._RECEIPT_MAX_REFS]
    # And the marker says how many it dropped, so the shorter list is not read as the whole list.
    assert "5 further aggregate(s) are not listed." in section["undetermined"], section


# --- the invariance boundary, pinned where it is rather than where it could be ---


def test_a_distinct_concatenation_with_an_ordering_arm_is_not_read_as_invariant():
    """The boundary of `_is_fan_immune`, pinned in the direction it currently errs.

    `STRING_AGG(DISTINCT x ORDER BY y)` parses to `GroupConcat(this=Order(this=Distinct(...)))`, so
    `agg.this` is an `exp.Order` and not the `exp.Distinct` the predicate tests for, and
    `args["distinct"]` is `None` as well. The same expression WITHOUT the ordering arm puts
    `exp.Distinct` directly under the aggregate and reads as invariant, so one keyword moves the
    answer.

    A duplication genuinely cannot move a DISTINCT concatenation, so the honest label here is
    `fan_out_invariant` and what it gets is `fan_trap`. That is the SAFE direction — it names a
    defect where there is none, rather than telling a reader a number is unaffected when it is —
    and it is not worth widening the predicate for, because the widening is where this split gets
    dangerous. It is pinned so the boundary cannot move without someone deciding to move it: a
    change that made this read invariant is a change that needs its own argument.

    The two parses are asserted alongside the report, since the whole behaviour rests on a fact
    about sqlglot's tree rather than about this module.
    """
    ordered = parse_one(
        f"SELECT STRING_AGG(DISTINCT orders.status, ',' ORDER BY orders.status) {FAN_JOIN}"
    ).find(exp.AggFunc)
    assert isinstance(ordered.this, exp.Order), ordered.this
    assert ordered.args.get("distinct") is None, ordered.args
    assert rt._is_fan_immune(ordered) is False, ordered

    plain = parse_one(f"SELECT STRING_AGG(DISTINCT orders.status, ',') {FAN_JOIN}").find(exp.AggFunc)
    assert isinstance(plain.this, exp.Distinct), plain.this
    assert rt._is_fan_immune(plain) is True, plain

    reports = _reports(
        f"SELECT STRING_AGG(DISTINCT orders.status, ',' ORDER BY orders.status) {FAN_JOIN}"
    )
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert [f.risk for f in reports[0].findings] == ["fan_trap"], reports[0].findings


def test_the_invariance_split_is_the_fan_branchs_alone_and_the_chasm_keeps_its_word():
    """The other boundary: `fan_out_invariant` is a FAN label, and a chasm does not get one.

    A cross-product cannot move a `MIN` or a `COUNT(DISTINCT)` any more than a fan-out can — the
    property is about the aggregate, not about which join duplicated the rows — so on the reading
    that produced the split, these two are as mislabelled as `MIN` over a fan was. They are not
    relabelled, and that is deliberate rather than an oversight: the spec scopes S1 to the fan
    branch, `_is_fan_immune` is called at one site inside the fan loop, and widening a risk
    vocabulary is a contract change that needs its own argument.

    It is asserted because nothing else would notice it move. `SKILL.md` documents both labels for a
    reader, `chasm_trap` and `fan_out_invariant` are both in `_MULTIPLYING_RISKS` so `status` is
    identical either way, and hoisting `_is_fan_immune` out of the fan loop into the shared path is
    a one-line change that reads like a simplification. This is the assertion that makes it a
    decision: the two aggregates here still say `chasm_trap`, and the word this file spent a slice
    removing from the fan branch is not quietly added to a second one.

    The statuses are pinned beside the risks. `chasm_trap` and `fan_out_invariant` are both
    multiplying risks, so a swap between them is invisible in `status` — which is exactly why the
    risk is what this asserts on.
    """
    reports = _reports(CHASM_OVER_INVARIANT_MEASURES)
    assert [(a.aggregate, a.status) for a in reports] == [
        ("MIN(o.revenue)", rt.MULTIPLIED), ("COUNT(DISTINCT t.id)", rt.MULTIPLIED)], reports
    assert [f.risk for a in reports for f in a.findings] == ["chasm_trap", "chasm_trap"], reports
    assert "fan_out_invariant" not in _every_risk(CHASM_OVER_INVARIANT_MEASURES), reports


# --- a WITH binds its name for every arm below it --------------------------


def test_a_set_operation_arm_sees_the_cte_the_statement_bound_above_it():
    """A WITH sits above the arms, so an arm that looks for it inside itself finds nothing.

    Measured on this statement before the fix: `main#1` reported `not_multiplied` over a laundered
    fan, because `_cte_names` was read off the ARM, the guard never fired, and `oi` stayed bound to
    the undeclared name `oi`. The unreadable-body shape failed the same way and is a row in the
    guard table above.

    The fix is narrow on purpose and both halves are asserted here. CTE names and bodies come from
    the arm's LEXICAL ANCESTORS, because a WITH binds its name for every arm below it — the same
    reason `_aggregate_reports` computes `visible` on the root. Which TABLES an arm reads does NOT:
    the alias map stays strictly per-arm, so the second arm below, which reads only `orders`, is
    honestly clean and is not dragged into the first arm's fan.

    The scope labels are asserted too. They are what tells a reader which arm an item belongs to,
    and reading CTE names off the root is exactly the kind of change that could have flattened two
    arms into one scope without any status moving.
    """
    reports = _reports(UNION_ARM_CTE_FAN)
    assert [(a.scope, a.status) for a in reports] == [
        ("main#1", rt.MULTIPLIED), ("main#2", rt.MULTIPLIED)], reports
    assert [a.joins for a in reports] == [[FAN_EDGE], [FAN_EDGE]], reports

    # And an arm that reads no CTE keeps its own answer, which is what "per-arm" has to mean. The
    # statement is `MIXED_SET_OPERATION`, a corpus member: the corpus asks that its laundered arm is
    # never reported clean, and this asks that its honest arm still is.
    mixed = _reports(MIXED_SET_OPERATION)
    assert [(a.scope, a.status) for a in mixed] == [
        ("main#1", rt.MULTIPLIED), ("main#2", rt.NOT_MULTIPLIED)], mixed


# --- and a WITH that does NOT bind here binds nothing -----------------------
#
# The other half of the same rule, and the one that was measured wrong. `sel.root().find_all(
# exp.CTE)` collects every CTE in the statement, keyed by folded name with last-writer-wins, so a
# CTE from a scope that does not bind for this SELECT could win the name. Both shapes below were
# receipts calling an inflated number sound, so both are corpus members and both are declared with
# the corpus: `SAME_NAME_PER_ARM`, `SAME_NAME_PER_ARM_SWAPPED` and
# `NESTED_WITH_SHADOWING_A_REAL_TABLE`.


def test_a_cte_declared_in_a_sibling_arm_does_not_bind_for_this_one():
    """Same name, two arms, and the answer may not depend on which was written first.

    Each arm declares its own `x`. In the joining arm `x` is `orders`, the one side of a fan over
    `order_items`; in the other it is `order_items` itself, read with no join at all. Read off the
    root, the two bodies collided on the folded key `x` and the last one written won for both arms,
    so the joining arm was told its `x` was `order_items` — no table on the one side of anything —
    and reported `not_multiplied`. Swapping the arms swapped which statement got the wrong body.

    Asserted as a PAIR rather than as two expected values, because the property is that the fan arm
    answers the same way whichever position it is written in. Per-arm expectations would pass on an
    implementation that is right for one ordering and wrong for the other, which is what it was.
    """
    forward = {a.scope: a.status for a in _reports(SAME_NAME_PER_ARM)}
    swapped = {a.scope: a.status for a in _reports(SAME_NAME_PER_ARM_SWAPPED)}
    assert forward["main#1"] == swapped["main#2"] == rt.MULTIPLIED, (forward, swapped)
    assert forward["main#2"] == swapped["main#1"] == rt.NOT_MULTIPLIED, (forward, swapped)


def test_a_with_inside_a_subquery_does_not_rebind_the_statements_own_table():
    """A CTE bound inside a `WHERE … IN (…)` is not in scope for the statement that contains it.

    `order_items` in the outer FROM is the declared table, and the join to it is the fan. Read off
    the root, the subquery's `WITH order_items AS (SELECT id FROM customers)` took the name, the
    outer `order_items` resolved to `customers`, and the fan the statement really takes vanished.
    """
    reports = _reports(NESTED_WITH_SHADOWING_A_REAL_TABLE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


def test_the_with_argument_is_read_under_the_spelling_sqlglot_actually_uses():
    """`with_`, not `with` — the same rename trap already documented for `from`.

    Read under one spelling only, `_visible_cte_bodies` returns the empty dict for every statement
    ever written. Nothing fails loudly: every CTE reference falls to the fail-closed binding and the
    receipt answers `undetermined` for statements it can answer. The corpus above forbids false
    cleans and would not notice, so the argument key is pinned directly.
    """
    tree = _parse(CTE_LAUNDERED_FAN)
    assert tree.args.get("with_") is not None, tree.args.keys()
    assert set(rt._visible_cte_bodies(tree)) == {"oi"}, rt._visible_cte_bodies(tree)


# --- the body's own FROM, and only its own ----------------------------------

# The docstring's own worked example for the no-FROM guard. `body.find(exp.From)` is RECURSIVE, so
# it reached the FROM inside the EXISTS and the guard never tested what it said it tested.
EXISTS_ONLY_CTE = (
    "WITH c AS (SELECT 1 AS x WHERE EXISTS (SELECT 1 FROM order_items)) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN c ON c.x = orders.id"
)
# A body whose FROM is a derived table. It hands back one row per DISTINCT order, not one row per
# order item, so reading through the wrapper named a join the statement does not take.
DERIVED_FROM_CTE = (
    "WITH c AS (SELECT * FROM (SELECT DISTINCT order_id FROM order_items) g) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN c ON c.order_id = orders.id"
)
# The count of tables stays WHOLE-SUBTREE. This body's own FROM is a plain table, and it is caught
# only because the IN subquery's table is counted too.
IN_SUBQUERY_CTE = (
    "WITH c AS (SELECT order_id FROM order_items WHERE order_id IN (SELECT id FROM orders)) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN c ON c.order_id = orders.id"
)


@pytest.mark.parametrize("label,sql", [
    ("the only FROM is inside a WHERE EXISTS", EXISTS_ONLY_CTE),
    ("the FROM is a derived table", DERIVED_FROM_CTE),
    ("a table in an IN subquery, counted whole-subtree", IN_SUBQUERY_CTE),
])
def test_a_cte_body_whose_own_from_names_no_table_is_undetermined(label, sql):
    """The grain-preserving guard reads the body's OWN `FROM`, under both argument spellings.

    Each of these bodies produces a row count that is not the row count of any table it mentions,
    and each was resolved to `order_items` anyway — the first two by `body.find(exp.From)` reaching
    one scope further in, and all three by a table count that is deliberately whole-subtree. The
    first two then reported `multiplied` naming `orders (1) <- order_items (N)`, a join the
    statement does not take; `undetermined` is what a body the analysis cannot read is worth.

    The third is here to hold the count where it is. Its own FROM is a plain `exp.Table`, so a fix
    that narrowed the COUNT to the body's own FROM as well would resolve it to `order_items` and
    credit it with a row count it does not have.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)
    assert reports[0].joins == [], (label, reports[0].joins)


# --- a grain is what its columns say, and only when they say it -------------

# `GROUP BY orders.id, order_items.id` is two columns. Strip the qualifiers and it is one, matching
# the single join key exactly and declaring the CTE unique on a key it is not unique on.
GRAIN_WITH_COLLIDING_BARE_NAMES = (
    "WITH g AS (SELECT orders.id AS id, order_items.id AS iid, SUM(order_items.quantity) q "
    "FROM orders JOIN order_items ON order_items.order_id = orders.id "
    "GROUP BY orders.id, order_items.id) "
    "SELECT SUM(customers.id) FROM customers JOIN g ON g.id = customers.id"
)
# `ROLLUP` adds a subtotal row per order, so the grain is not `{order_id}` and the join to it fans.
GRAIN_WITH_ROLLUP = (
    "WITH g AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
    "GROUP BY order_id, ROLLUP(product_id)) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN g ON g.order_id = orders.id"
)
GRAIN_WITH_CUBE = (
    "WITH g AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
    "GROUP BY CUBE(order_id, product_id)) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN g ON g.order_id = orders.id"
)
GRAIN_WITH_GROUPING_SETS = (
    "WITH g AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
    "GROUP BY GROUPING SETS ((order_id), (product_id))) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN g ON g.order_id = orders.id"
)


@pytest.mark.parametrize("label,sql", [
    ("two grain columns whose bare names collide", GRAIN_WITH_COLLIDING_BARE_NAMES),
    ("a column list beside a ROLLUP", GRAIN_WITH_ROLLUP),
    ("a pure CUBE", GRAIN_WITH_CUBE),
    ("pure GROUPING SETS", GRAIN_WITH_GROUPING_SETS),
])
def test_a_grain_the_group_by_does_not_state_is_undetermined(label, sql):
    """A grain read off `group.expressions` alone is not the grain the body emits.

    The bare-name case is the sharper one, because it produces a grain that is not merely incomplete
    but WRONG: `[k.name for k in keys]` strips the qualifier, `GROUP BY orders.id, order_items.id`
    becomes a one-element list, `infer_cardinality` finds it equal to the single join key, and the
    receipt declares the CTE unique on `id` — one row per customer — when it is one row per order
    item. The three grouping constructs each add rows the column list does not describe; `ROLLUP`
    beside a column list is the one that was silently wrong, since `CUBE` and `GROUPING SETS`
    written alone leave `expressions` empty and so failed closed by accident.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)


@pytest.mark.parametrize("label,expressions,expected", [
    ("plain columns", "GROUP BY order_id, product_id", ["order_id", "product_id"]),
    ("qualified columns that differ", "GROUP BY o.order_id, i.product_id",
     ["order_id", "product_id"]),
    ("folded to one spelling", "GROUP BY ORDER_ID", ["order_id"]),
    ("bare names that collide", "GROUP BY o.id, i.id", []),
    ("an expression rather than a column", "GROUP BY DATE_TRUNC('month', created_at)", []),
    ("a column list beside a ROLLUP", "GROUP BY order_id, ROLLUP(product_id)", []),
    ("a pure CUBE", "GROUP BY CUBE(order_id, product_id)", []),
    ("pure GROUPING SETS", "GROUP BY GROUPING SETS ((order_id), (product_id))", []),
    ("no GROUP BY at all", "", []),
])
def test_the_group_by_grain_is_empty_whenever_the_columns_do_not_state_it(
    label, expressions, expected,
):
    """`_group_by_grain` on the function, because every way of being empty means the same thing.

    Empty is `undetermined` downstream, and each row here is a different way the body's row grain is
    not the list of columns it wrote. Folding is asserted rather than assumed: the comparison this
    feeds is against a declared `Table.grain` that a Snowflake or Oracle catalog hands back in a
    different case from the one the query writes.
    """
    body = parse_one(f"SELECT order_id, SUM(quantity) FROM order_items o {expressions}")
    assert rt._group_by_grain(body) == expected, label


# --- one spelling on both sides of every comparison -------------------------

# The CTE is grouped to exactly the key it is joined on, so it is one row per order and nothing
# fans. The join key is written in a different case from the GROUP BY, which is a difference no
# database makes and this comparison did.
JOIN_KEY_IN_ANOTHER_CASE = (
    "WITH g AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN g ON g.ORDER_ID = orders.id"
)


def test_a_join_key_written_in_another_case_does_not_invent_a_fan():
    """Case folding, on the one comparison in this module that was missing it.

    `check_column_scope` states the module's convention as "matching is case-insensitive", and
    `_cte_names` and `_model_table_index` both fold. The grain comparison did not: the GROUP BY
    grain `order_id` and the join key `ORDER_ID` were read as different columns, `infer_cardinality`
    concluded the CTE is not unique on its join key, and a `many_to_one` edge put `orders` on the
    one side of a fan the statement does not have. It also walks straight past the empty-grain
    guard, since a case-mismatched grain is a non-empty one.

    The derived EDGE is asserted beside the status because the cardinality is the thing that was
    wrong; a status alone cannot tell a corrected edge from an abstention.
    """
    tree = _parse(JOIN_KEY_IN_ANOTHER_CASE)
    _scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(_sales_org()))
    assert [(r.from_table, r.to_table, r.relationship) for r in derived] == [
        ("g", "orders", "one_to_one")], derived
    assert [a.status for a in _reports(JOIN_KEY_IN_ANOTHER_CASE)] == [rt.NOT_MULTIPLIED]


def test_a_declared_grain_the_catalog_upper_cased_is_still_the_grain():
    """The same fold, on the side that comes from the MODEL rather than from the SQL.

    Snowflake and Oracle catalogs return uppercase identifiers, so `Table.grain` can arrive as
    `["ID"]` for a query that writes `orders.id`. Unfolded, the declared grain matched no join key,
    `infer_cardinality` read the far side as non-unique, and the derived edge came back
    `one_to_many` — a cardinality nobody declared, sitting on a receipt.
    """
    org = _sales_org()
    orders = next(t for t in org.subject_areas[0].tables_defined if t.name == "orders")
    orders.grain = ["ID"]
    tree = _parse(GRAIN_CHANGING_CTE)
    _scope, derived = rt._resolve_cte_scope(
        tree, rt._visible_cte_bodies(tree), rt._alias_map(tree, in_scope_only=True),
        rt._model_table_index(org))
    assert [(r.from_table, r.to_table, r.relationship) for r in derived] == [
        ("oi", "orders", "one_to_one")], derived


# --- availability on caller-controlled SQL ---------------------------------


def test_a_long_chain_of_ctes_declines_to_answer_rather_than_losing_the_receipt():
    """A linear chain is not a cycle, and `seen` only catches a cycle.

    990 CTEs each reading the one before fit in 29,573 characters, well under the 50,000-character
    statement cap, and raised `RecursionError` out of the resolver. `_receipt_for` catches bare
    `Exception` and returns `RECEIPT_BUILD_FAILED`, so the statement still ran and returned rows
    while the caller silently lost the trust layer — caller-chosen input that turns off the receipt
    without turning off the answer. A depth bound turns it into the fail-closed answer instead.

    The character count is asserted so the premise stays true: if the cap or the shape changed
    enough that this no longer fits inside it, the test would be measuring nothing.
    """
    links = 990
    bodies = ["c0 AS (SELECT * FROM order_items)"]
    bodies += [f"c{i} AS (SELECT * FROM c{i - 1})" for i in range(1, links)]
    sql = ("WITH " + ", ".join(bodies) +
           f" SELECT SUM(orders.total_amount) FROM orders JOIN c{links - 1} "
           f"ON c{links - 1}.order_id = orders.id")
    assert len(sql) < 50_000, len(sql)

    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], reports


def test_the_root_cte_set_and_the_model_index_are_built_once_per_statement(monkeypatch):
    """Neither of these is per-arm work, and both were.

    `_cte_names(tree.root())` was the unconditional left operand of the CTE guard's `&`, so it
    walked the WHOLE tree once per set-operation arm: measured at 471 arms, that one call was 93 to
    95% of the total, and a statement with NO CTE anywhere regressed from 0.14s to 5.4s while the
    comment beside it said such a statement "pays nothing for this". `_model_table_index` walks
    every table in the model and was rebuilt per arm on the `assemble_receipt` path, which already
    holds one.

    Counted rather than timed, because a wall-clock threshold on a shared runner is a flake and the
    property is not "fast" but "not once per arm". Ten arms, so a per-arm implementation cannot
    coincide with a per-statement one.
    """
    calls = {"cte_names": 0, "model_index": 0}
    real_cte_names, real_index = rt._cte_names, rt._model_table_index

    def counted_cte_names(tree):
        calls["cte_names"] += 1
        return real_cte_names(tree)

    def counted_index(org):
        calls["model_index"] += 1
        return real_index(org)

    monkeypatch.setattr(rt, "_cte_names", counted_cte_names)
    monkeypatch.setattr(rt, "_model_table_index", counted_index)

    arms = " UNION ALL ".join([CTE_LAUNDERED_FAN.split(") ", 1)[1]] * 10)
    sql = "WITH oi AS (SELECT * FROM order_items) " + arms
    reports = rt.pre_flight_check(sql, _sales_org()).aggregates
    assert len(reports) == 10, reports
    assert calls["cte_names"] == 1, calls
    assert calls["model_index"] == 1, calls


# --- the same defect class, one node type over -----------------------------
#
# Two independent re-reviews found the same root cause in two functions: `_value_sources` and
# `_grain_preserving_source` were both written as DENYLISTS with an unsafe default. One enumerated
# the alternation node types and let everything else union its children; the other rejected three
# `exp.Select` arguments and accepted the rest. In both, an unanticipated shape landed on the side
# that CLEARS a finding, and in both the docstring named the hazard without the code enforcing it.
# Both are now allowlists: what is understood is enumerated, and everything else fails closed.
#
# The batteries below are the per-finding regressions. The corpus above carries the seven shapes
# they were found on, so the property that no known-inflated statement reads clean covers them too.


@pytest.mark.parametrize("label,sql,aggregate", [
    ("GREATEST", GREATEST_ALTERNATION_FAN,
     "SUM(GREATEST(orders.total_amount, order_items.quantity))"),
    ("LEAST", LEAST_ALTERNATION_FAN, "SUM(LEAST(orders.total_amount, order_items.quantity))"),
    ("NVL2", NVL2_ALTERNATION_FAN, "SUM(NVL2(order_items.quantity, orders.total_amount, 0))"),
    ("DECODE", DECODE_ALTERNATION_FAN,
     "SUM(DECODE(order_items.product_id, 1, orders.total_amount, 0))"),
])
def test_an_alternation_spelled_as_a_function_still_names_the_edge_it_multiplies(
    label, sql, aggregate,
):
    """A7, A8. Four ordinary-SQL shapes that reported `not_multiplied` over a real fan.

    Each of these returns ONE of its arguments, so the value is at a table's grain only if every
    argument is — the rule `exp.Case` and `exp.Coalesce` were already read by. Without a case of
    their own they reached the generic branch, which unioned every operand, put `order_items` on the
    value path, and suppressed the only edge there was. Measured against base `439ecd1`, all four
    said `multiplied` there and `not_multiplied` here.

    The corpus above asserts only that these are not reported clean, which `undetermined` satisfies
    too. This asserts the answer, because `multiplied` naming the fan edge is what base gave and
    what the statement deserves: a one-side amount summed once per order item is inflated, whichever
    keyword the alternation was written with.
    """
    reports = _reports(sql)
    assert [(a.aggregate, a.status) for a in reports] == [(aggregate, rt.MULTIPLIED)], (
        label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)


@pytest.mark.parametrize("label,expression", [
    ("a scalar function with a many-side argument", "ROUND(orders.total_amount, 2)"),
    ("a function sqlglot has no node for", "SOME_UDF(orders.total_amount)"),
])
def test_a_node_type_the_value_path_does_not_enumerate_contributes_nothing(label, expression):
    """The inverted polarity, asserted on the classifier rather than only through an answer.

    This is the correction the four shapes above are symptoms of. A node `_value_operands` does not
    recognize used to UNION its children, so an unanticipated shape contributed its operands to the
    value path — and a many-side operand on the value path is exactly what clears a fan. Enumerated
    the other way round, it contributes nothing, an empty contribution suppresses no edge, and the
    fan is reported.

    Asserted through `_value_operands` because the two ways of contributing nothing are different
    facts. `COUNT(*)` contributes nothing because there is no column in it; these contribute nothing
    because this layer has never established what their operands mean. A test that only read the
    empty set could not tell a fail-closed default from a lucky parse.
    """
    node = parse_one(f"SELECT {expression} FROM orders").expressions[0]
    reading, operands = rt._value_operands(node)
    assert (reading, operands) == (rt._VALUE_UNKNOWN, []), (label, reading, operands)
    assert rt._value_sources(node, VALUE_SCOPE) == frozenset(), label


def test_a_wide_expression_inside_the_character_cap_does_not_cost_the_receipt():
    """The availability bound, and it is the same shape `_and_conjuncts` was made iterative for.

    sqlglot builds `a + 1 + 1 + …` LEFT-DEEP, so the tree is as deep as the expression is wide and a
    recursive value walk costs one Python frame per term. Measured: 989 terms is 4,052 characters
    against `sql_guard._MAX_SQL_CHARS` of 50,000 and raised `RecursionError`, which
    `execute_sql._receipt_for` catches under bare `Exception` and turns into `RECEIPT_BUILD_FAILED`
    — the statement runs, returns rows, and the caller silently loses the trust layer. On
    `cmd_preflight` and `cmd_prepare`, which have no such catch, it propagated as a traceback.

    So the widest expression the CHARACTER cap admits is what this asserts, rather than some number
    chosen to be comfortably inside it. 12,476 terms is 50,000 characters exactly; anything wider is
    refused by `sql_guard` before this layer is asked, so there is no shape left for a depth bound
    to have an opinion about. The status is asserted too: an analysis that survived by answering
    `undetermined` would pass a test that only asked for no exception.
    """
    terms = 12_476
    sql = f"SELECT SUM(orders.total_amount{' + 1' * terms}) {FAN_JOIN}"
    assert len(sql) == 50_000, len(sql)

    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], [a.status for a in reports]
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


@pytest.mark.parametrize("label,sql", [
    ("a LATERAL VIEW", LATERAL_VIEW_CTE),
    ("an UNPIVOT", UNPIVOT_CTE),
    ("a CONNECT BY", CONNECT_BY_CTE),
])
def test_a_cte_body_that_multiplies_its_own_rows_is_not_the_table_it_reads(label, sql):
    """The grain-preserving guard read three `exp.Select` arguments and accepted every other one.

    None of these bodies has a JOIN, a GROUP BY or a DISTINCT in it, so all three passed the whole
    denylist and resolved as though they handed `orders` back row for row. They do not: a LATERAL
    VIEW emits one row per exploded element, an UNPIVOT one row per unpivoted column, and a CONNECT
    BY one row per path through the hierarchy. Each was measured `not_multiplied` here against an
    `undetermined` on base `439ecd1`, which makes all three false cleans this branch created.

    The resolver is asserted as well as the status, because they are separable: a body that resolved
    to `orders` and then failed to find a fan would report `not_multiplied` for a second reason, and
    the fix has to be that the body is unreadable rather than that the fan is absent.
    """
    tree = _parse(sql)
    bodies = rt._visible_cte_bodies(tree.find(exp.Select))
    assert set(bodies) == {"o"}, (label, bodies)
    assert rt._grain_preserving_source(
        "o", bodies, rt._model_table_index(_sales_org()), set()) is None, label

    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)


def test_the_grain_preserving_allowlist_is_the_arguments_that_keep_the_rows():
    """The allowlist itself, so that widening it is a deliberate act and not a side effect.

    `exp.Select` carries thirty-odd arguments and only five of them leave the source's row count
    alone: the projection, the FROM under both of sqlglot's spellings for it, the WHERE, which
    removes rows without changing what a row IS, and the ORDER BY, which changes their sequence.
    Every other one either collapses rows (`group`, `distinct`), multiplies them (`joins`,
    `laterals`, `connect`, `match`, `pivots`), or truncates them (`limit`, `offset`), and a guard
    that named three of those and accepted the rest is how the three shapes above got through.

    `order` is in and `limit` is out, which is the pair that says why the allowlist is arguments and
    not intuitions: ORDER BY alone is a sequence, ORDER BY with LIMIT is a truncation, and the two
    live under separate keys. Both are asserted through the analysis below, because leaving `order`
    out costs a LOST FINDING rather than an over-report — the fan is real and the resolver can see
    it, and a receipt going quiet about a trap it can prove is the failure this spec is against.

    Asserted as a set rather than as a sequence: the order it is written in decides nothing, and a
    tuple comparison would fail on a reordering that changes no answer.
    """
    assert set(rt._GRAIN_PRESERVING_SELECT_ARGS) == {
        "expressions", "from_", "from", "where", "order"}
    for hazard in ("group", "distinct", "joins", "laterals", "connect", "match", "pivots",
                   "limit", "offset", "qualify", "with_"):
        assert hazard not in rt._GRAIN_PRESERVING_SELECT_ARGS, hazard

    ordered = ("WITH oi AS (SELECT * FROM order_items ORDER BY id) "
               "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id")
    assert [(a.status, a.joins) for a in _reports(ordered)] == [(rt.MULTIPLIED, [FAN_EDGE])]
    truncated = ("WITH oi AS (SELECT * FROM order_items ORDER BY id LIMIT 10) "
                 "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id")
    assert [(a.status, a.joins) for a in _reports(truncated)] == [(rt.UNDETERMINED, [])]


@pytest.mark.parametrize("label,sql,expected_map", [
    ("a LATERAL VIEW", LATERAL_VIEW_SOURCE, {"orders": "orders", "t": ""}),
    ("an UNPIVOT", UNPIVOT_SOURCE, {"o": "orders", "": ""}),
    ("a CONNECT BY", CONNECT_BY_SOURCE, {"orders": "orders", "": ""}),
    ("a PIVOT", PIVOT_SOURCE, {"orders": "orders", "p": ""}),
])
def test_a_row_multiplying_source_beside_the_from_clause_binds_nothing(label, sql, expected_map):
    """`_reference_sites`' own docstring claims a denylist; these are what made the claim false.

    It says every FROM/JOIN source that is not an `exp.Table` binds, and that a source kind nobody
    anticipated has to default to `undetermined`. Four constructs are neither: `laterals`, `connect`
    and `match` are SIBLINGS of `from_` on `exp.Select` rather than children of it, and `pivots`
    rides the `exp.Table`, which the table arm has already bound under its real name. A walk over
    `exp.From` and `exp.Join` reaches none of the four, so the scope map looked like a single clean
    table and every aggregate in the statement read `not_multiplied`.

    The false clean is PRE-EXISTING — base `439ecd1` answers `not_multiplied` on the first three as
    well — and it lives on the ungated surface: `cmd_preflight` and `cmd_prepare` in
    `semantic_model/cli.py` call `pre_flight_check` with no gate battery in front of it. `PIVOT`
    escaped only by accident, because it happens to contain an `exp.AggFunc`, which is not a
    property of PIVOT so much as of the example anyone writes.

    Both halves are asserted. The map says the alias is in scope and resolves to no model table,
    which is the honest answer; the status says what the analysis does with that. A binding that
    dropped `orders` instead of adding the empty one would also give `undetermined`, and would have
    lost the join the model declares.
    """
    assert rt._alias_map(_parse(sql).find(exp.Select), in_scope_only=True) == expected_map, label

    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)


def test_a_match_recognize_binds_nothing_on_the_dialect_that_speaks_it():
    """The fourth `exp.Select` argument, asserted off a tree rather than through a receipt.

    `MATCH_RECOGNIZE` does not parse on sqlglot's default grammar and `_sales_org` declares no
    dialect, so `pre_flight_check` against it answers "could not be parsed" and reaches none of this
    layer. That is not a reason to leave the argument unbound: Snowflake, Oracle and Trino all speak
    it, a model declaring any of them resolves that grammar, and one match per partition out of many
    rows is a row count nothing else in the statement states.

    The parse is asserted first so the test cannot silently start measuring a statement that no
    longer contains a MATCH_RECOGNIZE at all.
    """
    tree = parse_one(MATCH_RECOGNIZE_SOURCE, read="snowflake")
    select = tree.find(exp.Select)
    assert select.args.get("match") is not None, select.args
    assert rt._alias_map(select, in_scope_only=True) == {"orders": "orders", "": ""}


def test_the_receipts_own_reference_roster_is_unchanged_by_the_row_multiplying_binding():
    """ACE-099's contract: the DEFAULT walk is what the receipt and the refusal path read.

    `bind_derived` is opt-in precisely because `_projected_sensitive` refuses on what it finds and
    `assemble_receipt`'s roster discloses it. A row-multiplying source is not a table reference and
    must not appear as one, whatever the scope map needs to know about it — a receipt listing an
    empty-named table would be a disclosure of a source that has no name to disclose.
    """
    for label, sql in [("a LATERAL VIEW", LATERAL_VIEW_SOURCE), ("an UNPIVOT", UNPIVOT_SOURCE),
                       ("a CONNECT BY", CONNECT_BY_SOURCE), ("a PIVOT", PIVOT_SOURCE)]:
        refs = rt._table_references(_parse(sql))
        assert [(r.bare, r.scope) for r in refs] == [("orders", "main")], (label, refs)


def test_a_lateral_that_a_join_already_bound_is_not_bound_twice():
    """One source, one binding, whichever of the two walks reaches it first.

    `LATERAL (SELECT 1) l` is an `exp.Lateral` that is also a JOIN's `this`, so it is now reachable
    both as the clause's bound source and as a member of the row-multiplying walk. Binding it twice
    would put one source in the reference list twice, and a list is what the receipt's roster and
    the arm-counting assertions in `test_ace043_set_operation_arms.py` read.

    The ORDER is sqlglot's own traversal order and is asserted as written rather than sorted, for
    the reason `_table_references` states: `_RECEIPT_MAX_REFS` truncates from the front of exactly
    this order, so a reordering is a change in which references a capped receipt lists.
    """
    sql = "SELECT SUM(orders.total_amount) FROM orders, LATERAL (SELECT 1) l"
    sites = rt._reference_sites(_parse(sql).find(exp.Select), bind_derived=True)
    assert [(s.ref.bare, s.ref.alias) for s in sites] == [("", "l"), ("orders", None)], sites


def test_the_cte_chain_bound_admits_exactly_the_number_it_is_written_as():
    """`depth > _MAX_CTE_CHAIN` admitted sixty-FIVE hops for a constant that reads sixty-four.

    `depth` is 0 on the first hop, so the comparison has to be `>=` for the bound to be the number
    beside it. Nothing depended on the extra hop; a bound nobody can read off its own constant is
    the defect, because the next person to reason about it will reason from the constant.

    Both sides of the boundary, because either alone is satisfiable by a bound that is off by one in
    the other direction.
    """
    org, tidx = _sales_org(), None

    def chain(links: int) -> str:
        bodies = ["c0 AS (SELECT * FROM order_items)"]
        bodies += [f"c{i} AS (SELECT * FROM c{i - 1})" for i in range(1, links)]
        return ("WITH " + ", ".join(bodies) +
                f" SELECT SUM(orders.total_amount) FROM orders JOIN c{links - 1} "
                f"ON c{links - 1}.order_id = orders.id")

    tidx = rt._model_table_index(org)
    assert rt._MAX_CTE_CHAIN == 64
    at_the_bound = _parse(chain(rt._MAX_CTE_CHAIN))
    assert rt._grain_preserving_source(
        "c63", rt._visible_cte_bodies(at_the_bound.find(exp.Select)), tidx, set()) == "order_items"
    past_it = _parse(chain(rt._MAX_CTE_CHAIN + 1))
    assert rt._grain_preserving_source(
        "c64", rt._visible_cte_bodies(past_it.find(exp.Select)), tidx, set()) is None

    assert [a.status for a in _reports(chain(rt._MAX_CTE_CHAIN))] == [rt.MULTIPLIED]
    assert [a.status for a in _reports(chain(rt._MAX_CTE_CHAIN + 1))] == [rt.UNDETERMINED]


def test_a_cte_that_renames_its_grain_column_is_not_a_fan():
    """The two names `_cte_edge` compares came from opposite sides of the CTE.

    `_group_by_grain` reads what the body groups BY, which are its INPUT columns, and the join key
    is what the outer statement writes, which is the body's OUTPUT column. `SELECT id AS order_id …
    GROUP BY id` makes those differ, so `order_id` was compared against a grain of `{id}`, found no
    cover, and a CTE that really is one row per join key reported `multiplied`. Over-reporting, so
    no receipt said anything false — and still a false positive on legitimate SQL, which is what a
    reader learns to discount reports over.

    The unresolvable direction is asserted beside it. `SELECT id + 1 AS k` gives `k` no input column
    to stand for, so the key is compared as written and the fan is reported: what does not resolve
    stays over-reporting rather than becoming a guess.
    """
    reports = _reports(CTE_RENAMING_ITS_GRAIN)
    assert [(a.aggregate, a.status, a.joins) for a in reports] == [
        ("SUM(orders.total_amount)", rt.NOT_MULTIPLIED, [])], reports

    body = _parse(CTE_RENAMING_ITS_GRAIN).find(exp.CTE).this
    assert rt._projection_sources(body) == {"order_id": "id"}

    computed = ("WITH x AS (SELECT id + 1 AS k FROM customers GROUP BY id) "
                "SELECT SUM(orders.total_amount) FROM orders JOIN x ON x.k = orders.id")
    assert rt._projection_sources(_parse(computed).find(exp.CTE).this) == {}
    assert [a.status for a in _reports(computed)] == [rt.MULTIPLIED], computed

    # Two projections under one output name are two answers to a question that has to have one, so
    # the name is dropped rather than resolved to whichever was written last. `SELECT *` names no
    # column to resolve at all, and an output name that stands for itself is left as it is.
    ambiguous = _parse("SELECT id AS k, customer_id AS k FROM customers GROUP BY id")
    assert rt._projection_sources(ambiguous.find(exp.Select)) == {}
    repeated = _parse("SELECT id AS k, id AS k FROM customers GROUP BY id")
    assert rt._projection_sources(repeated.find(exp.Select)) == {"k": "id"}
    star = _parse("SELECT * FROM customers GROUP BY id")
    assert rt._projection_sources(star.find(exp.Select)) == {}


def test_the_derived_edge_does_not_depend_on_which_source_the_statement_wrote_first():
    """A receipt has to read the same way twice for the same SQL, which this module says of itself.

    `_resolve_cte_scope` resolved the far side of a derived edge out of the map it was still
    mutating, so whichever alias the statement wrote FIRST was resolved against a map the other one
    had not reached yet. One statement, two spellings of its FROM clause, two answers: `multiplied`
    naming the derived edge one way and `undetermined` the other. Both are the safe direction and
    neither is determinism.

    Two passes fix it: every grain-preserving rebinding settles first, and every edge is then
    derived against that one settled map. Asserted on the ANSWER rather than on the pass structure,
    because the property is that the two spellings agree and not that any particular internal order
    was used to make them.
    """
    withs = ("WITH p AS (SELECT * FROM orders), "
             "g AS (SELECT order_id, SUM(quantity) q FROM order_items "
             "GROUP BY order_id, product_id) ")
    preserving_first = withs + "SELECT SUM(p.total_amount) FROM p JOIN g ON g.order_id = p.id"
    changing_first = withs + "SELECT SUM(p.total_amount) FROM g JOIN p ON g.order_id = p.id"

    answers = [[(a.aggregate, a.status, a.joins) for a in _reports(sql)]
               for sql in (preserving_first, changing_first)]
    assert answers[0] == answers[1], answers
    assert answers[0] == [("SUM(p.total_amount)", rt.MULTIPLIED, ["orders (1) <- g (N)"])], answers
