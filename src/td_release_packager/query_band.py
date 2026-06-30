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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Dynamic QueryBand keys — set by the deployer at runtime as work
# progresses through phases, files, and wave-parallel streams. Listed
# here so the package_report and ships.build.json can advertise them
# even though their *values* only exist at deploy time.
DYNAMIC_QUERY_BAND_KEYS: List[str] = ["PHASE", "FILE", "STREAM", "WAVE"]


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
        build_number, package_name, environment, phase, file_name, extra
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


def describe_query_band(
    *,
    build_number: str,
    package_name: str,
    environment: str,
    operator_extras: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Return the canonical QueryBand description (issue #483).

    Single source of truth shared by the package report's DBQL Lookup
    card, the deploy report's same card, and the ``query_band`` block
    stamped into ``ships.build.json`` so an agent (or DBA) can find
    every statement in ``DBC.DBQLogTbl`` without having to parse the
    band format themselves.

    Args:
        build_number:    Build number — becomes the ``BUILD`` band value.
        package_name:    Logical package name — becomes ``PKG``.
        environment:     Target environment — becomes ``ENV``.
        operator_extras: Optional ``{key: value}`` pairs from
                         ``DeployConfig.query_band`` (or the equivalent
                         build-time configuration). Empty for a vanilla
                         build manifest; populated on the deploy report
                         when the operator passed ``--query-band`` extras.

    Returns:
        Four-key dict:

        - ``static``: the always-set ``BUILD`` / ``PKG`` / ``ENV`` keys.
        - ``dynamic_keys``: list of keys the deployer sets at runtime
          (``PHASE``, ``FILE``, ``STREAM``, ``WAVE``). Listed so a
          reader knows what to filter on; values are only known once a
          deploy has actually run.
        - ``operator_extras``: extra ``{key: value}`` pairs supplied by
          the operator. Empty dict when none.
        - ``dbql_filter_template``: ready-to-paste SQL WHERE-clause
          fragment filtering ``DBC.DBQLogTbl`` on the static keys plus
          any operator extras. Composed in a deterministic order so the
          string is stable across runs.
    """
    static: Dict[str, str] = {
        "BUILD": str(build_number),
        "PKG": str(package_name),
        "ENV": str(environment),
    }
    extras: Dict[str, str] = {
        str(k): str(v) for k, v in sorted((operator_extras or {}).items())
    }
    clauses: List[str] = [
        f"GetQueryBandValue(QueryBand, 0, '{k}') = '{v}'" for k, v in static.items()
    ]
    clauses.extend(
        f"GetQueryBandValue(QueryBand, 0, '{k}') = '{v}'" for k, v in extras.items()
    )
    return {
        "static": static,
        "dynamic_keys": list(DYNAMIC_QUERY_BAND_KEYS),
        "operator_extras": extras,
        "dbql_filter_template": "\n  AND ".join(clauses),
    }
