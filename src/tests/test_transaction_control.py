"""
test_transaction_control.py — transaction_control_in_payload rule (#173).

Covers detection of BT/ET, BEGIN/END TRANSACTION, COMMIT, ROLLBACK in
payload files; the comment exemption; the procedure-body exemption (while
still catching standalone BEGIN TRANSACTION); phase/line reporting; and
inspect.conf severity control.
"""

from __future__ import annotations

from td_release_packager.validate import (
    DEFAULT_RULES,
    _check_transaction_control,
    validate_directory,
)


def _rules(content: str, path: str = "DDL/x.sql"):
    return _check_transaction_control(path, content)


def _kinds(content: str):
    return {i.message.split("(")[1].split(")")[0] for i in _rules(content)}


class TestDetection:
    def test_bt_et_detected(self):
        kinds = _kinds("BT;\nINSERT INTO db.t SELECT 1;\nET;")
        assert "BT" in kinds and "ET" in kinds

    def test_begin_end_transaction_detected(self):
        kinds = _kinds("BEGIN TRANSACTION;\nUPDATE db.t SET x=1;\nEND TRANSACTION;")
        assert "BEGIN TRANSACTION" in kinds and "END TRANSACTION" in kinds

    def test_commit_and_rollback_detected(self):
        assert "COMMIT" in _kinds("COMMIT;")
        assert "ROLLBACK" in _kinds("ROLLBACK;")

    def test_severity_and_rule(self):
        i = _rules("COMMIT;")[0]
        assert i.rule == "transaction_control_in_payload"
        assert i.severity == "WARNING"

    def test_line_and_phase_reported(self):
        issues = _check_transaction_control(
            "DML/seed.dml", "INSERT INTO db.t SELECT 1;\nCOMMIT;"
        )
        assert len(issues) == 1
        assert issues[0].line == 2
        assert "[DML]" in issues[0].message

    def test_remediation_present(self):
        i = _rules("BT;")[0]
        assert i.remediation["requires_human_review"] is True
        assert "deployer" in i.remediation["recommended_action"]


class TestExemptions:
    def test_plain_ddl_clean(self):
        assert _rules("CREATE MULTISET TABLE db.t (x INT) PRIMARY INDEX (x);") == []

    def test_commented_transaction_control_not_flagged(self, tmp_path):
        # Comments are stripped before content checks — a DI-tool workaround
        # token in a comment must not fire.
        ddl = tmp_path / "payload" / "database" / "DML"
        ddl.mkdir(parents=True)
        (ddl / "db.seed.dml").write_text(
            "-- BT; (DI tool emits this; SHIPS deployer owns the transaction)\n"
            "INSERT INTO db.t SELECT 1;",
            encoding="utf-8",
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert not any(i.rule == "transaction_control_in_payload" for i in res.issues)

    def test_rollback_inside_procedure_body_exempt(self):
        # An exception-handler ROLLBACK inside BEGIN…END is procedural.
        sql = (
            "REPLACE PROCEDURE db.p()\n"
            "BEGIN\n"
            "    DECLARE EXIT HANDLER FOR SQLEXCEPTION\n"
            "    BEGIN\n"
            "        ROLLBACK;\n"
            "    END;\n"
            "    INSERT INTO db.t SELECT 1;\n"
            "END;"
        )
        assert _rules(sql) == []

    def test_begin_transaction_before_body_still_caught(self):
        # BEGIN TRANSACTION is not a compound body opener — still detected.
        assert "BEGIN TRANSACTION" in _kinds("BEGIN TRANSACTION;\nUPDATE db.t SET x=1;")


class TestConfig:
    def test_registered_warning_by_default(self):
        assert DEFAULT_RULES["transaction_control_in_payload"] == "WARNING"

    def test_validate_directory_flags(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DML"
        d.mkdir(parents=True)
        (d / "db.seed.dml").write_text(
            "BT;\nINSERT INTO db.t SELECT 1;\nET;", encoding="utf-8"
        )
        res = validate_directory(str(tmp_path / "payload" / "database"))
        assert any(i.rule == "transaction_control_in_payload" for i in res.issues)

    def test_strict_promotes_to_error(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DML"
        d.mkdir(parents=True)
        (d / "db.seed.dml").write_text("COMMIT;", encoding="utf-8")
        res = validate_directory(str(tmp_path / "payload" / "database"), strict=True)
        tc = [i for i in res.issues if i.rule == "transaction_control_in_payload"]
        assert tc and all(i.severity == "ERROR" for i in tc)

    def test_off_disables(self, tmp_path):
        d = tmp_path / "payload" / "database" / "DML"
        d.mkdir(parents=True)
        (d / "db.seed.dml").write_text("COMMIT;", encoding="utf-8")
        rules = dict(DEFAULT_RULES)
        rules["transaction_control_in_payload"] = "OFF"
        res = validate_directory(
            str(tmp_path / "payload" / "database"), rules_config=rules
        )
        assert not any(i.rule == "transaction_control_in_payload" for i in res.issues)
