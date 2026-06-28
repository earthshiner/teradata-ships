"""
token_ref.py — canonical token-reference regex vocabulary (issue #383).

The shape of a *tokenised database/object reference* — a ``{{TOKEN}}``, a
literal name, a prefix-token + literal suffix (``{{DB_PREFIX}}_DOM_STD_T``), or
a literal + token suffix — was independently encoded in ``eponymous_rename`` (DDL
header name extraction) and ``infer_grants`` (cross-database reference matching).
This module is the single source of truth for those building blocks so the two
consumers can compose their context-specific patterns from one vocabulary
instead of re-deriving the grammar (and drifting).

Building blocks only — each consumer composes its own anchored regex, because
the *contexts* differ (a statement-anchored ``CREATE`` header vs a mid-line
reference). See ``docs/references/tokenisation.md``.
"""

from __future__ import annotations

#: A single ``{{TOKEN}}`` atom. Token names start with a letter or underscore
#: and continue with word characters. Consumers compile with re.IGNORECASE.
TOKEN_ATOM = r"\{\{[A-Za-z_]\w*\}\}"

#: A double-quoted identifier: ``"My Object"``.
QUOTED_IDENT = r'"[^"]+"'

#: A bare SQL identifier: ``CallCentre`` / ``DOM_STD_T``.
BARE_IDENT = r"[A-Za-z_]\w*"

#: An object name as it appears after a ``.`` — quoted or bare.
OBJECT_NAME = rf"(?:{QUOTED_IDENT}|{BARE_IDENT})"

#: One name *segment* (a database or object name): a token, quoted, or bare
#: first atom, then any mix of further tokens / word runs so a prefix-token
#: reference like ``{{DB_PREFIX}}_DOM_STD_T`` is captured whole rather than
#: truncated at the closing ``}}`` (issue #309).
NAME_SEGMENT = rf"(?:(?:{TOKEN_ATOM}|{QUOTED_IDENT}|{BARE_IDENT})(?:{TOKEN_ATOM}|\w+)*)"

#: A database reference that STARTS with a token (whole-name token or
#: prefix-token + suffix). Used where only tokenised references are wanted.
DB_TOKEN_PART = rf"{TOKEN_ATOM}(?:\w+|{TOKEN_ATOM})*"

#: A database reference that STARTS with a literal name (letter, then 1–127
#: word chars) and may carry token / word continuations. The length bound and
#: letter-start match ``infer_grants``' original literal matcher exactly so
#: grant inference behaviour is preserved.
DB_LITERAL_PART = rf"[A-Za-z][A-Za-z0-9_]{{1,127}}(?:{TOKEN_ATOM}|\w+)*"
