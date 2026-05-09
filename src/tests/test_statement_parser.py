"""
test_statement_parser.py — Tests for the DDL deployer's parser module.

Covers:
    - Object type detection for all supported types
    - Qualified name extraction (with and without quotes)
    - Deploy intent detection (the DDL verb IS the intent)
    - MULTISET injection for tables
    - SPECIFIC function name extraction
    - Strategy derivation from intent
    - Index parent table extraction
    - Edge cases from real-world bugs
"""

import pytest

from database_package_deployer.statement_parser import (
    parse_statement_text,
    parse_statement_file,
    _detect_deploy_intent,
    _detect_object_type,
    _inject_multiset_if_missing,
    _split_qualified_name,
    parse_index_parent_table,
)
from database_package_deployer.models import (
    ObjectType,
    DeployIntent,
    DeployStrategy,
)


# ---------------------------------------------------------------
# Object type detection
# ---------------------------------------------------------------


class TestDetectObjectType:
    """Tests for _detect_object_type pattern matching."""

    def test_create_table(self):
        """CREATE TABLE → TABLE."""
        obj_type, _ = _detect_object_type("CREATE TABLE MyDB.T (Id INT);")
        assert obj_type == ObjectType.TABLE

    def test_create_multiset_table(self):
        """CREATE MULTISET TABLE → TABLE."""
        obj_type, _ = _detect_object_type("CREATE MULTISET TABLE MyDB.T (Id INT);")
        assert obj_type == ObjectType.TABLE

    def test_create_set_table(self):
        """CREATE SET TABLE → TABLE."""
        obj_type, _ = _detect_object_type("CREATE SET TABLE MyDB.T (Id INT);")
        assert obj_type == ObjectType.TABLE

    def test_create_volatile_table(self):
        """CREATE MULTISET VOLATILE TABLE → TABLE."""
        obj_type, _ = _detect_object_type(
            "CREATE MULTISET VOLATILE TABLE MyDB.T (Id INT);"
        )
        assert obj_type == ObjectType.TABLE

    def test_create_global_temporary_trace_table(self):
        """CREATE MULTISET GLOBAL TEMPORARY TRACE TABLE → TABLE."""
        ddl = "CREATE MULTISET GLOBAL TEMPORARY TRACE TABLE MyDB.T (Id INT);"
        obj_type, _ = _detect_object_type(ddl)
        assert obj_type == ObjectType.TABLE

    def test_replace_view(self):
        """REPLACE VIEW → VIEW."""
        obj_type, _ = _detect_object_type("REPLACE VIEW MyDB.V AS SELECT 1;")
        assert obj_type == ObjectType.VIEW

    def test_create_view(self):
        """CREATE VIEW → VIEW."""
        obj_type, _ = _detect_object_type("CREATE VIEW MyDB.V AS SELECT 1;")
        assert obj_type == ObjectType.VIEW

    def test_replace_macro(self):
        """REPLACE MACRO → MACRO."""
        obj_type, _ = _detect_object_type("REPLACE MACRO MyDB.M AS (SELECT 1;);")
        assert obj_type == ObjectType.MACRO

    def test_replace_procedure(self):
        """REPLACE PROCEDURE → PROCEDURE."""
        obj_type, _ = _detect_object_type("REPLACE PROCEDURE MyDB.P() BEGIN END;")
        assert obj_type == ObjectType.PROCEDURE

    def test_replace_function(self):
        """REPLACE FUNCTION → FUNCTION."""
        obj_type, _ = _detect_object_type(
            "REPLACE FUNCTION MyDB.F(x INT) RETURNS INT RETURN x;"
        )
        assert obj_type == ObjectType.FUNCTION

    def test_replace_specific_function(self):
        """REPLACE SPECIFIC FUNCTION → FUNCTION."""
        obj_type, _ = _detect_object_type(
            "REPLACE SPECIFIC FUNCTION MyDB.F_Int RETURNS INT RETURN 1;"
        )
        assert obj_type == ObjectType.FUNCTION

    def test_create_trigger(self):
        """CREATE TRIGGER → TRIGGER."""
        obj_type, _ = _detect_object_type(
            "CREATE TRIGGER MyDB.trg AFTER INSERT ON MyDB.T (SELECT 1;);"
        )
        assert obj_type == ObjectType.TRIGGER

    def test_replace_trigger(self):
        """REPLACE TRIGGER → TRIGGER — the bug that was fixed."""
        obj_type, _ = _detect_object_type(
            "REPLACE TRIGGER MyDB.trg AFTER INSERT ON MyDB.T (SELECT 1;);"
        )
        assert obj_type == ObjectType.TRIGGER

    def test_create_join_index(self):
        """CREATE JOIN INDEX → JOIN_INDEX."""
        obj_type, _ = _detect_object_type(
            "CREATE JOIN INDEX MyDB.JI AS SELECT * FROM MyDB.T;"
        )
        assert obj_type == ObjectType.JOIN_INDEX

    def test_create_hash_index(self):
        """CREATE HASH INDEX → HASH_INDEX."""
        obj_type, _ = _detect_object_type(
            "CREATE HASH INDEX MyDB.HI (Col) ON MyDB.T ORDER BY VALUES;"
        )
        assert obj_type == ObjectType.HASH_INDEX

    def test_create_index(self):
        """CREATE INDEX → INDEX."""
        obj_type, _ = _detect_object_type("CREATE INDEX idx_Name (Name) ON MyDB.T;")
        assert obj_type == ObjectType.INDEX

    def test_create_database(self):
        """CREATE DATABASE → DATABASE."""
        obj_type, _ = _detect_object_type(
            "CREATE DATABASE MyDB FROM DBC AS PERMANENT = 1e9;"
        )
        assert obj_type == ObjectType.DATABASE

    def test_create_user(self):
        """CREATE USER → USER."""
        obj_type, _ = _detect_object_type(
            "CREATE USER svc FROM MyDB AS PERMANENT = 1e6;"
        )
        assert obj_type == ObjectType.USER

    def test_create_profile(self):
        """CREATE PROFILE → PROFILE."""
        obj_type, _ = _detect_object_type("CREATE PROFILE batch_prf;")
        assert obj_type == ObjectType.PROFILE

    def test_create_role(self):
        """CREATE ROLE → ROLE."""
        obj_type, _ = _detect_object_type("CREATE ROLE read_only;")
        assert obj_type == ObjectType.ROLE

    def test_grant(self):
        """GRANT → GRANT."""
        obj_type, _ = _detect_object_type("GRANT SELECT ON MyDB TO SomeRole;")
        assert obj_type == ObjectType.GRANT

    def test_revoke(self):
        """REVOKE → REVOKE."""
        obj_type, _ = _detect_object_type("REVOKE SELECT ON MyDB FROM SomeRole;")
        assert obj_type == ObjectType.REVOKE

    def test_unknown(self):
        """Unrecognised DDL → UNKNOWN."""
        obj_type, _ = _detect_object_type("ALTER SESSION SET TIMEZONE TO 'UTC';")
        assert obj_type == ObjectType.UNKNOWN

    def test_create_map(self):
        """CREATE MAP → MAP."""
        obj_type, _ = _detect_object_type(
            "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        )
        assert obj_type == ObjectType.MAP

    def test_create_authorization(self):
        """CREATE AUTHORIZATION → AUTHORIZATION."""
        obj_type, _ = _detect_object_type(
            "CREATE AUTHORIZATION MyAuth AS DEFINER TRUSTED;"
        )
        assert obj_type == ObjectType.AUTHORIZATION

    def test_create_foreign_server(self):
        """CREATE FOREIGN SERVER → FOREIGN_SERVER."""
        obj_type, _ = _detect_object_type(
            "CREATE FOREIGN SERVER MyRemote USING LINK('host=x');"
        )
        assert obj_type == ObjectType.FOREIGN_SERVER

    def test_jar_install(self):
        """CALL SQLJ.INSTALL_JAR → JAR."""
        obj_type, _ = _detect_object_type(
            "CALL SQLJ.INSTALL_JAR('CJ!my.jar', 'MyJar', 0);"
        )
        assert obj_type == ObjectType.JAR

    def test_jar_replace(self):
        """CALL SQLJ.REPLACE_JAR → JAR."""
        obj_type, _ = _detect_object_type(
            "CALL SQLJ.REPLACE_JAR('CJ!my.jar', 'MyJar');"
        )
        assert obj_type == ObjectType.JAR


# ---------------------------------------------------------------
# Deploy intent detection
# ---------------------------------------------------------------


class TestDetectDeployIntent:
    """Tests for intent-aware deployment — the DDL verb IS the intent."""

    def test_table_always_idempotent(self):
        """Tables always use IDEMPOTENT_DEPLOY regardless of verb."""
        assert (
            _detect_deploy_intent("CREATE TABLE MyDB.T (Id INT);", ObjectType.TABLE)
            == DeployIntent.IDEMPOTENT_DEPLOY
        )

    def test_replace_view_intent(self):
        """REPLACE VIEW → REPLACE_WITH_BACKUP."""
        assert (
            _detect_deploy_intent("REPLACE VIEW MyDB.V AS SELECT 1;", ObjectType.VIEW)
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_create_view_intent(self):
        """CREATE VIEW → CREATE_ONLY."""
        assert (
            _detect_deploy_intent("CREATE VIEW MyDB.V AS SELECT 1;", ObjectType.VIEW)
            == DeployIntent.CREATE_ONLY
        )

    def test_replace_macro_intent(self):
        """REPLACE MACRO → REPLACE_WITH_BACKUP."""
        assert (
            _detect_deploy_intent(
                "REPLACE MACRO MyDB.M AS (SELECT 1;);", ObjectType.MACRO
            )
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_create_macro_intent(self):
        """CREATE MACRO → CREATE_ONLY."""
        assert (
            _detect_deploy_intent(
                "CREATE MACRO MyDB.M AS (SELECT 1;);", ObjectType.MACRO
            )
            == DeployIntent.CREATE_ONLY
        )

    def test_replace_procedure_intent(self):
        """REPLACE PROCEDURE → REPLACE_WITH_BACKUP."""
        assert (
            _detect_deploy_intent(
                "REPLACE PROCEDURE MyDB.P() BEGIN END;", ObjectType.PROCEDURE
            )
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_replace_function_intent(self):
        """REPLACE FUNCTION → REPLACE_WITH_BACKUP."""
        assert (
            _detect_deploy_intent(
                "REPLACE FUNCTION MyDB.F(x INT) RETURNS INT RETURN x;",
                ObjectType.FUNCTION,
            )
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_replace_specific_function_intent(self):
        """REPLACE SPECIFIC FUNCTION → REPLACE_WITH_BACKUP."""
        ddl = "REPLACE SPECIFIC FUNCTION MyDB.F_Int RETURNS INT RETURN 1;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.FUNCTION)
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_replace_trigger_intent(self):
        """REPLACE TRIGGER → REPLACE_WITH_BACKUP."""
        ddl = "REPLACE TRIGGER MyDB.trg AFTER INSERT ON MyDB.T (SELECT 1;);"
        assert (
            _detect_deploy_intent(ddl, ObjectType.TRIGGER)
            == DeployIntent.REPLACE_WITH_BACKUP
        )

    def test_create_trigger_intent(self):
        """CREATE TRIGGER → CREATE_ONLY."""
        ddl = "CREATE TRIGGER MyDB.trg AFTER INSERT ON MyDB.T (SELECT 1;);"
        assert (
            _detect_deploy_intent(ddl, ObjectType.TRIGGER) == DeployIntent.CREATE_ONLY
        )

    def test_join_index_always_drop_and_create(self):
        """JOIN INDEX → DROP_AND_CREATE (no REPLACE alternative)."""
        ddl = "CREATE JOIN INDEX MyDB.JI AS SELECT * FROM MyDB.T;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.JOIN_INDEX)
            == DeployIntent.DROP_AND_CREATE
        )

    def test_database_direct_execute(self):
        """DATABASE → DIRECT_EXECUTE."""
        ddl = "CREATE DATABASE MyDB FROM DBC AS PERMANENT = 1e9;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.DATABASE)
            == DeployIntent.DIRECT_EXECUTE
        )

    def test_grant_direct_execute(self):
        """GRANT → DIRECT_EXECUTE."""
        ddl = "GRANT SELECT ON MyDB TO SomeRole;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.GRANT) == DeployIntent.DIRECT_EXECUTE
        )

    def test_map_skip_if_exists(self):
        """MAP → SKIP_IF_EXISTS."""
        ddl = "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        assert _detect_deploy_intent(ddl, ObjectType.MAP) == DeployIntent.SKIP_IF_EXISTS

    def test_role_skip_if_exists(self):
        """ROLE → SKIP_IF_EXISTS (changed from DIRECT_EXECUTE)."""
        ddl = "CREATE ROLE analyst_role;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.ROLE) == DeployIntent.SKIP_IF_EXISTS
        )

    def test_profile_skip_if_exists(self):
        """PROFILE → SKIP_IF_EXISTS (changed from DIRECT_EXECUTE)."""
        ddl = "CREATE PROFILE batch_profile;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.PROFILE)
            == DeployIntent.SKIP_IF_EXISTS
        )

    def test_authorization_skip_if_exists(self):
        """AUTHORIZATION → SKIP_IF_EXISTS."""
        ddl = "CREATE AUTHORIZATION MyAuth AS DEFINER TRUSTED;"
        assert (
            _detect_deploy_intent(ddl, ObjectType.AUTHORIZATION)
            == DeployIntent.SKIP_IF_EXISTS
        )

    def test_foreign_server_skip_if_exists(self):
        """FOREIGN SERVER → SKIP_IF_EXISTS."""
        ddl = "CREATE FOREIGN SERVER MyRemote USING LINK('host=x');"
        assert (
            _detect_deploy_intent(ddl, ObjectType.FOREIGN_SERVER)
            == DeployIntent.SKIP_IF_EXISTS
        )

    def test_jar_direct_execute(self):
        """JAR → DIRECT_EXECUTE."""
        ddl = "CALL SQLJ.INSTALL_JAR('CJ!my.jar', 'MyJar', 0);"
        assert _detect_deploy_intent(ddl, ObjectType.JAR) == DeployIntent.DIRECT_EXECUTE


# ---------------------------------------------------------------
# Strategy derivation from intent
# ---------------------------------------------------------------


class TestStrategyDerivation:
    """Tests that intent maps correctly to deployment strategy."""

    def test_replace_view_strategy(self):
        """REPLACE VIEW → REPLACE_IN_PLACE strategy."""
        parsed = parse_statement_text("REPLACE VIEW MyDB.V AS SELECT 1;")
        assert parsed.strategy == DeployStrategy.REPLACE_IN_PLACE

    def test_create_view_strategy(self):
        """CREATE VIEW → CREATE_ONLY strategy."""
        parsed = parse_statement_text("CREATE VIEW MyDB.V AS SELECT 1;")
        assert parsed.strategy == DeployStrategy.CREATE_ONLY

    def test_table_strategy(self):
        """CREATE TABLE → IDEMPOTENT_DEPLOY strategy."""
        parsed = parse_statement_text("CREATE MULTISET TABLE MyDB.T (Id INT);")
        assert parsed.strategy == DeployStrategy.IDEMPOTENT_DEPLOY

    def test_join_index_strategy(self):
        """CREATE JOIN INDEX → DROP_AND_CREATE strategy."""
        parsed = parse_statement_text(
            "CREATE JOIN INDEX MyDB.JI AS SELECT * FROM MyDB.T;"
        )
        assert parsed.strategy == DeployStrategy.DROP_AND_CREATE

    def test_database_strategy(self):
        """CREATE DATABASE → DIRECT_EXECUTE strategy."""
        parsed = parse_statement_text(
            "CREATE DATABASE MyDB FROM DBC AS PERMANENT = 1e9;"
        )
        assert parsed.strategy == DeployStrategy.DIRECT_EXECUTE

    def test_map_strategy(self):
        """CREATE MAP → SKIP_IF_EXISTS strategy."""
        parsed = parse_statement_text(
            "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        )
        assert parsed.strategy == DeployStrategy.SKIP_IF_EXISTS

    def test_role_strategy(self):
        """CREATE ROLE → SKIP_IF_EXISTS strategy."""
        parsed = parse_statement_text("CREATE ROLE analyst_role;")
        assert parsed.strategy == DeployStrategy.SKIP_IF_EXISTS

    def test_authorization_strategy(self):
        """CREATE AUTHORIZATION → SKIP_IF_EXISTS strategy."""
        parsed = parse_statement_text("CREATE AUTHORIZATION MyAuth AS DEFINER TRUSTED;")
        assert parsed.strategy == DeployStrategy.SKIP_IF_EXISTS

    def test_foreign_server_strategy(self):
        """CREATE FOREIGN SERVER → SKIP_IF_EXISTS strategy."""
        parsed = parse_statement_text(
            "CREATE FOREIGN SERVER MyRemote USING LINK('host=x');"
        )
        assert parsed.strategy == DeployStrategy.SKIP_IF_EXISTS


# ---------------------------------------------------------------
# MULTISET injection
# ---------------------------------------------------------------


class TestMultisetInjection:
    """Tests for MULTISET injection in the deployer parser."""

    def test_inject_when_missing(self):
        """MULTISET injected when neither SET nor MULTISET specified."""
        ddl = "CREATE TABLE MyDB.T (Id INT);"
        result, injected = _inject_multiset_if_missing(ddl)
        assert injected is True
        assert "MULTISET TABLE" in result

    def test_no_inject_multiset_present(self):
        """No injection when MULTISET already present."""
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT);"
        result, injected = _inject_multiset_if_missing(ddl)
        assert injected is False

    def test_no_inject_set_present(self):
        """No injection when SET already present."""
        ddl = "CREATE SET TABLE MyDB.T (Id INT);"
        result, injected = _inject_multiset_if_missing(ddl)
        assert injected is False

    def test_inject_volatile(self):
        """MULTISET injected before VOLATILE TABLE."""
        ddl = "CREATE VOLATILE TABLE MyDB.T (Id INT);"
        result, injected = _inject_multiset_if_missing(ddl)
        assert injected is True
        assert "CREATE MULTISET VOLATILE TABLE" in result


# ---------------------------------------------------------------
# parse_statement_text — Full parsing
# ---------------------------------------------------------------


class TestParseDdlText:
    """Tests for the main parse_statement_text function."""

    def test_qualified_name_extraction(self):
        """Database and object names are correctly extracted."""
        parsed = parse_statement_text("CREATE MULTISET TABLE MyDB.Customer (Id INT);")
        assert parsed.database_name == "MyDB"
        assert parsed.object_name == "Customer"
        assert parsed.qualified_name == "MyDB.Customer"

    def test_multiline_procedure_name_qualified(self):
        """Regression for issue #48.

        Some legacy Teradata DDL splits the qualified name across two
        lines — DB name on one line, dot + object name on the next.
        The parser must capture both parts as the qualified name.

            REPLACE PROCEDURE PDE_D01_00_GCFR_UTP_0_P
                .GCFR_UT_BKEY_S_K_NextId_Log_CT (...)

        Without the fix, only 'PDE_D01_00_GCFR_UTP_0_P' was captured
        (no dot match), giving db_name=None → ValueError.
        """
        ddl = (
            "REPLACE PROCEDURE PDE_D01_00_GCFR_UTP_0_P\n"
            "    .GCFR_UT_BKEY_S_K_NextId_Log_CT (IN p1 INTEGER)\n"
            "BEGIN\n"
            "    SET p1 = 0;\n"
            "END;\n"
        )
        parsed = parse_statement_text(ddl)
        assert parsed.database_name == "PDE_D01_00_GCFR_UTP_0_P"
        assert parsed.object_name == "GCFR_UT_BKEY_S_K_NextId_Log_CT"
        assert parsed.qualified_name == (
            "PDE_D01_00_GCFR_UTP_0_P.GCFR_UT_BKEY_S_K_NextId_Log_CT"
        )

    def test_multiline_view_name_qualified(self):
        """Multi-line qualified name also works for views."""
        ddl = "REPLACE VIEW MyDB\n    .v_Active AS SELECT 1 AS x;\n"
        parsed = parse_statement_text(ddl)
        assert parsed.database_name == "MyDB"
        assert parsed.object_name == "v_Active"

    def test_multiline_function_name_qualified(self):
        """Multi-line qualified name also works for functions."""
        ddl = (
            "REPLACE FUNCTION MyDB\n"
            ".fn_Calc (x INTEGER)\n"
            "RETURNS INTEGER\n"
            "RETURN x + 1;\n"
        )
        parsed = parse_statement_text(ddl)
        assert parsed.database_name == "MyDB"
        assert parsed.object_name == "fn_Calc"

    def test_single_part_name_not_duplicated(self):
        """Single-part names are not duplicated (e.g. role.role)."""
        parsed = parse_statement_text("CREATE ROLE analyst_role;")
        assert parsed.qualified_name == "analyst_role"
        assert parsed.database_name == ""
        assert parsed.object_name == "analyst_role"

    def test_map_single_part_name(self):
        """MAP has single-part qualified name."""
        parsed = parse_statement_text(
            "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        )
        assert parsed.qualified_name == "TD_GlobalMap"
        assert parsed.database_name == ""

    def test_quoted_identifiers(self):
        """Quoted identifiers have quotes stripped."""
        parsed = parse_statement_text(
            'CREATE MULTISET TABLE "MyDB"."My Table" (Id INT);'
        )
        assert parsed.database_name == "MyDB"
        assert parsed.object_name == "My Table"

    def test_specific_function_name(self):
        """Function overload uses SPECIFIC name as object_name."""
        ddl = (
            "REPLACE FUNCTION MyDB.fn_Calc (parm1 INTEGER)\n"
            "RETURNS INTEGER\n"
            "SPECIFIC MyDB.fn_Calc_Int\n"
            "RETURN parm1 * 2;\n"
        )
        parsed = parse_statement_text(ddl)
        assert parsed.object_name == "fn_Calc_Int"
        assert parsed.object_type == ObjectType.FUNCTION

    def test_empty_ddl_raises(self):
        """Empty/whitespace DDL raises ValueError."""
        with pytest.raises(ValueError, match="[Cc]ould not classify"):
            parse_statement_text("   ")

    def test_unclassifiable_raises(self):
        """Unclassifiable DDL raises ValueError."""
        with pytest.raises(ValueError, match="[Cc]ould not classify"):
            parse_statement_text("ALTER SESSION SET TIMEZONE TO 'UTC';")

    def test_multiset_injected_flag(self):
        """multiset_injected flag is set when MULTISET is auto-injected."""
        parsed = parse_statement_text("CREATE TABLE MyDB.T (Id INT);")
        assert parsed.multiset_injected is True

    def test_deploy_intent_set(self):
        """deploy_intent field is populated."""
        parsed = parse_statement_text("REPLACE VIEW MyDB.V AS SELECT 1;")
        assert parsed.deploy_intent == DeployIntent.REPLACE_WITH_BACKUP


# ---------------------------------------------------------------
# parse_statement_file
# ---------------------------------------------------------------


class TestParseDdlFile:
    """Tests for file-based DDL parsing."""

    def test_parse_file(self, tmp_path):
        """DDL file is read, parsed, and file_path is recorded."""
        f = tmp_path / "MyDB.Customer.tbl"
        f.write_text("CREATE MULTISET TABLE MyDB.Customer (Id INT);", encoding="utf-8")

        parsed = parse_statement_file(str(f))

        assert parsed.object_name == "Customer"
        assert parsed.file_path == str(f)

    def test_missing_file_raises(self, tmp_path):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_statement_file(str(tmp_path / "missing.tbl"))


# ---------------------------------------------------------------
# _split_qualified_name
# ---------------------------------------------------------------


class TestSplitQualifiedName:
    """Tests for name splitting utility."""

    def test_two_part(self):
        """DB.Object splits correctly."""
        db, obj = _split_qualified_name("MyDB.Customer")
        assert db == "MyDB"
        assert obj == "Customer"

    def test_single_part(self):
        """Single-part name returns (None, name)."""
        db, obj = _split_qualified_name("Customer")
        assert db is None
        assert obj == "Customer"

    def test_quoted_names(self):
        """Quoted parts have quotes stripped."""
        db, obj = _split_qualified_name('"My DB"."My Table"')
        assert db == "My DB"
        assert obj == "My Table"


# ---------------------------------------------------------------
# parse_index_parent_table
# ---------------------------------------------------------------


class TestParseIndexParentTable:
    """Tests for extracting parent table from CREATE INDEX ON clause."""

    def test_parent_table_extracted(self):
        """ON db.table clause is correctly extracted."""
        ddl = "CREATE INDEX idx_Name (Name) ON MyDB.Customer;"
        result = parse_index_parent_table(ddl)
        assert result == ("MyDB", "Customer")

    def test_no_on_clause(self):
        """No ON clause returns None."""
        ddl = "CREATE INDEX idx_Name (Name);"
        result = parse_index_parent_table(ddl)
        assert result is None


# ---------------------------------------------------------------
# DML detection — INSERT/UPDATE/DELETE/MERGE classify as DML and
# route through DIRECT_EXECUTE so .dml files actually deploy.
# ---------------------------------------------------------------


class TestDmlDetection:
    """DML scripts must classify, parse, and route to DIRECT_EXECUTE."""

    def test_insert_into_classifies_as_dml(self):
        obj_type, _ = _detect_object_type(
            "INSERT INTO MyDB.Customer (Id, Name) VALUES (1, 'a');"
        )
        assert obj_type == ObjectType.DML

    def test_update_classifies_as_dml(self):
        obj_type, _ = _detect_object_type(
            "UPDATE MyDB.Customer SET Name = 'a' WHERE Id = 1;"
        )
        assert obj_type == ObjectType.DML

    def test_delete_from_classifies_as_dml(self):
        obj_type, _ = _detect_object_type("DELETE FROM MyDB.Customer WHERE Id = 1;")
        assert obj_type == ObjectType.DML

    def test_merge_into_classifies_as_dml(self):
        obj_type, _ = _detect_object_type(
            "MERGE INTO MyDB.Customer t USING MyDB.Stg s ON t.Id = s.Id "
            "WHEN MATCHED THEN UPDATE SET Name = s.Name;"
        )
        assert obj_type == ObjectType.DML

    def test_dml_uses_direct_execute_strategy(self):
        """DML deploy_intent and strategy are both DIRECT_EXECUTE."""
        parsed = parse_statement_text(
            "INSERT INTO MyDB.Customer (Id, Name) VALUES (1, 'a');",
            file_path="MyDB.Customer.dml",
        )
        assert parsed.object_type == ObjectType.DML
        assert parsed.deploy_intent == DeployIntent.DIRECT_EXECUTE
        assert parsed.strategy == DeployStrategy.DIRECT_EXECUTE

    def test_dml_qualified_name_is_filename_keyed(self):
        """Manifest key is DML:<basename> so multi-target scripts
        never collide on a shared first target."""
        parsed = parse_statement_text(
            "INSERT INTO MyDB.Customer (Id) VALUES (1);\n"
            "INSERT INTO MyDB.Order (Id) VALUES (1);",
            file_path="/some/path/source_a.multi_table.dml",
        )
        assert parsed.qualified_name == "DML:source_a.multi_table"
        # First target's database is preserved for the report
        assert parsed.database_name == "MyDB"

    def test_procedure_with_inner_insert_does_not_classify_as_dml(self):
        """A PROCEDURE body containing INSERT must classify as
        PROCEDURE — DML matching is the last rung in the pattern
        ladder."""
        ddl = (
            "REPLACE PROCEDURE MyDB.LoadCust () "
            "BEGIN INSERT INTO MyDB.Customer VALUES (1); END;"
        )
        obj_type, _ = _detect_object_type(ddl)
        assert obj_type == ObjectType.PROCEDURE

    def test_dml_in_comment_does_not_misclassify(self):
        """An INSERT inside a comment must not trigger DML detection
        when a CREATE/REPLACE statement follows."""
        ddl = (
            "-- INSERT INTO MyDB.Customer VALUES (1);\nREPLACE VIEW MyDB.V AS SELECT 1;"
        )
        obj_type, _ = _detect_object_type(ddl)
        # Comment stripping happens upstream of _detect_object_type;
        # but even with the comment, VIEW pattern matches first.
        assert obj_type == ObjectType.VIEW
