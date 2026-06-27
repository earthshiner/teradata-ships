"""
test_destructive_change.py — destructive_change lint rule (issue #169).

Covers detection of explicit destructive DDL in payload files (DROP /
DELETE DATABASE / ALTER ... DROP), the procedure-body exemption, line
numbers, remediation metadata, non-destructive statements left clean,
and inspect.conf OFF/severity control via validate_directory.
"""

from __future__ import annotations

from td_release_packager.validate import (
    DEFAULT_RULES,
    _check_destructive_change,
    validate_directory,
)


def _rules(content: str):
    return _check_destructive_change("DDL/x.sql", content)


class TestDetection:
    def test_drop_table_detected(self):
        issues = _rules("DROP TABLE MyDB.Customer;")
        assert len(issues) == 1
        i = issues[0]
        assert i.rule == "destructive_change"
        assert i.severity == "ERROR"
        assert i.line == 1
        assert "MyDB.Customer" in i.message
        assert "DROP TABLE" in i.message

    def test_all_drop_kinds_detected(self):
        for kind in (
            "TABLE",
            "VIEW",
            "MACRO",
            "PROCEDURE",
            "FUNCTION",
            "TRIGGER",
            "DATABASE",
            "USER",
        ):
            issues = _rules(f"DROP {kind} db.obj;")
            assert len(issues) == 1, kind
            assert f"DROP {kind}" in issues[0].message

    def test_drop_join_index_detected(self):
        issues = _rules("DROP JOIN INDEX db.ji;")
        assert len(issues) == 1
        assert "DROP JOIN INDEX" in issues[0].message

    def test_delete_database_detected(self):
        issues = _rules("DELETE DATABASE StagingDB;")
        assert len(issues) == 1
        assert "DELETE DATABASE" in issues[0].message
        assert "StagingDB" in issues[0].message

    def test_alter_table_drop_detected(self):
        issues = _rules("ALTER TABLE MyDB.Customer DROP COLUMN old_col;")
        assert len(issues) == 1
        assert "ALTER TABLE" in issues[0].message
        assert "MyDB.Customer" in issues[0].message

    def test_line_number_reported(self):
        sql = "CREATE MULTISET TABLE db.t (x INT);\n\nDROP TABLE db.old;"
        issues = _rules(sql)
        assert len(issues) == 1
        assert issues[0].line == 3

    def test_multiple_destructive_statements(self):
        sql = "DROP TABLE db.a;\nDROP VIEW db.b;"
        assert len(_rules(sql)) == 2

    def test_remediation_requires_human_review(self):
        i = _rules("DROP TABLE db.t;")[0]
        assert i.remediation["requires_human_review"] is True
        assert i.remediation["agent_may_fix"] is False
        assert i.remediation["automation_level"] == "manual_review_required"


class TestNonDestructive:
    def test_create_table_clean(self):
        assert _rules("CREATE MULTISET TABLE db.t (x INT) PRIMARY INDEX (x);") == []

    def test_replace_view_clean(self):
        # REPLACE is idempotent re-definition, not destructive.
        assert _rules("REPLACE VIEW db.v AS SELECT 1 AS x;") == []

    def test_drop_inside_procedure_body_exempt(self):
        # A procedure dropping a volatile/temp table inside BEGIN…END is
        # legitimate procedural logic, not a payload-level destructive DDL.
        sql = (
            "REPLACE PROCEDURE db.p()\n"
            "BEGIN\n"
            "    DROP TABLE tmp_work;\n"
            "    INSERT INTO db.t SELECT 1;\n"
            "END;"
        )
        assert _rules(sql) == []


class TestDefaultsAndConfig:
    def test_registered_as_error_by_default(self):
        assert DEFAULT_RULES["destructive_change"] == "ERROR"

    def test_validate_directory_flags_drop(self, tmp_path):
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.old.tbl").write_text("DROP TABLE db.old;", encoding="utf-8")
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert any(i.rule == "destructive_change" for i in res.issues)
        assert res.errors >= 1

    def test_off_disables_rule(self, tmp_path):
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.old.tbl").write_text("DROP TABLE db.old;", encoding="utf-8")
        rules = dict(DEFAULT_RULES)
        rules["destructive_change"] = "OFF"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        assert not any(i.rule == "destructive_change" for i in res.issues)

    def test_comment_only_drop_not_flagged(self, tmp_path):
        # The dispatch strips comments before content checks, so a DROP that
        # only appears in a comment must not trip the rule.
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.t.tbl").write_text(
            "-- TODO: DROP TABLE db.legacy once migrated\n"
            "CREATE MULTISET TABLE db.t (x INT) PRIMARY INDEX (x);",
            encoding="utf-8",
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert not any(i.rule == "destructive_change" for i in res.issues)
