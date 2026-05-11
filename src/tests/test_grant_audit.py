"""
test_grant_audit.py — Tests for privilege drift detection (GAP-014).

Uses mocked Teradata connections — no live database required.

Covers:
    - No drift: declared grants match live state → exit 0, no UNDECLARED/MISSING.
    - Undeclared grants: live Teradata has grants not in DCL files → UNDECLARED section.
    - Missing grants: DCL files declare grants not in live state → MISSING section.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from database_package_deployer.grant_audit import audit_grants, _parse_dcl_grants


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_grt(tmp_path: Path, content: str, filename: str = "grants.grt") -> str:
    """Write a .grt file and return the directory path."""
    dcl_dir = tmp_path / "payload" / "02_dcl"
    dcl_dir.mkdir(parents=True, exist_ok=True)
    (dcl_dir / filename).write_text(content, encoding="utf-8")
    return str(tmp_path)


def _cursor_with_rows(rows):
    """Return a mock cursor that returns *rows* on fetchall."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


# ---------------------------------------------------------------
# _parse_dcl_grants
# ---------------------------------------------------------------


def test_parse_dcl_grants_basic(tmp_path):
    """Parse a simple GRANT SELECT ON DB TO USER statement."""
    pkg = _make_grt(tmp_path, "GRANT SELECT ON MY_DB TO APP_USER;\n")
    grants = _parse_dcl_grants(str(Path(pkg) / "payload" / "02_dcl"))
    assert ("SELECT", "MY_DB", "APP_USER") in grants


def test_parse_dcl_empty_dir(tmp_path):
    """Empty directory → no grants."""
    dcl = tmp_path / "dcl"
    dcl.mkdir()
    grants = _parse_dcl_grants(str(dcl))
    assert grants == set()


# ---------------------------------------------------------------
# audit_grants: no drift
# ---------------------------------------------------------------


def test_audit_grants_no_drift(tmp_path):
    """Declared grants match live state → drift=False, all MATCHED."""
    pkg = _make_grt(tmp_path, "GRANT SELECT ON MY_DB TO APP_USER;\n")
    # Live returns exactly what's declared
    cursor = _cursor_with_rows([("APP_USER", "MY_DB", "", "SELECT")])
    report = audit_grants(cursor, pkg)
    assert not report["drift"]
    assert len(report["MATCHED"]) >= 1
    assert report["MISSING"] == []
    assert report["UNDECLARED"] == []


# ---------------------------------------------------------------
# audit_grants: undeclared grants
# ---------------------------------------------------------------


def test_audit_grants_undeclared(tmp_path):
    """Live state has grants not in DCL → UNDECLARED populated."""
    pkg = _make_grt(tmp_path, "GRANT SELECT ON MY_DB TO APP_USER;\n")
    # Live has extra grant not declared
    cursor = _cursor_with_rows(
        [
            ("APP_USER", "MY_DB", "", "SELECT"),  # declared
            ("DBA_USER", "MY_DB", "", "ALL"),  # undeclared
        ]
    )
    report = audit_grants(cursor, pkg)
    assert report["drift"]
    assert len(report["UNDECLARED"]) >= 1


# ---------------------------------------------------------------
# audit_grants: missing grants
# ---------------------------------------------------------------


def test_audit_grants_missing(tmp_path):
    """DCL declares grants not in live state → MISSING populated."""
    # Declare SELECT and INSERT, but live only has SELECT
    pkg = _make_grt(
        tmp_path,
        "GRANT SELECT ON MY_DB TO APP_USER;\nGRANT INSERT ON MY_DB TO APP_USER;\n",
    )
    cursor = _cursor_with_rows([("APP_USER", "MY_DB", "", "SELECT")])
    report = audit_grants(cursor, pkg)
    assert report["drift"]
    assert len(report["MISSING"]) >= 1
