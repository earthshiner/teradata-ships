"""
test_project_policy.py — Tests for ships.project_policy.json (#275).
"""

from __future__ import annotations

import json
from pathlib import Path

from td_release_packager.project_policy import (
    ALL_PROJECT_APPROVAL_TRIGGERS,
    ALL_PROJECT_STOP_CONDITIONS,
    PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD,
    PROJECT_APPROVE_TOKENISE_NO_DRY_RUN,
    PROJECT_POLICY_FILENAME,
    PROJECT_POLICY_SCHEMA_VERSION,
    PROJECT_STOP_INSPECT_ERRORS,
    PROJECT_STOP_SHIPS_YAML_MISSING,
    PROJECT_STOP_TOKENISE_CONFIG_INVALID,
    PROJECT_STOP_UNKNOWN_STATE,
    compute_project_policy,
    load_project_policy,
    write_project_policy,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "demo_project") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "ships.yaml").write_text(f"name: {name}\n", encoding="utf-8", newline="")
    return project


def _stop_names(policy):
    return [e.condition for e in policy.stop_conditions]


def _approval_names(policy):
    return [e.condition for e in policy.ask_for_human_approval_when]


# ---------------------------------------------------------------
# do-not flags
# ---------------------------------------------------------------


class TestDoNotFlags:
    def test_all_do_not_flags_true_by_default(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        d = policy.to_dict()
        assert d["do_not_modify_source_without_dry_run"] is True
        assert d["do_not_skip_inspect_before_package"] is True
        assert d["do_not_proceed_past_inspect_errors"] is True


# ---------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------


class TestStopConditions:
    def test_ships_yaml_missing_always_emitted(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        assert PROJECT_STOP_SHIPS_YAML_MISSING in _stop_names(policy)

    def test_inspect_errors_always_emitted(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        assert PROJECT_STOP_INSPECT_ERRORS in _stop_names(policy)

    def test_unknown_state_always_emitted(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        assert PROJECT_STOP_UNKNOWN_STATE in _stop_names(policy)

    def test_tokenise_invalid_emitted_only_when_config_present(self, tmp_path):
        # Absent: stop NOT emitted.
        project = _make_project(tmp_path)
        policy = compute_project_policy(str(project))
        assert PROJECT_STOP_TOKENISE_CONFIG_INVALID not in _stop_names(policy)

        # Present: stop emitted.
        (project / "config").mkdir()
        (project / "config" / "tokenise.conf").write_text(
            "s/$A/{{A}}/g\n", encoding="utf-8"
        )
        policy = compute_project_policy(str(project))
        assert PROJECT_STOP_TOKENISE_CONFIG_INVALID in _stop_names(policy)

    def test_each_stop_carries_metadata(self, tmp_path):
        project = _make_project(tmp_path)
        # Force tokenise stop too so we exercise every entry.
        (project / "config").mkdir()
        (project / "config" / "tokenise.conf").write_text(
            "s/$A/{{A}}/g\n", encoding="utf-8"
        )
        policy = compute_project_policy(str(project))
        for entry in policy.stop_conditions:
            assert entry.condition
            assert entry.detect_via
            assert entry.evidence_ref
            assert entry.instruction

    def test_stop_names_are_subset_of_closed_vocabulary(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "config").mkdir()
        (project / "config" / "tokenise.conf").write_text(
            "s/$A/{{A}}/g\n", encoding="utf-8"
        )
        policy = compute_project_policy(str(project))
        for name in _stop_names(policy):
            assert name in ALL_PROJECT_STOP_CONDITIONS


# ---------------------------------------------------------------
# Approval triggers
# ---------------------------------------------------------------


class TestApprovalTriggers:
    def test_both_triggers_always_emitted(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        names = _approval_names(policy)
        assert PROJECT_APPROVE_TOKENISE_NO_DRY_RUN in names
        assert PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD in names

    def test_each_approval_carries_metadata(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        for entry in policy.ask_for_human_approval_when:
            assert entry.condition
            assert entry.detect_via
            assert entry.evidence_ref
            assert entry.instruction

    def test_approval_names_are_subset_of_closed_vocabulary(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        for name in _approval_names(policy):
            assert name in ALL_PROJECT_APPROVAL_TRIGGERS


# ---------------------------------------------------------------
# Shape and serialisation
# ---------------------------------------------------------------


class TestShape:
    def test_schema_version_and_evaluated_at(self, tmp_path):
        policy = compute_project_policy(str(_make_project(tmp_path)))
        d = policy.to_dict()
        assert d["schema_version"] == PROJECT_POLICY_SCHEMA_VERSION
        assert d["evaluated_at"]

    def test_project_state_recorded(self, tmp_path):
        # No decisions log -> bare project is scaffolded.
        policy = compute_project_policy(str(_make_project(tmp_path)))
        assert policy.project_state == "scaffolded"

    def test_required_top_level_keys(self, tmp_path):
        d = compute_project_policy(str(_make_project(tmp_path))).to_dict()
        for key in (
            "schema_version",
            "evaluated_at",
            "purpose",
            "project_state",
            "do_not_modify_source_without_dry_run",
            "do_not_skip_inspect_before_package",
            "do_not_proceed_past_inspect_errors",
            "stop_conditions",
            "ask_for_human_approval_when",
            "instruction",
        ):
            assert key in d

    def test_top_level_instruction_mentions_stop_conditions(self, tmp_path):
        d = compute_project_policy(str(_make_project(tmp_path))).to_dict()
        assert "stop_conditions" in d["instruction"]


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        project = _make_project(tmp_path)
        path = write_project_policy(str(project))
        assert path.endswith(PROJECT_POLICY_FILENAME)
        loaded = load_project_policy(str(project))
        assert loaded["schema_version"] == PROJECT_POLICY_SCHEMA_VERSION
        assert loaded["project_state"] == "scaffolded"

    def test_load_returns_none_when_absent(self, tmp_path):
        project = _make_project(tmp_path)
        assert load_project_policy(str(project)) is None

    def test_load_returns_none_when_corrupt(self, tmp_path):
        project = _make_project(tmp_path)
        (project / PROJECT_POLICY_FILENAME).write_text("not json", encoding="utf-8")
        assert load_project_policy(str(project)) is None


# ---------------------------------------------------------------
# Integration: stage recording refreshes all three project files
# ---------------------------------------------------------------


class TestStageRecordingRefreshesPolicy:
    def test_scaffold_writes_index_actions_and_policy(self, tmp_path):
        from argparse import Namespace
        from td_release_packager.cli import _cmd_scaffold

        args = Namespace(
            name="auto_test",
            output=str(tmp_path),
            environments="DEV",
            repair=False,
        )
        try:
            _cmd_scaffold(args)
        except SystemExit:
            pass

        project = tmp_path / "auto_test"
        for fname in (
            "ships.project.json",
            "ships.project_actions.json",
            PROJECT_POLICY_FILENAME,
        ):
            assert (project / fname).is_file(), f"missing {fname}"

        policy = json.loads(
            (project / PROJECT_POLICY_FILENAME).read_text(encoding="utf-8")
        )
        assert policy["project_state"] == "scaffolded"
        # Both approval triggers must be present right after scaffold.
        approvals = [e["condition"] for e in policy["ask_for_human_approval_when"]]
        assert PROJECT_APPROVE_TOKENISE_NO_DRY_RUN in approvals
        assert PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD in approvals

    def test_index_policy_ref_points_at_policy_file(self, tmp_path):
        from argparse import Namespace
        from td_release_packager.cli import _cmd_scaffold

        args = Namespace(
            name="ref_check",
            output=str(tmp_path),
            environments="DEV",
            repair=False,
        )
        try:
            _cmd_scaffold(args)
        except SystemExit:
            pass

        index = json.loads(
            (tmp_path / "ref_check" / "ships.project.json").read_text(encoding="utf-8")
        )
        assert index["policy_ref"] == "ships.project_policy.json"
