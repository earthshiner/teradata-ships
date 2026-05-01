"""
validate_grants.py — Cross-file grant validation orchestrator.

This module is Step 2 of the SHIPS validate command. Step 1 (per-file
DDL lint) lives in ``validate.py``; this module compares the grants
*implied* by the DDL across an entire project against the *persisted*
``.grt`` files in the project's DCL/inter_db directory, and reports
or repairs any drift.

The heavy lifting — parsing DDL, extracting cross-database references,
deciding which privileges each reference implies, and emitting .grt
content — is delegated to ``infer_grants.py``. This module is a thin
orchestrator that:

    1. Calls infer_grants to derive the *expected* grants per grantee.
    2. Reads the *actual* .grt files persisted in dcl_dir.
    3. Compares semantically (set of grantor → privilege-set per
       grantee), producing a structured GrantValidationResult.
    4. Optionally writes missing/drifted files (fix mode).

Public API (matches what cli.py imports):

    validate_grants(project_dir, dcl_dir=None, verbose=False)
        Read-only audit. Returns a GrantValidationResult.

    fix_grants(project_dir, dcl_dir=None, verbose=False)
        Writes expected files for any missing or drifted grantees.
        Does NOT delete orphaned files — manual review required.
        Returns (GrantValidationResult, files_written: int).

    format_report(result)
        Human-readable summary string.

Drift semantics:
    Two .grt files are considered EQUIVALENT if they contain the same
    set of (grantor, privilege) pairs after parsing — formatting,
    comment headers, and statement ordering are ignored. This means
    manual edits that preserve the underlying grant set don't trigger
    drift, but manual additions/removals of privileges do.

Orphan policy:
    A .grt file in dcl_dir whose grantee is not present in the DDL
    inference is reported as ORPHANED. Orphans are NEVER auto-deleted
    in fix mode — they may be intentional manual grants outside the
    inference's reach. Manual review and removal are required.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from td_release_packager.infer_grants import (
    PRIV_ORDER,
    analyse_file,
    consolidate_grants,
    find_ddl_files,
    generate_grt_content,
    grantee_filename,
    strip_sql_comments,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default DCL directory
# ---------------------------------------------------------------------------

# Where infer_grants.py writes by default and where existing .grt files
# are expected to live. Mirrored here so a None dcl_dir resolves
# consistently between validation and inference.
_DEFAULT_DCL_SUBPATH = ("payload", "database", "DCL", "inter_db")


def _resolve_dcl_dir(project_dir: Path, dcl_dir: Optional[Path]) -> Path:
    """Resolve the DCL directory, applying the project default if None."""
    if dcl_dir is not None:
        return dcl_dir
    return project_dir.joinpath(*_DEFAULT_DCL_SUBPATH)


# ---------------------------------------------------------------------------
# GRANT statement parsing — for reading existing .grt files
# ---------------------------------------------------------------------------

# Identifier shape — accepts tokens, quoted ids, and bare ids.
_GRANT_IDENT = r'(?:\{\{[A-Za-z_]\w*\}\}|"[^"]+"|[A-Za-z_]\w*)'

# A GRANT statement of the canonical form produced by infer_grants:
#   GRANT priv1, priv2, ... ON <grantor> TO <grantee> [WITH GRANT OPTION];
_GRANT_STMT_RE = re.compile(
    rf"""
    \bGRANT\b\s+
    (?P<privileges>.+?)
    \s+\bON\b\s+
    (?P<grantor>{_GRANT_IDENT})
    \s+\bTO\b\s+
    (?P<grantee>{_GRANT_IDENT})
    (?:\s+\bWITH\b\s+\bGRANT\b\s+\bOPTION\b)?
    \s*;
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def _normalise_privilege(text: str) -> str:
    """Collapse whitespace and uppercase a single privilege fragment."""
    return " ".join(text.upper().split())


def _split_privileges(privs_str: str) -> Set[str]:
    """
    Split a comma-separated privilege list into a set of canonical
    privilege strings.

    Handles multi-word privileges (e.g. "EXECUTE PROCEDURE") by
    canonicalising each comma-separated fragment. Unknown tokens are
    preserved verbatim (uppercased) so unexpected privileges still
    surface as drift rather than silently disappearing.
    """
    parts = [p.strip() for p in privs_str.split(",")]
    return {_normalise_privilege(p) for p in parts if p.strip()}


def _parse_grt_content(content: str) -> Dict[str, Set[str]]:
    """
    Parse a .grt file's content into ``{grantor: set_of_privileges}``.

    Comments are stripped before parsing so commented-out GRANTs are
    correctly ignored. Multiple GRANT statements with the same grantor
    are merged (union of privilege sets). The grantee is implicit from
    the file's filename context; the caller is responsible for that
    mapping.
    """
    grants: Dict[str, Set[str]] = {}
    cleaned = strip_sql_comments(content)

    for match in _GRANT_STMT_RE.finditer(cleaned):
        grantor = match.group("grantor").strip()
        privs = _split_privileges(match.group("privileges"))
        grants.setdefault(grantor, set()).update(privs)

    return grants


def _read_grt_file(path: Path) -> Optional[Dict[str, Set[str]]]:
    """
    Read and parse a .grt file. Returns None if the file cannot be
    read (does not exist, permission denied, encoding error).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read %s: %s", path, e)
        return None
    return _parse_grt_content(content)


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------


@dataclass
class GranteeStatus:
    """Per-grantee classification for one .grt file's worth of grants."""

    grantee: str
    file_path: Path

    # Status — exactly one of these is True
    consistent: bool = False
    drifted: bool = False
    missing: bool = False
    orphaned: bool = False

    # Populated for drifted/missing/orphaned cases
    expected_grants: Dict[str, Set[str]] = field(default_factory=dict)
    actual_grants: Dict[str, Set[str]] = field(default_factory=dict)

    # Detailed drift breakdown (only for drifted)
    missing_privs: Dict[str, Set[str]] = field(default_factory=dict)
    extra_privs: Dict[str, Set[str]] = field(default_factory=dict)


@dataclass
class GrantValidationResult:
    """
    Outcome of cross-file grant validation.

    Attributes:
        statuses:     Per-grantee classification (one entry per
                      grantee considered).
        project_dir:  Project root that was scanned.
        dcl_dir:      DCL directory that was checked / written to.
        ddl_count:    Number of DDL files contributing grants.
    """

    statuses: List[GranteeStatus] = field(default_factory=list)
    project_dir: Optional[Path] = None
    dcl_dir: Optional[Path] = None
    ddl_count: int = 0

    @property
    def consistent(self) -> List[GranteeStatus]:
        return [s for s in self.statuses if s.consistent]

    @property
    def drifted(self) -> List[GranteeStatus]:
        return [s for s in self.statuses if s.drifted]

    @property
    def missing(self) -> List[GranteeStatus]:
        return [s for s in self.statuses if s.missing]

    @property
    def orphaned(self) -> List[GranteeStatus]:
        return [s for s in self.statuses if s.orphaned]

    @property
    def passed(self) -> bool:
        """True iff every grantee is consistent (no drift, no missing,
        no orphans). Used by cli.py to set the overall exit code."""
        return all(s.consistent for s in self.statuses)


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------


def _compute_drift(
    expected: Dict[str, Set[str]],
    actual: Dict[str, Set[str]],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    Compare expected vs actual grants. Returns
    ``(missing_privs, extra_privs)`` — both keyed by grantor.

    missing_privs[grantor]: privileges in expected but not in actual.
    extra_privs[grantor]:   privileges in actual but not in expected.

    Both empty dicts ⇒ semantically identical.
    """
    missing: Dict[str, Set[str]] = {}
    extra: Dict[str, Set[str]] = {}

    all_grantors = set(expected) | set(actual)
    for grantor in all_grantors:
        e = expected.get(grantor, set())
        a = actual.get(grantor, set())
        if e - a:
            missing[grantor] = e - a
        if a - e:
            extra[grantor] = a - e

    return missing, extra


def _classify_grantee(
    grantee: str,
    expected: Dict[str, Set[str]],
    dcl_dir: Path,
) -> GranteeStatus:
    """
    Classify one grantee by comparing inferred grants against the
    persisted .grt file (if any).
    """
    file_path = dcl_dir / grantee_filename(grantee)
    status = GranteeStatus(
        grantee=grantee,
        file_path=file_path,
        expected_grants=expected,
    )

    if not file_path.exists():
        status.missing = True
        return status

    actual = _read_grt_file(file_path)
    if actual is None:
        # Treat unreadable file as missing — caller will overwrite
        status.missing = True
        return status

    status.actual_grants = actual
    missing_privs, extra_privs = _compute_drift(expected, actual)

    if not missing_privs and not extra_privs:
        status.consistent = True
    else:
        status.drifted = True
        status.missing_privs = missing_privs
        status.extra_privs = extra_privs

    return status


def _find_orphans(
    expected_grantees: Set[str],
    dcl_dir: Path,
) -> List[GranteeStatus]:
    """
    Identify .grt files in dcl_dir whose grantee is not in the
    inferred set. These are reported but not auto-deleted.
    """
    if not dcl_dir.is_dir():
        return []

    orphans: List[GranteeStatus] = []
    for entry in sorted(dcl_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() != ".grt":
            continue

        # Recover the grantee from the filename: '{{TOK}}.grt' → '{{TOK}}'
        grantee_from_filename = entry.stem
        if grantee_from_filename in expected_grantees:
            continue

        actual = _read_grt_file(entry) or {}
        orphans.append(
            GranteeStatus(
                grantee=grantee_from_filename,
                file_path=entry,
                orphaned=True,
                actual_grants=actual,
            )
        )
    return orphans


# ---------------------------------------------------------------------------
# Inference harness — the bridge to infer_grants.py
# ---------------------------------------------------------------------------


def _infer_expected_grants(
    project_dir: Path,
    verbose: bool = False,
) -> Tuple[Dict[str, Dict[str, Set[str]]], List[Dict], int]:
    """
    Run infer_grants over the project to produce the expected grants.

    Returns:
        (consolidated, raw_results, ddl_count) where:
            consolidated: {grantee: {grantor: set_of_privileges}}
            raw_results:  per-file analysis dicts (kept for fix-mode
                          source attribution in .grt headers)
            ddl_count:    number of DDL files contributing grants
    """
    ddl_files = find_ddl_files(project_dir)
    raw_results: List[Dict] = []
    for ddl_file in ddl_files:
        result = analyse_file(ddl_file, verbose=verbose)
        if result and result.get("grants"):
            raw_results.append(result)
    consolidated = consolidate_grants(raw_results) if raw_results else {}
    return consolidated, raw_results, len(raw_results)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
) -> GrantValidationResult:
    """
    Validate that persisted .grt files match the grants implied by
    the project's DDL.

    Read-only — no files are written. Use ``fix_grants`` to repair
    drift.

    Args:
        project_dir: Root of the SHIPS project to scan.
        dcl_dir:     Directory containing .grt files. Defaults to
                     ``project_dir/payload/database/DCL/inter_db``.
        verbose:     Forwarded to infer_grants for diagnostic output.

    Returns:
        GrantValidationResult with per-grantee classifications.
        ``result.passed`` is True iff every grantee is consistent.
    """
    project_dir = Path(project_dir).resolve()
    dcl_dir = _resolve_dcl_dir(project_dir, dcl_dir)

    consolidated, _raw, ddl_count = _infer_expected_grants(project_dir, verbose)

    result = GrantValidationResult(
        project_dir=project_dir,
        dcl_dir=dcl_dir,
        ddl_count=ddl_count,
    )

    # Classify each inferred grantee
    for grantee in sorted(consolidated.keys()):
        expected = consolidated[grantee]
        status = _classify_grantee(grantee, expected, dcl_dir)
        result.statuses.append(status)

    # Detect orphans — .grt files with no DDL backing
    orphans = _find_orphans(set(consolidated.keys()), dcl_dir)
    result.statuses.extend(orphans)

    return result


def fix_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
) -> Tuple[GrantValidationResult, int]:
    """
    Repair grant drift by writing expected .grt files for every
    missing or drifted grantee. Orphaned files are reported but NOT
    deleted.

    Args:
        project_dir: Root of the SHIPS project to scan.
        dcl_dir:     Directory containing .grt files. Defaults to
                     ``project_dir/payload/database/DCL/inter_db``.
        verbose:     Forwarded to infer_grants for diagnostic output.

    Returns:
        ``(result, files_written)`` where:
            result:         The post-fix GrantValidationResult — drifted
                            and missing entries have been re-classified
                            as consistent (since they were just written).
            files_written:  Count of .grt files actually written.
    """
    project_dir = Path(project_dir).resolve()
    dcl_dir = _resolve_dcl_dir(project_dir, dcl_dir)

    consolidated, raw_results, ddl_count = _infer_expected_grants(project_dir, verbose)

    project_name = project_dir.name
    files_written = 0
    statuses: List[GranteeStatus] = []

    # Ensure target directory exists before any writes
    if consolidated:
        dcl_dir.mkdir(parents=True, exist_ok=True)

    for grantee in sorted(consolidated.keys()):
        expected = consolidated[grantee]
        pre_status = _classify_grantee(grantee, expected, dcl_dir)

        if pre_status.consistent:
            statuses.append(pre_status)
            continue

        # Drift or missing → write the expected file
        sources = [r for r in raw_results if r["grantee"] == grantee]
        content = generate_grt_content(grantee, expected, sources, project_name)
        pre_status.file_path.write_text(content, encoding="utf-8")
        files_written += 1

        # Mark as consistent in the post-fix result
        post_status = GranteeStatus(
            grantee=grantee,
            file_path=pre_status.file_path,
            consistent=True,
            expected_grants=expected,
            actual_grants=expected,
        )
        statuses.append(post_status)

    # Orphans are not touched — surface them in the result
    orphans = _find_orphans(set(consolidated.keys()), dcl_dir)
    statuses.extend(orphans)

    result = GrantValidationResult(
        statuses=statuses,
        project_dir=project_dir,
        dcl_dir=dcl_dir,
        ddl_count=ddl_count,
    )
    return result, files_written


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_priv_set(privs: Set[str]) -> str:
    """Format a privilege set in canonical order for human display."""
    ordered = sorted(
        privs,
        key=lambda p: PRIV_ORDER.index(p) if p in PRIV_ORDER else 999,
    )
    return ", ".join(ordered)


def _format_grants_block(
    grants: Dict[str, Set[str]],
    indent: str = "      ",
) -> str:
    """Format a grantor → privileges dict as readable lines."""
    if not grants:
        return f"{indent}(none)"
    lines = []
    for grantor in sorted(grants):
        lines.append(f"{indent}{grantor}: {_format_priv_set(grants[grantor])}")
    return "\n".join(lines)


def format_report(result: GrantValidationResult) -> str:
    """
    Render a human-readable summary of a GrantValidationResult.

    Used by cli.py for both validate and fix flows. Produces a
    multi-line string suitable for terminal output.
    """
    lines: List[str] = []

    total = len(result.statuses)
    consistent_n = len(result.consistent)
    drifted_n = len(result.drifted)
    missing_n = len(result.missing)
    orphaned_n = len(result.orphaned)

    lines.append(
        f"  Grantees: {total} total — "
        f"{consistent_n} consistent, "
        f"{drifted_n} drifted, "
        f"{missing_n} missing, "
        f"{orphaned_n} orphaned"
    )
    lines.append(f"  DDL files contributing grants: {result.ddl_count}")

    if total == 0:
        lines.append("")
        lines.append("  No cross-database grants inferred from this project.")
        return "\n".join(lines)

    # Per-grantee detail
    for status in result.statuses:
        if status.consistent:
            lines.append(f"\n  ✓ {status.grantee}: clean")
        elif status.drifted:
            lines.append(f"\n  ✗ {status.grantee}: drift detected")
            lines.append(f"      File: {status.file_path}")
            if status.missing_privs:
                lines.append("      Missing from .grt file:")
                lines.append(_format_grants_block(status.missing_privs))
            if status.extra_privs:
                lines.append("      Extra in .grt file (not implied by DDL):")
                lines.append(_format_grants_block(status.extra_privs))
        elif status.missing:
            lines.append(f"\n  ! {status.grantee}: missing .grt file")
            lines.append(f"      Expected at: {status.file_path}")
            lines.append("      Inferred grants:")
            lines.append(_format_grants_block(status.expected_grants))
        elif status.orphaned:
            lines.append(f"\n  ⚠ {status.grantee}: orphaned .grt file")
            lines.append(f"      File:    {status.file_path}")
            lines.append(
                "      No DDL in this project implies grants for this "
                "grantee. Review and remove manually if no longer needed."
            )

    return "\n".join(lines)
