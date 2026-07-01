"""Shared FixResult envelope used by every fixer.

Kept small on purpose — the same shape flows out of the CLI, out of the
MCP ``ships_fix`` tool, and (when #523 lands) into ``ships.decisions.json``
as the fix pipeline stage's decision record. Per-rule extras go under
``FixResultFile.details`` (per-file) and ``FixResult.totals`` (run-level)
rather than growing the top-level shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FixResultFile:
    """One file the fixer touched (or would touch under ``dry_run``).

    Attributes:
        file:    Path relative to the ``source_dir`` passed to the fixer.
        details: Rule-specific per-file counters. Convention: share keys
                 with the run-level :attr:`FixResult.totals` dict where
                 they mean the same thing (e.g. ``statements_fixed``),
                 plus any rule-specific structured extras (e.g. the
                 non-ASCII fixer records its per-codepoint substitution
                 map here).
    """

    file: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FixResult:
    """Envelope returned by every fixer.

    Attributes:
        rule_id:       Matches the ``rules_catalogue`` key the fixer targets.
        dry_run:       True when the fixer was invoked with ``dry_run=True``;
                       :attr:`files_changed` then reports what *would* change.
        files_scanned: Number of files the fixer looked at.
        files_changed: Files the fixer rewrote (or would rewrite under dry-run).
        totals:        Rule-specific aggregate counters
                       (e.g. ``{"statements_fixed": 4}``). Kept as a dict
                       rather than as fields so a new fixer does not force
                       a schema migration.
        errors:        Per-file errors surfaced during the run. Fixers
                       continue past errors and report them here rather
                       than aborting mid-walk (design decision — see the
                       #520 thread for the rationale).
    """

    rule_id: str
    dry_run: bool = False
    files_scanned: int = 0
    files_changed: list[FixResultFile] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def files_written(self) -> int:
        """Files actually written. Zero under ``dry_run`` regardless of matches."""
        return 0 if self.dry_run else len(self.files_changed)

    def to_dict(self) -> dict:
        """Stable JSON shape consumed by the CLI, MCP, and ``decisions.json``."""
        return {
            "rule_id": self.rule_id,
            "dry_run": self.dry_run,
            "files_scanned": self.files_scanned,
            "files_changed_count": len(self.files_changed),
            "files_written": self.files_written,
            "totals": dict(self.totals),
            "errors": list(self.errors),
            "files": [{"file": f.file, **f.details} for f in self.files_changed],
        }
