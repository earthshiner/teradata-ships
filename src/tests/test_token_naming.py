"""
test_token_naming.py — token_naming convention rule (#172).

A DDL object's database token should carry the kind suffix matching its type:
tables (+ indexes/triggers) in {{*_T}}, views in {{*_V}}. Only a clear T/V
mismatch is flagged; tokens without a kind suffix and the site-configurable
macro/procedure/function kinds are left alone.
"""

from __future__ import annotations

from td_release_packager.validate import (
    DEFAULT_RULES,
    _check_token_naming,
    validate_directory,
)


def _rules(content: str):
    return _check_token_naming("DDL/x.sql", content)


class TestMismatches:
    def test_view_in_t_token_flagged(self):
        issues = _rules("CREATE VIEW {{OMR_STD_T}}.MyView AS SELECT 1;")
        assert len(issues) == 1
        assert issues[0].rule == "token_naming"
        assert issues[0].severity == "WARNING"
        assert "_V" in issues[0].message

    def test_table_in_v_token_flagged(self):
        issues = _rules(
            "CREATE MULTISET TABLE {{OMR_STD_V}}.Customer (x INT) PRIMARY INDEX (x);"
        )
        assert len(issues) == 1
        assert "_T" in issues[0].message

    def test_remediation_present(self):
        i = _rules("CREATE VIEW {{X_T}}.V AS SELECT 1;")[0]
        assert i.remediation["requires_human_review"] is True
        assert "_V" in i.remediation["recommended_action"]


class TestNoFalsePositives:
    def test_view_in_v_token_clean(self):
        assert _rules("CREATE VIEW {{OMR_STD_V}}.MyView AS SELECT 1;") == []

    def test_table_in_t_token_clean(self):
        assert (
            _rules(
                "CREATE MULTISET TABLE {{OMR_STD_T}}.Customer (x INT) "
                "PRIMARY INDEX (x);"
            )
            == []
        )

    def test_token_without_kind_suffix_ignored(self):
        # No kind suffix → the project may not use the convention; no opinion.
        assert _rules("CREATE VIEW {{OMR_STD}}.MyView AS SELECT 1;") == []

    def test_literal_db_without_suffix_ignored(self):
        assert _rules("CREATE VIEW OMR_DB.MyView AS SELECT 1;") == []

    def test_macro_kind_not_checked(self):
        # Macro kind is site-configurable (default _T) — not flagged here even
        # in a _T token, to avoid false positives.
        assert _rules("CREATE MACRO {{OMR_STD_T}}.m AS (SELECT 1;);") == []

    def test_trigger_in_t_token_clean(self):
        # Triggers fire on tables → belong in _T.
        sql = "CREATE TRIGGER {{OMR_STD_T}}.trg AFTER INSERT ON {{OMR_STD_T}}.t"
        assert _rules(sql) == []


class TestConfig:
    def test_registered_warning_by_default(self):
        assert DEFAULT_RULES["token_naming"] == "WARNING"

    def test_validate_directory_flags(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DDL" / "views"
        d.mkdir(parents=True)
        (d / "{{X_T}}.v.viw").write_text(
            "CREATE VIEW {{X_T}}.v AS SELECT 1 AS a;", encoding="utf-8"
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert any(i.rule == "token_naming" for i in res.issues)

    def test_off_disables(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DDL" / "views"
        d.mkdir(parents=True)
        (d / "{{X_T}}.v.viw").write_text(
            "CREATE VIEW {{X_T}}.v AS SELECT 1 AS a;", encoding="utf-8"
        )
        rules = dict(DEFAULT_RULES)
        rules["token_naming"] = "OFF"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        assert not any(i.rule == "token_naming" for i in res.issues)
