"""
binary_harvester.py — Bring binary dependencies into the SHIPS payload.

Some SQL files reference binary artefacts that aren't themselves SQL:

  * **JAR install scripts** (``CALL SQLJ.INSTALL_JAR('CJ!../JAVA/JAR/X.jar', ...)``)
    point at .jar archives that must be deployed alongside.
  * **C UDFs** (``CREATE FUNCTION ... LANGUAGE C ...
    EXTERNAL NAME 'CS!alias!../FOO/foo.c!CH!alias_h!../FOO/foo.h'``)
    point at .c source and .h header files needed at deploy time.

Without harvesting these binaries, the deployer has nothing to upload
and the procedure or function is broken on the target. This module
closes that gap.

Approach:

  1. The classifier extracts the path references from the SQL
     content and populates ``ClassificationResult.related_files``.
  2. We resolve each reference relative to the SOURCE script's
     location (so ``../JAVA/JAR/X.jar`` from
     ``<src>/P_GCFR_UT/install.ddl`` resolves to
     ``<src>/JAVA/JAR/X.jar``).
  3. We copy the binary into the SAME directory as the SQL script's
     destination (so installs and binaries travel together — the
     deployer doesn't navigate paths).
  4. We rewrite the SQL content to use the new sibling-path form
     (``./X.jar`` / ``./foo.c``) so the deployed script's references
     resolve at deploy time.

What this module does NOT handle (deferred):

  * Path rewriting for absolute references (rare; warn if seen).
  * Deduplication when the same binary is referenced from multiple
    scripts (we copy each time — harmless but wasteful).
  * Binaries with no recognisable reference syntax — caller's
    responsibility to detect.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import List, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Data class
# ---------------------------------------------------------------


@dataclass(frozen=True)
class BinaryDependency:
    """One resolved binary referenced from a SQL script.

    Attributes:
        original_ref:    The path string as it appeared in the SQL
                         (e.g. ``"../JAVA/JAR/X.jar"``).
        source_path:     Resolved absolute path on disc (where to
                         read the bytes from).
        destination_path: Where SHIPS will write the binary inside
                         the payload tree.
        new_ref:         The path to use in the rewritten SQL
                         (e.g. ``"./X.jar"``).
        kind:            Free-form label — ``JAR_BINARY``,
                         ``C_SOURCE``, ``C_HEADER``, etc.
        exists:          Whether ``source_path`` was found. False
                         means the SQL referenced a path that
                         doesn't exist; the caller should warn and
                         skip the copy.
    """

    original_ref: str
    source_path: str
    destination_path: str
    new_ref: str
    kind: str
    exists: bool


# ---------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------


def _kind_for_path(path: str) -> str:
    """Best-effort classification of a binary path by extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".jar":
        return "JAR_BINARY"
    if ext == ".c":
        return "C_SOURCE"
    if ext == ".h":
        return "C_HEADER"
    if ext in (".cpp", ".cc", ".cxx"):
        return "CPP_SOURCE"
    if ext == ".o":
        return "C_OBJECT"
    return "BINARY"


def resolve_dependencies(
    *,
    related_paths: List[str],
    source_file_path: str,
    destination_dir: str,
) -> List[BinaryDependency]:
    """
    Resolve each ``related_paths`` reference to an absolute source
    path and a destination inside the SHIPS payload.

    Args:
        related_paths:     The path references extracted by the
                           classifier (e.g. ``"../JAVA/JAR/X.jar"``).
        source_file_path:  Absolute path of the SQL script that
                           originated the references — used as the
                           base for relative-path resolution.
        destination_dir:   Directory under the payload where the
                           binaries should be staged. Conventionally
                           the same directory as the SQL script's
                           destination, so installs and binaries
                           are siblings.

    Returns:
        ``BinaryDependency`` per reference. Order matches
        ``related_paths``. References that don't exist on disc are
        still returned (with ``exists=False``) so the caller can
        warn rather than fail silently.
    """
    deps: List[BinaryDependency] = []
    src_dir = os.path.dirname(os.path.abspath(source_file_path))

    for ref in related_paths:
        if os.path.isabs(ref):
            # Absolute path. Use as-is. (Rare; worth warning at
            # caller level but we don't reject.)
            resolved = os.path.normpath(ref)
        else:
            resolved = os.path.normpath(os.path.join(src_dir, ref))

        filename = os.path.basename(resolved)
        dest = os.path.join(destination_dir, filename)
        # Forward-slash form for the new SQL reference — Teradata
        # is comfortable with either, and ``./`` reads as obvious
        # "next to me" everywhere.
        new_ref = "./" + filename

        deps.append(
            BinaryDependency(
                original_ref=ref,
                source_path=resolved,
                destination_path=dest,
                new_ref=new_ref,
                kind=_kind_for_path(resolved),
                exists=os.path.isfile(resolved),
            )
        )

    return deps


# ---------------------------------------------------------------
# Copy + rewrite
# ---------------------------------------------------------------


def copy_binaries(
    deps: List[BinaryDependency],
    *,
    overwrite: bool = True,
) -> List[BinaryDependency]:
    """
    Copy each dependency's source bytes to its destination path.

    Args:
        deps:      Dependencies returned by ``resolve_dependencies``.
        overwrite: If False, skip existing destination files.

    Returns:
        The subset of ``deps`` that were actually copied. (Skipped
        and missing-source entries are excluded so the caller can
        report them to the user separately.)
    """
    copied: List[BinaryDependency] = []
    for dep in deps:
        if not dep.exists:
            logger.warning(
                "Binary not found, skipping: %s (referenced as %s)",
                dep.source_path,
                dep.original_ref,
            )
            continue

        if not overwrite and os.path.exists(dep.destination_path):
            logger.debug(
                "Destination exists, skipping: %s", dep.destination_path
            )
            continue

        os.makedirs(os.path.dirname(dep.destination_path), exist_ok=True)
        shutil.copy2(dep.source_path, dep.destination_path)
        logger.debug(
            "Copied binary: %s → %s",
            dep.source_path,
            dep.destination_path,
        )
        copied.append(dep)

    return copied


def rewrite_content(content: str, deps: List[BinaryDependency]) -> str:
    """
    Replace each ``original_ref`` with its ``new_ref`` in ``content``.

    Uses straight string substitution — collisions are unlikely
    because the originals are typed paths inside SQL string
    literals. Order matters when one reference is a prefix of
    another, so we apply longest-first.
    """
    if not deps:
        return content

    # Apply longest replacement first to avoid prefix-overlap.
    sorted_deps = sorted(
        deps, key=lambda d: len(d.original_ref), reverse=True
    )
    out = content
    for dep in sorted_deps:
        out = out.replace(dep.original_ref, dep.new_ref)
    return out


# ---------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------


@dataclass
class HarvestResult:
    """Outcome of harvesting binaries for a single SQL file.

    Attributes:
        rewritten_content: The SQL with paths updated. If no
                           binaries were resolved, equals the input.
        copied:            Successfully copied binaries.
        missing:           References that pointed at non-existent
                           source files.
        warnings:          Diagnostic messages.
    """

    rewritten_content: str
    copied: List[BinaryDependency]
    missing: List[BinaryDependency]
    warnings: List[str]


def harvest_binaries(
    *,
    content: str,
    related_paths: List[str],
    source_file_path: str,
    destination_dir: str,
    overwrite: bool = True,
) -> HarvestResult:
    """
    End-to-end binary harvest for one SQL file.

    Resolves ``related_paths`` against ``source_file_path``, copies
    each existing binary into ``destination_dir``, and rewrites
    ``content`` so the references point at the new sibling paths.

    Args:
        content:             SQL content (post-token-substitution).
        related_paths:       Path refs from the classifier.
        source_file_path:    Absolute path of the originating SQL
                             script — used as the base for relative
                             path resolution.
        destination_dir:     Where the binaries should be staged.
        overwrite:           Whether to overwrite existing
                             destination files.

    Returns:
        ``HarvestResult`` with the rewritten content, copied list,
        and any missing-source warnings.
    """
    if not related_paths:
        return HarvestResult(
            rewritten_content=content, copied=[], missing=[], warnings=[]
        )

    deps = resolve_dependencies(
        related_paths=related_paths,
        source_file_path=source_file_path,
        destination_dir=destination_dir,
    )

    missing = [d for d in deps if not d.exists]
    warnings: List[str] = []
    for d in missing:
        warnings.append(
            f"Binary referenced as {d.original_ref!r} not found on disc "
            f"(expected at {d.source_path}). Path NOT rewritten — "
            f"the deployer will likely fail this object."
        )

    copied = copy_binaries(deps, overwrite=overwrite)

    # Only rewrite refs for binaries we actually copied. Missing
    # binaries leave the original path so a downstream developer
    # can see the broken reference without it being silently
    # masked by a no-longer-correct ./X.jar.
    rewritten = rewrite_content(content, copied)

    return HarvestResult(
        rewritten_content=rewritten,
        copied=copied,
        missing=missing,
        warnings=warnings,
    )
