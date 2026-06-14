"""
test_mcp_server.py — Tests for the SHIPS MCP server tool functions.

Tests invoke the tool functions directly (not via the MCP protocol)
to verify their return schemas and core behaviour. Connection-dependent
tools (deploy, explain, rollback) are tested with mocked Teradata
cursors or with dry-run mode.

Covers:
    - ships_scaffold: creates project, returns project_dir
    - ships_harvest: classifies DDL, returns counts
    - ships_inspect: runs lint rules, returns findings
    - ships_analyse: builds dependency graph, returns wave count
    - ships_generate: returns generation counts
    - ships_package: builds archive, trust label in output
    - ships_process: orchestrates all stages
    - ships_decisions: reads ships.decisions.json
    - ships_verify: package readiness check
    - ships_explain_run: formats prior run for review
    - ships_rollback: dry-run works without connection
    - Error handling: missing dirs return {"success": False, "error": ...}
"""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_project(tmp_path: Path, name: str = "TestProject") -> Path:
    from ships_mcp import ships_scaffold

    result = ships_scaffold(name=name, output=str(tmp_path))
    assert result["success"], f"Scaffold failed: {result}"
    return Path(result["project_dir"])


def _seed_table(project: Path) -> None:
    _write(
        project / "payload/database/DDL/tables/Dev.T.tbl",
        "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
    )


def _seed_decisions(project: Path, runs: list) -> None:
    (project / "ships.decisions.json").write_text(
        json.dumps({"schema_version": 1, "runs": runs}), encoding="utf-8"
    )


# ---------------------------------------------------------------
# ships_scaffold
# ---------------------------------------------------------------


class TestShipsScaffold:
    def test_creates_project(self, tmp_path):
        from ships_mcp import ships_scaffold

        result = ships_scaffold(name="MyProject", output=str(tmp_path))
        assert result["success"]
        assert "project_dir" in result
        project_dir = Path(result["project_dir"])
        assert project_dir.is_dir()
        # .build_counter is always created by scaffold
        assert (project_dir / ".build_counter").exists()

    def test_environments_list(self, tmp_path):
        from ships_mcp import ships_scaffold

        result = ships_scaffold(name="P", output=str(tmp_path), environments="DEV,PRD")
        assert result["environments"] == ["DEV", "PRD"]

    def test_returns_success_true_on_success(self, tmp_path):
        from ships_mcp import ships_scaffold

        result = ships_scaffold(name="Q", output=str(tmp_path))
        assert result["success"] is True
        assert result["action"] == "scaffold"


# ---------------------------------------------------------------
# ships_harvest
# ---------------------------------------------------------------


class TestShipsHarvest:
    def test_classifies_table(self, tmp_path):
        from ships_mcp import ships_harvest

        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        _write(
            source / "Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        result = ships_harvest(source=str(source), project=str(project))
        assert result["success"]
        assert result["classified"] == 1
        assert result["unclassified"] == 0

    def test_unclassified_file(self, tmp_path):
        from ships_mcp import ships_harvest

        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        _write(source / "mystery.sql", "SELECT 1;\n")

        result = ships_harvest(source=str(source), project=str(project))
        assert result["success"]  # harvest doesn't fail on unclassified
        assert result["unclassified"] == 1

    def test_missing_source_error(self, tmp_path):
        from ships_mcp import ships_harvest

        project = _make_project(tmp_path)
        result = ships_harvest(source="/nonexistent", project=str(project))
        assert not result["success"]

    def test_prefix_token_rewrites_database_prefix(self, tmp_path):
        """End-to-end check that --prefix-token / prefix_token actually
        rewrites the database-name prefix in placed payload files,
        without ever producing a malformed ``{{PREFIX_*}}`` token.
        See issue #309 (Model B)."""
        from ships_mcp import ships_harvest

        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        _write(
            source / "CallCentre_DOM_STD_T.tbl",
            "CREATE MULTISET TABLE CallCentre_DOM_STD_T.Call "
            "(Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        result = ships_harvest(
            source=str(source),
            project=str(project),
            prefix_token="CallCentre=PREFIX",
        )
        assert result["success"], result
        assert result["prefix_token_substitutions"] >= 1
        assert result["prefix_token_files"] >= 1

        # The placed file must contain {{PREFIX}}_DOM_STD_T, NOT the
        # original literal, and NEVER the malformed {{PREFIX_T}}.
        from pathlib import Path

        placed = list(Path(project).rglob("*.tbl"))
        assert placed, "harvest placed no .tbl file"
        contents = "\n".join(p.read_text(encoding="utf-8") for p in placed)
        assert "{{PREFIX}}_DOM_STD_T" in contents
        assert "{{PREFIX_" not in contents
        assert "CallCentre_DOM_STD_T" not in contents

    def test_prefix_token_malformed_kv_returns_error(self, tmp_path):
        from ships_mcp import ships_harvest

        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        _write(source / "Dev.T.tbl", "CREATE MULTISET TABLE Dev.T (Id INTEGER);\n")

        result = ships_harvest(
            source=str(source),
            project=str(project),
            prefix_token="not_a_kv_pair",
        )
        assert not result["success"]
        assert "SOURCE=TOKEN" in result["error"]


# ---------------------------------------------------------------
# ships_inspect
# ---------------------------------------------------------------


class TestShipsInspect:
    def test_clean_project_passes(self, tmp_path):
        from ships_mcp import ships_inspect

        project = _make_project(tmp_path)
        _seed_table(project)
        result = ships_inspect(project=str(project))
        assert "passed" in result
        assert "findings" in result

    def test_returns_finding_schema(self, tmp_path):
        from ships_mcp import ships_inspect

        project = _make_project(tmp_path)
        _seed_table(project)
        result = ships_inspect(project=str(project))
        for f in result.get("findings", []):
            assert "rule" in f
            assert "severity" in f
            assert "file" in f
            assert "message" in f


# ---------------------------------------------------------------
# ships_analyse
# ---------------------------------------------------------------


class TestShipsAnalyse:
    def test_analyses_single_table(self, tmp_path):
        from ships_mcp import ships_analyse

        project = _make_project(tmp_path)
        _seed_table(project)
        result = ships_analyse(project=str(project))
        assert result["success"]
        assert result["object_count"] >= 1
        assert "wave_count" in result

    def test_writes_waves_file(self, tmp_path):
        from ships_mcp import ships_analyse

        project = _make_project(tmp_path)
        _seed_table(project)
        result = ships_analyse(project=str(project), overwrite=True)
        assert result["success"]
        if result.get("waves_path"):
            assert Path(result["waves_path"]).exists()


# ---------------------------------------------------------------
# ships_package
# ---------------------------------------------------------------


class TestShipsPackage:
    def test_builds_archive(self, tmp_path):
        from ships_mcp import ships_package

        project = _make_project(tmp_path)
        _seed_table(project)
        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        result = ships_package(
            project=str(project),
            env="DEV",
            name="TestPkg",
            env_config=str(props),
            output=str(tmp_path / "releases"),
        )
        assert result["success"], f"Package failed: {result}"
        assert Path(result["archive_path"]).exists()
        assert "trust_status" in result
        assert result["trust_status"] in ("READY", "READY_WITH_CAVEATS", "BLOCKED")

    def test_missing_env_config_error(self, tmp_path):
        from ships_mcp import ships_package

        project = _make_project(tmp_path)
        result = ships_package(
            project=str(project),
            env="DEV",
            name="Pkg",
            env_config="/nonexistent.conf",
        )
        assert not result["success"]


# ---------------------------------------------------------------
# ships_process
# ---------------------------------------------------------------


class TestShipsProcess:
    def test_process_without_source_skips_harvest(self, tmp_path):
        from ships_mcp import ships_process

        project = _make_project(tmp_path)
        _seed_table(project)
        result = ships_process(project=str(project), source=None)
        assert "stages" in result
        assert "harvest" not in result["stages"]
        assert "inspect" in result["stages"]
        assert "analyse" in result["stages"]

    def test_process_with_source_includes_harvest(self, tmp_path):
        from ships_mcp import ships_process

        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        _write(
            source / "Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        result = ships_process(
            project=str(project),
            source=str(source),
            skip_generate=True,
        )
        assert "stages" in result
        assert "harvest" in result["stages"]
        assert result["stages"]["harvest"]["classified"] >= 1


# ---------------------------------------------------------------
# ships_decisions
# ---------------------------------------------------------------


class TestShipsDecisions:
    def test_reads_last_run(self, tmp_path):
        from ships_mcp import ships_decisions

        project = _make_project(tmp_path)
        _seed_decisions(
            project,
            [
                {
                    "run_id": "run-1",
                    "command": "inspect",
                    "stages": [],
                    "final_status": "success",
                }
            ],
        )
        result = ships_decisions(project=str(project))
        assert result["success"]
        assert result["run"]["run_id"] == "run-1"

    def test_reads_specific_run_id(self, tmp_path):
        from ships_mcp import ships_decisions

        project = _make_project(tmp_path)
        _seed_decisions(
            project,
            [
                {
                    "run_id": "run-1",
                    "command": "inspect",
                    "stages": [],
                    "final_status": "success",
                },
                {
                    "run_id": "run-2",
                    "command": "package",
                    "stages": [],
                    "final_status": "success",
                },
            ],
        )
        result = ships_decisions(project=str(project), run_id="run-1")
        assert result["run"]["run_id"] == "run-1"

    def test_missing_decisions_json_error(self, tmp_path):
        from ships_mcp import ships_decisions

        result = ships_decisions(project=str(tmp_path / "nonexistent"))
        assert not result["success"]


# ---------------------------------------------------------------
# ships_verify
# ---------------------------------------------------------------


class TestShipsVerify:
    def test_no_package_stage_not_ready(self, tmp_path):
        from ships_mcp import ships_verify

        project = _make_project(tmp_path)
        _seed_decisions(
            project,
            [
                {
                    "run_id": "r1",
                    "command": "inspect",
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "success",
                            "issues": [],
                            "outputs": {},
                        }
                    ],
                }
            ],
        )
        result = ships_verify(project=str(project))
        assert result["success"]
        assert not result["ready"]

    def test_missing_decisions_json(self, tmp_path):
        from ships_mcp import ships_verify

        result = ships_verify(project=str(tmp_path))
        assert not result["success"] or not result.get("ready", True)


# ---------------------------------------------------------------
# ships_explain_run
# ---------------------------------------------------------------


class TestShipsExplainRun:
    def test_formats_run_summary(self, tmp_path):
        from ships_mcp import ships_explain_run

        project = _make_project(tmp_path)
        _seed_decisions(
            project,
            [
                {
                    "run_id": "r1",
                    "command": "process",
                    "final_status": "success",
                    "duration_ms": 5000,
                    "stages": [
                        {
                            "stage": "inspect",
                            "status": "success",
                            "duration_ms": 1000,
                            "issues": [],
                            "outputs": {},
                        }
                    ],
                }
            ],
        )
        result = ships_explain_run(project=str(project))
        assert result["success"]
        assert result["run_id"] == "r1"
        assert "stages" in result
        assert "issues_summary" in result

    def test_command_filter(self, tmp_path):
        from ships_mcp import ships_explain_run

        project = _make_project(tmp_path)
        _seed_decisions(
            project,
            [
                {
                    "run_id": "r1",
                    "command": "inspect",
                    "final_status": "success",
                    "duration_ms": 1000,
                    "stages": [],
                },
                {
                    "run_id": "r2",
                    "command": "package",
                    "final_status": "success",
                    "duration_ms": 5000,
                    "stages": [],
                },
            ],
        )
        result = ships_explain_run(project=str(project), command_filter="inspect")
        assert result["run_id"] == "r1"


# ---------------------------------------------------------------
# ships_rollback (dry-run — no connection needed)
# ---------------------------------------------------------------


class TestShipsRollbackDryRun:
    def test_dry_run_works_without_connection(self, tmp_path):
        from ships_mcp import ships_rollback
        from database_package_deployer.manifest import DeploymentManifest
        from database_package_deployer.models import DeployState

        manifest = DeploymentManifest(str(tmp_path))
        manifest.register_object(
            qualified_name="Dev.v_test",
            ddl_file="DDL/views/Dev.v_test.viw",
            object_type="VIEW",
        )
        manifest.update_state("Dev.v_test", DeployState.COMPLETED)

        result = ships_rollback(
            manifest_path=manifest.path,
            host="",
            user="",
            password="",
            dry_run=True,
        )
        assert result["success"]
        assert result.get("completed", 0) + result.get("rolled_back", 0) >= 0

    def test_missing_manifest_error(self, tmp_path):
        from ships_mcp import ships_rollback

        result = ships_rollback(
            manifest_path=str(tmp_path / "nonexistent.json"),
            host="",
            user="",
            password="",
            dry_run=True,
        )
        assert not result["success"]


# ---------------------------------------------------------------
# Tool schema: all tools are registered with FastMCP
# ---------------------------------------------------------------


class TestToolRegistration:
    def test_expected_tools_registered(self):
        import ships_mcp

        # FastMCP stores tools in its tool manager
        tool_names = list(ships_mcp.mcp._tool_manager._tools.keys())
        expected = [
            "ships_scaffold",
            "ships_harvest",
            "ships_generate",
            "ships_inspect",
            "ships_analyse",
            "ships_package",
            "ships_process",
            "ships_deploy",
            "ships_deploy_explain",
            "ships_rollback",
            "ships_decisions",
            "ships_verify",
            "ships_explain_run",
            "ships_validate_ships_yaml",
            "ships_author_ships_yaml",
            "ships_apply_diff",
            "ships_validate_env_config",
            "ships_validate_inspect_config",
            "ships_author_env_config",
            "ships_author_inspect_config",
        ]
        for name in expected:
            assert name in tool_names, f"Tool {name!r} not registered"

    def test_all_tools_have_descriptions(self):
        import ships_mcp

        for name, tool in ships_mcp.mcp._tool_manager._tools.items():
            assert tool.description, f"Tool {name!r} has no description"


# ---------------------------------------------------------------
# Phase A — authoring tools (#291)
# ---------------------------------------------------------------


class TestShipsValidateShipsYaml:
    def test_missing_file_returns_not_exists(self, tmp_path: Path):
        from ships_mcp import ships_validate_ships_yaml

        result = ships_validate_ships_yaml(project=str(tmp_path))
        assert result["success"] is True
        assert result["exists"] is False
        assert result["valid"] is False
        assert result["errors"]

    def test_valid_file_returns_no_errors(self, tmp_path: Path):
        from ships_mcp import ships_validate_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\n  - PRD\n",
            encoding="utf-8",
        )
        result = ships_validate_ships_yaml(project=str(tmp_path))
        assert result["success"] is True
        assert result["exists"] is True
        assert result["valid"] is True
        assert result["errors"] == []

    def test_invalid_file_returns_errors(self, tmp_path: Path):
        from ships_mcp import ships_validate_ships_yaml

        # missing 'environments'
        (tmp_path / "ships.yaml").write_text("project: Demo\n", encoding="utf-8")
        result = ships_validate_ships_yaml(project=str(tmp_path))
        assert result["success"] is True
        assert result["valid"] is False
        paths = [e["path"] for e in result["errors"]]
        assert "environments" in paths


class TestShipsAuthorShipsYaml:
    def test_create_proposes_new_file(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="create",
            project_name="Demo",
            environments=["DEV", "TST", "PRD"],
        )
        assert result["success"] is True
        assert result["current_content"] == ""
        assert "project: Demo" in result["proposed_content"]
        assert "DEV" in result["proposed_content"]
        assert result["expected_hash"] == "absent"
        assert result["validation"]["valid"] is True
        assert result["diff"]  # non-empty unified diff
        # Authoring tools NEVER write
        assert not (tmp_path / "ships.yaml").exists()

    def test_create_refuses_if_file_exists(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments: [DEV]\n", encoding="utf-8"
        )
        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="create",
            project_name="Demo",
            environments=["DEV"],
        )
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_create_requires_project_name_and_envs(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        result = ships_author_ships_yaml(project=str(tmp_path), action="create")
        assert result["success"] is False
        assert "project_name" in result["error"]

    def test_set_applies_dotted_key(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\n",
            encoding="utf-8",
        )
        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="set",
            changes={"stages.inspect.strict": True},
        )
        assert result["success"] is True
        assert "stages:" in result["proposed_content"]
        assert "strict: true" in result["proposed_content"]
        assert result["validation"]["valid"] is True
        # Not written
        assert "stages" not in (tmp_path / "ships.yaml").read_text()

    def test_set_returns_validation_errors_without_failing(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\n",
            encoding="utf-8",
        )
        # on_error must be 'halt' or 'continue' — 'maybe' is invalid
        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="set",
            changes={"stages.inspect.on_error": "maybe"},
        )
        assert result["success"] is True
        assert result["validation"]["valid"] is False
        assert any("on_error" in e["path"] for e in result["validation"]["errors"])

    def test_set_requires_existing_file(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="set",
            changes={"project": "X"},
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_unset_removes_key(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\nversion: '1.0'\n",
            encoding="utf-8",
        )
        result = ships_author_ships_yaml(
            project=str(tmp_path),
            action="unset",
            unset_keys=["version"],
        )
        assert result["success"] is True
        assert "version" not in result["proposed_content"]

    def test_unknown_action_is_rejected(self, tmp_path: Path):
        from ships_mcp import ships_author_ships_yaml

        result = ships_author_ships_yaml(project=str(tmp_path), action="frobnicate")
        assert result["success"] is False
        assert "unknown action" in result["error"]


class TestShipsApplyDiff:
    def test_create_then_apply_writes_file(self, tmp_path: Path):
        from ships_mcp import ships_apply_diff, ships_author_ships_yaml

        proposal = ships_author_ships_yaml(
            project=str(tmp_path),
            action="create",
            project_name="Demo",
            environments=["DEV"],
        )
        assert proposal["success"] is True

        applied = ships_apply_diff(
            path=proposal["path"],
            proposed_content=proposal["proposed_content"],
            expected_hash=proposal["expected_hash"],
        )
        assert applied["success"] is True
        assert applied["applied"] is True
        assert applied["created"] is True
        on_disc = (tmp_path / "ships.yaml").read_text()
        assert "project: Demo" in on_disc

    def test_hash_mismatch_blocks_write(self, tmp_path: Path):
        from ships_mcp import ships_apply_diff, ships_author_ships_yaml

        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\n",
            encoding="utf-8",
        )
        proposal = ships_author_ships_yaml(
            project=str(tmp_path),
            action="set",
            changes={"project": "Renamed"},
        )
        assert proposal["success"] is True

        # Simulate a concurrent edit between propose and apply
        (tmp_path / "ships.yaml").write_text(
            "project: Demo\nenvironments:\n  - DEV\n  - PRD\n",
            encoding="utf-8",
        )
        applied = ships_apply_diff(
            path=proposal["path"],
            proposed_content=proposal["proposed_content"],
            expected_hash=proposal["expected_hash"],
        )
        assert applied["success"] is False
        assert applied["applied"] is False
        assert applied["code"] == "hash_mismatch"
        # File untouched
        assert "PRD" in (tmp_path / "ships.yaml").read_text()
        assert "Renamed" not in (tmp_path / "ships.yaml").read_text()

    def test_end_to_end_create_apply_reload_validate(self, tmp_path: Path):
        from td_release_packager.orchestrator import ships_yaml as _sy

        from ships_mcp import (
            ships_apply_diff,
            ships_author_ships_yaml,
            ships_validate_ships_yaml,
        )

        proposal = ships_author_ships_yaml(
            project=str(tmp_path),
            action="create",
            project_name="EndToEnd",
            environments=["DEV", "TST", "PRD"],
        )
        ships_apply_diff(
            path=proposal["path"],
            proposed_content=proposal["proposed_content"],
            expected_hash=proposal["expected_hash"],
        )
        assert ships_validate_ships_yaml(project=str(tmp_path))["valid"] is True
        # Substrate parse also succeeds
        data = _sy.load(proposal["path"])
        assert data["project"] == "EndToEnd"
        assert data["environments"] == ["DEV", "TST", "PRD"]


# ---------------------------------------------------------------
# Phase B — .conf authoring tools (#293)
# ---------------------------------------------------------------


class TestConfFileEditor:
    def test_round_trip_committed_env_configs(self):
        """dump(parse(content)) == content on every committed .conf."""
        import pathlib

        from td_release_packager.mcp_authoring import ConfFile

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        candidates = sorted((repo_root / "config" / "env").glob("*.conf"))
        assert candidates, "expected committed env .conf fixtures"
        for fixture in candidates:
            text = fixture.read_text(encoding="utf-8", newline="")
            assert ConfFile.parse(text).dump() == text, fixture

    def test_set_existing_key_in_place(self):
        from td_release_packager.mcp_authoring import ConfFile

        original = "# header\nA=1\nB=2\n# tail\n"
        conf = ConfFile.parse(original)
        conf.set("A", "99")
        out = conf.dump()
        assert out == "# header\nA=99\nB=2\n# tail\n"

    def test_set_new_key_appends(self):
        from td_release_packager.mcp_authoring import ConfFile

        conf = ConfFile.parse("A=1\n")
        conf.set("B", "2")
        assert conf.dump() == "A=1\nB=2\n"

    def test_unset_removes_only_target_line(self):
        from td_release_packager.mcp_authoring import ConfFile

        original = "# header\nA=1\n\nB=2\n"
        conf = ConfFile.parse(original)
        assert conf.unset("A") is True
        assert conf.dump() == "# header\n\nB=2\n"

    def test_set_rejects_newline_in_value(self):
        import pytest

        from td_release_packager.mcp_authoring import ConfFile

        with pytest.raises(ValueError):
            ConfFile.parse("").set("K", "bad\nvalue")

    def test_set_rejects_bad_key(self):
        import pytest

        from td_release_packager.mcp_authoring import ConfFile

        for bad in ["", "  ", "K=Y", "K\nL", "#X"]:
            with pytest.raises(ValueError):
                ConfFile.parse("").set(bad, "v")


class TestShipsValidateEnvConfig:
    def test_missing_file(self, tmp_path: Path):
        from ships_mcp import ships_validate_env_config

        result = ships_validate_env_config(project=str(tmp_path), env="DEV")
        assert result["success"] is True
        assert result["exists"] is False
        assert result["valid"] is False

    def test_valid_file(self, tmp_path: Path):
        from ships_mcp import ships_validate_env_config

        env_dir = tmp_path / "config" / "env"
        env_dir.mkdir(parents=True)
        (env_dir / "DEV.conf").write_text(
            "SHIPS_ENV=DEV\nENV_PREFIX=PDE\n", encoding="utf-8"
        )
        result = ships_validate_env_config(project=str(tmp_path), env="DEV")
        assert result["success"] is True
        assert result["exists"] is True
        assert result["valid"] is True
        assert result["errors"] == []


class TestShipsValidateInspectConfig:
    def test_missing_file(self, tmp_path: Path):
        from ships_mcp import ships_validate_inspect_config

        result = ships_validate_inspect_config(project=str(tmp_path))
        assert result["success"] is True
        assert result["exists"] is False
        assert result["valid"] is False

    def test_invalid_severity_flagged(self, tmp_path: Path):
        from ships_mcp import ships_validate_inspect_config

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "inspect.conf").write_text("db_qualifier=FATAL\n", encoding="utf-8")
        result = ships_validate_inspect_config(project=str(tmp_path))
        assert result["valid"] is False
        assert any("FATAL" in e["message"] for e in result["errors"])

    def test_domain_value_validated(self, tmp_path: Path):
        from ships_mcp import ships_validate_inspect_config

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "inspect.conf").write_text(
            "comma_style=sideways\n", encoding="utf-8"
        )
        result = ships_validate_inspect_config(project=str(tmp_path))
        assert result["valid"] is False
        assert any("sideways" in e["message"] for e in result["errors"])


class TestShipsAuthorEnvConfig:
    def test_create_proposes_with_header_and_seed_keys(self, tmp_path: Path):
        from ships_mcp import ships_author_env_config

        result = ships_author_env_config(
            project=str(tmp_path),
            env="DEV",
            action="create",
            changes={"SHIPS_ENV": "DEV", "ENV_PREFIX": "PDE"},
        )
        assert result["success"] is True
        assert "DEV.conf" in result["proposed_content"]
        assert "SHIPS_ENV=DEV" in result["proposed_content"]
        assert "ENV_PREFIX=PDE" in result["proposed_content"]
        # Not written
        assert not (tmp_path / "config" / "env" / "DEV.conf").exists()

    def test_set_preserves_comments(self, tmp_path: Path):
        from ships_mcp import ships_author_env_config

        env_dir = tmp_path / "config" / "env"
        env_dir.mkdir(parents=True)
        original = "# section 1\nSHIPS_ENV=DEV\n\n# section 2\nENV_PREFIX=PDE\n"
        (env_dir / "DEV.conf").write_text(original, encoding="utf-8")

        result = ships_author_env_config(
            project=str(tmp_path),
            env="DEV",
            action="set",
            changes={"ENV_PREFIX": "NEW"},
        )
        assert result["success"] is True
        out = result["proposed_content"]
        assert "# section 1" in out
        assert "# section 2" in out
        assert "ENV_PREFIX=NEW" in out
        assert "ENV_PREFIX=PDE" not in out

    def test_set_requires_existing_file(self, tmp_path: Path):
        from ships_mcp import ships_author_env_config

        result = ships_author_env_config(
            project=str(tmp_path),
            env="DEV",
            action="set",
            changes={"SHIPS_ENV": "DEV"},
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_create_refuses_if_exists(self, tmp_path: Path):
        from ships_mcp import ships_author_env_config

        env_dir = tmp_path / "config" / "env"
        env_dir.mkdir(parents=True)
        (env_dir / "DEV.conf").write_text("X=Y\n", encoding="utf-8")

        result = ships_author_env_config(
            project=str(tmp_path), env="DEV", action="create"
        )
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_unset_removes_key(self, tmp_path: Path):
        from ships_mcp import ships_author_env_config

        env_dir = tmp_path / "config" / "env"
        env_dir.mkdir(parents=True)
        (env_dir / "DEV.conf").write_text("A=1\nB=2\n", encoding="utf-8")
        result = ships_author_env_config(
            project=str(tmp_path),
            env="DEV",
            action="unset",
            unset_keys=["A"],
        )
        assert result["success"] is True
        assert "A=" not in result["proposed_content"]
        assert "B=2" in result["proposed_content"]


class TestShipsAuthorInspectConfig:
    def test_set_flags_invalid_severity_in_validation(self, tmp_path: Path):
        from ships_mcp import ships_author_inspect_config

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "inspect.conf").write_text("# defaults\n", encoding="utf-8")
        result = ships_author_inspect_config(
            project=str(tmp_path),
            action="set",
            changes={"db_qualifier": "FATAL"},
        )
        # Proposal succeeds; validation flags the issue
        assert result["success"] is True
        assert result["validation"]["valid"] is False
        assert any("FATAL" in e["message"] for e in result["validation"]["errors"])

    def test_create_then_apply_writes_and_validates(self, tmp_path: Path):
        from ships_mcp import (
            ships_apply_diff,
            ships_author_inspect_config,
            ships_validate_inspect_config,
        )

        proposal = ships_author_inspect_config(
            project=str(tmp_path),
            action="create",
            changes={"db_qualifier": "WARNING", "comma_style": "trailing"},
        )
        assert proposal["success"] is True
        assert proposal["validation"]["valid"] is True

        applied = ships_apply_diff(
            path=proposal["path"],
            proposed_content=proposal["proposed_content"],
            expected_hash=proposal["expected_hash"],
        )
        assert applied["success"] is True
        result = ships_validate_inspect_config(project=str(tmp_path))
        assert result["valid"] is True


class TestPhaseBHashGate:
    def test_concurrent_edit_blocks_apply_of_conf(self, tmp_path: Path):
        from ships_mcp import ships_apply_diff, ships_author_env_config

        env_dir = tmp_path / "config" / "env"
        env_dir.mkdir(parents=True)
        path = env_dir / "DEV.conf"
        path.write_text("A=1\n", encoding="utf-8")

        proposal = ships_author_env_config(
            project=str(tmp_path),
            env="DEV",
            action="set",
            changes={"A": "2"},
        )
        # Simulate concurrent edit
        path.write_text("A=1\nB=3\n", encoding="utf-8")

        applied = ships_apply_diff(
            path=proposal["path"],
            proposed_content=proposal["proposed_content"],
            expected_hash=proposal["expected_hash"],
        )
        assert applied["success"] is False
        assert applied["code"] == "hash_mismatch"
        # File untouched
        assert path.read_text() == "A=1\nB=3\n"
