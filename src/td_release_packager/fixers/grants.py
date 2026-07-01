"""Auto-fixer for the ``grants_derivation`` rule (#526).

Generates or updates ``.grt`` / ``.dcl`` files under ``payload/database/DCL/``
so they match the grant set inferred from the payload's DDL. The fix is
additive: missing grants are appended, extras and orphans are left for
human review, and role files are created for role grantees that appear
in DCL but have no ``CREATE ROLE`` file yet.

Wraps :func:`td_release_packager.validate_grants.fix_grants`, translating
its ``(GrantValidationResult, files_written: int)`` tuple into the
:class:`FixResult` envelope every other fixer returns.

Historical note. Grants derivation used to live behind
``ships inspect --fix-grants``. That flag was removed in #526 (finishing
what #522 started for the other two fixers) so ``ships inspect`` is
strictly read-only. The behaviour did not change — same inferrer, same
writes; only the invocation surface moved.
"""

from __future__ import annotations

from pathlib import Path

from td_release_packager.fixers._registry import FixerSpec, register
from td_release_packager.fixers._result import FixResult, FixResultFile


def fix_grants(source_dir: str, dry_run: bool = False) -> FixResult:
    """Repair grant drift by adding missing inferred grants.

    Args:
        source_dir: SHIPS project directory (the parent of ``payload/``).
        dry_run:    When True, report what *would* be written without
                    touching disk.

    Returns:
        :class:`FixResult` with ``rule_id="grants_derivation"``. The
        registry's ``totals`` dict carries the projected write count
        under ``files_written``; each :class:`FixResultFile` is one
        grantee that would be created or appended-to, keyed by relative
        path under the project.
    """
    from td_release_packager.validate_grants import fix_grants as _fix_grants

    result = FixResult(rule_id="grants_derivation", dry_run=dry_run)

    project = Path(source_dir).resolve()
    validation, files_written = _fix_grants(project, dry_run=dry_run)

    result.files_scanned = validation.ddl_count
    result.totals["files_written"] = files_written

    # Build a per-file breakdown from the classification result. Under
    # dry_run the pre-fix status equals the post-fix status (no writes
    # happened), so ``missing`` and ``drifted + missing_privs`` grantees
    # are the ones the fixer will (or would) touch. Under apply the
    # writes have already happened, so the same grantees are what the
    # fixer just wrote to.
    for status in validation.statuses:
        touched = status.missing or (status.drifted and status.missing_privs)
        if not touched:
            continue
        try:
            rel = status.file_path.relative_to(project)
        except (ValueError, AttributeError):
            rel = Path(str(getattr(status, "file_path", status.grantee)))
        details: dict = {"grantee": status.grantee}
        if status.missing:
            details["action"] = "create"
        elif status.drifted and status.missing_privs:
            details["action"] = "append"
            details["missing_privs"] = sorted(status.missing_privs)
        result.files_changed.append(FixResultFile(file=str(rel), details=details))

    return result


SPEC = register(
    FixerSpec(
        rule_id="grants_derivation",
        apply=fix_grants,
        default_on=True,
        write_scope="payload",
    )
)
