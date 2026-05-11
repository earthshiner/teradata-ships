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


# ---------------------------------------------------------------
# MISSING_SENSITIVITY_CLASS / INVALID_SENSITIVITY_CLASS — GAP-009
# ---------------------------------------------------------------

from typing import Optional  # noqa: E402 — kept at bottom for circular-import safety

_VALID_SENSITIVITY_CLASSES = frozenset(
    {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "PCI", "PHI"}
)

# File extensions that represent DDL or view objects requiring classification.
_CLASSIFY_EXTENSIONS = frozenset({".tbl", ".viw", ".sql"})


def scan_sensitivity_class(
    rel_path: str,
    file_path: str,
    require_sensitivity_class: bool = False,
    violation_level: str = "warning",
) -> List[ValidationIssue]:
    """Check for missing or invalid sensitivity class companion files (GAP-009).

    Each DDL/view file under ``ddl/`` or ``viw/`` may have a companion
    ``.cls`` file containing a single sensitivity class token.  When
    ``require_sensitivity_class`` is True, missing ``.cls`` files emit
    a WARNING (or ERROR if ``violation_level='error'``).  An invalid
    value in an existing ``.cls`` file always emits ERROR regardless
    of ``require_sensitivity_class``.

    Args:
        rel_path:                  Relative path to the DDL/view file.
        file_path:                 Absolute path to the DDL/view file.
        require_sensitivity_class: Whether to enforce .cls presence.
        violation_level:           'warning' (default) or 'error' for missing .cls.

    Returns:
        List of ValidationIssue.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _CLASSIFY_EXTENSIONS:
        return []

    if not _is_in_target_dirs(rel_path, frozenset({"ddl", "viw"})):
        return []

    cls_path = os.path.splitext(file_path)[0] + ".cls"

    if not os.path.isfile(cls_path):
        if not require_sensitivity_class:
            return []
        sev = "ERROR" if violation_level.lower() == "error" else "WARNING"
        return [
            ValidationIssue(
                file=rel_path,
                rule="sensitivity_class",
                severity=sev,
                message=(
                    f"MISSING_SENSITIVITY_CLASS — no companion .cls file found for "
                    f"'{os.path.basename(file_path)}'. Create a .cls file with one of: "
                    f"{', '.join(sorted(_VALID_SENSITIVITY_CLASSES))}."
                ),
            )
        ]

    # .cls file exists — validate its content
    try:
        raw_class = open(cls_path, encoding="utf-8").read().strip().upper()
    except (OSError, UnicodeDecodeError) as exc:
        return [
            ValidationIssue(
                file=rel_path,
                rule="sensitivity_class",
                severity="ERROR",
                message=f"INVALID_SENSITIVITY_CLASS — could not read .cls file: {exc}",
            )
        ]

    if not raw_class:
        return [
            ValidationIssue(
                file=rel_path,
                rule="sensitivity_class",
                severity="ERROR",
                message=(
                    "INVALID_SENSITIVITY_CLASS — .cls file is empty. "
                    f"Use one of: {', '.join(sorted(_VALID_SENSITIVITY_CLASSES))}."
                ),
            )
        ]

    if raw_class not in _VALID_SENSITIVITY_CLASSES:
        return [
            ValidationIssue(
                file=rel_path,
                rule="sensitivity_class",
                severity="ERROR",
                message=(
                    f"INVALID_SENSITIVITY_CLASS — '{raw_class}' is not a recognised "
                    f"sensitivity class. Valid values: "
                    f"{', '.join(sorted(_VALID_SENSITIVITY_CLASSES))}."
                ),
            )
        ]

    return []


def read_sensitivity_class(file_path: str) -> Optional[str]:
    """Read the sensitivity class for a file from its companion .cls file.

    Args:
        file_path: Absolute path to the DDL/view file.

    Returns:
        Upper-cased class string (e.g. 'PII'), or None if absent/unreadable.
    """
    cls_path = os.path.splitext(file_path)[0] + ".cls"
    if not os.path.isfile(cls_path):
        return None
    try:
        raw = open(cls_path, encoding="utf-8").read().strip().upper()
        return raw if raw in _VALID_SENSITIVITY_CLASSES else None
    except (OSError, UnicodeDecodeError):
        return None
