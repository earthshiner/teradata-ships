"""
test_import_legacy_substitutions.py — Tests for the legacy sed
substitution importer (tools/import_legacy_substitutions.py).

Covers:
    1. Parser — each supported marker syntax, escapes, edge cases
    2. Properties emitter — header banner, duplicates, ordering
    3. Migration sed emitter — ordering, dedup, slash escaping
    4. CLI — --env, --output-dir, error paths
    5. Integration — Paul's original sed list parses cleanly and
       round-trips through the SHIPS token engine
"""

from __future__ import annotations

from pathlib import Path

import pytest

from td_release_packager import legacy_importer as importer
from td_release_packager.token_engine import read_properties


# ---------------------------------------------------------------
# Parser
# ---------------------------------------------------------------


class TestParseSedSubstitutions:
    """Tests for parse_sed_substitutions()."""

    def test_dollar_marker(self):
        """`s/$VAR/value/g` produces a Substitution with var=VAR."""
        subs = importer.parse_sed_substitutions("s/$ADMIN_USER/alice/g\n")
        assert len(subs) == 1
        assert subs[0].var_name == "ADMIN_USER"
        assert subs[0].original_marker == "$ADMIN_USER"
        assert subs[0].value == "alice"

    def test_braced_dollar_marker(self):
        """`s/${VAR}/value/g` is parsed as VAR."""
        subs = importer.parse_sed_substitutions("s/${SCHEMA}/myschema/g\n")
        assert len(subs) == 1
        assert subs[0].var_name == "SCHEMA"
        assert subs[0].original_marker == "${SCHEMA}"

    def test_double_amp_marker(self):
        """`s/&&VAR&&/value/g` is parsed as VAR."""
        subs = importer.parse_sed_substitutions("s/&&DATE_FORMAT&&/YYYY/g\n")
        assert len(subs) == 1
        assert subs[0].var_name == "DATE_FORMAT"
        assert subs[0].original_marker == "&&DATE_FORMAT&&"

    def test_escaped_dollar_marker(self):
        """`\\$VAR` is treated identically to `$VAR`."""
        subs = importer.parse_sed_substitutions("s/\\$ADMIN/x/g\n")
        assert len(subs) == 1
        assert subs[0].var_name == "ADMIN"

    def test_empty_value(self):
        """`s/$VAR//g` produces an empty value."""
        subs = importer.parse_sed_substitutions("s/$EMPTY//g\n")
        assert len(subs) == 1
        assert subs[0].value == ""

    def test_sed_escaped_slash_in_value(self):
        """`\\/` in the value unescapes to a literal `/`."""
        subs = importer.parse_sed_substitutions(
            "s/$PATH/usr\\/local\\/bin/g\n"
        )
        assert subs[0].value == "usr/local/bin"

    def test_value_with_sql_paren_type(self):
        """SQL type expressions with parens parse without issue."""
        subs = importer.parse_sed_substitutions(
            "s/&&TS_TYPE&&/TIMESTAMP(6)/g\n"
        )
        assert subs[0].value == "TIMESTAMP(6)"

    def test_value_with_quotes_and_punctuation(self):
        """Quoted values, semicolons, asterisks pass through."""
        subs = importer.parse_sed_substitutions(
            "s/&&DATE_FORMAT&&/FORMAT'YYYY-MM-DD'/g\n"
        )
        assert subs[0].value == "FORMAT'YYYY-MM-DD'"

    def test_blank_lines_skipped(self):
        """Blank lines do not produce substitutions."""
        subs = importer.parse_sed_substitutions(
            "\ns/$A/1/g\n\n\ns/$B/2/g\n\n"
        )
        assert [s.var_name for s in subs] == ["A", "B"]

    def test_comments_skipped(self):
        """Lines starting with `#` are ignored."""
        subs = importer.parse_sed_substitutions(
            "# header comment\ns/$A/1/g\n# trailing\n"
        )
        assert len(subs) == 1
        assert subs[0].var_name == "A"

    def test_unknown_line_skipped_with_warning(self, caplog):
        """A non-substitution line is skipped with a warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            subs = importer.parse_sed_substitutions(
                "echo hello\ns/$A/1/g\n"
            )

        assert len(subs) == 1
        assert subs[0].var_name == "A"
        assert any("not a sed substitution" in r.message for r in caplog.records)

    def test_unrecognised_marker_skipped_with_warning(self, caplog):
        """A sed rule with an unsupported marker syntax is skipped."""
        import logging

        with caplog.at_level(logging.WARNING):
            # %VAR% style is not in the supported set.
            subs = importer.parse_sed_substitutions("s/%FOO%/bar/g\n")

        assert subs == []
        assert any("not a recognised legacy" in r.message for r in caplog.records)

    def test_flags_other_than_g_accepted(self):
        """Sed flag modifiers other than `g` are accepted (and ignored)."""
        subs = importer.parse_sed_substitutions("s/$A/1/\ns/$B/2/i\n")
        assert {s.var_name for s in subs} == {"A", "B"}

    def test_duplicate_var_preserved_in_input_order(self):
        """Duplicates are preserved in order — caller decides resolution."""
        subs = importer.parse_sed_substitutions("s/$A/first/g\ns/$A/second/g\n")
        assert len(subs) == 2
        assert [s.value for s in subs] == ["first", "second"]
        assert [s.line_number for s in subs] == [1, 2]


# ---------------------------------------------------------------
# Properties emitter
# ---------------------------------------------------------------


class TestFormatPropertiesFile:
    """Tests for format_properties_file()."""

    def test_header_includes_env_name(self):
        """The generated banner names the environment."""
        subs = [importer.Substitution("$A", "A", "1", 1)]
        out = importer.format_properties_file("DEV", subs)
        assert "DEV.properties" in out

    def test_simple_dump(self):
        """Each substitution becomes one KEY=VALUE line."""
        subs = [
            importer.Substitution("$A", "A", "1", 1),
            importer.Substitution("&&B&&", "B", "2", 2),
        ]
        out = importer.format_properties_file("DEV", subs)
        assert "A=1" in out
        assert "B=2" in out

    def test_duplicate_emits_warning_comment(self):
        """A duplicate key produces a `# WARN` line before the override."""
        subs = [
            importer.Substitution("$A", "A", "first", 1),
            importer.Substitution("$A", "A", "second", 5),
        ]
        out = importer.format_properties_file("DEV", subs)
        assert "# WARN duplicate 'A' on line 5" in out
        # Both values present, last wins per .properties semantics
        # (file is parsed top-to-bottom, last assignment for a key
        # is the one that survives).
        assert out.count("A=first") == 1
        assert out.count("A=second") == 1

    def test_renders_full_seven_section_scaffold(self):
        """Output must include all 7 canonical sections as
        placeholders so the user can move imports into them."""
        subs = [importer.Substitution("$A", "A", "1", 1)]
        out = importer.format_properties_file("DEV", subs)

        for n in range(1, 8):
            assert f"# {n}." in out, f"section {n} header missing"
        # Plus section 8 for the imports themselves
        assert "# 8. Imported (UNCATEGORISED)" in out

    def test_imports_land_in_section_8_not_loose_at_top(self):
        """Imported entries must appear BELOW section 8's header,
        not interleaved with the canonical sections — the whole
        point is to give the user a re-section workflow."""
        subs = [importer.Substitution("$A", "A", "1", 1)]
        out = importer.format_properties_file("DEV", subs)

        sec8_pos = out.find("# 8. Imported (UNCATEGORISED)")
        a_pos = out.find("A=1")
        assert sec8_pos > 0
        assert a_pos > sec8_pos, (
            "import 'A=1' appears before section 8 header — should be "
            "INSIDE the imported section, not above it"
        )

    def test_sections_1_through_7_show_empty_hint(self):
        """Empty sections must carry the 'no entries' hint comment
        so the user knows to populate by moving from section 8."""
        subs = [importer.Substitution("$A", "A", "1", 1)]
        out = importer.format_properties_file("DEV", subs)
        # All seven canonical sections are empty for import-legacy
        assert out.count("no entries") == 7


# ---------------------------------------------------------------
# Migration sed emitter
# ---------------------------------------------------------------


class TestFormatMigrationSed:
    """Tests for format_migration_sed()."""

    def test_marker_to_token_rule(self):
        """Each substitution becomes `s/<marker>/{{VAR}}/g`."""
        subs = [importer.Substitution("$ADMIN_USER", "ADMIN_USER", "x", 1)]
        out = importer.format_migration_sed(subs)
        assert "s/$ADMIN_USER/{{ADMIN_USER}}/g" in out

    def test_double_amp_marker_preserved_in_lhs(self):
        """The `&&VAR&&` marker is preserved on the LHS, mapped to {{VAR}}."""
        subs = [importer.Substitution("&&DATE_FORMAT&&", "DATE_FORMAT", "x", 1)]
        out = importer.format_migration_sed(subs)
        assert "s/&&DATE_FORMAT&&/{{DATE_FORMAT}}/g" in out

    def test_duplicate_var_emits_one_rule(self):
        """Duplicate var_names produce a single migration rule."""
        subs = [
            importer.Substitution("$A", "A", "first", 1),
            importer.Substitution("$A", "A", "second", 5),
        ]
        out = importer.format_migration_sed(subs)
        assert out.count("s/$A/{{A}}/g") == 1


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


class TestCLI:
    """Tests for the main() entry point."""

    def test_writes_both_artefacts(self, tmp_path):
        """`main()` writes properties and migration files in expected places."""
        sed_file = tmp_path / "legacy.sh"
        sed_file.write_text("s/$A/one/g\ns/&&B&&/two/g\n", encoding="utf-8")

        rc = importer.main(
            ["--script", str(sed_file), "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 0

        props_path = tmp_path / "properties" / "DEV.properties"
        sed_path = tmp_path / "legacy_migration.sed"
        assert props_path.exists()
        assert sed_path.exists()

        props_content = props_path.read_text(encoding="utf-8")
        assert "A=one" in props_content
        assert "B=two" in props_content

        sed_content = sed_path.read_text(encoding="utf-8")
        assert "s/$A/{{A}}/g" in sed_content
        assert "s/&&B&&/{{B}}/g" in sed_content

    def test_missing_input_returns_nonzero(self, tmp_path, capsys):
        """A non-existent input file returns rc=1 with a stderr message."""
        rc = importer.main(
            ["--script", str(tmp_path / "nope.sh"), "--env", "DEV",
             "--output-dir", str(tmp_path)]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "input file not found" in captured.err

    def test_empty_substitution_set_returns_nonzero(self, tmp_path, capsys):
        """A sed file containing no recognisable rules returns rc=1."""
        sed_file = tmp_path / "junk.sh"
        sed_file.write_text("# only comments\n# no rules here\n", encoding="utf-8")

        rc = importer.main(
            ["--script", str(sed_file), "--env", "DEV", "--output-dir", str(tmp_path)]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "no recognisable substitutions" in captured.err


# ---------------------------------------------------------------
# Integration — Paul's original sed list, round-tripped through
# the SHIPS token engine.
# ---------------------------------------------------------------


# The exact sed list Paul started this conversation with. Embedding
# it here gives us a regression test that proves the importer + the
# token engine handle the real-world input end-to-end.
_PAUL_LEGACY_SED = """\
s/$ADMIN_USER/GCFR_APPL_ADMIN_USER/g
s/$PARENT_NODE/PDE_DEV_00/g
s/$DIAGNOSTICS_STMT/GET DIAGNOSTICS EXCEPTION 1 vError_Text MESSAGE_TEXT/g
s/$BASE_NODE/PDE_DEV_MDL/g
s/$BASE_T/PDE_DEV_00_MDL_0_T/g
s/$BASE_V/PDE_DEV_00_MDL_0_V/g
s/$GCFR_NODE/PDE_DEV_00_GCFR/g
s/$GCFR_T/PDE_DEV_00_GCFR_STD_0_T/g
s/$ETL_USER/PDE_DEV_00_GCFR_ETL_USR/g
s/$ETL_USER_ROLE/PDE_DEV_00_GCFR_ETL_USR_ROLE/g
s/$SQL_DATE_FORMAT/YYYY-MM-DD/g
s/$SQL_HIGH_DATE/9999-09-09/g
s/$SQL_TEXT_SIZE/31000/g
s/&&DATE_FORMAT&&/FORMAT'YYYY-MM-DD'/g
s/&&TS_TYPE&&/TIMESTAMP(6)/g
s/&&HIGH_DATE&&/DATE'9999-09-09'/g
s/&&LINE_FEED&&/'0A'XC/g
s/&&CURRENT_TS_TYPE&&/CURRENT_TIMESTAMP(6)/g
s/$DS_BT/ \\/*BT; Workaround for DI Tool Transaction Control *\\//g
s/$JAVA_XSP_SUPPORTED/YES/g
s/$CUSTOM_EXCEPTION_SUPPORTED_FOR_UNICODE/NO/g
s/&&TIME_TYPE&&/TIME(6)/g
s/$SQL_SECURITY_PRIVILEGE_OPTION//g
s/$BMAP_UNKNOWN/UNKNOWN/g
"""


class TestRealWorldRoundTrip:
    """The original sed list parses, emits, and re-resolves cleanly."""

    def test_parser_handles_full_paul_input(self):
        """All rules in the original input are recognised."""
        subs = importer.parse_sed_substitutions(_PAUL_LEGACY_SED)
        assert len(subs) == 24

        # Spot-check the trickier ones
        by_name = {s.var_name: s for s in subs}
        assert by_name["TS_TYPE"].value == "TIMESTAMP(6)"
        assert by_name["DATE_FORMAT"].value == "FORMAT'YYYY-MM-DD'"
        assert by_name["DS_BT"].value == (
            " /*BT; Workaround for DI Tool Transaction Control */"
        )
        assert by_name["SQL_SECURITY_PRIVILEGE_OPTION"].value == ""

    def test_emitted_properties_loads_through_token_engine(self, tmp_path):
        """Generated .properties file passes the token engine validator
        end-to-end (parens accepted, no malformed values, no unresolved
        references). This is the proof that the bootstrap is real."""
        sed_file = tmp_path / "legacy.sh"
        sed_file.write_text(_PAUL_LEGACY_SED, encoding="utf-8")

        rc = importer.main(
            ["--script", str(sed_file), "--env", "DEV",
             "--output-dir", str(tmp_path)]
        )
        assert rc == 0

        props_path = tmp_path / "properties" / "DEV.properties"
        tokens = read_properties(str(props_path))

        # Sanity — both literal and SQL-type tokens survived.
        assert tokens["PARENT_NODE"] == "PDE_DEV_00"
        assert tokens["TS_TYPE"] == "TIMESTAMP(6)"
        assert tokens["TIME_TYPE"] == "TIME(6)"
        assert tokens["CURRENT_TS_TYPE"] == "CURRENT_TIMESTAMP(6)"
        # The empty-value token is preserved as an empty string.
        assert tokens["SQL_SECURITY_PRIVILEGE_OPTION"] == ""
        # 24 unique var names in the input → 24 tokens out.
        assert len(tokens) == 24
