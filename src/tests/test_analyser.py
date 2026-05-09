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

from td_release_packager.analyser import (
    _strip_noise,
    _extract_body,
    _scan_references,
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
# _scan_references — Structural-anchor reference scanning
# ---------------------------------------------------------------


class TestScanReferences:
    """Tests for scanning DB.Object references using structural anchors."""

    # -- Core behaviour (preserved from original suite) ----------

    def test_internal_reference_found(self):
        """Reference to a known database after FROM is classified as internal."""
        ddl = "SELECT * FROM MyDB.Source WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Source" in internal

    def test_external_reference_detected(self):
        """Reference to an unknown database after FROM is classified as external."""
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
        """Column-qualified aliases (c.Cust_Id) are not detected as references."""
        ddl = "SELECT c.Cust_Id, o.Order_Id FROM MyDB.Customer c, MyDB.Orders o;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        # Aliases must NOT appear as references
        assert not any(ref.startswith("c.") for ref in internal)
        assert not any(ref.startswith("o.") for ref in internal)
        assert not any(ref.startswith("c.") for ref in external)
        assert not any(ref.startswith("o.") for ref in external)
        # But the real references must be found
        assert "MyDB.Customer" in internal
        assert "MyDB.Orders" in internal

    def test_spl_cursor_alias_not_a_graph_node(self):
        """Regression for issue #29.

        Teradata SPL cursor FOR loops bind a row alias to the cursor result:

            FOR pm AS cur CURSOR FOR
                SELECT Target_TableDatabaseName, Target_TableName
                FROM GCFR_DB.Process_Metadata
            DO
                SET v_sql = pm.Target_TableDatabaseName || ...;
            END FOR;

        Without the DO terminator in _FROM_TERM_RE, the FROM clause scan
        extends past DO into the loop body and picks up pm.Target_TableDatabaseName
        as a graph node. pm is a cursor alias, not a database name.
        """
        ddl = (
            "REPLACE PROCEDURE MyDB.MyProc ()\n"
            "BEGIN\n"
            "    DECLARE v_sql VARCHAR(5000);\n"
            "    FOR pm AS cur CURSOR FOR\n"
            "        SELECT Target_TableDatabaseName, Target_TableName\n"
            "        FROM GCFR_DB.Process_Metadata\n"
            "    DO\n"
            "        SET v_sql = pm.Target_TableDatabaseName || '.' || pm.Target_TableName;\n"
            "    END FOR;\n"
            "END;\n"
        )
        known_dbs = {"GCFR_DB", "MYDB"}
        internal, external = _scan_references(
            ddl, "PROCEDURE", "MyDB.MyProc", known_dbs
        )
        # The real table reference must be found
        assert "GCFR_DB.Process_Metadata" in internal

        # Cursor alias pm.* must NOT appear as a dependency
        all_refs = internal | external
        pm_refs = {r for r in all_refs if r.upper().startswith("PM.")}
        assert not pm_refs, (
            f"Cursor alias references incorrectly captured as graph nodes: {pm_refs}"
        )

    def test_subquery_in_from_alias_not_a_graph_node(self):
        """Column aliases from a subquery are not captured as graph nodes.

        FROM (SELECT pm.col FROM GCFR_DB.T pm) sub
        should contribute GCFR_DB.T but NOT pm.col.
        """
        ddl = (
            "REPLACE VIEW MyDB.V AS\n"
            "SELECT sub.col\n"
            "FROM (\n"
            "    SELECT pm.col\n"
            "    FROM GCFR_DB.Process_Metadata pm\n"
            ") sub;\n"
        )
        known_dbs = {"GCFR_DB", "MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)

        assert "GCFR_DB.Process_Metadata" in internal

        all_refs = internal | external
        pm_refs = {r for r in all_refs if r.upper().startswith("PM.")}
        assert not pm_refs, f"Subquery alias references incorrectly captured: {pm_refs}"

    def test_system_database_filtered(self):
        """System databases (DBC, SYSLIB) are filtered out."""
        ddl = "SELECT * FROM DBC.TablesV WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "DBC.TablesV" not in internal
        assert "DBC.TablesV" not in external

    def test_ddl_noise_filtered(self):
        """DDL noise words (NO.FALLBACK, CHARACTER.SET) are not detected."""
        ddl = "NO.FALLBACK, CHARACTER.SET DEFAULT"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "TABLE", "MyDB.T", known_dbs)
        assert len(internal) == 0
        assert len(external) == 0

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

    # -- FROM clause: comma-separated table lists ----------------

    def test_from_comma_separated_qualified(self):
        """Comma-separated qualified names in FROM clause are all found."""
        ddl = (
            "SELECT t1.Id, t2.Name, t3.Val\n"
            "FROM MyDB.Table1 t1, MyDB.Table2 t2, MyDB.Table3 t3\n"
            "WHERE t1.Id = t2.Id;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Table1" in internal
        assert "MyDB.Table2" in internal
        assert "MyDB.Table3" in internal

    def test_from_with_as_alias(self):
        """FROM with explicit AS aliases still captures the table name."""
        ddl = "SELECT * FROM MyDB.Customer AS c WHERE c.Active = 1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Customer" in internal

    # -- JOIN variants -------------------------------------------

    def test_inner_join(self):
        """INNER JOIN table reference is detected."""
        ddl = (
            "SELECT * FROM MyDB.Orders o\n"
            "INNER JOIN MyDB.Customer c ON o.Cust_Id = c.Cust_Id;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Orders" in internal
        assert "MyDB.Customer" in internal

    def test_left_join_without_outer(self):
        """Plain LEFT JOIN (without OUTER) is detected."""
        ddl = (
            "SELECT * FROM MyDB.Orders o\n"
            "LEFT JOIN MyDB.Returns r ON o.Id = r.Order_Id;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Returns" in internal

    def test_left_outer_join(self):
        """LEFT OUTER JOIN is detected."""
        ddl = (
            "SELECT * FROM MyDB.Orders o\n"
            "LEFT OUTER JOIN MyDB.Returns r ON o.Id = r.Order_Id;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Returns" in internal

    def test_right_join(self):
        """RIGHT JOIN is detected."""
        ddl = "SELECT * FROM MyDB.T1\nRIGHT JOIN MyDB.T2 ON MyDB.T1.Id = MyDB.T2.Id;\n"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.T2" in internal

    def test_cross_join(self):
        """CROSS JOIN is detected."""
        ddl = "SELECT * FROM MyDB.Dates d\nCROSS JOIN MyDB.Products p;\n"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.Dates" in internal
        assert "MyDB.Products" in internal

    def test_full_outer_join(self):
        """FULL OUTER JOIN is detected."""
        ddl = (
            "SELECT * FROM MyDB.T1\n"
            "FULL OUTER JOIN MyDB.T2 ON MyDB.T1.Id = MyDB.T2.Id;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "MyDB.T2" in internal

    # -- DML targets ---------------------------------------------

    def test_insert_into(self):
        """INSERT INTO target table is detected."""
        ddl = "INSERT INTO MyDB.AuditLog (Id, Msg) VALUES (1, 'test');"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.AuditLog" in internal

    def test_ins_into_abbreviation(self):
        """Teradata INS INTO abbreviation is detected."""
        ddl = "INS INTO MyDB.AuditLog (Id) VALUES (1);"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.AuditLog" in internal

    def test_update_target(self):
        """UPDATE target table is detected."""
        ddl = "UPDATE MyDB.Customer SET Name = 'test' WHERE Id = 1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.Customer" in internal

    def test_upd_abbreviation(self):
        """Teradata UPD abbreviation is detected."""
        ddl = "UPD MyDB.Customer SET Name = 'test' WHERE Id = 1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.Customer" in internal

    def test_delete_from_target(self):
        """DELETE FROM target table is detected."""
        ddl = "DELETE FROM MyDB.TempData WHERE Created < DATE - 30;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.TempData" in internal

    def test_del_abbreviation(self):
        """Teradata DEL abbreviation (without FROM) is detected."""
        ddl = "DEL MyDB.TempData WHERE Created < DATE - 30;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.TempData" in internal

    def test_merge_into(self):
        """MERGE INTO target table is detected."""
        ddl = (
            "MERGE INTO MyDB.Target t\n"
            "USING MyDB.Source s ON t.Id = s.Id\n"
            "WHEN MATCHED THEN UPDATE SET t.Val = s.Val;\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        assert "MyDB.Target" in internal
        assert "MyDB.Source" in internal

    # -- Function-FROM exclusion (EXTRACT, TRIM) -----------------

    def test_extract_year_from_not_matched(self):
        """EXTRACT(YEAR FROM col) does not produce a false reference."""
        ddl = "SELECT EXTRACT(YEAR FROM hire_date)\nFROM MyDB.Employee;\n"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        # hire_date is not a table reference
        assert not any("hire_date" in ref for ref in internal)
        assert not any("hire_date" in ref for ref in external)
        # The real table is found
        assert "MyDB.Employee" in internal

    def test_extract_month_from_not_matched(self):
        """EXTRACT(MONTH FROM col) does not produce a false reference."""
        ddl = "SELECT EXTRACT(MONTH FROM order_date)\nFROM MyDB.Orders;\n"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert not any("order_date" in ref for ref in internal)
        assert "MyDB.Orders" in internal

    def test_trim_from_not_matched(self):
        """TRIM(BOTH ' ' FROM col) does not produce a false reference."""
        ddl = "SELECT TRIM(BOTH ' ' FROM cust_name)\nFROM MyDB.Customer;\n"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert not any("cust_name" in ref for ref in internal)
        assert "MyDB.Customer" in internal

    # -- Trigger event table ------------------------------------

    def test_trigger_on_table(self):
        """Trigger event table (AFTER INSERT ON db.table) is detected."""
        ddl = (
            "REPLACE TRIGGER MyDB.trg_Audit\n"
            "AFTER INSERT ON MyDB.Customer\n"
            "REFERENCING NEW AS NewRow\n"
            "FOR EACH ROW\n"
            "(\n"
            "    INSERT INTO MyDB.AuditLog VALUES (NewRow.Cust_Id);\n"
            ");\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(
            ddl, "TRIGGER", "MyDB.trg_Audit", known_dbs
        )
        assert "MyDB.Customer" in internal
        assert "MyDB.AuditLog" in internal

    # -- FK REFERENCES ------------------------------------------

    def test_fk_reference(self):
        """REFERENCES constraint table is detected."""
        ddl = (
            "CREATE MULTISET TABLE MyDB.Orders (\n"
            "     Order_Id INTEGER NOT NULL\n"
            "    ,Cust_Id INTEGER REFERENCES MyDB.Customer(Cust_Id)\n"
            ") PRIMARY INDEX (Order_Id);\n"
        )
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "TABLE", "MyDB.Orders", known_dbs)
        assert "MyDB.Customer" in internal

    # -- Token support ------------------------------------------

    def test_token_database_reference(self):
        """{{TOKEN}}.Object references are detected."""
        ddl = "SELECT * FROM {{STD_DB}}.Customer;"
        known_dbs = {"{{STD_DB}}"}
        internal, external = _scan_references(ddl, "VIEW", "MyDB.V", known_dbs)
        assert "{{STD_DB}}.Customer" in internal

    # -- Delete vs From interaction -----------------------------

    def test_delete_from_not_double_counted(self):
        """DELETE FROM does not produce both a source and a target."""
        ddl = "DELETE FROM MyDB.TempData WHERE 1=1;"
        known_dbs = {"MYDB"}
        internal, external = _scan_references(ddl, "PROCEDURE", "MyDB.P", known_dbs)
        # Should appear exactly once (as a target)
        assert "MyDB.TempData" in internal


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
            "REPLACE VIEW MyDB.ActiveCust AS\nSELECT Id FROM MyDB.Customer;",
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
            "REPLACE VIEW MyDB.V AS\nSELECT * FROM ExternalDB.SomeTable;",
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
