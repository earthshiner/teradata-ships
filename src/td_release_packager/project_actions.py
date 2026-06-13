"""
project_actions.py — Project-side action vocabulary (#273, part of #268).

Companion to the package-side ``context/ships.actions.json`` from #143.
Tells an agent landing on a SHIPS *project root* which CLI actions are
safe to take autonomously, which are blocked, and which need human
approval first.

The list is derived from:

  - The project's current lifecycle state (from ``ships.project.json``)
  - The presence of source-side artefacts (``payload/``,
    ``config/tokenise.conf``, ``config/env/*.conf``)

**Action vocabulary (closed set v1)** — mirrors the SHIPS pre-package
pipeline:

    scaffold           Create or repair the project skeleton.
    harvest            Populate ``payload/`` from raw DDL.
    inspect            Lint, validate grants, trust signals.
    analyse            Dependency graph and wave order.
    scan               List tokens in source.
    tokenise           Apply ``config/tokenise.conf`` to source
                       (``migrate-source``). REWRITES SOURCE FILES —
                       always ``requires_human_approval``.
    import_legacy      Convert a pre-SHIPS sed/source tree into SHIPS form.
    decompose_names    Generate cascade-form ``.conf`` from literal
                       names.
    package            Build the deployable archive.

Refreshed by every project-mutating CLI command via the same
``_stage_recording`` hook that drives ``ships.project.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from td_release_packager.project_index import (
    STATE_ANALYSED,
    STATE_HARVESTED,
    STATE_INSPECTED,
    STATE_PACKAGED,
    STATE_SCAFFOLDED,
    compute_project_index,
)


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

PROJECT_ACTIONS_SCHEMA_VERSION = "1.0"
PROJECT_ACTIONS_FILENAME = "ships.project_actions.json"


# ---------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------

ACTION_SCAFFOLD = "scaffold"
ACTION_HARVEST = "harvest"
ACTION_INSPECT = "inspect"
ACTION_ANALYSE = "analyse"
ACTION_SCAN = "scan"
ACTION_TOKENISE = "tokenise"
ACTION_IMPORT_LEGACY = "import_legacy"
ACTION_DECOMPOSE_NAMES = "decompose_names"
ACTION_PACKAGE = "package"

ALL_PROJECT_ACTIONS = (
    ACTION_SCAFFOLD,
    ACTION_HARVEST,
    ACTION_INSPECT,
    ACTION_ANALYSE,
    ACTION_SCAN,
    ACTION_TOKENISE,
    ACTION_IMPORT_LEGACY,
    ACTION_DECOMPOSE_NAMES,
    ACTION_PACKAGE,
)


# ---------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------

REASON_REWRITES_SOURCE_FILES = "rewrites_source_files"
REASON_PROJECT_NOT_HARVESTED = "project_not_yet_harvested"


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class ProjectActionConstraint:
    """One action that is blocked or requires approval, plus the why."""

    action: str
    reason: str
    evidence_ref: str = ""
    instruction: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "evidence_ref": self.evidence_ref,
            "instruction": self.instruction,
        }


@dataclass
class ProjectActionsReport:
    """Aggregate project-side action controls."""

    evaluated_at: str
    project_state: str
    allowed_actions: List[str] = field(default_factory=list)
    blocked_actions: List[ProjectActionConstraint] = field(default_factory=list)
    requires_human_approval: List[ProjectActionConstraint] = field(default_factory=list)
    discovery_flags: Dict[str, bool] = field(default_factory=dict)
    schema_version: str = PROJECT_ACTIONS_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "project_state": self.project_state,
            "discovery_flags": dict(self.discovery_flags),
            "allowed_actions": list(self.allowed_actions),
            "blocked_actions": [c.to_dict() for c in self.blocked_actions],
            "requires_human_approval": [
                c.to_dict() for c in self.requires_human_approval
            ],
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def _discovery_flags(project_dir: str) -> Dict[str, bool]:
    """Lightweight presence checks for downstream composition."""
    flags = {
        "tokenise_config_present": os.path.isfile(
            os.path.join(project_dir, "config", "tokenise.conf")
        ),
        "env_configs_present": False,
        "source_payload_present": False,
    }
    env_dir = os.path.join(project_dir, "config", "env")
    if os.path.isdir(env_dir):
        flags["env_configs_present"] = any(
            f.endswith(".conf") for f in os.listdir(env_dir)
        )
    payload_dir = os.path.join(project_dir, "payload")
    if os.path.isdir(payload_dir):
        for _root, _dirs, files in os.walk(payload_dir):
            if any(not f.startswith(".") for f in files):
                flags["source_payload_present"] = True
                break
    return flags


def _build_allowed_and_blocked(
    state: str,
    flags: Dict[str, bool],
) -> tuple[List[str], List[ProjectActionConstraint], List[ProjectActionConstraint]]:
    allowed: List[str] = []
    blocked: List[ProjectActionConstraint] = []
    approval: List[ProjectActionConstraint] = []

    # Always-allowed read/structure actions.
    allowed.extend(
        [
            ACTION_SCAFFOLD,
            ACTION_HARVEST,
            ACTION_INSPECT,
            ACTION_ANALYSE,
            ACTION_SCAN,
            ACTION_IMPORT_LEGACY,
            ACTION_DECOMPOSE_NAMES,
        ]
    )

    # tokenise rewrites the source tree. Always approval-gated.
    approval.append(
        ProjectActionConstraint(
            action=ACTION_TOKENISE,
            reason=REASON_REWRITES_SOURCE_FILES,
            evidence_ref="config/tokenise.conf",
            instruction=(
                "Run `migrate-source --dry-run` first to preview the rewrite. "
                "Apply only after the operator reviews the diff."
            ),
        )
    )

    # Packaging only makes sense once there is something to package.
    # In the SCAFFOLDED state the payload tree is empty, so we gate on
    # approval (the agent may still want to package a fresh project for
    # testing, but it should pause first).
    if state == STATE_SCAFFOLDED and not flags.get("source_payload_present"):
        approval.append(
            ProjectActionConstraint(
                action=ACTION_PACKAGE,
                reason=REASON_PROJECT_NOT_HARVESTED,
                evidence_ref="ships.project.json",
                instruction=(
                    "The project has not been harvested yet — payload/ is empty. "
                    "Run `harvest` first, or confirm with the operator that "
                    "packaging an empty project is intended."
                ),
            )
        )
    else:
        allowed.append(ACTION_PACKAGE)

    return allowed, blocked, approval


def compute_project_actions(project_dir: str) -> ProjectActionsReport:
    """
    Derive the project-side action controls for ``project_dir``.

    Reads lifecycle state from ``ships.project.json`` (or recomputes
    it on the fly if absent) and combines with filesystem discovery
    flags. Does not mutate state.
    """
    index = compute_project_index(project_dir)
    flags = _discovery_flags(project_dir)
    allowed, blocked, approval = _build_allowed_and_blocked(
        index.lifecycle_state, flags
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return ProjectActionsReport(
        evaluated_at=now,
        project_state=index.lifecycle_state,
        allowed_actions=allowed,
        blocked_actions=blocked,
        requires_human_approval=approval,
        discovery_flags=flags,
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_project_actions(project_dir: str) -> str:
    """Compute and write ``ships.project_actions.json`` to ``project_dir``."""
    report = compute_project_actions(project_dir)
    path = os.path.join(project_dir, PROJECT_ACTIONS_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_project_actions(project_dir: str) -> Optional[dict]:
    """Load ``ships.project_actions.json`` if present."""
    path = os.path.join(project_dir, PROJECT_ACTIONS_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
