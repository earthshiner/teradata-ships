"""
deploy_runtime.py — File discovery and phase ordering for SHIPS deployments.

Extracted from the generated deploy.py template so that the logic can be
unit-tested without building a full package first.

This module is embedded in every SHIPS package under lib/ alongside the
rest of database_package_deployer and is imported by the generated deploy.py
at runtime via sys.path manipulation.

Functions
---------
discover_files(phase_path)
    Discover SQL files in a phase directory, ordered by the phase's
    sub-directory priority map then alphabetically within each sub-directory.

read_order_file(order_path, base_dir)
    Read an _order.txt control file listing filenames in deployment order.

Constants
---------
PHASE_SUBDIR_ORDERS
    Priority map used by discover_files to determine the correct deployment
    order within each standard SHIPS phase directory.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase sub-directory ordering
# ---------------------------------------------------------------------------
# Within each phase, objects must deploy in dependency order.  A plain
# alphabetical walk of the sub-directories gives the wrong result:
#
#   03_ddl  alphabetical: functions (F), jar_install (J), join_indexes (J),
#           macros (M), procedures (P), tables (T), views (V), triggers (T)
#
#   Correct order: tables → indexes → views → jar_install → procedures →
#                  functions → script_table_operators → triggers
#
# The map assigns a numeric priority to each known sub-directory; lower
# numbers deploy first.  Sub-directories not in the map (e.g. user-added
# folders) sort to the end alphabetically (sentinel = 9999).
PHASE_SUBDIR_ORDERS: Dict[str, Dict[str, int]] = {
    "00_system": {
        "maps": 0,
        "roles": 1,
        "profiles": 2,
        "authorizations": 3,
        "foreign_servers": 4,
    },
    "02_dcl": {
        "roles": 0,
        "users": 1,
        "inter_db": 2,
    },
    "03_ddl": {
        "tables": 10,
        "join_indexes": 11,
        "hash_indexes": 11,
        "secondary_indexes": 12,
        "views": 20,
        "macros": 21,
        "jar_install": 22,
        "procedures": 23,
        "functions": 24,
        "script_table_operators": 25,
        "triggers": 30,
    },
}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def discover_files(phase_path: str) -> List[str]:
    """
    Discover SQL files in a phase directory.

    Files are returned in deployment order:
      1. Sub-directories are sorted by their priority in PHASE_SUBDIR_ORDERS
         (lower number = deploys first). Sub-directories not in the map
         appear at the end, sorted alphabetically.
      2. Within each sub-directory, files are sorted alphabetically.
      3. Control files (names starting with ``_`` or ``.``) are skipped.

    This ordering guarantees, for example, that tables deploy before views
    (which depend on them) and that JAR install scripts deploy before the
    Java procedures that reference them — the two bugs that triggered the
    creation of this map.

    Args:
        phase_path: Absolute or relative path to the phase directory
                    (e.g. ``/pkg/payload/database/03_ddl``).

    Returns:
        List of absolute file paths in deployment order.
    """
    phase_name = os.path.basename(phase_path.rstrip(os.sep))
    order_map = PHASE_SUBDIR_ORDERS.get(phase_name, {})

    files_by_subdir: Dict[str, List[str]] = {}
    for root, dirs, filenames in os.walk(phase_path):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith("_") or f.startswith("."):
                continue  # skip control files (_order.txt, .gitkeep, etc.)
            full = os.path.join(root, f)
            rel = os.path.relpath(full, phase_path)
            top_subdir = rel.replace(os.sep, "/").split("/", 1)[0]
            files_by_subdir.setdefault(top_subdir, []).append(full)

    def _subdir_key(name: str):
        return (order_map.get(name, 9999), name)

    out: List[str] = []
    for subdir in sorted(files_by_subdir.keys(), key=_subdir_key):
        out.extend(files_by_subdir[subdir])
    return out


# ---------------------------------------------------------------------------
# Order file reader
# ---------------------------------------------------------------------------


def read_order_file(order_path: str, base_dir: str) -> List[str]:
    """
    Read an ``_order.txt`` control file listing filenames in deployment order.

    Lines starting with ``#`` and blank lines are ignored. Each remaining
    line is treated as a filename relative to *base_dir*. Files that do not
    exist on disk are skipped with a warning rather than raising an error —
    a missing file is a configuration issue the DBA should investigate, but
    it must not prevent the remaining files from deploying.

    Args:
        order_path: Path to the ``_order.txt`` file.
        base_dir:   Directory used to resolve relative filenames listed in
                    the order file.

    Returns:
        List of absolute paths for files that exist on disk, in the order
        they appear in the control file.
    """
    files: List[str] = []
    with open(order_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            full = os.path.join(base_dir, stripped)
            if os.path.exists(full):
                files.append(full)
            else:
                logger.warning(
                    "Order file references missing file: %s (base_dir: %s)",
                    stripped,
                    base_dir,
                )
    return files
