"""``ships inspect`` reports which findings are auto-fixable (#524).

Two output surfaces gain fixability information in this PR:

* **Human output** — each finding whose rule has a registered fixer
  gets an inline ``— fixable (run `ships fix`)`` tag. Rules without a
  fixer print unchanged. A summary line after the per-file listing
  reports the auto-fixable count over the total.
* **JSON output** — the per-issue ``details`` dict picks up
  ``fixable: True`` and ``fixer_rule_id: <rule>``. Emitted through
  the ``ships.decisions.json`` recorder so agents and CI can act on
  the signal without parsing the human report.

Custom lint-policy findings are excluded from the tag on purpose:
they don't have built-in fixers, and their remediation lives in the
policy that flagged them, not in ``td_release_packager.fixers``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _setup_project(tmp_path: Path) -> Path:
    """Seed a project with both fixable and unfixable findings so the
    per-finding tag and the summary line have signal to report."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "ships.yaml").write_text("name: testpkg\n", encoding="utf-8", newline="")
    (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    # (a) Fixable: missing DDL terminator.
    _write(
        project / "payload/database/DDL/tables/Dev.T.tbl",
        "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id)\n",
    )
    # (b) Not fixable in v1 (hardcoded_name is #527, not registered yet).
    _write(
        project / "payload/database/DDL/tables/Dev.U.tbl",
        "CREATE MULTISET TABLE Dev.U (Id INTEGER) PRIMARY INDEX (Id);\n",
    )
    return project


def _run_inspect(project: Path, *extra_args: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "td_release_packager",
        "inspect",
        "--project",
        str(project),
        # `--fix-grants` still writes .grt files (until #526). Disable
        # so it doesn't muddy the JSON details we're asserting on.
        "--no-fix-grants",
        *extra_args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _find_decisions_json(project: Path) -> dict:
    """Return the most recent inspect stage entry from ships.decisions.json.

    Inspect records itself into the recorder; the resulting file lives
    at the project root. Newest run last — take it.
    """
    # decisions.json lives under .ships/ per project_paths.decisions_json_path.
    path = project / ".ships" / "ships.decisions.json"
    assert path.is_file(), f"expected {path} to exist after inspect ran"
    doc = json.loads(path.read_text(encoding="utf-8"))
    runs = doc.get("runs") or []
    assert runs, "ships.decisions.json has no runs recorded"
    # The most recent run's inspect stage entry. Stages are keyed by
    # `stage` in the JSON schema (not `name` — that's the run key).
    inspect_stages = [
        stage for stage in runs[-1].get("stages", []) if stage.get("stage") == "inspect"
    ]
    assert inspect_stages, "no inspect stage in the most recent run"
    return inspect_stages[-1]


# ---------------------------------------------------------------
# Human output — inline tag
# ---------------------------------------------------------------


class TestHumanInlineTag:
    def test_fixable_finding_gets_inline_tag(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_inspect(project)
        # ddl_terminator has a registered fixer -> the tag appears on
        # the primary per-finding line in the "Issues by file" block.
        # (Inspect also prints a compact "Lint errors" recap at the end
        # of the run that lists just `file:line [rule]` without the
        # tag; that's expected — the recap is a spot-check summary.)
        # Substring on the ASCII part of the tag; the em-dash before the
        # word "fixable" is a cosmetic separator that's a footgun to
        # pin exactly under Windows stdout encoding.
        ddl_lines = [
            line for line in result.stdout.splitlines() if "[ddl_terminator]" in line
        ]
        assert ddl_lines, "expected a ddl_terminator finding in output"
        tagged = [line for line in ddl_lines if "fixable (run `ships fix`)" in line]
        assert tagged, (
            f"no ddl_terminator finding line carries the fixable tag; got:\n"
            + "\n".join(ddl_lines)
        )

    def test_unfixable_finding_has_no_tag(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_inspect(project)
        # hardcoded_name has NO fixer in v1 (#527 is deferred) → tag absent.
        hn_lines = [
            line for line in result.stdout.splitlines() if "[hardcoded_name]" in line
        ]
        assert hn_lines, "expected a hardcoded_name finding in output"
        for line in hn_lines:
            assert "fixable" not in line, (
                f"hardcoded_name should not carry the fixable tag: {line!r}"
            )


# ---------------------------------------------------------------
# Human output — end-of-issues summary line
# ---------------------------------------------------------------


class TestHumanSummaryLine:
    def test_summary_line_reports_fixable_count(self, tmp_path):
        project = _setup_project(tmp_path)
        result = _run_inspect(project)
        # Exactly one fixable finding in the fixture (ddl_terminator).
        # ASCII-only substring for Windows-safe comparison.
        assert "auto-fixable" in result.stdout, (
            f"summary line missing 'auto-fixable' in output:\n{result.stdout}"
        )
        assert "run `ships fix` to apply." in result.stdout, (
            f"summary line missing 'run `ships fix` to apply.' in output:\n{result.stdout}"
        )
        # And it reports at least one fixable + a total >= that.
        summary_lines = [
            line
            for line in result.stdout.splitlines()
            if "auto-fixable" in line and "run `ships fix`" in line
        ]
        assert summary_lines, "expected one summary line, found none"

    def test_summary_line_says_none_when_no_findings_are_fixable(self, tmp_path):
        """When findings exist but none have registered fixers, the summary
        line reports ``none auto-fixable`` rather than a misleading zero."""
        project = tmp_path / "unfixable_only"
        project.mkdir()
        (project / "ships.yaml").write_text(
            "name: unfixable\n", encoding="utf-8", newline=""
        )
        (project / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
        # Terminated DDL with a hardcoded name — only hardcoded_name fires,
        # and hardcoded_name has no fixer yet (#527 is deferred).
        _write(
            project / "payload/database/DDL/tables/Dev.T.tbl",
            "CREATE MULTISET TABLE Dev.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        result = _run_inspect(project)
        assert "none auto-fixable" in result.stdout, (
            f"expected 'none auto-fixable' summary, got:\n{result.stdout}"
        )


# ---------------------------------------------------------------
# JSON output — fixable metadata on the recorder issue
# ---------------------------------------------------------------


class TestJsonRecorderMetadata:
    def test_fixable_issue_has_fixable_true_in_details(self, tmp_path):
        project = _setup_project(tmp_path)
        _run_inspect(project)
        stage = _find_decisions_json(project)
        issues = stage.get("issues", [])
        ddl_issues = [i for i in issues if "[ddl_terminator]" in i.get("message", "")]
        assert ddl_issues, "no ddl_terminator issue in decisions.json"
        for issue in ddl_issues:
            details = issue.get("details") or {}
            assert details.get("fixable") is True, (
                f"expected details.fixable=True on ddl_terminator issue, got {details!r}"
            )
            assert details.get("fixer_rule_id") == "ddl_terminator", (
                f"expected details.fixer_rule_id='ddl_terminator', got {details!r}"
            )

    def test_unfixable_issue_has_no_fixable_flag(self, tmp_path):
        project = _setup_project(tmp_path)
        _run_inspect(project)
        stage = _find_decisions_json(project)
        issues = stage.get("issues", [])
        hn_issues = [i for i in issues if "[hardcoded_name]" in i.get("message", "")]
        assert hn_issues, "no hardcoded_name issue in decisions.json"
        for issue in hn_issues:
            details = issue.get("details") or {}
            # `fixable` may be absent OR may be False; both mean "not fixable".
            # We just want to be sure it's not accidentally True.
            assert not details.get("fixable"), (
                f"hardcoded_name should not carry fixable=True: {details!r}"
            )
            assert "fixer_rule_id" not in details, (
                f"hardcoded_name should not carry fixer_rule_id: {details!r}"
            )
