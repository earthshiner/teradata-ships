"""Shared DDL-kind detection primitive for the fixer package (#525).

Three of the current fixers — ``extension``, ``object_placement``, and
``type_suffix`` — need the same input: given a payload file, what
Teradata object kind is it (TABLE / VIEW / MACRO / PROCEDURE / FUNCTION /
STO / TRIGGER)? Extracting the detection into one module keeps parser
edge cases in one place with focused tests.

The primitive is deliberately **fast path first, content check second**:

1. Look up the file extension in
   :data:`td_release_packager.kind_suffix.EXTENSION_TO_KIND`. Payload
   files whose extension matches the harvest convention (``.tbl``,
   ``.viw``, ``.mcr``, ``.spl``, ``.fnc``, ``.trg``, ``.sto``, etc.)
   resolve here without opening the file.
2. Fall back to a lightweight ``CREATE …`` pattern scan on the file
   contents. Only fires when the extension is missing or unknown —
   e.g. a hand-written ``.sql`` file that harvest hasn't renamed yet.

Returns :class:`DdlKind.UNKNOWN` when neither path resolves. Callers
should treat that as "leave this file alone" — the fixers never make
changes to files they cannot confidently classify.
"""

from __future__ import annotations

import enum
import re
from typing import Optional


class DdlKind(enum.Enum):
    """The Teradata object kinds the fixers care about.

    Enum values are the kind name (not the token suffix letter) so
    ``TABLE`` and ``TRIGGER`` don't collide — they'd both suffix ``_T``
    per SHIPS convention, but they must remain distinct members here.
    Callers that need the suffix letter use :attr:`suffix` (round-trips
    through :data:`_KIND_TO_SUFFIX`).
    """

    TABLE = "TABLE"
    VIEW = "VIEW"
    MACRO = "MACRO"
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    STO = "STO"
    TRIGGER = "TRIGGER"
    UNKNOWN = "UNKNOWN"

    @property
    def suffix(self) -> str:
        """Kind-suffix letter used in tokens (``T``, ``V``, ...).

        Mirrors :data:`td_release_packager.kind_suffix.TYPE_TO_KIND`'s
        single-letter suffix; both TABLE and TRIGGER return ``T``
        because harvest colocates triggers with the tables they fire
        on. ``UNKNOWN`` returns an empty string — callers that need a
        stable placeholder should special-case it.
        """
        return _KIND_TO_SUFFIX[self]


_KIND_TO_SUFFIX: dict["DdlKind", str] = {
    DdlKind.TABLE: "T",
    DdlKind.VIEW: "V",
    DdlKind.MACRO: "M",
    DdlKind.PROCEDURE: "P",
    DdlKind.FUNCTION: "F",
    DdlKind.STO: "X",
    DdlKind.TRIGGER: "T",
    DdlKind.UNKNOWN: "",
}


# Extension → DdlKind lookup. Kept in sync with
# ``kind_suffix.EXTENSION_TO_KIND``.
_EXT_TO_KIND: dict[str, DdlKind] = {
    ".tbl": DdlKind.TABLE,
    ".viw": DdlKind.VIEW,
    ".mcr": DdlKind.MACRO,
    ".spl": DdlKind.PROCEDURE,
    ".fnc": DdlKind.FUNCTION,
    ".trg": DdlKind.TRIGGER,
    ".sto": DdlKind.STO,
}

# Content-based fallback. Matched against comment-stripped SQL when the
# extension is unknown; keeps the pattern set small on purpose — this
# is a fallback, the primary path is the extension lookup above.
_CREATE_PATTERNS: list[tuple[re.Pattern, DdlKind]] = [
    (
        re.compile(
            r"\bCREATE\s+(?:MULTISET\s+|SET\s+)?(?:VOLATILE\s+)?TABLE\b", re.IGNORECASE
        ),
        DdlKind.TABLE,
    ),
    (re.compile(r"\b(?:CREATE|REPLACE)\s+VIEW\b", re.IGNORECASE), DdlKind.VIEW),
    (re.compile(r"\bCREATE\s+MACRO\b", re.IGNORECASE), DdlKind.MACRO),
    (
        re.compile(r"\b(?:CREATE|REPLACE)\s+PROCEDURE\b", re.IGNORECASE),
        DdlKind.PROCEDURE,
    ),
    (re.compile(r"\b(?:CREATE|REPLACE)\s+FUNCTION\b", re.IGNORECASE), DdlKind.FUNCTION),
    (re.compile(r"\bCREATE\s+TRIGGER\b", re.IGNORECASE), DdlKind.TRIGGER),
]


def detect_ddl_kind(path: str, content: Optional[str] = None) -> DdlKind:
    """Classify a payload file by DDL kind.

    Args:
        path:    Filesystem path to the file. The extension drives the
                 fast path; the rest of the path is not consulted.
        content: Optional file contents. When ``None``, only the
                 extension is considered — cheaper for callers that
                 know the extension is authoritative and don't want
                 the fallback SQL scan.

    Returns:
        The detected :class:`DdlKind`. :attr:`DdlKind.UNKNOWN` when
        neither the extension nor the content produced a confident
        classification.
    """
    if not path:
        return DdlKind.UNKNOWN

    ext_kind = _EXT_TO_KIND.get(_extension(path))
    if ext_kind is not None:
        return ext_kind

    if content is None:
        return DdlKind.UNKNOWN

    for pattern, kind in _CREATE_PATTERNS:
        if pattern.search(content):
            return kind

    return DdlKind.UNKNOWN


def _extension(path: str) -> str:
    """Return the lower-cased ``.ext`` suffix of ``path`` (including the dot)."""
    dot_index = path.rfind(".")
    if dot_index < 0:
        return ""
    return path[dot_index:].lower()
