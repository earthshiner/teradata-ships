"""
test_policy.py — Tests for the canonical agent policy (#151).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.policy import (
    ALL_APPROVAL_TRIGGERS,
    ALL_STOP_CONDITIONS,
    APPROVE_MISSING_CHANGE_REF,
    APPROVE_MISSING_SIGNATURE,
    APPROVE_TLS_NOT_SATISFIED,
    APPROVE_TRUST_CAVEATS,
    POLICY_RESULT_REF,
    POLICY_SCHEMA_VERSION,
    STOP_PREFLIGHT_ERROR,
    STOP_TRUST_BLOCKED,
    AgentPolicy,
    compute_agent_policy,
    load_policy_result,
    write_policy_result,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _trust(status: str) -> dict:
    return {"status": status}


def _governance(**overrides) -> dict:
    base = {
        "require_change_ref": False,
        "require_signature": False,
        "require_asymmetric_signature": False,
        "require_approvals": 1,
        "require_tls": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------
# do-not flags
# ---------------------------------------------------------------


class TestDoNotFlags:
    def test_all_do_not_flags_true_by_default(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        d = policy.to_dict()
        assert d["do_not_infer_missing_tokens"] is True
        assert d["do_not_modify_payload"] is True
        assert d["do_not_deploy_if_blocked"] is True
        assert d["do_not_ignore_failed_integrity"] is True

    def test_do_not_flags_independent_of_governance(self):
        policy = compute_agent_policy(
            trust=_trust("BLOCKED"),
            governance=_governance(require_change_ref=True, require_tls=True),
        )
        d = policy.to_dict()
        # These bind the agent to SHIPS's safety contract regardless of policy.
        assert d["do_not_infer_missing_tokens"] is True
        assert d["do_not_modify_payload"] is True


# ---------------------------------------------------------------
# Stop-condition list
# ---------------------------------------------------------------


class TestStopConditions:
    def test_full_stop_list_always_emitted(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        names = [e.condition for e in policy.stop_conditions]
        assert set(names) == set(ALL_STOP_CONDITIONS)

    def test_each_stop_has_metadata(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        for entry in policy.stop_conditions:
            assert entry.condition
            assert entry.detect_via
            assert entry.evidence_ref
            assert entry.instruction

    def test_trust_blocked_points_at_trust_json(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        entry = next(
            e for e in policy.stop_conditions if e.condition == STOP_TRUST_BLOCKED
        )
        assert entry.evidence_ref == "context/ships.trust.json"
        assert "BLOCKED" in entry.detect_via

    def test_preflight_error_points_at_process_result(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        entry = next(
            e for e in policy.stop_conditions if e.condition == STOP_PREFLIGHT_ERROR
        )
        assert "process.result.json" in entry.evidence_ref


# ---------------------------------------------------------------
# Approval triggers (governance-derived)
# ---------------------------------------------------------------


class TestApprovalTriggers:
    def test_no_triggers_when_governance_unset_and_trust_ready(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        assert policy.ask_for_human_approval_when == []

    def test_caveats_status_triggers_trust_caveats(self):
        policy = compute_agent_policy(
            trust=_trust("READY_WITH_CAVEATS"), governance=_governance()
        )
        names = [e.condition for e in policy.ask_for_human_approval_when]
        assert APPROVE_TRUST_CAVEATS in names

    def test_change_ref_requirement_triggers_missing_change_ref(self):
        policy = compute_agent_policy(
            trust=_trust("READY"), governance=_governance(require_change_ref=True)
        )
        names = [e.condition for e in policy.ask_for_human_approval_when]
        assert APPROVE_MISSING_CHANGE_REF in names

    def test_signature_requirement_triggers_missing_signature(self):
        policy = compute_agent_policy(
            trust=_trust("READY"), governance=_governance(require_signature=True)
        )
        names = [e.condition for e in policy.ask_for_human_approval_when]
        assert APPROVE_MISSING_SIGNATURE in names

    def test_asymmetric_signature_also_triggers_missing_signature(self):
        policy = compute_agent_policy(
            trust=_trust("READY"),
            governance=_governance(require_asymmetric_signature=True),
        )
        names = [e.condition for e in policy.ask_for_human_approval_when]
        assert APPROVE_MISSING_SIGNATURE in names

    def test_tls_requirement_triggers_tls_not_satisfied(self):
        policy = compute_agent_policy(
            trust=_trust("READY"), governance=_governance(require_tls=True)
        )
        names = [e.condition for e in policy.ask_for_human_approval_when]
        assert APPROVE_TLS_NOT_SATISFIED in names

    def test_each_approval_has_metadata(self):
        policy = compute_agent_policy(
            trust=_trust("READY_WITH_CAVEATS"),
            governance=_governance(
                require_change_ref=True, require_signature=True, require_tls=True
            ),
        )
        assert len(policy.ask_for_human_approval_when) == len(ALL_APPROVAL_TRIGGERS)
        for entry in policy.ask_for_human_approval_when:
            assert entry.condition
            assert entry.detect_via
            assert entry.evidence_ref
            assert entry.instruction


# ---------------------------------------------------------------
# Shape and serialisation
# ---------------------------------------------------------------


class TestSerialisation:
    def test_schema_version_emitted(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        assert policy.to_dict()["schema_version"] == POLICY_SCHEMA_VERSION

    def test_trust_status_at_build_recorded(self):
        for status in ("READY", "READY_WITH_CAVEATS", "BLOCKED"):
            policy = compute_agent_policy(
                trust=_trust(status), governance=_governance()
            )
            assert policy.to_dict()["trust_status_at_build"] == status

    def test_unknown_status_recorded_as_unknown(self):
        policy = compute_agent_policy(trust={}, governance=_governance())
        assert policy.to_dict()["trust_status_at_build"] == "UNKNOWN"

    def test_top_level_instruction_present(self):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        assert "stop_conditions" in policy.to_dict()["instruction"]


# ---------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        policy = compute_agent_policy(trust=_trust("READY"), governance=_governance())
        path = write_policy_result(str(tmp_path), policy)
        assert path.endswith("ships.policy.json")
        loaded = load_policy_result(str(tmp_path))
        assert loaded["schema_version"] == POLICY_SCHEMA_VERSION
        assert len(loaded["stop_conditions"]) == len(ALL_STOP_CONDITIONS)

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_policy_result(str(tmp_path)) is None


# ---------------------------------------------------------------
# Integration: build_package emits canonical policy JSON
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


class TestBuildPackageEmitsPolicy:
    def test_policy_json_in_archive_and_pointer_in_manifests(
        self, tmp_path, tmp_project
    ):
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
        (main_arc, _manifest), _companion = build_package(cfg)

        with zipfile.ZipFile(main_arc) as zf:
            policy_name = next(
                n for n in zf.namelist() if n.endswith("ships.policy.json")
            )
            policy = json.loads(zf.read(policy_name))
            build_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_name))
            handoff_name = next(
                n for n in zf.namelist() if n.endswith("ships.handoff.json")
            )
            handoff = json.loads(zf.read(handoff_name))
            schema_name = next(
                n for n in zf.namelist() if n.endswith("ships.policy.schema.json")
            )
            schema = json.loads(zf.read(schema_name))

        # Build manifest carries the pointer.
        assert build_data.get("policy_ref") == POLICY_RESULT_REF
        # Handoff carries the pointer.
        assert handoff["policy_ref"] == POLICY_RESULT_REF
        # Canonical document holds the full body.
        assert policy["schema_version"] == POLICY_SCHEMA_VERSION
        assert len(policy["stop_conditions"]) == len(ALL_STOP_CONDITIONS)
        # Schema was published alongside the doc.
        assert schema["$id"].endswith("ships.policy.schema.json")
