"""
test_fix_non_ascii.py — Tests for the non-ASCII auto-fixer (#257).
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.validate import (
    NonAsciiFixResult,
    fix_non_ascii,
    validate_directory,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    (project / "payload" / "database" / "DML").mkdir(parents=True)
    return project


def _non_ascii_findings(project: Path) -> int:
    result = validate_directory(str(project))
    return sum(1 for issue in result.issues if issue.rule == "non_ascii")


# ---------------------------------------------------------------
# Each mapped character
# ---------------------------------------------------------------


class TestKnownReplacements:
    def test_em_dash_replaced_with_spaced_hyphen(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/v.viw",
            "/* title — description */\nREPLACE VIEW V AS SELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        text = f.read_text(encoding="utf-8")
        assert "—" not in text
        assert "title  -  description" in text

    def test_bullet_replaced_with_hyphen(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/v.viw",
            "-- • first\n-- • second\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        text = f.read_text(encoding="utf-8")
        assert "•" not in text
        assert "-- - first" in text

    def test_right_arrow_replaced_with_ascii_arrow(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/v.viw",
            "/* flow: A → B */\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        assert "A -> B" in f.read_text(encoding="utf-8")

    def test_box_drawing_replaced_with_hyphen(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/v.viw",
            "-- ── Change Log ──\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        assert "-- -- Change Log --" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------
# Unmapped characters — never substituted
# ---------------------------------------------------------------


class TestUnmappedCharsAreLeftAlone:
    def test_ufffd_is_not_substituted(self, tmp_path):
        project = _setup_project(tmp_path)
        original = "/* header � description */\nSELECT 1;\n"
        f = _write(project / "payload/database/DDL/views/v.viw", original)
        result = fix_non_ascii(str(project))
        assert result.files_written == 0
        # File untouched, U+FFFD still present.
        assert f.read_text(encoding="utf-8") == original

    def test_unmapped_unicode_is_not_substituted(self, tmp_path):
        # A perfectly valid non-ASCII character with no documented ASCII
        # equivalent in the suggestion map (e.g. en-dash U+2013) must not
        # be touched.
        project = _setup_project(tmp_path)
        original = "/* range – ok */\nSELECT 1;\n"
        f = _write(project / "payload/database/DDL/views/v.viw", original)
        result = fix_non_ascii(str(project))
        assert result.files_written == 0
        assert f.read_text(encoding="utf-8") == original

    def test_mixed_mapped_and_ufffd(self, tmp_path):
        # File contains both an em-dash (auto-fixable) and a U+FFFD
        # (unrecoverable). The em-dash is substituted; U+FFFD stays.
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/views/v.viw",
            "/* a — b � c */\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        text = f.read_text(encoding="utf-8")
        assert "—" not in text
        assert "�" in text  # still there
        # And the detector still flags the U+FFFD.
        assert _non_ascii_findings(project) == 1


# ---------------------------------------------------------------
# Counts / result shape
# ---------------------------------------------------------------


class TestResultShape:
    def test_per_char_counts_recorded(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/v.viw",
            "/* — — • */\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        fix = result.files_fixed[0]
        assert fix.substitutions["—"] == 2
        assert fix.substitutions["•"] == 1
        assert fix.total_chars_substituted == 3

    def test_to_dict_uses_unicode_codepoint_keys(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/v.viw",
            "/* — */\nSELECT 1;\n",
        )
        result = fix_non_ascii(str(project))
        d = result.to_dict()
        assert d["files_written"] == 1
        assert d["chars_substituted"] == 1
        assert d["files"][0]["substitutions"]["U+2014"] == 1


# ---------------------------------------------------------------
# Discovery / exclusions
# ---------------------------------------------------------------


class TestExclusions:
    def test_releases_directory_is_skipped(self, tmp_path):
        project = _setup_project(tmp_path)
        real = _write(
            project / "payload/database/DDL/views/v.viw",
            "/* — */\nSELECT 1;\n",
        )
        gen_dir = project / "releases" / "DEV_BUILD_0001" / "payload"
        gen_dir.mkdir(parents=True)
        gen = _write(gen_dir / "g.viw", "/* — */\nSELECT 1;\n")

        result = fix_non_ascii(str(project))
        assert result.files_written == 1
        assert "—" not in real.read_text(encoding="utf-8")
        assert "—" in gen.read_text(encoding="utf-8")

    def test_ships_work_directory_is_skipped(self, tmp_path):
        project = _setup_project(tmp_path)
        work = project / ".ships-work" / "payload"
        work.mkdir(parents=True)
        f = _write(work / "g.viw", "/* — */\nSELECT 1;\n")
        result = fix_non_ascii(str(project))
        assert result.files_written == 0
        assert "—" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------
# Idempotence + end-to-end
# ---------------------------------------------------------------


class TestIdempotenceAndEndToEnd:
    def test_running_twice_is_a_no_op(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/v.viw",
            "/* — • → ─ */\nSELECT 1;\n",
        )
        first = fix_non_ascii(str(project))
        second = fix_non_ascii(str(project))
        assert first.files_written == 1
        assert second.files_written == 0

    def test_detector_loses_findings_after_fix(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/views/v.viw",
            "VALUES ( 'em — dash' );\n",
        )
        _write(
            project / "payload/database/DML/seed.dml",
            "VALUES ( 'bullet • point' );\n",
        )
        # Before: two findings.
        assert _non_ascii_findings(project) == 2
        result = fix_non_ascii(str(project))
        assert result.files_written == 2
        # After: zero findings.
        assert _non_ascii_findings(project) == 0

    def test_ascii_only_file_not_touched(self, tmp_path):
        project = _setup_project(tmp_path)
        original = "REPLACE VIEW V AS SELECT 1;\n"
        f = _write(project / "payload/database/DDL/views/v.viw", original)
        result = fix_non_ascii(str(project))
        assert result.files_written == 0
        assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------


class TestDefensive:
    def test_empty_file_no_crash(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(project / "payload/database/DDL/views/v.viw", "")
        result = fix_non_ascii(str(project))
        assert isinstance(result, NonAsciiFixResult)
        assert result.files_written == 0

    def test_no_source_files(self, tmp_path):
        project = _setup_project(tmp_path)
        result = fix_non_ascii(str(project))
        assert result.files_written == 0
