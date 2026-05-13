"""
test_package_age.py — Tests for the package_age TTL check (GAP-012).

Covers:
    - Pass: package built today → passes regardless of threshold.
    - Warn: package_max_age_days=30, package built 32 days ago → WARNING.
    - Fail: package_age_violation_level=error, package 32 days old → ERROR.
    - Pass — disabled: package_max_age_days=0 → check skipped.
    - Edge: package_built_at absent → WARNING (timestamp absent message).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from database_package_deployer.preflight import check_package_age


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_build_json(pkg_dir: Path, **extra) -> None:
    manifest = {"package_filename": "PRD_Pkg_BUILD_0001.zip", **extra}
    (pkg_dir / "ships.build.json").write_text(json.dumps(manifest), encoding="utf-8")


_NOW = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------
# Pass: built today
# ---------------------------------------------------------------


def test_package_age_pass_built_today(tmp_path):
    """Package built today → no findings regardless of threshold."""
    _write_build_json(tmp_path, package_built_at=_iso(_NOW), package_max_age_days=30)
    assert check_package_age(str(tmp_path)) == []


# ---------------------------------------------------------------
# Warn: threshold exceeded
# ---------------------------------------------------------------


def test_package_age_warn_exceeded(tmp_path):
    """Package built 32 days ago, threshold=30 → WARNING."""
    old = _NOW - timedelta(days=32)
    _write_build_json(tmp_path, package_built_at=_iso(old), package_max_age_days=30)
    results = check_package_age(str(tmp_path))
    assert len(results) == 1
    assert results[0].severity == "WARNING"
    assert "32" in results[0].message or "day" in results[0].message


# ---------------------------------------------------------------
# Fail: violation_level=error
# ---------------------------------------------------------------


def test_package_age_fail_error_level(tmp_path):
    """package_age_violation_level=error, package 32 days old → ERROR."""
    old = _NOW - timedelta(days=32)
    _write_build_json(
        tmp_path,
        package_built_at=_iso(old),
        package_max_age_days=30,
        package_age_violation_level="error",
    )
    results = check_package_age(str(tmp_path))
    assert len(results) == 1
    assert results[0].severity == "ERROR"
    assert not results[0].passed


# ---------------------------------------------------------------
# Pass — disabled: package_max_age_days=0
# ---------------------------------------------------------------


def test_package_age_disabled(tmp_path):
    """package_max_age_days=0 → check skipped, no findings."""
    old = _NOW - timedelta(days=365)
    _write_build_json(tmp_path, package_built_at=_iso(old), package_max_age_days=0)
    assert check_package_age(str(tmp_path)) == []


# ---------------------------------------------------------------
# Edge: package_built_at absent
# ---------------------------------------------------------------


def test_package_age_built_at_absent(tmp_path):
    """No package_built_at and no timestamp → WARNING about missing field."""
    _write_build_json(tmp_path, package_max_age_days=30)
    results = check_package_age(str(tmp_path))
    assert len(results) == 1
    assert results[0].severity == "WARNING"
    assert (
        "absent" in results[0].message.lower()
        or "timestamp" in results[0].message.lower()
    )
