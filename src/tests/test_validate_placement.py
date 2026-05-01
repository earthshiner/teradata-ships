"""
Test object_placement rule integration in validate.py.

Verifies the new rule works within the existing validation
pipeline — rule config, severity mapping, --strict mode,
and correct file filtering.

Author: Paul / Teradata Field Engineering
"""

from pathlib import Path

import pytest

from td_release_packager.object_placement import ObjectPlacement
from td_release_packager.validate import (
    DEFAULT_RULES,
    generate_default_config,
    validate_directory,
)


# -------------------------------------------------------------------
# Sample DDL
# -------------------------------------------------------------------

LOCKING_VIEW = """\
-- LOCKING VIEW
CREATE VIEW {{DB_V}}.Mortgage AS
LOCKING ROW FOR ACCESS
SELECT
    Mortgage_Id
    ,Applicant_Name
FROM {{DB_T}}.Mortgage
;
"""

BAD_VIEW = """\
-- AI-Native Data Product: Domain Module
CREATE VIEW D01_MP_DOM_V.Mortgage_Current AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.Applicant_Name
    ,m.Status
FROM D01_MP_DOM_T.Mortgage m
WHERE m.Status <> 'CLOSED'
;
"""

GOOD_VIEW = """\
-- AI-Native Data Product: Domain Module
CREATE VIEW D01_MP_DOM_V.Mortgage_Current AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.Applicant_Name
    ,m.Status
FROM D01_MP_DOM_V.Mortgage m
WHERE m.Status <> 'CLOSED'
;
"""

GOOD_TABLE = """\
CREATE MULTISET TABLE D01_MP_DOM_T.Mortgage
    ,FALLBACK
    ,NO BEFORE JOURNAL
    ,NO AFTER JOURNAL
(
    Mortgage_Id     INTEGER NOT NULL
    ,Applicant_Name VARCHAR(200) NOT NULL
    ,Status         VARCHAR(20) NOT NULL
)
PRIMARY INDEX (Mortgage_Id)
;
"""


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest.fixture
def placement():
    """Standard separated placement engine."""
    return ObjectPlacement(
        {
            "strategy": "separated",
            "database_pattern_tables": "{BASE}_T",
            "database_pattern_views": "{BASE}_V",
            "locking_views": True,
        }
    )


@pytest.fixture
def temp_project(tmp_path):
    """Create a minimal project directory structure."""
    viw_dir = tmp_path / "domain" / "viw"
    viw_dir.mkdir(parents=True)
    tbl_dir = tmp_path / "domain" / "tbl"
    tbl_dir.mkdir(parents=True)
    return tmp_path


def write_file(directory: Path, filename: str, content: str) -> Path:
    """Write a file and return its path."""
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# -------------------------------------------------------------------
# Rule presence in defaults
# -------------------------------------------------------------------


class TestRuleRegistration:
    """Verify the rule is properly registered."""

    def test_object_placement_in_default_rules(self):
        """Rule should be in DEFAULT_RULES."""
        assert "object_placement" in DEFAULT_RULES

    def test_default_severity_is_error(self):
        """Default severity should be ERROR."""
        assert DEFAULT_RULES["object_placement"] == "ERROR"

    def test_rule_in_generated_config(self):
        """Rule should appear in generated inspect.conf."""
        config = generate_default_config()
        assert "object_placement=ERROR" in config


# -------------------------------------------------------------------
# Integration with validate_directory
# -------------------------------------------------------------------


class TestValidateDirectoryIntegration:
    """Test the rule within the full validation pipeline."""

    def test_bad_view_detected(self, temp_project, placement):
        """View referencing _T should produce an ERROR."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        # Find object_placement issues
        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 1
        assert op_issues[0].severity == "ERROR"
        assert "D01_MP_DOM_T" in op_issues[0].message

    def test_good_view_passes(self, temp_project, placement):
        """View referencing _V should not trigger the rule."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            GOOD_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 0

    def test_locking_view_exempt(self, temp_project, placement):
        """Locking view with -- LOCKING VIEW marker should be exempt."""
        write_file(
            temp_project / "domain" / "viw",
            "DB_V.Mortgage.viw",
            LOCKING_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 0

    def test_table_files_not_checked(self, temp_project, placement):
        """Table files (.tbl) should not trigger object_placement."""
        write_file(
            temp_project / "domain" / "tbl",
            "D01_MP_DOM_T.Mortgage.tbl",
            GOOD_TABLE,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 0

    def test_no_placement_skips_rule(self, temp_project):
        """Without placement engine, rule is skipped silently."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        # No placement parameter — rule inactive
        result = validate_directory(str(temp_project))

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 0

    def test_rule_off_in_config(self, temp_project, placement):
        """Rule set to OFF in inspect.conf should suppress issues."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        rules = dict(DEFAULT_RULES)
        rules["object_placement"] = "OFF"

        result = validate_directory(
            str(temp_project),
            rules_config=rules,
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 0

    def test_rule_warning_in_config(self, temp_project, placement):
        """Rule set to WARNING should produce WARNING not ERROR."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        rules = dict(DEFAULT_RULES)
        rules["object_placement"] = "WARNING"

        result = validate_directory(
            str(temp_project),
            rules_config=rules,
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 1
        assert op_issues[0].severity == "WARNING"

    def test_strict_mode_promotes_warning(self, temp_project, placement):
        """--strict should promote WARNING rules to ERROR."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        rules = dict(DEFAULT_RULES)
        rules["object_placement"] = "WARNING"

        result = validate_directory(
            str(temp_project),
            rules_config=rules,
            strict=True,
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 1
        assert op_issues[0].severity == "ERROR"

    def test_line_number_reported(self, temp_project, placement):
        """Issue should include the correct line number."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert len(op_issues) == 1
        # The FROM D01_MP_DOM_T.Mortgage line
        assert op_issues[0].line is not None
        assert op_issues[0].line > 0

    def test_suggestion_in_message(self, temp_project, placement):
        """Error message should suggest the correct views database."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        op_issues = [i for i in result.issues if i.rule == "object_placement"]
        assert "D01_MP_DOM_V" in op_issues[0].message

    def test_counts_include_placement_errors(self, temp_project, placement):
        """Placement errors should be counted in the result totals."""
        write_file(
            temp_project / "domain" / "viw",
            "D01_MP_DOM_V.Mortgage_Current.viw",
            BAD_VIEW,
        )

        result = validate_directory(
            str(temp_project),
            placement=placement,
        )

        assert result.errors > 0
        assert result.passed is False
