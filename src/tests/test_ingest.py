"""
test_ingest.py — Tests for the SHIPS DDL ingest (harvest) module.

Covers:
    - DDL classification (_classify_ddl) for all supported object types
    - Qualified name extraction (_extract_qualified_name)
    - SPECIFIC function name extraction
    - MULTISET injection for tables
    - REPLACE VIEW injection utility
    - Token candidate detection (hardcoded database names)
    - File discovery and filtering
"""

import os
import pytest

from td_release_packager.ingest import (
    _classify_ddl,
    _extract_qualified_name,
    _extract_specific_function_name,
    _inject_multiset,
    _inject_replace_view,
    _build_token_candidates,
    _discover_files,
    ingest_directory,
)


# ---------------------------------------------------------------
# _classify_ddl — Object type classification
# ---------------------------------------------------------------


class TestClassifyDDL:
    """Tests for DDL content classification."""

    # -- Tables --

    def test_create_multiset_table(self, ddl_create_table):
        """CREATE MULTISET TABLE is classified as TABLE."""
        assert _classify_ddl(ddl_create_table) == "TABLE"

    def test_create_table_without_multiset(self, ddl_create_table_no_multiset):
        """CREATE TABLE (no SET/MULTISET) is classified as TABLE."""
        assert _classify_ddl(ddl_create_table_no_multiset) == "TABLE"

    def test_create_set_table(self):
        """CREATE SET TABLE is classified as TABLE."""
        ddl = "CREATE SET TABLE MyDB.Dedup (Id INT) UNIQUE PRIMARY INDEX (Id);"
        assert _classify_ddl(ddl) == "TABLE"

    def test_create_volatile_table(self):
        """CREATE VOLATILE TABLE is classified as TABLE."""
        ddl = "CREATE MULTISET VOLATILE TABLE MyDB.Tmp (Col1 INT);"
        assert _classify_ddl(ddl) == "TABLE"

    def test_create_global_temporary_table(self):
        """CREATE GLOBAL TEMPORARY TABLE is classified as TABLE."""
        ddl = "CREATE MULTISET GLOBAL TEMPORARY TABLE MyDB.GTT (Col1 INT);"
        assert _classify_ddl(ddl) == "TABLE"

    def test_create_global_temporary_trace_table(self, ddl_global_temp_trace_table):
        """CREATE GLOBAL TEMPORARY TRACE TABLE — the bug that bit us."""
        assert _classify_ddl(ddl_global_temp_trace_table) == "TABLE"

    def test_replace_table(self):
        """REPLACE TABLE is classified as TABLE (Teradata supports this)."""
        ddl = "REPLACE TABLE MyDB.Customer (Id INT);"
        assert _classify_ddl(ddl) == "TABLE"

    # -- Views --

    def test_replace_view(self, ddl_replace_view):
        """REPLACE VIEW is classified as VIEW."""
        assert _classify_ddl(ddl_replace_view) == "VIEW"

    def test_create_view(self, ddl_create_view):
        """CREATE VIEW is classified as VIEW."""
        assert _classify_ddl(ddl_create_view) == "VIEW"

    # -- Macros --

    def test_replace_macro(self):
        """REPLACE MACRO is classified as MACRO."""
        ddl = "REPLACE MACRO MyDB.mc_Report AS (SELECT 1;);"
        assert _classify_ddl(ddl) == "MACRO"

    def test_create_macro(self):
        """CREATE MACRO is classified as MACRO."""
        ddl = "CREATE MACRO MyDB.mc_Report AS (SELECT 1;);"
        assert _classify_ddl(ddl) == "MACRO"

    # -- Procedures --

    def test_replace_procedure(self):
        """REPLACE PROCEDURE is classified as PROCEDURE."""
        ddl = "REPLACE PROCEDURE MyDB.sp_DoStuff() BEGIN END;"
        assert _classify_ddl(ddl) == "PROCEDURE"

    def test_create_procedure(self):
        """CREATE PROCEDURE is classified as PROCEDURE."""
        ddl = "CREATE PROCEDURE MyDB.sp_DoStuff() BEGIN END;"
        assert _classify_ddl(ddl) == "PROCEDURE"

    # -- Functions --

    def test_replace_function(self):
        """REPLACE FUNCTION is classified as FUNCTION."""
        ddl = "REPLACE FUNCTION MyDB.fn_Calc(x INT) RETURNS INT RETURN x;"
        assert _classify_ddl(ddl) == "FUNCTION"

    def test_replace_specific_function(self):
        """REPLACE SPECIFIC FUNCTION is classified as FUNCTION."""
        ddl = "REPLACE SPECIFIC FUNCTION MyDB.fn_Calc_Int RETURNS INT RETURN 1;"
        assert _classify_ddl(ddl) == "FUNCTION"

    # -- Triggers --

    def test_create_trigger(self):
        """CREATE TRIGGER is classified as TRIGGER."""
        ddl = "CREATE TRIGGER MyDB.trg_Audit AFTER INSERT ON MyDB.Tbl FOR EACH ROW (SELECT 1;);"
        assert _classify_ddl(ddl) == "TRIGGER"

    def test_replace_trigger(self, ddl_replace_trigger):
        """REPLACE TRIGGER is classified as TRIGGER — the bug that bit us."""
        assert _classify_ddl(ddl_replace_trigger) == "TRIGGER"

    # -- Join / Hash / Secondary Indexes --

    def test_create_join_index(self, ddl_create_join_index):
        """CREATE JOIN INDEX is classified as JOIN_INDEX."""
        assert _classify_ddl(ddl_create_join_index) == "JOIN_INDEX"

    def test_create_hash_index(self):
        """CREATE HASH INDEX is classified as HASH_INDEX."""
        ddl = (
            "CREATE HASH INDEX MyDB.HI_Cust (Cust_Id) ON MyDB.Customer ORDER BY VALUES;"
        )
        assert _classify_ddl(ddl) == "HASH_INDEX"

    def test_create_index(self):
        """CREATE INDEX is classified as INDEX."""
        ddl = "CREATE INDEX idx_Name (Name) ON MyDB.Customer;"
        assert _classify_ddl(ddl) == "INDEX"

    def test_create_unique_index(self):
        """CREATE UNIQUE INDEX is classified as INDEX."""
        ddl = "CREATE UNIQUE INDEX idx_Id (Id) ON MyDB.Customer;"
        assert _classify_ddl(ddl) == "INDEX"

    # -- Pre-requisites and DCL --

    def test_create_database(self, ddl_create_database):
        """CREATE DATABASE is classified as DATABASE."""
        assert _classify_ddl(ddl_create_database) == "DATABASE"

    def test_create_user(self):
        """CREATE USER is classified as USER."""
        ddl = "CREATE USER svc_account FROM MyDB AS PERMANENT = 1e6;"
        assert _classify_ddl(ddl) == "USER"

    def test_create_profile(self):
        """CREATE PROFILE is classified as PROFILE."""
        ddl = "CREATE PROFILE batch_profile;"
        assert _classify_ddl(ddl) == "PROFILE"

    def test_create_role(self):
        """CREATE ROLE is classified as ROLE."""
        ddl = "CREATE ROLE read_only;"
        assert _classify_ddl(ddl) == "ROLE"

    def test_grant(self, ddl_grant):
        """GRANT is classified as GRANT."""
        assert _classify_ddl(ddl_grant) == "GRANT"

    def test_revoke(self):
        """REVOKE is classified as REVOKE."""
        ddl = "REVOKE SELECT ON MyDB FROM SomeRole;"
        assert _classify_ddl(ddl) == "REVOKE"

    # -- Unclassifiable --

    def test_unclassifiable_returns_none(self):
        """Random text returns None."""
        assert _classify_ddl("This is not DDL at all.") is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert _classify_ddl("") is None

    # -- System-scope types --

    def test_create_map(self, ddl_create_map):
        """CREATE MAP is classified as MAP."""
        assert _classify_ddl(ddl_create_map) == "MAP"

    def test_create_map_contiguous(self):
        """CREATE MAP with CONTIGUOUS syntax is classified as MAP."""
        ddl = "CREATE MAP TD_GLOBALMAP CONTIGUOUS AMP BETWEEN 0 AND 7;"
        assert _classify_ddl(ddl) == "MAP"

    def test_create_authorization(self, ddl_create_authorization):
        """CREATE AUTHORIZATION is classified as AUTHORIZATION."""
        assert _classify_ddl(ddl_create_authorization) == "AUTHORIZATION"

    def test_create_foreign_server(self, ddl_create_foreign_server):
        """CREATE FOREIGN SERVER is classified as FOREIGN_SERVER."""
        assert _classify_ddl(ddl_create_foreign_server) == "FOREIGN_SERVER"

    # -- JAR installation --

    def test_jar_install(self, ddl_jar_install):
        """CALL SQLJ.INSTALL_JAR is classified as JAR."""
        assert _classify_ddl(ddl_jar_install) == "JAR"

    def test_jar_replace(self, ddl_jar_replace):
        """CALL SQLJ.REPLACE_JAR is classified as JAR."""
        assert _classify_ddl(ddl_jar_replace) == "JAR"

    # -- Specificity ordering --

    def test_join_index_before_table(self):
        """JOIN INDEX matched before generic TABLE pattern."""
        ddl = "CREATE JOIN INDEX MyDB.JI_X AS SELECT * FROM MyDB.T;"
        assert _classify_ddl(ddl) == "JOIN_INDEX"

    def test_hash_index_before_index(self):
        """HASH INDEX matched before generic INDEX pattern."""
        ddl = "CREATE HASH INDEX MyDB.HI_X (Col) ON MyDB.T ORDER BY VALUES;"
        assert _classify_ddl(ddl) == "HASH_INDEX"


# ---------------------------------------------------------------
# _extract_qualified_name
# ---------------------------------------------------------------


class TestExtractQualifiedName:
    """Tests for extracting DB.ObjectName from DDL."""

    def test_two_part_name(self, ddl_create_table):
        """Two-part DB.Object name is extracted correctly."""
        db, obj = _extract_qualified_name(ddl_create_table)
        assert db == "MyDB"
        assert obj == "Customer"

    def test_single_part_name(self):
        """Single-part name returns (None, ObjectName)."""
        ddl = "CREATE TABLE Customer (Id INT);"
        db, obj = _extract_qualified_name(ddl)
        assert db is None
        assert obj == "Customer"

    def test_quoted_identifiers(self):
        """Quoted identifiers have quotes stripped (simple names)."""
        ddl = 'CREATE TABLE "MyDB"."MyTable" (Id INT);'
        db, obj = _extract_qualified_name(ddl)
        assert db == "MyDB"
        assert obj == "MyTable"

    def test_view_name_extraction(self, ddl_replace_view):
        """View name is extracted from REPLACE VIEW."""
        db, obj = _extract_qualified_name(ddl_replace_view)
        assert db == "MyDB"
        assert obj == "ActiveCustomers"

    def test_no_match_returns_none(self):
        """Unclassifiable content returns (None, None)."""
        db, obj = _extract_qualified_name("SELECT 1;")
        assert db is None
        assert obj is None


# ---------------------------------------------------------------
# _extract_specific_function_name
# ---------------------------------------------------------------


class TestExtractSpecificFunctionName:
    """Tests for SPECIFIC name extraction from function DDL."""

    def test_specific_name_extracted(self, ddl_function_with_specific):
        """SPECIFIC name is extracted from the function body."""
        result = _extract_specific_function_name(ddl_function_with_specific)
        assert result == "fn_Calc_Int"

    def test_qualified_specific_name(self):
        """Qualified SPECIFIC name returns the object part only."""
        ddl = (
            "REPLACE FUNCTION MyDB.fn_X(p INT) RETURNS INT\n"
            "SPECIFIC MyDB.fn_X_OverloadA\n"
            "RETURN p;\n"
        )
        result = _extract_specific_function_name(ddl)
        assert result == "fn_X_OverloadA"

    def test_no_specific_clause(self):
        """Function without SPECIFIC clause returns None."""
        ddl = "REPLACE FUNCTION MyDB.fn_Simple(x INT) RETURNS INT RETURN x;"
        result = _extract_specific_function_name(ddl)
        assert result is None


# ---------------------------------------------------------------
# _inject_multiset
# ---------------------------------------------------------------


class TestInjectMultiset:
    """Tests for MULTISET injection into CREATE TABLE DDL."""

    def test_inject_when_missing(self, ddl_create_table_no_multiset):
        """MULTISET is injected when neither SET nor MULTISET is present."""
        result, injected = _inject_multiset(ddl_create_table_no_multiset)
        assert injected is True
        assert "MULTISET TABLE" in result

    def test_no_inject_when_multiset_present(self, ddl_create_table):
        """No injection when MULTISET is already present."""
        result, injected = _inject_multiset(ddl_create_table)
        assert injected is False
        assert result == ddl_create_table

    def test_no_inject_when_set_present(self):
        """No injection when SET is already specified."""
        ddl = "CREATE SET TABLE MyDB.Dedup (Id INT);"
        result, injected = _inject_multiset(ddl)
        assert injected is False
        assert result == ddl

    def test_inject_volatile_table(self):
        """MULTISET is injected before VOLATILE TABLE."""
        ddl = "CREATE VOLATILE TABLE MyDB.Tmp (Col INT);"
        result, injected = _inject_multiset(ddl)
        assert injected is True
        assert "CREATE MULTISET VOLATILE TABLE" in result

    def test_inject_global_temporary_trace(self):
        """MULTISET is injected for GLOBAL TEMPORARY TRACE TABLE."""
        ddl = "CREATE GLOBAL TEMPORARY TRACE TABLE MyDB.Trace (Col INT);"
        result, injected = _inject_multiset(ddl)
        assert injected is True
        assert "CREATE MULTISET GLOBAL TEMPORARY TRACE TABLE" in result


# ---------------------------------------------------------------
# _inject_replace_view
# ---------------------------------------------------------------


class TestInjectReplaceView:
    """Tests for CREATE VIEW → REPLACE VIEW conversion."""

    def test_create_view_converted(self, ddl_create_view):
        """CREATE VIEW is converted to REPLACE VIEW."""
        result, injected = _inject_replace_view(ddl_create_view)
        assert injected is True
        assert "REPLACE VIEW" in result
        assert "CREATE VIEW" not in result

    def test_replace_view_unchanged(self, ddl_replace_view):
        """REPLACE VIEW is left unchanged."""
        result, injected = _inject_replace_view(ddl_replace_view)
        assert injected is False
        assert result == ddl_replace_view

    def test_non_view_unchanged(self, ddl_create_table):
        """Non-view DDL is left unchanged."""
        result, injected = _inject_replace_view(ddl_create_table)
        assert injected is False


# ---------------------------------------------------------------
# _build_token_candidates
# ---------------------------------------------------------------


class TestBuildTokenCandidates:
    """Tests for hardcoded database name detection."""

    def test_user_databases_detected(self):
        """User databases are flagged as token candidates."""
        db_names = {
            "DEV01_STD": ["file1.tbl", "file2.viw"],
            "DEV01_SEM": ["file3.viw"],
        }
        result = _build_token_candidates(db_names)
        assert "DEV01_STD" in result
        assert "DEV01_SEM" in result

    def test_system_databases_excluded(self):
        """System databases (DBC, SYSUDTLIB, etc.) are not flagged."""
        db_names = {
            "DBC": ["file1.tbl"],
            "SYSUDTLIB": ["file2.fnc"],
            "SYSLIB": ["file3.spl"],
            "TD_SYSFNLIB": ["file4.fnc"],
            "MyDB": ["file5.tbl"],
        }
        result = _build_token_candidates(db_names)
        assert "DBC" not in result
        assert "SYSUDTLIB" not in result
        assert "SYSLIB" not in result
        assert "TD_SYSFNLIB" not in result
        assert "MyDB" in result


# ---------------------------------------------------------------
# _discover_files
# ---------------------------------------------------------------


class TestDiscoverFiles:
    """Tests for DDL file discovery."""

    def test_discovers_sql_extensions(self, tmp_path):
        """Files with standard SQL extensions are discovered."""
        for ext in [
            ".tbl",
            ".viw",
            ".spl",
            ".mcr",
            ".fnc",
            ".trg",
            ".jix",
            ".db",
            ".dcl",
        ]:
            (tmp_path / f"test{ext}").write_text("DDL", encoding="utf-8")

        files = _discover_files(str(tmp_path))
        assert len(files) == 9

    def test_skips_hidden_files(self, tmp_path):
        """Files starting with '.' are skipped."""
        (tmp_path / ".hidden.tbl").write_text("DDL", encoding="utf-8")
        (tmp_path / "visible.tbl").write_text("DDL", encoding="utf-8")

        files = _discover_files(str(tmp_path))
        assert len(files) == 1

    def test_skips_underscore_files(self, tmp_path):
        """Files starting with '_' are skipped."""
        (tmp_path / "_waves.txt").write_text("waves", encoding="utf-8")
        (tmp_path / "valid.tbl").write_text("DDL", encoding="utf-8")

        files = _discover_files(str(tmp_path))
        assert len(files) == 1

    def test_results_sorted(self, tmp_path):
        """Discovered files are sorted alphabetically."""
        for name in ["c.tbl", "a.tbl", "b.tbl"]:
            (tmp_path / name).write_text("DDL", encoding="utf-8")

        files = _discover_files(str(tmp_path))
        basenames = [os.path.basename(f) for f in files]
        assert basenames == ["a.tbl", "b.tbl", "c.tbl"]


# ---------------------------------------------------------------
# ingest_directory (integration)
# ---------------------------------------------------------------


class TestIngestDirectory:
    """Integration tests for the full ingest pipeline."""

    def test_ingest_basic_table(self, tmp_path, tmp_project, ddl_create_table):
        """A single table file is ingested, classified, and placed."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "customer.tbl").write_text(ddl_create_table, encoding="utf-8")

        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
        )

        assert result.classified == 1
        assert result.unclassified == 0
        assert len(result.files_placed) == 1
        assert result.files_placed[0][2] == "TABLE"

    def test_ingest_unclassifiable_file(self, tmp_path, tmp_project):
        """Unclassifiable files are reported with a warning."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "random.sql").write_text("SELECT 1 AS dummy;", encoding="utf-8")

        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
        )

        assert result.classified == 0
        assert result.unclassified == 1
        assert len(result.unclassified_files) == 1

    def test_ingest_multiset_injection(
        self, tmp_path, tmp_project, ddl_create_table_no_multiset
    ):
        """Tables without SET/MULTISET get MULTISET injected during ingest."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "orders.tbl").write_text(ddl_create_table_no_multiset, encoding="utf-8")

        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
        )

        assert result.multiset_injected == 1

    def test_ingest_missing_source_raises(self, tmp_project):
        """Missing source directory raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ingest_directory("/nonexistent/path", str(tmp_project))

    def test_ingest_missing_project_raises(self, tmp_path):
        """Missing project directory raises FileNotFoundError."""
        src = tmp_path / "source"
        src.mkdir()
        with pytest.raises(FileNotFoundError):
            ingest_directory(str(src), "/nonexistent/project")
