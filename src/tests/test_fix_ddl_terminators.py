"""
test_fix_ddl_terminators.py — Tests for the DDL-terminator auto-fixer (#253).
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.cli import _build_parser
from td_release_packager.fixers import FixResult
from td_release_packager.fixers.ddl_terminator import fix_ddl_terminators
from td_release_packager.validate import validate_directory


# ---------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------


class TestCLIFlags:
    """After #522 (ddl_terminator, non_ascii) and #526 (grants), ``ships
    inspect`` exposes no auto-fix flags at all — inspect is strictly
    read-only. All three fixers live in ``ships fix``.
    """

    def test_fix_ddl_terminators_flag_removed_from_inspect(self):
        import pytest

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "--project", ".", "--fix-ddl-terminators"])

    def test_no_fix_ddl_terminators_flag_removed_from_inspect(self):
        import pytest

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "--project", ".", "--no-fix-ddl-terminators"])

    def test_fix_grants_flag_removed_from_inspect(self):
        import pytest

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "--project", ".", "--fix-grants"])

    def test_no_fix_grants_flag_removed_from_inspect(self):
        import pytest

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "--project", ".", "--no-fix-grants"])


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _ships_yaml(project: Path) -> None:
    """Minimal ships.yaml so discovery resolver picks up our test extensions."""
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _ships_yaml(project)
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "procedures").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "triggers").mkdir(parents=True)
    return project


def _violations(project: Path) -> int:
    result = validate_directory(str(project))
    return sum(1 for issue in result.issues if issue.rule == "ddl_terminator")


# ---------------------------------------------------------------
# Simple cases
# ---------------------------------------------------------------


class TestSimpleFix:
    def test_table_missing_terminator_is_fixed(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )

        result = fix_ddl_terminators(str(project))

        assert result.files_written == 1
        assert result.totals["statements_fixed"] == 1
        assert f.read_text(encoding="utf-8").endswith(");\n")

    def test_view_missing_terminator_is_fixed(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/Dev.V.viw",
            "CREATE VIEW Dev.V AS SELECT 1 AS X FROM Dev.T\n",
        )
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 1
        assert f.read_text(encoding="utf-8").endswith(" Dev.T;\n")

    def test_already_terminated_file_is_not_touched(self, tmp_path):
        project = _setup_project(tmp_path)
        original = "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n"
        f = _write(project / "payload/database/DDL/tables/Dev.T.tbl", original)

        result = fix_ddl_terminators(str(project))

        assert result.files_written == 0
        assert result.totals["statements_fixed"] == 0
        assert f.read_text(encoding="utf-8") == original

    def test_file_with_no_ddl_is_not_touched(self, tmp_path):
        project = _setup_project(tmp_path)
        original = "-- just a banner comment, no DDL here\n"
        f = _write(project / "payload/database/DDL/tables/notes.tbl", original)
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 0
        assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------
# Boundary handling
# ---------------------------------------------------------------


class TestBoundaryHandling:
    def test_multiple_statements_only_missing_ones_fixed(self, tmp_path):
        project = _setup_project(tmp_path)
        original = (
            "CREATE MULTISET TABLE Dev.A (Id INTEGER) PRIMARY INDEX (Id);\n"
            "CREATE MULTISET TABLE Dev.B (Id INTEGER) PRIMARY INDEX (Id)\n"
            "CREATE MULTISET TABLE Dev.C (Id INTEGER) PRIMARY INDEX (Id);\n"
            "CREATE MULTISET TABLE Dev.D (Id INTEGER) PRIMARY INDEX (Id)\n"
        )
        f = _write(project / "payload/database/DDL/tables/many.tbl", original)
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 1
        assert result.totals["statements_fixed"] == 2
        text = f.read_text(encoding="utf-8")
        # All four statements now end with ";"
        for letter in ("A", "B", "C", "D"):
            assert f"Dev.{letter}" in text
        assert text.count(";") == 4

    def test_trailing_comment_does_not_get_semicolon_inside_it(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n"
            "-- ownership note\n",
        )
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 1
        text = f.read_text(encoding="utf-8")
        # The terminator must land BEFORE the trailing comment, never inside.
        assert ");\n-- ownership note\n" in text
        assert "note;" not in text

    def test_procedure_outer_terminator_added(self, tmp_path):
        project = _setup_project(tmp_path)
        # Stored procedure body has internal semi-colons; the rule only
        # flags the outer CREATE PROCEDURE missing its final ";".
        f = _write(
            project / "payload/database/DDL/procedures/Dev.SP.spl",
            "CREATE PROCEDURE Dev.SP() BEGIN DECLARE x INTEGER; SET x = 1; END\n",
        )
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 1
        assert f.read_text(encoding="utf-8").endswith("END;\n")

    def test_trailing_whitespace_preserved(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n\n\n",
        )
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 1
        # Terminator lands flush against ")"; trailing newlines stay intact.
        assert f.read_text(encoding="utf-8").endswith(");\n\n\n")


# ---------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------


class TestGeneratedPathExclusion:
    def test_releases_directory_is_skipped(self, tmp_path):
        project = _setup_project(tmp_path)
        # Real source file — should be fixed
        real = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        # Released artefact — must be left alone
        gen_dir = project / "releases" / "DEV_BUILD_0001" / "payload"
        gen_dir.mkdir(parents=True)
        gen = _write(
            gen_dir / "Dev.U.tbl",
            "CREATE MULTISET TABLE Dev.U (Id INTEGER) PRIMARY INDEX (Id)\n",
        )

        result = fix_ddl_terminators(str(project))

        assert result.files_written == 1
        assert real.read_text(encoding="utf-8").endswith(");\n")
        # The released file is untouched even though it has a violation.
        assert not gen.read_text(encoding="utf-8").rstrip().endswith(";")

    def test_ships_work_directory_is_skipped(self, tmp_path):
        project = _setup_project(tmp_path)
        work = project / ".ships-work" / "payload"
        work.mkdir(parents=True)
        f = _write(
            work / "Dev.X.tbl",
            "CREATE MULTISET TABLE Dev.X (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 0
        assert not f.read_text(encoding="utf-8").rstrip().endswith(";")


# ---------------------------------------------------------------
# Idempotence + end-to-end with the detector
# ---------------------------------------------------------------


class TestIdempotenceAndEndToEnd:
    def test_running_twice_is_a_no_op(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )

        first = fix_ddl_terminators(str(project))
        second = fix_ddl_terminators(str(project))

        assert first.files_written == 1
        assert second.files_written == 0

    def test_validate_dir_sees_zero_violations_after_fix(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        _write(
            project / "payload/database/DDL/views/Dev.V.viw",
            "CREATE VIEW Dev.V AS SELECT 1 AS X FROM Dev.T\n",
        )

        assert _violations(project) == 2

        result = fix_ddl_terminators(str(project))
        assert result.files_written == 2

        assert _violations(project) == 0

    def test_result_to_dict_shape(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = fix_ddl_terminators(str(project))
        d = result.to_dict()
        assert d["rule_id"] == "ddl_terminator"
        assert d["dry_run"] is False
        assert d["files_scanned"] >= 1
        assert d["files_written"] == 1
        assert d["totals"]["statements_fixed"] == 1
        assert len(d["files"]) == 1
        entry = d["files"][0]
        assert entry["statements_fixed"] == 1
        assert entry["file"].endswith("Dev.T.tbl")


# ---------------------------------------------------------------
# Defensive: empty / all-whitespace files
# ---------------------------------------------------------------


class TestEmptyAndWhitespace:
    def test_empty_file_no_crash(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(project / "payload/database/DDL/tables/empty.tbl", "")
        result = fix_ddl_terminators(str(project))
        assert isinstance(result, FixResult)
        assert result.rule_id == "ddl_terminator"
        assert result.files_written == 0

    def test_no_files_in_project(self, tmp_path):
        project = _setup_project(tmp_path)
        result = fix_ddl_terminators(str(project))
        assert result.files_written == 0
