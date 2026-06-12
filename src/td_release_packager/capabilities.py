"""
capabilities.py — Canonical machine-readable package capability flags.

Issue #149. Tells a downstream agent which operations the embedded
deployer supports and which governance requirements the package
enforces, without having to parse ``deploy.py --help`` or re-derive
flags from the manifest.

``context/ships.capabilities.json`` is the single source of truth;
``ships.manifest.json``, ``ships.handoff.json``, ``ships.context.json``,
and ``ships.index.json`` each carry a top-level ``capabilities_ref``
string pointing at this file (same pattern as ``trust_ref`` /
``actions_ref``).

**Flag vocabulary (closed for v1)**

Deployer capabilities — True for the embedded SHIPS deployer:

    dry_run_supported            ``deploy.py --dry-run``
    rollback_supported           Rollback script generated alongside deploy
    resume_supported             Resume from ``logs/.deploy_manifest.json``
    continue_on_error_supported  ``--continue-on-error``
    parallel_waves_supported     ``--wave-parallel``
    drift_detection_supported    Post-deploy DBC drift check

Package policy — derived from the manifest's ``governance`` block:

    approval_required            ``governance.require_approvals > 1``
    change_ref_required          ``governance.require_change_ref``
    integrity_check_required     ``governance.require_signature`` OR
                                 ``require_asymmetric_signature``
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

CAPABILITIES_SCHEMA_VERSION = "1.0"

CAPABILITIES_RESULT_FILENAME = "ships.capabilities.json"
CAPABILITIES_RESULT_REF = f"context/{CAPABILITIES_RESULT_FILENAME}"


# ---------------------------------------------------------------
# Flag names — re-exported as module constants so callers do not
# duplicate the string literals.
# ---------------------------------------------------------------

CAP_DRY_RUN_SUPPORTED = "dry_run_supported"
CAP_ROLLBACK_SUPPORTED = "rollback_supported"
CAP_RESUME_SUPPORTED = "resume_supported"
CAP_CONTINUE_ON_ERROR_SUPPORTED = "continue_on_error_supported"
CAP_PARALLEL_WAVES_SUPPORTED = "parallel_waves_supported"
CAP_DRIFT_DETECTION_SUPPORTED = "drift_detection_supported"

CAP_APPROVAL_REQUIRED = "approval_required"
CAP_CHANGE_REF_REQUIRED = "change_ref_required"
CAP_INTEGRITY_CHECK_REQUIRED = "integrity_check_required"

SUPPORTED_FLAGS = (
    CAP_DRY_RUN_SUPPORTED,
    CAP_ROLLBACK_SUPPORTED,
    CAP_RESUME_SUPPORTED,
    CAP_CONTINUE_ON_ERROR_SUPPORTED,
    CAP_PARALLEL_WAVES_SUPPORTED,
    CAP_DRIFT_DETECTION_SUPPORTED,
)

REQUIRED_FLAGS = (
    CAP_APPROVAL_REQUIRED,
    CAP_CHANGE_REF_REQUIRED,
    CAP_INTEGRITY_CHECK_REQUIRED,
)

ALL_CAPABILITY_FLAGS = SUPPORTED_FLAGS + REQUIRED_FLAGS


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class CapabilitiesReport:
    """Aggregate capability flags for a package."""

    evaluated_at: str
    dry_run_supported: bool = True
    rollback_supported: bool = True
    resume_supported: bool = True
    continue_on_error_supported: bool = True
    parallel_waves_supported: bool = True
    drift_detection_supported: bool = True
    approval_required: bool = False
    change_ref_required: bool = False
    integrity_check_required: bool = False
    schema_version: str = CAPABILITIES_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            CAP_DRY_RUN_SUPPORTED: self.dry_run_supported,
            CAP_ROLLBACK_SUPPORTED: self.rollback_supported,
            CAP_RESUME_SUPPORTED: self.resume_supported,
            CAP_CONTINUE_ON_ERROR_SUPPORTED: self.continue_on_error_supported,
            CAP_PARALLEL_WAVES_SUPPORTED: self.parallel_waves_supported,
            CAP_DRIFT_DETECTION_SUPPORTED: self.drift_detection_supported,
            CAP_APPROVAL_REQUIRED: self.approval_required,
            CAP_CHANGE_REF_REQUIRED: self.change_ref_required,
            CAP_INTEGRITY_CHECK_REQUIRED: self.integrity_check_required,
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def compute_capabilities_report(governance: Dict[str, Any]) -> CapabilitiesReport:
    """
    Derive capability flags for a package.

    Args:
        governance: The ``governance`` dict already built for the agent
                    context docs (see ``context_artifacts._governance``).
                    May be partial — missing keys default to "not required".

    Returns:
        Populated ``CapabilitiesReport``.
    """
    approval_required = int(governance.get("require_approvals") or 1) > 1
    change_ref_required = bool(governance.get("require_change_ref"))
    integrity_check_required = bool(
        governance.get("require_signature")
        or governance.get("require_asymmetric_signature")
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return CapabilitiesReport(
        evaluated_at=now,
        approval_required=approval_required,
        change_ref_required=change_ref_required,
        integrity_check_required=integrity_check_required,
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_capabilities_result(pkg_dir: str, report: CapabilitiesReport) -> str:
    """Write the canonical capabilities JSON to ``pkg_dir`` and return its path."""
    path = os.path.join(pkg_dir, "context", CAPABILITIES_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_capabilities_result(pkg_dir: str) -> Optional[dict]:
    """Load the canonical capabilities dict from ``pkg_dir`` or return None."""
    path = os.path.join(pkg_dir, "context", CAPABILITIES_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
