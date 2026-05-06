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

    # -- COMMENT ON variants — every comment-able object kind --

    def test_comment_on_view(self):
        """COMMENT ON VIEW <db>.<view> extracts (db, view)."""
        db, obj = _extract_qualified_name(
            "COMMENT ON VIEW MyDB.v_Active IS 'Active rows';"
        )
        assert db == "MyDB"
        assert obj == "v_Active"

    def test_comment_on_macro(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON MACRO MyDB.mc_Report IS 'Daily report';"
        )
        assert db == "MyDB"
        assert obj == "mc_Report"

    def test_comment_on_procedure(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON PROCEDURE MyDB.sp_DoStuff IS 'Background job';"
        )
        assert db == "MyDB"
        assert obj == "sp_DoStuff"

    def test_comment_on_function(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON FUNCTION MyDB.fn_Calc IS 'Risk calculation';"
        )
        assert db == "MyDB"
        assert obj == "fn_Calc"

    def test_comment_on_trigger(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON TRIGGER MyDB.trg_Audit IS 'Audit trail';"
        )
        assert db == "MyDB"
        assert obj == "trg_Audit"

    def test_comment_on_column_drops_column_segment(self):
        """COMMENT ON COLUMN db.table.col → (db, table) so all
        comments on the same table aggregate into one .cmt file."""
        db, obj = _extract_qualified_name(
            "COMMENT ON COLUMN MyDB.Customer.surname IS 'Last name';"
        )
        assert db == "MyDB"
        assert obj == "Customer"

    def test_comment_on_database_returns_unqualified_name(self):
        """System-scope COMMENT ON DATABASE: no DB qualifier — name only."""
        db, obj = _extract_qualified_name(
            "COMMENT ON DATABASE Production IS 'Live env';"
        )
        assert db is None
        assert obj == "Production"

    def test_comment_on_user(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON USER svc_etl IS 'ETL service account';"
        )
        assert db is None
        assert obj == "svc_etl"

    def test_comment_on_role(self):
        db, obj = _extract_qualified_name(
            "COMMENT ON ROLE read_only IS 'Read-only role';"
        )
        assert db is None
        assert obj == "read_only"

    def test_comment_on_view_with_token_qualifier(self):
        """Token-qualified targets (post-harvest) still extract correctly."""
        db, obj = _extract_qualified_name(
            "COMMENT ON VIEW {{MEM_DATABASE}}.v_Active IS 'Active rows';"
        )
        assert db == "{{MEM_DATABASE}}"
        assert obj == "v_Active"


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

    def test_create_table_inside_block_comment_is_ignored(self):
        """Regression: a /* */ header comment containing 'CREATE
        TABLE' must not be mistaken for the real DDL. MULTISET
        should be injected into the actual CREATE statement, not
        into the comment, and the comment's text must survive
        the rewrite untouched."""
        ddl = (
            "/* This procedure parents the inner CREATE TABLE\n"
            "   for the staging area. */\n"
            "CREATE TABLE MyDB.Stage (Id INT);\n"
        )
        result, injected = _inject_multiset(ddl)
        assert injected is True
        # Real CREATE TABLE got the MULTISET
        assert "CREATE MULTISET TABLE MyDB.Stage" in result
        # Comment text is intact — not modified
        assert "parents the inner CREATE TABLE" in result
        # No spurious MULTISET in the comment
        assert (
            "CREATE MULTISET TABLE\n"
            not in result.split("CREATE MULTISET TABLE MyDB.Stage")[0]
        )

    def test_create_table_inside_line_comment_is_ignored(self):
        """Same regression but with -- line comment instead of /* */."""
        ddl = (
            "-- TODO: remove the old CREATE TABLE pattern below\n"
            "CREATE TABLE MyDB.Stage (Id INT);\n"
        )
        result, injected = _inject_multiset(ddl)
        assert injected is True
        assert "CREATE MULTISET TABLE MyDB.Stage" in result
        # Comment unchanged
        assert "-- TODO: remove the old CREATE TABLE pattern below" in result

    def test_set_inside_comment_does_not_block_injection(self):
        """A comment that happens to contain 'SET' or 'MULTISET'
        must not cause the detector to think the file already
        has the modifier — it would skip injection erroneously."""
        ddl = (
            "/* MULTISET tables are required by team policy. */\n"
            "CREATE TABLE MyDB.Stage (Id INT);\n"
        )
        result, injected = _inject_multiset(ddl)
        assert injected is True
        assert "CREATE MULTISET TABLE MyDB.Stage" in result

    def test_create_table_inside_string_literal_is_ignored(self):
        """A CHECK constraint or other string literal containing
        the words 'CREATE TABLE' must not be treated as the real
        DDL — MULTISET should be injected into the actual statement."""
        ddl = (
            "CREATE TABLE MyDB.Stage (\n"
            "    Id INT,\n"
            "    Doc VARCHAR(100) "
            "CHECK (Doc NOT LIKE '%CREATE TABLE%')\n"
            ");\n"
        )
        result, injected = _inject_multiset(ddl)
        assert injected is True
        # Real CREATE TABLE got the MULTISET
        assert "CREATE MULTISET TABLE MyDB.Stage" in result
        # The string literal is unchanged — its CREATE TABLE survives
        # verbatim so the constraint still works
        assert "%CREATE TABLE%" in result


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

    def test_create_view_inside_comment_is_ignored(self):
        """Regression: a comment mentioning 'CREATE VIEW' must
        not be rewritten to 'REPLACE VIEW'. The real DDL gets
        the rewrite; the comment stays intact."""
        ddl = (
            "/* Replaces the older CREATE VIEW pattern. */\n"
            "CREATE VIEW MyDB.V AS SELECT 1;\n"
        )
        result, injected = _inject_replace_view(ddl)
        assert injected is True
        # Real CREATE VIEW rewritten
        assert "REPLACE VIEW MyDB.V" in result
        # Comment preserved verbatim
        assert "Replaces the older CREATE VIEW pattern." in result


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

    def test_ingest_harvests_jar_binary_alongside_install_script(
        self, tmp_path, tmp_project
    ):
        """End-to-end: source layout mirroring the user's GCFR case.
        Install script lives in scripts/, JAR in JAVA/JAR/. After
        harvest both should be in the project payload, and the
        install script's CJ! path should be rewritten to ./X.jar."""
        # Source: <tmp>/raw/scripts/install.ddl + <tmp>/raw/JAVA/JAR/X.jar
        src = tmp_path / "raw"
        scripts_dir = src / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "install.ddl").write_text(
            "DATABASE x;\n"
            "CALL SQLJ.INSTALL_JAR("
            "'CJ!../JAVA/JAR/ExecLargeSqlJ.jar', "
            "'JAR_EXECUTE_LARGE_SQL', 0);",
            encoding="utf-8",
        )
        jar_dir = src / "JAVA" / "JAR"
        jar_dir.mkdir(parents=True)
        (jar_dir / "ExecLargeSqlJ.jar").write_bytes(b"jar-bytes-here")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # SQL placed under DDL/jar_install/ as a .sjr
        assert any("jar_install" in dest for _, dest, _ in result.files_placed)

        # Binary recorded
        assert len(result.binaries_placed) == 1
        bin_src, bin_dest, bin_kind = result.binaries_placed[0]
        assert bin_kind == "JAR_BINARY"
        assert bin_dest.endswith("ExecLargeSqlJ.jar")

        # Binary physically copied into the project
        from pathlib import Path

        assert (Path(tmp_project) / bin_dest).exists()
        assert (Path(tmp_project) / bin_dest).read_bytes() == b"jar-bytes-here"

        # The install script's path was rewritten to ./X.jar form
        sql_dest = next(
            dest for _, dest, _ in result.files_placed if "jar_install" in dest
        )
        sql_text = (Path(tmp_project) / sql_dest).read_text(encoding="utf-8")
        assert "../JAVA/JAR/" not in sql_text
        assert "./ExecLargeSqlJ.jar" in sql_text

    def test_ingest_harvests_c_source_alongside_function(self, tmp_path, tmp_project):
        """End-to-end: a C UDF references .c/.h files in a sibling
        directory. After harvest both should be in DDL/functions/
        with the function's EXTERNAL NAME path rewritten."""
        src = tmp_path / "raw"
        fnc_dir = src / "fncs"
        fnc_dir.mkdir(parents=True)
        (fnc_dir / "foo.fnc").write_text(
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C NO SQL\n"
            "EXTERNAL NAME 'CS!foo!../C/foo.c!CH!foo_h!../C/foo.h';",
            encoding="utf-8",
        )
        c_dir = src / "C"
        c_dir.mkdir(parents=True)
        (c_dir / "foo.c").write_bytes(b"int foo(int x) { return x; }")
        (c_dir / "foo.h").write_bytes(b"int foo(int);")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # Two binaries copied (.c + .h)
        kinds = [k for _, _, k in result.binaries_placed]
        assert "C_SOURCE" in kinds
        assert "C_HEADER" in kinds

        # Both physically present in the function destination dir
        from pathlib import Path

        for bin_src, bin_dest, _ in result.binaries_placed:
            assert (Path(tmp_project) / bin_dest).exists()

        # Function content has rewritten paths
        fnc_dest = next(dest for _, dest, t in result.files_placed if t == "FUNCTION")
        fnc_text = (Path(tmp_project) / fnc_dest).read_text(encoding="utf-8")
        assert "../C/" not in fnc_text
        assert "./foo.c" in fnc_text
        assert "./foo.h" in fnc_text

    def test_ingest_warns_when_binary_reference_is_missing(self, tmp_path, tmp_project):
        """JAR install script that points at a non-existent binary
        should produce a classification warning so the user knows
        the deployer will fail."""
        src = tmp_path / "raw"
        scripts_dir = src / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "install.ddl").write_text(
            "CALL SQLJ.INSTALL_JAR('CJ!../missing/X.jar', 'a', 0);",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # No binary placed — there was nothing to copy
        assert result.binaries_placed == []
        # But a warning fired
        assert any("not found" in w for w in result.classification_warnings)

    def test_ingest_records_subtypes_for_c_udf(self, tmp_path, tmp_project):
        """A C UDF is classified as FUNCTION_C and the sub-type
        plus C source/header references propagate to IngestResult."""
        src = tmp_path / "source"
        src.mkdir()
        ddl = (
            "CREATE FUNCTION x.foo (a INT) RETURNS INT\n"
            "LANGUAGE C\n"
            "NO SQL\n"
            "PARAMETER STYLE SQL\n"
            "EXTERNAL NAME 'CS!foo!../FOO/foo.c!CH!foo_h!../FOO/foo.h';"
        )
        (src / "foo.fnc").write_text(ddl, encoding="utf-8")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        assert result.classified == 1
        # base type is FUNCTION (preserved for backward compat)
        assert result.files_placed[0][2] == "FUNCTION"
        # rich sub-type recorded against the staged path
        staged = result.files_placed[0][1]
        assert result.subtypes.get(staged) == "FUNCTION_C"
        # external refs captured
        refs = result.external_references.get(staged, [])
        assert any(p.endswith("foo.c") for p in refs)
        assert any(p.endswith("foo.h") for p in refs)

    def test_ingest_records_subtype_for_java_procedure(self, tmp_path, tmp_project):
        """A Java procedure is classified as PROCEDURE_JAVA and the
        JAR alias is recorded in external_references."""
        src = tmp_path / "source"
        src.mkdir()
        ddl = (
            "CREATE PROCEDURE x.foo()\n"
            "LANGUAGE JAVA\n"
            "PARAMETER STYLE JAVA\n"
            "EXTERNAL NAME 'jar_execute_large_sql:com.example.Foo.bar';"
        )
        (src / "foo.spl").write_text(ddl, encoding="utf-8")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        assert result.classified == 1
        assert result.files_placed[0][2] == "PROCEDURE"
        staged = result.files_placed[0][1]
        assert result.subtypes.get(staged) == "PROCEDURE_JAVA"
        assert result.external_references.get(staged) == ["jar_execute_large_sql"]

    def test_ingest_surfaces_filename_mismatch_warning(self, tmp_path, tmp_project):
        """A file named .tbl whose content is CREATE VIEW gets
        classified as VIEW (content wins) but a classification
        warning is surfaced for the user."""
        src = tmp_path / "source"
        src.mkdir()
        # .tbl extension says TABLE; content says VIEW
        (src / "looks_like_table.tbl").write_text(
            "CREATE VIEW x.v AS SELECT 1;", encoding="utf-8"
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # Content wins
        assert result.files_placed[0][2] == "VIEW"
        # Warning surfaced
        assert any("Filename mismatch" in w for w in result.classification_warnings)

    def test_ingest_no_classification_warnings_for_clean_files(
        self, tmp_path, tmp_project
    ):
        """A consistent file (matching extension + content) produces
        no classification warnings."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "ok.tbl").write_text("CREATE TABLE x.t (id INT);", encoding="utf-8")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)
        assert result.classification_warnings == []

    def test_ingest_sqlj_install_script_uses_sjr_extension(self, tmp_path, tmp_project):
        """A SQL file containing CALL SQLJ.INSTALL_JAR(...) is classified as
        JAR but the staged file gets ``.sjr`` (SQLJ Runtime install
        script), NOT ``.jar``. The latter is reserved for actual binary
        Java archives.

        Regression test for the GCFR_UT_Install_Jar.ddl case where a
        legacy DDL extension was being remapped to ``.jar``,
        misleadingly suggesting a binary archive.
        """
        src = tmp_path / "source"
        src.mkdir()
        # User's actual file contents (anonymised). Source extension
        # is .ddl (legacy) — SHIPS reclassifies based on content.
        (src / "GCFR_UT_Install_Jar.ddl").write_text(
            "DATABASE $GCFR_P_UT;\n"
            "\n"
            "CALL SQLJ.INSTALL_JAR("
            "'CJ!../JAVA/JAR/ExecLargeSqlJ.jar', "
            "'JAR_EXECUTE_LARGE_SQL', 0);\n"
            "\n"
            "CALL SQLJ.INSTALL_JAR("
            "'CJ!../JAVA/JAR/ExecLargeNOSSqlJ.jar', "
            "'JAR_EXECUTE_LARGE_NOS_SQL', 0);\n",
            encoding="utf-8",
        )

        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
        )

        assert result.classified == 1
        # Classification stays "JAR" — that's the semantic type
        assert result.files_placed[0][2] == "JAR"
        # ...but the staged file extension is .sjr, not .jar
        dest_path = result.files_placed[0][1]
        assert dest_path.endswith(".sjr"), f"Expected .sjr extension; got {dest_path}"
        # And it lives under DDL/jar_install (not DDL/JARs)
        assert "jar_install" in dest_path.replace("\\", "/")

    def test_ingest_splits_multistatement_ddl_and_grant(self, tmp_path, tmp_project):
        """A file with CREATE TABLE followed by GRANT splits into
        two destinations — TABLE under DDL/tables, GRANT under
        DCL/inter_db. Pre-existing splitter behaviour, captured here
        as a regression guard.
        """
        src = tmp_path / "raw"
        src.mkdir()
        (src / "compound.ddl").write_text(
            "CREATE MULTISET TABLE x.t (id INT);\nGRANT SELECT ON x.t TO ROLE READER;",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        types = sorted(t for _, _, t in result.files_placed)
        assert types == ["GRANT", "TABLE"]
        # Each placed in its conventional subdir
        dests = [d for _, d, _ in result.files_placed]
        assert any("DDL" in d and "tables" in d for d in dests)
        assert any("DCL" in d for d in dests)

    def test_ingest_force_reharvest_truncates_aggregating_files(
        self, tmp_path, tmp_project
    ):
        """COMMENT/STATISTICS/DML files aggregate by appending. On a
        fresh harvest run the file gets created fresh; on a SECOND
        run with --force it must TRUNCATE first before re-aggregating
        so old (e.g. untokenised) content from the previous run is
        not retained alongside the new (tokenised) content."""
        src = tmp_path / "raw"
        src.mkdir()
        (src / "schema.ddl").write_text(
            "CREATE MULTISET TABLE x.t (id INT);\n"
            "COMMENT ON TABLE x.t IS 'first version';\n"
            "COMMENT ON COLUMN x.t.id IS 'identifier';\n",
            encoding="utf-8",
        )

        # First harvest — aggregating .cmt file gets two statements.
        ingest_directory(str(src), str(tmp_project), detect_tokens=False)
        cmt = tmp_project / "payload" / "database" / "DDL" / "comments" / "x.t.cmt"
        first = cmt.read_text(encoding="utf-8")
        assert "first version" in first
        assert first.count("COMMENT ON TABLE") == 1

        # Edit source so the first-version comment is replaced.
        (src / "schema.ddl").write_text(
            "CREATE MULTISET TABLE x.t (id INT);\n"
            "COMMENT ON TABLE x.t IS 'second version';\n"
            "COMMENT ON COLUMN x.t.id IS 'identifier';\n",
            encoding="utf-8",
        )

        # Re-harvest with --force — the .cmt file must be truncated
        # at first touch in the new run, then re-aggregated.
        ingest_directory(str(src), str(tmp_project), detect_tokens=False, force=True)
        second = cmt.read_text(encoding="utf-8")

        # Old version must be gone, new version present, and only
        # ONE COMMENT ON TABLE statement (no duplication).
        assert "first version" not in second
        assert "second version" in second
        assert second.count("COMMENT ON TABLE") == 1
        assert second.count("COMMENT ON COLUMN") == 1

    def test_ingest_view_comments_get_eponymous_filenames(self, tmp_path, tmp_project):
        """COMMENT ON VIEW must aggregate per target view (eponymous
        <db>.<view>.cmt), not per source filename. Pre-fix, COMMENT
        ON VIEW fell through to source-filename fallback because the
        name-extraction regex only handled TABLE/COLUMN."""
        src = tmp_path / "raw"
        src.mkdir()
        # Two views in two different databases, plus their comments.
        (src / "views.ddl").write_text(
            "CREATE VIEW dbA.v_one AS SELECT 1 AS x;\n"
            "CREATE VIEW dbB.v_two AS SELECT 2 AS y;\n"
            "COMMENT ON VIEW dbA.v_one IS 'first view';\n"
            "COMMENT ON VIEW dbB.v_two IS 'second view';\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        comments_dir = tmp_project / "payload" / "database" / "DDL" / "comments"
        # Each view's comment lands in its own eponymous file.
        assert (comments_dir / "dbA.v_one.cmt").exists()
        assert (comments_dir / "dbB.v_two.cmt").exists()
        # And NOT in a source-filename-based fallback file.
        assert not (comments_dir / "views.cmt").exists()

    def test_ingest_does_not_split_inside_string_literal_with_semicolon(
        self, tmp_path, tmp_project
    ):
        """A COMMENT (or any DDL) whose string literal contains an
        embedded semicolon must NOT be split mid-string. Without the
        string-aware splitter, the file gets cut at the first ``;``
        inside ``'...'``, leaving an unterminated quote in the first
        chunk and the rest of the comment in the second chunk."""
        src = tmp_path / "raw"
        src.mkdir()
        # The inner ``;`` after "AML/CTF Act" must not break the split.
        (src / "compound.ddl").write_text(
            "CREATE MULTISET TABLE x.t (id INT);\n"
            "COMMENT ON TABLE x.t IS "
            "'Reference: AML risk rating. Regulated under "
            "AML/CTF Act; H rating requires Enhanced Due Diligence.';\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # Two statements split correctly — TABLE plus COMMENT.
        types = sorted(t for _, _, t in result.files_placed)
        assert types == ["COMMENT", "TABLE"]

        # The comment file content must contain the FULL comment text
        # — the string with the embedded semicolon round-trips intact.
        comment_dest = next(d for _, d, t in result.files_placed if t == "COMMENT")
        comment_path = os.path.join(str(tmp_project), comment_dest)
        with open(comment_path, encoding="utf-8") as f:
            content = f.read()
        # The complete string literal — including the inner ; — must
        # appear in the output, terminated by a closing single quote
        # then the statement-end semicolon.
        assert "AML/CTF Act; H rating requires Enhanced Due Diligence.'" in content

    def test_ingest_handles_doubled_quote_escape_in_literal(
        self, tmp_path, tmp_project
    ):
        """Teradata escapes embedded apostrophes by doubling them
        (``'O''Connor'``). The splitter must treat ``''`` as a literal
        quote inside the string, not as a quote-close-then-quote-open.
        A ``;`` after the doubled quote is still inside the string."""
        src = tmp_path / "raw"
        src.mkdir()
        (src / "compound.ddl").write_text(
            "CREATE MULTISET TABLE x.t (id INT);\n"
            "COMMENT ON TABLE x.t IS "
            "'O''Connor; surname with apostrophe; still one string';\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        types = sorted(t for _, _, t in result.files_placed)
        assert types == ["COMMENT", "TABLE"]

    def test_ingest_does_not_split_procedure_with_begin_end(
        self, tmp_path, tmp_project
    ):
        """A CREATE PROCEDURE with a BEGIN...END body must NOT be
        split on its internal semicolons — the splitter detects
        BEGIN and bails. The whole file lands as one PROCEDURE."""
        src = tmp_path / "raw"
        src.mkdir()
        (src / "proc.spl").write_text(
            "CREATE PROCEDURE x.foo (IN p INT)\n"
            "BEGIN\n"
            "  DECLARE local_v INT;\n"
            "  SET local_v = p;\n"
            "  UPDATE x.t SET v = local_v;\n"
            "END;",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        assert result.classified == 1
        assert result.files_placed[0][2] == "PROCEDURE"

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
