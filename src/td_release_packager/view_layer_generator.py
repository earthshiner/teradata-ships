"""
view_layer_generator.py — Object Placement Standard view layer generator.

This module is the **engine** for view-layer generation. It exposes
all of its work through importable functions so that the future
Generate stage of the orchestrator (build-order item 7) can drive
it directly, without invoking a subprocess or relying on a
``sys.path`` hack to reach a script under ``tools/``.

It also exposes ``main()`` so the standalone CLI shim at
``tools/generate_view_layer.py`` (and ``python -m
td_release_packager.view_layer_generator``) can keep working with
no behaviour change for users running ad-hoc.

For each ``.tbl`` file found in a SHIPS module, this engine produces:

    1. A 1:1 locking view in the matching ``{{<MOD>_DATABASE_V}}``
       database. The view declares an explicit column list in BOTH
       the header (interface contract) and the SELECT, uses
       ``LOCKING ROW FOR ACCESS``, and has the same object name as
       the source table.
    2. Rewrites of existing business views in the module:
         (a) ``SELECT *`` and ``alias.*`` forms are expanded into
             explicit, alias-qualified column lists by parsing the
             FROM and JOIN clauses and looking up source columns.
         (b) Direct ``{{<MOD>_DATABASE_T}}`` references are
             redirected to ``{{<MOD>_DATABASE_V}}`` so all reads
             go through the locking view layer.
    3. A ``CREATE DATABASE`` file for each views database that does
       not already exist in ``payload/database/pre-requisites/databases``.
    4. Consolidated ``.grt`` files (one per grantee views database)
       containing:
         (a) ``GRANT SELECT ON {{<MOD>_DATABASE_T}} TO {{<MOD>_DATABASE_V}}
             WITH GRANT OPTION;``  -- same-module read path
         (b) ``GRANT SELECT ON {{<OTHER>_DATABASE_V}} TO
             {{<MOD>_DATABASE_V}} WITH GRANT OPTION;``  -- cross-module
             reads detected in business views.

Filename conventions assumed:
    payload/database/DDL/tables/{{<MOD>_DATABASE_T}}.<TableName>.tbl
    payload/database/DDL/views/ {{<MOD>_DATABASE_V}}.<ViewName>.viw
    payload/database/pre-requisites/databases/{{<MOD>_DATABASE_V}}.db
    payload/database/DCL/inter_db/{{<MOD>_DATABASE_V}}.grt

The tool is idempotent: re-running produces no diff on already-correct
files. Use ``--dry-run`` to preview changes without writing.

Usage — explicit (standalone CLI):

    # Always works from a clone, no install required:
    python tools/generate_view_layer.py \\
        --project ./MortgagePlatform \\
        --modules DOM,SEM,MEM,OBS,STG

    # Equivalent once the package is installed (pip install -e . or uv sync):
    python -m td_release_packager.view_layer_generator \\
        --project ./MortgagePlatform \\
        --modules ALL --dry-run

Usage — orchestrated (as a library):

    from td_release_packager.view_layer_generator import (
        discover_modules,
        discover_tables,
        generate_locking_view_ddl,
        # ... etc
    )

Author: Paul Dancer / Ecosystem Architect - Teradata Field Technology Group
"""

# Standard library only — keeps this tool dependency-free.
import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

from td_release_packager.version_args import add_version_argument

logger = logging.getLogger("generate_view_layer")


# ---------------------------------------------------------------------------
# Constants — token patterns, paths, indentation
# ---------------------------------------------------------------------------

# Module token pattern:  {{<MODULE>_DATABASE_T}}  or  {{<MODULE>_DATABASE_V}}
# Captures: (module_name, suffix)  where suffix is 'T' or 'V'.
_MODULE_TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*?)_DATABASE_([TV])\}\}")

# Generic token reference (any {{XXX_DATABASE_[TV]}}).
_TOKEN_REF_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*?_DATABASE_[TV]\}\}")

# SQL comments — used for masking before structural parsing.
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Project sub-paths, relative to the SHIPS project root.
_PATH_TABLES = Path("payload") / "database" / "DDL" / "tables"
_PATH_VIEWS = Path("payload") / "database" / "DDL" / "views"
_PATH_DATABASES = Path("payload") / "database" / "pre-requisites" / "databases"
_PATH_GRANTS = Path("payload") / "database" / "DCL" / "inter_db"
_PATH_COMMENTS = Path("payload") / "database" / "DDL" / "comments"

# Comment-extraction patterns for .cmt files.
# COMMENT ON TABLE db.obj IS '...'
_CMT_TABLE_RE = re.compile(
    r"COMMENT\s+ON\s+TABLE\s+\S+\s+IS\s+'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)
# COMMENT ON COLUMN db.obj.col IS '...'
_CMT_COLUMN_RE = re.compile(
    r"COMMENT\s+ON\s+COLUMN\s+\S+\.(\w+)\s+IS\s+'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)

# Fixed suffix appended to the view-level comment to document the
# dirty-read guarantee of 1:1 locking views.
_LOCKING_VIEW_COMMENT_SUFFIX = (
    " — 1:1 locking view (LOCKING ROW FOR ACCESS) over {tables_token}.{object_name}."
)

# File extensions handled by SHIPS.
_EXT_TABLE = ".tbl"
_EXT_VIEW = ".viw"
_EXT_DB = ".db"
_EXT_GRANT = ".grt"
_EXT_COMMENT = ".cmt"

# Locking view marker — recognised by the validate.py rule.
_LOCKING_VIEW_MARKER = "-- LOCKING VIEW"

# Indentation: 4 spaces, leading-comma convention (no TABs).
_INDENT = "    "

# SQL keywords that must NOT be misread as table aliases when parsing
# a FROM/JOIN clause. Anything matched here is treated as a missing alias.
_ALIAS_STOPLIST = frozenset(
    {
        "ON",
        "WHERE",
        "GROUP",
        "ORDER",
        "HAVING",
        "QUALIFY",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "JOIN",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "MINUS",
        "AS",
        "AND",
        "OR",
        "NOT",
        "IS",
        "NULL",
        "BETWEEN",
        "IN",
        "EXISTS",
        "CASE",
        "WHEN",
        "SAMPLE",
        "WITH",
        "LOCKING",
        "FOR",
        "ACCESS",
        "READ",
        "WRITE",
        "EXCLUSIVE",
        "ROW",
        "TABLE",
        "LIMIT",
        "OFFSET",
        "FETCH",
        "FIRST",
        "NEXT",
        "ROWS",
        "ONLY",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TableSpec:
    """
    Parsed CREATE TABLE definition.

    Attributes:
        file_path:        Absolute path of the .tbl file.
        database_token:   Tables-side token, e.g. "{{DOM_DATABASE_T}}".
        module:           Module abbreviation, e.g. "DOM".
        object_name:      Table name (no database qualifier).
        columns:          Ordered column names.
        table_comment:    Text of COMMENT ON TABLE (without quotes), or
                          None when no comment exists in the sibling
                          .cmt file.
        column_comments:  Mapping of column_name → comment text for
                          columns that have a COMMENT ON COLUMN entry.
                          Columns without a comment are absent from the
                          dict; no spurious empty comments are emitted.
    """

    file_path: Path
    database_token: str
    module: str
    object_name: str
    columns: List[str]
    table_comment: Optional[str] = None
    column_comments: Dict[str, str] = field(default_factory=dict)


@dataclass
class ViewSpec:
    """
    Parsed business view (NOT a 1:1 locking view).

    A locking view is exempt from rewrite and is detected by the
    ``-- LOCKING VIEW`` marker in the file header.
    """

    file_path: Path
    database_token: str
    module: str
    object_name: str
    raw_content: str
    is_locking_view: bool


@dataclass
class FromClauseRef:
    """A single table reference in a FROM or JOIN clause."""

    database_token: str  # e.g. "{{DOM_DATABASE_T}}" or "{{DOM_DATABASE_V}}"
    object_name: str
    alias: str  # Empty string if the source had no alias.


@dataclass
class GenerationResult:
    """
    Aggregate counts and diagnostics from a generator run.

    All counters are incremented during execution. ``warnings`` and
    ``errors`` collect human-readable messages for the run summary.
    """

    locking_views_written: int = 0
    locking_views_unchanged: int = 0
    business_views_rewritten: int = 0
    business_views_unchanged: int = 0
    databases_written: int = 0
    databases_unchanged: int = 0
    grants_written: int = 0
    grants_unchanged: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ===========================================================================
# Section 1 — Token & filename helpers
# ===========================================================================


def parse_module_token(token: str) -> Optional[Tuple[str, str]]:
    """
    Parse a ``{{<MODULE>_DATABASE_<T|V>}}`` token.

    Args:
        token: A token literal including the surrounding braces.

    Returns:
        ``(module, suffix)`` where suffix is "T" or "V", or None if
        the token does not match the expected pattern.
    """
    match = _MODULE_TOKEN_RE.fullmatch(token)
    if not match:
        return None
    return match.group(1), match.group(2)


def companion_token(token: str) -> Optional[str]:
    """
    Return the companion token (T <-> V) for a module-database token.

    Example:
        ``{{DOM_DATABASE_T}}`` -> ``{{DOM_DATABASE_V}}``
    """
    parsed = parse_module_token(token)
    if parsed is None:
        return None
    module, suffix = parsed
    other = "V" if suffix == "T" else "T"
    return f"{{{{{module}_DATABASE_{other}}}}}"


def split_object_filename(filename: str) -> Optional[Tuple[str, str, str]]:
    """
    Split a SHIPS object filename into ``(token, object_name, ext)``.

    SHIPS files are named ``{{<TOKEN>}}.<ObjectName>.<ext>``.

    Args:
        filename: The base filename, no directory.

    Returns:
        Tuple of ``(token, object_name, ext)`` on success, or None
        if the filename does not parse.
    """
    name, ext = os.path.splitext(filename)
    if not ext:
        return None

    # The file can have multi-part object names (e.g. "Foo.Bar"),
    # but the token is always the first ".}}"-terminated chunk.
    if not name.startswith("{{"):
        return None
    end = name.find("}}")
    if end < 0:
        return None
    token = name[: end + 2]
    rest = name[end + 2 :]
    if not rest.startswith("."):
        return None
    object_name = rest[1:]
    if not object_name:
        return None
    return token, object_name, ext


# ===========================================================================
# Section 2 — Column extraction from CREATE TABLE
# ===========================================================================


def _strip_sql_comments(text: str) -> str:
    """Return text with ``--`` and ``/* */`` comments removed."""
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def _split_top_level(text: str, sep: str) -> List[str]:
    """
    Split *text* on *sep* but only at parenthesis depth zero,
    respecting single- and double-quoted string literals.
    """
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    in_string = False
    string_char = ""

    for ch in text:
        if in_string:
            current.append(ch)
            if ch == string_char:
                in_string = False
            continue
        if ch in ("'", '"'):
            in_string = True
            string_char = ch
            current.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)

    if current:
        parts.append("".join(current))
    return parts


def parse_table_columns(ddl_text: str) -> List[str]:
    """
    Extract ordered column names from a CREATE TABLE DDL.

    Walks the parenthesised column block with depth-counting so that
    nested type parens (``DECIMAL(15,2)``, ``VARCHAR(100) CHARACTER
    SET LATIN``) and multi-arg constraints don't split the entry list.

    Inline ``CONSTRAINT``, ``CHECK``, ``PRIMARY KEY``, ``UNIQUE``,
    ``FOREIGN KEY`` and table-level ``INDEX`` clauses are skipped.

    Args:
        ddl_text: Full text of a .tbl file.

    Returns:
        Ordered list of column names. Empty if no column block is
        found (caller should treat that as a parse failure).
    """
    cleaned = _strip_sql_comments(ddl_text)

    # If the table has no column block at all (e.g. malformed DDL
    # like ``CREATE TABLE D.X PRIMARY INDEX (id)``), we must not
    # mis-identify the PRIMARY INDEX parens as columns. Detect any
    # post-column clause keyword appearing before the first '(' and
    # bail out early.
    first_paren = cleaned.find("(")
    if first_paren >= 0:
        prefix_upper = cleaned[:first_paren].upper()
        for marker in (
            "PRIMARY INDEX",
            "PARTITION",
            "UNIQUE PRIMARY INDEX",
            "NO PRIMARY INDEX",
        ):
            if marker in prefix_upper:
                return []

    # Find the FIRST top-level parenthesised block — that is the
    # column list. Anything before (table options like ,FALLBACK)
    # and anything after (PRIMARY INDEX, PARTITION) is at depth 0
    # and not enclosed.
    depth = 0
    block_start = -1
    block_end = -1
    in_string = False
    string_char = ""

    for i, ch in enumerate(cleaned):
        if in_string:
            if ch == string_char:
                in_string = False
            continue
        if ch in ("'", '"'):
            in_string = True
            string_char = ch
            continue
        if ch == "(":
            if depth == 0:
                block_start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                block_end = i
                break

    if block_start < 0 or block_end < 0:
        return []

    block = cleaned[block_start:block_end]

    columns: List[str] = []
    for raw_entry in _split_top_level(block, ","):
        entry = raw_entry.strip().lstrip(",").strip()
        if not entry:
            continue

        upper = entry.upper()
        # Skip table-level constraints / indexes.
        if upper.startswith(
            ("CONSTRAINT ", "CHECK ", "PRIMARY ", "UNIQUE ", "FOREIGN ", "INDEX ")
        ):
            continue

        # Match: optional quoted name, otherwise bare identifier.
        match = re.match(r'\s*("[^"]+"|[A-Za-z_]\w*)', entry)
        if not match:
            continue
        name = match.group(1).strip('"')
        columns.append(name)

    return columns


# ===========================================================================
# Section 3 — Locking view emission
# ===========================================================================


def render_column_list(columns: List[str], qualifier: str = "") -> str:
    """
    Render a column list using the SHIPS leading-comma 4-space style.

    Args:
        columns:    Ordered column names.
        qualifier:  Optional alias prefix, e.g. "m" for ``m.col``.
                    Empty string emits bare column names.

    Returns:
        Multi-line string with one column per line, leading comma on
        every line except the first. No surrounding parens.

    Example output (qualifier=""):
              Mortgage_Id
            , Applicant_Name
            , Loan_Amount
    """
    if not columns:
        return ""
    prefix = f"{qualifier}." if qualifier else ""
    lines: List[str] = []
    for index, col in enumerate(columns):
        leader = "  " if index == 0 else ", "
        lines.append(f"{_INDENT}{leader}{prefix}{col}")
    return "\n".join(lines)


def generate_locking_view_ddl(table: TableSpec) -> str:
    """
    Generate the DDL text for a 1:1 locking view over a table.

    The view:
        - Lives in the views database (``{{<MOD>_DATABASE_V}}``)
        - Has the same object name as the table
        - Declares an explicit column list in BOTH the header
          (Coding Discipline rule 42) and the SELECT
        - Uses ``LOCKING ROW FOR ACCESS``
        - Carries the ``-- LOCKING VIEW`` marker so the validator
          exempts it from the object-placement rewrite rule

    Args:
        table: Parsed table spec with columns.

    Returns:
        Full DDL text, terminating semicolon and newline.
    """
    views_token = companion_token(table.database_token)
    if views_token is None:
        # Should never happen if discovery validated the token, but
        # surface the failure rather than producing broken DDL.
        raise ValueError(f"Cannot derive views token from '{table.database_token}'")

    column_block = render_column_list(table.columns, qualifier="")

    parts = [
        f"{_LOCKING_VIEW_MARKER}\n"
        f"-- 1:1 locking view over {table.database_token}.{table.object_name}\n"
        f"-- Generated by tools/generate_view_layer.py\n"
        f"CREATE VIEW {views_token}.{table.object_name}\n"
        f"(\n"
        f"{column_block}\n"
        f")\n"
        f"AS\n"
        f"LOCKING ROW FOR ACCESS\n"
        f"SELECT\n"
        f"{column_block}\n"
        f"FROM {table.database_token}.{table.object_name}\n"
        f";\n"
    ]

    # -- COMMENT ON VIEW inheritance --
    # When the source table has a COMMENT ON TABLE, emit the same text
    # on the view plus a fixed dirty-read suffix so any consumer can
    # identify the view's role from its metadata.
    if table.table_comment is not None:
        suffix = _LOCKING_VIEW_COMMENT_SUFFIX.format(
            tables_token=table.database_token,
            object_name=table.object_name,
        )
        full_comment = table.table_comment + suffix
        parts.append(
            f"COMMENT ON VIEW {views_token}.{table.object_name} IS '{full_comment}';\n"
        )

    # -- COMMENT ON COLUMN inheritance (pass-through, no transformation) --
    # Only emit for columns that have a comment in the source; absent
    # columns produce no output rather than a spurious empty comment.
    for col in table.columns:
        text = table.column_comments.get(col)
        if text is not None:
            parts.append(
                f"COMMENT ON COLUMN {views_token}.{table.object_name}.{col} "
                f"IS '{text}';\n"
            )

    return "".join(parts)


# ===========================================================================
# Section 4 — Locking view detection (matches validate.py)
# ===========================================================================


def is_locking_view(content: str) -> bool:
    """
    Detect a 1:1 locking view by header marker.

    Mirrors the detector in ``td_release_packager/validate.py`` so a
    view we generate here is recognised as exempt by the validator.
    """
    header = "\n".join(content.split("\n")[:20])
    return bool(re.search(r"--\s*LOCKING\s+VIEW", header, re.IGNORECASE))


# ===========================================================================
# Section 5 — FROM/JOIN parsing for SELECT * expansion
# ===========================================================================


# FROM <db>.<table> [AS] [<alias>]
_FROM_RE = re.compile(
    r"""
    \bFROM\s+
    (?P<db>\{\{[A-Z0-9_]+\}\}|"[^"]+"|[A-Za-z_]\w*)
    \.
    (?P<obj>"[^"]+"|[A-Za-z_]\w*)
    (?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# [INNER|LEFT [OUTER]|RIGHT [OUTER]|FULL [OUTER]|CROSS] JOIN <db>.<table>
_JOIN_RE = re.compile(
    r"""
    \b(?:(?:INNER|LEFT(?:\s+OUTER)?|RIGHT(?:\s+OUTER)?|
            FULL(?:\s+OUTER)?|CROSS)\s+)?
    JOIN\s+
    (?P<db>\{\{[A-Z0-9_]+\}\}|"[^"]+"|[A-Za-z_]\w*)
    \.
    (?P<obj>"[^"]+"|[A-Za-z_]\w*)
    (?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*))?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _alias_or_blank(raw: Optional[str]) -> str:
    """
    Validate a captured alias against the SQL keyword stoplist.

    Returns the raw alias if it's a real identifier, or an empty
    string if the regex over-captured (e.g. caught ``ON`` or
    ``WHERE`` instead of an alias).
    """
    if not raw:
        return ""
    if raw.upper() in _ALIAS_STOPLIST:
        return ""
    return raw


def parse_from_clause(view_body: str) -> List[FromClauseRef]:
    """
    Extract every ``FROM`` and ``JOIN`` table reference from a view
    body, in document order.

    Subqueries (parenthesised FROM/JOIN sources) are not handled — a
    view that uses them will simply yield fewer references and the
    SELECT-* expansion will surface a warning.

    Args:
        view_body: Text after ``AS`` and including ``LOCKING ROW FOR
                   ACCESS``, the SELECT, FROM, and JOINs.

    Returns:
        List of FromClauseRef in textual order.
    """
    refs: List[FromClauseRef] = []
    cleaned = _strip_sql_comments(view_body)

    # Single FROM clause (the leading one).
    from_match = _FROM_RE.search(cleaned)
    if from_match:
        refs.append(
            FromClauseRef(
                database_token=from_match.group("db"),
                object_name=from_match.group("obj"),
                alias=_alias_or_blank(from_match.group("alias")),
            )
        )

    # Zero or more JOIN clauses.
    for join_match in _JOIN_RE.finditer(cleaned):
        refs.append(
            FromClauseRef(
                database_token=join_match.group("db"),
                object_name=join_match.group("obj"),
                alias=_alias_or_blank(join_match.group("alias")),
            )
        )

    return refs


# ===========================================================================
# Section 6 — SELECT * expansion
# ===========================================================================


# Detects an unqualified SELECT * (with optional DISTINCT/TOP/etc).
# Anchored on the SELECT keyword followed by * before the next FROM.
_SELECT_STAR_RE = re.compile(
    r"\bSELECT\s+(?:DISTINCT\s+)?\*\s+FROM\b",
    re.IGNORECASE,
)

# Detects a qualified SELECT alias.* form (e.g. ``SELECT m.*``).
_SELECT_ALIAS_STAR_RE = re.compile(
    r"\bSELECT\s+(?:DISTINCT\s+)?([A-Za-z_]\w*)\.\*\s+FROM\b",
    re.IGNORECASE,
)


def _resolve_columns_for_ref(
    ref: FromClauseRef,
    column_index: Dict[Tuple[str, str], List[str]],
) -> Optional[List[str]]:
    """
    Look up columns for a FROM/JOIN reference.

    Indexed by (database_token, object_name). The token may be the
    tables-side ``_T`` form or the views-side ``_V`` form — for a 1:1
    locking view layer the columns are identical, so we try both.

    Returns:
        Columns list, or None if the reference cannot be resolved.
    """
    key = (ref.database_token, ref.object_name)
    if key in column_index:
        return column_index[key]

    # Try the companion token: a view referencing _V resolves through
    # the underlying _T columns (and vice versa for the unusual case
    # of a view that still references _T pre-rewrite).
    other = companion_token(ref.database_token)
    if other is not None:
        alt_key = (other, ref.object_name)
        if alt_key in column_index:
            return column_index[alt_key]

    return None


def expand_select_star(
    view_content: str,
    column_index: Dict[Tuple[str, str], List[str]],
    warnings: List[str],
    view_label: str,
) -> str:
    """
    Replace SELECT * (and SELECT alias.*) in a view body with an
    explicit alias-qualified column list.

    Smart expansion behaviour:
        - Each source table contributes its columns in declaration
          order, qualified by the source alias if one exists, or by
          the bare object name otherwise.
        - On column-name collision across sources, the FIRST source's
          column keeps the bare name and subsequent sources get an
          alias-derived AS rename, e.g. ``p.Created_Dt AS p_Created_Dt``.
          A warning is emitted per collision.
        - If any source's columns cannot be resolved (e.g. the
          referenced table isn't in the index, or the FROM clause
          uses a subquery), the view is returned unchanged and a
          warning is appended.

    Args:
        view_content: Full text of the .viw file.
        column_index: ``(database_token, object_name) -> [columns]``
                      built from all .tbl files across modules.
        warnings:     Mutable list — receives any soft failures.
        view_label:   Identifier used in warning messages
                      (typically the relative file path).

    Returns:
        Possibly modified view content.
    """
    has_unqualified = bool(_SELECT_STAR_RE.search(view_content))
    alias_star_match = _SELECT_ALIAS_STAR_RE.search(view_content)
    if not has_unqualified and not alias_star_match:
        return view_content

    refs = parse_from_clause(view_content)
    if not refs:
        warnings.append(
            f"{view_label}: SELECT * present but FROM clause could "
            f"not be parsed (subquery or unsupported syntax). "
            f"Left unchanged for manual review."
        )
        return view_content

    # If only an alias.* form is present, restrict to that one source.
    if alias_star_match and not has_unqualified:
        wanted_alias = alias_star_match.group(1)
        matched = [
            r for r in refs if r.alias and r.alias.upper() == wanted_alias.upper()
        ]
        if not matched:
            warnings.append(
                f"{view_label}: SELECT {wanted_alias}.* references "
                f"alias '{wanted_alias}' that was not found in the "
                f"FROM/JOIN clause. Left unchanged."
            )
            return view_content
        refs = matched

    # Resolve columns for each ref and detect collisions.
    expanded_lines: List[str] = []
    seen_output: Set[str] = set()
    failed_refs: List[FromClauseRef] = []
    collision_count = 0

    for ref in refs:
        cols = _resolve_columns_for_ref(ref, column_index)
        if cols is None:
            failed_refs.append(ref)
            continue
        # Choose qualifier: alias if present, else bare object name.
        qualifier = ref.alias or ref.object_name
        for col in cols:
            output_name = col
            if col.upper() in seen_output:
                # Collision: alias the duplicate to keep DDL valid.
                output_name = f"{qualifier}_{col}"
                collision_count += 1
                warnings.append(
                    f"{view_label}: column collision on '{col}' — "
                    f"renamed to '{output_name}' on {qualifier}."
                )
            seen_output.add(output_name.upper())
            line_prefix = "  " if not expanded_lines else ", "
            if col == output_name:
                expanded_lines.append(f"{_INDENT}{line_prefix}{qualifier}.{col}")
            else:
                expanded_lines.append(
                    f"{_INDENT}{line_prefix}{qualifier}.{col} AS {output_name}"
                )

    if failed_refs:
        names = ", ".join(f"{r.database_token}.{r.object_name}" for r in failed_refs)
        warnings.append(
            f"{view_label}: could not resolve columns for "
            f"{len(failed_refs)} source(s): {names}. View left unchanged."
        )
        return view_content

    if not expanded_lines:
        warnings.append(
            f"{view_label}: SELECT * resolved to zero columns. Left unchanged."
        )
        return view_content

    expanded_block = "\n".join(expanded_lines)
    replacement = f"SELECT\n{expanded_block}\nFROM"

    # Replace exactly one occurrence — whichever form was present.
    if has_unqualified:
        new_content = _SELECT_STAR_RE.sub(replacement, view_content, count=1)
    else:
        new_content = _SELECT_ALIAS_STAR_RE.sub(replacement, view_content, count=1)

    return new_content


# ===========================================================================
# Section 7 — _T to _V reference rewrite
# ===========================================================================


def rewrite_tables_to_views(
    view_content: str,
    warnings: List[str],
    view_label: str,
) -> Tuple[str, int]:
    """
    Redirect ``{{<MOD>_DATABASE_T}}`` references to
    ``{{<MOD>_DATABASE_V}}`` in a view body.

    Comment text and string literals are masked out so a reference
    inside a ``--`` comment or a ``'...'`` string is not rewritten.

    The locking view header marker is detected by the caller; this
    function unconditionally rewrites whatever it sees, so callers
    must NOT pass locking-view content here.

    Args:
        view_content: Full text of the .viw file.
        warnings:     Mutable list for soft failures (currently unused
                      but kept for parity with expand_select_star).
        view_label:   Identifier used in messages.

    Returns:
        Tuple of ``(new_content, replacement_count)``.
    """
    # Build a mask of positions that are inside comments/strings.
    mask = [False] * len(view_content)
    for pattern in (_BLOCK_COMMENT_RE, _LINE_COMMENT_RE):
        for match in pattern.finditer(view_content):
            for pos in range(match.start(), match.end()):
                mask[pos] = True
    # Strings — masked AFTER comments so a ' inside -- ... is irrelevant.
    string_re = re.compile(r"'(?:[^']|'')*'")
    for match in string_re.finditer(view_content):
        for pos in range(match.start(), match.end()):
            mask[pos] = True

    # Walk every {{<MOD>_DATABASE_T}} occurrence and rewrite if not masked.
    out_parts: List[str] = []
    last_end = 0
    count = 0
    for match in _MODULE_TOKEN_RE.finditer(view_content):
        if match.group(2) != "T":
            continue
        if mask[match.start()]:
            continue
        # Append text up to this match, then the rewritten token.
        out_parts.append(view_content[last_end : match.start()])
        module = match.group(1)
        out_parts.append(f"{{{{{module}_DATABASE_V}}}}")
        last_end = match.end()
        count += 1
    out_parts.append(view_content[last_end:])

    return "".join(out_parts), count


# ===========================================================================
# Section 8 — Database creation file emission
# ===========================================================================


def generate_database_ddl(database_token: str) -> str:
    """
    Generate a CREATE DATABASE script for a views database.

    Uses the standard SHIPS template — perm and spool are tokenised
    so projects can override per-environment via properties files.
    """
    return (
        f"-- Views database for the Object Placement Standard layer.\n"
        f"-- Generated by tools/generate_view_layer.py\n"
        f"CREATE DATABASE {database_token}\n"
        f"FROM {{{{ENV_PARENT_DATABASE}}}}\n"
        f"AS PERMANENT  = {{{{PERM_SPACE}}}}\n"
        f" , SPOOL      = {{{{SPOOL_SPACE}}}}\n"
        f" , NO FALLBACK\n"
        f" , NO BEFORE JOURNAL\n"
        f" , NO AFTER JOURNAL\n"
        f";\n"
    )


# ===========================================================================
# Section 9 — Grant file emission
# ===========================================================================


def generate_grant_ddl(
    grantee_token: str,
    grantor_tokens: List[str],
) -> str:
    """
    Generate a consolidated grants script for a single grantee
    views database.

    One ``GRANT SELECT ... WITH GRANT OPTION`` per grantor, in
    deterministic alphabetical order so re-runs produce no diff.

    Args:
        grantee_token:   Views database receiving the grants.
        grantor_tokens:  Source databases the views read from.

    Returns:
        Full DDL text, one statement per grantor.
    """
    lines: List[str] = [
        "-- Object Placement Standard read-path grants.",
        "-- Generated by tools/generate_view_layer.py",
        "-- Grantee: " + grantee_token,
    ]
    for grantor in sorted(set(grantor_tokens)):
        lines.append(f"GRANT SELECT ON {grantor} TO {grantee_token} WITH GRANT OPTION;")
    return "\n".join(lines) + "\n"


# ===========================================================================
# Section 10 — Discovery
# ===========================================================================


def _extract_table_comments(
    tbl_path: Path,
) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Extract ``COMMENT ON TABLE`` and ``COMMENT ON COLUMN`` text from
    the sibling ``.cmt`` file for a given ``.tbl`` file.

    The sibling file lives at ``DDL/comments/<stem>.cmt`` where
    ``<stem>`` is the ``.tbl`` filename without the ``.tbl`` extension
    (e.g. ``{{DOM_DATABASE_T}}.Customer``).

    If the ``.cmt`` file is absent or unreadable, both return values
    are empty — no spurious comments are fabricated.

    Args:
        tbl_path: Absolute path to the ``.tbl`` source file.

    Returns:
        ``(table_comment, column_comments)`` where:
            table_comment:    Unquoted comment text for the table, or
                              ``None`` when absent.
            column_comments:  ``{column_name: comment_text}`` for every
                              column that has a ``COMMENT ON COLUMN``
                              entry.  Column names are returned in their
                              original case from the DDL.
    """
    # Sibling comments directory: DDL/comments/ alongside DDL/tables/
    comments_dir = tbl_path.parent.parent / "comments"
    cmt_path = comments_dir / (tbl_path.stem + _EXT_COMMENT)

    if not cmt_path.is_file():
        return None, {}

    try:
        cmt_text = cmt_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not read comment file %s: %s", cmt_path, exc)
        return None, {}

    # Strip SQL comments so they don't confuse the IS '...' patterns.
    cmt_text = _BLOCK_COMMENT_RE.sub(" ", cmt_text)
    cmt_text = _LINE_COMMENT_RE.sub(" ", cmt_text)

    table_comment: Optional[str] = None
    m = _CMT_TABLE_RE.search(cmt_text)
    if m:
        table_comment = m.group(1)  # raw SQL-quoted text, '' escapes intact

    column_comments: Dict[str, str] = {}
    for m in _CMT_COLUMN_RE.finditer(cmt_text):
        col_name = m.group(1)
        col_text = m.group(2)
        column_comments[col_name] = col_text

    return table_comment, column_comments


def discover_tables(project_root: Path) -> List[TableSpec]:
    """
    Find and parse every ``.tbl`` file in the project's tables dir.

    Files that do not follow the SHIPS naming convention or whose
    token is not a module-database token are silently skipped — they
    do not belong to the OPS view-layer pipeline.

    For each table, the sibling ``.cmt`` file (if present) is also
    read to populate ``table_comment`` and ``column_comments`` on the
    returned ``TableSpec``.  Missing comment files are silently skipped.
    """
    tables_dir = project_root / _PATH_TABLES
    if not tables_dir.is_dir():
        return []

    specs: List[TableSpec] = []
    for path in sorted(tables_dir.iterdir()):
        if not path.is_file() or path.suffix != _EXT_TABLE:
            continue
        parts = split_object_filename(path.name)
        if parts is None:
            continue
        token, object_name, _ = parts

        parsed_token = parse_module_token(token)
        if parsed_token is None or parsed_token[1] != "T":
            # Either not a module-database token, or it's the views
            # side. Either way, skip.
            continue
        module = parsed_token[0]

        try:
            ddl_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue

        columns = parse_table_columns(ddl_text)
        if not columns:
            logger.warning("No columns parsed from %s — skipping.", path.name)
            continue

        table_comment, column_comments = _extract_table_comments(path)

        specs.append(
            TableSpec(
                file_path=path,
                database_token=token,
                module=module,
                object_name=object_name,
                columns=columns,
                table_comment=table_comment,
                column_comments=column_comments,
            )
        )
    return specs


def discover_views(project_root: Path) -> List[ViewSpec]:
    """Find every ``.viw`` file in the project's views dir."""
    views_dir = project_root / _PATH_VIEWS
    if not views_dir.is_dir():
        return []

    specs: List[ViewSpec] = []
    for path in sorted(views_dir.iterdir()):
        if not path.is_file() or path.suffix != _EXT_VIEW:
            continue
        parts = split_object_filename(path.name)
        if parts is None:
            continue
        token, object_name, _ = parts

        parsed_token = parse_module_token(token)
        if parsed_token is None or parsed_token[1] != "V":
            # Views must be in a _V database.
            continue
        module = parsed_token[0]

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue

        specs.append(
            ViewSpec(
                file_path=path,
                database_token=token,
                module=module,
                object_name=object_name,
                raw_content=content,
                is_locking_view=is_locking_view(content),
            )
        )
    return specs


def discover_modules(
    tables: List[TableSpec],
    views: Optional[List[ViewSpec]] = None,
) -> List[str]:
    """
    Return the sorted list of module abbreviations present.

    A module is "present" if it has at least one table OR at least
    one view. Including view-only modules matters for cross-module
    grant detection — a downstream module (e.g. SEM) may reference
    upstream views even when it owns no tables of its own.
    """
    modules: Set[str] = {t.module for t in tables}
    if views:
        modules.update(v.module for v in views)
    return sorted(modules)


# ===========================================================================
# Section 11 — Idempotent file write
# ===========================================================================


def write_if_different(
    path: Path,
    content: str,
    dry_run: bool,
) -> bool:
    """
    Write *content* to *path* iff the existing content differs.

    Args:
        path:     Destination file.
        content:  Desired content (already finalised).
        dry_run:  If True, do not touch the filesystem; only return
                  whether a write WOULD have happened.

    Returns:
        True if a write occurred (or would occur in dry-run), False
        if the file already had the desired content.
    """
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == content:
            return False

    if dry_run:
        logger.info("[DRY-RUN] Would write %s", path)
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("Wrote %s", path)
    return True


# ===========================================================================
# Section 12 — Pipeline orchestration
# ===========================================================================


def build_column_index(
    tables: List[TableSpec],
) -> Dict[Tuple[str, str], List[str]]:
    """
    Build the lookup ``(database_token, object_name) -> [columns]``.

    Each table is indexed under its tables-side token AND its
    matching views-side token (because the 1:1 locking view exposes
    the same columns), so the SELECT-* expander resolves either form.
    """
    index: Dict[Tuple[str, str], List[str]] = {}
    for table in tables:
        index[(table.database_token, table.object_name)] = list(table.columns)
        views_token = companion_token(table.database_token)
        if views_token:
            index[(views_token, table.object_name)] = list(table.columns)
    return index


def collect_cross_module_grants(
    rewritten_views: List[Tuple[ViewSpec, str]],
) -> Dict[str, Set[str]]:
    """
    Walk every rewritten business view to find cross-module reads.

    For each (grantee_views_db, set_of_grantor_dbs) the views in that
    module read from, return the grants that need to exist.

    Args:
        rewritten_views: List of ``(view_spec, rewritten_content)``
                         pairs from the rewrite phase.

    Returns:
        Dict ``{grantee_token: {grantor_token, ...}}`` containing
        cross-module read grants ONLY. Same-module grants
        (``_T -> _V`` for the view's own module) are added by the
        caller to keep this function focused on cross-module logic.
    """
    grants: Dict[str, Set[str]] = {}
    for view, content in rewritten_views:
        if view.is_locking_view:
            continue
        grantee = view.database_token
        cleaned = _strip_sql_comments(content)
        for match in _MODULE_TOKEN_RE.finditer(cleaned):
            ref_module = match.group(1)
            ref_suffix = match.group(2)
            ref_token = f"{{{{{ref_module}_DATABASE_{ref_suffix}}}}}"
            # Skip self-references to the view's own _V (a view in
            # SEM_V referencing SEM_V is a no-op grant).
            if ref_token == grantee:
                continue
            # Skip same-module _T references (the same-module grant
            # is added separately and unconditionally below).
            if ref_module == view.module and ref_suffix == "T":
                continue
            grants.setdefault(grantee, set()).add(ref_token)
    return grants


def run(
    project_root: Path,
    requested_modules: Optional[Set[str]],
    dry_run: bool,
) -> GenerationResult:
    """
    Drive the end-to-end generation pipeline.

    Args:
        project_root:       SHIPS project root.
        requested_modules:  None or empty set means "all detected
                            modules"; otherwise filter to these.
        dry_run:            If True, skip writes.

    Returns:
        Populated GenerationResult.
    """
    result = GenerationResult()

    # -- Phase 1: discover and index --------------------------------
    tables = discover_tables(project_root)
    views = discover_views(project_root)

    if not tables:
        result.errors.append(
            f"No tables found under {project_root / _PATH_TABLES}. "
            f"Is this a SHIPS project with the OPS token convention "
            f"({{{{<MOD>_DATABASE_T}}}})?"
        )
        return result

    available_modules = set(discover_modules(tables, views))
    if requested_modules:
        targets = requested_modules & available_modules
        missing = requested_modules - available_modules
        for module in sorted(missing):
            result.warnings.append(
                f"Module '{module}' requested but no .tbl files found."
            )
    else:
        targets = available_modules

    if not targets:
        result.errors.append("No matching modules to process.")
        return result

    logger.info(
        "Processing modules: %s (dry-run=%s)",
        ", ".join(sorted(targets)),
        dry_run,
    )

    column_index = build_column_index(tables)

    # -- Phase 2: locking views from tables -------------------------
    grantees_needing_same_module: Dict[str, str] = {}  # _V token -> _T token
    for table in tables:
        if table.module not in targets:
            continue
        ddl = generate_locking_view_ddl(table)
        views_token = companion_token(table.database_token)
        out_path = (
            project_root / _PATH_VIEWS / f"{views_token}.{table.object_name}{_EXT_VIEW}"
        )
        if write_if_different(out_path, ddl, dry_run):
            result.locking_views_written += 1
        else:
            result.locking_views_unchanged += 1
        # Record the same-module grant we'll emit later.
        grantees_needing_same_module[views_token] = table.database_token

    # -- Phase 3: rewrite existing business views -------------------
    rewritten: List[Tuple[ViewSpec, str]] = []
    for view in views:
        if view.module not in targets:
            continue
        # Locking views: skip rewrite, but DO include in grants scan
        # below by re-reading from disk after possible regeneration.
        if view.is_locking_view:
            # Re-read after our locking-view phase may have overwritten
            # the on-disk file with the canonical version.
            try:
                content = view.file_path.read_text(encoding="utf-8")
            except OSError:
                content = view.raw_content
            rewritten.append((view, content))
            continue

        view_label = str(view.file_path.relative_to(project_root))
        new_content = expand_select_star(
            view.raw_content,
            column_index,
            result.warnings,
            view_label,
        )
        new_content, count = rewrite_tables_to_views(
            new_content, result.warnings, view_label
        )
        if write_if_different(view.file_path, new_content, dry_run):
            result.business_views_rewritten += 1
        else:
            result.business_views_unchanged += 1
        rewritten.append((view, new_content))

    # -- Phase 4: views database creation files ---------------------
    seen_dbs: Set[str] = set()
    for views_token in grantees_needing_same_module:
        if views_token in seen_dbs:
            continue
        seen_dbs.add(views_token)
        ddl = generate_database_ddl(views_token)
        out_path = project_root / _PATH_DATABASES / f"{views_token}{_EXT_DB}"
        if not out_path.exists():
            if write_if_different(out_path, ddl, dry_run):
                result.databases_written += 1
        else:
            result.databases_unchanged += 1

    # -- Phase 5: consolidated grants -------------------------------
    cross_module = collect_cross_module_grants(rewritten)

    # Merge same-module grants on top of cross-module grants so
    # every views database we touched gets the _T -> _V baseline.
    all_grantees: Set[str] = set(grantees_needing_same_module)
    all_grantees.update(cross_module.keys())

    for grantee in sorted(all_grantees):
        grantor_set: Set[str] = set()
        same_module_grantor = grantees_needing_same_module.get(grantee)
        if same_module_grantor:
            grantor_set.add(same_module_grantor)
        grantor_set.update(cross_module.get(grantee, set()))
        if not grantor_set:
            continue
        ddl = generate_grant_ddl(grantee, list(grantor_set))
        out_path = project_root / _PATH_GRANTS / f"{grantee}{_EXT_GRANT}"
        if write_if_different(out_path, ddl, dry_run):
            result.grants_written += 1
        else:
            result.grants_unchanged += 1

    return result


# ===========================================================================
# Section 13 — CLI
# ===========================================================================


def _parse_modules_arg(raw: str) -> Optional[Set[str]]:
    """
    Convert ``--modules`` argument into a set, or None for ALL.
    """
    if not raw:
        return None
    if raw.strip().upper() == "ALL":
        return None
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _build_arg_parser(prog: Optional[str] = None) -> argparse.ArgumentParser:
    """
    Construct the CLI argument parser.

    Args:
        prog: Optional program name used in usage/help text. The shim
              and the ``python -m ...`` entry point each pass their own
              canonical invocation here so users see a copy-pasteable
              command in ``--help`` output instead of argparse's default
              ``view_layer_generator.py`` filename.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Generate the Object Placement Standard view layer "
            "(1:1 locking views, business view rewrites, _V databases, "
            "and consolidated grants) for one or more SHIPS modules."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Path to the SHIPS project root.",
    )
    parser.add_argument(
        "--modules",
        default="ALL",
        help=(
            "Comma-separated list of module abbreviations "
            "(e.g. DOM,SEM,MEM,OBS,STG) or 'ALL' to process every "
            "module discovered under payload/database/DDL/tables. "
            "Default: ALL."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging (INFO level).",
    )
    add_version_argument(parser)
    return parser


def _print_summary(result: GenerationResult) -> None:
    """Render a concise human-readable summary to stdout."""
    print("")
    print("View layer generation summary")
    print("-" * 60)
    print(f"  Locking views written:     {result.locking_views_written}")
    print(f"  Locking views unchanged:   {result.locking_views_unchanged}")
    print(f"  Business views rewritten:  {result.business_views_rewritten}")
    print(f"  Business views unchanged:  {result.business_views_unchanged}")
    print(f"  Databases written:         {result.databases_written}")
    print(f"  Databases unchanged:       {result.databases_unchanged}")
    print(f"  Grant files written:       {result.grants_written}")
    print(f"  Grant files unchanged:     {result.grants_unchanged}")
    if result.warnings:
        print("")
        print(f"Warnings ({len(result.warnings)}):")
        for warning in result.warnings:
            print(f"  - {warning}")
    if result.errors:
        print("")
        print(f"Errors ({len(result.errors)}):")
        for error in result.errors:
            print(f"  - {error}")


def main(argv: Optional[List[str]] = None, *, prog: Optional[str] = None) -> int:
    """
    Entry point. Returns process exit code (0 on success, 1 on error).

    Args:
        argv: Override the argv list (used by tests).
        prog: Override the program name shown in usage/help text.
              When None, argparse's default basename-of-sys.argv[0] is
              used (which is misleading for `python -m ...` invocations).
    """
    parser = _build_arg_parser(prog=prog)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    project_root = Path(args.project).resolve()
    if not project_root.is_dir():
        print(
            f"ERROR: --project path does not exist or is not a "
            f"directory: {project_root}",
            file=sys.stderr,
        )
        return 1

    requested = _parse_modules_arg(args.modules)
    result = run(project_root, requested, args.dry_run)

    _print_summary(result)

    if result.errors:
        return 1
    return 0


if __name__ == "__main__":
    # When invoked via `python -m td_release_packager.view_layer_generator`,
    # __package__ is the package name. When invoked as a bare file
    # (`python src/td_release_packager/view_layer_generator.py`), it's empty.
    if __package__:
        sys.exit(main(prog="python -m td_release_packager.view_layer_generator"))
    else:
        sys.exit(main())
