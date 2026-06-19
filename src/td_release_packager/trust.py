"""
trust.py — Phase 1 Trust Report for SHIPS packages.

Computes discrete trust signals from build-time artefacts and derives
a machine-readable status (READY / READY_WITH_CAVEATS / BLOCKED) that
tells a DBA or deployment agent whether a package is safe to promote.

**Why discrete signals, not a composite score**

A composite score (e.g. 94%) suffers from Goodhart's Law — once a score
becomes a target, it ceases to be a good measure. It also creates false
precision: "this package is 94% safe to deploy" is not a meaningful
statement. Discrete signals with a derived status are actionable:
"inspect_lint is WARN — two naming conventions failed" is specific enough
to fix.

**Phase 1 signals (computable at build time)**

| Signal               | Source                                  | Fail condition                  |
|----------------------|-----------------------------------------|---------------------------------|
| inspect_token_format    | ships.decisions.json inspect stage      | Any INSPECT_TOKEN_MALFORMED err          |
| inspect_lint            | ships.decisions.json inspect stage      | Any INSPECT_LINT_VIOLATION err           |
| inspect_grants          | ships.decisions.json inspect stage      | Any INSPECT_GRANT_VIOLATION err          |
| provenance_complete     | context/ships.provenance.json existence | File absent from payload                 |
| build_reproducible      | context/ships.build.json.source_dirty   | source_dirty == true                     |
| token_resolution_clean  | context/ships.token_resolution.json     | ≥1 clobber / undefined / rejected allow  |

**Status derivation**

  BLOCKED             Any signal has status="fail"
  READY_WITH_CAVEATS  One or more signals have status="warn"/"unknown", none "fail"
  READY               All signals pass

**Canonical artefact**

The full TrustReport is written to `context/ships.trust.json` and is the
single source of truth. Other manifests (ships.build.json, ships.context.json,
ships.manifest.json) reference it via `"trust_ref": "context/ships.trust.json"`
rather than embedding the body.
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
# Schema + status enums
# ---------------------------------------------------------------

TRUST_SCHEMA_VERSION = "1.0"

TRUST_PASS = "pass"
TRUST_WARN = "warn"
TRUST_FAIL = "fail"
TRUST_UNKNOWN = "unknown"

STATUS_READY = "READY"
STATUS_CAVEATS = "READY_WITH_CAVEATS"
STATUS_BLOCKED = "BLOCKED"

TRUST_RESULT_FILENAME = "ships.trust.json"
TRUST_RESULT_REF = f"context/{TRUST_RESULT_FILENAME}"

# Evidence file references — relative to the package root.
_EVIDENCE_INSPECT = "ships.decisions.json"
_EVIDENCE_PROVENANCE = "context/ships.provenance.json"
_EVIDENCE_BUILD = "context/ships.build.json"
_EVIDENCE_TOKEN_RESOLUTION = "context/ships.token_resolution.json"


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass
class TrustSignal:
    """One discrete trust signal."""

    status: str  # pass / warn / fail / unknown
    message: str  # human summary
    issues: List[str] = field(default_factory=list)
    evidence_paths: List[str] = field(default_factory=list)


@dataclass
class TrustReport:
    """Aggregate trust report for a package."""

    status: str  # READY / READY_WITH_CAVEATS / BLOCKED
    evaluated_at: str  # ISO-8601 timestamp
    signals: Dict[str, TrustSignal] = field(default_factory=dict)
    schema_version: str = TRUST_SCHEMA_VERSION

    @property
    def deploy_allowed(self) -> bool:
        """A non-BLOCKED package can be deployed (subject to gates)."""
        return self.status != STATUS_BLOCKED

    @property
    def override_allowed(self) -> bool:
        """True when an operator may force-deploy despite caveats.

        Policy v1: caveats are overridable, BLOCKED is not, READY has
        nothing to override. Per-signal overrides may come later.
        """
        return self.status == STATUS_CAVEATS

    @property
    def blocking_signals(self) -> List[str]:
        """Names of signals that contribute to a BLOCKED status."""
        return [name for name, sig in self.signals.items() if sig.status == TRUST_FAIL]

    @property
    def warning_signals(self) -> List[str]:
        """Names of signals that contribute to caveats."""
        return [
            name
            for name, sig in self.signals.items()
            if sig.status in (TRUST_WARN, TRUST_UNKNOWN)
        ]

    @property
    def evidence_paths(self) -> List[str]:
        """De-duplicated rollup of every signal's evidence paths."""
        seen: List[str] = []
        for sig in self.signals.values():
            for path in sig.evidence_paths:
                if path not in seen:
                    seen.append(path)
        return seen

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "deploy_allowed": self.deploy_allowed,
            "override_allowed": self.override_allowed,
            "evaluated_at": self.evaluated_at,
            "evidence_paths": self.evidence_paths,
            "blocking_signals": self.blocking_signals,
            "warning_signals": self.warning_signals,
            "signals": {
                name: {
                    "status": sig.status,
                    "message": sig.message,
                    "issues": sig.issues,
                    "evidence_paths": sig.evidence_paths,
                }
                for name, sig in self.signals.items()
            },
        }


# ---------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------


def compute_trust_report(source_dir: str, pkg_dir: str) -> TrustReport:
    """
    Compute Phase 1 trust signals and derive the trust status.

    Args:
        source_dir: SHIPS project root (contains ships.decisions.json).
        pkg_dir:    Built package root (contains the payload tree).

    Returns:
        Populated TrustReport.
    """
    signals: Dict[str, TrustSignal] = {}

    decisions_path = os.path.join(source_dir, "ships.decisions.json")
    decisions = _load_decisions(decisions_path)
    inspect_stage = _find_latest_inspect_stage(decisions)

    signals["inspect_token_format"] = _inspect_signal(
        inspect_stage,
        INSPECT_TOKEN_MALFORMED,
        "Malformed {{TOKEN}} markers",
        "No malformed token markers found",
        source_dir,
    )
    signals["inspect_lint"] = _inspect_signal(
        inspect_stage,
        INSPECT_LINT_VIOLATION,
        "Coding Discipline lint violations",
        "No lint violations found",
        source_dir,
    )
    signals["inspect_grants"] = _inspect_signal(
        inspect_stage,
        INSPECT_GRANT_VIOLATION,
        "Grant drift detected",
        "Grant validation clean",
        source_dir,
    )

    signals["provenance_complete"] = _provenance_signal(pkg_dir)
    signals["build_reproducible"] = _build_reproducible_signal(pkg_dir)
    signals["token_resolution_clean"] = _token_resolution_signal(pkg_dir)

    status = _derive_status(signals)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    return TrustReport(status=status, evaluated_at=now, signals=signals)


def write_trust_result(pkg_dir: str, report: TrustReport) -> str:
    """Write the canonical trust result JSON to ``pkg_dir`` and return its path."""
    path = os.path.join(pkg_dir, "context", TRUST_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_trust_result(pkg_dir: str) -> Optional[dict]:
    """Load the canonical trust result dict from ``pkg_dir`` or return None."""
    path = os.path.join(pkg_dir, "context", TRUST_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    source_dir: str = "",
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
            evidence_paths=[_EVIDENCE_INSPECT],
        )

    matching = [
        i
        for i in stage.get("issues", [])
        if i.get("code") == issue_code
        and not _is_generated_artifact_issue(i, source_dir)
    ]

    errors = [i for i in matching if i.get("severity") == "error"]
    warnings = [i for i in matching if i.get("severity") == "warning"]

    if errors:
        messages = [_format_issue_for_trust(i) for i in errors]
        return TrustSignal(
            status=TRUST_FAIL,
            message=f"{fail_message_prefix}: {len(errors)} error(s)",
            issues=messages[:10],  # cap to keep ships.trust.json small
            evidence_paths=[_EVIDENCE_INSPECT],
        )
    if warnings:
        messages = [_format_issue_for_trust(i) for i in warnings]
        return TrustSignal(
            status=TRUST_WARN,
            message=f"{fail_message_prefix}: {len(warnings)} warning(s)",
            issues=messages[:10],
            evidence_paths=[_EVIDENCE_INSPECT],
        )
    return TrustSignal(
        status=TRUST_PASS,
        message=pass_message,
        evidence_paths=[_EVIDENCE_INSPECT],
    )


def _is_generated_artifact_issue(issue: dict, source_dir: str = "") -> bool:
    """Return True when a decisions issue points at SHIPS-generated output."""
    location = str(issue.get("location") or "")
    if not location:
        return False

    normalised = location.replace("\\", "/")
    lowered = normalised.lower()
    generated_markers = (
        "/releases/",
        "/.ships-work/",
        "/_rollback/",
        "/logs/rollback/",
    )
    if any(marker in lowered for marker in generated_markers):
        return True

    if source_dir:
        source_abs = os.path.abspath(source_dir).replace("\\", "/").rstrip("/")
        release_prefix = f"{source_abs}/releases/".lower()
        if lowered.startswith(release_prefix):
            return True

    return False


def _format_issue_for_trust(issue: dict) -> str:
    """Render a decisions issue with its location kept in the trust report."""
    message = issue.get("message", "")
    location = issue.get("location", "")
    if location:
        return f"{location}: {message}"
    return message


def _provenance_signal(pkg_dir: str) -> TrustSignal:
    """Check whether the package contains ``context/ships.provenance.json``."""
    provenance_path = os.path.join(pkg_dir, "context", "ships.provenance.json")
    if os.path.exists(provenance_path):
        return TrustSignal(
            status=TRUST_PASS,
            message="context/ships.provenance.json present — deploy report drill-downs enabled",
            evidence_paths=[_EVIDENCE_PROVENANCE],
        )

    return TrustSignal(
        status=TRUST_WARN,
        message=(
            "context/ships.provenance.json not found — deploy report drill-downs "
            "will be disabled. Rebuild the package with the current SHIPS version "
            "to generate provenance."
        ),
        evidence_paths=[_EVIDENCE_PROVENANCE],
    )


def _build_reproducible_signal(pkg_dir: str) -> TrustSignal:
    """
    Trust signal: was the package built from a clean working tree?

    Reads ``source_dirty`` from context/ships.build.json in ``pkg_dir``.
    """
    build_json = os.path.join(pkg_dir, "context", "ships.build.json")
    if not os.path.exists(build_json):
        return TrustSignal(
            status=TRUST_PASS,
            message="context/ships.build.json absent — no evidence of dirty-tree build",
            evidence_paths=[_EVIDENCE_BUILD],
        )
    try:
        with open(build_json, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return TrustSignal(
            status=TRUST_PASS,
            message="context/ships.build.json unreadable — assuming clean build",
            evidence_paths=[_EVIDENCE_BUILD],
        )

    if manifest.get("source_dirty", False):
        return TrustSignal(
            status=TRUST_WARN,
            message="Package built from dirty working tree (--allow-dirty was passed). "
            "source_commit may not fully represent the deployed code.",
            evidence_paths=[_EVIDENCE_BUILD],
        )
    return TrustSignal(
        status=TRUST_PASS,
        message="Built from a clean working tree — source_commit is authoritative",
        evidence_paths=[_EVIDENCE_BUILD],
    )


def _token_resolution_signal(pkg_dir: str) -> TrustSignal:
    """Trust signal: did the token-resolution audit find anything dangerous?

    Reads ``context/ships.token_resolution.json``. Per spec §4:

    * **pass** — every env has 0 clobbers AND 0 undefined tokens AND 0
      rejected allow-list entries.
    * **warn** — clobber-free and no undefined/rejected, but at least one
      env has unused tokens or WARNING-class collisions (env-label /
      identity-alias).
    * **fail** — any env has ≥1 clobber, undefined tokens, or rejected
      allow-list entries.

    Absent artefact is UNKNOWN — the audit never ran (older packager
    versions, or a partial build). Promote when the artefact becomes
    universal.
    """
    from td_release_packager.token_resolution_artefact import load_artefact

    document = load_artefact(pkg_dir)
    if document is None:
        return TrustSignal(
            status=TRUST_UNKNOWN,
            message=(
                "context/ships.token_resolution.json not found — token-resolution "
                "audit did not run for this package. Rebuild with the current "
                "SHIPS version."
            ),
            evidence_paths=[_EVIDENCE_TOKEN_RESOLUTION],
        )

    envs = document.get("environments", []) or []
    if not envs:
        return TrustSignal(
            status=TRUST_PASS,
            message="No environments to audit (single-env package).",
            evidence_paths=[_EVIDENCE_TOKEN_RESOLUTION],
        )

    # Aggregate counters across every env in the package.
    total_clobbers = 0
    total_undefined: list[str] = []
    total_rejected = 0
    total_unused = 0
    warning_class_collisions = 0
    failing_envs: list[str] = []
    warning_envs: list[str] = []
    issues: list[str] = []

    for env in envs:
        name = env.get("env", "?")
        clobber_count = len(env.get("clobbers", []) or [])
        undefined = env.get("undefined", []) or []
        rejected = env.get("rejected_allowlist", []) or []
        unused = env.get("unused", []) or []
        # WARNING-class: env_label / identity-alias collisions (per spec §4).
        warn_classes = {"env_label", "alias"}
        warns = [
            c for c in env.get("collisions", []) or [] if c.get("class") in warn_classes
        ]

        env_has_fail = bool(clobber_count or undefined or rejected)
        env_has_warn = bool(unused or warns)

        if env_has_fail:
            failing_envs.append(name)
            total_clobbers += clobber_count
            total_undefined.extend(undefined)
            total_rejected += len(rejected)
            if clobber_count:
                issues.append(f"{name}: {clobber_count} object-identity clobber(s)")
            if undefined:
                issues.append(f"{name}: undefined token(s) {sorted(undefined)}")
            if rejected:
                issues.append(
                    f"{name}: {len(rejected)} allow-list entries rejected "
                    "(cannot suppress a clobber)"
                )
        elif env_has_warn:
            warning_envs.append(name)
            total_unused += len(unused)
            warning_class_collisions += len(warns)

    if failing_envs:
        return TrustSignal(
            status=TRUST_FAIL,
            message=(
                f"Token-resolution audit failed for {len(failing_envs)} env(s): "
                f"{sorted(set(failing_envs))}"
            ),
            issues=issues[:10],
            evidence_paths=[_EVIDENCE_TOKEN_RESOLUTION],
        )

    if warning_envs:
        bits: list[str] = []
        if total_unused:
            bits.append(f"{total_unused} unused token(s)")
        if warning_class_collisions:
            bits.append(f"{warning_class_collisions} benign warning-class collision(s)")
        return TrustSignal(
            status=TRUST_WARN,
            message=(
                "Token-resolution audit clean of clobbers but found "
                + ", ".join(bits)
                + f" across {sorted(set(warning_envs))}"
            ),
            evidence_paths=[_EVIDENCE_TOKEN_RESOLUTION],
        )

    return TrustSignal(
        status=TRUST_PASS,
        message="Token-resolution audit clean — no clobbers or undefined tokens",
        evidence_paths=[_EVIDENCE_TOKEN_RESOLUTION],
    )


# ---------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------


def _derive_status(signals: Dict[str, TrustSignal]) -> str:
    """Derive the top-level trust status from the signal set."""
    statuses = {sig.status for sig in signals.values()}
    if TRUST_FAIL in statuses:
        return STATUS_BLOCKED
    if TRUST_WARN in statuses or TRUST_UNKNOWN in statuses:
        return STATUS_CAVEATS
    return STATUS_READY


# ---------------------------------------------------------------
# Banner rendering (for CLI output)
# ---------------------------------------------------------------

_STATUS_ICON = {
    TRUST_PASS: "✓",
    TRUST_WARN: "⚠",
    TRUST_FAIL: "✗",
    TRUST_UNKNOWN: "?",
}

_HEADER_ICON = {
    STATUS_READY: "✓",
    STATUS_CAVEATS: "⚠",
    STATUS_BLOCKED: "✗",
}


def format_trust_banner(report: TrustReport, width: int = 64) -> str:
    """Return a formatted CLI banner string for the trust report."""
    bar = "=" * width
    header_icon = _HEADER_ICON.get(report.status, "?")
    lines = [
        f"\n{bar}",
        f"  Package Trust: {header_icon} {report.status}",
        bar,
    ]
    for name, sig in report.signals.items():
        icon = _STATUS_ICON.get(sig.status, "?")
        lines.append(f"  {icon} {name:<28} {sig.message}")
    lines.append(bar)
    return "\n".join(lines)
