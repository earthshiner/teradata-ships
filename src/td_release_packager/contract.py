"""
contract.py — object contract extraction + baseline diff (issue #171).

A SHIPS object forms a *contract* with its downstream consumers:

    * a VIEW      — its ordered column list,
    * a PROCEDURE — its ordered parameters (name, direction, datatype),
    * a TABLE     — its ordered columns (name, datatype).

This module extracts those contracts from payload DDL and diffs the current
source against a captured **baseline** (the last released contracts) to flag
backward-incompatible changes — removed/renamed/reordered view columns,
changed procedure parameters, dropped/retyped table columns, or an object
that has disappeared (a rename or drop).

Findings are returned as ``validate.ValidationIssue`` (rule
``contract_change``) so they flow through the normal inspect surfaces. The
baseline is captured with ``build_contracts`` + ``write_baseline`` (driven by
``inspect --update-contract-baseline``) and lives under ``.ships/``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from td_release_packager.validate import ValidationIssue

BASELINE_SCHEMA_VERSION = "1.0"
RULE_NAME = "contract_change"

# CREATE/REPLACE <kind> <db.obj> — captures the kind and qualified name. The
# body (column / parameter list) is located positionally after the match.
_OBJECT_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?P<kind>VIEW|PROCEDURE|TABLE)\s+"
    r"(?P<qualified>\"?[\w{}]+\"?(?:\s*\.\s*\"?[\w{}]+\"?)?)",
    re.IGNORECASE,
)
_PARAM_DIRECTIONS = {"IN", "OUT", "INOUT"}
# Entries in a CREATE TABLE body that are table-level constraints, not columns.
_TABLE_CONSTRAINT_LEADERS = {
    "PRIMARY",
    "UNIQUE",
    "CONSTRAINT",
    "CHECK",
    "FOREIGN",
    "INDEX",
    "PARTITION",
    "REFERENCES",
    "NO",
}


def _strip_comments(text: str) -> str:
    from td_release_packager.sql_text import strip_comments_and_string_literals

    # Keep string literals (column defaults etc. are not needed for the
    # contract); stripping comments is enough and preserves positions.
    return re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL))


def _first_paren_body(text: str, start: int) -> Optional[str]:
    """Return the contents of the first balanced ``(...)`` at/after ``start``."""
    open_idx = text.find("(", start)
    if open_idx == -1:
        return None
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return None


def _split_top_level_commas(body: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    cur = []
    for c in body:
        if c == "(":
            depth += 1
            cur.append(c)
        elif c == ")":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _norm_name(tok: str) -> str:
    return tok.strip().strip('"').lower()


def _norm_type(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).upper()


def extract_contract(content: str) -> Optional[dict]:
    """Extract the contract of the single object defined in ``content``.

    Returns ``{"kind", "qualified", "items"}`` or ``None`` when the file is
    not a VIEW/PROCEDURE/TABLE or its contract cannot be parsed (e.g. a view
    with no explicit column list — nothing to compare).
    """
    clean = _strip_comments(content)
    m = _OBJECT_RE.search(clean)
    if not m:
        return None
    kind = m.group("kind").upper()
    qualified = re.sub(r"\s+", "", m.group("qualified")).strip('"').lower()

    if kind == "VIEW":
        # Only an explicit column list (before AS) forms a comparable contract.
        as_idx = re.search(r"\bAS\b", clean[m.end() :], re.IGNORECASE)
        head = clean[m.end() : m.end() + as_idx.start()] if as_idx else clean[m.end() :]
        if "(" not in head:
            return None
        body = _first_paren_body(clean, m.end())
        if body is None:
            return None
        cols = [
            _norm_name(p.split()[0]) for p in _split_top_level_commas(body) if p.split()
        ]
        if not cols:
            return None
        return {"kind": kind, "qualified": qualified, "items": cols}

    if kind == "PROCEDURE":
        body = _first_paren_body(clean, m.end())
        params = []
        for entry in _split_top_level_commas(body or ""):
            toks = entry.split()
            if not toks:
                continue
            if toks[0].upper() in _PARAM_DIRECTIONS:
                direction = toks[0].upper()
                rest = toks[1:]
            else:
                direction = "IN"
                rest = toks
            if not rest:
                continue
            name = _norm_name(rest[0])
            dtype = _norm_type(" ".join(rest[1:]))
            params.append({"name": name, "direction": direction, "type": dtype})
        return {"kind": kind, "qualified": qualified, "items": params}

    # TABLE
    body = _first_paren_body(clean, m.end())
    if body is None:
        return None
    cols = []
    for entry in _split_top_level_commas(body):
        toks = entry.split()
        if not toks or toks[0].upper() in _TABLE_CONSTRAINT_LEADERS:
            continue
        name = _norm_name(toks[0])
        dtype = _norm_type(" ".join(toks[1:]))
        cols.append({"name": name, "type": dtype})
    return {"kind": kind, "qualified": qualified, "items": cols}


def build_contracts(payload_dir: str) -> Dict[str, dict]:
    """Walk ``payload_dir`` and return ``{qualified_name: contract}``."""
    contracts: Dict[str, dict] = {}
    for root, _dirs, files in os.walk(payload_dir):
        for f in sorted(files):
            if f.startswith(".") or f.startswith("_"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            contract = extract_contract(content)
            if contract:
                contracts[contract["qualified"]] = contract
    return contracts


def write_baseline(path: str, contracts: Dict[str, dict]) -> None:
    """Persist ``contracts`` as the baseline document at ``path``."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    doc = {"schema_version": BASELINE_SCHEMA_VERSION, "contracts": contracts}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_baseline(path: str) -> Optional[Dict[str, dict]]:
    """Load the baseline ``{qualified_name: contract}``, or None if absent."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc.get("contracts", {}) if isinstance(doc, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _diff_view(base: List[str], cur: List[str]) -> List[str]:
    changes = []
    cur_set = set(cur)
    for col in base:
        if col not in cur_set:
            changes.append(f"view column '{col}' was removed (or renamed)")
    common = [c for c in base if c in cur_set]
    cur_common = [c for c in cur if c in set(base)]
    if common != cur_common:
        changes.append("view columns were reordered")
    return changes


def _diff_procedure(base: List[dict], cur: List[dict]) -> List[str]:
    changes = []
    cur_by_name = {p["name"]: p for p in cur}
    for p in base:
        c = cur_by_name.get(p["name"])
        if c is None:
            changes.append(
                f"procedure parameter '{p['name']}' was removed (or renamed)"
            )
            continue
        if c["direction"] != p["direction"]:
            changes.append(
                f"procedure parameter '{p['name']}' direction changed "
                f"({p['direction']} → {c['direction']})"
            )
        if c["type"] != p["type"]:
            changes.append(
                f"procedure parameter '{p['name']}' datatype changed "
                f"({p['type']} → {c['type']})"
            )
    return changes


def _diff_table(base: List[dict], cur: List[dict]) -> List[str]:
    changes = []
    cur_by_name = {c["name"]: c for c in cur}
    for col in base:
        c = cur_by_name.get(col["name"])
        if c is None:
            changes.append(f"table column '{col['name']}' was dropped (or renamed)")
            continue
        if c["type"] != col["type"]:
            changes.append(
                f"table column '{col['name']}' datatype changed "
                f"({col['type']} → {c['type']})"
            )
    return changes


_DIFFERS = {"VIEW": _diff_view, "PROCEDURE": _diff_procedure, "TABLE": _diff_table}


def diff_contracts(baseline: Dict[str, dict], current: Dict[str, dict]) -> List[dict]:
    """Return a list of backward-incompatible changes vs the baseline.

    Each change is ``{qualified, kind, detail}``. Added objects and added
    columns/parameters are backward-compatible and are NOT reported.
    """
    changes: List[dict] = []
    for qualified, base in sorted(baseline.items()):
        cur = current.get(qualified)
        if cur is None:
            changes.append(
                {
                    "qualified": qualified,
                    "kind": base.get("kind", ""),
                    "detail": "object is no longer defined in source "
                    "(dropped or renamed)",
                }
            )
            continue
        if cur.get("kind") != base.get("kind"):
            changes.append(
                {
                    "qualified": qualified,
                    "kind": base.get("kind", ""),
                    "detail": f"object kind changed ({base.get('kind')} → "
                    f"{cur.get('kind')})",
                }
            )
            continue
        differ = _DIFFERS.get(base.get("kind", ""))
        if differ is None:
            continue
        for detail in differ(base.get("items", []), cur.get("items", [])):
            changes.append(
                {"qualified": qualified, "kind": base["kind"], "detail": detail}
            )
    return changes


def check_contract_changes(
    project_dir: str, payload_dir: str, severity: str = "WARNING"
) -> List[ValidationIssue]:
    """Compare current payload contracts against the baseline (issue #171).

    No-op when no baseline has been captured. Returns one ``ValidationIssue``
    per backward-incompatible change, rule ``contract_change``.
    """
    from td_release_packager.project_paths import contracts_baseline_path

    baseline = load_baseline(contracts_baseline_path(project_dir))
    if not baseline:
        return []

    current = build_contracts(payload_dir)
    issues: List[ValidationIssue] = []
    for change in diff_contracts(baseline, current):
        kind = (change["kind"] or "object").lower()
        issues.append(
            ValidationIssue(
                file=change["qualified"],
                rule=RULE_NAME,
                severity=severity,
                message=(
                    f"Backward-incompatible {kind} contract change on "
                    f"{change['qualified']}: {change['detail']}. This can break "
                    f"downstream consumers."
                ),
                remediation={
                    "safe_fix_available": False,
                    "automation_level": "manual_review_required",
                    "requires_human_review": True,
                    "change_kind": f"{kind}_contract_change",
                    "recommended_action": (
                        "Confirm no downstream consumer depends on the previous "
                        "contract, or version the object. Re-baseline with "
                        "`ships inspect --update-contract-baseline` once the "
                        "change is approved."
                    ),
                },
            )
        )
    return issues
