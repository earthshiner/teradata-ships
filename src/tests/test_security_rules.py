"""
test_security_rules.py — Tests for SECRET_PATTERN_DETECTED inspect rule (GAP-003).

Covers:
    - Pass: clean stored procedure with no secrets → no findings.
    - Fail — password: procedure body contains PASSWORD='abc123' → ERROR at correct line.
    - Fail — JDBC: macro body contains jdbc:teradata:// → ERROR.
    - Fail — private key: DML file contains BEGIN RSA PRIVATE KEY → ERROR.
    - Pass — false positive guard: column named user_password_hash (no assignment) → no finding.
    - Edge: multi-line procedure body, pattern on line N → found at correct line number.
    - Integration: validate_directory picks up secret_scan findings.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_release_packager.security_rules import scan_secret_patterns, scan_dynamic_sql
from td_release_packager.validate import ValidationResult


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _rel(subdir: str, filename: str) -> str:
    """Build a rel_path that looks like it came from scan_secret_patterns."""
    return os.path.join(subdir, filename)


def _abs(tmp_path: Path, subdir: str, filename: str, content: str) -> tuple:
    """Write content to tmp_path/subdir/filename and return (rel_path, file_path)."""
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    fp = d / filename
    fp.write_text(content, encoding="utf-8")
    rel = os.path.join(subdir, filename)
    return rel, str(fp)


# ---------------------------------------------------------------
# Pass: clean procedure body — no secrets
# ---------------------------------------------------------------


def test_secret_scan_pass_clean_procedure(tmp_path):
    """Clean stored procedure with no secrets → no findings."""
    content = (
        "REPLACE PROCEDURE MyDb.MyProc()\n"
        "BEGIN\n"
        "  SELECT * FROM MyDb.MyTable;\n"
        "END;\n"
    )
    rel, fp = _abs(tmp_path, "ddl", "MyProc.spl", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert issues == []


# ---------------------------------------------------------------
# Fail: inline PASSWORD assignment
# ---------------------------------------------------------------


def test_secret_scan_fail_password(tmp_path):
    """PASSWORD='abc123' in procedure body → ERROR at correct line."""
    content = (
        "REPLACE PROCEDURE MyDb.Proc()\n"
        "BEGIN\n"
        "  SET v_conn = 'jdbc:teradata://host';\n"
        "  -- connection password\n"
        "  SET v_pw = PASSWORD='abc123';\n"  # line 5
        "END;\n"
    )
    rel, fp = _abs(tmp_path, "ddl", "Proc.spl", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert len(issues) >= 1
    assert any(i.severity == "ERROR" for i in issues)
    # Matched text must NOT appear in the message (no 'abc123')
    for i in issues:
        assert "abc123" not in i.message
    # Line number should point to line 5
    pw_issue = next(i for i in issues if "PASSWORD" in i.message)
    assert pw_issue.line == 5


# ---------------------------------------------------------------
# Fail: JDBC connection string in macro body
# ---------------------------------------------------------------


def test_secret_scan_fail_jdbc(tmp_path):
    """jdbc:teradata:// in macro body → ERROR."""
    content = (
        "REPLACE MACRO MyDb.MyMacro AS (\n"
        "  SELECT 'jdbc:teradata://prodhost/database' AS conn_str\n"  # line 2
        ");\n"
    )
    rel, fp = _abs(tmp_path, "ddl", "MyMacro.mcr", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert len(issues) >= 1
    assert any(i.severity == "ERROR" and "JDBC" in i.message for i in issues)


# ---------------------------------------------------------------
# Fail: private key header in DML file
# ---------------------------------------------------------------


def test_secret_scan_fail_private_key(tmp_path):
    """BEGIN RSA PRIVATE KEY in DML file → ERROR."""
    content = (
        "-- seed data\n"
        "INSERT INTO MyDb.Keys (key_type, key_data) VALUES\n"
        "('rsa', '-----BEGIN RSA PRIVATE KEY-----');\n"  # line 3
    )
    rel, fp = _abs(tmp_path, "dml", "seed.dml", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert len(issues) >= 1
    assert any("Private key" in i.message for i in issues)
    pk_issue = next(i for i in issues if "Private key" in i.message)
    assert pk_issue.line == 3


# ---------------------------------------------------------------
# Pass — false positive guard: column name only (no assignment)
# ---------------------------------------------------------------


def test_secret_scan_no_false_positive_column_name(tmp_path):
    """Column named user_password_hash without assignment → no finding."""
    content = (
        "CREATE TABLE MyDb.Users (\n"
        "    user_id           INTEGER     NOT NULL\n"
        "   ,user_password_hash VARCHAR(64) NOT NULL\n"
        "   ,created_at        TIMESTAMP\n"
        ") PRIMARY INDEX (user_id);\n"
    )
    rel, fp = _abs(tmp_path, "ddl", "Users.tbl", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert issues == []


# ---------------------------------------------------------------
# Edge: multi-line body, pattern on specific line
# ---------------------------------------------------------------


def test_secret_scan_multiline_correct_line(tmp_path):
    """Pattern found in a multi-line body → line number is correct."""
    lines = [
        "REPLACE PROCEDURE MyDb.Proc ()\n",   # 1
        "BEGIN\n",                             # 2
        "  DECLARE v_x INTEGER;\n",            # 3
        "  SET v_x = 1;\n",                   # 4
        "  SET v_x = 2;\n",                   # 5
        "  SET v_x = 3;\n",                   # 6
        "  SET v_pw = PWD='s3cret!';\n",       # 7  ← pattern here
        "END;\n",                              # 8
    ]
    content = "".join(lines)
    rel, fp = _abs(tmp_path, "ddl", "Proc.spl", content)
    issues = scan_secret_patterns(rel, content, fp)
    assert any(i.line == 7 for i in issues), [i.line for i in issues]


# ---------------------------------------------------------------
# Scoping: file outside target directories → not scanned
# ---------------------------------------------------------------


def test_secret_scan_skips_non_target_directory(tmp_path):
    """File outside ddl/viw/dml/dcl is not scanned (no false positives)."""
    content = "SET v_pw = PASSWORD='abc123';\n"
    rel = os.path.join("config", "something.conf")
    fp = str(tmp_path / "config" / "something.conf")
    issues = scan_secret_patterns(rel, content, fp)
    assert issues == []


# ---------------------------------------------------------------
# Integration: validate_directory picks up secret_scan findings
# ---------------------------------------------------------------


def test_validate_directory_secret_scan_integration(tmp_path):
    """validate_directory surfaces SECRET_PATTERN_DETECTED findings."""
    from td_release_packager.validate import validate_directory

    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    # Place a file with a PASSWORD pattern
    (ddl_dir / "MyProc.spl").write_text(
        "REPLACE PROCEDURE D.P ()\nBEGIN\n  SET v = PASSWORD='bad1';\nEND;\n",
        encoding="utf-8",
    )
    result: ValidationResult = validate_directory(str(tmp_path))
    assert result.errors >= 1
    secret_issues = [i for i in result.issues if i.rule == "secret_scan"]
    assert len(secret_issues) >= 1
    assert all(i.severity == "ERROR" for i in secret_issues)
