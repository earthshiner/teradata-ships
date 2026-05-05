"""
test_legacy_placeholders.py — Phase A of placeholder-visibility:
detect $VAR / ${VAR} / &&VAR&& during harvest and surface a banner.

Three layers of test:

    1. Unit tests on ``find_legacy_placeholders``: each syntax,
       boundary handling, comment/literal stripping.
    2. Banner-format tests on ``format_legacy_placeholders_report``:
       headline shape, syntax breakdown, file truncation.
    3. End-to-end through ``ingest_directory``: a harvested file
       with $VAR placeholders populates ``IngestResult.legacy_placeholders``.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.ingest import ingest_directory
from td_release_packager.legacy_placeholders import (
    LegacyPlaceholderFinding,
    find_legacy_placeholders,
    format_legacy_placeholders_report,
)


# ---------------------------------------------------------------
# Unit: find_legacy_placeholders
# ---------------------------------------------------------------


class TestFindLegacyPlaceholders:
    """Detection across the three supported syntaxes plus
    boundary cases (comments, string literals, mid-identifier)."""

    def test_no_placeholders_returns_empty(self):
        ddl = "CREATE MULTISET TABLE MyDb.T (Id INT) PRIMARY INDEX (Id);"
        assert find_legacy_placeholders(ddl, "x.tbl") == []

    def test_dollar_var_detected(self):
        ddl = "CREATE MULTISET TABLE $UTL_T.BKEY_Domain (Id INT);"
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 1
        assert findings[0].syntax == "dollar"
        assert findings[0].placeholder == "$UTL_T"
        assert findings[0].var_name == "UTL_T"
        assert findings[0].line == 1

    def test_dollar_braced_detected(self):
        ddl = "CREATE MULTISET TABLE ${UTL_T}.X (Id INT);"
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 1
        assert findings[0].syntax == "dollar-braced"
        assert findings[0].placeholder == "${UTL_T}"
        assert findings[0].var_name == "UTL_T"

    def test_amp_amp_detected(self):
        ddl = "CREATE TABLE MyDb.T (D DATE NOT NULL &&DATE_FORMAT&&);"
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 1
        assert findings[0].syntax == "amp-amp"
        assert findings[0].placeholder == "&&DATE_FORMAT&&"
        assert findings[0].var_name == "DATE_FORMAT"

    def test_dollar_braced_does_not_double_count_inner_dollar(self):
        """``${VAR}`` should produce ONE finding under dollar-braced,
        NOT a second finding under the bare $VAR pattern claiming
        the inner ``$VAR`` portion."""
        ddl = "CREATE MULTISET TABLE ${UTL_T}.X (Id INT);"
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 1
        assert findings[0].syntax == "dollar-braced"

    def test_multiple_placeholders_in_order(self):
        ddl = (
            "CREATE MULTISET TABLE $UTL_T.X (\n"
            "    Id INT NOT NULL,\n"
            "    D  DATE NOT NULL &&DATE_FORMAT&&,\n"
            "    T  &&TS_TYPE&& NOT NULL\n"
            ");"
        )
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 3
        # Document order — line 1 first, then line 3, then line 4.
        assert [f.var_name for f in findings] == [
            "UTL_T",
            "DATE_FORMAT",
            "TS_TYPE",
        ]

    def test_placeholder_in_block_comment_ignored(self):
        ddl = (
            "/* example: $UTL_T.foo would be the qualifier */\n"
            "CREATE MULTISET TABLE MyDb.T (Id INT);"
        )
        assert find_legacy_placeholders(ddl, "x.tbl") == []

    def test_placeholder_in_line_comment_ignored(self):
        ddl = (
            "-- $UTL_T was replaced with MyDb manually\n"
            "CREATE MULTISET TABLE MyDb.T (Id INT);"
        )
        assert find_legacy_placeholders(ddl, "x.tbl") == []

    def test_placeholder_in_string_literal_ignored(self):
        """A procedure body building dynamic SQL with a placeholder
        inside a string literal must NOT be flagged — that's
        intentional content, not a build-time substitution."""
        ddl = (
            "CREATE PROCEDURE MyDb.sp_X ()\n"
            "BEGIN\n"
            "    DECLARE vSQL VARCHAR(500);\n"
            "    SET vSQL = 'SELECT * FROM $UTL_T.foo';\n"
            "END;"
        )
        assert find_legacy_placeholders(ddl, "x.spl") == []

    def test_mid_identifier_dollar_not_flagged(self):
        """``foo$bar`` is an unusual identifier in Teradata and is
        NOT a $VAR placeholder. The detector's lookbehind excludes
        mid-identifier dollars."""
        # Hypothetical column name with embedded $.
        ddl = "CREATE MULTISET TABLE MyDb.T (foo$bar INT);"
        assert find_legacy_placeholders(ddl, "x.tbl") == []

    def test_token_form_not_flagged(self):
        """Already-tokenised content uses {{TOKEN}} which is the
        SHIPS canonical form — must not be flagged."""
        ddl = "CREATE MULTISET TABLE {{T_DB}}.X (Id INT);"
        assert find_legacy_placeholders(ddl, "x.tbl") == []

    def test_line_column_correct(self):
        ddl = "first line\nsecond line $UTL_T.X\nthird line"
        findings = find_legacy_placeholders(ddl, "x.tbl")
        assert len(findings) == 1
        assert findings[0].line == 2
        # Column 13 = 1-based position of '$' on line 2 ("second line ").
        assert findings[0].column == 13

    def test_findings_carry_file_path(self):
        ddl = "CREATE TABLE $UTL_T.X (Id INT);"
        findings = find_legacy_placeholders(ddl, "/abs/path/X.tbl")
        assert len(findings) == 1
        assert findings[0].file_path == "/abs/path/X.tbl"


# ---------------------------------------------------------------
# Unit: format_legacy_placeholders_report
# ---------------------------------------------------------------


def _make_finding(
    syntax: str = "dollar",
    placeholder: str = "$VAR",
    var_name: str = "VAR",
    file_path: str = "/src/x.tbl",
    line: int = 1,
    column: int = 1,
) -> LegacyPlaceholderFinding:
    return LegacyPlaceholderFinding(
        syntax=syntax,
        placeholder=placeholder,
        var_name=var_name,
        file_path=file_path,
        line=line,
        column=column,
    )


class TestFormatLegacyPlaceholdersReport:
    """Banner shape — headline counts, per-syntax breakdown,
    file sample, call-to-action."""

    def test_empty_findings_returns_empty_string(self):
        assert format_legacy_placeholders_report([]) == ""

    def test_headline_counts_total_and_files(self):
        findings = [
            _make_finding(file_path="/src/a.tbl", line=1),
            _make_finding(file_path="/src/a.tbl", line=2),
            _make_finding(file_path="/src/b.tbl", line=1),
        ]
        out = format_legacy_placeholders_report(findings)
        assert "3 occurrences" in out
        assert "2 files" in out

    def test_per_syntax_breakdown(self):
        findings = [
            _make_finding(syntax="dollar", var_name="UTL_T"),
            _make_finding(syntax="amp-amp", var_name="DATE_FORMAT"),
            _make_finding(syntax="amp-amp", var_name="TS_TYPE"),
        ]
        out = format_legacy_placeholders_report(findings)
        assert "$VAR style" in out
        assert "&&VAR&& style" in out

    def test_call_to_action_names_import_legacy(self):
        findings = [_make_finding()]
        out = format_legacy_placeholders_report(findings)
        assert "import-legacy" in out
        # Phase B is named so users know the proposed CLI exists.
        assert "--scan-source" in out

    def test_project_dir_hint_appears_in_output_dir_flag(self):
        findings = [_make_finding()]
        out = format_legacy_placeholders_report(findings, project_dir_hint="myproj")
        assert "myproj/config" in out

    def test_relativises_paths_against_source_dir(self):
        findings = [_make_finding(file_path="/src/sub/x.tbl")]
        out = format_legacy_placeholders_report(findings, source_dir="/src")
        # Either Windows or Unix separator works; just check the
        # absolute root has been stripped.
        assert "/src/sub/x.tbl" not in out
        assert "x.tbl" in out

    def test_file_sample_truncated(self):
        # 8 distinct files; banner should show 5 + "+3 more files".
        findings = [_make_finding(file_path=f"/src/file_{i}.tbl") for i in range(8)]
        out = format_legacy_placeholders_report(findings)
        assert "showing 5 of 8" in out
        assert "+3 more files" in out

    def test_var_name_sample_truncated_per_syntax(self):
        # 10 distinct $VAR names — sample should top-N (currently 6).
        findings = [
            _make_finding(syntax="dollar", var_name=f"VAR_{i}") for i in range(10)
        ]
        out = format_legacy_placeholders_report(findings)
        assert "+4 more" in out


# ---------------------------------------------------------------
# Integration: ingest_directory populates legacy_placeholders
# ---------------------------------------------------------------


class TestIngestPopulatesLegacyPlaceholders:
    """End-to-end: a harvest run on source containing $VAR
    placeholders surfaces them in IngestResult.legacy_placeholders."""

    def _make_project(self, tmp_path: Path) -> Path:
        """Minimal SHIPS-shaped project layout."""
        project = tmp_path / "project"
        for sub in (
            "payload/database/DDL/tables",
            "payload/database/DDL/views",
            "payload/database/DDL/macros",
            "payload/database/DDL/procedures",
            "payload/database/pre-requisites/databases",
            "config/properties",
        ):
            (project / sub).mkdir(parents=True, exist_ok=True)
        (project / ".build_counter").write_text("0\n", encoding="utf-8")
        return project

    def test_dollar_placeholder_in_source_recorded(self, tmp_path):
        project = self._make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "BKEY_Domain.tbl").write_text(
            "CREATE MULTISET TABLE $UTL_T.BKEY_Domain (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        result = ingest_directory(
            source_dir=str(source),
            project_dir=str(project),
            detect_tokens=False,
        )

        assert len(result.legacy_placeholders) == 1
        finding = result.legacy_placeholders[0]
        assert finding.syntax == "dollar"
        assert finding.var_name == "UTL_T"
        assert finding.file_path.endswith("BKEY_Domain.tbl")

    def test_already_tokenised_source_no_findings(self, tmp_path):
        """Source that uses {{TOKEN}} form -- the canonical SHIPS
        end state -- produces zero findings, banner suppressed."""
        project = self._make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyTable.tbl").write_text(
            "CREATE MULTISET TABLE {{T_DB}}.MyTable (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        result = ingest_directory(
            source_dir=str(source),
            project_dir=str(project),
            detect_tokens=False,
        )

        assert result.legacy_placeholders == []

    def test_mixed_source_aggregates_per_syntax(self, tmp_path):
        """A source dir with multiple files using different
        placeholder syntaxes aggregates correctly into one
        flat list of findings, suitable for the banner."""
        project = self._make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.tbl").write_text(
            "CREATE MULTISET TABLE $UTL_T.A (Id INT);",
            encoding="utf-8",
        )
        (source / "b.tbl").write_text(
            "CREATE MULTISET TABLE ${UTL_T}.B (Id INT);",
            encoding="utf-8",
        )
        (source / "c.tbl").write_text(
            "CREATE TABLE MyDb.C (D DATE &&DATE_FORMAT&&);",
            encoding="utf-8",
        )

        result = ingest_directory(
            source_dir=str(source),
            project_dir=str(project),
            detect_tokens=False,
        )

        syntaxes = {f.syntax for f in result.legacy_placeholders}
        assert syntaxes == {"dollar", "dollar-braced", "amp-amp"}
        assert len(result.legacy_placeholders) == 3

    def test_placeholder_inside_comment_not_picked_up(self, tmp_path):
        """A header comment that mentions a placeholder must NOT
        produce a false positive."""
        project = self._make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "Clean.tbl").write_text(
            "/* Note: replace $LEGACY with {{T_DB}} when migrating. */\n"
            "CREATE MULTISET TABLE {{T_DB}}.Clean (Id INT) "
            "PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )

        result = ingest_directory(
            source_dir=str(source),
            project_dir=str(project),
            detect_tokens=False,
        )

        assert result.legacy_placeholders == []
