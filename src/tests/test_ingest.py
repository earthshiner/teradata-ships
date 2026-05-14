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

    def test_grant_create_table_on_database_uses_on_target_not_privilege(self):
        """GRANT CREATE TABLE ON db is DCL, not CREATE TABLE ON."""
        db, obj = _extract_qualified_name(
            "GRANT CREATE TABLE ON GDEV1T_UTLFW TO GDEV1P_UT;"
        )
        assert db is None
        assert obj == "GDEV1T_UTLFW"

    def test_grant_on_procedure_skips_object_kind_token(self):
        """ON PROCEDURE db.object is named from db.object, not Procedure."""
        db, obj = _extract_qualified_name(
            "GRANT EXECUTE PROCEDURE, ALTER EXTERNAL PROCEDURE, "
            'DROP PROCEDURE ON Procedure "SQLJ"."INSTALL_JAR" TO dbc;'
        )
        assert db == "SQLJ"
        assert obj == "INSTALL_JAR"

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

    # -- COMMENT ON COLUMN aggregates with the parent's other comments --
    # Teradata stores comments for view columns and for macro /
    # procedure / function parameters in DBC.TVM (exposed via
    # DBC.ColumnsV). The eponymous .cmt file groups every comment
    # for the same parent so view column comments aggregate with
    # the COMMENT ON VIEW, procedure parameter comments aggregate
    # with the COMMENT ON PROCEDURE, and so on. Encouraged for AI-
    # native data products: rich descriptions help both autonomous
    # agents and human readers.

    def test_comment_on_column_of_view_aggregates_with_view(self):
        """A COMMENT ON COLUMN on a view's column extracts the view's
        (db, view_name) so the .cmt file aggregates with the
        COMMENT ON VIEW and any sibling column comments."""
        db, obj = _extract_qualified_name(
            "COMMENT ON COLUMN MyDB.v_Active.customer_id IS 'PII identifier';"
        )
        assert db == "MyDB"
        assert obj == "v_Active"

    def test_comment_on_macro_parameter_aggregates_with_macro(self):
        """COMMENT ON COLUMN <db>.<macro>.<arg> extracts (db, macro)
        so parameter comments aggregate with COMMENT ON MACRO into
        the same eponymous .cmt file."""
        db, obj = _extract_qualified_name(
            "COMMENT ON COLUMN MyDB.mc_Report.start_date IS 'period start';"
        )
        assert db == "MyDB"
        assert obj == "mc_Report"

    def test_comment_on_procedure_parameter_aggregates_with_procedure(self):
        """COMMENT ON COLUMN <db>.<proc>.<arg> aggregates parameter
        comments with the procedure's own COMMENT ON statement."""
        db, obj = _extract_qualified_name(
            "COMMENT ON COLUMN MyDB.sp_LoadLoans.batch_size IS 'rows per batch';"
        )
        assert db == "MyDB"
        assert obj == "sp_LoadLoans"

    def test_comment_on_function_parameter_aggregates_with_function(self):
        """COMMENT ON COLUMN <db>.<fn>.<arg> aggregates parameter
        comments with the function's own COMMENT ON statement."""
        db, obj = _extract_qualified_name(
            "COMMENT ON COLUMN MyDB.fn_Calc.input_x IS 'denominator';"
        )
        assert db == "MyDB"
        assert obj == "fn_Calc"

    # -- String-literal interference (was: caused ``and.dml`` filenames) --

    def test_dml_ignores_comment_on_inside_string_literal(self):
        """An IS-string saying 'COMMENT ON TABLE and ...' must not be
        mistaken for a real COMMENT ON statement. The DML target
        (Memory.Change_Log) should be picked up instead. Pre-fix this
        produced ``(None, 'and')`` and a file called ``and.dml``."""
        chunk = (
            "INSERT INTO MortgagePlatform_Memory.Change_Log\n"
            "(change_id, change_description) VALUES\n"
            "('CL-001',\n"
            " 'Created complete Domain schema. All COMMENT ON TABLE "
            "and COMMENT ON COLUMN applied.');"
        )
        db, obj = _extract_qualified_name(chunk)
        assert db == "MortgagePlatform_Memory"
        assert obj == "Change_Log"

    def test_ddl_ignores_create_inside_string_literal(self):
        """A CREATE TABLE name should win even when an earlier IS-text
        contains keyword-like phrases such as 'CREATE schema' (which
        the regex would otherwise misread as a hint at the target)."""
        chunk = (
            "COMMENT ON TABLE MyDB.RealTable IS\n"
            "'Tracks how each CREATE TABLE Other.NotReal got applied';"
        )
        db, obj = _extract_qualified_name(chunk)
        assert db == "MyDB"
        assert obj == "RealTable"

    # -- GRANT / REVOKE eponymous extraction --

    def test_grant_on_database(self):
        """GRANT on a bare database name extracts the database name."""
        ddl = (
            "GRANT SELECT ON {{MortgagePlatform_Observability}} "
            "TO PUBLIC WITH GRANT OPTION;"
        )
        db, obj = _extract_qualified_name(ddl)
        # 1-part target — db None, obj is the database name itself
        assert db is None
        assert obj == "{{MortgagePlatform_Observability}}"

    def test_grant_on_table(self):
        """GRANT on db.table extracts both parts."""
        ddl = "GRANT SELECT ON MyDB.Customer TO read_role;"
        db, obj = _extract_qualified_name(ddl)
        assert db == "MyDB"
        assert obj == "Customer"

    def test_revoke_on_database(self):
        """REVOKE follows the same shape as GRANT."""
        ddl = "REVOKE ALL PRIVILEGES ON MyDB FROM old_role;"
        db, obj = _extract_qualified_name(ddl)
        assert db is None
        assert obj == "MyDB"

    def test_grant_with_complex_privilege_list(self):
        """The privilege list between GRANT and ON is consumed
        regardless of how many privileges are listed."""
        ddl = (
            "GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE FUNCTION "
            "ON MyDB.Foo TO role_admin;"
        )
        db, obj = _extract_qualified_name(ddl)
        assert db == "MyDB"
        assert obj == "Foo"


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

    # -----------------------------------------------------------
    # Pre-harvest payload clean (default behaviour)
    # -----------------------------------------------------------

    def test_ingest_default_cleans_orphaned_payload_files(
        self, tmp_path, tmp_project, ddl_create_table
    ):
        """Default re-harvest wipes harvest-owned files from a prior
        run before scanning source. Files whose source counterpart
        is gone do not survive into the new payload."""
        src = tmp_path / "source"
        src.mkdir()

        # First harvest places customer.tbl in the payload.
        (src / "customer.tbl").write_text(ddl_create_table, encoding="utf-8")
        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
        placed = list(tables_dir.glob("*.tbl"))
        assert len(placed) == 1, "first harvest should place one .tbl"
        orphan_path = placed[0]
        assert orphan_path.exists()

        # Source changes — the file driving customer.tbl is removed.
        orphan_path_name = orphan_path.name
        for f in src.iterdir():
            f.unlink()
        # New source produces a different table.
        (src / "order.tbl").write_text(
            ddl_create_table.replace("customer", "order").replace("Customer", "Order"),
            encoding="utf-8",
        )

        # Re-harvest with default clean_payload=True.
        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # Orphaned file from first run is gone, new file present.
        assert result.cleaned >= 1
        assert not (tables_dir / orphan_path_name).exists(), (
            "orphaned payload file should have been cleaned before re-harvest"
        )
        new_files = list(tables_dir.glob("*.tbl"))
        assert len(new_files) == 1
        assert new_files[0].name != orphan_path_name

    def test_ingest_keep_existing_preserves_orphans(
        self, tmp_path, tmp_project, ddl_create_table
    ):
        """``clean_payload=False`` (CLI: --keep-existing) is the
        legacy overlay behaviour: existing payload files survive a
        re-harvest even if they no longer have a source counterpart.
        Collision behaviour is governed by ``force``."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "customer.tbl").write_text(ddl_create_table, encoding="utf-8")
        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
        orphan_name = list(tables_dir.glob("*.tbl"))[0].name

        # Replace source so the old artefact would otherwise be orphaned.
        for f in src.iterdir():
            f.unlink()
        (src / "order.tbl").write_text(
            ddl_create_table.replace("customer", "order").replace("Customer", "Order"),
            encoding="utf-8",
        )

        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            clean_payload=False,
        )

        assert result.cleaned == 0
        assert (tables_dir / orphan_name).exists(), (
            "with clean_payload=False, orphaned files must survive"
        )

    # -----------------------------------------------------------
    # Output quality fixes (issues observed against
    # mortgage-ai-data-product-demo)
    # -----------------------------------------------------------

    def test_ingest_strips_leading_source_structure_comments(
        self, tmp_path, tmp_project
    ):
        """Source files commonly start with file/section banner
        comments ("D. MEMORY - CHANGE LOG") that reference a layout
        that no longer exists in the package. They must be stripped
        from each placed chunk so the deployed SQL is not littered
        with stale headers. Inline / trailing comments are preserved."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "documentation.sql").write_text(
            "-- =================================================\n"
            "-- D. MEMORY  -  CHANGE LOG\n"
            "-- =================================================\n"
            "\n"
            "INSERT INTO MyDB.Change_Log (id) VALUES (1);  -- inline kept\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        placed = tmp_project / "payload" / "database" / "DML" / "MyDB.Change_Log.dml"
        assert placed.exists()
        body = placed.read_text(encoding="utf-8")

        # Header banner gone
        assert "D. MEMORY" not in body
        assert "============" not in body
        # Real statement preserved
        assert "INSERT INTO MyDB.Change_Log" in body
        # Inline trailing comment preserved
        assert "-- inline kept" in body

    def test_ingest_grant_lands_in_eponymous_dcl(self, tmp_path, tmp_project):
        """GRANT statement should produce <db>.dcl (database-level)
        instead of falling back to the source filename. Pre-fix the
        same content would have produced ``01_observability_ddl.dcl``."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "01_observability_ddl.sql").write_text(
            "GRANT SELECT ON MortgagePlatform_Observability "
            "TO PUBLIC WITH GRANT OPTION;\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        dcl_dir = tmp_project / "payload" / "database" / "DCL" / "inter_db"
        assert (dcl_dir / "MortgagePlatform_Observability.dcl").exists(), (
            "GRANT must land in an eponymous .dcl file, not under the source filename"
        )
        assert not (dcl_dir / "01_observability_ddl.dcl").exists()

    def test_ingest_aggregates_view_and_column_comments_into_one_cmt(
        self, tmp_path, tmp_project
    ):
        """COMMENT ON VIEW plus COMMENT ON COLUMN entries for the same
        view all aggregate into a single ``<db>.<view>.cmt`` file —
        Teradata stores view column comments in DBC.TVM the same way
        it stores table column comments. Useful documentation for
        autonomous agents and human readers; harvest must preserve it."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "doc.sql").write_text(
            "COMMENT ON VIEW MyDB.v_Active IS 'Active customer rows';\n"
            "COMMENT ON COLUMN MyDB.v_Active.customer_id IS 'PII id';\n"
            "COMMENT ON COLUMN MyDB.v_Active.region IS 'AU state code';\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        cmt = (
            tmp_project
            / "payload"
            / "database"
            / "DDL"
            / "comments"
            / "MyDB.v_Active.cmt"
        )
        assert cmt.exists()
        body = cmt.read_text(encoding="utf-8")
        assert body.count("COMMENT ON VIEW") == 1
        assert body.count("COMMENT ON COLUMN") == 2
        assert "Active customer rows" in body
        assert "PII id" in body
        assert "AU state code" in body

    def test_ingest_aggregates_procedure_parameter_comments_with_procedure(
        self, tmp_path, tmp_project
    ):
        """Parameter comments on macros / procedures / functions —
        ``COMMENT ON COLUMN <db>.<routine>.<arg>`` — aggregate with
        the routine's own COMMENT ON into one eponymous .cmt file.
        The mortgage AI-native data product standard recommends this
        for richer parameter documentation visible to agents."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "doc.sql").write_text(
            "COMMENT ON PROCEDURE MyDB.sp_LoadLoans "
            "IS 'Loads a batch of loans';\n"
            "COMMENT ON COLUMN MyDB.sp_LoadLoans.batch_size "
            "IS 'rows per batch';\n"
            "COMMENT ON COLUMN MyDB.sp_LoadLoans.dry_run "
            "IS '1 = simulate only';\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        cmt = (
            tmp_project
            / "payload"
            / "database"
            / "DDL"
            / "comments"
            / "MyDB.sp_LoadLoans.cmt"
        )
        assert cmt.exists()
        body = cmt.read_text(encoding="utf-8")
        assert body.count("COMMENT ON PROCEDURE") == 1
        assert body.count("COMMENT ON COLUMN") == 2
        assert "rows per batch" in body
        assert "1 = simulate only" in body

    def test_ingest_dml_with_comment_on_inside_string_lands_eponymously(
        self, tmp_path, tmp_project
    ):
        """End-to-end regression: an INSERT with an IS-string mentioning
        'COMMENT ON TABLE and ...' must land in the DML eponymous file
        (MyDB.Change_Log.dml), not in ``and.dml``."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "documentation.sql").write_text(
            "INSERT INTO MyDB.Change_Log (id, descr) VALUES\n"
            "(1, 'All COMMENT ON TABLE and COMMENT ON COLUMN applied.');\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        dml_dir = tmp_project / "payload" / "database" / "DML"
        assert (dml_dir / "MyDB.Change_Log.dml").exists()
        assert not (dml_dir / "and.dml").exists(), (
            "string-literal text must not produce a nonsense filename"
        )

    # -----------------------------------------------------------
    # Multi-target DML keep-together policy (issue #68)
    # -----------------------------------------------------------

    def test_ingest_multi_target_dml_kept_together_as_multi_table(
        self, tmp_path, tmp_project
    ):
        """A source file with INSERTs into multiple distinct tables
        is placed as one ``<source_basename>.multi_table.dml`` file —
        statement order preserved, no per-statement splitting. This
        protects FK ordering / sequenced operations the source author
        encoded by listing statements in a particular order."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "load_domain.sql").write_text(
            "INSERT INTO MyDB.Customer_H (id) VALUES (1);\n"
            "INSERT INTO MyDB.Loan_H (id, customer_id) VALUES (1, 1);\n"
            "INSERT INTO MyDB.Payment_H (id, loan_id) VALUES (1, 1);\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        dml_dir = tmp_project / "payload" / "database" / "DML"
        assert (dml_dir / "load_domain.multi_table.dml").exists()
        # No per-target eponymous files should have been emitted.
        assert not (dml_dir / "MyDB.Customer_H.dml").exists()
        assert not (dml_dir / "MyDB.Loan_H.dml").exists()
        assert not (dml_dir / "MyDB.Payment_H.dml").exists()

        # Manifest carries the target list and target_count.
        rel_dest = next(iter(result.multi_table_targets))
        assert "load_domain.multi_table.dml" in rel_dest
        assert sorted(result.multi_table_targets[rel_dest]) == [
            "MyDB.Customer_H",
            "MyDB.Loan_H",
            "MyDB.Payment_H",
        ]

        # Order preserved: Customer_H must appear before Loan_H,
        # Loan_H before Payment_H.
        body = (dml_dir / "load_domain.multi_table.dml").read_text(encoding="utf-8")
        cust = body.index("Customer_H")
        loan = body.index("Loan_H")
        pay = body.index("Payment_H")
        assert cust < loan < pay

    def test_ingest_same_target_dml_aggregates_eponymously(self, tmp_path, tmp_project):
        """When every chunk in a multi-statement DML file targets the
        same table, it aggregates eponymously (regression — current
        behaviour). The multi-table rule fires only on >1 distinct
        target."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "ref_currency.sql").write_text(
            "INSERT INTO MyDB.Currency (cd, nm) VALUES ('AUD', 'AUS Dollar');\n"
            "INSERT INTO MyDB.Currency (cd, nm) VALUES ('USD', 'US Dollar');\n"
            "INSERT INTO MyDB.Currency (cd, nm) VALUES ('GBP', 'UK Pound');\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        dml_dir = tmp_project / "payload" / "database" / "DML"
        assert (dml_dir / "MyDB.Currency.dml").exists()
        assert not (dml_dir / "ref_currency.multi_table.dml").exists()
        assert not result.multi_table_targets

        body = (dml_dir / "MyDB.Currency.dml").read_text(encoding="utf-8")
        assert body.count("INSERT INTO") == 3

    def test_ingest_multi_table_dml_marker_forces_keep_together(
        self, tmp_path, tmp_project
    ):
        """A ``-- MULTI_TABLE_DML`` header marker forces keep-together
        treatment even when every chunk targets the same table.
        Useful when the source author wants to preserve a particular
        statement order on a single-target file (e.g. INSERT, UPDATE,
        DELETE applied as a sequence)."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "audit_seq.sql").write_text(
            "-- MULTI_TABLE_DML\n"
            "INSERT INTO MyDB.Audit (id, state) VALUES (1, 'open');\n"
            "UPDATE MyDB.Audit SET state = 'pending' WHERE id = 1;\n"
            "DELETE FROM MyDB.Audit WHERE id = 1 AND state = 'pending';\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        dml_dir = tmp_project / "payload" / "database" / "DML"
        assert (dml_dir / "audit_seq.multi_table.dml").exists()
        # Must NOT have aggregated eponymously despite single target.
        assert not (dml_dir / "MyDB.Audit.dml").exists()

    def test_ingest_mixed_ddl_dml_splits_per_statement(
        self, tmp_path, tmp_project, ddl_create_table
    ):
        """A source file with both DDL and DML chunks does not get
        the multi-table treatment — each chunk is placed eponymously
        as today. The keep-together rule applies only when every
        classified chunk is DML."""
        src = tmp_path / "source"
        src.mkdir()
        (src / "mixed.sql").write_text(
            ddl_create_table + "\nINSERT INTO MyDB.AnotherTable (id) VALUES (1);\n",
            encoding="utf-8",
        )

        ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        # DDL chunk → eponymous .tbl
        tables_dir = tmp_project / "payload" / "database" / "DDL" / "tables"
        assert any(p.suffix == ".tbl" for p in tables_dir.iterdir())
        # DML chunk → eponymous .dml (different target, but only one
        # DML chunk so it's unambiguous single-target)
        dml_dir = tmp_project / "payload" / "database" / "DML"
        assert (dml_dir / "MyDB.AnotherTable.dml").exists()
        # No multi_table file expected.
        assert not any("multi_table" in p.name for p in dml_dir.iterdir())

    def test_ingest_clean_preserves_gitkeep_and_control_files(
        self, tmp_path, tmp_project, ddl_create_table
    ):
        """The pre-harvest clean preserves .gitkeep markers (so empty
        directories stay tracked) and control files starting with
        ``_`` such as a user-curated _order.txt.

        Pre-seeded directory receives no new placement during this
        harvest, so .gitkeep is preserved by the clean alone — the
        existing harvest logic strips .gitkeep from a directory only
        when a real file is placed in it, which is correct behaviour
        and orthogonal to the pre-clean."""
        prereq_db_dir = (
            tmp_project / "payload" / "database" / "pre-requisites" / "databases"
        )
        prereq_db_dir.mkdir(parents=True, exist_ok=True)
        (prereq_db_dir / ".gitkeep").write_text("", encoding="utf-8")
        (prereq_db_dir / "_order.txt").write_text("a.db\nb.db\n", encoding="utf-8")
        (prereq_db_dir / "stale.db").write_text(
            "CREATE DATABASE stale;", encoding="utf-8"
        )

        # Source contains only a table — nothing destined for
        # pre-requisites/databases/ — so the clean alone determines
        # what survives in that directory.
        src = tmp_path / "source"
        src.mkdir()
        (src / "customer.tbl").write_text(ddl_create_table, encoding="utf-8")

        result = ingest_directory(str(src), str(tmp_project), detect_tokens=False)

        assert (prereq_db_dir / ".gitkeep").exists(), "gitkeep must survive"
        assert (prereq_db_dir / "_order.txt").exists(), (
            "control file _order.txt must survive"
        )
        assert not (prereq_db_dir / "stale.db").exists(), (
            "stale harvest-owned file must be cleaned"
        )
        assert result.cleaned >= 1
        assert (prereq_db_dir / "_order.txt").read_text(
            encoding="utf-8"
        ) == "a.db\nb.db\n"


# ---------------------------------------------------------------
# Kind-aware token substitution
# ---------------------------------------------------------------


class TestKindAwareTokenSubstitution:
    """Integration tests for kind-aware {{TOKEN_T}} / {{TOKEN_V}} emission.

    Each test writes source files into a temp directory, runs
    ``ingest_directory`` with an ``apply_tokens`` map, then reads the
    harvested payload to verify that kind-specific tokens were emitted
    correctly.
    """

    def test_owner_clause_uses_file_kind_T(self, tmp_path, tmp_project):
        """A .tbl file's CREATE TABLE owner reference becomes {{TOKEN_T}}."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "LegacyDB.my_table.tbl").write_text(
            "CREATE MULTISET TABLE LegacyDB.my_table (id INTEGER);",
            encoding="utf-8",
        )
        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={"LegacyDB": "{{LegacyDB}}"},
        )
        assert result.classified == 1
        harvested = list((tmp_project / "payload").rglob("*.tbl"))
        assert harvested, "no .tbl file placed"
        content = harvested[0].read_text(encoding="utf-8")
        assert "{{LegacyDB_T}}" in content, f"Expected {{{{LegacyDB_T}}}} in: {content}"
        assert "LegacyDB" not in content.replace("{{LegacyDB_T}}", ""), (
            "raw literal must be fully replaced"
        )

    def test_owner_clause_uses_file_kind_V(self, tmp_path, tmp_project):
        """A .viw file's REPLACE VIEW owner reference becomes {{TOKEN_V}}."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "LegacyDB.v_loans.viw").write_text(
            "REPLACE VIEW LegacyDB.v_loans AS SELECT 1 AS x;",
            encoding="utf-8",
        )
        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={"LegacyDB": "{{LegacyDB}}"},
        )
        assert result.classified == 1
        harvested = list((tmp_project / "payload").rglob("*.viw"))
        assert harvested, "no .viw file placed"
        content = harvested[0].read_text(encoding="utf-8")
        assert "{{LegacyDB_V}}" in content, f"Expected {{{{LegacyDB_V}}}} in: {content}"

    def test_cross_reference_resolved_from_kind_index(self, tmp_path, tmp_project):
        """A view body FROM clause referencing a table gets {{TOKEN_T}},
        even though the view file itself is kind V.

        Layer B cross-reference resolution: the kind index maps
        LegacyDB.my_table → T (from the .tbl source), so the FROM
        clause in the view gets {{LegacyDB_T}} while the view's own
        owner clause gets {{LegacyDB_V}}.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "LegacyDB.my_table.tbl").write_text(
            "CREATE MULTISET TABLE LegacyDB.my_table (id INTEGER);",
            encoding="utf-8",
        )
        (src / "LegacyDB.v_loans.viw").write_text(
            "REPLACE VIEW LegacyDB.v_loans AS\nSELECT id FROM LegacyDB.my_table;",
            encoding="utf-8",
        )
        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={"LegacyDB": "{{LegacyDB}}"},
        )
        assert result.classified == 2

        # The view file should have _V for its own owner and _T for the cross-ref
        views = list((tmp_project / "payload").rglob("*.viw"))
        assert views, "no .viw file placed"
        view_content = views[0].read_text(encoding="utf-8")

        assert "{{LegacyDB_V}}.v_loans" in view_content, (
            f"View owner should be {{{{LegacyDB_V}}}}: {view_content}"
        )
        assert "{{LegacyDB_T}}.my_table" in view_content, (
            f"Cross-ref to table should be {{{{LegacyDB_T}}}}: {view_content}"
        )

    def test_external_reference_defaults_to_V(self, tmp_path, tmp_project):
        """A qualified reference to an object NOT in the package defaults to _V.

        Per the design: downstream consumers in a SHIPS topology query
        the view layer, so unresolvable external references default to _V.
        """
        src = tmp_path / "src"
        src.mkdir()
        # Only define the view — ExternalDB is external (not in package)
        (src / "LegacyDB.v_joined.viw").write_text(
            "REPLACE VIEW LegacyDB.v_joined AS\n"
            "SELECT a.id FROM ExternalDB.some_view a;",
            encoding="utf-8",
        )
        result = ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={
                "LegacyDB": "{{LegacyDB}}",
                "ExternalDB": "{{ExternalDB}}",
            },
        )
        assert result.classified == 1
        views = list((tmp_project / "payload").rglob("*.viw"))
        content = views[0].read_text(encoding="utf-8")

        # External ref: not in kind_index → defaults to EXTERNAL_KIND_DEFAULT = V
        assert "{{ExternalDB_V}}.some_view" in content, (
            f"External ref should default to _V: {content}"
        )

    def test_already_kind_suffixed_literal_not_double_suffixed(
        self, tmp_path, tmp_project
    ):
        """A literal DB name that already ends with _V is applied as-is.

        Backward compatibility: token maps written before kind-aware
        tokenisation used kind-suffixed DB names directly (e.g.
        MortgagePlatform_Domain_V). These must not be double-suffixed
        to {{TOKEN_V_V}}.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "Dom_V.v_loans.viw").write_text(
            "REPLACE VIEW Dom_V.v_loans AS SELECT 1 AS x;",
            encoding="utf-8",
        )
        ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={"Dom_V": "{{Dom_V}}"},
        )
        harvested = list((tmp_project / "payload").rglob("*.viw"))
        content = harvested[0].read_text(encoding="utf-8")

        # Should be plain {{Dom_V}} — no additional suffix
        assert "{{Dom_V}}" in content, f"Expected plain {{{{Dom_V}}}} in: {content}"
        assert "{{Dom_V_V}}" not in content, "double suffix must not appear"

    def test_already_kind_encoded_token_not_double_suffixed(
        self, tmp_path, tmp_project
    ):
        """A token whose name already ends with a kind suffix is applied as-is.

        When the operator writes ``Dom={{DOM_DATABASE_T}}`` in token_map.conf,
        the base token is ``DOM_DATABASE_T``. Appending another suffix would
        produce ``{{DOM_DATABASE_T_V}}`` — wrong. The guard on the base token
        name prevents this.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "Dom.v_loans.viw").write_text(
            "REPLACE VIEW Dom.v_loans AS SELECT 1 AS x;",
            encoding="utf-8",
        )
        ingest_directory(
            str(src),
            str(tmp_project),
            detect_tokens=False,
            apply_tokens={"Dom": "{{DOM_DATABASE_T}}"},
        )
        harvested = list((tmp_project / "payload").rglob("*.viw"))
        content = harvested[0].read_text(encoding="utf-8")

        # Token already encodes kind — apply as-is
        assert "{{DOM_DATABASE_T}}" in content, (
            f"Pre-encoded token should be preserved: {content}"
        )
        assert "{{DOM_DATABASE_T_V}}" not in content, "double suffix must not appear"
