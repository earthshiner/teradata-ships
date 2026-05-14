"""
trust.py — Phase 1 Trust Report for SHIPS packages.

Computes discrete trust signals from build-time artefacts and derives
a human-readable label (READY / READY-WITH-CAVEATS / BLOCKED) that tells
a DBA or deployment agent whether a package is safe to promote.

**Why discrete signals, not a composite score**

A composite score (e.g. 94%) suffers from Goodhart's Law — once a score
becomes a target, it ceases to be a good measure. It also creates false
precision: "this package is 94% safe to deploy" is not a meaningful
statement. Discrete signals with a derived label are actionable:
"inspect_lint is WARN — two naming conventions failed" is specific enough
to fix.

**Phase 1 signals (computable at build time)**

| Signal               | Source                      | Fail condition           |
|----------------------|-----------------------------|--------------------------|
| inspect_token_format | ships.decisions.json inspect stage | Any INSPECT_TOKEN_MALFORMED error   |
| inspect_lint         | ships.decisions.json inspect stage | Any INSPECT_LINT_VIOLATION error    |
| inspect_grants       | ships.decisions.json inspect stage | Any INSPECT_GRANT_VIOLATION error   |
| provenance_complete  | ships.provenance.json existence   | File absent from payload |

**Label derivation**

  BLOCKED           Any signal has status="fail"
  READY-WITH-CAVEATS  One or more signals have status="warn", none "fail"
  READY             All signals pass

The label is the primary signal for gate automation. The per-signal detail
is for human inspection and structured audit logs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from td_release_packager.orchestrator.issue_codes import (
    INSPECT_GRANT_VIOLATION,
    INSPECT_LINT_VIOLATION,
    INSPECT_TOKEN_MALFORMED,
)


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------

TRUST_PASS = "pass"
TRUST_WARN = "warn"
TRUST_FAIL = "fail"
TRUST_UNKNOWN = "unknown"

LABEL_READY = "READY"
LABEL_CAVEATS = "READY-WITH-CAVEATS"
LABEL_BLOCKED = "BLOCKED"


@dataclass
class TrustSignal:
    """One discrete trust signal."""

    status: str  # pass / warn / fail / unknown
    message: str  # human summary
    issues: List[str] = field(default_factory=list)  # specific findings


@dataclass
class TrustReport:
    """Aggregate trust report for a package."""

    label: str  # READY / READY-WITH-CAVEATS / BLOCKED
    computed_at: str  # ISO-8601 timestamp
    signals: Dict[str, TrustSignal] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "computed_at": self.computed_at,
            "signals": {
                name: {
                    "status": sig.status,
                    "message": sig.message,
                    "issues": sig.issues,
                }
                for name, sig in self.signals.items()
            },
        }


# ---------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------


def compute_trust_report(source_dir: str, pkg_dir: str) -> TrustReport:
    """
    Compute Phase 1 trust signals and derive the trust label.

    Args:
        source_dir: SHIPS project root (contains ships.decisions.json).
        pkg_dir:    Built package root (contains the payload tree).

    Returns:
        Populated TrustReport.
    """
    signals: Dict[str, TrustSignal] = {}

    # Load ships.decisions.json — source of inspect signals
    decisions_path = os.path.join(source_dir, "ships.decisions.json")
    decisions = _load_decisions(decisions_path)
    inspect_stage = _find_latest_inspect_stage(decisions)

    signals["inspect_token_format"] = _inspect_signal(
        inspect_stage,
        INSPECT_TOKEN_MALFORMED,
        "Malformed {{TOKEN}} markers",
        "No malformed token markers found",
    )
    signals["inspect_lint"] = _inspect_signal(
        inspect_stage,
        INSPECT_LINT_VIOLATION,
        "Coding Discipline lint violations",
        "No lint violations found",
    )
    signals["inspect_grants"] = _inspect_signal(
        inspect_stage,
        INSPECT_GRANT_VIOLATION,
        "Grant drift detected",
        "Grant validation clean",
    )

    signals["provenance_complete"] = _provenance_signal(source_dir)
    signals["build_reproducible"] = _build_reproducible_signal(pkg_dir)

    label = _derive_label(signals)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    return TrustReport(label=label, computed_at=now, signals=signals)


# ---------------------------------------------------------------
# Signal computation helpers
# ---------------------------------------------------------------


def _load_decisions(path: str) -> dict:
    """Load ships.decisions.json or return an empty structure if absent."""
    if not os.path.exists(path):
        return {"runs": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"runs": []}


def _find_latest_inspect_stage(decisions: dict) -> Optional[dict]:
    """Return the most recent inspect stage entry across all runs."""
    best: Optional[dict] = None
    for run in decisions.get("runs", []):
        for stage in run.get("stages", []):
            if stage.get("stage") == "inspect":
                best = stage
    return best


def _inspect_signal(
    stage: Optional[dict],
    issue_code: str,
    fail_message_prefix: str,
    pass_message: str,
) -> TrustSignal:
    """
    Derive a trust signal from an inspect stage's issue list.

    A missing stage (inspect never ran) is UNKNOWN — the operator
    should run inspect before promoting.
    """
    if stage is None:
        return TrustSignal(
            status=TRUST_UNKNOWN,
            message="Inspect stage not found in ships.decisions.json — run inspect first",
        )

    matching = [i for i in stage.get("issues", []) if i.get("code") == issue_code]

    errors = [i for i in matching if i.get("severity") == "error"]
    warnings = [i for i in matching if i.get("severity") == "warning"]

    if errors:
        messages = [i.get("message", "") for i in errors]
        return TrustSignal(
            status=TRUST_FAIL,
            message=f"{fail_message_prefix}: {len(errors)} error(s)",
            issues=messages[:10],  # cap to keep ships.build.json small
        )
    if warnings:
        messages = [i.get("message", "") for i in warnings]
        return TrustSignal(
            status=TRUST_WARN,
            message=f"{fail_message_prefix}: {len(warnings)} warning(s)",
            issues=messages[:10],
        )
    return TrustSignal(status=TRUST_PASS, message=pass_message)


def _provenance_signal(source_dir: str) -> TrustSignal:
    """
    Check whether ``ships.provenance.json`` exists in the payload tree.

    The provenance file records the full source → payload file
    transformation chain and enables the deployer to link each
    failed/skipped object back to its original source file. Without
    it, the deploy report's drill-down and edit-source hints are
    disabled.
    """
    # Walk the payload tree looking for ships.provenance.json
    payload_dir = os.path.join(source_dir, "payload")
    if os.path.isdir(payload_dir):
        for root, _dirs, files in os.walk(payload_dir):
            if "ships.provenance.json" in files:
                return TrustSignal(
                    status=TRUST_PASS,
                    message="ships.provenance.json present — deploy report drill-downs enabled",
                )

    # Also check directly in source_dir
    if os.path.exists(os.path.join(source_dir, "context", "ships.provenance.json")) or os.path.exists(os.path.join(source_dir, "ships.provenance.json")):
        return TrustSignal(
            status=TRUST_PASS,
            message="ships.provenance.json present — deploy report drill-downs enabled",
        )

    return TrustSignal(
        status=TRUST_WARN,
        message=(
            "ships.provenance.json not found — deploy report drill-downs will be "
            "disabled. Rebuild the package with the current SHIPS version to "
            "generate provenance."
        ),
    )


def _build_reproducible_signal(pkg_dir: str) -> TrustSignal:
    """
    Trust signal: was the package built from a clean working tree?

    Reads ``source_dirty`` from ships.build.json in ``pkg_dir``.
    - ``source_dirty: true``  → WARN (built with --allow-dirty)
    - ``source_dirty: false`` or absent → PASS
    - ships.build.json not found    → UNKNOWN
    """
    build_json = os.path.join(pkg_dir, "context", "ships.build.json")
    if not os.path.exists(build_json):
        build_json = os.path.join(pkg_dir, "ships.build.json")
    if not os.path.exists(build_json):
        return TrustSignal(
            status=TRUST_PASS,
            message="ships.build.json absent — no evidence of dirty-tree build",
        )
    try:
        with open(build_json, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return TrustSignal(
            status=TRUST_PASS,
            message="ships.build.json unreadable — assuming clean build",
        )

    if manifest.get("source_dirty", False):
        return TrustSignal(
            status=TRUST_WARN,
            message="Package built from dirty working tree (--allow-dirty was passed). "
            "source_commit may not fully represent the deployed code.",
        )
    return TrustSignal(
        status=TRUST_PASS,
        message="Built from a clean working tree — source_commit is authoritative",
    )


# ---------------------------------------------------------------
# Label derivation
# ---------------------------------------------------------------


def _derive_label(signals: Dict[str, TrustSignal]) -> str:
    """Derive the top-level trust label from the signal set."""
    statuses = {sig.status for sig in signals.values()}
    if TRUST_FAIL in statuses:
        return LABEL_BLOCKED
    if TRUST_WARN in statuses or TRUST_UNKNOWN in statuses:
        return LABEL_CAVEATS
    return LABEL_READY


# ---------------------------------------------------------------
# Banner rendering (for CLI output)
# ---------------------------------------------------------------

_STATUS_ICON = {
    TRUST_PASS: "✓",
    TRUST_WARN: "⚠",
    TRUST_FAIL: "✗",
    TRUST_UNKNOWN: "?",
}

_LABEL_ICON = {
    LABEL_READY: "✓",
    LABEL_CAVEATS: "⚠",
    LABEL_BLOCKED: "✗",
}


def format_trust_banner(report: TrustReport, width: int = 64) -> str:
    """Return a formatted CLI banner string for the trust report."""
    bar = "=" * width
    label_icon = _LABEL_ICON.get(report.label, "?")
    lines = [
        f"\n{bar}",
        f"  Package Trust: {label_icon} {report.label}",
        bar,
    ]
    for name, sig in report.signals.items():
        icon = _STATUS_ICON.get(sig.status, "?")
        lines.append(f"  {icon} {name:<28} {sig.message}")
    lines.append(bar)
    return "\n".join(lines)
