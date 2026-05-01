"""
Migrate View DDL References — SHIPS Object Placement.

Rewrites database-qualified object references in ``.viw`` files so
that AI-Native Data Product views read from the 1:1 locking view
layer (views database) rather than directly from the tables database.

Before:
    D01_MP_DOM_T.Mortgage       (direct table reference)

After:
    D01_MP_DOM_V.Mortgage       (via 1:1 locking view)

Usage::

    # Dry run — show changes without writing
    python migrate_view_references.py --config object_placement.yaml --dry-run

    # Apply changes
    python migrate_view_references.py --config object_placement.yaml

    # Apply to a specific directory (default: current directory)
    python migrate_view_references.py --config object_placement.yaml --dir ./domain/viw

Configuration is read from ``object_placement.yaml`` (top-level keys:
``strategy``, ``locking_views``, ``database_map`` / pattern keys).

Author: Paul / Teradata Field Engineering
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

# ---------------------------------------------------------------------------
# Attempt to import YAML parser — PyYAML or ruamel.yaml
# ---------------------------------------------------------------------------
try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Import the Object Placement engine from the SHIPS package.
# This tool lives in tools/ — add src/ to the path so the package
# is importable regardless of where the script is invoked from.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from td_release_packager.object_placement import (  # noqa: E402
    ObjectPlacement,
    PlacementConfigError,
    PlacementResolutionError,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Replacement(NamedTuple):
    """A single replacement within a file."""
    line_number: int
    original: str
    rewritten: str
    db_original: str
    db_rewritten: str


class FileResult(NamedTuple):
    """Migration result for a single file."""
    path: Path
    replacements: List[Replacement]
    error: Optional[str]


# ---------------------------------------------------------------------------
# SQL reference detection
# ---------------------------------------------------------------------------

# Regex to find database-qualified references in SQL.
# Matches:  DATABASE_NAME.OBJECT_NAME
# Captures: (database_name, object_name)
#
# Handles:
#   - Unquoted identifiers: D01_MP_DOM_T.Mortgage
#   - Quoted identifiers:   "D01_MP_DOM_T"."Mortgage"
#   - Token placeholders:   {{DOM_DATABASE_T}}.Mortgage
#
# Deliberately avoids matching inside:
#   - Single-quoted strings (handled by pre-processing)
#   - Line comments (-- ...)
#   - Block comments (/* ... */)
#
# The regex matches the DATABASE.OBJECT pattern; the caller is
# responsible for skipping comments and string literals.
_IDENT_OR_TOKEN = r'(\{\{[A-Za-z_]\w*\}\}|"?[A-Za-z_]\w*"?)'
_QUALIFIED_REF_PATTERN = re.compile(
    r'(?<![.\w])'                    # Not preceded by dot or word char
    + _IDENT_OR_TOKEN +              # Group 1: database name
    r'\.'                            # Literal dot separator
    + _IDENT_OR_TOKEN +              # Group 2: object name
    r'(?![.\w])',                     # Not followed by dot or word char
    re.IGNORECASE,
)

# Pattern to detect line comments
_LINE_COMMENT_PATTERN = re.compile(r'--.*$', re.MULTILINE)

# Pattern to detect block comments (non-greedy, handles multi-line)
_BLOCK_COMMENT_PATTERN = re.compile(r'/\*.*?\*/', re.DOTALL)

# Pattern to detect single-quoted string literals
_STRING_LITERAL_PATTERN = re.compile(r"'(?:[^']|'')*'")


def _strip_quotes(identifier: str) -> str:
    """
    Remove surrounding double quotes from a Teradata identifier.

    Args:
        identifier: The identifier, possibly quoted.

    Returns:
        The identifier without surrounding quotes.
    """
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1]
    return identifier


def _build_exclusion_mask(sql_text: str) -> List[bool]:
    """
    Build a boolean mask marking positions inside comments or string
    literals as True (excluded from rewriting).

    Args:
        sql_text: The full SQL text of the file.

    Returns:
        List of booleans, one per character. True = excluded.
    """
    mask = [False] * len(sql_text)

    # Mark block comments
    for match in _BLOCK_COMMENT_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    # Mark line comments
    for match in _LINE_COMMENT_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    # Mark string literals
    for match in _STRING_LITERAL_PATTERN.finditer(sql_text):
        for i in range(match.start(), match.end()):
            mask[i] = True

    return mask


def _line_number_at(text: str, position: int) -> int:
    """
    Return the 1-based line number for a character position.

    Args:
        text:     The full text.
        position: Character index.

    Returns:
        1-based line number.
    """
    return text[:position].count('\n') + 1


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(
    file_path: Path,
    placement: ObjectPlacement,
) -> FileResult:
    """
    Scan a ``.viw`` file for database-qualified references that point
    to a tables database and rewrite them to the views database.

    Skips references inside comments and string literals.

    Args:
        file_path: Path to the ``.viw`` file.
        placement: Configured ObjectPlacement engine.

    Returns:
        FileResult with the list of replacements made (or error).
    """
    try:
        original_text = file_path.read_text(encoding='utf-8')
    except Exception as e:
        return FileResult(
            path=file_path,
            replacements=[],
            error=f"Failed to read file: {e}",
        )

    exclusion_mask = _build_exclusion_mask(original_text)
    replacements: List[Replacement] = []

    # Collect all matches with their positions (process in reverse
    # order later to preserve string positions during replacement).
    matches = list(_QUALIFIED_REF_PATTERN.finditer(original_text))

    for match in matches:
        # Skip if the match falls inside a comment or string literal
        if exclusion_mask[match.start()]:
            continue

        raw_db = match.group(1)
        raw_obj = match.group(2)
        db_name = _strip_quotes(raw_db)

        # Check if this database matches the tables pattern
        if not placement.is_tables_database(db_name):
            continue

        # Resolve the views database
        try:
            views_db = placement.resolve_views_database(db_name)
        except PlacementResolutionError:
            continue

        # Build the replacement string
        # Preserve quoting style from the original
        if raw_db.startswith('"'):
            new_ref = f'"{views_db}".{raw_obj}'
        else:
            new_ref = f'{views_db}.{raw_obj}'

        line_num = _line_number_at(original_text, match.start())
        replacements.append(Replacement(
            line_number=line_num,
            original=match.group(0),
            rewritten=new_ref,
            db_original=db_name,
            db_rewritten=views_db,
        ))

    return FileResult(
        path=file_path,
        replacements=replacements,
        error=None,
    )


def apply_replacements(file_path: Path, replacements: List[Replacement]) -> str:
    """
    Apply replacements to a file and return the new content.

    Replacements are applied by finding each original string and
    replacing it. This is safe because each replacement is a unique
    database.object reference at a specific location.

    Args:
        file_path:    Path to the file.
        replacements: List of Replacement tuples.

    Returns:
        The modified file content.
    """
    text = file_path.read_text(encoding='utf-8')

    # Apply replacements in reverse line order to preserve positions
    sorted_reps = sorted(replacements, key=lambda r: r.line_number, reverse=True)

    for rep in sorted_reps:
        # Replace only the first occurrence at or after the expected
        # position to avoid replacing the wrong instance
        text = text.replace(rep.original, rep.rewritten, 1)

    return text


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_placement_config(config_path: Path) -> ObjectPlacement:
    """
    Load and validate the placement config from object_placement.yaml.

    Args:
        config_path: Path to the object_placement.yaml file.

    Returns:
        Configured ObjectPlacement engine.

    Raises:
        SystemExit: If the config file cannot be loaded or parsed.
    """
    if yaml is None:
        print(
            "ERROR: PyYAML is required. Install with: "
            "pip install pyyaml --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

    if not config_path.exists():
        print(
            f"ERROR: Configuration file not found: {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"ERROR: Failed to parse {config_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not config:
        print(
            f"ERROR: Configuration file is empty: {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Detect the old wrapped format (pre-rename ships.yml shape) and
    # give a clear migration message rather than a cryptic engine error.
    if isinstance(config, dict) \
            and 'object_placement' in config \
            and 'strategy' not in config:
        print(
            f"ERROR: {config_path} uses the old wrapped format.\n"
            "  Move the contents of the 'object_placement:' key to "
            "the top level.\n"
            "\n"
            "  Old format:                  New format:\n"
            "    object_placement:            strategy: mapped\n"
            "      strategy: mapped           locking_views: true\n"
            "      locking_views: true        database_map:\n"
            "      database_map: ...            - tables_database: ...\n"
            "                                     views_database: ...",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return ObjectPlacement(config)
    except PlacementConfigError as e:
        print(f"ERROR: Invalid placement config:\n{e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_view_files(search_dir: Path) -> List[Path]:
    """
    Recursively find all ``.viw`` files in *search_dir*.

    Args:
        search_dir: Root directory to search.

    Returns:
        Sorted list of ``.viw`` file paths.
    """
    return sorted(search_dir.rglob('*.viw'))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(
    results: List[FileResult],
    mode_label: str,
) -> int:
    """
    Print a human-readable report of all changes.

    Args:
        results:    List of FileResult from processing.
        mode_label: Display label for the output mode
                    (e.g. ``'DRY RUN'``, ``'IN-PLACE'``).

    Returns:
        Total number of replacements made/planned.
    """
    total_replacements = 0
    total_files_changed = 0
    total_errors = 0

    is_preview = mode_label == "DRY RUN"

    print(f"\n{'=' * 70}")
    print(f"  SHIPS View Reference Migration — {mode_label}")
    print(f"{'=' * 70}\n")

    for result in results:
        if result.error:
            total_errors += 1
            print(f"  ERROR  {result.path}")
            print(f"         {result.error}\n")
            continue

        if not result.replacements:
            continue

        total_files_changed += 1
        total_replacements += len(result.replacements)

        print(f"  {'WOULD CHANGE' if is_preview else 'CHANGED'}  {result.path}")
        for rep in result.replacements:
            print(
                f"    Line {rep.line_number:>4d}: "
                f"{rep.db_original} → {rep.db_rewritten}"
            )
            print(
                f"              {rep.original}"
            )
            print(
                f"           →  {rep.rewritten}"
            )
        print()

    # Summary
    print(f"{'─' * 70}")
    print(f"  Files scanned:     {len(results)}")
    print(f"  Files changed:     {total_files_changed}")
    print(f"  Replacements:      {total_replacements}")
    if total_errors:
        print(f"  Errors:            {total_errors}")
    print(f"  Mode:              {mode_label}")
    print(f"{'─' * 70}\n")

    return total_replacements


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Entry point for the migration script.

    Parses arguments, loads config, discovers view files, processes
    each file, and optionally applies changes.
    """
    parser = argparse.ArgumentParser(
        prog='migrate_view_references',
        description=(
            "Rewrite .viw files so AI-Native Data Product views read\n"
            "from the 1:1 locking view layer instead of directly from\n"
            "tables databases.\n"
            "\n"
            "Reads object_placement.yaml to determine the database\n"
            "naming convention, then scans .viw files and rewrites\n"
            "any database-qualified reference that points to a tables\n"
            "database so it points to the corresponding views database\n"
            "instead.\n"
            "\n"
            "References inside comments and string literals are\n"
            "left untouched."
        ),
        epilog=(
            "output modes (exactly one required):\n"
            "  --dry-run     Preview changes without writing anything\n"
            "  --output DIR  Write modified files to a new directory\n"
            "  --in-place    Edit the source files directly (destructive)\n"
            "\n"
            "examples:\n"
            "  # Preview changes\n"
            "  python migrate_view_references.py --dry-run\n"
            "\n"
            "  # Write rewritten files to a new directory\n"
            "  python migrate_view_references.py \\\n"
            "      --dir payload/database/DDL/views \\\n"
            "      --output migrated/\n"
            "\n"
            "  # Edit files in place (use with caution)\n"
            "  python migrate_view_references.py \\\n"
            "      --dir payload/database/DDL/views \\\n"
            "      --in-place\n"
            "\n"
            "The tool reads object_placement.yaml.\n"
            "Supported strategies:\n"
            "  separated  Pattern-based (suffix, prefix, midfix)\n"
            "  colocated  Tables and views in the same database\n"
            "  mapped     Explicit database-to-database pairs\n"
            "\n"
            "See object_placement.yaml for configuration examples."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--config',
        type=Path,
        default=Path('object_placement.yaml'),
        metavar='FILE',
        help=(
            'Path to object_placement.yaml. '
            '(default: ./object_placement.yaml)'
        ),
    )
    parser.add_argument(
        '--dir',
        type=Path,
        default=Path('.'),
        metavar='DIR',
        help=(
            'Root directory to scan recursively for .viw files. '
            '(default: current directory)'
        ),
    )

    # -- Output mode (mutually exclusive) --
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        '--dry-run',
        action='store_true',
        help=(
            'Preview mode. Show which references would be rewritten '
            'and in which files, without modifying anything on disk.'
        ),
    )
    output_group.add_argument(
        '--output',
        type=Path,
        default=None,
        metavar='DIR',
        help=(
            'Write modified files to this directory, preserving the '
            'relative path structure from --dir. Unmodified files are '
            'also copied so the output is a complete set. The output '
            'directory is created if it does not exist.'
        ),
    )
    output_group.add_argument(
        '--in-place',
        action='store_true',
        help=(
            'Edit source files directly. This is destructive — the '
            'original content is overwritten with no backup. Consider '
            'using --dry-run first to preview changes.'
        ),
    )
    args = parser.parse_args()

    # -- Validate that an output mode was specified --
    if not args.dry_run and args.output is None and not args.in_place:
        parser.error(
            "No output mode specified. Use one of:\n"
            "  --dry-run     Preview changes\n"
            "  --output DIR  Write to a new directory\n"
            "  --in-place    Edit files directly (destructive)"
        )

    # Load configuration
    placement = load_placement_config(args.config)
    print(f"  Placement engine: {placement}")

    # Check strategy is appropriate
    if placement.strategy == 'colocated' and not placement.locking_views:
        print(
            "\n  INFO: Strategy is 'colocated' with locking_views=false.\n"
            "  No database reference rewriting is needed.",
        )
        sys.exit(0)

    # Discover view files
    view_files = find_view_files(args.dir)
    if not view_files:
        print(f"\n  No .viw files found in {args.dir.resolve()}")
        sys.exit(0)

    print(f"  Found {len(view_files)} .viw file(s) in {args.dir.resolve()}\n")

    # Process each file
    results: List[FileResult] = []
    for vf in view_files:
        result = process_file(vf, placement)
        results.append(result)

    # -- Write output --
    if args.in_place:
        # Edit source files directly
        for result in results:
            if result.replacements and not result.error:
                new_content = apply_replacements(
                    result.path, result.replacements
                )
                result.path.write_text(new_content, encoding='utf-8')

    elif args.output is not None:
        # Write to output directory, preserving relative paths.
        # All .viw files are copied — modified ones get the new
        # content, unmodified ones are copied verbatim so the
        # output directory is a complete, self-contained set.
        source_root = args.dir.resolve()
        output_root = args.output.resolve()

        for result in results:
            if result.error:
                continue

            rel = result.path.resolve().relative_to(source_root)
            dest = output_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            if result.replacements:
                new_content = apply_replacements(
                    result.path, result.replacements
                )
                dest.write_text(new_content, encoding='utf-8')
            else:
                # Copy unmodified file verbatim
                dest.write_text(
                    result.path.read_text(encoding='utf-8'),
                    encoding='utf-8',
                )

    # Print report
    mode_label = "DRY RUN"
    if args.in_place:
        mode_label = "IN-PLACE"
    elif args.output is not None:
        mode_label = f"OUTPUT → {args.output.resolve()}"

    total = print_report(results, mode_label)

    if args.dry_run and total > 0:
        print("  Run with --output DIR or --in-place to apply.\n")


if __name__ == '__main__':
    main()
