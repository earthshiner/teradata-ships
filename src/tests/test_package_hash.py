"""
test_package_hash.py — Tests for the package_hash preflight check (GAP-001).

Covers:
    - Pass: valid ZIP with matching .sha256 sidecar.
    - Fail — mismatch: valid ZIP with tampered sidecar (wrong hash).
    - Fail — missing sidecar: valid ZIP with no .sha256 sidecar.
    - Edge: sidecar in two-column 'hash filename' format → parsed correctly.
    - Skip: no ZIP found beside the package directory → no finding emitted.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from database_package_deployer.preflight import check_package_hash


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_zip(tmp_path: Path, zip_name: str = "DEV_TestPkg_BUILD_0001.zip") -> Path:
    """Create a minimal, real ZIP archive and return its path."""
    zip_path = tmp_path / zip_name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("BUILD.json", json.dumps({"package_filename": zip_name}))
        zf.writestr("payload/table.tbl", "CREATE MULTISET TABLE D.T (id INT);")
    return zip_path


def _sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_build_json(pkg_dir: Path, zip_name: str) -> None:
    """Write a BUILD.json referencing the given ZIP filename."""
    manifest = {
        "package_filename": zip_name,
        "environment": "DEV",
        "package_name": "TestPkg",
    }
    (pkg_dir / "BUILD.json").write_text(json.dumps(manifest), encoding="utf-8")


# ---------------------------------------------------------------
# Test: pass — valid ZIP with matching sidecar
# ---------------------------------------------------------------


def test_check_package_hash_pass(tmp_path):
    """Matching .sha256 sidecar → check passes with no errors."""
    zip_name = "DEV_TestPkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    digest = _sha256(zip_path)

    # Single-column sidecar format
    sidecar = tmp_path / (zip_name + ".sha256")
    sidecar.write_text(f"{digest}\n", encoding="utf-8")

    pkg_dir = tmp_path / "DEV_TestPkg_BUILD_0001"
    pkg_dir.mkdir()
    _make_build_json(pkg_dir, zip_name)

    results = check_package_hash(str(pkg_dir))

    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].check_name == "package_hash"
    assert results[0].severity == "INFO"


# ---------------------------------------------------------------
# Test: fail — sidecar contains wrong hash
# ---------------------------------------------------------------


def test_check_package_hash_fail_mismatch(tmp_path):
    """Tampered sidecar (wrong hash) → ERROR raised identifying the file."""
    zip_name = "DEV_TestPkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)

    # Write a deliberately wrong hash
    wrong_hash = "a" * 64
    sidecar = tmp_path / (zip_name + ".sha256")
    sidecar.write_text(f"{wrong_hash}\n", encoding="utf-8")

    pkg_dir = tmp_path / "DEV_TestPkg_BUILD_0001"
    pkg_dir.mkdir()
    _make_build_json(pkg_dir, zip_name)

    results = check_package_hash(str(pkg_dir))

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "package_hash" in results[0].check_name
    assert zip_name in results[0].message
    assert "mismatch" in results[0].message


# ---------------------------------------------------------------
# Test: fail — sidecar missing
# ---------------------------------------------------------------


def test_check_package_hash_fail_missing_sidecar(tmp_path):
    """ZIP present but no .sha256 sidecar → ERROR raised naming the missing file."""
    zip_name = "DEV_TestPkg_BUILD_0001.zip"
    _make_zip(tmp_path, zip_name)
    # Deliberately do NOT create the sidecar

    pkg_dir = tmp_path / "DEV_TestPkg_BUILD_0001"
    pkg_dir.mkdir()
    _make_build_json(pkg_dir, zip_name)

    results = check_package_hash(str(pkg_dir))

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "sidecar not found" in results[0].message
    assert zip_name in results[0].message


# ---------------------------------------------------------------
# Test: edge — two-column sidecar format ('hash  filename')
# ---------------------------------------------------------------


def test_check_package_hash_two_column_sidecar(tmp_path):
    """Two-column sha256sum format ('hash  filename') → hash parsed correctly."""
    zip_name = "DEV_TestPkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    digest = _sha256(zip_path)

    # sha256sum(1) two-column format: hash + two spaces + filename
    sidecar = tmp_path / (zip_name + ".sha256")
    sidecar.write_text(f"{digest}  {zip_name}\n", encoding="utf-8")

    pkg_dir = tmp_path / "DEV_TestPkg_BUILD_0001"
    pkg_dir.mkdir()
    _make_build_json(pkg_dir, zip_name)

    results = check_package_hash(str(pkg_dir))

    assert len(results) == 1
    assert results[0].passed is True, results[0].message


# ---------------------------------------------------------------
# Test: skip — ZIP not present beside package directory
# ---------------------------------------------------------------


def test_check_package_hash_skip_no_zip(tmp_path):
    """No ZIP found in parent directory → no findings emitted (skip)."""
    zip_name = "DEV_TestPkg_BUILD_0001.zip"
    # Do NOT create the ZIP

    pkg_dir = tmp_path / "DEV_TestPkg_BUILD_0001"
    pkg_dir.mkdir()
    _make_build_json(pkg_dir, zip_name)

    results = check_package_hash(str(pkg_dir))

    assert results == []
