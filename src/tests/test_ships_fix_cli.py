"""End-to-end tests for the ``ships fix`` CLI verb (#521).

Exercises the CLI as a black box — the subprocess call, argparse, the
dispatcher — through ``uv run ships fix ...``. Tests the shape of both
outputs (human and ``--json``), the mutually-exclusive-selectors rule,
and the ruff-style exit codes (0 = success, 1 = ``--dry-run`` would
apply, 2 = fixer or usage error).

Kept intentionally CLI-shaped rather than importing ``_cmd_fix``
directly: the argparse wiring, dispatch, and stream separation are
part of the contract and any regression in them should show up here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------
# Fixture project
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    """Seed the minimum SHIPS-shaped tree the fixers need.

    ``ships.yaml`` is required so ``resolve_harvest_extensions`` picks up
    the DDL suffix set. Payload subdirs are created as they'd be after
    ``ships harvest``.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    (project / "payload" / "database" / "DDL" / "views").mkdir(parents=True)
    return project


def _run_fix(project: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Invoke ``ships fix`` as a subprocess and return the result.

    Uses ``sys.executable -m td_release_packager`` so the test does not
    depend on ``ships`` being on PATH — matches how the other
    subprocess-shaped CLI tests in this suite invoke the CLI.
    """
    cmd = [
        sys.executable,
        "-m",
        "td_release_packager",
        "fix",
        "--project",
        str(project),
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------
# No-op run — nothing to fix
# ---------------------------------------------------------------


class TestNoOp:
    def test_empty_project_exits_zero(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_fix(project)
        assert result.returncode == 0, result.stderr
        # Human report should mention nothing to do.
        assert "Nothing to do" in result.stdout

    def test_already_clean_ddl_exits_zero(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        result = _run_fix(project)
        assert result.returncode == 0, result.stderr

    def test_dry_run_on_clean_project_exits_zero(self, tmp_path):
        """No pending work → exit 0 even under --dry-run."""
        project = _setup_project(tmp_path)
        result = _run_fix(project, "--dry-run")
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------
# Applied run — writes to disk
# ---------------------------------------------------------------


class TestApply:
    def test_missing_terminator_is_fixed_by_default(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project)
        assert result.returncode == 0, result.stderr
        assert f.read_text(encoding="utf-8").endswith(");\n")
        # Human report tone.
        assert "ddl_terminator" in result.stdout
        assert "rewritten" in result.stdout

    def test_non_ascii_is_not_default_on(self, tmp_path):
        """`non_ascii` is opt-in (default_on=False); bare `ships fix`
        must not substitute em-dashes even when there are matches."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "-- em—dash comment\n"
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        result = _run_fix(project)
        assert result.returncode == 0
        assert "—" in f.read_text(encoding="utf-8"), "non_ascii ran without opt-in"

    def test_all_flag_includes_opt_in_fixers(self, tmp_path):
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "-- em—dash comment\n"
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        result = _run_fix(project, "--all")
        assert result.returncode == 0, result.stderr
        assert "—" not in f.read_text(encoding="utf-8"), (
            "non_ascii did not run under --all"
        )


# ---------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path):
        project = _setup_project(tmp_path)
        original = "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n"
        f = _write(project / "payload/database/DDL/tables/Dev.T.tbl", original)
        result = _run_fix(project, "--dry-run")
        # File must be untouched.
        assert f.read_text(encoding="utf-8") == original

    def test_dry_run_with_pending_work_exits_one(self, tmp_path):
        """CI gate signal — non-zero exit when there is anything to fix."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project, "--dry-run")
        assert result.returncode == 1
        assert "would be rewritten" in result.stdout

    def test_dry_run_error_stream_stays_empty(self, tmp_path):
        """Exit code 1 is not an error — nothing should land on stderr."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project, "--dry-run")
        assert result.returncode == 1
        assert result.stderr == "", (
            f"expected clean stderr under dry-run, got: {result.stderr!r}"
        )


# ---------------------------------------------------------------
# --rules selector
# ---------------------------------------------------------------


class TestRulesSelector:
    def test_rules_restricts_to_named_set(self, tmp_path):
        """--rules non_ascii must not touch a DDL terminator finding."""
        project = _setup_project(tmp_path)
        f = _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "-- em—dash\nCREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project, "--rules", "non_ascii")
        assert result.returncode == 0, result.stderr
        text = f.read_text(encoding="utf-8")
        assert "—" not in text  # non_ascii DID run
        assert not text.rstrip().endswith(";"), (
            "ddl_terminator ran despite not being selected"
        )

    def test_rules_and_all_are_mutually_exclusive(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_fix(project, "--rules", "ddl_terminator", "--all")
        # argparse mutually_exclusive_group emits usage error → exit 2 by default.
        assert result.returncode == 2
        assert "not allowed with" in result.stderr

    def test_unknown_rule_id_exits_two(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_fix(project, "--rules", "does_not_exist")
        assert result.returncode == 2
        assert "unknown rule id" in result.stderr

    def test_unknown_rule_error_lists_available_rules(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_fix(project, "--rules", "does_not_exist")
        # The error message should tell the user what IS registered.
        assert "Registered fixers:" in result.stderr
        assert "ddl_terminator" in result.stderr
        assert "non_ascii" in result.stderr


# ---------------------------------------------------------------
# --json envelope
# ---------------------------------------------------------------


class TestJsonEnvelope:
    def test_json_envelope_is_valid_json(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        result = _run_fix(project, "--json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["success"] is True

    def test_json_envelope_shape(self, tmp_path):
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project, "--json")
        payload = json.loads(result.stdout)
        # Top-level keys.
        assert payload["success"] is True
        assert payload["dry_run"] is False
        assert Path(payload["project"]).resolve() == project.resolve()
        assert "rules_requested" in payload
        assert "rules_run" in payload
        assert "totals" in payload
        assert "rules" in payload
        # Aggregate totals.
        assert payload["totals"]["files_changed"] >= 1
        # Per-rule payload matches the FixResult.to_dict() shape.
        ddl_payload = next(
            r for r in payload["rules"] if r["rule_id"] == "ddl_terminator"
        )
        assert ddl_payload["files_changed_count"] >= 1
        assert ddl_payload["totals"]["statements_fixed"] >= 1
        assert isinstance(ddl_payload["files"], list)

    def test_json_dry_run_exits_one_with_valid_json(self, tmp_path):
        """Under --json --dry-run, the envelope must still be parseable
        even when we're going to exit 1."""
        project = _setup_project(tmp_path)
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
        )
        result = _run_fix(project, "--dry-run", "--json")
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["dry_run"] is True
        assert payload["totals"]["files_changed"] >= 1

    def test_json_unknown_rule_emits_error_envelope(self, tmp_path):
        """Even the failure path emits a JSON envelope when --json is set,
        so callers only ever parse one stream."""
        project = _setup_project(tmp_path)
        result = _run_fix(project, "--rules", "does_not_exist", "--json")
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert "does_not_exist" in payload["error"]


# ---------------------------------------------------------------
# Bad project handling
# ---------------------------------------------------------------


class TestProjectValidation:
    def test_missing_project_exits_two(self, tmp_path):
        # Point at a non-existent directory. --project is required so this
        # is the "exists but wrong path" case, not the argparse-usage case.
        target = tmp_path / "not-there"
        cmd = [
            sys.executable,
            "-m",
            "td_release_packager",
            "fix",
            "--project",
            str(target),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 2
        assert "does not exist" in result.stderr

    def test_project_flag_is_required(self):
        # Bare `ships fix` with no --project is an argparse usage error.
        cmd = [sys.executable, "-m", "td_release_packager", "fix"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 2
        assert "--project" in result.stderr
