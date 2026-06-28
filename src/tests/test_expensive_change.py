"""
test_expensive_change.py — teradata_expensive_change rule (#175).

Flags operationally expensive physical changes (O(table size), lock-heavy):
ADD column with DEFAULT (full table rewrite) and CREATE non-unique secondary
index (full index build). Findings carry live-metadata / lock / DBA-review
metadata. Avoids double-flagging the data_dependent_change cases.
"""

from __future__ import annotations

from td_release_packager.validate import (
    DEFAULT_RULES,
    _check_expensive_change,
    validate_directory,
)


def _rules(content: str):
    return _check_expensive_change("DDL/x.sql", content)


class TestDetection:
    def test_add_column_with_default(self):
        issues = _rules("ALTER TABLE MyDB.Customer ADD status INTEGER DEFAULT 0;")
        assert len(issues) == 1
        i = issues[0]
        assert i.rule == "teradata_expensive_change"
        assert i.severity == "WARNING"
        assert "MyDB.Customer" in i.message
        assert "rewrite" in i.message.lower()

    def test_create_secondary_index(self):
        issues = _rules("CREATE INDEX (email) ON MyDB.Customer;")
        assert len(issues) == 1
        assert "index" in issues[0].message.lower()
        assert "MyDB.Customer" in issues[0].message

    def test_metadata_flags(self):
        i = _rules("ALTER TABLE db.t ADD c INT DEFAULT 1;")[0]
        r = i.remediation
        assert r["requires_live_metadata"] is True
        assert r["possible_lock_impact"] is True
        assert r["possible_spool_or_perm_impact"] is True
        assert r["requires_dba_review"] is True
        assert "SELECT" in r["recommended_precheck"]

    def test_line_number(self):
        sql = "CREATE MULTISET TABLE db.t (x INT);\n\nCREATE INDEX (x) ON db.t;"
        issues = _rules(sql)
        assert len(issues) == 1
        assert issues[0].line == 3


class TestNoDoubleFlagging:
    def test_unique_index_not_flagged_here(self):
        # CREATE UNIQUE INDEX is data_dependent_change's job, not this rule.
        assert _rules("CREATE UNIQUE INDEX (email) ON db.t;") == []

    def test_add_not_null_without_default_not_flagged_here(self):
        # That's data_dependent_change (data safety), not the expensive rule.
        assert _rules("ALTER TABLE db.t ADD c INT NOT NULL;") == []

    def test_plain_create_table_clean(self):
        assert _rules("CREATE MULTISET TABLE db.t (x INT) PRIMARY INDEX (x);") == []

    def test_add_with_default_inside_procedure_body_exempt(self):
        sql = (
            "REPLACE PROCEDURE db.p()\n"
            "BEGIN\n"
            "    ALTER TABLE db.t ADD c INT DEFAULT 0;\n"
            "END;"
        )
        assert _rules(sql) == []


class TestConfig:
    def test_registered_warning_by_default(self):
        assert DEFAULT_RULES["teradata_expensive_change"] == "WARNING"

    def test_validate_directory_flags(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DDL" / "tables"
        d.mkdir(parents=True)
        (d / "db.t.tbl").write_text(
            "ALTER TABLE db.t ADD c INT DEFAULT 0;", encoding="utf-8"
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert any(i.rule == "teradata_expensive_change" for i in res.issues)

    def test_off_disables(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DDL" / "tables"
        d.mkdir(parents=True)
        (d / "db.t.tbl").write_text("CREATE INDEX (x) ON db.t;", encoding="utf-8")
        rules = dict(DEFAULT_RULES)
        rules["teradata_expensive_change"] = "OFF"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        assert not any(i.rule == "teradata_expensive_change" for i in res.issues)
