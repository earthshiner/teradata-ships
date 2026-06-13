"""
source_migrator.py — Apply a SHIPS tokenisation config to a
source DDL tree, without requiring the ``sed`` binary.

The canonical config file is ``<project>/config/tokenise.conf``
(loaded automatically by harvest/process). It supports two rule
forms in one file:

    s/LHS/RHS/g                       — literal substitution
    regex::PATTERN:=REPLACEMENT       — regex with $1..$9 back-refs

``import-legacy`` generates the literal form for ``$VAR`` /
``&&VAR&&`` migration. Hand-author the regex form when you need
capture groups (e.g. retokenising a project prefix on every
``<Project>_<DOMAIN>_<TIER>_<KIND>`` name).

Preview / apply::

    python -m td_release_packager migrate-source \\
        --tokenise-config  config/tokenise.conf \\
        --source           <source_dir> [--dry-run]

For backwards compatibility, ``--sed`` and
``config/legacy_migration.sed`` are still accepted as deprecated
aliases — they emit a one-line warning and will be removed in a
future release.
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

# Matches  regex::PATTERN:=REPLACEMENT  (issue #259).
#
# Split on the FIRST ``:=`` after the leading ``regex::`` keyword so
# the pattern itself may contain colons. Trailing whitespace is
# tolerated; everything else is part of the replacement.
_REGEX_RULE_PREFIX = "regex::"


@dataclass(frozen=True)
class MigrationRule:
    """One parsed substitution rule.

    Attributes:
        raw_lhs:   The literal pattern from the source file (for
                   display and hit-tracking keys). For ``s/.../.../g``
                   rules, this is the LHS exactly as written. For
                   ``regex::PATTERN:=REPLACEMENT`` rules, this is the
                   PATTERN exactly as written.
        lhs:       Compiled regex.
        rhs:       Replacement string. For literal rules this is the
                   literal RHS. For regex rules, ``$1..$9`` have been
                   translated to Python's ``\\1..\\9`` backref form so
                   ``Match.expand(rhs)`` Just Works.
        global_:   True when ``g`` flag was present (always True for
                   ``regex::`` rules — substitute every occurrence).
        is_regex:  True for ``regex::`` rules. Drives expansion in
                   the apply path: regex rules call ``m.expand(rhs)``
                   so back-references are honoured; literal rules
                   return ``rhs`` verbatim so a literal ``\\1`` in the
                   replacement stays a literal ``\\1``.
    """

    raw_lhs: str
    lhs: re.Pattern
    rhs: str
    global_: bool
    is_regex: bool = False


def _translate_regex_replacement(raw: str) -> str:
    """Translate ``$1..$9`` back-references to Python's ``\\1..\\9`` form.

    Conventions (familiar from sed / Perl):

    - ``$1..$9``  → ``\\1..\\9``   (capture group references)
    - ``$$``      → ``$``          (literal dollar)
    - ``$`` followed by anything else: kept as-is.

    Any existing ``\\`` in ``raw`` is escaped first so a literal
    backslash in the user's replacement does not collide with the
    backslash we inject for the back-reference.
    """
    # Escape literal backslashes so they survive ``Match.expand``.
    escaped = raw.replace("\\", "\\\\")

    out = []
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        nxt = escaped[i + 1] if i + 1 < len(escaped) else ""
        if nxt == "$":
            out.append("$")
            i += 2
        elif nxt.isdigit() and nxt != "0":
            out.append("\\" + nxt)
            i += 2
        else:
            out.append("$")
            i += 1
    return "".join(out)


def _unescape_sed(text: str) -> str:
    """Undo sed's backslash-escape of the ``/`` delimiter."""
    return text.replace("\\/", "/")


def parse_migration_sed(content: str) -> Tuple[List[MigrationRule], List[str]]:
    """Parse a SHIPS tokenisation config (``tokenise.conf``).

    Recognises two rule forms:

    1. ``s/LHS/RHS/[flags]`` — literal-string substitution. LHS is
       ``re.escape``'d, RHS used verbatim. This is the form
       ``import-legacy`` generates.
    2. ``regex::PATTERN:=REPLACEMENT`` — full regex substitution.
       PATTERN is compiled as Python regex (no escaping). REPLACEMENT
       supports ``$1..$9`` back-references and ``$$`` for a literal
       ``$``. Inline flags such as ``(?i)`` work. Issue #259.

    Comments (``#``) and blank lines are skipped without warning.
    Unrecognised non-blank, non-comment lines are returned in the
    second element of the tuple so the caller can warn the user.

    Args:
        content: Full text of the migration file.

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

        # -- Regex rule:  regex::PATTERN:=REPLACEMENT --
        if stripped.startswith(_REGEX_RULE_PREFIX):
            body = stripped[len(_REGEX_RULE_PREFIX) :]
            if ":=" not in body:
                logger.warning(
                    "Skipping regex rule missing ':=' separator: %s", stripped
                )
                skipped.append(stripped)
                continue
            raw_lhs, raw_rhs = body.split(":=", 1)
            try:
                lhs_re = re.compile(raw_lhs)
            except re.error as exc:
                logger.warning(
                    "Skipping regex rule with unparseable PATTERN %r: %s",
                    raw_lhs,
                    exc,
                )
                skipped.append(stripped)
                continue
            translated_rhs = _translate_regex_replacement(raw_rhs)
            rules.append(
                MigrationRule(
                    raw_lhs=raw_lhs,
                    lhs=lhs_re,
                    rhs=translated_rhs,
                    global_=True,  # Regex rules always replace every match.
                    is_regex=True,
                )
            )
            continue

        # -- Literal rule:  s/LHS/RHS/[flags] --
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
                is_regex=False,
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

    For ``s/.../.../g`` rules the replacement is returned verbatim so a
    literal ``\\1`` in the RHS stays a literal ``\\1``. For
    ``regex::PATTERN:=REPLACEMENT`` rules the replacement is expanded
    via ``Match.expand`` so ``$1..$9`` back-references (translated at
    parse time to ``\\1..\\9``) resolve to the matched capture groups.

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
            if rule.is_regex:
                return m.expand(rule.rhs)
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
        "--tokenise-config",
        dest="tokenise_config",
        default=None,
        metavar="CONFIG_FILE",
        help="Path to the tokenisation config "
        "(canonical name: ``config/tokenise.conf``).",
    )
    p.add_argument(
        "--sed",
        dest="sed_legacy_flag",
        default=None,
        metavar="SED_FILE",
        help="Deprecated alias for ``--tokenise-config``. "
        "Will be removed in a future release.",
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

    config_path_arg = args.tokenise_config or args.sed_legacy_flag
    if not config_path_arg:
        print(
            "ERROR: --tokenise-config is required (deprecated alias: --sed)",
            file=sys.stderr,
        )
        return 2
    if args.sed_legacy_flag and not args.tokenise_config:
        print(
            "  ⚠ --sed is deprecated; use --tokenise-config.",
            file=sys.stderr,
        )

    config_path = Path(config_path_arg)
    if not config_path.is_file():
        print(
            f"ERROR: tokenisation config not found: {config_path}",
            file=sys.stderr,
        )
        return 1

    source_path = Path(args.source)
    if not source_path.is_dir():
        print(f"ERROR: source directory not found: {source_path}", file=sys.stderr)
        return 1

    content = config_path.read_text(encoding="utf-8")
    rules, skipped = parse_migration_sed(content)

    if not rules:
        print(
            f"ERROR: no parseable tokenisation rules in {config_path}",
            file=sys.stderr,
        )
        return 1

    if skipped:
        for line in skipped:
            print(f"  WARN: skipped unparseable rule: {line}")

    print("=" * 64)
    print(f"  {'DRY RUN — ' if args.dry_run else ''}migrate-source")
    print("=" * 64)
    print(f"  Rules:      {len(rules)}")
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
