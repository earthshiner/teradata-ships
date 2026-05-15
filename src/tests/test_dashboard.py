"""
test_dashboard.py — Tests for the SHIPS Deployment Dashboard data layer.

Tests cover the data extraction and business logic functions.
HTTP routes are thin wrappers around these functions; testing the
data layer is sufficient for correctness coverage.

Covers:
    - read_build_json_from_zip: valid archive, missing ships.build.json, bad archive
    - archive_has_report: with and without package_report.html
    - check_approval: no sidecar, approved, rejected with reason
    - write_approval: approve writes .approved; reject writes .rejected;
                      toggle clears the old sidecar
    - scan_project: empty releases/, no releases dir, single archive,
                    grouped archives, multiple archives, ignores non-archives
    - scan_all_projects: missing dir warns and skips; multiple projects
    - generate_dbql_query: contains build number, package name, DBQL tables
    - PackageInfo.approval_status: Awaiting / Approved / Rejected
    - PackageInfo.archive_stem: strips .zip and .tar.gz
    - HTTP routes: index renders package table; detail page renders trust;
                   approve redirects; reject form and submit round-trip
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ships_dashboard import (
    PackageInfo,
    archive_has_report,
    check_approval,
    generate_dbql_query,
    read_build_json_from_zip,
    scan_all_projects,
    scan_project,
    write_approval,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_zip(tmp_path: Path, filename: str, members: dict[str, str]) -> str:
    """Write a zip archive with given {member_name: content} pairs."""
    archive_path = str(tmp_path / filename)
    with zipfile.ZipFile(archive_path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return archive_path


def _build_json(pkg_name="OMR", build_no="0042", env="PRD", label="READY") -> dict:
    return {
        "package_name": pkg_name,
        "build_number": build_no,
        "environment": env,
        "timestamp": "2026-05-10T12:00:00+00:00",
        "author": "tester",
        "description": "test build",
        "trust": {
            "label": label,
            "signals": {
                "inspect_lint": {"status": "pass", "detail": ""},
                "inspect_token_format": {"status": "pass", "detail": ""},
            },
        },
        "file_count": 10,
        "requires": [],
        "source_commit": "abc1234",
        "source_dirty": False,
    }


def _make_package_zip(tmp_path: Path, filename: str, **build_overrides) -> str:
    """Write a realistic package archive with ships.build.json (and optional report)."""
    bd = _build_json(**build_overrides)
    members = {f"{filename.split('.')[0]}/ships.build.json": json.dumps(bd)}
    if build_overrides.get("with_report"):
        members[f"{filename.split('.')[0]}/package_report.html"] = "<html></html>"
    return _make_zip(tmp_path, filename, members)


def _make_project(tmp_path: Path, archives: list[tuple[str, dict]]) -> Path:
    """Create a project structure with releases/ and given archives."""
    project = tmp_path / "project"
    releases = project / "releases"
    releases.mkdir(parents=True)
    for filename, build_kwargs in archives:
        bd = _build_json(**build_kwargs)
        members = {f"{Path(filename).stem}/context/ships.build.json": json.dumps(bd)}
        archive_path = releases / filename
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
    return project


# ---------------------------------------------------------------
# read_build_json_from_zip
# ---------------------------------------------------------------


class TestReadBuildJsonFromZip:
    def test_reads_build_json(self, tmp_path):
        bd = _build_json()
        archive = _make_zip(
            tmp_path, "pkg.zip", {"pkg/ships.build.json": json.dumps(bd)}
        )
        result = read_build_json_from_zip(archive)
        assert result is not None
        assert result["package_name"] == "OMR"
        assert result["build_number"] == "0042"

    def test_returns_none_when_no_build_json(self, tmp_path):
        archive = _make_zip(tmp_path, "pkg.zip", {"pkg/other.txt": "hello"})
        assert read_build_json_from_zip(archive) is None

    def test_returns_none_for_nonexistent_file(self, tmp_path):
        assert read_build_json_from_zip(str(tmp_path / "missing.zip")) is None

    def test_returns_none_for_bad_zip(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_text("not a zip file", encoding="utf-8")
        assert read_build_json_from_zip(str(bad)) is None


# ---------------------------------------------------------------
# archive_has_report
# ---------------------------------------------------------------


class TestArchiveHasReport:
    def test_returns_true_when_report_present(self, tmp_path):
        archive = _make_zip(
            tmp_path,
            "pkg.zip",
            {"pkg/package_report.html": "<html></html>", "pkg/ships.build.json": "{}"},
        )
        assert archive_has_report(archive) is True

    def test_returns_false_when_no_report(self, tmp_path):
        archive = _make_zip(tmp_path, "pkg.zip", {"pkg/ships.build.json": "{}"})
        assert archive_has_report(archive) is False

    def test_returns_false_for_nonexistent_file(self, tmp_path):
        assert archive_has_report(str(tmp_path / "missing.zip")) is False


# ---------------------------------------------------------------
# check_approval
# ---------------------------------------------------------------


class TestCheckApproval:
    def test_no_sidecar_returns_awaiting(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        approved, rejected, reason = check_approval(archive)
        assert not approved and not rejected and reason == ""

    def test_approved_sidecar(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        (tmp_path / "pkg.zip.approved").write_text("", encoding="utf-8")
        approved, rejected, reason = check_approval(archive)
        assert approved and not rejected

    def test_rejected_sidecar_with_reason(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        (tmp_path / "pkg.zip.rejected").write_text(
            "Wrong environment", encoding="utf-8"
        )
        approved, rejected, reason = check_approval(archive)
        assert not approved and rejected
        assert reason == "Wrong environment"


# ---------------------------------------------------------------
# write_approval
# ---------------------------------------------------------------


class TestWriteApproval:
    def test_approve_writes_approved_sidecar(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        write_approval(archive, approved=True)
        assert (tmp_path / "pkg.zip.approved").exists()
        assert not (tmp_path / "pkg.zip.rejected").exists()

    def test_reject_writes_rejected_sidecar(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        write_approval(archive, approved=False, reason="Too risky")
        assert (tmp_path / "pkg.zip.rejected").read_text(
            encoding="utf-8"
        ) == "Too risky"
        assert not (tmp_path / "pkg.zip.approved").exists()

    def test_approve_clears_rejected_sidecar(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        (tmp_path / "pkg.zip.rejected").write_text("old reason", encoding="utf-8")
        write_approval(archive, approved=True)
        assert not (tmp_path / "pkg.zip.rejected").exists()
        assert (tmp_path / "pkg.zip.approved").exists()

    def test_reject_clears_approved_sidecar(self, tmp_path):
        archive = str(tmp_path / "pkg.zip")
        (tmp_path / "pkg.zip.approved").write_text("", encoding="utf-8")
        write_approval(archive, approved=False, reason="changed mind")
        assert not (tmp_path / "pkg.zip.approved").exists()
        assert (tmp_path / "pkg.zip.rejected").exists()


# ---------------------------------------------------------------
# scan_project
# ---------------------------------------------------------------


class TestScanProject:
    def test_no_releases_dir_returns_empty(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        assert scan_project(str(project)) == []

    def test_empty_releases_dir_returns_empty(self, tmp_path):
        project = tmp_path / "proj"
        (project / "releases").mkdir(parents=True)
        assert scan_project(str(project)) == []

    def test_single_archive_returned(self, tmp_path):
        project = _make_project(tmp_path, [("OMR_DEV_0042.zip", {})])
        results = scan_project(str(project))
        assert len(results) == 1
        assert results[0].archive_filename == "OMR_DEV_0042.zip"

    def test_package_fields_populated(self, tmp_path):
        project = _make_project(
            tmp_path,
            [
                (
                    "OMR_PRD_0042.zip",
                    {"pkg_name": "OMR", "build_no": "0042", "env": "PRD"},
                )
            ],
        )
        results = scan_project(str(project))
        pkg = results[0]
        assert pkg.package_name == "OMR"
        assert pkg.build_number == "0042"
        assert pkg.environment == "PRD"
        assert pkg.trust_label == "READY"

    def test_non_archive_files_ignored(self, tmp_path):
        project = tmp_path / "proj"
        (project / "releases").mkdir(parents=True)
        (project / "releases" / "README.txt").write_text("ignore me", encoding="utf-8")
        assert scan_project(str(project)) == []

    def test_multiple_archives_returned(self, tmp_path):
        project = _make_project(
            tmp_path,
            [
                ("OMR_DEV_0041.zip", {"build_no": "0041"}),
                ("OMR_DEV_0042.zip", {"build_no": "0042"}),
            ],
        )
        results = scan_project(str(project))
        assert len(results) == 2

    def test_grouped_release_archives_are_discovered_recursively(self, tmp_path):
        project = _make_project(
            tmp_path,
            [
                (
                    "DEV_GCFR_BUILD_0012_20260515144900/DEV_GCFR_BUILD_0012_20260515144900_01_main.zip",
                    {"pkg_name": "GCFR", "build_no": "0012", "env": "DEV"},
                )
            ],
        )
        results = scan_project(str(project))
        assert len(results) == 1
        assert (
            results[0].archive_filename
            == "DEV_GCFR_BUILD_0012_20260515144900_01_main.zip"
        )
        assert results[0].package_name == "GCFR"

    def test_approved_sidecar_detected(self, tmp_path):
        project = _make_project(tmp_path, [("pkg.zip", {})])
        archive_path = str(project / "releases" / "pkg.zip")
        (Path(archive_path + ".approved")).write_text("", encoding="utf-8")
        results = scan_project(str(project))
        assert results[0].approved is True
        assert results[0].approval_status == "Approved"

    def test_archive_stem_strips_zip(self, tmp_path):
        project = _make_project(tmp_path, [("OMR_DEV_BUILD_0042.zip", {})])
        results = scan_project(str(project))
        assert results[0].archive_stem == "OMR_DEV_BUILD_0042"


# ---------------------------------------------------------------
# scan_all_projects
# ---------------------------------------------------------------


class TestScanAllProjects:
    def test_missing_project_dir_skipped(self, tmp_path):
        result = scan_all_projects([str(tmp_path / "nonexistent")])
        assert result == []

    def test_multiple_projects_combined(self, tmp_path):
        p1 = _make_project(tmp_path / "proj1", [("A.zip", {"pkg_name": "A"})])
        p2 = _make_project(tmp_path / "proj2", [("B.zip", {"pkg_name": "B"})])
        results = scan_all_projects([str(p1), str(p2)])
        names = {r.package_name for r in results}
        assert "A" in names and "B" in names


# ---------------------------------------------------------------
# generate_dbql_query
# ---------------------------------------------------------------


class TestGenerateDbqlQuery:
    def test_contains_build_number(self):
        q = generate_dbql_query("OMR", "0042")
        assert "0042" in q

    def test_contains_package_name(self):
        q = generate_dbql_query("OMR", "0042")
        assert "OMR" in q

    def test_references_dbql_table(self):
        q = generate_dbql_query("OMR", "0042")
        assert "DBC.DBQLogTbl" in q

    def test_uses_get_query_band_value(self):
        q = generate_dbql_query("OMR", "0042")
        assert "GetQueryBandValue" in q


# ---------------------------------------------------------------
# PackageInfo properties
# ---------------------------------------------------------------


class TestPackageInfoProperties:
    def _pkg(self, **kwargs):
        defaults = dict(
            project_name="proj",
            project_dir="/proj",
            archive_path="/proj/releases/pkg.zip",
            archive_filename="pkg.zip",
            package_name="OMR",
            build_number="0042",
            environment="DEV",
            timestamp="",
            author="",
            description="",
            trust_label="READY",
            trust_signals={},
            file_count=0,
            requires=[],
            source_commit="",
            source_dirty=False,
            approved=False,
            rejected=False,
            rejection_reason="",
            has_report=False,
        )
        defaults.update(kwargs)
        return PackageInfo(**defaults)

    def test_approval_status_awaiting(self):
        assert self._pkg().approval_status == "Awaiting"

    def test_approval_status_approved(self):
        assert self._pkg(approved=True).approval_status == "Approved"

    def test_approval_status_rejected(self):
        assert self._pkg(rejected=True).approval_status == "Rejected"

    def test_archive_stem_strips_zip(self):
        p = self._pkg(archive_filename="OMR_DEV_BUILD_0042_20260510.zip")
        assert p.archive_stem == "OMR_DEV_BUILD_0042_20260510"

    def test_archive_stem_strips_tar_gz(self):
        p = self._pkg(archive_filename="OMR_DEV_0042.tar.gz")
        assert p.archive_stem == "OMR_DEV_0042"


# ---------------------------------------------------------------
# HTTP routes — smoke tests via FastAPI TestClient
# ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    """TestClient pointed at a project with one READY and one BLOCKED package."""
    pytest.importorskip(
        "fastapi",
        reason="fastapi not installed — install with uv pip install -e '.[dashboard]'",
    )
    from fastapi.testclient import TestClient
    from ships_dashboard import create_app

    project = _make_project(
        tmp_path,
        [
            (
                "OMR_PRD_0042.zip",
                {"pkg_name": "OMR", "build_no": "0042", "env": "PRD", "label": "READY"},
            ),
            (
                "GCFR_DEV_0007.zip",
                {
                    "pkg_name": "GCFR",
                    "build_no": "0007",
                    "env": "DEV",
                    "label": "BLOCKED",
                },
            ),
        ],
    )
    app = create_app([str(project)])
    return TestClient(app, raise_server_exceptions=True)


class TestHttpRoutes:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_contains_package_names(self, client):
        resp = client.get("/")
        assert "OMR" in resp.text
        assert "GCFR" in resp.text

    def test_index_contains_trust_labels(self, client):
        resp = client.get("/")
        assert "READY" in resp.text
        assert "BLOCKED" in resp.text

    def test_detail_returns_200(self, client):
        resp = client.get("/package/OMR_PRD_0042")
        assert resp.status_code == 200

    def test_detail_contains_build_number(self, client):
        resp = client.get("/package/OMR_PRD_0042")
        assert "0042" in resp.text

    def test_detail_contains_trust_signals(self, client):
        resp = client.get("/package/OMR_PRD_0042")
        assert "inspect_lint" in resp.text

    def test_detail_404_for_unknown_stem(self, client):
        resp = client.get("/package/nonexistent_0000")
        assert resp.status_code == 404

    def test_approve_redirects(self, client):
        resp = client.get("/approve/OMR_PRD_0042", follow_redirects=False)
        assert resp.status_code == 303

    def test_approve_writes_sidecar(self, client, tmp_path):
        client.get("/approve/OMR_PRD_0042")
        sidecar = tmp_path / "project" / "releases" / "OMR_PRD_0042.zip.approved"
        assert sidecar.exists()

    def test_reject_form_returns_200(self, client):
        resp = client.get("/reject-form/OMR_PRD_0042")
        assert resp.status_code == 200
        assert "Reason" in resp.text

    def test_reject_submit_writes_sidecar(self, client, tmp_path):
        client.post(
            "/reject-submit/OMR_PRD_0042",
            data={"reason": "Too risky"},
        )
        sidecar = tmp_path / "project" / "releases" / "OMR_PRD_0042.zip.rejected"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == "Too risky"

    def test_api_packages_returns_list(self, client):
        resp = client.get("/api/packages")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_api_packages_fields(self, client):
        data = client.get("/api/packages").json()
        pkg = next(p for p in data if p["package_name"] == "OMR")
        assert pkg["build_number"] == "0042"
        assert pkg["trust_label"] == "READY"

    def test_api_build_json_returns_dict(self, client):
        resp = client.get("/api/package/OMR_PRD_0042/build_json")
        assert resp.status_code == 200
        assert resp.json()["package_name"] == "OMR"

    def test_api_dbql_query_contains_sql(self, client):
        resp = client.get("/api/package/OMR_PRD_0042/dbql_query")
        assert resp.status_code == 200
        q = resp.json()["query"]
        assert "DBC.DBQLogTbl" in q
        assert "0042" in q
