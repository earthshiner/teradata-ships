"""
project_policy.py — Project-side agent policy (#275, part of #268).

Companion to the package-side ``context/ships.policy.json`` from #151.
Tells an agent operating on a SHIPS *project root* (pre-package) which
state changes are safety stops, which need human approval, and which
do-not behaviours apply at all times.

Same shape as the package-side policy — same per-condition
``{condition, detect_via, evidence_ref, instruction}`` quadruple — so
an agent that already understands the package policy can read this
file with no new vocabulary.

**Three do-not flags** — all True for v1.

    do_not_modify_source_without_dry_run
    do_not_skip_inspect_before_package
    do_not_proceed_past_inspect_errors

**Closed stop_conditions vocabulary (v1)**

    ships_yaml_missing
    tokenise_config_invalid       (only emitted when the file exists)
    inspect_errors_present
    unknown_lifecycle_state       (defensive — should not fire)

**Closed ask_for_human_approval_when vocabulary (v1)**

    tokenise_without_dry_run
    package_empty_payload         (mirrors the same gate from
                                   ``project_actions.py``)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from td_release_packager.project_index import compute_project_index


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

PROJECT_POLICY_SCHEMA_VERSION = "1.0"
PROJECT_POLICY_FILENAME = "ships.project_policy.json"


# ---------------------------------------------------------------
# Stop-condition vocabulary
# ---------------------------------------------------------------

PROJECT_STOP_SHIPS_YAML_MISSING = "ships_yaml_missing"
PROJECT_STOP_TOKENISE_CONFIG_INVALID = "tokenise_config_invalid"
PROJECT_STOP_INSPECT_ERRORS = "inspect_errors_present"
PROJECT_STOP_UNKNOWN_STATE = "unknown_lifecycle_state"

ALL_PROJECT_STOP_CONDITIONS = (
    PROJECT_STOP_SHIPS_YAML_MISSING,
    PROJECT_STOP_TOKENISE_CONFIG_INVALID,
    PROJECT_STOP_INSPECT_ERRORS,
    PROJECT_STOP_UNKNOWN_STATE,
)


# ---------------------------------------------------------------
# Approval-trigger vocabulary
# ---------------------------------------------------------------

PROJECT_APPROVE_TOKENISE_NO_DRY_RUN = "tokenise_without_dry_run"
PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD = "package_empty_payload"

ALL_PROJECT_APPROVAL_TRIGGERS = (
    PROJECT_APPROVE_TOKENISE_NO_DRY_RUN,
    PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD,
)


# ---------------------------------------------------------------
# Per-condition catalogue
# ---------------------------------------------------------------

_STOP_CATALOGUE: Dict[str, Dict[str, str]] = {
    PROJECT_STOP_SHIPS_YAML_MISSING: {
        "detect_via": "<project_dir>/ships.yaml is absent.",
        "evidence_ref": "ships.project.json",
        "instruction": (
            "Stop. The directory is not a SHIPS project. "
            "Run scaffold first or change directories."
        ),
    },
    PROJECT_STOP_TOKENISE_CONFIG_INVALID: {
        "detect_via": (
            "<project_dir>/config/tokenise.conf is present but cannot be "
            "parsed (no valid rules)."
        ),
        "evidence_ref": "config/tokenise.conf",
        "instruction": (
            "Stop. Fix the malformed rules before harvest or migrate-source "
            "is allowed to run; harvest auto-applies the config."
        ),
    },
    PROJECT_STOP_INSPECT_ERRORS: {
        "detect_via": (
            "ships.decisions.json shows the latest inspect stage status == 'error'."
        ),
        "evidence_ref": "ships.decisions.json",
        "instruction": (
            "Stop. Resolve all inspect ERROR findings before harvesting more "
            "or packaging — these are gating issues, not warnings."
        ),
    },
    PROJECT_STOP_UNKNOWN_STATE: {
        "detect_via": (
            "ships.project.json.lifecycle_state is outside the closed v1 vocabulary."
        ),
        "evidence_ref": "ships.project.json",
        "instruction": (
            "Stop. The project index reports a lifecycle state SHIPS does "
            "not recognise — refresh tooling or ask the operator before "
            "taking any project-mutating action."
        ),
    },
}

_APPROVAL_CATALOGUE: Dict[str, Dict[str, str]] = {
    PROJECT_APPROVE_TOKENISE_NO_DRY_RUN: {
        "detect_via": (
            "About to call migrate-source without --dry-run on a non-empty source tree."
        ),
        "evidence_ref": "config/tokenise.conf",
        "instruction": (
            "Pause. Run migrate-source --dry-run first; surface the diff "
            "to the operator before applying."
        ),
    },
    PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD: {
        "detect_via": (
            "ships.project.json.lifecycle_state == 'scaffolded' and "
            "ships.project_actions.json.discovery_flags.source_payload_present "
            "== false."
        ),
        "evidence_ref": "ships.project.json",
        "instruction": (
            "Pause. Confirm with the operator that packaging an unharvested "
            "project is intended."
        ),
    },
}


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class ProjectPolicyEntry:
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
class ProjectAgentPolicy:
    """Aggregate project-side agent policy."""

    evaluated_at: str
    project_state: str
    stop_conditions: List[ProjectPolicyEntry] = field(default_factory=list)
    ask_for_human_approval_when: List[ProjectPolicyEntry] = field(default_factory=list)
    schema_version: str = PROJECT_POLICY_SCHEMA_VERSION

    # do_not_* flags are fixed for v1 — they bind the agent to SHIPS's
    # source-tree safety contract regardless of project state.
    do_not_modify_source_without_dry_run: bool = True
    do_not_skip_inspect_before_package: bool = True
    do_not_proceed_past_inspect_errors: bool = True

    purpose: str = (
        "Bound downstream agent behaviour on the project (pre-package) side "
        "and prevent unsafe inference or by-passing of SHIPS source-tree "
        "controls."
    )
    instruction: str = (
        "When any stop_conditions[*].condition is detected, follow that "
        "entry's instruction; do not proceed via inference or by bypassing "
        "SHIPS source-tree controls. When any "
        "ask_for_human_approval_when[*].condition is detected, follow that "
        "entry's instruction and resume only after explicit human approval."
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "purpose": self.purpose,
            "project_state": self.project_state,
            "do_not_modify_source_without_dry_run": self.do_not_modify_source_without_dry_run,
            "do_not_skip_inspect_before_package": self.do_not_skip_inspect_before_package,
            "do_not_proceed_past_inspect_errors": self.do_not_proceed_past_inspect_errors,
            "stop_conditions": [e.to_dict() for e in self.stop_conditions],
            "ask_for_human_approval_when": [
                e.to_dict() for e in self.ask_for_human_approval_when
            ],
            "instruction": self.instruction,
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def _make_entry(catalogue: Dict[str, Dict[str, str]], cond: str) -> ProjectPolicyEntry:
    cat = catalogue[cond]
    return ProjectPolicyEntry(
        condition=cond,
        detect_via=cat["detect_via"],
        evidence_ref=cat["evidence_ref"],
        instruction=cat["instruction"],
    )


def compute_project_policy(project_dir: str) -> ProjectAgentPolicy:
    """
    Derive the project-side agent policy for ``project_dir``.

    Stop conditions are emitted unconditionally for the SHIPS-safety
    invariants (ships_yaml_missing, inspect_errors_present,
    unknown_lifecycle_state). ``tokenise_config_invalid`` is emitted
    only when ``config/tokenise.conf`` exists — otherwise the agent
    has nothing to validate.

    Approval triggers are always emitted: they're guidance, not
    state-dependent.
    """
    index = compute_project_index(project_dir)

    stops: List[ProjectPolicyEntry] = [
        _make_entry(_STOP_CATALOGUE, PROJECT_STOP_SHIPS_YAML_MISSING),
        _make_entry(_STOP_CATALOGUE, PROJECT_STOP_INSPECT_ERRORS),
        _make_entry(_STOP_CATALOGUE, PROJECT_STOP_UNKNOWN_STATE),
    ]
    if os.path.isfile(os.path.join(project_dir, "config", "tokenise.conf")):
        stops.insert(
            -1,  # before the defensive unknown-state entry
            _make_entry(_STOP_CATALOGUE, PROJECT_STOP_TOKENISE_CONFIG_INVALID),
        )

    approvals: List[ProjectPolicyEntry] = [
        _make_entry(_APPROVAL_CATALOGUE, PROJECT_APPROVE_TOKENISE_NO_DRY_RUN),
        _make_entry(_APPROVAL_CATALOGUE, PROJECT_APPROVE_PACKAGE_EMPTY_PAYLOAD),
    ]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return ProjectAgentPolicy(
        evaluated_at=now,
        project_state=index.lifecycle_state,
        stop_conditions=stops,
        ask_for_human_approval_when=approvals,
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_project_policy(project_dir: str) -> str:
    """Compute and write ``ships.project_policy.json`` to ``project_dir``."""
    policy = compute_project_policy(project_dir)
    path = os.path.join(project_dir, PROJECT_POLICY_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_project_policy(project_dir: str) -> Optional[dict]:
    """Load ``ships.project_policy.json`` if present."""
    path = os.path.join(project_dir, PROJECT_POLICY_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
