"""
project_index.py — Agent-discoverable project context (#271, part of #268).

Companion to the package-side ``context/ships.index.json`` from #146.
This module produces ``<project_dir>/ships.project.json`` — a single,
read-first file an agent can open to learn the current lifecycle state
of a SHIPS project, the recommended next action, and pointers to every
evidence file in the project tree.

The file is refreshed on every project-mutating CLI command (scaffold,
harvest, inspect, analyse, package). The derivation is pure: state is
read from ``ships.decisions.json`` and the filesystem; nothing is
inferred.

**Lifecycle ladder (v1)**

    scaffolded  → harvest is the next step
    harvested   → inspect is the next step
    inspected   → analyse OR package is the next step
    analysed    → package is the next step
    packaged    → ship (deploy) is the next step

The ladder reflects the canonical SHIPS pipeline. Stages can be skipped
(e.g. you can package without running analyse), so the recommended
next action is a hint — never a hard requirement.

``actions_ref`` points at ``ships.project_actions.json`` (#273 — the
project-side action vocabulary). ``policy_ref`` points at
``ships.project_policy.json`` (#275 — the project-side agent policy).
Together with this index, those two files make up the project-side
agent contract under #268.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

PROJECT_INDEX_SCHEMA_VERSION = "1.0"
PROJECT_INDEX_FILENAME = "ships.project.json"


# ---------------------------------------------------------------
# Lifecycle vocabulary (closed for v1)
# ---------------------------------------------------------------

STATE_SCAFFOLDED = "scaffolded"
STATE_HARVESTED = "harvested"
STATE_INSPECTED = "inspected"
STATE_ANALYSED = "analysed"
STATE_PACKAGED = "packaged"

ALL_LIFECYCLE_STATES = (
    STATE_SCAFFOLDED,
    STATE_HARVESTED,
    STATE_INSPECTED,
    STATE_ANALYSED,
    STATE_PACKAGED,
)

# Ranking — used to derive the highest attained state from the
# decisions log. Higher number = later in the lifecycle.
_STATE_RANK = {
    STATE_SCAFFOLDED: 0,
    STATE_HARVESTED: 1,
    STATE_INSPECTED: 2,
    STATE_ANALYSED: 3,
    STATE_PACKAGED: 4,
}

# Which decisions-log stage name maps to which lifecycle state.
# ``ingest`` is the modern name for the harvest stage; ``process``
# is the meta-verb that runs multiple inner stages — it doesn't
# advance the lifecycle on its own.
_STAGE_TO_STATE = {
    "scaffold": STATE_SCAFFOLDED,
    "harvest": STATE_HARVESTED,
    "ingest": STATE_HARVESTED,
    "inspect": STATE_INSPECTED,
    "analyse": STATE_ANALYSED,
    "analyze": STATE_ANALYSED,
    "package": STATE_PACKAGED,
}

# Recommended next action per state. Strings are CLI invocations
# pre-filled with the project_dir at write time.
_NEXT_ACTIONS = {
    STATE_SCAFFOLDED: [
        "Harvest your raw DDL into payload/ — "
        "python -m td_release_packager harvest --source <ddl_dir> --project {project_dir}",
    ],
    STATE_HARVESTED: [
        "Inspect for token format, lint, and grant drift — "
        "python -m td_release_packager inspect --source {project_dir}",
    ],
    STATE_INSPECTED: [
        "Analyse dependencies and wave order — "
        "python -m td_release_packager analyze --source {project_dir} --graph {project_dir}/analysis",
        "Or package directly — "
        "python -m td_release_packager package --source {project_dir} --env DEV --name <pkg> --env-config config/env/DEV.conf",
    ],
    STATE_ANALYSED: [
        "Package the project for an environment — "
        "python -m td_release_packager package --source {project_dir} --env DEV --name <pkg> --env-config config/env/DEV.conf",
    ],
    STATE_PACKAGED: [
        "Ship — deploy the latest archive to a target system. "
        "Run `python -m database_package_deployer deploy <archive> --host <host> --user <user>`.",
    ],
}


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class ProjectIndex:
    """Aggregate project index for a SHIPS project."""

    schema_version: str
    evaluated_at: str
    project_name: str
    project_dir: str
    lifecycle_state: str
    next_recommended_actions: List[str] = field(default_factory=list)
    references: Dict[str, Any] = field(default_factory=dict)
    actions_ref: str = ""
    policy_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "project_name": self.project_name,
            "project_dir": self.project_dir,
            "lifecycle_state": self.lifecycle_state,
            "next_recommended_actions": list(self.next_recommended_actions),
            "references": dict(self.references),
            "actions_ref": self.actions_ref,
            "policy_ref": self.policy_ref,
        }


# ---------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------


def _read_project_name(project_dir: str) -> str:
    """Best-effort extraction of the project name from ships.yaml."""
    ships_yaml = os.path.join(project_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml):
        return os.path.basename(os.path.abspath(project_dir))
    try:
        with open(ships_yaml, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return os.path.basename(os.path.abspath(project_dir))


def _read_decisions(project_dir: str) -> dict:
    """Load ships.decisions.json or return an empty structure."""
    path = os.path.join(project_dir, "ships.decisions.json")
    if not os.path.isfile(path):
        return {"runs": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"runs": []}


def _highest_attained_state(decisions: dict, releases_present: bool) -> str:
    """Return the most advanced lifecycle state observed in the log."""
    rank = -1
    for run in decisions.get("runs", []):
        for stage in run.get("stages", []):
            name = stage.get("stage", "").lower()
            status = stage.get("status", "").lower()
            if status == "error":
                continue
            state = _STAGE_TO_STATE.get(name)
            if state is None:
                continue
            rank = max(rank, _STATE_RANK[state])

    # A built release on disk pins the project at PACKAGED even if the
    # decisions log was truncated or never written for that stage.
    if releases_present:
        rank = max(rank, _STATE_RANK[STATE_PACKAGED])

    if rank < 0:
        return STATE_SCAFFOLDED
    for state, r in _STATE_RANK.items():
        if r == rank:
            return state
    return STATE_SCAFFOLDED


def _collect_env_configs(project_dir: str) -> List[str]:
    env_dir = os.path.join(project_dir, "config", "env")
    if not os.path.isdir(env_dir):
        return []
    return sorted(
        os.path.relpath(p, project_dir)
        for p in glob.glob(os.path.join(env_dir, "*.conf"))
    )


def _latest_package(project_dir: str) -> Optional[str]:
    releases_dir = os.path.join(project_dir, "releases")
    if not os.path.isdir(releases_dir):
        return None
    zips = []
    for root, _dirs, files in os.walk(releases_dir):
        for f in files:
            if f.endswith(".zip"):
                zips.append(os.path.join(root, f))
    if not zips:
        return None
    latest = max(zips, key=os.path.getmtime)
    return os.path.relpath(latest, project_dir).replace(os.sep, "/")


def _references(project_dir: str) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}

    def _add_if_exists(key: str, rel_path: str) -> None:
        if os.path.exists(os.path.join(project_dir, rel_path)):
            refs[key] = rel_path

    _add_if_exists("ships_yaml", "ships.yaml")
    _add_if_exists("decisions_log", "ships.decisions.json")
    _add_if_exists("tokenise_config", "config/tokenise.conf")
    env_configs = _collect_env_configs(project_dir)
    if env_configs:
        refs["env_configs"] = env_configs
    latest = _latest_package(project_dir)
    if latest is not None:
        refs["latest_package"] = latest
    return refs


def compute_project_index(project_dir: str) -> ProjectIndex:
    """
    Derive the project index for ``project_dir``.

    Reads ``ships.yaml``, ``ships.decisions.json``, and the
    ``releases/`` directory; does not mutate state.
    """
    decisions = _read_decisions(project_dir)
    releases_present = _latest_package(project_dir) is not None
    state = _highest_attained_state(decisions, releases_present)
    refs = _references(project_dir)
    next_actions = [
        s.format(project_dir=project_dir) for s in _NEXT_ACTIONS.get(state, [])
    ]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    # ``actions_ref`` (#273) and ``policy_ref`` (#275) wire up the
    # project-side action vocabulary and agent policy respectively.
    # Together with this index they form the project-side agent
    # contract from #268.
    return ProjectIndex(
        schema_version=PROJECT_INDEX_SCHEMA_VERSION,
        evaluated_at=now,
        project_name=_read_project_name(project_dir),
        project_dir=os.path.abspath(project_dir),
        lifecycle_state=state,
        next_recommended_actions=next_actions,
        references=refs,
        actions_ref="ships.project_actions.json",
        policy_ref="ships.project_policy.json",
    )


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_project_index(project_dir: str) -> str:
    """Compute and write ``ships.project.json`` to ``project_dir``."""
    index = compute_project_index(project_dir)
    path = os.path.join(project_dir, PROJECT_INDEX_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_project_index(project_dir: str) -> Optional[dict]:
    """Load ``ships.project.json`` from ``project_dir`` if present."""
    path = os.path.join(project_dir, PROJECT_INDEX_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
