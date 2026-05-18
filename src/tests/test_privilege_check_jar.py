from __future__ import annotations

from database_package_deployer.models import DeployStrategy, ObjectType, ParsedStatement
from database_package_deployer.privilege_check import check_deployer_privileges


class SequencedCursor:
    def __init__(self, rights_rows):
        self.rights_rows = rights_rows
        self.last_sql = ""
        self.statements = []

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.statements.append((sql, params))

    def fetchone(self):
        if "SELECT USER" in self.last_sql:
            return ("DBC",)
        return None

    def fetchall(self):
        if "DBC.AllRightsV" in self.last_sql:
            return self.rights_rows
        return []


def _jar_statement(text: str) -> ParsedStatement:
    return ParsedStatement(
        file_path="payload/03_ddl/jar_install/GCFR_UT_Install_Jar.sjr",
        ddl_text=text,
        original_text=text,
        database_name="",
        object_name="JAR",
        object_type=ObjectType.JAR,
        strategy=DeployStrategy.DIRECT_EXECUTE,
        qualified_name="JAR",
    )


def test_jar_install_requires_external_procedure_privileges():
    cursor = SequencedCursor(rights_rows=[])
    parsed = _jar_statement(
        "DATABASE GDEV1P_UT;\n"
        "CALL SQLJ.INSTALL_JAR('CJ!GCFR_UT_Install_Jar.jar', 'GCFR_UT', 0);"
    )

    result = check_deployer_privileges(
        cursor=cursor,
        parsed_ddls=[parsed],
        created_databases=set(),
        package_name="GCFR",
        environment="DEV",
    )

    assert result.passed is False
    assert "GDEV1P_UT" in result.missing
    assert "CREATE EXTERNAL PROCEDURE" in result.missing["GDEV1P_UT"]
    assert "ALTER EXTERNAL PROCEDURE" in result.missing["GDEV1P_UT"]
    assert (
        "GRANT CREATE EXTERNAL PROCEDURE, ALTER EXTERNAL PROCEDURE ON GDEV1P_UT TO DBC;"
        in result.script
    )


def test_jar_install_external_procedure_privileges_pass_when_present():
    cursor = SequencedCursor(rights_rows=[("CE",), ("AE",)])
    parsed = _jar_statement(
        'DATABASE "GDEV1P_UT";\n'
        "CALL SQLJ.REPLACE_JAR('CJ!GCFR_UT_Install_Jar.jar', 'GCFR_UT');"
    )

    result = check_deployer_privileges(
        cursor=cursor,
        parsed_ddls=[parsed],
        created_databases=set(),
        package_name="GCFR",
        environment="DEV",
    )

    assert result.passed is True
    assert result.missing == {}
    assert result.checked_databases == ["GDEV1P_UT"]
