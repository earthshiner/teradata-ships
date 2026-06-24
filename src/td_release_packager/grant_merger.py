"""
grant_merger — privilege merge + deterministic emission for DCL files.

The handover (HANDOVER-ships-tokenised-filename-eponymy.md §7.1)
specifies one canonical privilege-merge rule across the pipeline.
A DCL file may collect multiple ``GRANT`` and ``REVOKE`` statements
over time, and statements that differ only in the privilege should
fold into a single statement with a comma-separated privilege list.
This module is the single implementation.

Merge rule:

    GRANT SELECT ON X TO Y WITH GRANT OPTION;
    GRANT INSERT ON X TO Y WITH GRANT OPTION;
            -- merge to -->
    GRANT SELECT, INSERT ON X TO Y WITH GRANT OPTION;

Merge key = ``(action, on_object, grantee, with_grant_option)``.
All four must match; ``action`` is GRANT or REVOKE.

Strict separations (these never merge):
  - GRANT and REVOKE.
  - Privilege grants (``GRANT <privs> ON <obj> TO <user>``) and role
    grants (``GRANT <role> TO <user>``) — different statement shape.

Deterministic order:
  - Statements sorted by ``(action, on_object, grantee, with_grant_option,
    sorted-privs)`` before emission. Unordered emission would reopen
    the determinism hole that issue #365 sets out to close.

This module is intentionally narrow. It does not classify grants
(see ``classifier.py``), does not produce filenames (see
``atomic_filename.py``), and does not parse arbitrary DCL syntax
beyond the canonical Teradata GRANT/REVOKE shapes the rest of SHIPS
emits. Tokens are preserved verbatim — the parser treats
``{{DB_PREFIX}}_T`` and ``CallCentre_T`` as opaque identifier text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------
# Statement model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PrivilegeGrant:
    """A privilege-style ``GRANT``/``REVOKE`` statement.

    Attributes:
        action: ``"GRANT"`` or ``"REVOKE"``.
        privileges: Tuple of privilege names in canonical upper case
            (``"SELECT"``, ``"INSERT"``, etc.). Order is preserved
            here; merging produces a sorted union.
        on_object: The ``ON`` target — a database or
            ``database.object`` identifier, possibly tokenised.
            Preserved verbatim.
        grantee: The ``TO`` (or ``FROM``) target — a user, role, or
            database. Preserved verbatim.
        with_grant_option: ``True`` if the statement carries the
            ``WITH GRANT OPTION`` clause.
    """

    action: str
    privileges: Tuple[str, ...]
    on_object: str
    grantee: str
    with_grant_option: bool

    @property
    def merge_key(self) -> Tuple[str, str, str, bool]:
        return (self.action, self.on_object, self.grantee, self.with_grant_option)


@dataclass(frozen=True)
class RoleGrant:
    """A role-style ``GRANT <role> TO <user>`` statement.

    Role grants have a distinct shape from privilege grants and
    must never merge with them — emission treats them as a separate
    bucket.
    """

    action: str  # GRANT or REVOKE
    role: str
    grantee: str
    with_admin_option: bool = False


# Either statement form.
Statement = "PrivilegeGrant | RoleGrant"


# --------------------------------------------------------------------------
# Parser — accepts the canonical Teradata GRANT/REVOKE shapes
# --------------------------------------------------------------------------


# A single statement is terminated by ``;`` and may span multiple
# lines. Comments and blank lines are skipped at the file level.
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


_PRIV_GRANT_RE = re.compile(
    r"""^\s*(?P<action>GRANT|REVOKE)\s+
        (?P<privileges>.+?)\s+
        ON\s+
        (?:(?:TABLE|VIEW|MACRO|PROCEDURE|EXTERNAL\s+PROCEDURE|FUNCTION|
             DATABASE|USER|ROLE|PROFILE)\s+)?
        (?P<on_object>\S+?)\s+
        (?P<direction>TO|FROM)\s+
        (?P<grantee>\S+?)
        (?P<with_grant>\s+WITH\s+GRANT\s+OPTION)?\s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


# Role grants: ``GRANT <role[, role2]> TO <user>``. Distinguished
# from privilege grants by the absence of an ``ON`` clause. Treat
# the comma-separated role list as one opaque token for now —
# expanding it is symmetric to privilege expansion but not required
# by the handover.
_ROLE_GRANT_RE = re.compile(
    r"""^\s*(?P<action>GRANT|REVOKE)\s+
        (?P<role>[A-Za-z_{][^;]*?)\s+
        TO\s+
        (?P<grantee>\S+?)
        (?P<with_admin>\s+WITH\s+ADMIN\s+OPTION)?\s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def _strip_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _split_statements(text: str) -> List[str]:
    """Split a DCL file body into individual statements.

    Comments are stripped first, then we split on ``;``. Trailing
    empty parts are dropped. Statement order is preserved — the
    caller sorts before emitting.
    """
    cleaned = _strip_comments(text)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def parse_statement(raw: str) -> Optional[Statement]:
    """Parse a single DCL statement.

    Privilege form is tried first because it's more specific (the
    ``ON`` clause disambiguates). If neither pattern matches,
    returns None — the caller decides whether to surface that as
    an error or pass the unparsed text through.
    """
    text = raw.strip().rstrip(";").strip()

    m = _PRIV_GRANT_RE.match(text)
    if m is not None:
        privileges_raw = m.group("privileges")
        privileges = tuple(
            p.strip().upper() for p in privileges_raw.split(",") if p.strip()
        )
        return PrivilegeGrant(
            action=m.group("action").upper(),
            privileges=privileges,
            on_object=m.group("on_object").strip(),
            grantee=m.group("grantee").strip(),
            with_grant_option=m.group("with_grant") is not None,
        )

    m = _ROLE_GRANT_RE.match(text)
    if m is not None:
        return RoleGrant(
            action=m.group("action").upper(),
            role=m.group("role").strip(),
            grantee=m.group("grantee").strip(),
            with_admin_option=m.group("with_admin") is not None,
        )

    return None


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


@dataclass
class MergeResult:
    """Output of ``merge_statements``.

    Attributes:
        merged: The merged statements in canonical order.
        unparsed: Statement bodies that failed to parse. Preserved
            verbatim so the caller can either pass them through or
            surface them as errors. Empty when the file is clean.
    """

    merged: List[Statement] = field(default_factory=list)
    unparsed: List[str] = field(default_factory=list)


def merge_statements(statements: List[Statement]) -> List[Statement]:
    """Fold compatible PrivilegeGrant statements; pass RoleGrants
    through.

    Two PrivilegeGrants merge iff their ``merge_key`` matches —
    ``(action, on_object, grantee, with_grant_option)``. Their
    privilege tuples are unioned and sorted alphabetically.

    RoleGrant statements never merge with anything (different
    shape). The output preserves them as-is.

    Emission order: the result is sorted by
    ``(action, on_object, grantee, with_grant_option, privileges)``
    so any two runs over the same input produce byte-identical
    output. ``REVOKE`` sorts after ``GRANT`` because ``"REVOKE" >
    "GRANT"`` lexically — coincidental but stable.
    """
    privilege_buckets: dict[Tuple[str, str, str, bool], set[str]] = {}
    role_grants: List[RoleGrant] = []

    for stmt in statements:
        if isinstance(stmt, PrivilegeGrant):
            bucket = privilege_buckets.setdefault(stmt.merge_key, set())
            bucket.update(stmt.privileges)
        elif isinstance(stmt, RoleGrant):
            role_grants.append(stmt)

    merged: List[Statement] = []
    for key, privs in privilege_buckets.items():
        action, on_object, grantee, with_grant = key
        merged.append(
            PrivilegeGrant(
                action=action,
                privileges=tuple(sorted(privs)),
                on_object=on_object,
                grantee=grantee,
                with_grant_option=with_grant,
            )
        )

    merged.sort(
        key=lambda s: (
            s.action,
            s.on_object,
            s.grantee,
            s.with_grant_option,
            s.privileges,
        )
    )

    # Role grants sort by their own key, emitted after privilege
    # grants. They cannot interleave because the shapes diverge.
    role_grants.sort(key=lambda r: (r.action, r.role, r.grantee, r.with_admin_option))
    return merged + list(role_grants)


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def emit_statement(stmt: Statement) -> str:
    """Render a single canonical SQL statement.

    Format mirrors the rest of the SHIPS code base — one statement
    per line, trailing semicolon, no extra whitespace. The
    privilege list keeps the sorted-and-unioned form merge produces.
    """
    if isinstance(stmt, PrivilegeGrant):
        privs = ", ".join(stmt.privileges)
        out = f"{stmt.action} {privs} ON {stmt.on_object}"
        out += (
            f" TO {stmt.grantee}" if stmt.action == "GRANT" else f" FROM {stmt.grantee}"
        )
        if stmt.with_grant_option:
            out += " WITH GRANT OPTION"
        return out + ";"
    if isinstance(stmt, RoleGrant):
        out = f"{stmt.action} {stmt.role}"
        out += (
            f" TO {stmt.grantee}" if stmt.action == "GRANT" else f" FROM {stmt.grantee}"
        )
        if stmt.with_admin_option:
            out += " WITH ADMIN OPTION"
        return out + ";"
    raise TypeError(f"unknown statement type: {type(stmt).__name__}")


def merge_and_emit(text: str) -> Tuple[str, List[str]]:
    """Parse a DCL file body, merge, and re-emit canonically.

    Returns:
        ``(emitted_text, unparsed_raw_statements)``. Unparsed raw
        statements are reported back to the caller so a DCL file
        with hand-written exotic syntax does not silently lose
        content.
    """
    raw_statements = _split_statements(text)
    parsed: List[Statement] = []
    unparsed: List[str] = []
    for raw in raw_statements:
        stmt = parse_statement(raw)
        if stmt is None:
            unparsed.append(raw)
        else:
            parsed.append(stmt)

    merged = merge_statements(parsed)
    lines = [emit_statement(s) for s in merged]
    return ("\n".join(lines) + ("\n" if lines else ""), unparsed)
