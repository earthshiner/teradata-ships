#!/usr/bin/env python3
"""
test_infer_grants.py — Unit tests for infer_grants.py

Tests cover:
    - Comment stripping
    - Alias and correlation name detection
    - View intent analysis (SELECT only)
    - Procedure intent analysis (INSERT, UPDATE, DELETE, MERGE, CALL)
    - Self-reference exclusion
    - Privilege consolidation (multiple files → one grantee)
    - Grant statement consolidation (multiple privileges → one statement)
    - .dcl file content generation
    - False positive rejection (aliases, table names as correlation names)
"""

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# Add src directory to path so td_release_packager package is importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from td_release_packager.infer_grants import (
    strip_sql_comments,
    find_all_db_references,
    analyse_file,
    consolidate_grants,
    generate_grt_content,
    grantee_filename,
    PRIV_SELECT,
    PRIV_INSERT,
    PRIV_UPDATE,
    PRIV_DELETE,
    PRIV_EXEC_PROC,
    PRIV_EXEC,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_temp_file(content: str, suffix: str = ".viw") -> Path:
    """
    Write content to a temporary file and return its Path.

    Args:
        content: The file content to write.
        suffix:  The file extension (default .viw).

    Returns:
        Path to the temporary file.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="test_grant_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return Path(path)


# ---------------------------------------------------------------------------
# Comment stripping tests
# ---------------------------------------------------------------------------


class TestStripComments:
    """Tests for strip_sql_comments()."""

    def test_strips_single_line_comment(self):
        sql = "SELECT 1 -- this is a comment\nFROM t"
        result = strip_sql_comments(sql)
        assert "--" not in result
        assert "SELECT 1" in result
        assert "FROM t" in result

    def test_strips_block_comment(self):
        sql = "SELECT /* block */ 1 FROM t"
        result = strip_sql_comments(sql)
        assert "/*" not in result
        assert "SELECT" in result
        assert "1 FROM t" in result

    def test_strips_multiline_block_comment(self):
        sql = "SELECT 1\n/* this is\na multiline\ncomment */\nFROM t"
        result = strip_sql_comments(sql)
        assert "/*" not in result
        assert "multiline" not in result

    def test_preserves_sql_without_comments(self):
        sql = "SELECT col1, col2 FROM db.table1"
        assert strip_sql_comments(sql) == sql


# ---------------------------------------------------------------------------
# Database reference extraction tests
# ---------------------------------------------------------------------------


class TestFindDbReferences:
    """Tests for find_all_db_references()."""

    def test_finds_tokenised_reference(self):
        sql = "SELECT col FROM {{DOM_DATABASE_T}}.Payment_H"
        refs = find_all_db_references(sql)
        assert "{{DOM_DATABASE_T}}" in refs

    def test_finds_multiple_tokenised_references(self):
        sql = textwrap.dedent("""
            SELECT l.col, p.col
            FROM {{DOM_DATABASE_V}}.Loan_H l
            INNER JOIN {{OBS_DATABASE_V}}.Score dq ON l.id = dq.id
        """)
        refs = find_all_db_references(sql)
        assert "{{DOM_DATABASE_V}}" in refs
        assert "{{OBS_DATABASE_V}}" in refs

    def test_excludes_aliases(self):
        """Table aliases like 'l', 'p', 'dq' must not appear as DB refs."""
        sql = textwrap.dedent("""
            SELECT l.col, p.col, dq.score
            FROM {{DOM_DATABASE_V}}.Loan_H l
            INNER JOIN {{DOM_DATABASE_V}}.Payment_H p ON l.id = p.id
            LEFT OUTER JOIN {{OBS_DATABASE_V}}.Score dq ON l.id = dq.id
        """)
        refs = find_all_db_references(sql)
        # Aliases must not appear
        assert "l" not in refs
        assert "p" not in refs
        assert "dq" not in refs

    def test_excludes_object_names_as_correlation_names(self):
        """
        Unqualified table names used as correlation names
        (e.g. TableName.column) must not be treated as DB refs.
        """
        sql = textwrap.dedent("""
            UPDATE {{OBS_DATABASE_T}}.Data_Quality_Score
            FROM {{DOM_DATABASE_V}}.Loan_H l
            SET quality_score = 100
            WHERE Data_Quality_Score.entity_key = l.loan_key
        """)
        refs = find_all_db_references(sql)
        assert "Data_Quality_Score" not in refs
        assert "{{OBS_DATABASE_T}}" in refs
        assert "{{DOM_DATABASE_V}}" in refs

    def test_excludes_system_databases(self):
        sql = "SELECT col FROM DBC.TablesV"
        refs = find_all_db_references(sql)
        assert "DBC" not in refs

    def test_returns_empty_for_no_references(self):
        sql = "SELECT 1"
        refs = find_all_db_references(sql)
        assert len(refs) == 0


# ---------------------------------------------------------------------------
# View analysis tests
# ---------------------------------------------------------------------------


class TestAnalyseView:
    """Tests for analyse_file() with view DDL."""

    def test_locking_view_implies_select(self):
        """A locking view SELECTing from _T implies SELECT grant on _T."""
        content = textwrap.dedent("""
            CREATE VIEW {{DOM_DATABASE_V}}.Payment_H
            (payment_key, loan_key)
            AS
            LOCKING ROW FOR ACCESS
            SELECT payment_key, loan_key
            FROM {{DOM_DATABASE_T}}.Payment_H;
        """)
        path = write_temp_file(content, ".viw")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "{{DOM_DATABASE_V}}"
            assert result["obj_type"] == "VIEW"
            assert "{{DOM_DATABASE_T}}" in result["grants"]
            assert result["grants"]["{{DOM_DATABASE_T}}"] == {PRIV_SELECT}
        finally:
            os.unlink(path)

    def test_cross_module_view(self):
        """A SEM view referencing DOM and OBS implies SELECT on both."""
        content = textwrap.dedent("""
            CREATE VIEW {{SEM_DATABASE_V}}.Summary
            (loan_key, score)
            AS
            LOCKING ROW FOR ACCESS
            SELECT l.loan_key, dq.score
            FROM {{DOM_DATABASE_V}}.Loan_H l
            LEFT OUTER JOIN {{OBS_DATABASE_V}}.Score dq
                ON l.loan_key = dq.entity_key;
        """)
        path = write_temp_file(content, ".viw")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "{{SEM_DATABASE_V}}"
            assert "{{DOM_DATABASE_V}}" in result["grants"]
            assert "{{OBS_DATABASE_V}}" in result["grants"]
            assert result["grants"]["{{DOM_DATABASE_V}}"] == {PRIV_SELECT}
            assert result["grants"]["{{OBS_DATABASE_V}}"] == {PRIV_SELECT}
        finally:
            os.unlink(path)

    def test_self_reference_excluded(self):
        """A view referencing its own database generates no grants."""
        content = textwrap.dedent("""
            CREATE VIEW {{DOM_DATABASE_V}}.Enriched
            (loan_key)
            AS
            LOCKING ROW FOR ACCESS
            SELECT loan_key
            FROM {{DOM_DATABASE_V}}.Loan_H;
        """)
        path = write_temp_file(content, ".viw")
        try:
            result = analyse_file(path)
            # Self-reference only — no cross-db grants
            assert result is None
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Procedure analysis tests
# ---------------------------------------------------------------------------


class TestAnalyseProcedure:
    """Tests for analyse_file() with stored procedure DDL."""

    def test_insert_select_decomposes_correctly(self):
        """INSERT INTO target, SELECT FROM source → INSERT on target, SELECT on source."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{DOM_DATABASE_T}}.Load_Data()
            BEGIN
                INSERT INTO {{DOM_DATABASE_T}}.Target_Table
                (col1, col2)
                SELECT s.col1, s.col2
                FROM {{STG_DATABASE_T}}.Source_Table s;
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "{{DOM_DATABASE_T}}"
            # DOM_T is self-ref (INSERT target + procedure host) — excluded
            # STG_T is read source
            assert "{{STG_DATABASE_T}}" in result["grants"]
            assert result["grants"]["{{STG_DATABASE_T}}"] == {PRIV_SELECT}
            assert "{{DOM_DATABASE_T}}" not in result["grants"]
        finally:
            os.unlink(path)

    def test_merge_decomposes_correctly(self):
        """MERGE INTO target USING source → INSERT+UPDATE on target, SELECT on source."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{DOM_DATABASE_T}}.Merge_Data()
            BEGIN
                MERGE INTO {{DOM_DATABASE_T}}.Target_Table t
                USING {{STG_DATABASE_T}}.Source_Table s
                ON t.id = s.id
                WHEN MATCHED THEN UPDATE SET col1 = s.col1
                WHEN NOT MATCHED THEN INSERT (id, col1) VALUES (s.id, s.col1);
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            # DOM_T is self-ref — excluded
            # STG_T is read source
            assert "{{STG_DATABASE_T}}" in result["grants"]
            assert PRIV_SELECT in result["grants"]["{{STG_DATABASE_T}}"]
        finally:
            os.unlink(path)

    def test_update_from_decomposes_correctly(self):
        """UPDATE target FROM source → UPDATE on target, SELECT on source."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{OBS_DATABASE_T}}.Refresh()
            BEGIN
                UPDATE {{OBS_DATABASE_T}}.Score
                FROM {{DOM_DATABASE_V}}.Loan_H l
                SET quality_score = 100
                WHERE Score.entity_key = l.loan_key;
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "{{OBS_DATABASE_T}}"
            # OBS_T is self-ref — excluded
            # DOM_V is read source
            assert "{{DOM_DATABASE_V}}" in result["grants"]
            assert result["grants"]["{{DOM_DATABASE_V}}"] == {PRIV_SELECT}
        finally:
            os.unlink(path)

    def test_call_implies_execute_procedure(self):
        """CALL {{DB}}.Procedure implies EXECUTE PROCEDURE on that DB."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{OBS_DATABASE_T}}.Runner()
            BEGIN
                CALL {{MEM_DATABASE_T}}.Log_Event('test', 'msg', CURRENT_TIMESTAMP);
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            assert "{{MEM_DATABASE_T}}" in result["grants"]
            assert PRIV_EXEC_PROC in result["grants"]["{{MEM_DATABASE_T}}"]
        finally:
            os.unlink(path)

    def test_cross_database_insert(self):
        """Procedure in DB_A inserting into DB_B → INSERT on DB_B."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{STG_DATABASE_T}}.Push_To_Domain()
            BEGIN
                INSERT INTO {{DOM_DATABASE_T}}.Target_Table
                (col1)
                SELECT s.col1
                FROM {{STG_DATABASE_T}}.Source_Table s;
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "{{STG_DATABASE_T}}"
            # DOM_T is a cross-db INSERT target
            assert "{{DOM_DATABASE_T}}" in result["grants"]
            assert result["grants"]["{{DOM_DATABASE_T}}"] == {PRIV_INSERT}
            # STG_T is self-ref — excluded
            assert "{{STG_DATABASE_T}}" not in result["grants"]
        finally:
            os.unlink(path)

    def test_delete_implies_delete_privilege(self):
        """DELETE FROM {{DB}}.Table implies DELETE on that DB."""
        content = textwrap.dedent("""
            CREATE PROCEDURE {{DOM_DATABASE_T}}.Cleanup()
            BEGIN
                DELETE FROM {{STG_DATABASE_T}}.Old_Data
                WHERE load_date < CURRENT_DATE - 30;
            END;
        """)
        path = write_temp_file(content, ".spl")
        try:
            result = analyse_file(path)
            assert result is not None
            assert "{{STG_DATABASE_T}}" in result["grants"]
            assert result["grants"]["{{STG_DATABASE_T}}"] == {PRIV_DELETE}
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Macro analysis tests
# ---------------------------------------------------------------------------


class TestAnalyseMacro:
    """Tests for analyse_file() with macro DDL."""

    def test_replace_macro_infers_select_and_update_grants(self):
        """REPLACE MACRO is analysed like CREATE MACRO for grant intent."""
        content = textwrap.dedent("""
            REPLACE MACRO {{GCFR_M}}.GCFR_Stream_BusDate_Special
            (
                Stream_Key SMALLINT NOT NULL
            )
            AS
            (
                SELECT Stream_Key
                FROM {{GCFR_V}}.GCFR_Stream_BusDate
                WHERE Stream_Key = :Stream_Key;

                UPDATE {{GCFR_V}}.GCFR_Stream_BusDate
                SET Processing_Flag = 0
                WHERE Stream_Key = :Stream_Key;
            );
        """)
        path = write_temp_file(content, ".mcr")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["obj_type"] == "MACRO"
            assert result["grantee"] == "{{GCFR_M}}"
            assert "{{GCFR_V}}" in result["grants"]
            assert PRIV_SELECT in result["grants"]["{{GCFR_V}}"]
            assert PRIV_UPDATE in result["grants"]["{{GCFR_V}}"]
        finally:
            os.unlink(path)

    def test_replace_macro_infers_literal_delete_database_grant(self):
        """Literal macro DELETE targets infer database-level grants."""
        content = textwrap.dedent("""
            REPLACE MACRO GDEV1M_GCFR.GCFR_Reg_Process_Type_Param
            AS
            (
                DELETE FROM GDEV1T_GCFR.GCFR_Process_Type_Param
                WHERE Process_Type_Code = :Process_Type_Code;
            );
        """)
        path = write_temp_file(content, ".mcr")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "GDEV1M_GCFR"
            assert result["grants"]["GDEV1T_GCFR"] == {PRIV_DELETE}
        finally:
            os.unlink(path)

    def test_replace_macro_infers_literal_select_database_grant(self):
        """Literal macro SELECT sources infer database-level grants."""
        content = textwrap.dedent("""
            REPLACE MACRO GDEV1M_GCFR.GCFR_Register_Multi_Func_Sup_Columns
            AS
            (
                SELECT Out_DB_Name
                FROM GDEV1T_GCFR.GCFR_Multi_Func_Columns
                WHERE Func_Code = :Func_Code;
            );
        """)
        path = write_temp_file(content, ".mcr")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "GDEV1M_GCFR"
            assert result["grants"]["GDEV1T_GCFR"] == {PRIV_SELECT}
        finally:
            os.unlink(path)

    def test_replace_macro_combines_literal_view_dml_and_select_grants(self):
        """One literal view used for DELETE, INSERT, and SELECT gets all intents."""
        content = textwrap.dedent("""
            REPLACE MACRO GDEV1M_GCFR.GCFR_Reg_Process_Type_Param
            (
                Process_Type BYTEINT,
                Param_Group VARCHAR(240),
                Param_name VARCHAR(240)
            )
            AS
            (
                DELETE FROM GDEV1V_GCFR.GCFR_Process_Type_Param
                WHERE Process_Type = :Process_Type
                AND Param_Group = :Param_Group
                AND Param_Name = :Param_Name;

                INSERT INTO GDEV1V_GCFR.GCFR_Process_Type_Param
                (Process_Type, Param_Group, Param_name)
                SELECT :Process_Type, :Param_Group, :Param_name;

                SELECT Process_Type, Param_Group, Param_name
                FROM GDEV1V_GCFR.GCFR_Process_Type_Param
                WHERE Process_Type = :Process_Type
                AND Param_Group = :Param_Group
                AND Param_Name = :Param_Name;
            );
        """)
        path = write_temp_file(content, ".mcr")
        try:
            result = analyse_file(path)
            assert result is not None
            assert result["grantee"] == "GDEV1M_GCFR"
            assert result["grants"]["GDEV1V_GCFR"] == {
                PRIV_DELETE,
                PRIV_INSERT,
                PRIV_SELECT,
            }
        finally:
            os.unlink(path)

    def test_exec_implies_execute(self):
        """EXEC {{DB}}.Macro implies EXECUTE on that DB."""
        content = textwrap.dedent("""
            CREATE MACRO {{DOM_DATABASE_T}}.Run_Load()
            AS
            (
                EXEC {{UTL_DATABASE_T}}.Refresh_Stats;
            );
        """)
        path = write_temp_file(content, ".mcr")
        try:
            result = analyse_file(path)
            assert result is not None
            assert "{{UTL_DATABASE_T}}" in result["grants"]
            assert PRIV_EXEC in result["grants"]["{{UTL_DATABASE_T}}"]
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Consolidation tests
# ---------------------------------------------------------------------------


class TestConsolidation:
    """Tests for consolidate_grants() and generate_grt_content()."""

    def test_consolidates_same_grantee(self):
        """Multiple files with the same grantee merge into one entry."""
        results = [
            {
                "file": "view1.viw",
                "grantee": "{{DOM_DATABASE_V}}",
                "obj_type": "VIEW",
                "obj_name": "View1",
                "grants": {"{{DOM_DATABASE_T}}": {PRIV_SELECT}},
            },
            {
                "file": "view2.viw",
                "grantee": "{{DOM_DATABASE_V}}",
                "obj_type": "VIEW",
                "obj_name": "View2",
                "grants": {"{{DOM_DATABASE_T}}": {PRIV_SELECT}},
            },
        ]
        consolidated = consolidate_grants(results)
        assert "{{DOM_DATABASE_V}}" in consolidated
        # Should merge into one grantor entry
        assert len(consolidated["{{DOM_DATABASE_V}}"]) == 1
        assert PRIV_SELECT in consolidated["{{DOM_DATABASE_V}}"]["{{DOM_DATABASE_T}}"]

    def test_consolidates_multiple_privileges(self):
        """Multiple privileges on the same pair are merged."""
        results = [
            {
                "file": "proc1.spl",
                "grantee": "{{DOM_DATABASE_T}}",
                "obj_type": "PROCEDURE",
                "obj_name": "Proc1",
                "grants": {"{{STG_DATABASE_T}}": {PRIV_SELECT}},
            },
            {
                "file": "proc2.spl",
                "grantee": "{{DOM_DATABASE_T}}",
                "obj_type": "PROCEDURE",
                "obj_name": "Proc2",
                "grants": {"{{STG_DATABASE_T}}": {PRIV_SELECT, PRIV_DELETE}},
            },
        ]
        consolidated = consolidate_grants(results)
        privs = consolidated["{{DOM_DATABASE_T}}"]["{{STG_DATABASE_T}}"]
        assert PRIV_SELECT in privs
        assert PRIV_DELETE in privs

    def test_generates_consolidated_grant_statement(self):
        """Multiple privileges on one pair produce one comma-separated GRANT."""
        grants = {
            "{{STG_DATABASE_T}}": {PRIV_SELECT, PRIV_INSERT, PRIV_DELETE},
        }
        sources = [
            {
                "file": "proc1.spl",
                "grantee": "{{DOM_DATABASE_T}}",
                "obj_type": "PROCEDURE",
                "obj_name": "Proc1",
                "grants": grants,
            },
        ]
        content = generate_grt_content(
            "{{DOM_DATABASE_T}}", grants, sources, "TestProject"
        )
        # Should produce one GRANT statement with all three privileges
        assert "GRANT SELECT, INSERT, DELETE ON {{STG_DATABASE_T}}" in content
        assert "WITH GRANT OPTION" in content
        # Should NOT produce three separate GRANT statements
        # (Count lines starting with GRANT, not the word GRANT which
        # also appears in 'WITH GRANT OPTION')
        grant_lines = [
            line for line in content.splitlines() if line.strip().startswith("GRANT ")
        ]
        assert len(grant_lines) == 1


# ---------------------------------------------------------------------------
# Filename derivation tests
# ---------------------------------------------------------------------------


class TestFilename:
    """Tests for grantee_filename()."""

    def test_tokenised_filename(self):
        assert grantee_filename("{{DOM_DATABASE_V}}") == "{{DOM_DATABASE_V}}.dcl"

    def test_literal_filename(self):
        assert grantee_filename("D01_MP_DOM_V") == "D01_MP_DOM_V.dcl"


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_table_ddl_returns_none(self):
        """CREATE TABLE files should return None (no cross-db refs expected)."""
        content = textwrap.dedent("""
            CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Payment_H
            (
                payment_key INTEGER NOT NULL,
                loan_key    INTEGER NOT NULL
            )
            PRIMARY INDEX (payment_key);
        """)
        path = write_temp_file(content, ".tbl")
        try:
            result = analyse_file(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_no_create_statement_returns_none(self):
        """A file without a CREATE statement returns None."""
        content = "-- just a comment\nSELECT 1;"
        path = write_temp_file(content, ".viw")
        try:
            result = analyse_file(path)
            assert result is None
        finally:
            os.unlink(path)

    def test_comments_do_not_produce_false_refs(self):
        """Database references inside comments are ignored."""
        content = textwrap.dedent("""
            /*
            ** This view used to reference {{OLD_DATABASE}}.Retired_Table
            */
            CREATE VIEW {{DOM_DATABASE_V}}.Active
            (col1)
            AS
            LOCKING ROW FOR ACCESS
            SELECT col1
            FROM {{DOM_DATABASE_T}}.Active;
        """)
        path = write_temp_file(content, ".viw")
        try:
            result = analyse_file(path)
            assert result is not None
            # OLD_DATABASE should not appear — it was in a comment
            assert "{{OLD_DATABASE}}" not in result["grants"]
            assert "{{DOM_DATABASE_T}}" in result["grants"]
        finally:
            os.unlink(path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
