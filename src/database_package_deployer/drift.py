"""
drift.py — Schema drift detection for SHIPS deployments.

Drift = a Teradata object was changed out-of-band (not by SHIPS) since
the last SHIPS deployment.  Detection works by comparing the current
SHOW output from the live database against a stored baseline — the SHOW
output captured immediately after SHIPS last deployed that object.

Why SHOW vs SHOW, not SHOW vs source DDL
-----------------------------------------
Teradata normalises DDL internally: it adds default properties (NO FALLBACK,
NO BEFORE JOURNAL, column compression markers, etc.) and reformats spacing
and capitalisation.  Comparing raw source DDL to SHOW output produces false
positives on almost every object.  Comparing SHOW-at-deploy-time to SHOW-now
compares two canonically formatted strings from the same Teradata system,
making the comparison meaningful and false-positive-free.

Baseline files
--------------
One file per deployed object, written/overwritten on every successful deploy:

    <baseline_dir>/<DatabaseName>.<ObjectName>.baseline

The content is the normalised SHOW output (stripped whitespace per line).
File count is bounded by the number of distinct objects in the payload —
the directory never grows with the number of runs.

Normalisation
-------------
Both the stored baseline and the live SHOW output pass through
``normalise_show`` before comparison.  This strips incidental whitespace
variance (trailing spaces, CR+LF line endings) while preserving all
structural differences that indicate real schema change.
"""

from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DriftResult:
    """Outcome of a single schema drift check.

    Attributes:
        detected:   True when the live SHOW output differs from the
                    stored baseline.
        diff_text:  Human-readable unified diff (empty when no drift).
        baseline:   The stored baseline text.
        current:    The live SHOW text used for comparison.
    """

    detected: bool
    diff_text: str = ""
    baseline: str = ""
    current: str = ""


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalise_show(text: str) -> str:
    """Normalise SHOW output for stable comparison.

    Strips trailing whitespace from every line and removes leading and
    trailing blank lines.  This eliminates incidental whitespace variance
    between Teradata versions and client drivers while preserving all
    structural differences that indicate real schema change.

    Args:
        text: Raw SHOW output from teradatasql.

    Returns:
        Normalised string suitable for equality comparison and diffing.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    # Drop leading blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    # Drop trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------


def baseline_path(baseline_dir: str, database_name: str, object_name: str) -> str:
    """Return the filesystem path for an object's baseline file.

    Args:
        baseline_dir:  Root directory for all baseline files.
        database_name: Resolved database name (e.g. ``OMR_STD``).
        object_name:   Object name (e.g. ``Customer``).

    Returns:
        Absolute path like ``<baseline_dir>/OMR_STD.Customer.baseline``.
    """
    filename = f"{database_name}.{object_name}.baseline"
    return os.path.join(baseline_dir, filename)


def read_baseline(
    baseline_dir: str, database_name: str, object_name: str
) -> str | None:
    """Read the stored baseline for an object.

    Args:
        baseline_dir:  Root directory for all baseline files.
        database_name: Resolved database name.
        object_name:   Object name.

    Returns:
        Normalised baseline text, or ``None`` if no baseline exists.
    """
    path = baseline_path(baseline_dir, database_name, object_name)
    if not os.path.isfile(path):
        return None
    try:
        return open(path, encoding="utf-8").read()
    except OSError as exc:
        logger.debug("drift: could not read baseline %s: %s", path, exc)
        return None


def write_baseline(
    baseline_dir: str,
    database_name: str,
    object_name: str,
    show_text: str,
) -> None:
    """Write (or overwrite) the baseline for an object.

    Called after every successful deploy.  Rolling horizon: only the
    most recent deploy's SHOW output is kept.

    Args:
        baseline_dir:  Root directory for all baseline files.
        database_name: Resolved database name.
        object_name:   Object name.
        show_text:     Raw SHOW output to store (will be normalised).
    """
    path = baseline_path(baseline_dir, database_name, object_name)
    try:
        os.makedirs(baseline_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(normalise_show(show_text))
        logger.debug("drift: baseline written → %s", path)
    except OSError as exc:
        logger.warning("drift: could not write baseline %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Drift check
# ---------------------------------------------------------------------------


def check_drift(
    baseline_dir: str,
    database_name: str,
    object_name: str,
    current_show: str,
) -> DriftResult:
    """Compare live SHOW output against the stored baseline.

    Args:
        baseline_dir:  Root directory for all baseline files.
        database_name: Resolved database name.
        object_name:   Object name.
        current_show:  Raw SHOW output just retrieved from Teradata.

    Returns:
        ``DriftResult(detected=False)`` when no baseline exists (first
        deploy — nothing to compare against) or when the outputs match.
        ``DriftResult(detected=True, diff_text=...)`` when drift is found.
    """
    stored = read_baseline(baseline_dir, database_name, object_name)
    if stored is None:
        return DriftResult(detected=False)

    normalised_current = normalise_show(current_show)

    if stored == normalised_current:
        return DriftResult(detected=False, baseline=stored, current=normalised_current)

    diff_lines = list(
        difflib.unified_diff(
            stored.splitlines(keepends=True),
            normalised_current.splitlines(keepends=True),
            fromfile=f"{database_name}.{object_name} (last SHIPS deploy)",
            tofile=f"{database_name}.{object_name} (current database)",
            lineterm="",
        )
    )
    diff_text = "".join(diff_lines)
    return DriftResult(
        detected=True,
        diff_text=diff_text,
        baseline=stored,
        current=normalised_current,
    )
