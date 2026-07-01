"""Auto-fixer for the ``ddl_terminator`` inspect rule.

Appends the missing terminating semicolon to deployable DDL statements
whose ending was left off. Idempotent. Skips SHIPS-generated paths
(``releases/``, ``.ships-work/``, ``_rollback/``) via the shared prune
helper.

Shared with ``validate.py`` for the detector primitives
(``_compute_terminator_insertions``, ``_strip_sql_comments``,
``_prune_generated_dirs``) — those are lint-side helpers that both this
fixer and the rule checker consume; extracting them into a shared
neutral home is a follow-up.
"""

from __future__ import annotations

import os

from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile


def _compute_terminator_insertions(stripped: str, raw: str) -> list[int]:
    """Return raw-content offsets where ``;`` should be inserted.

    The detector walks DDL verb starts in the comment-/string-stripped
    text. ``strip_comments_and_string_literals`` preserves character
    positions, so a stripped offset maps 1:1 to the raw content.

    For each segment whose stripped tail does not end with ``;``, we
    locate the last non-whitespace character of the segment in the
    *raw* content and report the index immediately AFTER it. Inserting
    ``;`` at that offset puts the terminator flush against the final
    token while preserving any trailing whitespace or comments.

    A file with no matching DDL verbs returns ``[]``.
    """
    # Lazy import to avoid pulling validate.py at module load — same
    # reasoning as the deferred imports in `fix_ddl_terminators`.
    from td_release_packager.validate import _DDL_TERMINATOR_START_RE

    matches = list(_DDL_TERMINATOR_START_RE.finditer(stripped))
    if not matches:
        return []

    insertions: list[int] = []
    for idx, match in enumerate(matches):
        seg_start = match.start()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(stripped)
        # Use the stripped text to decide whether a terminator is missing,
        # exactly as the detector does. This stops us from "fixing" a
        # statement that already has a ``;`` followed only by comments.
        seg_stripped = stripped[seg_start:seg_end].rstrip()
        if not seg_stripped or seg_stripped.endswith(";"):
            continue

        # Walk back through the STRIPPED segment to find the last
        # non-whitespace character. The stripper blanks comments and
        # string literals with spaces of the same length (positions
        # preserved), so trailing comments after the DDL are treated
        # as whitespace here — exactly what we want. Using ``raw``
        # would land the new ``;`` inside a trailing comment.
        insert_at = seg_end
        while insert_at > seg_start and stripped[insert_at - 1].isspace():
            insert_at -= 1
        if insert_at == seg_start:
            # Defensive guard: an all-whitespace segment cannot need a
            # terminator. Should not be reachable because the detector
            # would have skipped it too.
            continue
        insertions.append(insert_at)

    return insertions


def fix_ddl_terminators(source_dir: str, dry_run: bool = False) -> FixResult:
    """Append missing ``;`` terminators to deployable DDL statements.

    Walks ``source_dir`` using the same file-discovery rules as
    ``validate_directory`` (same extensions, same generated-path
    exclusions), re-uses validate's boundary regex on the comment- and
    string-stripped content, then inserts a semicolon at the last
    non-whitespace character of each violating statement segment in the
    *raw* file.

    Files that need no changes are not touched. Files inside SHIPS-
    generated paths are skipped entirely.

    The fix is idempotent: running it twice on a clean tree leaves the
    second run with ``files_changed == []``.

    Args:
        source_dir: Directory to walk (typically the SHIPS project root).
        dry_run:    When True, compute the fix list without writing.
                    The returned result reports what *would* have changed.

    Returns:
        :class:`FixResult` with ``rule_id="ddl_terminator"``,
        ``totals["statements_fixed"]`` counting the total semicolons
        that were (or would be) inserted, and per-file
        ``details["statements_fixed"]`` on each :class:`FixResultFile`.
    """
    # Deferred imports — validate.py is heavyweight and this module is
    # imported at package load time, so lazy-loading keeps
    # ``from td_release_packager.fixers import FIX_REGISTRY`` cheap.
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import (
        _prune_generated_dirs,
        _strip_sql_comments,
    )

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.add(".jar")

    result = FixResult(rule_id="ddl_terminator", dry_run=dry_run)
    total_statements_fixed = 0

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(
                    {
                        "file": os.path.relpath(file_path, source_dir),
                        "error": f"read failed: {exc}",
                    }
                )
                continue

            stripped = _strip_sql_comments(raw)
            insertions = _compute_terminator_insertions(stripped, raw)
            if not insertions:
                continue

            # Apply insertions right-to-left so earlier offsets stay
            # valid as the buffer grows.
            new_content = raw
            for offset in sorted(insertions, reverse=True):
                new_content = new_content[:offset] + ";" + new_content[offset:]

            if not dry_run:
                try:
                    with open(file_path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(new_content)
                except OSError as exc:
                    result.errors.append(
                        {
                            "file": os.path.relpath(file_path, source_dir),
                            "error": f"write failed: {exc}",
                        }
                    )
                    continue

            rel_path = os.path.relpath(file_path, source_dir)
            result.files_changed.append(
                FixResultFile(
                    file=rel_path, details={"statements_fixed": len(insertions)}
                )
            )
            total_statements_fixed += len(insertions)

    result.totals["statements_fixed"] = total_statements_fixed
    return result


SPEC = register(
    FixerSpec(
        rule_id="ddl_terminator",
        apply=fix_ddl_terminators,
        default_on=True,
        write_scope="payload",
    )
)
