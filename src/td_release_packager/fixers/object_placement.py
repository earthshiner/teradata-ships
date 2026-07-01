"""Auto-fixer for the ``object_placement`` inspect rule (#538).

Rewrites view-file DDL bodies so that qualified references to a
tables-database are replaced with the corresponding views-database.
The SHIPS Object Placement standard puts a 1:1 locking view layer
(``*_STD_V``) between the tables database (``*_STD_T``) and its
consumers; views should read from the view database, not the tables
database.

Mechanics:

* Load ``<project>/config/object_placement.yaml`` and construct an
  :class:`ObjectPlacement` engine.
* Walk ``.viw`` files under ``payload/`` (skipping locking views —
  those legitimately reference the ``_T`` companion).
* Use the same comment/string exclusion mask the rule check uses so a
  ``-- FROM Prod_STD_T.Foo`` comment is not rewritten.
* For each ``db.obj`` qualified reference whose ``db`` is a
  tables-database, rewrite the db portion to
  ``placement.resolve_views_database(db)``. The object part is
  untouched.

The rule is default-ERROR, the fix is mechanical, and
``resolve_views_database`` already exists — so the fixer is
registered ``default_on=True``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile

if TYPE_CHECKING:
    from td_release_packager.object_placement import ObjectPlacement


def _load_placement(project_dir: str) -> "ObjectPlacement | None":
    """Load ``<project>/config/object_placement.yaml`` into an engine.

    Returns None when the file is missing or the config is invalid —
    the fixer treats that identically to "rule inactive" and skips the
    run. Callers get a clean FixResult with ``totals["files_rewritten"]``
    of zero.
    """
    try:
        import yaml

        from td_release_packager.object_placement import (
            ObjectPlacement,
            PlacementConfigError,
        )
    except ImportError:
        return None

    yaml_path = os.path.join(project_dir, "config", "object_placement.yaml")
    if not os.path.isfile(yaml_path):
        return None
    try:
        with open(yaml_path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(config, dict):
        return None
    try:
        return ObjectPlacement(config)
    except PlacementConfigError:
        return None


def fix_object_placement(source_dir: str, dry_run: bool = False) -> FixResult:
    """Rewrite tables-db qualifiers in view files to views-db qualifiers.

    Args:
        source_dir: SHIPS project directory (parent of ``payload/``).
        dry_run:    When True, compute the rewrite list without writing.

    Returns:
        :class:`FixResult` with ``rule_id="object_placement"``.
        ``totals["files_rewritten"]`` counts touched files;
        ``totals["refs_rewritten"]`` counts individual qualifier
        substitutions. Each :class:`FixResultFile` carries a
        ``refs`` list under ``details`` with per-substitution
        ``{"line", "from_db", "to_db"}`` entries.
    """
    # Lazy imports — validate is heavyweight, and the fixer package is
    # imported at every ``ships fix`` invocation.
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import (
        _DB_QUALIFIED_REF_RE,
        _build_exclusion_mask,
        _is_locking_view,
        _prune_generated_dirs,
        _strip_identifier_quotes,
    )

    result = FixResult(rule_id="object_placement", dry_run=dry_run)

    placement = _load_placement(source_dir)
    if placement is None:
        # No placement engine → rule inactive → no work.
        result.totals["files_rewritten"] = 0
        result.totals["refs_rewritten"] = 0
        return result

    # Rule is inactive under these configs, mirror the check's guards
    # so the fixer never touches a file the check would leave alone.
    if not placement.locking_views or placement.strategy == "colocated":
        result.totals["files_rewritten"] = 0
        result.totals["refs_rewritten"] = 0
        return result

    known_extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    known_extensions.add(".viw")

    total_refs_rewritten = 0

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            # Only view files carry object_placement violations. Enforce
            # the extension filter explicitly so a stray ``.tbl`` with a
            # tables-db reference (legitimate) is not rewritten.
            if ext != ".viw":
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(
                    {
                        "file": os.path.relpath(file_path, source_dir),
                        "error": f"read failed: {exc}",
                    }
                )
                continue

            if _is_locking_view(content):
                continue

            exclusion_mask = _build_exclusion_mask(content)

            # Collect the substitutions right-to-left so earlier offsets
            # stay valid as we mutate the buffer.
            substitutions: list[tuple[int, int, str]] = []
            per_file_refs: list[dict] = []

            for match in _DB_QUALIFIED_REF_RE.finditer(content):
                if exclusion_mask[match.start()]:
                    continue

                raw_db = match.group(1)
                db_name = _strip_identifier_quotes(raw_db)

                if not placement.is_tables_database(db_name):
                    continue

                try:
                    views_db = placement.resolve_views_database(db_name)
                except Exception as exc:
                    result.errors.append(
                        {
                            "file": os.path.relpath(file_path, source_dir),
                            "error": (
                                f"cannot resolve views database for {db_name!r}: {exc}"
                            ),
                        }
                    )
                    continue

                # Preserve quoting so ``"Foo_T".Bar`` becomes
                # ``"Foo_V".Bar`` rather than losing its quotes.
                if raw_db.startswith('"') and raw_db.endswith('"'):
                    replacement = f'"{views_db}"'
                else:
                    replacement = views_db

                start = match.start(1)
                end = match.end(1)
                substitutions.append((start, end, replacement))

                line_num = content[:start].count("\n") + 1
                per_file_refs.append(
                    {
                        "line": line_num,
                        "from_db": db_name,
                        "to_db": views_db,
                    }
                )

            if not substitutions:
                continue

            new_content = content
            for start, end, replacement in sorted(substitutions, reverse=True):
                new_content = new_content[:start] + replacement + new_content[end:]

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

            rel_path = os.path.relpath(file_path, source_dir)
            result.files_changed.append(
                FixResultFile(
                    file=rel_path,
                    details={
                        "refs_rewritten": len(substitutions),
                        "refs": per_file_refs,
                    },
                )
            )
            total_refs_rewritten += len(substitutions)

    result.totals["files_rewritten"] = len(result.files_changed)
    result.totals["refs_rewritten"] = total_refs_rewritten
    return result


SPEC = register(
    FixerSpec(
        rule_id="object_placement",
        apply=fix_object_placement,
        default_on=True,
        write_scope="payload",
    )
)
