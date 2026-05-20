"""
discovery.py — Single source of truth for harvest-candidate
file extensions.

Different sites use different conventions for SQL filenames. The
canonical SHIPS list (``.tbl``, ``.viw``, ``.sql``, ``.bteq``, ...)
covers Teradata's common cases, but a project might add ``.bteq2``,
``.tdsql``, ``.b``, or anything else its team has agreed on. Hard-
coding the list in every discovery site -- harvest, inspect, prereq
scan, deploy -- means a site-specific extension silently disappears
from one stage even after another picks it up.

This module owns the canonical defaults AND the resolution logic
for project-level overrides. Every discovery site reads through
``resolve_harvest_extensions()`` so the answer is consistent.

Layered resolution (later sources extend earlier):

    1. ``DEFAULT_HARVEST_EXTENSIONS`` -- baked-in canonical set.
    2. ``ships.yaml`` ``discovery.extensions`` -- per-project list.
    3. ``extra=`` argument -- programmatic / CLI override.

Extensions are normalised to lower-case with a leading dot. The
resolver is forgiving: a malformed ships.yaml falls back to defaults
silently (the orchestrator's own validation surfaces the error
elsewhere — discovery should not block on it).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional


logger = logging.getLogger(__name__)


#: Canonical SHIPS-supplied harvest-candidate extensions.
#:
#: Modify with care — every discovery site reads from it, so an
#: addition or removal changes harvest, inspect, the intra-package
#: prereq scan, and (where applicable) the deployer's glob fallback.
#:
#: Binary artefacts (``.jar``, ``.c``, ``.h``, ``.cpp``, ``.cc``,
#: ``.cxx``) are deliberately EXCLUDED. They come into the payload
#: via the binary-harvest path (driven by ``EXTERNAL NAME`` /
#: ``CALL SQLJ.INSTALL_JAR`` references), not as standalone
#: discovered files. Including them here would trigger spurious
#: "unclassified" warnings for every binary sitting alongside a SQL
#: script.
DEFAULT_HARVEST_EXTENSIONS: frozenset = frozenset(
    {
        ".sql",
        ".tbl",
        ".viw",
        ".spl",
        ".mcr",
        ".fnc",
        ".trg",
        ".jix",
        ".idx",
        ".db",
        ".ddl",
        ".dcl",
        ".grt",
        ".dml",
        ".osql",
        ".map",
        ".rol",
        ".prf",
        ".auth",
        ".fsvr",
        ".sto",
        ".sjr",
        ".usr",
        # BTEQ-style extensions used by legacy Teradata codebases
        # for pure-SQL scripts. Included by default so a site that
        # uses ``.bteq`` for CREATE TABLE statements gets coverage
        # out of the box; further extensions can be added per
        # project via ``ships.yaml``'s ``discovery.extensions``.
        ".bteq",
        ".btq",
    }
)


def normalise_extension(ext: str) -> str:
    """Canonicalise an extension string: lower-case, with a leading dot.

    ``"BTEQ"`` and ``".BTEQ"`` and ``"bteq"`` all become ``".bteq"``.
    Empty or whitespace-only input returns the empty string —
    callers should filter those out before adding to the resolved
    set.
    """
    e = ext.strip().lower()
    if not e:
        return ""
    if not e.startswith("."):
        e = "." + e
    return e


def _read_ships_yaml_extensions(project_dir: str) -> set:
    """Read the ``discovery.extensions`` list from ``project_dir/ships.yaml``.

    Returns the normalised set of extensions found there, or an
    empty set if:
      - no ships.yaml exists
      - ships.yaml has no ``discovery`` block
      - the block has no ``extensions`` key
      - the value is not a list
      - parsing fails for any reason

    Failures are logged at DEBUG and do NOT raise. Discovery is a
    pre-everything step — it must not block on a malformed
    ships.yaml because the orchestrator's own validation surfaces
    that error with better context elsewhere.
    """
    ships_path = os.path.join(project_dir, "ships.yaml")
    if not os.path.isfile(ships_path):
        return set()

    try:
        # Local import: ``orchestrator.ships_yaml`` requires PyYAML.
        # Discovery is on the hot path of every stage, so we keep
        # the import inside the conditional branch to avoid paying
        # the parse cost when no ships.yaml exists.
        from td_release_packager.orchestrator import ships_yaml

        data = ships_yaml.load(ships_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "discovery: ships.yaml at %s could not be loaded (%s); "
            "falling back to defaults.",
            ships_path,
            exc,
        )
        return set()

    discovery_block = data.get("discovery") if isinstance(data, dict) else None
    if not isinstance(discovery_block, dict):
        return set()

    raw_extensions = discovery_block.get("extensions")
    if not isinstance(raw_extensions, list):
        return set()

    out: set = set()
    for entry in raw_extensions:
        if not isinstance(entry, str):
            continue
        normalised = normalise_extension(entry)
        if normalised:
            out.add(normalised)
    return out


def resolve_harvest_extensions(
    project_dir: Optional[str] = None,
    extra: Optional[Iterable[str]] = None,
) -> frozenset:
    """Resolve the effective harvest-candidate extension set.

    Sources, in precedence order (later sources extend earlier):

      1. ``DEFAULT_HARVEST_EXTENSIONS`` — always present.
      2. ``ships.yaml``'s ``discovery.extensions`` list, when
         ``project_dir`` points to a project containing one.
      3. The ``extra`` argument — programmatic / CLI override.

    All extensions are normalised (lower-case, leading dot). The
    return type is ``frozenset`` so callers can use it as a hashable
    membership-test set without worrying about accidental mutation.

    Args:
        project_dir: Optional project root. When supplied and the
                     directory contains ``ships.yaml``, its
                     ``discovery.extensions`` list extends the
                     defaults. Pass ``None`` to skip ships.yaml
                     resolution entirely (e.g. for ad-hoc scans).
        extra:       Optional iterable of additional extensions
                     (CLI overrides, test fixtures). Each entry is
                     normalised and added to the result.

    Returns:
        A frozenset of normalised extensions including every default,
        the ships.yaml additions, and the ``extra`` additions.
    """
    out = set(DEFAULT_HARVEST_EXTENSIONS)

    if project_dir:
        out.update(_read_ships_yaml_extensions(project_dir))

    if extra:
        for entry in extra:
            if not isinstance(entry, str):
                continue
            normalised = normalise_extension(entry)
            if normalised:
                out.add(normalised)

    return frozenset(out)
