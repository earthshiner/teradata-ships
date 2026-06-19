"""
token_resolution_artefact — context/ships.token_resolution.json.

Canonical, per-environment serialisation of the token-resolution audit
(see :mod:`token_audit`). The artefact is the single source of truth for:

* the Tokenisation tab of the package report,
* the ``token_resolution_clean`` trust signal,
* any agent or downstream tool that wants the audit data without
  re-running the whole pipeline.

Schema (v1.0)::

    {
      "schema_version": "1.0",
      "generated_at": "<ISO-8601 UTC>",
      "generated_by": "td_release_packager.token_resolution_artefact",
      "environments": [
        {
          "env": "DEV",
          "defined": 13,
          "undefined": [],
          "unused": [],
          "empty": [],
          "roles": {"DB_PREFIX": "IDENTITY", "PERM_SPACE": "SCALAR"},
          "clobbers": [
            {"physical_name": "db.x",
             "sources": ["a.viw", "b.viw"],
             "tokens": ["A", "B"]}
          ],
          "collisions": [
            {"value": "1e9",
             "tokens": ["PERM_SPACE", "SPOOL_SPACE"],
             "class": "scalar"}
          ],
          "rejected_allowlist": [
            {"tokens": ["A", "B"],
             "value": "db.x",
             "reason": "operator-stated reason"}
          ]
        }
      ]
    }

The audit's data model is hashable/frozen; this module flattens it into
plain JSON-safe dicts. No mutation of the input report.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Mapping, Sequence, Tuple

from td_release_packager.expected_collisions import RejectedEntry
from td_release_packager.token_audit import ResolutionReport


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

ARTEFACT_SCHEMA_VERSION = "1.0"
ARTEFACT_FILENAME = "ships.token_resolution.json"
ARTEFACT_REF = f"context/{ARTEFACT_FILENAME}"


# --------------------------------------------------------------------------
# Public type
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvAuditResult:
    """One environment's audit plus any rejected allow-list entries.

    Couples the report and the rejected-entry list because together they
    represent everything the artefact records for a single environment.
    """

    report: ResolutionReport
    rejected: Tuple[RejectedEntry, ...] = ()


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _serialise_env(result: EnvAuditResult) -> dict:
    """Flatten one EnvAuditResult to a JSON-safe dict."""
    report = result.report
    return {
        "env": report.env,
        "defined": report.defined_count,
        "undefined": list(report.undefined),
        "unused": list(report.unused),
        "empty": list(report.empty),
        "roles": {
            tok: assignment.role.value for tok, assignment in report.roles.items()
        },
        "clobbers": [
            {
                "physical_name": c.physical_name,
                "sources": list(c.sources),
                "tokens": list(c.tokens),
            }
            for c in report.clobbers
        ],
        "collisions": [
            {
                "value": c.value,
                "tokens": list(c.tokens),
                "class": c.classification.value,
            }
            for c in report.collisions
        ],
        "rejected_allowlist": [
            {
                "tokens": list(r.entry.tokens),
                "value": r.real_collision_value,
                "reason": r.entry.reason,
            }
            for r in result.rejected
        ],
    }


def compute_artefact(
    results: Iterable[EnvAuditResult],
    *,
    generated_at: str | None = None,
) -> dict:
    """Build the canonical artefact dict from per-env audit results.

    Args:
        results: one EnvAuditResult per environment included in the package.
        generated_at: ISO-8601 timestamp. Defaults to now (UTC). Accepting
            an override keeps the function deterministic in tests.
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return {
        "schema_version": ARTEFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_by": "td_release_packager.token_resolution_artefact",
        "environments": [_serialise_env(r) for r in results],
    }


def write_artefact(pkg_dir: str, document: dict) -> str:
    """Write the artefact JSON under ``<pkg_dir>/context/`` and return the path."""
    target = os.path.join(pkg_dir, "context", ARTEFACT_FILENAME)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return target


def load_artefact(pkg_dir: str) -> dict | None:
    """Load the artefact dict from a package directory, or None if absent."""
    path = os.path.join(pkg_dir, "context", ARTEFACT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Convenience: per-env audit with allow-list, packaged for write_artefact
# --------------------------------------------------------------------------


def audit_envs_with_allowlist(
    *,
    env_configs: Mapping[str, Mapping[str, str]],
    resolved_envs: Mapping[str, Mapping[str, str]],
    payload_files: Sequence[Tuple[str, str]],
    allowlist=None,
    referenced_tokens: Sequence[str] | None = None,
) -> List[EnvAuditResult]:
    """Audit one or more envs and apply the allow-list to each.

    Helper for callers (builder, inspect stage) that already have multiple
    env configs in hand. Each env gets its own audit pass; the allow-list
    is shared.

    Args:
        env_configs: mapping ``{env_name: {token: raw_value}}``.
        resolved_envs: mapping ``{env_name: {token: resolved_value}}``.
        payload_files: iterable of ``(relative_filename, sql_text)`` pairs.
            Same set passes to every env (payload is env-agnostic).
        allowlist: optional Allowlist from ``expected_collisions``.
        referenced_tokens: optional precomputed set of tokens referenced in
            payload. When supplied, used to derive ``undefined`` / ``unused``
            without rescanning.
    """
    from td_release_packager.expected_collisions import apply_to_report
    from td_release_packager.token_audit import audit_project

    payload_files = tuple(payload_files)
    results: List[EnvAuditResult] = []
    for env_name, raw in env_configs.items():
        resolved = resolved_envs.get(env_name, raw)
        defined = set(raw.keys())
        referenced = set(referenced_tokens or ())
        undefined = sorted(referenced - defined)
        unused = sorted(defined - referenced)
        empty = sorted(t for t in defined if not resolved.get(t, ""))

        report = audit_project(
            env=env_name,
            env_config=raw,
            resolved_env=resolved,
            payload_files=payload_files,
            defined_count=len(defined),
            undefined=undefined,
            unused=unused,
            empty=empty,
        )
        rejected: Tuple[RejectedEntry, ...] = ()
        if allowlist is not None and not allowlist.is_empty:
            report, rejected = apply_to_report(report, allowlist)
        results.append(EnvAuditResult(report=report, rejected=rejected))
    return results
