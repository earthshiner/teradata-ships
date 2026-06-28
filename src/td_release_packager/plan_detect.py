"""
plan_detect.py — auto-answer detectable packaging questions (issue #379).

Inspects a raw source DDL tree and pre-fills the ``decision-tree.yaml`` answers
that can be read straight off the files, so ``ships plan`` recommends a command
sequence without asking the user about facts the source already reveals:

    * ``source.type`` / ``source.dir`` — a filesystem path was given.
    * ``tokens.already``  — does any file contain ``{{TOKEN}}`` placeholders?
    * ``atomic.eponymous`` — is every DDL file a single object (atomic)?

Detection is conservative and read-only: ambiguous signals stay unanswered (the
caller falls back to the model defaults) and every decision is reported with its
evidence so the recommendation is auditable. SQL is scanned as text, never run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

#: A {{TOKEN}} placeholder.
_TOKEN_RE = re.compile(r"\{\{[^}]+\}\}")

#: A top-level CREATE/REPLACE of a deployable object. Counting these per file
#: tells us whether a file is atomic (exactly one) or compound (more than one).
_CREATE_RE = re.compile(
    r"(?im)^[ \t]*(?:CREATE|REPLACE)\s+"
    r"(?:MULTISET\s+|SET\s+|VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|DATABASE|USER|ROLE)\b"
)

_GRANT_RE = re.compile(r"(?im)^[ \t]*(?:GRANT|REVOKE)\b")
_DML_RE = re.compile(r"(?im)^[ \t]*(?:INSERT|UPDATE|DELETE|MERGE)\b")

#: Extensions worth scanning for DDL/object signals.
_DDL_EXTS = {
    ".sql",
    ".ddl",
    ".tbl",
    ".viw",
    ".view",
    ".prc",
    ".proc",
    ".mac",
    ".macro",
    ".fnc",
    ".func",
    ".trg",
    ".trigger",
    ".grt",
}


@dataclass
class Detection:
    """Auto-detected answers plus the evidence behind each one."""

    answers: Dict[str, Any] = field(default_factory=dict)
    findings: List[str] = field(default_factory=list)


def _iter_source_files(source_dir: str) -> List[str]:
    out: List[str] = []
    for root, _dirs, files in os.walk(source_dir):
        for f in sorted(files):
            if f.startswith(".") or f.startswith("_"):
                continue
            if os.path.splitext(f)[1].lower() in _DDL_EXTS:
                out.append(os.path.join(root, f))
    return out


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def detect_answers(source_dir: str) -> Detection:
    """Inspect ``source_dir`` and return auto-filled answers + findings."""
    det = Detection()
    if not os.path.isdir(source_dir):
        det.findings.append(f"source not found: {source_dir} — no detection performed")
        return det

    det.answers["source.type"] = "filesystem"
    det.answers["source.dir"] = source_dir

    files = _iter_source_files(source_dir)
    if not files:
        det.findings.append(
            "No DDL-like files found under the source — leaving content "
            "questions for the model defaults."
        )
        return det

    tokenised = 0
    compound: List[str] = []
    has_dcl = False
    has_dml = False

    for path in files:
        content = _read(path)
        if _TOKEN_RE.search(content):
            tokenised += 1
        if len(_CREATE_RE.findall(content)) > 1:
            compound.append(os.path.relpath(path, source_dir))
        if _GRANT_RE.search(content):
            has_dcl = True
        if _DML_RE.search(content):
            has_dml = True

    # -- tokens.already --
    if tokenised:
        det.answers["tokens.already"] = "yes"
        det.findings.append(
            f"{tokenised}/{len(files)} file(s) contain {{{{TOKEN}}}} placeholders "
            "-> tokens.already = yes"
        )
    else:
        det.answers["tokens.already"] = "no"
        det.findings.append(
            "No {{TOKEN}} placeholders found -> tokens.already = no (the plan "
            "will configure tokenisation)"
        )

    # -- atomic.eponymous --
    if compound:
        det.answers["atomic.eponymous"] = "no"
        sample = ", ".join(compound[:3]) + (" …" if len(compound) > 3 else "")
        det.findings.append(
            f"{len(compound)} file(s) hold multiple objects ({sample}) -> "
            "atomic.eponymous = no (SHIPS will auto-split them)"
        )
    else:
        det.answers["atomic.eponymous"] = "yes"
        det.findings.append(
            "Every DDL file holds a single object -> atomic.eponymous = yes"
        )

    # -- informational (no question id, but useful context) --
    if has_dcl:
        det.findings.append("GRANT/REVOKE statements present -> DCL detected.")
    if has_dml:
        det.findings.append("INSERT/UPDATE/DELETE/MERGE present -> DML detected.")

    return det


def merge_answers(
    detected: Dict[str, Any], overrides: Dict[str, Any]
) -> Dict[str, Any]:
    """Overlay user ``overrides`` on top of ``detected`` (overrides win)."""
    merged = dict(detected)
    for k, v in overrides.items():
        if v not in (None, ""):
            merged[k] = v
    return merged
