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
