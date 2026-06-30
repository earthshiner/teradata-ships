"""
test_query_band_visibility.py — QueryBand visibility in reports + JSON (#483).

Covers:
- ``query_band.describe_query_band`` — canonical static / dynamic_keys /
  operator_extras / dbql_filter_template shape.
- ``ships.build.json`` carries the ``query_band`` block.
- ``ships.manifest.json`` carries the ``query_band`` block.
- ``reporting.common.render_dbql_lookup_card`` — table rows + SQL snippet.
- Both HTML reports include the DBQL Lookup card.
"""

from __future__ import annotations

import json
from pathlib import Path

from td_release_packager._version import __version__ as SHIPS_VERSION
from td_release_packager.query_band import (
    DYNAMIC_QUERY_BAND_KEYS,
    describe_query_band,
)


# ---------------------------------------------------------------------------
# describe_query_band
# ---------------------------------------------------------------------------


class TestDescribeQueryBand:
    def test_static_keys_from_args(self):
        qb = describe_query_band(
            build_number="0042",
            package_name="MortgagePlatform",
            environment="PROD",
        )
        assert qb["static"] == {
            "BUILD": "0042",
            "PKG": "MortgagePlatform",
            "ENV": "PROD",
        }

    def test_dynamic_keys_are_listed(self):
        qb = describe_query_band(build_number="1", package_name="p", environment="DEV")
        assert qb["dynamic_keys"] == DYNAMIC_QUERY_BAND_KEYS
        assert "PHASE" in qb["dynamic_keys"]
        assert "FILE" in qb["dynamic_keys"]
        assert "STREAM" in qb["dynamic_keys"]
        assert "WAVE" in qb["dynamic_keys"]

    def test_operator_extras_default_to_empty(self):
        qb = describe_query_band(build_number="1", package_name="p", environment="DEV")
        assert qb["operator_extras"] == {}

    def test_operator_extras_threaded_through(self):
        qb = describe_query_band(
            build_number="0042",
            package_name="MortgagePlatform",
            environment="PROD",
            operator_extras={"CHG": "CHG0012345", "TICKET": "INC123"},
        )
        # Sorted into canonical order so the rendered SQL is stable.
        assert list(qb["operator_extras"].keys()) == ["CHG", "TICKET"]
        assert qb["operator_extras"]["CHG"] == "CHG0012345"

    def test_dbql_filter_template_includes_all_static_keys(self):
        qb = describe_query_band(
            build_number="0042",
            package_name="MortgagePlatform",
            environment="PROD",
        )
        template = qb["dbql_filter_template"]
        assert "GetQueryBandValue(QueryBand, 0, 'BUILD') = '0042'" in template
        assert "GetQueryBandValue(QueryBand, 0, 'PKG') = 'MortgagePlatform'" in template
        assert "GetQueryBandValue(QueryBand, 0, 'ENV') = 'PROD'" in template

    def test_dbql_filter_template_includes_operator_extras(self):
        qb = describe_query_band(
            build_number="0042",
            package_name="OMR",
            environment="PROD",
            operator_extras={"CHG": "CHG0012345"},
        )
        assert (
            "GetQueryBandValue(QueryBand, 0, 'CHG') = 'CHG0012345'"
            in (qb["dbql_filter_template"])
        )


# ---------------------------------------------------------------------------
# Embedded in the agentic JSON
# ---------------------------------------------------------------------------


def _make_manifest() -> object:
    from td_release_packager.models import BuildManifest

    return BuildManifest(
        build_number="0042",
        environment="PROD",
        package_name="MortgagePlatform",
        package_filename="PROD_MortgagePlatform_BUILD_0042.zip",
        timestamp="2026-06-29T00:00:00+00:00",
        project_name="MortgagePlatform",
        ships_version=SHIPS_VERSION,
    )


class TestShipsBuildJsonCarriesQueryBand:
    def test_block_present_with_static_keys(self, tmp_path):
        from td_release_packager.builder import _write_manifest_json

        pkg_dir = tmp_path / "pkg"
        (pkg_dir / "context").mkdir(parents=True)
        manifest = _make_manifest()
        _write_manifest_json(str(pkg_dir), manifest)

        data = json.loads(
            (pkg_dir / "context" / "ships.build.json").read_text(encoding="utf-8")
        )
        assert "query_band" in data
        assert data["query_band"]["static"]["BUILD"] == "0042"
        assert data["query_band"]["static"]["PKG"] == "MortgagePlatform"
        assert data["query_band"]["static"]["ENV"] == "PROD"
        assert "BUILD" in data["query_band"]["dbql_filter_template"]


class TestShipsManifestJsonCarriesQueryBand:
    def test_block_present_in_agent_manifest(self, tmp_path):
        from td_release_packager.context_artifacts import write_context_artifacts

        manifest = _make_manifest()
        write_context_artifacts(str(tmp_path), manifest)

        data = json.loads(
            (tmp_path / "context" / "ships.manifest.json").read_text(encoding="utf-8")
        )
        assert "query_band" in data
        assert data["query_band"]["static"]["BUILD"] == "0042"


# ---------------------------------------------------------------------------
# HTML card
# ---------------------------------------------------------------------------


class TestRenderDbqlLookupCard:
    def test_card_contains_static_keys_and_sql(self):
        from td_release_packager.reporting.common import render_dbql_lookup_card

        manifest_dict = {
            "build_number": "0042",
            "package_name": "MortgagePlatform",
            "environment": "PROD",
        }
        html = render_dbql_lookup_card(manifest_dict)

        assert "0042" in html
        assert "MortgagePlatform" in html
        assert "PROD" in html
        assert "DBC.DBQLogTbl" in html
        assert "GetQueryBandValue" in html

    def test_card_lists_dynamic_keys(self):
        from td_release_packager.reporting.common import render_dbql_lookup_card

        html = render_dbql_lookup_card(
            {"build_number": "1", "package_name": "p", "environment": "DEV"}
        )
        for key in ("PHASE", "FILE", "STREAM", "WAVE"):
            assert key in html

    def test_card_renders_operator_extras(self):
        from td_release_packager.reporting.common import render_dbql_lookup_card

        html = render_dbql_lookup_card(
            {"build_number": "1", "package_name": "p", "environment": "DEV"},
            operator_extras={"CHG": "CHG0012345"},
        )
        assert "CHG0012345" in html
        assert "operator extras" in html

    def test_card_escapes_html_in_values(self):
        from td_release_packager.reporting.common import render_dbql_lookup_card

        html = render_dbql_lookup_card(
            {
                "build_number": "1",
                "package_name": "<script>x</script>",
                "environment": "DEV",
            }
        )
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html

    def test_card_title_override(self):
        from td_release_packager.reporting.common import render_dbql_lookup_card

        html = render_dbql_lookup_card(
            {"build_number": "1", "package_name": "p", "environment": "DEV"},
            title="QueryBand used by this deployment",
        )
        assert "QueryBand used by this deployment" in html


# ---------------------------------------------------------------------------
# Package report integration
# ---------------------------------------------------------------------------


class TestPackageReportIncludesCard:
    def test_deploy_tab_contains_dbql_card(self):
        from td_release_packager.package_report import _deploy_tab

        manifest_dict = {
            "build_number": "0042",
            "package_name": "MortgagePlatform",
            "environment": "PROD",
            "package_filename": "PROD_MortgagePlatform_BUILD_0042.zip",
        }
        html = _deploy_tab(manifest_dict)
        assert "Find this in Teradata DBQL" in html
        assert "GetQueryBandValue" in html


# ---------------------------------------------------------------------------
# Shared copyCmd in chrome
# ---------------------------------------------------------------------------


class TestSharedCopyCmd:
    def test_render_page_includes_copy_cmd_script(self):
        from td_release_packager.reporting import common

        html = common.render_page(
            doc_title="t",
            header_title="h",
            tabs=[common.Tab(id="x", label="X", body="<p/>")],
        )
        assert "function copyCmd" in html
        assert "navigator.clipboard.writeText" in html
