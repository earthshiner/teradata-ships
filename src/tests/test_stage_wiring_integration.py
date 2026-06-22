"""
test_stage_wiring_integration.py — Verify that the newly wired CLI stages
(scaffold, harvest, analyse, package) write well-formed ships.decisions.json via
the orchestrator foundation.

Build-order items 4c–4f: refactor the four remaining SHIPS stages onto the
cascade + decisions integration pattern.

Pattern mirrors test_inspect_orchestrator_integration.py: each test invokes
the CLI function directly with a Namespace, traps the SystemExit, then loads
ships.decisions.json and asserts on its structure.

Covers:
    Scaffold  — records config, outputs; ships.decisions.json written post-hoc
    Harvest   — records inputs/outputs/issues; HARVEST_* codes emitted
    Analyse   — records wave/dep/cycle counts; ANALYSE_* codes emitted
    Package   — records archive outputs; PACKAGE_WARNING codes emitted
    Issue codes — new codes are registered, exported, and described
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from td_release_packager.cli import (
    _cmd_analyze,
    _cmd_explain,
    _cmd_generate,
    _cmd_ingest,
    _cmd_process,
    _cmd_scaffold,
    _cmd_verify,
)
from td_release_packager.orchestrator import issue_codes


# ---------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------


def _read_decisions(project: Path) -> dict:
    return json.loads(
        (project / ".ships" / "ships.decisions.json").read_text(encoding="utf-8")
    )


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
            ANALYSE_CYCLE,
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
    """scaffold writes ships.decisions.json after creating the project."""

    def test_scaffold_writes_decisions_json(self, tmp_path):
        project = _make_project(tmp_path)
        assert (project / ".ships" / "ships.decisions.json").exists()

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
        assert not (tmp_path / ".ships" / "ships.decisions.json").exists()


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
    """harvest stage writes inputs/outputs/issues to ships.decisions.json."""

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

    def test_harvest_auto_applies_project_legacy_migration(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "Step_01_CreateDatabases_v0.1.db").write_text(
            "CREATE DATABASE $BASE_NODE FROM $PARENT_NODE\n"
            "AS PERM=100e6/2*(HASHAMP()+1)\n"
            ";\n",
            encoding="utf-8",
        )
        (project / "config" / "tokenise.conf").write_text(
            "s/$BASE_NODE/{{BASE_NODE}}/g\ns/$PARENT_NODE/{{PARENT_NODE}}/g\n",
            encoding="utf-8",
        )

        args = _make_harvest_args(source, project)
        _run(_cmd_ingest, args)

        harvested = (
            project
            / "payload"
            / "database"
            / "pre-requisites"
            / "databases"
            / "{{BASE_NODE}}.db"
        )
        assert harvested.exists()
        assert "{{PARENT_NODE}}" in harvested.read_text(encoding="utf-8")
        assert "$BASE_NODE" in (source / "Step_01_CreateDatabases_v0.1.db").read_text(
            encoding="utf-8"
        )

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]
        assert stage["outputs"]["legacy_migration_files"] == 1
        assert stage["outputs"]["legacy_migration_substitutions"] == 2
        assert stage["config_resolved"]["legacy_migration_rules"]["value"] == 2


# ---------------------------------------------------------------
# Analyse stage
# ---------------------------------------------------------------


def _make_analyse_args(source: Path, **overrides) -> Namespace:
    args = Namespace(
        project=str(source),
        output=None,
        overwrite=False,
        graph=None,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestAnalyseStageRecording:
    """analyse stage writes object/wave/cycle counts to ships.decisions.json."""

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
        """Running analyse against a bare directory does not create ships.decisions.json."""
        loose = tmp_path / "loose"
        loose.mkdir()

        args = _make_analyse_args(loose)
        _run(_cmd_analyze, args)

        assert not (loose / ".ships" / "ships.decisions.json").exists()


# ---------------------------------------------------------------
# Generate stage (item 7)
# ---------------------------------------------------------------


def _make_generate_args(source: Path, **overrides) -> Namespace:
    args = Namespace(
        project=str(source),
        modules=None,
        dry_run=False,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestGenerateStageRecording:
    """generate stage wires onto _stage_recording and records result."""

    def _seed_table(self, project: Path) -> None:
        tables = project / "payload" / "database" / "DDL" / "tables"
        tables.mkdir(parents=True, exist_ok=True)
        # Minimal table using the SHIPS token convention expected by the generator
        (tables / "{{DOM_DATABASE_T}}.t_loan.tbl").write_text(
            "CREATE MULTISET TABLE {{DOM_DATABASE_T}}.t_loan "
            "(loan_id INTEGER NOT NULL) "
            "PRIMARY INDEX (loan_id);",
            encoding="utf-8",
        )

    def test_generate_writes_stage_to_decisions(self, tmp_path):
        project = _make_project(tmp_path)
        self._seed_table(project)

        args = _make_generate_args(project)
        _run(_cmd_generate, args)

        d = _read_decisions(project)
        stages = [s["stage"] for r in d["runs"] for s in r["stages"]]
        assert "generate" in stages

    def test_generate_records_outputs(self, tmp_path):
        project = _make_project(tmp_path)
        self._seed_table(project)

        args = _make_generate_args(project)
        _run(_cmd_generate, args)

        d = _read_decisions(project)
        gen_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "generate")
        stage = gen_run["stages"][0]
        assert "locking_views_written" in stage["outputs"]
        assert "business_views_rewritten" in stage["outputs"]
        assert "config_files" in stage["outputs"]

    def test_generate_reports_filename_convention_and_config_files(
        self, tmp_path, capsys
    ):
        project = _make_project(tmp_path)
        self._seed_table(project)

        args = _make_generate_args(project, dry_run=True)
        _run(_cmd_generate, args)

        out = capsys.readouterr().out
        assert "Convention:       payload filename convention (*_T → *_V)" in out
        assert "object placement: found, not read here" in out
        assert "inspect rules: found, not read here" in out

        d = _read_decisions(project)
        gen_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "generate")
        config = gen_run["stages"][0]["config_resolved"]
        assert "object_placement_config" in config
        assert "inspect_config" in config
        assert "token_map" in config

    def test_generate_no_tables_emits_info_skip_not_error(self, tmp_path):
        """No paired-token tables → generator skips with an INFO issue,
        not an error. View-layer generation is opt-in; an inapplicable
        payload should not break the pipeline."""
        project = _make_project(tmp_path)
        # Don't seed any tables

        args = _make_generate_args(project)
        _run(_cmd_generate, args)

        d = _read_decisions(project)
        gen_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "generate")
        stage = gen_run["stages"][0]
        issues_by_severity = {i["severity"]: i["code"] for i in stage["issues"]}
        assert "error" not in issues_by_severity
        assert issues_by_severity.get("info") == issue_codes.GENERATE_WARNING


# ---------------------------------------------------------------
# --auto-tokenise (item 9)
# ---------------------------------------------------------------


class TestAutoTokenise:
    """--auto-tokenise detects literals and applies tokens in one pass."""

    def test_auto_tokenise_applies_tokens(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "HardDB.t.tbl").write_text(
            "CREATE MULTISET TABLE HardDB.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project, auto_tokenise=True)
        _run(_cmd_ingest, args)

        # Payload should contain a tokenised file ({{HardDB}}.t.tbl)
        tbl_files = list((project / "payload").rglob("*.tbl"))
        assert tbl_files, "no .tbl placed in payload"
        content = tbl_files[0].read_text(encoding="utf-8")
        # Token was auto-applied — the raw qualified reference HardDB.t should
        # be replaced; the literal may still appear inside the token name
        # ({{HardDB_T}}) which is correct kind-aware output.
        assert "HardDB.t" not in content, (
            f"raw qualified reference HardDB.t still present: {content}"
        )
        assert "{{" in content, "expected a token in harvested content"

    def test_auto_tokenise_records_auto_derived_tokens(self, tmp_path):
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "HardDB.t.tbl").write_text(
            "CREATE MULTISET TABLE HardDB.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project, auto_tokenise=True)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]
        # auto_derived_tokens recorded in decisions
        assert "auto_derived_tokens" in stage.get("decisions", {})
        assert stage["decisions"]["auto_derived_tokens"] >= 1

    def test_auto_tokenise_already_tokenised_source(self, tmp_path):
        """Already-tokenised source: --auto-tokenise is a no-op (zero candidates)."""
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "{{DB}}.t.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_harvest_args(source, project, auto_tokenise=True)
        _run(_cmd_ingest, args)

        d = _read_decisions(project)
        harvest_run = next(r for r in d["runs"] if r["stages"][0]["stage"] == "harvest")
        stage = harvest_run["stages"][0]
        assert stage["decisions"].get("auto_derived_tokens", 0) == 0


# ---------------------------------------------------------------
# Process meta-verb (item 5 + --strict from item 8)
# ---------------------------------------------------------------


def _make_process_args(project: Path, source: Path = None, **overrides) -> Namespace:
    args = Namespace(
        project=str(project),
        source=str(source) if source else None,
        token_map=None,
        auto_tokenise=False,
        env_prefix=None,
        skip_generate=True,  # skip generate by default in tests
        inspect_config=None,
        env=None,
        env_config=None,
        root_parent=None,
        name=None,
        output=None,
        format="zip",
        author="",
        description="",
        commit="",
        strict=False,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestProcessMetaVerb:
    """process command orchestrates stages and records into one run."""

    def _seed_project_with_source(self, tmp_path: Path):
        """Return (project, source) with a single classifiable source file."""
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "Dev.t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )
        return project, source

    def test_process_writes_single_run_with_multiple_stages(self, tmp_path):
        """One process run in ships.decisions.json contains multiple stage entries."""
        project, source = self._seed_project_with_source(tmp_path)

        args = _make_process_args(project, source)
        _run(_cmd_process, args)

        d = _read_decisions(project)
        process_run = next(
            (r for r in d["runs"] if r.get("command") == "process"), None
        )
        assert process_run is not None, (
            "expected a 'process' run in ships.decisions.json"
        )
        stage_names = [s["stage"] for s in process_run["stages"]]
        assert "harvest" in stage_names
        assert "inspect" in stage_names
        assert "analyse" in stage_names

    def test_process_without_source_skips_harvest(self, tmp_path):
        """--source omitted → harvest stage absent from process run."""
        project = _make_project(tmp_path)
        # Seed payload directly (skipping harvest)
        tables = project / "payload" / "database" / "DDL" / "tables"
        tables.mkdir(parents=True, exist_ok=True)
        (tables / "Dev.t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )

        args = _make_process_args(project, source=None)
        _run(_cmd_process, args)

        d = _read_decisions(project)
        process_run = next(r for r in d["runs"] if r.get("command") == "process")
        stage_names = [s["stage"] for s in process_run["stages"]]
        assert "harvest" not in stage_names
        assert "inspect" in stage_names

    def test_process_root_parent_applies_before_inspect(self, tmp_path):
        """--root-parent makes parentless prereqs explicit in process payload."""
        project = _make_project(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        (source / "00-setup.sql").write_text(
            "CREATE DATABASE Demo_DB AS PERMANENT = 1000000;",
            encoding="utf-8",
        )

        args = _make_process_args(project, source, root_parent="DEMO_ROOT")
        _run(_cmd_process, args)

        prereq_files = list(
            (project / "payload" / "database" / "pre-requisites").rglob("*.db")
        )
        assert len(prereq_files) == 1
        content = prereq_files[0].read_text(encoding="utf-8")
        assert "CREATE DATABASE Demo_DB FROM DEMO_ROOT AS" in content

    def test_process_strict_aborts_on_stage_error(self, tmp_path):
        """--strict mode: cyclic views in inspect/analyse cause sys.exit(1)."""
        project = _make_project(tmp_path)
        # Plant a cyclic view pair to force an error in analyse
        views = project / "payload" / "database" / "DDL" / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "Dev.v_a.viw").write_text(
            "REPLACE VIEW Dev.v_a AS SELECT * FROM Dev.v_b;", encoding="utf-8"
        )
        (views / "Dev.v_b.viw").write_text(
            "REPLACE VIEW Dev.v_b AS SELECT * FROM Dev.v_a;", encoding="utf-8"
        )

        args = _make_process_args(project, source=None, strict=True)
        rc = _run(_cmd_process, args)

        # With --strict, the cycle in analyse causes a non-zero exit
        assert rc != 0, "expected non-zero exit under --strict with cyclic views"

    def test_process_developer_mode_continues_past_warnings(self, tmp_path):
        """Developer mode (no --strict): warnings don't abort the pipeline."""
        project, source = self._seed_project_with_source(tmp_path)

        # No --strict — pipeline should complete even if warnings exist
        args = _make_process_args(project, source)
        _run(_cmd_process, args)

        # Should complete all stages, exit 0 (or non-zero only on hard errors)
        d = _read_decisions(project)
        process_run = next(r for r in d["runs"] if r.get("command") == "process")
        stage_names = [s["stage"] for s in process_run["stages"]]
        # At minimum inspect and analyse ran
        assert "inspect" in stage_names
        assert "analyse" in stage_names

    def test_process_non_project_dir_exits_immediately(self, tmp_path):
        """process --project on a non-existent dir exits immediately."""
        args = _make_process_args(tmp_path / "nonexistent")
        rc = _run(_cmd_process, args)
        assert rc == 1


# ---------------------------------------------------------------
# explain command (item 6a)
# ---------------------------------------------------------------


def _make_explain_args(project: Path, **overrides) -> Namespace:
    args = Namespace(project=str(project), run_id=None, command_filter=None)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestExplainCommand:
    """explain renders a human-readable report from ships.decisions.json."""

    def _run_process_and_get_project(self, tmp_path: Path) -> Path:
        project, source = TestProcessMetaVerb()._seed_project_with_source(tmp_path)
        args = _make_process_args(project, source)
        _run(_cmd_process, args)
        return project

    def test_explain_exits_0_on_success_run(self, tmp_path):
        project = self._run_process_and_get_project(tmp_path)
        args = _make_explain_args(project)
        rc = _run(_cmd_explain, args)
        assert rc == 0

    def test_explain_no_decisions_json_exits_1(self, tmp_path):
        project = _make_project(tmp_path)
        # Don't run any pipeline — no ships.decisions.json content beyond scaffold
        # (scaffold writes ships.decisions.json but has no process run)
        # Delete it to simulate truly empty state
        (project / ".ships" / "ships.decisions.json").unlink(missing_ok=True)
        args = _make_explain_args(project)
        rc = _run(_cmd_explain, args)
        assert rc == 1

    def test_explain_invalid_project_dir_exits_1(self, tmp_path):
        args = _make_explain_args(tmp_path / "nonexistent")
        rc = _run(_cmd_explain, args)
        assert rc == 1

    def test_explain_command_filter_selects_correct_run(self, tmp_path):
        """--command filter picks the last run with that command name."""
        project = self._run_process_and_get_project(tmp_path)
        args = _make_explain_args(project, command_filter="process")
        rc = _run(_cmd_explain, args)
        assert rc == 0

    def test_explain_unknown_run_id_exits_1(self, tmp_path):
        project = self._run_process_and_get_project(tmp_path)
        args = _make_explain_args(project, run_id="does-not-exist-xyz")
        rc = _run(_cmd_explain, args)
        assert rc == 1

    def test_explain_shows_stage_count(self, tmp_path, capsys):
        """Output contains at least the inspect and analyse stage names."""
        project = self._run_process_and_get_project(tmp_path)
        args = _make_explain_args(project)
        _run(_cmd_explain, args)
        out = capsys.readouterr().out
        assert "inspect" in out
        assert "analyse" in out


# ---------------------------------------------------------------
# verify command (item 6b)
# ---------------------------------------------------------------


def _make_verify_args(project: Path, **overrides) -> Namespace:
    args = Namespace(project=str(project), run_id=None)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestVerifyCommand:
    """verify checks package readiness from ships.decisions.json."""

    def test_verify_no_package_stage_exits_1(self, tmp_path):
        """No package stage in ships.decisions.json → NOT READY."""
        project = _make_project(tmp_path)
        # Run process without packaging (no --env etc.)
        source = tmp_path / "src"
        source.mkdir()
        (source / "Dev.t.tbl").write_text(
            "CREATE MULTISET TABLE Dev.t (id INTEGER);", encoding="utf-8"
        )
        args = _make_process_args(project, source)
        _run(_cmd_process, args)

        vargs = _make_verify_args(project)
        rc = _run(_cmd_verify, vargs)
        assert rc == 1

    def test_verify_no_decisions_json_exits_1(self, tmp_path):
        project = _make_project(tmp_path)
        (project / ".ships" / "ships.decisions.json").unlink(missing_ok=True)
        args = _make_verify_args(project)
        rc = _run(_cmd_verify, args)
        assert rc == 1

    def test_verify_invalid_project_dir_exits_1(self, tmp_path):
        args = _make_verify_args(tmp_path / "nonexistent")
        rc = _run(_cmd_verify, args)
        assert rc == 1


# ---------------------------------------------------------------
# _maybe_pause (item 10) — non-interactive path
# ---------------------------------------------------------------


class TestMaybePause:
    """_maybe_pause silently returns when not in pause mode or not interactive."""

    def test_no_pause_flag_returns_immediately(self, tmp_path):
        """Without --pause, _maybe_pause is a no-op."""
        from td_release_packager.cli import _maybe_pause

        args = Namespace(pause=False)
        # Should not raise or prompt
        _maybe_pause("inspect", "success", args)

    def test_ci_env_suppresses_pause(self, tmp_path, monkeypatch):
        """CI=true suppresses pause even when --pause is set."""
        from td_release_packager.cli import _maybe_pause

        monkeypatch.setenv("CI", "true")
        args = Namespace(pause=True)
        # Should not raise or prompt (CI mode skips the prompt)
        _maybe_pause("inspect", "success", args)

    def test_non_tty_suppresses_pause(self, tmp_path, monkeypatch):
        """Non-TTY stdout suppresses pause."""
        from td_release_packager.cli import _maybe_pause
        import io

        args = Namespace(pause=True)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("SHIPS_CI", raising=False)
        monkeypatch.delenv("NO_PROMPT", raising=False)
        # Replace stdout with a non-TTY stream
        monkeypatch.setattr("sys.stdout", io.StringIO())
        # Should return silently — stdout.isatty() is False for StringIO
        _maybe_pause("inspect", "success", args)
