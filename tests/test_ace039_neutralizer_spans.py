"""`_neutralize` reports where the quoted identifiers went, in one coordinate frame.

The recon gate's niladic matcher needs to tell `SELECT "current_user" FROM audit_log` (a
column that happens to share a keyword's name) from `SELECT current_user` (the keyword
itself). The neutralizer drops quote delimiters on purpose — that unwrapping is what keeps
`SELECT*FROM"pg_read_file"('/etc/passwd')` visible to a `\\b`-anchored pattern — so the
information has to be carried alongside the text rather than left in it.

Two properties are load-bearing and neither fails loudly on its own:

1. **A span names its identifier's content, in the returned text's coordinates.** The scan
   re-supplies separator spaces and drops delimiters, so input offsets are not output
   offsets, and the strip would shift them again. A span in the wrong frame silently
   mis-identifies a token.
2. **The unwrapping is unchanged for every other consumer.** Reversing it, or applying the
   quoted-is-an-identifier rule to a call matcher, reopens the read-only-guard bypass that
   `c42f96c` closed against a verified PostgreSQL 16 file read. The welded corpus below is
   that regression guard, and it lives here — beside the change — rather than with the gate
   that will eventually consume the spans.
"""

from __future__ import annotations

import pytest
from guardrail import RULE_READ_ONLY
from sql_guard import _neutralize, check_read_only


def _identifiers(sql: str) -> list[str]:
    """The text each reported span actually indexes — the property, stated directly."""
    neutral = _neutralize(sql)
    return [neutral.text[start:end] for start, end in neutral.quoted]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('SELECT t."current_user" FROM t', ["current_user"]),
        ('SELECT * FROM"pg_class"', ["pg_class"]),
        ('SELECT "x"INTO evil FROM t', ["x"]),
        # Two adjacent spans inside one qualified name: the separator is NOT re-supplied
        # between them, so a naive "span plus one" would swallow the dot.
        ('SELECT "schema"."table" FROM t', ["schema", "table"]),
        # The doubled-quote escape is content, not a delimiter — the span must cover the
        # collapsed form (`a"b`), which is three characters, not the five that were written.
        ('SELECT "a""b" FROM t', ['a"b']),
        ('SELECT "current_user", "version" FROM audit_log', ["current_user", "version"]),
    ],
)
def test_every_span_indexes_the_identifier_it_came_from(sql: str, expected: list[str]) -> None:
    assert _identifiers(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        '   SELECT "current_user" FROM audit_log',
        '/* lead */ SELECT "current_user" FROM audit_log',
        'SELECT "current_user" FROM audit_log ;   ',
        '\n\t SELECT "current_user" FROM audit_log \n',
        '  /* both */ SELECT "current_user" FROM audit_log ;  ',
    ],
)
def test_the_spans_survive_the_strip(sql: str) -> None:
    """The strip happens inside `_neutralize`, so spans are reported against its result.

    Leading whitespace and a leading comment both shift every offset left; asserting the
    slice rather than a literal index is what makes this independent of by how much.
    """
    assert _identifiers(sql) == ["current_user"]


def test_an_unquoted_statement_reports_no_spans() -> None:
    assert _neutralize("SELECT current_user FROM t").quoted == ()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'quoted \"current_user\" text' FROM t",
        "SELECT id FROM t /* a \"current_user\" mention */",
        "SELECT id FROM t -- trailing \"current_user\"\n",
    ],
)
def test_a_literal_or_comment_contributes_no_span(sql: str) -> None:
    """Literals and comments blank to a space, so there is no identifier to point at.

    This matters in the safe direction: if a blanked region produced a span, the niladic
    matcher would skip a region of text that no longer exists, which is how an off-by-one
    becomes an under-refusal.
    """
    assert _neutralize(sql).quoted == ()


def test_the_text_is_already_stripped() -> None:
    """One frame, which is the whole point — the caller no longer strips."""
    neutral = _neutralize('   SELECT "x" FROM t   ')
    assert neutral.text == neutral.text.strip()
    assert neutral.text.startswith("SELECT")


# The forms `c42f96c` closed. A delimited identifier is self-delimiting, so these are valid
# statements with no whitespace before the quote; dropping the delimiters without
# re-supplying a boundary fused two tokens into one and destroyed the `\b` anchor, and the
# gate stopped SEEING the function rather than allowing it.
REJECT_WELDED_AFTER_SPAN_TRACKING = [
    "SELECT*FROM\"pg_read_file\"('/etc/passwd')",
    'SELECT*FROM"pg_ls_dir"(\'/\')',
    'SELECT 1 AS"a"INTO evil',
    'SELECT "x"INTO evil FROM t',
    'SELECT*FROM"dblink"(\'\',\'\')',
]


@pytest.mark.parametrize("sql", REJECT_WELDED_AFTER_SPAN_TRACKING)
def test_the_welded_forms_are_still_refused(sql: str) -> None:
    """The regression guard for the whole slice.

    Span tracking must not change what the read-only gate matches on. It reads `.text`,
    with the quotes dropped exactly as before, and never reads `.quoted`.
    """
    refusal = check_read_only(sql)
    assert refusal is not None, f"welded form no longer refused: {sql!r}"
    assert refusal.rule == RULE_READ_ONLY


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT t."current_user" FROM t',
        'SELECT "schema"."table" FROM "schema"."table"',
        'SELECT c."email" FROM customers c',
    ],
)
def test_legitimate_quoted_identifiers_are_still_allowed(sql: str) -> None:
    """The other direction of the same change: reporting a span refuses nothing by itself."""
    assert check_read_only(sql) is None
