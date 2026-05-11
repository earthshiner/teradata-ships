"""
preflight.py — Pre-flight validation for DDL deployments.

Runs lightweight checks BEFORE any DDL is executed:

    1. DDL parsing      — Can every file be parsed and classified?
    2. Database exists   — Do all target databases exist?
    3. Access rights     — Does the user have the required CREATE/DROP
                          rights on each target database?
    4. Perm space        — Is there free permanent space available
                          in each target database?
    5. JAR alias coverage — Every PROCEDURE LANGUAGE JAVA in the
                          package references a JAR alias. The alias
                          must be installed by some jar_install
                          script in the same package. Catches the
                          "binary in payload but unused" failure
                          mode where a procedure ships without its
                          install script.

Pre-flight is mandatory and runs automatically before every
deployment. It catches the most common failures (wrong permissions,
missing database, no space, missing JAR install) without touching
any data.

Note on access rights:
    Uses DBC.AllRightsV which resolves both direct grants and
    role-based grants. If DBC.AllRightsV is unavailable (older
    Teradata versions), falls back to DBC.AccessRightsV which
    only captures direct grants — role-based rights may not
    be detected, producing false negatives.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from database_package_deployer.statement_parser import parse_statement_file
from database_package_deployer.models import (
    ObjectType,
    ParsedStatement,
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
        Tuple of (PreflightResult, list_of_ParsedStatement).
        The ParsedStatement list only includes successfully parsed files.
    """
    checks = []
    parsed_ddls = []
    failed_files = []
    databases: Set[str] = set()
    object_counts: Dict[str, int] = {}

    # -- Phase 1: Parse all DDL files --
    # Track which databases are being created in this package
    # so we don't fail preflight for databases that don't exist yet.
    databases_being_created: Set[str] = set()

    for ddl_file in ddl_files:
        try:
            parsed = parse_statement_file(ddl_file)
            parsed_ddls.append(parsed)

            # Skip system-scope and DCL objects from database checks.
            # They don't have a real database qualifier.
            _SKIP_DB_CHECK_TYPES = {
                ObjectType.MAP,
                ObjectType.ROLE,
                ObjectType.PROFILE,
                ObjectType.AUTHORIZATION,
                ObjectType.FOREIGN_SERVER,
                ObjectType.GRANT,
                ObjectType.REVOKE,
            }

            if parsed.object_type not in _SKIP_DB_CHECK_TYPES:
                if parsed.database_name:
                    databases.add(parsed.database_name)

            # Track CREATE DATABASE objects — these will be created
            # by this package, so they won't exist during preflight.
            if parsed.object_type == ObjectType.DATABASE:
                databases_being_created.add(parsed.object_name)
                logger.debug(
                    "Database '%s' will be created by this package.",
                    parsed.object_name,
                )

            # Count by object type
            type_key = parsed.object_type.value
            object_counts[type_key] = object_counts.get(type_key, 0) + 1

            logger.debug(
                "Parsed: %s → %s %s [%s]",
                os.path.basename(ddl_file),
                parsed.object_type.value,
                parsed.qualified_name,
                parsed.deploy_intent.value if parsed.deploy_intent else "N/A",
            )

            checks.append(
                PreflightCheck(
                    check_name="ddl_parse",
                    passed=True,
                    database=parsed.database_name or "(system)",
                    message=(
                        f"Parsed {os.path.basename(ddl_file)}: "
                        f"{parsed.object_type.value} {parsed.qualified_name}"
                        + (" (MULTISET injected)" if parsed.multiset_injected else "")
                    ),
                )
            )

            if parsed.multiset_injected:
                checks.append(
                    PreflightCheck(
                        check_name="multiset_injection",
                        passed=True,
                        database=parsed.database_name,
                        message=(
                            f"{parsed.qualified_name}: MULTISET auto-injected "
                            f"(CREATE TABLE had no SET/MULTISET qualifier)."
                        ),
                        severity="WARNING",
                    )
                )

        except (ValueError, FileNotFoundError) as e:
            failed_files.append(ddl_file)
            checks.append(
                PreflightCheck(
                    check_name="ddl_parse",
                    passed=False,
                    database="UNKNOWN",
                    message=f"Failed to parse {os.path.basename(ddl_file)}: {e}",
                )
            )

    # -- Phase 2: Check databases exist --
    # Skip databases that will be created by this package.
    for db_name in sorted(databases):
        if not db_name:
            continue  # System-scope objects have no database

        if db_name in databases_being_created:
            logger.info(
                "Database '%s' is being created by this package — "
                "skipping existence check.",
                db_name,
            )
            checks.append(
                PreflightCheck(
                    check_name="database_exists",
                    passed=True,
                    database=db_name,
                    message=(f"Database '{db_name}' will be created by this package."),
                    severity="INFO",
                )
            )
            continue

        exists = _database_exists(cursor, db_name)
        logger.info(
            "Database '%s' %s.",
            db_name,
            "exists" if exists else "does NOT exist",
        )
        checks.append(
            PreflightCheck(
                check_name="database_exists",
                passed=exists,
                database=db_name,
                message=(
                    f"Database '{db_name}' exists."
                    if exists
                    else f"Database '{db_name}' does NOT exist."
                ),
            )
        )

    # -- Phase 3: Check access rights per database --
    # Determine which rights are needed per database.
    # Skip databases being created (rights won't exist yet) and
    # empty database names (system-scope objects).
    db_required_rights = _collect_required_rights(parsed_ddls)

    for db_name, rights in sorted(db_required_rights.items()):
        if not db_name:
            continue
        if db_name in databases_being_created:
            logger.info(
                "Database '%s' is being created — skipping access rights check.",
                db_name,
            )
            checks.append(
                PreflightCheck(
                    check_name="access_rights",
                    passed=True,
                    database=db_name,
                    message=(
                        f"Access rights for '{db_name}' will be "
                        f"established after creation."
                    ),
                    severity="INFO",
                )
            )
            continue
        right_checks = _check_access_rights(cursor, db_name, rights)
        checks.extend(right_checks)

    # -- Phase 4: Check perm space per database --
    # Only check databases that actually need tables/JIs
    # (space-consuming objects). Skip databases being created.
    space_databases = {
        p.database_name
        for p in parsed_ddls
        if p.object_type
        in (ObjectType.TABLE, ObjectType.JOIN_INDEX, ObjectType.HASH_INDEX)
        and p.database_name
        and p.database_name not in databases_being_created
    }

    for db_name in sorted(space_databases):
        space_checks = _check_perm_space(cursor, db_name, warn_space_below_pct)
        checks.extend(space_checks)

    # -- Phase 5: JAR alias coverage --
    # PROCEDURE LANGUAGE JAVA bodies reference an installed JAR by
    # alias via ``EXTERNAL NAME 'jar_alias:com.x.Foo.bar'``. The
    # alias must be installed by some CALL SQLJ.INSTALL_JAR earlier
    # in the deploy. When the binary harvester landed (2026-05-04)
    # the install scripts started shipping with the package, but
    # without a coverage check a procedure could still ship without
    # its install script and silently fail at deploy time.
    jar_checks = _check_jar_alias_coverage(parsed_ddls)
    checks.extend(jar_checks)

    # -- Phase 6: Excess privilege check (GAP-010) --
    # Warn if the deploy account holds elevated rights beyond what is
    # needed.  WARNING only — does not block deployment.
    checks.extend(_check_excess_privilege(cursor))

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
        "Pre-flight complete: %d files parsed, %d databases, %d errors, %d warnings",
        len(parsed_ddls),
        len(databases),
        errors,
        warnings,
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
            "SELECT 1 FROM DBC.DatabasesV WHERE DatabaseName = ?", [database_name]
        )
        return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("Database existence check failed for '%s': %s", database_name, e)
        return False


# ---------------------------------------------------------------
# Internal — Access rights checks
# ---------------------------------------------------------------


def _collect_required_rights(
    parsed_ddls: List[ParsedStatement],
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

        # Skip system-scope objects and DCL — they don't have
        # a target database to check rights on.
        if not db or parsed.object_type in (
            ObjectType.MAP,
            ObjectType.ROLE,
            ObjectType.PROFILE,
            ObjectType.AUTHORIZATION,
            ObjectType.FOREIGN_SERVER,
            ObjectType.GRANT,
            ObjectType.REVOKE,
            ObjectType.DATABASE,
            ObjectType.USER,
        ):
            continue

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
        checks.append(
            PreflightCheck(
                check_name="rights_view",
                passed=True,
                database=database_name,
                message=(
                    "Using DBC.AccessRightsV (direct grants only). "
                    "Role-based grants may not be detected. "
                    "False negatives possible."
                ),
                severity="WARNING",
            )
        )

    # Query the user's granted rights on this database
    granted_rights = _get_granted_rights(cursor, database_name, rights_view)

    for right_code, right_desc in sorted(required_rights):
        # Check if the right is granted (direct or via 'ALL')
        has_right = right_code.strip() in granted_rights or "AL" in granted_rights

        checks.append(
            PreflightCheck(
                check_name=f"access_{right_code.strip().lower()}",
                passed=has_right,
                database=database_name,
                message=(
                    f"{right_desc} ({right_code.strip()}) granted on '{database_name}'."
                    if has_right
                    else f"{right_desc} ({right_code.strip()}) NOT granted on '{database_name}'. "
                    f"Deployment will fail for objects requiring this right."
                ),
            )
        )

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
        return ("DBC.AllRightsV", False)
    except Exception:
        return ("DBC.AccessRightsV", True)


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
            [database_name],
        )
        return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logger.warning(
            "Rights check failed for '%s' via %s: %s", database_name, rights_view, e
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
            [database_name],
        )
        row = cursor.fetchone()

        if row is None or row[0] is None:
            checks.append(
                PreflightCheck(
                    check_name="perm_space",
                    passed=False,
                    database=database_name,
                    message=(
                        f"Could not retrieve perm space for '{database_name}'. "
                        f"Database may not exist or user lacks access."
                    ),
                )
            )
            return checks

        max_perm = row[0]
        current_perm = row[1] or 0
        free_perm = max_perm - current_perm

        if max_perm == 0:
            checks.append(
                PreflightCheck(
                    check_name="perm_space",
                    passed=False,
                    database=database_name,
                    message=(
                        f"Database '{database_name}' has zero MaxPerm allocated. "
                        f"No objects can be created."
                    ),
                )
            )
            return checks

        free_pct = (free_perm / max_perm) * 100 if max_perm > 0 else 0

        # Format sizes for human readability
        free_str = _format_bytes(free_perm)
        max_str = _format_bytes(max_perm)

        if free_perm <= 0:
            checks.append(
                PreflightCheck(
                    check_name="perm_space",
                    passed=False,
                    database=database_name,
                    message=(
                        f"Database '{database_name}' has NO free perm space "
                        f"({max_str} fully consumed). Cannot create objects."
                    ),
                )
            )
        elif free_pct < warn_below_pct:
            checks.append(
                PreflightCheck(
                    check_name="perm_space",
                    passed=True,
                    database=database_name,
                    message=(
                        f"Database '{database_name}' perm space low: "
                        f"{free_str} free of {max_str} ({free_pct:.1f}% free)."
                    ),
                    severity="WARNING",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    check_name="perm_space",
                    passed=True,
                    database=database_name,
                    message=(
                        f"Database '{database_name}' perm space OK: "
                        f"{free_str} free of {max_str} ({free_pct:.1f}% free)."
                    ),
                )
            )

    except Exception as e:
        logger.warning("Perm space check failed for '%s': %s", database_name, e)
        checks.append(
            PreflightCheck(
                check_name="perm_space",
                passed=True,  # Don't block on check failure
                database=database_name,
                message=(
                    f"Could not check perm space for '{database_name}': {e}. "
                    f"Proceeding without space validation."
                ),
                severity="WARNING",
            )
        )

    return checks


def _format_bytes(num_bytes: int) -> str:
    """
    Format a byte count as a human-readable string.

    Args:
        num_bytes: Byte count.

    Returns:
        Formatted string (e.g. '1.5 GB', '256 MB').
    """
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} EB"


# ---------------------------------------------------------------
# Internal — JAR alias coverage check (Phase 5)
# ---------------------------------------------------------------
#
# Patterns mirror the ones in
# ``td_release_packager.classifier.extract_externals`` (deliberately
# duplicated rather than imported — the deployer is a runtime
# component embedded inside packages and cannot import the build
# tool).

# CALL SQLJ.INSTALL_JAR('CJ!path/to/X.jar', 'jar_alias', 0);
#                        ^arg1: binary path  ^arg2: alias
_INSTALL_JAR_RE = re.compile(
    r"CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(\s*"
    r"'[^']*'\s*,\s*'([^']+)'",
    re.IGNORECASE,
)

# EXTERNAL NAME 'jar_alias:com.x.Foo.bar'
# Captures the alias before the first colon.
_EXTERNAL_NAME_JAR_RE = re.compile(
    r"EXTERNAL\s+NAME\s+'([^':]+):[^']+'",
    re.IGNORECASE,
)

# LANGUAGE JAVA marker — Teradata's keyword for Java external
# routines. Used to scope the alias-extraction to procedures that
# actually need a JAR (versus LANGUAGE C / SQL which use other
# external-reference shapes).
_LANGUAGE_JAVA_RE = re.compile(r"\bLANGUAGE\s+JAVA\b", re.IGNORECASE)


def _extract_installed_aliases(parsed_ddls: List[ParsedStatement]) -> Set[str]:
    """Aliases registered by SQLJ.INSTALL_JAR / REPLACE_JAR scripts.

    Walks parsed DDLs of type ``ObjectType.JAR`` and pulls the
    second argument from each ``CALL SQLJ.INSTALL_JAR(...)``. Returns
    the upper-cased alias set so the lookup is case-insensitive
    (Teradata identifier rules).
    """
    aliases: Set[str] = set()
    for parsed in parsed_ddls:
        if parsed.object_type != ObjectType.JAR:
            continue
        for match in _INSTALL_JAR_RE.finditer(parsed.ddl_text):
            aliases.add(match.group(1).upper())
    return aliases


def _extract_referenced_aliases(
    parsed_ddls: List[ParsedStatement],
) -> List[tuple]:
    """JAR aliases referenced by Java procedures.

    Returns a list of ``(parsed_ddl, alias)`` tuples — one per
    EXTERNAL NAME reference found in a procedure whose body
    contains LANGUAGE JAVA. The original ParsedStatement is kept so the
    failing check can name the offending file.
    """
    references: List[tuple] = []
    for parsed in parsed_ddls:
        if parsed.object_type != ObjectType.PROCEDURE:
            continue
        if not _LANGUAGE_JAVA_RE.search(parsed.ddl_text):
            continue
        for match in _EXTERNAL_NAME_JAR_RE.finditer(parsed.ddl_text):
            references.append((parsed, match.group(1).upper()))
    return references


def _check_jar_alias_coverage(
    parsed_ddls: List[ParsedStatement],
) -> List[PreflightCheck]:
    """Verify every Java procedure's JAR alias is installed in-package.

    The check is local to the package: it does NOT consult the live
    target. A pre-existing JAR on the target satisfies the runtime,
    but this preflight assumes the package is meant to be self-
    contained (carry its own install script). If a project chooses
    to rely on a pre-installed JAR, the rule fires and the operator
    can either add the install script or stub it out — either way
    the relationship is now visible in the report.

    Args:
        parsed_ddls: All successfully-parsed DDL files in the package.

    Returns:
        One PreflightCheck per Java procedure. Coverage failures are
        ERROR severity so the deploy is gated; passes are recorded
        as INFO so the report shows the jar_install→procedure pairing
        explicitly.
    """
    references = _extract_referenced_aliases(parsed_ddls)
    if not references:
        return []  # No Java procedures — rule is silently inactive.

    installed = _extract_installed_aliases(parsed_ddls)
    checks: List[PreflightCheck] = []

    for parsed, alias in references:
        filename = os.path.basename(parsed.file_path)
        if alias in installed:
            checks.append(
                PreflightCheck(
                    check_name="jar_alias_coverage",
                    passed=True,
                    database=parsed.database_name or "(system)",
                    message=(
                        f"{filename}: JAR alias '{alias}' is installed "
                        f"by a CALL SQLJ.INSTALL_JAR script in this "
                        f"package."
                    ),
                    severity="INFO",
                )
            )
        else:
            installed_summary = ", ".join(sorted(installed)) if installed else "(none)"
            checks.append(
                PreflightCheck(
                    check_name="jar_alias_coverage",
                    passed=False,
                    database=parsed.database_name or "(system)",
                    message=(
                        f"{filename}: PROCEDURE LANGUAGE JAVA references "
                        f"JAR alias '{alias}' but no jar_install script "
                        f"in this package installs that alias. Installed "
                        f"aliases: {installed_summary}. Either add the "
                        f"corresponding CALL SQLJ.INSTALL_JAR script to "
                        f"the package, or remove this procedure if the "
                        f"JAR is meant to be installed by another release."
                    ),
                )
            )

    return checks


# ---------------------------------------------------------------
# Excess privilege check (GAP-010)
# ---------------------------------------------------------------


def _check_excess_privilege(cursor) -> List[PreflightCheck]:
    """Warn if the deploying user holds elevated rights beyond what is needed.

    Queries ``DBC.UserRightsV`` for rights that the deploy account should
    not hold: GRANT OPTION (GD), rights on DBC itself, SYSTEM_ADMIN (SA),
    CREATE ACCESS MACRO (CA), or ALL rights on any database.

    This is a WARNING-level check — it does not block deployment but flags
    the excess for a security review.

    Args:
        cursor: Active Teradata database cursor.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    _ELEVATED = frozenset({"GD", "SA", "CA", "AL"})

    try:
        cursor.execute(
            "SELECT"
            "     TRIM(AccessRight) AS AccessRight"
            "    ,TRIM(DatabaseName) AS DatabaseName"
            "    ,TRIM(TableName) AS TableName"
            " FROM DBC.UserRightsV"
            " WHERE UserName = USER"
            "   AND ("
            "        TRIM(AccessRight) IN ('GD', 'SA', 'CA', 'AL')"
            "     OR TRIM(DatabaseName) = 'DBC'"
            "   )"
        )
        rows = cursor.fetchall()
    except Exception as exc:
        logger.warning("excess_privilege: query failed (non-fatal): %s", exc)
        return []

    if not rows:
        return [
            PreflightCheck(
                check_name="excess_privilege",
                passed=True,
                database="(system)",
                message="excess_privilege — no elevated rights detected on deploy user.",
                severity="INFO",
            )
        ]

    findings = [f"{r[0]} ON {r[1]}" for r in rows]
    logger.warning(
        "excess_privilege: deploy user holds elevated rights: %s",
        ", ".join(findings),
    )
    return [
        PreflightCheck(
            check_name="excess_privilege",
            passed=True,  # WARNING — advisory only, does not block
            database="(system)",
            message=(
                "excess_privilege — deploy user holds elevated rights: "
                + ", ".join(findings)
                + ". Review whether these rights are necessary for deployment."
            ),
            severity="WARNING",
        )
    ]


# ---------------------------------------------------------------
# Package-level integrity checks (GAP-001)
# ---------------------------------------------------------------


def _sha256_of_file(file_path: str) -> str:
    """Compute the SHA-256 hex digest of a file.

    Args:
        file_path: Path to the file to hash.

    Returns:
        Lowercase hex digest string.
    """
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_package_hash(package_dir: str) -> List[PreflightCheck]:
    """Verify the release ZIP against its SHA-256 sidecar (GAP-001).

    Reads BUILD.json from *package_dir* to discover the ZIP filename,
    then locates the archive and its ``.sha256`` sidecar in the parent
    directory.  If the sidecar is absent or the computed hash does not
    match the recorded value, an ERROR-level check is returned.

    The check is silently skipped (no finding emitted) when the ZIP is
    not present beside the extracted package directory — this happens
    when a DBA extracts the archive and deletes the original ZIP before
    running the deploy.  The sidecar-absent and hash-mismatch cases
    always produce an ERROR.

    Args:
        package_dir: Path to the extracted package directory (which
                     contains BUILD.json).

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        logger.debug(
            "package_hash: BUILD.json not found in '%s' — skipping check.", package_dir
        )
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("package_hash: could not read BUILD.json: %s", exc)
        return []

    package_filename = manifest.get("package_filename", "")
    if not package_filename:
        logger.debug(
            "package_hash: package_filename absent from BUILD.json — skipping."
        )
        return []

    zip_path = Path(package_dir).parent / package_filename
    if not zip_path.exists():
        logger.debug(
            "package_hash: archive '%s' not found beside package directory — skipping.",
            zip_path,
        )
        return []

    # Sidecar is the ZIP path with '.sha256' appended (not replacing the extension).
    sidecar_path = zip_path.parent / (zip_path.name + ".sha256")
    if not sidecar_path.exists():
        logger.error("package_hash: SHA-256 sidecar not found: %s", sidecar_path)
        return [
            PreflightCheck(
                check_name="package_hash",
                passed=False,
                database="(package)",
                message=(
                    f"package_hash — SHA-256 sidecar not found: {sidecar_path}. "
                    f"Ensure the .sha256 file is transferred alongside the package archive."
                ),
                severity="ERROR",
            )
        ]

    # Parse sidecar — handles single-column and two-column (sha256sum) formats.
    try:
        sidecar_text = sidecar_path.read_text(encoding="utf-8").strip()
        expected_hash = sidecar_text.split()[0]
    except (OSError, IndexError) as exc:
        logger.error("package_hash: could not read sidecar '%s': %s", sidecar_path, exc)
        return [
            PreflightCheck(
                check_name="package_hash",
                passed=False,
                database="(package)",
                message=(
                    f"package_hash — could not parse sidecar "
                    f"'{sidecar_path.name}': {exc}"
                ),
                severity="ERROR",
            )
        ]

    actual_hash = _sha256_of_file(str(zip_path))

    if actual_hash != expected_hash:
        logger.error(
            "package_hash: hash mismatch for '%s' — expected %s…, got %s…",
            zip_path.name,
            expected_hash[:12],
            actual_hash[:12],
        )
        return [
            PreflightCheck(
                check_name="package_hash",
                passed=False,
                database="(package)",
                message=(
                    f"package_hash — hash mismatch for '{zip_path.name}' "
                    f"(expected {expected_hash[:12]}…, got {actual_hash[:12]}…). "
                    f"The archive may have been corrupted or tampered with in transit."
                ),
                severity="ERROR",
            )
        ]

    logger.info("package_hash: '%s' verified OK (%s…)", zip_path.name, actual_hash[:12])
    return [
        PreflightCheck(
            check_name="package_hash",
            passed=True,
            database="(package)",
            message=(
                f"package_hash — '{zip_path.name}' SHA-256 verified OK "
                f"({actual_hash[:12]}…)."
            ),
            severity="INFO",
        )
    ]


# ---------------------------------------------------------------
# Environment lock check (GAP-002)
# ---------------------------------------------------------------


def check_env_lock(package_dir: str, deployed_env: str) -> List[PreflightCheck]:
    """Verify the package's target environment matches the deployment target (GAP-002).

    Reads ``target_env`` from BUILD.json and compares it to *deployed_env*
    (the ``--env`` flag supplied to the Ship command).  A mismatch — or a
    missing ``target_env`` field in older packages — is an ERROR that
    prevents any DDL from executing.

    The check is skipped silently when *deployed_env* is empty or ``None``,
    allowing deployments that do not specify ``--env`` to proceed without
    enforcement.

    Args:
        package_dir:  Path to the extracted package directory (contains BUILD.json).
        deployed_env: Target environment supplied by the operator (e.g. ``'PRD'``).
                      Pass an empty string or ``None`` to skip the check.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    if not deployed_env:
        logger.debug("env_lock: no --env supplied — skipping environment lock check.")
        return []

    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        logger.debug("env_lock: BUILD.json not found in '%s' — skipping.", package_dir)
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("env_lock: could not read BUILD.json: %s", exc)
        return []

    target_env = manifest.get("target_env", "")
    if not target_env:
        logger.error(
            "env_lock: 'target_env' absent from BUILD.json — package must be rebuilt "
            "with a version of SHIPS that stamps environment lock information."
        )
        return [
            PreflightCheck(
                check_name="env_lock",
                passed=False,
                database="(package)",
                message=(
                    "env_lock — 'target_env' is absent from BUILD.json. "
                    "Rebuild this package with SHIPS v2+ to stamp the environment "
                    "lock field, or remove --env from the Ship command to skip "
                    "this check."
                ),
                severity="ERROR",
            )
        ]

    if target_env.upper() != deployed_env.upper():
        logger.error(
            "env_lock: package built for '%s', attempted deployment to '%s'.",
            target_env,
            deployed_env,
        )
        return [
            PreflightCheck(
                check_name="env_lock",
                passed=False,
                database="(package)",
                message=(
                    f"env_lock — package built for '{target_env}', "
                    f"cannot deploy to '{deployed_env}'. "
                    f"Ensure you are using the correct package for this environment."
                ),
                severity="ERROR",
            )
        ]

    logger.info("env_lock: environment '%s' verified OK.", target_env)
    return [
        PreflightCheck(
            check_name="env_lock",
            passed=True,
            database="(package)",
            message=f"env_lock — package environment '{target_env}' matches target '{deployed_env}'.",
            severity="INFO",
        )
    ]


# ---------------------------------------------------------------
# Change reference check (GAP-004)
# ---------------------------------------------------------------


def check_change_ref_present(package_dir: str) -> List[PreflightCheck]:
    """Verify a change ticket reference is present when required (GAP-004).

    Reads ``require_change_ref`` and ``change_ref`` from BUILD.json.
    When ``require_change_ref`` is True and ``change_ref`` is null or
    absent, an ERROR is returned.

    When ``require_change_ref`` is False or absent, the check passes
    regardless of whether ``change_ref`` is set.

    Args:
        package_dir: Path to the extracted package directory.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        logger.debug("change_ref_present: BUILD.json not found — skipping.")
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("change_ref_present: could not read BUILD.json: %s", exc)
        return []

    require = manifest.get("require_change_ref", False)
    if not require:
        logger.debug(
            "change_ref_present: require_change_ref=false for this environment — skipping."
        )
        return []

    change_ref = manifest.get("change_ref")
    if not change_ref:
        logger.error(
            "change_ref_present: production deployment requires a change reference."
        )
        return [
            PreflightCheck(
                check_name="change_ref_present",
                passed=False,
                database="(package)",
                message=(
                    "change_ref_present — production deployment requires a change "
                    "reference (--change-ref <ticket>). Build this package with "
                    "--change-ref CHG0012345 and redeploy."
                ),
                severity="ERROR",
            )
        ]

    logger.info("change_ref_present: change_ref '%s' present — OK.", change_ref)
    return [
        PreflightCheck(
            check_name="change_ref_present",
            passed=True,
            database="(package)",
            message=f"change_ref_present — change reference '{change_ref}' present.",
            severity="INFO",
        )
    ]


# ---------------------------------------------------------------
# Package signature check (GAP-005)
# ---------------------------------------------------------------


def check_package_signature(package_dir: str) -> List[PreflightCheck]:
    """Verify the HMAC-SHA256 package signature sidecar (GAP-005).

    Locates the release ZIP via BUILD.json's ``package_filename``, then
    looks for a ``.hmac`` sidecar file beside it.  The check is
    conditional on three states:

    - ``require_signature: true`` in BUILD.json AND no ``.hmac`` file →
      ERROR.
    - ``.hmac`` file present but ``SHIPS_SIGNING_KEY`` not set (no key
      to verify) → ERROR.
    - ``.hmac`` file present, key available, hash mismatch → ERROR.
    - ``.hmac`` file present, key available, hash matches → pass (INFO).
    - ``.hmac`` absent AND ``require_signature: false`` → silently pass
      (no finding emitted).

    Args:
        package_dir: Path to the extracted package directory.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    from database_package_deployer.signing import resolve_signing_key, verify_hmac

    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("package_signature: could not read BUILD.json: %s", exc)
        return []

    package_filename = manifest.get("package_filename", "")
    if not package_filename:
        return []

    require_signature = manifest.get("require_signature", False)
    zip_path = Path(package_dir).parent / package_filename
    if not zip_path.exists():
        return []

    hmac_path = zip_path.parent / (zip_path.name + ".hmac")

    if not hmac_path.exists():
        if require_signature:
            logger.error(
                "package_signature: .hmac sidecar not found and signature is required."
            )
            return [
                PreflightCheck(
                    check_name="package_signature",
                    passed=False,
                    database="(package)",
                    message=(
                        f"package_signature — .hmac sidecar not found for "
                        f"'{zip_path.name}' and require_signature=true. "
                        f"Sign this package with --signing-key before deploying."
                    ),
                    severity="ERROR",
                )
            ]
        # Not required and absent → silently pass
        logger.debug("package_signature: no .hmac sidecar and not required — skipping.")
        return []

    # .hmac file exists — must verify
    key = resolve_signing_key()
    if key is None:
        logger.error(
            "package_signature: .hmac sidecar present but no signing key available."
        )
        return [
            PreflightCheck(
                check_name="package_signature",
                passed=False,
                database="(package)",
                message=(
                    "package_signature — cannot verify signature: "
                    f"SHIPS_SIGNING_KEY is not set. Set the environment "
                    f"variable or remove the .hmac sidecar if verification "
                    f"is not required."
                ),
                severity="ERROR",
            )
        ]

    try:
        expected_hmac = hmac_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [
            PreflightCheck(
                check_name="package_signature",
                passed=False,
                database="(package)",
                message=f"package_signature — could not read .hmac sidecar: {exc}",
                severity="ERROR",
            )
        ]

    zip_hash = _sha256_of_file(str(zip_path))
    if not verify_hmac(key, zip_hash, expected_hmac):
        logger.error("package_signature: HMAC mismatch for '%s'.", zip_path.name)
        return [
            PreflightCheck(
                check_name="package_signature",
                passed=False,
                database="(package)",
                message=(
                    f"package_signature — HMAC mismatch for '{zip_path.name}'. "
                    f"The archive may have been tampered with, or the wrong "
                    f"signing key is being used."
                ),
                severity="ERROR",
            )
        ]

    logger.info("package_signature: '%s' HMAC verified OK.", zip_path.name)
    return [
        PreflightCheck(
            check_name="package_signature",
            passed=True,
            database="(package)",
            message=f"package_signature — '{zip_path.name}' HMAC signature verified OK.",
            severity="INFO",
        )
    ]


# ---------------------------------------------------------------
# Multi-person authorisation check (GAP-006)
# ---------------------------------------------------------------


def check_mpa_approval(
    package_dir: str, approval_code: str = ""
) -> List[PreflightCheck]:
    """Verify a 4-eyes approval code when require_approvals >= 2 (GAP-006).

    The check reads ``require_approvals`` from BUILD.json.  When it is 1
    (or absent), the check passes with no findings.  When it is >= 2:

    - No ``--approval-code`` supplied → ERROR.
    - Code supplied but verification fails (wrong key or expired date) → ERROR.
    - Code supplied and valid for today (UTC) → pass.

    The approval code is ``HMAC-SHA256(key, sha256_of_zip + ':' + UTC_date)``.
    Yesterday's code is explicitly rejected — the code expires at midnight UTC.

    Args:
        package_dir:   Path to the extracted package directory.
        approval_code: Hex code produced by ``ships approve <package_zip>``.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mpa_approval: could not read BUILD.json: %s", exc)
        return []

    require_approvals = manifest.get("require_approvals", 1)
    if require_approvals < 2:
        logger.debug(
            "mpa_approval: require_approvals=%d — skipping.", require_approvals
        )
        return []

    if not approval_code:
        logger.error("mpa_approval: 4-eyes approval code required but not provided.")
        return [
            PreflightCheck(
                check_name="mpa_approval",
                passed=False,
                database="(package)",
                message=(
                    "mpa_approval — this deployment requires a second authorisation. "
                    "Ask a second authorised operator to run: "
                    "  ships approve <package_zip>  "
                    "then pass the printed code via --approval-code."
                ),
                severity="ERROR",
            )
        ]

    # Locate the ZIP to verify the approval code against
    package_filename = manifest.get("package_filename", "")
    if not package_filename:
        logger.warning("mpa_approval: package_filename absent from BUILD.json.")
        return []

    zip_path = Path(package_dir).parent / package_filename
    if not zip_path.exists():
        logger.warning(
            "mpa_approval: archive '%s' not found — cannot verify approval code.",
            zip_path,
        )
        return []

    from database_package_deployer.mpa import verify_approval_code

    if not verify_approval_code(str(zip_path), approval_code):
        logger.error("mpa_approval: approval code invalid or expired.")
        return [
            PreflightCheck(
                check_name="mpa_approval",
                passed=False,
                database="(package)",
                message=(
                    "mpa_approval — approval code is invalid or has expired "
                    "(codes are valid for the UTC calendar day they were generated). "
                    "Ask the approving operator to regenerate the code for today."
                ),
                severity="ERROR",
            )
        ]

    logger.info("mpa_approval: 4-eyes approval code verified OK.")
    return [
        PreflightCheck(
            check_name="mpa_approval",
            passed=True,
            database="(package)",
            message="mpa_approval — 4-eyes approval code verified OK.",
            severity="INFO",
        )
    ]


# ---------------------------------------------------------------
# Package age / TTL check (GAP-012)
# ---------------------------------------------------------------


def check_package_age(package_dir: str) -> List[PreflightCheck]:
    """Check whether the release package has exceeded its TTL (GAP-012).

    Reads ``package_built_at`` (falling back to ``timestamp``) and
    ``package_max_age_days`` from BUILD.json.  When the package age
    exceeds the threshold, emits a WARNING or ERROR depending on
    ``package_age_violation_level``.

    A threshold of 0 disables the check.

    Args:
        package_dir: Path to the extracted package directory.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    build_json = os.path.join(package_dir, "BUILD.json")
    if not os.path.isfile(build_json):
        return []

    try:
        with open(build_json, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("package_age: could not read BUILD.json: %s", exc)
        return []

    max_age = manifest.get("package_max_age_days", 30)
    if max_age == 0:
        logger.debug("package_age: check disabled (package_max_age_days=0).")
        return []

    built_at_str = manifest.get("package_built_at") or manifest.get("timestamp", "")
    if not built_at_str:
        return [
            PreflightCheck(
                check_name="package_age",
                passed=True,
                database="(package)",
                message="package_age — build timestamp absent from manifest.",
                severity="WARNING",
            )
        ]

    try:
        # Parse ISO 8601 — accept both trailing Z and +00:00
        built_at = datetime.fromisoformat(built_at_str.replace("Z", "+00:00"))
        if built_at.tzinfo is None:
            built_at = built_at.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        logger.warning(
            "package_age: could not parse build timestamp '%s': %s", built_at_str, exc
        )
        return []

    age_days = (datetime.now(tz=timezone.utc) - built_at).days
    if age_days <= max_age:
        return []

    violation_level = manifest.get("package_age_violation_level", "warning").lower()
    severity = "ERROR" if violation_level == "error" else "WARNING"

    logger.warning(
        "package_age: package is %d days old (threshold: %d days).", age_days, max_age
    )
    return [
        PreflightCheck(
            check_name="package_age",
            passed=(severity == "WARNING"),
            database="(package)",
            message=(
                f"package_age — package is {age_days} day(s) old "
                f"(threshold: {max_age} day(s)). "
                f"Consider rebuilding for a fresh deployment."
            ),
            severity=severity,
        )
    ]


# ---------------------------------------------------------------
# TLS / SSL connection check (GAP-015)
# ---------------------------------------------------------------


def check_tls_connection(
    package_dir: str,
    connection_params: Optional[dict] = None,
) -> List[PreflightCheck]:
    """Warn if the Teradata connection is not using TLS/SSL encryption (GAP-015).

    Inspects *connection_params* for recognised encryption indicators
    (``encryptdata``, ``sslmode``, ``ssl``).  When none are present or
    the value is falsy, a WARNING is emitted.

    When ``require_tls: true`` is set in BUILD.json (stamped from
    ships.yaml), the check is promoted to ERROR.

    Args:
        package_dir:       Extracted package directory.
        connection_params: Dict of teradatasql connection kwargs.  If
                           absent or None, the check is skipped.

    Returns:
        List of PreflightCheck results (zero or one entry).
    """
    if not connection_params:
        logger.debug("tls_connection: no connection params supplied — skipping.")
        return []

    # Read require_tls from BUILD.json
    require_tls = False
    build_json = os.path.join(package_dir, "BUILD.json")
    if os.path.isfile(build_json):
        try:
            with open(build_json, encoding="utf-8") as fh:
                manifest = json.load(fh)
            require_tls = bool(manifest.get("require_tls", False))
        except (OSError, json.JSONDecodeError):
            pass

    # Detect encryption parameters
    encrypted = any(
        _is_truthy(connection_params.get(key))
        for key in ("encryptdata", "sslmode", "ssl")
    )

    if encrypted:
        logger.info("tls_connection: TLS/SSL encryption is enabled — OK.")
        return [
            PreflightCheck(
                check_name="tls_connection",
                passed=True,
                database="(connection)",
                message="tls_connection — Teradata connection is using TLS/SSL encryption.",
                severity="INFO",
            )
        ]

    severity = "ERROR" if require_tls else "WARNING"
    logger.warning("tls_connection: connection is not using TLS/SSL encryption.")
    return [
        PreflightCheck(
            check_name="tls_connection",
            passed=(severity == "WARNING"),
            database="(connection)",
            message=(
                "tls_connection — Teradata connection is not using TLS/SSL encryption. "
                "Set encryptdata=true in the connection configuration."
            ),
            severity=severity,
        )
    ]


def _is_truthy(value) -> bool:
    """Return True for connection parameter values that indicate encryption is enabled."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).lower().strip()
    return s in ("true", "1", "yes", "on", "require", "verify-ca", "verify-full")
