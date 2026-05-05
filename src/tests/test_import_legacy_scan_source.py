"""
test_import_legacy_scan_source.py — Phase B of the placeholder
visibility work: ``import-legacy --scan-source`` discovers
non-SHIPS placeholders directly from a source DDL tree, with no
sed script required.

Three layers exercised:

  1. ``scan_source_directory`` (the heavy-lifting function):
     aggregation by var_name, multi-syntax handling, file walk,
     ScanResult shape.
  2. The new formatters (``scan_format_properties_file``,
     ``scan_format_report``) and the updated
     ``format_migration_sed`` (dedupe by (marker, var_name)).
  3. CLI: ``--scan-source`` end-to-end via ``main()`` -- both the
     happy path and the empty-findings path.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager import legacy_importer as importer


# ---------------------------------------------------------------
# scan_source_directory
# ---------------------------------------------------------------


class TestScanSourceDirectory:
    """Walk a source tree and aggregate findings into ScanResult."""

    def test_empty_directory_returns_empty_result(self, tmp_path):
        result = importer.scan_source_directory(str(tmp_path))
        assert result.substitutions == []
        assert result.var_counts == {}
        assert result.files_scanned == 0
        assert result.files_with_placeholders == 0
        assert result.total_occurrences == 0

    def test_missing_directory_raises(self, tmp_path):
        import pytest

        with pytest.raises(FileNotFoundError):
            importer.scan_source_directory(str(tmp_path / "nope"))

    def test_single_dollar_placeholder_aggregated(self, tmp_path):
        (tmp_path / "x.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        result = importer.scan_source_directory(str(tmp_path))

        assert result.files_scanned == 1
        assert result.files_with_placeholders == 1
        assert result.total_occurrences == 1
        assert result.var_counts == {"UTL_T": 1}
        assert len(result.substitutions) == 1
        assert result.substitutions[0].var_name == "UTL_T"
        assert result.substitutions[0].original_marker == "$UTL_T"
        # Value is empty -- the user fills it in after import.
        assert result.substitutions[0].value == ""

    def test_multiple_syntaxes_for_same_var_produce_multiple_substitutions(
        self, tmp_path
    ):
        """``$UTL_T``, ``${UTL_T}`` and ``&&UTL_T&&`` all converge on
        the same logical token, but the migration sed needs THREE
        rules so every form gets rewritten. Verify the scanner
        emits one Substitution per (marker, var_name) pair while
        var_counts shows the combined count."""
        (tmp_path / "a.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        (tmp_path / "b.tbl").write_text(
            "CREATE TABLE ${UTL_T}.B (Id INT);", encoding="utf-8"
        )
        (tmp_path / "c.tbl").write_text(
            "CREATE TABLE MyDb.C (D DATE &&UTL_T&&);", encoding="utf-8"
        )

        result = importer.scan_source_directory(str(tmp_path))

        # Three separate Substitutions, all UTL_T.
        assert len(result.substitutions) == 3
        markers = {s.original_marker for s in result.substitutions}
        assert markers == {"$UTL_T", "${UTL_T}", "&&UTL_T&&"}
        assert all(s.var_name == "UTL_T" for s in result.substitutions)

        # Single var_count entry showing the combined total.
        assert result.var_counts == {"UTL_T": 3}
        assert result.var_to_syntaxes["UTL_T"] == {"dollar", "dollar-braced", "amp-amp"}

    def test_substitutions_ordered_by_frequency_desc(self, tmp_path):
        """The most-impactful tokens (highest occurrence count) sort
        first so they appear at the top of the .properties file."""
        # 3x UTL_T, 1x DATE_FORMAT
        (tmp_path / "a.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        (tmp_path / "b.tbl").write_text(
            "CREATE TABLE $UTL_T.B (Id INT);", encoding="utf-8"
        )
        (tmp_path / "c.tbl").write_text(
            "CREATE TABLE $UTL_T.C (Id INT);", encoding="utf-8"
        )
        (tmp_path / "d.tbl").write_text(
            "CREATE TABLE MyDb.D (D DATE &&DATE_FORMAT&&);",
            encoding="utf-8",
        )

        result = importer.scan_source_directory(str(tmp_path))

        # First substitution is UTL_T (3 occurrences) before
        # DATE_FORMAT (1 occurrence).
        assert result.substitutions[0].var_name == "UTL_T"
        assert result.substitutions[-1].var_name == "DATE_FORMAT"

    def test_recursive_walk(self, tmp_path):
        """Files in nested sub-directories are discovered."""
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "x.tbl").write_text("CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8")

        result = importer.scan_source_directory(str(tmp_path))

        assert result.files_with_placeholders == 1
        assert result.var_counts == {"UTL_T": 1}

    def test_files_without_placeholders_counted_as_scanned(self, tmp_path):
        (tmp_path / "clean.tbl").write_text(
            "CREATE TABLE {{T_DB}}.X (Id INT);", encoding="utf-8"
        )
        (tmp_path / "dirty.tbl").write_text(
            "CREATE TABLE $UTL_T.Y (Id INT);", encoding="utf-8"
        )

        result = importer.scan_source_directory(str(tmp_path))

        assert result.files_scanned == 2
        assert result.files_with_placeholders == 1
        assert result.var_counts == {"UTL_T": 1}

    def test_per_token_sample_files_truncated(self, tmp_path):
        """Per-token (file, line) lists cap at the sample limit so a
        token used in 1000 files doesn't bloat the report."""
        # Generate more files than _SAMPLE_LIMIT.
        n = importer._SAMPLE_LIMIT + 5
        for i in range(n):
            (tmp_path / f"f_{i:03d}.tbl").write_text(
                "CREATE TABLE $UTL_T.X (Id INT);", encoding="utf-8"
            )

        result = importer.scan_source_directory(str(tmp_path))

        # Total count is the full n.
        assert result.var_counts["UTL_T"] == n
        # Sampled files cap at the limit.
        assert len(result.var_to_files["UTL_T"]) == importer._SAMPLE_LIMIT

    def test_already_tokenised_source_no_findings(self, tmp_path):
        (tmp_path / "x.tbl").write_text(
            "CREATE TABLE {{T_DB}}.X (Id INT);", encoding="utf-8"
        )
        result = importer.scan_source_directory(str(tmp_path))
        assert result.substitutions == []


# ---------------------------------------------------------------
# format_migration_sed dedup change ((marker, var_name) not just var_name)
# ---------------------------------------------------------------


class TestFormatMigrationSedDedupByMarkerAndVarName:
    """Multiple syntaxes for the same var_name must produce
    multiple sed rules. Pre-Phase-B the dedup was by var_name
    alone; the second-and-later syntaxes were silently dropped."""

    def test_multiple_syntaxes_each_get_a_rule(self):
        subs = [
            importer.Substitution("$UTL_T", "UTL_T", "", 1),
            importer.Substitution("${UTL_T}", "UTL_T", "", 1),
            importer.Substitution("&&UTL_T&&", "UTL_T", "", 1),
        ]
        out = importer.format_migration_sed(subs)
        assert "s/$UTL_T/{{UTL_T}}/g" in out
        assert "s/${UTL_T}/{{UTL_T}}/g" in out
        assert "s/&&UTL_T&&/{{UTL_T}}/g" in out

    def test_same_marker_twice_dedupes(self):
        """Truly identical (marker, var_name) pairs collapse to one
        rule -- the only kind of dedup that's actually safe."""
        subs = [
            importer.Substitution("$UTL_T", "UTL_T", "", 1),
            importer.Substitution("$UTL_T", "UTL_T", "", 7),
        ]
        out = importer.format_migration_sed(subs)
        assert out.count("s/$UTL_T/{{UTL_T}}/g") == 1


# ---------------------------------------------------------------
# scan_format_properties_file
# ---------------------------------------------------------------


def _scan_with(tmp_path: Path, files: dict) -> "importer.ScanResult":
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return importer.scan_source_directory(str(tmp_path))


class TestScanFormatPropertiesFile:
    """Properties output for scan-source: one entry per var_name,
    frequency-ordered, value empty, comment showing source."""

    def test_one_entry_per_var_name(self, tmp_path):
        scan = _scan_with(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE $UTL_T.A (Id INT);",
                "b.tbl": "CREATE TABLE ${UTL_T}.B (Id INT);",
            },
        )
        out = importer.scan_format_properties_file("DEV", scan)

        # Single UTL_T= entry even though two syntaxes were detected.
        assert out.count("UTL_T=") == 1

    def test_value_is_empty_user_fills(self, tmp_path):
        scan = _scan_with(tmp_path, {"a.tbl": "CREATE TABLE $UTL_T.A (Id INT);"})
        out = importer.scan_format_properties_file("DEV", scan)

        # Empty value -- "UTL_T=" with nothing after the equals.
        assert "UTL_T=\n" in out or "UTL_T=" in out.rstrip().splitlines()

    def test_comment_above_each_entry_shows_count_and_sample(self, tmp_path):
        scan = _scan_with(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE $UTL_T.A (Id INT);",
                "b.tbl": "CREATE TABLE $UTL_T.B (Id INT);",
            },
        )
        out = importer.scan_format_properties_file("DEV", scan)

        # The comment names the count and the sample file.
        assert "UTL_T: 2 occurrences" in out
        assert "a.tbl" in out  # whichever file was sampled first

    def test_frequency_order(self, tmp_path):
        scan = _scan_with(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE $UTL_T.A (Id INT);",
                "b.tbl": "CREATE TABLE $UTL_T.B (Id INT);",
                "c.tbl": "CREATE TABLE MyDb.C (D DATE &&DATE_FORMAT&&);",
            },
        )
        out = importer.scan_format_properties_file("DEV", scan)

        # UTL_T (count=2) appears in the file BEFORE DATE_FORMAT (count=1).
        utl_pos = out.find("UTL_T=")
        date_pos = out.find("DATE_FORMAT=")
        assert utl_pos != -1
        assert date_pos != -1
        assert utl_pos < date_pos


# ---------------------------------------------------------------
# scan_format_report
# ---------------------------------------------------------------


class TestScanFormatReport:
    """Audit report shape -- summary header, by-frequency table,
    per-token detail."""

    def test_summary_counts_present(self, tmp_path):
        scan = _scan_with(
            tmp_path,
            {
                "a.tbl": "CREATE TABLE $UTL_T.A (Id INT);",
                "b.tbl": "CREATE TABLE ${UTL_T}.B (Id INT);",
            },
        )
        out = importer.scan_format_report(scan, str(tmp_path))

        assert "Files scanned: 2" in out
        assert "Files with placeholders: 2" in out
        assert "Total occurrences: 2" in out
        assert "Distinct tokens: 1" in out

    def test_per_token_table_present(self, tmp_path):
        scan = _scan_with(tmp_path, {"a.tbl": "CREATE TABLE $UTL_T.A (Id INT);"})
        out = importer.scan_format_report(scan, str(tmp_path))

        assert "Tokens by frequency" in out
        assert "Per-token detail" in out
        assert "`UTL_T`" in out

    def test_empty_scan_produces_no_findings_message(self, tmp_path):
        scan = _scan_with(tmp_path, {"clean.tbl": "CREATE TABLE {{T_DB}}.X (Id INT);"})
        out = importer.scan_format_report(scan, str(tmp_path))

        assert "No placeholders found" in out


# ---------------------------------------------------------------
# CLI integration: --scan-source end-to-end
# ---------------------------------------------------------------


class TestScanSourceCLI:
    """End-to-end through ``main()`` -- the command writes the three
    artefacts and prints the next-steps banner."""

    def test_writes_three_artefacts(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "x.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        out_dir = tmp_path / "out"

        rc = importer.main(
            [
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(out_dir),
            ]
        )

        assert rc == 0
        assert (out_dir / "env" / "DEV.conf").exists()
        assert (out_dir / "legacy_migration.sed").exists()
        assert (out_dir / "scan_report.md").exists()

    def test_properties_file_has_uncategorised_token(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "x.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        out_dir = tmp_path / "out"

        importer.main(
            [
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(out_dir),
            ]
        )

        props = (out_dir / "env" / "DEV.conf").read_text(encoding="utf-8")
        assert "UTL_T=" in props

    def test_migration_sed_covers_all_syntaxes(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.tbl").write_text(
            "CREATE TABLE $UTL_T.A (Id INT);", encoding="utf-8"
        )
        (source / "b.tbl").write_text(
            "CREATE TABLE ${UTL_T}.B (Id INT);", encoding="utf-8"
        )
        (source / "c.tbl").write_text(
            "CREATE TABLE MyDb.C (D DATE &&DATE_FORMAT&&);",
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"

        importer.main(
            [
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(out_dir),
            ]
        )

        sed = (out_dir / "legacy_migration.sed").read_text(encoding="utf-8")
        assert "s/$UTL_T/{{UTL_T}}/g" in sed
        assert "s/${UTL_T}/{{UTL_T}}/g" in sed
        assert "s/&&DATE_FORMAT&&/{{DATE_FORMAT}}/g" in sed

    def test_empty_source_returns_zero_no_files_written(self, tmp_path):
        """If no placeholders are found the command exits 0 with an
        informational message and DOESN'T write empty artefacts."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "clean.tbl").write_text(
            "CREATE TABLE {{T_DB}}.X (Id INT);", encoding="utf-8"
        )
        out_dir = tmp_path / "out"

        rc = importer.main(
            [
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(out_dir),
            ]
        )

        assert rc == 0
        assert not (out_dir / "env" / "DEV.conf").exists()
        assert not (out_dir / "legacy_migration.sed").exists()
        assert not (out_dir / "scan_report.md").exists()

    def test_missing_source_dir_returns_one(self, tmp_path, capsys):
        rc = importer.main(
            [
                "--scan-source",
                str(tmp_path / "nope"),
                "--env",
                "DEV",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "source directory not found" in captured.err

    def test_script_and_scan_source_mutually_exclusive(self, tmp_path):
        """Argparse rejects passing both --script and --scan-source."""
        import pytest

        sed = tmp_path / "legacy.sh"
        sed.write_text("s/$A/x/g\n", encoding="utf-8")
        source = tmp_path / "src"
        source.mkdir()

        # argparse exits with SystemExit on mutually-exclusive
        # violation; pytest captures it.
        with pytest.raises(SystemExit):
            importer.main(
                [
                    "--script",
                    str(sed),
                    "--scan-source",
                    str(source),
                    "--env",
                    "DEV",
                    "--output-dir",
                    str(tmp_path),
                ]
            )

    def test_neither_mode_required(self, tmp_path):
        import pytest

        with pytest.raises(SystemExit):
            importer.main(["--env", "DEV", "--output-dir", str(tmp_path)])


# ---------------------------------------------------------------
# CLI dispatcher wiring (td_release_packager subcommand layer)
# ---------------------------------------------------------------
#
# These tests go through ``td_release_packager.cli.main()`` rather
# than ``legacy_importer.main()`` directly. Regression-pinning the
# subcommand argparse layer -- when --scan-source was first added,
# the standalone script's parser was updated but the
# td_release_packager subcommand's parser wasn't, so
# ``python -m td_release_packager import-legacy --scan-source ...``
# rejected the unknown argument.


class TestCLIDispatcherScanSource:
    """``python -m td_release_packager import-legacy --scan-source``
    must reach the importer end-to-end."""

    def _invoke_cli(self, argv):
        """Run the package-level CLI's main() and capture exit code."""
        import sys

        import pytest

        from td_release_packager.cli import main

        old_argv = sys.argv
        sys.argv = ["td_release_packager"] + argv
        try:
            with pytest.raises(SystemExit) as ei:
                main()
            return int(ei.value.code) if ei.value.code is not None else 0
        finally:
            sys.argv = old_argv

    def test_scan_source_via_subcommand(self, tmp_path, capsys):
        source = tmp_path / "src"
        source.mkdir()
        (source / "x.tbl").write_text(
            "CREATE TABLE $UTL_T.X (Id INT);", encoding="utf-8"
        )
        out = tmp_path / "out"

        rc = self._invoke_cli(
            [
                "import-legacy",
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(out),
            ]
        )
        capsys.readouterr()  # discard stdout

        assert rc == 0
        assert (out / "env" / "DEV.conf").exists()
        assert (out / "legacy_migration.sed").exists()
        assert (out / "scan_report.md").exists()

    def test_script_via_subcommand(self, tmp_path, capsys):
        sed_file = tmp_path / "legacy.sh"
        sed_file.write_text("s/$A/one/g\n", encoding="utf-8")
        out = tmp_path / "out"

        rc = self._invoke_cli(
            [
                "import-legacy",
                "--script",
                str(sed_file),
                "--env",
                "DEV",
                "--output-dir",
                str(out),
            ]
        )
        capsys.readouterr()

        assert rc == 0
        assert (out / "env" / "DEV.conf").exists()
        assert (out / "legacy_migration.sed").exists()
        # --script mode does NOT emit scan_report.md.
        assert not (out / "scan_report.md").exists()

    def test_subcommand_rejects_both_modes(self, tmp_path):
        """Argparse mutual exclusion at the subcommand layer too."""
        sed = tmp_path / "legacy.sh"
        sed.write_text("s/$A/x/g\n", encoding="utf-8")
        source = tmp_path / "src"
        source.mkdir()

        rc = self._invoke_cli(
            [
                "import-legacy",
                "--script",
                str(sed),
                "--scan-source",
                str(source),
                "--env",
                "DEV",
                "--output-dir",
                str(tmp_path),
            ]
        )
        # Argparse exits 2 on mutual-exclusion violation.
        assert rc == 2

    def test_subcommand_rejects_neither_mode(self, tmp_path):
        rc = self._invoke_cli(
            ["import-legacy", "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 2
