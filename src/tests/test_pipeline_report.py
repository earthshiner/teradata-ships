"""
test_pipeline_report.py — Tests for the pre-package pipeline report (#324).

The report is a pure projection of ``ships.decisions.json``: these tests
build a synthetic decisions file and assert the generated HTML reflects it,
plus the fail-safe behaviour of the ``regenerate_reports`` entry point.
"""

from __future__ import annotations

import json

from td_release_packager.reporting import generate_pipeline_report, regenerate_reports
from td_release_packager.reporting.pipeline_report import (
    REPORT_DIRNAME,
    REPORT_FILENAME,
    load_latest_run,
    scan_project_payload,
)


def _write_decisions(project_dir, runs):
    """Write a minimal ships.decisions.json under the project's .ships/ dir."""
    state_dir = project_dir / ".ships"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "ships.decisions.json"
    path.write_text(
        json.dumps({"schema_version": 1, "project": {}, "runs": runs}, indent=2),
        encoding="utf-8",
    )
    return path


def _harvest_run():
    """A run with a single successful harvest stage carrying metrics."""
    return {
        "run_id": "2026-06-17T00:00:00Z-abcd",
        "command": "harvest",
        "final_status": "warning",
        "duration_ms": 1234,
        "stages": [
            {
                "stage": "harvest",
                "status": "warning",
                "started_at": "2026-06-17T00:00:00.000000+00:00",
                "duration_ms": 1200,
                "inputs": {},
                "outputs": {
                    "classified": 42,
                    "unclassified": 3,
                    "files_placed": 45,
                    "multiset_injected": 0,
                },
                "issues": [
                    {
                        "severity": "warning",
                        "code": "HARVEST_UNCLASSIFIED",
                        "message": "3 files could not be classified",
                        "location": "src/unknown/foo.sql",
                    }
                ],
            }
        ],
    }


def test_generate_writes_report_to_output_dir(tmp_path):
    """A report is written under output/reports/ and is non-trivial HTML."""
    _write_decisions(tmp_path, [_harvest_run()])

    result = generate_pipeline_report(str(tmp_path))

    report = tmp_path / REPORT_DIRNAME / REPORT_FILENAME
    assert result == str(report)
    assert report.is_file()
    html = report.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Pipeline Report" in html
    assert "Teradata" in html


def test_report_reflects_harvest_metrics_and_issues(tmp_path):
    """Recorded metrics and issues surface in the rendered report."""
    _write_decisions(tmp_path, [_harvest_run()])
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")

    # Timeline + Harvest tabs present
    assert "Run timeline" in html
    assert "Harvest" in html
    # Metrics rendered
    assert "42" in html
    assert "classified" in html
    # Zero-noise metric suppressed
    assert "MULTISET injected" not in html
    # Issue surfaced
    assert "HARVEST_UNCLASSIFIED" in html
    assert "3 files could not be classified" in html
    # Brand navy present
    assert "#00233C" in html


def test_latest_run_is_used(tmp_path):
    """Only the most recent run drives the report."""
    old = _harvest_run()
    old["command"] = "old-run"
    newest = _harvest_run()
    newest["command"] = "newest-run"
    _write_decisions(tmp_path, [old, newest])

    run = load_latest_run(str(tmp_path))
    assert run["command"] == "newest-run"


def test_stages_are_merged_across_separate_runs(tmp_path):
    """Each step run as its own CLI invocation still surfaces in its tab.

    Users typically invoke ``ships harvest``, ``ships inspect`` ... as
    separate commands, so each becomes a one-stage run in decisions.json.
    The report must merge the most recent stage of each kind, not just
    render the literal final run.
    """

    def _single_stage_run(rid, cmd, stage_name, started, outputs):
        return {
            "run_id": rid,
            "command": cmd,
            "final_status": "success",
            "duration_ms": 100,
            "stages": [
                {
                    "stage": stage_name,
                    "status": "success",
                    "started_at": started,
                    "duration_ms": 90,
                    "inputs": {},
                    "outputs": outputs,
                    "issues": [],
                }
            ],
        }

    runs = [
        _single_stage_run(
            "r1",
            "harvest",
            "harvest",
            "2026-06-17T09:00:00+00:00",
            {"classified": 42, "files_placed": 42},
        ),
        _single_stage_run(
            "r2",
            "inspect",
            "inspect",
            "2026-06-17T09:00:01+00:00",
            {"lint_warnings": 5},
        ),
        _single_stage_run(
            "r3",
            "scan",
            "scan",
            "2026-06-17T09:00:02+00:00",
            {"unique_tokens": 17},
        ),
        _single_stage_run(
            "r4",
            "analyse",
            "analyse",
            "2026-06-17T09:00:03+00:00",
            {"object_count": 88, "wave_count": 4},
        ),
        _single_stage_run(
            "r5",
            "package",
            "package",
            "2026-06-17T09:00:04+00:00",
            {},
        ),
    ]
    _write_decisions(tmp_path, runs)

    merged = load_latest_run(str(tmp_path))
    # Run-level identity comes from the literal newest run.
    assert merged["command"] == "package"
    assert merged["run_id"] == "r5"
    # But stages are merged from all runs.
    stage_names = [s["stage"] for s in merged["stages"]]
    assert stage_names == ["harvest", "inspect", "scan", "analyse", "package"]

    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    # Each step tab now renders its own metrics instead of the placeholder.
    assert "Harvest has not run" not in html
    assert "Inspect has not run" not in html
    assert "Scan has not run" not in html
    assert "Analyse has not run" not in html
    assert "42" in html  # harvest classified
    assert "17" in html  # scan unique_tokens
    assert "88" in html  # analyse object_count


def test_merged_stages_follow_canonical_pipeline_order(tmp_path):
    """Timeline reflects the SHIPS pipeline order, not when each step
    last ran. Re-harvesting after packaging is common — harvest must
    still appear at its canonical position, not get pushed to the end.
    """

    def _run(rid, cmd, started):
        return {
            "run_id": rid,
            "command": cmd,
            "final_status": "success",
            "duration_ms": 100,
            "stages": [
                {
                    "stage": cmd,
                    "status": "success",
                    "started_at": started,
                    "duration_ms": 90,
                    "inputs": {},
                    "outputs": {},
                    "issues": [],
                }
            ],
        }

    # Out-of-order timestamps: scaffold/inspect/scan/analyse/package
    # ran first, then harvest was re-run last.
    runs = [
        _run("r1", "scaffold", "2026-06-17T09:00:00+00:00"),
        _run("r2", "inspect", "2026-06-17T09:00:01+00:00"),
        _run("r3", "scan", "2026-06-17T09:00:02+00:00"),
        _run("r4", "analyse", "2026-06-17T09:00:03+00:00"),
        _run("r5", "package", "2026-06-17T09:00:04+00:00"),
        _run("r6", "harvest", "2026-06-17T09:05:00+00:00"),
    ]
    _write_decisions(tmp_path, runs)

    merged = load_latest_run(str(tmp_path))
    stage_names = [s["stage"] for s in merged["stages"]]
    # Canonical SHIPS pipeline order, regardless of timestamps.
    assert stage_names == [
        "scaffold",
        "harvest",
        "inspect",
        "scan",
        "analyse",
        "package",
    ]


def test_most_recent_stage_wins_when_step_rerun(tmp_path):
    """Re-running a step replaces the older stage record in the merge."""
    older = {
        "run_id": "old",
        "command": "harvest",
        "final_status": "success",
        "duration_ms": 100,
        "stages": [
            {
                "stage": "harvest",
                "status": "success",
                "started_at": "2026-06-17T08:00:00+00:00",
                "duration_ms": 90,
                "inputs": {},
                "outputs": {"classified": 1},
                "issues": [],
            }
        ],
    }
    newer = json.loads(json.dumps(older))
    newer["run_id"] = "new"
    newer["stages"][0]["started_at"] = "2026-06-17T09:00:00+00:00"
    newer["stages"][0]["outputs"]["classified"] = 999
    _write_decisions(tmp_path, [older, newer])

    merged = load_latest_run(str(tmp_path))
    assert len(merged["stages"]) == 1
    assert merged["stages"][0]["outputs"]["classified"] == 999


def test_no_decisions_file_returns_none(tmp_path):
    """With no decisions file, nothing is written and None is returned."""
    assert generate_pipeline_report(str(tmp_path)) is None
    assert not (tmp_path / REPORT_DIRNAME).exists()


def test_empty_runs_returns_none(tmp_path):
    """An empty runs list is a no-op."""
    _write_decisions(tmp_path, [])
    assert generate_pipeline_report(str(tmp_path)) is None


def test_regenerate_reports_is_fail_safe(tmp_path):
    """The entry point swallows errors and never raises into the caller."""
    # Corrupt decisions file — must not raise.
    (tmp_path / "ships.decisions.json").write_text("{not json", encoding="utf-8")
    assert regenerate_reports(str(tmp_path)) is None
    # Non-existent directory — must not raise.
    assert regenerate_reports(str(tmp_path / "does-not-exist")) is None


def _multi_step_run():
    """A run covering harvest/inspect/scan/analyse with assorted metrics."""
    return {
        "run_id": "r-multi",
        "command": "process",
        "final_status": "warning",
        "duration_ms": 5000,
        "stages": [
            {
                "stage": "harvest",
                "status": "success",
                "started_at": "2026-06-17T09:00:00+00:00",
                "duration_ms": 2000,
                "inputs": {},
                "outputs": {"classified": 100, "files_placed": 100},
                "issues": [],
            },
            {
                "stage": "inspect",
                "status": "warning",
                "started_at": "2026-06-17T09:00:02+00:00",
                "duration_ms": 1500,
                "inputs": {"files_scanned": 100},
                "outputs": {"lint_warnings": 5, "files_with_issues": 3},
                "issues": [
                    {
                        "severity": "warning",
                        "code": "LINT_SELECT_STAR",
                        "message": "SELECT * discouraged",
                    }
                ],
            },
            {
                "stage": "scan",
                "status": "success",
                "started_at": "2026-06-17T09:00:03+00:00",
                "duration_ms": 800,
                "inputs": {"files_with_tokens": 20},
                "outputs": {"unique_tokens": 9},
                "issues": [],
            },
            {
                "stage": "analyse",
                "status": "success",
                "started_at": "2026-06-17T09:00:04+00:00",
                "duration_ms": 700,
                "inputs": {},
                "outputs": {
                    "object_count": 88,
                    "wave_count": 4,
                    "dependency_count": 60,
                },
                "issues": [],
            },
        ],
    }


def test_all_step_tabs_render_their_metrics(tmp_path):
    """Inspect/Scan/Analyse tabs surface their own metrics and issues."""
    _write_decisions(tmp_path, [_multi_step_run()])
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")

    for label in ("Inspect", "Scan", "Analyse"):
        assert label in html
    assert "unique tokens" in html  # scan metric
    assert "88" in html  # analyse object_count
    assert "4" in html  # wave_count
    assert "LINT_SELECT_STAR" in html  # inspect issue


def _make_project_payload(project_dir, files):
    """Write DDL files under payload/database/<rel> for each (rel, content)."""
    for rel, content in files:
        p = project_dir / "payload" / "database" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_scan_project_payload_classifies_and_assigns_waves(tmp_path):
    """Project payload scan returns typed records with waves from _waves.txt."""
    _make_project_payload(
        tmp_path,
        [
            ("DDL/tables/DB.Customer.tbl", "CREATE TABLE DB.Customer (Id INT);"),
            ("DDL/views/DB.ActiveVw.viw", "REPLACE VIEW DB.ActiveVw AS SELECT 1;"),
            ("DCL/inter_db/APP_DB.role.grt", "GRANT SELECT ON DB TO role;"),
        ],
    )
    (tmp_path / ".ships").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ships" / "_waves.txt").write_text(
        "payload/database/DDL/tables/DB.Customer.tbl\n"
        "---\n"
        "payload/database/DDL/views/DB.ActiveVw.viw\n",
        encoding="utf-8",
    )
    records = scan_project_payload(str(tmp_path))
    by_name = {r["name"]: r for r in records}

    assert by_name["DB.Customer"]["type"] == "TABLE"
    assert by_name["DB.Customer"]["wave"] == 1
    assert by_name["DB.ActiveVw"]["type"] == "VIEW"
    assert by_name["DB.ActiveVw"]["wave"] == 2
    assert by_name["APP_DB.role"]["type"] == "GRANT"
    assert by_name["APP_DB.role"]["phase"] == "DCL"


def test_scan_project_payload_empty_when_no_tree(tmp_path):
    """No payload tree → empty list, no crash."""
    assert scan_project_payload(str(tmp_path)) == []


def test_guide_tab_is_first_and_carries_pipeline_phases(tmp_path):
    """The Guide tab opens by default and lists the six pipeline stages."""
    _write_decisions(tmp_path, [_harvest_run()])
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    # Guide tab is the first active tab — its button carries the active class.
    assert ">Guide</button>" in html
    assert 'class="tab-btn active" onclick="switchTab(this,\'tab-guide' in html
    # All six canonical pipeline steps are described as phase cards.
    for label in (
        "Scaffold —",
        "Harvest —",
        "Inspect —",
        "Scan —",
        "Analyse —",
        "Package —",
    ):
        assert label in html
    # Shared Guide CSS is included (hero + step + glossary scaffolding).
    assert ".guide-hero" in html
    assert ".guide-step" in html
    assert ".guide-glossary" in html


def test_payload_summary_does_not_double_pluralise_statistics(tmp_path):
    """Object-type names that are already plural (STATISTICS) must not
    get an extra trailing ``s`` in the Payload tab summary line.
    Regression test for the user-reported ``statisticss`` bug.
    """
    _write_decisions(tmp_path, [_harvest_run()])
    (tmp_path / "payload" / "database" / "DDL" / "statistics").mkdir(parents=True)
    (tmp_path / "payload" / "database" / "DDL" / "statistics" / "DB.T.stt").write_text(
        "COLLECT STATISTICS COLUMN ( id ) ON DB.T;\n", encoding="utf-8"
    )
    (tmp_path / "payload" / "database" / "DDL" / "tables").mkdir(parents=True)
    (tmp_path / "payload" / "database" / "DDL" / "tables" / "DB.T.tbl").write_text(
        "CREATE MULTISET TABLE DB.T (id INTEGER) PRIMARY INDEX (id);\n",
        encoding="utf-8",
    )

    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "statisticss" not in html
    assert "statistics" in html


def test_payload_tab_renders_wave_svg(tmp_path):
    """With waves present, the Payload tab renders the shared wave SVG."""
    _write_decisions(tmp_path, [_harvest_run()])
    _make_project_payload(
        tmp_path,
        [("DDL/tables/DB.T.tbl", "CREATE TABLE DB.T (Id INT);")],
    )
    (tmp_path / ".ships").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ships" / "_waves.txt").write_text(
        "payload/database/DDL/tables/DB.T.tbl\n", encoding="utf-8"
    )
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "<svg" in html
    assert "Wave 1" in html
    assert "DB.T" in html


def test_payload_tab_without_waves_lists_objects(tmp_path):
    """Harvested but not analysed → info banner + object list, no crash."""
    _write_decisions(tmp_path, [_harvest_run()])
    _make_project_payload(
        tmp_path,
        [("DDL/tables/DB.T.tbl", "CREATE TABLE DB.T (Id INT);")],
    )
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "waves not computed yet" in html.lower()
    assert "DB.T" in html


def test_harvest_tab_handles_missing_stage(tmp_path):
    """A run without a harvest stage still renders, with a friendly note."""
    run = {
        "run_id": "r1",
        "command": "inspect",
        "final_status": "success",
        "duration_ms": 10,
        "stages": [
            {
                "stage": "inspect",
                "status": "success",
                "started_at": "2026-06-17T00:00:00+00:00",
                "duration_ms": 8,
                "inputs": {"files_scanned": 12},
                "outputs": {"lint_errors": 0},
                "issues": [],
            }
        ],
    }
    _write_decisions(tmp_path, [run])
    generate_pipeline_report(str(tmp_path))
    html = (tmp_path / REPORT_DIRNAME / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "Harvest has not run" in html
    # Inspect metric from inputs still appears in the timeline
    assert "files scanned" in html
