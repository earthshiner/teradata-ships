"""
policy.py — Canonical machine-readable agent policy.

Issue #151. Formalises the ``agent_policy`` block that previously lived
embedded in four context documents (ships.index.json, ships.context.json,
ships.manifest.json, ships.handoff.json) into a single canonical
``context/ships.policy.json`` with a published schema and per-condition
metadata.

This is the final P1 of the agent-native epic (#152). It composes on top
of #146 (trust), #143 (actions), #148 (required evidence), #149
(capabilities) — every stop condition and approval trigger here
references one of those canonical documents via ``evidence_ref`` so an
agent can detect the condition without out-of-band knowledge.

**Four do-not flags** — all True for v1; the policy is intentionally
conservative.

    do_not_infer_missing_tokens
    do_not_modify_payload
    do_not_deploy_if_blocked
    do_not_ignore_failed_integrity

**Closed stop_conditions vocabulary (v1)**

    trust_status_blocked
    package_integrity_failed
    signature_verification_failed
    target_environment_mismatch
    unresolved_tokens
    missing_required_approval
    missing_required_change_ref
    required_companion_package_not_deployed
    preflight_error

**Closed ask_for_human_approval_when vocabulary (v1)**

    trust_status_caveats
    missing_change_ref
    missing_signature
    tls_policy_not_satisfied

Each stop/approval entry carries ``{condition, detect_via, evidence_ref,
instruction}`` so an agent can detect the condition and act on it from
the policy doc alone.
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

POLICY_SCHEMA_VERSION = "1.0"

POLICY_RESULT_FILENAME = "ships.policy.json"
POLICY_RESULT_REF = f"context/{POLICY_RESULT_FILENAME}"


# ---------------------------------------------------------------
# Stop-condition vocabulary
# ---------------------------------------------------------------

STOP_TRUST_BLOCKED = "trust_status_blocked"
STOP_INTEGRITY_FAILED = "package_integrity_failed"
STOP_SIGNATURE_FAILED = "signature_verification_failed"
STOP_ENV_MISMATCH = "target_environment_mismatch"
STOP_UNRESOLVED_TOKENS = "unresolved_tokens"
STOP_MISSING_APPROVAL = "missing_required_approval"
STOP_MISSING_CHANGE_REF = "missing_required_change_ref"
STOP_REQUIRED_COMPANION = "required_companion_package_not_deployed"
STOP_PREFLIGHT_ERROR = "preflight_error"

ALL_STOP_CONDITIONS = (
    STOP_TRUST_BLOCKED,
    STOP_INTEGRITY_FAILED,
    STOP_SIGNATURE_FAILED,
    STOP_ENV_MISMATCH,
    STOP_UNRESOLVED_TOKENS,
    STOP_MISSING_APPROVAL,
    STOP_MISSING_CHANGE_REF,
    STOP_REQUIRED_COMPANION,
    STOP_PREFLIGHT_ERROR,
)


# ---------------------------------------------------------------
# Approval-trigger vocabulary
# ---------------------------------------------------------------

APPROVE_TRUST_CAVEATS = "trust_status_caveats"
APPROVE_MISSING_CHANGE_REF = "missing_change_ref"
APPROVE_MISSING_SIGNATURE = "missing_signature"
APPROVE_TLS_NOT_SATISFIED = "tls_policy_not_satisfied"

ALL_APPROVAL_TRIGGERS = (
    APPROVE_TRUST_CAVEATS,
    APPROVE_MISSING_CHANGE_REF,
    APPROVE_MISSING_SIGNATURE,
    APPROVE_TLS_NOT_SATISFIED,
)


# ---------------------------------------------------------------
# Static per-condition catalogue
#
# Each condition has a fixed (detect_via, evidence_ref, instruction)
# triple — the policy author defines these once; the evaluator decides
# which entries get emitted for a given package.
# ---------------------------------------------------------------

_STOP_CATALOGUE: Dict[str, Dict[str, str]] = {
    STOP_TRUST_BLOCKED: {
        "detect_via": "ships.trust.json.status == 'BLOCKED'",
        "evidence_ref": "context/ships.trust.json",
        "instruction": "Stop. Return the trust result and the blocking_signals list. Do not deploy.",
    },
    STOP_INTEGRITY_FAILED: {
        "detect_via": "Recomputed SHA-256 of payload tree does not match ships.integrity.json.package_hash.",
        "evidence_ref": "context/ships.integrity.json",
        "instruction": "Stop. Return the integrity manifest and the divergent file list. Do not deploy a tampered package.",
    },
    STOP_SIGNATURE_FAILED: {
        "detect_via": "ships.integrity.json signature verification fails against ships_public_key.",
        "evidence_ref": "context/ships.integrity.json",
        "instruction": "Stop. The package's signature did not verify. Return the integrity manifest. Do not deploy.",
    },
    STOP_ENV_MISMATCH: {
        "detect_via": "Operator-supplied target environment does not match ships.build.json.target_env.",
        "evidence_ref": "context/ships.build.json",
        "instruction": "Stop. Return the target_env from the build manifest and the operator-supplied environment. Do not deploy to the wrong environment.",
    },
    STOP_UNRESOLVED_TOKENS: {
        "detect_via": "Any '{{TOKEN}}' literal remains in the payload tree at deploy time.",
        "evidence_ref": "context/stages/inspect.result.json",
        "instruction": "Stop. Return the inspect result; identify the unresolved tokens. Do not deploy DDL with literal token markers.",
    },
    STOP_MISSING_APPROVAL: {
        "detect_via": "governance.require_approvals > 1 and recorded approvals < required count.",
        "evidence_ref": "context/ships.manifest.json",
        "instruction": "Stop. The package requires multi-approver sign-off that has not been recorded. Do not deploy.",
    },
    STOP_MISSING_CHANGE_REF: {
        "detect_via": "governance.require_change_ref == true and change_ref is unset.",
        "evidence_ref": "context/ships.manifest.json",
        "instruction": "Stop. Change-management requires a ticket reference. Do not deploy without one.",
    },
    STOP_REQUIRED_COMPANION: {
        "detect_via": "ships.manifest.json.dependency_contract.requires lists a companion package not yet deployed to the target.",
        "evidence_ref": "context/ships.manifest.json",
        "instruction": "Stop. Deploy the required companion package(s) first. Do not deploy this package out of order.",
    },
    STOP_PREFLIGHT_ERROR: {
        "detect_via": "Deployer preflight stage exits with a non-zero error code.",
        "evidence_ref": "context/stages/process.result.json",
        "instruction": "Stop. Return the preflight error output. Do not proceed past a failed preflight.",
    },
}

_APPROVAL_CATALOGUE: Dict[str, Dict[str, str]] = {
    APPROVE_TRUST_CAVEATS: {
        "detect_via": "ships.trust.json.status == 'READY_WITH_CAVEATS'.",
        "evidence_ref": "context/ships.trust.json",
        "instruction": "Pause and request human approval. Surface the warning_signals list so the operator can decide.",
    },
    APPROVE_MISSING_CHANGE_REF: {
        "detect_via": "governance.require_change_ref == true and change_ref is unset.",
        "evidence_ref": "context/ships.manifest.json",
        "instruction": "Pause and ask the operator for the change reference; record it before proceeding.",
    },
    APPROVE_MISSING_SIGNATURE: {
        "detect_via": "governance.require_signature or require_asymmetric_signature == true and signature is missing or invalid.",
        "evidence_ref": "context/ships.integrity.json",
        "instruction": "Pause. The package's signature is required but missing or invalid. Surface the integrity manifest before proceeding.",
    },
    APPROVE_TLS_NOT_SATISFIED: {
        "detect_via": "governance.require_tls == true and the connection is not TLS-protected.",
        "evidence_ref": "context/ships.manifest.json",
        "instruction": "Pause. The package requires TLS but the connection is not TLS-protected. Switch to TLS or ask the operator before proceeding.",
    },
}


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class PolicyEntry:
    """One stop condition or approval trigger plus its metadata."""

    condition: str
    detect_via: str
    evidence_ref: str
    instruction: str

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "detect_via": self.detect_via,
            "evidence_ref": self.evidence_ref,
            "instruction": self.instruction,
        }


@dataclass
class AgentPolicy:
    """Aggregate agent policy for a package."""

    evaluated_at: str
    trust_status_at_build: str = "UNKNOWN"
    stop_conditions: List[PolicyEntry] = field(default_factory=list)
    ask_for_human_approval_when: List[PolicyEntry] = field(default_factory=list)
    schema_version: str = POLICY_SCHEMA_VERSION

    # do_not_* flags are intentionally fixed for v1 — they bind the agent
    # to SHIPS's safety contract regardless of the package's governance
    # settings.
    do_not_infer_missing_tokens: bool = True
    do_not_modify_payload: bool = True
    do_not_deploy_if_blocked: bool = True
    do_not_ignore_failed_integrity: bool = True

    purpose: str = (
        "Bound downstream agent behaviour and prevent unsafe inference or "
        "bypass of SHIPS controls."
    )
    instruction: str = (
        "When any stop_conditions[*].condition is detected, follow that "
        "entry's instruction; do not proceed via inference or by bypassing "
        "SHIPS controls. When any ask_for_human_approval_when[*].condition "
        "is detected, follow that entry's instruction and resume only after "
        "explicit human approval."
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "purpose": self.purpose,
            "trust_status_at_build": self.trust_status_at_build,
            "do_not_infer_missing_tokens": self.do_not_infer_missing_tokens,
            "do_not_modify_payload": self.do_not_modify_payload,
            "do_not_deploy_if_blocked": self.do_not_deploy_if_blocked,
            "do_not_ignore_failed_integrity": self.do_not_ignore_failed_integrity,
            "stop_conditions": [e.to_dict() for e in self.stop_conditions],
            "ask_for_human_approval_when": [
                e.to_dict() for e in self.ask_for_human_approval_when
            ],
            "instruction": self.instruction,
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def _make_entry(catalogue: Dict[str, Dict[str, str]], condition: str) -> PolicyEntry:
    cat = catalogue[condition]
    return PolicyEntry(
        condition=condition,
        detect_via=cat["detect_via"],
        evidence_ref=cat["evidence_ref"],
        instruction=cat["instruction"],
    )


def compute_agent_policy(
    *,
    trust: Dict[str, Any],
    governance: Dict[str, Any],
) -> AgentPolicy:
    """
    Derive the agent policy for a package.

    Stop conditions are always emitted — they represent SHIPS's core
    safety contract and apply to every package. Approval triggers are
    governance-dependent: a package that doesn't require a change ref
    doesn't need ``missing_change_ref`` in its approval list.

    Args:
        trust:      The canonical trust document (``context/ships.trust.json``).
        governance: The governance dict from the build manifest.

    Returns:
        Populated ``AgentPolicy``.
    """
    trust_status = str(trust.get("status", "")).upper() or "UNKNOWN"

    # Every package gets the full stop-condition list — these are
    # invariants of the SHIPS safety contract, not package-specific.
    stops = [_make_entry(_STOP_CATALOGUE, c) for c in ALL_STOP_CONDITIONS]

    approvals: List[PolicyEntry] = []
    if trust_status == "READY_WITH_CAVEATS":
        approvals.append(_make_entry(_APPROVAL_CATALOGUE, APPROVE_TRUST_CAVEATS))
    if bool(governance.get("require_change_ref")):
        approvals.append(_make_entry(_APPROVAL_CATALOGUE, APPROVE_MISSING_CHANGE_REF))
    if bool(governance.get("require_signature")) or bool(
        governance.get("require_asymmetric_signature")
    ):
        approvals.append(_make_entry(_APPROVAL_CATALOGUE, APPROVE_MISSING_SIGNATURE))
    if bool(governance.get("require_tls")):
        approvals.append(_make_entry(_APPROVAL_CATALOGUE, APPROVE_TLS_NOT_SATISFIED))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return AgentPolicy(
        evaluated_at=now,
        trust_status_at_build=trust_status,
        stop_conditions=stops,
        ask_for_human_approval_when=approvals,
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_policy_result(pkg_dir: str, policy: AgentPolicy) -> str:
    """Write the canonical policy JSON to ``pkg_dir`` and return its path."""
    path = os.path.join(pkg_dir, "context", POLICY_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_policy_result(pkg_dir: str) -> Optional[dict]:
    """Load the canonical policy dict from ``pkg_dir`` or return None."""
    path = os.path.join(pkg_dir, "context", POLICY_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
