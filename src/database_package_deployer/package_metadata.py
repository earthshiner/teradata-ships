"""Helpers for locating SHIPS package metadata files."""

from __future__ import annotations

import os
from typing import Iterable

CONTEXT_DIR = "context"


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
    if filename.startswith("ships.") and filename.endswith(".json"):
        canonical = os.path.join(root, CONTEXT_DIR, filename)
        legacy = os.path.join(root, filename)
        return canonical if os.path.exists(canonical) or not os.path.exists(legacy) else legacy
    return os.path.join(root, filename)


def package_file_candidates(package_dir: str, filename: str) -> Iterable[str]:
    """Yield likely locations for a metadata file from nearest to broadest."""
    root = package_root(package_dir)
    if filename.startswith("ships.") and filename.endswith(".json"):
        yield os.path.join(root, CONTEXT_DIR, filename)
    yield os.path.join(root, filename)
    if os.path.basename(os.path.abspath(package_dir)).lower() != "logs":
        parent = os.path.dirname(os.path.abspath(package_dir))
        if filename.startswith("ships.") and filename.endswith(".json"):
            yield os.path.join(parent, CONTEXT_DIR, filename)
        yield os.path.join(parent, filename)
