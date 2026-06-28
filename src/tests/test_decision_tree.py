"""
test_decision_tree.py — declarative packaging decision model (#378).

Covers:
    - loading + validating the bundled tools/navigator/decision-tree.yaml
    - the shared condition DSL (eq / ne / truthy / all / any / derived)
    - visibility + warnings against representative questions
    - fail-closed validation (duplicate id, bad kind, dangling field ref)
    - lockstep guard: YAML question ids match the HTML wizard's inline model
"""

import os
import re

import pytest

from td_release_packager.decision_tree import (
    DecisionTreeError,
    active_warnings,
    derived_value,
    evaluate_condition,
    is_visible,
    load_decision_tree,
    parse_decision_tree,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_HTML = os.path.join(_REPO_ROOT, "tools", "navigator", "ships-navigator.html")


# ---------------------------------------------------------------
# Load + structure
# ---------------------------------------------------------------


class TestLoad:
    def test_bundled_model_loads(self):
        tree = load_decision_tree()
        assert tree.schema_version
        assert len(tree.questions) > 10
        # A couple of well-known nodes exist.
        assert tree.by_id("source.type") is not None
        assert tree.by_id("package.name") is not None

    def test_radio_questions_have_options(self):
        tree = load_decision_tree()
        for q in tree.questions:
            if q.kind == "radio":
                assert q.options, f"{q.id} radio has no options"

    def test_defaults_present(self):
        tree = load_decision_tree()
        assert tree.by_id("source.ref").default == "main"
        assert tree.by_id("analyse.namespace").default == "teradata://ships-analysis"


# ---------------------------------------------------------------
# Condition DSL
# ---------------------------------------------------------------


class TestConditions:
    def test_eq_ne(self):
        a = {"source.type": "github"}
        assert evaluate_condition(
            {"eq": {"field": "source.type", "value": "github"}}, a
        )
        assert not evaluate_condition(
            {"eq": {"field": "source.type", "value": "filesystem"}}, a
        )
        assert evaluate_condition(
            {"ne": {"field": "source.type", "value": "filesystem"}}, a
        )

    def test_truthy(self):
        assert evaluate_condition(
            {"truthy": "tokens.already"}, {"tokens.already": "no"}
        )
        assert not evaluate_condition({"truthy": "tokens.already"}, {})
        assert not evaluate_condition(
            {"truthy": "tokens.already"}, {"tokens.already": ""}
        )

    def test_all_combinator(self):
        cond = {
            "all": [
                {"truthy": "tokens.already"},
                {"ne": {"field": "tokens.already", "value": "yes"}},
            ]
        }
        assert evaluate_condition(cond, {"tokens.already": "no"})
        assert not evaluate_condition(cond, {"tokens.already": "yes"})
        assert not evaluate_condition(cond, {})

    def test_any_combinator(self):
        cond = {
            "any": [
                {"eq": {"field": "x", "value": "1"}},
                {"eq": {"field": "x", "value": "2"}},
            ]
        }
        assert evaluate_condition(cond, {"x": "2"})
        assert not evaluate_condition(cond, {"x": "3"})

    def test_none_always_visible(self):
        assert evaluate_condition(None, {})

    def test_unknown_operator_raises(self):
        with pytest.raises(DecisionTreeError):
            evaluate_condition({"bogus": 1}, {})


# ---------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------


class TestDerived:
    def test_target_os_windows_drive(self):
        assert derived_value("target_os", {"project.dir": "C:\\proj"}) == "windows"

    def test_target_os_windows_backslash(self):
        assert derived_value("target_os", {"project.dir": "a\\b"}) == "windows"

    def test_target_os_posix(self):
        assert derived_value("target_os", {"project.dir": "/home/me/proj"}) == "posix"

    def test_target_os_empty(self):
        assert derived_value("target_os", {}) == "posix"

    def test_bash_flavour_visible_only_on_windows(self):
        tree = load_decision_tree()
        q = tree.by_id("bash.flavour")
        assert is_visible(q, {"project.dir": "C:\\proj"})
        assert not is_visible(q, {"project.dir": "/home/me"})

    def test_unknown_derived_raises(self):
        with pytest.raises(DecisionTreeError):
            derived_value("nope", {})


# ---------------------------------------------------------------
# Visibility + warnings on real nodes
# ---------------------------------------------------------------


class TestRealNodes:
    def test_github_fields_hidden_for_filesystem(self):
        tree = load_decision_tree()
        owner = tree.by_id("source.owner_repo")
        assert is_visible(owner, {"source.type": "github"})
        assert not is_visible(owner, {"source.type": "filesystem"})

    def test_token_model_hidden_when_already_tokenised(self):
        tree = load_decision_tree()
        model = tree.by_id("tokens.model")
        assert is_visible(model, {"tokens.already": "no"})
        assert not is_visible(model, {"tokens.already": "yes"})
        assert not is_visible(model, {})

    def test_atomic_warning_fires_on_no_and_unsure(self):
        tree = load_decision_tree()
        q = tree.by_id("atomic.eponymous")
        assert active_warnings(q, {"atomic.eponymous": "no"})
        assert active_warnings(q, {"atomic.eponymous": "unsure"})
        assert active_warnings(q, {"atomic.eponymous": "yes"}) == []


# ---------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------


class TestValidation:
    def test_duplicate_id_rejected(self):
        data = {
            "questions": [
                {"id": "a", "label": "A", "kind": "text"},
                {"id": "a", "label": "A2", "kind": "text"},
            ]
        }
        with pytest.raises(DecisionTreeError, match="Duplicate"):
            parse_decision_tree(data)

    def test_bad_kind_rejected(self):
        data = {"questions": [{"id": "a", "label": "A", "kind": "slider"}]}
        with pytest.raises(DecisionTreeError, match="invalid kind"):
            parse_decision_tree(data)

    def test_radio_without_options_rejected(self):
        data = {"questions": [{"id": "a", "label": "A", "kind": "radio"}]}
        with pytest.raises(DecisionTreeError, match="no options"):
            parse_decision_tree(data)

    def test_dangling_field_reference_rejected(self):
        data = {
            "questions": [
                {
                    "id": "a",
                    "label": "A",
                    "kind": "text",
                    "show": {"eq": {"field": "ghost", "value": "x"}},
                }
            ]
        }
        with pytest.raises(DecisionTreeError, match="unknown field"):
            parse_decision_tree(data)


# ---------------------------------------------------------------
# Lockstep: YAML is the source of truth for the HTML wizard
# ---------------------------------------------------------------


def _html_question_ids() -> list:
    with open(_HTML, "r", encoding="utf-8") as fh:
        html = fh.read()
    # Slice the QUESTIONS array literal.
    start = html.index("const QUESTIONS = [")
    end = html.index("];", start)
    block = html[start:end]
    # Question-level ids only — options use value/label, conditions use field.
    return re.findall(r'\bid:\s*"([^"]+)"', block)


class TestLockstep:
    def test_yaml_ids_match_html_ids(self):
        tree = load_decision_tree()
        yaml_ids = set(tree.ids)
        html_ids = set(_html_question_ids())
        assert yaml_ids == html_ids, (
            "decision-tree.yaml and the HTML wizard's inline QUESTIONS have "
            f"drifted.\n  only in YAML: {sorted(yaml_ids - html_ids)}\n"
            f"  only in HTML: {sorted(html_ids - yaml_ids)}"
        )
