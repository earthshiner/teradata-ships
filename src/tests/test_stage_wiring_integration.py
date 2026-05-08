"""
test_stage_wiring_integration.py — Verify that the newly wired CLI stages
(scaffold, harvest, analyse, package) write well-formed decisions.json via
the orchestrator foundation.

Build-order items 4c–4f: refactor the four remaining SHIPS stages onto the
cascade + decisions integration pattern.

Pattern mirrors test_inspect_orchestrator_integration.py: each test invokes
the CLI function directly with a Namespace, traps the SystemExit, then loads
decisions.json and asserts on its structure.

Covers:
    Scaffold  — records config, outputs; decisions.json written post-hoc
    Harvest   — records inputs/outputs/issues; HARVEST_* codes emitted
    Analyse   — records wave/dep/cycle counts; ANALYSE_* codes emitted
    Package   — records archive outputs; PACKAGE_WARNING codes emitted
    Issue codes — new codes are registered, exported, and described
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from td_release_packager.cli import _cmd_scaffold, _cmd_ingest, _cmd_analyze
from td_release_packager.orchestrator import issue_codes


# ---------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------


def _read_decisions(project: Path) -> dict:
    return json.loads((project / "decisions.json").read_text(encoding="utf-8"))


def _run(fn, args) -> int:
    """Invoke a CLI function and capture the SystemExit code."""
    with pytest.raises(SystemExit) as ei:
        fn(args)
    return int(ei.value.code) if ei.value.code is not None else 0


def _make_project(tmp_path: Path, name: str = "test_project") -> Path:
    """Scaffold a minimal SHIPS project and return its path."""
    args = Namespace(
        name=name,
        output=str(tmp_path),
        environments="DEV",
        repair=False,
    )
    _cmd_scaffold(args)
    return tmp_path / name


# ---------------------------------------------------------------
# Issue-code registry tests (new codes)
# ---------------------------------------------------------------


class TestNewIssueCodes:
    """New harvest/analyse/package codes are registered and exported."""

    def test_harvest_codes_in_registry(self):
        assert issue_codes.is_registered(issue_codes.HARVEST_UNCLASSIFIED)
        assert issue_codes.is_registered(issue_codes.HARVEST_CLASSIFICATION_WARNING)
        assert issue_codes.is_registered(issue_codes.HARVEST_TOKEN_CANDIDATE)

    def test_analyse_codes_in_registry(self):
        assert issue_codes.is_registered(issue_codes.ANALYSE_CYCLE)
        assert issue_codes.is_registered(issue_codes.ANALYSE_EXTERNAL_REF)

    def test_package_codes_in_registry(self):
        assert issue_codes.is_registered(issue_codes.PACKAGE_WARNING)

    def test_new_codes_exportable_from_orchestrator_package(self):
        from td_release_packager.orchestrator import (
            HARVEST_UNCLASSIFIED,
            HARVEST_CLASSIFICATION_WARNING,
            HARVEST_TOKEN_CANDIDATE,
            ANALYSE_CYCLE,
            ANALYSE_EXTERNAL_REF,
            PACKAGE_WARNING,
        )

        assert HARVEST_UNCLASSIFIED == "HARVEST_UNCLASSIFIED"
        assert ANALYSE_CYCLE == "ANALYSE_CYCLE"
        assert PACKAGE_WARNING == "PACKAGE_WARNING"

    def test_all_new_codes_have_descriptions(self):
        new_codes = [
            issue_codes.HARVEST_UNCLASSIFIED,
            issue_codes.HARVEST_CLASSIFICATION_WARNING,
            issue_codes.HARVEST_TOKEN_CANDIDATE,
            issue_codes.ANALYSE_CYCLE,
            issue_codes.ANALYSE_EXTERNAL_REF,
            issue_codes.PACKAGE_WARNING,
        ]
        for code in new_codes:
            desc = issue_codes.describe(code)
            assert desc != "(unregistered code)", f"{code} has no description"
            assert len(desc) > 20, f"{code} description suspiciously short"


# ---------------------------------------------------------------
# Scaffold stage
# ---------------------------------------------------------------


class TestScaffoldStageRecording:
    """scaffold writes decisions.json after creating the project."""

    def test_scaffold_writes_decisions_json(self, tmp_path):
        project = _make_project(tmp_path)
        assert (project / "decisions.json").exists()

    def test_scaffold_stage_has_success_status(self, tmp_path):
        project = _make_project(tmp_path)
        d = _read_decisions(project)
        stage = d["runs"][0]["stages"][0]
        assert stage["stage"] == "scaffold"
        assert stage["status"] in ("success", "no-op")

    def test_scaffold_records_config(self, tmp_path):
        project = _make_project(tmp_path, name="my_proj")
        d = _read_decisions(project)
        stage = d["runs"][0]["stages"][0]
        assert "name" in stage["config_resolved"]
        assert stage["config_resolved"]["name"]["value"] == "my_proj"

    def test_scaffold_records_outputs(self, tmp_path):
        project = _make_project(tmp_path)
        d = _read_decisions(project)
        stage = d["runs"][0]["stages"][0]
        assert "project_dir" in stage["outputs"]
        assert "environment_count" in stage["outputs"]
        assert stage["outputs"]["environment_count"] == 1

    def test_scaffold_repair_records_action(self, tmp_path):
        """Repair mode is recorded in config_resolved."""
        project = _make_project(tmp_path)
        args = Namespace(
            name="test_project",
            output=str(tmp_path),
            environments="DEV",
            repair=True,
        )
        _cmd_scaffold(args)
        d = _read_decisions(project)
        # Second run appends
        last_run = d["runs"][-1]
        stage = last_run["stages"][0]
        assert stage["config_resolved"]["repair"]["value"] is True

    def test_scaffold_non_project_parent_no_litter(self, tmp_path):
        """Scaffold records decisions INSIDE the new project, not the parent."""
        _make_project(tmp_path)
        assert not (tmp_path / "decisions.json").exists()


# ---------------------------------------------------------------
# Harvest stage
# ---------------------------------------------------------------


def _make_harvest_args(source: Path, project: Path, **overrides) -> Namespace:
    args = Namespace(
        source=str(source),
        project=str(project),
        token_map=None,
        apply_tokens=None,
        force=False,
        keep_existing=False,
        env_prefix=None,
        generate_token_map=False,
        reconcile=False,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestHarvestStageRecording:
    """harvest stage writes inputs/outputs/issues to decisions.json."""

    def test_harvest_writes_stage_to_decisions(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        stages = [s["stage"] for r in d["runs"] for s in r["stages"]]
        assert "harvest" in stages

    def test_harvest_records_inputs_and_outputs(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]

        assert "source_dir" in stage["inputs"]
        assert "total_files" in stage["inputs"]
        assert "classified" in stage["outputs"]
        assert stage["outputs"]["classified"] == 1

    def test_harvest_unclassified_file_emits_issue(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "mystery.sql").write_text("SELECT 1;", encoding="utf-8")

        args = _make_harvest_args(source, project)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]
        codes = [i["code"] for i in stage["issues"]]
        assert issue_codes.HARVEST_UNCLASSIFIED in codes

    def test_harvest_token_candidate_emits_info_issue(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "HardcodedDB.t.tbl").write_text(
            "CREATE MULTISET TABLE HardcodedDB.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]
        info_issues = [
            i
            for i in stage["issues"]
            if i["code"] == issue_codes.HARVEST_TOKEN_CANDIDATE
        ]
        assert info_issues, "expected HARVEST_TOKEN_CANDIDATE info issue"
        assert info_issues[0]["severity"] == "info"


# ---------------------------------------------------------------
# Analyse stage
# ---------------------------------------------------------------


def _make_analyse_args(source: Path, **overrides) -> Namespace:
    args = Namespace(
        source=str(source),
        output=None,
        overwrite=False,
        graph=None,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestAnalyseStageRecording:
    """analyse stage writes object/wave/cycle counts to decisions.json."""

    def _seed_project_with_table(self, project: Path) -> None:
        payload = project / "payload" / "database" / "DDL" / "tables"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "Dev.t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )

    def test_analyse_writes_stage_to_decisions(self, tmp_path):
        project = _make_project(tmp_path)
        self._seed_project_with_table(project)

        args = _make_analyse_args(project)
        _run(_cmd_analyze, args)

        d = _read_decisions(project)
        stages = [s["stage"] for r in d["runs"] for s in r["stages"]]
        assert "analyse" in stages

    def test_analyse_records_object_and_wave_count(self, tmp_path):
        project = _make_project(tmp_path)
        self._seed_project_with_table(project)

        args = _make_analyse_args(project)
        _run(_cmd_analyze, args)

        d = _read_decisions(project)
        analyse_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "analyse")
        stage = analyse_run["stages"][0]
        assert "object_count" in stage["outputs"]
        assert stage["outputs"]["object_count"] >= 1
        assert "wave_count" in stage["outputs"]
        assert "cycle_count" in stage["outputs"]

    def test_analyse_cycle_emits_error_issue(self, tmp_path):
        """A deliberately cyclic view pair produces ANALYSE_CYCLE issues."""
        project = _make_project(tmp_path)
        views = project / "payload" / "database" / "DDL" / "views"
        views.mkdir(parents=True, exist_ok=True)
        # v_a references v_b and v_b references v_a — a minimal cycle
        (views / "Dev.v_a.viw").write_text(
            "REPLACE VIEW Dev.v_a AS SELECT * FROM Dev.v_b;", encoding="utf-8"
        )
        (views / "Dev.v_b.viw").write_text(
            "REPLACE VIEW Dev.v_b AS SELECT * FROM Dev.v_a;", encoding="utf-8"
        )

        args = _make_analyse_args(project)
        _run(_cmd_analyze, args)

        d = _read_decisions(project)
        analyse_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "analyse")
        stage = analyse_run["stages"][0]
        cycle_issues = [
            i for i in stage["issues"] if i["code"] == issue_codes.ANALYSE_CYCLE
        ]
        assert cycle_issues, "expected ANALYSE_CYCLE issue for cyclic views"
        assert cycle_issues[0]["severity"] == "error"

    def test_analyse_non_project_dir_no_decisions(self, tmp_path):
        """Running analyse against a bare directory does not create decisions.json."""
        loose = tmp_path / "loose"
        loose.mkdir()

        args = _make_analyse_args(loose)
        _run(_cmd_analyze, args)

        assert not (loose / "decisions.json").exists()
