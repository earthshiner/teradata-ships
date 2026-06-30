"""
sql_text.py — Shared SQL-text utilities.

Currently houses the position-preserving comment stripper that several
modules need: ``validate``, ``ingest``, ``builder``. Without comment
stripping, regex content scans match keywords inside ``/* ... */``
header comments and trigger spurious classifications, false-positive
warnings, or worse — DDL injection into comment text rather than into
the actual statement.

Position-preserving means each comment character is replaced with a
single space (newlines kept intact). The output string has the SAME
length as the input, with all non-comment characters at the SAME
offsets. This lets a regex match position in the cleaned content
also point at the same span in the original — useful for surgical
substitutions that must preserve surrounding comments.
"""

from __future__ import annotations

import re


# -- Compiled patterns. Block comments are non-greedy and span lines.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# Teradata SQL string literals: single-quoted, with doubled '' as
# the embedded-quote escape. Spans lines (rare but valid).
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'", re.DOTALL)


def _blank_preserving_newlines(match: "re.Match") -> str:
    """Replace every char in the match with a space, but keep
    newlines so line numbers in error messages stay accurate."""
    return "".join(" " if c != "\n" else "\n" for c in match.group(0))


def strip_comments_preserving_positions(content: str) -> str:
    """
    Replace SQL block and line comments with same-length whitespace.

    Block comments (``/* ... */``) and line comments (``-- ...``)
    are blanked character-by-character. Newlines inside block
    comments are preserved.

    Block comments are processed first so that any ``--`` inside
    a ``/* */`` is already blanked when the line-comment pass runs.

    Args:
        content: Raw SQL/DDL text.

    Returns:
        Same-length string with comment characters replaced by
        spaces. Non-comment characters are at the same offsets as
        in the input.
    """
    no_block = _BLOCK_COMMENT_RE.sub(_blank_preserving_newlines, content)
    return _LINE_COMMENT_RE.sub(_blank_preserving_newlines, no_block)


def strip_string_literals_preserving_positions(content: str) -> str:
    """
    Replace SQL single-quoted string literals with same-length
    whitespace.

    Why this matters: stored procedures often build dynamic SQL by
    concatenating string literals like ``'CREATE MULTISET TABLE '``.
    Without stripping, regex content scans match those keywords as
    if they were real DDL — misclassifying procedures as tables,
    triggering spurious MULTISET-injection / extension warnings,
    and so on.

    Teradata's literal syntax is single-quoted with doubled-quote
    escape (``'it''s a test'``). The regex handles both. Newlines
    inside multi-line literals are preserved so line numbers stay
    accurate.

    Args:
        content: SQL/DDL text. Pass content with comments already
                 stripped — comments containing single quotes
                 would otherwise be misread as literals.

    Returns:
        Same-length string with string-literal *content* replaced
        by spaces. The opening and closing quote characters are
        also replaced so a downstream regex can't accidentally
        match across them.
    """
    return _STRING_LITERAL_RE.sub(_blank_preserving_newlines, content)


def strip_comments_and_string_literals(content: str) -> str:
    """
    Combined stripper — single-pass state machine over the source.

    A naive sequential approach (comments first, then strings, or
    vice versa) is broken either way:

    * Comments-first eats ``--`` markers found INSIDE string literals
      and truncates everything from there to end-of-line, which can
      leave the literal unterminated and cause the string-literal
      pass to swallow code from later statements. (#499)
    * Strings-first misreads ``'`` characters inside ``/* ... */`` /
      ``-- ...`` comments as the start of a literal.

    A single-pass lexer tracking the current state (``code`` /
    ``string`` / ``line_comment`` / ``block_comment``) is the only
    correct treatment: a ``--`` inside a string is data, a ``'`` inside
    a comment is text. Output positions and line numbers are preserved
    because every blanked character is replaced by a space (newlines
    excepted) at its original offset.

    Use this in any rule check or content classifier that wants to
    reason about REAL DDL only, ignoring documentation comments AND
    dynamic-SQL strings inside procedure bodies.
    """
    out: list[str] = []
    n = len(content)
    i = 0
    state = "code"  # one of: code, string, line_comment, block_comment
    while i < n:
        c = content[i]
        nxt = content[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "'":
                state = "string"
                out.append(" ")
            elif c == "-" and nxt == "-":
                state = "line_comment"
                out.append(" ")
                out.append(" ")
                i += 2
                continue
            elif c == "/" and nxt == "*":
                state = "block_comment"
                out.append(" ")
                out.append(" ")
                i += 2
                continue
            else:
                out.append(c)
        elif state == "string":
            if c == "'":
                if nxt == "'":
                    # Doubled-quote escape — still inside the literal.
                    out.append(" ")
                    out.append(" ")
                    i += 2
                    continue
                state = "code"
                out.append(" ")
            elif c == "\n":
                out.append("\n")
            else:
                out.append(" ")
        elif state == "line_comment":
            if c == "\n":
                state = "code"
                out.append("\n")
            else:
                out.append(" ")
        else:  # block_comment
            if c == "*" and nxt == "/":
                state = "code"
                out.append(" ")
                out.append(" ")
                i += 2
                continue
            if c == "\n":
                out.append("\n")
            else:
                out.append(" ")
        i += 1
    return "".join(out)
