"""
validate.py — Teradata Coding Discipline linter.

Scans DDL files and reports conformance to configurable
engineering discipline rules:

    1. Database qualifier present (DB.ObjectName syntax)
    2. MULTISET or SET specified for tables
    3. UPPERCASE keywords
    4. Leading commas in column/parameter lists
    5. One object per file (no multi-statement DDL)
    6. Eponymous file naming (filename matches DDL content)
    7. No type suffixes on object names (_V, _T, VW_, SP_, etc.)
    8. {{TOKENS}} used (not hardcoded database names)
    9. CREATE required (REPLACE prohibited — deployer owns idempotency)
   10. Correct file extension per object type

Each rule's severity is configurable via inspect.conf:
    ERROR   — must fix before deployment
    WARNING — should fix, but won't block deployment
    OFF     — rule is disabled, no output
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Rule configuration
# ---------------------------------------------------------------

# -- Default severity for each rule --
# Used when no inspect.conf is provided, or for rules not
# listed in the config file.
DEFAULT_RULES: Dict[str, str] = {
    "db_qualifier": "ERROR",
    "set_multiset": "WARNING",
    "deploy_intent": "ERROR",
    "one_object": "WARNING",
    "eponymous": "WARNING",
    "extension": "WARNING",
    "type_suffix": "ERROR",
    "hardcoded_name": "WARNING",
    "keyword_case": "WARNING",
    "leading_commas": "WARNING",
}

# -- Valid severity values --
_VALID_SEVERITIES = {"ERROR", "WARNING", "OFF"}


def read_inspect_config(config_path: str) -> Dict[str, str]:
    """
    Read an inspect.conf file into a rules configuration dict.

    Format:
        # Comment lines start with '#'
        rule_name=SEVERITY

    Valid severities: ERROR, WARNING, OFF.
    Unknown rule names are accepted (future-proofing for
    custom rules). Invalid severities produce a warning and
    fall back to the default.

    Args:
        config_path: Path to the inspect.conf file.

    Returns:
        Dictionary of rule_name → severity, merged with defaults.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Inspect config not found: {config_path}"
        )

    # Start with defaults
    rules = dict(DEFAULT_RULES)

    with open(config_path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith('#'):
                continue

            # Split on first '='
            if '=' not in stripped:
                logger.warning(
                    "inspect.conf line %d: no '=' found, skipping: %s",
                    lineno, stripped
                )
                continue

            name, value = stripped.split('=', 1)
            name = name.strip().lower()
            value = value.strip().upper()

            if value not in _VALID_SEVERITIES:
                logger.warning(
                    "inspect.conf line %d: invalid severity '%s' "
                    "for rule '%s' — expected ERROR, WARNING, or OFF. "
                    "Using default.",
                    lineno, value, name
                )
                continue

            rules[name] = value

    logger.info(
        "Inspect config: %d rules loaded from %s",
        len(rules), config_path
    )

    return rules


def generate_default_config() -> str:
    """
    Generate the default inspect.conf content.

    Returns:
        Multi-line string suitable for writing to a file.
    """
    lines = [
        "# inspect.conf — Validation rule configuration",
        "#",
        "# Controls which rules the SHIPS inspector checks and at",
        "# what severity. Place this file in config/inspect.conf",
        "# within your project, or pass via --config on the CLI.",
        "#",
        "# Severity values:",
        "#   ERROR   — must fix before deployment (blocks --strict)",
        "#   WARNING — advisory, does not block deployment",
        "#   OFF     — rule is disabled entirely",
        "#",
        "# --strict mode promotes all WARNING rules to ERROR.",
        "# OFF rules remain off even in strict mode.",
        "",
        "# Structural rules",
        f"db_qualifier={DEFAULT_RULES['db_qualifier']}",
        f"set_multiset={DEFAULT_RULES['set_multiset']}",
        "# deploy_intent: REPLACE is prohibited — use CREATE.",
        "# The deployer owns idempotency via DROP+CREATE with rollback.",
        f"deploy_intent={DEFAULT_RULES['deploy_intent']}",
        f"one_object={DEFAULT_RULES['one_object']}",
        f"eponymous={DEFAULT_RULES['eponymous']}",
        f"extension={DEFAULT_RULES['extension']}",
        f"type_suffix={DEFAULT_RULES['type_suffix']}",
        "",
        "# Style rules",
        f"hardcoded_name={DEFAULT_RULES['hardcoded_name']}",
        f"keyword_case={DEFAULT_RULES['keyword_case']}",
        f"leading_commas={DEFAULT_RULES['leading_commas']}",
    ]
    return "\n".join(lines) + "\n"

# -- Forbidden type suffixes/prefixes --
_TYPE_SUFFIX_RE = re.compile(
    r'(?:_V|_T|_P|_VW|_SP|_TBL|_MCR|_FNC|_TRG|VW_|SP_|TBL_|FN_)\b',
    re.IGNORECASE,
)

# -- Keywords that should be UPPERCASE --
_KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'ON',
    'CREATE', 'TABLE', 'VIEW', 'INDEX', 'REPLACE', 'DROP',
    'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
    'GRANT', 'REVOKE', 'PRIMARY', 'UNIQUE', 'FOREIGN', 'KEY',
    'REFERENCES', 'DEFAULT', 'NULL', 'NOT', 'CHARACTER',
    'VARCHAR', 'INTEGER', 'DECIMAL', 'DATE', 'TIMESTAMP',
    'MULTISET', 'FALLBACK', 'JOURNAL', 'AFTER', 'BEFORE',
    'AS', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 'CROSS',
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'HAVING', 'GROUP',
    'ORDER', 'BY', 'BETWEEN', 'LIKE', 'EXISTS', 'UNION', 'ALL',
    'MERGE', 'USING', 'MATCHED',
]

# -- Expected extensions by object type --
_EXPECTED_EXT = {
    'TABLE': '.tbl',
    'VIEW': '.viw',
    'MACRO': '.mcr',
    'PROCEDURE': '.spl',
    'FUNCTION': '.fnc',
    'TRIGGER': '.trg',
    'JOIN_INDEX': '.jix',
    'HASH_INDEX': '.idx',
    'INDEX': '.idx',
    'MAP': '.map',
    'ROLE': '.rol',
    'PROFILE': '.prf',
    'AUTHORIZATION': '.auth',
    'FOREIGN_SERVER': '.fsvr',
    'JAR': '.jcl',
    'SCRIPT_TABLE_OPERATOR': '.sto',
}

# -- Classification patterns (reuse from ingest) --
_CLASSIFY_PATTERNS = [
    (re.compile(r'CREATE\s+JOIN\s+INDEX\b', re.I), "JOIN_INDEX"),
    (re.compile(r'CREATE\s+HASH\s+INDEX\b', re.I), "HASH_INDEX"),
    (re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\b', re.I), "INDEX"),
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b.*?TABLE\s+OPERATOR', re.I | re.DOTALL), "SCRIPT_TABLE_OPERATOR"),
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:MULTISET|SET)?\s*(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b', re.I), "TABLE"),
    (re.compile(r'(?:CREATE|REPLACE)\s+VIEW\b', re.I), "VIEW"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)MACRO\b', re.I), "MACRO"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)PROCEDURE\b', re.I), "PROCEDURE"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)(?:SPECIFIC\s+)?FUNCTION\b', re.I), "FUNCTION"),
    (re.compile(r'(?:CREATE|REPLACE)\s+TRIGGER\b', re.I), "TRIGGER"),
    (re.compile(r'CREATE\s+MAP\b', re.I), "MAP"),
    (re.compile(r'CREATE\s+AUTHORIZATION\b', re.I), "AUTHORIZATION"),
    (re.compile(r'CREATE\s+FOREIGN\s+SERVER\b', re.I), "FOREIGN_SERVER"),
    (re.compile(r'CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(', re.I), "JAR"),
]

# -- System-scope types: no database qualifier, no tokens expected --
_SYSTEM_SCOPE_TYPES = {
    "MAP", "ROLE", "PROFILE", "AUTHORIZATION", "FOREIGN_SERVER",
}

# -- Qualified name extraction --
_QUALIFIED_NAME_RE = re.compile(
    r'(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?'
    r'(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?'
    r'(?:TRACE\s+)?'
    r'(?:SPECIFIC\s+)?'
    r'(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|'
    r'JOIN\s+INDEX|HASH\s+INDEX)\s+'
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)

# -- SET/MULTISET detection --
_HAS_SET_MULTISET_RE = re.compile(
    r'CREATE\s+(?:MULTISET|SET)\s+', re.I,
)

# -- REPLACE detection (prohibited — deployer owns idempotency) --
# Matches REPLACE as a leading DDL verb for any replaceable type.
# Teradata syntax: REPLACE VIEW, REPLACE PROCEDURE, REPLACE MACRO,
#                  REPLACE FUNCTION, REPLACE SPECIFIC FUNCTION,
#                  REPLACE TRIGGER.
# CREATE is the required verb — the deployer handles existence
# checking, DROP, backup (via SHOW), and rollback.
_LEADING_REPLACE_RE = re.compile(
    r"^\s*REPLACE\s+"
    r"(?:VIEW|PROCEDURE|MACRO|TRIGGER|(?:SPECIFIC\s+)?FUNCTION)\b",
    re.IGNORECASE | re.MULTILINE,
)

# -- Token detection --
_TOKEN_RE = re.compile(r'\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}')

# -- Multi-statement detection --
_STATEMENT_START_RE = re.compile(
    r'^\s*(?:CREATE|REPLACE|DROP|GRANT|REVOKE|ALTER|INSERT|UPDATE|DELETE|MERGE)\b',
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ValidationIssue:
    """A single validation finding."""

    file: str
    rule: str
    severity: str       # 'ERROR' or 'WARNING'
    message: str
    line: Optional[int] = None


@dataclass
class ValidationResult:
    """Aggregate validation outcome."""

    files_scanned: int = 0
    files_passed: int = 0
    files_with_issues: int = 0
    errors: int = 0
    warnings: int = 0
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if no ERROR-level issues found."""
        return self.errors == 0


def validate_directory(
    source_dir: str,
    rules_config: Dict[str, str] = None,
    strict: bool = False,
) -> ValidationResult:
    """
    Validate all DDL files in a directory against the Coding Discipline.

    Args:
        source_dir:     Directory to scan.
        rules_config:   Dictionary of rule_name → severity (ERROR,
                        WARNING, OFF). If None, DEFAULT_RULES are used.
                        Load from inspect.conf via read_inspect_config().
        strict:         If True, all WARNING rules are promoted to
                        ERROR. OFF rules remain off even in strict mode.

    Returns:
        ValidationResult with per-file issues.
    """
    # -- Resolve rule config --
    if rules_config is None:
        rules_config = dict(DEFAULT_RULES)

    # --strict promotes WARNING → ERROR (OFF stays OFF)
    if strict:
        rules_config = {
            rule: ("ERROR" if sev == "WARNING" else sev)
            for rule, sev in rules_config.items()
        }

    result = ValidationResult()

    # Discover files
    files = []
    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith('.') or f.startswith('_'):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in ('.tbl', '.viw', '.spl', '.mcr', '.fnc', '.trg',
                        '.jix', '.idx', '.db', '.sql', '.ddl',
                        '.map', '.rol', '.prf', '.auth', '.fsvr',
                        '.sto', '.jcl', '.dcl', '.usr'):
                files.append(os.path.join(root, f))

    result.files_scanned = len(files)

    for file_path in files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            continue

        rel_path = os.path.relpath(file_path, source_dir)
        file_issues = []

        # -- Run all checks, collect raw issues --
        file_issues.extend(_check_db_qualifier(rel_path, content))
        file_issues.extend(_check_multiset(rel_path, content))
        file_issues.extend(_check_deploy_intent(rel_path, content, strict))
        file_issues.extend(_check_one_object(rel_path, content))
        file_issues.extend(_check_eponymous(rel_path, content, file_path))
        file_issues.extend(_check_extension(rel_path, content, file_path))
        file_issues.extend(_check_type_suffixes(rel_path, content))
        file_issues.extend(_check_hardcoded_names(rel_path, content))
        file_issues.extend(_check_keyword_case(rel_path, content))
        file_issues.extend(_check_leading_commas(rel_path, content))

        # -- Apply rule config: remap severity or drop OFF rules --
        # INFO issues are informational and not configurable —
        # they pass through unchanged.
        filtered_issues = []
        for issue in file_issues:
            if issue.severity == "INFO":
                filtered_issues.append(issue)
                continue
            configured_severity = rules_config.get(issue.rule, "WARNING")
            if configured_severity == "OFF":
                continue  # Rule is disabled — drop the issue
            issue.severity = configured_severity
            filtered_issues.append(issue)

        result.issues.extend(filtered_issues)

        if filtered_issues:
            result.files_with_issues += 1
        else:
            result.files_passed += 1

    result.errors = sum(1 for i in result.issues if i.severity == "ERROR")
    result.warnings = sum(1 for i in result.issues if i.severity == "WARNING")

    logger.info(
        "Validation: %d files, %d passed, %d with issues (%d errors, %d warnings)",
        result.files_scanned, result.files_passed,
        result.files_with_issues, result.errors, result.warnings,
    )

    return result


# ---------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------

def _check_db_qualifier(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check that the DDL uses Database.ObjectName syntax.

    System-scope objects (Maps, Roles, Profiles, Authorisations,
    Foreign Servers) are excluded — they have no database qualifier
    by design.
    """
    # Skip check for system-scope objects
    for pattern, obj_type in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            if obj_type in _SYSTEM_SCOPE_TYPES:
                return []
            break

    match = _QUALIFIED_NAME_RE.search(content)
    if match:
        name = match.group(1).replace('"', '')
        if '.' not in name:
            return [ValidationIssue(
                file=rel_path, rule="db_qualifier", severity="ERROR",
                message=f"Object '{name}' missing database qualifier. "
                        f"Use Database.{name} syntax.",
            )]
    return []


def _check_multiset(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that tables specify SET or MULTISET."""
    # Only applies to CREATE TABLE statements
    if not re.search(r'CREATE\s+(?:MULTISET\s+|SET\s+)?(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b',
                      content, re.I):
        return []

    if not _HAS_SET_MULTISET_RE.search(content):
        return [ValidationIssue(
            file=rel_path, rule="set_multiset", severity="WARNING",
            message="CREATE TABLE without SET/MULTISET. "
                    "MULTISET will be auto-injected at build time.",
        )]
    return []


def _check_deploy_intent(rel_path: str, content: str, strict: bool = False) -> List[ValidationIssue]:
    """
    Enforce CREATE over REPLACE for all replaceable object types.

    REPLACE is idempotent but provides no rollback path — the
    previous definition is silently overwritten with no backup.
    CREATE forces the deployer to explicitly handle existence:

        1. Capture existing DDL via SHOW (rollback artefact)
        2. DROP the existing object
        3. CREATE the new definition
        4. On failure → re-CREATE from the captured backup

    This gives autonomous agents (and humans) a clean rollback
    to the pre-package state.

    The deployer owns idempotency — not the developer's DDL verb.

    Args:
        rel_path: Relative path of the DDL file being checked.
        content:  Raw DDL file content.
        strict:   Not used for this rule (always ERROR by default),
                  kept for interface consistency.

    Returns:
        List of ValidationIssue — one issue if REPLACE is found,
        empty list if the file uses CREATE (correct).
    """
    if _LEADING_REPLACE_RE.search(content):
        return [ValidationIssue(
            file=rel_path,
            rule="deploy_intent",
            severity="ERROR",
            message=(
                "Uses REPLACE — use CREATE instead. "
                "The deployer handles idempotency via "
                "DROP-and-CREATE with automatic rollback. "
                "REPLACE overwrites silently with no backup."
            ),
        )]
    return []


def _check_one_object(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that the file contains only one DDL statement."""
    matches = _STATEMENT_START_RE.findall(content)
    # Filter out sub-statements inside procedures (BEGIN...END blocks)
    if len(matches) > 2:
        return [ValidationIssue(
            file=rel_path, rule="one_object", severity="WARNING",
            message=f"File contains {len(matches)} DDL statements. "
                    f"Discipline requires one object per file.",
        )]
    return []


def _check_eponymous(rel_path: str, content: str, file_path: str) -> List[ValidationIssue]:
    """Check that filename matches the DDL's Database.ObjectName."""
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    qualified = match.group(1).replace('"', '')
    basename = os.path.splitext(os.path.basename(file_path))[0]

    # Allow {{TOKENS}} in names — they'll be resolved at build time
    if '{{' in basename or '{{' in qualified:
        return []

    if basename.upper() != qualified.upper():
        return [ValidationIssue(
            file=rel_path, rule="eponymous", severity="WARNING",
            message=f"Filename '{basename}' does not match "
                    f"DDL object '{qualified}'.",
        )]
    return []


def _check_extension(rel_path: str, content: str, file_path: str) -> List[ValidationIssue]:
    """Check that file extension matches the object type."""
    obj_type = None
    for pattern, otype in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            obj_type = otype
            break

    if obj_type is None:
        return []

    expected = _EXPECTED_EXT.get(obj_type)
    if expected is None:
        return []

    actual = os.path.splitext(file_path)[1].lower()
    if actual != expected:
        return [ValidationIssue(
            file=rel_path, rule="extension", severity="WARNING",
            message=f"Extension '{actual}' — expected '{expected}' "
                    f"for {obj_type}.",
        )]
    return []


def _check_type_suffixes(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check for forbidden type suffixes on object names."""
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    qualified = match.group(1).replace('"', '')
    parts = qualified.split('.')
    obj_name = parts[-1]

    if _TYPE_SUFFIX_RE.search(obj_name):
        return [ValidationIssue(
            file=rel_path, rule="type_suffix", severity="ERROR",
            message=f"Object name '{obj_name}' contains a type suffix "
                    f"(_V, _T, VW_, etc.). Object type belongs in the "
                    f"database name, not the object name.",
        )]
    return []


def _check_hardcoded_names(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check for hardcoded database names (should be {{TOKENS}}).

    System-scope objects (Maps, Roles, Profiles, Authorisations,
    Foreign Servers) are excluded — they have no tokens by design.
    """
    # Skip check for system-scope objects
    for pattern, obj_type in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            if obj_type in _SYSTEM_SCOPE_TYPES:
                return []
            break

    # If the file already uses tokens, it's fine
    if _TOKEN_RE.search(content):
        return []

    # Check if there's a qualified name without tokens
    match = _QUALIFIED_NAME_RE.search(content)
    if match:
        qualified = match.group(1).replace('"', '')
        parts = qualified.split('.')
        if len(parts) == 2:
            db_name = parts[0]
            # Skip system databases
            system_dbs = {'DBC', 'SYSUDTLIB', 'SYSLIB', 'SYSJDBC',
                          'TD_SYSFNLIB', 'TDSTATS'}
            if db_name.upper() not in system_dbs:
                return [ValidationIssue(
                    file=rel_path, rule="hardcoded_name", severity="WARNING",
                    message=f"Database name '{db_name}' appears hardcoded. "
                            f"Consider using a {{{{TOKEN}}}} for environment portability.",
                )]
    return []


def _check_keyword_case(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check for lowercase SQL keywords.

    Only reports if more than 30% of keywords are lowercase —
    avoids false positives from identifiers that happen to match
    keyword names.
    """
    total = 0
    lowercase = 0

    # Check each word in the content against the keyword list
    words = re.findall(r'\b[A-Za-z]+\b', content)
    for word in words:
        if word.upper() in _KEYWORDS:
            total += 1
            if word != word.upper() and word != word.lower():
                pass  # Mixed case — skip
            elif word == word.lower():
                lowercase += 1

    if total > 5 and lowercase / total > 0.3:
        return [ValidationIssue(
            file=rel_path, rule="keyword_case", severity="WARNING",
            message=f"{lowercase}/{total} SQL keywords are lowercase. "
                    f"Discipline requires UPPERCASE keywords.",
        )]
    return []


def _check_leading_commas(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check for trailing comma convention (should be leading).

    Looks for lines ending with a comma followed by a column/param
    definition on the next line. If more trailing commas than leading,
    reports a warning.
    """
    lines = content.split('\n')
    trailing = 0
    leading = 0

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped.endswith(','):
            trailing += 1
        if stripped.lstrip().startswith(','):
            leading += 1

    # Only report if there's a clear trailing pattern
    if trailing > 3 and leading == 0:
        return [ValidationIssue(
            file=rel_path, rule="leading_commas", severity="WARNING",
            message=f"{trailing} trailing commas found. "
                    f"Discipline requires leading commas.",
        )]
    return []
