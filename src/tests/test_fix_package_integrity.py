"""
test_fix_package_integrity.py — Tests for the ``ships fix-package-integrity``
recovery command (#465).

Re-generates ``.sha256`` sidecars across ``<project>/releases/`` so the
sidecar matches the archive's current bytes. Used to repair the legacy
mismatches left by every build produced before #463 landed.
"""

from __future__ import annotations

import hashlib
import os
from argparse import Namespace

import pytest

from td_release_packager.cli import _cmd_fix_package_integrity


def _make_release(
    project,
    group_name: str,
    archive_name: str,
    archive_bytes: bytes,
    sidecar_digest: str | None,
):
    """Create one release-group archive + optional sidecar (idempotent dir)."""
    group_dir = project / "releases" / group_name
    group_dir.mkdir(parents=True, exist_ok=True)
    archive_path = group_dir / archive_name
    archive_path.write_bytes(archive_bytes)
    if sidecar_digest is not None:
        (group_dir / (archive_name + ".sha256")).write_text(
            f"{sidecar_digest}  {archive_name}\n", encoding="utf-8"
        )
    return archive_path


class TestFixPackageIntegrityCommand:
    def test_refreshes_mismatched_sidecar(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        archive = _make_release(
            project,
            "DEV_PKG_BUILD_0001_20260101000000",
            "DEV_PKG_BUILD_0001_20260101000000_02_main.zip",
            b"actual-payload",
            sidecar_digest=hashlib.sha256(b"DIFFERENT-BYTES").hexdigest(),
        )

        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=False))
        capsys.readouterr()
        assert rc == 0

        sidecar_text = (archive.parent / (archive.name + ".sha256")).read_text(
            encoding="utf-8"
        )
        live = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert sidecar_text.split()[0] == live

    def test_leaves_matching_sidecar_alone(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        archive = _make_release(
            project,
            "DEV_PKG_BUILD_0002_20260101000001",
            "DEV_PKG_BUILD_0002_20260101000001_02_main.zip",
            b"clean-payload",
            sidecar_digest=hashlib.sha256(b"clean-payload").hexdigest(),
        )
        sidecar = archive.parent / (archive.name + ".sha256")
        before = sidecar.read_text(encoding="utf-8")

        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=False))
        captured = capsys.readouterr()
        assert rc == 0
        # File untouched.
        assert sidecar.read_text(encoding="utf-8") == before
        # Summary reports the archive as already-consistent.
        assert "1 archive(s) already consistent" in captured.out

    def test_creates_sidecar_when_missing(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        archive = _make_release(
            project,
            "DEV_PKG_BUILD_0003_20260101000002",
            "DEV_PKG_BUILD_0003_20260101000002_02_main.zip",
            b"some-bytes",
            sidecar_digest=None,
        )

        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=False))
        captured = capsys.readouterr()
        assert rc == 0

        sidecar = archive.parent / (archive.name + ".sha256")
        assert sidecar.is_file()
        assert (
            sidecar.read_text(encoding="utf-8").split()[0]
            == hashlib.sha256(b"some-bytes").hexdigest()
        )
        assert "newly created" in captured.out

    def test_dry_run_does_not_write(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        stale_digest = hashlib.sha256(b"OTHER").hexdigest()
        archive = _make_release(
            project,
            "DEV_PKG_BUILD_0004_20260101000003",
            "DEV_PKG_BUILD_0004_20260101000003_02_main.zip",
            b"real-bytes",
            sidecar_digest=stale_digest,
        )
        sidecar = archive.parent / (archive.name + ".sha256")
        before = sidecar.read_text(encoding="utf-8")

        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=True))
        captured = capsys.readouterr()
        assert rc == 0
        # File untouched.
        assert sidecar.read_text(encoding="utf-8") == before
        assert "would refresh sidecar" in captured.out
        assert "Re-run without --dry-run" in captured.out

    def test_no_releases_dir_is_no_op(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=False))
        captured = capsys.readouterr()
        assert rc == 0
        assert "nothing to fix" in captured.out

    def test_handles_multiple_release_groups(self, tmp_path, capsys):
        project = tmp_path / "proj"
        project.mkdir()
        # Two release groups, each with prereqs + main; all sidecars stale.
        for build in ("BUILD_0001_20260101000000", "BUILD_0002_20260101000001"):
            for role in ("01_prereqs", "02_main"):
                _make_release(
                    project,
                    f"DEV_PKG_{build}",
                    f"DEV_PKG_{build}_{role}.zip",
                    f"{build}-{role}".encode(),
                    sidecar_digest=hashlib.sha256(b"STALE").hexdigest(),
                )

        rc = _cmd_fix_package_integrity(Namespace(project=str(project), dry_run=False))
        captured = capsys.readouterr()
        assert rc == 0
        assert "4 " in captured.out  # 4 archives refreshed

        # Every sidecar now matches its archive.
        for group in (project / "releases").iterdir():
            for archive in group.glob("*.zip"):
                sidecar = group / (archive.name + ".sha256")
                live = hashlib.sha256(archive.read_bytes()).hexdigest()
                assert sidecar.read_text(encoding="utf-8").split()[0] == live, (
                    f"{archive.name} sidecar still wrong"
                )
