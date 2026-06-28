"""
rules_catalogue.py — Canonical inspect-rule remediation catalogue (#144).

Wraps the inspect-rule registry (``validate.DEFAULT_RULES``) into the
now-familiar canonical-artefact pattern used by trust (#146),
actions (#143), capabilities (#149), policy (#151), required-evidence
(#148), and dependencies (#150).

``context/ships.rules.json`` lets an agent (or CI/CD tool) decide,
for each inspect finding, whether to:

* auto-apply a known safe fix,
* surface a guided remediation for an operator to confirm, or
* escalate for human review.

The catalogue is **per rule code**, not per finding — two findings of
the same rule share the same remediation profile. ``ships.decisions.json``
continues to carry the per-finding occurrences (file, line, message);
agents resolve the rule code against this catalogue to get the
``safe_fix_available`` / ``automation_level`` / ``recommended_action``
/ ``risk`` / ``requires_human_review`` fields.

The metadata is hand-curated. New rules added to
``validate.DEFAULT_RULES`` should also be added here; the test suite
guards that the two registries stay in lockstep.
"""

from __future__ import annotations

import json
import os
from typing import Optional


RULES_SCHEMA_VERSION = "1.0"

RULES_RESULT_FILENAME = "ships.rules.json"
RULES_RESULT_REF = f"context/{RULES_RESULT_FILENAME}"


# ---------------------------------------------------------------
# Remediation metadata per rule code
# ---------------------------------------------------------------
#
# Field semantics:
#
#   description            One sentence on what the rule checks.
#   default_severity       ERROR / WARNING / INFO / OFF — mirrors
#                          validate.DEFAULT_RULES.
#   safe_fix_available     True when a deterministic, low-risk
#                          mechanical fix exists (either built into
#                          SHIPS via a --fix-* flag or trivially
#                          scriptable from the message).
#   automation_level       "auto"   — safe to apply unattended.
#                          "guided" — apply but surface a diff /
#                                      confirmation step.
#                          "manual" — the fix is judgement-laden;
#                                      agents should not touch it
#                                      without an operator.
#   recommended_action     Imperative-mood sentence; what the actor
#                          should DO to clear the finding.
#   risk                   "low" / "medium" / "high" — risk of
#                          applying the fix automatically, NOT the
#                          risk of leaving the finding unfixed.
#   requires_human_review  True when the fix needs an operator's
#                          judgement (security, governance, semantic
#                          rename). False for mechanical reformatting.

_RULES: dict[str, dict[str, object]] = {
    # ---- Structural / naming -------------------------------------
    "db_qualifier": {
        "description": (
            "Every object reference must be qualified with its database "
            "(``Database.Object``) so the deployer never relies on the "
            "session default."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "auto",
        "recommended_action": (
            "Add the database qualifier (e.g. ``MYDB.TableName``) before "
            "the unqualified object name."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "extension": {
        "description": (
            "A staged file's extension must match the DDL kind of its "
            "content — ``*.tbl`` for tables, ``*.viw`` for views, etc."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": (
            "Rename the file so its extension matches the DDL kind, or "
            "move the content into a file with the correct extension."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "type_suffix": {
        "description": (
            "Tokens that resolve to object names must end with the kind "
            "suffix (``_T`` table, ``_V`` view, ``_M`` macro, ``_P`` "
            "procedure / JAR, ``_F`` function, ``_X`` STO)."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "auto",
        "recommended_action": (
            "Append the correct kind suffix to the token name in ``token_map.conf``."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "hardcoded_name": {
        "description": (
            "Object names that vary across environments should be "
            "tokenised, not hard-coded."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": (
            "Replace the literal name with a ``{{TOKEN}}`` reference "
            "and add the mapping to ``token_map.conf``."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "eponymous": {
        "description": (
            "A view and table sharing the same base name in different "
            "databases cause ambiguity for agents and operators."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Rename either the view or the table so the base names differ."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "one_object": {
        "description": (
            "Each DDL file should contain exactly one object so the "
            "deployer can deploy and roll back at object granularity."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": (
            "Split the file so each DDL statement lives in its own file."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "non_ascii": {
        "description": (
            "Non-ASCII characters in SQL source files cause Teradata "
            "error 6706 on LATIN-encoded databases (the server default)."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "auto",
        "recommended_action": (
            "Replace non-ASCII characters with ASCII equivalents; "
            "``ships inspect --fix-non-ascii`` does this automatically."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    # ---- DDL style -----------------------------------------------
    "keyword_case": {
        "description": (
            "SQL keywords (``CREATE``, ``TABLE``, ``SELECT`` ...) "
            "are conventionally uppercase. Teradata case-folds them "
            "and runs either way, so this is a style preference, not "
            "a correctness defect."
        ),
        "default_severity": "OFF",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Opt in by setting ``keyword_case=WARNING`` (or ERROR / INFO) "
            "in ``config/inspect.conf`` if your project enforces the "
            "UPPERCASE keyword convention. Most sites can leave this off."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "comma_log_level": {
        "description": (
            "Severity dial for the comma-style finding; does not emit its own findings."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Adjust ``comma_log_level`` in ``inspect.conf`` to change "
            "the severity of comma-style findings."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "set_multiset": {
        "description": (
            "``CREATE TABLE`` should declare SET (deduplicate rows) or "
            "MULTISET (allow duplicates) explicitly."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Choose SET or MULTISET based on the table's intended row "
            "semantics; do not guess."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "deploy_intent": {
        "description": (
            "Files may declare deploy intent (``-- intent: replace`` "
            "etc.) so the deployer knows whether to DROP+CREATE."
        ),
        "default_severity": "OFF",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Review the deploy intent annotation against the object's "
            "intended lifecycle."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "view_column_list": {
        "description": (
            "Views should declare an explicit column list so the view's "
            "contract is readable without introspecting the database."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": (
            "Add an explicit column list between the view name and "
            "``AS`` — e.g. ``CREATE VIEW db.MyView (ColA, ColB) AS "
            "SELECT ...``."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "ddl_terminator": {
        "description": (
            "Every deployable DDL statement must terminate with a "
            "semicolon; missing terminators break statement boundary "
            "detection."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "auto",
        "recommended_action": (
            "Append a semicolon to the final statement; "
            "``ships inspect --fix-ddl-terminators`` does this "
            "automatically."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "comment_length": {
        "description": (
            "Teradata ``COMMENT ON ... IS '...'`` text is limited to 254 characters."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": ("Shorten the COMMENT text to 254 characters or fewer."),
        "risk": "low",
        "requires_human_review": False,
    },
    # ---- Security ------------------------------------------------
    "secret_scan": {
        "description": (
            "DDL/DML bodies must not contain hard-coded credentials, "
            "JDBC connection strings, PEM private keys, or AWS keys."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Remove the credential from source. Replace it with a "
            "``$env:`` or ``vault:`` reference and store the secret in "
            "the configured secrets backend."
        ),
        "risk": "high",
        "requires_human_review": True,
    },
    "dynamic_sql": {
        "description": (
            "``EXECUTE IMMEDIATE`` / ``DBC.SYSEXECSQL`` / ``DBC.EXECSQL`` in "
            "procedures and macros. Each finding carries a per-finding "
            "``risk_category`` (#166): ``dynamic_sql_execute_immediate``, "
            "``dynamic_sql_calls_sys_exec_sql``, "
            "``dynamic_sql_concatenates_literal``, or — highest risk — "
            "``dynamic_sql_uses_unsanitised_parameter`` when a "
            "variable/parameter is concatenated into the SQL text "
            "(possible injection)."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Review for injection, privilege, and deployment risk. Do NOT "
            "auto-remove dynamic SQL — it is often intentional. Parameterise "
            "operator-supplied inputs rather than concatenating them."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "sensitivity_class": {
        "description": (
            "Every payload object should declare a sensitivity class "
            "(via a ``.cls`` companion file) so downstream data "
            "governance can enforce access policy."
        ),
        "default_severity": "OFF",
        "safe_fix_available": True,
        "automation_level": "guided",
        "recommended_action": (
            "Create a ``.cls`` companion file declaring the object's "
            "sensitivity class (e.g. PUBLIC, INTERNAL, CONFIDENTIAL, "
            "RESTRICTED)."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "vault_ref": {
        "description": (
            "Payload files must not carry unresolved ``$env:`` or "
            "``vault:`` references at package time."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Populate the referenced secret in the configured backend, "
            "or remove the reference if no longer needed."
        ),
        "risk": "high",
        "requires_human_review": True,
    },
    # ---- Cross-file / governance --------------------------------
    "zero_tokens": {
        "description": (
            "Every deployable DDL/DML object must reference at least "
            "one ``{{TOKEN}}`` so it resolves correctly per environment."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "guided",
        "recommended_action": (
            "Tokenise the environment-specific names so the file can "
            "be promoted across DEV → TST → PRD."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "intra_package_dependency": {
        "description": (
            "Diagnoses files in the package that depend on other files "
            "in the same package; the auto-split builder now handles "
            "this so the rule is off by default."
        ),
        "default_severity": "OFF",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "If the rule is enabled, either let the auto-split builder "
            "handle the dependency or split the package manually."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "object_placement": {
        "description": (
            "Each file must live in the phase directory that matches "
            "its DDL kind (``03_ddl/tables`` for tables, etc.)."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": True,
        "automation_level": "auto",
        "recommended_action": (
            "Move the file to the phase / kind directory listed in "
            "``config/object_placement.yaml``."
        ),
        "risk": "low",
        "requires_human_review": False,
    },
    "view_macro_self_reference": {
        "description": (
            "A view or macro must not reference its own "
            "``database.name`` during creation — Teradata rejects the "
            "DDL."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Eliminate the self-reference by renaming or restructuring "
            "the view / macro."
        ),
        "risk": "high",
        "requires_human_review": True,
    },
    # ---- Grant validation ---------------------------------------
    "public_grant_on_tables": {
        "description": (
            "``GRANT ... TO PUBLIC`` on tables grants access to every "
            "user on the system, almost always a mistake."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Replace ``GRANT ... TO PUBLIC`` with grants to explicit roles or users."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "review_unmapped_grants": {
        "description": ("A DCL grant references a role / user not in the grant map."),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Add the role / user to the grant map, or remove the "
            "grant if it is no longer required."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "warn_extra_grants": {
        "description": (
            "DCL contains a grant that the inferred grant set does not require. "
            "Default WARNING — the operator may have added grants the inferrer "
            "cannot derive from DDL, which is a soft signal rather than a "
            "packaging failure."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Either remove the extra grant from DCL or extend the "
            "inferred grant set to cover the use case. Promote to ERROR in "
            "``config/inspect.conf`` when you want .dcl files to be a pure "
            "reflection of the inferred set."
        ),
        "risk": "low",
        "requires_human_review": True,
    },
    "warn_external_grants": {
        "description": (
            "DCL grants access to a grantee that no DDL in the package implies "
            "— i.e. the grantee is external to the package's intent."
        ),
        "default_severity": "INFO",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Often legitimate (e.g. roles or databases granted access from "
            "outside this package). If the grant is unwanted, remove it from "
            "DCL; if every grant must be traceable to in-package DDL, promote "
            "this rule to ERROR in config/inspect.conf."
        ),
        "risk": "low",
        "requires_human_review": True,
    },
    # ---- Tokenised filename eponymy (issue #365, PR-5) -----------
    "filename_token_format": {
        "description": (
            "Filename contains a malformed ``{{...}}`` marker (orphan brace "
            "or invalid token contents). Package cannot substitute it; the "
            "file would land on an unintended path."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Rename the file so every ``{{TOKEN}}`` marker is well formed. "
            "Token names are uppercase letters, digits, and underscores; "
            "braces must be paired."
        ),
        "risk": "low",
        "requires_human_review": True,
    },
    "object_level_grant": {
        "description": (
            "GRANT/REVOKE targets a specific object (``ON db.obj``) or column "
            "(``GRANT SELECT (col) ON …``) rather than the containing database. "
            "Teradata best practice is to grant at the database level so "
            "privileges propagate and the access surface stays auditable."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Move the grant to the containing database (``GRANT … ON db TO …``), "
            "or — when the object-level grant is genuinely required — set "
            "``object_level_grant=OFF`` in ``config/inspect.conf``."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "destructive_change": {
        "description": (
            "An explicit destructive statement (``DROP …`` / ``DELETE "
            "DATABASE`` / ``ALTER TABLE … DROP``) was found in a payload "
            "file. These remove structures or data. The SHIPS deployer owns "
            "idempotent CREATE (any drop+create is its internal concern), so "
            "payload files should never carry destructive DDL."
        ),
        "default_severity": "ERROR",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Remove the destructive statement from the payload, or obtain "
            "explicit human approval before deploying. An agent must not "
            "auto-fix or deploy a destructive change without approval."
        ),
        "risk": "high",
        "requires_human_review": True,
    },
    "data_dependent_change": {
        "description": (
            "An ALTER TABLE / CREATE UNIQUE INDEX whose success depends on "
            "the data already in the table (adding NOT NULL without a "
            "DEFAULT, UNIQUE on possibly-duplicate data, CHECK existing rows "
            "may violate, or a PRIMARY INDEX / partitioning change that moves "
            "data). Structurally valid but can fail or rewrite the table at "
            "deploy time."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Run the finding's recommended precheck against the target "
            "environment (live metadata required); backfill / dedupe / supply "
            "a DEFAULT, or obtain approval before deploying."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "non_linear_package_history": {
        "description": (
            "A project-level check over the built packages under "
            "``releases/``. Flags a package sequence that cannot be trusted: "
            "a build number reused with different contents, an older build "
            "appearing after a newer one, an orphaned prereqs/main half, a "
            "package requiring a missing sibling, or an integrity sidecar "
            "that no longer matches its archive."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Rebuild the affected release group cleanly (a fresh build "
            "number), restore the missing package half, or regenerate the "
            "integrity sidecar. Never reuse a build number for different "
            "contents."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "transaction_control_in_payload": {
        "description": (
            "A transaction-control statement (BT/ET, BEGIN/END TRANSACTION, "
            "COMMIT, ROLLBACK) appears in a payload file. Transaction "
            "boundaries are owned by the SHIPS deployer, so payload files "
            "should not open, commit, or roll back transactions. Statements "
            "inside a procedure/function BEGIN…END body are exempt."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Remove the transaction-control statement from the payload; the "
            "deployer manages BT/ET / COMMIT / ROLLBACK around each "
            "deployment unit."
        ),
        "risk": "medium",
        "requires_human_review": True,
    },
    "token_naming": {
        "description": (
            "A DDL object's database token should carry the kind suffix "
            "matching its object type — tables (and their indexes/triggers) "
            "in a ``{{*_T}}`` token, views in a ``{{*_V}}`` token. Flags only "
            "a clear mismatch (e.g. a view in a ``_T`` token); tokens without "
            "a kind suffix are left alone, and macro/procedure/function kinds "
            "(site-configurable) are enforced via a custom lint policy instead."
        ),
        "default_severity": "WARNING",
        "safe_fix_available": False,
        "automation_level": "manual",
        "recommended_action": (
            "Move the object to a database token with the correct kind suffix, "
            "or set ``token_naming=OFF`` in ``config/inspect.conf`` if your "
            "project uses a different naming convention."
        ),
        "risk": "low",
        "requires_human_review": True,
    },
}


# ---------------------------------------------------------------
# Computation
# ---------------------------------------------------------------


def compute_rules_document() -> dict:
    """Return the agent-facing rules catalogue document.

    Shape:
    ``{schema_version, generated_by, rules: {rule_code: {...metadata}}}``.
    """
    return {
        "schema_version": RULES_SCHEMA_VERSION,
        "generated_by": "td_release_packager.rules_catalogue",
        "rules": {code: dict(meta) for code, meta in _RULES.items()},
    }


def rule_codes() -> list[str]:
    """Return all rule codes in catalogue order."""
    return list(_RULES.keys())


def remediation_for(rule_code: str) -> Optional[dict]:
    """Return the remediation metadata for ``rule_code`` or None."""
    meta = _RULES.get(rule_code)
    if meta is None:
        return None
    return dict(meta)


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_rules_result(pkg_dir: str) -> str:
    """Write the rules catalogue to ``<pkg_dir>/context/ships.rules.json``.

    Returns the absolute path that was written.
    """
    doc = compute_rules_document()
    path = os.path.join(pkg_dir, "context", RULES_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_rules_result(pkg_dir: str) -> Optional[dict]:
    """Load ``context/ships.rules.json`` from ``pkg_dir`` or return
    None when absent / unreadable."""
    path = os.path.join(pkg_dir, "context", RULES_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
