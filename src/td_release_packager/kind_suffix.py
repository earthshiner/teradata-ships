"""
kind_suffix.py — Kind-suffix mapping for SHIPS kind-aware tokenisation.

In a SHIPS topology each database name decomposes into a layer plus a
*kind* suffix that indicates what type of objects it holds:

  _T  — tables, indexes, triggers, DML targets
  _V  — views
  _M  — macros (site-configurable; default _T in current implementation)
  _P  — procedures (site-configurable; default _T)
  _F  — functions (site-configurable; default _T)

The mappings here are the canonical defaults.  Sites that deviate from
the defaults can override via token_map.conf structured sections
(Phase 3 opt-in — not yet implemented).

External references (objects not in the harvested package) default to
``_V``: in a layered SHIPS architecture the public access surface is the
view layer, so downstream consumers are more likely referencing views
than tables.  DML targets are an exception — they are always tables —
but DML target resolution uses filename context, not this path.

System schema references (DBC, SYSLIB, etc.) must be left untouched
entirely — they are not subject to the SHIPS token grammar.
"""

from __future__ import annotations

from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Kind suffix per *base* object type
# ---------------------------------------------------------------------------
# Procedures, macros, functions: physically colocate with tables in Teradata
# in practice, so _T is the sensible default.  Script Table Operators (STO)
# produce/consume tables → _T.  JARs follow the consuming procedure → _T
# (best-effort; Phase 3 will introduce consumer-chain lookup).
TYPE_TO_KIND: Dict[str, str] = {
    "TABLE": "T",
    "VIEW": "V",
    "MACRO": "T",
    "PROCEDURE": "T",
    "PROCEDURE_SPL": "T",
    "PROCEDURE_JAVA": "T",
    "FUNCTION": "T",
    "FUNCTION_C": "T",
    "FUNCTION_SQL": "T",
    "TRIGGER": "T",
    "JOIN_INDEX": "T",
    "HASH_INDEX": "T",
    "SECONDARY_INDEX": "T",
    "STO": "T",
    "JAR": "T",
    "DML": "T",
}

# ---------------------------------------------------------------------------
# Kind suffix per file extension (owner-clause fast path)
# ---------------------------------------------------------------------------
# When the base type is determined from the file extension alone (e.g. during
# a pre-scan before full classification), this table gives the same result
# without needing the full classifier.
EXTENSION_TO_KIND: Dict[str, str] = {
    ".tbl": "T",
    ".viw": "V",
    ".mcr": "T",
    ".spl": "T",
    ".fnc": "T",
    ".trg": "T",
    ".jix": "T",
    ".idx": "T",
    ".sto": "T",
    ".sjr": "T",
    ".dml": "T",
    ".ins": "T",
}

# ---------------------------------------------------------------------------
# External-reference default
# ---------------------------------------------------------------------------
# When a cross-reference cannot be resolved from the package object index
# (the referenced object lives outside the harvested set), default to _V.
# Downstream consumers in a SHIPS topology query the view layer, so an
# unresolvable external reference is more likely a view than a table.
# DML targets are resolved from filename context and don't reach this path.
EXTERNAL_KIND_DEFAULT: str = "V"

# ---------------------------------------------------------------------------
# Body-scan skip set
# ---------------------------------------------------------------------------
# Teradata scalar and aggregate functions are pure computation — they cannot
# SELECT FROM tables or views, cannot DML, cannot CALL procedures.  Skipping
# body scanning for these types avoids false positive cross-reference matches
# inside dynamic-SQL strings that happen to contain table names.
# Script Table Operators (STO) are different: they DO produce/consume tables.
BODY_SCAN_SKIP: frozenset = frozenset(
    {
        "FUNCTION",
        "FUNCTION_C",
        "FUNCTION_SQL",
    }
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def kind_for_type(obj_type: str) -> Optional[str]:
    """Return the kind suffix letter for an object type, or None.

    Args:
        obj_type: Object type string (e.g. 'TABLE', 'VIEW', 'PROCEDURE_SPL').

    Returns:
        Kind suffix letter ('T', 'V', ...) or None if not applicable.
    """
    return TYPE_TO_KIND.get(obj_type.upper() if obj_type else "")


def kind_for_extension(ext: str) -> Optional[str]:
    """Return the kind suffix letter for a file extension, or None.

    Args:
        ext: File extension including the leading dot (e.g. '.tbl', '.viw').

    Returns:
        Kind suffix letter or None if the extension is not in the table.
    """
    return EXTENSION_TO_KIND.get(ext.lower() if ext else "")


# ---------------------------------------------------------------------------
# System database exclusion
# ---------------------------------------------------------------------------
# References to system schemas must be excluded entirely from kind-suffix
# rewriting — they are system-supplied, fixed names, not subject to the
# SHIPS grammar.  Union of the lists in analyser._SYSTEM_DATABASES and
# ingest._build_token_candidates so a single constant covers all call sites.
SYSTEM_DATABASES: frozenset = frozenset(
    {
        "DBC",
        "SYSLIB",
        "SYSUDTLIB",
        "SYSUIF",
        "TD_SYSFNLIB",
        "TD_SYSXML",
        "SQLJ",
        "SYSSPATIAL",
        "DBCMNGR",
        "SYSJDBC",
        "SYSBAR",
        "SYSTEMFE",
        "TDSTATS",
        "TDWM",
        "TD_SYSGPL",
        "ALL",
        "DEFAULT",
        "PUBLIC",
        "EXTUSER",
    }
)

# ---------------------------------------------------------------------------
# Already-kind-encoded detection
# ---------------------------------------------------------------------------
# When a literal DB name or a base token name already ends with a recognised
# kind suffix (e.g. ``MortgagePlatform_Domain_V``, ``SEM_DATABASE_V``), the
# kind is already encoded and the kind-aware substitution must not add another
# suffix.  The check is case-insensitive and anchored to ``_<letter>`` at the
# end of the name.
_KIND_TERMINAL = frozenset({"T", "V", "M", "P", "F", "X", "J", "R"})


def has_kind_suffix(name: str) -> bool:
    """Return True if *name* already ends with a SHIPS kind suffix (``_T``, ``_V``, etc.).

    Used to detect:
    - Literal DB names already kind-encoded in source
      (e.g. ``MortgagePlatform_Domain_V``).
    - Base token names already kind-encoded in the token map
      (e.g. ``SEM_DATABASE_V`` from ``{{SEM_DATABASE_V}}``).

    When True, the kind-aware substitution falls back to plain
    word-boundary replacement so the existing kind encoding is preserved.

    Args:
        name: Identifier to inspect (literal DB name or base token name).

    Returns:
        True if the name's last segment is a single recognised kind letter.
    """
    parts = name.rsplit("_", 1)
    return len(parts) == 2 and parts[1].upper() in _KIND_TERMINAL


def is_body_scan_skip(obj_type: str) -> bool:
    """Return True if cross-reference body scanning should be skipped.

    Teradata function bodies are pure computation with no SQL references.
    Scanning them produces false positives from dynamic-SQL string content.

    Args:
        obj_type: Object type string.

    Returns:
        True if this type's body should be excluded from reference scanning.
    """
    return (obj_type.upper() if obj_type else "") in BODY_SCAN_SKIP
