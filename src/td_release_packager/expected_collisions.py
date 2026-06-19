"""
expected_collisions — operator allow-list for benign token collisions.

The collision audit raises every value-level token collision it finds. Most
of those are benign by design (env labels in an AGNOSTIC config, scalar
attribute pairs like ``PERM_SPACE == SPOOL_SPACE``). After the operator has
*seen* a benign pair, they should be able to record "yes, intentional" once
and stop getting nagged on every subsequent inspect run — without weakening
the dangerous check.

This module loads and applies ``config/expected_collisions.yaml``.

Format::

    expected:
      - tokens: [PERM_SPACE, SPOOL_SPACE]
        reason: "Both default to 1e9; interchangeable scalars."
      - tokens: [ENV_PREFIX, SHIPS_ENV]
        reason: "Env-label roots; identical in AGNOSTIC by design."

Each entry suppresses a collision whose token set is a **subset** of
``tokens`` — i.e. ``[A, B]`` entry matches both ``[A, B]`` and ``[A, B, C]``
when ``C`` is itself benign — and records the rationale in
``ships.decisions.json``.

**Safety invariant.** The allow-list may only downgrade a collision the
audit has already classified as benign (``SCALAR``, ``ENV_LABEL``, or
identity ``ALIAS``). An entry that names tokens whose audit-determined class
is ``REAL`` (an object-identity clobber) is refused: the original ERROR
finding stays, and a *separate* finding ``collision_allowlist_rejected`` is
emitted explaining why the suppression was denied. This stops the allow-list
becoming a footgun where an operator could mask a deploy-time clobber with
a yaml edit.

The classification step runs first; allow-list application is the second
pass. There is no way to reorder them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Sequence, Tuple

import yaml

from td_release_packager.token_audit import (
    CollisionClass,
    CollisionGroup,
    ResolutionReport,
)


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowlistEntry:
    """One operator-recorded benign collision.

    Attributes:
        tokens: token set the entry covers. The allow-list matches a
            collision group when ``collision.tokens`` is a subset of this set.
            Stored sorted so structural equality across YAML and Python is
            stable.
        reason: free-text rationale, written to ships.decisions.json so the
            allow-list is auditable.
        source_lineno: 1-based line number in the YAML source (for error
            messages). 0 when the entry was constructed in-memory.
    """

    tokens: Tuple[str, ...]
    reason: str
    source_lineno: int = 0

    def covers(self, collision_tokens: Sequence[str]) -> bool:
        """True when every token in the collision is in this entry's set."""
        return set(collision_tokens).issubset(set(self.tokens))


@dataclass(frozen=True)
class RejectedEntry:
    """An allow-list entry that the audit refused to honour.

    Emitted when the entry attempted to cover a REAL clobber. Becomes a
    ``collision_allowlist_rejected`` finding so the operator sees an
    explicit "your suppression was denied" rather than silently keeping
    the ERROR.
    """

    entry: AllowlistEntry
    real_collision_value: str
    real_collision_tokens: Tuple[str, ...]


@dataclass
class Allowlist:
    """Parsed allow-list document."""

    entries: Tuple[AllowlistEntry, ...] = ()
    source_path: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.entries


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class AllowlistParseError(ValueError):
    """Raised for syntactically malformed expected_collisions.yaml."""


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------


def parse_allowlist(content: str, *, source_path: str | None = None) -> Allowlist:
    """Parse expected_collisions.yaml content into an Allowlist.

    Args:
        content: yaml text.
        source_path: optional path for error messages.

    Raises:
        AllowlistParseError: on structural problems (not a dict, missing
            ``expected`` key with a list value, entries missing ``tokens``,
            duplicate token sets across entries, etc.).
    """
    try:
        data = yaml.safe_load(content) if content else None
    except yaml.YAMLError as e:
        raise AllowlistParseError(f"invalid YAML: {e}") from e

    if data is None:
        return Allowlist(entries=(), source_path=source_path)
    if not isinstance(data, dict):
        raise AllowlistParseError(
            "expected_collisions.yaml must be a mapping with an 'expected' key"
        )
    raw_entries = data.get("expected", [])
    if raw_entries is None:
        return Allowlist(entries=(), source_path=source_path)
    if not isinstance(raw_entries, list):
        raise AllowlistParseError("'expected' must be a list of entries")

    seen_token_sets: set[frozenset[str]] = set()
    entries: list[AllowlistEntry] = []
    for idx, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict):
            raise AllowlistParseError(f"entry #{idx} is not a mapping")
        tokens = raw.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise AllowlistParseError(
                f"entry #{idx}: 'tokens' must be a non-empty list"
            )
        if not all(isinstance(t, str) and t for t in tokens):
            raise AllowlistParseError(
                f"entry #{idx}: every token must be a non-empty string"
            )
        if len(tokens) < 2:
            raise AllowlistParseError(
                f"entry #{idx}: at least two tokens are required for a collision"
            )
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise AllowlistParseError(f"entry #{idx}: 'reason' must be a string")
        token_set = frozenset(tokens)
        if token_set in seen_token_sets:
            raise AllowlistParseError(
                f"entry #{idx}: token set {sorted(token_set)} is duplicated"
            )
        seen_token_sets.add(token_set)
        entries.append(
            AllowlistEntry(
                tokens=tuple(sorted(tokens)),
                reason=reason.strip(),
                source_lineno=idx,
            )
        )
    return Allowlist(entries=tuple(entries), source_path=source_path)


def load_allowlist(path: str) -> Allowlist:
    """Load an allow-list from disk. Returns an empty Allowlist when absent.

    The "no file" case is the common one for projects without operator-
    recorded suppressions; treat it as "nothing to allow-list" rather than
    an error.
    """
    if not os.path.exists(path):
        return Allowlist(entries=(), source_path=path)
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    return parse_allowlist(content, source_path=path)


def default_allowlist_path(project_dir: str) -> str:
    """Conventional path: ``<project>/config/expected_collisions.yaml``."""
    return os.path.join(project_dir, "config", "expected_collisions.yaml")


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


# Classes the allow-list IS permitted to downgrade. REAL is explicitly absent
# by safety invariant.
_DOWNGRADABLE: frozenset[CollisionClass] = frozenset(
    {
        CollisionClass.SCALAR,
        CollisionClass.ENV_LABEL,
        CollisionClass.ALIAS,
        CollisionClass.MIXED,
    }
)


def apply_allowlist(
    collisions: Sequence[CollisionGroup],
    allowlist: Allowlist,
) -> Tuple[Tuple[CollisionGroup, ...], Tuple[RejectedEntry, ...]]:
    """Apply ``allowlist`` to ``collisions``.

    For every collision group:
    * If an entry covers the group's tokens AND the group's current class is
      downgradable, the group's class becomes ``ALLOWLISTED``.
    * If an entry covers the group's tokens BUT the group is ``REAL``, the
      group is left ERROR and a ``RejectedEntry`` is recorded so the
      operator sees an explicit refusal.
    * Otherwise the group is unchanged.

    Returns:
        ``(updated_collisions, rejected_entries)``.
    """
    updated: list[CollisionGroup] = []
    rejected: list[RejectedEntry] = []

    for group in collisions:
        # First matching entry wins. Entry iteration order is YAML order so
        # the first author-recorded reason is what gets attributed.
        match = next(
            (e for e in allowlist.entries if e.covers(group.tokens)),
            None,
        )
        if match is None:
            updated.append(group)
            continue
        if group.classification is CollisionClass.REAL:
            # Safety invariant: refuse to suppress a real clobber.
            rejected.append(
                RejectedEntry(
                    entry=match,
                    real_collision_value=group.value,
                    real_collision_tokens=group.tokens,
                )
            )
            updated.append(group)  # unchanged — keep the ERROR
            continue
        if group.classification in _DOWNGRADABLE:
            updated.append(
                CollisionGroup(
                    value=group.value,
                    tokens=group.tokens,
                    classification=CollisionClass.ALLOWLISTED,
                )
            )
            continue
        updated.append(group)

    return tuple(updated), tuple(rejected)


def apply_to_report(
    report: ResolutionReport,
    allowlist: Allowlist,
) -> Tuple[ResolutionReport, Tuple[RejectedEntry, ...]]:
    """Return a new ResolutionReport with allow-list applied.

    The input ``report`` is not mutated. Caller chains this directly after
    ``audit_resolution`` when an allow-list file exists for the project.
    """
    updated_collisions, rejected = apply_allowlist(report.collisions, allowlist)
    new_report = ResolutionReport(
        env=report.env,
        clobbers=report.clobbers,
        collisions=updated_collisions,
        roles=report.roles,
        defined_count=report.defined_count,
        undefined=report.undefined,
        unused=report.unused,
        empty=report.empty,
    )
    return new_report, rejected
