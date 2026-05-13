"""
remote_source.py — Fetch source DDL from a remote GitHub repository.

Uses the GitHub REST API to download a repository tarball for any ref
(branch, tag, or commit SHA) and extracts it to a local directory so
the normal SHIPS pipeline (harvest → inspect → analyse → package) can
operate on it without requiring a local git clone.

No external dependencies beyond the Python standard library.

Authentication
--------------
Public repositories work without a token.  Private repositories require
a GitHub personal access token (PAT) with at least ``repo`` scope:

    python -m td_release_packager process \
        --source-github myorg/myrepo \
        --source-ref v1.2.3 \
        --github-token $GITHUB_TOKEN \
        ...

If ``--github-token`` is not passed, SHIPS reads the ``GITHUB_TOKEN``
environment variable automatically.

Rate limits
-----------
Unauthenticated requests are subject to GitHub's 60-requests-per-hour
rate limit per IP.  Authenticated requests allow 5000 per hour per token.
``fetch_github_source`` makes two API requests per call (one to resolve
the SHA, one to download the tarball).

GitHub Enterprise Server
------------------------
Set ``SHIPS_GITHUB_API_URL`` to your enterprise base URL
(e.g. ``https://github.mycompany.com/api/v3``) to route all requests
through it.  The default is ``https://api.github.com``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _api_base() -> str:
    return os.getenv("SHIPS_GITHUB_API_URL", _DEFAULT_API).rstrip("/")


def _headers(token: str) -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SHIPS-deployment-agent",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url: str, token: str, timeout: int = 30) -> bytes:
    """Execute a GET request and return the response body."""
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise ValueError(
            f"GitHub API error {exc.code} for {url}\n" + (f"  {body}" if body else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(
            f"Could not reach GitHub ({url}): {exc.reason}\n"
            "  Check your network connection and proxy settings."
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_ref(owner_repo: str, ref: str, token: str = "") -> str:
    """Return the full commit SHA for a GitHub ref.

    Args:
        owner_repo: ``"owner/repo"`` string.
        ref:        Branch name, tag name, or commit SHA.
        token:      GitHub PAT.  Falls back to ``GITHUB_TOKEN`` env var.

    Returns:
        Full 40-character commit SHA.

    Raises:
        ValueError: On HTTP error or if the ref does not exist.
    """
    token = token or os.getenv("GITHUB_TOKEN", "")
    url = f"{_api_base()}/repos/{owner_repo}/commits/{ref}"
    data = json.loads(_get(url, token))
    sha = data.get("sha", "")
    if not sha:
        raise ValueError(
            f"Could not resolve ref '{ref}' in {owner_repo} — "
            "branch, tag, or commit not found."
        )
    logger.debug("remote_source: %s@%s → %s", owner_repo, ref, sha[:12])
    return sha


def fetch_github_source(
    owner_repo: str,
    ref: str,
    dest_dir: str,
    token: str = "",
) -> str:
    """Download a GitHub repository tarball and extract it to ``dest_dir``.

    Makes two API calls:
      1. Resolve ``ref`` to a full commit SHA (via ``/commits/{ref}``).
      2. Download the tarball (via ``/tarball/{sha}``).

    The tarball root directory (``{owner}-{repo}-{sha8}/``) is stripped so
    ``dest_dir`` contains the repository contents directly.

    Args:
        owner_repo: ``"owner/repo"`` string, e.g. ``"myorg/myrepo"``.
        ref:        Branch name, tag name, or commit SHA.
        dest_dir:   Directory to extract into (must already exist).
        token:      GitHub PAT.  Falls back to ``GITHUB_TOKEN`` env var.
                    Optional for public repositories.

    Returns:
        The full 40-character commit SHA for the resolved ref.  Record
        this as ``source_commit`` in ``ships.build.json`` for traceability.

    Raises:
        ValueError: On HTTP error, missing ref, or extraction failure.
    """
    token = token or os.getenv("GITHUB_TOKEN", "")

    # 1. Resolve the ref to a commit SHA
    sha = resolve_ref(owner_repo, ref, token)

    # 2. Download the tarball
    tarball_url = f"{_api_base()}/repos/{owner_repo}/tarball/{sha}"
    logger.info("remote_source: fetching %s@%s (%s…)", owner_repo, ref, sha[:8])
    raw = _get(tarball_url, token, timeout=120)

    # 3. Extract, stripping the root directory
    #    GitHub tarballs have a single root component: "owner-repo-sha8/"
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            _extract_strip_root(tf, dest_dir)
    except tarfile.TarError as exc:
        raise ValueError(
            f"Failed to extract tarball for {owner_repo}@{ref}: {exc}"
        ) from exc

    logger.info("remote_source: extracted %s@%s → %s", owner_repo, sha[:8], dest_dir)
    return sha


def _extract_strip_root(tf: tarfile.TarFile, dest_dir: str) -> None:
    """Extract ``tf`` into ``dest_dir`` stripping the first path component.

    GitHub tarballs wrap everything in a single root directory such as
    ``myorg-myrepo-abc1234/``.  This function removes that wrapper so
    the repository contents land directly in ``dest_dir``.
    """
    members = tf.getmembers()
    if not members:
        return

    # Identify the root component (everything before the first '/')
    root = members[0].name.split("/")[0]

    for member in members:
        # Skip the root directory entry itself
        if member.name in (root, root + "/"):
            continue
        # Strip the root prefix
        if member.name.startswith(root + "/"):
            member.name = member.name[len(root) + 1 :]
        if not member.name:
            continue
        # Security: skip absolute paths and path traversal
        if member.name.startswith("/") or ".." in member.name.split("/"):
            logger.warning("remote_source: skipping unsafe path %s", member.name)
            continue
        tf.extract(member, dest_dir, filter="data")
