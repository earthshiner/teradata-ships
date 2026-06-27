"""
test_split_compound_objects.py — harvest must split multi-object files
that contain Teradata compound objects (stored procedures, SQL
functions, triggers, macros) into one atomic object per file.

Atomic splitting is a hard requirement for topological (wave) ordering:
every sourced object needs its own payload file so it becomes an
individual node in the dependency graph. The earlier splitter bailed out
of any file containing ``BEGIN`` or ``CREATE/REPLACE MACRO`` and returned
it whole, collapsing several objects into one node.

Covers:
    1. _compound_aware_semicolon_positions — paren + BEGIN…END + CASE
       depth tracking, control-flow END handling, balance reporting.
    2. _split_multi_statement — procedures / functions / triggers / macros
       mixed with tables split into atomic chunks; single compound objects
       and plain multi-DDL unaffected; unbalanced input declines to split.
    3. End-to-end ingest_directory — atomic payload files with correct
       extensions.
"""

from __future__ import annotations

import os

from td_release_packager.ingest import (
    _compound_aware_semicolon_positions,
    _split_multi_statement,
    ingest_directory,
)
from td_release_packager.sql_text import strip_comments_and_string_literals


def _n_chunks(sql: str) -> int:
    return len(_split_multi_statement(sql, "test.sql"))


# ---------------------------------------------------------------
# _compound_aware_semicolon_positions
# ---------------------------------------------------------------


class TestScanner:
    def _scan(self, sql: str):
        return _compound_aware_semicolon_positions(
            strip_comments_and_string_literals(sql)
        )

    def test_plain_statements_count_top_level_semicolons(self):
        positions, balanced = self._scan(
            "CREATE TABLE a (x INT); CREATE TABLE b (y INT);"
        )
        assert balanced is True
        assert len(positions) == 2

    def test_semicolons_inside_begin_end_are_not_boundaries(self):
        sql = "REPLACE PROCEDURE p() BEGIN SET x = 1; SET y = 2; END;"
        positions, balanced = self._scan(sql)
        assert balanced is True
        # Only the final ``;`` after END terminates the statement.
        assert len(positions) == 1

    def test_semicolons_inside_parens_are_not_boundaries(self):
        sql = "CREATE MACRO m AS (SELECT 1; SELECT 2;);"
        positions, balanced = self._scan(sql)
        assert balanced is True
        assert len(positions) == 1

    def test_end_if_does_not_close_begin(self):
        sql = (
            "REPLACE PROCEDURE p() BEGIN "
            "IF x = 1 THEN SET y = 2; END IF; SET z = 3; END;"
        )
        positions, balanced = self._scan(sql)
        assert balanced is True
        assert len(positions) == 1

    def test_case_expression_bare_end_does_not_close_begin(self):
        sql = (
            "REPLACE PROCEDURE p() BEGIN "
            "SET y = CASE WHEN x IS NULL THEN 0 ELSE x END; END;"
        )
        positions, balanced = self._scan(sql)
        assert balanced is True
        assert len(positions) == 1

    def test_unbalanced_begin_reports_not_balanced(self):
        # Missing END — cannot parse confidently.
        positions, balanced = self._scan("REPLACE PROCEDURE p() BEGIN SET x = 1;")
        assert balanced is False


# ---------------------------------------------------------------
# _split_multi_statement
# ---------------------------------------------------------------

_PROC = (
    "REPLACE PROCEDURE MyDB.sp_Touch()\n"
    "BEGIN\n"
    "    DECLARE v_n INTEGER;\n"
    "    IF v_n = 1 THEN\n"
    "        UPDATE MyDB.Customer SET c = CASE WHEN c IS NULL THEN 0 ELSE c END;\n"
    "    END IF;\n"
    "END;"
)
_TBL_A = (
    "CREATE MULTISET TABLE MyDB.Customer (Cust_Id INTEGER) PRIMARY INDEX (Cust_Id);"
)
_TBL_B = (
    "CREATE MULTISET TABLE MyDB.Orders (Order_Id INTEGER) PRIMARY INDEX (Order_Id);"
)
_MACRO = "CREATE MACRO MyDB.m AS (\n    SELECT 1;\n    SELECT 2;\n);"
_TRIGGER = (
    "CREATE TRIGGER MyDB.trg AFTER INSERT ON MyDB.Customer\n"
    "REFERENCING NEW AS n FOR EACH ROW (\n"
    "    INSERT INTO MyDB.log (id) VALUES (n.Cust_Id);\n"
    ");"
)
_FUNC = (
    "REPLACE FUNCTION MyDB.f(a INTEGER) RETURNS INTEGER\n"
    "LANGUAGE SQL\n"
    "BEGIN ATOMIC\n"
    "    RETURN a + 1;\n"
    "END;"
)


class TestSplitCompoundObjects:
    def test_table_procedure_table(self):
        assert _n_chunks(f"{_TBL_A}\n\n{_PROC}\n\n{_TBL_B}") == 3

    def test_table_macro_table(self):
        assert _n_chunks(f"{_TBL_A}\n\n{_MACRO}\n\n{_TBL_B}") == 3

    def test_trigger_then_table(self):
        assert _n_chunks(f"{_TRIGGER}\n\n{_TBL_B}") == 2

    def test_function_then_table(self):
        assert _n_chunks(f"{_FUNC}\n\n{_TBL_B}") == 2

    def test_two_procedures_split(self):
        assert _n_chunks(f"{_PROC}\n\n{_PROC.replace('sp_Touch', 'sp_Other')}") == 2


class TestNonSplitCases:
    def test_single_procedure_stays_whole(self):
        nested = (
            "REPLACE PROCEDURE MyDB.p()\n"
            "L1: BEGIN\n"
            "    DECLARE i INTEGER DEFAULT 0;\n"
            "    WHILE i < 10 DO\n"
            "        BEGIN SET i = i + 1; END;\n"
            "    END WHILE;\n"
            "END L1;"
        )
        assert _n_chunks(nested) == 1

    def test_plain_two_tables_still_split(self):
        assert _n_chunks(f"{_TBL_A}\n\n{_TBL_B}") == 2

    def test_unbalanced_begin_declines_to_split(self):
        # Missing END — keep the file whole rather than slice the body.
        broken = f"{_TBL_A}\n\nREPLACE PROCEDURE MyDB.p() BEGIN SET x = 1;"
        assert _n_chunks(broken) == 1


# ---------------------------------------------------------------
# End-to-end harvest
# ---------------------------------------------------------------


class TestHarvestAtomicSplit:
    def test_multi_object_file_yields_atomic_payload_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "multi.sql").write_text(
            f"{_TBL_A}\n\n{_PROC}\n\n{_TBL_B}", encoding="utf-8"
        )
        proj = tmp_path / "proj"
        (proj / "payload" / "database").mkdir(parents=True)

        ingest_directory(source_dir=str(src), project_dir=str(proj))

        produced = {
            f
            for _, _, files in os.walk(proj / "payload")
            for f in files
            if not f.startswith(".")
        }
        assert "MyDB.Customer.tbl" in produced
        assert "MyDB.Orders.tbl" in produced
        # The procedure is now its own atomic object, not folded into a table.
        assert "MyDB.sp_Touch.spl" in produced
