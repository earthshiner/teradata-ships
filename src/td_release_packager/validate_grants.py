"""
validate_grants.py — Cross-file grant validation orchestrator.

This module is Step 2 of the SHIPS validate command. Step 1 (per-file
DDL lint) lives in ``validate.py``; this module compares the grants
*implied* by the DDL across an entire project against the *persisted*
``.dcl`` files in the project's DCL/inter_db directory, and reports
or repairs any drift.

The heavy lifting — parsing DDL, extracting cross-database references,
deciding which privileges each reference implies, and emitting .dcl
content — is delegated to ``infer_grants.py``. This module is a thin
orchestrator that:

    1. Calls infer_grants to derive the *expected* grants per grantee.
    2. Reads the *actual* .dcl files persisted in dcl_dir.
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
    Two .dcl files are considered EQUIVALENT if they contain the same
    set of (grantor, privilege) pairs after parsing — formatting,
    comment headers, and statement ordering are ignored. This means
    manual edits that preserve the underlying grant set don't trigger
    drift, but manual additions/removals of privileges do.

Orphan policy:
    A .dcl file in dcl_dir whose grantee is not present in the DDL
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
    build_view_dependency_index,
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

# Where database-to-database grant scripts live by default. DCL is
# split by grantee class: roles/, users/, and inter_db/.
_DEFAULT_DCL_SUBPATH = ("payload", "database", "DCL", "inter_db")
_DEFAULT_ROLE_DCL_SUBPATH = ("payload", "database", "DCL", "roles")
_DEFAULT_USER_DCL_SUBPATH = ("payload", "database", "DCL", "users")


def _resolve_dcl_dir(project_dir: Path, dcl_dir: Optional[Path]) -> Path:
    """Resolve the DCL directory, applying the project default if None."""
    if dcl_dir is not None:
        return dcl_dir
    return project_dir.joinpath(*_DEFAULT_DCL_SUBPATH)


def _resolve_role_dcl_dir(project_dir: Path) -> Path:
    """Resolve the role grants directory."""
    return project_dir.joinpath(*_DEFAULT_ROLE_DCL_SUBPATH)


def _resolve_user_dcl_dir(project_dir: Path) -> Path:
    """Resolve the user grants directory."""
    return project_dir.joinpath(*_DEFAULT_USER_DCL_SUBPATH)


# ---------------------------------------------------------------------------
# GRANT statement parsing — for reading existing .dcl files
# ---------------------------------------------------------------------------

# Identifier shape — accepts tokens, quoted ids, and bare ids.
_GRANT_IDENT = r'(?:\{\{[A-Za-z_]\w*\}\}(?:[A-Za-z_]\w*)?|"[^"]+"|[A-Za-z_]\w*)'

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


def _parse_grt_content(
    content: str,
    expected_grantee: Optional[str] = None,
) -> Dict[str, Set[str]]:
    """
    Parse a .dcl file's content into ``{grantor: set_of_privileges}``.

    Comments are stripped before parsing so commented-out GRANTs are
    correctly ignored. Multiple GRANT statements with the same grantor
    are merged (union of privilege sets). Unknown privileges are preserved
    so they surface as drift rather than being silently discarded.

    When ``expected_grantee`` is supplied, only statements whose actual
    ``TO`` grantee matches that value are included. This keeps
    ``DCL/inter_db`` strict: a file named ``{{DB_X_V}}.dcl`` may only
    satisfy grants whose SQL says ``TO {{DB_X_V}}``. Misplaced role grants
    or grants to a different user/database are ignored for inter-db drift
    comparison and are therefore not treated as valid database-to-database
    grants.
    """
    grants: Dict[str, Set[str]] = {}
    expected_key = (
        _normalise_identifier(expected_grantee)
        if expected_grantee is not None
        else None
    )

    for grantor, grantee, privs in _iter_grant_statements(content):
        if expected_key is not None:
            actual_key = _normalise_identifier(grantee)
            if actual_key != expected_key:
                continue
        grants.setdefault(grantor, set()).update(privs)

    return grants


def _iter_grant_statements(content: str) -> List[Tuple[str, str, Set[str]]]:
    """Return ``(grantor, grantee, privileges)`` tuples from DCL content."""
    cleaned = strip_sql_comments(content)
    statements: List[Tuple[str, str, Set[str]]] = []
    for match in _GRANT_STMT_RE.finditer(cleaned):
        statements.append(
            (
                match.group("grantor").strip(),
                match.group("grantee").strip(),
                _split_privileges(match.group("privileges")),
            )
        )
    return statements


def _read_grt_file(
    path: Path,
    expected_grantee: Optional[str] = None,
) -> Optional[Dict[str, Set[str]]]:
    """
    Read and parse a .dcl file. Returns None if the file cannot be read.

    Supply ``expected_grantee`` when reading ``DCL/inter_db`` files so the
    parser only counts statements whose actual ``TO`` grantee matches the
    filename-derived grantee. This prevents role grants from being counted
    as inter-database grants.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read %s: %s", path, e)
        return None
    return _parse_grt_content(content, expected_grantee=expected_grantee)


def _normalise_identifier(identifier: str) -> str:
    """Normalise an identifier for filename/content comparisons."""
    text = identifier.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.upper()


def _is_role_identifier(identifier: str) -> bool:
    """True when a grant target is clearly a role name."""
    return _normalise_identifier(identifier).endswith("_ROLE")


def _role_grantees_in_file(path: Path) -> Set[str]:
    """Return role grantees referenced by a DCL file."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read %s: %s", path, e)
        return set()
    return {
        grantee
        for _grantor, grantee, _privs in _iter_grant_statements(content)
        if _is_role_identifier(grantee)
    }


def _is_database_role_grant_file(path: Path) -> bool:
    """
    True when a DCL file grants the database named by the file to roles.

    These files are intentional package artefacts, commonly used for
    READ/WRITE role access to a package database, so they are not
    orphaned merely because no DDL object owner implies them.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Cannot read %s: %s", path, e)
        return False

    statements = _iter_grant_statements(content)
    if not statements:
        return False

    file_grantor = _normalise_identifier(path.stem)
    return all(
        _normalise_identifier(grantor) == file_grantor
        and _is_role_identifier(grantee)
        for grantor, grantee, _privs in statements
    )


def _write_missing_role_files(project_dir: Path) -> int:
    """Create missing role DDL for role grantees found in DCL files."""
    role_dcl_dir = _resolve_role_dcl_dir(project_dir)
    user_dcl_dir = _resolve_user_dcl_dir(project_dir)
    dcl_dirs = [path for path in (role_dcl_dir, user_dcl_dir) if path.is_dir()]
    if not dcl_dirs:
        return 0

    roles: Set[str] = set()
    for dcl_dir in dcl_dirs:
        for entry in sorted(dcl_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() in {".dcl", ".grt"}:
                roles.update(_role_grantees_in_file(entry))

    if not roles:
        return 0

    role_dir = project_dir / "payload" / "database" / "system" / "roles"
    role_dir.mkdir(parents=True, exist_ok=True)

    files_written = 0
    for role in sorted(roles, key=_normalise_identifier):
        role_file = role_dir / f"{role}.rol"
        if role_file.exists():
            continue
        role_file.write_text(f"CREATE ROLE {role};\n", encoding="utf-8")
        files_written += 1

    return files_written


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------


@dataclass
class GranteeStatus:
    """Per-grantee classification for one .dcl file's worth of grants."""

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
    def drifted_extra_only(self) -> List[GranteeStatus]:
        """
        Drifted grantees whose .dcl file contains only *extra* privileges
        — grants present in the file but not implied by the DDL.
        There are no missing privileges (i.e. every grant SHIPS inferred
        is already present in the file).

        These are typically intentional manual additions: grants to roles,
        reporting users, or external consumers that SHIPS cannot infer from
        DDL alone.  They are safe to downgrade to warnings when
        ``warn_extra_grants`` is enabled in ships.yaml.
        """
        return [
            s for s in self.statuses
            if s.drifted and s.extra_privs and not s.missing_privs
        ]

    @property
    def drifted_missing_privs(self) -> List[GranteeStatus]:
        """
        Drifted grantees that have at least one *missing* privilege —
        a grant that SHIPS inferred from the DDL but is absent from the
        .dcl file.  These are always hard errors regardless of any
        warn_* setting, because the DDL is referencing access that has
        not been granted.
        """
        return [s for s in self.statuses if s.drifted and s.missing_privs]

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

    def passed_ignoring_orphans(self) -> bool:
        """
        True iff every non-orphaned grantee is consistent.

        Use this when ``warn_orphan_grants`` is enabled in ships.yaml —
        orphaned DCL files are reported as warnings rather than errors,
        so they must not block packaging.
        """
        return all(s.consistent for s in self.statuses if not s.orphaned)

    def passed_ignoring_extra_grants(self) -> bool:
        """
        True iff no grantee has missing privileges and no grantee is
        missing its .dcl file entirely.

        Use this when ``warn_extra_grants`` is enabled in ships.yaml.
        Grantees whose .dcl files contain only extra privileges (grants
        you added manually that SHIPS did not infer) are downgraded to
        warnings and do not block packaging.  Grantees with missing
        privileges (the DDL implies a grant that is absent from the
        .dcl file) remain hard errors.
        """
        for s in self.statuses:
            if s.missing:
                return False
            if s.drifted and s.missing_privs:
                return False
        return True

    def passed_ignoring_extra_grants_and_orphans(self) -> bool:
        """
        Combined check for when both ``warn_extra_grants`` and
        ``warn_orphan_grants`` are enabled in ships.yaml.

        Only missing .dcl files and drifted grantees that have missing
        privileges cause a hard failure.
        """
        for s in self.statuses:
            if s.missing:
                return False
            if s.drifted and s.missing_privs:
                return False
        return True


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
    persisted .dcl file (if any).
    """
    file_path = _grant_file_path(dcl_dir, grantee)
    status = GranteeStatus(
        grantee=grantee,
        file_path=file_path,
        expected_grants=expected,
    )

    if not file_path.exists():
        status.missing = True
        return status

    actual = _read_grt_file(file_path, expected_grantee=grantee)
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


def _grant_file_path(dcl_dir: Path, grantee: str) -> Path:
    """
    Resolve a grantee grant file, accepting canonical .dcl and legacy .grt.

    ``.dcl`` is the preferred extension for DCL scripts. Older view-layer
    generation emitted ``.grt``; accepting both keeps existing packages
    valid while new fixes can write ``.dcl``.
    """
    dcl_path = dcl_dir / grantee_filename(grantee)
    grt_path = dcl_path.with_suffix(".grt")
    if dcl_path.exists():
        return dcl_path
    if grt_path.exists():
        return grt_path
    return dcl_path


def _find_orphans(
    expected_grantees: Set[str],
    dcl_dir: Path,
) -> List[GranteeStatus]:
    """
    Identify persisted inter-db DCL grant files whose grantee is not in
    the inferred database-grantee set.
    """
    if not dcl_dir.is_dir():
        return []

    orphans: List[GranteeStatus] = []
    for entry in sorted(dcl_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in {".dcl", ".grt"}:
            continue

        # Recover the grantee from the filename: '{{TOK}}.dcl' → '{{TOK}}'
        grantee_from_filename = entry.stem
        if grantee_from_filename in expected_grantees:
            continue

        actual = _read_grt_file(entry, expected_grantee=grantee_from_filename) or {}
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
                          source attribution in .dcl headers)
            ddl_count:    number of DDL files contributing grants
    """
    ddl_files = find_ddl_files(project_dir)
    view_dependency_index = build_view_dependency_index(project_dir)
    raw_results: List[Dict] = []
    for ddl_file in ddl_files:
        result = analyse_file(
            ddl_file,
            verbose=verbose,
            view_dependency_index=view_dependency_index,
        )
        if result and result.get("grants"):
            raw_results.append(result)
            for passthrough_grantee, grants in result.get(
                "passthrough_grants",
                {},
            ).items():
                raw_results.append(
                    {
                        "file": result["file"],
                        "grantee": passthrough_grantee,
                        "obj_type": f"{result['obj_type']}_VIEW_PASSTHROUGH",
                        "obj_name": result["obj_name"],
                        "grants": grants,
                    }
                )
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
    Validate that persisted .dcl files match the grants implied by
    the project's DDL.

    Read-only — no files are written. Use ``fix_grants`` to repair
    drift.

    Args:
        project_dir: Root of the SHIPS project to scan.
        dcl_dir:     Directory containing .dcl files. Defaults to
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

    # Detect orphans — persisted DCL grant files with no DDL backing
    orphans = _find_orphans(set(consolidated.keys()), dcl_dir)
    result.statuses.extend(orphans)

    return result


def fix_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
) -> Tuple[GrantValidationResult, int]:
    """
    Repair grant drift by writing expected .dcl files for every
    missing or drifted grantee, and creating missing role DDL for
    role grantees referenced by existing DCL files. Orphaned files
    are reported but NOT deleted.

    Args:
        project_dir: Root of the SHIPS project to scan.
        dcl_dir:     Directory containing .dcl files. Defaults to
                     ``project_dir/payload/database/DCL/inter_db``.
        verbose:     Forwarded to infer_grants for diagnostic output.

    Returns:
        ``(result, files_written)`` where:
            result:         The post-fix GrantValidationResult — drifted
                            and missing entries have been re-classified
                            as consistent (since they were just written).
            files_written:  Count of .dcl files actually written.
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

    files_written += _write_missing_role_files(project_dir)

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


def _grant_statement_requires_grant_option(grantee: str) -> bool:
    """Return True when the generated GRANT statement may use grant option."""
    return not _is_role_identifier(grantee)


def _format_missing_grant_statements(
    grantee: str,
    grants: Dict[str, Set[str]],
    indent: str = "      ",
) -> str:
    """
    Format missing inferred grants as executable Teradata GRANT statements.

    Role grants never include ``WITH GRANT OPTION`` because Teradata does
    not allow grant option when granting privileges to a role. Inter-db
    grants keep grant option so a view/database can pass access through to
    downstream consumers.
    """
    if not grants:
        return f"{indent}(none)"

    suffix = " WITH GRANT OPTION" if _grant_statement_requires_grant_option(grantee) else ""
    lines: List[str] = []
    for grantor in sorted(grants):
        privileges = sorted(
            grants[grantor],
            key=lambda p: PRIV_ORDER.index(p) if p in PRIV_ORDER else 999,
        )
        for privilege in privileges:
            lines.append(
                f"{indent}GRANT {privilege} ON {grantor} TO {grantee}{suffix};"
            )
    return "\n".join(lines)


def format_report(
    result: GrantValidationResult,
    extra_grants_severity: str = "ERROR",
    orphan_grants_severity: str = "ERROR",
) -> str:
    """
    Render a human-readable summary of a GrantValidationResult.

    Used by cli.py for both validate and fix flows. Produces a
    multi-line string suitable for terminal output. Severity arguments
    control which configured non-blocking grant findings are visible:

    * extra_grants_severity=OFF suppresses extra-only drift entries.
    * orphan_grants_severity=OFF suppresses orphaned DCL entries.

    Missing inferred privileges are always shown because they are
    package-blocking errors.
    """
    extra_grants_severity = extra_grants_severity.upper()
    orphan_grants_severity = orphan_grants_severity.upper()

    def _should_show(status: GranteeStatus) -> bool:
        if status.orphaned and orphan_grants_severity == "OFF":
            return False
        if (
            status.drifted
            and status.extra_privs
            and not status.missing_privs
            and extra_grants_severity == "OFF"
        ):
            return False
        return True

    visible_statuses = [s for s in result.statuses if _should_show(s)]

    total = len(visible_statuses)
    consistent_n = len([s for s in visible_statuses if s.consistent])
    drifted_n = len([s for s in visible_statuses if s.drifted])
    missing_n = len([s for s in visible_statuses if s.missing])
    orphaned_n = len([s for s in visible_statuses if s.orphaned])

    lines: List[str] = []

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
        lines.append("  No grant findings are visible with the current inspect.conf settings.")
        return "\n".join(lines)

    # Per-grantee detail
    for status in visible_statuses:
        if status.consistent:
            lines.append(f"\n  ✓ {status.grantee}: clean")
        elif status.drifted:
            lines.append(f"\n  ✗ {status.grantee}: drift detected")
            lines.append(f"      File: {status.file_path}")
            if status.missing_privs:
                lines.append("      Required grant missing from .dcl file:")
                lines.append(
                    _format_missing_grant_statements(
                        status.grantee,
                        status.missing_privs,
                    )
                )
            if status.extra_privs and extra_grants_severity != "OFF":
                lines.append("      Extra in .dcl file (not implied by DDL):")
                lines.append(_format_grants_block(status.extra_privs))
        elif status.missing:
            lines.append(f"\n  ! {status.grantee}: missing .dcl file")
            lines.append(f"      Expected at: {status.file_path}")
            lines.append("      Inferred grants:")
            lines.append(_format_grants_block(status.expected_grants))
        elif status.orphaned:
            lines.append(f"\n  ⚠ {status.grantee}: orphaned .dcl file")
            lines.append(f"      File:    {status.file_path}")
            lines.append(
                "      No DDL in this project implies grants for this "
                "grantee. Review and remove manually if no longer needed."
            )

    return "\n".join(lines)
