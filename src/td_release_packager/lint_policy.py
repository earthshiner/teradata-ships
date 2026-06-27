"""
lint_policy.py — Custom SHIPS lint policy (issue #167).

Lets teams express project- or organisation-specific Teradata deployment
standards as data, without writing Python. A policy file at
``config/ships_lint_policy.yaml`` declares custom rules that ``inspect``
applies alongside the built-in checks. Each rule is a deny / required /
exclude regex over SQL text, scoped by object type and pipeline phase,
carrying a severity and agent-facing remediation metadata.

Design guarantees:
    - **No code execution.** Patterns are compiled with ``re`` and matched
      against payload text; SQL is treated as data, never evaluated.
    - **Fail closed in strict mode.** A malformed policy (bad YAML, unknown
      severity, uncompilable regex, …) raises ``LintPolicyError`` so a
      platform-mode run aborts rather than silently skipping a control.
    - **Lenient in developer mode.** Invalid individual rules are logged and
      skipped; the valid ones still load.

The loader returns ``CustomLintRule`` objects; application lives in
``validate._check_custom_policy`` so custom findings flow through the same
``ValidationIssue`` pipeline (console + ``ships.decisions.json``) as
built-in rules.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Set

import yaml

from td_release_packager.classifier import BASE_TYPES

logger = logging.getLogger(__name__)

#: Policy file name, resolved under ``<project>/config/``.
POLICY_FILENAME = "ships_lint_policy.yaml"

#: Severities a custom rule may declare. ``OFF`` loads the rule but
#: suppresses its findings (parity with inspect.conf semantics).
VALID_SEVERITIES: Set[str] = {"ERROR", "WARNING", "INFO", "OFF"}

#: Pipeline phases a rule may be scoped to. Aliases on the right map to
#: the canonical token on the left so authors can write either form.
_PHASE_ALIASES: Dict[str, str] = {
    "DDL": "DDL",
    "DCL": "DCL",
    "DML": "DML",
    "PREREQS": "PREREQS",
    "PRE-REQUISITES": "PREREQS",
    "PRE_REQUISITES": "PREREQS",
    "POST-INSTALL": "POST_INSTALL",
    "POST_INSTALL": "POST_INSTALL",
    "POST-INSTALL.": "POST_INSTALL",
}

#: Canonical phase tokens.
VALID_PHASES: Set[str] = {"DDL", "DCL", "DML", "PREREQS", "POST_INSTALL"}

#: Object types a rule may be scoped to: the classifier's base types plus
#: ``DCL`` as a convenience alias for GRANT/REVOKE statements.
VALID_OBJECT_TYPES: Set[str] = set(BASE_TYPES) | {"DCL"}

#: Agent-facing remediation keys carried through to machine-readable output.
#: Booleans are normalised to bool; the rest pass through as strings.
_REMEDIATION_BOOL_KEYS = {
    "safe_fix_available",
    "agent_may_fix",
    "agent_may_suggest",
    "requires_human_review",
    "requires_live_metadata",
}
_REMEDIATION_STR_KEYS = {
    "automation_level",
    "recommended_action",
    "stop_condition",
    "blocked_action",
}


class LintPolicyError(Exception):
    """Raised when a policy file is unreadable or invalid in strict mode."""


@dataclass
class CustomLintRule:
    """A single compiled custom lint rule."""

    name: str
    description: str
    severity: str
    object_types: Set[str] = field(default_factory=set)
    phases: Set[str] = field(default_factory=set)
    deny_pattern: Optional[Pattern] = None
    required_pattern: Optional[Pattern] = None
    exclude_pattern: Optional[Pattern] = None
    remediation: Dict[str, Any] = field(default_factory=dict)


def policy_path(project_dir: str) -> str:
    """Return the resolved path to the policy file (may not exist)."""
    return os.path.join(project_dir, "config", POLICY_FILENAME)


def load_lint_policy(project_dir: str, strict: bool = False) -> List[CustomLintRule]:
    """Load and validate the custom lint policy for ``project_dir``.

    Args:
        project_dir: SHIPS project root. The policy is read from
                     ``<project_dir>/config/ships_lint_policy.yaml``.
        strict:      Platform mode. When True, any policy-level or
                     rule-level error raises ``LintPolicyError`` (fail
                     closed). When False, rule-level errors are logged
                     and the offending rule is skipped.

    Returns:
        The list of valid ``CustomLintRule`` objects (empty when no
        policy file exists).

    Raises:
        LintPolicyError: In strict mode on any policy/rule error; in both
            modes when the file exists but is not valid YAML or not a
            mapping with a ``rules`` list (a structural error that cannot
            be recovered per-rule).
    """
    path = policy_path(project_dir)
    if not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise LintPolicyError(f"{path}: not valid YAML — {exc}") from exc

    if data is None:
        return []
    if not isinstance(data, dict) or "rules" not in data:
        raise LintPolicyError(
            f"{path}: top-level must be a mapping with a 'rules' list."
        )
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise LintPolicyError(f"{path}: 'rules' must be a list.")

    rules: List[CustomLintRule] = []
    seen: Set[str] = set()
    for index, raw in enumerate(raw_rules):
        try:
            rule = _parse_rule(raw, index, seen)
        except LintPolicyError as exc:
            if strict:
                raise
            logger.warning("Skipping invalid lint rule: %s", exc)
            continue
        seen.add(rule.name)
        rules.append(rule)

    logger.info("Custom lint policy: %d rule(s) loaded from %s", len(rules), path)
    return rules


def _parse_rule(raw: Any, index: int, seen: Set[str]) -> CustomLintRule:
    """Validate one raw rule mapping into a ``CustomLintRule``."""
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise LintPolicyError(f"{where}: must be a mapping.")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LintPolicyError(f"{where}: 'name' is required and must be a string.")
    name = name.strip()
    if name in seen:
        raise LintPolicyError(f"{where}: duplicate rule name {name!r}.")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise LintPolicyError(f"{where} ({name}): 'description' must be a string.")

    severity = str(raw.get("severity", "WARNING")).strip().upper()
    if severity == "WARN":
        severity = "WARNING"
    if severity not in VALID_SEVERITIES:
        raise LintPolicyError(
            f"{where} ({name}): severity {severity!r} invalid — "
            f"expected one of {sorted(VALID_SEVERITIES)}."
        )

    applies_to = raw.get("applies_to") or {}
    if not isinstance(applies_to, dict):
        raise LintPolicyError(f"{where} ({name}): 'applies_to' must be a mapping.")

    object_types = _parse_scope(
        applies_to.get("object_types"),
        valid={t.upper() for t in VALID_OBJECT_TYPES},
        what="object_types",
        where=where,
        name=name,
    )
    phases = _parse_scope(
        applies_to.get("phases"),
        valid=set(_PHASE_ALIASES),
        what="phases",
        where=where,
        name=name,
        canonicalise=_PHASE_ALIASES,
    )

    deny = _compile(raw.get("deny_pattern"), "deny_pattern", where, name)
    required = _compile(raw.get("required_pattern"), "required_pattern", where, name)
    exclude = _compile(raw.get("exclude_pattern"), "exclude_pattern", where, name)
    if deny is None and required is None:
        raise LintPolicyError(
            f"{where} ({name}): a rule needs at least one of "
            f"'deny_pattern' or 'required_pattern'."
        )

    remediation = _parse_remediation(raw.get("remediation"), where, name)

    return CustomLintRule(
        name=name,
        description=description or name,
        severity=severity,
        object_types=object_types,
        phases=phases,
        deny_pattern=deny,
        required_pattern=required,
        exclude_pattern=exclude,
        remediation=remediation,
    )


def _parse_scope(
    value: Any,
    *,
    valid: Set[str],
    what: str,
    where: str,
    name: str,
    canonicalise: Optional[Dict[str, str]] = None,
) -> Set[str]:
    """Validate an ``applies_to`` list (object_types / phases) → upper set."""
    if value is None:
        return set()
    if not isinstance(value, list):
        raise LintPolicyError(f"{where} ({name}): '{what}' must be a list.")
    out: Set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LintPolicyError(
                f"{where} ({name}): '{what}' entries must be strings."
            )
        token = item.strip().upper()
        if token not in valid:
            raise LintPolicyError(
                f"{where} ({name}): unknown {what} {item!r} — "
                f"expected one of {sorted(valid)}."
            )
        out.add(canonicalise[token] if canonicalise else token)
    return out


def _compile(value: Any, what: str, where: str, name: str) -> Optional[Pattern]:
    """Compile a rule regex (case-insensitive); validate it is a string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LintPolicyError(f"{where} ({name}): '{what}' must be a non-empty string.")
    try:
        return re.compile(value, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise LintPolicyError(
            f"{where} ({name}): '{what}' is not a valid regex — {exc}"
        ) from exc


def _parse_remediation(value: Any, where: str, name: str) -> Dict[str, Any]:
    """Validate the optional remediation block into a typed dict."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LintPolicyError(f"{where} ({name}): 'remediation' must be a mapping.")
    out: Dict[str, Any] = {}
    for key, val in value.items():
        if key in _REMEDIATION_BOOL_KEYS:
            if not isinstance(val, bool):
                raise LintPolicyError(
                    f"{where} ({name}): remediation.{key} must be a boolean."
                )
            out[key] = val
        elif key in _REMEDIATION_STR_KEYS:
            if not isinstance(val, str):
                raise LintPolicyError(
                    f"{where} ({name}): remediation.{key} must be a string."
                )
            out[key] = val
        else:
            # Unknown remediation keys pass through as-is (forward-compatible)
            # but only when JSON-serialisable scalars/lists/maps.
            out[key] = val
    return out
