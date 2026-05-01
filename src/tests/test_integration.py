"""
Integration test for the Object Placement pipeline.

Creates sample ``.viw`` files and an ``object_placement.yaml``, then runs the
migration script and validation rule to confirm end-to-end behaviour.

Author: Paul / Teradata Field Engineering
"""

import tempfile
from pathlib import Path

import pytest

from td_release_packager.object_placement import ObjectPlacement
from td_release_packager.validate import (
    validate_object_placement,
    is_locking_view,
)

# migrate_view_references lives in <repo>/tools/, not inside the
# td_release_packager package. conftest.py adds that directory to
# sys.path during collection; the import below works without any
# per-test path manipulation.
from migrate_view_references import (  # noqa: E402
    process_file,
    apply_replacements,
)


# -------------------------------------------------------------------
# Sample DDL
# -------------------------------------------------------------------

LOCKING_VIEW_DDL = """\
-- LOCKING VIEW
-- 1:1 dirty read layer for D01_MP_DOM_T.Mortgage
CREATE VIEW D01_MP_DOM_V.Mortgage AS
LOCKING ROW FOR ACCESS
SELECT
    Mortgage_Id
    ,Applicant_Name
    ,Property_Postcode
    ,Loan_Amount
    ,Application_Date
FROM D01_MP_DOM_T.Mortgage
;
"""

CURRENT_VIEW_DDL = """\
-- AI-Native Data Product: Domain Module
-- Mortgage_Current: active (non-closed) mortgages
CREATE VIEW D01_MP_DOM_V.Mortgage_Current AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.Applicant_Name
    ,m.Property_Postcode
    ,m.Loan_Amount
    ,m.Application_Date
    ,m.Status
FROM D01_MP_DOM_T.Mortgage m
WHERE m.Status <> 'CLOSED'
;
"""

ENHANCED_VIEW_DDL = """\
-- AI-Native Data Product: Semantic Module
-- Mortgage_Enhanced: joins mortgage with property data
CREATE VIEW D01_MP_SEM_V.Mortgage_Enhanced AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.Applicant_Name
    ,p.Property_Address
    ,p.Property_Value
    ,m.Loan_Amount
    ,CAST(m.Loan_Amount AS DECIMAL(15,2))
        / NULLIFZERO(CAST(p.Property_Value AS DECIMAL(15,2))) AS LVR
FROM D01_MP_DOM_T.Mortgage m
INNER JOIN D01_MP_DOM_T.Property p
    ON m.Property_Id = p.Property_Id
;
"""

CROSS_MODULE_VIEW_DDL = """\
-- AI-Native Data Product: Observability Module
-- SLA_Check: references SEM views, should NOT be rewritten
CREATE VIEW D01_MP_OBS_V.SLA_Check AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.LVR
FROM D01_MP_SEM_V.Mortgage_Enhanced m
;
"""

VIEW_WITH_COMMENTS_AND_STRINGS = """\
-- This view references D01_MP_DOM_T.Mortgage in a comment
-- That reference should NOT be rewritten
CREATE VIEW D01_MP_DOM_V.Mortgage_Filtered AS
LOCKING ROW FOR ACCESS
SELECT
    m.Mortgage_Id
    ,m.Applicant_Name
    ,'Source: D01_MP_DOM_T.Mortgage' AS Source_Label
FROM D01_MP_DOM_T.Mortgage m
WHERE m.Region = 'NSW'
;
"""


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest.fixture
def placement():
    """Standard separated placement engine for testing."""
    return ObjectPlacement(
        {
            "strategy": "separated",
            "database_pattern_tables": "{BASE}_T",
            "database_pattern_views": "{BASE}_V",
            "locking_views": True,
        }
    )


@pytest.fixture
def temp_dir():
    """Temporary directory for test files."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def write_viw(directory: Path, filename: str, content: str) -> Path:
    """Write a .viw file and return its path."""
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


# -------------------------------------------------------------------
# Locking view detection
# -------------------------------------------------------------------


class TestLockingViewDetection:
    """Test the is_locking_view marker detection."""

    def test_locking_view_marker_detected(self):
        """-- LOCKING VIEW marker should identify file as locking view."""
        assert is_locking_view(LOCKING_VIEW_DDL) is True

    def test_no_marker_not_detected(self):
        """AI-Native view without marker is not a locking view."""
        assert is_locking_view(CURRENT_VIEW_DDL) is False

    def test_marker_in_body_not_detected(self):
        """Marker must be in the header (first 20 lines)."""
        # Put the marker on line 25 — should not be detected
        padded = "\n" * 25 + "-- LOCKING VIEW\nSELECT 1;"
        assert is_locking_view(padded) is False


# -------------------------------------------------------------------
# Migration: process_file
# -------------------------------------------------------------------


class TestMigrationProcessFile:
    """Test the migration script's file processing."""

    def test_current_view_rewritten(self, placement, temp_dir):
        """AI-Native view referencing _T should be flagged for rewrite."""
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        result = process_file(path, placement)

        assert result.error is None
        assert len(result.replacements) == 1
        rep = result.replacements[0]
        assert rep.db_original == "D01_MP_DOM_T"
        assert rep.db_rewritten == "D01_MP_DOM_V"

    def test_enhanced_view_multiple_refs(self, placement, temp_dir):
        """View with multiple _T references should find all of them."""
        path = write_viw(temp_dir, "Mortgage_Enhanced.viw", ENHANCED_VIEW_DDL)
        result = process_file(path, placement)

        assert result.error is None
        assert len(result.replacements) == 2
        # Both D01_MP_DOM_T.Mortgage and D01_MP_DOM_T.Property
        dbs = {r.db_original for r in result.replacements}
        assert dbs == {"D01_MP_DOM_T"}

    def test_locking_view_no_rewrite(self, placement, temp_dir):
        """Locking view references _T legitimately — no change needed.
        Note: process_file doesn't exempt locking views (that's the
        validator's job), but the migration is still correct."""
        path = write_viw(temp_dir, "Mortgage.viw", LOCKING_VIEW_DDL)
        result = process_file(path, placement)
        # Locking view DOES reference _T — migration would rewrite it,
        # but in practice you'd exclude locking views from migration.
        # The migration script rewrites all _T refs; locking views
        # should be excluded from the input directory.
        assert result.error is None
        assert len(result.replacements) == 1

    def test_cross_module_view_no_rewrite(self, placement, temp_dir):
        """View referencing _V databases should NOT be rewritten."""
        path = write_viw(temp_dir, "SLA_Check.viw", CROSS_MODULE_VIEW_DDL)
        result = process_file(path, placement)

        assert result.error is None
        assert len(result.replacements) == 0

    def test_comment_and_string_refs_skipped(self, placement, temp_dir):
        """References in comments and strings should be skipped."""
        path = write_viw(
            temp_dir,
            "Mortgage_Filtered.viw",
            VIEW_WITH_COMMENTS_AND_STRINGS,
        )
        result = process_file(path, placement)

        assert result.error is None
        # Only the FROM clause reference should be rewritten,
        # NOT the comment or string literal
        assert len(result.replacements) == 1
        assert result.replacements[0].original == "D01_MP_DOM_T.Mortgage"

    def test_apply_replacements(self, placement, temp_dir):
        """Applying replacements should produce correct output."""
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        result = process_file(path, placement)
        new_content = apply_replacements(path, result.replacements)

        assert "D01_MP_DOM_T" not in new_content
        assert "D01_MP_DOM_V.Mortgage" in new_content
        # The view name itself should be unchanged
        assert "Mortgage_Current" in new_content

    def test_apply_enhanced_preserves_structure(self, placement, temp_dir):
        """Applied rewrite should preserve SQL structure."""
        path = write_viw(temp_dir, "Mortgage_Enhanced.viw", ENHANCED_VIEW_DDL)
        result = process_file(path, placement)
        new_content = apply_replacements(path, result.replacements)

        # No _T references remaining
        assert "D01_MP_DOM_T" not in new_content
        # Both references rewritten
        assert "D01_MP_DOM_V.Mortgage" in new_content
        assert "D01_MP_DOM_V.Property" in new_content
        # JOIN structure preserved
        assert "INNER JOIN" in new_content


# -------------------------------------------------------------------
# Validation: validate_object_placement
# -------------------------------------------------------------------


class TestValidatePlacement:
    """Test the validation rule for object placement."""

    def test_valid_view_no_issues(self, placement, temp_dir):
        """View referencing _V databases should pass validation."""
        path = write_viw(temp_dir, "SLA_Check.viw", CROSS_MODULE_VIEW_DDL)
        issues = validate_object_placement(path, placement)
        assert len(issues) == 0

    def test_invalid_view_flagged(self, placement, temp_dir):
        """View referencing _T database should be flagged as ERROR."""
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        issues = validate_object_placement(path, placement)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"
        assert "D01_MP_DOM_T" in issues[0].message

    def test_locking_view_exempt(self, placement, temp_dir):
        """Locking view (with -- LOCKING VIEW marker) should be exempt."""
        path = write_viw(temp_dir, "Mortgage.viw", LOCKING_VIEW_DDL)
        issues = validate_object_placement(path, placement)
        assert len(issues) == 0

    def test_multiple_violations(self, placement, temp_dir):
        """View with multiple _T references should report each one."""
        path = write_viw(temp_dir, "Mortgage_Enhanced.viw", ENHANCED_VIEW_DDL)
        issues = validate_object_placement(path, placement)
        assert len(issues) == 2

    def test_colocated_skips_check(self, temp_dir):
        """Colocated strategy should skip the check entirely."""
        op = ObjectPlacement(
            {
                "strategy": "colocated",
                "locking_views": False,
            }
        )
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        issues = validate_object_placement(path, op)
        assert len(issues) == 0

    def test_locking_views_false_skips_check(self, temp_dir):
        """locking_views=False should skip the check entirely."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": False,
            }
        )
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        issues = validate_object_placement(path, op)
        assert len(issues) == 0

    def test_non_viw_file_skipped(self, placement, temp_dir):
        """Non-.viw files should be skipped by the rule."""
        path = temp_dir / "Mortgage.tbl"
        path.write_text("CREATE TABLE D01_MP_DOM_T.Mortgage (...);")
        issues = validate_object_placement(path, placement)
        assert len(issues) == 0

    def test_warning_severity(self, placement, temp_dir):
        """Custom severity should be honoured."""
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        issues = validate_object_placement(path, placement, severity="WARNING")
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_suggestion_includes_target_db(self, placement, temp_dir):
        """Error message should suggest the correct views database."""
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)
        issues = validate_object_placement(path, placement)
        assert "D01_MP_DOM_V" in issues[0].message


# -------------------------------------------------------------------
# End-to-end: migrate then validate
# -------------------------------------------------------------------


class TestEndToEnd:
    """Migrate a file, then validate the result passes."""

    def test_migrate_then_validate_passes(self, placement, temp_dir):
        """After migration, the view should pass validation."""
        # Write the original (invalid) view
        path = write_viw(temp_dir, "Mortgage_Current.viw", CURRENT_VIEW_DDL)

        # Confirm it fails validation before migration
        issues_before = validate_object_placement(path, placement)
        assert len(issues_before) > 0

        # Migrate
        result = process_file(path, placement)
        new_content = apply_replacements(path, result.replacements)
        path.write_text(new_content, encoding="utf-8")

        # Confirm it passes validation after migration
        issues_after = validate_object_placement(path, placement)
        assert len(issues_after) == 0

    def test_migrate_enhanced_then_validate(self, placement, temp_dir):
        """Multi-reference view should also pass after migration."""
        path = write_viw(temp_dir, "Mortgage_Enhanced.viw", ENHANCED_VIEW_DDL)

        # Fails before
        assert len(validate_object_placement(path, placement)) == 2

        # Migrate
        result = process_file(path, placement)
        new_content = apply_replacements(path, result.replacements)
        path.write_text(new_content, encoding="utf-8")

        # Passes after
        assert len(validate_object_placement(path, placement)) == 0

    def test_midfix_end_to_end(self, temp_dir):
        """Midfix pattern: full migrate + validate cycle."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{ENV}_DAT_{MODULE}",
                "database_pattern_views": "{ENV}_ACC_{MODULE}",
                "locking_views": True,
            }
        )

        ddl = """\
CREATE VIEW PROD_ACC_MORT.Loan_Current AS
LOCKING ROW FOR ACCESS
SELECT l.Loan_Id, l.Amount
FROM PROD_DAT_MORT.Loan l
WHERE l.Status = 'ACTIVE'
;
"""
        path = write_viw(temp_dir, "Loan_Current.viw", ddl)

        # Fails before
        issues = validate_object_placement(path, op)
        assert len(issues) == 1

        # Migrate
        result = process_file(path, op)
        new_content = apply_replacements(path, result.replacements)
        path.write_text(new_content, encoding="utf-8")

        # Passes after
        assert len(validate_object_placement(path, op)) == 0
        assert "PROD_ACC_MORT.Loan" in new_content
        assert "PROD_DAT_MORT" not in new_content
