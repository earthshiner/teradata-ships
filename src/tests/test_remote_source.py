"""
test_remote_source.py — Tests for the GitHub tarball source fetcher.

Covers:
    - resolve_ref: success; HTTP 404; network error
    - fetch_github_source: full download + extraction; root dir stripped;
                           security skips absolute paths and .. traversal;
                           auth header set when token provided;
                           falls back to GITHUB_TOKEN env var
    - _extract_strip_root: members correctly stripped; root dir skipped;
                            content written to dest
    - _resolve_github_source CLI helper: sets args.source from GitHub;
                                         mutual exclusion with --source;
                                         bad owner/repo format rejected;
                                         resolves commit SHA into args.commit
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from td_release_packager.remote_source import (
    _extract_strip_root,
    fetch_github_source,
    resolve_ref,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_tarball(members: dict[str, str], root: str = "owner-repo-abc1234") -> bytes:
    """Build an in-memory .tar.gz with a root directory matching GitHub format."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Add root dir entry
        info = tarfile.TarInfo(name=root)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        # Add members under the root
        for name, content in members.items():
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{root}/{name}")
            info.size = len(encoded)
            tf.addfile(info, io.BytesIO(encoded))
    return buf.getvalue()


def _fake_response(body: bytes, status: int = 200):
    """Return a mock context manager that yields a response-like object."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------
# resolve_ref
# ---------------------------------------------------------------


class TestResolveRef:
    def test_returns_sha_on_success(self, tmp_path):
        body = json.dumps({"sha": "abc1234567890abcdef"}).encode()
        with patch("urllib.request.urlopen", return_value=_fake_response(body)):
            sha = resolve_ref("myorg/myrepo", "main", token="tok")
        assert sha == "abc1234567890abcdef"

    def test_raises_on_http_404(self):
        exc = urllib.error.HTTPError(
            url="https://api.github.com/...",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b"not found"),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(ValueError, match="404"):
                resolve_ref("myorg/myrepo", "bad-ref")

    def test_raises_on_network_error(self):
        exc = urllib.error.URLError(reason="Name or service not known")
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(ValueError, match="Could not reach GitHub"):
                resolve_ref("myorg/myrepo", "main")

    def test_auth_header_set_when_token_given(self):
        body = json.dumps({"sha": "abc123"}).encode()
        captured_req = []

        def fake_urlopen(req, timeout=None):
            captured_req.append(req)
            return _fake_response(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            resolve_ref("myorg/myrepo", "main", token="my_token")

        headers = captured_req[0].headers
        auth = headers.get("Authorization") or headers.get("authorization")
        assert auth == "Bearer my_token"

    def test_no_auth_header_without_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        body = json.dumps({"sha": "abc123"}).encode()
        captured_req = []

        def fake_urlopen(req, timeout=None):
            captured_req.append(req)
            return _fake_response(body)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            resolve_ref("myorg/myrepo", "main", token="")

        headers = captured_req[0].headers
        auth = headers.get("Authorization") or headers.get("authorization")
        assert not auth


# ---------------------------------------------------------------
# _extract_strip_root
# ---------------------------------------------------------------


class TestExtractStripRoot:
    def test_files_land_directly_in_dest(self, tmp_path):
        tarball = _make_tarball({"payload/T.tbl": "CREATE TABLE T;"})
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            _extract_strip_root(tf, str(tmp_path))
        assert (tmp_path / "payload" / "T.tbl").read_text() == "CREATE TABLE T;"

    def test_root_dir_entry_not_created(self, tmp_path):
        tarball = _make_tarball({"file.txt": "hello"})
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            _extract_strip_root(tf, str(tmp_path))
        # "owner-repo-abc1234" directory should NOT exist at the dest root
        dirs = [d.name for d in tmp_path.iterdir() if d.is_dir()]
        assert not any("abc1234" in d for d in dirs)

    def test_multiple_files_extracted(self, tmp_path):
        tarball = _make_tarball(
            {
                "a.tbl": "CREATE TABLE A;",
                "b.viw": "REPLACE VIEW B AS SELECT 1;",
            }
        )
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            _extract_strip_root(tf, str(tmp_path))
        assert (tmp_path / "a.tbl").exists()
        assert (tmp_path / "b.viw").exists()

    def test_path_traversal_skipped(self, tmp_path):
        # Build a tarball with a path-traversal member manually
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            root = "owner-repo-abc"
            info = tarfile.TarInfo(name=root)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
            # Malicious member: ../../etc/passwd
            content = b"evil"
            evil = tarfile.TarInfo(name=f"{root}/../../evil.txt")
            evil.size = len(content)
            tf.addfile(evil, io.BytesIO(content))

        tarball = buf.getvalue()
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            _extract_strip_root(tf, str(tmp_path))
        # evil.txt must not appear anywhere in tmp_path
        all_files = list(tmp_path.rglob("*"))
        assert not any("evil" in str(f) for f in all_files)


# ---------------------------------------------------------------
# fetch_github_source
# ---------------------------------------------------------------


class TestFetchGithubSource:
    def test_extracts_repo_contents_to_dest(self, tmp_path):
        sha_body = json.dumps({"sha": "abc1234567890"}).encode()
        tarball = _make_tarball({"src/Customer.tbl": "CREATE TABLE T;"})

        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _fake_response(sha_body)
            return _fake_response(tarball)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sha = fetch_github_source("myorg/myrepo", "main", str(tmp_path))

        assert sha == "abc1234567890"
        assert (tmp_path / "src" / "Customer.tbl").exists()

    def test_returns_commit_sha(self, tmp_path):
        sha_body = json.dumps({"sha": "deadbeef12345"}).encode()
        tarball = _make_tarball({"file.txt": "content"})
        responses = [_fake_response(sha_body), _fake_response(tarball)]

        with patch("urllib.request.urlopen", side_effect=responses):
            sha = fetch_github_source("o/r", "v1.0", str(tmp_path))

        assert sha == "deadbeef12345"

    def test_github_token_env_var_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env_token_xyz")
        sha_body = json.dumps({"sha": "abc123"}).encode()
        tarball = _make_tarball({"f.txt": "x"})
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            if len(captured) == 1:
                return _fake_response(sha_body)
            return _fake_response(tarball)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_github_source("o/r", "main", str(tmp_path), token="")

        # First request (resolve_ref) should carry the env token
        auth = captured[0].headers.get("Authorization") or captured[0].headers.get(
            "authorization"
        )
        assert "env_token_xyz" in auth

    def test_raises_on_download_error(self, tmp_path):
        sha_body = json.dumps({"sha": "abc123"}).encode()
        exc = urllib.error.HTTPError(
            url="...",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"forbidden"),
        )

        def fake_urlopen(req, timeout=None):
            if "commits" in req.full_url:
                return _fake_response(sha_body)
            raise exc

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(ValueError, match="403"):
                fetch_github_source("o/r", "main", str(tmp_path))


# ---------------------------------------------------------------
# _resolve_github_source CLI helper
# ---------------------------------------------------------------


class TestResolveGithubSourceCLI:
    """Test the CLI arg-mutation helper without running a subprocess."""

    def _make_args(self, **kwargs):
        defaults = dict(
            source=None,
            source_github=None,
            source_ref="main",
            github_token="",
            commit=None,
        )
        defaults.update(kwargs)
        return Namespace(**defaults)

    def test_no_op_when_source_github_not_set(self):
        from td_release_packager.cli import _resolve_github_source

        args = self._make_args(source="/local/path")
        holder = []
        _resolve_github_source(args, holder)
        assert args.source == "/local/path"
        assert holder == []

    def test_sets_args_source_to_temp_dir(self, tmp_path):
        from td_release_packager.cli import _resolve_github_source

        sha_body = json.dumps({"sha": "abc1234567"}).encode()
        tarball = _make_tarball({"file.txt": "content"})
        responses = [_fake_response(sha_body), _fake_response(tarball)]

        args = self._make_args(source_github="myorg/myrepo", source_ref="main")
        holder = []
        with patch("urllib.request.urlopen", side_effect=responses):
            _resolve_github_source(args, holder)

        assert args.source is not None
        assert os.path.isdir(args.source)
        assert len(holder) == 1  # TemporaryDirectory registered

        # Clean up
        for tmp in holder:
            tmp.cleanup()

    def test_sets_commit_sha_on_args(self, tmp_path):
        from td_release_packager.cli import _resolve_github_source

        sha_body = json.dumps({"sha": "deadbeef12345"}).encode()
        tarball = _make_tarball({"f.txt": "x"})
        responses = [_fake_response(sha_body), _fake_response(tarball)]

        args = self._make_args(source_github="o/r")
        holder = []
        with patch("urllib.request.urlopen", side_effect=responses):
            _resolve_github_source(args, holder)

        assert args.commit == "deadbeef12345"
        for tmp in holder:
            tmp.cleanup()

    def test_does_not_overwrite_explicit_commit(self, tmp_path):
        from td_release_packager.cli import _resolve_github_source

        sha_body = json.dumps({"sha": "auto_sha"}).encode()
        tarball = _make_tarball({"f.txt": "x"})
        responses = [_fake_response(sha_body), _fake_response(tarball)]

        args = self._make_args(source_github="o/r", commit="user_provided_sha")
        holder = []
        with patch("urllib.request.urlopen", side_effect=responses):
            _resolve_github_source(args, holder)

        assert args.commit == "user_provided_sha"
        for tmp in holder:
            tmp.cleanup()

    def test_falls_back_to_git_clone_when_api_returns_404(self):
        from td_release_packager.cli import _resolve_github_source

        args = self._make_args(
            source_github="NathanG-TD/cargointelligence-data-product",
            source_ref="master",
        )
        holder = []

        clone_result = MagicMock(returncode=0, stdout="", stderr="")
        sha_result = MagicMock(returncode=0, stdout="abc123456789\n", stderr="")

        with patch(
            "td_release_packager.remote_source.fetch_github_source",
            side_effect=ValueError("GitHub API error 404"),
        ), patch("subprocess.run", side_effect=[clone_result, sha_result]) as run:
            _resolve_github_source(args, holder)

        assert args.source is not None
        assert args.source.endswith("repo")
        assert args.commit == "abc123456789"
        assert run.call_args_list[0].args[0][:4] == [
            "git",
            "clone",
            "--depth",
            "1",
        ]
        for tmp in holder:
            tmp.cleanup()
