"""
test_data_dependent_change.py — data_dependent_change lint rule (#170).

Covers detection of ALTER TABLE / CREATE UNIQUE INDEX operations whose
success depends on existing data, the DEFAULT exemption for NOT NULL,
the CREATE-TABLE (new, empty) exemption, the procedure-body exemption,
precheck + live-metadata remediation, and inspect.conf control.
"""

from __future__ import annotations

from td_release_packager.validate import (
    DEFAULT_RULES,
    _check_data_dependent_change,
    validate_directory,
)


def _rules(content: str):
    return _check_data_dependent_change("DDL/x.sql", content)


def _kinds(content: str):
    return [i.message for i in _rules(content)]


class TestDetection:
    def test_add_not_null_without_default(self):
        issues = _rules("ALTER TABLE MyDB.Customer ADD status INTEGER NOT NULL;")
        assert len(issues) == 1
        i = issues[0]
        assert i.rule == "data_dependent_change"
        assert i.severity == "WARNING"
        assert "NOT NULL" in i.message
        assert "MyDB.Customer" in i.message

    def test_add_not_null_with_default_is_exempt(self):
        # A DEFAULT makes the NOT NULL add safe on existing rows.
        sql = "ALTER TABLE MyDB.Customer ADD status INTEGER NOT NULL DEFAULT 0;"
        assert _rules(sql) == []

    def test_add_unique_constraint(self):
        issues = _rules("ALTER TABLE db.t ADD CONSTRAINT u UNIQUE (email);")
        assert any("UNIQUE" in i.message for i in issues)

    def test_create_unique_index(self):
        issues = _rules("CREATE UNIQUE INDEX (email) ON db.Customer;")
        assert len(issues) == 1
        assert "UNIQUE INDEX" in issues[0].message
        assert "db.Customer" in issues[0].message

    def test_add_check_constraint(self):
        issues = _rules("ALTER TABLE db.t ADD CONSTRAINT c CHECK (age >= 0);")
        assert any("CHECK" in i.message for i in issues)

    def test_primary_index_change(self):
        issues = _rules("ALTER TABLE db.t MODIFY PRIMARY INDEX (new_col);")
        assert any("PRIMARY INDEX" in i.message for i in issues)

    def test_line_number(self):
        sql = "CREATE MULTISET TABLE db.t (x INT);\n\nALTER TABLE db.t ADD y INT NOT NULL;"
        issues = _rules(sql)
        assert len(issues) == 1
        assert issues[0].line == 3

    def test_remediation_flags_live_metadata_and_precheck(self):
        i = _rules("ALTER TABLE db.t ADD y INT NOT NULL;")[0]
        assert i.remediation["requires_live_metadata"] is True
        assert i.remediation["requires_human_review"] is True
        assert i.remediation["recommended_precheck"]
        assert "SELECT" in i.remediation["recommended_precheck"]


class TestExemptions:
    def test_create_table_with_not_null_clean(self):
        # New, empty table — NOT NULL columns are fine.
        sql = "CREATE MULTISET TABLE db.t (id INTEGER NOT NULL, name VARCHAR(10));"
        assert _rules(sql) == []

    def test_plain_add_nullable_column_clean(self):
        assert _rules("ALTER TABLE db.t ADD note VARCHAR(100);") == []

    def test_alter_inside_procedure_body_exempt(self):
        sql = (
            "REPLACE PROCEDURE db.p()\n"
            "BEGIN\n"
            "    ALTER TABLE db.t ADD y INTEGER NOT NULL;\n"
            "END;"
        )
        assert _rules(sql) == []


class TestDefaultsAndConfig:
    def test_registered_as_warning_by_default(self):
        assert DEFAULT_RULES["data_dependent_change"] == "WARNING"

    def test_validate_directory_flags(self, tmp_path):
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.t.tbl").write_text(
            "ALTER TABLE db.t ADD y INTEGER NOT NULL;", encoding="utf-8"
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert any(i.rule == "data_dependent_change" for i in res.issues)

    def test_error_when_configured(self, tmp_path):
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.t.tbl").write_text(
            "ALTER TABLE db.t ADD y INTEGER NOT NULL;", encoding="utf-8"
        )
        rules = dict(DEFAULT_RULES)
        rules["data_dependent_change"] = "ERROR"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        ddc = [i for i in res.issues if i.rule == "data_dependent_change"]
        assert ddc and ddc[0].severity == "ERROR"

    def test_off_disables(self, tmp_path):
        ddl = tmp_path / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True)
        (ddl / "db.t.tbl").write_text(
            "ALTER TABLE db.t ADD y INTEGER NOT NULL;", encoding="utf-8"
        )
        rules = dict(DEFAULT_RULES)
        rules["data_dependent_change"] = "OFF"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        assert not any(i.rule == "data_dependent_change" for i in res.issues)
