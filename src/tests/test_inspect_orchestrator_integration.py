"""
test_inspect_orchestrator_integration.py — Verify the ``inspect`` CLI
stage writes a well-formed ships.decisions.json via the orchestrator
foundation.

Item 4b in the orchestrator build order: refactor inspect onto the
cascade + decisions integration pattern that the scan stage piloted.

Each test invokes ``_cmd_validate`` directly with a constructed
argparse.Namespace, traps the SystemExit it raises (the CLI convention
for unix-style exit codes), then loads the resulting ships.decisions.json
and asserts on its structure.

Covers:
    1. Clean run → final_status=success, single inspect stage
    2. Config provenance for source / config / strict / skip_grants
    3. Inputs / outputs sections record the inspect counters
    4. Lint violations recorded with code=INSPECT_LINT_VIOLATION,
       severity matching the rule's emitted severity
    5. Malformed tokens recorded with code=INSPECT_TOKEN_MALFORMED
    6. Status auto-rollup: lint errors → status=error;
       lint warnings only → status=warning
    7. Append-only across multiple inspect runs
    8. Non-project directory → no ships.decisions.json written
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from td_release_packager.cli import _cmd_validate


def _make_namespace(source: Path, **overrides) -> Namespace:
    """Build a Namespace matching the inspect subparser's argspec.

    All inspect CLI flags get a default so individual tests only have
    to override the ones they care about. The defaults match a
    minimal ``td_release_packager inspect --project X --skip-grants``
    invocation (skip_grants=True keeps Step 2 out of the way for
    tests that don't set up .grt files).
    """
    args = Namespace(
        project=str(source),
        config=None,
        strict=False,
        skip_tokens=False,
        skip_keywords=False,
        skip_commas=False,
        fix_grants=False,
        skip_grants=True,
        dcl_dir=None,
        verbose=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _run_inspect(args) -> int:
    """Invoke ``_cmd_validate`` and return its exit code."""
    with pytest.raises(SystemExit) as ei:
        _cmd_validate(args)
    return int(ei.value.code) if ei.value.code is not None else 0


def _read_decisions(project: Path) -> dict:
    return json.loads((project / "ships.decisions.json").read_text(encoding="utf-8"))


def _make_project(tmp_path: Path) -> Path:
    """Minimal SHIPS-shaped project directory with payload/ marker."""
    project = tmp_path / "project"
    payload = project / "payload" / "database" / "DDL" / "tables"
    payload.mkdir(parents=True)
    return project


# ---------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------


class TestInspectCleanRun:
    """A project whose DDL passes every rule produces a clean
    ships.decisions.json — single stage, no issues, status=success."""

    def test_clean_project_writes_success_run(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        # A well-formed multi-set table that satisfies every rule.
        (
            project / "payload" / "database" / "DDL" / "tables" / "MyDB.Customer.tbl"
        ).write_text(
            "CREATE MULTISET TABLE {{STD_DB}}.Customer\n"
            "(\n"
            "     Cust_Id INTEGER NOT NULL\n"
            "    ,Cust_Name VARCHAR(100)\n"
            ")\n"
            "PRIMARY INDEX (Cust_Id);\n",
            encoding="utf-8",
        )

        exit_code = _run_inspect(_make_namespace(project))
        capsys.readouterr()

        assert exit_code == 0
        data = _read_decisions(project)
        assert data["schema_version"] == 1
        assert len(data["runs"]) == 1
        run = data["runs"][0]
        assert run["command"] == "inspect"
        assert run["final_status"] == "success"
        assert len(run["stages"]) == 1
        stage = run["stages"][0]
        assert stage["stage"] == "inspect"
        assert stage["status"] == "success"
        # PR2 (#351 follow-up): inspect now runs a token-coverage check
        # against every ``config/env/*.conf``. This fixture has no env
        # configs, so inspect emits a single INFO note documenting that
        # coverage could not be verified — the issue is informational
        # and does not bump the warnings/errors counters, so the run
        # stays ``status=success``. Allow that single INFO entry; any
        # other issue is still a regression.
        non_info_issues = [
            i for i in stage["issues"] if i.get("severity") != "info"
        ]
        assert non_info_issues == []
        coverage_notes = [
            i
            for i in stage["issues"]
            if i.get("severity") == "info"
            and i.get("code") == "TOKEN_UNDEFINED"
        ]
        assert len(coverage_notes) == 1

    def test_records_config_resolved_with_provenance(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        (project / "payload" / "database" / "DDL" / "tables" / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(project, strict=True))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        # Source recorded with cli/layer-5 provenance.
        assert stage["config_resolved"]["source"]["value"] == str(project)
        assert stage["config_resolved"]["source"]["source"] == "layer-5"
        assert stage["config_resolved"]["source"]["source_path"] == "cli"
        # strict was set to True.
        assert stage["config_resolved"]["strict"]["value"] is True
        # skip_grants=True from default Namespace builder.
        assert stage["config_resolved"]["skip_grants"]["value"] is True

    def test_records_inputs_and_outputs(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        (project / "payload" / "database" / "DDL" / "tables" / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(project))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        assert "source_dir" in stage["inputs"]
        assert "payload_dir" in stage["inputs"]
        assert stage["inputs"]["files_scanned"] >= 1
        # outputs include the headline pass/fail gates.
        assert stage["outputs"]["overall_passed"] is True
        assert stage["outputs"]["lint_passed"] is True
        assert stage["outputs"]["token_format_passed"] is True


# ---------------------------------------------------------------
# Validation issues — lint
# ---------------------------------------------------------------


class TestInspectRecordsLintIssues:
    """Lint findings from validate.py become ships.decisions.json issues
    with code=INSPECT_LINT_VIOLATION; the rule name is preserved in
    the message so explain can group by rule."""

    def test_lint_warning_recorded_as_warning_issue(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        # CREATE TABLE without SET/MULTISET fires set_multiset (WARNING).
        tbl_dir = project / "payload" / "database" / "DDL" / "tables"
        (tbl_dir / "{{DB}}.T.tbl").write_text(
            "CREATE TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        exit_code = _run_inspect(_make_namespace(project))
        capsys.readouterr()

        # A WARNING-only run does not fail inspect (exit_code 0).
        assert exit_code == 0
        stage = _read_decisions(project)["runs"][0]["stages"][0]
        warning_issues = [i for i in stage["issues"] if i["severity"] == "warning"]
        assert any(
            i["code"] == "INSPECT_LINT_VIOLATION" and "[set_multiset]" in i["message"]
            for i in warning_issues
        )

    def test_lint_error_recorded_as_error_issue(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        # Simulate a lint error via a db_qualifier violation (always ERROR).
        viw_dir = project / "payload" / "database" / "DDL" / "views"
        viw_dir.mkdir(parents=True)
        (viw_dir / "V.viw").write_text(
            "CREATE VIEW V AS SELECT 1;\n",
            encoding="utf-8",
        )

        exit_code = _run_inspect(_make_namespace(project))
        capsys.readouterr()

        assert exit_code == 1  # db_qualifier ERROR fails the run
        stage = _read_decisions(project)["runs"][0]["stages"][0]
        error_issues = [i for i in stage["issues"] if i["severity"] == "error"]
        assert any(
            i["code"] == "INSPECT_LINT_VIOLATION" and "[db_qualifier]" in i["message"]
            for i in error_issues
        )
        assert stage["status"] == "error"
        assert _read_decisions(project)["runs"][0]["final_status"] == "failed"

    def test_set_multiset_warning_recorded_as_warning_issue(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        # CREATE TABLE without SET/MULTISET fires set_multiset (WARNING).
        tbl_dir = project / "payload" / "database" / "DDL" / "tables"
        (tbl_dir / "{{DB}}.T.tbl").write_text(
            "CREATE TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        exit_code = _run_inspect(_make_namespace(project))
        capsys.readouterr()

        assert exit_code == 0  # warnings don't fail the run
        stage = _read_decisions(project)["runs"][0]["stages"][0]
        warning_issues = [i for i in stage["issues"] if i["severity"] == "warning"]
        assert any(
            i["code"] == "INSPECT_LINT_VIOLATION" and "[set_multiset]" in i["message"]
            for i in warning_issues
        )
        # Warning issues require explicit set_status — verify it stuck.
        assert stage["status"] == "warning"

    def test_lint_issue_location_carries_file_and_line(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        tbl_dir = project / "payload" / "database" / "DDL" / "tables"
        (tbl_dir / "{{DB}}.T.tbl").write_text(
            "CREATE TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(project))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        lint_issues = [
            i for i in stage["issues"] if "[set_multiset]" in i.get("message", "")
        ]
        assert lint_issues
        # Location should at least name the file (line is optional).
        assert ".tbl" in lint_issues[0]["location"]


# ---------------------------------------------------------------
# Validation issues — malformed tokens
# ---------------------------------------------------------------


class TestInspectRecordsMalformedTokens:
    def test_malformed_token_recorded_as_error_issue(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        tbl_dir = project / "payload" / "database" / "DDL" / "tables"
        # Stray whitespace inside braces — malformed token.
        (tbl_dir / "{{DB}}.T.tbl").write_text(
            "CREATE MULTISET TABLE {{ DB }}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(project))
        capsys.readouterr()

        stage = _read_decisions(project)["runs"][0]["stages"][0]
        codes = [i["code"] for i in stage["issues"]]
        assert "INSPECT_TOKEN_MALFORMED" in codes
        # Each malformed marker carries a precise location.
        token_issues = [
            i for i in stage["issues"] if i["code"] == "INSPECT_TOKEN_MALFORMED"
        ]
        assert all(":" in i["location"] for i in token_issues)


# ---------------------------------------------------------------
# Append-only behaviour
# ---------------------------------------------------------------


class TestInspectAppendsAcrossRuns:
    def test_second_run_appends_to_manifest(self, tmp_path, capsys):
        project = _make_project(tmp_path)
        (project / "payload" / "database" / "DDL" / "tables" / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(project))
        _run_inspect(_make_namespace(project))
        capsys.readouterr()

        data = _read_decisions(project)
        assert len(data["runs"]) == 2
        # All runs are inspect runs.
        assert all(r["command"] == "inspect" for r in data["runs"])
        # Distinct run_ids.
        run_ids = [r["run_id"] for r in data["runs"]]
        assert len(set(run_ids)) == 2


# ---------------------------------------------------------------
# Project-detection gate (Flag 1) — no manifest litter
# ---------------------------------------------------------------


class TestInspectSkipsManifestForNonProject:
    """Ad-hoc inspect invocations against arbitrary directories must
    not litter the filesystem with ships.decisions.json."""

    def test_no_manifest_for_directory_without_payload(self, tmp_path, capsys):
        loose_dir = tmp_path / "loose"
        loose_dir.mkdir()
        # A loose .tbl file at the root — no payload/ marker.
        (loose_dir / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE {{DB}}.T (Id INT) PRIMARY INDEX (Id);",
            encoding="utf-8",
        )

        _run_inspect(_make_namespace(loose_dir))
        capsys.readouterr()

        # Stdout still works; ships.decisions.json must NOT have appeared.
        assert not (loose_dir / "ships.decisions.json").exists()
