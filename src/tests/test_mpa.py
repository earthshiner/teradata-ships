"""
test_mpa.py — Tests for the multi-person authorisation (4-eyes) check (GAP-006).

Covers:
    - Pass: require_approvals=1, no approval code → passes (skipped).
    - Pass: require_approvals=2, valid code for today → passes.
    - Fail — expired: require_approvals=2, code for yesterday → ERROR.
    - Fail — invalid: require_approvals=2, wrong HMAC → ERROR.
    - Fail — not provided: require_approvals=2, no code → ERROR.
    - ships approve command: produces a code the preflight check accepts.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


from database_package_deployer.mpa import (
    generate_approval_code,
    verify_approval_code,
)
from database_package_deployer.preflight import check_mpa_approval


_KEY = b"test-key-do-not-use"


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_zip(tmp_path: Path, zip_name: str = "PRD_Pkg_BUILD_0001.zip") -> Path:
    zip_path = tmp_path / zip_name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "context/ships.build.json", json.dumps({"package_filename": zip_name})
        )
    return zip_path


def _write_build_json(pkg_dir: Path, zip_name: str, **extra) -> None:
    manifest = {"package_filename": zip_name, **extra}
    context_dir = pkg_dir / "context"
    context_dir.mkdir(exist_ok=True)
    (context_dir / "ships.build.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


# ---------------------------------------------------------------
# Pass: not required (require_approvals=1)
# ---------------------------------------------------------------


def test_mpa_pass_not_required(tmp_path):
    """require_approvals=1 → check passes (skipped), no code needed."""
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_approvals=1)
    results = check_mpa_approval(str(pkg_dir), approval_code="")
    assert results == []


# ---------------------------------------------------------------
# Pass: require_approvals=2, valid code for today
# ---------------------------------------------------------------


def test_mpa_pass_valid_code(tmp_path, monkeypatch):
    """require_approvals=2, valid approval code for today → passes."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_approvals=2)

    code = generate_approval_code(str(zip_path))
    assert code is not None

    results = check_mpa_approval(str(pkg_dir), approval_code=code)
    assert len(results) == 1
    assert results[0].passed is True


# ---------------------------------------------------------------
# Fail: code from yesterday is rejected
# ---------------------------------------------------------------


def test_mpa_fail_expired_code(tmp_path, monkeypatch):
    """Code generated with yesterday's date → ERROR (expired)."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    zip_path = _make_zip(tmp_path, zip_name)
    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_approvals=2)

    # Generate a code using yesterday's date
    from database_package_deployer.preflight import _sha256_of_file
    from database_package_deployer.signing import compute_hmac

    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    zip_hash = _sha256_of_file(str(zip_path))
    yesterday_code = compute_hmac(_KEY, f"{zip_hash}:{yesterday}")

    results = check_mpa_approval(str(pkg_dir), approval_code=yesterday_code)
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"


# ---------------------------------------------------------------
# Fail: wrong HMAC
# ---------------------------------------------------------------


def test_mpa_fail_invalid_code(tmp_path, monkeypatch):
    """Garbage approval code → ERROR."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    _make_zip(tmp_path, zip_name)
    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_approvals=2)

    results = check_mpa_approval(str(pkg_dir), approval_code="a" * 64)
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"


# ---------------------------------------------------------------
# Fail: code not provided
# ---------------------------------------------------------------


def test_mpa_fail_not_provided(tmp_path):
    """require_approvals=2, no approval code → ERROR."""
    zip_name = "PRD_Pkg_BUILD_0001.zip"
    pkg_dir = tmp_path / "PRD_Pkg_BUILD_0001"
    pkg_dir.mkdir()
    _write_build_json(pkg_dir, zip_name, require_approvals=2)
    results = check_mpa_approval(str(pkg_dir), approval_code="")
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].severity == "ERROR"
    assert "ships approve" in results[0].message


# ---------------------------------------------------------------
# ships approve produces a verifiable code
# ---------------------------------------------------------------


def test_ships_approve_code_accepted(tmp_path, monkeypatch):
    """generate_approval_code produces a code that verify_approval_code accepts."""
    monkeypatch.setenv("SHIPS_SIGNING_KEY", _KEY.decode())
    zip_path = _make_zip(tmp_path)
    code = generate_approval_code(str(zip_path))
    assert code is not None
    assert verify_approval_code(str(zip_path), code) is True
