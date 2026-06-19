"""
token_roles — env-independent classification of {{TOKEN}} usage roles.

A "role" describes how a token is *used* in the package, not what it is named.
Two tokens that share a value are dangerous only if at least one of them is
used to compose an object identity (a database, user, role, or qualified
object name). Two tokens that share a value but are only ever used as scalar
attributes (``PERM = {{t}}``, ``SPOOL = {{t}}``) are interchangeable and
harmless.

The audit (built on top of this module) classifies *collisions* by role; this
module classifies *tokens* by role. Role assignment is environment-
independent — it depends on payload structure and env-config RHS composition,
not on resolved values — so it is computed once per package.

Role precedence (most dangerous wins):

    IDENTITY  >  SCALAR  >  ENV_LABEL  >  UNUSED  >  UNKNOWN

A token seen in both identity and scalar positions is still classified
``IDENTITY`` but flagged ``mixed_use=True`` — a modelling smell the audit
reports separately.

This module is a pure primitive: no I/O beyond an optional ``walk_payload``
helper, no logging, no dependency on the inspect/validate stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Tuple

from td_release_packager.tokenised_name import (
    QualifiedName,
    extract_tokens,
    parse_qualified_name,
)


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------


class Role(str, Enum):
    """Token usage role, ordered most-dangerous-first.

    Values are strings so they survive JSON round-trips into
    ``ships.token_resolution.json`` without custom serialisers.
    """

    IDENTITY = "IDENTITY"
    SCALAR = "SCALAR"
    ENV_LABEL = "ENV_LABEL"
    UNUSED = "UNUSED"
    UNKNOWN = "UNKNOWN"


@dataclass
class TokenPositions:
    """Accumulator for every syntactic position a token has been seen in.

    Mutable by design — the position scanner ORs new findings in as it walks
    each payload file. ``classify`` reads the final state.
    """

    identity: bool = False
    scalar: bool = False
    env_config_composition: bool = False  # appears in env-config RHS only
    any_payload_reference: bool = False

    def merge(self, other: "TokenPositions") -> None:
        self.identity |= other.identity
        self.scalar |= other.scalar
        self.env_config_composition |= other.env_config_composition
        self.any_payload_reference |= other.any_payload_reference


@dataclass(frozen=True)
class RoleAssignment:
    """The classifier's verdict for one token."""

    role: Role
    mixed_use: bool
    positions: TokenPositions

    def to_dict(self) -> dict:
        return {
            "role": self.role.value,
            "mixed_use": self.mixed_use,
            "positions": {
                "identity": self.positions.identity,
                "scalar": self.positions.scalar,
                "env_config_composition": self.positions.env_config_composition,
                "any_payload_reference": self.positions.any_payload_reference,
            },
        }


# --------------------------------------------------------------------------
# Anchor patterns
# --------------------------------------------------------------------------
#
# Each pattern captures the *name* immediately following an identity-bearing
# SQL anchor. The name is then parsed with the shared tokenised-name parser
# to extract every token it composes — those tokens are marked IDENTITY.
#
# Anchors mirror the spec's role table (section 2a) plus the existing
# analyser's structural-reference list.

# Permissive name pattern: enough to swallow tokenised, quoted, qualified
# forms; the parser handles the structure.
_NAME_RX = r'(?:"[^"]+"|\{\{[A-Za-z_]\w*\}\}|[A-Za-z_$][\w$]*|&&[A-Za-z_]\w*&&)'
_QNAME_RX = rf"{_NAME_RX}(?:\s*\.\s*{_NAME_RX})?"

_IDENTITY_ANCHORS = [
    # CREATE DATABASE/USER <name>
    re.compile(rf"\bCREATE\s+(?:DATABASE|USER)\s+({_QNAME_RX})", re.IGNORECASE),
    # FROM <parent>  (only in CREATE DATABASE/USER context; we accept the
    # naive FROM-then-name here and trust the surrounding pattern — analyser
    # has the strict guard; for role classification a small over-classification
    # toward IDENTITY is safe per the "most dangerous wins" rule.)
    re.compile(
        rf"\bCREATE\s+(?:DATABASE|USER)\s+{_QNAME_RX}\s+FROM\s+({_QNAME_RX})",
        re.IGNORECASE | re.DOTALL,
    ),
    # CREATE ROLE <name>
    re.compile(rf"\bCREATE\s+ROLE\s+({_QNAME_RX})", re.IGNORECASE),
    # CREATE PROFILE <name>
    re.compile(rf"\bCREATE\s+PROFILE\s+({_QNAME_RX})", re.IGNORECASE),
    # GRANT ... ON <target>  (DCL target; the target is an identity)
    re.compile(rf"\bGRANT\s+[^;]*?\bON\s+({_QNAME_RX})", re.IGNORECASE | re.DOTALL),
    # GRANT ... TO <grantee>  (DCL grantee; the grantee is an identity)
    re.compile(rf"\bGRANT\s+[^;]*?\bTO\s+({_QNAME_RX})", re.IGNORECASE | re.DOTALL),
    # REVOKE mirrors GRANT
    re.compile(rf"\bREVOKE\s+[^;]*?\bON\s+({_QNAME_RX})", re.IGNORECASE | re.DOTALL),
    re.compile(rf"\bREVOKE\s+[^;]*?\bFROM\s+({_QNAME_RX})", re.IGNORECASE | re.DOTALL),
    # Anything CREATE/REPLACE'd in a database-qualified position
    re.compile(
        rf"\b(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+|GLOBAL\s+TEMPORARY\s+|VOLATILE\s+|RECURSIVE\s+)*"
        rf"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|JOIN\s+INDEX|HASH\s+INDEX|"
        rf"INDEX|TYPE|AUTHORIZATION|MAP|FOREIGN\s+SERVER)\s+({_QNAME_RX})",
        re.IGNORECASE,
    ),
]

# Scalar positions: ``KEY = <value>`` where KEY is a known attribute name.
# The value side may be a {{TOKEN}}, a numeric, or a literal.
_SCALAR_ANCHORS = [
    re.compile(
        rf"\b(?:PERM|PERMANENT|SPOOL|TEMPORARY|TEMP)\s*=\s*({_NAME_RX}|[0-9][\d.eE+\-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:ACCOUNT|DEFAULT\s+ROLE|DEFAULT\s+DATABASE|DEFAULT\s+JOURNAL\s+TABLE)\s*="
        rf"\s*({_NAME_RX})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:PASSWORD)\s*=\s*({_NAME_RX}|'[^']*')",
        re.IGNORECASE,
    ),
]


# --------------------------------------------------------------------------
# Scanners
# --------------------------------------------------------------------------


def _collect_tokens_from_name(text: str) -> Tuple[str, ...]:
    """Parse a captured SQL name; return all token references it composes.

    A capture like ``{{DB}}.{{OBJ}}`` yields ``('DB', 'OBJ')``. Both
    qualifier sides are identity-bearing per the role table — a tokenised
    database segment is by definition an identity position.

    Falls back to a loose token-scan if the capture is malformed (e.g. an
    anchor matched something the parser cannot interpret); this keeps
    classification robust against SQL the parser doesn't fully understand.
    """
    try:
        q: QualifiedName = parse_qualified_name(text)
        return q.tokens
    except Exception:
        return extract_tokens(text)


def scan_sql_text(
    sql: str,
    *,
    filename: str | None = None,
) -> dict[str, TokenPositions]:
    """Detect role positions for every ``{{TOKEN}}`` in one chunk of SQL.

    Args:
        sql: raw SQL source (may include legacy placeholder forms; the
            shared parser normalises them).
        filename: optional source filename. If it has the SHIPS pattern
            ``{{db}}.object.ext`` (i.e. the leading dot-segment contains a
            token), those tokens are recorded as IDENTITY since the filename
            naming convention treats that segment as the deploy-time database.

    Returns:
        ``{token_name: TokenPositions}`` covering every token referenced.
        Tokens absent from this dict were not referenced in ``sql``.
    """
    positions: dict[str, TokenPositions] = {}

    def _pos(tok: str) -> TokenPositions:
        return positions.setdefault(tok, TokenPositions())

    # Every token reference, anywhere — bootstraps any_payload_reference.
    for tok in extract_tokens(sql):
        _pos(tok).any_payload_reference = True

    # Identity anchors
    for pattern in _IDENTITY_ANCHORS:
        for match in pattern.finditer(sql):
            captured = match.group(1)
            for tok in _collect_tokens_from_name(captured):
                _pos(tok).identity = True
                _pos(tok).any_payload_reference = True

    # Scalar anchors
    for pattern in _SCALAR_ANCHORS:
        for match in pattern.finditer(sql):
            captured = match.group(1)
            for tok in _collect_tokens_from_name(captured):
                _pos(tok).scalar = True
                _pos(tok).any_payload_reference = True

    # Filename db-segment convention
    if filename:
        stem_dot = Path(filename).name.split(".", 1)
        if len(stem_dot) == 2 and stem_dot[0]:
            for tok in extract_tokens(stem_dot[0]):
                _pos(tok).identity = True
                _pos(tok).any_payload_reference = True

    return positions


def scan_env_config_composition(env_config: Mapping[str, str]) -> dict[str, bool]:
    """Mark tokens that appear in the RHS of another env-config entry.

    These are env-label / namespacing roots like ``ENV_PREFIX`` or
    ``SHIPS_ENV`` — they exist to compose other tokens, not to name objects
    directly. A token used both as an RHS component and in a payload
    identity position is still IDENTITY (precedence rule).

    Returns ``{token_name: True}`` for every token referenced on any RHS.
    """
    composed: dict[str, bool] = {}
    for value in env_config.values():
        for tok in extract_tokens(str(value)):
            composed[tok] = True
    return composed


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


@dataclass
class _Accumulator:
    """Internal merge state across many payload files + env config."""

    positions: dict[str, TokenPositions] = field(default_factory=dict)
    env_composed: dict[str, bool] = field(default_factory=dict)
    defined: set[str] = field(default_factory=set)


def _merge_positions(
    acc: dict[str, TokenPositions], add: dict[str, TokenPositions]
) -> None:
    for tok, p in add.items():
        acc.setdefault(tok, TokenPositions()).merge(p)


def classify(
    *,
    defined_tokens: Iterable[str],
    payload_positions: Mapping[str, TokenPositions],
    env_config_composition: Mapping[str, bool],
) -> dict[str, RoleAssignment]:
    """Combine scanned signals into a role per token.

    Args:
        defined_tokens: every token defined in the env config (LHS keys).
            UNUSED is computed by set-difference against payload references.
        payload_positions: output of one or more ``scan_sql_text`` calls,
            merged. Tokens never referenced in payload are absent.
        env_config_composition: output of ``scan_env_config_composition``.

    Returns:
        ``{token: RoleAssignment}`` for the union of all known tokens
        (defined + referenced in payload + composed in env config).
    """
    all_tokens: set[str] = set(defined_tokens)
    all_tokens.update(payload_positions.keys())
    all_tokens.update(env_config_composition.keys())

    out: dict[str, RoleAssignment] = {}
    for tok in all_tokens:
        pos = payload_positions.get(tok, TokenPositions())
        # env-config composition is a position too, for tokens that
        # never reach the payload directly.
        if env_config_composition.get(tok):
            pos.env_config_composition = True

        mixed = pos.identity and pos.scalar

        if pos.identity:
            role = Role.IDENTITY
        elif pos.scalar and not pos.identity:
            role = Role.SCALAR
        elif pos.env_config_composition and not pos.any_payload_reference:
            role = Role.ENV_LABEL
        elif not pos.any_payload_reference and not pos.env_config_composition:
            # Defined but never referenced anywhere.
            role = Role.UNUSED
        else:
            # Referenced in payload but not in any anchor we recognise.
            # The audit treats this as WARNING-class for triage.
            role = Role.UNKNOWN

        out[tok] = RoleAssignment(role=role, mixed_use=mixed, positions=pos)
    return out


# --------------------------------------------------------------------------
# Convenience walker
# --------------------------------------------------------------------------


_PAYLOAD_EXTENSIONS = {
    ".tbl",
    ".viw",
    ".vw",
    ".mac",
    ".prc",
    ".spl",
    ".fnc",
    ".trg",
    ".idx",
    ".ji",
    ".sql",
    ".ddl",
    ".dml",
    ".dcl",
    ".db",
    ".usr",
    ".rol",
    ".prf",
}


def walk_payload(payload_dir: Path | str) -> Iterable[Tuple[str, str]]:
    """Yield ``(filename, sql_text)`` for every SHIPS payload file under a tree.

    Filename is the path *relative to* ``payload_dir`` so callers can apply
    the ``{{db}}.object.ext`` convention without leaking absolute paths into
    classification.
    """
    root = Path(payload_dir)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _PAYLOAD_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        yield str(path.relative_to(root)), text


def classify_token_roles(
    payload_dir: Path | str | None,
    env_config: Mapping[str, str],
    *,
    extra_payloads: Iterable[Tuple[str, str]] | None = None,
) -> dict[str, RoleAssignment]:
    """High-level convenience: walk a payload tree + env config, classify.

    Args:
        payload_dir: directory to scan, or ``None`` to skip filesystem walk
            (useful in tests; pass ``extra_payloads`` instead).
        env_config: ``{token: value}`` dict already parsed from a .conf file.
        extra_payloads: iterable of ``(filename, sql_text)`` pairs appended
            after the directory walk. Lets callers feed in-memory payloads
            (test fixtures, generated views, etc.) without staging them.

    Returns:
        ``{token: RoleAssignment}`` ready for the audit.
    """
    accumulated: dict[str, TokenPositions] = {}

    if payload_dir is not None:
        for filename, sql in walk_payload(payload_dir):
            _merge_positions(accumulated, scan_sql_text(sql, filename=filename))

    if extra_payloads is not None:
        for filename, sql in extra_payloads:
            _merge_positions(accumulated, scan_sql_text(sql, filename=filename))

    composition = scan_env_config_composition(env_config)

    return classify(
        defined_tokens=env_config.keys(),
        payload_positions=accumulated,
        env_config_composition=composition,
    )
