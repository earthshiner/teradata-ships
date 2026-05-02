"""
Tests for malformed-token detection in token_engine.

Covers find_malformed_tokens, scan_malformed_tokens_in_directory,
format_malformed_tokens_report, and integration with the build flow
(builder aborts on malformed tokens before packaging).
"""

import os
import subprocess
import sys
from pathlib import Path


from td_release_packager.token_engine import (
    find_malformed_tokens,
    format_malformed_tokens_report,
    scan_malformed_tokens_in_directory,
)


def _run_subprocess(args, env, *, expect_success=True):
    """
    Run a subprocess and surface stderr on unexpected failure.

    The bare ``subprocess.run(..., check=True)`` pattern raises
    ``CalledProcessError`` without showing what the child wrote
    to stderr, which makes Windows-specific failures (path
    issues, encoding issues, missing dependencies in the test
    environment) opaque. This helper captures both streams and
    embeds them in the AssertionError message.

    Args:
        args:            Subprocess argv list.
        env:             Environment dict for the child.
        expect_success:  When True, asserts rc == 0 and surfaces
                         stdout/stderr in the assertion message
                         on failure. When False, returns the
                         CompletedProcess for the caller to
                         inspect (used when the test EXPECTS a
                         non-zero exit).

    Returns:
        ``subprocess.CompletedProcess`` instance.
    """
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    if expect_success and result.returncode != 0:
        raise AssertionError(
            f"Subprocess failed (rc={result.returncode}):\n"
            f"  cmd: {' '.join(args)}\n"
            f"  --- stdout ---\n{result.stdout}\n"
            f"  --- stderr ---\n{result.stderr}"
        )
    return result


# ---------------------------------------------------------------
# find_malformed_tokens — single-file inspection
# ---------------------------------------------------------------


class TestFindMalformedTokens:
    """find_malformed_tokens flags every shape of broken token."""

    def test_clean_content_returns_empty(self):
        """Well-formed tokens produce no findings."""
        content = "SELECT * FROM {{DB}}.t WHERE x = {{COL_A}};"
        assert find_malformed_tokens(content) == []

    def test_no_tokens_at_all_returns_empty(self):
        """Plain SQL with no tokens is fine."""
        content = "SELECT id FROM DBC.TablesV;"
        assert find_malformed_tokens(content) == []

    def test_whitespace_inside_braces_flagged(self):
        """{{ TOKEN }} with surrounding spaces is malformed."""
        content = "FROM {{ DBC }}.TablesV"
        issues = find_malformed_tokens(content)
        # Both '{{' and '}}' are flagged as orphans
        markers = [i["marker"] for i in issues]
        assert "{{" in markers
        assert "}}" in markers
        assert len(issues) == 2

    def test_double_tokenised_corruption_flagged(self):
        """The exact pattern from the harvester re-run bug."""
        content = "FROM {{{{DBC_DATABASE}}_DATABASE}}.TablesV"
        issues = find_malformed_tokens(content)
        # {{DBC_DATABASE}} is well-formed and gets masked.
        # The outer {{ and trailing }} are orphans.
        assert len(issues) == 2
        assert {i["marker"] for i in issues} == {"{{", "}}"}

    def test_unclosed_token_flagged(self):
        """{{TOKEN with no closing braces."""
        content = "FROM {{DBC.TablesV"
        issues = find_malformed_tokens(content)
        assert len(issues) == 1
        assert issues[0]["marker"] == "{{"

    def test_orphan_close_braces_flagged(self):
        """}} without matching opening."""
        content = "FROM DBC}}.TablesV"
        issues = find_malformed_tokens(content)
        assert len(issues) == 1
        assert issues[0]["marker"] == "}}"

    def test_newline_inside_braces_flagged(self):
        """Line-wrapped token name."""
        content = "FROM {{DBC_\nDATABASE}}.TablesV"
        issues = find_malformed_tokens(content)
        # {{ at start and }} after the wrap — both orphan
        assert len(issues) == 2

    def test_line_and_column_accurate(self):
        """Reported line/column point at the offending marker."""
        content = "line 1 ok\nline 2 with {{ bad }} marker\nline 3 ok"
        issues = find_malformed_tokens(content)
        first = issues[0]
        assert first["line"] == 2
        # '{{' appears at column 13 (1-based) on line 2
        assert first["column"] == 13
        assert "{{ bad }}" in first["line_content"]

    def test_line_content_strips_carriage_return(self):
        """CRLF line endings don't pollute reported line content."""
        content = "FROM {{ X }}.t\r\nNEXT LINE"
        issues = find_malformed_tokens(content)
        assert all("\r" not in i["line_content"] for i in issues)

    def test_multiple_issues_in_one_line(self):
        """All malformed markers on one line are listed."""
        content = "FROM {{ A }} JOIN {{ B }}"
        issues = find_malformed_tokens(content)
        # 2 orphan {{ + 2 orphan }} = 4 findings
        assert len(issues) == 4
        assert all(i["line"] == 1 for i in issues)

    def test_well_formed_with_dash_and_digits(self):
        """Token names allow underscores, digits, and hyphens."""
        content = "{{TOKEN_1}} and {{ABC-XYZ}}"
        assert find_malformed_tokens(content) == []

    def test_lowercase_token_is_well_formed_per_regex(self):
        """The strict regex allows lowercase — convention is upper but
        not enforced at the malformed-detection level."""
        content = "{{db_name}}"
        assert find_malformed_tokens(content) == []


# ---------------------------------------------------------------
# scan_malformed_tokens_in_directory — directory-tree sweep
# ---------------------------------------------------------------


class TestScanMalformedTokensInDirectory:
    """Directory scan applies the same rules as the per-file check."""

    def test_clean_directory_returns_empty(self, tmp_path):
        (tmp_path / "a.sql").write_text("SELECT {{TOKEN}} FROM x;", encoding="utf-8")
        (tmp_path / "b.sql").write_text("SELECT * FROM y;", encoding="utf-8")
        assert scan_malformed_tokens_in_directory(str(tmp_path)) == {}

    def test_finds_corruption_in_one_of_many_files(self, tmp_path):
        (tmp_path / "good.sql").write_text("SELECT {{T}} FROM x;", encoding="utf-8")
        (tmp_path / "bad.sql").write_text(
            "SELECT {{{{T}}_X}} FROM y;", encoding="utf-8"
        )
        result = scan_malformed_tokens_in_directory(str(tmp_path))
        assert len(result) == 1
        bad_file = next(iter(result))
        assert bad_file.endswith("bad.sql")

    def test_skips_hidden_and_underscore_prefixed_files(self, tmp_path):
        (tmp_path / "_skip.sql").write_text("{{ BAD }}", encoding="utf-8")
        (tmp_path / ".hidden.sql").write_text("{{ BAD }}", encoding="utf-8")
        assert scan_malformed_tokens_in_directory(str(tmp_path)) == {}

    def test_recurses_into_subdirectories(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.sql").write_text("{{ BAD }}", encoding="utf-8")
        result = scan_malformed_tokens_in_directory(str(tmp_path))
        assert len(result) == 1


# ---------------------------------------------------------------
# format_malformed_tokens_report — output formatting
# ---------------------------------------------------------------


class TestFormatMalformedTokensReport:
    """Report rendering produces useful output."""

    def test_empty_findings_returns_empty_string(self):
        assert format_malformed_tokens_report({}) == ""

    def test_report_includes_file_path(self):
        findings = {
            "/proj/views/foo.viw": [
                {
                    "line": 4,
                    "column": 17,
                    "marker": "{{",
                    "line_content": "FROM {{{{X}}_Y}}.t",
                }
            ]
        }
        report = format_malformed_tokens_report(findings)
        assert "/proj/views/foo.viw" in report
        assert "line 4" in report
        assert "col 17" in report
        assert "{{{{X}}_Y}}" in report

    def test_report_summary_counts_correct(self):
        findings = {
            "/a.sql": [
                {"line": 1, "column": 1, "marker": "{{", "line_content": "x"},
                {"line": 1, "column": 5, "marker": "}}", "line_content": "x"},
            ],
            "/b.sql": [
                {"line": 1, "column": 1, "marker": "{{", "line_content": "y"},
            ],
        }
        report = format_malformed_tokens_report(findings)
        assert "3 malformed token marker(s)" in report
        assert "2 file(s)" in report

    def test_report_mentions_common_cause(self):
        """The fix-it hint about ingest --token-map is included."""
        findings = {
            "/x.sql": [{"line": 1, "column": 1, "marker": "{{", "line_content": "x"}]
        }
        report = format_malformed_tokens_report(findings)
        assert "ingest" in report.lower()
        assert "token-map" in report.lower()


# ---------------------------------------------------------------
# Integration — builder aborts on malformed tokens
# ---------------------------------------------------------------


class TestBuilderAbortsOnMalformedTokens:
    """The build flow rejects packages with malformed tokens."""

    def test_build_fails_with_corrupted_file(self, tmp_path):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

        # Scaffold
        _run_subprocess(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "scaffold",
                "--name",
                "Tp",
                "--output",
                str(tmp_path),
                "--environments",
                "DEV",
            ],
            env,
        )
        project = tmp_path / "Tp"
        target = project / "payload/database/DDL/views/{{SEM_DATABASE_V}}.bad.viw"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "CREATE VIEW {{SEM_DATABASE_V}}.bad (id) AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT id FROM {{{{DBC_DATABASE}}_DATABASE}}.TablesV;\n",
            encoding="utf-8",
        )

        props = project / "config/properties/DEV.properties"
        props.write_text(
            "SHIPS_ENV=DEV\nENV_PREFIX=D01\nSHIPS_PROJECT=MP\n"
            "SEM_DATABASE_V=D01_MP_SEM_V\nDBC_DATABASE=DBC\n",
            encoding="utf-8",
        )
        output = tmp_path / "out"
        output.mkdir()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "package",
                "--source",
                str(project),
                "--env",
                "DEV",
                "--name",
                "Tp",
                "--properties",
                str(props),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        # Build aborted
        assert result.returncode != 0
        # Useful message in stderr
        assert "Malformed tokens detected" in result.stderr
        assert "{{SEM_DATABASE_V}}.bad.viw" in result.stderr
        # No package was produced
        assert not list(output.glob("*.zip"))

    def test_build_succeeds_with_clean_files(self, tmp_path):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

        _run_subprocess(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "scaffold",
                "--name",
                "Tp",
                "--output",
                str(tmp_path),
                "--environments",
                "DEV",
            ],
            env,
        )
        project = tmp_path / "Tp"

        target = project / "payload/database/DDL/views/D01_MP_DOM_V.OK.viw"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "CREATE VIEW {{DOM_DATABASE_V}}.OK (id) AS\n"
            "LOCKING ROW FOR ACCESS\nSELECT id FROM {{DOM_DATABASE_T}}.OK_T;\n",
            encoding="utf-8",
        )

        props = project / "config/properties/DEV.properties"
        props.write_text(
            "SHIPS_ENV=DEV\nENV_PREFIX=D01\nSHIPS_PROJECT=MP\n"
            "DOM_DATABASE_V=D01_MP_DOM_V\nDOM_DATABASE_T=D01_MP_DOM_T\n",
            encoding="utf-8",
        )
        output = tmp_path / "out"
        output.mkdir()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "package",
                "--source",
                str(project),
                "--env",
                "DEV",
                "--name",
                "Tp",
                "--properties",
                str(props),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        # Build succeeded
        assert result.returncode == 0
        assert list(output.glob("*.zip"))


# ---------------------------------------------------------------
# Integration — harvester word-boundary substitution
# ---------------------------------------------------------------


class TestHarvesterWordBoundarySubstitution:
    """Harvester apply-tokens must not corrupt already-tokenised files."""

    def _scaffold_and_harvest(self, tmp_path, source_content, token_map_content):
        """Helper: scaffold project, write source DDL, run harvest."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

        # Scaffold
        _run_subprocess(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "scaffold",
                "--name",
                "Tp",
                "--output",
                str(tmp_path),
                "--environments",
                "DEV",
            ],
            env,
        )
        project = tmp_path / "Tp"

        # Source DDL with the content under test
        src_dir = tmp_path / "raw"
        src_dir.mkdir()
        (src_dir / "view.viw").write_text(source_content, encoding="utf-8")

        # Token map
        tm = src_dir / "token_map.conf"
        tm.write_text(token_map_content, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "td_release_packager",
                "harvest",
                "--source",
                str(src_dir),
                "--project",
                str(project),
                "--token-map",
                str(tm),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        return result, project

    def test_re_harvest_does_not_double_tokenise(self, tmp_path):
        """The exact scenario that produced Paul's BUILD_0047 corruption.
        Source already contains {{DBC_DATABASE}}, harvester re-runs with
        DBC={{DBC_DATABASE}} mapping, must NOT corrupt the existing token.
        """
        source = (
            "CREATE VIEW {{SEM_DATABASE_V}}.lineage_graph AS\n"
            "SELECT id FROM {{DBC_DATABASE}}.TablesV;\n"
        )
        token_map = (
            "MortgagePlatform_Semantic_V={{SEM_DATABASE_V}}\nDBC={{DBC_DATABASE}}\n"
        )
        result, project = self._scaffold_and_harvest(tmp_path, source, token_map)
        assert result.returncode == 0

        # Find the harvested file and inspect it
        harvested = list((project / "payload").rglob("*.viw"))
        assert harvested, "no harvested .viw file produced"
        content = harvested[0].read_text(encoding="utf-8")

        # The corruption marker would be a quadruple opening brace
        assert "{{{{" not in content, f"Re-tokenisation corruption detected:\n{content}"
        # Original well-formed token preserved exactly once
        assert content.count("{{DBC_DATABASE}}") == 1

    def test_first_harvest_substitutes_literal_dbc(self, tmp_path):
        """A literal 'DBC' in non-tokenised source IS substituted."""
        source = (
            "CREATE VIEW MortgagePlatform_Semantic_V.lineage_graph AS\n"
            "SELECT id FROM DBC.TablesV;\n"
        )
        token_map = (
            "MortgagePlatform_Semantic_V={{SEM_DATABASE_V}}\nDBC={{DBC_DATABASE}}\n"
        )
        result, project = self._scaffold_and_harvest(tmp_path, source, token_map)
        assert result.returncode == 0

        harvested = list((project / "payload").rglob("*.viw"))
        content = harvested[0].read_text(encoding="utf-8")

        # Standalone DBC was tokenised
        assert "{{DBC_DATABASE}}" in content
        assert "{{SEM_DATABASE_V}}" in content
        # No literal DBC. left
        assert "DBC.TablesV" not in content

    def test_word_boundary_protects_overlapping_literals(self, tmp_path):
        """When two literals overlap (e.g. MortgagePlatform_Domain and
        MortgagePlatform_Domain_V), word boundaries prevent the shorter
        literal from corrupting the longer one's match."""
        source = (
            "CREATE VIEW MortgagePlatform_Domain_V.lineage_graph AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT id FROM MortgagePlatform_Domain.t1\n"
            "JOIN MortgagePlatform_Domain_V.v1 ON 1=1;\n"
        )
        token_map = (
            "MortgagePlatform_Domain={{DOM_DATABASE_T}}\n"
            "MortgagePlatform_Domain_V={{DOM_DATABASE_V}}\n"
        )
        result, project = self._scaffold_and_harvest(tmp_path, source, token_map)
        assert result.returncode == 0

        harvested = list((project / "payload").rglob("*.viw"))
        assert harvested, "no harvested .viw file produced"
        content = harvested[0].read_text(encoding="utf-8")

        # Each literal mapped to its correct token
        assert "{{DOM_DATABASE_T}}.t1" in content
        assert "{{DOM_DATABASE_V}}.v1" in content
        # No corruption
        assert "{{{{" not in content
