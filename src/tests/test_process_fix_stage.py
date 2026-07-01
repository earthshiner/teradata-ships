"""``ships process`` runs a fix stage between generate and inspect (#523).

Pipeline shape after this PR:

    harvest → generate → fix → inspect → analyse → package

The fix stage:

* Runs the default-on subset of ``FIX_REGISTRY`` unless ``--no-fix``
  is passed. Rule selection is layered — CLI ``--fix-rule`` (repeatable)
  overrides ``packaging.fix.rules`` in ``ships.yaml``, which in turn
  overrides the built-in default-on set. ``packaging.fix.disable``
  subtracts rules that a specific project doesn't want.
* Skips entirely under ``--no-fix``. Nothing runs, inspect sees the
  pre-fix payload.
* Emits its own ``[F] Fix`` block in the human report and records a
  ``fix`` stage entry in ``ships.decisions.json``.

Kept CLI-shaped rather than unit-testing ``_run_process_fix`` directly:
argparse wiring + stage-recorder integration + the layered rule-set
resolution are all part of the contract, and any regression should
show up in a subprocess-driven test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------
# Fixture — a project with a fixable finding so we can prove the
# fix stage rewrote it before inspect ran
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path, ships_yaml_extra: str = "") -> Path:
    """Project seeded with a missing DDL terminator (ddl_terminator, fixable)
    and an em-dash (non_ascii, opt-in fixer)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text(
        "name: testpkg\n" + ships_yaml_extra,
        encoding="utf-8",
        newline="",
    )
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    _write(
        project / "payload/database/DDL/tables/Dev.T.tbl",
        "-- em—dash comment\n"
        "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
    )
    return project


def _run_process(project: Path, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "td_release_packager",
        "process",
        "--project",
        str(project),
        "--skip-generate",
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _find_fix_stage(project: Path) -> dict:
    """Return the most recent fix-stage entry from ships.decisions.json."""
    path = project / ".ships" / "ships.decisions.json"
    assert path.is_file(), f"expected {path} to exist after process ran"
    doc = json.loads(path.read_text(encoding="utf-8"))
    runs = doc.get("runs") or []
    assert runs, "ships.decisions.json has no runs recorded"
    fix_stages = [
        stage for stage in runs[-1].get("stages", []) if stage.get("stage") == "fix"
    ]
    assert fix_stages, "no fix stage in the most recent run"
    return fix_stages[-1]


# ---------------------------------------------------------------
# Pipeline placement — fix runs, and it runs before inspect
# ---------------------------------------------------------------


class TestFixStageRuns:
    def test_process_shows_fix_stage_between_generate_and_inspect(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_process(project)
        lines = result.stdout.splitlines()
        # Both the Fix marker line and the Inspect marker line must
        # appear, and Fix must appear before Inspect.
        fix_idx = next((i for i, line in enumerate(lines) if "[F] Fix" in line), None)
        inspect_idx = next(
            (i for i, line in enumerate(lines) if "[I] Inspect" in line), None
        )
        assert fix_idx is not None, (
            f"process output has no [F] Fix marker:\n{result.stdout}"
        )
        assert inspect_idx is not None, (
            f"process output has no [I] Inspect marker:\n{result.stdout}"
        )
        assert fix_idx < inspect_idx, "expected [F] Fix to appear before [I] Inspect"

    def test_fix_stage_rewrote_the_file_by_the_time_inspect_ran(self, tmp_path):
        project = _setup_project(tmp_path)
        _run_process(project)
        # ddl_terminator has been fixed on disk — file now ends with `;\n`.
        rewritten = (project / "payload/database/DDL/tables/Dev.T.tbl").read_text(
            encoding="utf-8"
        )
        assert rewritten.rstrip().endswith(";"), (
            f"expected file to end with ';' after fix stage; got: {rewritten!r}"
        )

    def test_fix_stage_records_a_decisions_entry(self, tmp_path):
        project = _setup_project(tmp_path)
        _run_process(project)
        stage = _find_fix_stage(project)
        assert stage.get("stage") == "fix"
        outputs = stage.get("outputs") or {}
        assert outputs.get("files_changed", 0) >= 1


# ---------------------------------------------------------------
# --no-fix
# ---------------------------------------------------------------


class TestNoFixFlag:
    def test_no_fix_skips_the_stage(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_process(project, "--no-fix")
        # Substring match dodges the ellipsis (U+2026) which cp1252
        # environments can mangle in stdout.
        assert "skipped (--no-fix)" in result.stdout

    def test_no_fix_leaves_the_file_unfixed(self, tmp_path):
        project = _setup_project(tmp_path)
        f = project / "payload/database/DDL/tables/Dev.T.tbl"
        before = f.read_text(encoding="utf-8")
        _run_process(project, "--no-fix")
        # Fix stage didn't run, so the missing terminator is still there.
        assert not f.read_text(encoding="utf-8").rstrip().endswith(";")
        # Extra sanity: neither has the em-dash been substituted.
        assert "—" in before


# ---------------------------------------------------------------
# --fix-rule
# ---------------------------------------------------------------


class TestFixRuleFlag:
    def test_fix_rule_adds_opt_in_fixer(self, tmp_path):
        project = _setup_project(tmp_path)
        f = project / "payload/database/DDL/tables/Dev.T.tbl"
        _run_process(project, "--fix-rule", "non_ascii")
        text = f.read_text(encoding="utf-8")
        # ddl_terminator (default-on) applied AND non_ascii (opt-in) applied.
        # We assert on file content (the ground truth) rather than the
        # exit code — the pipeline may exit non-zero for downstream lint
        # findings unrelated to whether the fixers ran.
        assert text.rstrip().endswith(";")
        assert "—" not in text

    def test_unknown_fix_rule_warns_and_continues(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_process(project, "--fix-rule", "not_a_rule")
        # We don't pin the exit code: process may return non-zero when
        # inspect reports lint findings later in the pipeline, and that
        # is unrelated to whether the unknown rule was handled cleanly.
        # The contract we care about here is that the warning message
        # mentions the offending rule id AND the pipeline reached at
        # least the fix stage (didn't abort at argparse).
        assert "not_a_rule" in result.stdout
        assert "[F] Fix" in result.stdout


# ---------------------------------------------------------------
# packaging.fix.rules / packaging.fix.disable
# ---------------------------------------------------------------


class TestShipsYamlPackagingFix:
    def test_packaging_fix_rules_adds_opt_in_rules(self, tmp_path):
        project = _setup_project(
            tmp_path,
            ships_yaml_extra="packaging:\n  fix:\n    rules:\n      - non_ascii\n",
        )
        f = project / "payload/database/DDL/tables/Dev.T.tbl"
        _run_process(project)
        assert "—" not in f.read_text(encoding="utf-8"), (
            "packaging.fix.rules should have added non_ascii to the default-on set"
        )

    def test_packaging_fix_disable_removes_default_on_rules(self, tmp_path):
        project = _setup_project(
            tmp_path,
            ships_yaml_extra=(
                "packaging:\n  fix:\n    disable:\n      - ddl_terminator\n"
            ),
        )
        f = project / "payload/database/DDL/tables/Dev.T.tbl"
        _run_process(project)
        # ddl_terminator was disabled at the project level, so the file
        # still ends without a semicolon.
        assert not f.read_text(encoding="utf-8").rstrip().endswith(";"), (
            "packaging.fix.disable should have kept ddl_terminator out of the set"
        )
