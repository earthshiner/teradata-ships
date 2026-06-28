"""
changeset.py — git-native / baseline change detection (issue #114).

Computes the set of payload objects that changed since a reference point, plus
their downstream dependants, so a later step (#115) can build a minimal
package containing only what needs redeploying.

Two detection modes:

    * **git** (default when the project is a git repo and a ``since`` ref is
      given) — ``git diff --name-only <ref>..HEAD`` over the payload.
    * **baseline** (fallback) — compares each payload file's content hash
      against a captured snapshot (``.ships/changeset.baseline.json``).

Changed objects are expanded by a **forward dependants** walk: an object that
transitively depends on a changed object is included (a changed table pulls in
the views built on it), using the dependency graph from ``analyser``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

BASELINE_SCHEMA_VERSION = "1.0"


@dataclass
class ChangesetResult:
    """Outcome of change detection."""

    mode: str  # "git" | "baseline" | "none"
    changed: Set[str] = field(default_factory=set)  # directly changed objects
    dependants: Set[str] = field(default_factory=set)  # transitive dependants
    selected: Set[str] = field(default_factory=set)  # changed ∪ dependants
    changed_files: List[str] = field(default_factory=list)
    note: str = ""


def _is_git_repo(project_dir: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _git_changed_files(project_dir: str, since: str) -> Optional[List[str]]:
    """Return payload file basenames changed since ``since``, or None on error."""
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "diff", "--name-only", f"{since}..HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _file_hashes(payload_dir: str) -> Dict[str, str]:
    """Map payload-relative path → content hash for every payload file."""
    hashes: Dict[str, str] = {}
    for root, _dirs, files in os.walk(payload_dir):
        for f in sorted(files):
            if f.startswith(".") or f.startswith("_"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                continue
            rel = os.path.relpath(path, payload_dir).replace("\\", "/")
            hashes[rel] = digest
    return hashes


def write_changeset_baseline(project_dir: str, payload_dir: str) -> str:
    """Capture the current payload content hashes as the baseline; return path."""
    from td_release_packager.project_paths import (
        changeset_baseline_path,
        ensure_ships_state_dir,
    )

    ensure_ships_state_dir(project_dir)
    path = changeset_baseline_path(project_dir)
    doc = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "hashes": _file_hashes(payload_dir),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def _load_baseline_hashes(project_dir: str) -> Optional[Dict[str, str]]:
    from td_release_packager.project_paths import changeset_baseline_path

    path = changeset_baseline_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc.get("hashes", {}) if isinstance(doc, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _baseline_changed_files(project_dir: str, payload_dir: str) -> Optional[List[str]]:
    """Return payload-relative paths whose hash differs from the baseline.

    Returns None when no baseline has been captured (the caller reports that
    detection is unavailable rather than treating everything as changed).
    """
    baseline = _load_baseline_hashes(project_dir)
    if baseline is None:
        return None
    current = _file_hashes(payload_dir)
    changed = [rel for rel, h in current.items() if baseline.get(rel) != h]
    # Deletions: in baseline but gone now. Reported as changed paths too.
    changed += [rel for rel in baseline if rel not in current]
    return sorted(set(changed))


def _reverse_dependants(dependencies: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Invert ``{A: {deps}}`` (A depends on B) → ``{B: {dependants}}``."""
    rev: Dict[str, Set[str]] = {}
    for a, deps in dependencies.items():
        for b in deps:
            rev.setdefault(b, set()).add(a)
    return rev


def _expand_dependants(seed: Set[str], reverse: Dict[str, Set[str]]) -> Set[str]:
    """Forward BFS: all objects that transitively depend on the seed set."""
    out: Set[str] = set()
    queue = list(seed)
    while queue:
        node = queue.pop()
        for dep in reverse.get(node, ()):  # objects depending ON node
            if dep not in out and dep not in seed:
                out.add(dep)
                queue.append(dep)
    return out


def detect_changeset(
    project_dir: str,
    since: Optional[str] = None,
    payload_dir: Optional[str] = None,
) -> ChangesetResult:
    """Detect the changed object set + dependants for ``project_dir``.

    Precedence: git diff (when ``since`` is given and the project is a git
    repo) → content-hash baseline fallback. Changed files are mapped to
    qualified objects via the analyser index (by file basename, which is
    unique under the eponymous payload), then expanded by forward dependants.
    """
    from td_release_packager.analyser import analyse_project
    from td_release_packager.validate import resolve_inspect_root

    if payload_dir is None:
        payload_dir = resolve_inspect_root(project_dir)

    # -- 1. changed files (git preferred, baseline fallback) --
    mode = "none"
    note = ""
    changed_files: Optional[List[str]] = None
    if since and _is_git_repo(project_dir):
        changed_files = _git_changed_files(project_dir, since)
        if changed_files is not None:
            mode = "git"
    if changed_files is None:
        changed_files = _baseline_changed_files(project_dir, payload_dir)
        if changed_files is not None:
            mode = "baseline"
    if changed_files is None:
        return ChangesetResult(
            mode="none",
            note=(
                "No changeset baseline found and no usable git ref. Capture a "
                "baseline with `ships changeset --update-baseline`, or pass "
                "--since-tag / --since-commit in a git repo."
            ),
        )

    # -- 2. map changed files → qualified objects (by basename) --
    analysis = analyse_project(project_dir)
    by_basename: Dict[str, str] = {}
    for qn, obj in analysis.objects.items():
        by_basename[os.path.basename(obj.file_path)] = qn
    changed_objects: Set[str] = set()
    for path in changed_files:
        qn = by_basename.get(os.path.basename(path))
        if qn:
            changed_objects.add(qn)

    # -- 3. expand by forward dependants --
    reverse = _reverse_dependants(analysis.dependencies)
    dependants = _expand_dependants(changed_objects, reverse)

    return ChangesetResult(
        mode=mode,
        changed=changed_objects,
        dependants=dependants,
        selected=changed_objects | dependants,
        changed_files=changed_files,
        note=note,
    )
