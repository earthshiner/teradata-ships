"""
github_source.py — Download SHIPS release packages from GitHub (GAP-016).

Allows DBAs to deploy directly from a GitHub Release without file transfer.
Downloads the ZIP archive and all available sidecar files (.sha256, .hmac, .sig)
to a temporary directory, then runs the normal deploy flow.

Authentication:
    Uses GITHUB_TOKEN environment variable for private repositories.
    Public repositories do not require authentication.

Usage (CLI):
    ships deploy --from-github owner/repo --release-tag v1.2.3 --asset PKG.zip \\
        --env PRD --host myhost --user ships_dba
"""

import logging
import os
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"


def _make_request(url: str, headers: dict) -> bytes:
    """Perform a GET request and return the response body as bytes.

    Args:
        url:     Full URL to fetch.
        headers: HTTP headers dict (may include Authorization).

    Returns:
        Response body bytes.

    Raises:
        urllib.error.HTTPError: On non-200 responses.
        urllib.error.URLError:  On network errors.
    """
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response:
        return response.read()


def _build_headers() -> dict:
    """Build the HTTP headers for GitHub API requests.

    Includes ``Authorization: Bearer <token>`` when ``GITHUB_TOKEN`` is set.
    Always requests JSON from the API and binary for asset downloads.

    Returns:
        Headers dict.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def download_release_assets(
    owner_repo: str,
    release_tag: str,
    asset_name: str,
    dest_dir: str,
) -> str:
    """Download a release asset and available sidecars from a GitHub Release.

    Fetches the release metadata from the GitHub API, locates the named asset,
    downloads it, and then silently attempts to download the sidecar files
    ``<asset_name>.sha256``, ``<asset_name>.hmac``, and ``<asset_name>.sig``.
    Any sidecar that is not present in the release is skipped without error.

    Args:
        owner_repo:  Repository in ``owner/repo`` format (e.g. ``acme/myapp``).
        release_tag: GitHub Release tag (e.g. ``v1.2.3``) or ``latest``.
        asset_name:  Filename of the ZIP asset in the release.
        dest_dir:    Directory to download files into.

    Returns:
        Path to the downloaded ZIP file.

    Raises:
        ValueError:               When the named asset is not found in the release.
        urllib.error.HTTPError:   On non-200 API responses.
        urllib.error.URLError:    On network errors.
    """
    import json

    headers = _build_headers()

    if release_tag == "latest":
        api_url = f"{_GITHUB_API_BASE}/repos/{owner_repo}/releases/latest"
    else:
        api_url = f"{_GITHUB_API_BASE}/repos/{owner_repo}/releases/tags/{release_tag}"

    logger.info("github_source: fetching release metadata from %s", api_url)
    release_json = _make_request(api_url, headers)
    release = json.loads(release_json)

    assets = {a["name"]: a for a in release.get("assets", [])}

    if asset_name not in assets:
        available = ", ".join(sorted(assets.keys())) or "(none)"
        raise ValueError(
            f"Asset '{asset_name}' not found in release '{release_tag}' "
            f"of {owner_repo}. Available assets: {available}"
        )

    # Download the primary ZIP asset
    zip_path = _download_asset(assets[asset_name], dest_dir, headers)
    logger.info("github_source: downloaded '%s' → %s", asset_name, zip_path)

    # Attempt to download sidecars — silently skip any that are absent
    for suffix in (".sha256", ".hmac", ".sig"):
        sidecar_name = asset_name + suffix
        if sidecar_name in assets:
            sidecar_path = _download_asset(assets[sidecar_name], dest_dir, headers)
            logger.info(
                "github_source: downloaded sidecar '%s' → %s",
                sidecar_name,
                sidecar_path,
            )
        else:
            logger.debug(
                "github_source: sidecar '%s' not in release — skipping.", sidecar_name
            )

    return zip_path


def _download_asset(asset: dict, dest_dir: str, headers: dict) -> str:
    """Download a single release asset to *dest_dir*.

    Uses the asset's ``browser_download_url`` (public) or falls back to
    the API download URL with an ``Accept: application/octet-stream`` header.

    Args:
        asset:    GitHub asset metadata dict (from the releases API).
        dest_dir: Destination directory.
        headers:  Base request headers (will be copied and modified).

    Returns:
        Path to the downloaded file.
    """
    download_headers = dict(headers)
    download_headers["Accept"] = "application/octet-stream"

    url = asset.get("browser_download_url") or asset["url"]
    dest_path = os.path.join(dest_dir, asset["name"])

    logger.debug("github_source: downloading %s → %s", url, dest_path)
    body = _make_request(url, download_headers)

    Path(dest_path).write_bytes(body)
    return dest_path


def extract_zip_to_dir(zip_path: str, dest_dir: str) -> str:
    """Extract a ZIP archive to *dest_dir* and return the package directory.

    Assumes the ZIP contains a single top-level directory (the standard SHIPS
    package layout: ``<package_name>/``).  Returns the path to that directory.

    If the ZIP has multiple top-level entries or no top-level directory, the
    extracted root (``dest_dir``) is returned.

    Args:
        zip_path:  Path to the ZIP archive.
        dest_dir:  Directory to extract into.

    Returns:
        Path to the extracted package directory.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

        # Discover the top-level directory inside the archive
        top_level_dirs: set = set()
        for name in zf.namelist():
            parts = name.replace("\\", "/").split("/")
            if parts[0]:
                top_level_dirs.add(parts[0])

    if len(top_level_dirs) == 1:
        pkg_dir = os.path.join(dest_dir, next(iter(top_level_dirs)))
        if os.path.isdir(pkg_dir):
            logger.info("github_source: extracted package directory: %s", pkg_dir)
            return pkg_dir

    logger.info(
        "github_source: multiple or no top-level dirs in ZIP — using %s", dest_dir
    )
    return dest_dir
