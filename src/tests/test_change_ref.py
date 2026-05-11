"""
test_change_ref.py — Tests for the change_ref_present preflight check (GAP-004).

Covers:
    - Pass: require_change_ref=true, change_ref='CHG0012345' → passes.
    - Pass: require_change_ref=false, change_ref=null → passes.
    - Fail: require_change_ref=true, change_ref=null → ERROR raised.
    - Edge: change_ref field absent from manifest → treated as null (fail if required).
    - Builder model: change_ref and require_change_ref fields on BuildManifest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from database_package_deployer.preflight import check_change_ref_present


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write_build_json(pkg_dir: Path, **extra) -> None:
    manifest = {
        "package_filename": "PRD_Pkg_BUILD_0001.zip",
        "environment": "PRD",
        "package_name": "Pkg",
        **extra,
    }
    (pkg_dir / "BUILD.json").write_text(json.dumps(manifest), encoding="utf-8")


# ---------------------------------------------------------------
# Pass: required AND provided
# ---------------------------------------------------------------


def test_change_ref_pass_required_provided(tmp_path):
    """require_change_ref=true, change_ref='CHG0012345' → check passes."""
    _write_build_json(tmp_path, require_change_ref=True, change_ref="CHG0012345")
    results = check_change_ref_present(str(tmp_path))
    assert len(results) == 1
    assert results[0].passed is True
    assert "CHG0012345" in results[0].message


# ---------------------------------------------------------------
# Pass: not required, absent
# ---------------------------------------------------------------


def test_change_ref_pass_not_required(tmp_path):
    """require_change_ref=false, change_ref=null → check passes (skipped)."""
    _write_build_json(tmp_path, require_change_ref=False, change_ref=None)
    results = check_change_ref_present(str(tmp_path))
    assert results == []


# ---------------------------------------------------------------
# Fail: required but not provided
# ---------------------------------------------------------------


def test_change_ref_fail_required_missing(tmp_path):
    """require_change_ref=true, change_ref=null → ERROR raised."""
    _write_build_json(tmp_path, require_change_ref=True, change_ref=None)
    results = check_change_ref_present(str(tmp_path))
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "--change-ref" in results[0].message


# ---------------------------------------------------------------
# Edge: change_ref field absent → treated as null → fail if required
# ---------------------------------------------------------------


def test_change_ref_absent_field_fails_if_required(tmp_path):
    """Manifest lacks change_ref entirely → treated as null → ERROR if required."""
    _write_build_json(tmp_path, require_change_ref=True)
    # No change_ref key at all in manifest
    results = check_change_ref_present(str(tmp_path))
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"


# ---------------------------------------------------------------
# BuildManifest model fields
# ---------------------------------------------------------------


def test_build_manifest_change_ref_fields():
    """BuildManifest exposes change_ref (Optional[str]) and require_change_ref (bool)."""
    from td_release_packager.models import BuildManifest

    m = BuildManifest(
        build_number="0001",
        environment="PRD",
        package_name="Pkg",
        package_filename="PRD_Pkg_BUILD_0001.zip",
        timestamp="2026-05-11T00:00:00",
        change_ref="CHG0012345",
        require_change_ref=True,
    )
    assert m.change_ref == "CHG0012345"
    assert m.require_change_ref is True


def test_build_manifest_change_ref_defaults():
    """BuildManifest change_ref defaults to None and require_change_ref to False."""
    from td_release_packager.models import BuildManifest

    m = BuildManifest(
        build_number="0001",
        environment="DEV",
        package_name="Pkg",
        package_filename="DEV_Pkg_BUILD_0001.zip",
        timestamp="2026-05-11T00:00:00",
    )
    assert m.change_ref is None
    assert m.require_change_ref is False
