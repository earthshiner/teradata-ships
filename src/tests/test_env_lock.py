"""
test_env_lock.py — Tests for the env_lock preflight check (GAP-002).

Covers:
    - Pass: package built for PRD, Ship targeting PRD → check passes.
    - Fail: package built for DEV, Ship targeting PRD → ERROR raised.
    - Fail — missing field: manifest lacks target_env → ERROR with upgrade message.
    - Edge: comparison is case-insensitive (prd == PRD).
    - Skip: no --env supplied to Ship → check skipped (no findings).
    - Builder: target_env written to BuildManifest at Package time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from database_package_deployer.preflight import check_env_lock


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write_build_json(pkg_dir: Path, **extra) -> None:
    """Write a minimal ships.build.json with given fields."""
    manifest = {
        "package_filename": "DEV_Pkg_BUILD_0001.zip",
        "environment": "DEV",
        "package_name": "Pkg",
        **extra,
    }
    (pkg_dir / "ships.build.json").write_text(json.dumps(manifest), encoding="utf-8")


# ---------------------------------------------------------------
# Test: pass — environments match
# ---------------------------------------------------------------


def test_env_lock_pass(tmp_path):
    """Package built for PRD, deploying to PRD → check passes."""
    _write_build_json(tmp_path, target_env="PRD")
    results = check_env_lock(str(tmp_path), "PRD")
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].check_name == "env_lock"


# ---------------------------------------------------------------
# Test: fail — environment mismatch
# ---------------------------------------------------------------


def test_env_lock_fail_mismatch(tmp_path):
    """Package built for DEV, deploying to PRD → ERROR raised."""
    _write_build_json(tmp_path, target_env="DEV")
    results = check_env_lock(str(tmp_path), "PRD")
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "DEV" in results[0].message
    assert "PRD" in results[0].message


# ---------------------------------------------------------------
# Test: fail — missing target_env field
# ---------------------------------------------------------------


def test_env_lock_fail_missing_field(tmp_path):
    """Manifest lacks target_env → ERROR with upgrade guidance."""
    _write_build_json(tmp_path)  # no target_env key
    results = check_env_lock(str(tmp_path), "PRD")
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "target_env" in results[0].message


# ---------------------------------------------------------------
# Test: skip — no --env supplied
# ---------------------------------------------------------------


def test_env_lock_skip_no_env(tmp_path):
    """No deployed_env supplied → check skipped, no findings emitted."""
    _write_build_json(tmp_path, target_env="PRD")
    assert check_env_lock(str(tmp_path), "") == []
    assert check_env_lock(str(tmp_path), None) == []


# ---------------------------------------------------------------
# Test: edge — case-insensitive comparison
# ---------------------------------------------------------------


def test_env_lock_case_insensitive(tmp_path):
    """'prd' and 'PRD' are treated as the same environment."""
    _write_build_json(tmp_path, target_env="PRD")
    results = check_env_lock(str(tmp_path), "prd")
    assert len(results) == 1
    assert results[0].passed is True


# ---------------------------------------------------------------
# Test: builder stamps target_env into BuildManifest
# ---------------------------------------------------------------


def test_build_manifest_target_env():
    """BuildManifest dataclass exposes target_env field."""
    from td_release_packager.models import BuildManifest

    m = BuildManifest(
        build_number="0001",
        environment="PRD",
        package_name="TestPkg",
        package_filename="PRD_TestPkg_BUILD_0001.zip",
        timestamp="2026-05-11T00:00:00",
        target_env="PRD",
    )
    assert m.target_env == "PRD"


def test_build_manifest_target_env_default():
    """BuildManifest.target_env defaults to empty string for backward compatibility."""
    from td_release_packager.models import BuildManifest

    m = BuildManifest(
        build_number="0001",
        environment="DEV",
        package_name="TestPkg",
        package_filename="DEV_TestPkg_BUILD_0001.zip",
        timestamp="2026-05-11T00:00:00",
    )
    assert m.target_env == ""
