"""
test_launchers_and_favicons.py — Navigator launcher names + report favicons (#485).

Covers:
- Navigator's ``safeLauncherStem`` JS helper is wired into every
  launcher-emitting site in ``tools/navigator/ships-navigator.html``
  (no remaining hardcoded ``ships-run.ps1`` / ``.sh`` / ``.bat``).
- ``reporting.common.favicon_data_uri`` returns a distinct data URI
  per report kind, falls back to ``"package"`` for unknown kinds.
- ``render_page()`` injects a ``<link rel="icon">`` tag whose payload
  matches the chosen ``favicon_kind``.
- Pipeline / Package / Deploy callers pass the correct ``favicon_kind``.
- Navigator HTML carries its own orange ``N`` favicon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
NAVIGATOR_HTML = REPO_ROOT / "tools" / "navigator" / "ships-navigator.html"


# ---------------------------------------------------------------------------
# Navigator launcher names
# ---------------------------------------------------------------------------


class TestNavigatorLauncherNames:
    @pytest.fixture(scope="class")
    def html(self) -> str:
        return NAVIGATOR_HTML.read_text(encoding="utf-8")

    def test_safe_launcher_stem_helper_defined(self, html):
        assert "function safeLauncherStem" in html

    def test_no_executable_hardcoded_ships_run_filenames(self, html):
        # All references to literal `ships-run.ps1` / `.sh` / `.bat`
        # have been replaced with the dynamic stem. The lone surviving
        # mention is a comment block explaining the rename.
        for fname in ("ships-run.ps1", "ships-run.sh", "ships-run.bat"):
            hits = [
                line
                for line in html.splitlines()
                if fname in line and not line.lstrip().startswith(("//", "/*", "*"))
            ]
            assert hits == [], f"Found non-comment occurrences of {fname}: {hits}"

    def test_block_calls_use_launcher_stem(self, html):
        # Each of the six block() filename slots is built from
        # launcherStem rather than a string literal.
        block_filename_args = re.findall(r"block\([^,]+,\s*([^,]+),\s*\w+Text", html)
        assert len(block_filename_args) >= 6, "Expected at least 6 block() calls"
        for arg in block_filename_args:
            assert "launcherStem" in arg, (
                f"block() filename arg should reference launcherStem, got: {arg}"
            )

    def test_download_bundle_uses_launcher_stem(self, html):
        # downloadAllBundle composes file names from launcherStem now.
        bundle_push = re.findall(r"files\.push\(\{\s*name:\s*([^,]+),", html)
        # First three pushes are the .ps1 / .sh / .bat trio.
        first_three = bundle_push[:3]
        for arg in first_three:
            assert "launcherStem" in arg, (
                f"downloadAllBundle file name should reference launcherStem, got: {arg}"
            )


# ---------------------------------------------------------------------------
# favicon_data_uri
# ---------------------------------------------------------------------------


class TestFaviconDataUri:
    def test_returns_data_uri_with_svg(self):
        from td_release_packager.reporting.common import favicon_data_uri

        uri = favicon_data_uri("navigator")
        assert uri.startswith("data:image/svg+xml;utf8,")
        assert "<svg" in uri
        assert "<circle" in uri

    def test_navigator_uses_orange(self):
        from td_release_packager.reporting.common import favicon_data_uri

        uri = favicon_data_uri("navigator")
        # ORANGE = "#FF5F02" → percent-encoded as %23FF5F02 in data URI.
        assert "%23FF5F02" in uri
        assert ">N<" in uri

    def test_each_kind_has_distinct_payload(self):
        from td_release_packager.reporting.common import (
            FAVICON_KINDS,
            favicon_data_uri,
        )

        uris = {kind: favicon_data_uri(kind) for kind in FAVICON_KINDS}
        # Every variant should produce a unique payload — colour OR
        # letter changes for each kind.
        assert len(set(uris.values())) == len(FAVICON_KINDS)

    def test_unknown_kind_falls_back_to_package(self):
        from td_release_packager.reporting.common import favicon_data_uri

        unknown = favicon_data_uri("not-a-real-kind")
        pkg = favicon_data_uri("package")
        assert unknown == pkg

    def test_deploy_mode_variants_differ_from_each_other(self):
        from td_release_packager.reporting.common import favicon_data_uri

        variants = {
            kind: favicon_data_uri(kind)
            for kind in ("deployment", "dry_run", "explain", "replay")
        }
        # No two deploy-mode variants share a payload — that's the whole
        # point of giving each mode its own favicon.
        assert len(set(variants.values())) == 4


# ---------------------------------------------------------------------------
# render_page injection
# ---------------------------------------------------------------------------


class TestRenderPageEmitsFavicon:
    def _render(self, **overrides) -> str:
        from td_release_packager.reporting import common

        kwargs = {
            "doc_title": "t",
            "header_title": "h",
            "tabs": [common.Tab(id="x", label="X", body="<p>body</p>")],
        }
        kwargs.update(overrides)
        return common.render_page(**kwargs)

    def test_link_icon_tag_present(self):
        html = self._render()
        assert '<link rel="icon" type="image/svg+xml"' in html

    def test_default_kind_is_package(self):
        from td_release_packager.reporting.common import favicon_data_uri

        html = self._render()
        assert favicon_data_uri("package") in html

    def test_favicon_kind_kwarg_changes_payload(self):
        from td_release_packager.reporting.common import favicon_data_uri

        html = self._render(favicon_kind="pipeline")
        assert favicon_data_uri("pipeline") in html
        assert favicon_data_uri("package") not in html


# ---------------------------------------------------------------------------
# Callers wire the right kind
# ---------------------------------------------------------------------------


class TestCallersPassCorrectKind:
    def test_package_report_passes_package(self):
        src = (
            REPO_ROOT / "src" / "td_release_packager" / "package_report.py"
        ).read_text(encoding="utf-8")
        assert 'favicon_kind="package"' in src

    def test_pipeline_report_passes_pipeline(self):
        src = (
            REPO_ROOT
            / "src"
            / "td_release_packager"
            / "reporting"
            / "pipeline_report.py"
        ).read_text(encoding="utf-8")
        assert 'favicon_kind="pipeline"' in src

    def test_deploy_report_maps_modes_to_kinds(self):
        src = (REPO_ROOT / "src" / "database_package_deployer" / "report.py").read_text(
            encoding="utf-8"
        )
        # The deploy report derives favicon_kind from the run mode.
        for token in ('"dry_run"', '"explain"', '"replay"', '"deployment"'):
            assert token in src, f"deploy report should mention favicon kind {token}"


# ---------------------------------------------------------------------------
# Navigator HTML favicon
# ---------------------------------------------------------------------------


class TestNavigatorFavicon:
    def test_link_icon_tag_present(self):
        html = NAVIGATOR_HTML.read_text(encoding="utf-8")
        assert '<link rel="icon" type="image/svg+xml"' in html

    def test_navigator_favicon_uses_orange_n(self):
        html = NAVIGATOR_HTML.read_text(encoding="utf-8")
        # The Navigator gets the orange-N variant — the same colour the
        # Python helper produces, kept in sync by hand because the
        # Navigator is pure HTML/JS with no Python at render time.
        assert "%23FF5F02" in html
        assert ">N<" in html
