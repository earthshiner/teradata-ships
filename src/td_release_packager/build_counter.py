"""
build_counter.py — Automated build number management.

Manages a `.build_counter` file in the project root that stores
the current build number as a single integer. Each build reads
the current value, increments it, and writes back atomically.

The counter file is designed to be committed to Git — it tracks
the build sequence across the team. The write uses a temp-then-
rename pattern for crash safety.

File format:
    A single line containing an integer, e.g. "12\\n".
"""

import logging
import os

logger = logging.getLogger(__name__)

COUNTER_FILENAME = ".build_counter"


def read_build_number(project_dir: str) -> int:
    """
    Read the current build number without incrementing.

    Args:
        project_dir: Project root directory.

    Returns:
        The current build number.

    Raises:
        FileNotFoundError: If .build_counter does not exist.
                           Run the scaffolder to create it.
    """
    counter_path = os.path.join(project_dir, COUNTER_FILENAME)

    if not os.path.exists(counter_path):
        raise FileNotFoundError(
            f"Build counter not found: {counter_path}. "
            f"Run 'td_release_packager scaffold' to create a project, "
            f"or create the file manually with '0' as its content."
        )

    with open(counter_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    try:
        return int(content)
    except ValueError:
        raise ValueError(
            f"Build counter contains invalid value: '{content}'. "
            f"Expected a single integer in {counter_path}."
        )


def next_build_number(project_dir: str) -> int:
    """
    Read, increment, and persist the next build number.

    This is the primary entry point. Each call returns a unique,
    monotonically increasing build number.

    Uses write-to-temp-then-rename for atomicity — if the process
    crashes mid-write, the previous counter value is preserved.

    Args:
        project_dir: Project root directory.

    Returns:
        The new (incremented) build number.
    """
    current = read_build_number(project_dir)
    next_num = current + 1

    _write_counter(project_dir, next_num)

    logger.info("Build counter incremented: %d → %d", current, next_num)

    return next_num


def _write_counter(project_dir: str, value: int):
    """
    Write the build counter atomically.

    Uses write-to-temp-then-rename so a crash during write
    does not corrupt the counter file.

    Args:
        project_dir: Project root directory.
        value:       The build number to write.
    """
    counter_path = os.path.join(project_dir, COUNTER_FILENAME)
    tmp_path = counter_path + ".tmp"

    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(f"{value}\n")
        f.flush()
        os.fsync(f.fileno())

    try:
        os.replace(tmp_path, counter_path)
    except PermissionError:
        if os.path.exists(counter_path):
            import stat

            os.chmod(counter_path, stat.S_IWRITE)
            os.remove(counter_path)
        os.rename(tmp_path, counter_path)


def reset_build_number(project_dir: str, value: int = 0):
    """
    Reset the build counter to a specific value.

    Use with caution — resetting can cause build number collisions
    if packages with higher numbers already exist.

    Args:
        project_dir: Project root directory.
        value:       Value to reset to (default: 0).
    """
    _write_counter(project_dir, value)
    logger.warning("Build counter reset to %d", value)
