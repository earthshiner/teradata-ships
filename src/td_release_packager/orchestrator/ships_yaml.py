"""
ships_yaml.py — Schema, Layer 1 defaults, parser, and validator
for ``ships.yaml``.

``ships.yaml`` is the project meta-config — a single entry point at
the project root that declares the project, its environments,
pointers to the other config files (``inspect.conf``,
``object_placement.yaml``, ``token_map.conf``), and per-stage
``strict`` / ``on_error`` policies.

This module defines:

    LAYER_1_DEFAULTS    Hard-coded defaults — the lowest layer of the
                        five-layer cascade. Developer-mode-friendly.
    STAGES              Canonical stage names accepted in
                        ``stages.<name>`` blocks.
    VALID_ON_ERROR      The on_error vocabulary: ``halt`` or ``continue``.

    load(path)               Parse a ships.yaml file → raw dict.
    validate(data)           Schema-check a parsed dict → list of
                             ValidationError (empty == valid).
    apply_defaults(data)     Fill missing Layer 1 settings on a
                             validated dict; returns a new dict.
    generate_default(...)    Build a fresh ships.yaml dict suitable
                             for first-run scaffold of a new project.
    write_if_missing(...)    Persist a ships.yaml dict to disc only
                             if no file exists at the target path —
                             never overwrites an existing file.

Notes:
    - Layer 1 defaults are developer-mode-friendly
      (``strict: false``, ``on_error: continue``). Platform mode
      flips both via Layer 5 CLI flags or Layer 2 templates.
    - The token pattern ``{{TOKEN}}`` is built into SHIPS at Layer 1
      and is not user-overridable per the design's per-setting matrix.
    - ``validate()`` collects every problem before returning so the
      caller can surface a complete error list. It does not raise.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ---------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------

#: The seven canonical pipeline stages in deployment order.
STAGES: Tuple[str, ...] = (
    "scaffold",
    "harvest",
    "generate",
    "fix",
    "inspect",
    "analyse",
    "package",
    "ship",
)

#: Allowed values for ``stages.<name>.on_error``.
VALID_ON_ERROR: Tuple[str, ...] = ("halt", "continue")

#: The built-in token pattern. Per the design's per-setting matrix
#: this is Layer-1-only and not user-overridable. Captured here as
#: a sentinel so consumers needn't re-declare it.
TOKEN_PATTERN: str = "{{TOKEN}}"

#: Layer 1 defaults — the lowest layer of the cascade. Used when
#: no higher layer (template / project / env / CLI) specifies a value.
#: Developer-mode-friendly: lenient strictness, continue on error.
LAYER_1_DEFAULTS: Dict[str, Any] = {
    "config": {
        "inspect": "config/inspect.conf",
        "placement": "config/object_placement.yaml",
        "tokens": "config/token_map.conf",
    },
    "stages": {stage: {"strict": False, "on_error": "continue"} for stage in STAGES},
}


# ---------------------------------------------------------------
# Errors
# ---------------------------------------------------------------


class ShipsConfigError(Exception):
    """Raised by ``load()`` when the file is unreadable or unparseable."""


@dataclass(frozen=True)
class ValidationError:
    """
    A single schema violation.

    Attributes:
        path:     Dotted path into the config (e.g. ``stages.generate.on_error``).
        message:  Human-readable explanation.
    """

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


# ---------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------


def load(path: str) -> Dict[str, Any]:
    """
    Parse a ships.yaml file into a plain dict.

    Args:
        path: Filesystem path to the ships.yaml file.

    Returns:
        The parsed YAML as a dict (top-level mapping).

    Raises:
        ShipsConfigError: If the file is missing, unreadable, not a
            mapping at the top level, or invalid YAML.
    """
    if not os.path.exists(path):
        raise ShipsConfigError(f"ships.yaml not found at: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ShipsConfigError(f"ships.yaml is not valid YAML ({path}): {e}") from e
    except OSError as e:
        raise ShipsConfigError(f"ships.yaml unreadable ({path}): {e}") from e

    if data is None:
        # Empty file — treat as an empty mapping rather than None
        return {}

    if not isinstance(data, dict):
        raise ShipsConfigError(
            f"ships.yaml top-level must be a mapping, got "
            f"{type(data).__name__} ({path})"
        )

    return data


# ---------------------------------------------------------------
# Validation
# ---------------------------------------------------------------


def validate(data: Dict[str, Any]) -> List[ValidationError]:
    """
    Schema-check a ships.yaml dict.

    Collects every issue rather than failing on the first — callers
    typically want to print the full list to the user. An empty list
    means the document is valid.

    Args:
        data: A parsed ships.yaml dict (from ``load()`` or built in
              memory). Must be a dict; ``load()`` enforces that.

    Returns:
        A list of ValidationError. Empty if the document is valid.
    """
    errors: List[ValidationError] = []

    if not isinstance(data, dict):
        return [ValidationError("", "top-level must be a mapping")]

    # -- Required: project name --
    project = data.get("project")
    if project is None:
        errors.append(ValidationError("project", "missing required field"))
    elif not isinstance(project, str) or not project.strip():
        errors.append(ValidationError("project", "must be a non-empty string"))

    # -- Optional but typed: version --
    if "version" in data and not isinstance(data["version"], (str, int, float)):
        errors.append(
            ValidationError(
                "version",
                f"must be a string or number, got {type(data['version']).__name__}",
            )
        )

    # -- environments — required, non-empty list of strings --
    environments = data.get("environments")
    if environments is None:
        errors.append(ValidationError("environments", "missing required field"))
    elif not isinstance(environments, list) or not environments:
        errors.append(
            ValidationError("environments", "must be a non-empty list of strings")
        )
    else:
        for i, env in enumerate(environments):
            if not isinstance(env, str) or not env.strip():
                errors.append(
                    ValidationError(f"environments[{i}]", "must be a non-empty string")
                )

    # -- config block — optional; if present, sub-keys must be strings --
    config_block = data.get("config")
    if config_block is not None:
        if not isinstance(config_block, dict):
            errors.append(ValidationError("config", "must be a mapping"))
        else:
            for key, val in config_block.items():
                if not isinstance(val, str) or not val.strip():
                    errors.append(
                        ValidationError(
                            f"config.{key}", "must be a non-empty string path"
                        )
                    )

    # -- discovery block — optional. Currently only one knob:
    #    ``extensions`` (list of strings) extends the default
    #    harvest-candidate set. See discovery.DEFAULT_HARVEST_EXTENSIONS
    #    for the baked-in baseline; the values here are added on top.
    discovery_block = data.get("discovery")
    if discovery_block is not None:
        if not isinstance(discovery_block, dict):
            errors.append(ValidationError("discovery", "must be a mapping"))
        else:
            extensions = discovery_block.get("extensions")
            if extensions is not None:
                if not isinstance(extensions, list):
                    errors.append(
                        ValidationError(
                            "discovery.extensions",
                            f"must be a list of extension strings, got "
                            f"{type(extensions).__name__}",
                        )
                    )
                else:
                    for i, ext in enumerate(extensions):
                        if not isinstance(ext, str) or not ext.strip():
                            errors.append(
                                ValidationError(
                                    f"discovery.extensions[{i}]",
                                    "must be a non-empty string "
                                    "(e.g. '.bteq' or 'bteq')",
                                )
                            )

    # -- mcp block — optional; default settings for `python -m ships_mcp`.
    #    CLI flags and FASTMCP_* env vars still take precedence over these
    #    values at server start; this block exists so projects can pin a
    #    repeatable default instead of relying on memorised CLI strings.
    mcp_block = data.get("mcp")
    if mcp_block is not None:
        if not isinstance(mcp_block, dict):
            errors.append(ValidationError("mcp", "must be a mapping"))
        else:
            valid_transports = ("stdio", "sse", "streamable-http")
            if (
                "transport" in mcp_block
                and mcp_block["transport"] not in valid_transports
            ):
                errors.append(
                    ValidationError(
                        "mcp.transport",
                        f"must be one of {list(valid_transports)}, "
                        f"got {mcp_block['transport']!r}",
                    )
                )
            if "host" in mcp_block and (
                not isinstance(mcp_block["host"], str) or not mcp_block["host"].strip()
            ):
                errors.append(ValidationError("mcp.host", "must be a non-empty string"))
            if "port" in mcp_block:
                port = mcp_block["port"]
                if (
                    not isinstance(port, int)
                    or isinstance(port, bool)
                    or not (1 <= port <= 65535)
                ):
                    errors.append(
                        ValidationError(
                            "mcp.port", "must be an integer between 1 and 65535"
                        )
                    )
            if "path" in mcp_block and (
                not isinstance(mcp_block["path"], str)
                or not mcp_block["path"].startswith("/")
            ):
                errors.append(
                    ValidationError("mcp.path", "must be a string beginning with '/'")
                )
            if "stateless" in mcp_block and not isinstance(
                mcp_block["stateless"], bool
            ):
                errors.append(ValidationError("mcp.stateless", "must be a boolean"))
            if "log_level" in mcp_block and mcp_block["log_level"] not in (
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
                "CRITICAL",
            ):
                errors.append(
                    ValidationError(
                        "mcp.log_level",
                        "must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL",
                    )
                )

    # -- deployment block — optional; configures runtime deployment behaviour --
    deployment_block = data.get("deployment")
    if deployment_block is not None:
        if not isinstance(deployment_block, dict):
            errors.append(ValidationError("deployment", "must be a mapping"))
        else:
            baseline_dir = deployment_block.get("baseline_dir")
            if baseline_dir is not None:
                if not isinstance(baseline_dir, str) or not baseline_dir.strip():
                    errors.append(
                        ValidationError(
                            "deployment.baseline_dir",
                            "must be a non-empty string path (e.g. /shared/ships-baselines/OMR/)",
                        )
                    )

    # -- packaging block — optional; the "single front door" profile (#384).
    #    Lets `ships process` package with near-zero args by supplying
    #    defaults for source / name / default_env / env_config. All values
    #    are strings; default_env, when given, must be one of environments.
    #    This is the same block the SHIPS Navigator wizard persists (#382).
    packaging_block = data.get("packaging")
    if packaging_block is not None:
        if not isinstance(packaging_block, dict):
            errors.append(ValidationError("packaging", "must be a mapping"))
        else:
            # ``root_parent`` (#501) — when set, harvest/process inject a
            # ``FROM <root_parent>`` clause into top-level CREATE DATABASE /
            # CREATE USER statements that don't already have one, so wave
            # ordering can deploy them after their parent. Mirrors the
            # ``--root-parent`` CLI flag for the argless workflow.
            for key in ("source", "name", "default_env", "env_config", "root_parent"):
                if key in packaging_block:
                    val = packaging_block[key]
                    if not isinstance(val, str) or not val.strip():
                        errors.append(
                            ValidationError(
                                f"packaging.{key}", "must be a non-empty string"
                            )
                        )
            default_env = packaging_block.get("default_env")
            if (
                isinstance(default_env, str)
                and default_env.strip()
                and isinstance(environments, list)
                and default_env not in environments
            ):
                errors.append(
                    ValidationError(
                        "packaging.default_env",
                        f"{default_env!r} is not one of environments {environments}",
                    )
                )

            # ``fix`` sub-block (#523) — per-project defaults for the
            # `ships process` fix stage. Adds opt-in fixers to the
            # default-on set, or subtracts default-on fixers that a
            # particular project doesn't want (e.g. a project that
            # authors grants by hand can put ``disable: [grants_derivation]``
            # here).
            fix_block = packaging_block.get("fix")
            if fix_block is not None:
                if not isinstance(fix_block, dict):
                    errors.append(ValidationError("packaging.fix", "must be a mapping"))
                else:
                    for list_key in ("rules", "disable"):
                        entries = fix_block.get(list_key)
                        if entries is None:
                            continue
                        if not isinstance(entries, list) or not all(
                            isinstance(e, str) and e.strip() for e in entries
                        ):
                            errors.append(
                                ValidationError(
                                    f"packaging.fix.{list_key}",
                                    "must be a list of non-empty strings",
                                )
                            )

    # -- stages block — optional; each entry must be a known stage with
    #    valid strict/on_error values --
    stages_block = data.get("stages")
    if stages_block is not None:
        if not isinstance(stages_block, dict):
            errors.append(ValidationError("stages", "must be a mapping"))
        else:
            for stage_name, stage_cfg in stages_block.items():
                if stage_name not in STAGES:
                    errors.append(
                        ValidationError(
                            f"stages.{stage_name}",
                            f"unknown stage; valid stages are {list(STAGES)}",
                        )
                    )
                    continue
                if not isinstance(stage_cfg, dict):
                    errors.append(
                        ValidationError(f"stages.{stage_name}", "must be a mapping")
                    )
                    continue

                if "strict" in stage_cfg and not isinstance(stage_cfg["strict"], bool):
                    errors.append(
                        ValidationError(
                            f"stages.{stage_name}.strict",
                            f"must be a boolean, got "
                            f"{type(stage_cfg['strict']).__name__}",
                        )
                    )
                if (
                    "on_error" in stage_cfg
                    and stage_cfg["on_error"] not in VALID_ON_ERROR
                ):
                    errors.append(
                        ValidationError(
                            f"stages.{stage_name}.on_error",
                            f"must be one of {list(VALID_ON_ERROR)}, "
                            f"got {stage_cfg['on_error']!r}",
                        )
                    )

    return errors


# ---------------------------------------------------------------
# Defaults application
# ---------------------------------------------------------------


def apply_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a new dict with Layer 1 defaults filled in for any
    setting not present in ``data``.

    Does not mutate the input. Validation should be run first —
    this function does not check the document, only fills gaps.

    Args:
        data: A (validated) ships.yaml dict.

    Returns:
        A new dict with Layer 1 defaults applied.
    """
    out = _deep_copy_dict(data)

    # config.* — fill any missing key
    config = out.setdefault("config", {})
    if not isinstance(config, dict):
        # Validation should have caught this; defensive fall-through.
        return out
    for key, value in LAYER_1_DEFAULTS["config"].items():
        config.setdefault(key, value)

    # stages.<stage>.{strict,on_error} — fill per stage
    stages = out.setdefault("stages", {})
    if not isinstance(stages, dict):
        return out
    for stage in STAGES:
        stage_cfg = stages.setdefault(stage, {})
        if not isinstance(stage_cfg, dict):
            continue
        defaults = LAYER_1_DEFAULTS["stages"][stage]
        for key, value in defaults.items():
            stage_cfg.setdefault(key, value)

    return out


# ---------------------------------------------------------------
# Generate-on-first-run
# ---------------------------------------------------------------


def generate_default(
    project_name: str,
    environments: List[str],
    version: Optional[str] = "1.0",
) -> Dict[str, Any]:
    """
    Build a fresh ships.yaml dict for first-run scaffold.

    Produces the minimum the schema requires (project + environments)
    plus the canonical config-pointers block. Per-stage settings are
    omitted intentionally — they fall through to Layer 1 defaults at
    cascade-resolve time, keeping the file readable for new projects.

    Args:
        project_name:  Non-empty project identifier.
        environments:  Non-empty list of environment names.
        version:       Project version string (or None to omit).

    Returns:
        A dict suitable for ``yaml.safe_dump()`` and for passing
        through ``validate()`` without errors.

    Raises:
        ValueError: If ``project_name`` is empty or ``environments``
            is empty / not a list.
    """
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError("project_name must be a non-empty string")
    if not isinstance(environments, list) or not environments:
        raise ValueError("environments must be a non-empty list")
    for i, env in enumerate(environments):
        if not isinstance(env, str) or not env.strip():
            raise ValueError(f"environments[{i}] must be a non-empty string")

    out: Dict[str, Any] = {
        "project": project_name,
        "environments": list(environments),
        "config": dict(LAYER_1_DEFAULTS["config"]),
    }
    if version is not None:
        out["version"] = version
    return out


def write_if_missing(path: str, data: Dict[str, Any]) -> bool:
    """
    Write ``data`` as YAML to ``path``, but ONLY if no file exists
    there. An existing file — empty or not — is never overwritten.

    Atomic on success: writes via tempfile in the same directory
    then ``os.replace``, so a partial write cannot leave a corrupt
    ships.yaml in place.

    Args:
        path: Target filesystem path.
        data: Mapping to serialise.

    Returns:
        True  — file was written (didn't previously exist).
        False — file already existed; no action taken.

    Raises:
        OSError: On filesystem failures (parent dir missing, etc.).
    """
    if os.path.exists(path):
        return False

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=".ships_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data,
                f,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return True


# ---------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------


def _deep_copy_dict(data: Any) -> Any:
    """
    Deep-copy a JSON-/YAML-shaped value (dict, list, scalar).

    Avoids the ``copy.deepcopy`` overhead and keeps the surface
    small — defaults are simple shapes by construction.
    """
    if isinstance(data, dict):
        return {k: _deep_copy_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_deep_copy_dict(v) for v in data]
    return data


# ---------------------------------------------------------------
# Inspect-config readers
# ---------------------------------------------------------------
