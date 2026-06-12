"""
test_actions.py — Tests for canonical action controls (#143).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.actions import (
    ACTION_DEPLOY,
    ACTION_DRY_RUN,
    ACTION_FORWARD_TO_HUMAN,
    ACTION_MODIFY_PAYLOAD,
    ACTION_REPACKAGE,
    ACTION_ROLLBACK,
    ACTION_VERIFY_INTEGRITY,
    ACTIONS_RESULT_REF,
    ActionConstraint,
    ActionsReport,
    REASON_DBA_REVIEW_REQUIRED,
    REASON_NOT_ENVIRONMENT_PREREQ,
    REASON_TRUST_BLOCKED,
    REASON_TRUST_CAVEATS,
    compute_actions_report,
    load_actions_result,
    write_actions_result,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _trust(status: str) -> dict:
    return {"status": status}


def _action_names(items):
    """Extract just the .action field from a list of ActionConstraint/dict."""
    out = []
    for item in items:
        if isinstance(item, ActionConstraint):
            out.append(item.action)
        else:
            out.append(item["action"])
    return out


# ---------------------------------------------------------------
# Status × role matrix
# ---------------------------------------------------------------


class TestComputeActionsReport:
    def test_ready_single_package_allows_deploy(self):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        assert ACTION_DEPLOY in report.allowed_actions
        assert ACTION_DRY_RUN in report.allowed_actions
        assert report.deploy_allowed is True
        assert report.dry_run_allowed is True

    def test_ready_blocks_modify_payload_for_non_prereq(self):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        assert ACTION_MODIFY_PAYLOAD not in report.allowed_actions
        assert ACTION_MODIFY_PAYLOAD in _action_names(report.blocked_actions)
        assert report.payload_modification_allowed is False

    def test_caveats_routes_deploy_to_human_approval(self):
        report = compute_actions_report(
            trust=_trust("READY_WITH_CAVEATS"), role="single"
        )
        assert ACTION_DEPLOY not in report.allowed_actions
        assert ACTION_DEPLOY in _action_names(report.requires_human_approval)
        # The approval entry must carry the reason
        approval = next(
            c for c in report.requires_human_approval if c.action == ACTION_DEPLOY
        )
        assert approval.reason == REASON_TRUST_CAVEATS
        assert approval.evidence_ref.endswith("ships.trust.json")
        assert report.deploy_allowed is False

    def test_blocked_status_blocks_deploy(self):
        report = compute_actions_report(trust=_trust("BLOCKED"), role="single")
        assert ACTION_DEPLOY in _action_names(report.blocked_actions)
        blocked = next(c for c in report.blocked_actions if c.action == ACTION_DEPLOY)
        assert blocked.reason == REASON_TRUST_BLOCKED
        assert report.deploy_allowed is False

    def test_blocked_routes_dry_run_to_human_approval(self):
        report = compute_actions_report(trust=_trust("BLOCKED"), role="single")
        # Dry-run is diagnostic — operator can still ack and run it.
        assert ACTION_DRY_RUN in _action_names(report.requires_human_approval)
        assert ACTION_DRY_RUN not in report.allowed_actions

    def test_environment_prereq_with_placeholders_routes_modify_to_approval(self):
        report = compute_actions_report(
            trust=_trust("BLOCKED"),
            role="environment_prereqs",
            has_dba_placeholders=True,
        )
        names = _action_names(report.requires_human_approval)
        assert ACTION_MODIFY_PAYLOAD in names
        approval = next(
            c
            for c in report.requires_human_approval
            if c.action == ACTION_MODIFY_PAYLOAD
        )
        assert approval.reason == REASON_DBA_REVIEW_REQUIRED
        assert approval.evidence_ref.endswith("DBA_INSTRUCTIONS.md")

    def test_environment_prereq_allows_repackage(self):
        report = compute_actions_report(
            trust=_trust("READY_WITH_CAVEATS"),
            role="environment_prereqs",
            has_dba_placeholders=False,
        )
        assert ACTION_REPACKAGE in report.allowed_actions

    def test_non_environment_prereq_blocks_repackage(self):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        assert ACTION_REPACKAGE in _action_names(report.blocked_actions)
        blocked = next(
            c for c in report.blocked_actions if c.action == ACTION_REPACKAGE
        )
        assert blocked.reason == REASON_NOT_ENVIRONMENT_PREREQ

    def test_safe_actions_always_allowed(self):
        for status in ("READY", "READY_WITH_CAVEATS", "BLOCKED"):
            report = compute_actions_report(trust=_trust(status), role="single")
            assert ACTION_VERIFY_INTEGRITY in report.allowed_actions
            assert ACTION_ROLLBACK in report.allowed_actions
            assert ACTION_FORWARD_TO_HUMAN in report.allowed_actions


# ---------------------------------------------------------------
# Schema serialisation
# ---------------------------------------------------------------


class TestActionsReportToDict:
    def test_to_dict_schema(self):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        d = report.to_dict()
        assert d["schema_version"]
        assert d["evaluated_at"]
        assert isinstance(d["allowed_actions"], list)
        assert isinstance(d["blocked_actions"], list)
        assert isinstance(d["requires_human_approval"], list)
        assert isinstance(d["deploy_allowed"], bool)
        assert isinstance(d["dry_run_allowed"], bool)
        assert isinstance(d["payload_modification_allowed"], bool)

    def test_blocked_entries_are_objects_with_reason(self):
        report = compute_actions_report(trust=_trust("BLOCKED"), role="single")
        d = report.to_dict()
        for entry in d["blocked_actions"]:
            assert "action" in entry
            assert "reason" in entry
            assert "evidence_ref" in entry

    def test_allowed_entries_are_plain_strings(self):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        d = report.to_dict()
        for entry in d["allowed_actions"]:
            assert isinstance(entry, str)


# ---------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------


class TestActionsRoundTrip:
    def test_write_then_load(self, tmp_path):
        report = compute_actions_report(trust=_trust("READY"), role="single")
        path = write_actions_result(str(tmp_path), report)
        assert path.endswith("ships.actions.json")
        loaded = load_actions_result(str(tmp_path))
        assert loaded["allowed_actions"] == report.allowed_actions

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_actions_result(str(tmp_path)) is None


# ---------------------------------------------------------------
# Integration: build_package emits canonical actions JSON
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_project(tmp_path):
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "payload/database/pre-requisites/databases",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


class TestBuildPackageEmitsActions:
    def test_actions_json_in_archive(self, tmp_path, tmp_project):
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        _write(
            tmp_project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        cfg = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="TestPkg",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_arc, manifest), _companion = build_package(cfg)

        with zipfile.ZipFile(main_arc) as zf:
            actions_name = next(
                n for n in zf.namelist() if n.endswith("ships.actions.json")
            )
            actions = json.loads(zf.read(actions_name))
            build_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_name))

        # Build manifest carries the pointer.
        assert build_data.get("actions_ref") == ACTIONS_RESULT_REF
        # The canonical document holds the full body.
        assert actions["schema_version"]
        assert "allowed_actions" in actions
        assert "blocked_actions" in actions
        assert "requires_human_approval" in actions
        assert isinstance(actions["deploy_allowed"], bool)
