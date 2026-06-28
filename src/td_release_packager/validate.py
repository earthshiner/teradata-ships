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
from typing import Any, Dict, List, Optional

from td_release_packager.atomic_filename import (
    FilenameDerivationError,
    derive_filename_from_text,
)
from td_release_packager.classifier import (
    TYPE_TO_EXTENSION as _CANONICAL_EXT,
    _CLASSIFY_PATTERNS as _ALL_CLASSIFY_PATTERNS,
)
from td_release_packager.sql_text import (
    strip_comments_and_string_literals as _strip_sql_comments,
)
from td_release_packager.token_engine import find_malformed_tokens


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
    # a correctness defect — Teradata case-folds them and runs fine. Most
    # sites don't enforce UPPERCASE strictly, and surfacing the finding by
    # default reads as friction during onboarding. Default OFF so the rule
    # is opt-in. Projects that enforce the discipline can set
    # ``keyword_case=WARNING`` (or ERROR/INFO) in ``config/inspect.conf``.
    "keyword_case": "OFF",
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
    # warn_external_grants applies to .dcl files for grantees that no DDL in
    # the package implies (e.g. roles granted access inside this package
    # whose GRANT ROLE ... TO USER is owned by a DBA, IGA system, or
    # autonomous agent outside the package). These are commonly legitimate
    # and require no operator action, so they default to INFO — surfaced
    # in the report and audit trail without blocking the build.
    #
    # Renamed from warn_orphan_grants in 2026-06: the term "external"
    # better reflects that the grantee lives outside the package's intent
    # rather than being a deficiency. Old configs using warn_orphan_grants
    # must be updated.
    "warn_external_grants": "INFO",
    # -- Tokenised filename eponymy rules (issue #365, PR-5) --
    # filename_token_format: every {{...}} marker in a filename must
    # be a syntactically valid token. Orphan ``{{`` or ``}}`` pairs in
    # the name are package-blocking: the build cannot substitute a
    # malformed token and the file will land on an unintended path.
    "filename_token_format": "ERROR",
    # object_level_grant: warns when a GRANT or REVOKE targets a
    # specific object (``db.obj``) or column (``GRANT SELECT (col)``)
    # rather than the containing database. Teradata best practice is
    # to grant at the database level so privileges propagate
    # consistently and the access surface is small. Object- and
    # column-level grants are valid SQL but produce sprawling, hard-
    # to-audit privilege graphs. WARNING by default — projects with
    # explicit object-level requirements can set this to OFF.
    "object_level_grant": "WARNING",
    # destructive_change (issue #169): explicit DROP / DELETE DATABASE /
    # ALTER ... DROP statements in payload files remove structures or data
    # and must not deploy without human review. The SHIPS deployer owns
    # idempotent CREATE (drop+create is its internal concern), so payload
    # files should never carry destructive DDL. ERROR by default — this is
    # a deploy-blocking safety control, not a style preference.
    "destructive_change": "ERROR",
    # data_dependent_change (issue #170): ALTER TABLE / CREATE UNIQUE INDEX
    # operations whose success depends on the data already in the table
    # (adding NOT NULL without a default, UNIQUE on possibly-duplicate
    # data, CHECK that existing rows may violate, PRIMARY INDEX /
    # partitioning changes that move data). Structurally valid but can
    # fail or rewrite the table at deploy time. WARNING by default — set
    # to ERROR in config/inspect.conf where prechecks are mandatory.
    "data_dependent_change": "WARNING",
    # non_linear_package_history (issue #168): a project-level check over
    # the built packages under <project>/releases/. Detects a package
    # sequence that cannot be trusted — a build number reused with
    # different contents, an older build appearing after a newer one, an
    # orphaned prereqs/main half, a package that requires a missing
    # sibling, or an integrity sidecar that no longer matches its archive.
    # WARNING by default (early development); set to ERROR for
    # release/promotion workflows in config/inspect.conf.
    "non_linear_package_history": "WARNING",
    # transaction_control_in_payload (issue #173): BT/ET, BEGIN/END
    # TRANSACTION, COMMIT, and ROLLBACK belong to the SHIPS deployer, which
    # owns the transaction boundary — they should not be hidden inside
    # payload files. WARNING by default; --strict promotes to ERROR for
    # platform workflows. Transaction control inside a procedure/function
    # BEGIN…END body (e.g. an exception-handler ROLLBACK) is exempt.
    "transaction_control_in_payload": "WARNING",
    # token_naming (issue #172): a DDL object's database token should carry
    # the kind suffix matching its object type — tables (and their indexes /
    # triggers) in a ``{{*_T}}`` token, views in a ``{{*_V}}`` token. Only
    # the unambiguous T/V case is enforced (macro/procedure/function kind
    # suffixes are site-configurable; express those via a custom lint
    # policy). WARNING by default; flags only a clear mismatch (e.g. a view
    # placed in a ``_T`` token), never a token without a kind suffix.
    "token_naming": "WARNING",
    # contract_change (issue #171): compares the current source against a
    # captured contract baseline (.ships/contracts.baseline.json) and flags
    # backward-incompatible changes — removed/renamed/reordered view columns,
    # changed procedure parameters, dropped/retyped table columns, or an
    # object that disappeared. No-op until a baseline is captured with
    # ``inspect --update-contract-baseline``. WARNING by default; set to ERROR
    # when comparing against a governed baseline / previous release.
    "contract_change": "WARNING",
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

    # Wrong-file guard (issue #386). An env/token config
    # (config/env/<ENV>.conf) accidentally passed via --config would
    # otherwise have every TOKEN=value line treated as an unknown rule
    # with an invalid severity and silently skipped, leaving the
    # operator with a baffling "no rules applied" result. We count
    # assignments to tell an env config apart from a genuine
    # inspect.conf that merely has a typo, and raise a pointed error
    # only when nothing in the file resembles inspect rules at all.
    assignment_count = 0
    valid_count = 0
    known_rule_count = 0

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

            assignment_count += 1
            if name in DEFAULT_RULES or name in _DOMAIN_VALUE_RULES:
                known_rule_count += 1

            # -- Retired key shim --
            # PR6 of the deterministic-deploy programme renamed
            # ``warn_orphan_grants`` to ``warn_external_grants`` (default
            # INFO). Per the user-confirmed handover plan, the old key
            # is NOT silently accepted: an inspect.conf carrying it
            # would otherwise inherit the new INFO default for the
            # external-grant rule without the operator noticing, which
            # is the silent-failure mode we are trying to prevent.
            # Surface a clear, actionable error here so a stale config
            # is fixed at inspect time rather than discovered later
            # via Trust Report drift.
            if name == "warn_orphan_grants":
                raise ValueError(
                    f"inspect.conf line {lineno}: 'warn_orphan_grants' "
                    "has been renamed to 'warn_external_grants' "
                    "(2026-06; default INFO). Rename the key and re-run. "
                    "The old key is no longer accepted — see "
                    "docs/references/inspect_rules.md for the new "
                    "semantics."
                )

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
                valid_count += 1
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
            valid_count += 1

    # Wrong-file guard (issue #386): the file has assignments but not one
    # of them names a known inspect rule or carries a valid severity.
    # That is the signature of an env/token config passed by mistake, not
    # a real inspect.conf — fail fast with a pointer to the right file.
    if assignment_count >= 2 and valid_count == 0 and known_rule_count == 0:
        raise ValueError(
            f"'{config_path}' does not look like an inspect rules file: "
            f"none of its {assignment_count} KEY=VALUE line(s) name a known "
            f"inspect rule or carry a valid severity "
            f"(ERROR/WARNING/INFO/OFF).\n"
            f"--config expects an inspect.conf where each line is "
            f"'rule_name=SEVERITY' (see config/inspect.conf).\n"
            f"This file looks like an env/token config "
            f"(config/env/<ENV>.conf). inspect discovers env configs "
            f"automatically under config/env/ — do not pass them with "
            f"--config."
        )

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
        "# token_naming: a DDL object's database token should carry the kind",
        "# suffix matching its type — tables/indexes/triggers in {{*_T}},",
        "# views in {{*_V}}. Flags only a clear mismatch (e.g. a view in a",
        "# _T token); tokens without a kind suffix are left alone. WARNING.",
        f"token_naming={DEFAULT_RULES['token_naming']}",
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
        "# destructive_change: explicit DROP / DELETE DATABASE /",
        "# ALTER ... DROP statements in payload files remove structures",
        "# or data and must not deploy without human review. The SHIPS",
        "# deployer owns idempotent CREATE, so payloads should not carry",
        "# destructive DDL. ERROR by default (deploy-blocking safety).",
        f"destructive_change={DEFAULT_RULES['destructive_change']}",
        "# data_dependent_change: ALTER TABLE / CREATE UNIQUE INDEX whose",
        "# success depends on existing data (NOT NULL without DEFAULT,",
        "# UNIQUE on possibly-duplicate data, CHECK existing rows may",
        "# violate, PRIMARY INDEX / partitioning changes that move data).",
        "# WARNING by default; set to ERROR where prechecks are mandatory.",
        f"data_dependent_change={DEFAULT_RULES['data_dependent_change']}",
        "# non_linear_package_history: project-level check over releases/.",
        "# Flags reused build numbers with different contents, out-of-order",
        "# builds, orphaned prereqs/main halves, missing required siblings,",
        "# and integrity sidecar mismatches. WARNING by default; set to",
        "# ERROR for release/promotion workflows.",
        f"non_linear_package_history={DEFAULT_RULES['non_linear_package_history']}",
        "# transaction_control_in_payload: BT/ET, BEGIN/END TRANSACTION,",
        "# COMMIT, ROLLBACK belong to the deployer, not payload files.",
        "# WARNING by default; --strict promotes to ERROR. Transaction",
        "# control inside a procedure BEGIN…END body is exempt.",
        f"transaction_control_in_payload={DEFAULT_RULES['transaction_control_in_payload']}",
        "# contract_change: compares current source against a captured",
        "# contract baseline (.ships/contracts.baseline.json) and flags",
        "# backward-incompatible changes (removed view columns, changed proc",
        "# params, dropped table columns, ...). No-op until a baseline is",
        "# captured with `inspect --update-contract-baseline`. WARNING; set",
        "# ERROR when comparing against a governed baseline.",
        f"contract_change={DEFAULT_RULES['contract_change']}",
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
        "# warn_external_grants",
        "#   Controls .dcl files for a grantee that no DDL in the package implies —",
        "#   i.e. the grantee is external to the package's intent. The file exists",
        "#   but SHIPS found no DDL reference that would require access to be granted",
        "#   to that grantee.",
        "#",
        "#   Common legitimate causes:",
        "#     - A role is granted database access inside this package, but",
        "#       GRANT ROLE … TO USER is managed outside it (by a DBA, IGA",
        "#       system, or autonomous agent).",
        "#     - The package pre-provisions access rights that a downstream",
        "#       process or separate package will activate.",
        "#",
        "#   INFO    (default) — external grants are surfaced in the report and",
        "#                       audit trail. They do not block the build because",
        "#                       they are commonly legitimate.",
        "#   WARNING           — external grants are reported as warnings.",
        "#   ERROR             — external grants block packaging. Use this posture",
        "#                       for fully self-contained packages where every",
        "#                       grant must be traceable to DDL in this package.",
        "#   OFF               — external grants are silently accepted.",
        "#",
        "# Note: external .dcl files are never auto-deleted by --fix-grants.",
        "# They require manual review and removal.",
        f"warn_external_grants={DEFAULT_RULES['warn_external_grants']}",
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
        "# Tokenised filename eponymy rules (issue #365, PR-5)",
        "# filename_token_format: every {{...}} marker in a filename must be a",
        "# syntactically valid token. Orphan {{ or }} pairs in the name are",
        "# package-blocking: the build cannot substitute a malformed token and",
        "# the file lands on an unintended path. Defaults to ERROR.",
        f"filename_token_format={DEFAULT_RULES['filename_token_format']}",
        "# object_level_grant: warns when a GRANT/REVOKE targets a specific",
        "# object (ON db.obj) or column (GRANT SELECT (col) ON ...) rather",
        "# than the containing database. Teradata best practice is to grant at",
        "# the database level — privileges propagate to all objects in the",
        "# container and the access surface stays auditable. WARNING by default;",
        "# set to OFF for projects with explicit object-level requirements.",
        f"object_level_grant={DEFAULT_RULES['object_level_grant']}",
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

# -- DML-like types: qualified through a statement target (INSERT INTO
# Db.Object, etc.) rather than a CREATE/REPLACE clause. zero_tokens
# recognises their qualifier via _DML_QUALIFIED_TARGET_RE, not the
# DDL-shaped _QUALIFIED_NAME_RE (issue #410).
_DML_LIKE_TYPES = {"DML", "ORDERED_SQL"}

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

# Permissive token-aware identifier — accepts prefix-token shapes
# (``{{DB_PREFIX}}_T``), multi-token shapes (``{{ENV}}_{{SUFFIX}}``),
# and bare identifiers. Used by the tokenised-payload eponymy check
# (PR-5, issue #365) where the body's qualified name carries a token
# concatenated with literal text.
_TOKEN_AWARE_NAME_PART = (
    r"(?:"
    r"(?:\{\{[A-Z][A-Z0-9_]*\}\}|\"[^\"]+\"|[A-Za-z_]\w*)"
    r"(?:\{\{[A-Z][A-Z0-9_]*\}\}|\w+)*"
    r")"
)
_TOKEN_AWARE_QUALIFIED_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    rf"(?P<dbpart>{_TOKEN_AWARE_NAME_PART})"
    r"\s*\.\s*"
    rf"(?P<objpart>{_TOKEN_AWARE_NAME_PART})",
    re.IGNORECASE | re.MULTILINE,
)

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
    # Agent-facing remediation metadata, populated for custom-policy
    # findings (issue #167). None for built-in rules. Carried through to
    # machine-readable inspect output (ships.decisions.json).
    remediation: Optional[Dict[str, Any]] = None


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
    custom_rules: Optional[List[Any]] = None,
) -> ValidationResult:
    """
    Validate all DDL files in a directory against the Coding Discipline.

    Thin traced wrapper — see ``_validate_directory_impl`` for the full
    implementation.  Emits a ``ships.validate`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.

    ``custom_rules`` is an optional list of ``lint_policy.CustomLintRule``
    applied alongside the built-in checks (issue #167).
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
            custom_rules=custom_rules,
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
    custom_rules: Optional[List[Any]] = None,
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
        file_issues.extend(_check_destructive_change(rel_path, clean))
        file_issues.extend(_check_data_dependent_change(rel_path, clean))
        file_issues.extend(_check_transaction_control(rel_path, clean))
        file_issues.extend(_check_ddl_terminator(rel_path, clean))
        file_issues.extend(_check_view_macro_self_reference(rel_path, clean))
        file_issues.extend(_check_one_object(rel_path, clean))
        file_issues.extend(_check_eponymous(rel_path, clean, file_path))
        file_issues.extend(_check_extension(rel_path, clean, file_path))
        file_issues.extend(_check_type_suffixes(rel_path, clean))
        file_issues.extend(_check_token_naming(rel_path, clean))
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
        # PR-5 (issue #365): tokenised-filename eponymy + DCL hygiene.
        file_issues.extend(_check_filename_token_format(rel_path, file_path))
        file_issues.extend(_check_object_level_grant(rel_path, clean))

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

        # -- Custom lint policy (issue #167) --
        # Custom findings carry their own severity from the policy file, so
        # they bypass the inspect.conf remap above. --strict still promotes
        # WARNING → ERROR for parity with built-in rules; INFO/OFF are left
        # as-is (OFF rules never fire — handled in _check_custom_policy).
        if custom_rules:
            for issue in _check_custom_policy(rel_path, clean, custom_rules):
                if strict and issue.severity == "WARNING":
                    issue.severity = "ERROR"
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


def _phase_for_path(rel_path: str) -> Optional[str]:
    """Map a payload-relative path to its pipeline phase token.

    The deployable tree is ``payload/database/<PHASE>/...`` and inspect
    runs from ``payload/database``, so the first path segment is the
    phase directory. Returns a canonical phase token (DDL / DCL / DML /
    PREREQS / POST_INSTALL) or None when the path has no recognisable
    phase segment (e.g. inspect run against an arbitrary directory).
    """
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    head = parts[0].strip().lower()
    mapping = {
        "ddl": "DDL",
        "dcl": "DCL",
        "dml": "DML",
        "pre-requisites": "PREREQS",
        "pre_requisites": "PREREQS",
        "prerequisites": "PREREQS",
        "post-install": "POST_INSTALL",
        "post_install": "POST_INSTALL",
    }
    return mapping.get(head)


def _object_type_for_content(content: str) -> Optional[str]:
    """Best-effort object type for a payload file, as a base type.

    Reuses the classifier patterns already used by the extension check.
    GRANT / REVOKE resolve to the ``DCL`` convenience alias so a policy
    can target ``object_types: [DCL]`` without naming both verbs.
    """
    for pattern, otype in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            from td_release_packager.classifier import base_type

            base = base_type(otype) or otype
            if base in ("GRANT", "REVOKE"):
                return "DCL"
            return base
    return None


def _check_custom_policy(
    rel_path: str,
    content: str,
    custom_rules: List["Any"],
) -> List[ValidationIssue]:
    """Apply the custom lint policy (issue #167) to one file.

    For each rule scoped to this file's object type and phase, evaluates
    the deny / required / exclude patterns over the comment-stripped
    content and emits a ``ValidationIssue`` carrying the rule's severity
    and remediation metadata. ``OFF`` rules are loaded but never fire.

    Patterns are matched with ``re`` (compiled at load time) — SQL is
    treated as data, never executed.
    """
    if not custom_rules:
        return []

    obj_type = _object_type_for_content(content)
    phase = _phase_for_path(rel_path)
    issues: List[ValidationIssue] = []

    for rule in custom_rules:
        if rule.severity == "OFF":
            continue
        # Scope: empty set means "applies to all"; otherwise the file's
        # type/phase must be in the set. A rule scoped to a type we could
        # not classify simply does not apply.
        if rule.object_types and (
            obj_type is None or obj_type not in rule.object_types
        ):
            continue
        if rule.phases and (phase is None or phase not in rule.phases):
            continue
        if rule.exclude_pattern and rule.exclude_pattern.search(content):
            continue

        triggered = False
        if rule.deny_pattern and rule.deny_pattern.search(content):
            triggered = True
        if rule.required_pattern and not rule.required_pattern.search(content):
            triggered = True

        if triggered:
            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule=rule.name,
                    severity=rule.severity,
                    message=rule.description,
                    # Always a dict (possibly empty) so downstream can tell a
                    # custom-policy finding from a built-in one, which leaves
                    # remediation as None. Empty dicts are dropped from JSON.
                    remediation=dict(rule.remediation),
                )
            )
    return issues


# Destructive-change detection (issue #169). Patterns are line-anchored
# (MULTILINE) and run against comment-stripped content. The object name,
# when present, is captured in group "obj" for the finding message.
# ``^[ \t]*`` (not ``^\s*``) so the anchor stays on the statement's own
# line — ``\s`` would cross newlines and report the preceding blank line.
_DESTRUCTIVE_DROP_RE = re.compile(
    r"^[ \t]*DROP\s+"
    r"(?P<kind>TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|DATABASE|USER|"
    r"ROLE|PROFILE|JOIN\s+INDEX|HASH\s+INDEX|INDEX)\b"
    r"(?:\s+(?P<obj>[A-Za-z0-9_.\"{}]+))?",
    re.IGNORECASE | re.MULTILINE,
)
_DESTRUCTIVE_DELETE_DB_RE = re.compile(
    r"^[ \t]*DELETE\s+DATABASE\s+(?P<obj>[A-Za-z0-9_.\"{}]+)?",
    re.IGNORECASE | re.MULTILINE,
)
_DESTRUCTIVE_ALTER_DROP_RE = re.compile(
    r"^[ \t]*ALTER\s+TABLE\s+(?P<obj>[A-Za-z0-9_.\"{}]+)\s+DROP\b",
    re.IGNORECASE | re.MULTILINE,
)

#: Agent guidance attached to every destructive_change finding so an
#: autonomous actor knows it must stop, not auto-fix or deploy (#169).
_DESTRUCTIVE_REMEDIATION = {
    "requires_human_review": True,
    "agent_may_fix": False,
    "agent_may_suggest": False,
    "automation_level": "manual_review_required",
    "recommended_action": (
        "Remove the destructive statement from the payload, or obtain "
        "explicit human approval before deploying. The SHIPS deployer "
        "owns idempotent CREATE — payload files should not drop objects "
        "or data."
    ),
}


def _check_destructive_change(rel_path: str, content: str) -> List[ValidationIssue]:
    """Flag explicit destructive DDL in a payload file (issue #169).

    Detects ``DROP <object>``, ``DELETE DATABASE``, and
    ``ALTER TABLE ... DROP`` — statements that remove structures or data
    and must not deploy without explicit human review.

    To avoid false positives on procedure/function bodies that legitimately
    drop volatile/temp tables, only the region **before** the first
    ``BEGIN`` is scanned: an eponymous payload file's primary statement is
    a ``CREATE`` header, so a destructive statement here is the file's own
    top-level operation rather than internal procedural logic. The deployer's
    own drop+create is internal and never appears in the payload.
    """
    # Limit the scan to the top-level statement region (before any compound
    # BEGIN…END body).
    begin = re.search(r"\bBEGIN\b", content, re.IGNORECASE)
    region = content[: begin.start()] if begin else content

    issues: List[ValidationIssue] = []

    def _emit(kind: str, obj: Optional[str], pos: int) -> None:
        line = region.count("\n", 0, pos) + 1
        target = f" on {obj}" if obj else ""
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="destructive_change",
                severity="ERROR",
                message=(
                    f"Destructive statement ({kind}{target}) found in payload. "
                    f"Destructive changes (DROP / DELETE DATABASE / ALTER…DROP) "
                    f"remove structures or data and require explicit human "
                    f"review and approval before deployment."
                ),
                line=line,
                remediation=dict(_DESTRUCTIVE_REMEDIATION),
            )
        )

    for m in _DESTRUCTIVE_DROP_RE.finditer(region):
        kind = "DROP " + re.sub(r"\s+", " ", m.group("kind").upper())
        _emit(kind, m.group("obj"), m.start())
    for m in _DESTRUCTIVE_DELETE_DB_RE.finditer(region):
        _emit("DELETE DATABASE", m.group("obj"), m.start())
    for m in _DESTRUCTIVE_ALTER_DROP_RE.finditer(region):
        _emit("ALTER TABLE … DROP", m.group("obj"), m.start())

    return issues


# Data-dependent-change detection (issue #170). Matches each ALTER TABLE
# statement (body bounded by the next ``;`` — ``[^;]`` spans newlines but
# not statement boundaries) plus CREATE UNIQUE INDEX. Sub-conditions are
# tested in Python against the statement body.
_ALTER_TABLE_STMT_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?P<obj>[A-Za-z0-9_.\"{}]+)(?P<body>[^;]*)",
    re.IGNORECASE,
)
_CREATE_UNIQUE_INDEX_RE = re.compile(
    r"\bCREATE\s+UNIQUE\s+INDEX\b[^;]*?\bON\s+(?P<obj>[A-Za-z0-9_.\"{}]+)",
    re.IGNORECASE,
)


def _check_data_dependent_change(rel_path: str, content: str) -> List[ValidationIssue]:
    """Flag DDL whose success depends on existing table data (issue #170).

    Targets operations on *existing* tables — ``ALTER TABLE`` and
    ``CREATE UNIQUE INDEX`` — that are structurally valid but can fail or
    rewrite the table depending on the data already present:

      * adding a ``NOT NULL`` column/constraint without a ``DEFAULT``,
      * adding a ``UNIQUE`` constraint or unique index,
      * adding a ``CHECK`` constraint,
      * changing ``PRIMARY INDEX`` / partitioning (data movement).

    A ``CREATE TABLE`` is a new, empty object and is never flagged. Each
    finding indicates that live metadata is required to assess the risk and
    carries a recommended precheck. As with destructive_change, only the
    top-level region (before any ``BEGIN``) is scanned so procedural
    ``ALTER`` inside a body is not misread.
    """
    begin = re.search(r"\bBEGIN\b", content, re.IGNORECASE)
    region = content[: begin.start()] if begin else content

    issues: List[ValidationIssue] = []

    def _emit(kind: str, obj: str, pos: int, precheck: str) -> None:
        line = region.count("\n", 0, pos) + 1
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="data_dependent_change",
                severity="WARNING",
                message=(
                    f"Data-dependent change ({kind}) on existing table {obj}. "
                    f"This is structurally valid but its success depends on the "
                    f"data already in the table — assess against live metadata "
                    f"before deploying."
                ),
                line=line,
                remediation={
                    "safe_fix_available": False,
                    "automation_level": "manual_review_required",
                    "requires_human_review": True,
                    "requires_live_metadata": True,
                    "recommended_precheck": precheck,
                    "recommended_action": (
                        "Run the precheck against the target environment; "
                        "remediate the data (backfill / dedupe / supply a "
                        "DEFAULT) or obtain approval before deploying."
                    ),
                },
            )
        )

    for m in _ALTER_TABLE_STMT_RE.finditer(region):
        obj = m.group("obj")
        body = m.group("body") or ""
        body_u = body.upper()
        pos = m.start()
        if re.search(r"\bNOT\s+NULL\b", body_u) and not re.search(
            r"\bDEFAULT\b", body_u
        ):
            _emit(
                "adds NOT NULL without DEFAULT",
                obj,
                pos,
                f"SELECT COUNT(*) FROM {obj}; — adding NOT NULL fails if rows "
                f"exist with no value. Supply a DEFAULT or backfill first.",
            )
        if re.search(r"\bUNIQUE\b", body_u):
            _emit(
                "adds a UNIQUE constraint",
                obj,
                pos,
                f"Check for duplicates before adding UNIQUE: SELECT <cols>, "
                f"COUNT(*) FROM {obj} GROUP BY <cols> HAVING COUNT(*) > 1;",
            )
        if re.search(r"\bCHECK\s*\(", body_u):
            _emit(
                "adds a CHECK constraint",
                obj,
                pos,
                f"Run the CHECK predicate as a SELECT against {obj} to find "
                f"rows that would violate it before adding the constraint.",
            )
        if re.search(
            r"\b(?:PRIMARY\s+INDEX|PARTITION\s+BY|MODIFY\s+PRIMARY)\b", body_u
        ):
            _emit(
                "changes PRIMARY INDEX / partitioning",
                obj,
                pos,
                f"Assess the size and lock impact of {obj}; changing the "
                f"primary index / partitioning redistributes existing rows "
                f"and may need a scratch-table rebuild.",
            )

    for m in _CREATE_UNIQUE_INDEX_RE.finditer(region):
        _emit(
            "creates a UNIQUE INDEX",
            m.group("obj"),
            m.start(),
            f"Check for duplicates before creating a unique index: "
            f"SELECT <cols>, COUNT(*) FROM {m.group('obj')} GROUP BY <cols> "
            f"HAVING COUNT(*) > 1;",
        )

    return issues


# Transaction-control detection (issue #173). Line-anchored (``^[ \t]*``) so a
# control statement is matched on its own line, run against comment-stripped
# content (commented BT/ET/COMMIT never fire).
_TXN_CONTROL_PATTERNS = [
    (re.compile(r"^[ \t]*BT\s*;", re.IGNORECASE | re.MULTILINE), "BT"),
    (re.compile(r"^[ \t]*ET\s*;", re.IGNORECASE | re.MULTILINE), "ET"),
    (
        re.compile(r"^[ \t]*BEGIN\s+TRANSACTION\b", re.IGNORECASE | re.MULTILINE),
        "BEGIN TRANSACTION",
    ),
    (
        re.compile(r"^[ \t]*END\s+TRANSACTION\b", re.IGNORECASE | re.MULTILINE),
        "END TRANSACTION",
    ),
    (re.compile(r"^[ \t]*COMMIT\b", re.IGNORECASE | re.MULTILINE), "COMMIT"),
    (re.compile(r"^[ \t]*ROLLBACK\b", re.IGNORECASE | re.MULTILINE), "ROLLBACK"),
]

# A procedure/function compound body opens with ``BEGIN`` that is NOT
# ``BEGIN TRANSACTION``. Transaction control inside such a body (e.g. an
# exception-handler ROLLBACK) is procedural, not a payload-level statement.
_COMPOUND_BEGIN_RE = re.compile(r"\bBEGIN\b(?!\s+TRANSACTION\b)", re.IGNORECASE)


def _check_transaction_control(rel_path: str, content: str) -> List[ValidationIssue]:
    """Flag transaction-control statements in a payload file (issue #173).

    BT/ET, BEGIN/END TRANSACTION, COMMIT, and ROLLBACK belong to the SHIPS
    deployer, which owns the transaction boundary — they should not be hidden
    inside payload files. Detection runs on comment-stripped content (so
    commented DI-tool workaround tokens never fire) and only over the region
    before any procedure/function compound ``BEGIN`` body, so an
    exception-handler ROLLBACK inside a stored procedure is exempt while a
    standalone ``BEGIN TRANSACTION`` is still caught.
    """
    begin = _COMPOUND_BEGIN_RE.search(content)
    region = content[: begin.start()] if begin else content

    phase = _phase_for_path(rel_path)
    phase_note = f" [{phase}]" if phase else ""

    issues: List[ValidationIssue] = []
    for pattern, kind in _TXN_CONTROL_PATTERNS:
        for m in pattern.finditer(region):
            line = region.count("\n", 0, m.start()) + 1
            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule="transaction_control_in_payload",
                    severity="WARNING",
                    message=(
                        f"Transaction-control statement ({kind}) in payload"
                        f"{phase_note}. Transaction boundaries are owned by the "
                        f"SHIPS deployer — remove {kind} from the payload file."
                    ),
                    line=line,
                    remediation={
                        "safe_fix_available": False,
                        "automation_level": "manual_review_required",
                        "requires_human_review": True,
                        "recommended_action": (
                            "Remove the transaction-control statement; the "
                            "deployer manages BT/ET / COMMIT / ROLLBACK around "
                            "each deployment unit."
                        ),
                    },
                )
            )
    return issues


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
    """Check that filename matches the DDL's Database.ObjectName.

    For tokenised payloads (issue #365, PR-5) the comparison runs
    against the canonical ``derive_filename`` rendering of the
    parsed identity — so ``{{DB_PREFIX}}_T.Customer.tbl`` agrees
    with a body that declares ``CREATE TABLE {{DB_PREFIX}}_T.Customer``.
    A drifted filename, or a body whose identity differs from what
    the name encodes, surfaces as a clear ``eponymous`` finding.
    """
    filename = os.path.basename(file_path)
    basename = os.path.splitext(filename)[0]
    ext = os.path.splitext(filename)[1]

    # Tokenised payload: ``_QUALIFIED_NAME_RE`` doesn't accept tokens
    # in identifiers, so the token-aware ``_INTRA_QUALIFIED_NAME_RE``
    # carries the identity match. The comparison runs via the
    # canonical ``derive_filename`` so prefix-token shapes
    # ({{DB_PREFIX}}_T.X) and whole-name tokens ({{TOK}}.X) work
    # identically. Returning early on parse errors avoids piggy-
    # backing this rule on derivation defects the
    # filename_token_format rule reports separately.
    if "{{" in basename:
        token_match = _TOKEN_AWARE_QUALIFIED_NAME_RE.search(content)
        if token_match is None:
            return []
        qualified = f"{token_match.group('dbpart')}.{token_match.group('objpart')}"
        try:
            expected_filename = derive_filename_from_text(qualified, ext)
        except FilenameDerivationError:
            return []
        if filename != expected_filename:
            return [
                ValidationIssue(
                    file=rel_path,
                    rule="eponymous",
                    severity="WARNING",
                    message=(
                        f"Filename {filename!r} does not match the "
                        f"identity declared in the body "
                        f"({qualified!r} → expected {expected_filename!r})."
                    ),
                )
            ]
        return []

    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return []

    qualified = match.group(1).replace('"', "")

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


def _check_filename_token_format(
    rel_path: str, file_path: str
) -> List[ValidationIssue]:
    """Reject malformed ``{{...}}`` markers in atomic-file filenames.

    Step-0 token-format scanning has always run on file *contents*;
    issue #365 extends it to filenames so a typo like
    ``{DB_PREFIX}}_T.X.tbl`` is caught before Package tries to
    substitute it and produces a path no operator expects.

    Returns one issue per malformed marker so a multi-defect filename
    surfaces every defect in a single inspect run.
    """
    filename = os.path.basename(file_path)
    bad = find_malformed_tokens(filename)
    if not bad:
        return []
    issues: List[ValidationIssue] = []
    for marker in bad:
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="filename_token_format",
                severity="ERROR",
                message=(
                    f"Malformed {marker['marker']!r} marker in filename "
                    f"{filename!r} at column {marker['column']}. "
                    "Filenames must contain only well-formed "
                    "``{{TOKEN}}`` substitutions."
                ),
            )
        )
    return issues


# DCL statement detector — uses the canonical grant_merger parser.
# Kept lazy-imported so validate stays loadable in environments
# (e.g. minimal CLI) where the merger module is not on the path.
_GRANT_REVOKE_LINE_RE = re.compile(
    r"^\s*(?:GRANT|REVOKE)\b",
    re.IGNORECASE | re.MULTILINE,
)
# Column-list grants: ``GRANT SELECT (col1, col2) ON db.tbl TO user``.
# The privilege portion carries a parenthesised column list — distinct
# from the standard ``GRANT SELECT ON ...`` shape.
_COLUMN_GRANT_RE = re.compile(
    r"^\s*(?:GRANT|REVOKE)\s+[A-Z_, ]+?\([^)]*\)\s+ON\b",
    re.IGNORECASE | re.MULTILINE,
)


def _check_object_level_grant(rel_path: str, content: str) -> List[ValidationIssue]:
    """Warn on GRANT/REVOKE that targets a specific object or column.

    Teradata best practice (per coding standards on this codebase):
    grant at the database level so privileges propagate through the
    container and the access surface stays small. Object-level
    (``ON db.obj``) and column-level (``GRANT SELECT (col) ON …``)
    grants are valid SQL but produce sprawling privilege graphs
    that are hard to audit and easy to drift on.

    Only inspects ``.dcl`` and ``.grt`` files — the rule does not
    apply to DDL bodies that happen to contain GRANT statements
    (e.g. inside a procedure body, where the discipline differs).
    """
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in {".dcl", ".grt"}:
        return []

    # Lazy import keeps the merger optional at load time.
    from td_release_packager.grant_merger import (
        PrivilegeGrant,
        _split_statements,
        parse_statement,
    )

    issues: List[ValidationIssue] = []

    # Column-list grants are detected on the raw text — the canonical
    # parser does not preserve the parenthesised privilege column
    # list (it treats it as part of the privilege string).
    if _COLUMN_GRANT_RE.search(content):
        issues.append(
            ValidationIssue(
                file=rel_path,
                rule="object_level_grant",
                severity="WARNING",
                message=(
                    "Column-level GRANT/REVOKE detected. Teradata best "
                    "practice is to grant at the database level — column "
                    "grants produce sprawling privilege graphs that drift."
                ),
            )
        )

    # Object-level grants: ON target carries a ``.``.
    for raw in _split_statements(content):
        stmt = parse_statement(raw)
        if not isinstance(stmt, PrivilegeGrant):
            continue
        if "." in stmt.on_object:
            issues.append(
                ValidationIssue(
                    file=rel_path,
                    rule="object_level_grant",
                    severity="WARNING",
                    message=(
                        f"Object-level grant detected: "
                        f"{stmt.action} ON {stmt.on_object} TO {stmt.grantee}. "
                        "Prefer database-level grants — privileges "
                        "propagate to all objects in the container and "
                        "the access surface stays auditable."
                    ),
                )
            )

    return issues


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


# Object types whose database token should carry an unambiguous kind suffix.
# Macro/procedure/function kinds are site-configurable (default _T), so they
# are deliberately excluded — enforce those via a custom lint policy (#167).
_TOKEN_NAMING_EXPECTED_KIND = {
    "TABLE": "T",
    "JOIN_INDEX": "T",
    "HASH_INDEX": "T",
    "INDEX": "T",
    "TRIGGER": "T",
    "VIEW": "V",
}


def _check_token_naming(rel_path: str, content: str) -> List[ValidationIssue]:
    """Check a DDL object's database token carries the right kind suffix (#172).

    Tables (and their indexes/triggers) belong in a ``{{*_T}}`` token; views in
    a ``{{*_V}}`` token. Flags only a *clear* mismatch — a view in a ``_T``
    token, a table in a ``_V`` token. A token with no kind suffix is left
    alone (not every project uses the convention), and macro/procedure/function
    kinds (site-configurable) are not checked here.
    """
    m = _TOKEN_AWARE_QUALIFIED_NAME_RE.search(content)
    if not m:
        return []

    obj_type = _object_type_for_content(content)
    expected = _TOKEN_NAMING_EXPECTED_KIND.get(obj_type or "")
    if expected is None:
        return []

    dbpart = (m.group("dbpart") or "").strip()
    if dbpart.startswith("{{") and dbpart.endswith("}}"):
        token = dbpart[2:-2].strip()
    else:
        token = dbpart.strip('"')

    from td_release_packager.kind_suffix import has_kind_suffix

    if not has_kind_suffix(token):
        return []

    actual = token.rsplit("_", 1)[1].upper()
    # Only the unambiguous T/V confusion is a finding here.
    if actual in {"T", "V"} and actual != expected:
        line = content.count("\n", 0, m.start()) + 1
        return [
            ValidationIssue(
                file=rel_path,
                rule="token_naming",
                severity="WARNING",
                message=(
                    f"{obj_type} should live in a {{{{*_{expected}}}}} database "
                    f"token; found '{dbpart}' (kind suffix '_{actual}'). "
                    f"Place {obj_type.lower()}s in a '_{expected}' token so "
                    f"object placement stays consistent and agent-readable."
                ),
                line=line,
                remediation={
                    "safe_fix_available": False,
                    "automation_level": "manual_review_required",
                    "requires_human_review": True,
                    "recommended_action": (
                        f"Move the object to a '_{expected}' database token "
                        f"(or set token_naming=OFF in config/inspect.conf if "
                        f"your project uses a different convention)."
                    ),
                },
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

    # Case 2b: DML scripts (INSERT/UPDATE/DELETE/MERGE) and ordered SQL
    # qualify through their statement target rather than a CREATE clause,
    # so the DDL-shaped _QUALIFIED_NAME_RE above never matches them. A
    # fully-qualified DML target (e.g. ``INSERT INTO Db.Object ...``)
    # gives SHIPS a literal database name to auto-tokenise, so it passes
    # exactly like case 2 (issue #410).
    if obj_type in _DML_LIKE_TYPES and _DML_QUALIFIED_TARGET_RE.search(content):
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
                # Emit at WARNING as a neutral floor — the framework
                # remaps to whatever ``rules_config["keyword_case"]`` says.
                # The default is OFF so the finding is suppressed for
                # most projects; sites that enforce the discipline opt
                # in by setting ``keyword_case=WARNING`` (or ERROR / INFO)
                # in ``config/inspect.conf``. Emitting at INFO would
                # collide with the framework's INFO-bypass (which keeps
                # deliberate policy notes like ``comma_style=as-per-source``
                # at INFO regardless of config), making the opt-in setting
                # silently ineffective.
                severity="WARNING",
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

# DML statement target qualifier: INSERT INTO / MERGE INTO / UPDATE /
# DELETE [FROM] followed by Database.Object (or {{TOKEN}}.Object). DML
# scripts have no CREATE clause for _QUALIFIED_NAME_RE to capture, so
# zero_tokens recognises their qualifier through the statement target
# instead (issue #410). Anchored to statement start (MULTILINE) so a
# dotted string literal in a VALUES clause cannot masquerade as a
# qualifier.
_DML_QUALIFIED_TARGET_RE = re.compile(
    r"^\s*(?:INSERT\s+INTO|MERGE\s+INTO|UPDATE|DELETE(?:\s+FROM)?)\s+"
    + _IDENT_OR_TOKEN_RE
    + r"\."
    + _IDENT_OR_TOKEN_RE,
    re.IGNORECASE | re.MULTILINE,
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
