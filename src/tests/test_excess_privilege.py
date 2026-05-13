"""
test_excess_privilege.py — Tests for the excess_privilege preflight check (GAP-010).

Uses a mock/stub for the Teradata connection — no live database required.

Covers:
    - Pass: deploy user has only CT, DT, R on target databases → passes.
    - Warn — DBC rights: deploy user has any right where DatabaseName='DBC' → WARNING.
    - Warn — grant option: deploy user holds AccessRight='GD' → WARNING.
"""

from __future__ import annotations

from unittest.mock import MagicMock


from database_package_deployer.preflight import _check_excess_privilege


# ---------------------------------------------------------------
# Mock cursor helpers
# ---------------------------------------------------------------


def _cursor_with_rows(rows):
    """Return a mock cursor whose fetchall returns *rows*."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


# ---------------------------------------------------------------
# Pass: no elevated rights
# ---------------------------------------------------------------


def test_excess_privilege_pass_clean(tmp_path):
    """Deploy user with only CT/DT/R rights → passed INFO check."""
    cursor = _cursor_with_rows([])
    results = _check_excess_privilege(cursor)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].severity == "INFO"


# ---------------------------------------------------------------
# Warn: rights on DBC database
# ---------------------------------------------------------------


def test_excess_privilege_warn_dbc_rights():
    """Deploy user has a right where DatabaseName='DBC' → WARNING."""
    cursor = _cursor_with_rows([("R", "DBC", "")])
    results = _check_excess_privilege(cursor)
    assert len(results) == 1
    assert results[0].severity == "WARNING"
    assert "DBC" in results[0].message


# ---------------------------------------------------------------
# Warn: grant option (GD)
# ---------------------------------------------------------------


def test_excess_privilege_warn_grant_option():
    """Deploy user holds AccessRight='GD' (GRANT OPTION) → WARNING."""
    cursor = _cursor_with_rows([("GD", "MY_DB", "")])
    results = _check_excess_privilege(cursor)
    assert len(results) == 1
    assert results[0].severity == "WARNING"
    assert "GD" in results[0].message


# ---------------------------------------------------------------
# Query failure is non-fatal
# ---------------------------------------------------------------


def test_excess_privilege_query_failure_non_fatal():
    """Query failure (e.g. view unavailable) → empty result, no exception."""
    cursor = MagicMock()
    cursor.execute.side_effect = Exception("DBC.UserRightsV not available")
    results = _check_excess_privilege(cursor)
    assert results == []
