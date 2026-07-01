"""Auto-fixer for the ``extension`` inspect rule (#525).

Renames payload files whose extension does not match their DDL kind —
e.g. a ``.sql`` file that harvest hasn't renamed to ``.tbl``. The
target extension is the canonical one from
:data:`td_release_packager.kind_suffix.EXTENSION_TO_KIND` for the
detected :class:`DdlKind`.

Opt-in (not default-on) because a rename shows up as a delete + add
pair in git — a reviewer should see the change explicitly rather than
have it merged silently under a bare ``ships fix`` run. Register
callers that want it (e.g. ``ships fix --rules extension``, or
``packaging.fix.rules: [extension]`` in ships.yaml).

Skips SHIPS-generated paths (``releases/``, ``.ships-work/``,
``_rollback/``) and files whose extension the fixer cannot confidently
map to a kind (:class:`DdlKind.UNKNOWN`). Also skips a file when its
extension is already correct, or when the target filename already
exists on disk (no destructive overwrite).
"""

from __future__ import annotations

import os

from td_release_packager.fixers._detect import DdlKind, detect_ddl_kind
from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile

# Canonical extension per kind — the target the fixer renames files to.
# Kept small and internal so a future contributor extends this table
# rather than plumbing extension-selection into every call site.
_KIND_TO_EXT: dict[DdlKind, str] = {
    DdlKind.TABLE: ".tbl",
    DdlKind.VIEW: ".viw",
    DdlKind.MACRO: ".mcr",
    DdlKind.PROCEDURE: ".spl",
    DdlKind.FUNCTION: ".fnc",
    DdlKind.TRIGGER: ".trg",
    DdlKind.STO: ".sto",
}


def fix_extension(source_dir: str, dry_run: bool = False) -> FixResult:
    """Rename payload files so their extension matches the DDL kind.

    Walks ``source_dir`` using the same file-discovery rules as the
    other payload-writing fixers. Files whose extension is already
    canonical are skipped; files whose kind can't be detected
    confidently are skipped. Never overwrites an existing file.

    Args:
        source_dir: Directory to walk (typically the SHIPS project root).
        dry_run:    When True, compute the rename list without touching
                    disk. The returned result reports what would change.

    Returns:
        :class:`FixResult` with ``rule_id="extension"``.
        ``totals["files_renamed"]`` counts the successful renames (or
        the projected count under dry-run). Each
        :class:`FixResultFile` records ``old_ext``, ``new_ext``, and
        the detected ``kind`` under ``details``.
    """
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.validate import _prune_generated_dirs

    # ``resolve_harvest_extensions`` returns the set of extensions
    # harvest is willing to consume. The fixer inspects any file with
    # such an extension (or a hand-written ``.sql`` — added below so
    # legacy payload trees aren't invisible) and re-labels those whose
    # content says otherwise.
    known_extensions = set(resolve_harvest_extensions(project_dir=source_dir))
    known_extensions.add(".sql")

    result = FixResult(rule_id="extension", dry_run=dry_run)
    files_renamed = 0

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        _prune_generated_dirs(dirs)
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in known_extensions:
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

            kind = detect_ddl_kind(file_path, content)
            if kind == DdlKind.UNKNOWN:
                continue

            target_ext = _KIND_TO_EXT.get(kind)
            if target_ext is None or target_ext == ext:
                continue

            stem = os.path.splitext(filename)[0]
            target_path = os.path.join(root, stem + target_ext)

            # Never overwrite. If the target already exists (e.g. the
            # user manually split a file) surface it as a conflict for
            # human review rather than silently clobbering.
            if os.path.exists(target_path):
                result.errors.append(
                    {
                        "file": os.path.relpath(file_path, source_dir),
                        "error": (
                            f"target {os.path.relpath(target_path, source_dir)} "
                            f"already exists — leaving both files in place"
                        ),
                    }
                )
                continue

            if not dry_run:
                try:
                    os.replace(file_path, target_path)
                except OSError as exc:
                    result.errors.append(
                        {
                            "file": os.path.relpath(file_path, source_dir),
                            "error": f"rename failed: {exc}",
                        }
                    )
                    continue

            files_renamed += 1
            rel_before = os.path.relpath(file_path, source_dir)
            rel_after = os.path.relpath(target_path, source_dir)
            result.files_changed.append(
                FixResultFile(
                    file=rel_before,
                    details={
                        "old_ext": ext,
                        "new_ext": target_ext,
                        "kind": kind.name,
                        "renamed_to": rel_after,
                    },
                )
            )

    result.totals["files_renamed"] = files_renamed
    return result


SPEC = register(
    FixerSpec(
        rule_id="extension",
        apply=fix_extension,
        default_on=False,
        write_scope="payload",
    )
)
