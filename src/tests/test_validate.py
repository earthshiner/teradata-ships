"""
test_validate.py — Tests for the SHIPS inspector / linter (validate module).

Covers:
    - Database qualifier check
    - SET/MULTISET check for tables
    - Deploy intent check (REPLACE prohibited — CREATE required)
    - One-object-per-file check
    - Eponymous file naming check
    - File extension check
    - Type suffix/prefix check
    - Hardcoded name detection
    - Keyword case check
    - Leading comma check
    - Full directory validation
"""

import pytest

from td_release_packager.validate import (
    _check_db_qualifier,
    _check_multiset,
    _check_deploy_intent,
    _check_view_macro_self_reference,
    _check_one_object,
    _check_eponymous,
    _check_extension,
    _check_type_suffixes,
    _check_hardcoded_names,
    _check_keyword_case,
    _check_leading_commas,
    validate_directory,
    read_inspect_config,
    generate_default_config,
    DEFAULT_RULES,
)


# ---------------------------------------------------------------
# _check_db_qualifier
# ---------------------------------------------------------------


class TestCheckDbQualifier:
    """Tests for database qualifier presence."""

    def test_qualified_name_passes(self):
        """DB.Object name produces no issues."""
        issues = _check_db_qualifier("test.tbl", "CREATE TABLE MyDB.Customer (Id INT);")
        assert issues == []

    def test_unqualified_name_flagged(self):
        """Unqualified name is flagged."""
        issues = _check_db_qualifier("test.tbl", "CREATE TABLE Customer (Id INT);")
        assert len(issues) == 1
        assert issues[0].rule == "db_qualifier"

    def test_token_in_qualifier_passes(self):
        """{{TOKEN}}.Object is accepted as qualified."""
        ddl = "CREATE TABLE {{STD_DATABASE}}.Customer (Id INT);"
        issues = _check_db_qualifier("test.tbl", ddl)
        assert issues == []

    def test_system_scope_map_skipped(self):
        """CREATE MAP has no qualifier — not flagged (system-scope)."""
        ddl = "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        issues = _check_db_qualifier("test.map", ddl)
        assert issues == []

    def test_system_scope_role_skipped(self):
        """CREATE ROLE has no qualifier — not flagged (system-scope)."""
        ddl = "CREATE ROLE analyst_role;"
        issues = _check_db_qualifier("test.rol", ddl)
        assert issues == []

    def test_system_scope_authorization_skipped(self):
        """CREATE AUTHORIZATION has no qualifier — not flagged (system-scope)."""
        ddl = "CREATE AUTHORIZATION MyAuth AS DEFINER TRUSTED;"
        issues = _check_db_qualifier("test.auth", ddl)
        assert issues == []


# ---------------------------------------------------------------
# _check_multiset
# ---------------------------------------------------------------


class TestCheckMultiset:
    """Tests for SET/MULTISET presence on tables."""

    def test_multiset_present_passes(self):
        """MULTISET TABLE produces no issues."""
        issues = _check_multiset("test.tbl", "CREATE MULTISET TABLE MyDB.T (Id INT);")
        assert issues == []

    def test_set_present_passes(self):
        """SET TABLE produces no issues."""
        issues = _check_multiset("test.tbl", "CREATE SET TABLE MyDB.T (Id INT);")
        assert issues == []

    def test_missing_set_multiset_flagged(self):
        """Missing SET/MULTISET on TABLE is flagged."""
        issues = _check_multiset("test.tbl", "CREATE TABLE MyDB.T (Id INT);")
        assert len(issues) == 1
        assert issues[0].rule == "set_multiset"

    def test_non_table_not_checked(self):
        """Views and other types are not checked for SET/MULTISET."""
        issues = _check_multiset("test.viw", "CREATE VIEW MyDB.V AS SELECT 1;")
        assert issues == []


# ---------------------------------------------------------------
# _check_deploy_intent (strict mode)
# ---------------------------------------------------------------


class TestCheckDeployIntent:
    """Tests for CREATE-not-REPLACE enforcement."""

    def test_replace_view_rejected(self):
        """REPLACE VIEW produces ERROR."""
        ddl = "REPLACE VIEW MyDB.V AS SELECT 1;"
        issues = _check_deploy_intent("v.viw", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "deploy_intent"
        assert issues[0].severity == "ERROR"
        assert "DROP-and-CREATE" in issues[0].message

    def test_create_view_passes(self):
        """CREATE VIEW produces no deploy_intent issue."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_procedure_rejected(self):
        """REPLACE PROCEDURE produces ERROR."""
        ddl = "REPLACE PROCEDURE MyDB.sp_X() BEGIN END;"
        issues = _check_deploy_intent("x.spl", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_create_procedure_passes(self):
        """CREATE PROCEDURE produces no deploy_intent issue."""
        ddl = "CREATE PROCEDURE MyDB.sp_X() BEGIN END;"
        assert _check_deploy_intent("x.spl", ddl) == []

    def test_replace_trigger_rejected(self):
        """REPLACE TRIGGER produces ERROR."""
        ddl = "REPLACE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        issues = _check_deploy_intent("x.trg", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_create_trigger_passes(self):
        """CREATE TRIGGER produces no deploy_intent issue."""
        ddl = (
            "CREATE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        )
        assert _check_deploy_intent("x.trg", ddl) == []

    def test_replace_function_rejected(self):
        """REPLACE FUNCTION produces ERROR."""
        ddl = "REPLACE FUNCTION MyDB.fn_X(p INT) RETURNS INT RETURN p;"
        issues = _check_deploy_intent("x.fnc", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_create_function_passes(self):
        """CREATE FUNCTION produces no deploy_intent issue."""
        ddl = "CREATE FUNCTION MyDB.fn_X(p INT) RETURNS INT RETURN p;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_replace_specific_function_rejected(self):
        """REPLACE SPECIFIC FUNCTION produces ERROR."""
        ddl = "REPLACE SPECIFIC FUNCTION MyDB.fn_X_Int RETURNS INT RETURN 1;"
        issues = _check_deploy_intent("x.fnc", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_create_specific_function_passes(self):
        """CREATE SPECIFIC FUNCTION produces no deploy_intent issue."""
        ddl = "CREATE SPECIFIC FUNCTION MyDB.fn_X_Int RETURNS INT RETURN 1;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_replace_macro_rejected(self):
        """REPLACE MACRO produces ERROR."""
        ddl = "REPLACE MACRO MyDB.mc_X AS (SELECT 1;);"
        issues = _check_deploy_intent("x.mcr", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_create_macro_passes(self):
        """CREATE MACRO produces no deploy_intent issue."""
        ddl = "CREATE MACRO MyDB.mc_X AS (SELECT 1;);"
        assert _check_deploy_intent("x.mcr", ddl) == []

    def test_create_join_index_passes(self):
        """CREATE JOIN INDEX has no REPLACE form — should not trigger."""
        ddl = "CREATE JOIN INDEX MyDB.JI_X AS SELECT * FROM MyDB.T;"
        assert _check_deploy_intent("x.jix", ddl) == []

    def test_create_table_not_checked(self):
        """Tables have no REPLACE form — should not trigger."""
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT);"
        assert _check_deploy_intent("t.tbl", ddl) == []

    def test_replace_case_insensitive(self):
        """Detection is case-insensitive."""
        for verb in ["replace", "Replace", "REPLACE"]:
            ddl = f"{verb} VIEW MyDB.V AS SELECT 1;"
            issues = _check_deploy_intent("v.viw", ddl)
            assert len(issues) == 1, f"Failed for verb: {verb}"

    def test_replace_with_leading_whitespace(self):
        """Leading whitespace before REPLACE is still detected."""
        ddl = "   REPLACE VIEW MyDB.V AS SELECT 1;"
        issues = _check_deploy_intent("v.viw", ddl)
        assert len(issues) == 1

    def test_replace_in_line_comment_not_flagged(self):
        """REPLACE in a comment is not flagged (line starts with --)."""
        ddl = "-- was REPLACE VIEW\nCREATE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_inside_procedure_body_flagged(self):
        """REPLACE on an inner line of a procedure body IS flagged."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "  REPLACE VIEW MyDB.temp_v AS SELECT 1;\n"
            "END;"
        )
        issues = _check_deploy_intent("x.spl", ddl)
        assert len(issues) == 1


# ---------------------------------------------------------------
# _check_view_macro_self_reference
# ---------------------------------------------------------------


class TestCheckViewMacroSelfReference:
    """Tests for view/macro self-reference detection."""

    # -- Views: positive cases (must flag) --

    def test_tokenised_view_self_reference_flagged(self):
        """{{V}}.X selecting from {{V}}.X is flagged."""
        ddl = (
            "CREATE VIEW {{DOM_V}}.CustomerOrders AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT *\n"
            "FROM {{DOM_V}}.CustomerOrders;"
        )
        issues = _check_view_macro_self_reference("x.viw", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "view_macro_self_reference"
        assert issues[0].severity == "ERROR"
        assert "{{DOM_V}}.CustomerOrders" in issues[0].message
        assert issues[0].line is not None

    def test_literal_view_self_reference_flagged(self):
        """Non-tokenised literal name self-reference is flagged."""
        ddl = "CREATE VIEW MyDB.MyView AS\nSELECT * FROM MyDB.MyView;"
        issues = _check_view_macro_self_reference("v.viw", ddl)
        assert len(issues) == 1
        assert "MyDB.MyView" in issues[0].message

    def test_quoted_view_self_reference_flagged(self):
        """Quoted identifiers in both header and body are flagged."""
        ddl = 'CREATE VIEW "MyDB"."MyView" AS\nSELECT * FROM "MyDB"."MyView";'
        issues = _check_view_macro_self_reference("v.viw", ddl)
        assert len(issues) == 1

    def test_replace_view_self_reference_flagged(self):
        """REPLACE VIEW form is also detected."""
        ddl = "REPLACE VIEW {{DOM_V}}.X AS\nSELECT 1 FROM {{DOM_V}}.X;"
        issues = _check_view_macro_self_reference("x.viw", ddl)
        assert len(issues) == 1

    # -- Views: negative cases (must not flag) --

    def test_locking_view_pattern_passes(self):
        """The standard 1:1 locking view pattern is not flagged.

        {{V_DB}}.X selecting from {{T_DB}}.X is the required
        Object Placement Standard pattern -- different database,
        same object name, must pass.
        """
        ddl = (
            "CREATE VIEW {{DOM_V}}.CustomerOrders AS\n"
            "LOCKING ROW FOR ACCESS\n"
            "SELECT *\n"
            "FROM {{DOM_T}}.CustomerOrders;"
        )
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_different_object_same_db_passes(self):
        """{{V}}.X selecting from {{V}}.Y is not a self-reference."""
        ddl = "CREATE VIEW {{DOM_V}}.X AS\nSELECT * FROM {{DOM_V}}.Y;"
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_substring_object_name_not_flagged(self):
        """{{V}}.Customer must not match inside {{V}}.CustomerOrders."""
        ddl = (
            "CREATE VIEW {{DOM_V}}.Customer AS\nSELECT * FROM {{DOM_V}}.CustomerOrders;"
        )
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_self_reference_in_line_comment_not_flagged(self):
        """Self-reference inside a -- comment must not be flagged."""
        ddl = (
            "CREATE VIEW {{DOM_V}}.X AS\n"
            "-- previously did SELECT FROM {{DOM_V}}.X\n"
            "SELECT * FROM {{DOM_T}}.X;"
        )
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_self_reference_in_block_comment_not_flagged(self):
        """Self-reference inside /* ... */ must not be flagged."""
        ddl = (
            "CREATE VIEW {{DOM_V}}.X AS\n"
            "/* historical: was SELECT FROM {{DOM_V}}.X */\n"
            "SELECT * FROM {{DOM_T}}.X;"
        )
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_unqualified_self_reference_not_flagged(self):
        """Unqualified bare-name reference is not flagged here.

        ``db_qualifier`` rule catches the missing qualifier.
        Keeping these rules orthogonal avoids double-reporting.
        """
        ddl = "CREATE VIEW {{DOM_V}}.X AS\nSELECT * FROM X;"
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    def test_unqualified_view_name_not_checked(self):
        """If the view itself is unqualified, the rule cannot
        determine self-reference and returns no issues."""
        ddl = "CREATE VIEW UnqualifiedView AS SELECT 1 FROM UnqualifiedView;"
        assert _check_view_macro_self_reference("x.viw", ddl) == []

    # -- Macros --

    def test_macro_self_reference_via_exec_flagged(self):
        """A macro EXECing itself is flagged (infinite loop at runtime)."""
        ddl = (
            "CREATE MACRO {{DOM_M}}.RebuildX AS (\n"
            "  DELETE FROM {{DOM_T}}.X;\n"
            "  EXEC {{DOM_M}}.RebuildX;\n"
            ");"
        )
        issues = _check_view_macro_self_reference("x.mcr", ddl)
        assert len(issues) == 1
        assert "{{DOM_M}}.RebuildX" in issues[0].message

    def test_macro_referencing_other_object_passes(self):
        """A macro referencing other database objects is fine."""
        ddl = (
            "CREATE MACRO {{DOM_M}}.RebuildX AS (\n"
            "  INSERT INTO {{DOM_T}}.X SELECT * FROM {{DOM_T}}.Y;\n"
            ");"
        )
        assert _check_view_macro_self_reference("x.mcr", ddl) == []

    # -- Out-of-scope object types --

    def test_table_not_checked(self):
        """CREATE TABLE is out of scope for this rule."""
        ddl = "CREATE MULTISET TABLE {{DOM_T}}.X (Id INTEGER);"
        assert _check_view_macro_self_reference("x.tbl", ddl) == []

    def test_procedure_not_checked(self):
        """CREATE PROCEDURE is out of scope (separate rule planned)."""
        ddl = "CREATE PROCEDURE {{DOM_P}}.sp_X()\nBEGIN\n  CALL {{DOM_P}}.sp_X();\nEND;"
        assert _check_view_macro_self_reference("x.spl", ddl) == []

    def test_no_create_statement_no_match(self):
        """Random text without a CREATE/REPLACE header returns empty."""
        assert _check_view_macro_self_reference("x.viw", "-- empty file\n") == []

    # -- Match details --

    def test_case_insensitive_keyword_match(self):
        """create / Create / CREATE all detected, body match is also case-insensitive."""
        for verb in ("create", "Create", "CREATE", "replace", "REPLACE"):
            ddl = f"{verb} VIEW {{{{DOM_V}}}}.X AS SELECT * FROM {{{{dom_v}}}}.x;"
            issues = _check_view_macro_self_reference("x.viw", ddl)
            assert len(issues) == 1, f"Failed for verb: {verb}"

    def test_line_number_points_at_body_match(self):
        """Reported line number matches the body occurrence, not the header."""
        ddl = (
            "CREATE VIEW {{DOM_V}}.X AS\n"  # line 1
            "LOCKING ROW FOR ACCESS\n"  # line 2
            "SELECT *\n"  # line 3
            "FROM {{DOM_V}}.X;"  # line 4
        )
        issues = _check_view_macro_self_reference("x.viw", ddl)
        assert len(issues) == 1
        assert issues[0].line == 4

    def test_whitespace_around_dot_is_caught(self):
        """Teradata accepts whitespace around the qualifier dot.

        ``MyDB . MyView`` is valid Teradata syntax, so a self-reference
        written that way must still be flagged. The regex is
        deliberately tolerant of inter-segment whitespace.
        """
        ddl = 'CREATE VIEW MyDB.MyView AS\nSELECT * FROM MyDB . "MyView";'
        issues = _check_view_macro_self_reference("v.viw", ddl)
        assert len(issues) == 1


# ---------------------------------------------------------------
# _check_one_object
# ---------------------------------------------------------------


class TestCheckOneObject:
    """Tests for single-object-per-file rule."""

    def test_single_statement_passes(self):
        """One DDL statement passes."""
        ddl = "CREATE TABLE MyDB.T (Id INT);"
        assert _check_one_object("t.tbl", ddl) == []

    def test_multiple_statements_flagged(self):
        """Multiple DDL statements are flagged."""
        ddl = (
            "CREATE TABLE MyDB.T1 (Id INT);\n"
            "CREATE TABLE MyDB.T2 (Id INT);\n"
            "CREATE VIEW MyDB.V AS SELECT 1;\n"
        )
        issues = _check_one_object("multi.sql", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "one_object"

    def test_procedure_with_inner_statements_allowed(self):
        """Procedure with INSERT/UPDATE in its body is exactly ONE
        DDL statement (the CREATE PROCEDURE). Body DML must NOT
        count toward the one-object threshold."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "    INSERT INTO MyDB.Log VALUES (1);\n"
            "END;\n"
        )
        issues = _check_one_object("x.spl", ddl)
        assert issues == []

    def test_real_world_procedure_with_if_else_branches(self):
        """Regression test: GCFR_BB_ProcessIDTool_Set.spl had
        CREATE PROCEDURE with an IF/ELSE block doing one INSERT
        and one UPDATE inside BEGIN...END. The previous regex
        counted INSERT + UPDATE as additional 'DDL statements',
        firing a spurious one_object warning. With DML excluded
        from the count, this passes cleanly."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X(IN flag BYTEINT)\n"
            "MAIN:\n"
            "BEGIN\n"
            "    IF flag = 0 THEN\n"
            "        INSERT INTO MyDB.t (a) VALUES (1);\n"
            "    ELSE\n"
            "        UPDATE MyDB.t SET a = 1 WHERE a = 0;\n"
            "    END IF;\n"
            "END MAIN;\n"
        )
        issues = _check_one_object("real.spl", ddl)
        assert issues == [], (
            "Procedure with body DML must not trip the one-object "
            "rule — body INSERT/UPDATE are DML, not DDL."
        )

    def test_two_top_level_dml_does_not_count(self):
        """Even at top level, INSERT and UPDATE shouldn't count
        toward the DDL count — they're DML statements. A file
        with CREATE TABLE followed by INSERT is unusual but not
        a violation of one-object-per-DDL."""
        ddl = (
            "CREATE TABLE MyDB.t (a INT);\n"
            "INSERT INTO MyDB.t VALUES (1);\n"
        )
        issues = _check_one_object("seed.tbl", ddl)
        assert issues == []

    def test_two_top_level_create_statements_flagged(self):
        """Two real DDL statements (both CREATE) still trip the
        rule — that's the actual one-object violation."""
        ddl = (
            "CREATE TABLE MyDB.t1 (a INT);\n"
            "CREATE TABLE MyDB.t2 (a INT);\n"
        )
        issues = _check_one_object("two.sql", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "one_object"


# ---------------------------------------------------------------
# _check_eponymous
# ---------------------------------------------------------------


class TestCheckEponymous:
    """Tests for filename-matches-DDL-content rule."""

    def test_matching_name_passes(self, tmp_path):
        """Filename matching DDL object passes."""
        f = tmp_path / "MyDB.Customer.tbl"
        f.write_text("CREATE TABLE MyDB.Customer (Id INT);", encoding="utf-8")
        issues = _check_eponymous("MyDB.Customer.tbl", f.read_text(), str(f))
        assert issues == []

    def test_mismatched_name_flagged(self, tmp_path):
        """Filename not matching DDL object is flagged."""
        f = tmp_path / "wrong_name.tbl"
        f.write_text("CREATE TABLE MyDB.Customer (Id INT);", encoding="utf-8")
        issues = _check_eponymous("wrong_name.tbl", f.read_text(), str(f))
        assert len(issues) == 1
        assert issues[0].rule == "eponymous"

    def test_tokenised_name_passes(self, tmp_path):
        """Names with {{TOKENS}} are not checked (resolved at build time)."""
        f = tmp_path / "{{DB}}.Customer.tbl"
        f.write_text("CREATE TABLE {{DB}}.Customer (Id INT);", encoding="utf-8")
        issues = _check_eponymous("{{DB}}.Customer.tbl", f.read_text(), str(f))
        assert issues == []


# ---------------------------------------------------------------
# _check_extension
# ---------------------------------------------------------------


class TestCheckExtension:
    """Tests for correct file extension per object type."""

    def test_correct_extension_passes(self, tmp_path):
        """Correct extension for object type passes."""
        f = tmp_path / "test.tbl"
        f.write_text("CREATE TABLE MyDB.T (Id INT);", encoding="utf-8")
        issues = _check_extension("test.tbl", f.read_text(), str(f))
        assert issues == []

    def test_wrong_extension_flagged(self, tmp_path):
        """Wrong extension for object type is flagged."""
        f = tmp_path / "test.sql"
        f.write_text("CREATE TABLE MyDB.T (Id INT);", encoding="utf-8")
        issues = _check_extension("test.sql", f.read_text(), str(f))
        assert len(issues) == 1
        assert issues[0].rule == "extension"

    def test_view_extension(self, tmp_path):
        """View should use .viw extension."""
        f = tmp_path / "test.sql"
        f.write_text("CREATE VIEW MyDB.V AS SELECT 1;", encoding="utf-8")
        issues = _check_extension("test.sql", f.read_text(), str(f))
        assert len(issues) == 1
        assert ".viw" in issues[0].message


# ---------------------------------------------------------------
# _check_type_suffixes
# ---------------------------------------------------------------


class TestCheckTypeSuffixes:
    """Tests for forbidden type suffix/prefix detection."""

    def test_no_suffix_passes(self):
        """Clean object name passes."""
        ddl = "CREATE TABLE MyDB.Customer (Id INT);"
        issues = _check_type_suffixes("t.tbl", ddl)
        assert issues == []

    def test_view_suffix_flagged(self):
        """_V suffix on object name is flagged as ERROR."""
        ddl = "CREATE VIEW MyDB.Customer_V AS SELECT 1;"
        issues = _check_type_suffixes("v.viw", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "ERROR"

    def test_table_suffix_flagged(self):
        """_T suffix on object name is flagged."""
        ddl = "CREATE TABLE MyDB.Customer_T (Id INT);"
        issues = _check_type_suffixes("t.tbl", ddl)
        assert len(issues) == 1

    def test_sp_suffix_flagged(self):
        """_SP suffix on object name is flagged."""
        ddl = "CREATE PROCEDURE MyDB.DoStuff_SP() BEGIN END;"
        issues = _check_type_suffixes("p.spl", ddl)
        assert len(issues) == 1


# ---------------------------------------------------------------
# _check_hardcoded_names
# ---------------------------------------------------------------


class TestCheckHardcodedNames:
    """Tests for hardcoded database name detection."""

    def test_tokenised_name_passes(self):
        """DDL using {{TOKENS}} passes."""
        ddl = "CREATE TABLE {{STD_DB}}.Customer (Id INT);"
        issues = _check_hardcoded_names("t.tbl", ddl)
        assert issues == []

    def test_hardcoded_user_db_flagged(self):
        """Hardcoded user database name is flagged as WARNING."""
        ddl = "CREATE TABLE DEV01_STD.Customer (Id INT);"
        issues = _check_hardcoded_names("t.tbl", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "hardcoded_name"

    def test_system_db_not_flagged(self):
        """System databases (DBC, SYSUDTLIB) are not flagged."""
        ddl = "CREATE TABLE DBC.SomeSystem (Id INT);"
        issues = _check_hardcoded_names("t.tbl", ddl)
        assert issues == []

    def test_system_scope_map_not_flagged(self):
        """MAP has no tokens — not flagged (system-scope objects don't use tokens)."""
        ddl = "CREATE MAP TD_GlobalMap CONTIGUOUS AMP BETWEEN 0 AND 7;"
        issues = _check_hardcoded_names("t.map", ddl)
        assert issues == []

    def test_system_scope_role_not_flagged(self):
        """ROLE has no tokens — not flagged (system-scope)."""
        ddl = "CREATE ROLE analyst_role;"
        issues = _check_hardcoded_names("t.rol", ddl)
        assert issues == []


# ---------------------------------------------------------------
# _check_keyword_case
# ---------------------------------------------------------------


class TestCheckKeywordCase:
    """Tests for SQL keyword case checking."""

    def test_uppercase_passes(self):
        """All-uppercase keywords pass."""
        ddl = "CREATE TABLE MyDB.T (Id INTEGER NOT NULL);"
        issues = _check_keyword_case("t.tbl", ddl)
        assert issues == []

    def test_mostly_lowercase_flagged(self):
        """Majority lowercase keywords are flagged."""
        ddl = (
            "create table MyDB.T (\n"
            "    id integer not null\n"
            "   ,name varchar(100) default 'x'\n"
            "   ,created date\n"
            ") primary index (id);\n"
        )
        issues = _check_keyword_case("t.tbl", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "keyword_case"


# ---------------------------------------------------------------
# _check_leading_commas
# ---------------------------------------------------------------


class TestCheckLeadingCommas:
    """Tests for leading vs trailing comma convention."""

    def test_leading_commas_pass(self):
        """Leading comma style passes."""
        ddl = (
            "CREATE TABLE MyDB.T\n"
            "(\n"
            "     Id INTEGER\n"
            "    ,Name VARCHAR(100)\n"
            "    ,Created DATE\n"
            ");\n"
        )
        issues = _check_leading_commas("t.tbl", ddl)
        assert issues == []

    def test_trailing_commas_flagged(self):
        """Trailing comma style is flagged when count > 3."""
        ddl = (
            "CREATE TABLE MyDB.T (\n"
            "    Id INTEGER,\n"
            "    Name VARCHAR(100),\n"
            "    Email VARCHAR(200),\n"
            "    Phone VARCHAR(20),\n"
            "    Created DATE\n"
            ");\n"
        )
        issues = _check_leading_commas("t.tbl", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "leading_commas"


# ---------------------------------------------------------------
# validate_directory (integration)
# ---------------------------------------------------------------


class TestValidateDirectory:
    """Integration tests for full directory validation."""

    def test_clean_project_passes(self, tmp_path):
        """A well-formed DDL file passes all checks."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE {{STD_DB}}.Customer\n"
            "(\n"
            "     Cust_Id INTEGER NOT NULL\n"
            "    ,Cust_Name VARCHAR(100)\n"
            ")\n"
            "PRIMARY INDEX (Cust_Id);\n",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 1
        assert result.errors == 0

    def test_strict_mode_catches_replace_view(self, tmp_path):
        """REPLACE VIEW is caught as an error (default severity)."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.V.viw").write_text(
            "REPLACE VIEW {{DB}}.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.errors > 0
        assert not result.passed

    def test_create_view_passes_all_checks(self, tmp_path):
        """CREATE VIEW produces no deploy_intent issue."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{DB}}.V.viw").write_text(
            "CREATE VIEW {{DB}}.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        deploy_issues = [i for i in result.issues if i.rule == "deploy_intent"]
        assert deploy_issues == []


# ---------------------------------------------------------------
# read_inspect_config
# ---------------------------------------------------------------


class TestReadInspectConfig:
    """Tests for reading inspect.conf configuration files."""

    def test_basic_config(self, tmp_path):
        """Key=value pairs are read and merged with defaults."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "leading_commas=OFF\nkeyword_case=OFF\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["leading_commas"] == "OFF"
        assert rules["keyword_case"] == "OFF"
        # Defaults preserved for unmentioned rules — verified
        # against DEFAULT_RULES so the test does not duplicate the
        # default-severity spec.
        assert rules["db_qualifier"] == DEFAULT_RULES["db_qualifier"]
        assert rules["type_suffix"] == DEFAULT_RULES["type_suffix"]

    def test_comments_and_blanks_skipped(self, tmp_path):
        """Lines starting with '#' and blank lines are ignored."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "# This is a comment\n\nleading_commas=OFF\n  \n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["leading_commas"] == "OFF"

    def test_case_insensitive_values(self, tmp_path):
        """Severity values are case-insensitive (normalised to uppercase)."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "leading_commas=off\nkeyword_case=Warning\ntype_suffix=error\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["leading_commas"] == "OFF"
        assert rules["keyword_case"] == "WARNING"
        assert rules["type_suffix"] == "ERROR"

    def test_invalid_severity_ignored(self, tmp_path):
        """Invalid severity values are ignored — default is kept."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "leading_commas=BANANA\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        # Default should be preserved
        assert rules["leading_commas"] == DEFAULT_RULES["leading_commas"]

    def test_custom_rule_accepted(self, tmp_path):
        """Unknown rule names are accepted (future-proofing)."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "my_custom_rule=ERROR\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["my_custom_rule"] == "ERROR"

    def test_missing_config_raises(self, tmp_path):
        """Missing config file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            read_inspect_config(str(tmp_path / "missing.conf"))

    def test_default_config_includes_every_default_rule(self):
        """Every rule in DEFAULT_RULES must appear as a key in the
        generated inspect.conf. Catches rules that are registered
        but never exposed to users for configuration.

        This test never needs updating when new rules are added —
        DEFAULT_RULES is the source of truth and the test derives
        from it.
        """
        content = generate_default_config()
        missing = [rule for rule in DEFAULT_RULES if f"{rule}=" not in content]
        assert not missing, (
            f"Rules registered in DEFAULT_RULES but missing from "
            f"generate_default_config() output: {missing}"
        )

    def test_default_config_only_references_registered_rules(self):
        """Every rule key in the generated inspect.conf must exist in
        DEFAULT_RULES. Catches typos and stale entries in the template.

        Pairs with the test above — together they enforce a bidirectional
        consistency between DEFAULT_RULES and the inspect.conf template,
        without either side hard-coding a rule list.
        """
        content = generate_default_config()
        unregistered = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            rule_name = stripped.split("=", 1)[0].strip()
            if rule_name not in DEFAULT_RULES:
                unregistered.append(rule_name)
        assert not unregistered, (
            f"Rules in generate_default_config() output but not "
            f"in DEFAULT_RULES: {unregistered}"
        )

    def test_all_default_severities_are_valid(self):
        """Every default severity in DEFAULT_RULES is one of the
        recognised values. Catches typos like 'WARN' or 'WARMING'."""
        valid_severities = {"ERROR", "WARNING", "OFF"}
        invalid = {
            rule: sev
            for rule, sev in DEFAULT_RULES.items()
            if sev not in valid_severities
        }
        assert not invalid, (
            f"Rules in DEFAULT_RULES with invalid severities "
            f"(must be one of {sorted(valid_severities)}): {invalid}"
        )


# ---------------------------------------------------------------
# generate_default_config
# ---------------------------------------------------------------


class TestGenerateDefaultConfig:
    """Tests for default inspect.conf generation."""

    def test_contains_all_rules(self):
        """Generated config contains all default rules."""
        content = generate_default_config()
        for rule_name in DEFAULT_RULES:
            assert rule_name in content

    def test_each_rule_line_has_correct_default_severity(self):
        """For each registered rule, the generated config has a line
        ``rule_name=DEFAULT_SEVERITY``. Stronger than just checking
        that severity strings appear somewhere — pins down that each
        rule's specific default is what the template emits.

        Derives from DEFAULT_RULES so it never needs updating when
        rules are added.
        """
        content = generate_default_config()
        wrong = []
        for rule, severity in DEFAULT_RULES.items():
            expected_line = f"{rule}={severity}"
            if expected_line not in content:
                wrong.append((rule, severity))
        assert not wrong, (
            f"Rules whose generated config line does not match DEFAULT_RULES: {wrong}"
        )

    def test_roundtrip(self, tmp_path):
        """Generated config can be read back and matches defaults."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(generate_default_config(), encoding="utf-8")

        rules = read_inspect_config(str(conf))

        for rule, severity in DEFAULT_RULES.items():
            assert rules[rule] == severity


# ---------------------------------------------------------------
# Rule config integration (OFF / severity override / strict)
# ---------------------------------------------------------------


class TestRuleConfigIntegration:
    """Integration tests for configurable rule severity."""

    def test_off_rule_produces_no_issues(self, tmp_path):
        """A rule set to OFF produces no issues."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        # This DDL has trailing commas — would normally produce a warning
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (\n"
            "    Id INTEGER,\n"
            "    Name VARCHAR(100),\n"
            "    Email VARCHAR(200),\n"
            "    Phone VARCHAR(20),\n"
            "    Created DATE\n"
            ");\n",
            encoding="utf-8",
        )

        rules = dict(DEFAULT_RULES)
        rules["leading_commas"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules)

        comma_issues = [i for i in result.issues if i.rule == "leading_commas"]
        assert comma_issues == []

    def test_warning_promoted_to_error_in_strict(self, tmp_path):
        """WARNING rules become ERROR in strict mode."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        # Missing SET/MULTISET — default severity is WARNING
        (ddl_dir / "{{DB}}.T.tbl").write_text(
            "CREATE TABLE {{DB}}.T (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path), strict=True)

        multiset_issues = [i for i in result.issues if i.rule == "set_multiset"]
        assert len(multiset_issues) == 1
        assert multiset_issues[0].severity == "ERROR"

    def test_off_rule_stays_off_in_strict(self, tmp_path):
        """OFF rules remain off even in strict mode."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (\n"
            "    Id INTEGER,\n"
            "    Name VARCHAR(100),\n"
            "    Email VARCHAR(200),\n"
            "    Phone VARCHAR(20),\n"
            "    Created DATE\n"
            ");\n",
            encoding="utf-8",
        )

        rules = dict(DEFAULT_RULES)
        rules["leading_commas"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules, strict=True)

        comma_issues = [i for i in result.issues if i.rule == "leading_commas"]
        assert comma_issues == []

    def test_error_override(self, tmp_path):
        """A rule set to ERROR produces ERROR-severity issues."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE TABLE {{DB}}.T (Id INTEGER);",
            encoding="utf-8",
        )

        rules = dict(DEFAULT_RULES)
        rules["set_multiset"] = "ERROR"  # Promote from default WARNING
        result = validate_directory(str(tmp_path), rules_config=rules)

        multiset_issues = [i for i in result.issues if i.rule == "set_multiset"]
        assert len(multiset_issues) == 1
        assert multiset_issues[0].severity == "ERROR"

    def test_default_config_no_crash(self, tmp_path):
        """validate_directory with no config uses defaults without crashing."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 1
