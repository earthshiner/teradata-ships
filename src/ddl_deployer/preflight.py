"""
preflight.py — Pre-flight validation for DDL deployments.

Runs lightweight checks BEFORE any DDL is executed:

    1. DDL parsing      — Can every file be parsed and classified?
    2. Database exists   — Do all target databases exist?
    3. Access rights     — Does the user have the required CREATE/DROP
                          rights on each target database?
    4. Perm space        — Is there free permanent space available
                          in each target database?

Pre-flight is mandatory and runs automatically before every
deployment. It catches the most common failures (wrong permissions,
missing database, no space) without touching any data.

Note on access rights:
    Uses DBC.AllRightsV which resolves both direct grants and
    role-based grants. If DBC.AllRightsV is unavailable (older
    Teradata versions), falls back to DBC.AccessRightsV which
    only captures direct grants — role-based rights may not
    be detected, producing false negatives.
"""

import logging
import os
from typing import Dict, List, Set

from ddl_deployer.ddl_parser import parse_ddl_file
from ddl_deployer.models import (
    ObjectType,
    ParsedDDL,
    PreflightCheck,
    PreflightResult,
    REQUIRED_RIGHTS,
)

logger = logging.getLogger(__name__)


def run_preflight(
    cursor,
    ddl_files: List[str],
    warn_space_below_pct: float = 10.0,
) -> tuple:
    """
    Run all pre-flight checks and return results with parsed DDL.

    This is the main entry point. It parses all DDL files, then
    validates permissions and space for each target database.

    Args:
        cursor:                Active Teradata database cursor.
        ddl_files:             List of DDL file paths to validate.
        warn_space_below_pct:  Warn if free perm space drops below
                               this percentage (default: 10%).

    Returns:
        Tuple of (PreflightResult, list_of_ParsedDDL).
        The ParsedDDL list only includes successfully parsed files.
    """
    checks = []
    parsed_ddls = []
    failed_files = []
    databases: Set[str] = set()
    object_counts: Dict[str, int] = {}

    # -- Phase 1: Parse all DDL files --
    for ddl_file in ddl_files:
        try:
            parsed = parse_ddl_file(ddl_file)
            parsed_ddls.append(parsed)
            databases.add(parsed.database_name)

            # Count by object type
            type_key = parsed.object_type.value
            object_counts[type_key] = object_counts.get(type_key, 0) + 1

            checks.append(PreflightCheck(
                check_name="ddl_parse",
                passed=True,
                database=parsed.database_name,
                message=(
                    f"Parsed {os.path.basename(ddl_file)}: "
                    f"{parsed.object_type.value} {parsed.qualified_name}"
                    + (" (MULTISET injected)" if parsed.multiset_injected else "")
                ),
            ))

            if parsed.multiset_injected:
                checks.append(PreflightCheck(
                    check_name="multiset_injection",
                    passed=True,
                    database=parsed.database_name,
                    message=(
                        f"{parsed.qualified_name}: MULTISET auto-injected "
                        f"(CREATE TABLE had no SET/MULTISET qualifier)."
                    ),
                    severity="WARNING",
                ))

        except (ValueError, FileNotFoundError) as e:
            failed_files.append(ddl_file)
            checks.append(PreflightCheck(
                check_name="ddl_parse",
                passed=False,
                database="UNKNOWN",
                message=f"Failed to parse {os.path.basename(ddl_file)}: {e}",
            ))

    # -- Phase 2: Check databases exist --
    for db_name in sorted(databases):
        exists = _database_exists(cursor, db_name)
        checks.append(PreflightCheck(
            check_name="database_exists",
            passed=exists,
            database=db_name,
            message=(
                f"Database '{db_name}' exists."
                if exists else
                f"Database '{db_name}' does NOT exist."
            ),
        ))

    # -- Phase 3: Check access rights per database --
    # Determine which rights are needed per database
    db_required_rights = _collect_required_rights(parsed_ddls)

    for db_name, rights in sorted(db_required_rights.items()):
        right_checks = _check_access_rights(cursor, db_name, rights)
        checks.extend(right_checks)

    # -- Phase 4: Check perm space per database --
    # Only check databases that actually need tables/JIs (space-consuming objects)
    space_databases = {
        p.database_name for p in parsed_ddls
        if p.object_type in (ObjectType.TABLE, ObjectType.JOIN_INDEX, ObjectType.HASH_INDEX)
    }

    for db_name in sorted(space_databases):
        space_checks = _check_perm_space(cursor, db_name, warn_space_below_pct)
        checks.extend(space_checks)

    # -- Tally results --
    errors = sum(1 for c in checks if not c.passed and c.severity == "ERROR")
    warnings = sum(1 for c in checks if not c.passed and c.severity == "WARNING")
    # Also count passed warnings (like MULTISET injection)
    warnings += sum(1 for c in checks if c.passed and c.severity == "WARNING")

    result = PreflightResult(
        passed=(errors == 0),
        checks=checks,
        databases=sorted(databases),
        object_count=object_counts,
        errors=errors,
        warnings=warnings,
    )

    logger.info(
        "Pre-flight complete: %d files parsed, %d databases, "
        "%d errors, %d warnings",
        len(parsed_ddls), len(databases), errors, warnings
    )

    return (result, parsed_ddls)


# ---------------------------------------------------------------
# Internal — Database existence check
# ---------------------------------------------------------------

def _database_exists(cursor, database_name: str) -> bool:
    """
    Check if a database exists in DBC.DatabasesV.

    Args:
        cursor:         Active database cursor.
        database_name:  Database name to check.

    Returns:
        True if the database exists.
    """
    try:
        cursor.execute(
            "SELECT 1 FROM DBC.DatabasesV WHERE DatabaseName = ?",
            [database_name]
        )
        return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("Database existence check failed for '%s': %s", database_name, e)
        return False


# ---------------------------------------------------------------
# Internal — Access rights checks
# ---------------------------------------------------------------

def _collect_required_rights(
    parsed_ddls: List[ParsedDDL],
) -> Dict[str, Set[tuple]]:
    """
    Determine which access rights are needed per target database.

    Aggregates the required rights across all objects targeting
    each database, so we check each right only once per database.

    Args:
        parsed_ddls: List of successfully parsed DDL files.

    Returns:
        Dict of database_name → set of (right_code, description) tuples.
    """
    db_rights: Dict[str, Set[tuple]] = {}

    for parsed in parsed_ddls:
        db = parsed.database_name
        if db not in db_rights:
            db_rights[db] = set()

        rights = REQUIRED_RIGHTS.get(parsed.object_type, [])
        for right_tuple in rights:
            db_rights[db].add(right_tuple)

    return db_rights


def _check_access_rights(
    cursor,
    database_name: str,
    required_rights: Set[tuple],
) -> List[PreflightCheck]:
    """
    Check whether the current user has the required rights on a database.

    Tries DBC.AllRightsV first (includes role grants). If that view
    is unavailable, falls back to DBC.AccessRightsV (direct grants
    only) with a warning about potential false negatives.

    Args:
        cursor:           Active database cursor.
        database_name:    Target database.
        required_rights:  Set of (right_code, description) tuples.

    Returns:
        List of PreflightCheck results, one per required right.
    """
    checks = []

    # Determine which rights view to use
    rights_view, using_fallback = _get_rights_view(cursor)

    if using_fallback:
        checks.append(PreflightCheck(
            check_name="rights_view",
            passed=True,
            database=database_name,
            message=(
                "Using DBC.AccessRightsV (direct grants only). "
                "Role-based grants may not be detected. "
                "False negatives possible."
            ),
            severity="WARNING",
        ))

    # Query the user's granted rights on this database
    granted_rights = _get_granted_rights(cursor, database_name, rights_view)

    for right_code, right_desc in sorted(required_rights):
        # Check if the right is granted (direct or via 'ALL')
        has_right = right_code.strip() in granted_rights or 'AL' in granted_rights

        checks.append(PreflightCheck(
            check_name=f"access_{right_code.strip().lower()}",
            passed=has_right,
            database=database_name,
            message=(
                f"{right_desc} ({right_code.strip()}) granted on '{database_name}'."
                if has_right else
                f"{right_desc} ({right_code.strip()}) NOT granted on '{database_name}'. "
                f"Deployment will fail for objects requiring this right."
            ),
        ))

    return checks


def _get_rights_view(cursor) -> tuple:
    """
    Determine whether DBC.AllRightsV is available.

    Args:
        cursor: Active database cursor.

    Returns:
        Tuple of (view_name, is_fallback).
        ('DBC.AllRightsV', False) if available.
        ('DBC.AccessRightsV', True) if falling back.
    """
    try:
        cursor.execute("SELECT TOP 1 1 FROM DBC.AllRightsV")
        cursor.fetchone()
        return ('DBC.AllRightsV', False)
    except Exception:
        return ('DBC.AccessRightsV', True)


def _get_granted_rights(
    cursor,
    database_name: str,
    rights_view: str,
) -> Set[str]:
    """
    Query the granted rights for the current user on a database.

    Args:
        cursor:         Active database cursor.
        database_name:  Target database.
        rights_view:    'DBC.AllRightsV' or 'DBC.AccessRightsV'.

    Returns:
        Set of granted right codes (trimmed).
    """
    try:
        cursor.execute(
            f"SELECT TRIM(AccessRight) FROM {rights_view} "
            f"WHERE DatabaseName = ? "
            f"AND (UserName = USER OR UserName = 'PUBLIC')",
            [database_name]
        )
        return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logger.warning(
            "Rights check failed for '%s' via %s: %s",
            database_name, rights_view, e
        )
        return set()


# ---------------------------------------------------------------
# Internal — Perm space checks
# ---------------------------------------------------------------

def _check_perm_space(
    cursor,
    database_name: str,
    warn_below_pct: float,
) -> List[PreflightCheck]:
    """
    Check available permanent space in a database.

    Queries DBC.DiskSpaceV to determine free perm space. Reports
    an ERROR if the database has zero free space, and a WARNING
    if free space is below the threshold percentage.

    Args:
        cursor:         Active database cursor.
        database_name:  Target database.
        warn_below_pct: Warning threshold as a percentage.

    Returns:
        List of PreflightCheck results.
    """
    checks = []

    try:
        cursor.execute(
            "SELECT "
            "     SUM(MaxPerm) AS MaxPerm"
            "    ,SUM(CurrentPerm) AS CurrentPerm"
            " FROM DBC.DiskSpaceV"
            " WHERE DatabaseName = ?",
            [database_name]
        )
        row = cursor.fetchone()

        if row is None or row[0] is None:
            checks.append(PreflightCheck(
                check_name="perm_space",
                passed=False,
                database=database_name,
                message=(
                    f"Could not retrieve perm space for '{database_name}'. "
                    f"Database may not exist or user lacks access."
                ),
            ))
            return checks

        max_perm = row[0]
        current_perm = row[1] or 0
        free_perm = max_perm - current_perm

        if max_perm == 0:
            checks.append(PreflightCheck(
                check_name="perm_space",
                passed=False,
                database=database_name,
                message=(
                    f"Database '{database_name}' has zero MaxPerm allocated. "
                    f"No objects can be created."
                ),
            ))
            return checks

        free_pct = (free_perm / max_perm) * 100 if max_perm > 0 else 0

        # Format sizes for human readability
        free_str = _format_bytes(free_perm)
        max_str = _format_bytes(max_perm)

        if free_perm <= 0:
            checks.append(PreflightCheck(
                check_name="perm_space",
                passed=False,
                database=database_name,
                message=(
                    f"Database '{database_name}' has NO free perm space "
                    f"({max_str} fully consumed). Cannot create objects."
                ),
            ))
        elif free_pct < warn_below_pct:
            checks.append(PreflightCheck(
                check_name="perm_space",
                passed=True,
                database=database_name,
                message=(
                    f"Database '{database_name}' perm space low: "
                    f"{free_str} free of {max_str} ({free_pct:.1f}% free)."
                ),
                severity="WARNING",
            ))
        else:
            checks.append(PreflightCheck(
                check_name="perm_space",
                passed=True,
                database=database_name,
                message=(
                    f"Database '{database_name}' perm space OK: "
                    f"{free_str} free of {max_str} ({free_pct:.1f}% free)."
                ),
            ))

    except Exception as e:
        logger.warning("Perm space check failed for '%s': %s", database_name, e)
        checks.append(PreflightCheck(
            check_name="perm_space",
            passed=True,  # Don't block on check failure
            database=database_name,
            message=(
                f"Could not check perm space for '{database_name}': {e}. "
                f"Proceeding without space validation."
            ),
            severity="WARNING",
        ))

    return checks


def _format_bytes(num_bytes: int) -> str:
    """
    Format a byte count as a human-readable string.

    Args:
        num_bytes: Byte count.

    Returns:
        Formatted string (e.g. '1.5 GB', '256 MB').
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} EB"
