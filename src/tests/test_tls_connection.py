"""
test_tls_connection.py — Tests for TLS/SSL connection enforcement (GAP-015).

Uses mocked connection parameters — no live database required.

Covers:
    - Pass: connection config includes encryptdata=true → passes.
    - Warn: connection config has no encryption parameters → WARNING.
    - Fail: require_tls=true, no encryption → ERROR.
"""

from __future__ import annotations

import json
from pathlib import Path


from database_package_deployer.preflight import check_tls_connection


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _pkg_dir(tmp_path: Path, require_tls: bool = False) -> str:
    manifest = {
        "package_filename": "PRD_Pkg_BUILD_0001.zip",
        "require_tls": require_tls,
    }
    (tmp_path / "ships.build.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------
# Pass: encryptdata=true
# ---------------------------------------------------------------


def test_tls_pass_encryptdata(tmp_path):
    """connection config with encryptdata='true' → check passes."""
    pkg = _pkg_dir(tmp_path)
    results = check_tls_connection(pkg, {"host": "td", "encryptdata": "true"})
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].severity == "INFO"


def test_tls_pass_sslmode(tmp_path):
    """connection config with sslmode='require' → check passes."""
    pkg = _pkg_dir(tmp_path)
    results = check_tls_connection(pkg, {"host": "td", "sslmode": "require"})
    assert len(results) == 1
    assert results[0].passed is True


# ---------------------------------------------------------------
# Warn: no encryption params
# ---------------------------------------------------------------


def test_tls_warn_no_encryption(tmp_path):
    """No encryption parameters → WARNING."""
    pkg = _pkg_dir(tmp_path, require_tls=False)
    results = check_tls_connection(pkg, {"host": "td"})
    assert len(results) == 1
    assert results[0].severity == "WARNING"
    assert results[0].passed is True  # WARNING = advisory, not blocking


# ---------------------------------------------------------------
# Fail: require_tls=true, no encryption
# ---------------------------------------------------------------


def test_tls_fail_required_not_encrypted(tmp_path):
    """require_tls=true and no encryption → ERROR."""
    pkg = _pkg_dir(tmp_path, require_tls=True)
    results = check_tls_connection(pkg, {"host": "td"})
    assert len(results) == 1
    assert results[0].severity == "ERROR"
    assert not results[0].passed


# ---------------------------------------------------------------
# Skip: no connection params
# ---------------------------------------------------------------


def test_tls_skip_no_params(tmp_path):
    """No connection params → check skipped, no findings."""
    pkg = _pkg_dir(tmp_path)
    assert check_tls_connection(pkg, None) == []
    assert check_tls_connection(pkg, {}) == []
