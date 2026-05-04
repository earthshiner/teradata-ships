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
   11. Object placement (views must not reference tables databases directly)

Each rule's severity is configurable via inspect.conf:
    ERROR   — must fix before deployment
    WARNING — should fix, but won't block deployment
    OFF     — rule is disabled, no output
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# -- Optional: Object Placement engine --
# If object_placement.py is available, the object_placement rule
# can validate that views do not reference tables databases directly.
try:
    from .object_placement import ObjectPlacement

    _HAS_PLACEMENT = True
except ImportError:
    _HAS_PLACEMENT = False


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
    # Extension is ERROR, not WARNING. A staged file whose
    # extension disagrees with its content is the package and the
    # metadata lying to each other — the deployer and any
    # automation reading the payload have to be able to TRUST that
    # *.tbl contains a table, *.spl contains a procedure, etc.
    # Catching the lie at inspect time is the whole point.
    "extension": "ERROR",
    "type_suffix": "ERROR",
    "hardcoded_name": "WARNING",
    "keyword_case": "WARNING",
    "leading_commas": "WARNING",
    "object_placement": "ERROR",
    "view_macro_self_reference": "ERROR",
    "public_grant_on_tables": "WARNING",
    "review_unmapped_grants": "WARNING",
    # intra_package_dependency defaults to OFF because the
    # ``package`` stage now auto-splits affected sources into a
    # paired prereqs + main bundle (Phase 2 of this work). The
    # rule still exists for teams that want to enforce manual
    # splits — set it to ERROR or WARNING in inspect.conf to
    # surface the structural pattern at lint time.
    "intra_package_dependency": "OFF",
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
        raise FileNotFoundError(f"Inspect config not found: {config_path}")

    # Start with defaults
    rules = dict(DEFAULT_RULES)

    with open(config_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Split on first '='
            if "=" not in stripped:
                logger.warning(
                    "inspect.conf line %d: no '=' found, skipping: %s", lineno, stripped
                )
                continue

            name, value = stripped.split("=", 1)
            name = name.strip().lower()
            value = value.strip().upper()

            if value not in _VALID_SEVERITIES:
                logger.warning(
                    "inspect.conf line %d: invalid severity '%s' "
                    "for rule '%s' — expected ERROR, WARNING, or OFF. "
                    "Using default.",
                    lineno,
                    value,
                    name,
                )
                continue

            rules[name] = value

    logger.info("Inspect config: %d rules loaded from %s", len(rules), config_path)

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
        "",
        "# Object Placement rules",
        "# object_placement: views must not reference tables databases",
        "# directly — all access via 1:1 locking view layer.",
        "# Requires object_placement.yaml in the project root.",
        f"object_placement={DEFAULT_RULES['object_placement']}",
        "# view_macro_self_reference: a view selecting from itself",
        "# (or a macro EXECing itself) is always a bug — recursive",
        "# definition fails at deploy time and infinite-loops at",
        "# runtime. Cross-database same-name references are allowed",
        "# (the standard 1:1 locking view pattern).",
        f"view_macro_self_reference={DEFAULT_RULES['view_macro_self_reference']}",
        "",
        "# Grant architecture rules",
        "# public_grant_on_tables: GRANT ... TO PUBLIC on a tables",
        "# database bypasses the placement architecture (tables are",
        "# meant to be private). The rule allows it but warns —",
        "# promote to ERROR if you want to forbid this entirely.",
        f"public_grant_on_tables={DEFAULT_RULES['public_grant_on_tables']}",
        "# review_unmapped_grants: GRANT targets a database that is",
        "# neither a tables nor a views database in your placement",
        "# map. Either add it to database_map in object_placement.yaml",
        "# or confirm it's an out-of-scope database (cross-project,",
        "# external service, etc.). System databases (DBC, SYSLIB,",
        "# TDStats, etc.) are auto-excluded.",
        f"review_unmapped_grants={DEFAULT_RULES['review_unmapped_grants']}",
        "",
        "# Cross-file structural rules",
        "# intra_package_dependency: object lives in a database/user that",
        "# is CREATEd elsewhere in this same package. The package stage",
        "# now auto-splits affected sources into a paired prereqs + main",
        "# bundle, so the structural mistake is fixed transparently at",
        "# build time and this rule defaults to OFF. Set to WARNING or",
        "# ERROR if you want lint-time visibility (e.g. policy-driven",
        "# manual splits, or CI gates that pre-date the auto-split).",
        f"intra_package_dependency={DEFAULT_RULES['intra_package_dependency']}",
    ]
    return "\n".join(lines) + "\n"


# -- Forbidden type suffixes/prefixes --
_TYPE_SUFFIX_RE = re.compile(
    r"(?:_V|_T|_P|_VW|_SP|_TBL|_MCR|_FNC|_TRG|VW_|SP_|TBL_|FN_)\b",
    re.IGNORECASE,
)

# -- Keywords that should be UPPERCASE --
_KEYWORDS = [
    "SELECT",
    "FROM",
    "WHERE",
    "AND",
    "OR",
    "NOT",
    "IN",
    "ON",
    "CREATE",
    "TABLE",
    "VIEW",
    "INDEX",
    "REPLACE",
    "DROP",
    "INSERT",
    "INTO",
    "VALUES",
    "UPDATE",
    "SET",
    "DELETE",
    "GRANT",
    "REVOKE",
    "PRIMARY",
    "UNIQUE",
    "FOREIGN",
    "KEY",
    "REFERENCES",
    "DEFAULT",
    "NULL",
    "NOT",
    "CHARACTER",
    "VARCHAR",
    "INTEGER",
    "DECIMAL",
    "DATE",
    "TIMESTAMP",
    "MULTISET",
    "FALLBACK",
    "JOURNAL",
    "AFTER",
    "BEFORE",
    "AS",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "OUTER",
    "CROSS",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "HAVING",
    "GROUP",
    "ORDER",
    "BY",
    "BETWEEN",
    "LIKE",
    "EXISTS",
    "UNION",
    "ALL",
    "MERGE",
    "USING",
    "MATCHED",
]

# -- Expected extensions by object type --
_EXPECTED_EXT = {
    "TABLE": ".tbl",
    "VIEW": ".viw",
    "MACRO": ".mcr",
    "PROCEDURE": ".spl",
    "FUNCTION": ".fnc",
    "TRIGGER": ".trg",
    "JOIN_INDEX": ".jix",
    "HASH_INDEX": ".idx",
    "INDEX": ".idx",
    "MAP": ".map",
    "ROLE": ".rol",
    "PROFILE": ".prf",
    "AUTHORIZATION": ".auth",
    "FOREIGN_SERVER": ".fsvr",
    # SQLJ install scripts use ``.sjr`` — see ingest._TYPE_TO_EXTENSION
    # for the rationale. Keep these two maps in sync.
    "JAR": ".sjr",
    "SCRIPT_TABLE_OPERATOR": ".sto",
}

# -- Classification patterns (mirrors classifier.py order) --
#
# CRITICAL ORDERING NOTE: PROCEDURE / FUNCTION / MACRO MUST come
# before TABLE. Stored procedures often contain dynamic-SQL string
# literals like ``'CREATE MULTISET TABLE '||...`` for runtime
# table creation. Even with string-literal stripping enabled, the
# defense-in-depth ordering means a procedure file always
# classifies as PROCEDURE — not as TABLE due to a string-literal
# match further down.
_CLASSIFY_PATTERNS = [
    (re.compile(r"CREATE\s+JOIN\s+INDEX\b", re.I), "JOIN_INDEX"),
    (re.compile(r"CREATE\s+HASH\s+INDEX\b", re.I), "HASH_INDEX"),
    (re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I), "INDEX"),
    (
        re.compile(
            r"(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b.*?TABLE\s+OPERATOR",
            re.I | re.DOTALL,
        ),
        "SCRIPT_TABLE_OPERATOR",
    ),
    # Procedure-style first — these have bodies that can contain
    # string-literal SQL building CREATE TABLE etc.
    (re.compile(r"(?:CREATE\s+|REPLACE\s+)PROCEDURE\b", re.I), "PROCEDURE"),
    (
        re.compile(r"(?:CREATE\s+|REPLACE\s+)(?:SPECIFIC\s+)?FUNCTION\b", re.I),
        "FUNCTION",
    ),
    (re.compile(r"(?:CREATE\s+|REPLACE\s+)MACRO\b", re.I), "MACRO"),
    (re.compile(r"(?:CREATE|REPLACE)\s+TRIGGER\b", re.I), "TRIGGER"),
    # Plain DDL types — only reached if no procedural type matched.
    (
        re.compile(
            r"(?:CREATE|REPLACE)\s+(?:MULTISET|SET)?\s*(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b",
            re.I,
        ),
        "TABLE",
    ),
    (re.compile(r"(?:CREATE|REPLACE)\s+VIEW\b", re.I), "VIEW"),
    (re.compile(r"CREATE\s+MAP\b", re.I), "MAP"),
    (re.compile(r"CREATE\s+AUTHORIZATION\b", re.I), "AUTHORIZATION"),
    (re.compile(r"CREATE\s+FOREIGN\s+SERVER\b", re.I), "FOREIGN_SERVER"),
    (re.compile(r"CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(", re.I), "JAR"),
]

# -- System-scope types: no database qualifier, no tokens expected --
_SYSTEM_SCOPE_TYPES = {
    "MAP",
    "ROLE",
    "PROFILE",
    "AUTHORIZATION",
    "FOREIGN_SERVER",
}

# -- Qualified name extraction --
_QUALIFIED_NAME_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)

# -- View/macro definition name (for self-reference rule) --
# Captures the fully qualified name of a VIEW or MACRO being defined,
# handling all three identifier forms used in tokenised projects:
#   1. Literal:    MyDb.MyView
#   2. Tokenised:  {{V_DB}}.MyView
#   3. Quoted:     "MyDb"."MyView"
#
# Two named groups: dbpart (database/token) and objpart (object name).
# Mixed forms (e.g. {{V_DB}}."MyView") are accepted.
_VIEW_MACRO_DEF_NAME_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+(?:VIEW|MACRO)\s+"
    r'(?P<dbpart>"[^"]+"|\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}|[A-Za-z_]\w*)'
    r"\s*\.\s*"
    r'(?P<objpart>"[^"]+"|[A-Za-z_]\w*)',
    re.IGNORECASE,
)

# -- SET/MULTISET detection --
_HAS_SET_MULTISET_RE = re.compile(
    r"CREATE\s+(?:MULTISET|SET)\s+",
    re.I,
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
_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}")

# -- intra_package_dependency rule helpers ----------------------
#
# The rule needs two pieces of information that the existing
# regexes do not provide together:
#
#   1. The names of databases / users CREATEd inside the package
#      (so we can recognise them when they appear as qualifiers).
#   2. The qualifier portion of a CREATE TABLE / VIEW / etc.
#      written in tokenised form -- e.g. ``CREATE TABLE
#      {{MY_DB}}.foo``. The general-purpose ``_QUALIFIED_NAME_RE``
#      above does not accept ``{{TOKEN}}`` in the database slot,
#      so we provide a token-aware variant scoped to this rule.

# Identifier shape: bare ident, quoted ident, or {{TOKEN}}
_PREREQ_IDENT_FRAG = (
    r'(?:\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}|"[^"]+"|[A-Za-z_]\w*)'
)

# CREATE DATABASE <name> | CREATE USER <name>
_CREATE_DATABASE_NAME_RE = re.compile(
    r"\bCREATE\s+DATABASE\s+(" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE,
)
_CREATE_USER_NAME_RE = re.compile(
    r"\bCREATE\s+USER\s+(" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE,
)

# Token-aware qualified-name extractor. Mirrors the structure of
# ``_QUALIFIED_NAME_RE`` above but accepts ``{{TOKEN}}`` in the
# database slot and REQUIRES a two-part qualified name.
_INTRA_QUALIFIED_NAME_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    r"(?P<dbpart>" + _PREREQ_IDENT_FRAG + r")"
    r"\s*\.\s*"
    r"(?P<objpart>" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE,
)

# -- Comment stripping ------------------------------------------
# Imported from the shared sql_text module so validate, ingest,
# and builder all use the same position-preserving implementation.
# Without comment stripping, regex content scans match keywords
# inside /* ... */ header comments and trigger spurious warnings.

from td_release_packager.sql_text import (
    strip_comments_and_string_literals as _strip_sql_comments,
)


# -- Multi-DDL-statement detection --
# Counts ONLY DDL/DCL statements that "create or change an object" —
# the verbs the one-object-per-file discipline cares about.
#
# Deliberately EXCLUDES INSERT / UPDATE / DELETE / MERGE because
# those are DML and they appear legitimately inside procedure /
# trigger / function bodies. Including them caused false positives
# for any real procedure with IF/ELSE branches doing INSERT and
# UPDATE — the body's DML count would push the file over the
# one-object threshold even though it contains exactly one DDL
# statement (the CREATE PROCEDURE).
_STATEMENT_START_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE|DROP|GRANT|REVOKE|ALTER)\b",
    re.IGNORECASE | re.MULTILINE,
)

# ---------------------------------------------------------------
# Grant rule infrastructure — shared by:
#   _check_public_grant_on_tables
#   _check_unmapped_grants
# ---------------------------------------------------------------

# Identifier shape — accepts the three forms that appear in GRANT
# targets: tokens, Teradata-quoted ids, and bare ids.
_GRANT_IDENT = r'(?:\{\{[A-Za-z_]\w*\}\}|"[^"]+"|[A-Za-z_]\w*)'

# Full GRANT statement. Captures privileges, target (db or db.obj),
# and grantees. Permissive on whitespace so multi-line GRANTs match.
_GRANT_STMT_RE = re.compile(
    rf"""
    \bGRANT\b\s+
    (?P<privileges>.+?)
    \s+\bON\b\s+
    (?P<target>
        {_GRANT_IDENT}                # database part
        (?:\s*\.\s*{_GRANT_IDENT}     # optional .object_name
            (?:\s*\([^)]*\))?         # optional (arg_type_list)
        )?
    )
    \s+\bTO\b\s+
    (?P<grantees>.+?)
    (?:\s+\bWITH\b\s+\bGRANT\b\s+\bOPTION\b)?
    \s*;
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# Detect PUBLIC as a standalone grantee. Word boundaries prevent
# false positives on identifiers like 'PUBLIC_REPORTING_ROLE'.
_GRANT_PUBLIC_GRANTEE_RE = re.compile(r"\bPUBLIC\b", re.IGNORECASE)

# Teradata system databases auto-excluded from the unmapped-grants
# rule. These are well-known system catalogs and libraries that
# legitimately appear in cross-database grants but are never going
# to be in a project's placement map. Comparison is upper-case
# (Teradata identifiers are case-insensitive).
#
# 'ALL' is included to handle 'GRANT LOGON ON ALL ...' where ALL
# is a Teradata keyword for "all hosts", not a database name.
_TERADATA_SYSTEM_DATABASES = frozenset(
    s.upper()
    for s in (
        "DBC",
        "SYSLIB",
        "SystemFe",
        "SQLJ",
        "SYSUDTLIB",
        "TDStats",
        "TD_SERVER_DB",
        "TD_SYSGPL",
        "TD_SYSXML",
        "TD_SYSFNLIB",
        "console",
        "crashdumps",
        "LockLogShredder",
        "TDQCM",
        "TDQCD",
        "TDPUSER",
        "TDMAPS",
        "Sys_Calendar",
        "ALL",  # for GRANT LOGON ON ALL ...
    )
)


def _extract_grant_database(target: str) -> str:
    """
    Extract the database part of a GRANT target.

    Examples::

        'D01_MP_OBS_T'           → 'D01_MP_OBS_T'
        'D01_MP_OBS_T.MyTable'   → 'D01_MP_OBS_T'
        '{{OBS_DATABASE_T}}'     → '{{OBS_DATABASE_T}}'
        '"DBC"'                  → '"DBC"'  (quoted form preserved
                                              — caller strips quotes
                                              before comparison)

    Tokens and quoted identifiers cannot contain ``.``, so a simple
    split-on-first-dot is correct for all three identifier forms.
    """
    if "." in target:
        return target.split(".", 1)[0].strip()
    return target.strip()


def _normalise_prereq_name(raw: str) -> str:
    """Strip surrounding double quotes and upper-case a prereq name.

    Token forms (``{{MY_DB}}``) are preserved verbatim — comparison
    is then literal so a tokenised ``CREATE DATABASE {{X}}`` matches a
    tokenised ``CREATE TABLE {{X}}.foo``. Quoted bare names lose their
    quotes so ``"MyDb"`` and ``MyDb`` compare equal under
    Teradata's case-insensitive identifier rules.
    """
    name = raw.strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name.upper()


def _collect_package_prereqs(source_dir: str) -> set:
    """Pre-pass: collect databases / users CREATEd within the package.

    Walks ``source_dir`` for files that can plausibly host a
    ``CREATE DATABASE`` / ``CREATE USER`` statement and extracts the
    created name. The candidate-extension list comes from the
    central discovery resolver, so any project-specific extension
    declared in ``ships.yaml``'s ``discovery.extensions`` block is
    honoured here too — without this, a ``CREATE DATABASE`` in a
    custom-extension file would silently bypass Phase 1's
    intra_package_dependency rule.

    Comments are stripped before matching so a CREATE DATABASE
    appearing inside a header block is not treated as real DDL.

    Args:
        source_dir: Directory walked by ``validate_directory``.

    Returns:
        Set of normalised (upper-cased, token-preserving) database
        and user names. Empty set when the package contains no
        prerequisite-creation statements — in which case the
        per-file rule is silently inactive.
    """
    from td_release_packager.discovery import resolve_harvest_extensions

    candidate_extensions = resolve_harvest_extensions(project_dir=source_dir)
    prereqs: set = set()

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith(".") or f.startswith("_"):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in candidate_extensions:
                continue

            file_path = os.path.join(root, f)
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            clean = _strip_sql_comments(content)
            for regex in (_CREATE_DATABASE_NAME_RE, _CREATE_USER_NAME_RE):
                for match in regex.finditer(clean):
                    name = _normalise_prereq_name(match.group(1))
                    if name:
                        prereqs.add(name)

    return prereqs


@dataclass
class ValidationIssue:
    """A single validation finding."""

    file: str
    rule: str
    severity: str  # 'ERROR' or 'WARNING'
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
    placement: "ObjectPlacement" = None,
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
        placement:      Optional ObjectPlacement engine for the
                        object_placement rule. If None, the rule is
                        skipped silently.

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

    # -- Pre-pass: collect package-internal prerequisite names --
    # Used by the intra_package_dependency rule to decide whether
    # an object's qualifier database / user is created elsewhere
    # in the same package. Empty set => rule is silently inactive.
    package_prereqs = _collect_package_prereqs(source_dir)

    # Discover files. Uses the central resolver so any project-
    # specific extensions declared in ships.yaml's
    # ``discovery.extensions`` block are picked up automatically.
    # ``.jar`` is legacy passthrough — see ingest convention; it's
    # added on top of the resolver's set so existing packages with
    # bare .jar references still get linted even though discovery
    # itself excludes binaries.
    from td_release_packager.discovery import resolve_harvest_extensions

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.add(".jar")

    files = []
    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith(".") or f.startswith("_"):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in extensions:
                files.append(os.path.join(root, f))

    result.files_scanned = len(files)

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            continue

        rel_path = os.path.relpath(file_path, source_dir)
        file_issues = []

        # Strip SQL comments BEFORE running content checks so that
        # words like "CREATE TABLE" appearing inside /* purpose: */
        # block headers don't trigger DDL pattern matches. The
        # stripper preserves newlines so line numbers in any rule's
        # error message remain accurate.
        clean = _strip_sql_comments(content)

        # -- Run all checks, collect raw issues --
        file_issues.extend(_check_db_qualifier(rel_path, clean))
        file_issues.extend(_check_multiset(rel_path, clean))
        file_issues.extend(_check_deploy_intent(rel_path, clean, strict))
        file_issues.extend(_check_view_macro_self_reference(rel_path, clean))
        file_issues.extend(_check_one_object(rel_path, clean))
        file_issues.extend(_check_eponymous(rel_path, clean, file_path))
        file_issues.extend(_check_extension(rel_path, clean, file_path))
        file_issues.extend(_check_type_suffixes(rel_path, clean))
        file_issues.extend(_check_hardcoded_names(rel_path, clean))
        file_issues.extend(_check_keyword_case(rel_path, clean))
        file_issues.extend(_check_leading_commas(rel_path, clean))
        file_issues.extend(
            _check_object_placement(rel_path, clean, file_path, placement)
        )
        file_issues.extend(
            _check_public_grant_on_tables(rel_path, clean, file_path, placement)
        )
        file_issues.extend(
            _check_unmapped_grants(rel_path, clean, file_path, placement)
        )
        file_issues.extend(
            _check_intra_package_dependency(
                rel_path, clean, file_path, package_prereqs
            )
        )

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
        result.files_scanned,
        result.files_passed,
        result.files_with_issues,
        result.errors,
        result.warnings,
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
        name = match.group(1).replace('"', "")
        if "." not in name:
            return [
                ValidationIssue(
                    file=rel_path,
                    rule="db_qualifier",
                    severity="ERROR",
                    message=f"Object '{name}' missing database qualifier. "
                    f"Use Database.{name} syntax.",
                )
            ]
    return []


def _check_multiset(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that tables specify SET or MULTISET."""
    # Only applies to CREATE TABLE statements
    if not re.search(
        r"CREATE\s+(?:MULTISET\s+|SET\s+)?(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b",
        content,
        re.I,
    ):
        return []

    if not _HAS_SET_MULTISET_RE.search(content):
        return [
            ValidationIssue(
                file=rel_path,
                rule="set_multiset",
                severity="WARNING",
                message="CREATE TABLE without SET/MULTISET. "
                "MULTISET will be auto-injected at build time.",
            )
        ]
    return []


def _check_deploy_intent(
    rel_path: str, content: str, strict: bool = False
) -> List[ValidationIssue]:
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
        return [
            ValidationIssue(
                file=rel_path,
                rule="deploy_intent",
                severity="ERROR",
                message=(
                    "Uses REPLACE — use CREATE instead. "
                    "The deployer handles idempotency via "
                    "DROP-and-CREATE with automatic rollback. "
                    "REPLACE overwrites silently with no backup."
                ),
            )
        ]
    return []


def _strip_sql_comments(content: str) -> str:
    """
    Strip SQL comments from content while preserving string offsets.

    Replaces ``--`` line comments and ``/* ... */`` block comments with
    runs of spaces of equal length so that line numbers and character
    positions in the output align with the input. This lets callers
    compute line numbers from match positions in the stripped content
    without an offset translation table.

    Args:
        content: SQL text possibly containing comments.

    Returns:
        Same length as input, with comments blanked to spaces (newlines
        preserved so line counts match).
    """

    # Block comments first: /* ... */ — may span lines, so preserve
    # newlines literally and replace everything else with a space.
    def _blank_block(match: "re.Match") -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    no_block = re.sub(r"/\*.*?\*/", _blank_block, content, flags=re.DOTALL)

    # Line comments: -- to end of line — never span lines, so a flat
    # equal-length space run is sufficient.
    no_line = re.sub(
        r"--[^\n]*",
        lambda m: " " * len(m.group(0)),
        no_block,
    )
    return no_line


def _check_view_macro_self_reference(
    rel_path: str, content: str
) -> List[ValidationIssue]:
    """
    Flag views and macros that reference their own fully qualified
    name in the body.

    A view selecting from itself is always a bug — the definition is
    recursive, the deploy fails, and the resulting object is unusable.
    A macro EXECing itself loops infinitely at runtime. Both cases are
    flagged ERROR by default; there is no legitimate use case.

    The check matches the *fully qualified* name (database segment plus
    object segment), so cross-database same-name references are NOT
    flagged. That preserves the standard 1:1 locking view pattern
    where ``{{V_DB}}.X`` legitimately selects from ``{{T_DB}}.X``.

    Substring collisions are avoided by requiring the matched span to
    end at a non-identifier character: ``{{V}}.Customer`` will not
    match inside ``{{V}}.CustomerOrders``.

    Comments are stripped before searching, so a self-reference inside
    a ``--`` line comment or ``/* ... */`` block comment does not
    trigger the rule.

    Unqualified self-references (e.g. bare ``X`` in a view defined as
    ``{{V}}.X``) are not flagged here -- the ``db_qualifier`` rule
    catches the missing qualifier already.

    Args:
        rel_path: Relative path of the DDL file being checked.
        content: Raw DDL file content.

    Returns:
        List of ValidationIssue — one issue per detected self-reference
        (typically zero or one; multiple matches for the same name
        produce a single issue pointing at the first occurrence).
    """
    # Only views and macros are in scope. Procedures and functions
    # have legitimate recursive patterns and need a separate rule.
    header = _VIEW_MACRO_DEF_NAME_RE.search(content)
    if header is None:
        return []

    db_part = header.group("dbpart").replace('"', "")
    obj_part = header.group("objpart").replace('"', "")
    qualified_name = f"{db_part}.{obj_part}"

    # Body starts immediately after the header match. Comments are
    # stripped so commented-out self-references are not flagged.
    body_offset = header.end()
    stripped_body = _strip_sql_comments(content[body_offset:])

    # Build a search regex that matches the literal qualified name
    # case-insensitively (Teradata identifier rules) and refuses
    # matches that continue into another identifier character. Each
    # segment is allowed an optional surrounding pair of quotes and
    # the dot is allowed surrounding whitespace, so the body match
    # works whether identifiers are bare ('MyDB.MyView'), quoted
    # ('"MyDB"."MyView"'), tokenised ('{{V_DB}}.MyView'), or any
    # mix of the three. The leading side is unambiguous because
    # qualified names start with '"', '{', or a letter.
    name_re = re.compile(
        r'"?' + re.escape(db_part) + r'"?\s*\.\s*'
        r'"?' + re.escape(obj_part) + r'"?'
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )

    body_match = name_re.search(stripped_body)
    if body_match is None:
        return []

    # Compute 1-based line number of the first body match within the
    # full original content.
    abs_pos = body_offset + body_match.start()
    line_num = content[:abs_pos].count("\n") + 1

    return [
        ValidationIssue(
            file=rel_path,
            rule="view_macro_self_reference",
            severity="ERROR",
            line=line_num,
            message=(
                f"References itself: '{qualified_name}' appears in "
                f"the body of its own definition. A view selecting "
                f"from itself is always a bug; a macro EXECing "
                f"itself loops infinitely. Did you mean to reference "
                f"the corresponding tables-database object "
                f"(e.g. the {{{{T_DB}}}} counterpart of "
                f"{{{{V_DB}}}})?"
            ),
        )
    ]


def _check_one_object(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check that the file contains only one DDL statement.

    Counts top-level DDL/DCL verbs (CREATE / REPLACE / DROP /
    GRANT / REVOKE / ALTER). DML verbs like INSERT / UPDATE /
    DELETE / MERGE are NOT counted — they appear legitimately
    inside procedure and trigger bodies.
    """
    matches = _STATEMENT_START_RE.findall(content)
    if len(matches) > 1:
        return [
            ValidationIssue(
                file=rel_path,
                rule="one_object",
                severity="WARNING",
                message=f"File contains {len(matches)} DDL statements. "
                f"Discipline requires one object per file.",
            )
        ]
    return []


def _check_eponymous(
    rel_path: str, content: str, file_path: str
) -> List[ValidationIssue]:
    """Check that filename matches the DDL's Database.ObjectName."""
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    qualified = match.group(1).replace('"', "")
    basename = os.path.splitext(os.path.basename(file_path))[0]

    # Allow {{TOKENS}} in names — they'll be resolved at build time
    if "{{" in basename or "{{" in qualified:
        return []

    if basename.upper() != qualified.upper():
        return [
            ValidationIssue(
                file=rel_path,
                rule="eponymous",
                severity="WARNING",
                message=f"Filename '{basename}' does not match "
                f"DDL object '{qualified}'.",
            )
        ]
    return []


def _check_extension(
    rel_path: str, content: str, file_path: str
) -> List[ValidationIssue]:
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
        return [
            ValidationIssue(
                file=rel_path,
                rule="extension",
                severity="WARNING",
                message=f"Extension '{actual}' — expected '{expected}' for {obj_type}.",
            )
        ]
    return []


def _check_type_suffixes(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check for forbidden type suffixes on object names."""
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    qualified = match.group(1).replace('"', "")
    parts = qualified.split(".")
    obj_name = parts[-1]

    if _TYPE_SUFFIX_RE.search(obj_name):
        return [
            ValidationIssue(
                file=rel_path,
                rule="type_suffix",
                severity="ERROR",
                message=f"Object name '{obj_name}' contains a type suffix "
                f"(_V, _T, VW_, etc.). Object type belongs in the "
                f"database name, not the object name.",
            )
        ]
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
        qualified = match.group(1).replace('"', "")
        parts = qualified.split(".")
        if len(parts) == 2:
            db_name = parts[0]
            # Skip system databases
            system_dbs = {
                "DBC",
                "SYSUDTLIB",
                "SYSLIB",
                "SYSJDBC",
                "TD_SYSFNLIB",
                "TDSTATS",
            }
            if db_name.upper() not in system_dbs:
                return [
                    ValidationIssue(
                        file=rel_path,
                        rule="hardcoded_name",
                        severity="WARNING",
                        message=f"Database name '{db_name}' appears hardcoded. "
                        f"Consider using a {{{{TOKEN}}}} for environment portability.",
                    )
                ]
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
    words = re.findall(r"\b[A-Za-z]+\b", content)
    for word in words:
        if word.upper() in _KEYWORDS:
            total += 1
            if word != word.upper() and word != word.lower():
                pass  # Mixed case — skip
            elif word == word.lower():
                lowercase += 1

    if total > 5 and lowercase / total > 0.3:
        return [
            ValidationIssue(
                file=rel_path,
                rule="keyword_case",
                severity="WARNING",
                message=f"{lowercase}/{total} SQL keywords are lowercase. "
                f"Discipline requires UPPERCASE keywords.",
            )
        ]
    return []


def _check_leading_commas(rel_path: str, content: str) -> List[ValidationIssue]:
    """
    Check for trailing comma convention (should be leading).

    Looks for lines ending with a comma followed by a column/param
    definition on the next line. If more trailing commas than leading,
    reports a warning.
    """
    lines = content.split("\n")
    trailing = 0
    leading = 0

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped.endswith(","):
            trailing += 1
        if stripped.lstrip().startswith(","):
            leading += 1

    # Only report if there's a clear trailing pattern
    if trailing > 3 and leading == 0:
        return [
            ValidationIssue(
                file=rel_path,
                rule="leading_commas",
                severity="WARNING",
                message=f"{trailing} trailing commas found. "
                f"Discipline requires leading commas.",
            )
        ]
    return []


# ---------------------------------------------------------------
# Object Placement rule
# ---------------------------------------------------------------

# Marker comments that identify a 1:1 locking view. If found in
# the file header (first 20 lines), the view is exempt from the
# object_placement rule because it legitimately references the
# tables database.
#
# The recommended marker is:  -- LOCKING VIEW
_LOCKING_VIEW_MARKERS = [
    re.compile(r"--\s*LOCKING\s+VIEW", re.IGNORECASE),
    re.compile(r"--\s*1:1\s+VIEW", re.IGNORECASE),
    re.compile(r"--\s*DIRTY\s+READ\s+VIEW", re.IGNORECASE),
]

# Database-qualified reference: DATABASE.OBJECT
# Also matches {{TOKEN}}.OBJECT for tokenised DDL.
_IDENT_OR_TOKEN_RE = r'(\{\{[A-Za-z_]\w*\}\}|"?[A-Za-z_]\w*"?)'
_DB_QUALIFIED_REF_RE = re.compile(
    r"(?<![.\w])" + _IDENT_OR_TOKEN_RE + r"\." + _IDENT_OR_TOKEN_RE + r"(?![.\w])",
    re.IGNORECASE,
)

# Patterns for excluding comments and string literals from analysis
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _build_exclusion_mask(text: str) -> List[bool]:
    """
    Build a boolean mask marking positions inside comments or
    string literals as True (excluded from analysis).

    Args:
        text: The full SQL text of the file.

    Returns:
        List of booleans, one per character. True = excluded.
    """
    mask = [False] * len(text)
    for pattern in (_BLOCK_COMMENT_RE, _LINE_COMMENT_RE, _STRING_LITERAL_RE):
        for match in pattern.finditer(text):
            for i in range(match.start(), match.end()):
                mask[i] = True
    return mask


def _is_locking_view(content: str) -> bool:
    """
    Determine whether the SQL content represents a 1:1 locking view.

    Detection is based on marker comments in the first 20 lines
    of the file header. The recommended marker is ``-- LOCKING VIEW``.
    Markers are checked case-insensitively.

    Args:
        content: The full SQL text of the view file.

    Returns:
        True if the file is identified as a 1:1 locking view.
    """
    header = "\n".join(content.split("\n")[:20])
    return any(marker.search(header) for marker in _LOCKING_VIEW_MARKERS)


def _strip_identifier_quotes(identifier: str) -> str:
    """Remove surrounding double quotes from a Teradata identifier."""
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1]
    return identifier


def _check_object_placement(
    rel_path: str,
    content: str,
    file_path: str,
    placement: "ObjectPlacement" = None,
) -> List[ValidationIssue]:
    """
    Check that .viw files do not reference tables databases directly.

    All view access should go through the 1:1 locking view layer in
    the views database. This rule is only active when:

        1. An ObjectPlacement engine is provided (from object_placement.yaml).
        2. The placement strategy has locking_views enabled.
        3. The file is a .viw file.
        4. The file is NOT a 1:1 locking view (exempt by
           ``-- LOCKING VIEW`` header marker).

    Args:
        rel_path:  Relative path of the file being checked.
        content:   Raw file content.
        file_path: Absolute path of the file.
        placement: Optional ObjectPlacement engine. If None, the
                   rule is skipped silently.

    Returns:
        List of ValidationIssue — one per offending reference.
    """
    # -- Guard clauses: skip when the rule does not apply --

    # No placement engine → rule is inactive
    if placement is None:
        return []

    # Module not available → rule is inactive
    if not _HAS_PLACEMENT:
        return []

    # Only applies when locking views are enabled
    if not placement.locking_views:
        return []

    # Colocated strategy has no database separation
    if placement.strategy == "colocated":
        return []

    # Only validate .viw files
    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".viw":
        return []

    # Exempt 1:1 locking views (they legitimately reference _T)
    if _is_locking_view(content):
        return []

    # -- Scan for database-qualified references to tables databases --
    exclusion_mask = _build_exclusion_mask(content)
    issues: List[ValidationIssue] = []

    for match in _DB_QUALIFIED_REF_RE.finditer(content):
        # Skip if inside a comment or string literal
        if exclusion_mask[match.start()]:
            continue

        raw_db = match.group(1)
        db_name = _strip_identifier_quotes(raw_db)

        # Check if this database matches the tables pattern
        if not placement.is_tables_database(db_name):
            continue

        line_num = content[: match.start()].count("\n") + 1
        qualified_ref = match.group(0)

        # Build the suggestion with the correct views database
        try:
            views_db = placement.resolve_views_database(db_name)
            suggestion = (
                f"Change '{db_name}' to '{views_db}' so the view "
                f"reads from the 1:1 locking view layer."
            )
        except Exception:
            suggestion = "Views must not reference tables databases directly."

        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="object_placement",
                severity="ERROR",
                line=line_num,
                message=(
                    f"Direct reference to tables database "
                    f"'{db_name}' in '{qualified_ref}'. {suggestion}"
                ),
            )
        )

    return issues


def _check_public_grant_on_tables(
    rel_path: str,
    content: str,
    file_path: str,
    placement: "ObjectPlacement" = None,
) -> List[ValidationIssue]:
    """
    Flag GRANT ... TO PUBLIC statements that target a tables database.

    Tables databases are architecturally private under the SHIPS
    placement standard — read access should flow through the views
    database's locking-view layer. A grant to PUBLIC on a tables
    database bypasses that architecture, exposing every underlying
    table to all users.

    The rule is conservative — it only fires when:

        1. An ObjectPlacement engine is provided.
        2. The placement strategy is NOT 'colocated' (which has no
           tables/views distinction to enforce).
        3. The file is a .grt file.
        4. A GRANT statement's grantee list includes PUBLIC (matched
           with word boundaries — 'PUBLIC_REPORTING_ROLE' does not
           trigger the rule).
        5. The GRANT's target database matches a known tables
           database per the placement engine.

    Tokenised forms (e.g. ``{{OBS_DATABASE_T}}``) are recognised
    when they appear in the placement's ``database_map``.

    Args:
        rel_path:  Relative path of the file being checked.
        content:   Raw file content.
        file_path: Absolute path of the file (used for extension check).
        placement: Optional ObjectPlacement engine. If None, the
                   rule is skipped silently.

    Returns:
        List of ValidationIssue — one per offending GRANT statement.
    """
    # -- Guard clauses: skip when the rule does not apply --

    if placement is None or not _HAS_PLACEMENT:
        return []

    if placement.strategy == "colocated":
        return []

    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".grt":
        return []

    # -- Scan GRANT statements, skipping any inside comments/strings --
    exclusion_mask = _build_exclusion_mask(content)
    issues: List[ValidationIssue] = []

    for match in _GRANT_STMT_RE.finditer(content):
        if exclusion_mask[match.start()]:
            continue

        grantees = match.group("grantees")
        if not _GRANT_PUBLIC_GRANTEE_RE.search(grantees):
            continue

        target = match.group("target")
        database = _extract_grant_database(target)
        db_unquoted = _strip_identifier_quotes(database)

        if not placement.is_tables_database(db_unquoted):
            continue

        line_num = content[: match.start()].count("\n") + 1
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="public_grant_on_tables",
                severity="WARNING",
                line=line_num,
                message=(
                    f"GRANT ... TO PUBLIC on tables database "
                    f"'{database}'. Tables databases are architecturally "
                    f"private under the SHIPS placement standard — read "
                    f"access should flow through the views layer. If "
                    f"this grant is intentional (e.g. cross-database "
                    f"service users, batch processing accounts), "
                    f"consider granting on the corresponding views "
                    f"database instead, or restrict the grantee to a "
                    f"specific role rather than PUBLIC."
                ),
            )
        )

    return issues


def _check_unmapped_grants(
    rel_path: str,
    content: str,
    file_path: str,
    placement: "ObjectPlacement" = None,
) -> List[ValidationIssue]:
    """
    Flag GRANT statements targeting databases not in the placement map.

    Surfaces grants where the target database is neither a tables
    database nor a views database per the placement configuration.
    These warrant review — either the database belongs in the
    placement map and was missed, or the grant is intentionally
    targeting an out-of-scope database (e.g. cross-project grant,
    external service database) and the warning can be silenced for
    that file or the rule disabled.

    Skip conditions:

        1. No ObjectPlacement engine provided.
        2. Placement strategy is 'colocated' (no map to be 'in').
        3. File is not a .grt file.
        4. Target database is in the Teradata system-database
           allowlist (DBC, SYSLIB, TDStats, etc.).
        5. Target database IS in the placement map (as either a
           tables or views database).

    Args:
        rel_path:  Relative path of the file being checked.
        content:   Raw file content.
        file_path: Absolute path of the file.
        placement: Optional ObjectPlacement engine. If None, the
                   rule is skipped silently.

    Returns:
        List of ValidationIssue — one per unmapped GRANT target.
    """
    # -- Guard clauses --

    if placement is None or not _HAS_PLACEMENT:
        return []

    if placement.strategy == "colocated":
        return []

    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".grt":
        return []

    # -- Scan GRANT statements --
    exclusion_mask = _build_exclusion_mask(content)
    issues: List[ValidationIssue] = []

    for match in _GRANT_STMT_RE.finditer(content):
        if exclusion_mask[match.start()]:
            continue

        target = match.group("target")
        database = _extract_grant_database(target)
        db_unquoted = _strip_identifier_quotes(database)
        db_upper = db_unquoted.upper()

        # System databases bypass the rule entirely
        if db_upper in _TERADATA_SYSTEM_DATABASES:
            continue

        # Known tables or views database — already in the map
        if placement.is_tables_database(db_unquoted):
            continue
        if placement.is_views_database(db_unquoted):
            continue

        line_num = content[: match.start()].count("\n") + 1
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="review_unmapped_grants",
                severity="WARNING",
                line=line_num,
                message=(
                    f"GRANT targets database '{database}' which is "
                    f"not in the placement map (neither tables nor "
                    f"views). Either add it to the database_map in "
                    f"object_placement.yaml, or confirm this is an "
                    f"out-of-scope database (e.g. cross-project "
                    f"grant, external service database). Well-known "
                    f"Teradata system databases (DBC, SYSLIB, "
                    f"TDStats, etc.) are auto-excluded."
                ),
            )
        )

    return issues


# ---------------------------------------------------------------
# intra_package_dependency rule
# ---------------------------------------------------------------


def _check_intra_package_dependency(
    rel_path: str,
    content: str,
    file_path: str,
    package_prereqs: set,
) -> List[ValidationIssue]:
    """Flag objects that live in a database CREATEd by the same package.

    SHIPS validates packages with ``deploy --explain``, which runs
    ``EXPLAIN <ddl>`` against the live target. EXPLAIN of
    ``CREATE TABLE x.foo`` requires database ``x`` to already exist
    on the target — but if the same package also contains
    ``CREATE DATABASE x``, that statement has not yet been deployed
    when the dependant is explained, and Teradata DDL is auto-commit
    so a transactional dry-run is impossible.

    The fix is structural: prerequisites belong in their own
    package, deployed first. This rule surfaces the misplacement at
    inspect time so the explain report stays accurate-or-silent
    rather than noisy-but-eventually-correct.

    Args:
        rel_path:        Relative path of the file under check.
        content:         File content (already comment-stripped by
                         the dispatcher).
        file_path:       Absolute path. Used to skip prereq files
                         themselves (``.db`` / ``.usr``) — those
                         CREATE the database and are never the
                         dependant.
        package_prereqs: Upper-cased set of database / user names
                         CREATEd within this package, produced by
                         ``_collect_package_prereqs``.

    Returns:
        Empty list when the rule does not apply (no prereqs in the
        package, or this file is the prereq, or the qualifier does
        not match a prereq). Otherwise a single ValidationIssue
        pointing at the qualifier with a fix-it message.
    """
    # Empty prereq set → rule is silently inactive (no false positives
    # for packages that don't include any CREATE DATABASE/USER).
    if not package_prereqs:
        return []

    # The prereq files themselves are never the dependant. Skip them
    # explicitly even though the qualified-name regex would not match
    # CREATE DATABASE/USER — defence in depth against misclassified
    # files (e.g. a stray ``.db`` containing CREATE TABLE).
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".db", ".usr"):
        return []

    match = _INTRA_QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    db_part_raw = match.group("dbpart").strip()
    db_normalised = _normalise_prereq_name(db_part_raw)
    if db_normalised not in package_prereqs:
        return []

    line_num = content[: match.start("dbpart")].count("\n") + 1

    return [
        ValidationIssue(
            file=rel_path,
            rule="intra_package_dependency",
            severity="ERROR",
            line=line_num,
            message=(
                f"Object lives in database '{db_part_raw}' which is "
                f"CREATEd elsewhere in the same package. SHIPS uses "
                f"EXPLAIN-based dry-run validation against the live "
                f"target — but the prerequisite database does not "
                f"exist on the target until that earlier statement "
                f"is deployed (Teradata DDL is auto-commit, so "
                f"transactional dry-run is not possible). Fix: "
                f"split the package — emit CREATE DATABASE/USER as "
                f"a separate prerequisites package deployed first, "
                f"OR remove the CREATE DATABASE/USER from this "
                f"package if the database already exists in the "
                f"target environment."
            ),
        )
    ]


# ---------------------------------------------------------------
# Public API — wrappers for external callers
# ---------------------------------------------------------------
#
# These wrappers exist so external tools and tests can run individual
# rules against a single file without going through validate_directory.
# Internally they delegate to the same _check_* functions used by the
# dispatcher, so behaviour is identical — but the wrappers handle file
# I/O and accept a severity override that bypasses the inspect.conf
# dispatch loop.


def validate_object_placement(
    path,
    placement,
    severity: str = "ERROR",
) -> List[ValidationIssue]:
    """
    Validate a single file's object placement (public API).

    External wrapper around ``_check_object_placement``. Reads the
    file, runs the check, and applies the requested severity. Used by
    migration tools and integration tests that need to validate one
    file at a time.

    Args:
        path:      Path to the file to validate (str or Path).
        placement: Configured ObjectPlacement engine.
        severity:  Severity to emit on violations. Defaults to ERROR
                   to match the dispatcher's default. Pass 'WARNING'
                   to soften.

    Returns:
        List of ValidationIssue. Empty if the file passes, can't be
        read, or the rule does not apply.
    """
    file_path = str(path)
    rel_path = os.path.basename(file_path)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    issues = _check_object_placement(rel_path, content, file_path, placement)
    if severity != "ERROR":
        for issue in issues:
            issue.severity = severity
    return issues


def is_locking_view(content: str) -> bool:
    """
    Public alias for the 1:1 locking-view header detector.

    A view is treated as a locking view (and exempted from the
    object_placement rule) when its first 20 lines contain one of
    the recognised marker comments — see ``_LOCKING_VIEW_MARKERS``.

    Args:
        content: Full SQL text of the .viw file.

    Returns:
        True if the file is identified as a 1:1 locking view.
    """
    return _is_locking_view(content)
