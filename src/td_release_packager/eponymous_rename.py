"""
eponymous_rename.py — Auto-rename DDL files to match their content.

Extracts the qualified Database.ObjectName from each DDL file and
renames (or copies) the file so the filename matches the DDL. This
eliminates the friction of manual file naming and ensures the
Inspect eponymous validation rule passes.

Integration points:
    - Builder: call rename_to_eponymous() during package construction
    - Ingest:  call rename_to_eponymous() during harvest
    - CLI:     python eponymous_rename.py --dir <path> [--dry-run]

The function is non-destructive by default — it copies to an output
directory. Use --in-place for direct renames.

Author: Paul / Teradata Field Engineering
"""

import argparse
import logging
import re
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# -- Comment stripping (same as ddl_parser.py) --
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_sql_comments(text: str) -> str:
    """Remove SQL comments for safe regex classification."""
    stripped = _BLOCK_COMMENT_RE.sub(" ", text)
    stripped = _LINE_COMMENT_RE.sub(" ", stripped)
    return stripped


# -- DDL patterns for name extraction --
# Covers all object types that follow Database.ObjectName convention.
_QUALIFIED_DDL_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+"
    r"(?:(?:MULTISET|SET)\s+)?"
    r"(?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:JOIN\s+INDEX|HASH\s+INDEX|TABLE|VIEW|MACRO|PROCEDURE|"
    r"FUNCTION|TRIGGER)\s+"
    r"((?:\{\{[A-Za-z_]\w*\}\}|\"[^\"]+\"|[A-Za-z_]\w*)"
    r"(?:\.(?:\{\{[A-Za-z_]\w*\}\}|\"[^\"]+\"|[A-Za-z_]\w*))?)",
    re.IGNORECASE,
)

# -- Single-name DDL patterns (no database qualifier) --
_SINGLE_NAME_DDL_RE = re.compile(
    r"(?:CREATE)\s+"
    r"(?:DATABASE|USER|ROLE|PROFILE|MAP|AUTHORIZATION|"
    r"FOREIGN\s+SERVER)\s+"
    r"((?:\{\{[A-Za-z_]\w*\}\}|\"[^\"]+\"|[A-Za-z_]\w*))",
    re.IGNORECASE,
)

# -- Extension map by detected object type keyword --
_EXT_MAP = {
    "TABLE": ".tbl",
    "VIEW": ".viw",
    "MACRO": ".mcr",
    "PROCEDURE": ".spl",
    "FUNCTION": ".fnc",
    "TRIGGER": ".trg",
    "JOIN INDEX": ".jix",
    "HASH INDEX": ".idx",
    "DATABASE": ".db",
    "USER": ".usr",
    "ROLE": ".rol",
    "PROFILE": ".prf",
    "MAP": ".map",
    "AUTHORIZATION": ".auth",
    "FOREIGN SERVER": ".fsvr",
}

# -- Object type keyword extraction --
_OBJ_TYPE_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+"
    r"(?:(?:MULTISET|SET)\s+)?"
    r"(?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(JOIN\s+INDEX|HASH\s+INDEX|TABLE|VIEW|MACRO|PROCEDURE|"
    r"FUNCTION|TRIGGER|DATABASE|USER|ROLE|PROFILE|MAP|"
    r"AUTHORIZATION|FOREIGN\s+SERVER)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------


class RenameAction(NamedTuple):
    """A single file rename action."""

    original_path: Path
    new_filename: str
    qualified_name: str
    object_type: str
    reason: str  # 'renamed' | 'already_correct' | 'skipped'


# ---------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------


def _strip_quotes(identifier: str) -> str:
    """Remove surrounding double quotes from an identifier."""
    s = identifier.strip()
    return s[1:-1] if s.startswith('"') and s.endswith('"') else s


def extract_eponymous_name(
    ddl_text: str,
) -> Optional[Tuple[str, str, str]]:
    """
    Extract the eponymous filename from DDL content.

    Parses the DDL to find the qualified name and object type,
    then computes the correct filename.

    Args:
        ddl_text: Raw DDL file content.

    Returns:
        Tuple of (eponymous_filename, qualified_name, object_type)
        or None if the DDL cannot be parsed.
    """
    clean = _strip_sql_comments(ddl_text)

    # Detect object type keyword
    type_match = _OBJ_TYPE_RE.search(clean)
    if not type_match:
        return None
    obj_type_raw = type_match.group(1).upper()
    # Normalise multi-word types
    obj_type_key = re.sub(r"\s+", " ", obj_type_raw)

    # Determine the correct extension
    ext = _EXT_MAP.get(obj_type_key)
    if ext is None:
        return None

    # Extract the qualified name
    # Try two-part names first (Database.Object)
    match = _QUALIFIED_DDL_RE.search(clean)
    if match:
        raw_name = match.group(1)
        # Keep tokens as-is, strip quotes from identifiers
        parts = raw_name.split(".", 1)
        if len(parts) == 2:
            db_part = parts[0] if parts[0].startswith("{{") else _strip_quotes(parts[0])
            obj_part = (
                parts[1] if parts[1].startswith("{{") else _strip_quotes(parts[1])
            )
            qualified = f"{db_part}.{obj_part}"
        else:
            part = parts[0] if parts[0].startswith("{{") else _strip_quotes(parts[0])
            qualified = part
        return (f"{qualified}{ext}", qualified, obj_type_key)

    # Try single-name patterns (DATABASE, USER, etc.)
    match = _SINGLE_NAME_DDL_RE.search(clean)
    if match:
        raw_name = match.group(1)
        name = raw_name if raw_name.startswith("{{") else _strip_quotes(raw_name)
        return (f"{name}{ext}", name, obj_type_key)

    return None


def compute_renames(
    file_paths: List[Path],
) -> List[RenameAction]:
    """
    Compute rename actions for a list of DDL files.

    For each file, extracts the qualified name from the DDL content
    and computes what the filename should be. Returns a list of
    actions — some files may already be correct, some need renaming,
    and some may be unparseable (skipped).

    Args:
        file_paths: List of DDL file paths to check.

    Returns:
        List of RenameAction tuples.
    """
    actions: List[RenameAction] = []

    for fpath in file_paths:
        try:
            content = fpath.read_text(encoding="utf-8")
        except Exception as e:
            actions.append(
                RenameAction(
                    original_path=fpath,
                    new_filename=fpath.name,
                    qualified_name="",
                    object_type="",
                    reason=f"skipped: {e}",
                )
            )
            continue

        result = extract_eponymous_name(content)
        if result is None:
            actions.append(
                RenameAction(
                    original_path=fpath,
                    new_filename=fpath.name,
                    qualified_name="",
                    object_type="",
                    reason="skipped: could not parse DDL",
                )
            )
            continue

        eponymous_name, qualified, obj_type = result

        if fpath.name == eponymous_name:
            actions.append(
                RenameAction(
                    original_path=fpath,
                    new_filename=eponymous_name,
                    qualified_name=qualified,
                    object_type=obj_type,
                    reason="already_correct",
                )
            )
        else:
            actions.append(
                RenameAction(
                    original_path=fpath,
                    new_filename=eponymous_name,
                    qualified_name=qualified,
                    object_type=obj_type,
                    reason="renamed",
                )
            )

    return actions


def rename_to_eponymous(
    source_dir: Path,
    output_dir: Optional[Path] = None,
    in_place: bool = False,
    dry_run: bool = False,
    extensions: Optional[List[str]] = None,
) -> List[RenameAction]:
    """
    Rename DDL files to match their qualified Database.ObjectName.

    Scans source_dir for DDL files, parses each to extract the
    qualified name, and renames/copies to the eponymous filename.

    Args:
        source_dir:  Directory containing DDL files.
        output_dir:  Write renamed files here (preserving relative
                     paths). If None and not in_place, returns
                     actions without writing.
        in_place:    Rename files in source_dir directly.
        dry_run:     Compute actions without writing anything.
        extensions:  File extensions to process. Defaults to all
                     DDL extensions.

    Returns:
        List of RenameAction tuples describing what was (or would
        be) done.
    """
    if extensions is None:
        extensions = list(_EXT_MAP.values())

    # Discover files
    file_paths = []
    for ext in extensions:
        file_paths.extend(sorted(source_dir.rglob(f"*{ext}")))

    # Compute renames
    actions = compute_renames(file_paths)

    if dry_run:
        return actions

    # Apply renames
    for action in actions:
        if action.reason.startswith("skipped") or action.reason == "already_correct":
            continue

        src = action.original_path

        if in_place:
            dest = src.parent / action.new_filename
            if dest.exists() and dest != src:
                logger.warning(
                    "Target already exists, skipping: %s → %s",
                    src.name,
                    action.new_filename,
                )
                continue
            src.rename(dest)
            logger.info("Renamed: %s → %s", src.name, action.new_filename)

        elif output_dir is not None:
            rel_parent = src.parent.relative_to(source_dir)
            dest_dir = output_dir / rel_parent
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / action.new_filename

            content = src.read_text(encoding="utf-8")
            dest.write_text(content, encoding="utf-8")
            logger.info(
                "Copied: %s → %s",
                src.name,
                dest,
            )

    return actions


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


def _print_report(actions: List[RenameAction], dry_run: bool) -> None:
    """Print a human-readable summary of rename actions."""
    renamed = [a for a in actions if a.reason == "renamed"]
    correct = [a for a in actions if a.reason == "already_correct"]
    skipped = [a for a in actions if a.reason.startswith("skipped")]

    mode = "DRY RUN" if dry_run else "APPLIED"

    print(f"\n{'=' * 70}")
    print(f"  Eponymous Rename — {mode}")
    print(f"{'=' * 70}\n")

    if renamed:
        for a in renamed:
            print(
                f"  {'WOULD RENAME' if dry_run else 'RENAMED'}  {a.original_path.name}"
            )
            print(f"        →  {a.new_filename}")
            print(f"           ({a.object_type}: {a.qualified_name})\n")

    if skipped:
        for a in skipped:
            print(f"  SKIPPED  {a.original_path.name}  ({a.reason})")
        print()

    print(f"{'─' * 70}")
    print(f"  Files scanned:      {len(actions)}")
    print(f"  Already correct:    {len(correct)}")
    print(f"  Renamed:            {len(renamed)}")
    print(f"  Skipped:            {len(skipped)}")
    print(f"  Mode:               {mode}")
    print(f"{'─' * 70}\n")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="eponymous_rename",
        description=(
            "Auto-rename DDL files to match their qualified\n"
            "Database.ObjectName as declared in the DDL content.\n"
            "\n"
            "Parses each file, extracts the qualified name, and\n"
            "renames the file so filename matches content.\n"
            "\n"
            "Example:\n"
            "  MortgagePlatform_Domain_AMLRiskRating_R.tbl\n"
            "  → {{DOM_DATABASE_T}}.AMLRiskRating_R.tbl"
        ),
        epilog=(
            "output modes (exactly one required):\n"
            "  --dry-run     Preview renames without writing\n"
            "  --output DIR  Copy renamed files to a new directory\n"
            "  --in-place    Rename files directly (destructive)\n"
            "\n"
            "examples:\n"
            "  python eponymous_rename.py --dir payload/ --dry-run\n"
            "  python eponymous_rename.py --dir payload/ --output normalised/\n"
            "  python eponymous_rename.py --dir payload/ --in-place\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Directory to scan recursively for DDL files.",
    )

    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview renames without writing.",
    )
    output_group.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="Copy renamed files to this directory.",
    )
    output_group.add_argument(
        "--in-place",
        action="store_true",
        help="Rename files directly (destructive).",
    )
    args = parser.parse_args()

    if not args.dry_run and args.output is None and not args.in_place:
        parser.error(
            "No output mode specified. Use one of:\n"
            "  --dry-run     Preview renames\n"
            "  --output DIR  Copy to a new directory\n"
            "  --in-place    Rename directly (destructive)"
        )

    actions = rename_to_eponymous(
        source_dir=args.dir,
        output_dir=args.output,
        in_place=args.in_place,
        dry_run=args.dry_run,
    )

    _print_report(actions, args.dry_run)


if __name__ == "__main__":
    main()
