"""
perm_footprint.py — Allocated PERM-space footprint scan (#473).

Walks every ``CREATE DATABASE`` / ``CREATE USER`` file under a project's
``payload/database/pre-requisites/`` tree and returns the *minimum
permanent space* the deployment requires from each parent — i.e. the
floor the parent must have free before ``ships deploy`` can succeed.

Consumed by the pre-package pipeline report so an operator can see the
allocation requirement before building, without having to grep PERM
clauses by hand or wait for the env-prereqs DBA instructions inside
the built package.

Important: the figure is **allocation, not data size**. ``PERM = 1GB``
means the database can hold up to 1 GB; actual usage depends on what
gets loaded. Callers must label the figure accordingly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from td_release_packager.environment_prereqs import (
    _CREATE_PARENT_RE,
    _PERM_RE,
    _parse_perm_bytes,
)


_PERM_TOKEN_RE = re.compile(
    r"\bPERM\s*=\s*\{\{[A-Za-z_]\w*\}\}",
    re.IGNORECASE,
)


@dataclass
class DatabasePerm:
    """One CREATE DATABASE/USER's perm contribution."""

    child_name: str
    child_type: str  # "DATABASE" | "USER"
    parent_name: Optional[str]
    perm_bytes: int  # 0 when missing or tokenised
    tokenised_perm: bool  # True when PERM = {{TOKEN}}
    source_file: str  # payload-relative path


@dataclass
class ParentTotal:
    """Aggregated perm claim against one parent."""

    parent_name: str
    total_bytes: int
    child_count: int


@dataclass
class PermFootprint:
    """Pre-package perm-space rollup for a project's payload."""

    total_bytes: int = 0
    db_count: int = 0
    user_count: int = 0
    by_parent: List[ParentTotal] = field(default_factory=list)
    unresolved: List[DatabasePerm] = field(default_factory=list)
    per_database: List[DatabasePerm] = field(default_factory=list)


_PREREQ_SUBPATH = os.path.join("payload", "database", "pre-requisites")


def compute_perm_footprint(project_dir: str) -> PermFootprint:
    """Walk ``<project>/payload/database/pre-requisites/`` and aggregate.

    Empty / missing prereq tree returns an empty footprint — a project
    whose payload only contains DDL/DML and no CREATE DATABASE/USER
    files has no allocation claim of its own.

    Tokenised PERM values (``PERM = {{TOKEN}}``) are excluded from the
    rollup and listed under ``unresolved`` so the report can flag them
    separately. A missing PERM clause is treated as ``PERM = 0`` —
    Teradata's default — and counts cleanly in the total.
    """
    footprint = PermFootprint()

    root = os.path.join(project_dir, _PREREQ_SUBPATH)
    if not os.path.isdir(root):
        return footprint

    parent_totals: Dict[str, ParentTotal] = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in (".db", ".usr"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        match = _CREATE_PARENT_RE.search(text)
        # ``CREATE DATABASE x AS PERM = N;`` without FROM is also valid —
        # the database is then implicitly under whatever logged-in user
        # creates it. ``_CREATE_PARENT_RE`` requires a FROM, so fall
        # back to a looser match for the child name + perm here.
        child_name: Optional[str] = None
        child_type = "DATABASE" if suffix == ".db" else "USER"
        parent_name: Optional[str] = None
        if match is not None:
            child_type_kw = match.group(1).upper()
            if child_type_kw in ("DATABASE", "USER"):
                child_type = child_type_kw
            child_name = match.group(2).strip("\"'")
            parent_name = match.group(3).strip("\"'")

        # PERM value — distinguish literal from tokenised.
        perm_bytes = 0
        tokenised_perm = bool(_PERM_TOKEN_RE.search(text))
        if not tokenised_perm:
            perm_match = _PERM_RE.search(text)
            if perm_match is not None:
                perm_bytes = _parse_perm_bytes(perm_match.group(1), perm_match.group(2))

        # If we couldn't extract the child name from a CREATE header,
        # fall back to the filename stem so the breakdown still has
        # something to display.
        if not child_name:
            child_name = path.stem

        entry = DatabasePerm(
            child_name=child_name,
            child_type=child_type,
            parent_name=parent_name,
            perm_bytes=perm_bytes,
            tokenised_perm=tokenised_perm,
            source_file=os.path.relpath(path, project_dir).replace("\\", "/"),
        )
        footprint.per_database.append(entry)

        if child_type == "USER":
            footprint.user_count += 1
        else:
            footprint.db_count += 1

        if tokenised_perm:
            footprint.unresolved.append(entry)
            continue

        footprint.total_bytes += perm_bytes
        if parent_name:
            key = parent_name.upper()
            existing = parent_totals.get(key)
            if existing is None:
                parent_totals[key] = ParentTotal(
                    parent_name=parent_name,
                    total_bytes=perm_bytes,
                    child_count=1,
                )
            else:
                existing.total_bytes += perm_bytes
                existing.child_count += 1

    footprint.by_parent = sorted(
        parent_totals.values(), key=lambda p: (-p.total_bytes, p.parent_name)
    )
    return footprint
