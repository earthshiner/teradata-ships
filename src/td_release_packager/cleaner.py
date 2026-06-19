"""
cleaner.py — Explicit "reset prior pipeline output" implementation.

Backs the ``ships_clean`` MCP tool and the ``clean`` CLI subcommand.
Removes prior SHIPS output by ``shutil.rmtree`` of whole subtrees, never
by reconstructing per-file paths. The per-file approach is what produced
the contamination bug where differently-tokenised ``.dcl`` filenames
survived a re-harvest (HANDOVER 2026-06-19, §2).

Design points
-------------
* Synchronous and fast — no detached subprocess, no run_id.
* ``dry_run=True`` by default; callers must opt in to actually delete.
* Refuses any directory that doesn't look like a SHIPS project (no
  ``ships.yaml``) so a mistyped path can never ``rmtree`` something
  unrelated.
* ``config/`` and the ``.build_counter`` file are never touched —
  monotonic build numbers are useful provenance across rebuilds.
* Emptied directories are recreated (with a ``.gitkeep`` marker) so the
  project tree stays valid for the next Scaffold/Harvest.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Mapping from scope name → list of (relative path, kind) tuples.
# ``kind`` is "dir" for subtrees that get rmtree-then-recreate semantics,
# or "file" for single files (e.g. the decisions ledger).
_SCOPE_TARGETS: Dict[str, List[Tuple[str, str]]] = {
    "runs": [(".ships/runs", "dir")],
    "payload": [(os.path.join("payload", "database"), "dir")],
    "releases": [("releases", "dir")],
    "reports": [(os.path.join("output", "reports"), "dir")],
    "decisions": [("ships.decisions.json", "file")],
}

# Scopes that ``scope=all`` expands to. ``.build_counter`` is
# deliberately omitted — monotonic build numbers survive a full reset.
# TODO(Paul): if a build-counter reset is ever wanted, expose it as a
# separate flag (e.g. ``reset_build_counter=False``) rather than folding
# it into ``all``.
_ALL_SCOPES: List[str] = ["runs", "payload", "releases", "reports", "decisions"]

CLEAN_SCOPES: List[str] = [*_SCOPE_TARGETS.keys(), "all"]


def _count_tree(path: str) -> Tuple[int, int]:
    """Return ``(file_count, dir_count)`` under ``path``.

    A missing path is reported as ``(0, 0)``; a single file as
    ``(1, 0)``. Used so the dry-run preview can tell the caller how many
    things would be removed before we touch the disk.
    """
    if not os.path.exists(path):
        return (0, 0)
    if os.path.isfile(path):
        return (1, 0)
    files = 0
    dirs = 0
    for _root, dnames, fnames in os.walk(path):
        files += len(fnames)
        dirs += len(dnames)
    return (files, dirs)


def _rmtree_and_recreate(target: str, *, seed_gitkeep: bool = True) -> Tuple[int, int]:
    """Wipe a directory by ``rmtree`` and recreate it empty.

    This is the single low-level primitive shared with the harvest
    payload-clean step (see :func:`td_release_packager.ingest._clean_payload_tree`).
    Using ``rmtree`` rather than per-file unlink is the whole point of
    the new clean tool: filename reconstruction is what historically
    failed for tokenised names that varied between runs.

    Args:
        target: Absolute path to the directory to wipe. A missing
            path is a no-op.
        seed_gitkeep: When True (default), drop an empty ``.gitkeep`` in
            the recreated directory so git keeps tracking it.

    Returns:
        ``(files_removed, dirs_removed)`` counted before the wipe.
    """
    if not os.path.isdir(target):
        return (0, 0)

    files, dirs = _count_tree(target)
    shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    if seed_gitkeep:
        gitkeep = os.path.join(target, ".gitkeep")
        try:
            with open(gitkeep, "w", encoding="utf-8") as fh:
                fh.write("")
        except OSError as exc:
            logger.warning("Could not seed .gitkeep in %s: %s", target, exc)
    return (files, dirs)


def _resolve_lifecycle_state(project: str) -> str | None:
    """Best-effort lookup of ``lifecycle_state`` after a clean.

    Imported lazily so this module stays importable even if the
    project-index module's dependencies change. Any failure is swallowed
    — the field is informational, not load-bearing.
    """
    try:
        from td_release_packager.project_index import compute_project_index

        idx = compute_project_index(project)
        if isinstance(idx, dict):
            return idx.get("lifecycle_state")
        return getattr(idx, "lifecycle_state", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("lifecycle_state lookup failed after clean: %s", exc)
        return None


def clean_project(project: str, scope: str = "payload", dry_run: bool = True) -> dict:
    """Wipe prior pipeline output for a SHIPS project.

    Args:
        project: Project root (must contain ``ships.yaml``).
        scope: One of ``runs``, ``payload``, ``releases``, ``reports``,
            ``decisions``, or ``all``. Default ``payload`` — the usual
            "give me a clean harvest surface" intent.
        dry_run: When True (default), only report what would be removed.

    Returns:
        Envelope dict::

            {
                "success": bool,
                "scope": str,
                "dry_run": bool,
                "project_dir": str,
                "targets": [{"path": str, "kind": str, "exists": bool}, ...],
                "removed_files": int,
                "removed_dirs": int,
                "lifecycle_state_after": str | None,
                "error": str | None,
            }
    """
    project_abs = os.path.abspath(project)

    # Guard 1: directory must exist. Surfaced as a clean error, not a raw
    # OSError, so MCP callers can render it.
    if not os.path.isdir(project_abs):
        return {
            "success": False,
            "scope": scope,
            "dry_run": dry_run,
            "project_dir": project_abs,
            "targets": [],
            "removed_files": 0,
            "removed_dirs": 0,
            "lifecycle_state_after": None,
            "error": f"project directory not found: {project_abs}",
        }

    # Guard 2: must look like a SHIPS project. Without this gate a
    # mistyped ``--project`` could rmtree something unrelated.
    if not os.path.isfile(os.path.join(project_abs, "ships.yaml")):
        return {
            "success": False,
            "scope": scope,
            "dry_run": dry_run,
            "project_dir": project_abs,
            "targets": [],
            "removed_files": 0,
            "removed_dirs": 0,
            "lifecycle_state_after": None,
            "error": (
                f"not a SHIPS project (no ships.yaml at {project_abs}); refusing to clean"
            ),
        }

    # Guard 3: scope must be one we know how to resolve.
    if scope not in CLEAN_SCOPES:
        return {
            "success": False,
            "scope": scope,
            "dry_run": dry_run,
            "project_dir": project_abs,
            "targets": [],
            "removed_files": 0,
            "removed_dirs": 0,
            "lifecycle_state_after": None,
            "error": (f"unknown scope {scope!r}; choose one of {CLEAN_SCOPES}"),
        }

    resolved_scopes = _ALL_SCOPES if scope == "all" else [scope]

    targets: List[dict] = []
    removed_files = 0
    removed_dirs = 0

    # First pass — measure. We always populate ``targets`` and counts so
    # the dry-run preview and the applied result share the same shape.
    measurements: List[Tuple[str, str, str, int, int]] = []
    for sub_scope in resolved_scopes:
        for rel, kind in _SCOPE_TARGETS[sub_scope]:
            abs_path = os.path.join(project_abs, rel)
            f, d = _count_tree(abs_path)
            measurements.append((rel, kind, abs_path, f, d))
            targets.append(
                {"path": rel, "kind": kind, "exists": os.path.exists(abs_path)}
            )
            removed_files += f
            removed_dirs += d

    if dry_run:
        return {
            "success": True,
            "scope": scope,
            "dry_run": True,
            "project_dir": project_abs,
            "targets": targets,
            "removed_files": removed_files,
            "removed_dirs": removed_dirs,
            "lifecycle_state_after": None,
            "error": None,
        }

    # Apply — rmtree (or unlink for the single-file decisions ledger).
    for _rel, kind, abs_path, _f, _d in measurements:
        if not os.path.exists(abs_path):
            continue
        if kind == "file":
            try:
                os.remove(abs_path)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", abs_path, exc)
            continue
        # kind == "dir": rmtree + recreate. seed_gitkeep keeps git happy.
        _rmtree_and_recreate(abs_path, seed_gitkeep=True)

    return {
        "success": True,
        "scope": scope,
        "dry_run": False,
        "project_dir": project_abs,
        "targets": targets,
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "lifecycle_state_after": _resolve_lifecycle_state(project_abs),
        "error": None,
    }
