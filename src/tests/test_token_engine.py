"""
test_token_engine.py — Tests for the SHIPS token substitution engine.

Covers:
    - Properties file reading (comments, blanks, duplicates)
    - Internal reference resolution ({{TOKEN}} within values)
    - Circular reference detection
    - Token scanning in files and directories
    - Token validation (undefined, unused, reserved)
    - Token substitution in content
    - File-level substitution
"""

import os
import pytest

from td_release_packager.token_engine import (
    read_properties,
    _resolve_internal_references,
    scan_tokens_in_file,
    scan_tokens_in_directory,
    validate_tokens,
    substitute_tokens,
    substitute_file,
    derive_token_name,
    generate_token_map,
    write_token_map,
    read_token_map,
)


# ---------------------------------------------------------------
# read_properties
# ---------------------------------------------------------------

class TestReadProperties:
    """Tests for reading and parsing .properties files."""

    def test_basic_key_value(self, tmp_path):
        """Simple KEY=VALUE pairs are read correctly."""
        props = tmp_path / "test.properties"
        props.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")

        result = read_properties(str(props))

        assert result["FOO"] == "bar"
        assert result["BAZ"] == "qux"

    def test_comments_and_blanks_skipped(self, tmp_path):
        """Lines starting with '#' and empty lines are ignored."""
        props = tmp_path / "test.properties"
        props.write_text(
            "# This is a comment\n"
            "\n"
            "TOKEN=value\n"
            "  \n"
            "# Another comment\n",
            encoding="utf-8",
        )

        result = read_properties(str(props))

        assert result == {"TOKEN": "value"}

    def test_value_with_equals_sign(self, tmp_path):
        """Values containing '=' are preserved (split on first '=' only)."""
        props = tmp_path / "test.properties"
        props.write_text("CONN=host=myserver;port=1025\n", encoding="utf-8")

        result = read_properties(str(props))

        assert result["CONN"] == "host=myserver;port=1025"

    def test_whitespace_stripped(self, tmp_path):
        """Leading/trailing whitespace on names and values is stripped."""
        props = tmp_path / "test.properties"
        props.write_text("  TOKEN  =  value with spaces  \n", encoding="utf-8")

        result = read_properties(str(props))

        assert result["TOKEN"] == "value with spaces"

    def test_duplicate_key_uses_last(self, tmp_path):
        """Duplicate keys use the last-defined value."""
        props = tmp_path / "test.properties"
        props.write_text("TOKEN=first\nTOKEN=second\n", encoding="utf-8")

        result = read_properties(str(props))

        assert result["TOKEN"] == "second"

    def test_missing_file_raises(self, tmp_path):
        """FileNotFoundError raised for non-existent properties file."""
        with pytest.raises(FileNotFoundError):
            read_properties(str(tmp_path / "missing.properties"))

    def test_line_without_equals_skipped(self, tmp_path):
        """Lines without '=' are skipped with a warning."""
        props = tmp_path / "test.properties"
        props.write_text("VALID=yes\nbad line no equals\n", encoding="utf-8")

        result = read_properties(str(props))

        assert result == {"VALID": "yes"}

    def test_empty_name_skipped(self, tmp_path):
        """Lines with '=' but empty name are skipped."""
        props = tmp_path / "test.properties"
        props.write_text("=orphan_value\nVALID=yes\n", encoding="utf-8")

        result = read_properties(str(props))

        assert result == {"VALID": "yes"}


# ---------------------------------------------------------------
# _resolve_internal_references
# ---------------------------------------------------------------

class TestResolveInternalReferences:
    """Tests for iterative {{TOKEN}} resolution within property values."""

    def test_simple_reference(self):
        """A value referencing another token is resolved."""
        tokens = {
            "PREFIX": "DEV",
            "DATABASE": "{{PREFIX}}_STD",
        }

        result = _resolve_internal_references(tokens)

        assert result["DATABASE"] == "DEV_STD"

    def test_chained_references(self):
        """Multi-level chained references are resolved iteratively."""
        tokens = {
            "ENV": "DEV",
            "PREFIX": "A_{{ENV}}",
            "DATABASE": "{{PREFIX}}_OMR_STD",
        }

        result = _resolve_internal_references(tokens)

        assert result["PREFIX"] == "A_DEV"
        assert result["DATABASE"] == "A_DEV_OMR_STD"

    def test_multiple_tokens_in_one_value(self):
        """A value containing multiple token references is fully resolved."""
        tokens = {
            "ENV_PREFIX": "A_D01",
            "PROJECT": "OMR",
            "DATABASE": "{{ENV_PREFIX}}_{{PROJECT}}_STD",
        }

        result = _resolve_internal_references(tokens)

        assert result["DATABASE"] == "A_D01_OMR_STD"

    def test_circular_reference_converges_to_unresolved(self):
        """Mutual A↔B references converge to self-references (left unresolved)."""
        tokens = {
            "A": "{{B}}",
            "B": "{{A}}",
        }

        # Does not raise — mutual refs converge to self-refs
        result = _resolve_internal_references(tokens, max_passes=10)

        # Both values still contain unresolved {{}} references
        assert "{{" in result["A"]
        assert "{{" in result["B"]

    def test_self_reference_left_unresolved(self):
        """A token referencing itself is left as-is (not infinite loop)."""
        tokens = {
            "SELF": "prefix_{{SELF}}_suffix",
        }

        # Should not raise — self-references are skipped by the replacer
        result = _resolve_internal_references(tokens)

        # The self-reference remains unresolved
        assert "{{SELF}}" in result["SELF"]

    def test_unknown_reference_left_unresolved(self):
        """References to undefined tokens are left as-is."""
        tokens = {
            "DATABASE": "{{UNKNOWN}}_STD",
        }

        result = _resolve_internal_references(tokens)

        assert result["DATABASE"] == "{{UNKNOWN}}_STD"

    def test_no_references_passthrough(self):
        """Values without {{}} pass through unchanged."""
        tokens = {"PLAIN": "no_tokens_here"}

        result = _resolve_internal_references(tokens)

        assert result["PLAIN"] == "no_tokens_here"


# ---------------------------------------------------------------
# scan_tokens_in_file / scan_tokens_in_directory
# ---------------------------------------------------------------

class TestTokenScanning:
    """Tests for discovering {{TOKEN}} references in files."""

    def test_scan_single_file(self, tmp_path):
        """Tokens in a single file are discovered."""
        ddl = tmp_path / "test.tbl"
        ddl.write_text(
            "CREATE TABLE {{STD_DATABASE}}.MyTable\n"
            "( Col1 INTEGER );\n",
            encoding="utf-8",
        )

        tokens = scan_tokens_in_file(str(ddl))

        assert tokens == {"STD_DATABASE"}

    def test_scan_multiple_tokens(self, tmp_path):
        """Multiple distinct tokens in one file are all found."""
        ddl = tmp_path / "test.viw"
        ddl.write_text(
            "REPLACE VIEW {{STD_DATABASE}}.MyView AS\n"
            "SELECT * FROM {{SEM_DATABASE}}.Source;\n",
            encoding="utf-8",
        )

        tokens = scan_tokens_in_file(str(ddl))

        assert tokens == {"STD_DATABASE", "SEM_DATABASE"}

    def test_scan_directory_skips_hidden(self, tmp_path):
        """Hidden files and underscore-prefixed files are skipped."""
        (tmp_path / "visible.tbl").write_text("{{TOKEN_A}}", encoding="utf-8")
        (tmp_path / ".hidden.tbl").write_text("{{TOKEN_B}}", encoding="utf-8")
        (tmp_path / "_waves.txt").write_text("{{TOKEN_C}}", encoding="utf-8")

        result = scan_tokens_in_directory(str(tmp_path))

        assert len(result) == 1
        assert "TOKEN_A" in list(result.values())[0]

    def test_scan_directory_skips_sample_files(self, tmp_path):
        """Files ending in .sample are skipped."""
        (tmp_path / "template.sample").write_text("{{TOKEN}}", encoding="utf-8")
        (tmp_path / "real.tbl").write_text("{{TOKEN}}", encoding="utf-8")

        result = scan_tokens_in_directory(str(tmp_path))

        # Only real.tbl should be returned
        assert len(result) == 1

    def test_scan_file_no_tokens(self, tmp_path):
        """File with no tokens returns an empty set."""
        ddl = tmp_path / "test.tbl"
        ddl.write_text("CREATE TABLE MyDB.Stuff (Col1 INT);\n", encoding="utf-8")

        tokens = scan_tokens_in_file(str(ddl))

        assert tokens == set()


# ---------------------------------------------------------------
# validate_tokens
# ---------------------------------------------------------------

class TestValidateTokens:
    """Tests for token validation (undefined and unused detection)."""

    def test_all_tokens_defined(self):
        """No errors when all referenced tokens are defined."""
        values = {"DB": "MyDB", "USER": "MyUser"}
        usage = {"file1.tbl": {"DB"}, "file2.viw": {"USER"}}

        errors, warnings = validate_tokens(values, usage)

        assert errors == []

    def test_undefined_token_is_error(self):
        """Tokens referenced but not defined produce errors."""
        values = {"DB": "MyDB"}
        usage = {"file1.tbl": {"DB", "MISSING"}}

        errors, warnings = validate_tokens(values, usage)

        assert any("MISSING" in e for e in errors)

    def test_unused_token_is_warning(self):
        """Tokens defined but never referenced produce warnings."""
        values = {"DB": "MyDB", "EXTRA": "unused"}
        usage = {"file1.tbl": {"DB"}}

        errors, warnings = validate_tokens(values, usage)

        assert any("EXTRA" in w for w in warnings)

    def test_reserved_properties_not_flagged_unused(self):
        """SHIPS_ENV, SHIPS_PROJECT, ENV_PREFIX are never flagged as unused."""
        values = {
            "SHIPS_ENV": "DEV",
            "SHIPS_PROJECT": "OMR",
            "ENV_PREFIX": "A_D01",
            "DB": "MyDB",
        }
        usage = {"file1.tbl": {"DB"}}

        errors, warnings = validate_tokens(values, usage)

        # None of the reserved properties should appear in warnings
        reserved = {"SHIPS_ENV", "SHIPS_PROJECT", "ENV_PREFIX"}
        for w in warnings:
            for r in reserved:
                assert r not in w


# ---------------------------------------------------------------
# substitute_tokens
# ---------------------------------------------------------------

class TestSubstituteTokens:
    """Tests for {{TOKEN}} replacement in content strings."""

    def test_basic_substitution(self):
        """Single token is replaced with its value."""
        content = "CREATE TABLE {{DB}}.MyTable (Col1 INT);"
        values = {"DB": "ProdDB"}

        result, count = substitute_tokens(content, values)

        assert result == "CREATE TABLE ProdDB.MyTable (Col1 INT);"
        assert count == 1

    def test_multiple_substitutions(self):
        """Multiple occurrences of the same token are all replaced."""
        content = "{{DB}}.TableA JOIN {{DB}}.TableB"
        values = {"DB": "ProdDB"}

        result, count = substitute_tokens(content, values)

        assert result == "ProdDB.TableA JOIN ProdDB.TableB"
        assert count == 2

    def test_undefined_token_raises(self):
        """Undefined token in content raises KeyError."""
        content = "{{MISSING}}.MyTable"
        values = {}

        with pytest.raises(KeyError, match="MISSING"):
            substitute_tokens(content, values)

    def test_no_tokens_no_change(self):
        """Content without tokens passes through unchanged."""
        content = "SELECT 1 AS dummy;"
        values = {"DB": "ProdDB"}

        result, count = substitute_tokens(content, values)

        assert result == content
        assert count == 0


# ---------------------------------------------------------------
# substitute_file
# ---------------------------------------------------------------

class TestSubstituteFile:
    """Tests for file-level token substitution."""

    def test_file_substitution(self, tmp_path):
        """Tokens in a source file are replaced in the destination."""
        src = tmp_path / "source.tbl"
        src.write_text(
            "CREATE TABLE {{DB}}.Orders (Id INT);",
            encoding="utf-8",
        )
        dest = tmp_path / "output" / "resolved.tbl"
        values = {"DB": "ProdDB"}

        count = substitute_file(str(src), str(dest), values)

        assert count == 1
        assert dest.read_text(encoding="utf-8") == (
            "CREATE TABLE ProdDB.Orders (Id INT);"
        )

    def test_creates_dest_directory(self, tmp_path):
        """Destination directory is created if it does not exist."""
        src = tmp_path / "source.tbl"
        src.write_text("{{TOKEN}}", encoding="utf-8")
        dest = tmp_path / "deep" / "nested" / "output.tbl"

        substitute_file(str(src), str(dest), {"TOKEN": "VALUE"})

        assert dest.exists()


# ---------------------------------------------------------------
# Integration: read_properties with internal references
# ---------------------------------------------------------------

class TestPropertiesIntegration:
    """End-to-end tests using the sample properties fixture."""

    def test_env_topology_resolution(self, sample_properties_file):
        """Properties with {{ENV_PREFIX}} and {{SHIPS_PROJECT}} resolve correctly."""
        tokens = read_properties(str(sample_properties_file))

        assert tokens["SHIPS_ENV"] == "DEV"
        assert tokens["ENV_PREFIX"] == "A_D01"
        assert tokens["SHIPS_PROJECT"] == "OMR"
        assert tokens["STD_DATABASE"] == "A_D01_OMR_STD"
        assert tokens["SEM_DATABASE"] == "A_D01_OMR_SEM"


# ---------------------------------------------------------------
# derive_token_name
# ---------------------------------------------------------------

class TestDeriveTokenName:
    """Tests for deriving token names by stripping environment prefixes."""

    def test_standard_prefix_strip(self):
        """Standard prefix is stripped, leaving the suffix."""
        assert derive_token_name("A_D01_OMR_STD", "A_D01") == "OMR_STD"

    def test_short_prefix(self):
        """Short prefix (e.g. 'P') is stripped correctly."""
        assert derive_token_name("P_OMR_STD", "P") == "OMR_STD"

    def test_multi_char_prefix(self):
        """Multi-character prefix (e.g. 'DEV01') is stripped."""
        assert derive_token_name("DEV01_CORE", "DEV01") == "CORE"

    def test_prefix_not_found(self):
        """When prefix doesn't match, full name is returned."""
        assert derive_token_name("PROD_CORE", "DEV01") == "PROD_CORE"

    def test_case_insensitive(self):
        """Prefix matching is case-insensitive."""
        assert derive_token_name("a_d01_OMR_STD", "A_D01") == "OMR_STD"

    def test_prefix_is_entire_name(self):
        """When prefix equals the full name, full name is returned."""
        assert derive_token_name("A_D01", "A_D01") == "A_D01"

    def test_underscore_separator_stripped(self):
        """Leading underscore after prefix is stripped."""
        assert derive_token_name("DEV_CORE_DB", "DEV") == "CORE_DB"


# ---------------------------------------------------------------
# generate_token_map
# ---------------------------------------------------------------

class TestGenerateTokenMap:
    """Tests for building literal → {{TOKEN}} mappings."""

    def test_basic_generation(self):
        """Database names are mapped to {{TOKEN}} placeholders."""
        db_names = {
            "A_D01_OMR_STD": ["file1.tbl", "file2.viw"],
            "A_D01_OMR_SEM": ["file3.viw"],
        }
        result = generate_token_map(db_names, "A_D01")

        assert result["A_D01_OMR_STD"] == "{{OMR_STD}}"
        assert result["A_D01_OMR_SEM"] == "{{OMR_SEM}}"

    def test_system_dbs_excluded(self):
        """System databases (DBC, SYSUDTLIB) are excluded."""
        db_names = {
            "DBC": ["file1.tbl"],
            "SYSUDTLIB": ["file2.fnc"],
            "A_D01_OMR_STD": ["file3.tbl"],
        }
        result = generate_token_map(db_names, "A_D01")

        assert "DBC" not in result
        assert "SYSUDTLIB" not in result
        assert "A_D01_OMR_STD" in result

    def test_empty_input(self):
        """Empty db_names produces empty map."""
        result = generate_token_map({}, "A_D01")
        assert result == {}

    def test_no_prefix_uses_full_name(self):
        """Without env_prefix, full database name becomes the token."""
        db_names = {
            "CORE_STD": ["file1.tbl"],
            "SHARED_UTILS": ["file2.viw"],
        }
        result = generate_token_map(db_names)

        assert result["CORE_STD"] == "{{CORE_STD}}"
        assert result["SHARED_UTILS"] == "{{SHARED_UTILS}}"

    def test_no_prefix_still_excludes_system_dbs(self):
        """System databases are excluded even without a prefix."""
        db_names = {
            "DBC": ["file1.tbl"],
            "CORE_STD": ["file2.tbl"],
        }
        result = generate_token_map(db_names)

        assert "DBC" not in result
        assert "CORE_STD" in result


# ---------------------------------------------------------------
# write_token_map / read_token_map
# ---------------------------------------------------------------

class TestTokenMapIO:
    """Tests for writing and reading token_map.conf files."""

    def test_roundtrip(self, tmp_path):
        """Written token map can be read back identically."""
        token_map = {
            "A_D01_OMR_STD": "{{OMR_STD}}",
            "A_D01_OMR_SEM": "{{OMR_SEM}}",
        }
        db_names = {
            "A_D01_OMR_STD": ["file1.tbl", "file2.viw"],
            "A_D01_OMR_SEM": ["file3.viw"],
        }
        map_path = str(tmp_path / "config" / "token_map.conf")

        write_token_map(map_path, token_map, db_names, "A_D01")
        loaded = read_token_map(map_path)

        assert loaded == token_map

    def test_read_skips_comments(self, tmp_path):
        """Comment lines and blank lines are skipped."""
        conf = tmp_path / "token_map.conf"
        conf.write_text(
            "# This is a comment\n"
            "\n"
            "MY_DB={{MY_TOKEN}}\n"
            "  \n"
            "# Another comment\n",
            encoding="utf-8",
        )

        result = read_token_map(str(conf))

        assert result == {"MY_DB": "{{MY_TOKEN}}"}

    def test_read_rejects_non_token_values(self, tmp_path):
        """Values without {{}} are skipped with a warning."""
        conf = tmp_path / "token_map.conf"
        conf.write_text(
            "GOOD_DB={{GOOD_TOKEN}}\n"
            "BAD_DB=plain_value\n",
            encoding="utf-8",
        )

        result = read_token_map(str(conf))

        assert "GOOD_DB" in result
        assert "BAD_DB" not in result

    def test_read_missing_file_raises(self, tmp_path):
        """Missing token map file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            read_token_map(str(tmp_path / "missing.conf"))

    def test_write_creates_directories(self, tmp_path):
        """write_token_map creates parent directories if needed."""
        map_path = str(tmp_path / "deep" / "nested" / "token_map.conf")
        write_token_map(map_path, {"DB": "{{TOK}}"}, {"DB": ["f.tbl"]}, "X")

        assert os.path.exists(map_path)

    def test_write_includes_reference_counts(self, tmp_path):
        """Written file includes reference count comments."""
        db_names = {
            "A_D01_OMR_STD": ["f1.tbl", "f2.viw", "f3.viw"],
        }
        token_map = {"A_D01_OMR_STD": "{{OMR_STD}}"}
        map_path = str(tmp_path / "token_map.conf")

        write_token_map(map_path, token_map, db_names, "A_D01")

        content = open(map_path, encoding="utf-8").read()
        assert "3 references" in content
