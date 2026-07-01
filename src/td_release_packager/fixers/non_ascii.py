"""Auto-fixer for the ``non_ascii`` inspect rule.

Substitutes deterministic non-ASCII characters (em-dash, bullet, right
arrow, box-drawing horizontal) with their lossless ASCII equivalents.
Characters NOT in the substitution map (notably ``U+FFFD`` — original
byte is unrecoverable) are deliberately left alone and continue to
surface as ``[non_ascii]`` findings on the next inspect run.

Idempotent. Skips SHIPS-generated paths.
"""

from __future__ import annotations

import os

from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile

# Codepoints whose lossless ASCII equivalent is well-known. These are
# the rich-text characters that word processors and rich-text editors
# inject silently. Every entry MUST be a substitution that preserves
# meaning — no "best guess" mode. ``U+FFFD`` is deliberately NOT in
# this set: the original byte is lost, so we cannot substitute safely.
_NON_ASCII_AUTO_FIX_REPLACEMENTS: dict[str, str] = {
    "—": " - ",  # em-dash → spaced hyphen (NOT "--" — that opens a SQL comment)
    "•": "-",  # bullet → hyphen
    "→": "->",  # rightwards arrow → ASCII arrow
    "─": "-",  # box drawings light horizontal → hyphen
}


def fix_non_ascii(source_dir: str, dry_run: bool = False) -> FixResult:
    """Substitute non-ASCII characters that have a known ASCII equivalent.

    Walks ``source_dir`` using the same file-discovery rules as
    ``validate_directory`` (same extensions, same generated-path
    exclusions), reads each file as UTF-8 strict (non-UTF-8 files are
    recorded under :attr:`FixResult.errors` and skipped), and replaces
    every character whose codepoint is in the built-in substitution map
    with the documented ASCII equivalent.

    Args:
        source_dir: Directory to walk (typically the SHIPS project root).
        dry_run:    When True, compute the fix list without writing.

    Returns:
        :class:`FixResult` with ``rule_id="non_ascii"``,
        ``totals["chars_substituted"]`` counting the total substitutions,
        and per-file ``details["substitutions"]`` mapping
        ``"U+XXXX"`` codepoint strings to counts, plus
        ``details["total_chars_substituted"]`` for the per-file total.
    """
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import _prune_generated_dirs

    extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    extensions.add(".jar")

    result = FixResult(rule_id="non_ascii", dry_run=dry_run)
    total_chars_substituted = 0

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

            # Fast path: nothing to do.
            if not any(ch in raw for ch in _NON_ASCII_AUTO_FIX_REPLACEMENTS):
                continue

            counts: dict[str, int] = {}
            new_content = raw
            for ch, replacement in _NON_ASCII_AUTO_FIX_REPLACEMENTS.items():
                if ch not in new_content:
                    continue
                counts[ch] = new_content.count(ch)
                new_content = new_content.replace(ch, replacement)

            if new_content == raw:
                continue

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

            file_total = sum(counts.values())
            total_chars_substituted += file_total
            rel_path = os.path.relpath(file_path, source_dir)
            result.files_changed.append(
                FixResultFile(
                    file=rel_path,
                    details={
                        "substitutions": {
                            f"U+{ord(c):04X}": count for c, count in counts.items()
                        },
                        "total_chars_substituted": file_total,
                    },
                )
            )

    result.totals["chars_substituted"] = total_chars_substituted
    return result


SPEC = register(
    FixerSpec(
        rule_id="non_ascii",
        apply=fix_non_ascii,
        default_on=False,
        write_scope="payload",
    )
)
