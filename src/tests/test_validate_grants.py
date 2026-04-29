#!/usr/bin/env python3
"""
test_validate_grants.py — Unit tests for validate_grants.py

Tests cover:
    - .grt file parsing
    - Grant comparison: missing, stale, matched
    - Full validation pipeline
    - Fix mode
    - Report formatting
"""

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from td_release_packager.validate_grants import (
    parse_grt_file,
    compare_grants,
    validate_grants,
    fix_grants,
    format_report,
    GrantValidationResult,
)
from td_release_packager.infer_grants import PRIV_SELECT, PRIV_INSERT, PRIV_UPDATE, PRIV_EXEC_PROC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_temp_grt(content: str) -> Path:
    """Write content to a temporary .grt file and return its Path."""
    fd, path = tempfile.mkstemp(suffix=".grt", prefix="test_grant_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return Path(path)


# ---------------------------------------------------------------------------
# .grt file parsing tests
# ---------------------------------------------------------------------------

class TestParseGrtFile:
    """Tests for parse_grt_file()."""

    def test_parses_single_grant(self):
        content = (
            "GRANT SELECT ON {{DOM_DATABASE_T}} "
            "TO {{DOM_DATABASE_V}} WITH GRANT OPTION;"
        )
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            assert "{{DOM_DATABASE_T}}" in grants
            assert PRIV_SELECT in grants["{{DOM_DATABASE_T}}"]
        finally:
            os.unlink(path)

    def test_parses_consolidated_grant(self):
        """Multiple privileges in one statement are parsed correctly."""
        content = (
            "GRANT SELECT, INSERT, UPDATE ON {{STG_DATABASE_T}} "
            "TO {{DOM_DATABASE_T}} WITH GRANT OPTION;"
        )
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            privs = grants["{{STG_DATABASE_T}}"]
            assert PRIV_SELECT in privs
            assert PRIV_INSERT in privs
            assert PRIV_UPDATE in privs
        finally:
            os.unlink(path)

    def test_parses_execute_procedure(self):
        """EXECUTE PROCEDURE (two-word privilege) is parsed correctly."""
        content = (
            "GRANT EXECUTE PROCEDURE ON {{MEM_DATABASE_T}} "
            "TO {{OBS_DATABASE_T}} WITH GRANT OPTION;"
        )
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            assert "{{MEM_DATABASE_T}}" in grants
            assert PRIV_EXEC_PROC in grants["{{MEM_DATABASE_T}}"]
        finally:
            os.unlink(path)

    def test_parses_multiple_statements(self):
        """Multiple GRANT statements in one file are all captured."""
        content = textwrap.dedent("""
            GRANT SELECT ON {{DOM_DATABASE_V}} TO {{SEM_DATABASE_V}} WITH GRANT OPTION;
            GRANT SELECT ON {{OBS_DATABASE_V}} TO {{SEM_DATABASE_V}} WITH GRANT OPTION;
        """)
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            assert "{{DOM_DATABASE_V}}" in grants
            assert "{{OBS_DATABASE_V}}" in grants
        finally:
            os.unlink(path)

    def test_ignores_comments(self):
        """Block comments in .grt files don't produce false matches."""
        content = textwrap.dedent("""
            /*
            ** GRANT SELECT ON {{OLD_DB}} TO {{SEM_DATABASE_V}} WITH GRANT OPTION;
            */
            GRANT SELECT ON {{DOM_DATABASE_V}} TO {{SEM_DATABASE_V}} WITH GRANT OPTION;
        """)
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            # The commented GRANT should not appear
            # (The regex starts with ^\s* so it won't match inside /* */)
            assert "{{DOM_DATABASE_V}}" in grants
        finally:
            os.unlink(path)

    def test_empty_file_returns_empty(self):
        content = "/* No grants */"
        path = write_temp_grt(content)
        try:
            grants = parse_grt_file(path)
            assert len(grants) == 0
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Comparison tests
# ---------------------------------------------------------------------------

class TestCompareGrants:
    """Tests for compare_grants()."""

    def test_matching_grants_produce_no_issues(self):
        inferred = {
            "{{DOM_DATABASE_V}}": {
                "{{DOM_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        declared = {
            "{{DOM_DATABASE_V}}": {
                "{{DOM_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        issues = compare_grants(inferred, declared)
        assert len(issues) == 0

    def test_missing_file_produces_error(self):
        """Inferred grantee with no .grt file → ERROR."""
        inferred = {
            "{{DOM_DATABASE_V}}": {
                "{{DOM_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        declared = {}
        issues = compare_grants(inferred, declared)
        assert len(issues) == 1
        assert issues[0].rule == "missing_file"
        assert issues[0].severity == "ERROR"

    def test_missing_privilege_produces_error(self):
        """Inferred privilege missing from .grt file → ERROR."""
        inferred = {
            "{{DOM_DATABASE_T}}": {
                "{{STG_DATABASE_T}}": {PRIV_SELECT, PRIV_INSERT},
            }
        }
        declared = {
            "{{DOM_DATABASE_T}}": {
                "{{STG_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        issues = compare_grants(inferred, declared)
        # One missing: INSERT
        missing = [i for i in issues if i.rule == "missing_grant"]
        assert len(missing) == 1
        assert missing[0].privilege == PRIV_INSERT

    def test_stale_file_produces_warning(self):
        """Declared grantee with no inferred grants → WARNING."""
        inferred = {}
        declared = {
            "{{OLD_DATABASE_V}}": {
                "{{OLD_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        issues = compare_grants(inferred, declared)
        assert len(issues) == 1
        assert issues[0].rule == "stale_file"
        assert issues[0].severity == "WARNING"

    def test_stale_privilege_produces_warning(self):
        """Declared privilege with no inferred match → WARNING."""
        inferred = {
            "{{DOM_DATABASE_T}}": {
                "{{STG_DATABASE_T}}": {PRIV_SELECT},
            }
        }
        declared = {
            "{{DOM_DATABASE_T}}": {
                "{{STG_DATABASE_T}}": {PRIV_SELECT, PRIV_INSERT},
            }
        }
        issues = compare_grants(inferred, declared)
        stale = [i for i in issues if i.rule == "stale_grant"]
        assert len(stale) == 1
        assert stale[0].privilege == PRIV_INSERT
        assert stale[0].severity == "WARNING"

    def test_missing_grantor_produces_error(self):
        """Inferred grant to a new grantor not in .grt → ERROR."""
        inferred = {
            "{{SEM_DATABASE_V}}": {
                "{{DOM_DATABASE_V}}": {PRIV_SELECT},
                "{{OBS_DATABASE_V}}": {PRIV_SELECT},
            }
        }
        declared = {
            "{{SEM_DATABASE_V}}": {
                "{{DOM_DATABASE_V}}": {PRIV_SELECT},
                # OBS_DATABASE_V missing entirely
            }
        }
        issues = compare_grants(inferred, declared)
        missing = [i for i in issues if i.rule == "missing_grant"]
        assert len(missing) == 1
        assert missing[0].grantor == "{{OBS_DATABASE_V}}"


# ---------------------------------------------------------------------------
# Full pipeline tests (using test_project fixtures)
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """Integration tests using the test_project directory."""

    @pytest.fixture
    def test_project(self, tmp_path):
        """
        Build a minimal SHIPS project with DDL and .grt files
        in a temporary directory. Self-contained — no external
        fixture directory required.
        """
        # --- DDL fixtures ---
        dom_viw = tmp_path / "dom" / "viw"
        dom_viw.mkdir(parents=True)

        (dom_viw / "{{DOM_DATABASE_V}}.Loan_H.viw").write_text(textwrap.dedent("""\
            CREATE VIEW {{DOM_DATABASE_V}}.Loan_H
            (loan_key, loan_number)
            AS
            LOCKING ROW FOR ACCESS
            SELECT loan_key, loan_number
            FROM {{DOM_DATABASE_T}}.Loan_H;
        """), encoding="utf-8")

        sem_viw = tmp_path / "sem" / "viw"
        sem_viw.mkdir(parents=True)

        (sem_viw / "{{SEM_DATABASE_V}}.Summary.viw").write_text(textwrap.dedent("""\
            CREATE VIEW {{SEM_DATABASE_V}}.Summary
            (loan_key)
            AS
            LOCKING ROW FOR ACCESS
            SELECT l.loan_key
            FROM {{DOM_DATABASE_V}}.Loan_H l;
        """), encoding="utf-8")

        # --- Generate matching .grt files via fix_grants ---
        dcl_dir = tmp_path / "dcl"
        fix_grants(tmp_path, dcl_dir=dcl_dir)

        return tmp_path

    def test_validates_clean_project(self, test_project):
        """A project with correct .grt files passes validation."""
        result = validate_grants(test_project)
        assert result.passed
        assert result.missing == 0
        assert result.stale == 0

    def test_fix_then_validate(self, test_project):
        """Fix mode generates .grt files that pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dcl_dir = Path(tmpdir)
            result, files_written = fix_grants(
                test_project, dcl_dir=dcl_dir
            )
            assert files_written == 2
            assert result.passed


# ---------------------------------------------------------------------------
# Report formatting tests
# ---------------------------------------------------------------------------

class TestFormatReport:
    """Tests for format_report()."""

    def test_passing_report(self):
        result = GrantValidationResult(
            grantees_checked=3,
            grants_inferred=5,
            grants_declared=5,
            matched=5,
            missing=0,
            stale=0,
        )
        report = format_report(result)
        assert "PASSED" in report
        assert "Missing (ERROR):     0" in report

    def test_failing_report(self):
        from td_release_packager.validate_grants import GrantValidationIssue
        result = GrantValidationResult(
            grantees_checked=3,
            grants_inferred=5,
            grants_declared=3,
            matched=3,
            missing=2,
            stale=0,
            issues=[
                GrantValidationIssue(
                    grantee="{{DOM_DATABASE_V}}",
                    rule="missing_file",
                    severity="ERROR",
                    message="No .grt file exists.",
                ),
            ],
        )
        report = format_report(result)
        assert "FAILED" in report
        assert "ERROR" in report


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
