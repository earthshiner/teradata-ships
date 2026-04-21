"""
conftest.py — Shared pytest fixtures for the SHIPS test suite.

Provides temporary directory structures, sample DDL content,
and properties files used across multiple test modules.
"""

import os
import pytest
from pathlib import Path


# ---------------------------------------------------------------
# Temporary project scaffolding
# ---------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """
    Create a minimal SHIPS project structure in a temp directory.

    Returns the project root path. Structure:
        project/
            payload/
                database/
                    DDL/
                        tables/
                        views/
                        macros/
                        procedures/
                        functions/
                        triggers/
                        join_indexes/
                    pre-requisites/
                        databases/
                    DCL/
                        roles/
                        inter_db/
            config/
                properties/
            .build_counter   (contains "0")
    """
    project = tmp_path / "project"
    project.mkdir()

    # -- Payload directories --
    payload = project / "payload" / "database"
    for subdir in [
        "system/maps", "system/roles", "system/profiles",
        "system/authorizations", "system/foreign_servers",
        "DDL/tables", "DDL/views", "DDL/macros",
        "DDL/procedures", "DDL/functions", "DDL/triggers",
        "DDL/join_indexes", "DDL/JARs", "DDL/script_table_operators",
        "pre-requisites/databases",
        "DCL/roles", "DCL/inter_db",
    ]:
        (payload / subdir).mkdir(parents=True, exist_ok=True)

    # -- Config directories --
    (project / "config" / "properties").mkdir(parents=True)

    # -- Build counter --
    (project / ".build_counter").write_text("0\n", encoding="utf-8")

    return project


@pytest.fixture
def sample_properties_file(tmp_path):
    """
    Create a sample .properties file with token definitions.

    Returns the path to the properties file.
    """
    props = tmp_path / "DEV.properties"
    props.write_text(
        "# DEV environment properties\n"
        "SHIPS_ENV=DEV\n"
        "ENV_PREFIX=A_D01\n"
        "SHIPS_PROJECT=OMR\n"
        "STD_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD\n"
        "SEM_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM\n"
        "UNUSED_TOKEN=some_value\n",
        encoding="utf-8",
    )
    return props


# ---------------------------------------------------------------
# Sample DDL strings
# ---------------------------------------------------------------

@pytest.fixture
def ddl_create_table():
    """Standard CREATE TABLE DDL with database qualifier."""
    return (
        "CREATE MULTISET TABLE MyDB.Customer\n"
        "    ,NO FALLBACK\n"
        "    ,NO BEFORE JOURNAL\n"
        "    ,NO AFTER JOURNAL\n"
        "(\n"
        "     Cust_Id INTEGER NOT NULL\n"
        "    ,Cust_Name VARCHAR(100)\n"
        "    ,Created_Dt DATE\n"
        ")\n"
        "PRIMARY INDEX (Cust_Id);\n"
    )


@pytest.fixture
def ddl_create_table_no_multiset():
    """CREATE TABLE without SET/MULTISET — needs injection."""
    return (
        "CREATE TABLE MyDB.Orders\n"
        "(\n"
        "     Order_Id INTEGER NOT NULL\n"
        "    ,Cust_Id INTEGER\n"
        ")\n"
        "PRIMARY INDEX (Order_Id);\n"
    )


@pytest.fixture
def ddl_replace_view():
    """REPLACE VIEW DDL — idempotent deployment intent."""
    return (
        "REPLACE VIEW MyDB.ActiveCustomers AS\n"
        "SELECT Cust_Id, Cust_Name\n"
        "FROM MyDB.Customer\n"
        "WHERE Active_Flag = 'Y';\n"
    )


@pytest.fixture
def ddl_create_view():
    """CREATE VIEW DDL — non-idempotent (CREATE_ONLY intent)."""
    return (
        "CREATE VIEW MyDB.NewView AS\n"
        "SELECT 1 AS Dummy;\n"
    )


@pytest.fixture
def ddl_create_join_index():
    """CREATE JOIN INDEX DDL — DROP_AND_CREATE strategy."""
    return (
        "CREATE JOIN INDEX MyDB.JI_Customer AS\n"
        "SELECT Cust_Id, Cust_Name\n"
        "FROM MyDB.Customer\n"
        "PRIMARY INDEX (Cust_Id);\n"
    )


@pytest.fixture
def ddl_replace_trigger():
    """REPLACE TRIGGER DDL."""
    return (
        "REPLACE TRIGGER MyDB.trg_AuditCustomer\n"
        "AFTER INSERT ON MyDB.Customer\n"
        "REFERENCING NEW AS NewRow\n"
        "FOR EACH ROW\n"
        "(\n"
        "    INSERT INTO MyDB.AuditLog VALUES (NewRow.Cust_Id, CURRENT_TIMESTAMP);\n"
        ");\n"
    )


@pytest.fixture
def ddl_create_database():
    """CREATE DATABASE DDL — DIRECT_EXECUTE strategy."""
    return (
        "CREATE DATABASE MyDB\n"
        "FROM DBC\n"
        "AS PERMANENT = 1e9\n"
        "   ,SPOOL = 1e8;\n"
    )


@pytest.fixture
def ddl_grant():
    """GRANT statement — DIRECT_EXECUTE strategy."""
    return "GRANT SELECT ON MyDB TO SomeRole;\n"


@pytest.fixture
def ddl_function_with_specific():
    """Function with SPECIFIC name for overload handling."""
    return (
        "REPLACE FUNCTION MyDB.fn_Calc (parm1 INTEGER)\n"
        "RETURNS INTEGER\n"
        "LANGUAGE SQL\n"
        "SPECIFIC MyDB.fn_Calc_Int\n"
        "CONTAINS SQL\n"
        "DETERMINISTIC\n"
        "SQL SECURITY DEFINER\n"
        "RETURN parm1 * 2;\n"
    )


@pytest.fixture
def ddl_global_temp_trace_table():
    """GLOBAL TEMPORARY TRACE TABLE — edge case from real-world bugs."""
    return (
        "CREATE MULTISET GLOBAL TEMPORARY TRACE TABLE MyDB.TempTrace\n"
        "(\n"
        "     Trace_Id INTEGER\n"
        "    ,Trace_Msg VARCHAR(200)\n"
        ")\n"
        "ON COMMIT PRESERVE ROWS;\n"
    )


# ---------------------------------------------------------------
# System-scope DDL fixtures
# ---------------------------------------------------------------

@pytest.fixture
def ddl_create_map():
    """CREATE MAP DDL — system-scope, no database qualifier."""
    return "CREATE MAP TD_1AmpSparseMap_1Node FROM TD_MAP1 AMPCOUNT = 1;\n"


@pytest.fixture
def ddl_create_authorization():
    """CREATE AUTHORIZATION DDL — system-scope."""
    return (
        "CREATE AUTHORIZATION MyAuth\n"
        "AS DEFINER TRUSTED\n"
        "USER 'svc_account'\n"
        "PASSWORD 'secret';\n"
    )


@pytest.fixture
def ddl_create_foreign_server():
    """CREATE FOREIGN SERVER DDL — system-scope."""
    return (
        "CREATE FOREIGN SERVER MyRemoteServer\n"
        "USING\n"
        "    LINK('host=remote.example.com');\n"
    )


@pytest.fixture
def ddl_jar_install():
    """JAR installation via SQLJ.INSTALL_JAR."""
    return (
        "DATABASE {{JARS_DB}};\n"
        "CALL SQLJ.INSTALL_JAR('CJ!{{JARFILE}}', '{{JARNAME}}', 0);\n"
    )


@pytest.fixture
def ddl_jar_replace():
    """JAR replacement via SQLJ.REPLACE_JAR."""
    return (
        "DATABASE {{JARS_DB}};\n"
        "CALL SQLJ.REPLACE_JAR('CJ!{{JARFILE}}', '{{JARNAME}}');\n"
    )
