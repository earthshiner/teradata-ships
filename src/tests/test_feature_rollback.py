"""
test_feature_rollback.py — Tests for the feature rollback module (issue #37).

Covers:
    - verify_git_tag: valid tag returns commit hash; missing tag raises;
                      git not available raises; timeout raises
    - extract_tagged_source: successful extraction populates dest dir;
                             git archive failure raises; timeout raises
    - build_rollback_package: increments build counter; passes correct
                              commit hash; uses tag in default description;
                              explicit description overrides default;
                              raises on bad tag; counter not incremented
                              when tag check fails before counter write
    - build counter helpers: read/write round-trip; missing file raises
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from td_release_packager.rollback import (
    _read_build_number,
    _write_build_number,
    extract_tagged_source,
    verify_git_tag,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _counter(tmp_path: Path, value: int) -> Path:
    (tmp_path / ".ships").mkdir(parents=True, exist_ok=True)
    p = tmp_path / ".ships" / ".build_counter"
    p.write_text(str(value) + "\n", encoding="utf-8")
    return p


def _fake_run(returncode=0, stdout="", stderr=b""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ---------------------------------------------------------------
# verify_git_tag
# ---------------------------------------------------------------


class TestVerifyGitTag:
    def test_returns_commit_hash_on_success(self, tmp_path):
        commit = "abc1234567890abcdef\n"
        with patch("td_release_packager.rollback.subprocess.run") as mock_run:
            mock_run.return_value = _fake_run(stdout=commit)
            result = verify_git_tag(str(tmp_path), "v1.2.3")
        assert result == commit.strip()

    def test_raises_on_nonzero_returncode(self, tmp_path):
        with patch("td_release_packager.rollback.subprocess.run") as mock_run:
            mock_run.return_value = _fake_run(returncode=128)
            with pytest.raises(ValueError, match="v1.2.3"):
                verify_git_tag(str(tmp_path), "v1.2.3")

    def test_raises_when_git_not_found(self, tmp_path):
        with patch(
            "td_release_packager.rollback.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            with pytest.raises(ValueError, match="git is not available"):
                verify_git_tag(str(tmp_path), "v1.2.3")

    def test_raises_on_timeout(self, tmp_path):
        with patch(
            "td_release_packager.rollback.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 30),
        ):
            with pytest.raises(ValueError, match="timed out"):
                verify_git_tag(str(tmp_path), "v1.2.3")

    def test_uses_refs_tags_prefix_in_command(self, tmp_path):
        with patch("td_release_packager.rollback.subprocess.run") as mock_run:
            mock_run.return_value = _fake_run(stdout="abc123\n")
            verify_git_tag(str(tmp_path), "v1.2.3")
        args = mock_run.call_args[0][0]
        assert "refs/tags/v1.2.3" in args


# ---------------------------------------------------------------
# extract_tagged_source
# ---------------------------------------------------------------


class TestExtractTaggedSource:
    def test_extracts_tar_content(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tf:
            content = b"CREATE TABLE T (id INT);"
            info = tarfile.TarInfo(name="payload/T.tbl")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        tar_bytes = buf.getvalue()

        dest = tmp_path / "extracted"
        dest.mkdir()

        with patch("td_release_packager.rollback.subprocess.run") as mock_run:
            mock_run.return_value = _fake_run(stdout=tar_bytes)
            extract_tagged_source("/fake/repo", "v1.2.3", str(dest))

        assert (dest / "payload" / "T.tbl").exists()

    def test_raises_on_git_archive_failure(self, tmp_path):
        with patch("td_release_packager.rollback.subprocess.run") as mock_run:
            mock_run.return_value = _fake_run(
                returncode=128, stderr=b"fatal: not a git repository"
            )
            with pytest.raises(ValueError, match="git archive failed"):
                extract_tagged_source("/fake/repo", "v1.2.3", str(tmp_path))

    def test_raises_on_timeout(self, tmp_path):
        with patch(
            "td_release_packager.rollback.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 120),
        ):
            with pytest.raises(ValueError, match="timed out"):
                extract_tagged_source("/fake/repo", "v1.2.3", str(tmp_path))


# ---------------------------------------------------------------
# Build counter helpers
# ---------------------------------------------------------------


class TestBuildCounter:
    def test_read_returns_integer(self, tmp_path):
        _counter(tmp_path, 42)
        assert _read_build_number(str(tmp_path)) == 42

    def test_read_raises_when_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _read_build_number(str(tmp_path))

    def test_write_then_read_roundtrip(self, tmp_path):
        _counter(tmp_path, 0)
        _write_build_number(str(tmp_path), 17)
        assert _read_build_number(str(tmp_path)) == 17

    def test_write_overwrites_existing(self, tmp_path):
        _counter(tmp_path, 5)
        _write_build_number(str(tmp_path), 99)
        assert _read_build_number(str(tmp_path)) == 99


# ---------------------------------------------------------------
# build_rollback_package — wiring tests (git + build mocked)
# ---------------------------------------------------------------


class TestBuildRollbackPackage:
    def _setup(self, tmp_path):
        _counter(tmp_path, 10)
        env_conf = tmp_path / "DEV.conf"
        env_conf.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")
        return tmp_path, str(env_conf)

    def _fake_manifest(self):
        m = MagicMock()
        m.build_number = "0011"
        m.source_commit = "abc1234"
        m.warnings = []
        return m

    def test_increments_build_counter(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        with (
            patch("td_release_packager.rollback.verify_git_tag", return_value="abc"),
            patch("td_release_packager.rollback.extract_tagged_source"),
            patch(
                "td_release_packager.rollback.build_package",
                return_value=(("/fake/pkg.zip", self._fake_manifest()), None),
            ),
        ):
            build_rollback_package(
                str(proj), "v1.0.0", "DEV", env_conf, "pkg", str(proj)
            )

        assert _read_build_number(str(proj)) == 11

    def test_passes_commit_hash_to_build_config(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        captured = []

        def capture(config):
            captured.append(config)
            return (("/fake/pkg.zip", self._fake_manifest()), None)

        with (
            patch(
                "td_release_packager.rollback.verify_git_tag", return_value="deadbeef"
            ),
            patch("td_release_packager.rollback.extract_tagged_source"),
            patch("td_release_packager.rollback.build_package", side_effect=capture),
        ):
            build_rollback_package(
                str(proj), "v2.0.0", "DEV", env_conf, "pkg", str(proj)
            )

        assert captured[0].source_commit == "deadbeef"
        assert captured[0].build_number == 11

    def test_default_description_contains_tag(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        captured = []

        def capture(config):
            captured.append(config)
            return (("/fake/pkg.zip", self._fake_manifest()), None)

        with (
            patch("td_release_packager.rollback.verify_git_tag", return_value="abc"),
            patch("td_release_packager.rollback.extract_tagged_source"),
            patch("td_release_packager.rollback.build_package", side_effect=capture),
        ):
            build_rollback_package(
                str(proj), "v3.1.0", "DEV", env_conf, "pkg", str(proj)
            )

        assert "v3.1.0" in captured[0].description

    def test_explicit_description_overrides_default(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        captured = []

        def capture(config):
            captured.append(config)
            return (("/fake/pkg.zip", self._fake_manifest()), None)

        with (
            patch("td_release_packager.rollback.verify_git_tag", return_value="abc"),
            patch("td_release_packager.rollback.extract_tagged_source"),
            patch("td_release_packager.rollback.build_package", side_effect=capture),
        ):
            build_rollback_package(
                str(proj),
                "v1.0.0",
                "DEV",
                env_conf,
                "pkg",
                str(proj),
                description="Emergency rollback — incident #42",
            )

        assert captured[0].description == "Emergency rollback — incident #42"

    def test_raises_when_tag_invalid(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        with patch(
            "td_release_packager.rollback.verify_git_tag",
            side_effect=ValueError("Tag 'bad-tag' does not exist"),
        ):
            with pytest.raises(ValueError, match="bad-tag"):
                build_rollback_package(
                    str(proj), "bad-tag", "DEV", env_conf, "pkg", str(proj)
                )

    def test_counter_not_incremented_when_tag_check_fails(self, tmp_path):
        from td_release_packager.rollback import build_rollback_package

        proj, env_conf = self._setup(tmp_path)
        with patch(
            "td_release_packager.rollback.verify_git_tag",
            side_effect=ValueError("Tag not found"),
        ):
            with pytest.raises(ValueError):
                build_rollback_package(
                    str(proj), "missing", "DEV", env_conf, "pkg", str(proj)
                )

        # verify_git_tag failed before we touched the counter
        assert _read_build_number(str(proj)) == 10
