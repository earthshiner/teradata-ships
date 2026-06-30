"""
test_mcp_new_tools.py — MCP coverage for the latest SHIPS features.

Guards that the agent-facing MCP surface reflects the CLI:
    - ships_changeset      (#114/#115 detection)
    - ships_plan           (#379 detect-and-recommend)
    - ships_metadata_export(#244 Alation/Collibra/DataHub)
"""

import json
from pathlib import Path

import ships_mcp


def _mk_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".ships").mkdir(parents=True, exist_ok=True)
    (project / ".ships" / ".build_counter").write_text("0\n", encoding="utf-8")
    (project / "payload/database/DDL/tables/DB.Customer.tbl").write_text(
        "CREATE MULTISET TABLE DB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    (project / "payload/database/DDL/views/DB.ActiveCust.viw").write_text(
        "REPLACE VIEW DB.ActiveCust AS SELECT Id FROM DB.Customer;\n",
        encoding="utf-8",
    )
    return project


class TestShipsChangeset:
    def test_objects_mode_expands_dependants(self, tmp_path):
        project = _mk_project(tmp_path)
        result = ships_mcp.ships_changeset(str(project), objects="DB.Customer")
        assert result["success"] is True
        assert result["mode"] == "objects"
        assert "DB.Customer" in result["changed"]
        assert "DB.ActiveCust" in result["dependants"]
        assert set(result["selected"]) == {"DB.Customer", "DB.ActiveCust"}

    def test_no_baseline_reports_none(self, tmp_path):
        project = _mk_project(tmp_path)
        result = ships_mcp.ships_changeset(str(project))
        # No git ref and no baseline → mode none, success False, helpful note.
        assert result["mode"] == "none"
        assert result["success"] is False
        assert "baseline" in result["note"].lower()

    def test_missing_project(self, tmp_path):
        result = ships_mcp.ships_changeset(str(tmp_path / "nope"))
        assert result["success"] is False
        assert "not found" in result["error"]


class TestShipsPlan:
    def test_plan_from_source(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "c.sql").write_text(
            "CREATE MULTISET TABLE OMR_STD.Customer (Id INTEGER);\n", encoding="utf-8"
        )
        result = ships_mcp.ships_plan(
            str(src),
            project=str(tmp_path / "proj"),
            env="DEV,TST",
            name="create_objects",
        )
        assert result["success"] is True
        assert any(c.startswith("ships process") for c in result["commands"])
        assert result["plan"]["envs"] == ["DEV", "TST"]
        assert result["detected"]  # detection findings present

    def test_missing_source(self, tmp_path):
        result = ships_mcp.ships_plan(str(tmp_path / "nope"))
        assert result["success"] is False
        assert "not found" in result["error"]


def _mk_package(tmp_path: Path) -> Path:
    root = tmp_path / "pkg" / "01_main"
    ctx = root / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "ships.build.json").write_text(
        json.dumps(
            {
                "package_name": "CallCentre",
                "build_number": "0042",
                "environment": "DEV",
                "description": "Call centre product.",
            }
        ),
        encoding="utf-8",
    )
    (ctx / "ships.dependencies.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"type": "VIEW", "database": "CC_V", "object_name": "v_calls"},
                    {"type": "TABLE", "database": "CC_T", "object_name": "calls"},
                ],
                "edges": [
                    {
                        "source": "CC_T.calls",
                        "target": "CC_V.v_calls",
                        "type": "internal",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (ctx / "ships.trust.json").write_text(
        json.dumps({"status": "READY", "blocking_signals": [], "warning_signals": []}),
        encoding="utf-8",
    )
    (ctx / "ships.integrity.json").write_text(
        json.dumps({"package_hash": "abc123"}), encoding="utf-8"
    )
    return tmp_path / "pkg"


class TestDispatchArgs:
    """The async dispatch tools must forward the new CLI flags verbatim."""

    def _capture(self, monkeypatch):
        captured = {}

        def fake_launch(module, args, project_dir):
            captured["module"] = module
            captured["args"] = args
            return {"success": True, "run_id": "test"}

        monkeypatch.setattr(ships_mcp, "_launch_background", fake_launch)
        return captured

    def test_process_forwards_github_and_provenance(self, monkeypatch):
        import asyncio

        captured = self._capture(monkeypatch)
        asyncio.run(
            ships_mcp.ships_process(
                project="/p",
                source_github="acme/omr",
                source_ref="v1",
                output="/out",
                root_parent="DATAPRODUCTS",
                author="ci",
            )
        )
        args = captured["args"]
        assert "--source-github" in args and "acme/omr" in args
        assert "--source-ref" in args and "v1" in args
        assert "--output" in args and "/out" in args
        assert "--root-parent" in args and "DATAPRODUCTS" in args
        assert "--author" in args and "ci" in args

    def test_package_forwards_changeset_and_github(self, monkeypatch):
        import asyncio

        captured = self._capture(monkeypatch)
        asyncio.run(
            ships_mcp.ships_package(
                project="/p",
                env="DEV",
                name="pkg",
                env_config="/p/DEV.conf",
                since_tag="v1.0",
                source_github="acme/omr",
                change_ref="CHG1",
            )
        )
        args = captured["args"]
        assert "--since-tag" in args and "v1.0" in args
        assert "--source-github" in args and "acme/omr" in args
        assert "--change-ref" in args and "CHG1" in args


class TestShipsMetadataExport:
    def test_alation(self, tmp_path):
        pkg = _mk_package(tmp_path)
        out = tmp_path / "out"
        result = ships_mcp.ships_metadata_export(
            str(pkg), str(out), catalogue="alation"
        )
        assert result["success"] is True
        assert result["catalogue"] == "alation"
        assert "data_product.json" in result["files"]
        assert (out / "alation" / "manifest.json").is_file()

    def test_datahub(self, tmp_path):
        pkg = _mk_package(tmp_path)
        out = tmp_path / "out"
        result = ships_mcp.ships_metadata_export(
            str(pkg), str(out), catalogue="datahub"
        )
        assert result["success"] is True
        assert "datahub_mcps.json" in result["files"]

    def test_unknown_catalogue(self, tmp_path):
        pkg = _mk_package(tmp_path)
        result = ships_mcp.ships_metadata_export(
            str(pkg), str(tmp_path / "out"), catalogue="bogus"
        )
        assert result["success"] is False
        assert "unknown catalogue" in result["error"]

    def test_missing_package(self, tmp_path):
        result = ships_mcp.ships_metadata_export(
            str(tmp_path / "nope"), str(tmp_path / "out")
        )
        assert result["success"] is False


# ---------------------------------------------------------------
# ships_stage (#487) — bounded git-staging gate.
#
# The underlying ``stage_project`` is covered by test_stager.py; here we
# only exercise the MCP wrapper: tool registration, default-path
# envelope shape, and the injectable-callable surface used to keep
# tests fast and offline.
# ---------------------------------------------------------------


def _stage_project_fixture(root: Path) -> Path:
    project = root / "stage_proj"
    project.mkdir()
    (project / "ships.yaml").write_text("project: mcp_stage\n", encoding="utf-8")
    (project / "config").mkdir()
    (project / "payload" / "database" / "DDL").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "t.sql").write_text(
        "CREATE TABLE x (i INTEGER);\n", encoding="utf-8"
    )
    return project


class TestShipsStage:
    def test_tool_is_registered(self):
        """The MCP tool must be importable from ships_mcp."""
        assert callable(getattr(ships_mcp, "ships_stage", None))
        assert callable(getattr(ships_mcp, "_ships_stage_impl", None))

    def test_refuses_non_ships_project(self, tmp_path):
        result = ships_mcp.ships_stage(str(tmp_path / "nope"))
        assert result["success"] is False
        assert "ships.yaml" in (result["error"] or "")

    def test_dry_run_with_passing_gates_lists_paths(self, tmp_path):
        project = _stage_project_fixture(tmp_path)
        result = ships_mcp._ships_stage_impl(
            project=str(project),
            dry_run=True,
            run_scan=lambda _: 0,
            run_inspect=lambda _: 0,
            git=lambda _argv: 0,
        )
        assert result["success"] is True
        assert result["dry_run"] is True
        assert "ships.yaml" in result["staged_paths"]
        assert "config" in result["staged_paths"]
        assert "payload" in result["staged_paths"]

    def test_scan_failure_blocks(self, tmp_path):
        project = _stage_project_fixture(tmp_path)
        git_calls = []
        result = ships_mcp._ships_stage_impl(
            project=str(project),
            dry_run=False,
            run_scan=lambda _: 1,
            run_inspect=lambda _: 0,
            git=lambda argv: git_calls.append(argv) or 0,
        )
        assert result["success"] is False
        assert result["blocked_by"] == "scan"
        # git must NOT have been invoked on a blocked gate.
        assert git_calls == []

    def test_inspect_failure_blocks(self, tmp_path):
        project = _stage_project_fixture(tmp_path)
        git_calls = []
        result = ships_mcp._ships_stage_impl(
            project=str(project),
            dry_run=False,
            run_scan=lambda _: 0,
            run_inspect=lambda _: 1,
            git=lambda argv: git_calls.append(argv) or 0,
        )
        assert result["success"] is False
        assert result["blocked_by"] == "inspect"
        assert git_calls == []

    def test_clean_pass_invokes_git_with_owned_paths(self, tmp_path):
        project = _stage_project_fixture(tmp_path)
        git_calls = []
        result = ships_mcp._ships_stage_impl(
            project=str(project),
            dry_run=False,
            run_scan=lambda _: 0,
            run_inspect=lambda _: 0,
            git=lambda argv: git_calls.append(list(argv)) or 0,
        )
        assert result["success"] is True
        assert result["blocked_by"] is None
        assert len(git_calls) == 1
        argv = git_calls[0]
        assert argv[:2] == ["add", "--"]
        assert set(argv[2:]) == {"ships.yaml", "config", "payload"}

    def test_envelope_keys_are_stable(self, tmp_path):
        project = _stage_project_fixture(tmp_path)
        result = ships_mcp._ships_stage_impl(
            project=str(project),
            dry_run=True,
            run_scan=lambda _: 0,
            run_inspect=lambda _: 0,
        )
        for key in (
            "success",
            "dry_run",
            "project_dir",
            "staged_paths",
            "blocked_by",
            "error",
            "scan_exit_code",
            "inspect_exit_code",
        ):
            assert key in result
