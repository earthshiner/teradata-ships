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
import tempfile
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
