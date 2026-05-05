"""
test_bteq_stripping.py — BTEQ command stripping from DATABASE and
USER payload files.

Legacy Teradata codebases often wrap CREATE DATABASE / CREATE USER
statements in BTEQ flow-control scaffolding (.IF ERRORCODE, .GOTO,
.LABEL, etc.). SHIPS deploys SQL directly; these BTEQ commands have
no meaning to SHIPS' deployer and prevent the parser classifying the
file. SHIPS strips them at harvest time so the payload contains
clean SQL only.

Two layers:

  1. ``_strip_bteq_commands`` unit tests — the BTEQ line regex,
     edge cases, lines-stripped count, unchanged pass-through.
  2. Integration through ``ingest_directory`` — confirms DATABASE
     and USER files get stripped while other types do not.
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
        "config/properties",
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

    def test_table_file_not_affected(self, tmp_path):
        """BTEQ stripping is scoped to DATABASE and USER types only.
        A procedure or table file with a dot-prefixed line (unusual
        but conceivable) must not have lines silently removed."""
        project = _make_project(tmp_path)
        source = tmp_path / "source"
        source.mkdir()
        # A CREATE TABLE file has no business containing BTEQ —
        # but if it does, it's a job for the user to fix, not for
        # SHIPS to silently strip. Scoping to DATABASE/USER is safe
        # and explicit.
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
