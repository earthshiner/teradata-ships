"""
test_dynamic_sql_categories.py — refined dynamic SQL risk categories (#166).

The single dynamic_sql finding is split into risk categories carried in each
finding's remediation: execute_immediate / calls_sys_exec_sql /
concatenates_literal / uses_unsanitised_parameter. Concatenating a
variable/parameter into executed SQL is the highest-risk category and sets
requires_human_review; every finding tells agents not to auto-remove dynamic
SQL.
"""

from __future__ import annotations

from td_release_packager.security_rules import scan_dynamic_sql
from td_release_packager.validate import DEFAULT_RULES, validate_directory


def _scan(sql: str):
    return scan_dynamic_sql("p.spl", sql, "p.spl")


def _cats(sql: str):
    return {i.remediation["risk_category"] for i in _scan(sql)}


class TestCategories:
    def test_execute_immediate_plain(self):
        issues = _scan("BEGIN\n EXECUTE IMMEDIATE v_sql;\nEND;")
        assert len(issues) == 1
        assert issues[0].remediation["risk_category"] == "dynamic_sql_execute_immediate"
        assert issues[0].remediation["requires_human_review"] is False

    def test_sysexecsql(self):
        assert _cats("BEGIN\n CALL DBC.SysExecSQL(:v);\nEND;") == {
            "dynamic_sql_calls_sys_exec_sql"
        }

    def test_execsql(self):
        assert _cats("BEGIN\n CALL DBC.ExecSQL(:v);\nEND;") == {
            "dynamic_sql_calls_sys_exec_sql"
        }

    def test_parameter_concatenation_is_unsanitised(self):
        issues = _scan(
            "BEGIN\n EXECUTE IMMEDIATE 'SELECT ' || p_col || ' FROM t';\nEND;"
        )
        assert issues[0].remediation["risk_category"] == (
            "dynamic_sql_uses_unsanitised_parameter"
        )
        assert issues[0].remediation["requires_human_review"] is True

    def test_literal_only_concatenation(self):
        issues = _scan("BEGIN\n EXECUTE IMMEDIATE 'SELECT ' || 'x';\nEND;")
        assert issues[0].remediation["risk_category"] == (
            "dynamic_sql_concatenates_literal"
        )
        assert issues[0].remediation["requires_human_review"] is False

    def test_cross_line_assembly_flagged(self):
        sql = (
            "BEGIN\n  SET v = 'DROP TABLE ' || iName;\n  CALL DBC.SysExecSQL(:v);\nEND;"
        )
        cats = _cats(sql)
        assert "dynamic_sql_uses_unsanitised_parameter" in cats
        assert "dynamic_sql_calls_sys_exec_sql" in cats


class TestAgentGuidanceAndScope:
    def test_agent_must_not_auto_remove(self):
        i = _scan("BEGIN\n EXECUTE IMMEDIATE v;\nEND;")[0]
        assert i.remediation["agent_may_fix"] is False
        assert i.remediation["safe_fix_available"] is False
        assert "do not" in i.message.lower()

    def test_rule_name_is_dynamic_sql(self):
        # One config key controls severity for all categories.
        assert all(i.rule == "dynamic_sql" for i in _scan("EXECUTE IMMEDIATE v;"))
        assert DEFAULT_RULES["dynamic_sql"] == "WARNING"

    def test_non_procedure_extension_skipped(self):
        assert scan_dynamic_sql("t.tbl", "EXECUTE IMMEDIATE v;", "t.tbl") == []

    def test_benign_concatenation_without_exec_not_flagged(self):
        # No dynamic-exec construct in the file → ordinary concatenation
        # must not be flagged.
        assert _scan("BEGIN\n SET msg = 'hello ' || who;\nEND;") == []


class TestIntegration:
    def test_via_validate_directory(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DDL" / "procedures"
        d.mkdir(parents=True)
        (d / "{{X}}.p.spl").write_text(
            "CREATE PROCEDURE {{X}}.p (IN iName VARCHAR(50))\n"
            "BEGIN\n"
            "  EXECUTE IMMEDIATE 'SELECT ' || iName || ' FROM {{X}}.t';\n"
            "END;",
            encoding="utf-8",
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        ds = [i for i in res.issues if i.rule == "dynamic_sql"]
        assert ds
        assert any(
            i.remediation
            and i.remediation.get("risk_category")
            == "dynamic_sql_uses_unsanitised_parameter"
            for i in ds
        )
