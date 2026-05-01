"""
wave_parser.py — Parse wave definitions for parallel deployment.

Reads a _waves.txt file where blank lines separate waves.
Objects within the same wave have no mutual dependencies and
can execute in parallel across multiple streams.

File format:
    # Comment lines start with '#'
    # Objects in the same block execute in parallel.
    # Blank lines separate sequential waves.

    # Wave 1 — databases (no dependencies)
    STD.db
    SEM.db

    # Wave 2 — grants (depend on databases above)
    grant_std.sql
    grant_sem.sql

    # Wave 3 — tables
    Property.tbl
    Mortgage.tbl

Validation:
    - No object may appear in more than one wave.
    - Every listed object must exist as a file.
    - Empty waves (two consecutive blank lines) are ignored.
"""

import logging
import os
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def parse_waves_file(
    waves_path: str,
    base_dir: str,
) -> List[List[str]]:
    """
    Parse a _waves.txt file into a list of waves.

    Each wave is a list of absolute file paths. Waves are
    ordered — wave 0 executes first, wave 1 after wave 0
    completes, and so on.

    Args:
        waves_path: Path to the _waves.txt file.
        base_dir:   Base directory for resolving filenames.

    Returns:
        List of waves, where each wave is a list of file paths.

    Raises:
        FileNotFoundError: If _waves.txt or a listed file is missing.
        ValueError: If an object appears in multiple waves.
    """
    if not os.path.exists(waves_path):
        raise FileNotFoundError(f"Waves file not found: {waves_path}")

    with open(waves_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    waves = []
    current_wave = []
    seen: Dict[str, int] = {}  # filename → wave number (for dupe check)

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip comment lines
        if stripped.startswith("#"):
            continue

        # Blank line = wave boundary
        if not stripped:
            if current_wave:
                waves.append(current_wave)
                current_wave = []
            continue

        # Resolve to absolute path
        full_path = os.path.join(base_dir, stripped)
        if not os.path.exists(full_path):
            # Build a helpful error with potential matches
            target_dir = os.path.dirname(full_path)
            filename = os.path.basename(stripped)
            ext = os.path.splitext(filename)[1].lower()

            # Extract the object name (part after first dot,
            # before extension) for fuzzy matching
            name_parts = os.path.splitext(filename)[0].split(".", 1)
            obj_name = name_parts[1] if len(name_parts) == 2 else None

            # Scan the target directory for potential matches
            suggestions = []
            if obj_name and os.path.isdir(target_dir):
                for candidate in sorted(os.listdir(target_dir)):
                    if candidate.startswith("_"):
                        continue
                    cand_ext = os.path.splitext(candidate)[1].lower()
                    cand_parts = os.path.splitext(candidate)[0].split(".", 1)
                    cand_obj = cand_parts[1] if len(cand_parts) == 2 else cand_parts[0]
                    if cand_obj == obj_name and cand_ext == ext:
                        suggestions.append(candidate)

            # Build the error message
            msg_parts = [
                f"Wave file line {lineno}: '{stripped}' not found.",
                f"  Resolved to: {full_path}",
            ]
            if suggestions:
                msg_parts.append(
                    "  The file may have been renamed during packaging. "
                    "Possible matches:"
                )
                for s in suggestions:
                    msg_parts.append(f"    → {s}")
                msg_parts.append(
                    "  Fix: update _waves.txt to use the resolved "
                    "filename, or re-run the SHIPS analyser to "
                    "regenerate it."
                )
            else:
                msg_parts.append("  No similar files found in the target directory.")
                if os.path.isdir(target_dir):
                    existing = sorted(
                        f
                        for f in os.listdir(target_dir)
                        if not f.startswith("_")
                        and os.path.splitext(f)[1].lower() == ext
                    )
                    if existing:
                        msg_parts.append(
                            f"  {len(existing)} {ext} file(s) in "
                            f"{os.path.basename(target_dir)}/:"
                        )
                        # Show first 10 to avoid wall of text
                        for f in existing[:10]:
                            msg_parts.append(f"    {f}")
                        if len(existing) > 10:
                            msg_parts.append(f"    ... and {len(existing) - 10} more")

            raise FileNotFoundError("\n".join(msg_parts))

        # Duplicate check
        wave_num = len(waves)
        if stripped in seen:
            raise ValueError(
                f"Wave file line {lineno}: '{stripped}' appears in "
                f"wave {seen[stripped]} and wave {wave_num}. "
                f"Each object must appear in exactly one wave."
            )
        seen[stripped] = wave_num

        current_wave.append(full_path)

    # Don't forget the last wave (file may not end with blank line)
    if current_wave:
        waves.append(current_wave)

    # Filter out empty waves
    waves = [w for w in waves if w]

    total_objects = sum(len(w) for w in waves)
    logger.info(
        "Parsed %d waves with %d total objects from %s",
        len(waves),
        total_objects,
        waves_path,
    )

    return waves


def validate_waves(waves: List[List[str]]) -> Tuple[List[str], List[str]]:
    """
    Validate a parsed wave structure.

    Checks:
        - No empty waves.
        - No duplicate files across waves.
        - At least one wave with at least one file.

    Args:
        waves: List of waves (each a list of file paths).

    Returns:
        Tuple of (errors, warnings).
    """
    errors = []
    warnings = []

    if not waves:
        errors.append("No waves defined.")
        return (errors, warnings)

    # Check for duplicates
    seen = {}
    for wave_idx, wave in enumerate(waves):
        if not wave:
            warnings.append(f"Wave {wave_idx + 1} is empty.")

        for fpath in wave:
            basename = os.path.basename(fpath)
            if basename in seen:
                errors.append(
                    f"'{basename}' in wave {wave_idx + 1} duplicates "
                    f"wave {seen[basename]}."
                )
            seen[basename] = wave_idx + 1

    total = sum(len(w) for w in waves)
    logger.info(
        "Wave validation: %d waves, %d objects, %d errors, %d warnings",
        len(waves),
        total,
        len(errors),
        len(warnings),
    )

    return (errors, warnings)


def flatten_waves(waves: List[List[str]]) -> List[str]:
    """
    Flatten waves into a single ordered list.

    Used as a fallback when parallel execution is disabled
    (streams=1) — executes wave by wave, sequentially within
    each wave.

    Args:
        waves: List of waves.

    Returns:
        Flat list of file paths in wave order.
    """
    result = []
    for wave in waves:
        result.extend(wave)
    return result
