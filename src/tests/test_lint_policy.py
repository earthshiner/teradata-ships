"""
test_lint_policy.py — custom SHIPS lint policy (issue #167).

Covers:
    1. load_lint_policy — valid load, missing/empty file, structural errors
       (raise in both modes), rule-level errors (raise in strict, skip in
       dev), scope/severity/regex/remediation validation, phase aliasing.
    2. _check_custom_policy — deny / required / exclude evaluation, scope
       filtering by object type and phase, OFF suppression, remediation
       carried on the finding.
    3. validate_directory integration — custom findings appear with their
       policy severity, strict promotes WARNING→ERROR, remediation present.
"""

from __future__ import annotations

import re
import textwrap

import pytest

from td_release_packager.lint_policy import (
    CustomLintRule,
    LintPolicyError,
    load_lint_policy,
    policy_path,
)
from td_release_packager.validate import (
    _check_custom_policy,
    validate_directory,
)


def _write_policy(project, body: str) -> None:
    cfg = project / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "ships_lint_policy.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


# ---------------------------------------------------------------
# load_lint_policy
# ---------------------------------------------------------------


class TestLoadPolicy:
    def test_no_file_returns_empty(self, tmp_path):
        assert load_lint_policy(str(tmp_path)) == []

    def test_empty_file_returns_empty(self, tmp_path):
        _write_policy(tmp_path, "")
        assert load_lint_policy(str(tmp_path)) == []

    def test_valid_policy_loads(self, tmp_path):
        _write_policy(
            tmp_path,
            """
            rules:
              - name: no_replace_view
                description: Use CREATE VIEW.
                severity: ERROR
                applies_to:
                  object_types: [VIEW]
                  phases: [DDL]
                deny_pattern: '^\\s*replace\\s+view\\b'
                remediation:
                  safe_fix_available: true
                  automation_level: reviewable_codemod
                  recommended_action: Change REPLACE to CREATE.
                  requires_human_review: false
            """,
        )
        rules = load_lint_policy(str(tmp_path))
        assert len(rules) == 1
        r = rules[0]
        assert r.name == "no_replace_view"
        assert r.severity == "ERROR"
        assert r.object_types == {"VIEW"}
        assert r.phases == {"DDL"}
        assert r.deny_pattern is not None
        assert r.remediation["safe_fix_available"] is True
        assert r.remediation["requires_human_review"] is False

    def test_phase_aliases_canonicalised(self, tmp_path):
        _write_policy(
            tmp_path,
            """
            rules:
              - name: r
                severity: WARNING
                applies_to:
                  phases: [pre-requisites, post-install]
                deny_pattern: 'x'
            """,
        )
        r = load_lint_policy(str(tmp_path))[0]
        assert r.phases == {"PREREQS", "POST_INSTALL"}

    def test_dcl_object_type_alias_allowed(self, tmp_path):
        _write_policy(
            tmp_path,
            """
            rules:
              - name: r
                severity: WARNING
                applies_to:
                  object_types: [DCL]
                deny_pattern: 'x'
            """,
        )
        assert load_lint_policy(str(tmp_path))[0].object_types == {"DCL"}

    # -- structural errors raise in BOTH modes --

    def test_bad_yaml_raises(self, tmp_path):
        _write_policy(tmp_path, "rules: [: : :\n")
        with pytest.raises(LintPolicyError):
            load_lint_policy(str(tmp_path), strict=False)

    def test_missing_rules_key_raises(self, tmp_path):
        _write_policy(tmp_path, "policy: nope\n")
        with pytest.raises(LintPolicyError):
            load_lint_policy(str(tmp_path), strict=False)

    # -- rule-level errors: raise in strict, skip in dev --

    @pytest.mark.parametrize(
        "body",
        [
            # missing name
            "rules:\n  - severity: ERROR\n    deny_pattern: x\n",
            # unknown severity
            "rules:\n  - name: r\n    severity: LOUD\n    deny_pattern: x\n",
            # bad regex
            "rules:\n  - name: r\n    severity: ERROR\n    deny_pattern: '('\n",
            # no deny or required pattern
            "rules:\n  - name: r\n    severity: ERROR\n",
            # unknown object type
            "rules:\n  - name: r\n    severity: ERROR\n    deny_pattern: x\n"
            "    applies_to:\n      object_types: [WIDGET]\n",
            # unknown phase
            "rules:\n  - name: r\n    severity: ERROR\n    deny_pattern: x\n"
            "    applies_to:\n      phases: [SOMETIME]\n",
            # remediation wrong type
            "rules:\n  - name: r\n    severity: ERROR\n    deny_pattern: x\n"
            "    remediation:\n      safe_fix_available: yes-please\n",
        ],
    )
    def test_rule_error_strict_raises_dev_skips(self, tmp_path, body):
        _write_policy(tmp_path, body)
        with pytest.raises(LintPolicyError):
            load_lint_policy(str(tmp_path), strict=True)
        # Dev mode: the bad rule is skipped, leaving no valid rules.
        assert load_lint_policy(str(tmp_path), strict=False) == []

    def test_duplicate_name_strict_raises(self, tmp_path):
        _write_policy(
            tmp_path,
            """
            rules:
              - name: dup
                severity: ERROR
                deny_pattern: a
              - name: dup
                severity: ERROR
                deny_pattern: b
            """,
        )
        with pytest.raises(LintPolicyError):
            load_lint_policy(str(tmp_path), strict=True)
        # Dev: first kept, duplicate skipped.
        assert len(load_lint_policy(str(tmp_path), strict=False)) == 1

    def test_policy_path(self, tmp_path):
        assert policy_path(str(tmp_path)).endswith("ships_lint_policy.yaml")


# ---------------------------------------------------------------
# _check_custom_policy
# ---------------------------------------------------------------


def _rule(**kw) -> CustomLintRule:
    defaults = dict(name="r", description="desc", severity="WARNING")
    defaults.update(kw)
    return CustomLintRule(**defaults)


class TestApply:
    def test_deny_pattern_fires(self):
        rule = _rule(deny_pattern=re.compile(r"replace\s+view", re.I))
        issues = _check_custom_policy(
            "DDL/views/a.viw", "REPLACE VIEW x AS SELECT 1;", [rule]
        )
        assert len(issues) == 1
        assert issues[0].rule == "r"
        assert issues[0].severity == "WARNING"

    def test_deny_pattern_no_match_is_clean(self):
        rule = _rule(deny_pattern=re.compile(r"replace\s+view", re.I))
        assert (
            _check_custom_policy(
                "DDL/views/a.viw", "CREATE VIEW x AS SELECT 1;", [rule]
            )
            == []
        )

    def test_required_pattern_missing_fires(self):
        rule = _rule(required_pattern=re.compile(r"create\s+view\s+\S+\s*\(", re.I))
        # No column list → required pattern absent → fires.
        issues = _check_custom_policy(
            "DDL/views/a.viw", "CREATE VIEW x AS SELECT 1;", [rule]
        )
        assert len(issues) == 1

    def test_required_pattern_present_is_clean(self):
        rule = _rule(required_pattern=re.compile(r"create\s+view\s+\S+\s*\(", re.I))
        assert (
            _check_custom_policy(
                "DDL/views/a.viw", "CREATE VIEW x (c) AS SELECT 1;", [rule]
            )
            == []
        )

    def test_exclude_pattern_suppresses(self):
        rule = _rule(
            deny_pattern=re.compile(r"replace\s+view", re.I),
            exclude_pattern=re.compile(r"--\s*ships:allow", re.I),
        )
        sql = "-- ships:allow\nREPLACE VIEW x AS SELECT 1;"
        assert _check_custom_policy("DDL/views/a.viw", sql, [rule]) == []

    def test_scope_by_object_type(self):
        rule = _rule(
            object_types={"VIEW"},
            deny_pattern=re.compile(r"create", re.I),
        )
        # A table file → VIEW-scoped rule does not apply.
        tbl = "CREATE MULTISET TABLE db.t (x INT) PRIMARY INDEX (x);"
        assert _check_custom_policy("DDL/tables/t.tbl", tbl, [rule]) == []
        # A view file → applies.
        viw = "CREATE VIEW db.v AS SELECT 1;"
        assert len(_check_custom_policy("DDL/views/v.viw", viw, [rule])) == 1

    def test_scope_by_phase(self):
        rule = _rule(
            phases={"DML"},
            deny_pattern=re.compile(r"create", re.I),
        )
        viw = "CREATE VIEW db.v AS SELECT 1;"
        # DDL phase → DML-scoped rule does not apply.
        assert _check_custom_policy("DDL/views/v.viw", viw, [rule]) == []

    def test_off_rule_never_fires(self):
        rule = _rule(severity="OFF", deny_pattern=re.compile(r"create", re.I))
        assert (
            _check_custom_policy(
                "DDL/views/v.viw", "CREATE VIEW v AS SELECT 1;", [rule]
            )
            == []
        )

    def test_remediation_carried(self):
        rule = _rule(
            deny_pattern=re.compile(r"replace", re.I),
            remediation={"safe_fix_available": True, "recommended_action": "fix it"},
        )
        issues = _check_custom_policy(
            "DDL/views/v.viw", "REPLACE VIEW v AS SELECT 1;", [rule]
        )
        assert issues[0].remediation == {
            "safe_fix_available": True,
            "recommended_action": "fix it",
        }


# ---------------------------------------------------------------
# validate_directory integration
# ---------------------------------------------------------------


class TestValidateDirectoryIntegration:
    def _project_with_view(self, tmp_path):
        views = tmp_path / "payload" / "database" / "DDL" / "views"
        views.mkdir(parents=True)
        (views / "db.v.viw").write_text(
            "REPLACE VIEW db.v AS SELECT 1 AS x;", encoding="utf-8"
        )
        return tmp_path

    def _deny_replace_rule(self, severity="ERROR"):
        return _rule(
            name="no_replace_view",
            severity=severity,
            object_types={"VIEW"},
            phases={"DDL"},
            deny_pattern=re.compile(r"^\s*replace\s+view\b", re.I | re.M),
            remediation={"automation_level": "reviewable_codemod"},
        )

    def test_custom_finding_appears_with_severity_and_remediation(self, tmp_path):
        proj = self._project_with_view(tmp_path)
        res = validate_directory(
            str(proj / "payload" / "database"),
            custom_rules=[self._deny_replace_rule("ERROR")],
        )
        custom = [i for i in res.issues if i.rule == "no_replace_view"]
        assert len(custom) == 1
        assert custom[0].severity == "ERROR"
        assert custom[0].remediation == {"automation_level": "reviewable_codemod"}
        assert res.errors >= 1

    def test_strict_promotes_warning_to_error(self, tmp_path):
        proj = self._project_with_view(tmp_path)
        res = validate_directory(
            str(proj / "payload" / "database"),
            custom_rules=[self._deny_replace_rule("WARNING")],
            strict=True,
        )
        custom = [i for i in res.issues if i.rule == "no_replace_view"]
        assert custom and custom[0].severity == "ERROR"

    def test_no_custom_rules_is_unchanged(self, tmp_path):
        proj = self._project_with_view(tmp_path)
        res = validate_directory(str(proj / "payload" / "database"), custom_rules=[])
        assert [i for i in res.issues if i.rule == "no_replace_view"] == []
