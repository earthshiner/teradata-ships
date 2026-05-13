"""
test_asym_signing.py — Tests for Ed25519 asymmetric package signing (Option C).

All tests are skipped gracefully when the ``cryptography`` package is not
installed, so the test suite passes in environments without it.
"""

import json
import os
import tempfile

import pytest

cryptography = pytest.importorskip(
    "cryptography",
    reason="cryptography package not installed — skipping Ed25519 signing tests.",
)

from database_package_deployer.asym_signing import (  # noqa: E402
    generate_keypair,
    sign_zip,
    verify_zip,
)
from database_package_deployer.preflight import check_asymmetric_signature  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(tmp_dir: str, content: bytes = b"fake zip content") -> str:
    """Write a small fake ZIP file and return its path."""
    zip_path = os.path.join(tmp_dir, "test_package.zip")
    with open(zip_path, "wb") as fh:
        fh.write(content)
    return zip_path


def _make_build_json(tmp_dir: str, package_filename: str = "test_package.zip", **extra):
    """Write a minimal ships.build.json alongside the fake ZIP."""
    manifest = {"package_filename": package_filename}
    manifest.update(extra)
    build_json = os.path.join(tmp_dir, "ships.build.json")
    with open(build_json, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return build_json


# ---------------------------------------------------------------------------
# generate_keypair
# ---------------------------------------------------------------------------


def test_generate_keypair_returns_pem_strings():
    private_pem, public_pem = generate_keypair()

    assert isinstance(private_pem, str)
    assert isinstance(public_pem, str)
    assert "PRIVATE KEY" in private_pem
    assert "PUBLIC KEY" in public_pem


def test_generate_keypair_produces_unique_pairs():
    pair_a = generate_keypair()
    pair_b = generate_keypair()

    assert pair_a[0] != pair_b[0], "Private keys should be unique across calls."
    assert pair_a[1] != pair_b[1], "Public keys should be unique across calls."


# ---------------------------------------------------------------------------
# sign_zip / verify_zip
# ---------------------------------------------------------------------------


def test_sign_zip_writes_sig_file():
    private_pem, _public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        sig_path = sign_zip(zip_path, private_pem)

        assert os.path.isfile(sig_path)
        assert sig_path == zip_path + ".sig"
        content = open(sig_path, encoding="utf-8").read().strip()
        # Should be base64 — no whitespace except the trailing newline we stripped
        import base64

        base64.b64decode(content)  # Must not raise


def test_verify_zip_with_correct_key_returns_true():
    private_pem, public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        sign_zip(zip_path, private_pem)

        assert verify_zip(zip_path, public_pem) is True


def test_verify_zip_with_wrong_key_returns_false():
    private_pem, _correct_public_pem = generate_keypair()
    _wrong_private_pem, wrong_public_pem = generate_keypair()

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        sign_zip(zip_path, private_pem)

        assert verify_zip(zip_path, wrong_public_pem) is False


def test_verify_zip_with_tampered_content_returns_false():
    private_pem, public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp, content=b"original content")
        sign_zip(zip_path, private_pem)

        # Tamper with the ZIP after signing
        with open(zip_path, "wb") as fh:
            fh.write(b"tampered content")

        assert verify_zip(zip_path, public_pem) is False


def test_verify_zip_missing_sig_file_returns_false():
    _private_pem, public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        # No .sig file written

        assert verify_zip(zip_path, public_pem) is False


# ---------------------------------------------------------------------------
# check_asymmetric_signature preflight checks
# ---------------------------------------------------------------------------


def test_check_asym_sig_pass_valid_sig():
    """Valid .sig sidecar and matching public key → passed=True."""
    private_pem, public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        _make_build_json(tmp)
        sign_zip(zip_path, private_pem)

        results = check_asymmetric_signature(tmp, public_key_path="")
        # Public key must be passed via env var since there's no key file
        # Use the ships.build.json embedded key approach
        _make_build_json(tmp, ships_public_key=public_pem)

        results = check_asymmetric_signature(tmp, public_key_path="")
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].check_name == "asym_signature"


def test_check_asym_sig_fail_mismatch():
    """Tampered ZIP with valid .sig sidecar → passed=False."""
    private_pem, public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp, content=b"original")
        _make_build_json(tmp, ships_public_key=public_pem)
        sign_zip(zip_path, private_pem)

        # Tamper
        with open(zip_path, "wb") as fh:
            fh.write(b"tampered")

        results = check_asymmetric_signature(tmp, public_key_path="")
        assert len(results) == 1
        assert results[0].passed is False
        assert "INVALID" in results[0].message


def test_check_asym_sig_fail_required_absent():
    """require_asymmetric_signature=True but no .sig → passed=False."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_zip(tmp)
        _make_build_json(tmp, require_asymmetric_signature=True)

        results = check_asymmetric_signature(tmp, public_key_path="")
        assert len(results) == 1
        assert results[0].passed is False
        assert "absent" in results[0].message.lower()


def test_check_asym_sig_skip_not_required_no_sig():
    """No .sig sidecar and require_asymmetric_signature=False → silently skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        _make_zip(tmp)
        _make_build_json(tmp, require_asymmetric_signature=False)

        results = check_asymmetric_signature(tmp, public_key_path="")
        assert results == []


def test_check_asym_sig_fail_no_key(monkeypatch):
    """.sig present but no public key available → passed=False."""
    private_pem, _public_pem = generate_keypair()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = _make_zip(tmp)
        _make_build_json(tmp)  # No ships_public_key embedded
        sign_zip(zip_path, private_pem)

        # Ensure no env vars supply a key
        monkeypatch.delenv("SHIPS_PUBLIC_KEY_PATH", raising=False)
        monkeypatch.delenv("SHIPS_PUBLIC_KEY", raising=False)

        results = check_asymmetric_signature(tmp, public_key_path="")
        assert len(results) == 1
        assert results[0].passed is False
        assert "no public key" in results[0].message.lower()
