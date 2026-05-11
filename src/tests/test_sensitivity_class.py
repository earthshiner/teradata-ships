"""
test_sensitivity_class.py — Tests for data sensitivity classification (GAP-009).

Covers:
    - Pass: require_sensitivity_class=false, no .cls file → passes.
    - Pass: require_sensitivity_class=true, .cls with 'PII' → passes.
    - Warn: require_sensitivity_class=true, no .cls → WARNING.
    - Fail: .cls file contains 'TOPSECRET' → ERROR INVALID_SENSITIVITY_CLASS.
    - Manifest: sensitivity_class present in read_sensitivity_class().
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_release_packager.security_rules import (
    scan_sensitivity_class,
    read_sensitivity_class,
    _VALID_SENSITIVITY_CLASSES,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_tbl(tmp_path: Path, filename: str = "D.MyTable.tbl") -> tuple:
    """Write a minimal .tbl file and return (rel_path, file_path)."""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    fp = ddl_dir / filename
    fp.write_text("CREATE MULTISET TABLE D.MyTable (id INT) PRIMARY INDEX(id);", encoding="utf-8")
    rel = os.path.join("ddl", filename)
    return rel, str(fp)


# ---------------------------------------------------------------
# Pass: not required, no .cls
# ---------------------------------------------------------------


def test_sensitivity_class_pass_not_required(tmp_path):
    """require_sensitivity_class=false, no .cls file → no findings."""
    rel, fp = _make_tbl(tmp_path)
    issues = scan_sensitivity_class(rel, fp, require_sensitivity_class=False)
    assert issues == []


# ---------------------------------------------------------------
# Pass: required, present with valid value
# ---------------------------------------------------------------


def test_sensitivity_class_pass_required_present(tmp_path):
    """require_sensitivity_class=true, .cls with 'PII' → no findings."""
    rel, fp = _make_tbl(tmp_path)
    Path(fp.replace(".tbl", ".cls")).write_text("PII\n", encoding="utf-8")
    issues = scan_sensitivity_class(rel, fp, require_sensitivity_class=True)
    assert issues == []


# ---------------------------------------------------------------
# Warn: required, missing .cls
# ---------------------------------------------------------------


def test_sensitivity_class_warn_missing(tmp_path):
    """require_sensitivity_class=true, no .cls → WARNING."""
    rel, fp = _make_tbl(tmp_path)
    issues = scan_sensitivity_class(rel, fp, require_sensitivity_class=True, violation_level="warning")
    assert len(issues) == 1
    assert issues[0].severity == "WARNING"
    assert "MISSING_SENSITIVITY_CLASS" in issues[0].message


# ---------------------------------------------------------------
# Fail: invalid sensitivity class value
# ---------------------------------------------------------------


def test_sensitivity_class_fail_invalid_value(tmp_path):
    """'.cls' file contains 'TOPSECRET' → ERROR INVALID_SENSITIVITY_CLASS."""
    rel, fp = _make_tbl(tmp_path)
    Path(fp.replace(".tbl", ".cls")).write_text("TOPSECRET\n", encoding="utf-8")
    issues = scan_sensitivity_class(rel, fp, require_sensitivity_class=True)
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"
    assert "INVALID_SENSITIVITY_CLASS" in issues[0].message
    assert "TOPSECRET" in issues[0].message


# ---------------------------------------------------------------
# read_sensitivity_class returns the correct value
# ---------------------------------------------------------------


def test_read_sensitivity_class_pii(tmp_path):
    """read_sensitivity_class returns 'PII' when .cls contains PII."""
    rel, fp = _make_tbl(tmp_path)
    Path(fp.replace(".tbl", ".cls")).write_text("pii\n", encoding="utf-8")
    assert read_sensitivity_class(fp) == "PII"


def test_read_sensitivity_class_none_when_absent(tmp_path):
    """read_sensitivity_class returns None when no .cls file exists."""
    rel, fp = _make_tbl(tmp_path)
    assert read_sensitivity_class(fp) is None


# ---------------------------------------------------------------
# All valid class values are accepted
# ---------------------------------------------------------------


@pytest.mark.parametrize("cls_val", sorted(_VALID_SENSITIVITY_CLASSES))
def test_sensitivity_class_all_valid_values(tmp_path, cls_val):
    """Each valid sensitivity class value passes without error."""
    rel, fp = _make_tbl(tmp_path, f"D.Tbl_{cls_val}.tbl")
    Path(fp.replace(".tbl", ".cls")).write_text(cls_val + "\n", encoding="utf-8")
    issues = scan_sensitivity_class(rel, fp, require_sensitivity_class=True)
    assert issues == [], f"Unexpected issues for class '{cls_val}': {issues}"


# ---------------------------------------------------------------
# Integration: validate_directory with sensitivity_class=WARNING
# ---------------------------------------------------------------


def test_validate_directory_sensitivity_class(tmp_path):
    """validate_directory flags MISSING_SENSITIVITY_CLASS when enabled."""
    from td_release_packager.validate import validate_directory

    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "D.MyTable.tbl").write_text(
        "CREATE MULTISET TABLE D.MyTable (id INT) PRIMARY INDEX(id);",
        encoding="utf-8",
    )
    # Enable sensitivity class enforcement at WARNING level
    result = validate_directory(str(tmp_path), rules_config={"sensitivity_class": "WARNING"})
    cls_issues = [i for i in result.issues if i.rule == "sensitivity_class"]
    assert len(cls_issues) >= 1
    assert all(i.severity == "WARNING" for i in cls_issues)
