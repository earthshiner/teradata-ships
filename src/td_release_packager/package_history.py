"""
package_history.py — non-linear package history check (issue #168).

A project-level inspect check over the built packages under
``<project>/releases/``. Unlike the per-file Coding Discipline rules, this
scans the *release groups* SHIPS has already produced and flags a package
sequence that cannot be trusted:

    * an integrity sidecar (``.sha256``) that no longer matches its archive,
    * a package that ``requires`` a sibling archive missing from its group,
    * an orphaned ``prereqs`` half with no ``main`` in the same group,
    * a release group whose archives disagree on build number / timestamp,
    * a build number reused across groups with different contents,
    * an older build number appearing after a newer one (out-of-order).

Findings are returned as ``validate.ValidationIssue`` objects (rule
``non_linear_package_history``) so they flow through the same console and
``ships.decisions.json`` surfaces as built-in lint findings. The check is a
no-op when ``releases/`` is absent or empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List, Optional

from td_release_packager.validate import ValidationIssue

RELEASES_DIRNAME = "releases"
RULE_NAME = "non_linear_package_history"

# Build number + timestamp are embedded in every archive / group name as
# ``..._BUILD_<build>_<timestamp>_...``. env / package_name precede BUILD and
# may themselves contain underscores, so we anchor on the BUILD marker rather
# than trying to split the whole name.
_BUILD_RE = re.compile(r"_BUILD_(?P<build>\d+)_(?P<ts>\d+)")


def _read_release_group(group_dir: str) -> Optional[dict]:
    """Load ``release_group.json`` from a group directory, or None."""
    path = os.path.join(group_dir, "release_group.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _sidecar_digest(checksum_path: str) -> Optional[str]:
    """Return the hex digest recorded in a ``.sha256`` sidecar (first token)."""
    try:
        with open(checksum_path, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    return first.split()[0].lower() if first else None


def _file_digest(path: str) -> Optional[str]:
    """Return the SHA-256 hex digest of a file's bytes, or None."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest().lower()
    except OSError:
        return None


def _parse_build_ts(name: str):
    """Return ``(build_int, ts_str)`` parsed from a name, or ``(None, None)``."""
    m = _BUILD_RE.search(name)
    if not m:
        return None, None
    return int(m.group("build")), m.group("ts")


def check_package_history(
    project_dir: str, severity: str = "WARNING"
) -> List[ValidationIssue]:
    """Scan ``<project_dir>/releases/`` for non-linear package history.

    Args:
        project_dir: SHIPS project root.
        severity:    Severity to stamp on findings (caller resolves it from
                     ``inspect.conf``; ``OFF`` is handled by the caller).

    Returns:
        A list of ``ValidationIssue`` (possibly empty). No-op when there is
        no ``releases/`` directory.
    """
    releases = os.path.join(project_dir, RELEASES_DIRNAME)
    if not os.path.isdir(releases):
        return []

    issues: List[ValidationIssue] = []

    def _emit(location: str, message: str) -> None:
        issues.append(
            ValidationIssue(
                file=location,
                rule=RULE_NAME,
                severity=severity,
                message=message,
                remediation={
                    "safe_fix_available": False,
                    "automation_level": "manual_review_required",
                    "requires_human_review": True,
                    "recommended_action": (
                        "Rebuild the affected release group with a fresh build "
                        "number, restore the missing package half, or "
                        "regenerate the integrity sidecar. Never reuse a build "
                        "number for different contents."
                    ),
                },
            )
        )

    # cohort key (env, name) -> list of per-group facts for cross-group checks
    cohorts: Dict[tuple, List[dict]] = {}

    for entry in sorted(os.listdir(releases)):
        group_dir = os.path.join(releases, entry)
        if not os.path.isdir(group_dir):
            continue
        doc = _read_release_group(group_dir)
        if not doc:
            # Not a SHIPS release group (no manifest) — skip silently.
            continue

        rel_loc = f"{RELEASES_DIRNAME}/{entry}"
        packages = doc.get("packages") or []
        env = doc.get("environment", "")
        name = doc.get("package_name", "")

        present_archives = {p.get("archive") for p in packages if p.get("archive")}
        roles = {(p.get("role") or "").lower() for p in packages}

        # -- Per-package integrity + requires --
        group_digests: Dict[str, str] = {}
        builds_seen = set()
        ts_seen = set()
        for pkg in packages:
            archive = pkg.get("archive")
            if not archive:
                continue
            b, ts = _parse_build_ts(archive)
            if b is not None:
                builds_seen.add(b)
                ts_seen.add(ts)

            archive_path = os.path.join(group_dir, archive)
            checksum_name = pkg.get("checksum") or (archive + ".sha256")
            checksum_path = os.path.join(group_dir, checksum_name)

            # Integrity sidecar mismatch.
            if os.path.isfile(archive_path):
                recorded = _sidecar_digest(checksum_path)
                if recorded is None:
                    _emit(
                        f"{rel_loc}/{archive}",
                        f"Package '{archive}' has no readable integrity sidecar "
                        f"({checksum_name}). The archive's integrity cannot be "
                        f"verified.",
                    )
                else:
                    actual = _file_digest(archive_path)
                    group_digests[archive] = actual or ""
                    if actual is not None and actual != recorded:
                        _emit(
                            f"{rel_loc}/{archive}",
                            f"Integrity sidecar mismatch for '{archive}': the "
                            f".sha256 records {recorded[:12]}… but the archive "
                            f"hashes to {actual[:12]}…. The package has changed "
                            f"since it was built.",
                        )

            # Required sibling present in the same group?
            for req in pkg.get("requires") or []:
                if req not in present_archives:
                    _emit(
                        f"{rel_loc}/{archive}",
                        f"Package '{archive}' requires sibling '{req}', which is "
                        f"missing from the release group. A main package cannot "
                        f"deploy without its prerequisite half.",
                    )

        # -- Orphaned prereqs half (a prereqs with no main) --
        has_prereqs = any("prereq" in r for r in roles)
        has_main = any(r == "main" for r in roles)
        if has_prereqs and not has_main:
            _emit(
                rel_loc,
                "Release group has a prereqs package but no matching main "
                "package. The pair is incomplete.",
            )

        # -- Internal consistency: one build number + timestamp per group --
        if len(builds_seen) > 1 or len(ts_seen) > 1:
            _emit(
                rel_loc,
                "Release group mixes archives from different builds "
                f"(build numbers {sorted(builds_seen)}, timestamps "
                f"{sorted(ts_seen)}). A release group must be one coherent "
                "build.",
            )

        # Record a cohort fact for cross-group checks.
        build_int, ts_str = _parse_build_ts(entry)
        if build_int is None and builds_seen:
            build_int = sorted(builds_seen)[0]
        # Group content identity = digest of the sorted per-archive digests.
        content_key = hashlib.sha256(
            json.dumps(sorted(group_digests.items())).encode()
        ).hexdigest()
        cohorts.setdefault((env, name), []).append(
            {
                "group": entry,
                "loc": rel_loc,
                "build": build_int,
                "ts": ts_str or "",
                "content": content_key,
            }
        )

    # -- Cross-group checks per (env, name) cohort --
    for (env, name), groups in cohorts.items():
        # Build number reused with different contents.
        by_build: Dict[int, List[dict]] = {}
        for g in groups:
            if g["build"] is not None:
                by_build.setdefault(g["build"], []).append(g)
        for build, members in by_build.items():
            distinct_content = {m["content"] for m in members}
            if len(members) > 1 and len(distinct_content) > 1:
                locs = ", ".join(sorted(m["group"] for m in members))
                _emit(
                    f"{RELEASES_DIRNAME}/",
                    f"Build number {build} for {env}/{name} is reused across "
                    f"release groups with different contents: {locs}. A build "
                    f"number must identify exactly one set of bytes.",
                )

        # Older build number appearing after a newer one (out-of-order).
        ordered = sorted(
            [g for g in groups if g["build"] is not None and g["ts"]],
            key=lambda g: g["ts"],
        )
        for prev, cur in zip(ordered, ordered[1:]):
            if cur["build"] < prev["build"]:
                _emit(
                    f"{RELEASES_DIRNAME}/{cur['group']}",
                    f"Non-linear build history for {env}/{name}: build "
                    f"{cur['build']} ({cur['group']}) was created after build "
                    f"{prev['build']} ({prev['group']}) but has a lower build "
                    f"number.",
                )

    return issues
