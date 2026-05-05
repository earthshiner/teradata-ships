"""
test_discovery.py — Tests for the ``discovery`` module that owns
the harvest-candidate extension list and its ships.yaml override.

Three layers exercised:

    1. ``DEFAULT_HARVEST_EXTENSIONS`` baseline shape.
    2. ``resolve_harvest_extensions`` layered resolution
       (defaults + ships.yaml + extra).
    3. End-to-end through ``ingest._discover_files`` and
       ``validate_directory`` so a project with a custom extension
       in ships.yaml actually sees its files harvested + linted.
"""

from __future__ import annotations

import textwrap

from td_release_packager.discovery import (
    DEFAULT_HARVEST_EXTENSIONS,
    normalise_extension,
    resolve_harvest_extensions,
)
from td_release_packager.ingest import _discover_files
from td_release_packager.orchestrator import ships_yaml
from td_release_packager.validate import (
    _collect_package_prereqs,
    validate_directory,
)


# ---------------------------------------------------------------
# normalise_extension
# ---------------------------------------------------------------


class TestNormaliseExtension:
    """Canonicalisation: lower-case, leading dot, whitespace stripped."""

    def test_already_canonical(self):
        assert normalise_extension(".bteq") == ".bteq"

    def test_uppercase_lowered(self):
        assert normalise_extension(".BTEQ") == ".bteq"

    def test_missing_leading_dot_added(self):
        assert normalise_extension("bteq") == ".bteq"

    def test_uppercase_no_dot(self):
        assert normalise_extension("BTEQ") == ".bteq"

    def test_whitespace_stripped(self):
        assert normalise_extension("  .bteq  ") == ".bteq"

    def test_empty_returns_empty(self):
        assert normalise_extension("") == ""

    def test_whitespace_only_returns_empty(self):
        assert normalise_extension("   ") == ""


# ---------------------------------------------------------------
# DEFAULT_HARVEST_EXTENSIONS
# ---------------------------------------------------------------


class TestDefaultExtensions:
    """The default set is the load-bearing canonical list — every
    discovery site reads from it. These tests pin the membership of
    extensions a future cleanup might be tempted to remove."""

    def test_canonical_sql_extensions_present(self):
        for ext in (".sql", ".tbl", ".viw", ".spl", ".mcr"):
            assert ext in DEFAULT_HARVEST_EXTENSIONS

    def test_bteq_and_btq_present(self):
        assert ".bteq" in DEFAULT_HARVEST_EXTENSIONS
        assert ".btq" in DEFAULT_HARVEST_EXTENSIONS

    def test_binaries_excluded(self):
        """Binary artefacts come into the payload via the binary-
        harvest path, not standalone discovery. Including them
        here would trigger spurious 'unclassified' warnings for
        every binary sitting alongside a SQL script."""
        for ext in (".jar", ".c", ".h", ".cpp", ".cc", ".cxx", ".o"):
            assert ext not in DEFAULT_HARVEST_EXTENSIONS

    def test_all_extensions_normalised(self):
        """Every default starts with a dot and is lower-case."""
        for ext in DEFAULT_HARVEST_EXTENSIONS:
            assert ext.startswith(".")
            assert ext == ext.lower()


# ---------------------------------------------------------------
# resolve_harvest_extensions
# ---------------------------------------------------------------


class TestResolveHarvestExtensions:
    """Layered resolution: defaults + ships.yaml + extra."""

    def test_no_project_dir_returns_defaults(self):
        result = resolve_harvest_extensions()
        assert result == DEFAULT_HARVEST_EXTENSIONS

    def test_project_dir_without_ships_yaml(self, tmp_path):
        """A project dir that does not contain ships.yaml falls back
        to defaults — discovery never fails for missing config."""
        result = resolve_harvest_extensions(project_dir=str(tmp_path))
        assert result == DEFAULT_HARVEST_EXTENSIONS

    def test_extra_argument_extends_defaults(self):
        result = resolve_harvest_extensions(extra=[".tdsql"])
        assert ".tdsql" in result
        # Defaults still present.
        assert ".bteq" in result
        assert ".tbl" in result

    def test_extra_argument_normalised(self):
        """Extras go through the same normalisation as ships.yaml entries."""
        result = resolve_harvest_extensions(extra=["TDSQL", " .B "])
        assert ".tdsql" in result
        assert ".b" in result

    def test_ships_yaml_extensions_added(self, tmp_path):
        ships_path = tmp_path / "ships.yaml"
        ships_path.write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions:
                    - .tdsql
                    - bteq2
                """
            ).strip(),
            encoding="utf-8",
        )

        result = resolve_harvest_extensions(project_dir=str(tmp_path))

        assert ".tdsql" in result
        assert ".bteq2" in result
        # Defaults still present.
        assert ".bteq" in result

    def test_ships_yaml_and_extra_both_applied(self, tmp_path):
        """ships.yaml extensions union with the extra argument —
        neither shadows the other."""
        ships_path = tmp_path / "ships.yaml"
        ships_path.write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )

        result = resolve_harvest_extensions(
            project_dir=str(tmp_path), extra=[".cli_only"]
        )

        assert ".tdsql" in result  # from ships.yaml
        assert ".cli_only" in result  # from extra
        assert ".bteq" in result  # from defaults

    def test_malformed_ships_yaml_falls_back_to_defaults(self, tmp_path):
        """A malformed ships.yaml does NOT block discovery —
        validation surfaces the error elsewhere."""
        ships_path = tmp_path / "ships.yaml"
        ships_path.write_text("not: valid: yaml: at all: -", encoding="utf-8")

        result = resolve_harvest_extensions(project_dir=str(tmp_path))

        assert result == DEFAULT_HARVEST_EXTENSIONS

    def test_ships_yaml_without_discovery_block(self, tmp_path):
        """A ships.yaml that doesn't declare a discovery block is
        valid — defaults remain in effect."""
        ships_path = tmp_path / "ships.yaml"
        ships_path.write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                """
            ).strip(),
            encoding="utf-8",
        )

        result = resolve_harvest_extensions(project_dir=str(tmp_path))

        assert result == DEFAULT_HARVEST_EXTENSIONS

    def test_ships_yaml_extensions_not_a_list(self, tmp_path):
        """If discovery.extensions is malformed (not a list),
        defaults still apply — discovery is forgiving."""
        ships_path = tmp_path / "ships.yaml"
        ships_path.write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: "this should be a list"
                """
            ).strip(),
            encoding="utf-8",
        )

        result = resolve_harvest_extensions(project_dir=str(tmp_path))

        assert result == DEFAULT_HARVEST_EXTENSIONS

    def test_returns_frozenset(self):
        """Frozenset return type prevents accidental mutation by callers."""
        assert isinstance(resolve_harvest_extensions(), frozenset)


# ---------------------------------------------------------------
# ships.yaml schema validation
# ---------------------------------------------------------------


class TestShipsYamlDiscoveryValidation:
    """The new ``discovery`` block must validate cleanly when
    well-formed and fail with precise paths when malformed."""

    def _base_doc(self):
        return {"project": "demo", "environments": ["DEV"]}

    def test_valid_discovery_extensions(self):
        doc = self._base_doc()
        doc["discovery"] = {"extensions": [".tdsql", "bteq2"]}
        errors = ships_yaml.validate(doc)
        assert errors == []

    def test_no_discovery_block_is_valid(self):
        errors = ships_yaml.validate(self._base_doc())
        assert errors == []

    def test_discovery_not_a_mapping(self):
        doc = self._base_doc()
        doc["discovery"] = "should be mapping"
        errors = ships_yaml.validate(doc)
        paths = [e.path for e in errors]
        assert "discovery" in paths

    def test_extensions_not_a_list(self):
        doc = self._base_doc()
        doc["discovery"] = {"extensions": ".tdsql"}  # bare string, not list
        errors = ships_yaml.validate(doc)
        paths = [e.path for e in errors]
        assert "discovery.extensions" in paths

    def test_extensions_entry_not_a_string(self):
        doc = self._base_doc()
        doc["discovery"] = {"extensions": [".tdsql", 42]}
        errors = ships_yaml.validate(doc)
        paths = [e.path for e in errors]
        assert "discovery.extensions[1]" in paths

    def test_extensions_entry_empty_string(self):
        doc = self._base_doc()
        doc["discovery"] = {"extensions": [".tdsql", "  "]}
        errors = ships_yaml.validate(doc)
        paths = [e.path for e in errors]
        assert "discovery.extensions[1]" in paths


# ---------------------------------------------------------------
# End-to-end through harvest discovery
# ---------------------------------------------------------------


class TestEndToEndHarvest:
    """A project with ``.tdsql`` declared in ships.yaml has its
    .tdsql files harvested even though .tdsql isn't a SHIPS default."""

    def test_custom_extension_picked_up_via_ships_yaml(self, tmp_path):
        # Project root with ships.yaml declaring .tdsql.
        (tmp_path / "ships.yaml").write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )
        # A .tdsql file that would normally be skipped.
        f = tmp_path / "MyDB.Custom.tdsql"
        f.write_text(
            "CREATE MULTISET TABLE MyDB.Custom (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        # _discover_files now consults ships.yaml when project_dir is given.
        files = _discover_files(str(tmp_path), project_dir=str(tmp_path))

        assert str(f) in files

    def test_default_canonical_extensions_still_work(self, tmp_path):
        """Adding ships.yaml extensions does NOT shadow the canonical
        defaults — .tbl is still discovered alongside the new
        custom extension."""
        (tmp_path / "ships.yaml").write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )
        f_tbl = tmp_path / "MyDB.Standard.tbl"
        f_tbl.write_text(
            "CREATE MULTISET TABLE MyDB.Standard (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )
        f_custom = tmp_path / "MyDB.Custom.tdsql"
        f_custom.write_text(
            "CREATE MULTISET TABLE MyDB.Custom (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        files = _discover_files(str(tmp_path), project_dir=str(tmp_path))

        assert str(f_tbl) in files
        assert str(f_custom) in files

    def test_explicit_file_patterns_bypass_ships_yaml(self, tmp_path):
        """When the caller passes file_patterns explicitly, ships.yaml
        is NOT consulted — the caller's list wins outright. This
        keeps test fixtures and programmatic callers in control."""
        (tmp_path / "ships.yaml").write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )
        f_custom = tmp_path / "MyDB.Custom.tdsql"
        f_custom.write_text("CREATE TABLE MyDB.Custom (Id INT);", encoding="utf-8")

        # Explicit patterns omit .tdsql -> the file is skipped even
        # though ships.yaml declares the extension.
        files = _discover_files(
            str(tmp_path),
            file_patterns=[".tbl"],
            project_dir=str(tmp_path),
        )

        assert str(f_custom) not in files


# ---------------------------------------------------------------
# End-to-end through inspect
# ---------------------------------------------------------------


class TestEndToEndInspect:
    """validate_directory must respect the same project-level config."""

    def test_custom_extension_linted(self, tmp_path):
        (tmp_path / "ships.yaml").write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        # CREATE TABLE without SET/MULTISET -> set_multiset rule fires.
        (ddl_dir / "MyDB.T.tdsql").write_text(
            "CREATE TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 1
        assert any(i.rule == "set_multiset" for i in result.issues)


# ---------------------------------------------------------------
# End-to-end through prereq scan
# ---------------------------------------------------------------


class TestEndToEndPrereqScan:
    """The intra_package_dependency rule's pre-pass must scan
    custom-extension files too — otherwise a CREATE DATABASE in a
    .tdsql file would silently bypass Phase 1."""

    def test_create_database_in_custom_extension(self, tmp_path):
        (tmp_path / "ships.yaml").write_text(
            textwrap.dedent(
                """
                project: demo
                environments: [DEV]
                discovery:
                  extensions: [.tdsql]
                """
            ).strip(),
            encoding="utf-8",
        )
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.tdsql").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYDB"}
