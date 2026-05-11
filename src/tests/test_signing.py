"""
test_signing.py — Tests for HMAC-SHA256 package signing (GAP-005).

Covers:
    - Pass: valid ZIP, valid .hmac, correct key → passes.
    - Pass: unsigned, not required → passes (no finding).
    - Fail — mismatch: valid .hmac with wrong key → ERROR.
    - Fail — required, absent: require_signature=true, no .hmac → ERROR.
    - Fail — no key to verify: .hmac present, no signing key in env → ERROR.
    - Security: comparison uses hmac.compare_digest (not ==).
    - sign_package: writes .hmac sidecar.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import zipfile
from pathlib import Path

import pytest

from database_package_deployer.signing import (
    compute_hmac,
    resolve_signing_key,
    sign_package,
    verify_hmac,
)
from database_package_deployer.preflight import check_package_signature


_KEY = b"test-key-do-not-use"


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_zip(tmp_path: Path, zip_name: str = "PRD_Pkg_BUILD_0001.zip") -> Path:
    zip_path = tmp_path / zip_name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("BUILD.json", json.dumps({"package_filename": zip_name}))
    return zip_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_build_json(pkg_dir: Path, zip_name: str, **extra) -> None:
    manifest = {"package_filename": zip_name, **extra}
    (pkg_dir / "BUILD.json").write_text(json.dumps(manifest), encoding="utf-8")


def _make_hmac_sidecar(zip_path: Path, key: bytes) -> Path:
    """Sign the zip and write the .hmac sidecar."""
    zip_hash = _sha256(zip_path)
    hmac_hex = compute_hmac(key, zip_hash)
    sidecar = zip_path.parent / (zip_path.name + ".hmac")
    sidecar.write_text(hmac_hex + "\n", encoding="utf-8")
    return sidecar


# ---------------------------------------------------------------
# Core HMAC helpers
# ---------------------------------------------------------------


def test_compute_verify_hmac_roundtrip():
    """compute_hmac + verify_hmac form a correct round-trip."""
    sha = "a" * 64
    hexmac = compute_hmac(_KEY, sha)
    assert verify_hmac(_KEY, sha, hexmac)


def test_verify_hmac_wrong_key():
    """Different key → verify returns False."""
    sha = "b" * 64
    hexmac = compute_hmac(_KEY, sha)
    assert not verify_hmac(b"wrong-key-do-not-use", sha, hexmac)


def test_verify_hmac_uses_compare_digest():
    """verify_hmac must call hmac.compare_digest, not ==."""
    import database_package_deployer.signing as _signing_mod

    original = _hmac.compare_digest
    calls = []

    def _spy(a, b):
        calls.append((a, b))
        return original(a, b)

    _signing_mod._hmac.compare_digest = _spy
    try:
        verify_hmac(_KEY, "c" * 64, compute_hmac(_KEY, "c" * 64))
    finally:
        _signing_mod._hmac.compare_digest = original

    assert len(calls) == 1, "hmac.compare_digest was not called"


# ---------------------------------------------------------------
# sign_package helper
# ---------------------------------------------------------------


def test_sign_package_writes_sidecar(tmp_path, monkeypatch):
    """sign_package writes a .hmac sidecar when a key is available."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_path = _make_zip(tmp_path)
    hmac_path = sign_package(str(zip_path))
    assert hmac_path is not None
    assert Path(hmac_path).exists()
    # The written HMAC must verify correctly
    recorded = Path(hmac_path).read_text().strip()
    assert verify_hmac(_KEY, _sha256(zip_path), recorded)


def test_sign_package_no_key_returns_none(tmp_path, monkeypatch):
    """sign_package returns None silently when no key is available."""
    monkeypatch.delenv("SHIPS_SIGNING_KEY", raising=False)
    zip_path = _make_zip(tmp_path)
    assert sign_package(str(zip_path), key_path=None) is None


# ---------------------------------------------------------------
# check_package_signature
# ---------------------------------------------------------------


def test_package_signature_pass(tmp_path, monkeypatch):
    """Valid .hmac with correct key → check passes."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    _make_hmac_sidecar(zip_path, _KEY)

    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name)

    results = check_package_signature(str(pkg_dir))
    assert len(results) == 1
    assert results[0].passed is True


def test_package_signature_pass_unsigned_not_required(tmp_path, monkeypatch):
    """No .hmac sidecar and require_signature=false → silently passes."""
    monkeypatch.delenv("SHIPS_SIGNING_KEY", raising=False)
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    _make_zip(tmp_path, zip_name)

    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_signature=False)

    results = check_package_signature(str(pkg_dir))
    assert results == []


def test_package_signature_fail_mismatch(tmp_path, monkeypatch):
    """Valid .hmac but wrong key at verify time → ERROR."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", "wrong-key-do-not-use")
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    _make_hmac_sidecar(zip_path, _KEY)  # signed with _KEY

    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name)

    results = check_package_signature(str(pkg_dir))
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "mismatch" in results[0].message.lower()


def test_package_signature_fail_required_absent(tmp_path, monkeypatch):
    """require_signature=true, no .hmac → ERROR."""
    monkeypatch.delenv("SHIPS_SIGNING_KEY", raising=False)
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    _make_zip(tmp_path, zip_name)

    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_signature=True)

    results = check_package_signature(str(pkg_dir))
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"


def test_package_signature_fail_no_key_to_verify(tmp_path, monkeypatch):
    """.hmac present but SHIPS_SIGNING_KEY not set → ERROR."""
    monkeypatch.delenv("SHIPS_SIGNING_KEY", raising=False)
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    _make_hmac_sidecar(zip_path, _KEY)

    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name)

    results = check_package_signature(str(pkg_dir))
    assert len(results) == 1
    assert results[0].passed is False
    assert "SHIPS_SIGNING_KEY" in results[0].message
