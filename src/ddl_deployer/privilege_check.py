"""
privilege_check.py — Deployer privilege verification.

Verifies that the deploying user has sufficient privileges to
create, drop, and replace all object types in the target databases.
If privileges are missing, generates a prerequisite GRANT script
for the System Administrator to execute before deployment.

The check distinguishes between:
  - Databases being created by THIS package: the deploying user
    receives automatic creator rights — no additional grants needed.
  - Databases that already exist: explicit grants are required if
    the deploying user is not the owner/creator.

Teradata compound GRANT keywords are used (TABLE, VIEW, etc.)
which bundle CREATE + DROP privileges per object type.

Usage:
    From deployer.py, call after preflight completes:

        from ddl_deployer.privilege_check import check_deployer_privileges

        priv_result = check_deployer_privileges(
            cursor=cursor,
            parsed_ddls=parsed_ddls,
            created_databases=created_databases,  # set of DBs created by this package
            package_name="SHIPS_TEST",
            environment="DEV",
        )
        if not priv_result.passed:
            # priv_result.script contains the prerequisite SQL
            # priv_result.missing describes what's missing
            ...
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Teradata access right code mapping
# ---------------------------------------------------------------
# Source: DBC.AllRightsV.AccessRight column.
# Codes from Paul's reference query — note CP = CHECKPOINT,
# NOT CREATE PROCEDURE (which is PC).

# Maps ObjectType → set of required AccessRight codes.
# Each set contains the CREATE + DROP codes needed for that
# type's deployment strategy.
_REQUIRED_RIGHTS: Dict[str, Set[str]] = {
    "TABLE":       {"CT", "DT"},   # CREATE TABLE + DROP TABLE
    "JOIN_INDEX":  {"CT", "DT"},   # Join indexes use TABLE rights
    "HASH_INDEX":  {"CT", "DT"},   # Hash indexes use TABLE rights
    "INDEX":       {"CT", "DT"},   # Secondary indexes use TABLE rights
    "VIEW":        {"CV", "DV"},   # CREATE VIEW + DROP VIEW
    "MACRO":       {"CM", "DM"},   # CREATE MACRO + DROP MACRO
    "PROCEDURE":   {"PC", "PD"},   # CREATE PROCEDURE + DROP PROCEDURE
    "FUNCTION":    {"CF", "DF"},   # CREATE FUNCTION + DROP FUNCTION
    "TRIGGER":     {"CG", "DG"},   # CREATE TRIGGER + DROP TRIGGER
}

# Maps ObjectType → the compound GRANT keyword that grants the
# required privileges (CREATE + DROP) in a single statement.
_GRANT_KEYWORDS: Dict[str, str] = {
    "TABLE":       "TABLE",
    "JOIN_INDEX":  "TABLE",      # JIX/HIX/SI use TABLE privilege
    "HASH_INDEX":  "TABLE",
    "INDEX":       "TABLE",
    "VIEW":        "VIEW",
    "MACRO":       "MACRO",
    "PROCEDURE":   "PROCEDURE",
    "FUNCTION":    "FUNCTION",
    "TRIGGER":     "TRIGGER",
}

# Human-readable labels for access right codes (for reporting)
_RIGHT_LABELS: Dict[str, str] = {
    "CT": "CREATE TABLE",    "DT": "DROP TABLE",
    "CV": "CREATE VIEW",     "DV": "DROP VIEW",
    "CM": "CREATE MACRO",    "DM": "DROP MACRO",
    "PC": "CREATE PROCEDURE","PD": "DROP PROCEDURE",
    "CF": "CREATE FUNCTION", "DF": "DROP FUNCTION",
    "CG": "CREATE TRIGGER",  "DG": "DROP TRIGGER",
}


# ---------------------------------------------------------------
# Result model
# ---------------------------------------------------------------

@dataclass
class PrivilegeCheckResult:
    """
    Outcome of the deployer privilege check.

    Attributes:
        passed:     True if all required privileges are in place.
        user:       The deploying user (from SESSION).
        missing:    Dict of database → list of missing right labels.
        script:     Generated prerequisite GRANT SQL (empty if passed).
        skipped_databases:  Databases skipped because the package
                            creates them (automatic creator rights).
        checked_databases:  Databases that were checked.
    """

    passed: bool = True
    user: str = ""
    missing: Dict[str, List[str]] = field(default_factory=dict)
    script: str = ""
    skipped_databases: List[str] = field(default_factory=list)
    checked_databases: List[str] = field(default_factory=list)


# ---------------------------------------------------------------
# Core check
# ---------------------------------------------------------------

def check_deployer_privileges(
    cursor: Any,
    parsed_ddls: list,
    created_databases: Set[str],
    package_name: str = "",
    environment: str = "",
) -> PrivilegeCheckResult:
    """
    Verify deployer privileges and generate prerequisite script
    if any are missing.

    Args:
        cursor:             Active Teradata cursor.
        parsed_ddls:        List of ParsedDDL objects from preflight.
        created_databases:  Set of database names being created by
                            this package (automatic creator rights —
                            no check needed).
        package_name:       Package name for the script header.
        environment:        Environment name for the script header.

    Returns:
        PrivilegeCheckResult — check passed if True, otherwise
        the .script attribute contains the prerequisite SQL.
    """
    result = PrivilegeCheckResult()

    # -- Get the deploying user's name --
    try:
        cursor.execute("SELECT USER")
        row = cursor.fetchone()
        result.user = row[0].strip() if row else "UNKNOWN"
    except Exception as e:
        logger.warning(
            "Could not determine deploying user: %s — "
            "generating full prerequisite script as precaution.",
            e,
        )
        result.user = "<deploying_user>"

    # -- Build required privileges per database --
    # Maps database_name → set of GRANT keywords needed.
    db_requirements: Dict[str, Set[str]] = defaultdict(set)

    # Also track object counts per database for the script comments
    db_object_counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    created_upper = {db.upper() for db in created_databases}

    for parsed in parsed_ddls:
        db_name = parsed.database_name
        obj_type = parsed.object_type.value if hasattr(
            parsed.object_type, 'value'
        ) else str(parsed.object_type)

        if not db_name:
            continue

        # Skip system/DCL objects (GRANT, ROLE, DATABASE, USER, PROFILE)
        # — these don't require object-creation rights on a target DB.
        if obj_type in ("GRANT", "ROLE", "DATABASE", "USER", "PROFILE",
                        "UNKNOWN"):
            continue

        grant_kw = _GRANT_KEYWORDS.get(obj_type)
        if grant_kw:
            db_requirements[db_name].add(grant_kw)
            db_object_counts[db_name][obj_type] += 1

    if not db_requirements:
        logger.info("No database-scoped objects to check privileges for.")
        result.passed = True
        return result

    # -- Partition databases into created-by-package vs existing --
    for db_name in sorted(db_requirements.keys()):
        if db_name.upper() in created_upper:
            result.skipped_databases.append(db_name)
        else:
            result.checked_databases.append(db_name)

    # -- For existing databases, check actual privileges --
    missing_by_db: Dict[str, Set[str]] = {}

    for db_name in result.checked_databases:
        needed_keywords = db_requirements[db_name]

        # Collect all access right codes we need to verify
        needed_codes = set()
        for kw in needed_keywords:
            # Find the object types that map to this keyword
            for obj_type, grant_kw in _GRANT_KEYWORDS.items():
                if grant_kw == kw:
                    needed_codes.update(_REQUIRED_RIGHTS.get(
                        obj_type, set()
                    ))
                    break  # One match per keyword is sufficient

        # Query existing rights
        existing_codes = _get_user_rights(
            cursor, db_name, result.user,
        )

        # Compute delta
        missing_codes = needed_codes - existing_codes
        if missing_codes:
            missing_by_db[db_name] = missing_codes

    # -- Build result --
    if missing_by_db:
        result.passed = False
        result.missing = {
            db: [_RIGHT_LABELS.get(c, c) for c in sorted(codes)]
            for db, codes in missing_by_db.items()
        }
        result.script = _generate_prerequisite_script(
            db_requirements=db_requirements,
            db_object_counts=db_object_counts,
            missing_by_db=missing_by_db,
            user=result.user,
            package_name=package_name,
            environment=environment,
        )
        logger.warning(
            "Deployer privilege check FAILED — %d database(s) "
            "have missing privileges. See prerequisite script.",
            len(missing_by_db),
        )
    else:
        result.passed = True
        if result.checked_databases:
            logger.info(
                "Deployer privileges verified for %d database(s).",
                len(result.checked_databases),
            )

    if result.skipped_databases:
        logger.info(
            "Skipped privilege check for %d database(s) created "
            "by this package: %s",
            len(result.skipped_databases),
            ", ".join(result.skipped_databases),
        )

    return result


def generate_full_prerequisite_script(
    parsed_ddls: list,
    user: str = "<deploying_user>",
    package_name: str = "",
    environment: str = "",
) -> str:
    """
    Generate the complete prerequisite GRANT script for all
    databases in the package, without checking existing rights.

    Useful as a standalone CLI command:
        python -m ddl_deployer prerequisites --source <package>

    Args:
        parsed_ddls:    List of ParsedDDL objects.
        user:           Target user for the GRANT statements.
        package_name:   Package name for the header.
        environment:    Environment name for the header.

    Returns:
        The prerequisite GRANT SQL as a string.
    """
    # Build requirements (same logic as check_deployer_privileges)
    db_requirements: Dict[str, Set[str]] = defaultdict(set)
    db_object_counts: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for parsed in parsed_ddls:
        db_name = parsed.database_name
        obj_type = parsed.object_type.value if hasattr(
            parsed.object_type, 'value'
        ) else str(parsed.object_type)

        if not db_name:
            continue
        if obj_type in ("GRANT", "ROLE", "DATABASE", "USER",
                        "PROFILE", "UNKNOWN"):
            continue

        grant_kw = _GRANT_KEYWORDS.get(obj_type)
        if grant_kw:
            db_requirements[db_name].add(grant_kw)
            db_object_counts[db_name][obj_type] += 1

    return _generate_prerequisite_script(
        db_requirements=db_requirements,
        db_object_counts=db_object_counts,
        missing_by_db=None,  # Generate for ALL, not just missing
        user=user,
        package_name=package_name,
        environment=environment,
    )


# ---------------------------------------------------------------
# Internal — database rights query
# ---------------------------------------------------------------

def _get_user_rights(
    cursor: Any,
    database_name: str,
    user_name: str,
) -> Set[str]:
    """
    Query existing access rights for a user on a database.

    Returns the set of AccessRight codes (e.g. {'CT', 'DT', 'CV'})
    that the user holds on the specified database.

    Falls back to an empty set if the query fails (e.g. no
    SELECT on DBC.AllRightsV), which causes the privilege check
    to report all rights as missing — a safe default that
    results in the full prerequisite script being generated.

    Args:
        cursor:         Active Teradata cursor.
        database_name:  Database to check.
        user_name:      Deploying user.

    Returns:
        Set of AccessRight code strings.
    """
    try:
        cursor.execute(
            "SELECT AccessRight "
            "FROM DBC.AllRightsV "
            "WHERE DatabaseName = ? "
            "AND UserName = ? "
            "AND TableName = 'All'",
            [database_name, user_name],
        )
        rows = cursor.fetchall()
        return {row[0].strip() for row in rows if row and row[0]}
    except Exception as e:
        logger.warning(
            "Could not query rights for '%s' on '%s': %s — "
            "assuming no rights (will generate prerequisite script).",
            user_name, database_name, e,
        )
        return set()


# ---------------------------------------------------------------
# Internal — prerequisite script generator
# ---------------------------------------------------------------

# Preferred ordering of GRANT keywords in the output
_GRANT_ORDER = [
    "TABLE", "VIEW", "MACRO", "PROCEDURE", "FUNCTION", "TRIGGER",
]


def _generate_prerequisite_script(
    db_requirements: Dict[str, Set[str]],
    db_object_counts: Dict[str, Dict[str, int]],
    missing_by_db: Optional[Dict[str, Set[str]]],
    user: str,
    package_name: str = "",
    environment: str = "",
) -> str:
    """
    Generate the prerequisite GRANT SQL.

    Args:
        db_requirements:   database → set of GRANT keywords.
        db_object_counts:  database → {obj_type → count}.
        missing_by_db:     database → set of missing access codes.
                           If None, generates for ALL databases.
        user:              Target user for GRANT TO.
        package_name:      Package name for the header.
        environment:       Environment name for the header.

    Returns:
        Multi-line SQL string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pkg_line = f"-- Package:     {package_name}" if package_name else ""
    env_line = f"-- Environment: {environment}" if environment else ""

    lines = [
        "-- ================================================================",
        "-- SHIPS Deployer: Pre-requisite Privileges",
    ]
    if pkg_line:
        lines.append(pkg_line)
    if env_line:
        lines.append(env_line)
    lines.extend([
        f"-- Generated:   {now}",
        "--",
        "-- Run this as System Administrator before deploying.",
        "-- ================================================================",
        "",
    ])

    # Determine which databases to include
    if missing_by_db is not None:
        # Only databases with missing rights
        target_dbs = sorted(missing_by_db.keys())
    else:
        # All databases
        target_dbs = sorted(db_requirements.keys())

    for db_name in target_dbs:
        keywords = db_requirements.get(db_name, set())
        counts = db_object_counts.get(db_name, {})

        # Build the object count comment
        count_parts = []
        for obj_type in sorted(counts.keys()):
            c = counts[obj_type]
            label = obj_type.lower()
            if c > 1:
                label += "s"
            count_parts.append(f"{c} {label}")
        comment = ", ".join(count_parts) if count_parts else "objects"

        # Order the GRANT keywords deterministically
        ordered_kws = [
            kw for kw in _GRANT_ORDER if kw in keywords
        ]
        # Include any keywords not in the preferred order
        ordered_kws.extend(
            sorted(kw for kw in keywords if kw not in _GRANT_ORDER)
        )

        kw_str = ", ".join(ordered_kws)

        lines.append(f"-- {db_name}: {comment}")
        lines.append(
            f"GRANT {kw_str} ON {db_name} TO {user};"
        )
        lines.append("")

    return "\n".join(lines)
