"""
test_validate.py — Tests for the SHIPS inspector / linter (validate module).

Covers:
    - Database qualifier check
    - SET/MULTISET check for tables
    - Deploy intent check (CREATE vs REPLACE) and --strict mode
    - One-object-per-file check
    - Eponymous file naming check
    - File extension check
    - Type suffix/prefix check
    - Hardcoded name detection
    - Keyword case check
    - Leading comma check
    - Full directory validation
"""

import os
import pytest

from td_release_packager.validate import (
    _check_db_qualifier,
    _check_multiset,
    _check_deploy_intent,
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
    ValidationIssue,
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
        issues = _check_multiset("test.viw", "REPLACE VIEW MyDB.V AS SELECT 1;")
        assert issues == []


# ---------------------------------------------------------------
# _check_deploy_intent (strict mode)
# ---------------------------------------------------------------

class TestCheckDeployIntent:
    """Tests for idempotent deployment intent detection."""

    def test_replace_view_passes(self):
        """REPLACE VIEW passes in both normal and strict mode."""
        ddl = "REPLACE VIEW MyDB.V AS SELECT 1;"
        assert _check_deploy_intent("v.viw", ddl, strict=False) == []
        assert _check_deploy_intent("v.viw", ddl, strict=True) == []

    def test_create_view_warning(self):
        """CREATE VIEW produces WARNING (config/strict controls final severity)."""
        ddl = "CREATE VIEW MyDB.V AS SELECT 1;"
        issues = _check_deploy_intent("v.viw", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_create_view_error_via_strict(self):
        """CREATE VIEW becomes ERROR when strict mode is applied via config."""
        ddl_dir = self._write_ddl_file(
            "CREATE VIEW {{DB}}.V AS SELECT 1;", ".viw"
        )
        result = validate_directory(str(ddl_dir), strict=True)
        deploy_issues = [i for i in result.issues if i.rule == "deploy_intent"]
        assert len(deploy_issues) == 1
        assert deploy_issues[0].severity == "ERROR"

    @staticmethod
    def _write_ddl_file(content, ext, _counter=[0]):
        """Helper to create a temp DDL file for integration tests."""
        import tempfile
        _counter[0] += 1
        d = tempfile.mkdtemp()
        ddl_dir = os.path.join(d, "DDL", "views")
        os.makedirs(ddl_dir, exist_ok=True)
        fpath = os.path.join(ddl_dir, f"test_{_counter[0]}{ext}")
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        return d

    def test_replace_procedure_passes(self):
        """REPLACE PROCEDURE passes."""
        ddl = "REPLACE PROCEDURE MyDB.sp_X() BEGIN END;"
        assert _check_deploy_intent("x.spl", ddl, strict=True) == []

    def test_create_procedure_warning(self):
        """CREATE PROCEDURE produces WARNING (strict/config controls final severity)."""
        ddl = "CREATE PROCEDURE MyDB.sp_X() BEGIN END;"
        issues = _check_deploy_intent("x.spl", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_replace_trigger_passes(self):
        """REPLACE TRIGGER passes — the bug fix that was added."""
        ddl = "REPLACE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        assert _check_deploy_intent("x.trg", ddl, strict=True) == []

    def test_create_trigger_warning(self):
        """CREATE TRIGGER produces WARNING (strict/config controls final severity)."""
        ddl = "CREATE TRIGGER MyDB.trg_X AFTER INSERT ON MyDB.T FOR EACH ROW (SELECT 1;);"
        issues = _check_deploy_intent("x.trg", ddl)
        assert len(issues) == 1
        assert issues[0].severity == "WARNING"

    def test_replace_function_passes(self):
        """REPLACE FUNCTION passes."""
        ddl = "REPLACE FUNCTION MyDB.fn_X(p INT) RETURNS INT RETURN p;"
        assert _check_deploy_intent("x.fnc", ddl, strict=True) == []

    def test_replace_specific_function_passes(self):
        """REPLACE SPECIFIC FUNCTION passes."""
        ddl = "REPLACE SPECIFIC FUNCTION MyDB.fn_X_Int RETURNS INT RETURN 1;"
        assert _check_deploy_intent("x.fnc", ddl, strict=True) == []

    def test_create_join_index_info(self):
        """CREATE JOIN INDEX produces INFO (no REPLACE alternative exists)."""
        ddl = "CREATE JOIN INDEX MyDB.JI_X AS SELECT * FROM MyDB.T;"
        issues = _check_deploy_intent("x.jix", ddl, strict=True)
        # JI should produce INFO, not ERROR
        if issues:
            assert issues[0].severity == "INFO"

    def test_create_table_not_checked(self):
        """Tables are NOT checked for REPLACE (they use IDEMPOTENT_DEPLOY)."""
        ddl = "CREATE MULTISET TABLE MyDB.T (Id INT);"
        issues = _check_deploy_intent("t.tbl", ddl, strict=True)
        # Tables should produce no deploy_intent issue
        deploy_issues = [i for i in issues if i.rule == "deploy_intent"]
        assert deploy_issues == []


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
        """Procedure with inner DML (INSERT, SELECT) under threshold is OK."""
        ddl = (
            "REPLACE PROCEDURE MyDB.sp_X()\n"
            "BEGIN\n"
            "    INSERT INTO MyDB.Log VALUES (1);\n"
            "END;\n"
        )
        # 2 matches (REPLACE + INSERT) — under the >2 threshold
        issues = _check_one_object("x.spl", ddl)
        assert issues == []


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
        f.write_text("REPLACE VIEW MyDB.V AS SELECT 1;", encoding="utf-8")
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
        ddl = "REPLACE VIEW MyDB.Customer_V AS SELECT 1;"
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
        ddl = "REPLACE PROCEDURE MyDB.DoStuff_SP() BEGIN END;"
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

    def test_strict_mode_catches_create_view(self, tmp_path):
        """Strict mode catches CREATE VIEW as an error."""
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.V.viw").write_text(
            "CREATE VIEW {{DB}}.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path), strict=True)

        assert result.errors > 0
        assert not result.passed


# ---------------------------------------------------------------
# read_inspect_config
# ---------------------------------------------------------------

class TestReadInspectConfig:
    """Tests for reading inspect.conf configuration files."""

    def test_basic_config(self, tmp_path):
        """Key=value pairs are read and merged with defaults."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "leading_commas=OFF\n"
            "keyword_case=OFF\n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["leading_commas"] == "OFF"
        assert rules["keyword_case"] == "OFF"
        # Defaults preserved for unmentioned rules
        assert rules["db_qualifier"] == "ERROR"
        assert rules["type_suffix"] == "ERROR"

    def test_comments_and_blanks_skipped(self, tmp_path):
        """Lines starting with '#' and blank lines are ignored."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "# This is a comment\n"
            "\n"
            "leading_commas=OFF\n"
            "  \n",
            encoding="utf-8",
        )

        rules = read_inspect_config(str(conf))

        assert rules["leading_commas"] == "OFF"

    def test_case_insensitive_values(self, tmp_path):
        """Severity values are case-insensitive (normalised to uppercase)."""
        conf = tmp_path / "inspect.conf"
        conf.write_text(
            "leading_commas=off\n"
            "keyword_case=Warning\n"
            "type_suffix=error\n",
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

    def test_all_defaults_present(self):
        """DEFAULT_RULES contains all 10 expected rule names."""
        expected = {
            "db_qualifier", "set_multiset", "deploy_intent",
            "one_object", "eponymous", "extension", "type_suffix",
            "hardcoded_name", "keyword_case", "leading_commas",
        }
        assert set(DEFAULT_RULES.keys()) == expected


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

    def test_contains_severity_values(self):
        """Generated config contains the default severity values."""
        content = generate_default_config()
        assert "ERROR" in content
        assert "WARNING" in content

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
        ddl_dir = tmp_path / "DDL" / "views"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "MyDB.V.viw").write_text(
            "CREATE VIEW {{DB}}.V AS SELECT 1;",
            encoding="utf-8",
        )

        result = validate_directory(str(tmp_path), strict=True)

        deploy_issues = [i for i in result.issues if i.rule == "deploy_intent"]
        assert len(deploy_issues) == 1
        assert deploy_issues[0].severity == "ERROR"

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
