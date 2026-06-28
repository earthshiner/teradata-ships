"""
project_paths.py — Single source of truth for SHIPS path conventions.

Machine-managed state files live under ``<project>/.ships/`` so they
stay clearly separate from the user-edited ``config/`` and ``payload/``
surfaces. Hand-editing ``.ships/`` is unsupported; wiping it forces a
clean rebuild without risk to anything the developer authored.

The directory is added to the scaffolded ``.gitignore`` — each
developer / CI pipeline maintains its own build sequence and decision
history rather than fighting over a shared one.

Every consumer in the codebase should resolve these paths through
this module rather than constructing literals like
``os.path.join(project_dir, ".build_counter")`` inline.
"""

from __future__ import annotations

import os

SHIPS_STATE_DIRNAME = ".ships"

BUILD_COUNTER_FILENAME = ".build_counter"
DECISIONS_FILENAME = "ships.decisions.json"
WAVES_FILENAME = "_waves.txt"
CONTRACTS_BASELINE_FILENAME = "contracts.baseline.json"
CHANGESET_BASELINE_FILENAME = "changeset.baseline.json"

OBJECT_PLACEMENT_YAML_FILENAME = "object_placement.yaml"
CONFIG_DIRNAME = "config"

#: The canonical tokenisation config (issue #383). ``config/tokenise.conf`` is
#: the single source of truth for a project's tokenisation rules; every consumer
#: resolves it through :func:`tokenise_conf_path` (for file access) or
#: :data:`TOKENISE_CONF_RELPATH` (for a forward-slash catalogue reference)
#: rather than hard-coding the literal.
TOKENISE_CONF_FILENAME = "tokenise.conf"
TOKENISE_CONF_RELPATH = "config/tokenise.conf"


def ships_state_dir(project_dir: str) -> str:
    """Return ``<project_dir>/.ships`` without creating it."""
    return os.path.join(project_dir, SHIPS_STATE_DIRNAME)


def ensure_ships_state_dir(project_dir: str) -> str:
    """Return ``<project_dir>/.ships``, creating it if missing."""
    path = ships_state_dir(project_dir)
    os.makedirs(path, exist_ok=True)
    return path


def build_counter_path(project_dir: str) -> str:
    """Return the resolved path to ``.build_counter`` under ``.ships/``."""
    return os.path.join(ships_state_dir(project_dir), BUILD_COUNTER_FILENAME)


def decisions_json_path(project_dir: str) -> str:
    """Return the resolved path to ``ships.decisions.json`` under ``.ships/``."""
    return os.path.join(ships_state_dir(project_dir), DECISIONS_FILENAME)


def waves_txt_path(project_dir: str) -> str:
    """Return the resolved path to the project-root ``_waves.txt`` under ``.ships/``.

    NOTE: This is the analyse-stage output that lives at the project
    root. The per-phase ``payload/<phase>/_waves.txt`` files that
    travel inside built packages are unrelated and stay where they
    are.
    """
    return os.path.join(ships_state_dir(project_dir), WAVES_FILENAME)


def contracts_baseline_path(project_dir: str) -> str:
    """Return the resolved path to the object-contract baseline under ``.ships/``.

    The baseline (issue #171) is the last-captured snapshot of each object's
    contract (view columns, procedure parameters, table columns) that
    ``inspect`` compares the current source against to flag backward-
    incompatible changes.
    """
    return os.path.join(ships_state_dir(project_dir), CONTRACTS_BASELINE_FILENAME)


def changeset_baseline_path(project_dir: str) -> str:
    """Return the resolved path to the changeset content-hash baseline.

    Used by git-less change detection (issue #114): a snapshot of each
    payload file's content hash that the next run diffs against to find what
    changed when the project is not a git repository.
    """
    return os.path.join(ships_state_dir(project_dir), CHANGESET_BASELINE_FILENAME)


def tokenise_conf_path(project_dir: str) -> str:
    """Return the resolved path to the canonical ``config/tokenise.conf``.

    The single source of truth for a project's tokenisation rules (issue #383).
    Hand-edited config, so it lives under ``config/`` rather than ``.ships/``.
    """
    return os.path.join(project_dir, CONFIG_DIRNAME, TOKENISE_CONF_FILENAME)


def object_placement_yaml_path(project_dir: str) -> str:
    """Return the resolved path to the user-edited ``object_placement.yaml``.

    This is hand-edited config, NOT machine-managed state, so it
    stays under ``config/`` rather than moving to ``.ships/``.
    """
    return os.path.join(project_dir, CONFIG_DIRNAME, OBJECT_PLACEMENT_YAML_FILENAME)
