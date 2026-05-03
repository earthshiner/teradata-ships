"""
test_scan_orchestrator_integration.py — Verify the `scan` CLI stage
writes a well-formed decisions.json via the orchestrator foundation.

This is the pilot for build-order item 4 (refactor existing stages
onto cascade + decisions). Each test invokes ``_cmd_scan`` directly
with a constructed argparse.Namespace (faster than subprocess and
easier to introspect failures), then loads the resulting
decisions.json and asserts on its structure.

Covers:
    1. Single run produces a valid manifest with one ``scan`` stage
    2. Config provenance is recorded for source + properties
    3. Inputs / outputs sections record scan artefacts
    4. Validation errors → issues with severity=error, code=TOKEN_UNDEFINED
    5. Validation warnings → issues with severity=warning, code=TOKEN_UNUSED
    6. Status auto-rollup: errors → status=error, warnings → status=warning
    7. Multiple scan runs append to existing decisions.json
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from td_release_packager.cli import _cmd_scan


def _make_project(tmp_path: Path, *, ddl_content: str) -> Path:
    """Create a minimal SHIPS-shaped project with one DDL file."""
    payload = tmp_path / "payload" / "database" / "DDL" / "tables"
    payload.mkdir(parents=True)
    (payload / "demo.tbl").write_text(ddl_content, encoding="utf-8")
    return tmp_path


def _read_decisions(project: Path) -> dict:
    return json.loads((project / "decisions.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------


class TestScanWritesDecisionsManifest:
    def test_creates_decisions_json_with_one_run_one_stage(self, tmp_path):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{STD_DATABASE}}.demo (id INT);\n",
        )

        _cmd_scan(Namespace(source=str(project), properties=None))

        data = _read_decisions(project)
        assert data["schema_version"] == 1
        assert len(data["runs"]) == 1
        run = data["runs"][0]
        assert run["command"] == "scan"
        assert run["final_status"] == "success"
        assert len(run["stages"]) == 1
        assert run["stages"][0]["stage"] == "scan"

    def test_records_config_resolved_with_provenance(self, tmp_path):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{X}}.demo (id INT);\n",
        )

        _cmd_scan(Namespace(source=str(project), properties=None))

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        assert "source" in stage["config_resolved"]
        assert stage["config_resolved"]["source"]["value"] == str(project)
        assert stage["config_resolved"]["source"]["source"] == "layer-5"
        assert stage["config_resolved"]["source"]["source_path"] == "cli"
        # properties was None — recorded as null with same provenance
        assert stage["config_resolved"]["properties"]["value"] is None

    def test_records_inputs_and_outputs(self, tmp_path):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{A}}.x (id INT); -- {{B}}\n",
        )

        _cmd_scan(Namespace(source=str(project), properties=None))

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        # inputs include the resolved scan_directory
        assert "scan_directory" in stage["inputs"]
        assert stage["inputs"]["files_with_tokens"] == 1
        # outputs include the unique tokens list
        assert stage["outputs"]["unique_tokens"] == 2
        assert sorted(stage["outputs"]["tokens"]) == ["A", "B"]


# ---------------------------------------------------------------
# Validation issues
# ---------------------------------------------------------------


class TestScanRecordsValidationIssues:
    def test_undefined_token_recorded_as_error_issue(self, tmp_path, capsys):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{UNDEFINED}}.demo (id INT);\n",
        )
        # Properties file that does NOT define UNDEFINED
        props = project / "config" / "properties" / "DEV.properties"
        props.parent.mkdir(parents=True)
        props.write_text("DEFINED=value\n", encoding="utf-8")

        _cmd_scan(Namespace(source=str(project), properties=str(props)))
        capsys.readouterr()  # discard stdout

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        error_issues = [i for i in stage["issues"] if i["severity"] == "error"]
        assert len(error_issues) >= 1
        assert any(i["code"] == "TOKEN_UNDEFINED" for i in error_issues)
        # Error issue auto-upgrades stage status to "error"
        assert stage["status"] == "error"
        # Run final_status rolls up to "failed" when any stage errors
        assert _read_decisions(project)["runs"][0]["final_status"] == "failed"

    def test_unused_token_recorded_as_warning_issue(self, tmp_path, capsys):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{USED}}.demo (id INT);\n",
        )
        # Properties file defines an extra token never referenced in DDL
        props = project / "config" / "properties" / "DEV.properties"
        props.parent.mkdir(parents=True)
        props.write_text("USED=v1\nUNUSED=v2\n", encoding="utf-8")

        _cmd_scan(Namespace(source=str(project), properties=str(props)))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        warning_issues = [i for i in stage["issues"] if i["severity"] == "warning"]
        assert len(warning_issues) >= 1
        assert any(i["code"] == "TOKEN_UNUSED" for i in warning_issues)
        # Warnings require explicit set_status — verify it was set
        assert stage["status"] == "warning"
        assert _read_decisions(project)["runs"][0]["final_status"] == "warning"

    def test_missing_properties_file_recorded_as_error(self, tmp_path, capsys):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{X}}.demo (id INT);\n",
        )

        _cmd_scan(Namespace(
            source=str(project),
            properties=str(project / "nonexistent.properties"),
        ))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        codes = [i["code"] for i in stage["issues"]]
        assert "PROPERTIES_NOT_FOUND" in codes


# ---------------------------------------------------------------
# Append-only behaviour
# ---------------------------------------------------------------


class TestScanAppendsAcrossRuns:
    def test_second_scan_run_appends_to_manifest(self, tmp_path, capsys):
        project = _make_project(
            tmp_path,
            ddl_content="CREATE TABLE {{X}}.demo (id INT);\n",
        )

        _cmd_scan(Namespace(source=str(project), properties=None))
        _cmd_scan(Namespace(source=str(project), properties=None))
        capsys.readouterr()

        data = _read_decisions(project)
        assert len(data["runs"]) == 2
        # Distinct run_ids
        run_ids = [r["run_id"] for r in data["runs"]]
        assert len(set(run_ids)) == 2


# ---------------------------------------------------------------
# Project-detection gate (Flag 1)
# ---------------------------------------------------------------


class TestScanSkipsManifestForNonProjectDirectories:
    """Ad-hoc scans against arbitrary directories must not litter
    the filesystem with decisions.json. The gate is project
    detection (presence of payload/ or ships.yaml)."""

    def test_no_manifest_for_directory_without_payload(self, tmp_path, capsys):
        # Bare directory with a stray file but no payload/ or ships.yaml
        loose_dir = tmp_path / "loose"
        loose_dir.mkdir()
        (loose_dir / "stuff.sql").write_text(
            "CREATE TABLE {{X}}.demo (id INT);\n", encoding="utf-8"
        )

        _cmd_scan(Namespace(source=str(loose_dir), properties=None))
        capsys.readouterr()

        # Stdout still works; decisions.json must NOT have appeared
        assert not (loose_dir / "decisions.json").exists()

    def test_manifest_written_when_ships_yaml_present(self, tmp_path, capsys):
        """ships.yaml alone (no payload/) is sufficient to trigger
        manifest writing — it's the orchestrator config marker."""
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "ships.yaml").write_text("# minimal\n", encoding="utf-8")

        _cmd_scan(Namespace(source=str(proj), properties=None))
        capsys.readouterr()

        assert (proj / "decisions.json").exists()


# ---------------------------------------------------------------
# Helpers — _looks_like_ships_project, _NullStageRecorder
# ---------------------------------------------------------------


class TestProjectDetection:
    def test_payload_dir_detected(self, tmp_path):
        from td_release_packager.cli import _looks_like_ships_project
        (tmp_path / "payload").mkdir()
        assert _looks_like_ships_project(str(tmp_path)) is True

    def test_ships_yaml_detected(self, tmp_path):
        from td_release_packager.cli import _looks_like_ships_project
        (tmp_path / "ships.yaml").write_text("# ok\n", encoding="utf-8")
        assert _looks_like_ships_project(str(tmp_path)) is True

    def test_neither_marker_means_not_a_project(self, tmp_path):
        from td_release_packager.cli import _looks_like_ships_project
        (tmp_path / "random.txt").write_text("x", encoding="utf-8")
        assert _looks_like_ships_project(str(tmp_path)) is False

    def test_nonexistent_path_is_not_a_project(self, tmp_path):
        from td_release_packager.cli import _looks_like_ships_project
        assert _looks_like_ships_project(str(tmp_path / "nope")) is False


class TestNullStageRecorder:
    """All StageRecorder methods must be callable without raising,
    so the same call site works under either real or null recording."""

    def test_all_methods_are_no_ops(self):
        from td_release_packager.cli import _NullStageRecorder

        rec = _NullStageRecorder()
        # None of these should raise
        rec.set_status("success")
        rec.set_config_resolved("k", "v", "layer-5", "cli")
        rec.set_inputs(a=1, b="x")
        rec.set_outputs(c=2)
        rec.set_decisions(d="y")
        rec.add_issue("error", "TOKEN_UNDEFINED", "msg")
        rec.add_issue("warning", "TOKEN_UNUSED", "msg2", location="f.sql:3")
