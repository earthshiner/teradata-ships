#!/usr/bin/env python3
"""
infer_grants.py — Intent-Based Cross-Database Grant Inference for SHIPS Projects

Scans a SHIPS project directory for DDL files (views, procedures, macros,
triggers, functions), parses each file to determine what SQL operations are
performed on objects in each referenced database, and generates consolidated
.grt files per grantee database.

Core axiom:
    The SQL verb applied to objects in each specific database determines the
    minimum privilege the owning (grantee) database needs on that referenced
    (grantor) database. A single DML statement may touch multiple databases
    with different intents — each database receives only the privilege matching
    the operation applied to its objects.

Grant rules:
    - Container-level only: GRANT ... ON {database} TO {database}
    - WITH GRANT OPTION always (pass-through to consumers)
    - Self-references excluded (same database needs no grant)
    - Token-aware: works on tokenised DDL (e.g. {{DOM_DATABASE_T}})
    - One .grt file per grantee database, all privileges consolidated
    - Multiple privileges on the same grantor→grantee pair consolidated
      into a single comma-separated GRANT statement

Usage:
    python infer_grants.py <project_dir> [--output-dir <dir>] [--dry-run] [--verbose]

Examples:
    # Generate .grt files into the project's payload/database/DCL/inter_db/ directory
    python infer_grants.py ./MortgagePlatform

    # Preview without writing files
    python infer_grants.py ./MortgagePlatform --dry-run --verbose

    # Specify a custom output directory
    python infer_grants.py ./MortgagePlatform --output-dir ./MortgagePlatform/dcl/inter_db

Output directory:
    The default output is <project_dir>/payload/database/DCL/inter_db/ — the DCL
    subdirectory reserved for inter-database (container-to-container)
    grants. The DCL directory structure is:

        dcl/
        ├── roles/      — GRANT ... TO {role}
        ├── users/      — GRANT ... TO {user}
        └── inter_db/   — GRANT ... ON {database} TO {database}

    This tool generates inter-database grants exclusively. Role and
    user grants are managed separately.

Author: Paul Dancer — Teradata Worldwide Field Tech
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Set, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DDL file extensions we scan (lowercase, without leading dot)
SCANNABLE_EXTENSIONS = {"viw", "spl", "mcr", "trg", "fnc"}

# Regex to match tokenised database references: {{TOKEN}}.ObjectName
# Captures the full token including braces and the object name
RE_TOKEN_REF = re.compile(
    r"\{\{([A-Z][A-Z0-9_]*)\}\}"  # group 1: token name (inside braces)
    r"\s*\.\s*"  # dot separator (optional whitespace)
    r"([A-Za-z_][A-Za-z0-9_]*)",  # group 2: object name
    re.IGNORECASE,
)

# Regex to match non-tokenised fully-qualified references: Database.ObjectName
# Only matches when the database name is NOT a token (no braces)
RE_LITERAL_REF = re.compile(
    r"(?<!\{)\b([A-Z][A-Z0-9_]{1,127})"  # group 1: database name
    r"\s*\.\s*"  # dot separator
    r"([A-Za-z_][A-Za-z0-9_]*)\b",  # group 2: object name
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# DML target patterns — identify which databases are WRITE targets
# These patterns match the database reference immediately after the DML verb.
# Everything else (FROM, JOIN) is a READ (SELECT) reference.
# ---------------------------------------------------------------------------

# INSERT INTO {{DB}}.Table or INSERT {{DB}}.Table
RE_INSERT_TARGET = re.compile(
    r"\bINSERT\s+(?:INTO\s+)?"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# UPDATE {{DB}}.Table (the target — not the FROM source)
RE_UPDATE_TARGET = re.compile(
    r"\bUPDATE\s+"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# DELETE [FROM] {{DB}}.Table
# Careful: DELETE FROM is the target, not a read source
RE_DELETE_TARGET = re.compile(
    r"\bDELETE\s+(?:FROM\s+)?"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# MERGE INTO {{DB}}.Table
RE_MERGE_TARGET = re.compile(
    r"\bMERGE\s+INTO\s+"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# CALL {{DB}}.Procedure
RE_CALL_TARGET = re.compile(
    r"\bCALL\s+"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# EXEC[UTE] {{DB}}.Macro
RE_EXEC_TARGET = re.compile(
    r"\bEXEC(?:UTE)?\s+"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# CREATE/REPLACE statement — extracts the owning database and object type
RE_CREATE_STMT = re.compile(
    r"\b(?:CREATE|REPLACE)\s+"
    r"(VIEW|PROCEDURE|MACRO|FUNCTION|TRIGGER|(?:MULTISET\s+)?TABLE"
    r"|SET\s+TABLE|VOLATILE\s+TABLE)"
    r"\s+"
    r"(?:\{\{([A-Z][A-Z0-9_]*)\}\}|([A-Z][A-Z0-9_]{1,127}))"
    r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# Privilege names as Teradata expects them
PRIV_SELECT = "SELECT"
PRIV_INSERT = "INSERT"
PRIV_UPDATE = "UPDATE"
PRIV_DELETE = "DELETE"
PRIV_EXEC_PROC = "EXECUTE PROCEDURE"
PRIV_EXEC = "EXECUTE"

# Canonical ordering for privilege consolidation in GRANT statements
PRIV_ORDER = [
    PRIV_SELECT,
    PRIV_INSERT,
    PRIV_UPDATE,
    PRIV_DELETE,
    PRIV_EXEC_PROC,
    PRIV_EXEC,
]


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------


def strip_sql_comments(sql: str) -> str:
    """
    Remove SQL comments from the input text.

    Strips both:
        - Single-line comments: -- ... to end of line
        - Block comments:       /* ... */ (including nested)

    Args:
        sql: Raw SQL text, potentially with comments.

    Returns:
        SQL text with all comments replaced by whitespace.
    """
    # Strip block comments first (non-greedy to handle multiple blocks)
    result = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Strip single-line comments
    result = re.sub(r"--[^\n]*", " ", result)
    return result


# ---------------------------------------------------------------------------
# Database reference extraction
# ---------------------------------------------------------------------------


def extract_db_ref(
    match: re.Match, token_group: int = 1, literal_group: int = 2
) -> str:
    """
    Extract the database reference from a regex match.

    The DML target patterns have two capture groups for the database:
    one for tokenised references ({{TOKEN}}) and one for literal references.
    This helper returns whichever one matched, formatted consistently.

    Args:
        match:         The regex match object.
        token_group:   Group index for the token capture.
        literal_group: Group index for the literal capture.

    Returns:
        The database reference as a string. Tokenised references are
        returned as '{{TOKEN}}'; literal references are returned as-is.
    """
    token = match.group(token_group)
    literal = match.group(literal_group)
    if token:
        return f"{{{{{token}}}}}"
    return literal


def find_all_db_references(sql: str, tokens_only: bool = True) -> Set[str]:
    """
    Find all fully-qualified database references in SQL text.

    In SHIPS projects, all database references are tokenised
    (e.g. {{DOM_DATABASE_T}}.Loan_H). Literal references like
    'mx.column' or 'dl.amount' are table aliases, not database
    names. Setting tokens_only=True (the default) matches only
    tokenised references, eliminating alias false positives.

    When tokens_only=False, literal references are also matched
    with alias/keyword blacklisting — useful for pre-tokenised
    or ad-hoc DDL analysis.

    Args:
        sql:         Comment-stripped SQL text.
        tokens_only: If True (default), match only {{TOKEN}}.Object
                     references. If False, also match literal
                     Database.Object references with alias filtering.

    Returns:
        Set of database references (tokens as '{{TOKEN}}', literals as-is).
    """
    refs = set()

    # --- Tokenised references (always matched) ---
    for match in RE_TOKEN_REF.finditer(sql):
        refs.add(f"{{{{{match.group(1)}}}}}")

    # --- Literal references (only when tokens_only=False) ---
    if not tokens_only:
        # Collect known object names and aliases to use as a blacklist
        known_objects = set()
        known_aliases = set()

        # Object names from tokenised references
        for match in RE_TOKEN_REF.finditer(sql):
            known_objects.add(match.group(2).upper())

        # Table/view aliases from FROM, JOIN, USING clauses
        re_alias = re.compile(
            r"\b(?:FROM|JOIN|USING)\s+"
            r"(?:\{\{[A-Z][A-Z0-9_]*\}\}\s*\.\s*)?"
            r"[A-Za-z_][A-Za-z0-9_]*"
            r"\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        sql_keywords = {
            "ON",
            "WHERE",
            "SET",
            "INNER",
            "LEFT",
            "RIGHT",
            "CROSS",
            "FULL",
            "OUTER",
            "JOIN",
            "AND",
            "OR",
            "NOT",
            "IN",
            "EXISTS",
            "BETWEEN",
            "LIKE",
            "AS",
            "WHEN",
            "THEN",
            "ELSE",
            "END",
            "CASE",
            "GROUP",
            "ORDER",
            "BY",
            "HAVING",
            "UNION",
            "ALL",
            "EXCEPT",
            "INTERSECT",
            "INTO",
            "FROM",
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "VALUES",
            "WITH",
            "GRANT",
            "OPTION",
            "LOCKING",
            "ROW",
            "FOR",
            "ACCESS",
            "TABLE",
            "VIEW",
            "USING",
            "MATCHED",
            "VOLATILE",
        }
        for match in re_alias.finditer(sql):
            alias_candidate = match.group(1).upper()
            if alias_candidate not in sql_keywords:
                known_aliases.add(alias_candidate)

        # Aliases from UPDATE target
        re_update_alias = re.compile(
            r"\bUPDATE\s+"
            r"(?:\{\{[A-Z][A-Z0-9_]*\}\}\s*\.\s*)?"
            r"([A-Za-z_][A-Za-z0-9_]*)"
            r"\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        for match in re_update_alias.finditer(sql):
            obj_name = match.group(1).upper()
            alias_candidate = match.group(2).upper()
            known_objects.add(obj_name)
            if alias_candidate not in sql_keywords:
                known_aliases.add(alias_candidate)

        exclusions = known_objects | known_aliases

        system_dbs = {
            "DBC",
            "SYSLIB",
            "SYSUDTLIB",
            "SYSSPATIAL",
            "SYSJDBC",
            "SYSBAR",
            "TDSTATS",
            "TDWM",
            "SYSTEMFE",
            "DBCMNGR",
            "SYSADMIN",
            "CAST",
            "TRIM",
            "COALESCE",
            "CASE",
            "WHEN",
            "THEN",
            "ELSE",
            "END",
            "AND",
            "NOT",
            "NULL",
            "DATE",
            "TIME",
            "TIMESTAMP",
            "INTERVAL",
            "CHARACTER",
            "VARCHAR",
            "INTEGER",
            "DECIMAL",
            "FLOAT",
            "BYTEINT",
            "SMALLINT",
            "BIGINT",
            "LOCKING",
            "ROW",
            "FOR",
            "ACCESS",
        }
        for match in RE_LITERAL_REF.finditer(sql):
            db_name = match.group(1)
            db_upper = db_name.upper()
            if db_upper not in system_dbs and db_upper not in exclusions:
                refs.add(db_name)

    return refs


# ---------------------------------------------------------------------------
# Intent analysis per file
# ---------------------------------------------------------------------------


def analyse_file(filepath: Path, verbose: bool = False) -> Optional[Dict]:
    """
    Analyse a single DDL file and extract its grant implications.

    Parses the DDL to determine:
        1. The owning database (grantee) from the CREATE statement
        2. The object type (VIEW, PROCEDURE, MACRO, etc.)
        3. All referenced databases and the operations performed on each

    Args:
        filepath: Path to the DDL file.
        verbose:  If True, print diagnostic information.

    Returns:
        A dict with keys:
            'file':     str — the source filename
            'grantee':  str — the owning database token/name
            'obj_type': str — the object type
            'obj_name': str — the object name
            'grants':   dict — {grantor_db: set of privileges}
        or None if the file cannot be parsed.
    """
    try:
        raw_sql = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  WARNING: Cannot read {filepath}: {e}", file=sys.stderr)
        return None

    # Strip comments before analysis
    sql = strip_sql_comments(raw_sql)

    # --- Step 1: Identify the owning database from the CREATE statement ---
    create_match = RE_CREATE_STMT.search(sql)
    if not create_match:
        if verbose:
            print(f"  SKIP: No CREATE statement found in {filepath.name}")
        return None

    obj_type_raw = create_match.group(1).upper().strip()
    # Normalise compound object types
    if "TABLE" in obj_type_raw:
        obj_type = "TABLE"
    else:
        obj_type = obj_type_raw

    # Extract owning database
    grantee = extract_db_ref(create_match, token_group=2, literal_group=3)
    obj_name = create_match.group(4)

    if verbose:
        print(f"  Analysing: {filepath.name}")
        print(f"    Object:  {grantee}.{obj_name} ({obj_type})")

    # --- Step 2: Determine intent per referenced database ---
    # grants_map: {grantor_database: set of privileges}
    grants_map: Dict[str, Set[str]] = defaultdict(set)

    # For views, the entire body is SELECT context — every referenced
    # database gets SELECT
    if obj_type == "VIEW":
        # Extract the body after the AS keyword
        as_match = re.search(r"\bAS\b", sql, re.IGNORECASE)
        if as_match:
            body = sql[as_match.end() :]
            all_refs = find_all_db_references(body)
            for db_ref in all_refs:
                grants_map[db_ref].add(PRIV_SELECT)

    # For procedures, macros, triggers, functions — parse DML intent
    elif obj_type in ("PROCEDURE", "MACRO", "TRIGGER", "FUNCTION"):
        # --- Write targets: identify databases being written to ---
        # In SHIPS DDL, all database references are tokenised.
        # Skip any literal matches (aliases or missing tokenisation).

        # INSERT targets
        for match in RE_INSERT_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_INSERT)

        # UPDATE targets
        for match in RE_UPDATE_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_UPDATE)

        # DELETE targets
        for match in RE_DELETE_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_DELETE)

        # MERGE targets — implies both INSERT and UPDATE
        for match in RE_MERGE_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_INSERT)
            grants_map[db].add(PRIV_UPDATE)

        # CALL targets — implies EXECUTE PROCEDURE
        for match in RE_CALL_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_EXEC_PROC)

        # EXEC/EXECUTE targets — implies EXECUTE (macros)
        for match in RE_EXEC_TARGET.finditer(sql):
            db = extract_db_ref(match)
            if not db.startswith("{{"):
                continue
            grants_map[db].add(PRIV_EXEC)

        # --- Read sources: all FROM/JOIN references → SELECT ---
        # This covers standalone SELECTs, INSERT...SELECT FROM,
        # UPDATE...FROM, DELETE with subqueries, MERGE...USING
        all_refs = find_all_db_references(sql)
        # Exclude the grantee itself and any databases already identified
        # as write targets — they still get SELECT if they appear in FROM/JOIN
        # context. Actually, a write target database CAN also be read from
        # in the same file (e.g. SELECT from the same DB you INSERT into).
        # So we add SELECT to all referenced databases.
        for db_ref in all_refs:
            # Every referenced database is at minimum being read from
            # UNLESS it only appears as a CALL or EXEC target.
            # Check if this db_ref appears in a FROM/JOIN context
            # For simplicity, we add SELECT to all non-CALL/non-EXEC refs
            if db_ref not in grants_map or grants_map[db_ref] - {
                PRIV_EXEC_PROC,
                PRIV_EXEC,
            }:
                grants_map[db_ref].add(PRIV_SELECT)
            elif db_ref in grants_map and grants_map[db_ref] & {
                PRIV_EXEC_PROC,
                PRIV_EXEC,
            }:
                # Only CALL/EXEC — don't add SELECT unless it also appears
                # in a FROM/JOIN context. Check explicitly.
                from_join_pattern = re.compile(
                    r"\b(?:FROM|JOIN)\s+" + re.escape(db_ref) + r"\s*\.\s*\w+",
                    re.IGNORECASE,
                )
                if from_join_pattern.search(sql):
                    grants_map[db_ref].add(PRIV_SELECT)

    # Tables don't typically reference other databases in their DDL
    elif obj_type == "TABLE":
        if verbose:
            print("    Skipping TABLE — no cross-database references expected")
        return None

    # --- Step 3: Remove self-references ---
    grants_map.pop(grantee, None)

    # Remove empty entries
    grants_map = {k: v for k, v in grants_map.items() if v}

    if not grants_map:
        if verbose:
            print("    No cross-database grants required")
        return None

    if verbose:
        for grantor, privs in sorted(grants_map.items()):
            priv_list = ", ".join(sorted(privs, key=lambda p: PRIV_ORDER.index(p)))
            print(f"    Grant: {priv_list} ON {grantor} TO {grantee}")

    return {
        "file": filepath.name,
        "grantee": grantee,
        "obj_type": obj_type,
        "obj_name": obj_name,
        "grants": dict(grants_map),
    }


# ---------------------------------------------------------------------------
# Consolidation and .grt file generation
# ---------------------------------------------------------------------------


def consolidate_grants(
    results: List[Dict],
) -> Dict[str, Dict[str, Set[str]]]:
    """
    Consolidate all per-file grant results into per-grantee summaries.

    Merges grants from multiple files that share the same grantee database
    into a single consolidated structure.

    Args:
        results: List of analysis dicts from analyse_file().

    Returns:
        A nested dict: {grantee: {grantor: set_of_privileges}}
    """
    # consolidated: {grantee_db: {grantor_db: set of privileges}}
    consolidated: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))

    for result in results:
        grantee = result["grantee"]
        for grantor, privs in result["grants"].items():
            consolidated[grantee][grantor].update(privs)

    return dict(consolidated)


def generate_grt_content(
    grantee: str,
    grants: Dict[str, Set[str]],
    sources: List[Dict],
    project_name: str,
) -> str:
    """
    Generate the content of a single .grt file for a grantee database.

    Produces Teradata GRANT statements with consolidated privileges per
    grantor→grantee pair, ordered canonically.

    Args:
        grantee:      The grantee database token/name.
        grants:       {grantor_db: set_of_privileges} for this grantee.
        sources:      List of analysis dicts that contributed to this grantee.
        project_name: The SHIPS project name (for the file header comment).

    Returns:
        The complete .grt file content as a string.
    """
    lines = []

    # --- File header ---
    lines.append("/*")
    lines.append(f"** Implied grants for {grantee}")
    lines.append("** Auto-generated by infer_grants.py from DDL intent analysis")
    lines.append(f"** Source: SHIPS project {project_name}")
    lines.append("**")
    lines.append(f"** Grantee: {grantee}")
    lines.append("**")
    lines.append("** Axiom: container-level grants derived from SQL verb")
    lines.append("**         decomposition per referenced database.")
    lines.append("**         Each referenced database receives only the")
    lines.append("**         privilege matching the operation applied to")
    lines.append("**         its objects.")
    lines.append("**")
    lines.append("** Contributing DDL files:")
    for src in sorted(sources, key=lambda s: s["file"]):
        lines.append(f"**   {src['file']} ({src['obj_type']}: {src['obj_name']})")
    lines.append("*/")
    lines.append("")

    # --- GRANT statements ---
    # Sort grantors alphabetically for deterministic output
    for grantor in sorted(grants.keys()):
        privs = grants[grantor]
        # Sort privileges in canonical order
        sorted_privs = sorted(privs, key=lambda p: PRIV_ORDER.index(p))
        priv_str = ", ".join(sorted_privs)
        lines.append(f"GRANT {priv_str} ON {grantor} TO {grantee} WITH GRANT OPTION;")

    lines.append("")  # trailing newline
    return "\n".join(lines)


def grantee_filename(grantee: str) -> str:
    """
    Derive the .grt filename from a grantee database reference.

    For tokenised references like '{{DOM_DATABASE_V}}', the filename
    uses the token directly: {{DOM_DATABASE_V}}.grt
    For literal references, the name is used as-is.

    Args:
        grantee: The grantee database token/name.

    Returns:
        The filename string (e.g. '{{DOM_DATABASE_V}}.grt').
    """
    return f"{grantee}.grt"


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------


def find_ddl_files(project_dir: Path) -> List[Path]:
    """
    Recursively find all DDL files in a SHIPS project directory.

    Scans for files with extensions matching SCANNABLE_EXTENSIONS
    (.viw, .spl, .mcr, .trg, .fnc).

    Args:
        project_dir: Root directory of the SHIPS project.

    Returns:
        Sorted list of Path objects for each DDL file found.
    """
    ddl_files = []
    for root, _dirs, files in os.walk(project_dir):
        for fname in files:
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in SCANNABLE_EXTENSIONS:
                ddl_files.append(Path(root) / fname)
    return sorted(ddl_files)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    """
    Entry point for the grant inference tool.

    Parses command-line arguments, scans the SHIPS project directory,
    analyses each DDL file, consolidates grants per grantee, and writes
    .grt files to the output directory.
    """
    parser = argparse.ArgumentParser(
        description="Infer cross-database grants from DDL intent in a SHIPS project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Root directory of the SHIPS project to scan.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Directory to write .grt files to. "
            "Defaults to <project_dir>/payload/database/DCL/inter_db/ — the DCL "
            "subdirectory for inter-database grants."
        ),
    )
    parser.add_argument(
        "--project-name",
        type=str,
        default=None,
        help=(
            "Project name for the .grt file headers. "
            "Defaults to the project directory name."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated .grt content to stdout without writing files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print diagnostic information during analysis.",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"ERROR: {project_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    project_name = args.project_name or project_dir.name
    output_dir = args.output_dir or (
        project_dir / "payload" / "database" / "DCL" / "inter_db"
    )

    # --- Step 1: Find DDL files ---
    ddl_files = find_ddl_files(project_dir)
    print(f"SHIPS Grant Inference — {project_name}")
    print(f"  Project:    {project_dir}")
    print(f"  Output:     {output_dir}")
    print(f"  DDL files:  {len(ddl_files)} found")
    print()

    if not ddl_files:
        print("  No scannable DDL files found. Nothing to do.")
        sys.exit(0)

    # --- Step 2: Analyse each file ---
    results: List[Dict] = []
    for ddl_file in ddl_files:
        result = analyse_file(ddl_file, verbose=args.verbose)
        if result:
            results.append(result)

    print(f"\n  Files with cross-database references: {len(results)}")

    if not results:
        print("  No cross-database grants required. Nothing to generate.")
        sys.exit(0)

    # --- Step 3: Consolidate by grantee ---
    consolidated = consolidate_grants(results)
    print(f"  Grantee databases: {len(consolidated)}")
    print()

    # --- Step 4: Generate .grt files ---
    for grantee in sorted(consolidated.keys()):
        grants = consolidated[grantee]
        # Collect the source files that contributed to this grantee
        sources = [r for r in results if r["grantee"] == grantee]

        content = generate_grt_content(grantee, grants, sources, project_name)
        filename = grantee_filename(grantee)

        # Count total grant statements
        grant_count = len(grants)
        # Count total privileges
        priv_count = sum(len(privs) for privs in grants.values())

        print(
            f"  {filename}: "
            f"{grant_count} statement(s), "
            f"{priv_count} privilege(s) "
            f"from {len(sources)} DDL file(s)"
        )

        if args.dry_run:
            print()
            print(content)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = output_dir / filename
            out_path.write_text(content, encoding="utf-8")
            print(f"    → Written to {out_path}")

    print(f"\nDone. {len(consolidated)} .grt file(s) generated.")


if __name__ == "__main__":
    main()
