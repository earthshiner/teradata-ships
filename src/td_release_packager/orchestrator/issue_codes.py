"""
issue_codes.py — Central registry for ``StageRecorder.add_issue`` codes.

Every issue recorded into ``ships.decisions.json`` carries a short stable
identifier (``code``) so downstream tools (``explain``, CI dashboards,
auditors) can group and trend issues without parsing free-text
``message`` fields. This module is the single source of truth for the
catalogue.

Why a registry rather than ad-hoc strings:

  - **Stability**: codes get referenced from CI rules, runbooks, and
    docs. Renaming one in passing breaks consumers silently. Putting
    them here forces the rename to be a deliberate change.
  - **Discoverability**: a newcomer adding a stage can see what codes
    already exist and reuse rather than invent.
  - **Documentation**: ``explain`` (build-order item 6) can render
    each issue's description from this registry without per-stage
    knowledge.

Naming convention:
    ``<DOMAIN>_<CONDITION>`` in SCREAMING_SNAKE_CASE.

    Domain prefixes currently in use:
      - TOKEN_*       Token substitution / cascade resolution
      - PROPERTIES_*  .conf file handling
      - HARVEST_*     Harvest / ingest stage (item 4c)
      - INSPECT_*     Inspect / validate stage (item 4b)
      - ANALYSE_*     Dependency analysis stage (item 4d)
      - PACKAGE_*     Package / build stage (item 4f)
      - GENERATE_*    Generate / view-layer stage (item 7)
"""

from __future__ import annotations

from typing import Dict


# ---------------------------------------------------------------
# Token / properties domain
# ---------------------------------------------------------------

#: Token referenced in DDL but no value defined in properties.
TOKEN_UNDEFINED = "TOKEN_UNDEFINED"

#: Token defined in properties but never referenced in any DDL file.
TOKEN_UNUSED = "TOKEN_UNUSED"

#: Config file path was provided but the file does not exist.
PROPERTIES_NOT_FOUND = "PROPERTIES_NOT_FOUND"


# ---------------------------------------------------------------
# Inspect domain (build-order item 4 — inspect rollout)
# ---------------------------------------------------------------
#
# Inspect runs three steps and surfaces three families of finding.
# Coarse codes (one per step) keep the registry small while still
# letting `explain` group issues by step. The originating rule
# name from validate.py is carried in the issue's free-text
# message so finer-grained queries remain possible.

#: Step 0 — A {{TOKEN}} marker is malformed (whitespace inside
#: braces, double-tokenisation, orphan braces). Survives substitution
#: silently and ends up in deployed SQL.
INSPECT_TOKEN_MALFORMED = "INSPECT_TOKEN_MALFORMED"

#: Step 1 — A Coding Discipline lint rule fired against a DDL file.
#: The originating rule (db_qualifier, set_multiset, deploy_intent,
#: ...) appears in the message body.
INSPECT_LINT_VIOLATION = "INSPECT_LINT_VIOLATION"

#: Step 2 — Cross-file grant validation found a drifted, missing,
#: or orphaned grant relative to the inferred intent.
INSPECT_GRANT_VIOLATION = "INSPECT_GRANT_VIOLATION"


# ---------------------------------------------------------------
# Harvest domain (build-order item 4c)
# ---------------------------------------------------------------

#: A source file could not be classified into any known DDL/DML type.
#: Unclassified files are not placed in the payload. The filename is
#: carried in the message body so the operator can investigate.
HARVEST_UNCLASSIFIED = "HARVEST_UNCLASSIFIED"

#: The rich classifier surfaced a warning for a source file — typically
#: a filename-vs-content mismatch (e.g. a .tbl file containing a VIEW
#: statement) or an unrecognised external reference pattern.
HARVEST_CLASSIFICATION_WARNING = "HARVEST_CLASSIFICATION_WARNING"

#: A hardcoded database or user name was found in the payload that has
#: not yet been replaced with a {{TOKEN}}. Recorded at ``info`` level —
#: these are candidates, not errors; the operator decides whether to
#: tokenise them.
HARVEST_TOKEN_CANDIDATE = "HARVEST_TOKEN_CANDIDATE"


# ---------------------------------------------------------------
# Analyse domain (build-order item 4d)
# ---------------------------------------------------------------

#: A circular dependency was detected in the DDL object graph. Objects
#: involved in the cycle cannot be assigned to waves and will block
#: deployment. The cycle members are listed in the message body.
ANALYSE_CYCLE = "ANALYSE_CYCLE"

#: A DDL object references another object that is not present in the
#: current payload. The reference may resolve at deploy time (the target
#: already exists on the target system) or may indicate a missing file.
#: Recorded at ``info`` level so the analyst can decide.
ANALYSE_EXTERNAL_REF = "ANALYSE_EXTERNAL_REF"


# ---------------------------------------------------------------
# Package domain (build-order item 4f)
# ---------------------------------------------------------------

#: The builder emitted a warning during package assembly — typically a
#: token substitution anomaly, a missing rollback script, or a file
#: that could not be included. The full warning text is in the message.
PACKAGE_WARNING = "PACKAGE_WARNING"


# ---------------------------------------------------------------
# Generate domain (build-order item 7)
# ---------------------------------------------------------------

#: The view-layer generator emitted a non-fatal warning — typically
#: a requested module that has no matching table files, a column that
#: couldn't be resolved for SELECT * expansion, or a REPLACE/CREATE
#: rewrite that may need manual review.
GENERATE_WARNING = "GENERATE_WARNING"

#: The view-layer generator encountered a fatal error that prevented
#: output from being written — typically no tables found in the
#: payload (suggesting the project has not been harvested yet) or
#: no matching modules after filtering.
GENERATE_ERROR = "GENERATE_ERROR"


# ---------------------------------------------------------------
# Registry — code → human description
# ---------------------------------------------------------------


ISSUE_CODES: Dict[str, str] = {
    HARVEST_UNCLASSIFIED: (
        "A source file could not be classified into any known DDL/DML type "
        "and was not placed in the payload. Investigate the file — it may "
        "use a non-standard SQL dialect, contain only comments, or be "
        "misnamed. The filename is in the message body."
    ),
    HARVEST_CLASSIFICATION_WARNING: (
        "The classifier surfaced a warning for a source file. Common causes: "
        "filename extension does not match the DDL verb (e.g. a .tbl file "
        "containing REPLACE VIEW), or an external reference pattern that "
        "could not be resolved. Review and rename the file or correct the DDL."
    ),
    HARVEST_TOKEN_CANDIDATE: (
        "A hardcoded database or user name was detected in the payload that "
        "has not yet been replaced with a {{TOKEN}} reference. This is an "
        "informational signal — the operator decides whether to tokenise it "
        "or leave it as a literal. Re-run harvest with --generate-token-map "
        "to produce a token_map.conf for substitution."
    ),
    ANALYSE_CYCLE: (
        "A circular dependency was detected in the DDL object graph. The "
        "objects involved cannot be assigned to deployment waves and will "
        "block wave-parallel deployment. Break the cycle by adding a "
        "pre-requisite DDL split or restructuring object ownership. The "
        "cycle members are listed in the message body."
    ),
    ANALYSE_EXTERNAL_REF: (
        "A DDL object references another object that is not present in the "
        "current payload. The reference may resolve safely at deploy time if "
        "the target already exists on the target system — or it may indicate "
        "a missing file that should be included in the project. Review the "
        "reference and add the target to the payload if needed."
    ),
    GENERATE_WARNING: (
        "The view-layer generator emitted a warning during generation. "
        "Common causes: a requested module has no matching table files in "
        "the payload (check that harvest ran first), a column could not be "
        "resolved for SELECT * expansion, or a business-view rewrite "
        "produced output that may need manual review."
    ),
    GENERATE_ERROR: (
        "The view-layer generator encountered a fatal error and could not "
        "write output. Most common cause: no tables found in the payload "
        "(run harvest first to populate payload/database/DDL/tables/). "
        "Also occurs when requested modules don't match any discovered "
        "table files."
    ),
    PACKAGE_WARNING: (
        "The builder emitted a warning during package assembly. Typical "
        "causes: a token substitution anomaly (undefined token, double- "
        "substitution), a missing rollback script, or a file that could not "
        "be included in the archive. The full warning text is in the message."
    ),
    TOKEN_UNDEFINED: (
        "A {{TOKEN}} reference in DDL has no corresponding entry in "
        "the resolved properties file. Build will fail at substitution."
    ),
    TOKEN_UNUSED: (
        "A token is defined in the properties file but never "
        "referenced by any DDL file. Likely dead config — review "
        "whether to remove it or whether a referencing file is "
        "missing."
    ),
    PROPERTIES_NOT_FOUND: (
        "The --env-config path was supplied but the file does not "
        "exist on disc. Check the path and re-run."
    ),
    INSPECT_TOKEN_MALFORMED: (
        "A {{TOKEN}} marker is malformed — typically stray whitespace "
        "inside the braces, a double-substitution from a re-run "
        "harvest, or orphan braces from an editor mishap. Survives "
        "substitution silently and ends up in deployed SQL, so it "
        "must be fixed at source."
    ),
    INSPECT_LINT_VIOLATION: (
        "A Coding Discipline rule (db_qualifier, set_multiset, "
        "deploy_intent, etc.) flagged a DDL file. The originating "
        "rule name is carried in the message body so explain and "
        "CI tooling can group findings by rule."
    ),
    INSPECT_GRANT_VIOLATION: (
        "Cross-file grant validation found a discrepancy between the "
        "inferred grant set (from DDL intent analysis) and the .grt "
        "files in the project's DCL/ tree — typically a drifted, "
        "missing, or orphaned grant. Re-run with --fix-grants to "
        "regenerate the .grt files."
    ),
}


def describe(code: str) -> str:
    """
    Return the human description for an issue code.

    Args:
        code: A code that should appear in ``ISSUE_CODES``.

    Returns:
        The description, or the literal string ``"(unregistered code)"``
        if the code is not in the registry. The fallback is intentional
        — never raise on a lookup, since a missing description is a
        documentation gap rather than a runtime fault.
    """
    return ISSUE_CODES.get(code, "(unregistered code)")


def is_registered(code: str) -> bool:
    """True if ``code`` appears in the central registry."""
    return code in ISSUE_CODES
