#!/usr/bin/env python3
"""test_content_provenance.py — Issue #398

Tests for the Content Provenance tab in the package report:
  - _load_content_provenance: reads context/ships.provenance.json (v2),
    returns the parsed dict or None for missing / malformed / unsupported-version.
  - _content_provenance_tab: renders one row per packaged file with the
    full source -> eponymous -> token_resolved -> package chain, falling
    back to a placeholder when the loader returned None.
  - generate_package_report integration: the new tab is wired in beside
    Build Provenance and renders both presence and absence cleanly.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from td_release_packager.package_report import (
    _content_provenance_tab,
    _load_content_provenance,
    generate_package_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_provenance(pkg_dir: Path, doc: dict) -> None:
    ctx = pkg_dir / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "ships.provenance.json").write_text(json.dumps(doc), encoding="utf-8")


def _make_payload(pkg_dir: Path, files):
    """Mirror of test_package_report._make_payload — minimal payload skeleton."""
    for rel, content in files:
        dest = pkg_dir / "payload" / "database" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _minimal_manifest(pkg_name="test_pkg", build_no="0001", env="DEV") -> dict:
    return {
        "package_name": pkg_name,
        "build_number": build_no,
        "environment": env,
        "file_count": 1,
        "package_filename": f"{pkg_name}_{build_no}.zip",
        "release_group": f"{env}_{pkg_name}_BUILD_{build_no}_20260625120000",
    }


def _v2_doc_two_entries() -> dict:
    """A realistic two-entry v2 document covering an untokenised file and a
    prefix-tokenised view (the shape that exercises every stage status)."""
    return {
        "schema_version": "2.0",
        "version": 2,
        "generated_at": "2026-06-25T23:00:00+00:00",
        "entries": {
            "payload/03_ddl/tables/DB.T.tbl": {
                "stages": [
                    {
                        "stage": "source",
                        "path": "src/legacy/DB.T.sql",
                        "status": "applied",
                    },
                    {
                        "stage": "eponymous",
                        "path": "DB.T.tbl",
                        "status": "applied",
                        "note": "renamed from legacy",
                    },
                    {
                        "stage": "token_resolved",
                        "path": "DB.T.tbl",
                        "status": "no_op",
                        "note": "no tokens in filename",
                    },
                    {
                        "stage": "package",
                        "path": "payload/03_ddl/tables/DB.T.tbl",
                        "status": "applied",
                    },
                ]
            },
            "payload/03_ddl/views/{{DB_PREFIX}}_SEM_STD_V.MyView.viw": {
                "stages": [
                    {
                        "stage": "source",
                        "path": "src/views/MyView.sql",
                        "status": "applied",
                    },
                    {
                        "stage": "eponymous",
                        "path": "{{DB_PREFIX}}_SEM_STD_V.MyView.viw",
                        "status": "applied",
                    },
                    {
                        "stage": "token_resolved",
                        "path": "{{DB_PREFIX}}_SEM_STD_V.MyView.viw",
                        "status": "no_op",
                        "note": "tokens stay in filename until package-time substitution",
                    },
                    {
                        "stage": "package",
                        "path": "payload/03_ddl/views/{{DB_PREFIX}}_SEM_STD_V.MyView.viw",
                        "status": "applied",
                    },
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# _load_content_provenance
# ---------------------------------------------------------------------------


class TestLoadContentProvenance:
    def test_returns_parsed_doc_for_v2_file(self, tmp_path):
        _write_provenance(tmp_path, _v2_doc_two_entries())
        result = _load_content_provenance(str(tmp_path))
        assert result is not None
        assert result["version"] == 2
        assert "payload/03_ddl/tables/DB.T.tbl" in result["entries"]

    def test_returns_none_when_file_missing(self, tmp_path):
        assert _load_content_provenance(str(tmp_path)) is None

    def test_returns_none_when_file_malformed(self, tmp_path):
        ctx = tmp_path / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "ships.provenance.json").write_text(
            "not valid json {{", encoding="utf-8"
        )
        assert _load_content_provenance(str(tmp_path)) is None

    def test_returns_none_for_unrecognised_version(self, tmp_path):
        """v1 (flat package_path: source_path dict) is not parseable by this
        loader — surface as 'not available' rather than render garbage."""
        _write_provenance(tmp_path, {"version": 1, "entries": {}})
        assert _load_content_provenance(str(tmp_path)) is None

    def test_returns_none_when_entries_missing(self, tmp_path):
        _write_provenance(tmp_path, {"version": 2, "generated_at": "x"})
        assert _load_content_provenance(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# _content_provenance_tab
# ---------------------------------------------------------------------------


class TestContentProvenanceTab:
    def test_renders_placeholder_when_doc_is_none(self):
        html = _content_provenance_tab(None)
        assert "not available" in html
        assert "ships.provenance.json" in html

    def test_renders_table_with_one_row_per_entry(self):
        html = _content_provenance_tab(_v2_doc_two_entries())
        assert "payload/03_ddl/tables/DB.T.tbl" in html
        assert "MyView.viw" in html
        assert "src/legacy/DB.T.sql" in html
        assert "src/views/MyView.sql" in html

    def test_renders_all_four_stage_names(self):
        html = _content_provenance_tab(_v2_doc_two_entries())
        for stage in ("source", "eponymous", "token_resolved", "package"):
            assert stage in html, f"stage {stage!r} missing from rendered tab"

    def test_renders_status_badges_for_each_status(self):
        doc = {
            "version": 2,
            "generated_at": "x",
            "entries": {
                "p": {
                    "stages": [
                        {"stage": "source", "path": "s", "status": "applied"},
                        {
                            "stage": "eponymous",
                            "path": "e",
                            "status": "skipped",
                            "note": "binary file",
                        },
                        {
                            "stage": "token_resolved",
                            "path": "t",
                            "status": "no_op",
                            "note": "no tokens",
                        },
                        {
                            "stage": "package",
                            "path": "p",
                            "status": "failed",
                            "note": "synthetic test",
                        },
                    ]
                }
            },
        }
        html = _content_provenance_tab(doc)
        for s in ("applied", "skipped", "no_op", "failed"):
            assert s in html, f"status {s!r} badge missing from rendered tab"

    def test_uses_viewer_link_when_available(self):
        viewer_links = {
            "payload/03_ddl/tables/DB.T.tbl": ".package_report_code/0001_a3f8b2c19e4d.html"
        }
        html = _content_provenance_tab(_v2_doc_two_entries(), viewer_links=viewer_links)
        assert ".package_report_code/0001_a3f8b2c19e4d.html" in html

    def test_falls_back_to_plain_text_without_viewer_link(self):
        html = _content_provenance_tab(_v2_doc_two_entries(), viewer_links={})
        assert "payload/03_ddl/tables/DB.T.tbl" in html

    def test_renders_summary_with_file_count(self):
        html = _content_provenance_tab(_v2_doc_two_entries())
        assert "2" in html  # two-entry doc — file count should appear
        assert "files" in html

    def test_empty_entries_renders_empty_placeholder(self):
        html = _content_provenance_tab(
            {"version": 2, "generated_at": "x", "entries": {}}
        )
        assert "empty" in html.lower() or "zero entries" in html


# ---------------------------------------------------------------------------
# generate_package_report integration
# ---------------------------------------------------------------------------


class TestGeneratePackageReportContentProvenance:
    def test_tab_present_when_provenance_json_exists(self, tmp_path):
        _write_provenance(tmp_path, _v2_doc_two_entries())
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")

        # Tab id + label
        assert "tab-content-provenance" in html
        assert "Content Provenance" in html
        # An entry from the doc surfaced
        assert "src/legacy/DB.T.sql" in html

    def test_tab_shows_placeholder_when_provenance_json_absent(self, tmp_path):
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")

        assert "Content Provenance" in html
        assert "context/ships.provenance.json" in html

    def test_both_provenance_tabs_coexist(self, tmp_path):
        """Build Provenance and Content Provenance both render — they answer
        different questions (pipeline stages vs per-file source chain)."""
        _write_provenance(tmp_path, _v2_doc_two_entries())
        _make_payload(
            tmp_path,
            [("03_ddl/tables/DB.T.tbl", "CREATE MULTISET TABLE DB.T (Id INTEGER);")],
        )
        generate_package_report(str(tmp_path), _minimal_manifest())
        html = (tmp_path / "package_report.html").read_text(encoding="utf-8")

        assert "tab-provenance" in html
        assert "tab-content-provenance" in html
        assert "Build Provenance" in html
        assert "Content Provenance" in html
