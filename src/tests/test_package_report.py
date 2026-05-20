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
    _objects_tab,
    _parse_waves_txt,
    _scan_payload,
    _script_summary,
    _summary_tab,
    _trust_tab,
    _waves_tab,
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
        f.write_text("views/DB.Shared.viw\n---\nmacros/DB.Shared.viw\n", encoding="utf-8")
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
        waves.write_text("views/DB.Shared.viw\n---\nmacros/DB.Shared.viw\n", encoding="utf-8")
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
    def test_ready_label_green(self):
        trust = {"label": "READY", "signals": {}}
        html = _trust_tab(trust)
        assert "READY" in html
        assert "#198754" in html  # green

    def test_blocked_label_red(self):
        trust = {"label": "BLOCKED", "signals": {}}
        html = _trust_tab(trust)
        assert "BLOCKED" in html
        assert "#DC3545" in html  # red

    def test_signals_rendered(self):
        trust = {
            "label": "READY",
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
        html = _trust_tab({"label": "READY", "signals": {}})
        assert "No signals recorded" in html


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
        assert "companion" in html.lower() or "Deploy the companion" in html

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
        )
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair

        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
        report_entries = [n for n in names if n.endswith("package_report.html")]
        assert report_entries, (
            f"package_report.html not found in archive. Files: {names[:20]}"
        )
