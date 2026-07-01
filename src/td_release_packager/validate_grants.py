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
    4. Optionally adds missing inferred grants (fix mode).

Public API (matches what cli.py imports):

    validate_grants(project_dir, dcl_dir=None, verbose=False)
        Read-only audit. Returns a GrantValidationResult.

    fix_grants(project_dir, dcl_dir=None, verbose=False)
        Adds missing inferred grants to the correct DCL file. Missing
        files are created; existing drifted files are repaired
        additively. Extra and external grants are never removed.
        Returns (GrantValidationResult, files_written: int).

    format_report(result)
        Human-readable summary string.

Drift semantics:
    Two .dcl files are considered EQUIVALENT if they contain the same
    set of (grantor, privilege) pairs after parsing — formatting,
    comment headers, and statement ordering are ignored. This means
    manual edits that preserve the underlying grant set don't trigger
    drift, but manual additions/removals of privileges do.

External-grant policy:
    A .dcl file in dcl_dir whose grantee is not present in the DDL
    inference is reported as an EXTERNAL grant (renamed from
    ORPHANED — the grantee is external to the package's intent
    rather than orphaned by it). External grants are NEVER auto-
    deleted in fix mode and surface at INFO by default, because
    they are commonly legitimate (e.g. roles whose
    ``GRANT ROLE … TO USER`` lives outside the package). Internal
    data-model fields and helper names (``.orphaned``,
    ``_find_orphans``, ``passed_ignoring_orphans``) retain the
    historic naming to avoid a wide refactor; only user-facing
    surfaces use "external".
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


def _mask_sql_comments_preserving_length(sql: str) -> str:
    """Mask SQL comments with whitespace while preserving string length.

    This lets grant statement regex match spans be mapped back to the
    original text for safe statement relocation/removal. Newlines are
    preserved so the surrounding file layout remains stable.
    """

    def _blank(match: re.Match) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    masked = re.sub(r"/\*.*?\*/", _blank, sql, flags=re.DOTALL)
    masked = re.sub(r"--[^\n]*", _blank, masked)
    return masked


def _iter_grant_statement_matches(content: str) -> List[re.Match]:
    """Return regex matches for executable GRANT statements in DCL content."""
    return list(_GRANT_STMT_RE.finditer(_mask_sql_comments_preserving_length(content)))


def _iter_grant_statements(content: str) -> List[Tuple[str, str, Set[str]]]:
    """Return ``(grantor, grantee, privileges)`` tuples from DCL content."""
    statements: List[Tuple[str, str, Set[str]]] = []
    for match in _iter_grant_statement_matches(content):
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
    """True when a grant target is clearly a role name.

    Handles both literal role names, such as ``BIONIC_READ_ROLE``, and
    tokenised role names, such as ``{{READ_ROLE}}``.
    """
    normalised = _normalise_identifier(identifier)
    if normalised.startswith("{{") and normalised.endswith("}}"):
        normalised = normalised[2:-2]
    return normalised.endswith("_ROLE")


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
        _normalise_identifier(grantor) == file_grantor and _is_role_identifier(grantee)
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
            s
            for s in self.statuses
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

        Use this when ``warn_external_grants`` is enabled in ships.yaml —
        external-grantee DCL files are reported as warnings rather than errors,
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
        ``warn_external_grants`` are enabled in ships.yaml.

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


def _subtract_grants(
    required: Dict[str, Set[str]],
    actual: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
    """Return grant privileges present in required but absent from actual."""
    missing: Dict[str, Set[str]] = {}
    for grantor, privileges in required.items():
        absent = set(privileges) - actual.get(grantor, set())
        if absent:
            missing[grantor] = absent
    return missing


def _collapse_excess_blank_lines(content: str) -> str:
    """Keep relocated DCL readable after statement removal."""
    return re.sub(r"\n{3,}", "\n\n", content).strip() + "\n"


def _remove_spans(content: str, spans: List[Tuple[int, int]]) -> str:
    """Remove non-overlapping spans from content, processing from the end."""
    updated = content
    for start, end in sorted(spans, reverse=True):
        line_start = updated.rfind("\n", 0, start) + 1
        line_end = updated.find("\n", end)
        if line_end == -1:
            line_end = len(updated)
        else:
            line_end += 1
        prefix = updated[line_start:start]
        # If the statement occupied the whole physical line, remove the line.
        if not prefix.strip():
            start, end = line_start, line_end
        updated = updated[:start] + updated[end:]
    return _collapse_excess_blank_lines(updated)


def _relocate_role_grants_from_inter_db(
    project_dir: Path,
    inter_db_dcl_dir: Path,
) -> int:
    """Move role-targeted GRANT statements from DCL/inter_db to DCL/roles.

    Earlier SHIPS builds could leave GRANT ... TO <role> statements under
    ``DCL/inter_db``.  That folder is reserved for database-to-database
    grants.  In fix mode, relocation is safe because the statement itself
    identifies a role grantee and Teradata role grants must not include
    ``WITH GRANT OPTION``.
    """
    if not inter_db_dcl_dir.is_dir():
        return 0

    role_dcl_dir = _resolve_role_dcl_dir(project_dir)
    files_written = 0

    for entry in sorted(inter_db_dcl_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in {".dcl", ".grt"}:
            continue

        try:
            content = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Cannot read %s: %s", entry, e)
            continue

        grants_by_role: Dict[str, Dict[str, Set[str]]] = {}
        spans_to_remove: List[Tuple[int, int]] = []

        for match in _iter_grant_statement_matches(content):
            grantor = match.group("grantor").strip()
            grantee = match.group("grantee").strip()
            if not _is_role_identifier(grantee):
                continue

            grants_by_role.setdefault(grantee, {}).setdefault(grantor, set()).update(
                _split_privileges(match.group("privileges"))
            )
            spans_to_remove.append(match.span())

        if not grants_by_role:
            continue

        for role, grants in sorted(
            grants_by_role.items(),
            key=lambda item: _normalise_identifier(item[0]),
        ):
            role_file = role_dcl_dir / grantee_filename(role)
            actual = (
                _read_grt_file(role_file, expected_grantee=role)
                if role_file.exists()
                else {}
            ) or {}
            missing = _subtract_grants(grants, actual)
            if not missing:
                continue

            if role_file.exists():
                if _append_missing_grants_to_file(role_file, role, missing):
                    files_written += 1
            else:
                if _write_full_expected_grants_file(
                    role_file,
                    role,
                    missing,
                    [],
                    project_dir.name,
                ):
                    files_written += 1

        updated_content = _remove_spans(content, spans_to_remove)
        if updated_content != content:
            entry.write_text(updated_content, encoding="utf-8")
            files_written += 1

    return files_written


def _target_dcl_dir_for_grantee(
    project_dir: Path,
    default_inter_db_dcl_dir: Path,
    grantee: str,
) -> Path:
    """Resolve the DCL directory that owns grants for ``grantee``.

    Inferred database-to-database grants are written under ``DCL/inter_db``.
    Role grants, if ever supplied to this repair path, are written under
    ``DCL/roles`` and must not use ``WITH GRANT OPTION``.
    """
    if _is_role_identifier(grantee):
        return _resolve_role_dcl_dir(project_dir)
    return default_inter_db_dcl_dir


def _write_full_expected_grants_file(
    file_path: Path,
    grantee: str,
    expected: Dict[str, Set[str]],
    sources: List[Dict],
    project_name: str,
) -> bool:
    """Create or replace a missing/unreadable DCL file from expected grants."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = generate_grt_content(grantee, expected, sources, project_name)

    if not _grant_statement_requires_grant_option(grantee):
        content = content.replace(" WITH GRANT OPTION;", ";")

    existing = file_path.read_text(encoding="utf-8") if file_path.exists() else None
    if existing == content:
        return False

    file_path.write_text(content, encoding="utf-8")
    return True


def _render_missing_grant_append_block(
    grantee: str,
    missing_grants: Dict[str, Set[str]],
) -> str:
    """Render only the missing grants as appendable DCL statements."""
    statements = _format_missing_grant_statements(
        grantee,
        missing_grants,
        indent="",
    )
    if not statements.strip():
        return ""

    return "\n".join(
        [
            "",
            "/* SHIPS --fix-grants: missing inferred grants appended */",
            statements,
            "",
        ]
    )


def _append_missing_grants_to_file(
    file_path: Path,
    grantee: str,
    missing_grants: Dict[str, Set[str]],
) -> bool:
    """Append missing inferred grants to an existing DCL file.

    Existing content is preserved. Extra grants are not removed or moved;
    they remain visible to normal validation unless suppressed by
    inspect.conf severity settings.
    """
    if not missing_grants:
        return False

    file_path.parent.mkdir(parents=True, exist_ok=True)
    existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    block = _render_missing_grant_append_block(grantee, missing_grants)
    if not block:
        return False

    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    file_path.write_text(f"{existing}{separator}{block}", encoding="utf-8")
    return True


def fix_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
    dry_run: bool = False,
) -> Tuple[GrantValidationResult, int]:
    """
    Repair grant drift by adding missing inferred grants.

    Missing DCL files are created from the inferred grant set. Existing
    drifted DCL files are repaired additively by appending only missing
    inferred grants. Extra grants and orphaned files are deliberately left
    untouched for human review. Role grants, if routed through this path,
    are written under ``DCL/roles`` and never include ``WITH GRANT OPTION``.

    Args:
        project_dir: Root of the SHIPS project to scan.
        dcl_dir:     Directory containing inter-db .dcl files. Defaults to
                     ``project_dir/payload/database/DCL/inter_db``.
        verbose:     Forwarded to infer_grants for diagnostic output.
        dry_run:     When True, count what *would* be written without
                     touching the filesystem. The returned
                     ``files_written`` reports the projected total. The
                     status classification runs identically (no writes
                     occur, so post-fix status equals pre-fix status);
                     this is the shape the ``grants_derivation`` fixer
                     needs for the ``ships fix`` verb (#526).

    Returns:
        ``(result, files_written)`` where:
            result:         The post-fix GrantValidationResult. If a file
                            also contains extra grants, it may still be
                            reported as drifted after missing grants are
                            appended.
            files_written:  Count of DCL/role DDL files created or updated
                            (or projected under ``dry_run``).
    """
    project_dir = Path(project_dir).resolve()
    dcl_dir = _resolve_dcl_dir(project_dir, dcl_dir)

    # Relocations and role-file creations touch disk unconditionally.
    # Under ``dry_run`` we skip both, at the cost of not counting them —
    # the vast majority of ``ships fix`` grants dry-runs surface the
    # projections that matter (missing DCL files, drifted files with
    # missing privs), and the relocate/role-file paths are legacy /
    # cleanup passes that the operator typically sees once.
    if not dry_run:
        files_written = _relocate_role_grants_from_inter_db(project_dir, dcl_dir)
    else:
        files_written = 0

    consolidated, raw_results, ddl_count = _infer_expected_grants(project_dir, verbose)

    project_name = project_dir.name
    statuses: List[GranteeStatus] = []

    for grantee in sorted(consolidated.keys()):
        expected = consolidated[grantee]
        target_dcl_dir = _target_dcl_dir_for_grantee(project_dir, dcl_dir, grantee)
        pre_status = _classify_grantee(grantee, expected, target_dcl_dir)

        if pre_status.consistent:
            statuses.append(pre_status)
            continue

        sources = [r for r in raw_results if r["grantee"] == grantee]

        if pre_status.missing:
            if dry_run:
                # A missing DCL file will always be created — one write
                # per grantee. Count without invoking the writer.
                files_written += 1
            elif _write_full_expected_grants_file(
                pre_status.file_path,
                grantee,
                expected,
                sources,
                project_name,
            ):
                files_written += 1
        elif pre_status.drifted and pre_status.missing_privs:
            if dry_run:
                files_written += 1
            elif _append_missing_grants_to_file(
                pre_status.file_path,
                grantee,
                pre_status.missing_privs,
            ):
                files_written += 1

        # Under dry_run there was no write, so the post-fix status is
        # identical to the pre-fix classification.
        if dry_run:
            statuses.append(pre_status)
        else:
            statuses.append(_classify_grantee(grantee, expected, target_dcl_dir))

    if not dry_run:
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


def _render_grant_statement(
    grantor: str,
    grantee: str,
    privileges: Set[str],
    with_grant_option: bool,
) -> str:
    """Render a canonical Teradata GRANT statement."""
    ordered = sorted(
        privileges,
        key=lambda p: PRIV_ORDER.index(p) if p in PRIV_ORDER else 999,
    )
    privilege_text = ", ".join(ordered)
    suffix = " WITH GRANT OPTION" if with_grant_option else ""
    return f"GRANT {privilege_text} ON {grantor} TO {grantee}{suffix};"


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

    with_grant_option = _grant_statement_requires_grant_option(grantee)
    lines: List[str] = []
    for grantor in sorted(grants):
        for privilege in sorted(
            grants[grantor],
            key=lambda p: PRIV_ORDER.index(p) if p in PRIV_ORDER else 999,
        ):
            lines.append(
                f"{indent}"
                f"{_render_grant_statement(grantor, grantee, {privilege}, with_grant_option)}"
            )
    return "\n".join(lines)


def format_report(
    result: GrantValidationResult,
    extra_grants_severity: str = "ERROR",
    external_grants_severity: str = "INFO",
) -> str:
    """
    Render a human-readable summary of a GrantValidationResult.

    Used by cli.py for both validate and fix flows. Produces a
    multi-line string suitable for terminal output. Severity arguments
    control which configured non-blocking grant findings are visible:

    * extra_grants_severity=OFF suppresses extra-only drift entries.
    * external_grants_severity=OFF suppresses external-grantee .dcl
      entries (.dcl files for grantees that no DDL in the package
      implies).

    Missing inferred privileges are always shown because they are
    package-blocking errors.
    """
    extra_grants_severity = extra_grants_severity.upper()
    external_grants_severity = external_grants_severity.upper()

    def _should_show(status: GranteeStatus) -> bool:
        # Internally still .orphaned on the dataclass; user-facing
        # vocabulary is "external grant" — see the warn_external_grants
        # rule for the rationale.
        if status.orphaned and external_grants_severity == "OFF":
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
    external_n = len([s for s in visible_statuses if s.orphaned])

    lines: List[str] = []

    lines.append(
        f"  Grantees: {total} total — "
        f"{consistent_n} consistent, "
        f"{drifted_n} drifted, "
        f"{missing_n} missing, "
        f"{external_n} external"
    )
    lines.append(f"  DDL files contributing grants: {result.ddl_count}")

    if total == 0:
        lines.append("")
        if result.statuses:
            lines.append(
                "  No grant findings are visible with the current inspect.conf settings."
            )
        else:
            lines.append("  No cross-database grants inferred from this project.")
        return "\n".join(lines)

    # Per-grantee detail
    for status in visible_statuses:
        if status.consistent:
            lines.append(f"\n  ✓ {status.grantee}: clean")
        elif status.drifted:
            lines.append(f"\n  ✗ {status.grantee}: drift detected")
            lines.append(f"      File: {status.file_path}")
            if status.missing_privs:
                lines.append(
                    "      Required by the package payload but absent from the .dcl:"
                )
                lines.append(
                    _format_missing_grant_statements(
                        status.grantee,
                        status.missing_privs,
                    )
                )
            if status.extra_privs and extra_grants_severity != "OFF":
                lines.append(
                    "      Grants specified but not required by the package payload:"
                )
                lines.append(_format_grants_block(status.extra_privs))
        elif status.missing:
            lines.append(f"\n  ! {status.grantee}: missing .dcl file")
            lines.append(f"      Expected at: {status.file_path}")
            lines.append("      Inferred grants:")
            lines.append(_format_grants_block(status.expected_grants))
        elif status.orphaned:
            lines.append(f"\n  ℹ {status.grantee}: external grant")
            lines.append(f"      File:    {status.file_path}")
            lines.append(
                "      This package grants access to the grantee but no DDL "
                "in the package implies the grant (the grantee — a role, "
                "database, or user — is external to the package). Often "
                "legitimate; promote warn_external_grants to ERROR in "
                "inspect.conf for fully self-contained packages."
            )

    return "\n".join(lines)
