"""
grant_audit.py — Privilege drift detection for SHIPS deployments (GAP-014).

Compares the grants declared in a package's DCL files against the current
live grant state in Teradata.

Output categories:
    UNDECLARED  — grant exists in Teradata but is not in the package DCL files
    MISSING     — grant is in the DCL files but does not exist in Teradata
    MATCHED     — grant is declared and present (correct state)

Exit codes (used by the CLI):
    0 — no drift detected (all MATCHED)
    1 — drift detected (any UNDECLARED or MISSING)
"""

import logging
import os
import re
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# DCL file parsing
# ---------------------------------------------------------------

# Simplified GRANT parser: extracts (right, database, grantee) triples.
_GRANT_RE = re.compile(
    r"\bGRANT\b\s+(?P<rights>.+?)\s+\bON\b\s+(?P<db>[^\s]+)\s+\bTO\b\s+(?P<grantee>[^;]+)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_dcl_grants(dcl_dir: str) -> Set[Tuple[str, str, str]]:
    """Parse GRANT statements from all .grt files under *dcl_dir*.

    Returns a set of (right_code, database_name, grantee) tuples.
    Only the first word of GRANT privileges is extracted (e.g. 'SELECT').
    """
    grants: Set[Tuple[str, str, str]] = set()
    if not os.path.isdir(dcl_dir):
        return grants

    for root, _, files in os.walk(dcl_dir):
        for fname in sorted(files):
            if not fname.endswith(".grt"):
                continue
            fpath = os.path.join(root, fname)
            try:
                content = open(fpath, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for m in _GRANT_RE.finditer(content):
                rights_raw = m.group("rights").strip()
                db = m.group("db").strip().rstrip(";").strip()
                grantee = m.group("grantee").strip().rstrip(";").strip()
                # Multiple privileges in one GRANT — take first word as key
                for right_token in re.split(r"[,\s]+", rights_raw):
                    right = right_token.strip().upper()
                    if right:
                        grants.add((right, db.upper(), grantee.upper()))

    return grants


def _query_live_grants(cursor, db_list: List[str]) -> Set[Tuple[str, str, str]]:
    """Query live grants from DBC.AllRightsV for the given databases.

    Returns a set of (access_right, database_name, user_name) tuples.
    """
    if not db_list:
        return set()

    # Build parameterised IN clause
    placeholders = ",".join(["?" for _ in db_list])
    try:
        cursor.execute(
            f"SELECT"
            f"     TRIM(UserName)"
            f"    ,TRIM(DatabaseName)"
            f"    ,TRIM(TableName)"
            f"    ,TRIM(AccessRight)"
            f" FROM DBC.AllRightsV"
            f" WHERE TRIM(DatabaseName) IN ({placeholders})"
            f" ORDER BY 1, 2, 3, 4",
            db_list,
        )
        rows = cursor.fetchall()
    except Exception as exc:
        logger.warning("grant_audit: query failed: %s", exc)
        return set()

    grants: Set[Tuple[str, str, str]] = set()
    for row in rows:
        grantee, db, _tbl, right = row[0], row[1], row[2], row[3]
        grants.add((right.upper(), db.upper(), grantee.upper()))
    return grants


def audit_grants(
    cursor,
    package_dir: str,
) -> Dict[str, Any]:
    """Compare declared vs live grants and return a categorised report.

    Args:
        cursor:       Active Teradata database cursor.
        package_dir:  Extracted package directory (contains DCL files).

    Returns:
        Dict with keys 'MATCHED', 'UNDECLARED', 'MISSING', and 'drift' (bool).
    """
    dcl_dir = os.path.join(package_dir, "payload", "02_dcl")
    if not os.path.isdir(dcl_dir):
        dcl_dir = package_dir  # Fall back to scanning whole dir

    declared = _parse_dcl_grants(dcl_dir)

    # Collect the set of databases referenced by declared grants
    db_list = sorted({db for _right, db, _grantee in declared} if declared else [])

    live = _query_live_grants(cursor, db_list)

    matched = declared & live
    missing = declared - live
    undeclared = live - declared

    return {
        "MATCHED": sorted(matched),
        "MISSING": sorted(missing),
        "UNDECLARED": sorted(undeclared),
        "drift": bool(missing or undeclared),
    }
