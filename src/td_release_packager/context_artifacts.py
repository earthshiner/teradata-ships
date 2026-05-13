"""
context_artifacts.py — Agent-facing SHIPS context artefacts.

The package builder already emits ships.build.json, ships.provenance.json,
ships.integrity.json, reports, and deployment scripts. Those files are
excellent for machines and DBAs, but autonomous agents also need a compact,
stable context contract that explains what the package is, where it sits in
the workflow, what evidence exists, and what should happen next.

This module writes the SHIPS agent-context artefacts into each generated
package:

    ships.index.json      — canonical read-first entrypoint for agents
    ships.context.json    — durable workflow context and constraints
    ships.manifest.json   — agent-safe package inventory and governance summary
    ships.handoff.json    — next-actor instructions and readiness guidance

They intentionally reference detailed evidence instead of duplicating it.
That keeps the context budget small while preserving traceability.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from td_release_packager.models import BuildConfig, BuildManifest

CONTEXT_SCHEMA_VERSION = "1.0"
INDEX_SCHEMA_VERSION = "1.0"
INDEX_FILENAME = "ships.index.json"
CONTEXT_FILENAME = "ships.context.json"
MANIFEST_FILENAME = "ships.manifest.json"
HANDOFF_FILENAME = "ships.handoff.json"
BUILD_FILENAME = "ships.build.json"
PROVENANCE_FILENAME = "ships.provenance.json"
INTEGRITY_FILENAME = "ships.integrity.json"
DECISIONS_FILENAME = "ships.decisions.json"
PACKAGE_REPORT_FILENAME = "package_report.html"
README_FILENAME = "README.txt"


def write_context_artifacts(
    pkg_dir: str,
    manifest: BuildManifest,
    config: Optional[BuildConfig] = None,
) -> Dict[str, str]:
    """
    Write SHIPS agent-context artefacts into a package directory.

    Args:
        pkg_dir: Package directory that will later be archived.
        manifest: ships.build.json manifest object for this package.
        config: Optional build configuration. Present on the normal build
            path; omitted when regenerating context for an auto-split sibling.

    Returns:
        Mapping of logical artefact filename to filesystem path.
    """
    os.makedirs(pkg_dir, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest_dict = _to_dict(manifest)
    config_dict = _to_dict(config) if config is not None else {}
    context_id = _context_id(manifest_dict)

    artefacts = {
        CONTEXT_FILENAME: _build_context_document(
            context_id=context_id,
            generated_at=generated_at,
            manifest=manifest_dict,
            config=config_dict,
        ),
        MANIFEST_FILENAME: _build_agent_manifest_document(
            context_id=context_id,
            generated_at=generated_at,
            manifest=manifest_dict,
        ),
        HANDOFF_FILENAME: _build_handoff_document(
            context_id=context_id,
            generated_at=generated_at,
            manifest=manifest_dict,
        ),
        INDEX_FILENAME: _build_index_document(
            context_id=context_id,
            generated_at=generated_at,
            manifest=manifest_dict,
        ),
    }

    written: Dict[str, str] = {}
    for filename, document in artefacts.items():
        path = os.path.join(pkg_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(document, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        written[filename] = path

    return written


def _to_dict(value: Any) -> Dict[str, Any]:
    """Return a JSON-friendly dictionary for dataclasses or plain objects."""
    if value is None:
        return {}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _context_id(manifest: Dict[str, Any]) -> str:
    """Create a deterministic short context id from stable package metadata."""
    seed = "|".join(
        str(manifest.get(k, ""))
        for k in ("package_filename", "build_number", "environment", "timestamp")
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"ships-context-{digest}"


def _package_state(manifest: Dict[str, Any]) -> str:
    """Return the workflow state implied by package manifest metadata."""
    trust = manifest.get("trust") or {}
    label = str(trust.get("label", "")).upper()
    if label == "BLOCKED":
        return "package-built-blocked"
    if label == "READY-WITH-CAVEATS":
        return "package-built-ready-with-caveats"
    return "package-built-awaiting-deployment"


def _governance(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Extract agent-relevant governance and policy controls."""
    return {
        "target_env": manifest.get("target_env") or manifest.get("environment"),
        "change_ref": manifest.get("change_ref"),
        "require_change_ref": bool(manifest.get("require_change_ref")),
        "require_signature": bool(manifest.get("require_signature")),
        "require_asymmetric_signature": bool(
            manifest.get("require_asymmetric_signature")
        ),
        "require_approvals": int(manifest.get("require_approvals") or 1),
        "require_tls": bool(manifest.get("require_tls")),
        "package_max_age_days": int(manifest.get("package_max_age_days") or 0),
        "package_age_violation_level": manifest.get("package_age_violation_level"),
    }


def _entrypoints() -> Dict[str, Dict[str, Any]]:
    """Return self-describing SHIPS package entrypoints."""
    return {
        "index": {
            "path": INDEX_FILENAME,
            "description": "Canonical read-first package index. Describes the SHIPS metadata files, recommended read order, and agent instructions.",
            "required": True,
            "audience": ["agent", "human", "ci_cd", "dba", "governance"],
        },
        "handoff": {
            "path": HANDOFF_FILENAME,
            "description": "Next-actor instructions describing what should happen next, what must be reviewed, and what evidence should be checked before deployment.",
            "required": True,
            "audience": ["agent", "dba", "human"],
        },
        "context": {
            "path": CONTEXT_FILENAME,
            "description": "Durable workflow context for agents and humans, including objective, current state, constraints, assumptions, governance, trust status, and references.",
            "required": True,
            "audience": ["agent", "human", "ci_cd"],
        },
        "build": {
            "path": BUILD_FILENAME,
            "description": "Authoritative technical build manifest containing build identity, package version, target environment, token summary, trust label, policy flags, and build-time metadata.",
            "required": True,
            "audience": ["agent", "human", "ci_cd", "dba"],
        },
        "manifest": {
            "path": MANIFEST_FILENAME,
            "description": "Compact, agent-safe package inventory describing included artefacts, object counts, token usage, dependency contract, governance settings, and deployment-relevant contents.",
            "required": True,
            "audience": ["agent", "dba", "ci_cd", "governance"],
        },
        "integrity": {
            "path": INTEGRITY_FILENAME,
            "description": "Hashes and tamper-evidence metadata used to confirm that package contents have not changed unexpectedly.",
            "required": True,
            "audience": ["agent", "ci_cd", "dba", "governance"],
        },
        "provenance": {
            "path": PROVENANCE_FILENAME,
            "description": "Source lineage and origin evidence, including source-to-package filename transformations and traceability metadata.",
            "required": True,
            "audience": ["agent", "governance", "ci_cd", "dba"],
        },
        "decisions": {
            "path": DECISIONS_FILENAME,
            "description": "Project-level decision log containing build-stage decisions, warnings, issue codes, rule outcomes, and rationale captured during SHIPS pipeline execution.",
            "required": False,
            "audience": ["agent", "human", "governance"],
        },
        "package_report": {
            "path": PACKAGE_REPORT_FILENAME,
            "description": "Human-readable HTML report with package inventory, wave visualisation, trust report, and deploy command guidance.",
            "required": False,
            "audience": ["human", "dba", "governance"],
        },
        "readme": {
            "path": README_FILENAME,
            "description": "Human quick-start instructions for inspecting, verifying, and deploying the package.",
            "required": False,
            "audience": ["human", "dba"],
        },
    }


def _recommended_read_order() -> list[str]:
    """Return the canonical read order by entrypoint key."""
    return [
        "index",
        "handoff",
        "context",
        "build",
        "manifest",
        "integrity",
        "provenance",
        "decisions",
        "package_report",
    ]


def _agent_instructions() -> Dict[str, Any]:
    """Return standing instructions for downstream SHIPS-aware agents."""
    return {
        "summary": "Before taking action on this package, read ships.index.json first, then follow recommended_read_order.",
        "before_action": [
            "Read ships.index.json to discover the package context contract and entrypoints.",
            "Read ships.handoff.json to determine the requested next action and blocking conditions.",
            "Read ships.context.json to understand workflow state, constraints, governance settings, and trust status.",
            "Read ships.integrity.json before trusting package contents.",
            "Read ships.manifest.json before modifying, deploying, summarising, or routing package contents.",
            "Read ships.provenance.json when source lineage, repository traceability, or filename transformation evidence matters.",
            "Read ships.decisions.json when stage outcomes, warnings, issue codes, or decision rationale are needed.",
        ],
        "must_not_assume": [
            "target environment",
            "deployment approval",
            "trust status",
            "object deployment order",
            "token resolution status",
            "package integrity",
            "source provenance",
        ],
        "blocking_rule": "Do not deploy or approve a package when the trust label is BLOCKED or when integrity, signature, target-environment, approval, change-reference, or TLS policy checks fail.",
    }


def _evidence_files() -> Dict[str, str]:
    """Canonical evidence files expected within a SHIPS package."""
    return {key: value["path"] for key, value in _entrypoints().items()}


def _safe_token_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """
    Summarise token usage without duplicating resolved values.

    ships.build.json already contains the full token map. Agent context should be
    small and should avoid re-spreading environment-specific values unless an
    actor deliberately opens ships.build.json.
    """
    tokens = manifest.get("tokens_resolved") or {}
    return {
        "token_count": len(tokens),
        "token_names": sorted(tokens.keys()),
        "values_redacted": True,
        "full_values_reference": "ships.build.json#/tokens_resolved",
    }


def _build_index_document(
    *,
    context_id: str,
    generated_at: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Build ships.index.json, the canonical package read-first contract."""
    return {
        "schema": "teradata-ships/package-index/v1",
        "schema_version": INDEX_SCHEMA_VERSION,
        "package_type": "teradata-ships",
        "index_version": INDEX_SCHEMA_VERSION,
        "read_first": INDEX_FILENAME,
        "context_id": context_id,
        "generated_at": generated_at,
        "package": {
            "name": manifest.get("package_name"),
            "filename": manifest.get("package_filename"),
            "environment": manifest.get("environment"),
            "build_number": manifest.get("build_number"),
            "current_state": _package_state(manifest),
        },
        "entrypoints": _entrypoints(),
        "recommended_read_order": _recommended_read_order(),
        "agent_instructions": _agent_instructions(),
    }


def _build_context_document(
    *,
    context_id: str,
    generated_at: str,
    manifest: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Build ships.context.json."""
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "context_id": context_id,
        "generated_at": generated_at,
        "purpose": "Durable SHIPS workflow context for humans, CI/CD, MCP tools, and autonomous agents.",
        "workflow": "package-build",
        "stage": "package",
        "current_state": _package_state(manifest),
        "objective": "Deploy a trusted, self-contained Teradata package without relying on prior chat or agent memory.",
        "package": {
            "name": manifest.get("package_name"),
            "filename": manifest.get("package_filename"),
            "environment": manifest.get("environment"),
            "build_number": manifest.get("build_number"),
            "role": manifest.get("role") or "single",
            "release_group": manifest.get("release_group") or "",
            "requires": manifest.get("requires") or [],
        },
        "source_of_truth": {
            "source_dir": config.get("source_dir", ""),
            "source_commit": manifest.get("source_commit") or "",
            "source_dirty": bool(manifest.get("source_dirty")),
            "env_config_file": config.get("env_config_file", ""),
        },
        "constraints": [
            "Read ships.index.json first; it is the canonical package entrypoint and context contract.",
            "Preserve Teradata SQL syntax and deployment order.",
            "Do not change business logic during package handoff or deployment.",
            "Use ships.build.json as the authoritative technical build manifest.",
            "Use ships.manifest.json for compact agent-safe inventory and policy context.",
            "Use ships.provenance.json for file-level source-to-package traceability.",
            "Do not rely on conversational memory between agents; carry this package context forward.",
        ],
        "governance": _governance(manifest),
        "trust": manifest.get("trust") or {},
        "context_budget": {
            "preferred_agent_prompting": "Load ships.index.json first, then open referenced evidence only when needed.",
            "detailed_evidence_is_referenced_not_repeated": True,
            "token_values_are_not_duplicated_here": True,
        },
        "references": _evidence_files(),
    }


def _build_agent_manifest_document(
    *,
    context_id: str,
    generated_at: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Build ships.manifest.json."""
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "context_id": context_id,
        "generated_at": generated_at,
        "package": {
            "name": manifest.get("package_name"),
            "filename": manifest.get("package_filename"),
            "environment": manifest.get("environment"),
            "build_number": manifest.get("build_number"),
            "built_at": manifest.get("timestamp"),
            "author": manifest.get("author"),
            "description": manifest.get("description"),
            "source_commit": manifest.get("source_commit") or "",
            "source_dirty": bool(manifest.get("source_dirty")),
        },
        "inventory": {
            "file_count": manifest.get("file_count") or 0,
            "phase_inventory": manifest.get("phase_inventory") or {},
            "discovery": manifest.get("discovery") or {},
            "baseline_dir": manifest.get("baseline_dir") or "",
        },
        "dependency_contract": {
            "role": manifest.get("role") or "single",
            "release_group": manifest.get("release_group") or "",
            "requires": manifest.get("requires") or [],
            "deploy_order_hint": "Deploy required companion packages first, then this package.",
        },
        "tokens": _safe_token_summary(manifest),
        "warnings": manifest.get("warnings") or [],
        "governance": _governance(manifest),
        "trust": manifest.get("trust") or {},
        "evidence": _evidence_files(),
    }


def _build_handoff_document(
    *,
    context_id: str,
    generated_at: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Build ships.handoff.json."""
    governance = _governance(manifest)
    required_actions = [
        "Read ships.index.json first and follow recommended_read_order.",
        "Review ships.build.json, ships.manifest.json, and package_report.html.",
        "Verify package integrity before deployment.",
        "Confirm target environment matches the package target_env.",
        "Deploy required companion packages first if dependency_contract.requires is not empty.",
        "Run deploy.py from the package root or use the embedded deployer entry point.",
        "Capture deployment logs and post-deploy evidence.",
    ]
    if governance["require_change_ref"]:
        required_actions.insert(2, "Confirm a valid change_ref is present before deployment.")
    if governance["require_signature"] or governance["require_asymmetric_signature"]:
        required_actions.insert(2, "Verify package signature before deployment.")
    if governance["require_approvals"] > 1:
        required_actions.insert(2, "Obtain the required four-eyes approval before deployment.")
    if governance["require_tls"]:
        required_actions.insert(2, "Use a TLS/SSL-protected Teradata connection.")

    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "context_id": context_id,
        "generated_at": generated_at,
        "handoff_type": "package-to-deployment",
        "from_actor": "ships-package-builder",
        "to_actor": "human-operator-or-deployment-agent",
        "current_state": _package_state(manifest),
        "package": {
            "name": manifest.get("package_name"),
            "filename": manifest.get("package_filename"),
            "environment": manifest.get("environment"),
            "build_number": manifest.get("build_number"),
            "role": manifest.get("role") or "single",
            "requires": manifest.get("requires") or [],
        },
        "required_actions": required_actions,
        "preconditions": {
            "target_environment_must_match": governance["target_env"],
            "change_ref_required": governance["require_change_ref"],
            "signature_required": governance["require_signature"],
            "asymmetric_signature_required": governance["require_asymmetric_signature"],
            "approvals_required": governance["require_approvals"],
            "tls_required": governance["require_tls"],
        },
        "blocking_conditions": [
            "Trust label is BLOCKED.",
            "Package integrity or signature verification fails.",
            "Target environment does not match target_env.",
            "Required approval, change reference, or TLS policy is not satisfied.",
            "A required companion package listed in requires has not been deployed first.",
        ],
        "evidence_to_return": [
            "deployment result summary",
            "logs/.deploy_manifest.json",
            "query-band or audit references",
            "post-install validation outputs",
            "any drift, skipped, failed, or waived object details",
        ],
        "references": _evidence_files(),
    }
