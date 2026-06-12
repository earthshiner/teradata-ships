"""
test_capabilities.py — Tests for canonical capability flags (#149).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.capabilities import (
    ALL_CAPABILITY_FLAGS,
    CAPABILITIES_RESULT_REF,
    CAPABILITIES_SCHEMA_VERSION,
    REQUIRED_FLAGS,
    SUPPORTED_FLAGS,
    CapabilitiesReport,
    compute_capabilities_report,
    load_capabilities_result,
    write_capabilities_result,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _governance(**overrides) -> dict:
    base = {
        "require_change_ref": False,
        "require_signature": False,
        "require_asymmetric_signature": False,
        "require_approvals": 1,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------
# Supported flags are constant for the embedded deployer
# ---------------------------------------------------------------


class TestSupportedFlags:
    def test_all_supported_flags_default_to_true(self):
        report = compute_capabilities_report(_governance())
        for flag in SUPPORTED_FLAGS:
            assert getattr(report, flag) is True, f"{flag} should be True"

    def test_supported_flags_unaffected_by_governance(self):
        report = compute_capabilities_report(
            _governance(
                require_change_ref=True,
                require_signature=True,
                require_approvals=4,
            )
        )
        for flag in SUPPORTED_FLAGS:
            assert getattr(report, flag) is True


# ---------------------------------------------------------------
# Required flags derive from governance
# ---------------------------------------------------------------


class TestRequiredFlagsDerivation:
    def test_no_requirements_by_default(self):
        report = compute_capabilities_report(_governance())
        assert report.approval_required is False
        assert report.change_ref_required is False
        assert report.integrity_check_required is False

    def test_approval_required_when_require_approvals_gt_1(self):
        report = compute_capabilities_report(_governance(require_approvals=2))
        assert report.approval_required is True

    def test_approval_not_required_when_one_approver(self):
        report = compute_capabilities_report(_governance(require_approvals=1))
        assert report.approval_required is False

    def test_change_ref_required(self):
        report = compute_capabilities_report(_governance(require_change_ref=True))
        assert report.change_ref_required is True

    def test_integrity_check_required_when_signature_required(self):
        report = compute_capabilities_report(_governance(require_signature=True))
        assert report.integrity_check_required is True

    def test_integrity_check_required_when_asymmetric_signature_required(self):
        report = compute_capabilities_report(
            _governance(require_asymmetric_signature=True)
        )
        assert report.integrity_check_required is True

    def test_governance_with_missing_keys_defaults_to_not_required(self):
        report = compute_capabilities_report({})
        assert report.approval_required is False
        assert report.change_ref_required is False
        assert report.integrity_check_required is False


# ---------------------------------------------------------------
# Schema serialisation
# ---------------------------------------------------------------


class TestCapabilitiesReportToDict:
    def test_to_dict_includes_every_flag(self):
        report = compute_capabilities_report(_governance())
        d = report.to_dict()
        for flag in ALL_CAPABILITY_FLAGS:
            assert flag in d, f"{flag} missing from to_dict output"

    def test_to_dict_includes_metadata(self):
        report = compute_capabilities_report(_governance())
        d = report.to_dict()
        assert d["schema_version"] == CAPABILITIES_SCHEMA_VERSION
        assert d["evaluated_at"]

    def test_all_flag_values_are_booleans(self):
        report = compute_capabilities_report(_governance())
        d = report.to_dict()
        for flag in ALL_CAPABILITY_FLAGS:
            assert isinstance(d[flag], bool), f"{flag} value is not a bool"


# ---------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------


class TestCapabilitiesRoundTrip:
    def test_write_then_load(self, tmp_path):
        report = compute_capabilities_report(
            _governance(require_change_ref=True, require_approvals=3)
        )
        path = write_capabilities_result(str(tmp_path), report)
        assert path.endswith("ships.capabilities.json")
        loaded = load_capabilities_result(str(tmp_path))
        assert loaded["change_ref_required"] is True
        assert loaded["approval_required"] is True

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_capabilities_result(str(tmp_path)) is None


# ---------------------------------------------------------------
# Integration: build_package emits canonical capabilities JSON
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


class TestBuildPackageEmitsCapabilities:
    def test_capabilities_json_in_archive(self, tmp_path, tmp_project):
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
            caps_name = next(
                n for n in zf.namelist() if n.endswith("ships.capabilities.json")
            )
            caps = json.loads(zf.read(caps_name))
            build_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_name))

        # Build manifest carries the pointer.
        assert build_data.get("capabilities_ref") == CAPABILITIES_RESULT_REF
        # The canonical document holds the full body.
        assert caps["schema_version"]
        for flag in ALL_CAPABILITY_FLAGS:
            assert flag in caps
        # All deployer-capability flags True for the current build.
        for flag in SUPPORTED_FLAGS:
            assert caps[flag] is True

    def test_capabilities_visible_in_manifest_and_handoff_via_ref(
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
            handoff_name = next(
                n for n in zf.namelist() if n.endswith("ships.handoff.json")
            )
            handoff = json.loads(zf.read(handoff_name))
            mfst_name = next(
                n for n in zf.namelist() if n.endswith("ships.manifest.json")
            )
            mfst = json.loads(zf.read(mfst_name))

        assert handoff["capabilities_ref"] == CAPABILITIES_RESULT_REF
        assert mfst["capabilities_ref"] == CAPABILITIES_RESULT_REF
