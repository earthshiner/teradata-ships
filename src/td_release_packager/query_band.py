"""
query_band.py — Query band management for deployment audit trail.

Sets a Teradata query band at session level before deployment begins,
and updates it per-phase and per-file during execution. All SQL
executed during deployment carries the query band, creating a full
audit trail in DBC.DBQLogTbl.

Query band format:
    BUILD=0012;PKG=MortgagePlatform;ENV=PROD;PHASE=DDL;FILE=Customers.tbl;

DBC.DBQLogTbl can then be queried:
    SELECT QueryBand, StartTime, UserName, StatementType
    FROM DBC.DBQLogTbl
    WHERE GetQueryBandValue(QueryBand, 0, 'BUILD') = '0012'
    ORDER BY StartTime;
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def build_query_band(
    build_number: str,
    package_name: str,
    environment: str,
    phase: str = "",
    file_name: str = "",
    extra: Optional[Dict[str, str]] = None,
) -> str:
    """
    Construct a Teradata query band string.

    Args:
        build_number:  Build number (e.g. '0012').
        package_name:  Package name (e.g. 'MortgagePlatform').
        environment:   Environment (e.g. 'PROD').
        phase:         Current deployment phase (e.g. 'DDL').
        file_name:     Current file being deployed.
        extra:         Additional key=value pairs.

    Returns:
        Formatted query band string (semicolon-delimited).
    """
    parts = [
        f"BUILD={build_number}",
        f"PKG={package_name}",
        f"ENV={environment}",
    ]

    if phase:
        parts.append(f"PHASE={phase}")
    if file_name:
        parts.append(f"FILE={file_name}")
    if extra:
        for key, value in sorted(extra.items()):
            parts.append(f"{key}={value}")

    return ";".join(parts) + ";"


def set_session_query_band(
    cursor,
    build_number: str,
    package_name: str,
    environment: str,
    phase: str = "",
    file_name: str = "",
    extra: Optional[Dict[str, str]] = None,
):
    """
    Set the query band for the current session.

    Uses SET QUERY_BAND ... FOR SESSION so all subsequent
    SQL on this connection carries the band.

    Args:
        cursor:         Active Teradata database cursor.
        build_number:   Build number.
        package_name:   Package name.
        environment:    Environment.
        phase:          Current deployment phase.
        file_name:      Current file being deployed.
        extra:          Additional key=value pairs.
    """
    band = build_query_band(
        build_number, package_name, environment,
        phase, file_name, extra
    )

    sql = f"SET QUERY_BAND = '{band}' FOR SESSION"

    try:
        cursor.execute(sql)
        logger.debug("Query band set: %s", band)
    except Exception as e:
        # Query band failure should not block deployment
        logger.warning("Failed to set query band: %s", e)


def update_phase(cursor, phase: str, **kwargs):
    """
    Update the query band to reflect the current phase.

    Convenience wrapper — re-sets the session query band
    with the new phase value.

    Args:
        cursor: Active database cursor.
        phase:  New phase name.
        **kwargs: Passed to set_session_query_band.
    """
    set_session_query_band(cursor, phase=phase, **kwargs)


def clear_query_band(cursor):
    """
    Clear the session query band.

    Called at the end of deployment to clean up.

    Args:
        cursor: Active database cursor.
    """
    try:
        cursor.execute("SET QUERY_BAND = NONE FOR SESSION")
        logger.debug("Query band cleared.")
    except Exception as e:
        logger.warning("Failed to clear query band: %s", e)
