"""
source_migrator.py — Apply a SHIPS ``legacy_migration.sed`` to a
source DDL tree, without requiring the ``sed`` binary.

``import-legacy`` produces a ``legacy_migration.sed`` that converts
legacy substitution markers (``$VAR``, ``${VAR}``, ``&&VAR&&``) to
SHIPS ``{{TOKEN}}`` references. On Linux/Mac the DBA would run::

    find <src> -type f -name '*.sql' -exec sed -i -f legacy_migration.sed {} +

On Windows ``sed`` is not available by default. This module provides
an equivalent pure-Python path::

    python -m td_release_packager migrate-source \\
        --sed  config/legacy_migration.sed \\
        --source <source_dir> [--dry-run]

The sed parser understands only the subset of sed that
``import-legacy`` emits::

    s/<literal_marker>/{{TOKEN}}/g

It does NOT attempt to handle general sed syntax (address ranges,
multi-line scripts, back-references, etc.).  Any line it cannot
interpret is silently skipped with a warning so a malformed line
cannot corrupt files.

Run with ``--dry-run`` first to preview what would change.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Sed rule parsing
# ---------------------------------------------------------------

# Matches  s/LHS/RHS/[flags]
# The LHS and RHS may contain escaped slashes (\/).
_SED_RULE_RE = re.compile(r"^s/((?:[^/\\]|\\.)*?)/((?:[^/\\]|\\.)*)/([a-zA-Z]*)\s*$")


@dataclass(frozen=True)
class MigrationRule:
    """One parsed ``s/LHS/RHS/g`` substitution rule.

    Attributes:
        raw_lhs:  The literal pattern from the sed script (with any
                  backslash-escapes intact). Used for display.
        lhs:      Compiled regex for the left-hand side.
        rhs:      Replacement string (after sed-escape processing).
        global_:  True when the ``g`` flag was present (always for
                  SHIPS-generated scripts, but honoured from input).
    """

    raw_lhs: str
    lhs: re.Pattern
    rhs: str
    global_: bool


def _unescape_sed(text: str) -> str:
    """Undo sed's backslash-escape of the ``/`` delimiter."""
    return text.replace("\\/", "/")


def parse_migration_sed(content: str) -> Tuple[List[MigrationRule], List[str]]:
    """Parse a ``legacy_migration.sed`` file.

    Recognises only the ``s/LHS/RHS/[flags]`` form that
    ``import-legacy`` generates.  Comments (``#``) and blank lines
    are skipped without warning.  Unrecognised non-blank, non-comment
    lines are returned in the second element of the tuple so the
    caller can warn the user.

    Args:
        content: Full text of the sed script.

    Returns:
        ``(rules, skipped_lines)`` where ``rules`` is a list of
        ``MigrationRule`` objects in file order and ``skipped_lines``
        is a list of raw lines that could not be parsed.
    """
    rules: List[MigrationRule] = []
    skipped: List[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m = _SED_RULE_RE.match(stripped)
        if not m:
            skipped.append(stripped)
            continue

        raw_lhs, raw_rhs, flags = m.group(1), m.group(2), m.group(3)
        lhs_pattern = re.escape(_unescape_sed(raw_lhs))
        rhs = _unescape_sed(raw_rhs)
        global_ = "g" in flags.lower()

        try:
            lhs_re = re.compile(lhs_pattern)
        except re.error as exc:
            logger.warning(
                "Skipping sed rule with unparseable LHS %r: %s",
                raw_lhs,
                exc,
            )
            skipped.append(stripped)
            continue

        rules.append(
            MigrationRule(
                raw_lhs=raw_lhs,
                lhs=lhs_re,
                rhs=rhs,
                global_=global_,
            )
        )

    return (rules, skipped)


# ---------------------------------------------------------------
# File application
# ---------------------------------------------------------------


@dataclass
class MigrationResult:
    """Aggregate outcome of a ``migrate_source_directory`` run.

    Attributes:
        files_scanned:  Total files inspected.
        files_changed:  Files where at least one rule matched.
        files_skipped:  Files skipped (binary, unreadable, etc.).
        rule_hits:      Per-rule match count across all files.
        changed_files:  Paths of changed (or would-change) files.
        dry_run:        Whether the run was a preview only.
    """

    files_scanned: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    rule_hits: dict = field(default_factory=dict)
    changed_files: List[str] = field(default_factory=list)
    dry_run: bool = False


def _apply_rules_to_text(content: str, rules: List[MigrationRule]) -> Tuple[str, dict]:
    """Apply every rule to ``content`` in order.

    Returns:
        ``(new_content, hits)`` where ``hits`` maps ``raw_lhs`` to
        the number of substitutions made.
    """
    result = content
    hits: dict = {}
    for rule in rules:
        count = [0]

        def _replace(m, rule=rule, count=count):
            count[0] += 1
            return rule.rhs

        new = rule.lhs.sub(
            _replace,
            result,
            count=0 if rule.global_ else 1,
        )
        if count[0]:
            hits[rule.raw_lhs] = hits.get(rule.raw_lhs, 0) + count[0]
        result = new

    return (result, hits)


def apply_migration_rules_to_text(
    content: str,
    rules: List[MigrationRule],
) -> Tuple[str, dict]:
    """Apply parsed legacy migration rules to one SQL text buffer.

    This is the in-memory counterpart to ``migrate_source_directory``.
    Harvest/process use it to normalise legacy ``$VAR`` / ``&&VAR&&``
    markers before classification without rewriting the user's source
    checkout.
    """
    return _apply_rules_to_text(content, rules)


def migrate_source_directory(
    source_dir: str,
    rules: List[MigrationRule],
    dry_run: bool = False,
    project_dir: Optional[str] = None,
) -> MigrationResult:
    """Apply migration rules to every SQL-bearing file in ``source_dir``.

    Uses the SHIPS discovery resolver for extension selection so any
    project-specific extensions in ``ships.yaml`` are honoured.

    Args:
        source_dir:   Root of the source DDL tree to migrate.
        rules:        Parsed migration rules from ``parse_migration_sed``.
        dry_run:      When ``True``, compute what would change but do
                      not write any files.
        project_dir:  Optional SHIPS project root, consulted by the
                      discovery resolver for ``ships.yaml`` overrides.

    Returns:
        ``MigrationResult`` summarising the run.
    """
    from td_release_packager.discovery import resolve_harvest_extensions

    extensions = resolve_harvest_extensions(project_dir=project_dir)
    result = MigrationResult(dry_run=dry_run)

    for root, dirs, files in os.walk(source_dir):
        dirs.sort()
        for filename in sorted(files):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                original = Path(file_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                result.files_skipped += 1
                logger.debug("Skipped (unreadable): %s", file_path)
                continue

            new_content, hits = _apply_rules_to_text(original, rules)

            if not hits:
                continue  # no change

            result.files_changed += 1
            result.changed_files.append(file_path)
            for raw_lhs, count in hits.items():
                result.rule_hits[raw_lhs] = result.rule_hits.get(raw_lhs, 0) + count

            if not dry_run:
                Path(file_path).write_text(new_content, encoding="utf-8")
                logger.info("Updated: %s", file_path)

    return result


# ---------------------------------------------------------------
# CLI entry point (invoked from td_release_packager cli.py)
# ---------------------------------------------------------------


def main(argv=None):
    """Standalone CLI for ``migrate-source``."""
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="td_release_packager migrate-source",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--sed",
        required=True,
        metavar="SED_FILE",
        help="Path to the ``legacy_migration.sed`` generated by "
        "``import-legacy``. Can also be any sed script containing "
        "``s/LHS/RHS/g`` rules.",
    )
    p.add_argument(
        "--source",
        required=True,
        metavar="SOURCE_DIR",
        help="Root of the source DDL tree to migrate.",
    )
    p.add_argument(
        "--project",
        metavar="PROJECT_DIR",
        help="Optional SHIPS project root. Consulted by the discovery "
        "resolver for ``ships.yaml`` extension overrides.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without writing any files.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    sed_path = Path(args.sed)
    if not sed_path.is_file():
        print(f"ERROR: sed file not found: {sed_path}", file=sys.stderr)
        return 1

    source_path = Path(args.source)
    if not source_path.is_dir():
        print(f"ERROR: source directory not found: {source_path}", file=sys.stderr)
        return 1

    content = sed_path.read_text(encoding="utf-8")
    rules, skipped = parse_migration_sed(content)

    if not rules:
        print(f"ERROR: no parseable s/LHS/RHS/g rules in {sed_path}", file=sys.stderr)
        return 1

    if skipped:
        for line in skipped:
            print(f"  WARN: skipped unparseable sed line: {line}")

    print("=" * 64)
    print(f"  {'DRY RUN — ' if args.dry_run else ''}migrate-source")
    print("=" * 64)
    print(f"  Sed rules:  {len(rules)}")
    print(f"  Source:     {source_path}")
    if args.dry_run:
        print("  Mode:       DRY RUN (no files will be written)")
    print()

    result = migrate_source_directory(
        source_dir=str(source_path),
        rules=rules,
        dry_run=args.dry_run,
        project_dir=args.project,
    )

    # Print per-file changes
    for path in result.changed_files:
        rel = os.path.relpath(path, str(source_path))
        verb = "Would change" if args.dry_run else "Updated"
        print(f"  {verb}: {rel}")

    print()
    print("=" * 64)
    print(f"  Files scanned:  {result.files_scanned}")
    print(
        f"  {'Would change' if args.dry_run else 'Changed'}:      {result.files_changed}"
    )
    if result.files_skipped:
        print(f"  Skipped:        {result.files_skipped} (binary or unreadable)")

    if result.rule_hits:
        print()
        print("  Substitutions made:")
        for raw_lhs, count in sorted(result.rule_hits.items(), key=lambda kv: -kv[1]):
            token = (
                "{{" + re.sub(r"^\\?\$\{?|(\}?)$|\^|&&", "", raw_lhs).rstrip("&") + "}}"
            )
            print(f"    {raw_lhs:30s} → {token}  ({count}×)")

    if args.dry_run and result.files_changed:
        print()
        print("  Re-run without --dry-run to apply changes.")

    print("=" * 64)

    return 0
