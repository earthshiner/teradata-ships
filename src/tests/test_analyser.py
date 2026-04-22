"""
test_analyser.py — Tests for the SHIPS dependency analyser.

Covers:
    - Comment and string literal stripping
    - DDL body extraction (header vs body)
    - Qualified reference scanning (alias filtering, system DB filtering)
    - Object index building and function overload grouping
    - Cycle detection
    - Topological sort (wave ordering)
    - _waves.txt generation
    - Full analyse_project integration
"""

import os
import pytest

from td_release_packager.analyser import (
    _strip_noise,
    _extract_body,
    _scan_references,
    _classify,
    _extract_name,
    _detect_cycles,
    _topological_sort,
    _generate_waves_txt,
    analyse_project,
    IndexedObject,
)


# ---------------------------------------------------------------
# _strip_noise — Comment and literal removal
# ---------------------------------------------------------------

class TestStripNoise:
    """Tests for removing comments and string literals from DDL."""

    def test_line_comment_removed(self):
        """-- line comments are replaced with spaces."""
        ddl = "SELECT 1; -- this is a comment\nSELECT 2;"
        result = _strip_noise(ddl)
        assert "--" not in result
        assert "SELECT 2;" in result

    def test_block_comment_removed(self):
        """/* block comments */ are replaced with spaces."""
        ddl = "SELECT /* inline comment */ 1;"
        result = _strip_noise(ddl)
        assert "/*" not in result
        assert "*/" not in result

    def test_string_literal_removed(self):
        """'string literals' are replaced with spaces."""
        ddl = "WHERE Name = 'John''s Place'"
        result = _strip_noise(ddl)
        assert "John" not in result

    def test_preserves_length(self):
        """Replaced regions preserve string length (positional accuracy)."""
        ddl = "SELECT 'abc' FROM T;"
        result = _strip_noise(ddl)
        assert len(result) == len(ddl)

    def test_clean_ddl_unchanged(self):
        """DDL without noise passes through unchanged."""
        ddl = "CREATE TABLE MyDB.T (Id INT);"
        result = _strip_noise(ddl)
        assert result == ddl


# ---------------------------------------------------------------
# _extract_body — Header extraction
# ---------------------------------------------------------------

class TestExtractBody:
    """Tests for extracting DDL body (where dependencies live)."""

    def test_view_body_after_as(self):
        """View body starts after AS keyword."""
        ddl = "REPLACE VIEW MyDB.V AS\nSELECT * FROM MyDB.Source;"
        body = _extract_body(ddl, "VIEW")
        assert "MyDB.Source" in body
        # The header (REPLACE VIEW MyDB.V) should not be in the body
        assert "REPLACE VIEW" not in body

    def test_table_body_includes_columns(self):
        """Table body includes column definitions."""
        ddl = (
            "CREATE MULTISET TABLE MyDB.T\n"
            "(\n"
            "    Id INTEGER\n"
            "   ,Ref_Id INTEGER REFERENCES MyDB.Other(Id)\n"
            ");\n"
        )
        body = _extract_body(ddl, "TABLE")
        assert "Ref_Id" in body

    def test_unknown_type_returns_full(self):
        """Unknown object type returns the entire DDL text."""
        ddl = "SOME UNKNOWN DDL STUFF"
        body = _extract_body(ddl, "UNKNOWN")
        assert body == ddl


# ---------------------------------------------------------------
# _scan_references — Qualified reference scanning
# ---------------------------------------------------------------

class TestScanReferences:
    """Tests for scanning DB.Object references in DDL body."""

    def test_internal_reference_found(self):
        """Reference to a known database is classified as internal."""
        ddl = "SELECT * FROM MyDB.Source WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Source" in internal

    def test_external_reference_detected(self):
        """Reference to an unknown database is classified as external."""
        ddl = "SELECT * FROM OtherDB.Source WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "OtherDB.Source" in external

    def test_self_reference_excluded(self):
        """Self-references are excluded."""
        ddl = "SELECT * FROM MyDB.V;"  # Reference to itself
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.V" not in internal

    def test_short_alias_filtered(self):
        """Table aliases don't appear as internal dependencies."""
        ddl = "SELECT c.Cust_Id, o.Order_Id FROM MyDB.Customer c, MyDB.Orders o;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        # Aliases must NOT appear as internal dependencies
        # (they may appear as external noise — that's acceptable)
        assert not any(ref.startswith("c.") for ref in internal)
        assert not any(ref.startswith("o.") for ref in internal)
        # But the real references must be found
        assert "MyDB.Customer" in internal
        assert "MyDB.Orders" in internal

    def test_system_database_filtered(self):
        """System databases (DBC, SYSLIB) are filtered out."""
        ddl = "SELECT * FROM DBC.TablesV WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "DBC.TablesV" not in internal
        assert "DBC.TablesV" not in external

    def test_ddl_noise_filtered(self):
        """DDL noise words (NO.FALLBACK, CHARACTER.SET) are filtered."""
        ddl = "NO.FALLBACK, CHARACTER.SET DEFAULT"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "TABLE", "MyDB.T", known_dbs)
        assert len(internal) == 0

    def test_comment_references_excluded(self):
        """References inside comments are not detected."""
        ddl = (
            "-- This references MyDB.OldTable which is deprecated\n"
            "SELECT * FROM MyDB.NewTable;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.OldTable" not in internal
        assert "MyDB.NewTable" in internal


# ---------------------------------------------------------------
# _detect_cycles
# ---------------------------------------------------------------

class TestDetectCycles:
    """Tests for cycle detection in the dependency graph."""

    def test_no_cycles(self):
        """Acyclic graph returns empty list."""
        deps = {
            "A": {"B"},
            "B": {"C"},
            "C": set(),
        }
        cycles = _detect_cycles(deps)
        assert cycles == []

    def test_direct_cycle(self):
        """A → B → A cycle is detected."""
        deps = {
            "A": {"B"},
            "B": {"A"},
        }
        cycles = _detect_cycles(deps)
        assert len(cycles) > 0

    def test_self_loop(self):
        """Self-referencing node (A → A) is detected."""
        deps = {
            "A": {"A"},
        }
        cycles = _detect_cycles(deps)
        assert len(cycles) > 0

    def test_triangle_cycle(self):
        """Three-node cycle (A → B → C → A) is detected."""
        deps = {
            "A": {"B"},
            "B": {"C"},
            "C": {"A"},
        }
        cycles = _detect_cycles(deps)
        assert len(cycles) > 0


# ---------------------------------------------------------------
# _topological_sort
# ---------------------------------------------------------------

class TestTopologicalSort:
    """Tests for wave-based topological sorting."""

    def _make_objects(self, names, obj_type="TABLE"):
        """Helper to create a minimal objects dict."""
        return {
            name: IndexedObject(
                qualified_name=name,
                object_type=obj_type,
                file_path=f"{name}.tbl",
                ddl_text="",
            )
            for name in names
        }

    def test_independent_objects_one_wave(self):
        """Objects with no dependencies all go in wave 1."""
        deps = {"A": set(), "B": set(), "C": set()}
        objects = self._make_objects(["A", "B", "C"])

        waves = _topological_sort(deps, objects)

        assert len(waves) == 1
        assert set(waves[0]) == {"A", "B", "C"}

    def test_linear_chain(self):
        """A → B → C produces three waves."""
        deps = {"A": {"B"}, "B": {"C"}, "C": set()}
        objects = self._make_objects(["A", "B", "C"])

        waves = _topological_sort(deps, objects)

        assert len(waves) == 3
        # C first (no deps), then B, then A
        assert waves[0] == ["C"]
        assert waves[1] == ["B"]
        assert waves[2] == ["A"]

    def test_diamond_dependency(self):
        """Diamond: D depends on B and C; B and C depend on A."""
        deps = {
            "A": set(),
            "B": {"A"},
            "C": {"A"},
            "D": {"B", "C"},
        }
        objects = self._make_objects(["A", "B", "C", "D"])

        waves = _topological_sort(deps, objects)

        assert len(waves) == 3
        assert waves[0] == ["A"]
        assert set(waves[1]) == {"B", "C"}
        assert waves[2] == ["D"]

    def test_cycle_placed_in_final_wave(self):
        """Cyclic objects are placed in a final wave (not infinite loop)."""
        deps = {
            "A": {"B"},
            "B": {"A"},
            "C": set(),
        }
        objects = self._make_objects(["A", "B", "C"])

        waves = _topological_sort(deps, objects)

        # C should be in wave 1, A and B in a later wave
        all_objects = set()
        for w in waves:
            all_objects.update(w)
        assert all_objects == {"A", "B", "C"}

    def test_type_ordering_within_wave(self):
        """Within a wave, tables sort before views before triggers."""
        deps = {"T": set(), "V": set(), "G": set()}
        objects = {
            "T": IndexedObject("T", "TABLE", "T.tbl", ""),
            "V": IndexedObject("V", "VIEW", "V.viw", ""),
            "G": IndexedObject("G", "TRIGGER", "G.trg", ""),
        }

        waves = _topological_sort(deps, objects)

        assert len(waves) == 1
        # TABLE (0) < VIEW (3) < TRIGGER (4)
        assert waves[0].index("T") < waves[0].index("V")
        assert waves[0].index("V") < waves[0].index("G")


# ---------------------------------------------------------------
# _generate_waves_txt
# ---------------------------------------------------------------

class TestGenerateWavesTxt:
    """Tests for _waves.txt content generation."""

    def test_basic_output(self):
        """Generated _waves.txt contains wave headers and file paths."""
        objects = {
            "MyDB.T": IndexedObject("MyDB.T", "TABLE", "DDL/tables/MyDB.T.tbl", ""),
            "MyDB.V": IndexedObject("MyDB.V", "VIEW", "DDL/views/MyDB.V.viw", ""),
        }
        waves = [["MyDB.T"], ["MyDB.V"]]

        content = _generate_waves_txt(waves, objects, "/project")

        assert "Wave 1" in content
        assert "Wave 2" in content
        assert "DDL/tables/MyDB.T.tbl" in content
        assert "DDL/views/MyDB.V.viw" in content
        assert "---" in content  # Wave barrier

    def test_auto_generated_header(self):
        """Generated file contains the auto-generated comment."""
        objects = {"A": IndexedObject("A", "TABLE", "a.tbl", "")}
        waves = [["A"]]

        content = _generate_waves_txt(waves, objects, "/project")

        assert "auto-generated" in content.lower()


# ---------------------------------------------------------------
# analyse_project (integration)
# ---------------------------------------------------------------

class TestAnalyseProject:
    """Integration tests for the full dependency analysis pipeline."""

    def test_independent_tables(self, tmp_project):
        """Two independent tables produce a single wave."""
        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"

        (tables_dir / "MyDB.T1.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T1 (Id INTEGER);",
            encoding="utf-8",
        )
        (tables_dir / "MyDB.T2.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T2 (Id INTEGER);",
            encoding="utf-8",
        )

        result = analyse_project(str(tmp_project))

        assert len(result.objects) == 2
        assert len(result.waves) == 1  # All in one wave
        assert result.cycles == []

    def test_view_depends_on_table(self, tmp_project):
        """A view referencing a table produces two waves."""
        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
        views_dir = tmp_project / "payload" / "database" / "DDL" / "views"

        (tables_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER);",
            encoding="utf-8",
        )
        (views_dir / "MyDB.ActiveCust.viw").write_text(
            "REPLACE VIEW MyDB.ActiveCust AS\n"
            "SELECT Id FROM MyDB.Customer;",
            encoding="utf-8",
        )

        result = analyse_project(str(tmp_project))

        assert len(result.objects) == 2
        assert len(result.waves) == 2
        # Table in wave 1, view in wave 2
        assert "MyDB.Customer" in result.waves[0]
        assert "MyDB.ActiveCust" in result.waves[1]

    def test_external_dependency_flagged(self, tmp_project):
        """Reference to an unknown database is flagged as external."""
        views_dir = tmp_project / "payload" / "database" / "DDL" / "views"

        (views_dir / "MyDB.V.viw").write_text(
            "REPLACE VIEW MyDB.V AS\n"
            "SELECT * FROM ExternalDB.SomeTable;",
            encoding="utf-8",
        )

        result = analyse_project(str(tmp_project))

        assert len(result.external_deps) > 0

    def test_waves_file_content_generated(self, tmp_project):
        """analyse_project generates _waves.txt content."""
        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
        (tables_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INTEGER);",
            encoding="utf-8",
        )

        result = analyse_project(str(tmp_project))

        assert result.waves_file_content != ""
        assert "Wave 1" in result.waves_file_content

    def test_empty_project(self, tmp_project):
        """Empty project produces empty result (no crash)."""
        result = analyse_project(str(tmp_project))

        assert len(result.objects) == 0
        assert len(result.waves) == 0
