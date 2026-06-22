"""
test_project_actions.py — Tests for ships.project_actions.json (#273).
"""

from __future__ import annotations

import json
from pathlib import Path

from td_release_packager.project_actions import (
    ACTION_HARVEST,
    ACTION_PACKAGE,
    ACTION_SCAFFOLD,
    ACTION_TOKENISE,
    ALL_PROJECT_ACTIONS,
    PROJECT_ACTIONS_FILENAME,
    PROJECT_ACTIONS_SCHEMA_VERSION,
    REASON_PROJECT_NOT_HARVESTED,
    REASON_REWRITES_SOURCE_FILES,
    compute_project_actions,
    load_project_actions,
    write_project_actions,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "demo_project") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "ships.yaml").write_text(f"name: {name}\n", encoding="utf-8", newline="")
    return project


def _write_decisions(project: Path, runs):
    (project / ".ships").mkdir(parents=True, exist_ok=True)
    (project / ".ships" / "ships.decisions.json").write_text(
        json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8"
    )


def _run(stage_name: str, status: str = "success") -> dict:
    return {"command": stage_name, "stages": [{"stage": stage_name, "status": status}]}


def _action_names(items):
    out = []
    for item in items:
        if hasattr(item, "action"):
            out.append(item.action)
        else:
            out.append(item["action"])
    return out


# ---------------------------------------------------------------
# Allowed actions
# ---------------------------------------------------------------


class TestAllowedActions:
    def test_always_allowed_set_in_scaffolded_state(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        for a in (
            "scaffold",
            "harvest",
            "inspect",
            "analyse",
            "scan",
            "import_legacy",
            "decompose_names",
        ):
            assert a in report.allowed_actions, f"{a} should always be allowed"

    def test_harvested_state_promotes_package_to_allowed(self, tmp_path):
        project = _make_project(tmp_path)
        # Drop a payload file so the discovery flag flips True.
        payload = project / "payload" / "database" / "DDL" / "tables"
        payload.mkdir(parents=True)
        (payload / "Dev.T.tbl").write_text(
            "CREATE MULTISET TABLE Dev.T (Id INTEGER);", encoding="utf-8"
        )
        _write_decisions(project, [_run("harvest")])
        report = compute_project_actions(str(project))
        assert ACTION_PACKAGE in report.allowed_actions

    def test_packaged_state_keeps_package_allowed(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("inspect"), _run("package")])
        report = compute_project_actions(str(project))
        assert ACTION_PACKAGE in report.allowed_actions


# ---------------------------------------------------------------
# Approval gates
# ---------------------------------------------------------------


class TestApprovalGates:
    def test_tokenise_always_requires_approval(self, tmp_path):
        """tokenise rewrites source files in place — never autonomous."""
        for label, state_setup in (
            ("scaffolded", None),
            ("harvested", [_run("harvest")]),
            ("inspected", [_run("inspect")]),
            ("analysed", [_run("analyse")]),
            ("packaged", [_run("package")]),
        ):
            (tmp_path / label).mkdir()
            project = _make_project(tmp_path / label)
            if state_setup is not None:
                _write_decisions(project, state_setup)
            report = compute_project_actions(str(project))
            names = _action_names(report.requires_human_approval)
            assert ACTION_TOKENISE in names, "tokenise must always be approval-gated"

    def test_tokenise_approval_carries_reason_and_instruction(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        entry = next(
            c for c in report.requires_human_approval if c.action == ACTION_TOKENISE
        )
        assert entry.reason == REASON_REWRITES_SOURCE_FILES
        assert "dry-run" in entry.instruction.lower()
        assert entry.evidence_ref == "config/tokenise.conf"

    def test_package_in_empty_scaffold_requires_approval(self, tmp_path):
        """No payload yet → packaging requires approval."""
        project = _make_project(tmp_path)
        report = compute_project_actions(str(project))
        approval_names = _action_names(report.requires_human_approval)
        assert ACTION_PACKAGE in approval_names
        entry = next(
            c for c in report.requires_human_approval if c.action == ACTION_PACKAGE
        )
        assert entry.reason == REASON_PROJECT_NOT_HARVESTED

    def test_package_allowed_once_payload_has_content(self, tmp_path):
        project = _make_project(tmp_path)
        payload = project / "payload" / "database" / "DDL" / "tables"
        payload.mkdir(parents=True)
        (payload / "Dev.T.tbl").write_text(
            "CREATE MULTISET TABLE Dev.T (Id INTEGER);", encoding="utf-8"
        )
        report = compute_project_actions(str(project))
        assert ACTION_PACKAGE in report.allowed_actions
        assert ACTION_PACKAGE not in _action_names(report.requires_human_approval)


# ---------------------------------------------------------------
# Discovery flags
# ---------------------------------------------------------------


class TestDiscoveryFlags:
    def test_default_flags_are_false(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        assert report.discovery_flags == {
            "tokenise_config_present": False,
            "env_configs_present": False,
            "source_payload_present": False,
        }

    def test_tokenise_config_flag(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "config").mkdir()
        (project / "config" / "tokenise.conf").write_text(
            "s/$A/{{A}}/g\n", encoding="utf-8"
        )
        flags = compute_project_actions(str(project)).discovery_flags
        assert flags["tokenise_config_present"] is True

    def test_env_configs_flag(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "config" / "env").mkdir(parents=True)
        (project / "config" / "env" / "DEV.conf").write_text(
            "SHIPS_ENV=DEV\n", encoding="utf-8"
        )
        flags = compute_project_actions(str(project)).discovery_flags
        assert flags["env_configs_present"] is True

    def test_payload_flag(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "payload" / "database" / "DDL").mkdir(parents=True)
        (project / "payload" / "database" / "DDL" / "X.tbl").write_text(
            "CREATE MULTISET TABLE X (Id INTEGER);", encoding="utf-8"
        )
        flags = compute_project_actions(str(project)).discovery_flags
        assert flags["source_payload_present"] is True


# ---------------------------------------------------------------
# Shape and serialisation
# ---------------------------------------------------------------


class TestShape:
    def test_schema_version_and_evaluated_at(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        d = report.to_dict()
        assert d["schema_version"] == PROJECT_ACTIONS_SCHEMA_VERSION
        assert d["evaluated_at"]

    def test_project_state_recorded(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("harvest")])
        report = compute_project_actions(str(project))
        assert report.project_state == "harvested"

    def test_required_top_level_keys(self, tmp_path):
        d = compute_project_actions(str(_make_project(tmp_path))).to_dict()
        for key in (
            "schema_version",
            "evaluated_at",
            "project_state",
            "discovery_flags",
            "allowed_actions",
            "blocked_actions",
            "requires_human_approval",
        ):
            assert key in d

    def test_constraint_entries_have_required_fields(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        for entry in report.requires_human_approval:
            d = entry.to_dict()
            for key in ("action", "reason", "evidence_ref", "instruction"):
                assert key in d
                assert isinstance(d[key], str)

    def test_no_action_appears_in_more_than_one_list(self, tmp_path):
        report = compute_project_actions(str(_make_project(tmp_path)))
        a = set(report.allowed_actions)
        b = set(_action_names(report.blocked_actions))
        c = set(_action_names(report.requires_human_approval))
        assert not (a & b)
        assert not (a & c)
        assert not (b & c)

    def test_every_action_appears_somewhere(self, tmp_path):
        # No action from the closed vocabulary should be silently dropped.
        report = compute_project_actions(str(_make_project(tmp_path)))
        names = (
            set(report.allowed_actions)
            | set(_action_names(report.blocked_actions))
            | set(_action_names(report.requires_human_approval))
        )
        for action in ALL_PROJECT_ACTIONS:
            assert action in names, f"{action} missing from output"


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        project = _make_project(tmp_path)
        path = write_project_actions(str(project))
        assert path.endswith(PROJECT_ACTIONS_FILENAME)
        loaded = load_project_actions(str(project))
        assert loaded["schema_version"] == PROJECT_ACTIONS_SCHEMA_VERSION

    def test_load_returns_none_when_absent(self, tmp_path):
        project = _make_project(tmp_path)
        assert load_project_actions(str(project)) is None

    def test_load_returns_none_when_corrupt(self, tmp_path):
        project = _make_project(tmp_path)
        (project / PROJECT_ACTIONS_FILENAME).write_text("not json", encoding="utf-8")
        assert load_project_actions(str(project)) is None


# ---------------------------------------------------------------
# Integration: stage recording refreshes both files
# ---------------------------------------------------------------


class TestStageRecordingRefreshesActions:
    def test_scaffold_writes_both_index_and_actions(self, tmp_path):
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
        assert (project / "ships.project.json").is_file()
        assert (project / PROJECT_ACTIONS_FILENAME).is_file()
        actions = json.loads(
            (project / PROJECT_ACTIONS_FILENAME).read_text(encoding="utf-8")
        )
        assert actions["project_state"] == "scaffolded"
        # tokenise must still be in the approval list right after scaffold.
        assert any(
            e["action"] == ACTION_TOKENISE for e in actions["requires_human_approval"]
        )

    def test_scaffolded_index_actions_ref_points_at_actions_file(self, tmp_path):
        from argparse import Namespace
        from td_release_packager.cli import _cmd_scaffold

        args = Namespace(
            name="auto_test_ref",
            output=str(tmp_path),
            environments="DEV",
            repair=False,
        )
        try:
            _cmd_scaffold(args)
        except SystemExit:
            pass

        project = tmp_path / "auto_test_ref"
        index = json.loads((project / "ships.project.json").read_text(encoding="utf-8"))
        assert index["actions_ref"] == "ships.project_actions.json"
