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

        from database_package_deployer.privilege_check import check_deployer_privileges

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
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

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
    "TABLE": {"CT", "DT"},  # CREATE TABLE + DROP TABLE
    "JOIN_INDEX": {"CT", "DT"},  # Join indexes use TABLE rights
    "HASH_INDEX": {"CT", "DT"},  # Hash indexes use TABLE rights
    "INDEX": {"CT", "DT"},  # Secondary indexes use TABLE rights
    "VIEW": {"CV", "DV"},  # CREATE VIEW + DROP VIEW
    "MACRO": {"CM", "DM"},  # CREATE MACRO + DROP MACRO
    "PROCEDURE": {"PC", "PD"},  # CREATE PROCEDURE + DROP PROCEDURE
    "FUNCTION": {"CF", "DF"},  # CREATE FUNCTION + DROP FUNCTION
    "TRIGGER": {"CG", "DG"},  # CREATE TRIGGER + DROP TRIGGER
    # SQLJ.INSTALL_JAR / SQLJ.REPLACE_JAR create or replace Java external
    # procedure metadata in the target database. Teradata reports missing
    # access as CREATE/ALTER EXTERNAL PROCEDURE rather than as JAR rights.
    "JAR": {"CE", "AE"},  # CREATE/ALTER EXTERNAL PROCEDURE
}

# Maps ObjectType → the compound GRANT keyword that grants the
# required privileges (CREATE + DROP) in a single statement.
_GRANT_KEYWORDS: Dict[str, str] = {
    "TABLE": "TABLE",
    "JOIN_INDEX": "TABLE",  # JIX/HIX/SI use TABLE privilege
    "HASH_INDEX": "TABLE",
    "INDEX": "TABLE",
    "VIEW": "VIEW",
    "MACRO": "MACRO",
    "PROCEDURE": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "TRIGGER": "TRIGGER",
    "JAR": "CREATE EXTERNAL PROCEDURE, ALTER EXTERNAL PROCEDURE",
}

# Human-readable labels for access right codes (for reporting)
_RIGHT_LABELS: Dict[str, str] = {
    "CT": "CREATE TABLE",
    "DT": "DROP TABLE",
    "CV": "CREATE VIEW",
    "DV": "DROP VIEW",
    "CM": "CREATE MACRO",
    "DM": "DROP MACRO",
    "PC": "CREATE PROCEDURE",
    "PD": "DROP PROCEDURE",
    "CF": "CREATE FUNCTION",
    "DF": "DROP FUNCTION",
    "CG": "CREATE TRIGGER",
    "DG": "DROP TRIGGER",
    "CE": "CREATE EXTERNAL PROCEDURE",
    "AE": "ALTER EXTERNAL PROCEDURE",
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


_DATABASE_STATEMENT_RE = re.compile(
    # ``{{TOKEN}}\w*`` keeps tokenised database names with a literal
    # suffix as one identifier — ``DATABASE {{DB_PREFIX}}_Domain;`` must
    # match in full instead of failing to match at all (#454).
    r"""
    (?:^|;)\s*DATABASE\s+
    ("[^"]+"|\{\{[A-Za-z_][A-Za-z0-9_]*\}\}\w*|[A-Za-z_][A-Za-z0-9_]*)
    \s*;
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _strip_identifier_quotes(name: str) -> str:
    """Return an identifier without surrounding double quotes."""
    name = (name or "").strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1]
    return name


def _infer_jar_target_database(ddl_text: str) -> str:
    """Infer the target database for a SQLJ JAR install script.

    SQLJ.INSTALL_JAR / SQLJ.REPLACE_JAR scripts are executed using the
    current database context, commonly established by a leading
    ``DATABASE db_name;`` statement.  The generic statement parser classifies
    the script as ObjectType.JAR but does not give it a normal
    Database.ObjectName, so the deployer privilege check must infer the
    target database from that DATABASE statement.
    """
    match = _DATABASE_STATEMENT_RE.search(ddl_text or "")
    if not match:
        return ""
    return _strip_identifier_quotes(match.group(1))


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
        parsed_ddls:        List of ParsedStatement objects from preflight.
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
    db_object_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    created_upper = {db.upper() for db in created_databases}

    # Track databases that contain at least one IDEMPOTENT_DEPLOY table —
    # those require DROP TABLE even if the CREATE right is sufficient for
    # SKIP_IF_EXISTS objects.  A missing DT on an IDEMPOTENT_DEPLOY table
    # causes Error 3803 (table already exists) at runtime because the
    # deployer cannot drop the old table before recreating it.
    idempotent_table_dbs: Set[str] = set()

    for parsed in parsed_ddls:
        db_name = parsed.database_name
        obj_type = (
            parsed.object_type.value
            if hasattr(parsed.object_type, "value")
            else str(parsed.object_type)
        )

        if not db_name and obj_type == "JAR":
            db_name = _infer_jar_target_database(getattr(parsed, "ddl_text", ""))

        if not db_name:
            continue

        # Skip system/DCL objects (GRANT, ROLE, DATABASE, USER, PROFILE)
        # — these don't require object-creation rights on a target DB.
        if obj_type in ("GRANT", "ROLE", "DATABASE", "USER", "PROFILE", "UNKNOWN"):
            continue

        grant_kw = _GRANT_KEYWORDS.get(obj_type)
        if grant_kw:
            db_requirements[db_name].add(grant_kw)
            db_object_counts[db_name][obj_type] += 1

        # Record databases with IDEMPOTENT_DEPLOY tables so we can verify
        # DT is granted even when the deploy strategy implies it is needed.
        deploy_strategy = getattr(parsed, "deploy_strategy", None)
        strategy_str = (
            deploy_strategy.value
            if hasattr(deploy_strategy, "value")
            else str(deploy_strategy or "")
        )
        if obj_type == "TABLE" and "IDEMPOTENT" in strategy_str.upper():
            idempotent_table_dbs.add(db_name)

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
                    needed_codes.update(_REQUIRED_RIGHTS.get(obj_type, set()))
                    break  # One match per keyword is sufficient

        # Query existing rights
        existing_codes = _get_user_rights(
            cursor,
            db_name,
            result.user,
        )

        # Compute delta
        missing_codes = needed_codes - existing_codes

        # Extra check: if this database has IDEMPOTENT_DEPLOY tables,
        # DROP TABLE (DT) is unconditionally required — the deployer must
        # drop the existing table before recreating it.  CT alone is
        # insufficient and will cause Error 3803 at runtime.
        if db_name in idempotent_table_dbs and "DT" not in existing_codes:
            missing_codes.add("DT")
            logger.warning(
                "Database '%s' has IDEMPOTENT_DEPLOY tables but deploying "
                "user '%s' does not hold DROP TABLE (DT) — adding to "
                "missing privileges. Without DT the deployer cannot replace "
                "existing tables and will fail with Error 3803.",
                db_name,
                result.user,
            )

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
            "Skipped privilege check for %d database(s) created by this package: %s",
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
        python -m database_package_deployer prerequisites --source <package>

    Args:
        parsed_ddls:    List of ParsedStatement objects.
        user:           Target user for the GRANT statements.
        package_name:   Package name for the header.
        environment:    Environment name for the header.

    Returns:
        The prerequisite GRANT SQL as a string.
    """
    # Build requirements (same logic as check_deployer_privileges)
    db_requirements: Dict[str, Set[str]] = defaultdict(set)
    db_object_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for parsed in parsed_ddls:
        db_name = parsed.database_name
        obj_type = (
            parsed.object_type.value
            if hasattr(parsed.object_type, "value")
            else str(parsed.object_type)
        )

        if not db_name and obj_type == "JAR":
            db_name = _infer_jar_target_database(getattr(parsed, "ddl_text", ""))

        if not db_name:
            continue
        if obj_type in ("GRANT", "ROLE", "DATABASE", "USER", "PROFILE", "UNKNOWN"):
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
    Query all effective access rights for a user on a database.

    Combines direct user grants (DBC.AllRightsV) with rights inherited
    via roles (DBC.AllRoleRightsV joined through DBC.RoleMembersV).
    Both sources are needed because a deploying service account commonly
    holds DDL rights through a role rather than as direct database grants.

    Falls back to an empty set if both queries fail (e.g. no SELECT on
    DBC views), which causes the privilege check to report all rights as
    missing — a safe default that generates the full prerequisite script.

    Args:
        cursor:         Active Teradata cursor.
        database_name:  Database to check.
        user_name:      Deploying user.

    Returns:
        Set of AccessRight code strings combining direct and role grants.
    """
    rights: Set[str] = set()

    # -- Direct grants on the database --
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
        rights.update(row[0].strip() for row in rows if row and row[0])
    except Exception as e:
        logger.warning(
            "Could not query direct rights for '%s' on '%s': %s",
            user_name,
            database_name,
            e,
        )

    # -- Role-based grants on the database --
    # A deploying service account often holds DDL rights through a role
    # rather than as direct grants.  DBC.AllRightsV does NOT include
    # role-inherited rights, so we must also check DBC.AllRoleRightsV.
    try:
        cursor.execute(
            "SELECT RR.AccessRight "
            "FROM DBC.AllRoleRightsV AS RR "
            "JOIN DBC.RoleMembersV AS RM "
            "  ON RM.RoleName = RR.RoleName "
            "WHERE RR.DatabaseName = ? "
            "AND   RM.Grantee      = ? "
            "AND   RR.TableName    = 'All'",
            [database_name, user_name],
        )
        rows = cursor.fetchall()
        rights.update(row[0].strip() for row in rows if row and row[0])
    except Exception as e:
        logger.warning(
            "Could not query role-based rights for '%s' on '%s': %s — "
            "role-inherited privileges will not be considered.",
            user_name,
            database_name,
            e,
        )

    if not rights:
        logger.warning(
            "No rights found for '%s' on '%s' (direct or via roles) — "
            "assuming no rights (will generate prerequisite script).",
            user_name,
            database_name,
        )

    return rights


# ---------------------------------------------------------------
# Internal — prerequisite script generator
# ---------------------------------------------------------------

# Preferred ordering of GRANT keywords in the output
_GRANT_ORDER = [
    "TABLE",
    "VIEW",
    "MACRO",
    "PROCEDURE",
    "FUNCTION",
    "TRIGGER",
    "CREATE EXTERNAL PROCEDURE, ALTER EXTERNAL PROCEDURE",
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
    lines.extend(
        [
            f"-- Generated:   {now}",
            "--",
            "-- Run this as System Administrator before deploying.",
            "-- ================================================================",
            "",
        ]
    )

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
        ordered_kws = [kw for kw in _GRANT_ORDER if kw in keywords]
        # Include any keywords not in the preferred order
        ordered_kws.extend(sorted(kw for kw in keywords if kw not in _GRANT_ORDER))

        kw_str = ", ".join(ordered_kws)

        lines.append(f"-- {db_name}: {comment}")
        lines.append(f"GRANT {kw_str} ON {db_name} TO {user};")
        lines.append("")

    return "\n".join(lines)
