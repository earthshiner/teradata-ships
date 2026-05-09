"""
test_bteq_stripping.py — BTEQ command stripping applied universally
before classification.

Legacy Teradata codebases often wrap SQL of all types (CREATE DATABASE,
CREATE USER, GRANT, REVOKE, etc.) in BTEQ flow-control scaffolding
(.IF ERRORCODE, .GOTO, .LABEL, etc.). SHIPS deploys SQL directly;
these BTEQ commands have no meaning to SHIPS' deployer and prevent the
parser classifying the file. SHIPS strips them from raw_content before
the multi-statement split and classification so that any file type
classifies correctly.

Two layers:

  1. ``_strip_bteq_commands`` unit tests — the BTEQ line regex,
     edge cases, lines-stripped count, unchanged pass-through.
  2. Integration through ``ingest_directory`` — confirms that
     DATABASE, USER, and DCL files all get stripped, and that a clean
     SQL file produces no spurious warning.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.ingest import _strip_bteq_commands, ingest_directory


# ---------------------------------------------------------------
# _strip_bteq_commands
# ---------------------------------------------------------------


class TestStripBteqCommands:
    def test_if_errorcode_stripped(self):
        content = (
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "CREATE DATABASE MyDB FROM DBC AS PERM=0;\n"
        )
        cleaned, n = _strip_bteq_commands(content)
        assert ".IF" not in cleaned
        assert "CREATE DATABASE" in cleaned
        assert n == 1

    def test_goto_stripped(self):
        content = ".GOTO LABEL_END\nCREATE USER X FROM DBC AS PERM=0;\n"
        cleaned, n = _strip_bteq_commands(content)
        assert ".GOTO" not in cleaned
        assert n == 1

    def test_multiple_bteq_lines_all_stripped(self):
        content = (
            ".LOGON server/user,pass\n"
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
            "CREATE DATABASE MyDB FROM DBC AS PERM=0;\n"
            ".LOGOFF\n"
        )
        cleaned, n = _strip_bteq_commands(content)
        assert n == 3
        assert "CREATE DATABASE" in cleaned
        assert ".LOGON" not in cleaned
        assert ".LOGOFF" not in cleaned

    def test_pure_sql_unchanged(self):
        sql = "CREATE DATABASE MyDB FROM DBC\nAS PERM=0;\n"
        cleaned, n = _strip_bteq_commands(sql)
        assert n == 0
        assert cleaned == sql

    def test_leading_whitespace_before_dot_is_stripped(self):
        content = (
            "  .IF ERRORCODE <> 0 THEN .GOTO ERR\nCREATE USER X FROM DBC AS PERM=0;\n"
        )
        cleaned, n = _strip_bteq_commands(content)
        assert n == 1
        assert ".IF" not in cleaned

    def test_dot_in_sql_not_stripped(self):
        """A qualified name like DBC.SysExecSQL in the MIDDLE of a line
        must not be treated as a BTEQ command — the dot is not at the
        start of the line."""
        content = "CALL DBC.SysExecSQL(:vSQL);\n"
        cleaned, n = _strip_bteq_commands(content)
        assert n == 0
        assert "DBC.SysExecSQL" in cleaned

    def test_dot_in_perm_expression_not_stripped(self):
        """``AS PERM=15e6/2*(HASHAMP()+1)`` contains no leading dot."""
        content = "CREATE DATABASE MyDB FROM DBC\nAS PERM=15e6/2*(HASHAMP()+1)\n;\n"
        cleaned, n = _strip_bteq_commands(content)
        assert n == 0

    def test_blank_lines_collapsed(self):
        """Multiple blank lines left after stripping collapse to one."""
        content = ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n\n\nCREATE DATABASE X FROM DBC AS PERM=0;\n"
        cleaned, n = _strip_bteq_commands(content)
        assert "\n\n\n" not in cleaned

    def test_gcfr_database_example(self):
        """Exact user-reported example."""
        content = (
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
            "\n"
            "CREATE DATABASE PDE_D01_00_GCFR_CPP_0_P FROM PDE_D01_00_GCFR_API\n"
            "AS PERM=15e6/2*(HASHAMP()+1)\n"
            ";\n"
        )
        cleaned, n = _strip_bteq_commands(content)
        assert n == 1
        assert "CREATE DATABASE PDE_D01_00_GCFR_CPP_0_P" in cleaned
        assert ".IF" not in cleaned

    def test_gcfr_user_example(self):
        """Exact user-reported USER example."""
        content = (
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n"
            "\n"
            "CREATE USER PDE_D01_00_GCFR_ETL_USR FROM PDE_D01_00\n"
            "AS PASSWORD=PDE_D01_00_GCFR_ETL_USR\n"
            "   PERM=0\n"
            "   TEMPORARY=0\n"
            "   DEFAULT DATABASE=PDE_D01_00_GCFR_ETL_USR\n"
            "   NO FALLBACK\n"
            "   DEFAULT ROLE = ALL\n"
            ";\n"
        )
        cleaned, n = _strip_bteq_commands(content)
        assert n == 1
        assert "CREATE USER" in cleaned
        assert "DEFAULT DATABASE=PDE_D01_00_GCFR_ETL_USR" in cleaned
        assert ".IF" not in cleaned


# ---------------------------------------------------------------
# Integration: ingest_directory strips BTEQ from DATABASE/USER
# ---------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "payload/database/pre-requisites/users",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


class TestIngestBteqStripping:
    def test_database_file_stripped(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.db").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "CREATE DATABASE MyDB FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        # One file placed in the payload.
        assert result.classified == 1
        # Warning emitted naming the stripped lines.
        assert any(".IF" in w or "BTEQ" in w for w in result.classification_warnings)
        # Payload file contains only SQL.
        db_files = list(
            (project / "payload" / "database" / "pre-requisites" / "databases").glob(
                "*.db"
            )
        )
        assert len(db_files) == 1
        content = db_files[0].read_text(encoding="utf-8")
        assert ".IF" not in content
        assert "CREATE DATABASE" in content

    def test_user_file_stripped(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyUser.usr").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "CREATE USER MyUser FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        usr_files = list(
            (project / "payload" / "database" / "pre-requisites" / "users").glob(
                "*.usr"
            )
        )
        assert len(usr_files) == 1
        content = usr_files[0].read_text(encoding="utf-8")
        assert ".IF" not in content
        assert "CREATE USER" in content

    def test_dcl_file_with_bteq_preamble_classifies_correctly(self, tmp_path):
        """Regression for issue #52.

        A GRANT statement preceded by a BTEQ .IF ERRORCODE guard must
        classify as GRANT after stripping — not UNKNOWN. This was the
        root cause: stripping was scoped to DATABASE/USER only, so the
        .IF line survived into the classifier for DCL files.
        """
        project = _make_project(tmp_path)
        (project / "payload/database/DCL/inter_db").mkdir(parents=True, exist_ok=True)
        source = tmp_path / "source"
        source.mkdir()
        (source / "grants.dcl").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "GRANT CREATE PROCEDURE ON {{PROC_DB}} TO {{ADMIN_USER}};\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        assert result.classified == 1, (
            f"expected GRANT to classify correctly; unclassified: {result.unclassified_files}"
        )
        assert result.unclassified == 0
        # BTEQ warning must still be surfaced so the developer knows
        # the file contained BTEQ commands that were stripped.
        assert any("BTEQ" in w for w in result.classification_warnings)
        # Payload file must not contain the BTEQ command.
        dcl_files = list(
            (project / "payload" / "database" / "DCL" / "inter_db").glob("*.dcl")
        )
        assert dcl_files, "no DCL file placed in payload"
        content = dcl_files[0].read_text(encoding="utf-8")
        assert ".IF" not in content
        assert "GRANT" in content

    def test_pure_sql_file_no_bteq_warning(self, tmp_path):
        """A TABLE file with clean SQL must not produce a BTEQ warning."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INT) PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        assert result.classified == 1
        bteq_warnings = [w for w in result.classification_warnings if "BTEQ" in w]
        assert bteq_warnings == []

    def test_clean_database_file_no_warning(self, tmp_path):
        """A DATABASE file without BTEQ commands must not produce a
        spurious warning about stripping."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.db").write_text(
            "CREATE DATABASE MyDB FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        bteq_warnings = [w for w in result.classification_warnings if "BTEQ" in w]
        assert bteq_warnings == []

    def test_ddl_table_file_with_bteq_preamble_stripped(self, tmp_path):
        """DDL files (.tbl) with a BTEQ preamble are stripped and
        classify correctly. SHIPS does not want BTEQ in DDL output."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.Customer.tbl").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) "
            "PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        assert result.classified == 1
        assert result.unclassified == 0
        assert any("BTEQ" in w for w in result.classification_warnings)
        tbl_files = list(
            (project / "payload" / "database" / "DDL" / "tables").glob("*.tbl")
        )
        assert tbl_files, "no .tbl placed in payload"
        content = tbl_files[0].read_text(encoding="utf-8")
        assert ".IF" not in content
        assert "CREATE MULTISET TABLE" in content

    def test_ddl_view_file_with_bteq_preamble_stripped(self, tmp_path):
        """DDL files (.viw) with a BTEQ preamble are stripped and
        classify correctly."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        (source / "MyDB.v_active.viw").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "REPLACE VIEW MyDB.v_active AS SELECT 1 AS x;\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        assert result.classified == 1
        assert result.unclassified == 0
        viw_files = list(
            (project / "payload" / "database" / "DDL" / "views").glob("*.viw")
        )
        assert viw_files
        assert ".IF" not in viw_files[0].read_text(encoding="utf-8")

    def test_dml_file_with_bteq_preamble_stripped(self, tmp_path):
        """DML files (.dml) with a BTEQ preamble are stripped and
        classify correctly. SHIPS does not want BTEQ in DML output."""
        project = _make_project(tmp_path)
        (project / "payload/database/DML").mkdir(parents=True, exist_ok=True)
        source = tmp_path / "source"
        source.mkdir()
        (source / "load.dml").write_text(
            ".IF ERRORCODE <> 0 THEN .GOTO ERR\n\n"
            "INSERT INTO MyDB.Customer (Id) VALUES (1);\n",
            encoding="utf-8",
        )

        result = ingest_directory(str(source), str(project), detect_tokens=False)

        assert result.classified == 1
        assert result.unclassified == 0
        assert any("BTEQ" in w for w in result.classification_warnings)
        dml_files = list((project / "payload" / "database" / "DML").glob("*.dml"))
        assert dml_files, "no .dml placed in payload"
        assert ".IF" not in dml_files[0].read_text(encoding="utf-8")
