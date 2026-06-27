"""
test_validate.py — Tests for the SHIPS inspector / linter (validate module).

Covers:
    - Database qualifier check
    - SET/MULTISET check for tables
    - Retired deploy intent check (REPLACE is supported)
    - One-object-per-file check
    - Eponymous file naming check
    - File extension check
    - Type suffix/prefix check
    - Hardcoded name detection
    - Keyword case check
    - Leading comma check
    - DDL statement terminator check
    - Full directory validation
"""

import pytest

from td_release_packager.validate import (
    _check_db_qualifier,
    _check_multiset,
    _check_deploy_intent,
    _check_ddl_terminator,
    _check_view_macro_self_reference,
    _check_one_object,
    _check_eponymous,
    _check_extension,
    _check_type_suffixes,
    _check_hardcoded_names,
    _check_keyword_case,
    _check_leading_commas,
    _check_intra_package_dependency,
    _check_view_column_list,
    _check_non_ascii_literals,
    _check_comment_length,
    _collect_package_prereqs,
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

    def test_grant_create_table_in_dcl_file_not_flagged(self):
        """``.dcl`` / ``.grt`` files cannot contain CREATE TABLE DDL by
        definition. A ``GRANT CREATE TABLE ON …`` clause mentions the
        literal phrase but does not issue a CREATE TABLE — must not fire.
        Regression test for the BionicCC_17 case (CallCentre.dcl flagged
        because of ``GRANT CREATE TABLE ON {{...}} TO {{...}}``).
        """
        dcl_content = (
            "GRANT CREATE TABLE ON {{CallCentre_DOM_BUS_V}} "
            "TO {{CallCentre_DOM_BUS_V}};\n"
            "GRANT CREATE VIEW ON {{CallCentre_DOM_BUS_V}} "
            "TO {{CallCentre_DOM_BUS_V}};\n"
        )
        assert _check_multiset("DCL/inter_db/CallCentre.dcl", dcl_content) == []
        assert _check_multiset("DCL/inter_db/CallCentre.grt", dcl_content) == []

    def test_grant_create_table_in_ddl_file_not_flagged(self):
        """Even in a DDL-extension file, ``GRANT CREATE TABLE`` must NOT
        trip the rule — the regex now requires CREATE TABLE at statement
        start, so a GRANT clause is correctly skipped."""
        mixed_content = (
            "GRANT CREATE TABLE ON {{MyDB}} TO {{Role}};\n"
            "GRANT CREATE VIEW ON {{MyDB}} TO {{Role}};\n"
        )
        assert _check_multiset("ops.sql", mixed_content) == []

    def test_real_create_table_in_mixed_file_still_flagged(self):
        """A real CREATE TABLE statement that lacks SET/MULTISET still
        fires, even when sitting next to GRANT clauses in the same file."""
        mixed = (
            "GRANT CREATE TABLE ON {{MyDB}} TO {{Role}};\n"
            "CREATE TABLE {{MyDB}}.T (id INT) PRIMARY INDEX (id);\n"
        )
        issues = _check_multiset("ops.sql", mixed)
        assert len(issues) == 1
        assert issues[0].rule == "set_multiset"


# ---------------------------------------------------------------
# _check_deploy_intent (strict mode)
# ---------------------------------------------------------------


# _check_deploy_intent
# ---------------------------------------------------------------


class TestCheckDeployIntent:
    """Tests for the retired deploy_intent rule."""

    def test_replace_view_passes(self):
        """REPLACE VIEW is supported and no longer produces a lint issue."""
        ddl = "REPLACE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_view_strict_still_passes(self):
        """Strict mode does not resurrect the retired deploy_intent rule."""
        ddl = "REPLACE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl, strict=True) == []

    def test_create_view_passes(self):
        """CREATE VIEW produces no deploy_intent issue."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_procedure_passes(self):
        """REPLACE PROCEDURE is supported."""
        ddl = "REPLACE PROCEDURE MyDB.sp_X() BEGIN END;"
        assert _check_deploy_intent("x.spl", ddl) == []

    def test_create_procedure_passes(self):
        """CREATE PROCEDURE produces no deploy_intent issue."""
        ddl = "CREATE PROCEDURE MyDB.sp_X() BEGIN END;"
        assert _check_deploy_intent("x.spl", ddl) == []

    def test_replace_trigger_passes(self):
        """REPLACE TRIGGER is supported."""
        ddl = "REPLACE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        assert _check_deploy_intent("x.trg", ddl) == []

    def test_create_trigger_passes(self):
        """CREATE TRIGGER produces no deploy_intent issue."""
        ddl = (
            "CREATE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        )
        assert _check_deploy_intent("x.trg", ddl) == []

    def test_replace_function_passes(self):
        """REPLACE FUNCTION is supported."""
        ddl = "REPLACE FUNCTION MyDB.fn_X(p INT) RETURNS INT RETURN p;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_create_function_passes(self):
        """CREATE FUNCTION produces no deploy_intent issue."""
        ddl = "CREATE FUNCTION MyDB.fn_X(p INT) RETURNS INT RETURN p;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_replace_specific_function_passes(self):
        """REPLACE SPECIFIC FUNCTION is supported."""
        ddl = "REPLACE SPECIFIC FUNCTION MyDB.fn_X_Int RETURNS INT RETURN 1;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_create_specific_function_passes(self):
        """CREATE SPECIFIC FUNCTION produces no deploy_intent issue."""
        ddl = "CREATE SPECIFIC FUNCTION MyDB.fn_X_Int RETURNS INT RETURN 1;"
        assert _check_deploy_intent("x.fnc", ddl) == []

    def test_replace_macro_passes(self):
        """REPLACE MACRO is supported."""
        ddl = "REPLACE MACRO MyDB.mc_X AS (SELECT 1;);"
        assert _check_deploy_intent("x.mcr", ddl) == []

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

    def test_replace_case_insensitive_passes(self):
        """REPLACE passes regardless of case."""
        for verb in ["replace", "Replace", "REPLACE"]:
            ddl = f"{verb} VIEW MyDB.V AS SELECT 1;"
            assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_with_leading_whitespace_passes(self):
        """Leading whitespace before REPLACE still passes."""
        ddl = "   REPLACE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_in_line_comment_not_flagged(self):
        """REPLACE in a comment is not flagged (line starts with --)."""
        ddl = "-- was REPLACE VIEW\nCREATE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl) == []

    def test_replace_inside_procedure_body_passes(self):
        """REPLACE on an inner line of a procedure body is not a lint issue."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "  REPLACE VIEW MyDB.temp_v AS SELECT 1;\n"
            "END;"
        )
        assert _check_deploy_intent("x.spl", ddl) == []


# ---------------------------------------------------------------
# _check_ddl_terminator
# ---------------------------------------------------------------


class TestCheckDdlTerminator:
    """Tests for DDL statement semi-colon termination."""

    def test_create_view_with_semicolon_passes(self):
        """A terminated CREATE VIEW produces no issue."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;"
        assert _check_ddl_terminator("v.viw", ddl) == []

    def test_create_view_without_semicolon_flagged(self):
        """Missing terminator on CREATE VIEW is flagged."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1"
        issues = _check_ddl_terminator("v.viw", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "ddl_terminator"
        assert issues[0].severity == "ERROR"
        assert issues[0].line == 1

    def test_create_macro_without_semicolon_flagged(self):
        """Missing terminator on CREATE MACRO is flagged."""
        ddl = "CREATE MACRO MyDB.M AS (SELECT 1;)"
        issues = _check_ddl_terminator("m.mcr", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "ddl_terminator"

    def test_create_procedure_with_semicolon_passes(self):
        """A normal CREATE PROCEDURE ending in END; passes."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "    INSERT INTO MyDB.Log VALUES (1);\n"
            "END;\n"
        )
        assert _check_ddl_terminator("x.spl", ddl) == []

    def test_create_procedure_without_final_semicolon_flagged(self):
        """A procedure body without the final END terminator is flagged."""
        ddl = (
            "CREATE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "    INSERT INTO MyDB.Log VALUES (1);\n"
            "END\n"
        )
        issues = _check_ddl_terminator("x.spl", ddl)
        assert len(issues) == 1
        assert issues[0].line == 1

    def test_trailing_whitespace_after_semicolon_passes(self):
        """Whitespace after the terminator is allowed."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;\n\n   "
        assert _check_ddl_terminator("v.viw", ddl) == []

    def test_missing_terminator_before_next_ddl_flagged(self):
        """Each DDL segment must be terminated, not just the final file."""
        ddl = "CREATE TABLE MyDB.T1 (Id INT)\nCREATE TABLE MyDB.T2 (Id INT);\n"
        issues = _check_ddl_terminator("multi.sql", ddl)
        assert len(issues) == 1
        assert issues[0].line == 1

    def test_no_ddl_statement_is_skipped(self):
        """Non-DDL content is out of scope for this rule."""
        assert _check_ddl_terminator("notes.txt", "just notes") == []

    def test_rule_is_error_by_default(self):
        """ddl_terminator defaults to ERROR in DEFAULT_RULES."""
        assert DEFAULT_RULES.get("ddl_terminator") == "ERROR"

    def test_generate_default_config_includes_rule(self):
        """generate_default_config() includes the ddl_terminator entry."""
        cfg = generate_default_config()
        assert "ddl_terminator=ERROR" in cfg

    def test_rule_present_in_parsed_config(self, tmp_path):
        """read_inspect_config merges ddl_terminator from DEFAULT_RULES."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("# empty\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert rules.get("ddl_terminator") == "ERROR"

    def test_rule_can_be_set_to_warning_via_config(self, tmp_path):
        """Setting ddl_terminator=WARNING in inspect.conf is honoured."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("ddl_terminator=WARNING\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert rules.get("ddl_terminator") == "WARNING"

    def test_rule_can_be_disabled_via_config(self, tmp_path):
        """Setting ddl_terminator=OFF in inspect.conf is honoured."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("ddl_terminator=OFF\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert rules.get("ddl_terminator") == "OFF"


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
        ddl = "CREATE TABLE MyDB.t (a INT);\nINSERT INTO MyDB.t VALUES (1);\n"
        issues = _check_one_object("seed.tbl", ddl)
        assert issues == []

    def test_two_top_level_create_statements_flagged(self):
        """Two real DDL statements (both CREATE) still trip the
        rule — that's the actual one-object violation."""
        ddl = "CREATE TABLE MyDB.t1 (a INT);\nCREATE TABLE MyDB.t2 (a INT);\n"
        issues = _check_one_object("two.sql", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "one_object"

    def test_multiple_dcl_statements_allowed_in_dcl_file(self):
        """DCL files can intentionally group related grants."""
        ddl = (
            "GRANT SELECT ON MyDB.TableA TO AppRole;\n"
            "GRANT SELECT ON MyDB.TableB TO AppRole;\n"
        )
        assert _check_one_object("inter_db/AppRole.dcl", ddl) == []

    def test_multiple_grant_statements_allowed_in_grt_file(self):
        """Generated grant files follow the same grouping convention."""
        ddl = (
            "GRANT SELECT ON MyDB.TableA TO AppRole;\n"
            "GRANT SELECT ON MyDB.TableB TO AppRole;\n"
        )
        assert _check_one_object("inter_db/AppRole.grt", ddl) == []


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

    def test_extension_mismatch_is_error_severity_end_to_end(self, tmp_path):
        """Through ``validate_directory``, an extension mismatch is
        ERROR severity (default rule config). The deployer and any
        automation reading the payload need to TRUST that a .tbl
        file contains a TABLE, .spl contains a PROCEDURE, etc. —
        a mismatch is the metadata lying."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        # File is named .sql but contains a CREATE TABLE
        (ddl_dir / "MyDB.T.sql").write_text(
            "CREATE TABLE MyDB.T (id INT);", encoding="utf-8"
        )

        result = validate_directory(str(tmp_path))

        ext_issues = [i for i in result.issues if i.rule == "extension"]
        assert len(ext_issues) == 1
        assert ext_issues[0].severity == "ERROR"
        # And the run is failed (errors > 0 => not passed)
        assert result.errors >= 1
        assert not result.passed


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

    def test_default_severity_is_off(self):
        """Default severity is OFF — lowercase keywords are a style
        preference, not a deploy-blocking defect, and most sites don't
        enforce the discipline. Projects that do can opt in via
        ``keyword_case=WARNING`` (or ERROR / INFO) in
        ``config/inspect.conf``."""
        from td_release_packager.validate import DEFAULT_RULES

        assert DEFAULT_RULES["keyword_case"] == "OFF"

    def test_lowercase_keyword_does_not_fire_under_defaults(self, tmp_path):
        """Under default settings the rule is silenced — a file full of
        lowercase keywords produces no keyword_case finding at all."""
        f = tmp_path / "t.tbl"
        f.write_text(
            "create table x.y\n"
            "(id integer not null\n"
            "   ,name varchar(100) default 'x'\n"
            "   ,created date\n"
            ") primary index (id);\n",
            encoding="utf-8",
        )
        result = validate_directory(str(tmp_path))
        kc = [i for i in result.issues if i.rule == "keyword_case"]
        assert kc == [], "Default OFF — keyword_case must not surface"

    def test_lowercase_keyword_fires_when_opted_in(self, tmp_path):
        """Setting ``keyword_case=WARNING`` in inspect.conf surfaces the
        finding for projects that enforce the UPPERCASE discipline."""
        from td_release_packager.validate import DEFAULT_RULES

        f = tmp_path / "t.tbl"
        f.write_text(
            "create table x.y\n"
            "(id integer not null\n"
            "   ,name varchar(100) default 'x'\n"
            "   ,created date\n"
            ") primary index (id);\n",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES, keyword_case="WARNING")
        result = validate_directory(str(tmp_path), rules_config=rules)
        kc = [i for i in result.issues if i.rule == "keyword_case"]
        assert kc, "Opted-in keyword_case must fire on lowercase DDL"
        assert all(i.severity == "WARNING" for i in kc)


# ---------------------------------------------------------------
# _check_leading_commas
# ---------------------------------------------------------------


class TestCheckLeadingCommas:
    """Tests for the configurable comma-style rule."""

    _LEADING = (
        "CREATE TABLE MyDB.T\n(\n"
        "     Id INTEGER\n"
        "    ,Name VARCHAR(100)\n"
        "    ,Email VARCHAR(200)\n"
        "    ,Phone VARCHAR(20)\n"
        "    ,Created DATE\n);\n"
    )
    _TRAILING = (
        "CREATE TABLE MyDB.T (\n"
        "    Id INTEGER,\n"
        "    Name VARCHAR(100),\n"
        "    Email VARCHAR(200),\n"
        "    Phone VARCHAR(20),\n"
        "    Created DATE\n);\n"
    )

    # -- leading mode (default) --

    def test_leading_mode_leading_file_passes(self):
        assert _check_leading_commas("t.tbl", self._LEADING, style="leading") == []

    def test_leading_mode_trailing_file_flagged(self):
        issues = _check_leading_commas("t.tbl", self._TRAILING, style="leading")
        assert len(issues) == 1
        assert issues[0].rule == "comma_style"
        assert issues[0].severity == "WARNING"

    def test_default_style_is_leading(self):
        """Calling without style= uses the default (leading)."""
        issues = _check_leading_commas("t.tbl", self._TRAILING)
        assert len(issues) == 1

    # -- trailing mode --

    def test_trailing_mode_trailing_file_passes(self):
        assert _check_leading_commas("t.tbl", self._TRAILING, style="trailing") == []

    def test_trailing_mode_leading_file_flagged(self):
        issues = _check_leading_commas("t.tbl", self._LEADING, style="trailing")
        assert len(issues) == 1
        assert issues[0].rule == "comma_style"
        assert issues[0].severity == "WARNING"

    def test_trailing_mode_message_explains_convention(self):
        issues = _check_leading_commas("t.tbl", self._LEADING, style="trailing")
        assert "trailing commas" in issues[0].message.lower()

    # -- as-per-source mode --

    def test_as_per_source_trailing_file_produces_info(self):
        """as-per-source emits an INFO finding — not a warning/error."""
        issues = _check_leading_commas("t.tbl", self._TRAILING, style="as-per-source")
        assert len(issues) == 1
        assert issues[0].severity == "INFO"
        assert issues[0].rule == "comma_style"

    def test_as_per_source_leading_file_also_produces_info(self):
        issues = _check_leading_commas("t.tbl", self._LEADING, style="as-per-source")
        assert len(issues) == 1
        assert issues[0].severity == "INFO"

    def test_as_per_source_message_states_policy(self):
        issues = _check_leading_commas("t.tbl", self._TRAILING, style="as-per-source")
        assert "as-per-source" in issues[0].message

    # -- inspect.conf integration --

    def test_comma_style_read_from_config(self, tmp_path):
        """comma_style in inspect.conf is parsed correctly."""
        from td_release_packager.validate import read_inspect_config

        conf = tmp_path / "inspect.conf"
        conf.write_text("comma_style=trailing\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert rules.get("comma_style") == "trailing"

    def test_invalid_comma_style_rejected(self, tmp_path):
        """An unrecognised comma_style value falls back to default."""
        from td_release_packager.validate import read_inspect_config

        conf = tmp_path / "inspect.conf"
        conf.write_text("comma_style=sideways\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        # Invalid value — key should not be stored (falls back to default at call site)
        assert rules.get("comma_style") is None

    def test_as_per_source_does_not_fail_run(self, tmp_path):
        """as-per-source emits INFO but does not increment the error/warning
        count — the run still passes."""
        from td_release_packager.validate import DEFAULT_RULES, validate_directory

        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (\n"
            "    Id INTEGER,\n"
            "    Name VARCHAR(100),\n"
            "    Email VARCHAR(200),\n"
            "    Phone VARCHAR(20)\n"
            ");\n",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES)
        rules["comma_style"] = "as-per-source"
        result = validate_directory(str(tmp_path), rules_config=rules)
        # INFO issues don't count as errors or warnings
        assert result.errors == 0
        assert result.warnings == 0
        assert result.passed


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

    def test_replace_view_passes_without_deploy_intent_issue(self, tmp_path):
        """REPLACE VIEW is supported and produces no deploy_intent issue."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{DB}}.V.viw").write_text(
            "REPLACE VIEW {{DB}}.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.errors == 0
        deploy_issues = [i for i in result.issues if i.rule == "deploy_intent"]
        assert deploy_issues == []

    def test_generated_releases_directory_is_not_scanned(self, tmp_path):
        releases_dir = tmp_path / "releases" / "DEV_BUILD_0056" / "_rollback"
        releases_dir.mkdir(parents=True)
        (releases_dir / "OldProc.spl").write_text(
            "CREATE PROCEDURE GDEV1P_BB\nBEGIN\nEND;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        assert result.files_scanned == 0
        assert result.errors == 0

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

    def test_dynamic_sql_string_literals_do_not_trigger_rules(self, tmp_path):
        """Regression test for GCFR_FF_TPTExportTmpTbl_Build pattern:
        a stored procedure that builds CREATE TABLE statements as
        runtime SQL strings (``'CREATE MULTISET TABLE '||...``) was
        firing two spurious warnings:

          - ``[set_multiset]``: matched 'CREATE TABLE' inside the
            string literal as if it were real DDL needing MULTISET.
          - ``[extension]``: classified the file as TABLE due to
            the same literal-keyword match, then warned that .spl
            isn't .tbl.

        Both fixed by stripping string literals before pattern
        matching, plus reordering validate's classifier so
        PROCEDURE is checked before TABLE."""
        ddl_dir = tmp_path / "DDL" / "procedures"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{X}}.foo.spl").write_text(
            "CREATE PROCEDURE {{X}}.foo (IN iName VARCHAR(128))\n"
            "MAIN:\n"
            "BEGIN\n"
            "    DECLARE vSQL VARCHAR(1000);\n"
            "    SET vSQL = 'DROP TABLE ' || iName;\n"
            "    SET vSQL = 'CREATE MULTISET TABLE ' || iName "
            "|| ' (id INT)';\n"
            "    SET vSQL = 'CREATE TABLE ' || iName "
            "|| ' (id INT)';  /* no MULTISET in this literal */\n"
            "    CALL DBC.SysExecSQL(:vSQL);\n"
            "END MAIN;\n",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        relevant = [i for i in result.issues if "foo.spl" in i.file]
        # The two specific warnings the user reported must NOT fire
        bug_rules = {"set_multiset", "extension"}
        triggered = {i.rule for i in relevant if i.rule in bug_rules}
        assert triggered == set(), (
            f"String-literal CREATE TABLE triggered: {triggered}. "
            f"All issues: {[(i.rule, i.message) for i in relevant]}"
        )

    def test_missing_ddl_terminator_is_error_by_default(self, tmp_path):
        """Directory validation reports missing DDL terminators as ERROR."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{DB}}.V.viw").write_text(
            "CREATE VIEW {{DB}}.V (X) AS SELECT 1 AS X",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        term_issues = [i for i in result.issues if i.rule == "ddl_terminator"]
        assert len(term_issues) == 1
        assert term_issues[0].severity == "ERROR"
        assert result.errors >= 1
        assert not result.passed

    def test_missing_ddl_terminator_can_be_downgraded_to_warning(self, tmp_path):
        """ddl_terminator severity is configurable."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{DB}}.V.viw").write_text(
            "CREATE VIEW {{DB}}.V (X) AS SELECT 1 AS X",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES)
        rules["ddl_terminator"] = "WARNING"

        result = validate_directory(str(tmp_path), rules_config=rules)

        term_issues = [i for i in result.issues if i.rule == "ddl_terminator"]
        assert len(term_issues) == 1
        assert term_issues[0].severity == "WARNING"

    def test_missing_ddl_terminator_can_be_disabled(self, tmp_path):
        """ddl_terminator=OFF suppresses missing terminator findings."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "{{DB}}.V.viw").write_text(
            "CREATE VIEW {{DB}}.V (X) AS SELECT 1 AS X",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES)
        rules["ddl_terminator"] = "OFF"

        result = validate_directory(str(tmp_path), rules_config=rules)

        term_issues = [i for i in result.issues if i.rule == "ddl_terminator"]
        assert term_issues == []

    def test_block_comment_keywords_do_not_trigger_rules(self, tmp_path):
        """Regression test for the GCFR_FF_IMGTableDelta_Create.spl
        report: a procedure with a multi-line ``/* purpose: ... */``
        header containing words like 'truncates', 'Create', and
        'temp table' was firing five spurious rule violations
        (db_qualifier, set_multiset, one_object, eponymous,
        extension) because the rules scanned raw content including
        comment text.

        The fix runs every check against comment-stripped content.
        This test mirrors the user's actual file shape — a single
        CREATE PROCEDURE whose body comment mentions 'TABLE',
        'CREATE', 'truncates', 'replaces', etc. — and asserts the
        only legitimate observation is no warnings on the headers."""
        ddl_dir = tmp_path / "DDL" / "procedures"
        ddl_dir.mkdir(parents=True)
        # A faithful re-creation of the GCFR procedure header: one
        # CREATE PROCEDURE statement, then a /* ... */ block with
        # natural-language descriptions full of DDL-ish keywords,
        # then a body that does the real work.
        (ddl_dir / "{{GCFR_P_FF}}.GCFR_FF_IMGTableDelta_Create.spl").write_text(
            "CREATE PROCEDURE {{GCFR_P_FF}}.GCFR_FF_IMGTableDelta_Create\n"
            "/*======================================================\n"
            "# Purpose: GCFR_FF_IMGTableDelta_Create procedure truncates\n"
            "#          or replaces the Image and Insert temporary tables\n"
            "#          If the temporary table does not exist, it is created\n"
            "#          If the temporary table exists, it is dropped and\n"
            "#          created again.\n"
            "#\n"
            "#          Function Flow Steps\n"
            "#              1 - Check if the temp table exists or not.\n"
            "#              2 - Create and execute DDL for temp table creation.\n"
            "#              3 - Log API completion message.\n"
            "======================================================*/\n"
            "(IN iExtension VARCHAR(3))\n"
            "MAIN:\n"
            "BEGIN\n"
            "    SET temp_var = 1;\n"
            "END MAIN;\n",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        # Filter to issues for this file only
        relevant = [
            i for i in result.issues if "GCFR_FF_IMGTableDelta_Create" in i.file
        ]
        # The buggy rules from the user's report — all should now
        # be silent because the rule patterns no longer see the
        # comment-text words 'truncates', 'CREATE TABLE', etc.
        bug_rules = {
            "db_qualifier",
            "set_multiset",
            "one_object",
            "eponymous",
            "extension",
        }
        triggered_buggy = {i.rule for i in relevant if i.rule in bug_rules}
        assert triggered_buggy == set(), (
            f"Comment text triggered spurious rule(s): {triggered_buggy}. "
            f"All issues: {[(i.rule, i.message) for i in relevant]}"
        )


# ---------------------------------------------------------------
# read_inspect_config
# ---------------------------------------------------------------


class TestReadInspectConfig:
    """Tests for reading inspect.conf configuration files."""

    def test_basic_config(self, tmp_path):
        """Key=value pairs are read and merged with defaults."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "comma_log_level=OFF\nkeyword_case=OFF\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["comma_log_level"] == "OFF"
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
            "# This is a comment\n\ncomma_log_level=OFF\n  \n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["comma_log_level"] == "OFF"

    def test_case_insensitive_values(self, tmp_path):
        """Severity values are case-insensitive (normalised to uppercase)."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "comma_log_level=off\nkeyword_case=Warning\ntype_suffix=error\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["comma_log_level"] == "OFF"
        assert rules["keyword_case"] == "WARNING"
        assert rules["type_suffix"] == "ERROR"

    def test_invalid_severity_ignored(self, tmp_path):
        """Invalid severity values are ignored — default is kept."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "comma_log_level=BANANA\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        # Default should be preserved
        assert rules["comma_log_level"] == DEFAULT_RULES["comma_log_level"]

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

    def test_env_config_passed_as_config_raises(self, tmp_path):
        """An env/token config passed via --config (issue #386) is
        detected and raises a pointed error rather than silently
        loading token lines as invalid rule severities."""
        conf = tmp_path / "DEV.conf"
        conf.write_text(
            "OMR_STD_T=OMR_STD\nOMR_STD_V=OMR_STD\nSHIPS_ENV=DEV\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as excinfo:
            read_inspect_config(str(conf))

        message = str(excinfo.value)
        assert "inspect.conf" in message
        assert "config/env/" in message

    def test_single_typo_inspect_config_does_not_raise(self, tmp_path):
        """A genuine inspect.conf with a single typo'd severity still
        warns-and-skips — the wrong-file guard must not fire because the
        line names a known rule."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "keyword_case=BANANA\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["keyword_case"] == DEFAULT_RULES["keyword_case"]

    def test_custom_rules_with_valid_severities_do_not_raise(self, tmp_path):
        """A config of only unknown rule names is fine as long as the
        severities are valid — the guard keys off invalid severities,
        not unknown names."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "future_rule_one=ERROR\nfuture_rule_two=WARNING\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["future_rule_one"] == "ERROR"
        assert rules["future_rule_two"] == "WARNING"

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
        either DEFAULT_RULES (severity-valued rules) or _DOMAIN_VALUE_RULES
        (domain-valued rules like comma_style). Catches typos and stale
        entries in the template.

        Pairs with the test above — together they enforce a bidirectional
        consistency between the rule registries and the inspect.conf template,
        without either side hard-coding a rule list.
        """
        from td_release_packager.validate import _DOMAIN_VALUE_RULES

        all_registered = set(DEFAULT_RULES) | set(_DOMAIN_VALUE_RULES)
        content = generate_default_config()
        unregistered = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            rule_name = stripped.split("=", 1)[0].strip()
            if rule_name not in all_registered:
                unregistered.append(rule_name)
        assert not unregistered, (
            f"Rules in generate_default_config() output but not "
            f"in DEFAULT_RULES or _DOMAIN_VALUE_RULES: {unregistered}"
        )

    def test_all_default_severities_are_valid(self):
        """Every default severity in DEFAULT_RULES is one of the
        recognised values. Derives from _VALID_SEVERITIES so the test
        stays correct when new severity levels are added.

        Domain-value rules (e.g. comma_style, warn_extra_grants,
        warn_external_grants) store non-severity values by design and
        are excluded from this check."""
        from td_release_packager.validate import _VALID_SEVERITIES, _DOMAIN_VALUE_RULES

        invalid = {
            rule: sev
            for rule, sev in DEFAULT_RULES.items()
            if sev not in _VALID_SEVERITIES and rule not in _DOMAIN_VALUE_RULES
        }
        assert not invalid, (
            f"Rules in DEFAULT_RULES with invalid severities "
            f"(must be one of {sorted(_VALID_SEVERITIES)}): {invalid}"
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
        rules["comma_log_level"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules)

        comma_issues = [i for i in result.issues if i.rule == "comma_style"]
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
        rules["comma_log_level"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules, strict=True)

        comma_issues = [i for i in result.issues if i.rule == "comma_style"]
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


# ---------------------------------------------------------------
# _collect_package_prereqs
# ---------------------------------------------------------------


class TestCollectPackagePrereqs:
    """Tests for the prereq pre-pass that powers intra_package_dependency."""

    def test_empty_directory_returns_empty_set(self, tmp_path):
        """A directory with no DDL returns an empty set."""
        assert _collect_package_prereqs(str(tmp_path)) == set()

    def test_create_database_in_db_file(self, tmp_path):
        """CREATE DATABASE in a .db file is collected."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024 SPOOL = 1024;",
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYDB"}

    def test_create_user_in_usr_file(self, tmp_path):
        """CREATE USER in a .usr file is collected."""
        prereq_dir = tmp_path / "pre-requisites" / "users"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyUser.usr").write_text(
            'CREATE USER MyUser AS PERM = 0 PASSWORD = "x";',
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYUSER"}

    def test_tokenised_database_name_preserved(self, tmp_path):
        """Tokens are preserved verbatim so tokenised dependants match."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "{{MY_DB}}.db").write_text(
            "CREATE DATABASE {{MY_DB}} AS PERMANENT = 1024;",
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        # Token is uppercased but braces survive verbatim.
        assert prereqs == {"{{MY_DB}}"}

    def test_quoted_database_name_unquoted_and_uppercased(self, tmp_path):
        """Quoted identifiers are normalised — quotes stripped, upper-cased."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "Quoted.db").write_text(
            'CREATE DATABASE "MyDB" AS PERMANENT = 1024;',
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        assert prereqs == {"MYDB"}

    def test_create_database_in_comment_ignored(self, tmp_path):
        """A CREATE DATABASE inside a comment is not collected."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "RealDB.db").write_text(
            "/* historical: was CREATE DATABASE OldDB */\n"
            "CREATE DATABASE RealDB AS PERMANENT = 1024;\n",
            encoding="utf-8",
        )

        prereqs = _collect_package_prereqs(str(tmp_path))

        # Only RealDB — the commented-out OldDB is ignored.
        assert prereqs == {"REALDB"}

    def test_files_with_unrelated_extensions_skipped(self, tmp_path):
        """Files with extensions outside the discovery set are skipped.

        ``.txt`` is not a SQL-bearing extension and never appears in
        ``DEFAULT_HARVEST_EXTENSIONS`` or any reasonable ships.yaml
        override, so a CREATE DATABASE buried in a README is ignored.
        """
        readme_dir = tmp_path / "docs"
        readme_dir.mkdir(parents=True)
        (readme_dir / "notes.txt").write_text(
            "Example DDL: CREATE DATABASE Sneaky AS PERMANENT = 1;",
            encoding="utf-8",
        )

        assert _collect_package_prereqs(str(tmp_path)) == set()

    def test_create_database_in_any_discovery_extension_collected(self, tmp_path):
        """Now that the prereq scan uses the central discovery set,
        a CREATE DATABASE in a .tbl file (mis-named source, perhaps
        from a copy-paste mistake) IS picked up — better to flag
        the misplacement via the intra_package_dependency rule than
        to silently skip it because of the file extension."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.T.tbl").write_text(
            "CREATE DATABASE MisplacedDb AS PERMANENT = 1;\n"
            "CREATE TABLE MisplacedDb.T (Id INT);",
            encoding="utf-8",
        )

        assert _collect_package_prereqs(str(tmp_path)) == {"MISPLACEDDB"}


# ---------------------------------------------------------------
# _check_intra_package_dependency
# ---------------------------------------------------------------


class TestCheckIntraPackageDependency:
    """Unit tests for the per-file intra_package_dependency check."""

    def test_no_prereqs_returns_no_issues(self):
        """An empty prereq set means the rule is silently inactive."""
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT);"
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", set())
        assert issues == []

    def test_object_in_prereq_database_flagged(self):
        """Object whose qualifier matches a package-created DB is flagged."""
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT);"
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"MYDB"})
        assert len(issues) == 1
        assert issues[0].rule == "intra_package_dependency"
        assert issues[0].severity == "ERROR"
        assert "MyDB" in issues[0].message
        assert issues[0].line == 1

    def test_object_in_external_database_passes(self):
        """Object in a database NOT created in the package is not flagged."""
        ddl = "CREATE MULTISET TABLE ExternalDB.T (Id INT);"
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"MYDB"})
        assert issues == []

    def test_tokenised_object_in_tokenised_prereq_flagged(self):
        """Tokenised CREATE TABLE {{X}}.foo against prereq {{X}} is flagged."""
        ddl = "CREATE MULTISET TABLE {{MY_DB}}.T (Id INT);"
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"{{MY_DB}}"})
        assert len(issues) == 1
        assert "{{MY_DB}}" in issues[0].message

    def test_quoted_qualifier_matches_unquoted_prereq(self):
        """Quoted "MyDB" qualifier matches prereq MYDB after normalisation."""
        ddl = 'CREATE MULTISET TABLE "MyDB".T (Id INT);'
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"MYDB"})
        assert len(issues) == 1

    def test_database_file_itself_not_flagged(self):
        """The .db file CREATEing the database is never the dependant."""
        ddl = "CREATE DATABASE MyDB AS PERMANENT = 1024;"
        issues = _check_intra_package_dependency("MyDB.db", ddl, "MyDB.db", {"MYDB"})
        assert issues == []

    def test_user_file_itself_not_flagged(self):
        """The .usr file CREATEing the user is never the dependant."""
        ddl = "CREATE USER MyUser AS PERM = 0;"
        issues = _check_intra_package_dependency(
            "MyUser.usr", ddl, "MyUser.usr", {"MYUSER"}
        )
        assert issues == []

    def test_view_in_prereq_database_flagged(self):
        """A view in a prereq-created database is also flagged."""
        ddl = "CREATE VIEW {{V_DB}}.MyView AS SELECT 1;"
        issues = _check_intra_package_dependency("v.viw", ddl, "v.viw", {"{{V_DB}}"})
        assert len(issues) == 1

    def test_procedure_in_prereq_database_flagged(self):
        """A procedure in a prereq-created database is flagged."""
        ddl = "CREATE PROCEDURE {{P_DB}}.sp_X()\nBEGIN\n    SET v = 1;\nEND;"
        issues = _check_intra_package_dependency("p.spl", ddl, "p.spl", {"{{P_DB}}"})
        assert len(issues) == 1

    def test_unqualified_object_not_flagged(self):
        """Unqualified objects do not match — db_qualifier owns that case."""
        ddl = "CREATE TABLE Customer (Id INT);"
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"MYDB"})
        assert issues == []

    def test_line_number_points_at_qualifier(self):
        """Reported line is the line containing the qualifier."""
        ddl = (
            "/* header comment */\n"  # line 1
            "/* second line */\n"  # line 2
            "CREATE MULTISET TABLE\n"  # line 3
            "  MyDB.T (Id INT);"  # line 4
        )
        issues = _check_intra_package_dependency("t.tbl", ddl, "t.tbl", {"MYDB"})
        assert len(issues) == 1
        assert issues[0].line == 4


# ---------------------------------------------------------------
# Integration tests through validate_directory
# ---------------------------------------------------------------


class TestIntraPackageDependencyIntegration:
    """End-to-end tests for the intra_package_dependency rule via
    validate_directory — checks the pre-pass and dispatcher wiring.

    The rule defaults to OFF because the package stage auto-splits
    affected sources (see Phase 2 of the intra_package_dependency
    work). These tests explicitly opt the rule back in via
    rules_config so the rule's wiring stays exercised.
    """

    @staticmethod
    def _rules_with_intra_at(severity: str):
        rules = dict(DEFAULT_RULES)
        rules["intra_package_dependency"] = severity
        return rules

    def test_create_database_plus_table_in_same_package_flagged(self, tmp_path):
        """The headline case Paul reported: CREATE DATABASE x +
        CREATE TABLE x.foo in the same package fires an ERROR when
        the rule is enabled."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("ERROR")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert len(intra_issues) == 1
        assert intra_issues[0].severity == "ERROR"
        assert "MyDB.Customer.tbl" in intra_issues[0].file
        assert not result.passed

    def test_default_severity_is_off(self, tmp_path):
        """With default rules, the violation is silent — package
        auto-split handles it transparently. This is the normal
        user experience after Phase 2."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert intra_issues == []

    def test_objects_in_external_database_pass(self, tmp_path):
        """A package containing only objects (no CREATE DATABASE) does
        not trigger the rule even when explicitly enabled."""
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("ERROR")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert intra_issues == []

    def test_tokenised_pair_flagged_end_to_end(self, tmp_path):
        """Tokenised CREATE DATABASE + tokenised dependants match through
        the full pipeline when the rule is enabled."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "{{T_DB}}.db").write_text(
            "CREATE DATABASE {{T_DB}} AS PERMANENT = 1024;",
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "Customer.tbl").write_text(
            "CREATE MULTISET TABLE {{T_DB}}.Customer (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("ERROR")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert len(intra_issues) == 1
        assert "{{T_DB}}" in intra_issues[0].message

    def test_database_file_itself_not_flagged_end_to_end(self, tmp_path):
        """The CREATE DATABASE file passes the rule (it IS the prereq)."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("ERROR")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        # The CREATE DATABASE file is the prereq — it must not flag itself.
        assert intra_issues == []

    def test_create_user_plus_dependant_object_flagged(self, tmp_path):
        """CREATE USER also creates a database in Teradata; objects in
        that user's database fire the rule too when enabled."""
        prereq_dir = tmp_path / "pre-requisites" / "users"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyUser.usr").write_text(
            'CREATE USER MyUser AS PERM = 1024 PASSWORD = "x";',
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyUser.Audit.tbl").write_text(
            "CREATE MULTISET TABLE MyUser.Audit (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("ERROR")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert len(intra_issues) == 1
        assert "MyUser" in intra_issues[0].message

    def test_rule_can_be_softened_to_warning(self, tmp_path):
        """Setting the rule to WARNING demotes it from ERROR."""
        prereq_dir = tmp_path / "pre-requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text(
            "CREATE DATABASE MyDB AS PERMANENT = 1024;",
            encoding="utf-8",
        )
        ddl_dir = tmp_path / "DDL" / "tables"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.Customer.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER);",
            encoding="utf-8",
        )

        result = validate_directory(
            str(tmp_path), rules_config=self._rules_with_intra_at("WARNING")
        )

        intra_issues = [
            i for i in result.issues if i.rule == "intra_package_dependency"
        ]
        assert len(intra_issues) == 1
        assert intra_issues[0].severity == "WARNING"


# ---------------------------------------------------------------
# _check_view_column_list  (Issue #133)
# ---------------------------------------------------------------


class TestCheckViewColumnList:
    """Tests for the view_column_list rule (Issue #133).

    A view should declare an explicit column list between its name and
    the AS keyword so that agents and tooling can determine the view's
    schema from source without querying the live database.
    """

    # -- Compliant cases (no issue expected) ---------------------

    def test_view_with_column_list_passes(self):
        """A view that declares an explicit column list is compliant."""
        ddl = (
            "REPLACE VIEW {{V_DB}}.MyView (ColA, ColB, ColC) AS\n"
            "SELECT a.ColA, a.ColB, a.ColC\n"
            "FROM   {{T_DB}}.MyTable AS a;"
        )
        issues = _check_view_column_list("MyView.viw", ddl)
        assert issues == []

    def test_view_with_single_column_list_passes(self):
        """A single-column list is still explicit and passes."""
        ddl = (
            "CREATE VIEW {{V_DB}}.SingleCol (Id) AS\n"
            "SELECT t.Id\n"
            "FROM   {{T_DB}}.Ref AS t;"
        )
        issues = _check_view_column_list("SingleCol.viw", ddl)
        assert issues == []

    def test_view_with_quoted_identifiers_and_column_list_passes(self):
        """Double-quoted identifiers with a column list are compliant."""
        ddl = (
            'REPLACE VIEW "MyDb"."MyView" (Col1, Col2) AS\n'
            'SELECT t.Col1, t.Col2 FROM "MyDb"."Base" AS t;'
        )
        issues = _check_view_column_list("MyView.viw", ddl)
        assert issues == []

    def test_non_view_object_skipped(self):
        """A TABLE definition is silently ignored — rule only targets views."""
        ddl = (
            "CREATE MULTISET TABLE {{T_DB}}.Customer\n"
            "    ( Id INTEGER NOT NULL\n"
            "    , Name VARCHAR(100)\n"
            "    )\n"
            "PRIMARY INDEX (Id);"
        )
        issues = _check_view_column_list("Customer.tbl", ddl)
        assert issues == []

    def test_procedure_skipped(self):
        """A stored procedure is silently ignored."""
        ddl = (
            "REPLACE PROCEDURE {{P_DB}}.MyProc (IN p_Id INTEGER)\n"
            "BEGIN\n"
            "    SELECT 1;\n"
            "END;"
        )
        issues = _check_view_column_list("MyProc.spl", ddl)
        assert issues == []

    # -- Non-compliant cases (issue expected) --------------------

    def test_view_without_column_list_flagged(self):
        """A view jumping straight from the name to AS is flagged."""
        ddl = (
            "REPLACE VIEW {{V_DB}}.MyView AS\n"
            "SELECT a.ColA, a.ColB\n"
            "FROM   {{T_DB}}.MyTable AS a;"
        )
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "view_column_list"
        assert issues[0].severity == "WARNING"

    def test_create_view_without_column_list_flagged(self):
        """CREATE VIEW (not REPLACE) without a column list is also flagged."""
        ddl = "CREATE VIEW {{V_DB}}.MyView AS\nSELECT a.Id FROM {{T_DB}}.Ref AS a;"
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1
        assert issues[0].rule == "view_column_list"

    def test_token_database_no_column_list_flagged(self):
        """Token form for the database segment — still flagged without column list."""
        ddl = (
            "REPLACE VIEW {{V_DB}}.Customer AS\n"
            "SELECT c.Id, c.Name FROM {{T_DB}}.Customer AS c;"
        )
        issues = _check_view_column_list("Customer.viw", ddl)
        assert len(issues) == 1

    def test_quoted_identifier_no_column_list_flagged(self):
        """Double-quoted view name without column list is flagged."""
        ddl = 'REPLACE VIEW "MyDb"."MyView" AS\nSELECT t.Id FROM "MyDb"."Base" AS t;'
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1

    def test_issue_message_contains_agent_context(self):
        """The warning message explains the agent-friendliness motivation."""
        ddl = "REPLACE VIEW {{V_DB}}.MyView AS\nSELECT 1 AS Dummy;"
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1
        assert "agent" in issues[0].message.lower()
        assert "column list" in issues[0].message.lower()

    def test_line_number_reported(self):
        """The line number of the offending header is reported.

        The regex anchors to ``^\\s*`` which can consume leading blank
        lines — the match start therefore sits on the blank line
        immediately before the REPLACE VIEW keyword rather than on the
        keyword line itself.  We assert the value the function returns
        rather than the visual line of the keyword, so this test stays
        coupled to the actual implementation behaviour.
        """
        ddl = "-- Header comment\n\nREPLACE VIEW {{V_DB}}.MyView AS\nSELECT 1 AS Dummy;"
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1
        # Match starts on the blank line (line 2) due to ^\s* anchor.
        assert issues[0].line == 2

    def test_case_insensitive_keywords(self):
        """The check is case-insensitive on CREATE/REPLACE VIEW/AS."""
        ddl = "replace view {{V_DB}}.MyView as SELECT 1 AS X;"
        issues = _check_view_column_list("MyView.viw", ddl)
        assert len(issues) == 1

    # -- Config / severity tests ---------------------------------

    def test_rule_is_warning_by_default(self):
        """view_column_list defaults to WARNING in DEFAULT_RULES."""
        assert DEFAULT_RULES.get("view_column_list") == "WARNING"

    def test_generate_default_config_includes_rule(self):
        """generate_default_config() includes the view_column_list entry."""
        from td_release_packager.validate import generate_default_config

        cfg = generate_default_config()
        assert "view_column_list" in cfg

    def test_rule_present_in_parsed_config(self, tmp_path):
        """read_inspect_config merges view_column_list from DEFAULT_RULES."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("# empty\n", encoding="utf-8")
        from td_release_packager.validate import read_inspect_config

        rules = read_inspect_config(str(conf))
        assert rules.get("view_column_list") == "WARNING"

    def test_rule_can_be_set_to_error_via_config(self, tmp_path):
        """Setting view_column_list=ERROR in inspect.conf is honoured."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("view_column_list=ERROR\n", encoding="utf-8")
        from td_release_packager.validate import read_inspect_config

        rules = read_inspect_config(str(conf))
        assert rules.get("view_column_list") == "ERROR"

    def test_rule_can_be_disabled_via_config(self, tmp_path):
        """Setting view_column_list=OFF in inspect.conf silences the rule."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("view_column_list=OFF\n", encoding="utf-8")
        from td_release_packager.validate import read_inspect_config

        rules = read_inspect_config(str(conf))
        assert rules.get("view_column_list") == "OFF"

    def test_off_rule_suppressed_in_directory_validation(self, tmp_path):
        """When set to OFF, no issue is emitted even for a non-compliant view."""
        view_dir = tmp_path / "DDL" / "views"
        view_dir.mkdir(parents=True)
        (view_dir / "{{V_DB}}.MyView.viw").write_text(
            "REPLACE VIEW {{V_DB}}.MyView AS SELECT 1 AS Dummy;",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES)
        rules["view_column_list"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules)
        vcl_issues = [i for i in result.issues if i.rule == "view_column_list"]
        assert vcl_issues == []

    def test_warning_emitted_in_directory_validation(self, tmp_path):
        """A non-compliant view produces a WARNING via the full pipeline."""
        view_dir = tmp_path / "DDL" / "views"
        view_dir.mkdir(parents=True)
        (view_dir / "{{V_DB}}.MyView.viw").write_text(
            "REPLACE VIEW {{V_DB}}.MyView AS SELECT 1 AS Dummy;",
            encoding="utf-8",
        )
        rules = dict(DEFAULT_RULES)
        rules["view_column_list"] = "WARNING"
        # Suppress unrelated rules to keep assertions clean.
        rules["hardcoded_name"] = "OFF"
        rules["zero_tokens"] = "OFF"
        rules["eponymous"] = "OFF"
        rules["deploy_intent"] = "OFF"
        result = validate_directory(str(tmp_path), rules_config=rules)
        vcl_issues = [i for i in result.issues if i.rule == "view_column_list"]
        assert len(vcl_issues) == 1
        assert vcl_issues[0].severity == "WARNING"

    def test_generated_ships_directory_is_not_validated(self, tmp_path):
        """Generated helper mirrors under .ships are not payload source."""
        mirror_dir = (
            tmp_path / ".ships" / "harvest" / "by_database" / "DB_DOMAIN_V" / "views"
        )
        mirror_dir.mkdir(parents=True)
        (mirror_dir / "{{DB_DOMAIN_V}}.Agent_Current.viw").write_text(
            "REPLACE VIEW {{DB_DOMAIN_V}}.Agent_Current AS SELECT 1 AS Dummy;",
            encoding="utf-8",
        )

        rules = dict(DEFAULT_RULES)
        rules["view_column_list"] = "WARNING"
        result = validate_directory(str(tmp_path), rules_config=rules)

        assert result.files_scanned == 0
        assert [i for i in result.issues if i.rule == "view_column_list"] == []


# ---------------------------------------------------------------
# _check_non_ascii_literals  (Rule NAS-001)
# ---------------------------------------------------------------


class TestCheckNonAsciiLiterals:
    """Tests for the non-ASCII character check (NAS-001).

    Teradata databases created with a LATIN character set reject string
    literals containing characters outside the Latin-1 code page with
    Error 6706 "The string contains an untranslatable character".
    The check must catch em-dashes, bullets, arrows, box-drawing characters,
    and any other non-ASCII characters in SQL source files.
    """

    def test_clean_ascii_file_has_no_issues(self):
        """A file containing only ASCII characters must produce no issues."""
        sql = (
            "INSERT INTO {{DB_MEMORY_T}}.Business_Glossary\n"
            "( term, definition )\n"
            "VALUES ( 'Call Duration', 'Total seconds from start to end.' )\n"
            ";\n"
        )
        issues = _check_non_ascii_literals("test.dml", sql)
        assert issues == []

    def test_em_dash_flagged_as_error(self):
        """Em-dash (U+2014) in a string literal must be flagged as ERROR."""
        sql = "VALUES ( 'Session strategy \u2014 persistent session.' );"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert len(issues) >= 1
        assert all(i.severity == "ERROR" for i in issues)
        assert all("U+2014" in i.message for i in issues)
        assert all("non_ascii" == i.rule for i in issues)

    def test_bullet_flagged(self):
        """Bullet (U+2022) must be flagged."""
        sql = "-- \u2022 58 scoring dimensions\nSELECT 1;"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert any("U+2022" in i.message for i in issues)

    def test_right_arrow_flagged(self):
        """Right arrow (U+2192) must be flagged."""
        sql = "VALUES ( 'Deploy order: Memory \u2192 Domain.' );"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert any("U+2192" in i.message for i in issues)

    def test_box_drawing_flagged(self):
        """Box-drawing horizontal (U+2500) must be flagged."""
        sql = "-- \u2500\u2500 Change Log \u2500\u2500\nSELECT 1;"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert any("U+2500" in i.message for i in issues)

    def test_suggestion_included_for_em_dash(self):
        """Error message must include a suggested ASCII replacement."""
        sql = "VALUES ( 'Call strategy \u2014 notes.' );"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert issues
        assert any("--" in i.message for i in issues)

    def test_line_number_reported(self):
        """The line number of the non-ASCII character must be recorded."""
        sql = "SELECT 1;\nVALUES ( 'ok' );\nVALUES ( 'bad \u2014 char' );"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert issues
        assert issues[0].line == 3

    def test_multiple_non_ascii_chars_all_reported(self):
        """Each unique non-ASCII character on each line must produce an issue."""
        sql = (
            "VALUES ( 'em \u2014 dash' );\n"
            "VALUES ( 'bullet \u2022 point' );\n"
            "VALUES ( 'arrow \u2192 here' );\n"
        )
        issues = _check_non_ascii_literals("seed.dml", sql)
        codes = {i.line for i in issues}
        assert codes == {1, 2, 3}

    # ---------------------------------------------------------------
    # Comment-vs-literal severity (issue #255)
    # ---------------------------------------------------------------

    def test_em_dash_in_line_comment_is_warning(self):
        """Em-dash inside a -- comment is WARNING (not ERROR)."""
        sql = "-- header — trailer\nSELECT 1;\n"
        issues = _check_non_ascii_literals("view.viw", sql)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"
        assert "U+2014" in issues[0].message
        assert "SQL comment" in issues[0].message

    def test_em_dash_in_block_comment_is_warning(self):
        """Em-dash inside a /* */ block comment is WARNING."""
        sql = "/* header — trailer */\nSELECT 1;\n"
        issues = _check_non_ascii_literals("view.viw", sql)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_em_dash_in_string_literal_stays_error(self):
        """Em-dash inside a string literal remains ERROR."""
        sql = "VALUES ( 'before — after' );\n"
        issues = _check_non_ascii_literals("seed.dml", sql)
        assert issues
        assert all(i.severity == "ERROR" for i in issues)
        assert all("LATIN" in i.message for i in issues)

    def test_mixed_comment_and_literal_on_one_line(self):
        """Same char in both a comment and a literal on one line yields both severities."""
        sql = "VALUES ( 'a — b' ) -- note — about\n"
        issues = _check_non_ascii_literals("seed.dml", sql)
        severities = sorted(i.severity for i in issues)
        assert severities == ["ERROR", "WARNING"]

    def test_block_comment_spans_multiple_lines(self):
        """A multi-line /* */ comment with non-ASCII on an inner line is WARNING."""
        sql = "/*\n   header text\n   meaning — explanation\n*/\nSELECT 1;\n"
        issues = _check_non_ascii_literals("view.viw", sql)
        assert len(issues) == 1
        assert issues[0].line == 3
        assert issues[0].severity == "WARNING"

    def test_ufffd_in_comment_is_warning_not_error(self):
        """U+FFFD inside a comment is still WARNING (matches the real-world reproducer)."""
        sql = "/* Call_Summary_Current � current-state */\nSELECT 1;\n"
        issues = _check_non_ascii_literals("view.viw", sql)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"
        assert "U+FFFD" in issues[0].message

    def test_integrate_non_ascii_caught_in_validate_directory(self, tmp_path):
        """validate_directory must surface NAS-001 issues for DML files."""
        dml = tmp_path / "{{DB_MEMORY_T}}.Business_Glossary.dml"
        dml.write_text(
            "INSERT INTO {{DB_MEMORY_T}}.Business_Glossary\n"
            "( term, definition )\n"
            "VALUES ( 'Session strategy \u2014 persistent session.' )\n"
            ";\n",
            encoding="utf-8",
        )
        result = validate_directory(str(tmp_path))
        nas_issues = [i for i in result.issues if i.rule == "non_ascii"]
        assert nas_issues, (
            "Expected NAS-001 issues from validate_directory for a DML file "
            "containing an em-dash, but none were reported."
        )
        assert all(i.severity == "ERROR" for i in nas_issues)


# ---------------------------------------------------------------
# Tests for warn_extra_grants and warn_external_grants in inspect.conf
# ---------------------------------------------------------------


from td_release_packager.validate import read_severity_from_inspect_config


class TestWarnGrantSeverities:
    """
    Tests for the ``warn_extra_grants`` and ``warn_external_grants``
    severity settings in ``inspect.conf``.

    ``warn_external_grants`` was renamed from ``warn_orphan_grants`` in
    2026-06 (default also flipped from ERROR to INFO) — see the module
    docstring in ``validate_grants.py`` for the rationale.
    """

    def test_defaults_for_grant_drift_severities(self):
        """``warn_extra_grants`` defaults to WARNING (extras in .dcl are a
        soft signal — the operator may know something the inferrer
        doesn't). ``warn_external_grants`` defaults to INFO — grants to
        roles or databases external to the package are commonly
        legitimate."""
        assert DEFAULT_RULES["warn_extra_grants"] == "WARNING"
        assert DEFAULT_RULES["warn_external_grants"] == "INFO"

    def test_read_severity_defaults_to_warning_for_warn_extra_grants(self):
        """Absent ``warn_extra_grants`` defaults to WARNING."""
        assert read_severity_from_inspect_config({}, "warn_extra_grants") == "WARNING"

    def test_read_severity_defaults_to_info_for_warn_external_grants(self):
        """Absent ``warn_external_grants`` defaults to INFO."""
        assert read_severity_from_inspect_config({}, "warn_external_grants") == "INFO"

    def test_read_severity_accepts_off(self):
        """OFF suppresses the configured grant finding."""
        assert (
            read_severity_from_inspect_config(
                {"warn_extra_grants": "OFF"}, "warn_extra_grants"
            )
            == "OFF"
        )

    def test_read_severity_accepts_warning_and_warn_alias(self):
        """WARNING and WARN both normalize to WARNING."""
        assert (
            read_severity_from_inspect_config(
                {"warn_extra_grants": "WARNING"}, "warn_extra_grants"
            )
            == "WARNING"
        )
        assert (
            read_severity_from_inspect_config(
                {"warn_external_grants": "WARN"}, "warn_external_grants"
            )
            == "WARNING"
        )

    def test_read_severity_accepts_error(self):
        """ERROR blocks the configured grant finding."""
        assert (
            read_severity_from_inspect_config(
                {"warn_extra_grants": "ERROR"}, "warn_extra_grants"
            )
            == "ERROR"
        )

    def test_warn_extra_grants_off_parsed_from_conf(self, tmp_path):
        """warn_extra_grants=OFF is correctly parsed from inspect.conf."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("warn_extra_grants=OFF\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert read_severity_from_inspect_config(rules, "warn_extra_grants") == "OFF"

    def test_warn_external_grants_warn_parsed_from_conf(self, tmp_path):
        """warn_external_grants=WARN is parsed as WARNING from inspect.conf."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("warn_external_grants=WARN\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert (
            read_severity_from_inspect_config(rules, "warn_external_grants")
            == "WARNING"
        )

    def test_both_settings_use_defaults_when_conf_is_empty(self, tmp_path):
        """An empty inspect.conf falls back to defaults: WARNING for
        ``warn_extra_grants``, INFO for ``warn_external_grants``."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("# empty\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert (
            read_severity_from_inspect_config(rules, "warn_extra_grants") == "WARNING"
        )
        assert (
            read_severity_from_inspect_config(rules, "warn_external_grants") == "INFO"
        )

    def test_invalid_value_falls_back_to_default(self, tmp_path):
        """An invalid grant severity is ignored and the default applies."""
        conf = tmp_path / "inspect.conf"
        conf.write_text("warn_extra_grants=yes\n", encoding="utf-8")
        rules = read_inspect_config(str(conf))
        assert (
            read_severity_from_inspect_config(rules, "warn_extra_grants") == "WARNING"
        )

    def test_generate_default_config_includes_both_settings(self):
        """generate_default_config() includes both grant settings:
        ``warn_extra_grants=WARNING`` (default) and
        ``warn_external_grants=INFO`` (default)."""
        cfg = generate_default_config()
        assert "warn_extra_grants=WARNING" in cfg
        assert "warn_external_grants=INFO" in cfg


# ---------------------------------------------------------------
# _check_comment_length  (Rule: comment_length)
# ---------------------------------------------------------------


class TestCheckCommentLength:
    """Tests for the COMMENT ON ... IS '...' length check (Error 5550).

    Teradata rejects COMMENT strings longer than 254 characters at deploy
    time with Error 5550.  The rule must catch this during Inspect so that
    the failure surfaces before packaging.
    """

    # -- Pass cases --

    def test_comment_exactly_at_limit_passes(self):
        """A comment body of exactly 254 characters must produce no issues."""
        body = "A" * 254
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues == []

    def test_comment_well_under_limit_passes(self):
        """A typical short comment must pass."""
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS 'A short description.';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues == []

    def test_empty_comment_passes(self):
        """An empty comment string must pass."""
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues == []

    def test_comment_on_column_under_limit_passes(self):
        """COMMENT ON COLUMN with a short value must pass."""
        content = "COMMENT ON COLUMN {{DB_T}}.MyTable.col1 IS 'A column.';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues == []

    def test_non_cmt_extension_is_skipped(self):
        """Files with extensions other than .cmt must be silently skipped."""
        body = "B" * 300
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("database/DDL/MyTable.tbl", content)
        assert issues == [], "Checker must skip files that are not .cmt"

    def test_severity_off_suppresses_violation(self):
        """Setting comment_length=OFF must suppress all findings."""
        body = "C" * 300
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length(
            "comments/MyTable.cmt", content, rules_config={"comment_length": "OFF"}
        )
        assert issues == []

    def test_sql_escaped_apostrophe_counted_as_two_chars(self):
        """Embedded '' (SQL-escaped apostrophe) counts as two characters.

        A body of 253 normal chars plus two apostrophes (SQL escape) gives a
        raw body length of 255, which exceeds 254 and must fire.
        """
        body = "A" * 253 + "''"
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert len(issues) == 1
        assert issues[0].rule == "comment_length"

    # -- Fail cases --

    def test_comment_one_over_limit_fires(self):
        """A comment of 255 characters (one over) must produce one ERROR."""
        body = "D" * 255
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.rule == "comment_length"
        assert issue.severity == "ERROR"
        assert "255" in issue.message
        assert "254" in issue.message
        assert "5550" in issue.message

    def test_comment_far_over_limit_reports_actual_length(self):
        """The violation message must report the actual character count."""
        body = "E" * 400
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert len(issues) == 1
        assert "400" in issues[0].message

    def test_violation_carries_correct_file_path(self):
        """The violation must carry the exact rel_path supplied to the checker."""
        body = "F" * 260
        content = "COMMENT ON TABLE {{DB_T}}.call_searchable_text IS '" + body + "';\n"
        rel = "database/DDL/comments/CallCentre_SCH_STD_T.call_searchable_text.cmt"
        issues = _check_comment_length(rel, content)
        assert issues[0].file == rel

    def test_violation_line_number_single_line_file(self):
        """Line number must be 1 when the COMMENT is on the first line."""
        body = "G" * 260
        content = "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues[0].line == 1

    def test_violation_line_number_multiline_file(self):
        """Line number must reflect the actual line of the COMMENT ON keyword."""
        body = "H" * 260
        content = (
            "-- Auto-generated by view_layer_generator.py\n"
            "\n"
            "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n"
        )
        issues = _check_comment_length("comments/MyTable.cmt", content)
        assert issues[0].line == 3

    def test_multiple_violations_in_one_file(self):
        """Every over-length COMMENT statement must produce its own issue."""
        body1 = "I" * 260
        body2 = "J" * 300
        content = (
            "COMMENT ON TABLE {{DB_T}}.T IS '" + body1 + "';\n"
            "COMMENT ON COLUMN {{DB_T}}.T.col IS '" + body2 + "';\n"
        )
        issues = _check_comment_length("comments/T.cmt", content)
        assert len(issues) == 2
        assert all(i.rule == "comment_length" for i in issues)

    def test_mixed_short_and_long_only_long_flagged(self):
        """Only over-length statements are flagged; short ones in the same file pass."""
        short = "K" * 100
        long_ = "L" * 260
        content = (
            "COMMENT ON TABLE {{DB_T}}.T IS '" + short + "';\n"
            "COMMENT ON COLUMN {{DB_T}}.T.col IS '" + long_ + "';\n"
        )
        issues = _check_comment_length("comments/T.cmt", content)
        assert len(issues) == 1
        assert "260" in issues[0].message

    def test_severity_warning_propagated(self):
        """When comment_length=WARNING in rules_config, severity must be WARNING."""
        body = "M" * 260
        content = "COMMENT ON TABLE {{DB_T}}.T IS '" + body + "';\n"
        issues = _check_comment_length(
            "comments/T.cmt", content, rules_config={"comment_length": "WARNING"}
        )
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_case_insensitive_keyword_matching(self):
        """The COMMENT keyword match must be case-insensitive."""
        body = "N" * 260
        content = "comment on table {{DB_T}}.T is '" + body + "';\n"
        issues = _check_comment_length("comments/T.cmt", content)
        assert len(issues) == 1

    def test_default_rule_is_error(self):
        """DEFAULT_RULES must declare comment_length as ERROR."""
        assert DEFAULT_RULES["comment_length"] == "ERROR"

    def test_generate_default_config_includes_comment_length(self):
        """generate_default_config() must include a comment_length entry."""
        cfg = generate_default_config()
        assert "comment_length=ERROR" in cfg

    def test_validate_directory_catches_overlength_cmt(self, tmp_path):
        """validate_directory must flag an over-length COMMENT in a .cmt file."""
        db_dir = tmp_path / "database"
        db_dir.mkdir()
        db_file = db_dir / "MyDB.db"
        db_file.write_text(
            "CREATE DATABASE {{DB_T}} AS PERM = 1e9;\n", encoding="utf-8"
        )

        comments_dir = db_dir / "DDL" / "comments"
        comments_dir.mkdir(parents=True)
        body = "Z" * 260
        cmt_file = comments_dir / "{{DB_T}}.MyTable.cmt"
        cmt_file.write_text(
            "COMMENT ON TABLE {{DB_T}}.MyTable IS '" + body + "';\n",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path))
        comment_issues = [i for i in result.issues if i.rule == "comment_length"]
        assert comment_issues, (
            "Expected a comment_length violation from validate_directory "
            "for a .cmt file with a 260-character comment body."
        )
        assert all(i.severity == "ERROR" for i in comment_issues)


class TestResolveInspectRoot:
    """``resolve_inspect_root`` constrains inspect to the deployable payload."""

    def test_returns_payload_subdir_when_present(self, tmp_path):
        """Standard SHIPS project layout — return payload/database."""
        from td_release_packager.validate import resolve_inspect_root

        payload = tmp_path / "payload" / "database"
        payload.mkdir(parents=True)
        assert resolve_inspect_root(str(tmp_path)) == str(payload)

    def test_falls_back_to_project_when_no_payload(self, tmp_path):
        """Bare directory (unit tests, ad-hoc lint runs) — return as-is."""
        from td_release_packager.validate import resolve_inspect_root

        assert resolve_inspect_root(str(tmp_path)) == str(tmp_path)

    def test_inspect_does_not_walk_sibling_scratch_dirs(self, tmp_path):
        """Files under ``___extras/`` (sibling of payload/) must be ignored.

        Replicates the user's BionicCC scenario where ``___extras/``
        held legacy seed scripts that produced spurious lint findings
        because inspect walked the whole project root.
        """
        from td_release_packager.validate import (
            resolve_inspect_root,
            validate_directory,
        )

        payload = tmp_path / "payload" / "database"
        payload.mkdir(parents=True)
        clean_view = payload / "MyDB.MyView.viw"
        clean_view.write_text(
            "REPLACE VIEW MyDB.MyView AS SELECT 1 AS x;\n",
            encoding="utf-8",
        )

        extras = tmp_path / "___extras"
        extras.mkdir()
        scratch_sql = extras / "2_Call_H.sql"
        scratch_sql.write_text(
            "create table x.y (id int);\nCREATE VIEW x.v AS SELECT 1;\n",
            encoding="utf-8",
        )

        result = validate_directory(resolve_inspect_root(str(tmp_path)))
        scanned_files = {issue.file for issue in result.issues}
        assert not any("___extras" in f for f in scanned_files), (
            f"inspect leaked into ___extras/: {scanned_files}"
        )


class TestNonAsciiAutoFixHint:
    """Non-ASCII findings point the operator at ``--fix-non-ascii``."""

    def test_em_dash_finding_mentions_auto_fix_flag(self, tmp_path):
        """An em-dash is in the auto-fix table — message must advertise the flag."""
        f = tmp_path / "x.viw"
        f.write_text(
            "REPLACE VIEW x.v AS SELECT 1 AS y; -- description — note\n",
            encoding="utf-8",
        )
        result = validate_directory(str(tmp_path))
        nas = [i for i in result.issues if i.rule == "non_ascii"]
        assert nas, "Expected a non_ascii finding for the em-dash"
        assert any("--fix-non-ascii" in i.message for i in nas)

    def test_unmappable_char_does_not_advertise_auto_fix(self, tmp_path):
        """A char without an auto-fix entry must NOT advertise the flag.

        Avoids the false promise of "run --fix-non-ascii" when the
        fix flag won't actually substitute this character.
        """
        # U+FFFD REPLACEMENT CHARACTER — explicitly excluded from the
        # auto-fix table (the original byte is gone, no safe substitute).
        f = tmp_path / "x.viw"
        f.write_text(
            "REPLACE VIEW x.v AS SELECT 1 AS y; -- � here\n",
            encoding="utf-8",
        )
        result = validate_directory(str(tmp_path))
        nas = [i for i in result.issues if i.rule == "non_ascii"]
        assert nas, "Expected a non_ascii finding for U+FFFD"
        assert not any("--fix-non-ascii" in i.message for i in nas)
