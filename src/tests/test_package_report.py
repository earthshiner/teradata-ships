"""
test_package_report.py — Tests for the interactive package report generator.

Covers:
    - _parse_waves_txt: absent file, valid file, multi-wave, separators
    - _scan_payload: empty payload, single file, extension mapping, wave assignment
    - _objects_tab: renders rows and filter buttons for each type
    - _waves_tab: no wave data message; renders SVG when waves present
    - _trust_tab: READY/BLOCKED/CAVEATS styling; signal rows; empty signals
    - _deploy_tab: commands present; prereqs note when requires non-empty
    - generate_package_report: end-to-end write; package_report.html in pkg_dir;
      embedded in the zip via build_package
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path


from td_release_packager.package_report import (
    _file_link,
    _objects_tab,
    _parse_waves_txt,
    _scan_payload,
    _script_summary,
    _summary_tab,
    _trust_tab,
    _waves_tab,
    _write_package_viewers,
    _deploy_tab,
    generate_package_report,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_payload(tmp_path: Path, files: list[tuple[str, str]]) -> Path:
    """Write DDL files into a payload structure.

    Each entry is (relative_path, content), e.g.
    ("03_ddl/tables/DB.Customer.tbl", "CREATE MULTISET TABLE ...").
    Returns the pkg_dir (tmp_path itself).
    """
    for rel, content in files:
        p = tmp_path / "payload" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def _minimal_manifest(pkg_name="test_pkg", build_no="0001", env="DEV") -> dict:
    return {
        "package_name": pkg_name,
        "build_number": build_no,
        "environment": env,
        "file_count": 1,
        "timestamp": "2026-05-10T12:00:00+00:00",
        "author": "tester",
        "description": "test",
        "trust": {
            "label": "READY",
            "signals": {
                "inspect_lint": {"status": "pass", "detail": "No violations"},
                "inspect_token_format": {"status": "pass", "detail": ""},
            },
        },
        "requires": [],
    }


# ---------------------------------------------------------------
# _parse_waves_txt
# ---------------------------------------------------------------


class TestParseWavesTxt:
    def test_returns_empty_when_no_file(self, tmp_path):
        result = _parse_waves_txt(str(tmp_path / "nonexistent.txt"))
        assert result == {}

    def test_single_wave(self, tmp_path):
        f = tmp_path / "_waves.txt"
        f.write_text("tables/DB.Customer.tbl\ntables/DB.Orders.tbl\n", encoding="utf-8")
        result = _parse_waves_txt(str(f))
        assert result["tables/DB.Customer.tbl"] == 1
        assert result["tables/DB.Orders.tbl"] == 1
        assert result["DB.Customer.tbl"] == 1
        assert result["DB.Orders.tbl"] == 1

    def test_multi_wave_with_separator(self, tmp_path):
        f = tmp_path / "_waves.txt"
        f.write_text(
            "tables/DB.Customer.tbl\n---\nviews/DB.ActiveCustomers.viw\n",
            encoding="utf-8",
        )
        result = _parse_waves_txt(str(f))
        assert result["tables/DB.Customer.tbl"] == 1
        assert result["views/DB.ActiveCustomers.viw"] == 2

    def test_comments_ignored(self, tmp_path):
        f = tmp_path / "_waves.txt"
        f.write_text("# Wave 1 — tables\ntables/DB.T.tbl\n", encoding="utf-8")
        result = _parse_waves_txt(str(f))
        assert result["tables/DB.T.tbl"] == 1
        assert result["DB.T.tbl"] == 1

    def test_blank_lines_ignored(self, tmp_path):
        f = tmp_path / "_waves.txt"
        f.write_text("\ntables/DB.T.tbl\n\n", encoding="utf-8")
        result = _parse_waves_txt(str(f))
        assert result["tables/DB.T.tbl"] == 1
        assert result["DB.T.tbl"] == 1

    def test_duplicate_basenames_do_not_share_ambiguous_wave_alias(self, tmp_path):
        f = tmp_path / "_waves.txt"
        f.write_text(
            "views/DB.Shared.viw\n---\nmacros/DB.Shared.viw\n", encoding="utf-8"
        )
        result = _parse_waves_txt(str(f))
        assert result["views/DB.Shared.viw"] == 1
        assert result["macros/DB.Shared.viw"] == 2
        assert "DB.Shared.viw" not in result


# ---------------------------------------------------------------
# _scan_payload
# ---------------------------------------------------------------


class TestScanPayload:
    def test_empty_payload_returns_empty(self, tmp_path):
        (tmp_path / "payload").mkdir()
        assert _scan_payload(str(tmp_path)) == []

    def test_no_payload_dir_returns_empty(self, tmp_path):
        assert _scan_payload(str(tmp_path)) == []

    def test_tbl_classified_as_table(self, tmp_path):
        _make_payload(tmp_path, [("03_ddl/tables/DB.Customer.tbl", "CREATE TABLE")])
        records = _scan_payload(str(tmp_path))
        assert len(records) == 1
        assert records[0]["type"] == "TABLE"
        assert records[0]["name"] == "DB.Customer"
        assert records[0]["path"] == "payload/03_ddl/tables/DB.Customer.tbl"

    def test_viw_classified_as_view(self, tmp_path):
        _make_payload(
            tmp_path, [("03_ddl/views/DB.ActiveCustomers.viw", "REPLACE VIEW")]
        )
        records = _scan_payload(str(tmp_path))
        assert records[0]["type"] == "VIEW"

    def test_spl_classified_as_procedure(self, tmp_path):
        _make_payload(
            tmp_path, [("03_ddl/procedures/DB.sp_Test.spl", "REPLACE PROCEDURE")]
        )
        records = _scan_payload(str(tmp_path))
        assert records[0]["type"] == "PROCEDURE"

    def test_control_files_skipped(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                ("03_ddl/tables/DB.T.tbl", "CREATE TABLE"),
                ("03_ddl/tables/_waves.txt", "DB.T.tbl"),
                ("03_ddl/tables/_order.txt", "DB.T.tbl"),
                ("03_ddl/tables/.gitkeep", ""),
            ],
        )
        records = _scan_payload(str(tmp_path))
        assert len(records) == 1

    def test_wave_assigned_from_waves_txt(self, tmp_path):
        _make_payload(tmp_path, [("03_ddl/tables/DB.T.tbl", "CREATE TABLE")])
        waves = tmp_path / "payload" / "03_ddl" / "_waves.txt"
        waves.write_text("tables/DB.T.tbl\n", encoding="utf-8")
        records = _scan_payload(str(tmp_path))
        assert records[0]["wave"] == 1

    def test_wave_lookup_uses_phase_relative_path_before_filename(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                ("03_ddl/views/DB.Shared.viw", "REPLACE VIEW"),
                ("03_ddl/macros/DB.Shared.viw", "REPLACE MACRO"),
            ],
        )
        waves = tmp_path / "payload" / "03_ddl" / "_waves.txt"
        waves.write_text(
            "views/DB.Shared.viw\n---\nmacros/DB.Shared.viw\n", encoding="utf-8"
        )
        records = _scan_payload(str(tmp_path))

        by_path = {record["path"]: record["wave"] for record in records}
        assert by_path["payload/03_ddl/views/DB.Shared.viw"] == 1
        assert by_path["payload/03_ddl/macros/DB.Shared.viw"] == 2

    def test_no_wave_file_gives_none_wave(self, tmp_path):
        _make_payload(tmp_path, [("03_ddl/tables/DB.T.tbl", "CREATE TABLE")])
        records = _scan_payload(str(tmp_path))
        assert records[0]["wave"] is None

    def test_multiple_types_sorted(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                ("03_ddl/views/DB.V.viw", "REPLACE VIEW"),
                ("03_ddl/tables/DB.T.tbl", "CREATE TABLE"),
            ],
        )
        records = _scan_payload(str(tmp_path))
        types = [r["type"] for r in records]
        assert "TABLE" in types and "VIEW" in types

    def test_records_include_script_intent(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                ("02_dcl/inter_db/DB.dcl", "REVOKE SELECT ON DB FROM role;"),
                ("03_ddl/procedures/DB.P.spl", "REPLACE PROCEDURE DB.P() BEGIN END;"),
                ("04_dml/seed.dml", "INSERT INTO DB.T (id) VALUES (1);"),
            ],
        )
        records = _scan_payload(str(tmp_path))
        by_file = {r["file"]: r["intent"] for r in records}
        assert by_file["DB.dcl"] == "REVOKE"
        assert by_file["DB.P.spl"] == "REPLACE/PROCEDURE"
        assert by_file["seed.dml"] == "INSERT"

    def test_grt_files_are_reported_as_dcl_grants(self, tmp_path):
        _make_payload(
            tmp_path,
            [("02_dcl/inter_db/APP_DB.grt", "GRANT SELECT ON DATA_DB TO APP_DB;")],
        )

        records = _scan_payload(str(tmp_path))

        assert records[0]["type"] == "GRANT"
        assert records[0]["phase"] == "DCL"
        assert records[0]["intent"] == "GRANT"

    def test_invalid_utf8_payload_does_not_abort_scan(self, tmp_path):
        file_path = tmp_path / "payload" / "03_ddl" / "procedures" / "DB.Native.spl"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"REPLACE PROCEDURE DB.Native()\xff\xfe")

        records = _scan_payload(str(tmp_path))

        assert len(records) == 1
        assert records[0]["file"] == "DB.Native.spl"
        assert records[0]["intent"] == "REPLACE/PROCEDURE"


# ---------------------------------------------------------------
# _summary_tab
# ---------------------------------------------------------------


class TestSummaryTab:
    def test_counts_dcl_ddl_and_dml_intents(self):
        records = [
            {"phase": "DCL", "intent": "GRANT", "type": "GRANT"},
            {"phase": "DCL", "intent": "REVOKE", "type": "GRANT"},
            {"phase": "DDL", "intent": "CREATE/PROCEDURE", "type": "PROCEDURE"},
            {"phase": "DML", "intent": "DELETE", "type": "DML"},
        ]
        summary = _script_summary(records)
        assert summary["DCL"]["GRANT"] == 1
        assert summary["DCL"]["REVOKE"] == 1
        assert summary["DDL"]["CREATE/PROCEDURE"] == 1
        assert summary["DML"]["DELETE"] == 1
        assert summary["DML"]["MERGE"] == 0

    def test_renders_zero_baseline_rows_and_flags(self):
        records = [
            {"phase": "DCL", "intent": "REVOKE", "type": "GRANT"},
            {"phase": "DML", "intent": "DELETE", "type": "DML"},
        ]
        html = _summary_tab(records)
        assert "DROP/TABLE" in html
        assert "MERGE" in html
        assert "DCL contains REVOKE scripts but no GRANT scripts" in html

    def test_nonzero_rows_get_has_count_class(self):
        """Issue #277 — rows with a non-zero count are tagged so the
        Summary tab can visually emphasise what's actually in the
        package, and faded-out rows are tagged separately."""
        records = [
            {"phase": "DCL", "intent": "REVOKE", "type": "GRANT"},
            {"phase": "DML", "intent": "DELETE", "type": "DML"},
        ]
        html = _summary_tab(records)
        # The two intents with records produce has-count rows.
        assert '<tr class="has-count"><td>REVOKE</td><td>1</td></tr>' in html
        assert '<tr class="has-count"><td>DELETE</td><td>1</td></tr>' in html
        # A baseline row that stayed at zero is tagged zero-count.
        assert '<tr class="zero-count"><td>MERGE</td><td>0</td></tr>' in html


# ---------------------------------------------------------------
# _objects_tab
# ---------------------------------------------------------------


class TestObjectsTab:
    def _sample_records(self):
        return [
            {
                "name": "DB.Customer",
                "type": "TABLE",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.Customer.tbl",
                "ext": ".tbl",
            },
            {
                "name": "DB.ActiveCustomers",
                "type": "VIEW",
                "phase": "DDL",
                "wave": 2,
                "file": "DB.ActiveCustomers.viw",
                "ext": ".viw",
            },
        ]

    def test_contains_object_names(self):
        html = _objects_tab(self._sample_records())
        assert "DB.Customer" in html
        assert "DB.ActiveCustomers" in html

    def test_contains_type_filter_buttons(self):
        html = _objects_tab(self._sample_records())
        assert "TABLE" in html
        assert "VIEW" in html

    def test_wave_displayed(self):
        html = _objects_tab(self._sample_records())
        assert "Wave 1" in html
        assert "Wave 2" in html

    def test_file_names_are_package_relative_links(self):
        records = self._sample_records()
        records[0]["path"] = "payload/03_ddl/tables/DB.Customer.tbl"
        html = _objects_tab(records)
        assert 'href="payload/03_ddl/tables/DB.Customer.tbl"' in html
        assert 'title="payload/03_ddl/tables/DB.Customer.tbl"' in html

    def test_object_names_have_full_name_tooltips(self):
        html = _objects_tab(self._sample_records())
        assert "title='DB.Customer'" in html

    def test_empty_records_no_crash(self):
        html = _objects_tab([])
        assert "<table" in html

    def test_blocking_trust_issue_flagged_on_matching_object(self):
        records = self._sample_records()
        records[0]["path"] = "payload/03_ddl/tables/DB.Customer.tbl"
        trust = {
            "label": "BLOCKED",
            "signals": {
                "inspect_lint": {
                    "status": "fail",
                    "message": "Coding Discipline lint violations: 1 error(s)",
                    "issues": [
                        "payload/03_ddl/tables/DB.Customer.tbl:1: [db_qualifier] bad"
                    ],
                }
            },
        }

        html = _objects_tab(records, trust)

        assert "BLOCKS TRUST" in html
        assert "inspect_lint" in html


# ---------------------------------------------------------------
# _waves_tab
# ---------------------------------------------------------------


class TestWavesTab:
    def test_no_wave_data_shows_message(self):
        html = _waves_tab([])
        assert "No wave data available" in html

    def test_renders_svg_when_waves_present(self):
        records = [
            {
                "name": "DB.T",
                "type": "TABLE",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.T.tbl",
                "ext": ".tbl",
            },
            {
                "name": "DB.V",
                "type": "VIEW",
                "phase": "DDL",
                "wave": 2,
                "file": "DB.V.viw",
                "ext": ".viw",
            },
        ]
        html = _waves_tab(records)
        assert "<svg" in html
        assert "Wave 1" in html
        assert "Wave 2" in html

    def test_truncated_wave_names_have_full_name_tooltip(self):
        records = [
            {
                "name": "DB.VeryLongObjectNameThatShouldBeTruncatedInSvg",
                "type": "VIEW",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.VeryLongObjectNameThatShouldBeTruncatedInSvg.viw",
                "ext": ".viw",
            },
        ]
        html = _waves_tab(records)
        assert "…" in html
        assert "<title>DB.VeryLongObjectNameThatShouldBeTruncatedInSvg</title>" in html

    def test_serial_column_for_none_wave(self):
        records = [
            {
                "name": "DB.DB1",
                "type": "DATABASE",
                "phase": "Pre-requisites",
                "wave": None,
                "file": "DB.DB1.db",
                "ext": ".db",
            },
        ]
        html = _waves_tab(records)
        assert "Serial" in html


# ---------------------------------------------------------------
# _trust_tab
# ---------------------------------------------------------------


class TestTrustTab:
    def test_ready_status_green(self):
        trust = {"status": "READY", "signals": {}}
        html = _trust_tab(trust)
        assert "READY" in html
        assert "#198754" in html  # green

    def test_blocked_status_red(self):
        trust = {"status": "BLOCKED", "signals": {}}
        html = _trust_tab(trust)
        assert "BLOCKED" in html
        assert "#DC3545" in html  # red

    def test_signals_rendered(self):
        trust = {
            "status": "READY",
            "signals": {
                # message is the canonical key (real trust signals)
                "inspect_lint": {"status": "pass", "message": "No violations"},
                "inspect_token_format": {
                    "status": "fail",
                    "message": "Malformed token",
                },
                # detail is the legacy key — kept for backward compat
                "inspect_grants": {"status": "warn", "detail": "Legacy detail key"},
            },
        }
        html = _trust_tab(trust)
        assert "inspect_lint" in html
        assert "inspect_token_format" in html
        assert "Malformed token" in html
        assert "Legacy detail key" in html

    def test_empty_signals_no_crash(self):
        html = _trust_tab({"status": "READY", "signals": {}})
        assert "No signals recorded" in html


# ---------------------------------------------------------------
# Signal name explanations
# ---------------------------------------------------------------


class TestSignalExplanations:
    """Tests for the expandable signal-name explanation cells.

    Covers ``_signal_name_cell`` directly and the integration through
    ``_trust_tab``.
    """

    # -- _signal_name_cell unit tests --

    def test_known_signals_render_as_details_element(self):
        from td_release_packager.package_report import _signal_name_cell

        for sig in (
            "inspect_lint",
            "inspect_token_format",
            "inspect_grants",
            "provenance_complete",
            "build_reproducible",
        ):
            html = _signal_name_cell(sig)
            assert "<details" in html, f"No <details> for {sig}"
            assert "<summary" in html, f"No <summary> for {sig}"
            assert sig in html, f"Signal name missing from cell for {sig}"

    def test_unknown_signal_degrades_to_plain_text(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("some_future_signal")
        assert "<details" not in html
        assert "some_future_signal" in html

    def test_inspect_lint_explanation_contains_expected_concepts(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("inspect_lint")
        assert "Coding Discipline" in html
        assert "If this fails" in html

    def test_inspect_token_format_explanation_references_token_syntax(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("inspect_token_format")
        assert "{{TOKEN}}" in html or "TOKEN" in html
        assert "If this fails" in html

    def test_inspect_grants_explanation_mentions_dcl(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("inspect_grants")
        assert "GRANT" in html or "DCL" in html
        assert "If this fails" in html

    def test_provenance_complete_explanation_mentions_provenance_json(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("provenance_complete")
        assert "ships.provenance.json" in html
        assert "If this fails" in html

    def test_build_reproducible_explanation_mentions_dirty_tree(self):
        from td_release_packager.package_report import _signal_name_cell

        html = _signal_name_cell("build_reproducible")
        assert "allow-dirty" in html or "dirty" in html
        assert "If this fails" in html

    def test_signal_name_cell_escapes_html(self):
        from td_release_packager.package_report import _signal_name_cell

        # Injects a signal name with HTML characters — must not render raw tags.
        html = _signal_name_cell("<script>alert(1)</script>")
        assert "<script>" not in html

    # -- _trust_tab integration --

    def test_trust_tab_renders_details_for_all_known_signals(self):
        trust = {
            "label": "READY",
            "signals": {
                "inspect_lint": {"status": "pass", "message": "No violations"},
                "inspect_token_format": {"status": "pass", "message": "Clean"},
                "inspect_grants": {"status": "pass", "message": "Clean"},
                "provenance_complete": {"status": "pass", "message": "Present"},
                "build_reproducible": {"status": "pass", "message": "Clean tree"},
            },
        }
        html = _trust_tab(trust)
        assert html.count("<details") == 5

    def test_trust_tab_unknown_signal_has_no_details(self):
        trust = {
            "label": "READY",
            "signals": {
                "unknown_new_signal": {"status": "pass", "message": "ok"},
            },
        }
        html = _trust_tab(trust)
        assert "<details" not in html
        assert "unknown_new_signal" in html

    def test_trust_tab_includes_table_id_for_css_scoping(self):
        html = _trust_tab({"label": "READY", "signals": {}})
        assert "trust-signals-table" in html

    def test_trust_tab_css_hides_native_details_marker(self):
        html = _trust_tab({"label": "READY", "signals": {}})
        assert "list-style: none" in html or "list-style:none" in html

    def test_trust_tab_short_title_present_in_expansion(self):
        trust = {
            "label": "READY",
            "signals": {
                "inspect_lint": {"status": "pass", "message": "No violations"},
            },
        }
        html = _trust_tab(trust)
        assert "Coding Discipline lint" in html

    def test_trust_tab_if_this_fails_guidance_present(self):
        trust = {
            "label": "BLOCKED",
            "signals": {
                "inspect_grants": {
                    "status": "fail",
                    "message": "Grant drift: 1 error",
                    "issues": ["DB.table: undeclared grant"],
                },
            },
        }
        html = _trust_tab(trust)
        assert "If this fails" in html


# ---------------------------------------------------------------
# _deploy_tab
# ---------------------------------------------------------------


class TestDeployTab:
    def test_all_three_commands_present(self):
        html = _deploy_tab(_minimal_manifest())
        assert "--dry-run" in html
        assert "--streams 4" in html
        assert "--continue-on-error" in html

    def test_no_prereqs_note_when_empty_requires(self):
        html = _deploy_tab(_minimal_manifest())
        assert "companion" not in html.lower()

    def test_prereqs_note_when_requires_set(self):
        m = _minimal_manifest()
        m["requires"] = ["OMR_prereqs_DEV_BUILD_0001.zip"]
        html = _deploy_tab(m)
        assert "Associated packages" in html
        assert "OMR_prereqs_DEV_BUILD_0001.zip" in html

    def test_release_group_recommendation_when_requires_set(self):
        m = _minimal_manifest(pkg_name="BIONICCC_17", build_no="0038")
        m["package_filename"] = "DEV_BIONICCC_17_BUILD_0038_02_main.zip"
        m["release_group"] = "DEV_BIONICCC_17_BUILD_0038"
        m["requires"] = ["DEV_BIONICCC_17_BUILD_0038_01_prereqs.zip"]

        html = _deploy_tab(m)

        assert "Recommended" in html
        assert "deploy_release.py" in html
        assert "python deploy_release.py --host" in html
        assert "Single-package commands" in html
        assert "Associated packages" in html
        assert "DEV_BIONICCC_17_BUILD_0038_01_prereqs.zip" in html

    def test_copy_buttons_present(self):
        html = _deploy_tab(_minimal_manifest())
        assert "clipboard" in html or "copyCmd" in html


# ---------------------------------------------------------------
# generate_package_report — end-to-end
# ---------------------------------------------------------------


class TestGeneratePackageReport:
    def test_writes_html_file(self, tmp_path):
        _make_payload(
            tmp_path, [("03_ddl/tables/DB.T.tbl", "CREATE TABLE DB.T (id INT);")]
        )
        m = _minimal_manifest()
        path = generate_package_report(str(tmp_path), m)
        assert os.path.isfile(path)
        assert path.endswith("package_report.html")

    def test_html_contains_package_name(self, tmp_path):
        _make_payload(tmp_path, [("03_ddl/tables/DB.T.tbl", "CREATE TABLE")])
        m = _minimal_manifest(pkg_name="my_project")
        generate_package_report(str(tmp_path), m)
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "my_project" in html

    def test_html_contains_object_name(self, tmp_path):
        _make_payload(tmp_path, [("03_ddl/tables/DB.Customer.tbl", "CREATE TABLE")])
        m = _minimal_manifest()
        generate_package_report(str(tmp_path), m)
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "DB.Customer" in html

    def test_html_contains_summary_tab(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                ("02_dcl/inter_db/DB.dcl", "GRANT SELECT ON DB TO role;"),
                ("03_ddl/macros/DB.M.mcr", "REPLACE MACRO DB.M AS (SELECT 1;);"),
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "tab-summary" in html
        assert "Summary" in html
        assert "REPLACE/MACRO" in html

    def test_summary_count_highlight_css_rules_present(self, tmp_path):
        """Issue #277 — the CSS that drives row emphasis is in the
        generated document."""
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert ".summary-table tr.has-count td" in html
        assert ".summary-table tr.zero-count td" in html

    def test_environment_prereq_banner_names_extracted_package_dir(self, tmp_path):
        manifest = _minimal_manifest()
        manifest["role"] = "environment_prereqs"
        manifest["package_filename"] = "DEV_APP_BUILD_0001_00_environment_prereqs.zip"

        generate_package_report(str(tmp_path), manifest)

        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "Do not edit" in html
        assert "source project" in html
        assert ".ships-work/DEV_APP_BUILD_0001_00_environment_prereqs" in html
        assert "--package-dir" in html

    def test_invalid_utf8_payload_still_writes_report(self, tmp_path):
        file_path = tmp_path / "payload" / "03_ddl" / "procedures" / "DB.Native.spl"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"REPLACE PROCEDURE DB.Native()\xff\xfe")

        path = generate_package_report(str(tmp_path), _minimal_manifest())

        assert os.path.isfile(path)
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "DB.Native" in html

    def test_html_is_valid_utf8_and_has_doctype(self, tmp_path):
        _make_payload(tmp_path, [])
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html

    def test_report_embedded_in_zip_via_build_package(self, tmp_project, tmp_path):
        """build_package embeds package_report.html in the archive."""
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        ddl = tmp_project / "payload" / "database" / "DDL" / "tables"
        ddl.mkdir(parents=True, exist_ok=True)
        (ddl / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);\n",
            encoding="utf-8",
        )
        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="report_test",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
            allow_dirty=True,
        )
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair

        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        report_entries = [n for n in names if n.endswith("package_report.html")]
        assert report_entries, (
            f"package_report.html not found in archive. Files: {names[:20]}"
        )


# ---------------------------------------------------------------
# report_viewer — shared SQL highlighting helpers
# ---------------------------------------------------------------


class TestReportViewer:
    """Tests for the shared SQL syntax-highlighting module.

    Verifies that ``highlight_sql`` and ``source_viewer_html`` produce
    correct output independently of either report module.
    """

    def test_highlight_sql_wraps_keywords(self):
        from td_release_packager.report_viewer import highlight_sql

        result = highlight_sql("CREATE TABLE DB.T (Id INTEGER);")
        assert '<span class="sql-keyword">CREATE</span>' in result
        assert '<span class="sql-keyword">TABLE</span>' in result

    def test_highlight_sql_wraps_string_literal(self):
        from td_release_packager.report_viewer import highlight_sql

        result = highlight_sql("COMMENT ON TABLE T IS 'hello world';")
        assert '<span class="sql-string">&#x27;hello world&#x27;</span>' in result

    def test_highlight_sql_wraps_line_comment(self):
        from td_release_packager.report_viewer import highlight_sql

        result = highlight_sql("-- this is a comment\nSELECT 1;")
        assert '<span class="sql-comment">-- this is a comment</span>' in result

    def test_highlight_sql_wraps_block_comment(self):
        from td_release_packager.report_viewer import highlight_sql

        result = highlight_sql("/* block */ SELECT 1;")
        assert '<span class="sql-comment">/* block */</span>' in result

    def test_highlight_sql_does_not_match_keyword_inside_string(self):
        from td_release_packager.report_viewer import highlight_sql

        # The word CREATE inside a string literal must NOT be wrapped as a keyword.
        result = highlight_sql("IS 'CREATE TABLE foo';")
        # The string span wraps the whole literal — no nested keyword span inside it.
        assert "sql-string" in result
        # There should be exactly zero keyword spans (CREATE is inside the literal).
        assert result.count('class="sql-keyword"') == 0

    def test_highlight_sql_escapes_html_in_plain_text(self):
        from td_release_packager.report_viewer import highlight_sql

        result = highlight_sql("a < b AND b > c")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_source_viewer_html_is_complete_document(self):
        from td_release_packager.report_viewer import source_viewer_html

        html = source_viewer_html(
            title="Test file",
            packaged_path="payload/03_ddl/tables/DB.T.tbl",
            source_path="C:/SCM/project/source/DB.T.tbl",
            content="CREATE MULTISET TABLE DB.T (Id INTEGER);",
        )
        assert "<!DOCTYPE html>" in html
        assert "DB.T.tbl" in html
        assert '<span class="sql-keyword">CREATE</span>' in html
        # Metadata lines present
        assert "payload/03_ddl/tables/DB.T.tbl" in html
        assert "C:/SCM/project/source/DB.T.tbl" in html

    def test_source_viewer_html_escapes_title(self):
        from td_release_packager.report_viewer import source_viewer_html

        html = source_viewer_html(
            title="<script>alert(1)</script>",
            packaged_path="payload/x.tbl",
            source_path="x.tbl",
            content="SELECT 1;",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_safe_viewer_filename_basic(self):
        from td_release_packager.report_viewer import safe_viewer_filename

        name = safe_viewer_filename("03_ddl/tables/DB.Customer.tbl", 1)
        assert name.startswith("0001_")
        assert name.endswith(".html")
        # Path separators replaced with underscores
        assert "/" not in name

    def test_safe_viewer_filename_zero_padded(self):
        from td_release_packager.report_viewer import safe_viewer_filename

        assert safe_viewer_filename("x.tbl", 42).startswith("0042_")

    def test_safe_viewer_filename_empty_path_uses_fallback(self):
        from td_release_packager.report_viewer import safe_viewer_filename

        name = safe_viewer_filename("", 7)
        assert "source_7" in name
        assert name.endswith(".html")


# ---------------------------------------------------------------
# _write_package_viewers and viewer link integration
# ---------------------------------------------------------------


class TestWritePackageViewers:
    """Tests for viewer-page generation in the package report.

    Covers the new ``_write_package_viewers`` function and the updated
    ``_file_link`` / ``_objects_tab`` integration.
    """

    def _make_pkg(self, tmp_path: Path, files: list[tuple[str, str]]) -> Path:
        """Write payload files and return the pkg_dir."""
        for rel, content in files:
            p = tmp_path / "payload" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return tmp_path

    # -- _write_package_viewers unit tests --

    def test_writes_viewer_pages_for_each_record(self, tmp_path):
        # _write_package_viewers imported at top of file

        self._make_pkg(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.Customer.tbl",
                    "CREATE MULTISET TABLE DB.Customer (Id INTEGER);",
                ),
                (
                    "03_ddl/views/DB.v_Active.viw",
                    "REPLACE VIEW DB.v_Active AS SELECT 1;",
                ),
            ],
        )
        records = [
            {
                "path": "payload/03_ddl/tables/DB.Customer.tbl",
                "file": "DB.Customer.tbl",
            },
            {"path": "payload/03_ddl/views/DB.v_Active.viw", "file": "DB.v_Active.viw"},
        ]
        links = _write_package_viewers(str(tmp_path), records)

        assert len(links) == 2
        # Each link points into the hidden viewer directory
        for href in links.values():
            assert href.startswith(".package_report_code/")
            viewer_path = tmp_path / href.replace("/", os.sep)
            assert viewer_path.exists(), f"Viewer page not written: {viewer_path}"

    def test_viewer_pages_contain_highlighted_sql(self, tmp_path):
        # _write_package_viewers imported at top of file

        self._make_pkg(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        records = [{"path": "payload/03_ddl/tables/DB.T.tbl", "file": "DB.T.tbl"}]
        links = _write_package_viewers(str(tmp_path), records)

        assert links
        href = next(iter(links.values()))
        viewer_html = (tmp_path / href.replace("/", os.sep)).read_text(encoding="utf-8")
        assert '<span class="sql-keyword">CREATE</span>' in viewer_html

    def test_link_keys_use_forward_slashes_no_leading_dot(self, tmp_path):
        # _write_package_viewers imported at top of file

        self._make_pkg(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "SELECT 1;")],
        )
        records = [{"path": "payload/03_ddl/tables/DB.T.tbl", "file": "DB.T.tbl"}]
        links = _write_package_viewers(str(tmp_path), records)

        key = next(iter(links.keys()))
        assert "\\" not in key
        assert not key.startswith(".")
        assert not key.startswith("/")

    def test_missing_file_skipped_gracefully(self, tmp_path):
        # _write_package_viewers imported at top of file

        # Record references a file that does not exist on disk — must not crash.
        records = [
            {"path": "payload/03_ddl/tables/NonExistent.tbl", "file": "NonExistent.tbl"}
        ]
        links = _write_package_viewers(str(tmp_path), records)
        assert links == {}

    def test_record_with_empty_path_skipped(self, tmp_path):
        # _write_package_viewers imported at top of file

        records = [{"path": "", "file": ""}]
        links = _write_package_viewers(str(tmp_path), records)
        assert links == {}

    # -- _file_link viewer-link integration --

    def test_file_link_uses_viewer_href_when_available(self):
        # _file_link imported at top of file

        record = {"path": "payload/03_ddl/tables/DB.T.tbl", "file": "DB.T.tbl"}
        viewer_links = {
            "payload/03_ddl/tables/DB.T.tbl": ".package_report_code/0001_payload_03_ddl_tables_DB.T.tbl.html"
        }
        html = _file_link(record, viewer_links)
        assert ".package_report_code/" in html
        # The title attribute still shows the raw payload path for discoverability
        assert 'title="payload/03_ddl/tables/DB.T.tbl"' in html

    def test_file_link_falls_back_to_raw_path_without_viewer_links(self):
        # _file_link imported at top of file

        record = {"path": "payload/03_ddl/tables/DB.T.tbl", "file": "DB.T.tbl"}
        html = _file_link(record)
        assert 'href="payload/03_ddl/tables/DB.T.tbl"' in html

    def test_file_link_falls_back_when_path_not_in_viewer_links(self):
        # _file_link imported at top of file

        record = {"path": "payload/03_ddl/tables/DB.T.tbl", "file": "DB.T.tbl"}
        # Viewer links exist but for a different file
        viewer_links = {
            "payload/03_ddl/tables/Other.tbl": ".package_report_code/0001_Other.html"
        }
        html = _file_link(record, viewer_links)
        assert 'href="payload/03_ddl/tables/DB.T.tbl"' in html

    # -- _objects_tab with viewer_links --

    def test_objects_tab_links_point_to_viewer_when_supplied(self):
        # _objects_tab imported at top of file

        records = [
            {
                "name": "DB.Customer",
                "type": "TABLE",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.Customer.tbl",
                "path": "payload/03_ddl/tables/DB.Customer.tbl",
                "ext": ".tbl",
                "intent": "CREATE_ONLY",
            }
        ]
        viewer_links = {
            "payload/03_ddl/tables/DB.Customer.tbl": ".package_report_code/0001_DB.Customer.tbl.html"
        }
        html = _objects_tab(records, viewer_links=viewer_links)
        assert ".package_report_code/" in html
        # Raw path must NOT be the href (it appears in title only)
        assert 'href="payload/03_ddl/tables/DB.Customer.tbl"' not in html

    def test_objects_tab_falls_back_to_raw_path_without_viewer_links(self):
        # _objects_tab imported at top of file

        records = [
            {
                "name": "DB.Customer",
                "type": "TABLE",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.Customer.tbl",
                "path": "payload/03_ddl/tables/DB.Customer.tbl",
                "ext": ".tbl",
                "intent": "CREATE_ONLY",
            }
        ]
        html = _objects_tab(records)
        assert 'href="payload/03_ddl/tables/DB.Customer.tbl"' in html

    # -- generate_package_report end-to-end --

    def test_generate_package_report_writes_viewer_directory(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.Customer.tbl",
                    "CREATE MULTISET TABLE DB.Customer (Id INTEGER);",
                )
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        viewer_dir = tmp_path / ".package_report_code"
        assert viewer_dir.is_dir(), "Viewer directory was not created"
        viewer_files = list(viewer_dir.glob("*.html"))
        assert viewer_files, "No viewer HTML files written"

    def test_generate_package_report_report_links_to_viewer(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.Customer.tbl",
                    "CREATE MULTISET TABLE DB.Customer (Id INTEGER);",
                )
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        report_html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert ".package_report_code/" in report_html

    def test_generate_package_report_viewer_contains_highlighted_sql(self, tmp_path):
        _make_payload(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.Customer.tbl",
                    "CREATE MULTISET TABLE DB.Customer (Id INTEGER);",
                )
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        viewer_dir = tmp_path / ".package_report_code"
        viewer_file = next(viewer_dir.glob("*.html"))
        viewer_html = viewer_file.read_text(encoding="utf-8")
        assert '<span class="sql-keyword">CREATE</span>' in viewer_html

    def test_generate_package_report_viewer_contains_packaged_path_metadata(
        self, tmp_path
    ):
        _make_payload(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.Customer.tbl",
                    "CREATE MULTISET TABLE DB.Customer (Id INTEGER);",
                )
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        viewer_dir = tmp_path / ".package_report_code"
        viewer_file = next(viewer_dir.glob("*.html"))
        viewer_html = viewer_file.read_text(encoding="utf-8")
        # Packaged path metadata line must be present
        assert "03_ddl/tables/DB.Customer.tbl" in viewer_html

    def test_generate_package_report_multiple_files_get_separate_viewers(
        self, tmp_path
    ):
        _make_payload(
            tmp_path,
            [
                (
                    "03_ddl/tables/DB.T1.tbl",
                    "CREATE MULTISET TABLE DB.T1 (Id INTEGER);",
                ),
                (
                    "03_ddl/tables/DB.T2.tbl",
                    "CREATE MULTISET TABLE DB.T2 (Id INTEGER);",
                ),
                ("03_ddl/views/DB.v1.viw", "REPLACE VIEW DB.v1 AS SELECT 1;"),
            ],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        viewer_dir = tmp_path / ".package_report_code"
        viewer_files = list(viewer_dir.glob("*.html"))
        assert len(viewer_files) == 3

    def test_generate_package_report_empty_payload_no_viewer_dir(self, tmp_path):
        # Empty payload — no files to view, so the viewer dir should not be created.
        (tmp_path / "payload").mkdir()
        generate_package_report(str(tmp_path), _minimal_manifest())
        viewer_dir = tmp_path / ".package_report_code"
        assert not viewer_dir.exists()


# ---------------------------------------------------------------
# _load_build_provenance and _build_provenance_tab
# ---------------------------------------------------------------


class TestBuildProvenanceTab:
    """Tests for the Build Provenance tab in the package report."""

    def _sample_stages(self):
        return [
            {
                "stage": "harvest",
                "status": "success",
                "started_at": "2026-05-28T12:00:00+00:00",
                "finished_at": "2026-05-28T12:00:03+00:00",
                "duration_ms": 3100,
                "inputs": {"source_dir": "/project", "total_files": 22},
                "outputs": {
                    "classified": 778,
                    "unclassified": 10,
                    "files_placed": 778,
                    "multiset_injected": 37,
                    "cleaned": 211,
                },
                "decisions": {},
                "issues": [],
                "issue_counts": {"error": 0, "warning": 0, "info": 0},
            },
            {
                "stage": "inspect",
                "status": "warning",
                "started_at": "2026-05-28T12:00:04+00:00",
                "finished_at": "2026-05-28T12:00:06+00:00",
                "duration_ms": 2200,
                "inputs": {"files_scanned": 45},
                "outputs": {
                    "lint_errors": 0,
                    "lint_warnings": 2,
                    "files_with_issues": 1,
                    "overall_passed": True,
                },
                "decisions": {},
                "issues": [
                    {
                        "severity": "warning",
                        "code": "LEADING_COMMA_VIOLATION",
                        "message": "File uses trailing commas",
                        "location": "payload/03_ddl/tables/DB.T.tbl",
                    }
                ],
                "issue_counts": {"error": 0, "warning": 1, "info": 0},
            },
            {
                "stage": "analyse",
                "status": "success",
                "started_at": "2026-05-28T12:00:07+00:00",
                "finished_at": "2026-05-28T12:00:08+00:00",
                "duration_ms": 800,
                "inputs": {},
                "outputs": {
                    "object_count": 153,
                    "wave_count": 6,
                    "dependency_count": 42,
                    "cycle_count": 0,
                },
                "decisions": {},
                "issues": [],
                "issue_counts": {"error": 0, "warning": 0, "info": 0},
            },
        ]

    # -- _load_build_provenance --

    def test_returns_empty_when_no_decisions_json(self, tmp_path):
        from td_release_packager.package_report import _load_build_provenance

        result = _load_build_provenance(str(tmp_path))
        assert result == []

    def test_returns_stages_from_latest_run(self, tmp_path):
        import json
        from td_release_packager.package_report import _load_build_provenance

        decisions = {
            "schema_version": 1,
            "runs": [
                {
                    "run_id": "old",
                    "stages": [{"stage": "harvest", "status": "success"}],
                },
                {
                    "run_id": "latest",
                    "stages": self._sample_stages(),
                },
            ],
        }
        (tmp_path / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
        stages = _load_build_provenance(str(tmp_path))
        assert len(stages) == 3
        assert stages[0]["stage"] == "harvest"

    def test_walks_up_from_subdirectory(self, tmp_path):
        """Decisions file in project root is found when pkg_dir is a subdir."""
        import json
        from td_release_packager.package_report import _load_build_provenance

        decisions = {
            "schema_version": 1,
            "runs": [{"run_id": "r1", "stages": self._sample_stages()}],
        }
        (tmp_path / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
        subdir = tmp_path / "releases" / "pkg"
        subdir.mkdir(parents=True)
        stages = _load_build_provenance(str(subdir))
        assert len(stages) == 3

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        from td_release_packager.package_report import _load_build_provenance

        (tmp_path / "ships.decisions.json").write_text("not json", encoding="utf-8")
        result = _load_build_provenance(str(tmp_path))
        assert result == []

    def test_returns_empty_when_runs_list_empty(self, tmp_path):
        import json
        from td_release_packager.package_report import _load_build_provenance

        decisions = {"schema_version": 1, "runs": []}
        (tmp_path / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
        result = _load_build_provenance(str(tmp_path))
        assert result == []

    # -- _build_provenance_tab --

    def test_returns_placeholder_when_no_stages(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab([])
        assert "ships.decisions.json" in html
        assert "not available" in html

    def test_all_stage_names_appear(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        assert "harvest" in html
        assert "inspect" in html
        assert "analyse" in html

    def test_status_icons_rendered(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        # success → ✔, warning → ⚠
        assert "✔" in html
        assert "⚠" in html

    def test_issue_counts_shown_for_warning_stage(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        assert "1 warning" in html

    def test_issue_message_in_detail_panel(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        assert "LEADING_COMMA_VIOLATION" in html
        assert "File uses trailing commas" in html

    def test_error_stage_auto_opens(self):
        from td_release_packager.package_report import _build_provenance_tab

        stages = self._sample_stages()
        stages[0]["status"] = "error"
        stages[0]["issue_counts"] = {"error": 1, "warning": 0, "info": 0}
        stages[0]["issues"] = [
            {"severity": "error", "code": "E001", "message": "Something failed"}
        ]
        html = _build_provenance_tab(stages)
        # The details element for the error stage must have the open attribute
        assert "<details open" in html

    def test_key_metrics_rendered_for_harvest(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        assert "778" in html  # classified count
        assert "classified" in html

    def test_key_metrics_rendered_for_analyse(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        assert "153" in html  # object_count
        assert "6" in html  # wave_count

    def test_zero_noise_metrics_suppressed(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        # cycle_count=0 and lint_errors=0 should not appear as metrics
        assert "0 cycles" not in html
        assert "0 lint errors" not in html

    def test_duration_formatted(self):
        from td_release_packager.package_report import _build_provenance_tab

        html = _build_provenance_tab(self._sample_stages())
        # harvest is 3100 ms → "3.1 s"
        assert "3.1 s" in html

    def test_generate_package_report_includes_provenance_tab(self, tmp_path):
        """End-to-end: tab button and pane are present in the generated report."""
        import json

        decisions = {
            "schema_version": 1,
            "runs": [{"run_id": "r1", "stages": self._sample_stages()}],
        }
        (tmp_path / "ships.decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")

        assert "tab-provenance" in html
        assert "Build Provenance" in html
        assert "harvest" in html

    def test_generate_package_report_provenance_tab_absent_gracefully(self, tmp_path):
        """No decisions.json → tab is present but shows the placeholder message."""
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")

        # Tab button still present
        assert "Build Provenance" in html
        # Placeholder message visible
        assert "not available" in html


# ---------------------------------------------------------------
# _guide_tab
# ---------------------------------------------------------------


class TestGuideTab:
    """Tests for the reader's Guide tab."""

    def _manifest(self, **overrides):
        m = _minimal_manifest()
        m.update(overrides)
        return m

    def _records(self):
        return [
            {
                "name": "DB.Customer",
                "type": "TABLE",
                "phase": "DDL",
                "wave": 1,
                "file": "DB.Customer.tbl",
                "path": "payload/03_ddl/tables/DB.Customer.tbl",
                "ext": ".tbl",
                "intent": "CREATE_ONLY",
            },
            {
                "name": "DB.v_Active",
                "type": "VIEW",
                "phase": "DDL",
                "wave": 2,
                "file": "DB.v_Active.viw",
                "path": "payload/03_ddl/views/DB.v_Active.viw",
                "ext": ".viw",
                "intent": "CREATE_ONLY",
            },
            {
                "name": "DB.role",
                "type": "GRANT",
                "phase": "DCL",
                "wave": None,
                "file": "DB.role.dcl",
                "path": "payload/02_dcl/DB.role.dcl",
                "ext": ".dcl",
                "intent": "GRANT",
            },
        ]

    def test_guide_tab_renders_without_crash(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert html

    def test_build_number_appears(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(build_number="0042"), self._records())
        assert "0042" in html

    def test_environment_appears(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(environment="PRD"), self._records())
        assert "PRD" in html

    def test_present_phases_listed(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert "DCL" in html
        assert "DDL" in html

    def test_wave_count_mentioned_when_waves_present(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        # 2 waves (wave 1 and 2) should be mentioned
        assert "2" in html
        assert "waves" in html

    def test_no_wave_sentence_when_no_waves(self):
        from td_release_packager.package_report import _guide_tab

        records = [r.copy() for r in self._records()]
        for r in records:
            r["wave"] = None
        html = _guide_tab(self._manifest(), records)
        # When there are no waves, the specific wave-count sentence must not appear.
        # The sentence always starts with "The DDL phase is further divided into"
        assert "further divided into" not in html

    def test_main_package_role_description(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(role="main"), self._records())
        assert "main package" in html

    def test_prereqs_role_description(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(role="prereqs"), self._records())
        assert "pre-requisites package" in html

    def test_five_steps_present(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert "guide-step-num" in html
        assert html.count("guide-step-num") == 5

    def test_glossary_contains_ships(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert "<dt>SHIPS</dt>" in html

    def test_glossary_contains_wave(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert "<dt>Wave</dt>" in html

    def test_tooltip_data_tip_attributes_present(self):
        from td_release_packager.package_report import _guide_tab

        html = _guide_tab(self._manifest(), self._records())
        assert "data-tip=" in html

    def test_guide_tab_in_generate_package_report(self, tmp_path):
        """End-to-end: Guide tab button and pane appear in the report."""
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        assert "tab-guide" in html
        assert "Reader&#x27;s Guide" in html or "Reader's Guide" in html
        assert "guide-hero" in html

    def test_guide_is_first_active_tab(self, tmp_path):
        """Guide tab should be the active tab (first) in the generated report."""
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")
        # The guide pane must be the active one
        assert 'id="tab-guide" class="tab-pane active card"' in html
        # Summary must NOT be active
        assert 'id="tab-summary" class="tab-pane active' not in html
