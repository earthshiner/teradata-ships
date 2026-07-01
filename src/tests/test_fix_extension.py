"""Extension fixer + shared DDL-kind detector (#525).

Covers two modules:

* ``td_release_packager.fixers._detect`` — the shared
  ``detect_ddl_kind()`` primitive with the ``DdlKind`` enum.
  Extension fast path first; content fallback second; UNKNOWN when
  neither resolves. Kept small on purpose so the two follow-up fixers
  (``type_suffix``, ``object_placement``) can share the same
  primitive.
* ``td_release_packager.fixers.extension`` — renames payload files
  whose extension does not match their DDL kind. Opt-in
  (default_on=False) because a rename shows up as delete + add in git,
  and a reviewer should see the change explicitly rather than have
  it merged silently under a bare ``ships fix`` run.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.fixers import FIX_REGISTRY, FixResult
from td_release_packager.fixers._detect import DdlKind, detect_ddl_kind
from td_release_packager.fixers.extension import fix_extension


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    return project


# ---------------------------------------------------------------
# _detect.detect_ddl_kind
# ---------------------------------------------------------------


class TestDetectDdlKindFastPath:
    def test_extension_tbl_returns_table(self):
        assert detect_ddl_kind("Dev.T.tbl") == DdlKind.TABLE

    def test_extension_viw_returns_view(self):
        assert detect_ddl_kind("Dev.V.viw") == DdlKind.VIEW

    def test_extension_spl_returns_procedure(self):
        assert detect_ddl_kind("Dev.SP.spl") == DdlKind.PROCEDURE

    def test_extension_mcr_returns_macro(self):
        assert detect_ddl_kind("Dev.M.mcr") == DdlKind.MACRO

    def test_extension_fnc_returns_function(self):
        assert detect_ddl_kind("Dev.F.fnc") == DdlKind.FUNCTION

    def test_extension_sto_returns_sto(self):
        assert detect_ddl_kind("Dev.X.sto") == DdlKind.STO

    def test_unknown_extension_without_content_returns_unknown(self):
        assert detect_ddl_kind("Dev.T.sql") == DdlKind.UNKNOWN


class TestDetectDdlKindContentFallback:
    def test_content_create_table_returns_table(self):
        assert (
            detect_ddl_kind("Dev.T.sql", "CREATE MULTISET TABLE Dev.T (Id INTEGER);\n")
            == DdlKind.TABLE
        )

    def test_content_create_view_returns_view(self):
        assert (
            detect_ddl_kind("Dev.V.sql", "CREATE VIEW Dev.V AS SELECT 1;\n")
            == DdlKind.VIEW
        )

    def test_content_replace_view_returns_view(self):
        assert (
            detect_ddl_kind("Dev.V.sql", "REPLACE VIEW Dev.V AS SELECT 1;\n")
            == DdlKind.VIEW
        )

    def test_content_create_procedure_returns_procedure(self):
        assert (
            detect_ddl_kind("Dev.SP.sql", "CREATE PROCEDURE Dev.SP() BEGIN END;\n")
            == DdlKind.PROCEDURE
        )

    def test_extension_wins_over_content(self):
        """Extension is authoritative; the fallback only fires when the
        extension is unknown. A .tbl file with view content is treated
        as a table (the payload extension convention is the SHIPS
        contract)."""
        result = detect_ddl_kind("Dev.T.tbl", "CREATE VIEW Dev.T AS SELECT 1;\n")
        assert result == DdlKind.TABLE

    def test_no_extension_no_matching_content_returns_unknown(self):
        assert detect_ddl_kind("Dev.T.sql", "-- just a comment\n") == DdlKind.UNKNOWN


class TestDdlKindEnum:
    def test_suffix_property_matches_kind_suffix_letters(self):
        # Round-trip check between the enum value and the token suffix
        # letter used elsewhere in SHIPS.
        assert DdlKind.TABLE.suffix == "T"
        assert DdlKind.VIEW.suffix == "V"
        assert DdlKind.PROCEDURE.suffix == "P"
        assert DdlKind.FUNCTION.suffix == "F"
        assert DdlKind.MACRO.suffix == "M"
        assert DdlKind.STO.suffix == "X"
        assert DdlKind.UNKNOWN.suffix == ""


# ---------------------------------------------------------------
# extension fixer — registry entry
# ---------------------------------------------------------------


class TestExtensionRegistryEntry:
    def test_extension_is_registered(self):
        assert "extension" in FIX_REGISTRY

    def test_extension_is_opt_in(self):
        """Opt-in (default_on=False) because a rename shows as delete +
        add in git — reviewers should see the change explicitly."""
        assert FIX_REGISTRY["extension"].default_on is False

    def test_extension_writes_to_payload(self):
        assert FIX_REGISTRY["extension"].write_scope == "payload"


# ---------------------------------------------------------------
# extension fixer — rename behaviour
# ---------------------------------------------------------------


class TestExtensionRenames:
    def test_sql_view_renamed_to_viw(self, tmp_path):
        project = _setup_project(tmp_path)
        original = project / "payload/database/DDL/views/Dev.V.sql"
        _write(original, "REPLACE VIEW Dev.V AS SELECT 1;\n")

        result = fix_extension(str(project))

        assert isinstance(result, FixResult)
        assert result.rule_id == "extension"
        assert result.totals["files_renamed"] == 1
        assert not original.exists()
        renamed = project / "payload/database/DDL/views/Dev.V.viw"
        assert renamed.is_file()
        assert renamed.read_text(encoding="utf-8").startswith("REPLACE VIEW")

    def test_already_correct_extension_not_touched(self, tmp_path):
        project = _setup_project(tmp_path)
        original = project / "payload/database/DDL/tables/Dev.T.tbl"
        _write(original, "CREATE MULTISET TABLE Dev.T (Id INTEGER);\n")

        result = fix_extension(str(project))

        assert result.totals["files_renamed"] == 0
        assert original.is_file()

    def test_unknown_kind_not_touched(self, tmp_path):
        """A .sql file with no matching CREATE pattern stays as-is —
        we never rename a file we can't confidently classify."""
        project = _setup_project(tmp_path)
        f = project / "payload/database/DDL/tables/notes.sql"
        _write(f, "-- notes file, no DDL here\n")

        result = fix_extension(str(project))

        assert result.totals["files_renamed"] == 0
        assert f.is_file()


class TestExtensionDryRun:
    def test_dry_run_does_not_rename(self, tmp_path):
        project = _setup_project(tmp_path)
        original = project / "payload/database/DDL/views/Dev.V.sql"
        _write(original, "REPLACE VIEW Dev.V AS SELECT 1;\n")

        result = fix_extension(str(project), dry_run=True)

        # Original file stays; the projected file is NOT created.
        assert original.is_file()
        assert not (project / "payload/database/DDL/views/Dev.V.viw").exists()
        # But the fixer reports what it would have done.
        assert result.dry_run is True
        assert result.totals["files_renamed"] == 1
        assert len(result.files_changed) == 1
        details = result.files_changed[0].details
        assert details["old_ext"] == ".sql"
        assert details["new_ext"] == ".viw"
        assert details["kind"] == "VIEW"

    def test_files_written_is_zero_under_dry_run(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Dev.V.sql",
            "REPLACE VIEW Dev.V AS SELECT 1;\n",
        )
        result = fix_extension(str(project), dry_run=True)
        # FixResult.files_written is zero under dry_run regardless of
        # matches (design decision — see #520 thread).
        assert result.files_written == 0


class TestExtensionCollisionAvoidance:
    def test_conflict_when_target_exists_is_reported_and_not_overwritten(
        self, tmp_path
    ):
        """If both ``Dev.V.sql`` and ``Dev.V.viw`` exist, don't clobber
        the .viw file. Surface the conflict for human review."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/Dev.V.sql",
            "REPLACE VIEW Dev.V AS SELECT 1;\n",
        )
        existing_target = project / "payload/database/DDL/views/Dev.V.viw"
        _write(existing_target, "REPLACE VIEW Dev.V AS SELECT 2;\n")

        result = fix_extension(str(project))

        # Both files still exist.
        assert (project / "payload/database/DDL/views/Dev.V.sql").is_file()
        assert existing_target.is_file()
        # And the conflict shows up as an error entry in the result.
        assert result.errors
        assert any("already exists" in e.get("error", "") for e in result.errors)
