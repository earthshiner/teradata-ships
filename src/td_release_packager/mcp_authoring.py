"""
mcp_authoring.py — shared helpers for the SHIPS MCP authoring tool family.

Phase A of #291: authoring tools return a *proposal* (current content,
proposed content, unified diff, and a content hash of the current file).
Nothing is written to disc by an authoring tool.  A separate tool —
``ships_apply_diff`` — re-hashes the current file, compares against the
hash returned with the proposal, and writes the proposed content
atomically only on a match.

The unified diff is for human / agent review.  The apply path uses
``proposed_content`` directly, not diff-application, because applying
unified diffs programmatically is fragile (context lines, fuzz
factors).  The ``expected_hash`` provides the integrity check the diff
metaphor implies.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------

#: Sentinel hash returned for a non-existent file.  ``ships_apply_diff``
#: accepts this value when creating a new file; otherwise the file
#: must hash to ``expected_hash`` before any write is performed.
ABSENT_FILE_HASH: str = "absent"


def content_hash(text: str) -> str:
    """Return a stable SHA-256 hex digest of ``text`` (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: str) -> str:
    """Return ``content_hash`` of the file at ``path``, or
    :data:`ABSENT_FILE_HASH` if the file does not exist.
    """
    if not os.path.exists(path):
        return ABSENT_FILE_HASH
    with open(path, "r", encoding="utf-8") as f:
        return content_hash(f.read())


# ---------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------


def read_or_empty(path: str) -> str:
    """Return file contents, or an empty string if the file is absent."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def unified_diff(
    current: str,
    proposed: str,
    path: str,
    *,
    context_lines: int = 3,
) -> str:
    """Return a unified diff string between ``current`` and ``proposed``.

    Both inputs are full file contents.  Trailing-newline behaviour
    matches GNU diff: a missing final newline is annotated.
    """
    current_lines = current.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        current_lines,
        proposed_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context_lines,
    )
    return "".join(diff_iter)


# ---------------------------------------------------------------
# YAML serialisation (canonical form shared with write_if_missing)
# ---------------------------------------------------------------


def dump_yaml(data: Dict[str, Any]) -> str:
    """Serialise ``data`` to YAML in the canonical SHIPS form.

    Matches the settings used by
    :func:`td_release_packager.orchestrator.ships_yaml.write_if_missing`
    so authored content is byte-identical to scaffolded content.
    """
    buf = io.StringIO()
    yaml.safe_dump(
        data,
        buf,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return buf.getvalue()


# ---------------------------------------------------------------
# Dotted-key edits
# ---------------------------------------------------------------


def _split_dotted(key: str) -> List[str]:
    """Split a dotted key, rejecting empty segments."""
    if not key or not isinstance(key, str):
        raise ValueError("dotted key must be a non-empty string")
    parts = key.split(".")
    if any(p == "" for p in parts):
        raise ValueError(f"dotted key has an empty segment: {key!r}")
    return parts


def set_dotted(data: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set ``dotted_key`` to ``value`` in ``data`` (mutates in place).

    Intermediate dicts are created as needed.  Raises ``TypeError`` if
    an intermediate path collides with a non-dict scalar.
    """
    parts = _split_dotted(dotted_key)
    cursor: Dict[str, Any] = data
    for segment in parts[:-1]:
        next_node = cursor.get(segment)
        if next_node is None:
            next_node = {}
            cursor[segment] = next_node
        elif not isinstance(next_node, dict):
            raise TypeError(
                f"cannot descend into {dotted_key!r}: "
                f"segment {segment!r} is a {type(next_node).__name__}"
            )
        cursor = next_node
    cursor[parts[-1]] = value


def unset_dotted(data: Dict[str, Any], dotted_key: str) -> bool:
    """Delete ``dotted_key`` from ``data``.

    Returns True if a key was removed, False if it was already absent.
    """
    parts = _split_dotted(dotted_key)
    cursor: Any = data
    for segment in parts[:-1]:
        if not isinstance(cursor, dict) or segment not in cursor:
            return False
        cursor = cursor[segment]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False
    del cursor[parts[-1]]
    return True


# ---------------------------------------------------------------
# Structure-preserving KEY=VALUE editor (Phase B)
# ---------------------------------------------------------------
#
# .conf files in SHIPS are hand-curated with comments, numbered
# sections, and meaningful blank lines. A YAML-style "load → mutate
# dict → dump" round-trip would discard all of that. ``ConfFile``
# parses a file into an ordered list of items and only rewrites the
# lines that actually changed; unmodified lines (including comments
# and blanks) are passed through byte-for-byte.
#
# Format (matches read_env_config / read_inspect_config):
#     # comments start with '#'
#     KEY=VALUE
#     KEY = VALUE       (spaces around '=' allowed; preserved on
#                        unmodified lines, normalised on edited lines)


_KEY_RE = re.compile(r"^\s*([^#=\s][^=]*?)\s*=")


@dataclass(frozen=True)
class _ConfLine:
    """A single physical line in a .conf file.

    ``raw`` retains the original text including its line terminator,
    so dumping an unmodified file is byte-identical to the input.
    ``key`` is set only when the line declares a KEY=VALUE pair.
    """

    raw: str
    key: Optional[str] = None


class ConfFile:
    """An ordered, structure-preserving view of a KEY=VALUE .conf file.

    Round-trip property: ``ConfFile.parse(text).dump() == text`` for
    any well-formed input.  Edits replace only the targeted line(s);
    everything else is passed through verbatim.

    Edited / appended lines are written in canonical ``KEY=VALUE`` form
    (no spaces around ``=``).  Unmodified lines keep their original
    spacing, comments, and trailing whitespace.
    """

    def __init__(self, lines: List[_ConfLine]) -> None:
        self._lines: List[_ConfLine] = list(lines)

    # -- parsing / serialising ----------------------------------

    @classmethod
    def parse(cls, content: str) -> "ConfFile":
        lines: List[_ConfLine] = []
        for raw in content.splitlines(keepends=True):
            key = cls._extract_key(raw)
            lines.append(_ConfLine(raw=raw, key=key))
        return cls(lines)

    def dump(self) -> str:
        return "".join(line.raw for line in self._lines)

    @staticmethod
    def _extract_key(raw: str) -> Optional[str]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            return None
        match = _KEY_RE.match(raw)
        if not match:
            return None
        name = match.group(1).strip()
        return name or None

    # -- reads --------------------------------------------------

    def keys(self) -> List[str]:
        return [line.key for line in self._lines if line.key is not None]

    def get(self, key: str) -> Optional[str]:
        for line in self._lines:
            if line.key == key:
                # Split on first '=' and strip; matches read_env_config
                _, _, value = line.raw.partition("=")
                return value.strip().rstrip("\r\n").strip()
        return None

    def has(self, key: str) -> bool:
        return any(line.key == key for line in self._lines)

    # -- mutations ----------------------------------------------

    def set(self, key: str, value: str) -> None:
        """Set ``key`` to ``value``.  Replaces existing entry in place;
        appends at end-of-file if the key is absent.

        Replaced lines are normalised to ``KEY=VALUE`` form, preserving
        only the original line terminator.  Appended lines use the
        dominant terminator already present in the file (``\\n``-only
        when the file is empty).
        """
        self._reject_bad_key(key)
        if "\n" in value or "\r" in value:
            raise ValueError(f"value for {key!r} must not contain newlines")

        for i, line in enumerate(self._lines):
            if line.key == key:
                ending = self._line_ending(line.raw)
                self._lines[i] = _ConfLine(raw=f"{key}={value}{ending}", key=key)
                return

        # Append. Ensure the previous line ends with a newline so the
        # new entry starts on its own line.
        ending = self._dominant_ending()
        if self._lines:
            last = self._lines[-1]
            if not last.raw.endswith(("\n", "\r")):
                self._lines[-1] = _ConfLine(raw=last.raw + ending, key=last.key)
        self._lines.append(_ConfLine(raw=f"{key}={value}{ending}", key=key))

    def unset(self, key: str) -> bool:
        """Remove the line declaring ``key``.  Returns True if removed,
        False if the key was not present.

        Surrounding blank lines and comments are left untouched —
        minimal-change is more important than tidying.
        """
        self._reject_bad_key(key)
        for i, line in enumerate(self._lines):
            if line.key == key:
                del self._lines[i]
                return True
        return False

    # -- helpers ------------------------------------------------

    @staticmethod
    def _reject_bad_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string")
        if "=" in key or "\n" in key or "\r" in key or key.startswith("#"):
            raise ValueError(f"invalid key: {key!r}")

    @staticmethod
    def _line_ending(raw: str) -> str:
        if raw.endswith("\r\n"):
            return "\r\n"
        if raw.endswith("\n"):
            return "\n"
        if raw.endswith("\r"):
            return "\r"
        return ""

    def _dominant_ending(self) -> str:
        for line in reversed(self._lines):
            ending = self._line_ending(line.raw)
            if ending:
                return ending
        return "\n"


# ---------------------------------------------------------------
# Token-candidate analysis (Phase B.5)
# ---------------------------------------------------------------
#
# Read-only payload scanner + simple clustering heuristics that
# surface DRY opportunities in a flat token_map.conf.  Crucially
# does NOT mutate payload/ — unlike ingest_directory's detect
# mode, which still rewrites files during classification.


#: Extensions that SHIPS-harvested payload files use, used by
#: :func:`scan_payload_databases` to find every DDL file whose owner
#: database should be considered a token candidate.
#:
#: This set mirrors ``td_release_packager.kind_suffix.EXTENSION_TO_KIND``
#: plus the database / DCL / DML extensions that don't appear there.  The
#: previous list missed ``.viw`` (the canonical SHIPS extension for
#: views) which silently dropped every view from the candidate analyser
#: (issue #311 — view-target databases under-reported).  Keep this in
#: lock-step with ``EXTENSION_TO_KIND`` if the harvest convention grows
#: new extensions.
_PAYLOAD_DDL_EXTENSIONS = {
    ".bteq",
    ".db",
    ".dcl",
    ".ddl",
    ".ddml",
    ".dml",
    ".fnc",
    ".idx",
    ".ins",
    ".jix",
    ".mac",
    ".mcr",
    ".sjr",
    ".spl",
    ".sql",
    ".sto",
    ".tbl",
    ".trg",
    ".udf",
    ".viw",
    ".vw",
}


def scan_payload_databases(project_dir: str) -> Dict[str, List[str]]:
    """Walk ``<project>/payload/`` for DDL files and collect the
    qualified database name from each.

    Returns a dict of ``database_name → [relative file paths]``.
    Comment-stripping and qualified-name extraction reuse the same
    primitives as harvest, so the literals returned match what
    ``ships harvest`` would discover — without rewriting any file.
    """
    from td_release_packager.ingest import _extract_qualified_name
    from td_release_packager.sql_text import strip_comments_preserving_positions

    payload_root = os.path.join(project_dir, "payload")
    out: Dict[str, List[str]] = {}
    if not os.path.isdir(payload_root):
        return out

    for root, _dirs, files in os.walk(payload_root):
        for name in files:
            if os.path.splitext(name)[1].lower() not in _PAYLOAD_DDL_EXTENSIONS:
                continue
            src = os.path.join(root, name)
            try:
                with open(src, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except OSError:
                continue
            clean = strip_comments_preserving_positions(raw)
            db_name, _obj = _extract_qualified_name(clean)
            if not db_name:
                continue
            rel = os.path.relpath(src, project_dir).replace(os.sep, "/")
            out.setdefault(db_name, []).append(rel)
    return out


def _shared_prefix_tokens(names: List[str], min_tokens: int = 2) -> str:
    """Return the longest leading ``_``-separated token sequence shared
    by every name in ``names``.  Empty string if fewer than
    ``min_tokens`` segments are shared.
    """
    if not names:
        return ""
    split = [n.split("_") for n in names]
    common: List[str] = []
    for column in zip(*split):
        if all(c == column[0] and c for c in column):
            common.append(column[0])
        else:
            break
    if len(common) < min_tokens:
        return ""
    return "_".join(common)


def cluster_token_candidates(
    db_names: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Group ``db_names`` by structural similarity.

    Produces three lenses for the same set of literals:

    * ``literals`` — every candidate with its reference count and
      file list, sorted by descending count then by name.
    * ``prefix_clusters`` — groups of two or more names sharing a
      leading ``_``-separated prefix of at least two tokens.
    * ``suffix_clusters`` — groups of two or more names sharing
      the trailing token (commonly ``_T`` / ``_V`` / ``_M`` for
      object-kind suffixes).

    System databases are NOT filtered here — the caller should pass
    a pre-filtered dict (e.g. via
    :func:`td_release_packager.ingest._build_token_candidates`) so
    the lens output is meaningful.
    """
    literals = [
        {
            "name": name,
            "ref_count": len(files),
            "files": sorted(set(files)),
        }
        for name, files in sorted(
            db_names.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
    ]

    # Prefix clusters — group names by their leading two-segment prefix.
    by_prefix: Dict[str, List[str]] = {}
    for name in db_names:
        head = "_".join(name.split("_")[:2])
        if not head or "_" not in name:
            continue
        by_prefix.setdefault(head, []).append(name)
    prefix_clusters = []
    for prefix, members in by_prefix.items():
        if len(members) < 2:
            continue
        shared = _shared_prefix_tokens(members, min_tokens=2)
        if not shared:
            continue
        prefix_clusters.append(
            {
                "prefix": shared,
                "members": sorted(members),
                "count": len(members),
            }
        )
    prefix_clusters.sort(key=lambda c: (-c["count"], c["prefix"]))

    # Suffix clusters — group by trailing token (eg _T / _V / _M).
    by_suffix: Dict[str, List[str]] = {}
    for name in db_names:
        if "_" not in name:
            continue
        tail = name.rsplit("_", 1)[-1]
        if not tail:
            continue
        by_suffix.setdefault(tail, []).append(name)
    suffix_clusters = [
        {"suffix": suffix, "members": sorted(members), "count": len(members)}
        for suffix, members in by_suffix.items()
        if len(members) >= 2
    ]
    suffix_clusters.sort(key=lambda c: (-c["count"], c["suffix"]))

    return {
        "literal_count": len(literals),
        "literals": literals,
        "prefix_clusters": prefix_clusters,
        "suffix_clusters": suffix_clusters,
    }


# ---------------------------------------------------------------
# Hash-gated write
# ---------------------------------------------------------------


class HashMismatchError(Exception):
    """Raised when the on-disc file no longer matches ``expected_hash``."""


def safe_write(
    path: str,
    proposed_content: str,
    expected_hash: str,
) -> Dict[str, Any]:
    """Atomically write ``proposed_content`` to ``path`` iff the current
    file hashes to ``expected_hash``.

    A non-existent file is treated as :data:`ABSENT_FILE_HASH`; pass
    that value to create a new file.  Any mismatch raises
    :class:`HashMismatchError`.

    Atomic on success: writes via tempfile in the same directory then
    ``os.replace``, so a partial write cannot corrupt the target.

    Returns a small dict describing what happened — caller surfaces it
    to the MCP client.
    """
    if not isinstance(proposed_content, str):
        raise TypeError("proposed_content must be a string")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError("expected_hash must be a non-empty string")

    current_hash = file_hash(path)
    if current_hash != expected_hash:
        raise HashMismatchError(
            f"file {path!r} hash {current_hash} does not match "
            f"expected {expected_hash} — file changed between propose "
            "and apply, or expected_hash is stale."
        )

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=".ships_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(proposed_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {
        "path": path,
        "created": current_hash == ABSENT_FILE_HASH,
        "new_hash": content_hash(proposed_content),
    }


# ---------------------------------------------------------------
# Proposal envelope (shared shape for all authoring tools)
# ---------------------------------------------------------------


def build_proposal(
    path: str,
    proposed_content: str,
    *,
    validation_errors: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Assemble the standard authoring-tool return envelope.

    Authoring tools share this shape so the MCP client (and tests)
    can rely on a uniform contract regardless of file kind.
    """
    current = read_or_empty(path)
    current_h = content_hash(current) if os.path.exists(path) else ABSENT_FILE_HASH
    diff = unified_diff(current, proposed_content, path)
    return {
        "path": path,
        "current_content": current,
        "proposed_content": proposed_content,
        "diff": diff,
        "expected_hash": current_h,
        "validation": {
            "valid": not bool(validation_errors),
            "errors": list(validation_errors or []),
        },
        "unchanged": current == proposed_content,
    }
