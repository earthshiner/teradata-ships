"""Helpers for locating SHIPS package metadata files.

The canonical package metadata contract starts at
``context/ships.index.json``.  All SHIPS JSON metadata files live under the
package ``context/`` directory.  SHIPS has not released a root-level metadata
contract, so there is intentionally no legacy fallback.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

CONTEXT_DIR = "context"
PACKAGE_INDEX = "ships.index.json"


def is_ships_json(filename: str) -> bool:
    """Return True when *filename* is one of the SHIPS JSON metadata files."""
    name = os.path.basename(filename.replace("\\", "/"))
    return name.startswith("ships.") and name.endswith(".json")


def package_root(candidate: str) -> str:
    """Return the extracted package root for a package or logs directory."""
    path = os.path.abspath(candidate)
    if os.path.basename(path).lower() == "logs":
        return os.path.dirname(path)
    return path


def package_file(package_dir: str, filename: str) -> str:
    """Return the canonical path for a SHIPS metadata file.

    All ``ships.*.json`` files live under ``context/``. Non-SHIPS files remain
    relative to the package root.
    """
    root = package_root(package_dir)
    if is_ships_json(filename):
        return os.path.join(root, CONTEXT_DIR, os.path.basename(filename))
    return os.path.join(root, filename)


def package_file_candidates(package_dir: str, filename: str) -> Iterable[str]:
    """Yield the canonical package metadata location.

    Kept as an iterable helper for callers that already loop over candidates;
    it deliberately yields no root-level fallback.
    """
    yield package_file(package_dir, filename)


def package_index_file(package_dir: str) -> str:
    """Return the canonical read-first package index path."""
    return package_file(package_dir, PACKAGE_INDEX)


def read_package_json(
    package_dir: str,
    filename: str,
) -> dict[str, Any]:
    """Read a package metadata JSON document.

    Args:
        package_dir: Package root, or package ``logs`` directory.
        filename: Metadata filename such as ``ships.build.json``.
    Returns:
        Parsed JSON object, or an empty dict when absent/unreadable.
    """
    path = package_file(package_dir, filename)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def read_package_index(package_dir: str) -> dict[str, Any]:
    """Read ``context/ships.index.json``."""
    return read_package_json(package_dir, PACKAGE_INDEX)
