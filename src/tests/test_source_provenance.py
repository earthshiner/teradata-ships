"""
test_source_provenance.py — Harvest source map + pipeline-report rendering (#466).

Two layers:

  1. ``ingest._emit_source_map`` writes ``.ships/harvest/source_map.json``
     mapping each payload-relative path to the source file it came from.
  2. ``reporting.common.render_issue_list`` reads that map and decorates
     each inspect finding with a "↳ source: <path>" subline so the
     reader knows which user-authored file to edit.
"""

from __future__ import annotations

import json

from td_release_packager.ingest import _emit_source_map
from td_release_packager.reporting.common import (
    lookup_source_provenance,
    render_issue_category_summary,
    render_issue_list,
)


# ---------------------------------------------------------------------------
# _emit_source_map
# ---------------------------------------------------------------------------


class TestEmitSourceMap:
    def test_writes_keyed_by_dest_relpath(self, tmp_path):
        source = tmp_path / "src"
        project = tmp_path / "proj"
        source.mkdir()
        project.mkdir()

        files_placed = [
            (
                "10_domain/views_locking/foo.ddl",
                "payload/database/DDL/views/{{T}}.foo.viw",
                "VIEW",
            ),
            (
                "10_domain/tables/foo.ddl",
                "payload/database/DDL/tables/{{T}}.foo.tbl",
                "TABLE",
            ),
        ]
        out = _emit_source_map(str(project), str(source), files_placed)

        assert out is not None
        doc = json.loads(
            (project / ".ships" / "harvest" / "source_map.json").read_text()
        )

        # Sanity: top-level metadata
        assert doc["schema_version"] == "1.0"
        assert doc["source_root"].endswith("/src")
        assert doc["project_root"].endswith("/proj")

        # Each entry is keyed by the dest-relpath the inspect tab can match.
        viw_key = "payload/database/DDL/views/{{T}}.foo.viw"
        tbl_key = "payload/database/DDL/tables/{{T}}.foo.tbl"
        assert viw_key in doc["entries"]
        assert doc["entries"][viw_key]["source_relpath"] == (
            "10_domain/views_locking/foo.ddl"
        )
        assert doc["entries"][viw_key]["source_abspath"].endswith(
            "/src/10_domain/views_locking/foo.ddl"
        )
        assert doc["entries"][viw_key]["type"] == "VIEW"
        assert doc["entries"][tbl_key]["type"] == "TABLE"

    def test_empty_placement_returns_none(self, tmp_path):
        # No files placed → no map written, no path returned.
        out = _emit_source_map(str(tmp_path), str(tmp_path), [])
        assert out is None
        assert not (tmp_path / ".ships" / "harvest" / "source_map.json").exists()


# ---------------------------------------------------------------------------
# lookup_source_provenance — path-form normalisation
# ---------------------------------------------------------------------------


class TestLookupSourceProvenance:
    @staticmethod
    def _map() -> dict:
        return {
            "entries": {
                "payload/database/DDL/views/{{DB}}.foo.viw": {
                    "source_relpath": "10_domain/foo.ddl",
                    "source_abspath": "/abs/10_domain/foo.ddl",
                    "type": "VIEW",
                },
            }
        }

    def test_strips_trailing_line_suffix(self):
        # Inspect emits locations like "DDL\views\foo.viw:37".
        entry = lookup_source_provenance("DDL\\views\\{{DB}}.foo.viw:37", self._map())
        assert entry is not None
        assert entry["source_relpath"] == "10_domain/foo.ddl"

    def test_normalises_windows_separators(self):
        entry = lookup_source_provenance(
            "payload\\database\\DDL\\views\\{{DB}}.foo.viw", self._map()
        )
        assert entry is not None

    def test_expands_payload_database_prefix(self):
        entry = lookup_source_provenance("DDL/views/{{DB}}.foo.viw", self._map())
        assert entry is not None
        assert entry["type"] == "VIEW"

    def test_unknown_location_returns_none(self):
        assert lookup_source_provenance("nowhere.viw", self._map()) is None

    def test_empty_source_map_returns_none(self):
        assert lookup_source_provenance("anywhere.viw", None) is None
        assert lookup_source_provenance("anywhere.viw", {}) is None


# ---------------------------------------------------------------------------
# render_issue_list — source subline integration
# ---------------------------------------------------------------------------


class TestRenderIssueListWithSourceMap:
    def test_source_subline_rendered_when_match(self):
        source_map = {
            "entries": {
                "payload/database/DDL/views/{{DB}}.foo.viw": {
                    "source_relpath": "10_domain/foo.ddl",
                    "source_abspath": "C:/src/10_domain/foo.ddl",
                    "type": "VIEW",
                }
            }
        }
        issues = [
            {
                "severity": "error",
                "code": "INSPECT_LINT_VIOLATION",
                "message": "rule fired",
                "location": "DDL\\views\\{{DB}}.foo.viw:42",
            }
        ]
        html = render_issue_list(issues, source_map=source_map)
        assert "↳ source:" in html
        assert "10_domain/foo.ddl" in html
        # Absolute path renders as the source span's hover title.
        assert "C:/src/10_domain/foo.ddl" in html

    def test_no_source_map_renders_without_subline(self):
        issues = [
            {
                "severity": "error",
                "code": "INSPECT_LINT_VIOLATION",
                "message": "rule fired",
                "location": "DDL\\views\\foo.viw:42",
            }
        ]
        html = render_issue_list(issues)
        assert "↳ source:" not in html

    def test_unmatched_location_renders_without_subline(self):
        source_map = {
            "entries": {
                "payload/database/DDL/views/{{DB}}.other.viw": {
                    "source_relpath": "10_domain/other.ddl",
                    "source_abspath": "C:/src/10_domain/other.ddl",
                    "type": "VIEW",
                }
            }
        }
        issues = [
            {
                "severity": "error",
                "code": "INSPECT_LINT_VIOLATION",
                "message": "rule fired",
                "location": "DDL\\views\\unmatched.viw:42",
            }
        ]
        html = render_issue_list(issues, source_map=source_map)
        assert "↳ source:" not in html


# ---------------------------------------------------------------
# render_issue_list — empty-issues branch (#495)
# ---------------------------------------------------------------


class TestRenderIssueListEmpty:
    """The empty-issues branch must not contradict the stage status badge."""

    def test_empty_with_no_status_renders_green_no_issues(self):
        """Back-compat — callers that don't pass stage_status get the
        original 'No issues recorded.' green note."""
        html = render_issue_list([])
        assert "No issues recorded" in html
        # Green colour (#28A745) signals "all good".
        assert "#28A745" in html

    def test_empty_with_success_status_renders_green_no_issues(self):
        html = render_issue_list([], stage_status="success")
        assert "No issues recorded" in html
        assert "#28A745" in html

    def test_empty_with_error_status_renders_red_failure_note(self):
        """The failing stage WITH zero issues used to show 'No issues
        recorded.' in green, contradicting the red ✗ badge above it.
        Now it shows an honest red 'failed without detail' note."""
        html = render_issue_list([], stage_status="error")
        assert "No issues recorded" not in html
        assert "Stage failed" in html
        assert "no detailed issues logged" in html
        # Red colour (#DC3545) matches the error badge.
        assert "#DC3545" in html


# ---------------------------------------------------------------
# render_issue_category_summary — per-code count table (#513)
# ---------------------------------------------------------------


def _issue(code: str, sev: str, msg: str = "") -> dict:
    return {"code": code, "severity": sev, "message": msg}


class TestRenderIssueCategorySummary:
    """The summary answers 'which knob do I turn first?' when there are
    many findings of a few recurring codes (#513)."""

    def test_returns_empty_when_below_threshold(self):
        """Fewer than 5 issues → no summary. The flat list is already
        the summary at that scale."""
        issues = [
            _issue("HARDCODED_NAME", "warning"),
            _issue("COMMA_STYLE", "info"),
        ]
        assert render_issue_category_summary(issues) == ""

    def test_returns_empty_when_only_one_code(self):
        """Single (code, severity) bucket → no table (nothing to rank)."""
        issues = [_issue("HARDCODED_NAME", "warning") for _ in range(10)]
        assert render_issue_category_summary(issues) == ""

    def test_renders_table_when_multiple_codes_and_over_threshold(self):
        issues = (
            [_issue("HARDCODED_NAME", "warning")] * 27
            + [_issue("COMMA_STYLE", "info")] * 8
            + [_issue("DB_QUALIFIER", "error")] * 2
        )
        html = render_issue_category_summary(issues)
        assert "Findings by category" in html
        assert "HARDCODED_NAME" in html
        assert "COMMA_STYLE" in html
        assert "DB_QUALIFIER" in html
        # Counts render.
        assert ">27<" in html
        assert ">8<" in html
        assert ">2<" in html

    def test_error_appears_before_warning_appears_before_info(self):
        """Severity precedence: error > warning > info, then count desc."""
        issues = (
            [_issue("A", "info")] * 10
            + [_issue("B", "warning")] * 5
            + [_issue("C", "error")] * 2
        )
        html = render_issue_category_summary(issues)
        # C (error, 2) appears BEFORE B (warning, 5) appears BEFORE A (info, 10).
        pos_a = html.index(">A<")
        pos_b = html.index(">B<")
        pos_c = html.index(">C<")
        assert pos_c < pos_b < pos_a

    def test_within_severity_higher_count_first(self):
        """Two warning codes → the one with more findings ranks first."""
        issues = [_issue("A", "warning")] * 3 + [_issue("B", "warning")] * 20
        html = render_issue_category_summary(issues)
        assert html.index(">B<") < html.index(">A<")

    def test_min_total_threshold_configurable(self):
        """`min_total=2` lets small stages summarise too — useful for
        unit tests and for future opt-in tuning if the default becomes
        too conservative."""
        issues = [
            _issue("A", "warning"),
            _issue("B", "warning"),
        ]
        # Default (5) suppresses.
        assert render_issue_category_summary(issues) == ""
        # Lowered threshold surfaces the table.
        html = render_issue_category_summary(issues, min_total=2)
        assert "Findings by category" in html

    def test_uncoded_issue_falls_back_to_placeholder(self):
        """Issues with empty code shouldn't crash the aggregator."""
        issues = [_issue("HARDCODED_NAME", "warning")] * 5 + [_issue("", "warning")] * 3
        html = render_issue_category_summary(issues)
        assert "(uncoded)" in html
