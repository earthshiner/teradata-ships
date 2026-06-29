"""
orphan_database.py — Flag prereq database/user declarations the payload doesn't reference (#475).

Project-level inspect rule. Walks every ``.db`` / ``.usr`` file under
``<project>/payload/database/pre-requisites/`` and checks each declared
database name against a set of "referenced" database names harvested
from the rest of the payload. Anything declared-but-never-referenced is
emitted as an ``orphan_database`` finding.

Typical cause: two naming conventions cross-wired. SHIPS' view-layer
generator emits an abbreviated ``{{DB_PREFIX}}_<MOD>_<TIER>_V`` database
to match the locking-view module token (``_DOM_/_MEM_/_OBS_/...``),
while a hand-authored ``databases/`` tree under source already declared
the full-name companion (``_Domain_STD_V`` / ``_Memory_STD_V`` / ...).
Both lands in ``pre-requisites/databases/``; one gets used at deploy
time, the other becomes dead infrastructure. The check surfaces the
mismatch at lint time so the operator can pick a convention before the
deploy goes out.

The detector is deliberately *conservative*: it reports a database as
orphan only when no qualified ``<db>.<obj>`` reference appears in any
payload file, no grant targets/grantees mention it, and no other
``.db`` / ``.usr`` declares ``CREATE ... FROM <db>``. False positives
are kept to a minimum because the flag implies the operator should
delete the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Set

from td_release_packager.validate import ValidationIssue


RULE_NAME = "orphan_database"

_PREREQ_DBS_SUBPATH = os.path.join("payload", "database", "pre-requisites", "databases")
_PREREQ_USERS_SUBPATH = os.path.join("payload", "database", "pre-requisites", "users")
_PAYLOAD_SUBPATH = os.path.join("payload", "database")

# Suffixes whose bodies can mention a database in a referenceable position.
_PAYLOAD_SCAN_SUFFIXES = frozenset(
    {
        ".tbl",
        ".viw",
        ".spl",
        ".mcr",
        ".fnc",
        ".trg",
        ".idx",
        ".jix",
        ".sto",
        ".cmt",
        ".stt",
        ".dml",
        ".osql",
        ".sql",
        ".ddl",
        ".dcl",
        ".grt",
        ".fk",
        ".db",
        ".usr",
        ".auth",
        ".fsvr",
        ".rol",
        ".prf",
        ".map",
        ".sjr",
    }
)

# Strip comments and string literals before scanning so a database name
# embedded in a ``-- comment`` or ``'string literal'`` doesn't count
# as a real reference.
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

# A database identifier — bare ident, quoted ident, or a tokenised
# ``{{TOKEN}}`` with optional literal suffix (covers both Shape A and
# Shape B from #454/#456). Group 1 captures the identifier.
_DB_IDENT = r'(?:"[^"]+"|\{\{[A-Za-z_]\w*\}\}\w*|[A-Za-z_]\w*)'

# Anywhere a ``<db>.<obj>`` reference is allowed to appear in payload
# SQL — qualified objects in CREATE/DROP/REPLACE/COMMENT ON statements,
# FROM/JOIN/INTO clauses, GRANT targets, etc. The regex is intentionally
# permissive: any ``<db>.<obj>`` anchor is counted. Surrounding
# whitespace around the dot is tolerated to match Teradata's accepted
# spacing rules.
_DB_DOT_OBJ_RE = re.compile(
    rf"(?<![.\w])({_DB_IDENT})\s*\.\s*(?:{_DB_IDENT})(?![.\w])",
)

# ``GRANT ... TO <db>`` / ``REVOKE ... FROM <db>`` — the grantee is a
# database/user name on its own (no following ``.<obj>``).
_GRANT_TO_RE = re.compile(
    rf"\b(?:GRANT|REVOKE)\b[^;]*?\b(?:TO|FROM)\s+({_DB_IDENT})\b",
    re.IGNORECASE | re.DOTALL,
)

# ``CREATE DATABASE child FROM parent`` / ``CREATE USER ... FROM
# parent`` — the parent is referenced.
_CREATE_FROM_RE = re.compile(
    rf"\bCREATE\s+(?:DATABASE|USER)\s+{_DB_IDENT}\s+FROM\s+({_DB_IDENT})",
    re.IGNORECASE,
)

# Header of a ``.db`` / ``.usr`` declaration — captures the declared
# database name, which is the "owner" of this file (NOT a reference;
# the file is what we're checking *for* orphans).
_DECLARED_DB_RE = re.compile(
    rf"\bCREATE\s+(?:DATABASE|USER)\s+({_DB_IDENT})",
    re.IGNORECASE,
)


def _strip_noise(text: str) -> str:
    """Remove comments + string literals so references inside them don't count."""
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    text = _STRING_LITERAL_RE.sub("''", text)
    return text


def _normalise(name: str) -> str:
    """Normalise an identifier for case-insensitive comparison."""
    return name.strip().strip('"').upper()


def _collect_referenced_databases(project_dir: str, declared: Set[str]) -> Set[str]:
    """Scan every payload file for database references; return their normalised names.

    ``declared`` is the set of *normalised* database names we're testing
    for orphan status — used to skip the file that declares each (a
    database doesn't count as referencing itself).
    """
    referenced: Set[str] = set()
    payload_root = os.path.join(project_dir, _PAYLOAD_SUBPATH)
    if not os.path.isdir(payload_root):
        return referenced

    for path in Path(payload_root).rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in _PAYLOAD_SCAN_SUFFIXES:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = _strip_noise(raw)

        # If the file is a database declaration, exclude its own
        # declared name from "referenced" — the file declaring ``X``
        # mentions ``X`` in its CREATE header, but that's the
        # declaration itself, not a reference *to* ``X`` from another
        # object.
        own_declared: Set[str] = set()
        if suffix in (".db", ".usr"):
            for m in _DECLARED_DB_RE.finditer(text):
                own_declared.add(_normalise(m.group(1)))

        # ``<db>.<obj>`` references — covers every CREATE TABLE,
        # SELECT FROM, GRANT ON, COMMENT ON, etc. that names a
        # qualified object.
        for m in _DB_DOT_OBJ_RE.finditer(text):
            name = _normalise(m.group(1))
            if name in own_declared:
                continue
            referenced.add(name)

        # ``GRANT ... TO <db>`` — grantee is a bare database/user name.
        for m in _GRANT_TO_RE.finditer(text):
            name = _normalise(m.group(1))
            if name in own_declared:
                continue
            referenced.add(name)

        # ``CREATE DATABASE child FROM parent`` — parent reference.
        for m in _CREATE_FROM_RE.finditer(text):
            name = _normalise(m.group(1))
            if name in own_declared:
                continue
            referenced.add(name)

    return referenced


def check_orphan_databases(
    project_dir: str, severity: str = "WARNING"
) -> List[ValidationIssue]:
    """Scan prereq ``.db`` / ``.usr`` files for orphan database declarations.

    Args:
        project_dir: SHIPS project root.
        severity:    Severity to stamp on findings (resolved by the
                     caller from ``inspect.conf``; ``OFF`` is handled
                     by the caller).

    Returns:
        A list of ``ValidationIssue`` (possibly empty). No-op when
        there is no ``pre-requisites/`` tree.
    """
    issues: List[ValidationIssue] = []

    # 1. Collect every declared database/user from prereq files.
    declarations: List[tuple[str, str, Path]] = []  # (declared_name, kind, path)
    for sub in (_PREREQ_DBS_SUBPATH, _PREREQ_USERS_SUBPATH):
        root = os.path.join(project_dir, sub)
        if not os.path.isdir(root):
            continue
        kind = "DATABASE" if sub == _PREREQ_DBS_SUBPATH else "USER"
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in (".db", ".usr"):
                continue
            try:
                text = _strip_noise(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            m = _DECLARED_DB_RE.search(text)
            if m is None:
                continue
            declarations.append((m.group(1).strip('"'), kind, path))

    if not declarations:
        return issues

    declared_set = {_normalise(name) for name, _, _ in declarations}
    referenced = _collect_referenced_databases(project_dir, declared_set)

    # 2. For each declaration, flag if its name is never referenced.
    for declared_name, kind, path in declarations:
        norm = _normalise(declared_name)
        if norm in referenced:
            continue
        rel = os.path.relpath(path, project_dir).replace("\\", "/")
        issues.append(
            ValidationIssue(
                file=rel,
                rule=RULE_NAME,
                severity=severity,
                message=(
                    f"{kind.title()} '{declared_name}' is declared but "
                    f"nothing in the payload references it. Likely "
                    f"causes: a stale hand-authored declaration "
                    f"superseded by the view-layer generator's emitted "
                    f"name (e.g. ``{{{{DB_PREFIX}}}}_Domain_STD_V`` "
                    f"vs ``{{{{DB_PREFIX}}}}_DOM_STD_V``), or a "
                    f"database declared upfront but never populated. "
                    f"Either delete this file or point payload objects "
                    f"at it."
                ),
                remediation={
                    "safe_fix_available": False,
                    "automation_level": "manual_review_required",
                    "requires_human_review": True,
                    "recommended_action": (
                        "Decide which naming convention this project "
                        "uses, then either delete the orphan "
                        "declaration or rename payload references "
                        "to point at it."
                    ),
                },
            )
        )

    return issues
