"""Tests for per-stage result emission (#145)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from td_release_packager.cli import (
    STAGE_RESULT_SCHEMA,
    _build_standalone_stage_results,
    _collect_latest_stage_entries,
    _next_action_for,
    _normalise_stage_result,
)


# ---------------------------------------------------------------
# _next_action_for
# ---------------------------------------------------------------


class TestNextAction:
    def test_explicit_decision_wins(self):
        entry = {
            "stage": "inspect",
            "status": "success",
            "decisions": {"next_action": "Custom directive."},
        }
        assert _next_action_for(entry) == "Custom directive."

    def test_inspect_success_suggests_analyse(self):
        entry = {"stage": "inspect", "status": "success", "decisions": {}}
        assert "ships analyse" in _next_action_for(entry)

    def test_inspect_error_suggests_remediation(self):
        entry = {"stage": "inspect", "status": "error", "decisions": {}}
        action = _next_action_for(entry)
        assert "ships.rules.json" in action or "remediate" in action.lower()

    def test_package_success_suggests_ship(self):
        entry = {"stage": "package", "status": "success", "decisions": {}}
        assert "ships ship" in _next_action_for(entry)

    def test_unknown_stage_returns_empty(self):
        entry = {"stage": "unknown_stage", "status": "success", "decisions": {}}
        assert _next_action_for(entry) == ""


# ---------------------------------------------------------------
# _normalise_stage_result
# ---------------------------------------------------------------


class TestNormaliseStageResult:
    def test_all_required_fields_present(self):
        entry = {
            "stage": "inspect",
            "status": "success",
            "started_at": "2026-06-14T12:00:00+00:00",
            "finished_at": "2026-06-14T12:00:02+00:00",
            "duration_ms": 2000,
            "inputs": {"project": "."},
            "outputs": {"files_scanned": 12},
            "decisions": {},
            "issues": [],
        }
        doc = _normalise_stage_result(entry)
        for key in (
            "schema",
            "stage",
            "status",
            "started_at",
            "finished_at",
            "duration_ms",
            "inputs",
            "outputs",
            "decisions",
            "issues",
            "issue_counts",
            "next_action",
        ):
            assert key in doc
        assert doc["schema"] == STAGE_RESULT_SCHEMA

    def test_issue_counts_aggregated(self):
        entry = {
            "stage": "inspect",
            "status": "warning",
            "issues": [
                {"severity": "error", "code": "x", "message": "a"},
                {"severity": "error", "code": "x", "message": "b"},
                {"severity": "warning", "code": "y", "message": "c"},
                {"severity": "info", "code": "z", "message": "d"},
            ],
        }
        doc = _normalise_stage_result(entry)
        assert doc["issue_counts"] == {"error": 2, "warning": 1, "info": 1}


# ---------------------------------------------------------------
# _collect_latest_stage_entries
# ---------------------------------------------------------------


class TestCollectLatestStageEntries:
    def test_empty_data_returns_empty_dict(self):
        assert _collect_latest_stage_entries({}) == {}
        assert _collect_latest_stage_entries({"runs": []}) == {}

    def test_one_run_per_stage(self):
        data = {
            "runs": [
                {
                    "stages": [
                        {"stage": "harvest", "finished_at": "t1"},
                        {"stage": "inspect", "finished_at": "t2"},
                    ]
                }
            ]
        }
        latest = _collect_latest_stage_entries(data)
        assert set(latest.keys()) == {"harvest", "inspect"}

    def test_most_recent_wins(self):
        data = {
            "runs": [
                {"stages": [{"stage": "inspect", "finished_at": "t1", "tag": "old"}]},
                {"stages": [{"stage": "inspect", "finished_at": "t2", "tag": "new"}]},
            ]
        }
        latest = _collect_latest_stage_entries(data)
        assert latest["inspect"]["tag"] == "new"

    def test_unfinished_entries_skipped(self):
        data = {
            "runs": [
                {"stages": [{"stage": "inspect", "finished_at": "t1", "tag": "done"}]},
                {
                    "stages": [
                        {"stage": "inspect", "finished_at": None, "tag": "partial"}
                    ]
                },
            ]
        }
        latest = _collect_latest_stage_entries(data)
        assert latest["inspect"]["tag"] == "done"


# ---------------------------------------------------------------
# _build_standalone_stage_results
# ---------------------------------------------------------------


@pytest.fixture
def decisions_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    decisions = {
        "schema_version": "1.0",
        "runs": [
            {
                "run_id": "r1",
                "stages": [
                    {
                        "stage": "harvest",
                        "status": "success",
                        "started_at": "2026-06-14T10:00:00+00:00",
                        "finished_at": "2026-06-14T10:00:05+00:00",
                        "duration_ms": 5000,
                        "inputs": {"project": str(project)},
                        "outputs": {"files_harvested": 12},
                        "decisions": {},
                        "issues": [],
                    },
                ],
            },
            {
                "run_id": "r2",
                "stages": [
                    {
                        "stage": "inspect",
                        "status": "success",
                        "started_at": "2026-06-14T10:05:00+00:00",
                        "finished_at": "2026-06-14T10:05:03+00:00",
                        "duration_ms": 3000,
                        "inputs": {"project": str(project)},
                        "outputs": {"rules_evaluated": 26},
                        "decisions": {},
                        "issues": [],
                    },
                ],
            },
            {
                "run_id": "r3",
                "stages": [
                    {
                        "stage": "analyse",
                        "status": "success",
                        "started_at": "2026-06-14T10:10:00+00:00",
                        "finished_at": "2026-06-14T10:10:01+00:00",
                        "duration_ms": 1000,
                        "inputs": {},
                        "outputs": {"objects": 2, "waves": 2},
                        "decisions": {},
                        "issues": [],
                    },
                ],
            },
        ],
    }
    (project / "ships.decisions.json").write_text(
        json.dumps(decisions), encoding="utf-8"
    )
    return project


class TestBuildStandaloneStageResults:
    def test_collects_all_three_stages(self, decisions_project):
        results = _build_standalone_stage_results(str(decisions_project))
        assert set(results.keys()) == {
            "harvest.result.json",
            "inspect.result.json",
            "analyse.result.json",
        }

    def test_each_result_validates_against_schema(self, decisions_project):
        from td_release_packager.context_artifacts import DEFAULT_SCHEMAS

        schema = DEFAULT_SCHEMAS["ships.stage_result.schema.json"]
        Draft202012Validator.check_schema(schema)
        results = _build_standalone_stage_results(str(decisions_project))
        for filename, doc in results.items():
            Draft202012Validator(schema).validate(doc)

    def test_current_stage_entry_override(self, decisions_project):
        """An in-flight package stage should appear in results even though
        DecisionsManifest may not have flushed it yet."""
        current = {
            "stage": "package",
            "status": "success",
            "started_at": "2026-06-14T10:15:00+00:00",
            "finished_at": "2026-06-14T10:15:01+00:00",
            "duration_ms": 1000,
            "inputs": {},
            "outputs": {"archive_path": "/tmp/pkg.zip"},
            "decisions": {},
            "issues": [],
        }
        results = _build_standalone_stage_results(
            str(decisions_project), current_stage_entry=current
        )
        assert "package.result.json" in results
        assert (
            results["package.result.json"]["outputs"]["archive_path"] == "/tmp/pkg.zip"
        )

    def test_missing_decisions_file_no_crash(self, tmp_path):
        empty = tmp_path / "empty_proj"
        empty.mkdir()
        results = _build_standalone_stage_results(str(empty))
        assert results == {}


# ---------------------------------------------------------------
# End-to-end: standalone package build emits per-stage results
# ---------------------------------------------------------------


class TestStandaloneBuildEmitsStageResults:
    """Build a package via the CLI's `package` command flow and confirm
    the archive carries per-stage result files for harvest / inspect /
    analyse / package even though they ran as separate stages."""

    def test_archive_contains_all_stage_results(self, tmp_path):
        # Set up minimal project with a recorded harvest, inspect, analyse
        # in decisions.json plus a packageable payload.
        from td_release_packager.builder import build_package
        from td_release_packager.cli import (
            _build_standalone_stage_results,
            _write_process_results_to_zip,
        )
        from td_release_packager.models import BuildConfig

        project = tmp_path / "proj"
        payload = project / "payload" / "database" / "DDL" / "tables"
        payload.mkdir(parents=True)
        (payload / "DOM_T.TBL.tbl").write_text(
            "CREATE TABLE {{CORE_T}}.Customer (id INT);\n",
            encoding="utf-8",
        )
        env_conf = project / "config" / "env" / "DEV.conf"
        env_conf.parent.mkdir(parents=True)
        env_conf.write_text("CORE_T = DEV_CORE_T\n", encoding="utf-8")
        (project / ".ships").mkdir(parents=True, exist_ok=True)
        (project / ".ships" / ".build_counter").write_text("0", encoding="utf-8")

        # Seed decisions.json with harvest+inspect+analyse stages from
        # separate prior runs.
        decisions = {
            "schema_version": "1.0",
            "runs": [
                {
                    "run_id": "harvest_run",
                    "stages": [
                        {
                            "stage": "harvest",
                            "status": "success",
                            "started_at": "2026-06-14T09:00:00+00:00",
                            "finished_at": "2026-06-14T09:00:01+00:00",
                            "duration_ms": 1000,
                            "inputs": {},
                            "outputs": {"files": 1},
                            "decisions": {},
                            "issues": [],
                        }
                    ],
                },
                {
                    "run_id": "inspect_run",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "success",
                            "started_at": "2026-06-14T09:01:00+00:00",
                            "finished_at": "2026-06-14T09:01:01+00:00",
                            "duration_ms": 1000,
                            "inputs": {},
                            "outputs": {"rules": 26},
                            "decisions": {},
                            "issues": [],
                        }
                    ],
                },
                {
                    "run_id": "analyse_run",
                    "stages": [
                        {
                            "stage": "analyse",
                            "status": "success",
                            "started_at": "2026-06-14T09:02:00+00:00",
                            "finished_at": "2026-06-14T09:02:01+00:00",
                            "duration_ms": 1000,
                            "inputs": {},
                            "outputs": {"waves": 1},
                            "decisions": {},
                            "issues": [],
                        }
                    ],
                },
            ],
        }
        (project / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )

        config = BuildConfig(
            source_dir=str(project),
            environment="DEV",
            package_name="stage_smoke",
            env_config_file=str(env_conf),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (archive_path, _manifest), _companion = build_package(config)

        # Simulate the CLI tail end: build standalone stage results +
        # write them into the archive. This mirrors what _run_build does
        # after build_package returns.
        current = {
            "stage": "package",
            "status": "success",
            "started_at": "2026-06-14T09:03:00+00:00",
            "finished_at": "2026-06-14T09:03:02+00:00",
            "duration_ms": 2000,
            "inputs": {},
            "outputs": {"archive_path": archive_path},
            "decisions": {},
            "issues": [],
        }
        results = _build_standalone_stage_results(
            str(project), current_stage_entry=current
        )
        _write_process_results_to_zip(archive_path, results)

        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            for stage in ("harvest", "inspect", "analyse", "package"):
                hit = [n for n in names if n.endswith(f"{stage}.result.json")]
                assert hit, f"{stage}.result.json missing from archive"
                doc = json.loads(zf.read(hit[0]))
                assert doc["schema"] == STAGE_RESULT_SCHEMA
                assert doc["stage"] == stage
                assert doc["status"] == "success"
                assert isinstance(doc["next_action"], str)
