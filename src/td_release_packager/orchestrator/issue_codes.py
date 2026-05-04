"""
issue_codes.py — Central registry for ``StageRecorder.add_issue`` codes.

Every issue recorded into ``decisions.json`` carries a short stable
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
      - PROPERTIES_*  .properties file handling
      - HARVEST_*     Reserved for harvest stage (item 4 rollout)
      - INSPECT_*     Reserved for inspect stage (item 4 rollout)
      - PACKAGE_*     Reserved for package stage (item 4 rollout)
      - GENERATE_*    Reserved for generate stage (item 7)
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

#: Properties file path was provided but the file does not exist.
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
# Registry — code → human description
# ---------------------------------------------------------------


ISSUE_CODES: Dict[str, str] = {
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
        "The --properties path was supplied but the file does not "
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
