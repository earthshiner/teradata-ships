"""
test_project_index.py — Tests for ships.project.json (#271).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from td_release_packager.project_index import (
    ALL_LIFECYCLE_STATES,
    PROJECT_INDEX_FILENAME,
    PROJECT_INDEX_SCHEMA_VERSION,
    STATE_ANALYSED,
    STATE_HARVESTED,
    STATE_INSPECTED,
    STATE_PACKAGED,
    STATE_SCAFFOLDED,
    compute_project_index,
    load_project_index,
    write_project_index,
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
    (project / "ships.decisions.json").write_text(
        json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8"
    )


def _run(stage_name: str, status: str = "success") -> dict:
    return {"command": stage_name, "stages": [{"stage": stage_name, "status": status}]}


# ---------------------------------------------------------------
# Lifecycle derivation
# ---------------------------------------------------------------


class TestLifecycleDerivation:
    def test_bare_project_is_scaffolded(self, tmp_path):
        project = _make_project(tmp_path)
        index = compute_project_index(str(project))
        assert index.lifecycle_state == STATE_SCAFFOLDED

    def test_scaffold_only_stage_keeps_scaffolded(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("scaffold")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_SCAFFOLDED

    def test_harvest_advances_to_harvested(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("scaffold"), _run("harvest")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_HARVESTED

    def test_ingest_alias_also_advances_to_harvested(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("ingest")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_HARVESTED

    def test_inspect_advances_to_inspected(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("harvest"), _run("inspect")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_INSPECTED

    def test_analyse_advances_to_analysed(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("inspect"), _run("analyse")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_ANALYSED

    def test_analyze_us_spelling_also_works(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("inspect"), _run("analyze")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_ANALYSED

    def test_package_advances_to_packaged(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("inspect"), _run("package")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_PACKAGED

    def test_release_zip_on_disk_pins_to_packaged(self, tmp_path):
        # The decisions log doesn't mention package, but the release
        # archive does — that's enough to mark the project as packaged.
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("inspect")])
        releases = project / "releases"
        releases.mkdir()
        (releases / "DEV_pkg_BUILD_0001.zip").write_bytes(b"PK\x03\x04")
        assert compute_project_index(str(project)).lifecycle_state == STATE_PACKAGED

    def test_error_status_does_not_advance(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("harvest"), _run("inspect", status="error")])
        # Inspect errored — last successful stage was harvest.
        assert compute_project_index(str(project)).lifecycle_state == STATE_HARVESTED

    def test_unknown_stage_is_ignored(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("harvest"), _run("not_a_real_stage")])
        assert compute_project_index(str(project)).lifecycle_state == STATE_HARVESTED


# ---------------------------------------------------------------
# Next-recommended actions
# ---------------------------------------------------------------


class TestNextRecommendedActions:
    def test_every_state_has_at_least_one_recommendation(self, tmp_path):
        for state in ALL_LIFECYCLE_STATES:
            (tmp_path / state).mkdir()
            project = _make_project(tmp_path / state)
            if state == STATE_SCAFFOLDED:
                pass  # bare project is already SCAFFOLDED
            elif state == STATE_HARVESTED:
                _write_decisions(project, [_run("harvest")])
            elif state == STATE_INSPECTED:
                _write_decisions(project, [_run("inspect")])
            elif state == STATE_ANALYSED:
                _write_decisions(project, [_run("analyse")])
            elif state == STATE_PACKAGED:
                _write_decisions(project, [_run("package")])
            index = compute_project_index(str(project))
            assert index.next_recommended_actions
            # All entries are non-empty strings.
            for entry in index.next_recommended_actions:
                assert isinstance(entry, str) and entry.strip()

    def test_recommendation_contains_project_dir(self, tmp_path):
        project = _make_project(tmp_path)
        index = compute_project_index(str(project))
        assert any(str(project) in s for s in index.next_recommended_actions)


# ---------------------------------------------------------------
# References block
# ---------------------------------------------------------------


class TestReferences:
    def test_ships_yaml_reference_present(self, tmp_path):
        project = _make_project(tmp_path)
        index = compute_project_index(str(project))
        assert index.references.get("ships_yaml") == "ships.yaml"

    def test_decisions_log_reference_when_present(self, tmp_path):
        project = _make_project(tmp_path)
        _write_decisions(project, [_run("scaffold")])
        index = compute_project_index(str(project))
        assert index.references.get("decisions_log") == "ships.decisions.json"

    def test_env_configs_listed(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "config" / "env").mkdir(parents=True)
        (project / "config" / "env" / "DEV.conf").write_text(
            "SHIPS_ENV=DEV\n", encoding="utf-8", newline=""
        )
        (project / "config" / "env" / "PRD.conf").write_text(
            "SHIPS_ENV=PRD\n", encoding="utf-8", newline=""
        )
        index = compute_project_index(str(project))
        env_paths = index.references.get("env_configs", [])
        assert (
            "config/env/DEV.conf" in env_paths or "config\\env\\DEV.conf" in env_paths
        )

    def test_tokenise_config_reference_when_present(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "config").mkdir()
        (project / "config" / "tokenise.conf").write_text(
            "s/$A/{{A}}/g\n", encoding="utf-8", newline=""
        )
        index = compute_project_index(str(project))
        assert index.references.get("tokenise_config") == "config/tokenise.conf"

    def test_latest_package_reference_when_present(self, tmp_path):
        project = _make_project(tmp_path)
        (project / "releases").mkdir()
        (project / "releases" / "DEV_pkg_BUILD_0001.zip").write_bytes(b"PK\x03\x04")
        index = compute_project_index(str(project))
        assert "DEV_pkg_BUILD_0001.zip" in index.references.get("latest_package", "")


# ---------------------------------------------------------------
# Shape and serialisation
# ---------------------------------------------------------------


class TestShape:
    def test_schema_version_and_evaluated_at_emitted(self, tmp_path):
        index = compute_project_index(str(_make_project(tmp_path)))
        d = index.to_dict()
        assert d["schema_version"] == PROJECT_INDEX_SCHEMA_VERSION
        assert d["evaluated_at"]
        # ISO-8601-ish.
        assert "T" in d["evaluated_at"]

    def test_required_keys_present(self, tmp_path):
        index = compute_project_index(str(_make_project(tmp_path)))
        d = index.to_dict()
        for key in (
            "schema_version",
            "evaluated_at",
            "project_name",
            "project_dir",
            "lifecycle_state",
            "next_recommended_actions",
            "references",
            "actions_ref",
            "policy_ref",
        ):
            assert key in d

    def test_actions_ref_points_at_project_actions(self, tmp_path):
        # #273 wired actions_ref to ships.project_actions.json.
        index = compute_project_index(str(_make_project(tmp_path)))
        d = index.to_dict()
        assert d["actions_ref"] == "ships.project_actions.json"

    def test_policy_ref_points_at_project_policy(self, tmp_path):
        # #275 wired policy_ref to ships.project_policy.json.
        index = compute_project_index(str(_make_project(tmp_path)))
        d = index.to_dict()
        assert d["policy_ref"] == "ships.project_policy.json"

    def test_project_name_read_from_ships_yaml(self, tmp_path):
        project = _make_project(tmp_path, name="customised_name")
        # Override the ships.yaml to assert quoting handling too.
        (project / "ships.yaml").write_text(
            'name: "with spaces and quotes"\n', encoding="utf-8", newline=""
        )
        index = compute_project_index(str(project))
        assert index.project_name == "with spaces and quotes"

    def test_project_name_falls_back_to_dirname(self, tmp_path):
        project = tmp_path / "no_ships_yaml"
        project.mkdir()
        index = compute_project_index(str(project))
        assert index.project_name == "no_ships_yaml"


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_write_then_load(self, tmp_path):
        project = _make_project(tmp_path)
        path = write_project_index(str(project))
        assert path.endswith(PROJECT_INDEX_FILENAME)
        loaded = load_project_index(str(project))
        assert loaded["schema_version"] == PROJECT_INDEX_SCHEMA_VERSION
        assert loaded["lifecycle_state"] == STATE_SCAFFOLDED

    def test_load_returns_none_when_absent(self, tmp_path):
        project = _make_project(tmp_path)
        assert load_project_index(str(project)) is None

    def test_load_returns_none_when_corrupt(self, tmp_path):
        project = _make_project(tmp_path)
        (project / PROJECT_INDEX_FILENAME).write_text("not json", encoding="utf-8")
        assert load_project_index(str(project)) is None


# ---------------------------------------------------------------
# Integration: stage recording refreshes the index
# ---------------------------------------------------------------


class TestStageRecordingRefreshesIndex:
    def test_scaffold_writes_project_index(self, tmp_path):
        """An end-to-end scaffold run leaves ships.project.json in place."""
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
        index_file = project / PROJECT_INDEX_FILENAME
        assert index_file.is_file()
        data = json.loads(index_file.read_text(encoding="utf-8"))
        assert data["lifecycle_state"] == STATE_SCAFFOLDED
        assert data["project_name"] == "auto_test"
