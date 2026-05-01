"""
Validate Object Placement — SHIPS Validation Rule.

Checks that ``.viw`` files do not contain direct references to tables
databases. All view access should go through the 1:1 locking view
layer in the views database.

This module provides a standalone validation function that can be
integrated into the main ``validate.py`` validation pipeline.

Rules:
    1. Any ``.viw`` file referencing a tables database is an ERROR.
    2. Exception: 1:1 locking views (identified by the marker comment
       ``-- LOCKING VIEW``) are exempt — they legitimately reference
       tables databases.
    3. The rule only applies when ``locking_views`` is True in the
       object placement config.
    4. For ``colocated`` strategy, the rule is skipped (no separation).

Author: Paul / Teradata Field Engineering
"""

import re
from pathlib import Path
from typing import List, NamedTuple

from .object_placement import ObjectPlacement


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ValidationIssue(NamedTuple):
    """A single validation issue found in a file."""

    file_path: Path
    line_number: int
    severity: str  # 'ERROR' or 'WARNING'
    message: str
    reference: str  # The offending qualified reference


# ---------------------------------------------------------------------------
# Locking view detection
# ---------------------------------------------------------------------------

# Marker comments that identify a 1:1 locking view.
# The recommended marker is:  -- LOCKING VIEW
_LOCKING_VIEW_MARKERS = [
    re.compile(r"--\s*LOCKING\s+VIEW", re.IGNORECASE),
    re.compile(r"--\s*1:1\s+VIEW", re.IGNORECASE),
    re.compile(r"--\s*DIRTY\s+READ\s+VIEW", re.IGNORECASE),
]


def is_locking_view(sql_text: str) -> bool:
    """
    Determine whether the SQL text represents a 1:1 locking view.

    Detection is based on marker comments in the file header. The
    markers are checked case-insensitively.

    A more structural check could look for ``LOCKING ROW FOR ACCESS``
    with no ``WHERE`` clause, but marker comments are more reliable
    and explicit.

    Args:
        sql_text: The full SQL text of the view file.

    Returns:
        True if the file is identified as a 1:1 locking view.
    """
    # Only check the first 20 lines (header area) for efficiency
    header_lines = sql_text.split("\n")[:20]
    header = "\n".join(header_lines)

    for marker in _LOCKING_VIEW_MARKERS:
        if marker.search(header):
            return True

    return False


# ---------------------------------------------------------------------------
# Reference detection (reuses patterns from migrate_view_references)
# ---------------------------------------------------------------------------

_IDENT_OR_TOKEN = r'(\{\{[A-Za-z_]\w*\}\}|"?[A-Za-z_]\w*"?)'
_QUALIFIED_REF_PATTERN = re.compile(
    r"(?<![.\w])" + _IDENT_OR_TOKEN + r"\." + _IDENT_OR_TOKEN + r"(?![.\w])",
    re.IGNORECASE,
)

_LINE_COMMENT_PATTERN = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_PATTERN = re.compile(r"'(?:[^']|'')*'")


def _build_exclusion_mask(sql_text: str) -> List[bool]:
    """
    Build a boolean mask marking positions inside comments or string
    literals as True (excluded from validation).

    Args:
        sql_text: The full SQL text of the file.

    Returns:
        List of booleans, one per character. True = excluded.
    """
    mask = [False] * len(sql_text)

    for match in _BLOCK_COMMENT_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    for match in _LINE_COMMENT_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    for match in _STRING_LITERAL_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    return mask


def _strip_quotes(identifier: str) -> str:
    """Remove surrounding double quotes from an identifier."""
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1]
    return identifier


# ---------------------------------------------------------------------------
# Validation function
# ---------------------------------------------------------------------------


def validate_object_placement(
    file_path: Path,
    placement: ObjectPlacement,
    severity: str = "ERROR",
) -> List[ValidationIssue]:
    """
    Validate that a ``.viw`` file does not directly reference any
    tables database.

    If the file is identified as a 1:1 locking view (via marker
    comments), it is exempt from this rule.

    Args:
        file_path: Path to the ``.viw`` file.
        placement: Configured ObjectPlacement engine.
        severity:  Severity level for violations
                   (``'ERROR'`` or ``'WARNING'``).

    Returns:
        List of ValidationIssue tuples. Empty if the file passes.
    """
    issues: List[ValidationIssue] = []

    # Rule only applies when locking views are enabled
    if not placement.locking_views:
        return issues

    # Rule does not apply to colocated strategy (no separation)
    if placement.strategy == "colocated":
        return issues

    # Only validate .viw files
    if file_path.suffix.lower() != ".viw":
        return issues

    # Read file
    try:
        sql_text = file_path.read_text(encoding="utf-8")
    except Exception as e:
        issues.append(
            ValidationIssue(
                file_path=file_path,
                line_number=0,
                severity="ERROR",
                message=f"Failed to read file: {e}",
                reference="",
            )
        )
        return issues

    # Exempt 1:1 locking views
    if is_locking_view(sql_text):
        return issues

    # Build exclusion mask for comments and strings
    exclusion_mask = _build_exclusion_mask(sql_text)

    # Scan for database-qualified references
    for match in _QUALIFIED_REF_PATTERN.finditer(sql_text):
        # Skip if inside a comment or string literal
        if exclusion_mask[match.start()]:
            continue

        raw_db = match.group(1)
        db_name = _strip_quotes(raw_db)

        # Check if this database matches the tables pattern
        if placement.is_tables_database(db_name):
            line_num = sql_text[: match.start()].count("\n") + 1
            qualified_ref = match.group(0)

            try:
                views_db = placement.resolve_views_database(db_name)
                suggestion = (
                    f"  Change '{db_name}' to '{views_db}' so the view "
                    f"reads from the 1:1 locking view layer."
                )
            except Exception:
                suggestion = "  Views should not reference tables databases directly."

            issues.append(
                ValidationIssue(
                    file_path=file_path,
                    line_number=line_num,
                    severity=severity,
                    message=(
                        f"Direct reference to tables database '{db_name}'. "
                        f"All view access must go through the 1:1 locking "
                        f"view layer.\n{suggestion}"
                    ),
                    reference=qualified_ref,
                )
            )

    return issues


def validate_directory(
    search_dir: Path,
    placement: ObjectPlacement,
    severity: str = "ERROR",
) -> List[ValidationIssue]:
    """
    Validate all ``.viw`` files in a directory tree.

    Args:
        search_dir: Root directory to scan.
        placement:  Configured ObjectPlacement engine.
        severity:   Severity level for violations.

    Returns:
        Aggregated list of ValidationIssue tuples.
    """
    all_issues: List[ValidationIssue] = []

    for viw_file in sorted(search_dir.rglob("*.viw")):
        issues = validate_object_placement(viw_file, placement, severity)
        all_issues.extend(issues)

    return all_issues


def format_issues(issues: List[ValidationIssue]) -> str:
    """
    Format validation issues as a human-readable report string.

    Args:
        issues: List of ValidationIssue tuples.

    Returns:
        Formatted report string.
    """
    if not issues:
        return "  No object placement violations found.\n"

    lines = []
    current_file = None

    for issue in issues:
        if issue.file_path != current_file:
            current_file = issue.file_path
            lines.append(f"\n  {current_file}")

        lines.append(
            f"    [{issue.severity}] Line {issue.line_number}: {issue.reference}"
        )
        for msg_line in issue.message.split("\n"):
            lines.append(f"      {msg_line}")

    error_count = sum(1 for i in issues if i.severity == "ERROR")
    warn_count = sum(1 for i in issues if i.severity == "WARNING")
    lines.append(f"\n  Total: {error_count} error(s), {warn_count} warning(s)")

    return "\n".join(lines)
