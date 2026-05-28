"""
Tests for td_release_packager.validate_grants — the cross-file
grant orchestrator (Step 2 of the validate command).

Covers:
    - Lifecycle: missing → fix → consistent → drift → orphan
    - The .grt parser (_parse_grt_content)
    - Drift detection edge cases
    - Custom dcl_dir
    - Multi-source consolidation
    - Report rendering

The orchestrator delegates analysis to infer_grants.py, so tests
build small SHIPS-shaped projects on disk and exercise the public
API end-to-end — they do not mock infer_grants. This means the
tests double as integration tests for the infer_grants ↔
validate_grants seam.
"""

import pytest
from pathlib import Path

from td_release_packager.validate_grants import (
    GrantValidationResult,
    GranteeStatus,
    _compute_drift,
    _parse_grt_content,
    _resolve_dcl_dir,
    fix_grants,
    format_report,
    validate_grants,
)


# -------------------------------------------------------------------
# Helpers — build a tiny SHIPS-shaped project with view DDL
# -------------------------------------------------------------------


def _make_view_ddl(grantee: str, grantor: str, view_name: str = "V") -> str:
    """Generate a minimal CREATE VIEW that implies SELECT on grantor."""
    return (
        f"CREATE VIEW {grantee}.{view_name} (col1) AS\n"
        f"LOCKING ROW FOR ACCESS\n"
        f"SELECT col1 FROM {grantor}.SomeTable;\n"
    )


def _make_view_with_two_sources(grantee: str, g1: str, g2: str) -> str:
    """View that JOINs across two source databases — implies SELECT on both."""
    return (
        f"CREATE VIEW {grantee}.Joined (id, name, amount) AS\n"
        f"LOCKING ROW FOR ACCESS\n"
        f"SELECT a.id, a.name, b.amount\n"
        f"FROM {g1}.TableA a\n"
        f"INNER JOIN {g2}.TableB b ON a.id = b.id;\n"
    )


@pytest.fixture
def project(tmp_path):
    """Empty SHIPS-like project root."""
    views_dir = tmp_path / "payload" / "database" / "DDL" / "views"
    views_dir.mkdir(parents=True)
    return tmp_path


def _add_view(project: Path, filename: str, content: str) -> Path:
    views_dir = project / "payload" / "database" / "DDL" / "views"
    path = views_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _dcl_dir(project: Path) -> Path:
    return project / "payload" / "database" / "DCL" / "inter_db"


def _role_dcl_dir(project: Path) -> Path:
    return project / "payload" / "database" / "DCL" / "roles"


# ===================================================================
# Lifecycle: missing → fix → consistent → drift → orphan
# ===================================================================


class TestLifecycle:
    def test_empty_project_passes(self, tmp_path):
        """No DDL files → no inferred grants → passed=True."""
        result = validate_grants(tmp_path)
        assert result.passed
        assert result.ddl_count == 0
        assert result.statuses == []

    def test_missing_dcl_files_detected(self, project):
        """View DDL exists but no .dcl file — grantee classified as missing."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.missing) == 1
        assert result.missing[0].grantee == "{{DOM_V}}"
        assert result.missing[0].expected_grants == {"{{DOM_T}}": {"SELECT"}}

    def test_fix_writes_missing_files(self, project):
        """fix_grants creates .dcl files; post-fix result is consistent."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        result, files_written = fix_grants(project)
        assert files_written == 1
        assert result.passed
        assert (_dcl_dir(project) / "{{DOM_V}}.dcl").exists()

    def test_fix_ignores_resolved_downstream_copies(self, project):
        """--fix-grants writes DCL from tokenised payload, not .ships copies."""
        _add_view(
            project,
            "{{DB_DOMAIN_V}}.Agent_Current.viw",
            _make_view_ddl("{{DB_DOMAIN_V}}", "{{DB_DOMAIN_T}}"),
        )
        resolved_copy = (
            project
            / ".ships"
            / "harvest"
            / "by_database"
            / "DB_DOMAIN_V"
            / "views"
            / "CallCentre_DOM_STD_V.Agent_Current.viw"
        )
        resolved_copy.parent.mkdir(parents=True, exist_ok=True)
        resolved_copy.write_text(
            _make_view_ddl("CallCentre_DOM_STD_V", "CallCentre_DOM_STD_T"),
            encoding="utf-8",
        )

        result, files_written = fix_grants(project)

        assert files_written == 1
        assert result.passed
        assert (_dcl_dir(project) / "{{DB_DOMAIN_V}}.dcl").exists()
        assert not (_dcl_dir(project) / "CallCentre_DOM_STD_V.dcl").exists()

    def test_validate_after_fix_is_consistent(self, project):
        """Re-validate after fix — clean state."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        result = validate_grants(project)
        assert result.passed
        assert len(result.consistent) == 1

    def test_drift_detected_after_corruption(self, project):
        """Modify a .dcl to remove a privilege — drift detected."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        # Replace SELECT with INSERT — drift in BOTH directions
        grt = _dcl_dir(project) / "{{DOM_V}}.dcl"
        grt.write_text(
            grt.read_text(encoding="utf-8").replace("SELECT", "INSERT"),
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.drifted) == 1
        d = result.drifted[0]
        assert d.missing_privs == {"{{DOM_T}}": {"SELECT"}}
        assert d.extra_privs == {"{{DOM_T}}": {"INSERT"}}

    def test_orphan_detected(self, project):
        """A .dcl for a grantee with no DDL backing — orphaned."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        # Add a stray .dcl for a grantee that no DDL references
        orphan = _dcl_dir(project) / "{{LEGACY_V}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{LEGACY_T}} TO {{LEGACY_V}} WITH GRANT OPTION;\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.orphaned) == 1
        assert result.orphaned[0].grantee == "{{LEGACY_V}}"

    def test_fix_does_not_delete_orphans(self, project):
        """Fix mode is conservative — orphans are preserved for manual review."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        orphan = _dcl_dir(project) / "{{LEGACY_V}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{LEGACY_T}} TO {{LEGACY_V}} WITH GRANT OPTION;\n",
            encoding="utf-8",
        )
        result, _files = fix_grants(project)
        assert orphan.exists()  # Not deleted
        # And still flagged in the post-fix result
        assert len(result.orphaned) == 1

    def test_database_role_grant_file_lives_under_roles(self, project):
        """Database-to-role DCL belongs under DCL/roles."""
        dcl = _role_dcl_dir(project)
        dcl.mkdir(parents=True)
        (dcl / "{{DB_DOMAIN_T}}.dcl").write_text(
            (
                "GRANT SELECT, INSERT, UPDATE, DELETE ON {{DB_DOMAIN_T}} "
                "TO {{DB_DOMAIN_T}}_WRITE_ROLE;\n"
            ),
            encoding="utf-8",
        )

        result = validate_grants(project)

        assert result.passed
        assert result.orphaned == []

    def test_fix_creates_missing_role_files_for_role_grants(self, project):
        """Role grantees in DCL are materialised as package role DDL."""
        dcl = _role_dcl_dir(project)
        dcl.mkdir(parents=True)
        (dcl / "{{DB_DOMAIN_T}}.dcl").write_text(
            (
                "GRANT SELECT, INSERT, UPDATE, DELETE ON {{DB_DOMAIN_T}} "
                "TO {{DB_DOMAIN_T}}_WRITE_ROLE;\n"
            ),
            encoding="utf-8",
        )

        result, files_written = fix_grants(project)

        role_dir = project / "payload" / "database" / "system" / "roles"
        assert result.passed
        assert files_written == 1
        assert (role_dir / "{{DB_DOMAIN_T}}_WRITE_ROLE.rol").read_text(
            encoding="utf-8"
        ) == "CREATE ROLE {{DB_DOMAIN_T}}_WRITE_ROLE;\n"

    def test_existing_grt_file_satisfies_inter_db_grant(self, project):
        """Legacy/generated .grt files are accepted as DCL scripts."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        dcl = _dcl_dir(project)
        dcl.mkdir(parents=True)
        (dcl / "{{DOM_V}}.grt").write_text(
            "GRANT SELECT ON {{DOM_T}} TO {{DOM_V}} WITH GRANT OPTION;\n",
            encoding="utf-8",
        )

        result = validate_grants(project)

        assert result.passed
        assert result.consistent[0].file_path.name == "{{DOM_V}}.grt"


# ===================================================================
# .grt parser — _parse_grt_content
# ===================================================================


class TestParser:
    def test_single_grant(self):
        content = "GRANT SELECT ON {{T}} TO {{V}} WITH GRANT OPTION;\n"
        assert _parse_grt_content(content) == {"{{T}}": {"SELECT"}}

    def test_multiple_privileges(self):
        content = "GRANT SELECT, INSERT, UPDATE ON {{T}} TO {{V}};\n"
        assert _parse_grt_content(content) == {"{{T}}": {"SELECT", "INSERT", "UPDATE"}}

    def test_multi_word_privilege(self):
        content = "GRANT EXECUTE PROCEDURE ON {{P}} TO {{V}};\n"
        assert _parse_grt_content(content) == {"{{P}}": {"EXECUTE PROCEDURE"}}

    def test_multiple_grants_same_grantor_merged(self):
        """Two GRANTs for the same grantor — privileges union."""
        content = "GRANT SELECT ON {{T}} TO {{V}};\nGRANT INSERT ON {{T}} TO {{V}};\n"
        assert _parse_grt_content(content) == {"{{T}}": {"SELECT", "INSERT"}}

    def test_multiple_grants_different_grantors(self):
        content = "GRANT SELECT ON {{A}} TO {{V}};\nGRANT SELECT ON {{B}} TO {{V}};\n"
        assert _parse_grt_content(content) == {
            "{{A}}": {"SELECT"},
            "{{B}}": {"SELECT"},
        }

    def test_comments_stripped(self):
        content = (
            "/* Header comment */\n"
            "-- Line comment GRANT INSERT ON {{X}} TO {{Y}};\n"
            "GRANT SELECT ON {{T}} TO {{V}};\n"
        )
        # Only the real GRANT is parsed
        assert _parse_grt_content(content) == {"{{T}}": {"SELECT"}}

    def test_case_insensitive_keywords(self):
        content = "grant select on {{T}} to {{V}};\n"
        assert _parse_grt_content(content) == {"{{T}}": {"SELECT"}}

    def test_empty_content(self):
        assert _parse_grt_content("") == {}

    def test_only_comments(self):
        assert _parse_grt_content("/* nothing */\n-- empty file\n") == {}

    def test_token_with_role_suffix_grantee_is_parsed(self):
        content = "GRANT SELECT ON {{DB_DOMAIN_T}} TO {{DB_DOMAIN_T}}_READ_ROLE;\n"
        assert _parse_grt_content(content) == {"{{DB_DOMAIN_T}}": {"SELECT"}}


# ===================================================================
# Drift computation — _compute_drift
# ===================================================================


class TestComputeDrift:
    def test_identical_no_drift(self):
        a = {"{{T}}": {"SELECT"}}
        missing, extra = _compute_drift(a, a)
        assert missing == {}
        assert extra == {}

    def test_missing_privilege(self):
        expected = {"{{T}}": {"SELECT", "INSERT"}}
        actual = {"{{T}}": {"SELECT"}}
        missing, extra = _compute_drift(expected, actual)
        assert missing == {"{{T}}": {"INSERT"}}
        assert extra == {}

    def test_extra_privilege(self):
        expected = {"{{T}}": {"SELECT"}}
        actual = {"{{T}}": {"SELECT", "DELETE"}}
        missing, extra = _compute_drift(expected, actual)
        assert missing == {}
        assert extra == {"{{T}}": {"DELETE"}}

    def test_missing_grantor_entirely(self):
        expected = {"{{A}}": {"SELECT"}, "{{B}}": {"SELECT"}}
        actual = {"{{A}}": {"SELECT"}}
        missing, extra = _compute_drift(expected, actual)
        assert missing == {"{{B}}": {"SELECT"}}
        assert extra == {}

    def test_extra_grantor_entirely(self):
        expected = {"{{A}}": {"SELECT"}}
        actual = {"{{A}}": {"SELECT"}, "{{B}}": {"SELECT"}}
        missing, extra = _compute_drift(expected, actual)
        assert missing == {}
        assert extra == {"{{B}}": {"SELECT"}}


# ===================================================================
# Multi-source consolidation
# ===================================================================


class TestMultiSourceConsolidation:
    """When multiple DDL files imply grants for the same grantee, the
    .dcl should consolidate them — privilege union per grantor."""

    def test_two_views_same_grantee_different_grantors(self, project):
        _add_view(
            project,
            "{{V}}.A.viw",
            _make_view_ddl("{{V}}", "{{T1}}", "A"),
        )
        _add_view(
            project,
            "{{V}}.B.viw",
            _make_view_ddl("{{V}}", "{{T2}}", "B"),
        )
        result, _ = fix_grants(project)
        assert result.passed

        grt = (_dcl_dir(project) / "{{V}}.dcl").read_text(encoding="utf-8")
        # Both grantors must appear in the file
        assert "{{T1}}" in grt
        assert "{{T2}}" in grt

    def test_view_joining_two_sources(self, project):
        """A single view JOINing two databases implies SELECT on both."""
        _add_view(
            project,
            "{{V}}.Joined.viw",
            _make_view_with_two_sources("{{V}}", "{{T1}}", "{{T2}}"),
        )
        result, _ = fix_grants(project)
        assert result.passed

        actual = _parse_grt_content(
            (_dcl_dir(project) / "{{V}}.dcl").read_text(encoding="utf-8")
        )
        assert actual == {"{{T1}}": {"SELECT"}, "{{T2}}": {"SELECT"}}

    def test_multiple_grantees_distinct_files(self, project):
        """Two grantees → two separate .dcl files."""
        _add_view(
            project,
            "{{V1}}.A.viw",
            _make_view_ddl("{{V1}}", "{{T}}", "A"),
        )
        _add_view(
            project,
            "{{V2}}.B.viw",
            _make_view_ddl("{{V2}}", "{{T}}", "B"),
        )
        result, files_written = fix_grants(project)
        assert files_written == 2
        assert result.passed
        assert (_dcl_dir(project) / "{{V1}}.dcl").exists()
        assert (_dcl_dir(project) / "{{V2}}.dcl").exists()

    def test_macro_dml_through_view_emits_two_hop_grants(self, project):
        """Macro owner gets view DB grants; view owner gets base DB grants."""
        views_dir = project / "payload" / "database" / "DDL" / "views"
        macros_dir = project / "payload" / "database" / "DDL" / "macros"
        macros_dir.mkdir(parents=True)
        (views_dir / "GDEV1V_GCFR.GCFR_Process_Type_Param.viw").write_text(
            (
                "REPLACE VIEW GDEV1V_GCFR.GCFR_Process_Type_Param AS\n"
                "SELECT Process_Type, Param_Group, Param_Name\n"
                "FROM GDEV1T_GCFR.GCFR_Process_Type_Param;\n"
            ),
            encoding="utf-8",
        )
        (macros_dir / "GDEV1M_GCFR.GCFR_Reg_Process_Type_Param.mcr").write_text(
            (
                "REPLACE MACRO GDEV1M_GCFR.GCFR_Reg_Process_Type_Param AS\n"
                "(\n"
                "    DELETE FROM GDEV1V_GCFR.GCFR_Process_Type_Param\n"
                "    WHERE Process_Type = :Process_Type;\n"
                "\n"
                "    INSERT INTO GDEV1V_GCFR.GCFR_Process_Type_Param\n"
                "    (Process_Type, Param_Group, Param_Name)\n"
                "    SELECT :Process_Type, :Param_Group, :Param_Name;\n"
                "\n"
                "    SELECT Process_Type, Param_Group, Param_Name\n"
                "    FROM GDEV1V_GCFR.GCFR_Process_Type_Param;\n"
                ");\n"
            ),
            encoding="utf-8",
        )

        result, files_written = fix_grants(project)

        assert result.passed
        assert files_written == 2
        macro_grants = _parse_grt_content(
            (_dcl_dir(project) / "GDEV1M_GCFR.dcl").read_text(encoding="utf-8")
        )
        view_grants = _parse_grt_content(
            (_dcl_dir(project) / "GDEV1V_GCFR.dcl").read_text(encoding="utf-8")
        )
        assert macro_grants == {
            "GDEV1V_GCFR": {"DELETE", "INSERT", "SELECT"},
        }
        assert view_grants == {
            "GDEV1T_GCFR": {"DELETE", "INSERT", "SELECT"},
        }


# ===================================================================
# Custom dcl_dir
# ===================================================================


class TestCustomDclDir:
    def test_custom_dcl_dir_is_honoured(self, project, tmp_path):
        """Files written to and read from the supplied dcl_dir, not default."""
        _add_view(
            project,
            "{{V}}.A.viw",
            _make_view_ddl("{{V}}", "{{T}}"),
        )
        custom_dcl = tmp_path / "elsewhere"
        result, files_written = fix_grants(project, dcl_dir=custom_dcl)
        assert files_written == 1
        assert (custom_dcl / "{{V}}.dcl").exists()
        assert not _dcl_dir(project).exists()

        # Validate against the same custom dir → consistent
        result = validate_grants(project, dcl_dir=custom_dcl)
        assert result.passed

    def test_default_dcl_dir_resolution(self, tmp_path):
        """_resolve_dcl_dir applies the SHIPS default when None."""
        result = _resolve_dcl_dir(tmp_path, None)
        assert result == tmp_path / "payload" / "database" / "DCL" / "inter_db"

    def test_explicit_dcl_dir_passes_through(self, tmp_path):
        custom = tmp_path / "custom"
        result = _resolve_dcl_dir(tmp_path, custom)
        assert result == custom


# ===================================================================
# Result properties
# ===================================================================


class TestResultProperties:
    def test_passed_true_when_all_consistent(self):
        result = GrantValidationResult(
            statuses=[
                GranteeStatus(
                    grantee="{{A}}", file_path=Path("/tmp/a"), consistent=True
                ),
                GranteeStatus(
                    grantee="{{B}}", file_path=Path("/tmp/b"), consistent=True
                ),
            ]
        )
        assert result.passed

    def test_passed_false_with_drift(self):
        result = GrantValidationResult(
            statuses=[
                GranteeStatus(
                    grantee="{{A}}", file_path=Path("/tmp/a"), consistent=True
                ),
                GranteeStatus(grantee="{{B}}", file_path=Path("/tmp/b"), drifted=True),
            ]
        )
        assert not result.passed

    def test_passed_false_with_missing(self):
        result = GrantValidationResult(
            statuses=[
                GranteeStatus(grantee="{{A}}", file_path=Path("/tmp/a"), missing=True),
            ]
        )
        assert not result.passed

    def test_passed_false_with_orphan(self):
        result = GrantValidationResult(
            statuses=[
                GranteeStatus(grantee="{{A}}", file_path=Path("/tmp/a"), orphaned=True),
            ]
        )
        assert not result.passed

    def test_empty_result_passes(self):
        """No grantees inferred → trivially passed."""
        assert GrantValidationResult().passed


# ===================================================================
# Report rendering
# ===================================================================


class TestFormatReport:
    def test_empty_result_message(self):
        result = GrantValidationResult()
        report = format_report(result)
        assert "No cross-database grants" in report

    def test_consistent_marker(self, project):
        _add_view(
            project,
            "{{V}}.A.viw",
            _make_view_ddl("{{V}}", "{{T}}"),
        )
        fix_grants(project)
        result = validate_grants(project)
        report = format_report(result)
        assert "✓" in report
        assert "{{V}}" in report
        assert "clean" in report

    def test_drift_marker(self, project):
        _add_view(
            project,
            "{{V}}.A.viw",
            _make_view_ddl("{{V}}", "{{T}}"),
        )
        fix_grants(project)
        # Corrupt
        grt = _dcl_dir(project) / "{{V}}.dcl"
        grt.write_text(
            grt.read_text(encoding="utf-8").replace("SELECT", "DELETE"),
            encoding="utf-8",
        )
        result = validate_grants(project)
        report = format_report(result)
        assert "✗" in report
        assert "drift" in report.lower()

    def test_missing_marker(self, project):
        _add_view(
            project,
            "{{V}}.A.viw",
            _make_view_ddl("{{V}}", "{{T}}"),
        )
        result = validate_grants(project)
        report = format_report(result)
        assert "missing" in report.lower()

    def test_orphan_marker(self, project):
        # Stray .dcl with no DDL
        dcl = _dcl_dir(project)
        dcl.mkdir(parents=True)
        (dcl / "{{LEGACY}}.dcl").write_text(
            "GRANT SELECT ON {{X}} TO {{LEGACY}} WITH GRANT OPTION;\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        report = format_report(result)
        assert "orphan" in report.lower()
        assert "{{LEGACY}}" in report

    def test_summary_counts(self, project):
        # 1 consistent
        _add_view(
            project,
            "{{V1}}.A.viw",
            _make_view_ddl("{{V1}}", "{{T1}}"),
        )
        fix_grants(project)
        # Add 1 missing
        _add_view(
            project,
            "{{V2}}.B.viw",
            _make_view_ddl("{{V2}}", "{{T2}}"),
        )
        result = validate_grants(project)
        report = format_report(result)
        assert "1 consistent" in report
        assert "1 missing" in report


# -------------------------------------------------------------------
# Tests for passed_ignoring_orphans() — the warn_orphan_grants mode
# -------------------------------------------------------------------


class TestPassedIgnoringOrphans:
    """
    Covers the ``passed_ignoring_orphans()`` helper on
    GrantValidationResult, which supports the
    ``inspect.warn_orphan_grants`` ships.yaml option.

    When that option is enabled the inspect command uses
    ``passed_ignoring_orphans()`` rather than ``passed`` to
    determine whether the grant step blocks packaging.  Orphaned
    DCL files are still detected and reported (as warnings), but
    they do NOT cause a hard failure.
    """

    def test_no_orphans_passes_same_as_passed(self, project):
        """With no orphans, passed_ignoring_orphans() == passed."""
        _add_view(
            project,
            "{{DOM_V}}.Sales.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        result = validate_grants(project)
        assert result.passed
        assert result.passed_ignoring_orphans()

    def test_orphan_only_passes_ignoring_orphans(self, project):
        """
        A result with only orphaned entries — no drift, no missing —
        fails ``passed`` but passes ``passed_ignoring_orphans()``.

        This is the canonical scenario: a role is granted database
        access within the package, but the GRANT ROLE … TO USER is
        managed outside the package.
        """
        _add_view(
            project,
            "{{DOM_V}}.Orders.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        # Inject an orphaned DCL whose grantee has no DDL backing
        orphan = _dcl_dir(project) / "{{EXTERNAL_ROLE}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{DOM_T}} TO {{EXTERNAL_ROLE}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        # Strict mode (default) — should fail
        assert not result.passed
        assert len(result.orphaned) == 1
        # Warn mode — orphan does not block
        assert result.passed_ignoring_orphans()

    def test_drift_still_fails_ignoring_orphans(self, project):
        """
        Drift (mismatched privileges) must still cause a hard failure
        even when warn_orphan_grants is enabled.
        """
        _add_view(
            project,
            "{{DOM_V}}.Items.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        # Corrupt the DCL to introduce drift
        dcl_file = _dcl_dir(project) / "{{DOM_V}}.dcl"
        dcl_file.write_text(
            "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        # Also add an orphan so both conditions coexist
        orphan = _dcl_dir(project) / "{{EXTERNAL_ROLE}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{DOM_T}} TO {{EXTERNAL_ROLE}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.drifted) == 1
        assert len(result.orphaned) == 1
        # Drift is still a hard failure even in warn mode
        assert not result.passed_ignoring_orphans()

    def test_missing_still_fails_ignoring_orphans(self, project):
        """
        Missing DCL files (inferred but not persisted) must still
        cause a hard failure even when warn_orphan_grants is enabled.
        """
        _add_view(
            project,
            "{{DOM_V}}.Products.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        # Do NOT call fix_grants — the DCL file is intentionally absent
        # Also add an orphan so both conditions coexist
        dcl = _dcl_dir(project)
        dcl.mkdir(parents=True)
        orphan = dcl / "{{EXTERNAL_ROLE}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{DOM_T}} TO {{EXTERNAL_ROLE}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.missing) == 1
        assert len(result.orphaned) == 1
        # Missing is still a hard failure even in warn mode
        assert not result.passed_ignoring_orphans()

    def test_empty_result_passes_ignoring_orphans(self, project):
        """An empty project (no grants anywhere) passes trivially."""
        result = validate_grants(project)
        assert result.passed
        assert result.passed_ignoring_orphans()


# -------------------------------------------------------------------
# Tests for drifted_extra_only / drifted_missing_privs /
# passed_ignoring_extra_grants() — the warn_extra_grants mode
# -------------------------------------------------------------------


class TestPassedIgnoringExtraGrants:
    """
    Covers the ``drifted_extra_only``, ``drifted_missing_privs``, and
    ``passed_ignoring_extra_grants()`` / ``passed_ignoring_extra_grants_and_orphans()``
    helpers on GrantValidationResult, which support the
    ``inspect.warn_extra_grants`` ships.yaml option.

    When enabled, drifted grantees whose .dcl files contain only *extra*
    privileges (grants you added manually beyond what SHIPS infers) are
    downgraded to warnings.  Grantees with *missing* inferred privileges
    remain hard errors — the DDL is referencing access that has not been
    granted.
    """

    def _setup_consistent_project(self, project: Path) -> Path:
        """Helper: create a view + matching .dcl so baseline is consistent."""
        _add_view(
            project,
            "{{DOM_V}}.Sales.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        return _dcl_dir(project) / "{{DOM_V}}.dcl"

    def test_extra_only_drift_classified_correctly(self, project):
        """
        A .dcl with all inferred grants PLUS extra manual grants →
        drifted_extra_only, not drifted_missing_privs.
        """
        dcl_file = self._setup_consistent_project(project)
        # Append an extra grant not implied by any DDL
        existing = dcl_file.read_text(encoding="utf-8")
        dcl_file.write_text(
            existing + "\nGRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.drifted) == 1
        assert len(result.drifted_extra_only) == 1
        assert len(result.drifted_missing_privs) == 0

    def test_missing_privs_drift_classified_correctly(self, project):
        """
        A .dcl that is MISSING an inferred grant → drifted_missing_privs,
        not drifted_extra_only.
        """
        _add_view(
            project,
            "{{DOM_V}}.Items.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        # Overwrite with a different privilege — removes inferred SELECT
        dcl_file = _dcl_dir(project) / "{{DOM_V}}.dcl"
        dcl_file.write_text(
            "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.drifted_missing_privs) == 1
        assert len(result.drifted_extra_only) == 0

    def test_extra_only_passes_ignoring_extra_grants(self, project):
        """
        Extra-only drift does not block when warn_extra_grants is enabled.
        """
        dcl_file = self._setup_consistent_project(project)
        existing = dcl_file.read_text(encoding="utf-8")
        dcl_file.write_text(
            existing + "\nGRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert result.passed_ignoring_extra_grants()

    def test_missing_privs_still_fails_ignoring_extra_grants(self, project):
        """
        Missing inferred privileges remain a hard error even when
        warn_extra_grants is enabled.
        """
        _add_view(
            project,
            "{{DOM_V}}.Products.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        dcl_file = _dcl_dir(project) / "{{DOM_V}}.dcl"
        # Replace the inferred grant with something else entirely
        dcl_file.write_text(
            "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert not result.passed_ignoring_extra_grants()

    def test_missing_dcl_file_still_fails_ignoring_extra_grants(self, project):
        """
        A completely absent .dcl file is still a hard error even when
        warn_extra_grants is enabled (nothing was granted at all).
        """
        _add_view(
            project,
            "{{DOM_V}}.Orders.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        # Do NOT call fix_grants — the .dcl file is intentionally absent
        result = validate_grants(project)
        assert not result.passed
        assert not result.passed_ignoring_extra_grants()

    def test_combined_extra_grants_and_orphans(self, project):
        """
        Both extra-only drift AND orphaned entries present.
        passed_ignoring_extra_grants_and_orphans() returns True;
        passed_ignoring_extra_grants() alone also returns True since
        orphans have no missing_privs.
        """
        dcl_file = self._setup_consistent_project(project)
        # Extra manual grant on the consistent grantee
        existing = dcl_file.read_text(encoding="utf-8")
        dcl_file.write_text(
            existing + "\nGRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        # Orphaned DCL for a grantee with no DDL backing
        orphan = _dcl_dir(project) / "{{EXTERNAL_ROLE}}.dcl"
        orphan.write_text(
            "GRANT SELECT ON {{DOM_T}} TO {{EXTERNAL_ROLE}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed
        assert len(result.drifted_extra_only) == 1
        assert len(result.orphaned) == 1
        assert result.passed_ignoring_extra_grants_and_orphans()

    def test_missing_privs_blocks_combined_check(self, project):
        """
        Even with both warn flags active, missing inferred privileges
        remain a hard failure.
        """
        _add_view(
            project,
            "{{DOM_V}}.Accounts.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        fix_grants(project)
        dcl_file = _dcl_dir(project) / "{{DOM_V}}.dcl"
        # Replace the inferred grant entirely — creates missing_privs
        dcl_file.write_text(
            "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}};\n",
            encoding="utf-8",
        )
        result = validate_grants(project)
        assert not result.passed_ignoring_extra_grants_and_orphans()

    def test_no_drift_passes_ignoring_extra_grants(self, project):
        """Clean project — both helpers agree with passed."""
        self._setup_consistent_project(project)
        result = validate_grants(project)
        assert result.passed
        assert result.passed_ignoring_extra_grants()
        assert result.passed_ignoring_extra_grants_and_orphans()


class TestFixGrantsAdditiveRepair:
    def test_fix_grants_appends_missing_inferred_grant_without_removing_extra(self, project):
        """--fix-grants appends required grants and preserves extra grants."""
        _add_view(
            project,
            "{{DOM_V}}.Customer.viw",
            _make_view_ddl("{{DOM_V}}", "{{DOM_T}}"),
        )
        dcl = _dcl_dir(project)
        dcl.mkdir(parents=True)
        dcl_file = dcl / "{{DOM_V}}.dcl"
        dcl_file.write_text(
            "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}} WITH GRANT OPTION;\n",
            encoding="utf-8",
        )

        result, files_written = fix_grants(project)
        content = dcl_file.read_text(encoding="utf-8")

        assert files_written == 1
        assert "GRANT INSERT ON {{DOM_T}} TO {{DOM_V}} WITH GRANT OPTION;" in content
        assert "GRANT SELECT ON {{DOM_T}} TO {{DOM_V}} WITH GRANT OPTION;" in content
        assert len(result.drifted) == 1
        assert result.drifted[0].missing_privs == {}
        assert result.drifted[0].extra_privs == {"{{DOM_T}}": {"INSERT"}}

    def test_fix_grants_creates_role_grant_without_grant_option(self, project):
        """Role-targeted DCL created via the repair path never uses grant option."""
        from td_release_packager import validate_grants as vg

        original_infer = vg._infer_expected_grants
        try:
            vg._infer_expected_grants = lambda _project, _verbose=False: (
                {"{{APP}}_READ_ROLE": {"{{DOM_T}}": {"SELECT"}}},
                [
                    {
                        "file": "synthetic.viw",
                        "grantee": "{{APP}}_READ_ROLE",
                        "obj_type": "VIEW",
                        "obj_name": "synthetic",
                        "grants": {"{{DOM_T}}": {"SELECT"}},
                    }
                ],
                1,
            )
            result, files_written = vg.fix_grants(project)
        finally:
            vg._infer_expected_grants = original_infer

        role_file = _role_dcl_dir(project) / "{{APP}}_READ_ROLE.dcl"
        content = role_file.read_text(encoding="utf-8")
        assert files_written == 2
        assert result.passed
        assert "GRANT SELECT ON {{DOM_T}} TO {{APP}}_READ_ROLE;" in content
        assert "WITH GRANT OPTION" not in content
        role_ddl = project / "payload" / "database" / "system" / "roles" / "{{APP}}_READ_ROLE.rol"
        assert role_ddl.read_text(encoding="utf-8") == "CREATE ROLE {{APP}}_READ_ROLE;\n"
