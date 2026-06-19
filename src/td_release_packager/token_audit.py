"""
token_audit — per-environment resolved-object-identity collision audit.

The pre-existing tokenisation report flags any two tokens whose resolved
values are equal. That's a values-only test; it has no notion of whether the
colliding tokens were *meant* to be distinct, so it cannot distinguish a
dangerous clobber from a benign env-label or scalar match.

This module reframes the check:

* **Primary signal — resolved-object-identity clobber.** Walk the payload,
  resolve each logical source object's physical name (database segment +
  object name), and group by physical name case-insensitively. Two or more
  *distinct* logical sources sharing one physical name is a deploy-time
  clobber: two things would merge onto one name. This is the only condition
  that should block packaging.

* **Secondary signal — value-level token collision, classified by role.**
  Tokens with the same resolved value, classified by the highest role
  appearing among members (IDENTITY > SCALAR > ENV_LABEL). This produces the
  Class column in the report and feeds the inspect-rule severity dispatch.

Roles come from :mod:`token_roles`; resolved physical names come from the
shared parser in :mod:`tokenised_name`.

The audit is pure: no I/O beyond the optional ``audit_project`` helper that
walks a directory and wraps the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Tuple

from td_release_packager.token_roles import (
    Role,
    RoleAssignment,
    classify_token_roles,
)
from td_release_packager.tokenised_name import (
    QualifiedName,
    TokenisedNameError,
    parse_qualified_name,
)


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------


class CollisionClass(str, Enum):
    """Classification of a value-level token collision.

    Severity ordering mirrors :class:`Role` precedence; ``REAL`` is the only
    class that defaults to ERROR severity. Stored as strings for JSON safety.
    """

    REAL = "real"  # ≥2 IDENTITY tokens naming disjoint objects → clobber
    ALIAS = "alias"  # ≥2 IDENTITY tokens naming the same object → DRY candidate
    ENV_LABEL = "env_label"  # all members ENV_LABEL
    SCALAR = "scalar"  # all members SCALAR
    MIXED = "mixed"  # members span roles in a way the rules above don't cover
    ALLOWLISTED = "allowlisted"  # operator-confirmed benign (wired in step c)


@dataclass(frozen=True)
class LogicalObject:
    """One source object in the payload.

    ``source_id`` uniquely identifies the source (typically the payload-
    relative file path). ``name`` is the parsed object name AST. ``database``
    and ``object`` are convenience accessors derived from ``name``.
    """

    source_id: str
    name: QualifiedName

    @property
    def database(self):
        return self.name.database

    @property
    def object(self):
        return self.name.object


@dataclass(frozen=True)
class Clobber:
    """Two or more distinct logical objects sharing one physical name."""

    physical_name: str  # already case-normalised
    sources: Tuple[str, ...]  # source_ids, sorted
    tokens: Tuple[str, ...]  # IDENTITY tokens composing the colliding names


@dataclass(frozen=True)
class CollisionGroup:
    """A value-level collision: ≥2 tokens resolving to one value."""

    value: str
    tokens: Tuple[str, ...]  # sorted
    classification: CollisionClass


@dataclass
class ResolutionReport:
    """Per-environment audit result.

    Attributes:
        env: environment name (e.g. ``DEV``).
        clobbers: object-identity clobbers (the dangerous condition).
        collisions: value-level token collisions, classified.
        roles: token → role assignment used for the classification (handy for
            UIs and debugging; not part of the gate decision).
        defined_count, undefined, unused, empty: pass-through of the existing
            tokenisation report's per-env counters, so the report can keep
            its current matrix shape.
    """

    env: str
    clobbers: Tuple[Clobber, ...] = ()
    collisions: Tuple[CollisionGroup, ...] = ()
    roles: Mapping[str, RoleAssignment] = field(default_factory=dict)
    defined_count: int = 0
    undefined: Tuple[str, ...] = ()
    unused: Tuple[str, ...] = ()
    empty: Tuple[str, ...] = ()

    @property
    def real_collisions(self) -> Tuple[CollisionGroup, ...]:
        """Subset that should block packaging by default."""
        return tuple(
            c for c in self.collisions if c.classification == CollisionClass.REAL
        )

    @property
    def benign_collisions(self) -> Tuple[CollisionGroup, ...]:
        return tuple(
            c
            for c in self.collisions
            if c.classification
            in (
                CollisionClass.ENV_LABEL,
                CollisionClass.SCALAR,
                CollisionClass.ALLOWLISTED,
            )
        )


# --------------------------------------------------------------------------
# SHIPS filename convention
# --------------------------------------------------------------------------


# Filenames follow ``<db_segment>.<object_segment>.<ext>``. The db segment may
# itself be tokenised (``{{DB_PREFIX}}_SEM_STD``). Object names may also be
# tokenised in whole-name token-map deployments.
_FILENAME_DB_OBJ = re.compile(r"^(?P<db>[^.]+)\.(?P<obj>[^.]+)\.[^.]+$")


def _parse_payload_filename(filename: str) -> QualifiedName | None:
    """Extract the logical object name from a SHIPS payload filename.

    Returns ``None`` if the filename does not match the ``db.obj.ext`` shape
    (scaffolding files, README, etc.).
    """
    name = Path(filename).name
    match = _FILENAME_DB_OBJ.match(name)
    if not match:
        return None
    try:
        return parse_qualified_name(f"{match['db']}.{match['obj']}")
    except TokenisedNameError:
        return None


# --------------------------------------------------------------------------
# Resolved-object-identity grouping
# --------------------------------------------------------------------------


def _normalise_physical(name: str) -> str:
    """Case-fold a resolved physical name for collision comparison.

    Teradata identifiers are case-insensitive by default. Quoted identifiers
    are preserved as-is; downstream rendering already strips the quotes when
    parsing returned them as quoted, so by the time we reach this function
    everything is bare text.
    """
    return name.casefold()


def detect_clobbers(
    objects: Sequence[LogicalObject],
    env: Mapping[str, str],
    *,
    roles: Mapping[str, RoleAssignment] | None = None,
) -> Tuple[Clobber, ...]:
    """Group objects by resolved physical name; emit clobbers.

    Args:
        objects: every LogicalObject the audit knows about (one per
            payload file matching the SHIPS naming convention).
        env: env-config token → value mapping for this environment.
        roles: optional precomputed role map. Used only to attribute the
            clobber to IDENTITY tokens composing the colliding names.

    Returns:
        Tuple of Clobber records (one per physical name with ≥2 distinct
        sources), sorted by physical_name.
    """
    by_physical: dict[str, list[LogicalObject]] = {}
    for obj in objects:
        try:
            resolved = obj.name.resolve(env, strict=False)
        except KeyError:
            # Under-resolved (missing token) → cannot participate in clobber
            # detection meaningfully. The existing "undefined" check covers it.
            continue
        by_physical.setdefault(_normalise_physical(resolved), []).append(obj)

    clobbers: list[Clobber] = []
    for physical, group in by_physical.items():
        # Deduplicate by source_id so the same file recorded twice does not
        # synthesise a clobber.
        distinct_sources = sorted({o.source_id for o in group})
        if len(distinct_sources) < 2:
            continue
        # Tokens responsible: every IDENTITY token referenced across the
        # colliding names. Falls back to "every token referenced" if roles
        # were not supplied.
        all_tokens: set[str] = set()
        for o in group:
            all_tokens.update(o.name.tokens)
        if roles is not None:
            attributed = tuple(
                sorted(
                    t
                    for t in all_tokens
                    if roles.get(t) and roles[t].role is Role.IDENTITY
                )
            )
        else:
            attributed = tuple(sorted(all_tokens))
        clobbers.append(
            Clobber(
                physical_name=physical,
                sources=tuple(distinct_sources),
                tokens=attributed,
            )
        )
    clobbers.sort(key=lambda c: c.physical_name)
    return tuple(clobbers)


# --------------------------------------------------------------------------
# Value-level collision classification
# --------------------------------------------------------------------------


def _group_tokens_by_resolved_value(
    env: Mapping[str, str],
    resolved: Mapping[str, str],
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Return ``(value, sorted tokens)`` for every value shared by ≥2 tokens.

    ``resolved`` is the substituted env map (one pass through preview_resolve
    or substitute_tokens). Empty resolved values are excluded so they are not
    reported here; the "Empty" column already covers them.
    """
    by_value: dict[str, list[str]] = {}
    for name in env.keys():
        value = resolved.get(name, "")
        if value == "":
            continue
        by_value.setdefault(value, []).append(name)
    groups = [
        (value, tuple(sorted(names)))
        for value, names in by_value.items()
        if len(names) > 1
    ]
    groups.sort(key=lambda g: g[1])
    return tuple(groups)


def classify_collision(
    tokens: Sequence[str],
    roles: Mapping[str, RoleAssignment],
    *,
    clobbers: Sequence[Clobber] = (),
) -> CollisionClass:
    """Assign a CollisionClass to a token-pair-or-group.

    Rules (per spec section 2b):

    * All members SCALAR → ``SCALAR``
    * All members ENV_LABEL → ``ENV_LABEL``
    * ≥2 members IDENTITY and at least one of them appears in a clobber's
      attributed tokens → ``REAL``
    * ≥2 members IDENTITY but none appear in any clobber → ``ALIAS`` (they
      name the same logical object — a DRY candidate, not a clobber)
    * Anything else → ``MIXED`` (a smell; surfaced as WARNING for triage)
    """
    if not tokens:
        return CollisionClass.MIXED

    member_roles = [roles.get(t).role if roles.get(t) else Role.UNKNOWN for t in tokens]

    if all(r is Role.SCALAR for r in member_roles):
        return CollisionClass.SCALAR
    if all(r is Role.ENV_LABEL for r in member_roles):
        return CollisionClass.ENV_LABEL

    identity_members = [t for t, r in zip(tokens, member_roles) if r is Role.IDENTITY]
    if len(identity_members) >= 2:
        clobber_tokens: set[str] = set()
        for c in clobbers:
            clobber_tokens.update(c.tokens)
        if any(t in clobber_tokens for t in identity_members):
            return CollisionClass.REAL
        return CollisionClass.ALIAS

    return CollisionClass.MIXED


# --------------------------------------------------------------------------
# Top-level audit
# --------------------------------------------------------------------------


def audit_resolution(
    *,
    env: str,
    env_config: Mapping[str, str],
    resolved_env: Mapping[str, str],
    payload_objects: Sequence[LogicalObject],
    roles: Mapping[str, RoleAssignment],
    defined_count: int = 0,
    undefined: Sequence[str] = (),
    unused: Sequence[str] = (),
    empty: Sequence[str] = (),
) -> ResolutionReport:
    """Run the resolved-object-identity audit for one environment.

    Args:
        env: environment name for the report.
        env_config: raw env-config token → value map.
        resolved_env: env_config after internal {{TOKEN}} resolution.
        payload_objects: every LogicalObject the audit should consider.
        roles: token → RoleAssignment from the role classifier.
        defined_count, undefined, unused, empty: existing tokenisation report
            counters passed through unchanged.

    Returns:
        ResolutionReport ready to render in the package report and gate on
        in inspect rule severity.
    """
    clobbers = detect_clobbers(payload_objects, resolved_env, roles=roles)
    value_groups = _group_tokens_by_resolved_value(env_config, resolved_env)
    collisions = tuple(
        CollisionGroup(
            value=value,
            tokens=tokens,
            classification=classify_collision(tokens, roles, clobbers=clobbers),
        )
        for value, tokens in value_groups
    )
    return ResolutionReport(
        env=env,
        clobbers=clobbers,
        collisions=collisions,
        roles=roles,
        defined_count=defined_count,
        undefined=tuple(undefined),
        unused=tuple(unused),
        empty=tuple(empty),
    )


# --------------------------------------------------------------------------
# Convenience: walk a payload directory and run the audit
# --------------------------------------------------------------------------


def collect_payload_objects(
    payload_files: Iterable[Tuple[str, str]],
) -> Tuple[LogicalObject, ...]:
    """Build LogicalObjects from ``(filename, _sql_text)`` pairs.

    Currently uses only the filename (SHIPS naming convention is the
    canonical object identity carrier). The ``_sql_text`` argument is
    accepted for forward compatibility — a later pass may also harvest
    CREATE DATABASE / CREATE ROLE statements from bodies to catch objects
    not present as their own .db / .rol files.
    """
    out: list[LogicalObject] = []
    for filename, _sql_text in payload_files:
        parsed = _parse_payload_filename(filename)
        if parsed is None:
            continue
        out.append(LogicalObject(source_id=filename, name=parsed))
    return tuple(out)


def audit_project(
    *,
    env: str,
    env_config: Mapping[str, str],
    resolved_env: Mapping[str, str],
    payload_files: Iterable[Tuple[str, str]],
    defined_count: int = 0,
    undefined: Sequence[str] = (),
    unused: Sequence[str] = (),
    empty: Sequence[str] = (),
) -> ResolutionReport:
    """High-level helper: classify roles + audit resolution in one call."""
    payload_files = tuple(payload_files)  # iterate twice
    objects = collect_payload_objects(payload_files)
    roles = classify_token_roles(
        payload_dir=None,
        env_config=env_config,
        extra_payloads=payload_files,
    )
    return audit_resolution(
        env=env,
        env_config=env_config,
        resolved_env=resolved_env,
        payload_objects=objects,
        roles=roles,
        defined_count=defined_count,
        undefined=undefined,
        unused=unused,
        empty=empty,
    )
