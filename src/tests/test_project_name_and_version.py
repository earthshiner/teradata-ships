"""
test_project_name_and_version.py — Project name + SHIPS version provenance (#481).

Covers:
- ``project_paths.resolve_project_name`` — ships.yaml lookup with dir-name fallback.
- ``reporting.common.render_page`` — orange ribbon markup when project_name is set.
- Agentic JSON writers (decisions / context / manifest / handoff / index / trust /
  actions / capabilities / integrity) — every artefact carries ``ships_version``
  and ``project_name`` at the root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from td_release_packager._version import __version__ as SHIPS_VERSION
from td_release_packager.project_paths import (
    resolve_project_name,
    ships_yaml_path,
)


# ---------------------------------------------------------------------------
# resolve_project_name
# ---------------------------------------------------------------------------


class TestResolveProjectName:
    def test_reads_project_field_from_ships_yaml(self, tmp_path):
        project = tmp_path / "anywhere_on_disk"
        project.mkdir()
        (project / "ships.yaml").write_text(
            "project: CustomerDNA\nversion: '1.0'\n",
            encoding="utf-8",
        )
        assert resolve_project_name(str(project)) == "CustomerDNA"

    def test_falls_back_to_directory_basename_when_yaml_missing(self, tmp_path):
        project = tmp_path / "CallCentre"
        project.mkdir()
        # No ships.yaml present.
        assert resolve_project_name(str(project)) == "CallCentre"

    def test_falls_back_when_ships_yaml_has_no_project_field(self, tmp_path):
        project = tmp_path / "MortgagePlatform"
        project.mkdir()
        (project / "ships.yaml").write_text(
            "version: '1.0'\nenvironments: [DEV]\n",
            encoding="utf-8",
        )
        assert resolve_project_name(str(project)) == "MortgagePlatform"

    def test_strips_quotes_around_yaml_value(self, tmp_path):
        project = tmp_path / "x"
        project.mkdir()
        (project / "ships.yaml").write_text(
            'project: "QuotedName"\n',
            encoding="utf-8",
        )
        assert resolve_project_name(str(project)) == "QuotedName"

    def test_ignores_commented_project_lines(self, tmp_path):
        project = tmp_path / "RealName"
        project.mkdir()
        (project / "ships.yaml").write_text(
            "# project: WrongName\nversion: '1.0'\n",
            encoding="utf-8",
        )
        assert resolve_project_name(str(project)) == "RealName"

    def test_unreadable_yaml_falls_back_to_basename(self, tmp_path):
        project = tmp_path / "FallbackName"
        project.mkdir()
        # Create as a directory rather than a file so open() fails.
        os.mkdir(ships_yaml_path(str(project)))
        assert resolve_project_name(str(project)) == "FallbackName"


# ---------------------------------------------------------------------------
# HTML ribbon
# ---------------------------------------------------------------------------


class TestProjectRibbon:
    def _render(self, **overrides) -> str:
        from td_release_packager.reporting import common

        kwargs = {
            "doc_title": "t",
            "header_title": "Header",
            "tabs": [common.Tab(id="x", label="X", body="<p>body</p>")],
        }
        kwargs.update(overrides)
        return common.render_page(**kwargs)

    def test_ribbon_absent_when_no_project_name(self):
        # The CSS class definition is always in <style>, but the ribbon
        # <div> only renders when project_name is set.
        html = self._render()
        assert '<div class="project-ribbon">' not in html

    def test_ribbon_shows_project_and_default_version(self):
        html = self._render(project_name="CustomerDNA")
        assert '<div class="project-ribbon">' in html
        assert "CustomerDNA" in html
        assert f"SHIPS v{SHIPS_VERSION}" in html

    def test_explicit_ships_version_overrides_default(self):
        html = self._render(project_name="CallCentre", ships_version="9.9.9")
        assert "CallCentre" in html
        assert "SHIPS v9.9.9" in html
        assert f"SHIPS v{SHIPS_VERSION}" not in html

    def test_ribbon_html_is_escaped(self):
        html = self._render(project_name="<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# Agentic JSON writers
# ---------------------------------------------------------------------------


def _make_manifest(project_name: str = "TestProj") -> object:
    from td_release_packager.models import BuildManifest

    return BuildManifest(
        build_number="0001",
        environment="DEV",
        package_name="pkg",
        package_filename="DEV_pkg_BUILD_0001.zip",
        timestamp="2026-06-29T00:00:00+00:00",
        project_name=project_name,
        ships_version=SHIPS_VERSION,
    )


class TestDecisionsManifestProvenance:
    def test_fresh_manifest_stamps_both_fields(self, tmp_path):
        from td_release_packager.orchestrator.decisions import DecisionsManifest

        project = tmp_path / "MyProj"
        project.mkdir()
        (project / "ships.yaml").write_text(
            "project: ResolvedFromYaml\n", encoding="utf-8"
        )

        state_dir = project / ".ships"
        state_dir.mkdir()
        decisions_path = state_dir / "ships.decisions.json"

        manifest = DecisionsManifest(str(decisions_path))
        manifest.save()

        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        assert data["ships_version"] == SHIPS_VERSION
        assert data["project_name"] == "ResolvedFromYaml"

    def test_existing_manifest_gets_provenance_stamped_on_load(self, tmp_path):
        from td_release_packager.orchestrator.decisions import DecisionsManifest

        project = tmp_path / "OldProj"
        project.mkdir()
        state_dir = project / ".ships"
        state_dir.mkdir()
        decisions_path = state_dir / "ships.decisions.json"
        decisions_path.write_text(
            json.dumps({"schema_version": 1, "project": {}, "runs": []}),
            encoding="utf-8",
        )

        manifest = DecisionsManifest(str(decisions_path))
        manifest.save()

        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        assert data["ships_version"] == SHIPS_VERSION
        assert data["project_name"] == "OldProj"


class TestContextArtefactsProvenance:
    def test_every_context_document_carries_both_fields(self, tmp_path):
        from td_release_packager.context_artifacts import write_context_artifacts

        manifest = _make_manifest("CustomerDNA")
        write_context_artifacts(str(tmp_path), manifest)

        for filename in (
            "ships.context.json",
            "ships.manifest.json",
            "ships.handoff.json",
            "ships.index.json",
        ):
            data = json.loads(
                (tmp_path / "context" / filename).read_text(encoding="utf-8")
            )
            assert data["ships_version"] == SHIPS_VERSION, filename
            assert data["project_name"] == "CustomerDNA", filename


class TestTrustResultProvenance:
    def test_write_trust_result_stamps_both_fields(self, tmp_path):
        from datetime import datetime, timezone

        from td_release_packager.trust import (
            STATUS_READY,
            TrustReport,
            write_trust_result,
        )

        report = TrustReport(
            status=STATUS_READY,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )
        path = write_trust_result(
            str(tmp_path),
            report,
            project_name="CallCentre",
            ships_version="2.0.0",
        )

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["ships_version"] == "2.0.0"
        assert data["project_name"] == "CallCentre"

    def test_write_trust_result_defaults_version_to_module_version(self, tmp_path):
        from datetime import datetime, timezone

        from td_release_packager.trust import (
            STATUS_READY,
            TrustReport,
            write_trust_result,
        )

        report = TrustReport(
            status=STATUS_READY,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )
        path = write_trust_result(str(tmp_path), report, project_name="P")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["ships_version"] == SHIPS_VERSION


class TestActionsResultProvenance:
    def test_write_actions_result_stamps_both_fields(self, tmp_path):
        from td_release_packager.actions import (
            compute_actions_report,
            write_actions_result,
        )

        report = compute_actions_report(
            trust={
                "status": "READY",
                "deploy_allowed": True,
                "override_allowed": False,
                "blocking_signals": [],
                "warning_signals": [],
            },
            role="single",
        )
        path = write_actions_result(
            str(tmp_path),
            report,
            project_name="MortgagePlatform",
        )

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["ships_version"] == SHIPS_VERSION
        assert data["project_name"] == "MortgagePlatform"


class TestCapabilitiesResultProvenance:
    def test_write_capabilities_result_stamps_both_fields(self, tmp_path):
        from td_release_packager.capabilities import (
            CapabilitiesReport,
            write_capabilities_result,
        )

        report = CapabilitiesReport(evaluated_at="2026-06-29T00:00:00+00:00")
        path = write_capabilities_result(
            str(tmp_path),
            report,
            project_name="CustomerDNA",
        )

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["ships_version"] == SHIPS_VERSION
        assert data["project_name"] == "CustomerDNA"


class TestBuildManifestFields:
    def test_defaults_are_empty_strings(self):
        manifest = _make_manifest(project_name="")
        assert manifest.project_name == ""
        # The fixture sets ships_version to the current version, but the
        # dataclass default is "".
        from td_release_packager.models import BuildManifest

        minimal = BuildManifest(
            build_number="1",
            environment="DEV",
            package_name="p",
            package_filename="p.zip",
            timestamp="2026-06-29T00:00:00+00:00",
        )
        assert minimal.project_name == ""
        assert minimal.ships_version == ""
