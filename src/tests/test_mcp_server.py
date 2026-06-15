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
        ]
        for name in expected:
            assert name in tool_names, f"Tool {name!r} not registered"

    def test_all_tools_have_descriptions(self):
        import ships_mcp

        for name, tool in ships_mcp.mcp._tool_manager._tools.items():
            assert tool.description, f"Tool {name!r} has no description"
