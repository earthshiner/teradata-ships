"""
validate_grants.py — Cross-database grant validation for SHIPS projects.

Part of the SHIPS Inspect phase. Runs after validate.py (per-file lint)
and performs cross-file analysis to ensure that all implied grants
required by the DDL are declared in .grt files, and that no stale
grants remain.

Workflow:
    1. Infer the required grant set by analysing all DDL files
       (views, procedures, macros, triggers, functions)
    2. Parse the existing .grt files in the project's dcl/ directory
    3. Compare inferred vs declared:
       - MISSING: inferred grant has no matching .grt entry → ERROR
       - STALE:   .grt entry has no matching inferred grant → WARNING
       - MATCH:   inferred and declared agree → OK
    4. Optionally (--fix) generate or update .grt files to match
       the inferred set

Integration:
    Called by the SHIPS inspect CLI alongside validate.py:

        td_release_packager inspect <project>
          → validate.py          (per-file lint)
          → validate_grants.py   (cross-file grant analysis)

Usage:
    # Validate only — report missing/stale grants
    python validate_grants.py <project_dir>

    # Validate and fix — generate/update .grt files
    python validate_grants.py <project_dir> --fix

    # Verbose output
    python validate_grants.py <project_dir> --verbose

Author: Paul Dancer — Teradata Worldwide Field Tech
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# --- Import the inference engine ---
try:
    # Package import (when running as part of td_release_packager)
    from td_release_packager.infer_grants import (
        find_ddl_files,
        analyse_file,
        consolidate_grants,
        generate_grt_content,
        grantee_filename,
        PRIV_ORDER,
    )
except ImportError:
    # Direct import (when running standalone)
    from infer_grants import (
        find_ddl_files,
        analyse_file,
        consolidate_grants,
        generate_grt_content,
        grantee_filename,
        PRIV_ORDER,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex to parse a GRANT statement from a .grt file
# Matches: GRANT priv1[, priv2, ...] ON grantor TO grantee [WITH GRANT OPTION];
RE_GRANT_STMT = re.compile(
    r"^\s*GRANT\s+"
    r"((?:[A-Z]+(?:\s+[A-Z]+)?)"       # first privilege (may be two words)
    r"(?:\s*,\s*"                        # comma separator
    r"(?:[A-Z]+(?:\s+[A-Z]+)?))*)"      # additional privileges
    r"\s+ON\s+"
    r"(\S+)"                             # grantor database
    r"\s+TO\s+"
    r"(\S+)"                             # grantee database
    r"(?:\s+WITH\s+GRANT\s+OPTION)?"     # optional WITH GRANT OPTION
    r"\s*;",
    re.IGNORECASE | re.MULTILINE
)


# ---------------------------------------------------------------------------
# Data classes — compatible with validate.py patterns
# ---------------------------------------------------------------------------

@dataclass
class GrantValidationIssue:
    """A single grant validation finding."""

    grantee: str
    rule: str           # 'missing_grant', 'stale_grant', 'missing_file'
    severity: str       # 'ERROR' or 'WARNING'
    message: str
    grantor: Optional[str] = None
    privilege: Optional[str] = None


@dataclass
class GrantValidationResult:
    """Aggregate grant validation outcome."""

    grantees_checked: int = 0
    grants_inferred: int = 0
    grants_declared: int = 0
    missing: int = 0
    stale: int = 0
    matched: int = 0
    issues: List[GrantValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if no ERROR-level issues found."""
        return all(i.severity != "ERROR" for i in self.issues)


# ---------------------------------------------------------------------------
# .grt file parser
# ---------------------------------------------------------------------------

def parse_grt_file(filepath: Path) -> Dict[str, Set[str]]:
    """
    Parse a .grt file and extract the declared grants.

    Reads GRANT statements from the file and builds a map of
    grantor database → set of privileges.

    Args:
        filepath: Path to the .grt file.

    Returns:
        Dict mapping grantor database references to sets of
        privilege strings (e.g. {'{{DOM_DATABASE_T}}': {'SELECT'}}).
    """
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  WARNING: Cannot read {filepath}: {e}", file=sys.stderr)
        return {}

    grants: Dict[str, Set[str]] = defaultdict(set)

    for match in RE_GRANT_STMT.finditer(content):
        priv_str = match.group(1).strip()
        grantor = match.group(2).strip()
        # grantee = match.group(3).strip()  # available but not needed here

        # Split comma-separated privileges, normalise whitespace
        privs = [p.strip().upper() for p in priv_str.split(",")]
        for priv in privs:
            if priv:
                grants[grantor].add(priv)

    return dict(grants)


def find_grt_files(project_dir: Path, dcl_dir: Optional[Path] = None) -> List[Path]:
    """
    Find all .grt files in the project's DCL directory.

    Args:
        project_dir: Root directory of the SHIPS project.
        dcl_dir:     Optional explicit DCL directory. Defaults to
                     <project_dir>/dcl/.

    Returns:
        Sorted list of Path objects for each .grt file found.
    """
    search_dir = dcl_dir or (project_dir / "dcl")
    if not search_dir.is_dir():
        return []

    return sorted(search_dir.glob("*.grt"))


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def compare_grants(
    inferred: Dict[str, Dict[str, Set[str]]],
    declared: Dict[str, Dict[str, Set[str]]],
) -> List[GrantValidationIssue]:
    """
    Compare inferred grants against declared grants.

    Produces issues for:
        - MISSING grants: inferred but not declared (ERROR)
        - STALE grants:   declared but not inferred (WARNING)

    Args:
        inferred: {grantee: {grantor: set_of_privileges}} from DDL analysis.
        declared: {grantee: {grantor: set_of_privileges}} from .grt files.

    Returns:
        List of GrantValidationIssue objects.
    """
    issues: List[GrantValidationIssue] = []

    # All grantees from both sets
    all_grantees = set(inferred.keys()) | set(declared.keys())

    for grantee in sorted(all_grantees):
        inferred_grants = inferred.get(grantee, {})
        declared_grants = declared.get(grantee, {})

        # --- Check for missing .grt file entirely ---
        if grantee in inferred and grantee not in declared:
            # Summarise what's needed
            grant_pairs = []
            for grantor in sorted(inferred_grants.keys()):
                privs = sorted(
                    inferred_grants[grantor],
                    key=lambda p: PRIV_ORDER.index(p)
                    if p in PRIV_ORDER else 99
                )
                grant_pairs.append(
                    f"{', '.join(privs)} ON {grantor}"
                )
            issues.append(GrantValidationIssue(
                grantee=grantee,
                rule="missing_file",
                severity="ERROR",
                message=(
                    f"No .grt file exists for {grantee}. "
                    f"Inferred grants: {'; '.join(grant_pairs)}. "
                    f"Run with --fix to generate."
                ),
            ))
            continue

        # --- Check for stale .grt file (no inferred grants) ---
        if grantee not in inferred and grantee in declared:
            issues.append(GrantValidationIssue(
                grantee=grantee,
                rule="stale_file",
                severity="WARNING",
                message=(
                    f".grt file exists for {grantee} but no DDL "
                    f"requires grants for this database. "
                    f"The file may be stale."
                ),
            ))
            continue

        # --- Per-grantor comparison ---
        all_grantors = set(inferred_grants.keys()) | set(declared_grants.keys())

        for grantor in sorted(all_grantors):
            inferred_privs = inferred_grants.get(grantor, set())
            declared_privs = declared_grants.get(grantor, set())

            # Missing privileges
            missing_privs = inferred_privs - declared_privs
            for priv in sorted(
                missing_privs,
                key=lambda p: PRIV_ORDER.index(p)
                if p in PRIV_ORDER else 99
            ):
                issues.append(GrantValidationIssue(
                    grantee=grantee,
                    rule="missing_grant",
                    severity="ERROR",
                    grantor=grantor,
                    privilege=priv,
                    message=(
                        f"Missing: GRANT {priv} ON {grantor} "
                        f"TO {grantee} WITH GRANT OPTION — "
                        f"required by DDL but not declared in .grt file."
                    ),
                ))

            # Stale privileges
            stale_privs = declared_privs - inferred_privs
            for priv in sorted(
                stale_privs,
                key=lambda p: PRIV_ORDER.index(p)
                if p in PRIV_ORDER else 99
            ):
                issues.append(GrantValidationIssue(
                    grantee=grantee,
                    rule="stale_grant",
                    severity="WARNING",
                    grantor=grantor,
                    privilege=priv,
                    message=(
                        f"Stale: GRANT {priv} ON {grantor} "
                        f"TO {grantee} — declared in .grt file but "
                        f"no DDL references this privilege."
                    ),
                ))

    return issues


# ---------------------------------------------------------------------------
# Main validation function (importable)
# ---------------------------------------------------------------------------

def validate_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
) -> GrantValidationResult:
    """
    Validate that all implied cross-database grants are declared.

    Runs the full inference → comparison pipeline:
        1. Scan DDL files and infer required grants
        2. Parse existing .grt files
        3. Compare and report differences

    Args:
        project_dir: Root directory of the SHIPS project.
        dcl_dir:     Optional explicit DCL directory.
        verbose:     If True, print diagnostic information.

    Returns:
        GrantValidationResult with all findings.
    """
    result = GrantValidationResult()

    # --- Step 1: Infer grants from DDL ---
    ddl_files = find_ddl_files(project_dir)
    if verbose:
        print(f"  Grant inference: {len(ddl_files)} DDL files found")

    analysis_results = []
    for ddl_file in ddl_files:
        analysis = analyse_file(ddl_file, verbose=verbose)
        if analysis:
            analysis_results.append(analysis)

    inferred = consolidate_grants(analysis_results)
    result.grants_inferred = sum(
        sum(len(privs) for privs in grantors.values())
        for grantors in inferred.values()
    )

    if verbose:
        print(
            f"  Inferred: {result.grants_inferred} privilege(s) "
            f"across {len(inferred)} grantee(s)"
        )

    # --- Step 2: Parse existing .grt files ---
    grt_files = find_grt_files(project_dir, dcl_dir)
    declared: Dict[str, Dict[str, Set[str]]] = {}

    for grt_path in grt_files:
        # Derive grantee from filename (strip .grt extension)
        grantee = grt_path.stem
        grants = parse_grt_file(grt_path)
        if grants:
            declared[grantee] = grants

    result.grants_declared = sum(
        sum(len(privs) for privs in grantors.values())
        for grantors in declared.values()
    )

    if verbose:
        print(
            f"  Declared: {result.grants_declared} privilege(s) "
            f"across {len(declared)} .grt file(s)"
        )

    # --- Step 3: Compare ---
    result.grantees_checked = len(set(inferred.keys()) | set(declared.keys()))
    result.issues = compare_grants(inferred, declared)

    # Tally issue types
    result.missing = sum(
        1 for i in result.issues
        if i.rule in ("missing_grant", "missing_file")
    )
    result.stale = sum(
        1 for i in result.issues
        if i.rule in ("stale_grant", "stale_file")
    )
    result.matched = result.grants_inferred - result.missing

    return result


# ---------------------------------------------------------------------------
# Fix mode — generate/update .grt files
# ---------------------------------------------------------------------------

def fix_grants(
    project_dir: Path,
    dcl_dir: Optional[Path] = None,
    verbose: bool = False,
) -> Tuple[GrantValidationResult, int]:
    """
    Infer grants and write/update .grt files to match.

    This is the --fix mode: it generates .grt files that exactly
    match the inferred grant set. Existing .grt files are overwritten
    with the inferred content. Stale .grt files (no matching DDL) are
    reported but NOT deleted — manual review is required.

    Args:
        project_dir: Root directory of the SHIPS project.
        dcl_dir:     Optional explicit DCL directory.
        verbose:     If True, print diagnostic information.

    Returns:
        Tuple of (GrantValidationResult, files_written).
    """
    output_dir = dcl_dir or (project_dir / "dcl")

    # --- Run inference ---
    ddl_files = find_ddl_files(project_dir)
    analysis_results = []
    for ddl_file in ddl_files:
        analysis = analyse_file(ddl_file, verbose=verbose)
        if analysis:
            analysis_results.append(analysis)

    inferred = consolidate_grants(analysis_results)
    project_name = project_dir.name

    # --- Generate .grt files ---
    files_written = 0
    output_dir.mkdir(parents=True, exist_ok=True)

    for grantee in sorted(inferred.keys()):
        grants = inferred[grantee]
        sources = [r for r in analysis_results if r["grantee"] == grantee]

        content = generate_grt_content(
            grantee, grants, sources, project_name
        )
        filename = grantee_filename(grantee)
        out_path = output_dir / filename
        out_path.write_text(content, encoding="utf-8")
        files_written += 1

        if verbose:
            grant_count = len(grants)
            priv_count = sum(len(privs) for privs in grants.values())
            print(
                f"  Written: {filename} — "
                f"{grant_count} statement(s), "
                f"{priv_count} privilege(s)"
            )

    # --- Now validate the result (should be clean) ---
    result = validate_grants(project_dir, dcl_dir, verbose=False)

    return result, files_written


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(result: GrantValidationResult) -> str:
    """
    Format a grant validation result as a human-readable report.

    Args:
        result: The GrantValidationResult to format.

    Returns:
        Multi-line report string.
    """
    lines = []
    lines.append("Grant Validation Report")
    lines.append("=" * 50)
    lines.append(
        f"  Grantees checked:   {result.grantees_checked}"
    )
    lines.append(
        f"  Privileges inferred: {result.grants_inferred}"
    )
    lines.append(
        f"  Privileges declared: {result.grants_declared}"
    )
    lines.append(
        f"  Matched:             {result.matched}"
    )
    lines.append(
        f"  Missing (ERROR):     {result.missing}"
    )
    lines.append(
        f"  Stale (WARNING):     {result.stale}"
    )
    lines.append("")

    if result.issues:
        # Group issues by grantee
        by_grantee: Dict[str, List[GrantValidationIssue]] = defaultdict(list)
        for issue in result.issues:
            by_grantee[issue.grantee].append(issue)

        for grantee in sorted(by_grantee.keys()):
            lines.append(f"  {grantee}:")
            for issue in by_grantee[grantee]:
                marker = "ERROR  " if issue.severity == "ERROR" else "WARNING"
                lines.append(f"    [{marker}] {issue.message}")
            lines.append("")

    if result.passed:
        lines.append("Result: PASSED — all inferred grants are declared.")
    else:
        lines.append(
            "Result: FAILED — missing grants must be resolved "
            "before deployment. Run with --fix to generate .grt files."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """
    Entry point for the grant validation tool.

    Parses command-line arguments, runs validation or fix mode,
    and prints the report.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Validate cross-database grants in a SHIPS project. "
            "Part of the SHIPS Inspect phase."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Root directory of the SHIPS project.",
    )
    parser.add_argument(
        "--dcl-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing .grt files. "
            "Defaults to <project_dir>/dcl/"
        ),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Generate or update .grt files to match inferred grants. "
            "Existing .grt files are overwritten."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print diagnostic information during analysis.",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"ERROR: {project_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"SHIPS Grant Validation — {project_dir.name}")
    print()

    if args.fix:
        # --- Fix mode: generate/update .grt files ---
        result, files_written = fix_grants(
            project_dir, args.dcl_dir, verbose=args.verbose
        )
        print(f"  Generated {files_written} .grt file(s)")
        print()
        print(format_report(result))
    else:
        # --- Validate mode: compare and report ---
        result = validate_grants(
            project_dir, args.dcl_dir, verbose=args.verbose
        )
        print(format_report(result))

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
