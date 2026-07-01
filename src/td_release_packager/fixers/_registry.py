"""Fix registry — one entry per rule that has an automated fix.

The registry is the single source of truth consumed by both the CLI
(``ships fix``, ``ships inspect``'s legacy flags, ``ships process``'s
fix stage) and the MCP server (``ships_fix``, ``ships_list_fixable_rules``).
A lockstep test (``test_fixers_registry_catalogue_lockstep.py``) asserts
every registered fixer maps to a rules-catalogue entry with
``safe_fix_available=True``, so the two cannot silently drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from td_release_packager.fixers._result import FixResult

_ALLOWED_WRITE_SCOPES = frozenset({"payload", "config"})


@dataclass(frozen=True)
class FixerSpec:
    """Registry entry for one fixer.

    Attributes:
        rule_id:     Matches a key in ``rules_catalogue._RULES`` with
                     ``safe_fix_available=True``. The lockstep test rejects
                     a mismatch.
        apply:       Signature ``apply(source_dir: str, dry_run: bool) -> FixResult``.
                     Fixers MUST:

                     - be idempotent (a second run on a clean tree reports
                       ``files_changed=[]``);
                     - never abort the walk on a single-file error — record
                       the error under :attr:`FixResult.errors` and continue;
                     - honour ``dry_run`` strictly (no writes when True).
        default_on:  True when ``ships fix`` (invoked with no ``--rules``
                     / ``--all`` flag) should apply this fixer. Set False
                     for opt-in-only fixers whose fix carries operator-
                     review cost (e.g. ``non_ascii`` where substitutions
                     produce diff noise a reviewer wants to see explicitly
                     before deploy).
        write_scope: Which tree the fixer mutates. ``"payload"`` (default)
                     rewrites files under ``payload/``; ``"config"``
                     rewrites files under ``config/`` (e.g. the upcoming
                     ``type_suffix`` fixer rewrites ``config/token_map.conf``).
                     Declared so future tooling can restrict which scopes
                     ``ships fix`` may touch in a given run.
    """

    rule_id: str
    apply: Callable[[str, bool], FixResult]
    default_on: bool
    write_scope: str = "payload"

    def __post_init__(self) -> None:
        if self.write_scope not in _ALLOWED_WRITE_SCOPES:
            raise ValueError(
                f"FixerSpec {self.rule_id!r} has unknown write_scope "
                f"{self.write_scope!r} (allowed: {sorted(_ALLOWED_WRITE_SCOPES)})"
            )


FIX_REGISTRY: dict[str, FixerSpec] = {}


def register(spec: FixerSpec) -> FixerSpec:
    """Register a :class:`FixerSpec`.

    Idempotent for the exact same spec (safe under module re-imports in
    test fixtures). Rejects a rule-id collision with a *different* apply
    callable — that would silently swap fixer behaviour depending on
    import order.

    Called from each fixer module at import time.
    """
    existing = FIX_REGISTRY.get(spec.rule_id)
    if existing is not None:
        same = (
            existing.rule_id == spec.rule_id
            and existing.apply is spec.apply
            and existing.default_on == spec.default_on
            and existing.write_scope == spec.write_scope
        )
        if same:
            return existing
        raise ValueError(
            f"fixer for rule {spec.rule_id!r} already registered with a "
            f"different implementation — check for a duplicate register() call"
        )
    FIX_REGISTRY[spec.rule_id] = spec
    return spec


def registered_fixers() -> list[FixerSpec]:
    """Every registered :class:`FixerSpec` in rule-id order."""
    return [FIX_REGISTRY[rid] for rid in sorted(FIX_REGISTRY)]


def default_on_rules() -> list[str]:
    """Rule ids whose fixers run under a bare ``ships fix`` (no ``--rules`` / ``--all``)."""
    return sorted(rid for rid, spec in FIX_REGISTRY.items() if spec.default_on)
