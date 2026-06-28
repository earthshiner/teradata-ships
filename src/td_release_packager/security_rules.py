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
secrets from leaking into inspect reports, CI logs, and ships.decisions.json.
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
    into inspect reports, CI pipelines, and ``ships.decisions.json``.

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

# Dynamic-SQL execution constructs → (regex, base risk category, label).
# These are the unambiguous "dynamic SQL is present and executed" signals.
_DYNAMIC_SQL_CONSTRUCTS: List[tuple] = [
    (
        re.compile(r"(?i)\bEXECUTE\s+IMMEDIATE\b"),
        "dynamic_sql_execute_immediate",
        "EXECUTE IMMEDIATE",
    ),
    (
        re.compile(r"(?i)\bCALL\s+DBC\.SYSEXECSQL\b"),
        "dynamic_sql_calls_sys_exec_sql",
        "DBC.SYSEXECSQL",
    ),
    (
        re.compile(r"(?i)\bCALL\s+DBC\.EXECSQL\b"),
        "dynamic_sql_calls_sys_exec_sql",
        "DBC.EXECSQL",
    ),
]

# String-concatenation operator used to build dynamic SQL from parts.
_CONCAT_RE = re.compile(r"\|\|")
# A bare (unquoted) identifier operand of a `||` concatenation — i.e. a
# variable or parameter spliced into SQL text. We strip quoted literals
# first, so anything word-shaped left next to `||` is an identifier/param.
_QUOTED_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_CONCAT_IDENTIFIER_RE = re.compile(r"(?:\|\|\s*([A-Za-z_]\w*)|([A-Za-z_]\w*)\s*\|\|)")
# A quoted literal whose text contains a SQL verb — i.e. the line is
# assembling SQL text (not just concatenating ordinary strings).
_SQL_VERB_IN_LITERAL_RE = re.compile(
    r"'(?:''|[^'])*\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|REPLACE|"
    r"DROP|ALTER|GRANT|REVOKE|CALL)\b",
    re.IGNORECASE,
)

#: Agent guidance shared by every dynamic-SQL finding (#166): an agent must
#: NOT auto-remove or auto-rewrite dynamic SQL — it needs human review.
_DYNAMIC_SQL_REMEDIATION_BASE = {
    "safe_fix_available": False,
    "automation_level": "manual_review_required",
    "agent_may_fix": False,
    "recommended_action": (
        "Review the dynamic SQL for injection, privilege, and deployment "
        "risk. Do NOT auto-remove or auto-rewrite it — dynamic execution is "
        "often intentional; rewriting can change behaviour."
    ),
}


def _classify_dynamic_sql(line_no_literals: str) -> "Optional[str]":
    """Return a concatenation risk category for a construct line, or None.

    ``line_no_literals`` has quoted string literals already blanked, so a
    surviving word-shaped operand next to ``||`` is an identifier/parameter
    (the unsanitised-parameter case); concatenation with only literals
    remaining is the lower-risk literal case.
    """
    if not _CONCAT_RE.search(line_no_literals):
        return None
    if _CONCAT_IDENTIFIER_RE.search(line_no_literals):
        return "dynamic_sql_uses_unsanitised_parameter"
    return "dynamic_sql_concatenates_literal"


def scan_dynamic_sql(
    rel_path: str,
    content: str,
    file_path: str,
) -> List[ValidationIssue]:
    """Scan stored procedures and macros for dynamic SQL, by risk category.

    Refines the single ``dynamic_sql`` finding into the risk categories from
    issue #166. Every finding carries a ``risk_category`` (one of
    ``dynamic_sql_execute_immediate``, ``dynamic_sql_calls_sys_exec_sql``,
    ``dynamic_sql_concatenates_literal``, ``dynamic_sql_uses_unsanitised_parameter``)
    in its remediation, plus agent guidance to never auto-remove dynamic SQL.

    When the executing line concatenates (``||``) values into the SQL text,
    the category escalates: concatenating a bare identifier/parameter is the
    highest-risk ``dynamic_sql_uses_unsanitised_parameter`` (possible
    injection); concatenating only literals is ``dynamic_sql_concatenates_literal``.

    All findings use rule ``dynamic_sql``, so a single ``inspect.conf`` key
    controls severity (WARNING by default; set to ERROR to block dynamic SQL).
    Scoped to procedure/macro files by extension.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _DYNAMIC_SQL_EXTENSIONS:
        return []

    issues: List[ValidationIssue] = []
    construct_lines: set = set()
    lines = content.splitlines()

    for lineno, line in enumerate(lines, start=1):
        for pattern, base_category, label in _DYNAMIC_SQL_CONSTRUCTS:
            if not pattern.search(line):
                continue
            construct_lines.add(lineno)
            # Blank quoted literals so concatenation analysis only sees code.
            line_no_literals = _QUOTED_LITERAL_RE.sub("''", line)
            concat_category = _classify_dynamic_sql(line_no_literals)
            category = concat_category or base_category
            requires_review = category == "dynamic_sql_uses_unsanitised_parameter"

            if category == "dynamic_sql_uses_unsanitised_parameter":
                risk_note = (
                    "concatenates a variable/parameter into the SQL text — "
                    "possible SQL injection if the value is unsanitised"
                )
            elif category == "dynamic_sql_concatenates_literal":
                risk_note = "builds the SQL text by concatenating literals"
            else:
                risk_note = "executes dynamic SQL — bypasses compile-time checks"

            remediation = dict(_DYNAMIC_SQL_REMEDIATION_BASE)
            remediation["risk_category"] = category
            remediation["requires_human_review"] = requires_review

            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule="dynamic_sql",
                    severity="WARNING",
                    line=lineno,
                    message=(
                        f"DYNAMIC_SQL_DETECTED [{category}] — {label} at line "
                        f"{lineno}: {risk_note}. Review intent; do not "
                        f"auto-remove dynamic SQL."
                    ),
                    remediation=remediation,
                )
            )
            break

    # Second pass: SQL assembled by concatenation (often across lines, e.g.
    # ``SET v = 'DROP TABLE ' || iName;`` followed by EXECUTE IMMEDIATE v).
    # Only runs when the file actually executes dynamic SQL, so ordinary
    # string concatenation is never flagged.
    if construct_lines:
        for lineno, line in enumerate(lines, start=1):
            if lineno in construct_lines or not _CONCAT_RE.search(line):
                continue
            if not _SQL_VERB_IN_LITERAL_RE.search(line):
                continue
            blanked = _QUOTED_LITERAL_RE.sub("''", line)
            category = _classify_dynamic_sql(blanked)
            if category is None:
                continue
            requires_review = category == "dynamic_sql_uses_unsanitised_parameter"
            risk_note = (
                "assembles SQL by concatenating a variable/parameter — "
                "possible SQL injection if unsanitised"
                if requires_review
                else "assembles SQL by concatenating literals"
            )
            remediation = dict(_DYNAMIC_SQL_REMEDIATION_BASE)
            remediation["risk_category"] = category
            remediation["requires_human_review"] = requires_review
            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule="dynamic_sql",
                    severity="WARNING",
                    line=lineno,
                    message=(
                        f"DYNAMIC_SQL_DETECTED [{category}] — dynamic SQL "
                        f"assembly at line {lineno}: {risk_note}. Review intent; "
                        f"do not auto-remove dynamic SQL."
                    ),
                    remediation=remediation,
                )
            )

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


# ---------------------------------------------------------------
# VAULT_REF_UNRESOLVED — GAP-011
# ---------------------------------------------------------------

_UNRESOLVED_REF_RE = re.compile(r"\$env:[A-Za-z_]\w*|vault:[^\s]+")


def scan_vault_refs(
    rel_path: str,
    content: str,
    file_path: str,
) -> List[ValidationIssue]:
    """Detect unresolved $env: or vault: prefixes in a payload file (GAP-011).

    After Harvest completes token substitution, no payload file should still
    contain literal ``$env:`` or ``vault:`` strings.  If they are present,
    it means the Harvest resolution was bypassed or the token map was hand-
    edited after resolution.

    Args:
        rel_path:  Relative path to the payload file.
        content:   File content (post-token-substitution).
        file_path: Absolute path (not used; kept for API consistency).

    Returns:
        List of ValidationIssue — one per matching line.
    """
    issues: List[ValidationIssue] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if _UNRESOLVED_REF_RE.search(line):
            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule="vault_ref",
                    severity="ERROR",
                    line=lineno,
                    message=(
                        f"VAULT_REF_UNRESOLVED — unresolved secret reference "
                        f"($env: or vault:) found at line {lineno}. "
                        f"This token should have been resolved during Harvest. "
                        f"Check that the token map value was correctly set."
                    ),
                )
            )
    return issues
