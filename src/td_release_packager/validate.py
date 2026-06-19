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
    9. CREATE preferred (REPLACE permitted — deployer captures rollback snapshot for both)
   10. Correct file extension per object type
   11. Object placement (views must not reference tables databases directly)
   12. View column list (views should declare an explicit column list before AS)
   13. DDL statement terminator (DDL must end with a semi-colon)

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

from td_release_packager.classifier import (
    TYPE_TO_EXTENSION as _CANONICAL_EXT,
    _CLASSIFY_PATTERNS as _ALL_CLASSIFY_PATTERNS,
)
from td_release_packager.sql_text import (
    strip_comments_and_string_literals as _strip_sql_comments,
)


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
    "deploy_intent": "OFF",
    "one_object": "WARNING",
    "eponymous": "WARNING",
    # Security rules (GAP-003, GAP-008).
    # secret_scan: scan DDL/DML bodies for embedded credentials. ERROR.
    "secret_scan": "ERROR",
    # dynamic_sql: detect EXECUTE IMMEDIATE / DBC.SYSEXECSQL in procedures.
    # WARNING because dynamic SQL has legitimate uses.
    "dynamic_sql": "WARNING",
    # Data governance rules (GAP-009).
    # sensitivity_class: check for .cls companion files. OFF by default;
    # activate with require_sensitivity_class=true in ships.yaml.
    "sensitivity_class": "OFF",
    # Vault / secret ref rule (GAP-011).
    # vault_ref: detect unresolved $env: or vault: prefixes in payload files.
    "vault_ref": "ERROR",
    # Environment token coverage rule.
    # zero_tokens: every deployable DDL/DML object must reference at least one
    # {{TOKEN}} placeholder so it resolves correctly per environment.
    # Files with zero token references have hardcoded environment assumptions
    # and cannot be safely promoted across DEV → TST → PRD.
    "zero_tokens": "ERROR",
    # Extension is ERROR, not WARNING. A staged file whose
    # extension disagrees with its content is the package and the
    # metadata lying to each other — the deployer and any
    # automation reading the payload have to be able to TRUST that
    # *.tbl contains a table, *.spl contains a procedure, etc.
    # Catching the lie at inspect time is the whole point.
    "extension": "ERROR",
    "type_suffix": "ERROR",
    "hardcoded_name": "WARNING",
    # keyword_case: lowercase SQL keywords are a stylistic preference, not
    # a correctness defect — Teradata case-folds them and runs fine. Default
    # to INFO so the rule documents the discipline without cluttering the
    # error/warning counts on legacy onboarding. Projects that enforce
    # UPPERCASE strictly can flip it back via ``config/inspect.conf``.
    "keyword_case": "INFO",
    # comma_log_level controls the SEVERITY of a comma-style finding.
    # Use comma_style to control WHAT is checked (leading/trailing/as-per-source).
    "comma_log_level": "WARNING",
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
    # view_column_list: views should declare an explicit column list
    # between the view name and the AS keyword, e.g.
    #   CREATE VIEW db.MyView (ColA, ColB) AS SELECT ...
    # Omitting the column list makes the view's contract implicit —
    # agents and tooling must introspect the live database to discover
    # column names rather than reading them from source. WARNING by
    # default; promote to ERROR in agent-heavy environments.
    "view_column_list": "WARNING",
    # ddl_terminator: every deployable DDL statement must terminate
    # with a semi-colon. Missing terminators make package parsing,
    # deployment scripting, and downstream agent hand-off ambiguous.
    # Defaults to ERROR because a package should not proceed when the
    # statement boundary is unclear.
    "ddl_terminator": "ERROR",
    # non_ascii: non-ASCII characters in SQL source files cause Teradata
    # Error 6706 ("untranslatable character") on databases created with a
    # LATIN character set (the server default).  Replace em-dashes, bullets,
    # arrows, and box-drawing characters with ASCII equivalents.
    "non_ascii": "ERROR",
    # comment_length: Teradata COMMENT text is limited to 254 characters.
    # Longer COMMENT ON ... IS '...' values fail at deploy time with
    # Error 5550, so inspect catches them before packaging.
    "comment_length": "ERROR",
    # Grant validation severities.
    # ERROR blocks packaging/deployment trust, WARNING/WARN reports but does
    # not block, and OFF suppresses the finding.
    #
    # warn_extra_grants applies only to drift entries that contain extra
    # privileges and no missing inferred privileges. Defaults to WARNING:
    # the operator added grants in the .dcl that SHIPS could not infer
    # from the DDL — that's a soft signal (they may know something the
    # inferrer doesn't), not a packaging failure. Missing inferred grants
    # remain hard errors because required access is absent from the DCL.
    "warn_extra_grants": "WARNING",
    "warn_orphan_grants": "ERROR",
    # Token-resolution collision audit (spec: token-collision audit, step b).
    # A "collision" is two or more tokens whose resolved values match for a
    # given environment. Severity depends on the USAGE ROLE of the colliding
    # tokens, not their names. The role classifier in token_roles.py
    # determines which class each collision falls into; this dispatch decides
    # how severely to report each class.
    #
    # collision_object_identity — two DISTINCT logical objects (databases,
    # users, roles, qualified objects) resolve to the same physical name →
    # deploy-time clobber. ERROR by default; this is the only collision class
    # that should block packaging.
    "collision_object_identity": "ERROR",
    # collision_env_label — env-label roots (SHIPS_ENV, ENV_PREFIX, INSTANCE)
    # share a value. Usually intentional (e.g. AGNOSTIC env). WARNING so the
    # operator sees it without it blocking.
    "collision_env_label": "WARNING",
    # collision_scalar — attribute/scalar tokens (PERM_SPACE, SPOOL_SPACE,
    # numerics) share a value. Expected and harmless; OFF by default.
    "collision_scalar": "OFF",
    # collision_identity_alias — two identity tokens name the SAME logical
    # object (redundant alias). Not dangerous; a DRY-collapse candidate
    # handled by propose-only remediation in a later step. WARNING.
    "collision_identity_alias": "WARNING",
    # collision_allowlist_rejected — emitted when expected_collisions.yaml
    # tried to suppress a REAL clobber. Safety invariant: the allow-list may
    # only downgrade benign classes; an attempt to mask an object-identity
    # clobber is itself a defect. ERROR so the rejected suppression is
    # always visible.
    "collision_allowlist_rejected": "ERROR",
}

# -- Valid severity values --
# Casing convention: UPPER in config files (inspect.conf), lower in
# ships.decisions.json / JSON output. The vocab is the same; the casing
# follows each format's own convention.
#
# INFO is intentionally separate from OFF: INFO emits a visible note
# (e.g. "comma_style=as-per-source: consistency not enforced") that
# appears in the report and is recorded in ships.decisions.json so the
# policy is auditable. OFF is completely silent.
_VALID_SEVERITIES = {"ERROR", "WARNING", "WARN", "INFO", "OFF"}

# -- Comma style configuration --
# Two orthogonal keys control comma placement inspection:
#
#   comma_style    — WHAT to check (domain-valued, not a severity)
#     leading        flag files that use trailing commas (default)
#     trailing       flag files that use leading commas
#     as-per-source  do not enforce any convention; emit a single
#                    INFO note so ships.decisions.json records the policy
#                    explicitly. Pair with comma_log_level=OFF to also
#                    silence the INFO note (no output, no record).
#
#   comma_log_level — HOW SEVERELY to report violations (severity-valued)
#     WARNING  (default)
#     ERROR    block deployment / fail --strict
#     OFF      suppress all comma findings including INFO
DEFAULT_COMMA_STYLE = "leading"
_VALID_COMMA_STYLES = {"leading", "trailing", "as-per-source"}

# Keys in inspect.conf that take domain values rather than severities.
# read_inspect_config handles these separately from severity rules.
_DOMAIN_VALUE_RULES: Dict[str, set] = {
    "comma_style": _VALID_COMMA_STYLES,
}

# Maps a domain-value rule name to the severity key that controls
# how violations from that rule are reported. Used by the dispatch
# loop in validate_directory so the rule name exposed to the user
# (comma_style) stays clean while the severity lives in a
# consistently-named companion key (comma_log_level).
_RULE_LOG_LEVEL_KEY: Dict[str, str] = {
    "comma_style": "comma_log_level",
}


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

            # Domain-value rules (e.g. comma_style) accept a specific
            # vocabulary rather than ERROR/WARNING/OFF.
            if name in _DOMAIN_VALUE_RULES:
                value_lower = value.strip().lower()
                valid = _DOMAIN_VALUE_RULES[name]
                if value_lower not in valid:
                    logger.warning(
                        "inspect.conf line %d: invalid value '%s' "
                        "for '%s' — expected one of %s. Using default.",
                        lineno,
                        value.strip(),
                        name,
                        sorted(valid),
                    )
                    continue
                rules[name] = value_lower
                continue

            value = value.strip().upper()
            if value == "WARN":
                value = "WARNING"

            if value not in _VALID_SEVERITIES:
                logger.warning(
                    "inspect.conf line %d: invalid severity '%s' "
                    "for rule '%s' — expected ERROR, WARNING/WARN, INFO, or OFF. "
                    "Using default.",
                    lineno,
                    value,
                    name,
                )
                continue

            rules[name] = value

    logger.info("Inspect config: %d rules loaded from %s", len(rules), config_path)

    return rules


def read_severity_from_inspect_config(
    rules: Dict[str, str],
    key: str,
    default: Optional[str] = None,
    *,
    strict: bool = False,
) -> str:
    """
    Read a severity-valued key from a parsed inspect.conf rules dict.

    The returned value is normalized to one of ``ERROR``, ``WARNING``,
    ``INFO``, or ``OFF``. ``WARN`` is accepted as an alias for ``WARNING``.

    Args:
        rules:   Dict returned by ``read_inspect_config``.
        key:     Rule key to look up, such as ``warn_extra_grants``.
        default: Explicit fallback severity. When ``None`` (the usual
                 case), the per-key default in ``DEFAULT_RULES`` is
                 used, with a final fallback to ``ERROR`` for keys
                 not registered there.
        strict:  When true, promote ``WARNING`` to ``ERROR`` to match
                 the normal inspect ``--strict`` behaviour.

    Returns:
        Normalized severity string.
    """
    if default is None:
        default = DEFAULT_RULES.get(key, "ERROR")
    value = rules.get(key, default)
    severity = str(value).strip().upper()
    if severity == "WARN":
        severity = "WARNING"
    if severity not in {"ERROR", "WARNING", "INFO", "OFF"}:
        severity = default.strip().upper()
    if strict and severity == "WARNING":
        return "ERROR"
    return severity


def read_bool_from_inspect_config(rules: Dict[str, str], key: str) -> bool:
    """
    Deprecated compatibility wrapper for older callers.

    Prefer ``read_severity_from_inspect_config`` for new code. This returns
    true only when the configured severity is ``WARNING``/``WARN``.
    """
    return read_severity_from_inspect_config(rules, key) == "WARNING"


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
        "# Severity values (for rules that accept severities):",
        "#   ERROR   — must fix before deployment (blocks --strict)",
        "#   WARNING — advisory, does not block deployment",
        "#   INFO    — informational; visible in report and ships.decisions.json",
        "#             but does not count as a failure. Use for rules that",
        "#             record a deliberate policy choice rather than a violation.",
        "#   OFF     — rule is disabled and completely silent",
        "#",
        "# Casing note: use UPPER here (config file convention).",
        "# ships.decisions.json uses lowercase (JSON convention) for the same vocab.",
        "#",
        "# --strict mode promotes all WARNING rules to ERROR.",
        "# OFF rules remain off even in strict mode.",
        "",
        "# Structural rules",
        f"db_qualifier={DEFAULT_RULES['db_qualifier']}",
        f"set_multiset={DEFAULT_RULES['set_multiset']}",
        "# deploy_intent: retired. REPLACE is permitted and fully supported.",
        "# The deployer captures a pre-flight snapshot before executing either verb,",
        "# so rollback coverage is equivalent. Older values are ignored in practice.",
        f"deploy_intent={DEFAULT_RULES['deploy_intent']}",
        f"one_object={DEFAULT_RULES['one_object']}",
        f"eponymous={DEFAULT_RULES['eponymous']}",
        f"extension={DEFAULT_RULES['extension']}",
        f"type_suffix={DEFAULT_RULES['type_suffix']}",
        "",
        "# Style rules",
        f"hardcoded_name={DEFAULT_RULES['hardcoded_name']}",
        f"keyword_case={DEFAULT_RULES['keyword_case']}",
        "#",
        "# comma_style controls WHAT is checked (domain-valued, not a severity):",
        "#   leading      (default) — flag files that use trailing commas.",
        "#   trailing               — flag files that use leading commas.",
        "#   as-per-source          — no enforcement; one INFO note is emitted",
        "#                           so ships.decisions.json records this as a deliberate",
        "#                           policy choice. Pair with comma_log_level=OFF to",
        "#                           silence the INFO note entirely (no screen output,",
        "#                           no record in ships.decisions.json).",
        f"comma_style={DEFAULT_COMMA_STYLE}",
        "#",
        "# comma_log_level controls HOW SEVERELY violations are reported.",
        "# Applies when comma_style is 'leading' or 'trailing'.",
        "# Set to OFF to silence all comma findings including INFO notes.",
        f"comma_log_level={DEFAULT_RULES['comma_log_level']}",
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
        "#",
        "# ---------------------------------------------------------------------------",
        "# Grant validation severities",
        "#",
        "# These settings control how SHIPS reacts when the cross-file grant",
        "# validation (Step 2 of Inspect) finds a discrepancy between what the",
        "# DDL implies should be granted and what is in the .dcl files.",
        "#",
        "# IMPORTANT: grants that SHIPS inferred from the DDL but are completely",
        "# absent from the .dcl files are ALWAYS a hard error regardless of these",
        "# settings — required access is missing and the deployment will fail.",
        "# Run 'ships inspect --fix-grants' to append missing statements.",
        "#",
        "# warn_extra_grants",
        "#   Controls .dcl files that contain privileges BEYOND what SHIPS inferred",
        "#   from the DDL — i.e. grants you added manually to the .dcl file.",
        "#",
        "#   ERROR             — any privilege not inferred from DDL is treated as",
        "#                       drift and blocks packaging. Use this posture when",
        "#                       the .dcl files should be a pure reflection of the",
        "#                       DDL with no manual additions.",
        "#   WARNING (default) — extra privileges are reported but do not block",
        "#                       packaging. The operator may have added grants",
        "#                       the inferrer can't derive from DDL; that's a",
        "#                       soft signal, not a build failure.",
        "#   OFF               — extra privileges are silently accepted. Use when",
        "#                       the .dcl files are intentionally richer than what",
        "#                       SHIPS infers and you do not want any noise.",
        f"warn_extra_grants={DEFAULT_RULES['warn_extra_grants']}",
        "#",
        "# warn_orphan_grants",
        "#   Controls .dcl files for a grantee that no DDL in the package implies —",
        "#   i.e. the file exists but SHIPS found no DDL reference that would",
        "#   require access to be granted to that grantee.",
        "#",
        "#   Common legitimate causes:",
        "#     - A role is granted database access inside this package, but",
        "#       GRANT ROLE … TO USER is managed outside it (by a DBA, IGA",
        "#       system, or autonomous agent).",
        "#     - The package pre-provisions access rights that a downstream",
        "#       process or separate package will activate.",
        "#",
        "#   ERROR   (default) — orphaned .dcl files block packaging. Use this",
        "#                       posture for fully self-contained packages where",
        "#                       every grant must be traceable to DDL in this",
        "#                       package.",
        "#   WARNING           — orphaned .dcl files are reported but do not",
        "#                       block packaging.",
        "#   OFF               — orphaned .dcl files are silently accepted.",
        "#",
        "# Note: orphaned .dcl files are never auto-deleted by --fix-grants.",
        "# They require manual review and removal.",
        f"warn_orphan_grants={DEFAULT_RULES['warn_orphan_grants']}",
        "",
        "",
        "# Security rules (GAP-003, GAP-008)",
        "# secret_scan: scan DDL/DML file bodies for embedded credentials.",
        "# Reports SECRET_PATTERN_DETECTED. Defaults to ERROR.",
        f"secret_scan={DEFAULT_RULES['secret_scan']}",
        "# dynamic_sql: detect EXECUTE IMMEDIATE / DBC.SYSEXECSQL in procedures.",
        "# Reports DYNAMIC_SQL_DETECTED. Defaults to WARNING (dynamic SQL has",
        "# legitimate uses — promote to ERROR to enforce a blanket ban).",
        f"dynamic_sql={DEFAULT_RULES['dynamic_sql']}",
        "",
        "# Data governance rules (GAP-009)",
        "# sensitivity_class: check for .cls companion files alongside DDL/view objects.",
        "# OFF by default — set to WARNING or ERROR to enforce.",
        f"sensitivity_class={DEFAULT_RULES['sensitivity_class']}",
        "",
        "# Vault / secret reference rule (GAP-011)",
        "# vault_ref: detect unresolved $env: or vault: prefixes in payload files.",
        "# Defaults to ERROR — these should never appear in deployed payload.",
        f"vault_ref={DEFAULT_RULES['vault_ref']}",
        "",
        "# Environment token coverage rule",
        "# zero_tokens: every deployable DDL/DML object must reference at least one",
        "# {{TOKEN}} placeholder. Files with zero tokens have hardcoded environment",
        "# assumptions and cannot be safely promoted across environments.",
        "# Defaults to ERROR. Set to WARNING for gradual migration of legacy codebases,",
        "# or OFF to disable while performing the initial tokenisation sweep.",
        f"zero_tokens={DEFAULT_RULES['zero_tokens']}",
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
        "",
        "# Agent-friendliness rules",
        "# view_column_list: views should declare an explicit column list between",
        "# the view name and the AS keyword, e.g.",
        "#   CREATE VIEW db.MyView (ColA, ColB) AS SELECT ...",
        "# Omitting the list makes the view's schema contract implicit — agents",
        "# and tooling must introspect the live database to discover column names",
        "# rather than reading them from source. WARNING by default; promote to",
        "# ERROR in agent-heavy environments. Set to OFF to disable entirely.",
        f"view_column_list={DEFAULT_RULES['view_column_list']}",
        "",
        "# Statement boundary rules",
        "# ddl_terminator: every deployable DDL statement must terminate with",
        "# a semi-colon (;). Missing terminators make package parsing,",
        "# deployment scripting, and downstream agent hand-off ambiguous.",
        "# Defaults to ERROR. Set to WARNING for gradual adoption or OFF to disable.",
        f"ddl_terminator={DEFAULT_RULES['ddl_terminator']}",
        "#",
        "# non_ascii: non-ASCII characters in SQL source files cause Teradata",
        "# Error 6706 on databases created with a LATIN character set (the server",
        "# default). Replace em-dashes, bullets, arrows, and box-drawing characters",
        "# with ASCII equivalents before packaging.",
        "# Defaults to ERROR because the failure is silent until deploy time.",
        f"non_ascii={DEFAULT_RULES['non_ascii']}",
        "#",
        "# comment_length: COMMENT ON ... IS '...' text must be <= 254",
        "# characters. Teradata rejects longer comment strings with Error 5550.",
        "# Defaults to ERROR because this is a deterministic deploy-time failure.",
        f"comment_length={DEFAULT_RULES['comment_length']}",
        "",
        "",
        "# Token-resolution collision audit",
        "#",
        "# A 'collision' is two or more tokens whose resolved values match for",
        "# a given environment. Severity depends on the usage ROLE of the",
        "# colliding tokens, not their names.",
        "#",
        "# collision_object_identity: two DISTINCT logical objects resolve to",
        "# the same physical name — a deploy-time clobber. ERROR (default).",
        "# This is the only collision class that should block packaging.",
        f"collision_object_identity={DEFAULT_RULES['collision_object_identity']}",
        "#",
        "# collision_env_label: env-label roots (SHIPS_ENV, ENV_PREFIX,",
        "# INSTANCE) share a value. Usually intentional (e.g. AGNOSTIC).",
        "# WARNING (default).",
        f"collision_env_label={DEFAULT_RULES['collision_env_label']}",
        "#",
        "# collision_scalar: attribute/scalar tokens (PERM_SPACE, SPOOL_SPACE,",
        "# numerics) share a value. Expected and harmless. OFF (default).",
        f"collision_scalar={DEFAULT_RULES['collision_scalar']}",
        "#",
        "# collision_identity_alias: two identity tokens name the SAME object",
        "# (redundant alias). Not dangerous; a DRY-collapse candidate handled",
        "# by propose-only remediation. WARNING (default).",
        f"collision_identity_alias={DEFAULT_RULES['collision_identity_alias']}",
        "#",
        "# collision_allowlist_rejected: expected_collisions.yaml tried to",
        "# suppress a REAL clobber. Always ERROR — the suppression is denied.",
        f"collision_allowlist_rejected={DEFAULT_RULES['collision_allowlist_rejected']}",
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
# Derived from the canonical TYPE_TO_EXTENSION in classifier.py.
# Excludes types that validate does not check extensions for:
# prerequisites (DATABASE, USER), DCL (GRANT, REVOKE), metadata
# (COMMENT, STATISTICS), and binary support files (C_SOURCE, C_HEADER).
_EXPECTED_EXT = {
    k: v
    for k, v in _CANONICAL_EXT.items()
    if k
    not in {
        "DATABASE",
        "USER",
        "GRANT",
        "REVOKE",
        "COMMENT",
        "STATISTICS",
        "C_SOURCE",
        "C_HEADER",
    }
}

# -- Classification patterns --
# Filtered view of the canonical patterns from classifier.py.
# Excludes types that validate does not lint (prerequisites, DCL, metadata).
_VALIDATE_OMIT = frozenset(
    {"DATABASE", "USER", "PROFILE", "ROLE", "COMMENT", "GRANT", "REVOKE"}
)
_CLASSIFY_PATTERNS = [
    (p, t) for p, t in _ALL_CLASSIFY_PATTERNS if t not in _VALIDATE_OMIT
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
#
# Anchored to start-of-statement (``^\s*`` + ``re.MULTILINE``) so a
# DDL verb appearing inside another statement -- e.g. the ``CREATE
# PROCEDURE`` privilege inside ``GRANT CREATE PROCEDURE ON db TO
# user`` -- doesn't get its capture group greedily claim the next
# token (which would be ``ON``) as the object name. See
# classifier.py for the full rationale.
_QUALIFIED_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE | re.MULTILINE,
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
# Anchored — see _QUALIFIED_NAME_RE.
_VIEW_MACRO_DEF_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:VIEW|MACRO)\s+"
    r'(?P<dbpart>"[^"]+"|\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}|[A-Za-z_]\w*)'
    r"\s*\.\s*"
    r'(?P<objpart>"[^"]+"|[A-Za-z_]\w*)',
    re.IGNORECASE | re.MULTILINE,
)

# -- View-without-column-list detection (Issue #133) --
#
# Matches a CREATE/REPLACE VIEW header that jumps straight to AS without
# declaring a column list in parentheses first.  The pattern intentionally
# ends at AS so that a view WITH a column list (the correct form) does not
# match at all:
#
#   No column list (fires):
#     CREATE VIEW db.MyView AS SELECT ...
#     REPLACE VIEW "db"."MyView" AS SELECT ...
#     CREATE VIEW {{V_DB}}.MyView   AS SELECT ...      ← token form
#
#   With column list (does NOT fire):
#     CREATE VIEW db.MyView (ColA, ColB) AS SELECT ...
#
# Identifier forms recognised in the view name:
#   bare ident    [A-Za-z_]\w*
#   double-quoted \"[^\"]+\"
#   token         \{\{[A-Za-z_][A-Za-z0-9_-]*\}\}
#
# The three forms are combined into _VCL_IDENT_FRAG and used for both
# the database segment and the object segment.  A dot with optional
# surrounding whitespace separates the two segments.
#
# The negative look-ahead ``(?!\s*\()`` rejects headers that are
# immediately followed by an opening parenthesis — those carry an
# explicit column list and are therefore compliant.
_VCL_IDENT_FRAG = (
    r"(?:\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}"  # {{TOKEN}}
    r'|"[^"]+"'  # "Quoted Identifier"
    r"|[A-Za-z_]\w*)"  # bare_identifier
)
_VIEW_NO_COLUMN_LIST_RE = re.compile(
    r"^\s*\b(?:CREATE|REPLACE)\b\s+\bVIEW\b\s+"
    + _VCL_IDENT_FRAG  # database / schema part
    + r"\s*\.\s*"  # dot separator
    + _VCL_IDENT_FRAG  # object name part
    + r"\s*(?!\s*\()\s*\bAS\b",  # AS with NO preceding '(' → no column list
    re.IGNORECASE | re.MULTILINE,
)

# -- SET/MULTISET detection --
# Anchored to start-of-statement: a procedure body that mentions
# ``SET`` (Teradata's variable assignment keyword) followed by an
# upstream ``CREATE`` keyword in a comment must not be mistaken
# for a real ``CREATE SET TABLE`` declaration.
_HAS_SET_MULTISET_RE = re.compile(
    r"^\s*CREATE\s+(?:MULTISET|SET)\s+",
    re.IGNORECASE | re.MULTILINE,
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
_PREREQ_IDENT_FRAG = r'(?:\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}|"[^"]+"|[A-Za-z_]\w*)'

# CREATE DATABASE <name> | CREATE USER <name>
# Anchored — without ``^\s*`` a GRANT statement listing ``CREATE
# DATABASE`` as a privilege would be scooped up as a real database
# creation. See classifier.py for the full rationale.
_CREATE_DATABASE_NAME_RE = re.compile(
    r"^\s*CREATE\s+DATABASE\s+(" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE | re.MULTILINE,
)
_CREATE_USER_NAME_RE = re.compile(
    r"^\s*CREATE\s+USER\s+(" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE | re.MULTILINE,
)

# Token-aware qualified-name extractor. Mirrors the structure of
# ``_QUALIFIED_NAME_RE`` above but accepts ``{{TOKEN}}`` in the
# database slot and REQUIRES a two-part qualified name.
# Anchored — see _QUALIFIED_NAME_RE.
_INTRA_QUALIFIED_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    r"(?P<dbpart>" + _PREREQ_IDENT_FRAG + r")"
    r"\s*\.\s*"
    r"(?P<objpart>" + _PREREQ_IDENT_FRAG + r")",
    re.IGNORECASE | re.MULTILINE,
)

# -- Comment stripping ------------------------------------------
# Imported from the shared sql_text module so validate, ingest,
# and builder all use the same position-preserving implementation.
# Without comment stripping, regex content scans match keywords
# inside /* ... */ header comments and trigger spurious warnings.


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

# -- DDL statement terminator detection --
#
# This rule is intentionally scoped to DDL verbs rather than DML.  It
# validates statement boundaries for deployable object definitions and
# structural changes while avoiding false positives on ordinary DML that
# may legitimately appear inside stored procedure / trigger bodies.
_DDL_TERMINATOR_START_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE|DROP|ALTER)\b",
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


_GENERATED_DIR_NAMES = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ships",
        ".ships-work",
        "__pycache__",
        "_rollback",
        "releases",
    }
)


def _prune_generated_dirs(dirs: list[str]) -> None:
    """Prevent validation from walking SHIPS-generated artefact directories."""
    dirs[:] = [d for d in dirs if d not in _GENERATED_DIR_NAMES]


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
        _prune_generated_dirs(dirs)
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


def resolve_inspect_root(project_dir: str) -> str:
    """Return the directory ``inspect`` should walk for a SHIPS project.

    The deployable artefact of a SHIPS project lives at
    ``payload/database/``. Linting must focus on that artefact, not on
    user-owned scratch directories that sit alongside it at the project
    root (``___extras/``, original source dumps, fabrication scripts,
    spreadsheets). Falls back to ``project_dir`` itself when no payload
    subtree exists — preserves the bare-directory behaviour relied on
    by unit tests and by callers running inspect against an arbitrary
    directory.
    """
    payload = os.path.join(project_dir, "payload", "database")
    return payload if os.path.isdir(payload) else project_dir


def validate_directory(
    source_dir: str,
    rules_config: Dict[str, str] = None,
    strict: bool = False,
    placement: "ObjectPlacement" = None,
) -> ValidationResult:
    """
    Validate all DDL files in a directory against the Coding Discipline.

    Thin traced wrapper — see ``_validate_directory_impl`` for the full
    implementation.  Emits a ``ships.validate`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.
    """
    from ships_tracing import stage_span

    with stage_span(
        "ships.validate",
        **{"ships.source_dir": source_dir, "ships.strict": strict},
    ) as _span:
        result = _validate_directory_impl(
            source_dir,
            rules_config=rules_config,
            strict=strict,
            placement=placement,
        )
        _span.set_attribute("ships.files_scanned", result.files_scanned)
        _span.set_attribute("ships.issues_errors", result.errors)
        _span.set_attribute("ships.issues_warnings", result.warnings)
        _span.set_attribute("ships.passed", result.passed)
        return result


def _validate_directory_impl(
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
        _prune_generated_dirs(dirs)
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
        file_issues.extend(_check_security(rel_path, content, file_path, rules_config))
        file_issues.extend(_check_db_qualifier(rel_path, clean))
        file_issues.extend(_check_multiset(rel_path, clean))
        file_issues.extend(_check_deploy_intent(rel_path, clean, strict))
        file_issues.extend(_check_ddl_terminator(rel_path, clean))
        file_issues.extend(_check_view_macro_self_reference(rel_path, clean))
        file_issues.extend(_check_one_object(rel_path, clean))
        file_issues.extend(_check_eponymous(rel_path, clean, file_path))
        file_issues.extend(_check_extension(rel_path, clean, file_path))
        file_issues.extend(_check_type_suffixes(rel_path, clean))
        file_issues.extend(_check_hardcoded_names(rel_path, clean))
        file_issues.extend(_check_zero_tokens(rel_path, clean))
        file_issues.extend(_check_keyword_case(rel_path, clean))
        file_issues.extend(
            _check_leading_commas(
                rel_path,
                clean,
                style=rules_config.get("comma_style", DEFAULT_COMMA_STYLE),
            )
        )
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
            _check_intra_package_dependency(rel_path, clean, file_path, package_prereqs)
        )
        file_issues.extend(_check_view_column_list(rel_path, clean))
        file_issues.extend(_check_comment_length(rel_path, content, rules_config))
        file_issues.extend(_check_non_ascii_literals(rel_path, content))

        # -- Apply rule config: remap severity or drop OFF rules --
        # INFO issues are informational and not configurable —
        # they pass through unchanged.
        filtered_issues = []
        for issue in file_issues:
            # Resolve the severity key — domain-value rules (e.g. comma_style)
            # have a companion severity key (e.g. comma_log_level) that controls
            # how their findings are reported, including INFO-level notes.
            severity_key = _RULE_LOG_LEVEL_KEY.get(issue.rule, issue.rule)
            configured_severity = rules_config.get(severity_key, "WARNING")
            if configured_severity == "OFF":
                continue  # Rule silenced — drop the issue (including INFO notes)
            # INFO findings are informational; keep their severity unchanged
            # unless the rule has been explicitly set to OFF above.
            if issue.severity != "INFO":
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
    """Check that CREATE TABLE statements specify SET or MULTISET.

    DCL files (``.dcl`` / ``.grt``) are excluded by extension: a
    ``GRANT CREATE TABLE ON … TO …`` clause mentions the literal
    phrase "CREATE TABLE" without issuing a CREATE TABLE statement,
    and these extensions can never legitimately carry table DDL.

    Inside DDL files we still split on ``;`` and only match CREATE
    TABLE at statement start — so a ``GRANT CREATE TABLE`` clause that
    legitimately appears in a mixed-DCL/.sql script is also ignored.
    Comments and string literals were already stripped before this
    rule runs, so ``;`` is a safe statement separator.
    """
    if os.path.splitext(rel_path)[1].lower() in {".dcl", ".grt", ".grants"}:
        return []

    table_pattern = re.compile(
        r"^\s*CREATE\s+(?:MULTISET\s+|SET\s+)?"
        r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
        r"(?:TRACE\s+)?TABLE\b",
        re.I,
    )

    for statement in content.split(";"):
        if table_pattern.match(statement) and not _HAS_SET_MULTISET_RE.search(
            statement
        ):
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
    Preserve the retired deploy_intent rule hook without flagging REPLACE.

    REPLACE is normal Teradata DDL style and is fully supported by SHIPS:
    the deployer records the verb as deployment intent, captures rollback
    snapshots with SHOW, and routes REPLACE-capable objects through the
    replace-in-place strategy. The old CREATE-preferred advisory made large
    legacy codebases look broken despite being deployable, so the rule now
    stays silent even if older inspect.conf files still mention it.

    Args:
        rel_path: Relative path of the DDL file being checked.
        content:  Raw DDL file content.
        strict:   When True, promotes WARNING issues to ERROR.

    Returns:
        Empty list. Kept for API compatibility with older tests/extensions.
    """
    return []


def _check_ddl_terminator(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that each DDL statement terminates with a semi-colon.

    The inspector packages DDL as discrete, agent-readable artefacts.
    A missing statement terminator makes the statement boundary implicit,
    which is fragile for deployment scripting and downstream automation.

    The caller passes comment/string-literal-stripped SQL, so trailing
    comments after a valid semi-colon are ignored and text such as
    ``'CREATE VIEW ...'`` inside dynamic SQL strings does not produce a
    false DDL match.

    Args:
        rel_path: Relative path of the file being checked.
        content:  Comment/string-literal-stripped SQL content.

    Returns:
        One ValidationIssue per DDL statement segment that does not end
        with ``;``; otherwise an empty list.
    """
    matches = list(_DDL_TERMINATOR_START_RE.finditer(content))
    if not matches:
        return []

    issues: List[ValidationIssue] = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        statement_text = content[match.start() : end].rstrip()
        if not statement_text or statement_text.endswith(";"):
            continue

        line_num = content[: match.start()].count("\n") + 1
        verb = match.group(0).strip().split()[0].upper()
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="ddl_terminator",
                severity="ERROR",
                line=line_num,
                message=(
                    f"{verb} DDL statement does not terminate with a semi-colon (;). "
                    "Add an explicit terminator so deployment scripts and "
                    "downstream agents can determine the statement boundary reliably."
                ),
            )
        )

    return issues


# ---------------------------------------------------------------
# DDL terminator auto-fix (#253)
# ---------------------------------------------------------------


@dataclass
class DDLTerminatorFix:
    """One file that was rewritten by ``fix_ddl_terminators``."""

    file: str  # path relative to source_dir
    statements_fixed: int


@dataclass
class DDLTerminatorFixResult:
    """Aggregate result of a ``fix_ddl_terminators`` run."""

    files_scanned: int = 0
    files_fixed: List[DDLTerminatorFix] = field(default_factory=list)

    @property
    def files_written(self) -> int:
        return len(self.files_fixed)

    @property
    def statements_fixed(self) -> int:
        return sum(f.statements_fixed for f in self.files_fixed)

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_written": self.files_written,
            "statements_fixed": self.statements_fixed,
            "files": [
                {"file": f.file, "statements_fixed": f.statements_fixed}
                for f in self.files_fixed
            ],
        }


def _compute_terminator_insertions(stripped: str, raw: str) -> List[int]:
    """Return raw-content offsets where ``;`` should be inserted.

    The detector walks DDL verb starts in the comment-/string-stripped
    text. ``strip_comments_and_string_literals`` preserves character
    positions, so a stripped offset maps 1:1 to the raw content.

    For each segment whose stripped tail does not end with ``;``, we
    locate the last non-whitespace character of the segment in the
    *raw* content and report the index immediately AFTER it. Inserting
    ``;`` at that offset puts the terminator flush against the final
    token while preserving any trailing whitespace or comments.

    A file with no matching DDL verbs returns ``[]``.
    """
    matches = list(_DDL_TERMINATOR_START_RE.finditer(stripped))
    if not matches:
        return []

    insertions: List[int] = []
    for idx, match in enumerate(matches):
        seg_start = match.start()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(stripped)
        # Use the stripped text to decide whether a terminator is missing,
        # exactly as the detector does. This stops us from "fixing" a
        # statement that already has a ``;`` followed only by comments.
        seg_stripped = stripped[seg_start:seg_end].rstrip()
        if not seg_stripped or seg_stripped.endswith(";"):
            continue

        # Walk back through the STRIPPED segment to find the last
        # non-whitespace character. The stripper blanks comments and
        # string literals with spaces of the same length (positions
        # preserved), so trailing comments after the DDL are treated
        # as whitespace here — exactly what we want. Using ``raw``
        # would land the new ``;`` inside a trailing comment.
        insert_at = seg_end
        while insert_at > seg_start and stripped[insert_at - 1].isspace():
            insert_at -= 1
        if insert_at == seg_start:
            # Defensive guard: an all-whitespace segment cannot need a
            # terminator. Should not be reachable because the detector
            # would have skipped it too.
            continue
        insertions.append(insert_at)

    return insertions


def fix_ddl_terminators(
    source_dir: str, dry_run: bool = False
) -> DDLTerminatorFixResult:
    """Add missing ``;`` terminators to deployable DDL statements.

    Walks ``source_dir`` using the same file-discovery rules as
    ``validate_dir`` (same extensions, same generated-path exclusions),
    re-uses the detector's boundary regex on the comment-/string-
    stripped content, then inserts a semi-colon at the last non-
    whitespace character of each violating statement segment in the
    *raw* file.

    Files that need no changes are not touched. Files inside SHIPS-
    generated paths (``releases/``, ``.ships-work/``, ``_rollback/``)
    are skipped entirely.

    The fix is idempotent: running it twice on a clean tree leaves the
    second run with ``files_written == 0``.

    Args:
        source_dir: Directory to walk.
        dry_run:    When True, compute the fix list but do not write
                    any file.  The returned result still reports
                    ``files_fixed`` (i.e. what *would* have changed).
    """
    from td_release_packager.discovery import resolve_harvest_extensions

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.add(".jar")

    result = DDLTerminatorFixResult()

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            stripped = _strip_sql_comments(raw)
            insertions = _compute_terminator_insertions(stripped, raw)
            if not insertions:
                continue

            # Apply insertions right-to-left so earlier offsets stay
            # valid as we mutate the buffer.
            new_content = raw
            for offset in sorted(insertions, reverse=True):
                new_content = new_content[:offset] + ";" + new_content[offset:]

            if not dry_run:
                try:
                    with open(file_path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(new_content)
                except OSError:
                    continue

            rel_path = os.path.relpath(file_path, source_dir)
            result.files_fixed.append(
                DDLTerminatorFix(file=rel_path, statements_fixed=len(insertions))
            )

    return result


# ---------------------------------------------------------------
# Non-ASCII auto-fix (#257)
# ---------------------------------------------------------------

# Codepoints whose lossless ASCII equivalent is well-known. These are
# the rich-text characters word processors and rich-text editors inject
# silently. Every entry here MUST be a substitution that preserves
# meaning — there is no "best guess" mode. U+FFFD is deliberately NOT
# in this set: the original byte is lost, so we cannot substitute
# safely.
_NON_ASCII_AUTO_FIX_REPLACEMENTS: Dict[str, str] = {
    "—": " - ",  # em-dash → spaced hyphen (NOT "--" — that opens a SQL comment)
    "•": "-",  # bullet → hyphen
    "→": "->",  # rightwards arrow → ASCII arrow
    "─": "-",  # box drawings light horizontal → hyphen
}


@dataclass
class NonAsciiFix:
    """One file that was rewritten by ``fix_non_ascii``."""

    file: str  # path relative to source_dir
    substitutions: Dict[str, int] = field(default_factory=dict)

    @property
    def total_chars_substituted(self) -> int:
        return sum(self.substitutions.values())

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "total_chars_substituted": self.total_chars_substituted,
            "substitutions": {
                f"U+{ord(c):04X}": count for c, count in self.substitutions.items()
            },
        }


@dataclass
class NonAsciiFixResult:
    """Aggregate result of a ``fix_non_ascii`` run."""

    files_scanned: int = 0
    files_fixed: List[NonAsciiFix] = field(default_factory=list)

    @property
    def files_written(self) -> int:
        return len(self.files_fixed)

    @property
    def chars_substituted(self) -> int:
        return sum(f.total_chars_substituted for f in self.files_fixed)

    def to_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "files_written": self.files_written,
            "chars_substituted": self.chars_substituted,
            "files": [f.to_dict() for f in self.files_fixed],
        }


def fix_non_ascii(source_dir: str, dry_run: bool = False) -> NonAsciiFixResult:
    """Substitute non-ASCII characters that have a known ASCII equivalent.

    Walks ``source_dir`` using the same file-discovery rules as
    ``validate_directory`` (same extensions, same generated-path
    exclusions), reads each file as UTF-8 (strict — non-UTF-8 files
    are skipped), and replaces every character whose codepoint is in
    ``_NON_ASCII_AUTO_FIX_REPLACEMENTS`` with the documented ASCII
    equivalent.

    Characters NOT in the map (notably U+FFFD — unrecoverable; the
    original byte is gone) are deliberately left alone and will still
    surface as ``[non_ascii]`` findings on the next ``inspect`` run.

    Files inside SHIPS-generated paths (``releases/``, ``.ships-work/``,
    ``_rollback/``) are skipped.

    The fix is idempotent: a clean re-run produces ``files_written == 0``.
    """
    from td_release_packager.discovery import resolve_harvest_extensions

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.add(".jar")

    result = NonAsciiFixResult()

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            # Fast path: nothing to do.
            if not any(ch in raw for ch in _NON_ASCII_AUTO_FIX_REPLACEMENTS):
                continue

            counts: Dict[str, int] = {}
            new_content = raw
            for ch, replacement in _NON_ASCII_AUTO_FIX_REPLACEMENTS.items():
                if ch not in new_content:
                    continue
                counts[ch] = new_content.count(ch)
                new_content = new_content.replace(ch, replacement)

            if new_content == raw:
                continue

            if not dry_run:
                try:
                    with open(file_path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(new_content)
                except OSError:
                    continue

            rel_path = os.path.relpath(file_path, source_dir)
            result.files_fixed.append(NonAsciiFix(file=rel_path, substitutions=counts))

    return result


# NOTE: An older comment-only ``_strip_sql_comments`` used to live
# here and silently shadowed the import at the top of the module.
# Removed: every caller wants both comments AND string literals
# stripped (otherwise dynamic-SQL strings like ``'CREATE TABLE ...'``
# inside procedure bodies trigger spurious set_multiset / qualifier
# warnings — see the regression test in test_validate.py for the
# canonical reproducer). The shared
# ``td_release_packager.sql_text.strip_comments_and_string_literals``
# (imported as ``_strip_sql_comments``) is the single source of
# truth.


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

    DCL/grant files are intentionally grouped by grantee or
    deployment concern, so multiple GRANT / REVOKE statements in a
    ``.dcl`` or ``.grt`` file are valid and useful.

    Counts top-level DDL/DCL verbs (CREATE / REPLACE / DROP /
    GRANT / REVOKE / ALTER). DML verbs like INSERT / UPDATE /
    DELETE / MERGE are NOT counted — they appear legitimately
    inside procedure and trigger bodies.
    """
    if os.path.splitext(rel_path)[1].lower() in {".dcl", ".grt"}:
        return []

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


def _check_zero_tokens(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that every deployable DDL/DML object is usable by SHIPS for tokenisation.

    Three cases, in order of environment awareness:

        1. {{TOKEN}} present  → PASS.  The file is already tokenised.
        2. Hardcoded Database.Object name, no token  → PASS.  SHIPS can detect the
           literal database name and auto-tokenise it via ``--auto-tokenise``.
           The ``hardcoded_name`` WARNING surfaces this separately.
        3. No database qualifier AND no token  → ERROR.  The developer has written
           an unqualified object name (e.g. ``CREATE TABLE Customer (...)``).
           SHIPS has nothing to tokenise because there is no database name to
           replace.  The developer must add the database qualifier themselves.

    System-scope objects (Maps, Roles, Profiles, Authorisations, Foreign Servers)
    are excluded — they have no database qualifier by design and are identical
    across all environments.

    Args:
        rel_path: Relative path of the file being checked.
        content:  Comment-stripped file content.

    Returns:
        One ValidationIssue (ERROR) only for case 3 — no qualifier and no token.
        Empty list for cases 1 and 2.
    """
    # Only applies to source files. Post-Harvest payload files legitimately
    # contain resolved literal names — do not fire on them.
    _path_parts = set(re.split(r"[/\\]", rel_path))
    if "payload" in _path_parts:
        return []

    # Must be a classifiable DDL/DML object type.
    obj_type = None
    for pattern, otype in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            obj_type = otype
            break

    if obj_type is None:
        return []

    # System-scope objects carry no database qualifier — tokens not expected.
    if obj_type in _SYSTEM_SCOPE_TYPES:
        return []

    # Case 1: file already has {{TOKEN}} references — environment-aware.
    if _TOKEN_RE.search(content):
        return []

    # Case 2: file has a database-qualified name (Database.Object or
    # {{TOKEN}}.Object).  The hardcoded literal gives SHIPS something to
    # detect and replace via --auto-tokenise.  Pass here; the separate
    # hardcoded_name WARNING will surface the literal for the developer.
    name_match = _QUALIFIED_NAME_RE.search(content)
    if name_match:
        qualified = name_match.group(1).replace('"', "")
        if "." in qualified:
            return []

    # Case 3: no token AND no database qualifier — SHIPS cannot auto-tokenise
    # because there is no database name to work with.  The developer must add
    # a database qualifier (e.g. {{MY_DB}}.ObjectName) before SHIPS can help.
    return [
        ValidationIssue(
            file=rel_path,
            rule="zero_tokens",
            severity="ERROR",
            message=(
                "No database qualifier found in this file. Every deployable DDL "
                "and DML object must be fully qualified as Database.ObjectName "
                "(or {{TOKEN}}.ObjectName). Without a database qualifier SHIPS "
                "cannot tokenise the file and it cannot be safely deployed to "
                "any environment. Add a database qualifier — use a {{TOKEN}} "
                "placeholder if the target database varies per environment, or "
                "a literal name that SHIPS can auto-tokenise via "
                "'ships harvest --auto-tokenise'."
            ),
        )
    ]


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
                # Default severity for this rule is INFO — the framework
                # will remap to whatever ``rules_config["keyword_case"]``
                # says, so projects that enforce UPPERCASE strictly still
                # get WARNING/ERROR via their inspect.conf.
                severity="INFO",
                message=f"{lowercase}/{total} SQL keywords are lowercase. "
                f"Discipline prefers UPPERCASE keywords.",
            )
        ]
    return []


def _check_leading_commas(
    rel_path: str,
    content: str,
    style: str = DEFAULT_COMMA_STYLE,
) -> List[ValidationIssue]:
    """Check comma placement against the configured style convention.

    Three modes controlled by the ``comma_style`` config key:

    ``leading`` (default)
        Warns when a file uses trailing commas exclusively. The
        violation is reported as ``leading_commas`` so its severity
        can be tuned independently via the ``leading_commas`` rule.

    ``trailing``
        Warns when a file uses leading commas exclusively — the
        mirror of ``leading`` mode for teams with the opposite standard.

    ``as-per-source``
        Does not check comma placement. An INFO finding is emitted
        once so ``ships.decisions.json`` records that comma consistency was
        a deliberate policy choice rather than an oversight.

    Args:
        rel_path: Relative file path (for issue reporting).
        content:  File content (already comment-stripped by caller).
        style:    Comma style to enforce. One of the values in
                  ``_VALID_COMMA_STYLES``. Defaults to
                  ``DEFAULT_COMMA_STYLE`` ("leading").

    Returns:
        A list of ValidationIssue (zero or one entry).
    """
    if style == "as-per-source":
        # Emit once per-file INFO so the omission is visible in the report
        # and recorded in ships.decisions.json — not silently swallowed.
        return [
            ValidationIssue(
                file=rel_path,
                rule="comma_style",
                severity="INFO",
                message=(
                    "comma_style=as-per-source: comma placement not enforced. "
                    "Files in this project may use either leading or trailing "
                    "commas — consistency is not mandated."
                ),
            )
        ]

    lines = content.split("\n")
    trailing = 0
    leading = 0

    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith(","):
            trailing += 1
        if stripped.lstrip().startswith(","):
            leading += 1

    if style == "leading":
        # Warn when the file clearly uses trailing commas and no leading.
        if trailing > 3 and leading == 0:
            return [
                ValidationIssue(
                    file=rel_path,
                    rule="comma_style",
                    severity="WARNING",
                    message=(
                        f"{trailing} trailing commas found. "
                        f"Site convention (comma_style=leading) requires "
                        f"leading commas. Set comma_style=trailing or "
                        f"comma_style=as-per-source in inspect.conf to change."
                    ),
                )
            ]
    elif style == "trailing":
        # Warn when the file clearly uses leading commas and no trailing.
        if leading > 3 and trailing == 0:
            return [
                ValidationIssue(
                    file=rel_path,
                    rule="comma_style",
                    severity="WARNING",
                    message=(
                        f"{leading} leading commas found. "
                        f"Site convention (comma_style=trailing) requires "
                        f"trailing commas. Set comma_style=leading or "
                        f"comma_style=as-per-source in inspect.conf to change."
                    ),
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
# View column list rule (Issue #133)
# ---------------------------------------------------------------


def _check_view_column_list(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check that CREATE/REPLACE VIEW declares an explicit column list.

    An explicit column list between the view name and the AS keyword
    makes the view's schema contract self-describing at the source
    level.  Without it, agents and tooling must connect to the live
    database and execute ``HELP VIEW`` or query ``DBC.ColumnsV`` to
    discover column names.  That makes the solution less agent-friendly
    and breaks any workflow that reasons about view shape from source
    alone.

    Compliant form:
        CREATE VIEW {{V_DB}}.MyView (ColA, ColB, ColC) AS
        SELECT a.ColA, a.ColB, a.ColC
        FROM   {{T_DB}}.MyTable AS a;

    Non-compliant (fires this rule):
        CREATE VIEW {{V_DB}}.MyView AS
        SELECT a.ColA, a.ColB, a.ColC
        FROM   {{T_DB}}.MyTable AS a;

    Only ``.viw`` files and content whose first DDL verb is CREATE or
    REPLACE VIEW are in scope — other object types are silently skipped.
    Comments are already stripped by the caller before this function
    is invoked.

    Args:
        rel_path: Relative path of the file being checked.
        content:  Comment-stripped SQL content.

    Returns:
        A single ValidationIssue (WARNING by default) when the view
        header omits the column list; an empty list when the header
        is compliant or the file is not a view.
    """
    # Only applies to view DDL.  Check the first matching pattern so we
    # don't waste cycles on tables, procedures, etc.
    is_view = False
    for pattern, obj_type in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            if obj_type == "VIEW":
                is_view = True
            break  # first match wins; any non-VIEW type exits early

    if not is_view:
        return []

    match = _VIEW_NO_COLUMN_LIST_RE.search(content)
    if match is None:
        # Column list present (or unqualified name — db_qualifier catches that).
        return []

    line_num = content[: match.start()].count("\n") + 1
    return [
        ValidationIssue(
            file=rel_path,
            rule="view_column_list",
            severity="WARNING",
            line=line_num,
            message=(
                "VIEW is defined without an explicit column list before AS. "
                "Add a column list — e.g. CREATE VIEW db.MyView (Col1, Col2) AS — "
                "so the view's schema contract is self-describing from source. "
                "Without it, agents and tooling must query the live database "
                "(HELP VIEW / DBC.ColumnsV) to discover column names, which "
                "makes the solution less agent-friendly and breaks source-only "
                "analysis workflows."
            ),
        )
    ]


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
def _check_non_ascii_literals(rel_path: str, content: str) -> List[ValidationIssue]:
    """Detect non-ASCII characters in SQL source files (Rule NAS-001).

    Teradata databases created with a LATIN character set (the server default)
    reject any string literal that contains a character outside the Latin-1
    code page with Error 6706 "The string contains an untranslatable character".
    This includes common Unicode punctuation that word processors and rich-text
    editors silently introduce:

      - Em-dash (U+2014, "\u2014")  ->  use  " - "  (not " -- " which creates a SQL comment)
      - Bullet   (U+2022, "\u2022")  ->  use  "-"
      - Right arrow (U+2192, "\u2192")  ->  use  "->"
      - Box-drawing horizontal (U+2500, "\u2500")  ->  use  "-"

    The check runs on the *original* content (before comment stripping) so
    that characters in both comments and string literals are caught.

    Non-ASCII characters inside SQL comments are safe to deploy (the Teradata
    server never parses them), but they create a maintenance hazard: if a
    comment line is later moved into a string literal it will silently break
    the DML.  The check therefore flags them at WARNING severity in comments
    and ERROR severity inside string literals.

    Args:
        rel_path: Relative file path (for error messages).
        content:  Raw file content, read as UTF-8 before comment stripping.

    Returns:
        List of ValidationIssue, one per non-ASCII character occurrence.
    """
    issues: List[ValidationIssue] = []

    # Fast-path: ASCII-only files need no further processing.
    try:
        content.encode("ascii")
        return issues
    except UnicodeEncodeError:
        pass

    # Suggested ASCII replacements for the characters most commonly
    # introduced by rich-text editors and word processors.
    _SUGGESTIONS: dict = {
        "\\u2014": '" - " (em-dash -> hyphen; do NOT use -- which creates a SQL comment)',
        "\\u2022": '"-" (bullet -> hyphen)',
        "\\u2192": '"->" (right arrow -> ASCII arrow)',
        "\\u2500": '"-" (box-drawing -> hyphen)',
    }

    # Strip comments only (NOT string literals) and preserve positions.
    # Characters whose original position is whitespace after stripping
    # were inside a ``--`` or ``/* ... */`` block — those are WARNING
    # because the Teradata server never parses comment content. Any
    # non-ASCII character still present in the stripped form is in
    # real SQL (code or a string literal) and is ERROR — that is the
    # path that risks Error 6706 on LATIN databases.
    from td_release_packager.sql_text import strip_comments_preserving_positions

    no_comments = strip_comments_preserving_positions(content)

    # Index splitlines() lines by their start offset so per-line column
    # positions map back to the absolute offset used to look up the
    # stripped char. ``splitlines()`` drops the line terminator length,
    # so we step manually using the original content.
    offset = 0
    for line_no, line in enumerate(content.splitlines(keepends=True), start=1):
        line_offset = offset
        offset += len(line)
        try:
            line.encode("ascii")
            continue  # line is clean
        except UnicodeEncodeError:
            pass

        # Iterate by position so we can classify each occurrence
        # individually. A line with both a comment em-dash and a
        # literal em-dash must produce one WARNING and one ERROR.
        reported_keys: set = set()
        for col, char in enumerate(line):
            if ord(char) <= 127:
                continue
            abs_offset = line_offset + col
            in_comment = (
                abs_offset < len(no_comments)
                and no_comments[abs_offset].isspace()
                and not char.isspace()
            )
            severity = "WARNING" if in_comment else "ERROR"
            # De-duplicate per (line, char, severity) so the same char
            # repeated on one line in the same context only reports once,
            # matching the original behaviour of the rule.
            dedup_key = (line_no, char, severity)
            if dedup_key in reported_keys:
                continue
            reported_keys.add(dedup_key)

            char_key = "\\u{:04x}".format(ord(char))
            suggestion = _SUGGESTIONS.get(
                char_key,
                "replace with an ASCII equivalent",
            )
            location = (
                "SQL comment (Teradata server does not parse comment content; "
                "still a maintenance hazard — replace before the comment text "
                "is moved into a string literal)"
                if in_comment
                else "SQL source -- Teradata Error 6706 on LATIN databases"
            )
            # When this character has a registered ASCII substitute, point
            # the operator at the auto-fix flag — low-friction is the goal.
            autofix_hint = (
                " Run `ships inspect --fix-non-ascii` to substitute it automatically."
                if char in _NON_ASCII_AUTO_FIX_REPLACEMENTS
                else ""
            )
            issues.append(
                ValidationIssue(
                    rule="non_ascii",
                    severity=severity,
                    file=rel_path,
                    line=line_no,
                    message=(
                        f"Non-ASCII character U+{ord(char):04X} {repr(char)} "
                        f"in {location}. Suggestion: {suggestion}.{autofix_hint}"
                    ),
                )
            )

    return issues


_COMMENT_TEXT_RE = re.compile(
    r"\bCOMMENT\s+ON\b.*?\bIS\s*'((?:''|[^'])*)'",
    re.IGNORECASE | re.DOTALL,
)


def _check_comment_length(
    rel_path: str,
    content: str,
    rules_config: Dict[str, str] = None,
) -> List[ValidationIssue]:
    """Detect COMMENT strings longer than Teradata's 254 character limit.

    Teradata raises Error 5550 when a COMMENT ON ... IS '...' body exceeds
    254 characters.  This rule runs only for ``.cmt`` companion files because
    those are the package convention for deployable COMMENT statements.
    """
    if os.path.splitext(rel_path)[1].lower() != ".cmt":
        return []

    if rules_config is None:
        rules_config = {}
    severity = rules_config.get(
        "comment_length",
        DEFAULT_RULES.get("comment_length", "ERROR"),
    )
    if severity == "OFF":
        return []

    issues: List[ValidationIssue] = []
    for match in _COMMENT_TEXT_RE.finditer(content):
        body = match.group(1)
        body_len = len(body)
        if body_len <= 254:
            continue

        line_no = content.count("\n", 0, match.start()) + 1
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="comment_length",
                severity=severity,
                line=line_no,
                message=(
                    f"COMMENT text is {body_len} characters, exceeding "
                    "Teradata's 254 character limit. Deploy will fail with "
                    "Error 5550. Shorten the COMMENT ON ... IS string."
                ),
            )
        )

    return issues


# Security rules dispatcher (GAP-003, GAP-008)
# ---------------------------------------------------------------


def _check_security(
    rel_path: str,
    content: str,
    file_path: str,
    rules_config: Dict[str, str] = None,
) -> List[ValidationIssue]:
    """Dispatch security rule scans for a single file.

    Calls scan_secret_patterns (GAP-003), scan_dynamic_sql (GAP-008), and
    scan_sensitivity_class (GAP-009).  Uses the *raw* file content (not
    comment-stripped) so that patterns inside SQL string literals are
    still detected.

    Args:
        rel_path:     Path relative to the source directory.
        content:      Raw (unstripped) file content.
        file_path:    Absolute file path.
        rules_config: Current rules dict (consulted for sensitivity_class).

    Returns:
        Combined list of ValidationIssue from all security scans.
    """
    from td_release_packager.security_rules import (
        scan_dynamic_sql,
        scan_secret_patterns,
        scan_sensitivity_class,
        scan_vault_refs,
    )

    issues: List[ValidationIssue] = []
    issues.extend(scan_secret_patterns(rel_path, content, file_path))
    issues.extend(scan_dynamic_sql(rel_path, content, file_path))
    issues.extend(scan_vault_refs(rel_path, content, file_path))

    if rules_config is None:
        rules_config = {}
    sens_sev = rules_config.get(
        "sensitivity_class", DEFAULT_RULES.get("sensitivity_class", "OFF")
    )
    if sens_sev != "OFF":
        violation_level = "error" if sens_sev == "ERROR" else "warning"
        issues.extend(
            scan_sensitivity_class(
                rel_path=rel_path,
                file_path=file_path,
                require_sensitivity_class=True,
                violation_level=violation_level,
            )
        )
    return issues


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
