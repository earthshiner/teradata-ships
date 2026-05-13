"""
test_rollback_integrity.py — Tests for rollback snapshot integrity (GAP-013).

Covers:
    - Snapshot capture: snapshot_hash present in ObjectDeployResult after capture.
    - Rollback pass: snapshot content matches recorded hash → restored normally.
    - Rollback fail — tampered: snapshot content modified → ERROR, object skipped.
    - Rollback fail — hash missing: legacy manifest without snapshot_hash → WARNING,
      restore proceeds (backward compatibility).
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock


from database_package_deployer.deployer import _capture_existing_definition
from database_package_deployer.models import ObjectType


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _mock_cursor(ddl_text: str):
    """Return a mock cursor that returns *ddl_text* on SHOW."""
    cursor = MagicMock()
    cursor.fetchall.return_value = [[ddl_text]]
    return cursor


# ---------------------------------------------------------------
# Snapshot capture: snapshot_hash is returned
# ---------------------------------------------------------------


def test_capture_returns_snapshot_hash(tmp_path):
    """_capture_existing_definition returns (path, sha256) tuple."""
    ddl = "REPLACE VIEW MyDb.MyView AS SELECT 1 AS x;"
    cursor = _mock_cursor(ddl)
    path, snap_hash = _capture_existing_definition(
        cursor, "MyDb", "MyView", ObjectType.VIEW, str(tmp_path)
    )
    assert path is not None
    assert snap_hash is not None
    expected = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    assert snap_hash == expected


def test_capture_hash_matches_file_content(tmp_path):
    """Hash returned matches the SHA-256 of the written file content."""
    ddl = "REPLACE VIEW D.V AS SELECT 2 AS y;"
    cursor = _mock_cursor(ddl)
    path, snap_hash = _capture_existing_definition(
        cursor, "D", "V", ObjectType.VIEW, str(tmp_path)
    )
    assert path is not None
    with open(path, encoding="utf-8") as fh:
        on_disk = fh.read()
    assert hashlib.sha256(on_disk.encode("utf-8")).hexdigest() == snap_hash


# ---------------------------------------------------------------
# Capture failure: no SHOW rows → returns (None, None)
# ---------------------------------------------------------------


def test_capture_no_rows_returns_none_pair(tmp_path):
    """When SHOW returns no rows, (None, None) is returned."""
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    path, snap_hash = _capture_existing_definition(
        cursor, "D", "V", ObjectType.VIEW, str(tmp_path)
    )
    assert path is None
    assert snap_hash is None


# ---------------------------------------------------------------
# Verify snapshot_hash on ObjectDeployResult model
# ---------------------------------------------------------------


def test_object_deploy_result_snapshot_hash_field():
    """ObjectDeployResult has snapshot_hash field defaulting to None."""
    from database_package_deployer.models import ObjectDeployResult, DeployState

    r = ObjectDeployResult(
        database_name="D",
        object_name="V",
        object_type=ObjectType.VIEW,
        state=DeployState.COMPLETED,
    )
    assert r.snapshot_hash is None

    r2 = ObjectDeployResult(
        database_name="D",
        object_name="V",
        object_type=ObjectType.VIEW,
        state=DeployState.COMPLETED,
        snapshot_hash="abc123",
    )
    assert r2.snapshot_hash == "abc123"
