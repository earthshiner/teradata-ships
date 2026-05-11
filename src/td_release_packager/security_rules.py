"""
security_rules.py — Security-focused Inspect rules for the SHIPS pipeline.

Contains rules that scan DDL and DML file bodies for security issues:

    SECRET_PATTERN_DETECTED  — Embedded credentials or secret patterns in
                               DDL/DML files (GAP-003).  ERROR severity.

    DYNAMIC_SQL_DETECTED     — Dynamic SQL constructs inside stored procedures
                               and macros (GAP-008).  WARNING severity.

Rules in this module follow the same ValidationIssue contract as validate.py:
each function returns a list of ValidationIssue objects (empty on pass).

Files are scanned LINE BY LINE so that accurate 1-based line numbers can be
reported.  Matched text is NEVER included in the issue message to prevent
secrets from leaking into inspect reports, CI logs, and decisions.json.
"""

import os
import re
from typing import List

from td_release_packager.validate import ValidationIssue

# ---------------------------------------------------------------
# Target directory scoping
# ---------------------------------------------------------------

# Secret scanning applies to files in these source subdirectories.
# The match is against the *relative* path produced by validate.py
# (e.g. 'ddl/tables/D.MyTable.tbl', 'dml/D.Load.dml').
_SECRET_SCAN_DIRS = frozenset({"ddl", "viw", "dml", "dcl"})

# Dynamic SQL scanning applies only to procedures and macros.
_DYNAMIC_SQL_EXTENSIONS = frozenset({".spl", ".sql", ".mcr", ".prc"})


def _is_in_target_dirs(rel_path: str, target_dirs: frozenset) -> bool:
    """Return True if rel_path starts with one of the target directory names."""
    # Normalise path separators; check the leading component.
    parts = re.split(r"[/\\]", rel_path)
    return len(parts) > 1 and parts[0].lower() in target_dirs


# ---------------------------------------------------------------
# SECRET_PATTERN_DETECTED — GAP-003
# ---------------------------------------------------------------

# Compiled (pattern, description) pairs.
# IMPORTANT: descriptions identify the CATEGORY of secret, never the value.
_SECRET_PATTERNS: List[tuple] = [
    (
        re.compile(r'(?i)\bPASSWORD\s*=\s*[\'"][^\'"]{4,}[\'"]'),
        "Inline PASSWORD assignment",
    ),
    (
        re.compile(r'(?i)\bPWD\s*=\s*[\'"][^\'"]{4,}[\'"]'),
        "Inline PWD assignment",
    ),
    (
        re.compile(r"(?i)jdbc:[a-z]+://"),
        "JDBC connection string",
    ),
    (
        re.compile(r"(?i)terajdbc:"),
        "Teradata JDBC connection string",
    ),
    (
        re.compile(r'(?i)\bDSN\s*=\s*[\'"][^\'"]+[\'"]'),
        "DSN connection string",
    ),
    (
        re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
        "Private key header",
    ),
    (
        re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
        "AWS Access Key ID pattern",
    ),
    (
        re.compile(r"(?i)aws_secret_access_key\s*=\s*[^\s]{20,}"),
        "AWS secret access key",
    ),
]


def scan_secret_patterns(
    rel_path: str,
    content: str,
    file_path: str,
) -> List[ValidationIssue]:
    """Scan a DDL/DML file body for embedded credentials or secret patterns.

    The scan is scoped to files under ``ddl/``, ``viw/``, ``dml/``, and
    ``dcl/`` in the project source tree.  Each line is checked against the
    compiled pattern set.

    Matched text is deliberately excluded from the report — only the
    pattern description is emitted.  This prevents secrets from leaking
    into inspect reports, CI pipelines, and ``decisions.json``.

    Args:
        rel_path:  Path relative to the source directory (used for scope
                   check and in the returned issue).
        content:   Full file content.
        file_path: Absolute path (used for extension-based scoping when
                   needed).

    Returns:
        List of ValidationIssue (one per matching line, rule
        ``secret_scan``, severity ``ERROR``).
    """
    if not _is_in_target_dirs(rel_path, _SECRET_SCAN_DIRS):
        return []

    issues: List[ValidationIssue] = []

    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, description in _SECRET_PATTERNS:
            if pattern.search(line):
                issues.append(
                    ValidationIssue(
                        file=rel_path,
                        rule="secret_scan",
                        severity="ERROR",
                        line=lineno,
                        message=(
                            f"SECRET_PATTERN_DETECTED — {description} found at "
                            f"line {lineno}. Remove hardcoded credentials from "
                            f"DDL/DML files and use token substitution instead."
                        ),
                    )
                )
                # One finding per line per pattern; break to avoid duplicate
                # findings on the same line from related patterns.
                break

    return issues


# ---------------------------------------------------------------
# DYNAMIC_SQL_DETECTED — GAP-008
# ---------------------------------------------------------------

_DYNAMIC_SQL_PATTERNS: List[tuple] = [
    (
        re.compile(r"(?i)\bEXECUTE\s+IMMEDIATE\b"),
        "EXECUTE IMMEDIATE — dynamic SQL execution",
    ),
    (
        re.compile(r"(?i)\bCALL\s+DBC\.SYSEXECSQL\b"),
        "DBC.SYSEXECSQL — dynamic SQL execution",
    ),
    (
        re.compile(r"(?i)\bCALL\s+DBC\.EXECSQL\b"),
        "DBC.EXECSQL — dynamic SQL execution",
    ),
]


def scan_dynamic_sql(
    rel_path: str,
    content: str,
    file_path: str,
) -> List[ValidationIssue]:
    """Scan stored procedures and macros for dynamic SQL constructs.

    Dynamic SQL has legitimate uses, so findings are WARNING rather than
    ERROR.  The rule is scoped to procedure and macro files by extension
    (``.spl``, ``.sql``, ``.mcr``, ``.prc``).

    Args:
        rel_path:  Path relative to the source directory.
        content:   Full file content.
        file_path: Absolute path (used for extension check).

    Returns:
        List of ValidationIssue (one per matching line, rule
        ``dynamic_sql``, severity ``WARNING``).
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _DYNAMIC_SQL_EXTENSIONS:
        return []

    issues: List[ValidationIssue] = []

    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, description in _DYNAMIC_SQL_PATTERNS:
            if pattern.search(line):
                issues.append(
                    ValidationIssue(
                        file=rel_path,
                        rule="dynamic_sql",
                        severity="WARNING",
                        line=lineno,
                        message=(
                            f"DYNAMIC_SQL_DETECTED — {description} at line {lineno}. "
                            f"Dynamic SQL bypasses compile-time checks and may "
                            f"introduce SQL injection risk. Review intent."
                        ),
                    )
                )
                break

    return issues
