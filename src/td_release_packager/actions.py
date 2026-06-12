"""
actions.py — Canonical machine-readable action controls for SHIPS packages.

Issue #143. The trust result (see ``trust.py``) tells an agent whether a
package is safe; this module tells an agent *which actions* it may take
on the package right now. ``context/ships.actions.json`` is the single
source of truth; ``ships.context.json`` / ``ships.manifest.json`` /
``ships.handoff.json`` / ``ships.index.json`` each carry an
``actions_ref`` pointer back to this file.

**Action vocabulary (closed set v1)**

    deploy             Run the package against a target environment.
    dry_run            Run the package with --dry-run (no changes applied).
    modify_payload     Edit files under ``payload/``. Only meaningful for
                       environment_prereq packages that await DBA review.
    repackage          Re-run ``ships repackage`` to refresh the archive
                       after DBA edits.
    verify_integrity   Validate the package hash and signature.
    rollback           Run the rollback path against a previously
                       deployed instance of this package.
    forward_to_human   Hand the package off to a human operator.

**Three categorisation lists**

    allowed_actions[]            Simple action names — safe to take
                                 autonomously.
    blocked_actions[]            Objects (action, reason, evidence_ref)
                                 — agent must NOT take. Reason and
                                 evidence pointer let the agent
                                 explain itself.
    requires_human_approval[]    Objects (action, reason, evidence_ref)
                                 — needs human approval before taking.

Three convenience booleans mirror the lists for fast branching:
``deploy_allowed``, ``dry_run_allowed``, ``payload_modification_allowed``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

ACTIONS_SCHEMA_VERSION = "1.0"

ACTIONS_RESULT_FILENAME = "ships.actions.json"
ACTIONS_RESULT_REF = f"context/{ACTIONS_RESULT_FILENAME}"


# ---------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------

ACTION_DEPLOY = "deploy"
ACTION_DRY_RUN = "dry_run"
ACTION_MODIFY_PAYLOAD = "modify_payload"
ACTION_REPACKAGE = "repackage"
ACTION_VERIFY_INTEGRITY = "verify_integrity"
ACTION_ROLLBACK = "rollback"
ACTION_FORWARD_TO_HUMAN = "forward_to_human"

ALL_ACTIONS = (
    ACTION_DEPLOY,
    ACTION_DRY_RUN,
    ACTION_MODIFY_PAYLOAD,
    ACTION_REPACKAGE,
    ACTION_VERIFY_INTEGRITY,
    ACTION_ROLLBACK,
    ACTION_FORWARD_TO_HUMAN,
)


# ---------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------

REASON_TRUST_BLOCKED = "trust_status_blocked"
REASON_TRUST_CAVEATS = "trust_caveats_present"
REASON_DBA_REVIEW_REQUIRED = "dba_review_required"
REASON_NOT_ENVIRONMENT_PREREQ = "not_environment_prereq_package"
REASON_ALWAYS_SAFE = "always_safe"


# ---------------------------------------------------------------
# Evidence references
# ---------------------------------------------------------------

_EVIDENCE_TRUST = "context/ships.trust.json"
_EVIDENCE_DBA = "context/prerequisites/DBA_INSTRUCTIONS.md"


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class ActionConstraint:
    """One action that is blocked or requires approval, plus the why."""

    action: str
    reason: str
    evidence_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "evidence_ref": self.evidence_ref,
        }


@dataclass
class ActionsReport:
    """Aggregate action controls for a package."""

    evaluated_at: str
    allowed_actions: List[str] = field(default_factory=list)
    blocked_actions: List[ActionConstraint] = field(default_factory=list)
    requires_human_approval: List[ActionConstraint] = field(default_factory=list)
    schema_version: str = ACTIONS_SCHEMA_VERSION

    @property
    def deploy_allowed(self) -> bool:
        return ACTION_DEPLOY in self.allowed_actions

    @property
    def dry_run_allowed(self) -> bool:
        return ACTION_DRY_RUN in self.allowed_actions

    @property
    def payload_modification_allowed(self) -> bool:
        return ACTION_MODIFY_PAYLOAD in self.allowed_actions

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "deploy_allowed": self.deploy_allowed,
            "dry_run_allowed": self.dry_run_allowed,
            "payload_modification_allowed": self.payload_modification_allowed,
            "allowed_actions": list(self.allowed_actions),
            "blocked_actions": [c.to_dict() for c in self.blocked_actions],
            "requires_human_approval": [
                c.to_dict() for c in self.requires_human_approval
            ],
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def compute_actions_report(
    *,
    trust: Dict[str, Any],
    role: str,
    has_dba_placeholders: bool = False,
) -> ActionsReport:
    """
    Derive the action controls for a package.

    Args:
        trust: The canonical trust document (``context/ships.trust.json``).
        role:  ``manifest.role`` — ``main`` / ``prereqs`` /
               ``environment_prereqs`` / ``single`` / ``""``.
        has_dba_placeholders: True when the package still contains
               ``<DBA_*>`` placeholders awaiting DBA review.

    Returns:
        Populated ``ActionsReport``.
    """
    status = str(trust.get("status", "")).upper()
    is_blocked = status == "BLOCKED"
    has_caveats = status == "READY_WITH_CAVEATS"
    is_env_prereq = role == "environment_prereqs"

    allowed: List[str] = []
    blocked: List[ActionConstraint] = []
    approval: List[ActionConstraint] = []

    # -- deploy --
    if is_blocked:
        blocked.append(
            ActionConstraint(ACTION_DEPLOY, REASON_TRUST_BLOCKED, _EVIDENCE_TRUST)
        )
    elif has_caveats:
        approval.append(
            ActionConstraint(ACTION_DEPLOY, REASON_TRUST_CAVEATS, _EVIDENCE_TRUST)
        )
    else:
        allowed.append(ACTION_DEPLOY)

    # -- dry_run -- diagnostic; blocked packages still need human ack to dry-run
    if is_blocked:
        approval.append(
            ActionConstraint(ACTION_DRY_RUN, REASON_TRUST_BLOCKED, _EVIDENCE_TRUST)
        )
    else:
        allowed.append(ACTION_DRY_RUN)

    # -- modify_payload -- only meaningful for environment-prereq packages
    if is_env_prereq and has_dba_placeholders:
        approval.append(
            ActionConstraint(
                ACTION_MODIFY_PAYLOAD, REASON_DBA_REVIEW_REQUIRED, _EVIDENCE_DBA
            )
        )
    else:
        blocked.append(
            ActionConstraint(
                ACTION_MODIFY_PAYLOAD,
                REASON_NOT_ENVIRONMENT_PREREQ
                if not is_env_prereq
                else REASON_ALWAYS_SAFE,
                _EVIDENCE_TRUST if not is_env_prereq else "",
            )
        )

    # -- repackage -- only sensible for env-prereq packages after DBA edits
    if is_env_prereq:
        allowed.append(ACTION_REPACKAGE)
    else:
        blocked.append(
            ActionConstraint(
                ACTION_REPACKAGE, REASON_NOT_ENVIRONMENT_PREREQ, _EVIDENCE_TRUST
            )
        )

    # -- always-safe actions --
    allowed.append(ACTION_VERIFY_INTEGRITY)
    allowed.append(ACTION_ROLLBACK)
    allowed.append(ACTION_FORWARD_TO_HUMAN)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return ActionsReport(
        evaluated_at=now,
        allowed_actions=allowed,
        blocked_actions=blocked,
        requires_human_approval=approval,
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_actions_result(pkg_dir: str, report: ActionsReport) -> str:
    """Write the canonical actions JSON to ``pkg_dir`` and return its path."""
    path = os.path.join(pkg_dir, "context", ACTIONS_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_actions_result(pkg_dir: str) -> Optional[dict]:
    """Load the canonical actions dict from ``pkg_dir`` or return None."""
    path = os.path.join(pkg_dir, "context", ACTIONS_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
