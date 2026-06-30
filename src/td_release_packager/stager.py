"""
stager.py — `ships stage` implementation.

A bounded git-staging gate for SHIPS projects (issue #487).

Stages exactly the project's SHIPS-owned paths into the git index:

    - ``ships.yaml``
    - ``config/`` (env configs, inspect.conf, tokenise.conf, …)
    - ``payload/`` (DDL / DCL / DML)

Gated on ``ships scan`` and ``ships inspect``: if either reports an
error, the index is not touched and ``blocked_by`` records which
check failed. A pre-flight check also refuses the stage when the
project is not inside any git repo so the operator sees a clear
"run `git init`" hint instead of a generic ``fatal: not a git
repository`` from a downstream ``git add``. ``--dry-run`` prints
the path list without staging.

Explicit non-goals (kept tight so the verb doesn't drift into a
git wrapper):

    - No commit message handling.
    - No ``git commit`` invocation.
    - No signing / hook configuration.
    - No support for non-SHIPS files.

The caller (the CLI) wires the scan + inspect callables; the test
suite passes stubs. Decoupling that way keeps the stager itself a
pure orchestration function with no CLI-internal dependencies.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from td_release_packager.project_paths import (
    CONFIG_DIRNAME,
    SHIPS_YAML_FILENAME,
)

PAYLOAD_DIRNAME = "payload"

# Order matters for display — the project marker first, then config,
# then the payload tree. Callers iterate this in order so the staged
# path list reads top-down.
SHIPS_OWNED_PATHS: tuple[str, ...] = (
    SHIPS_YAML_FILENAME,
    CONFIG_DIRNAME,
    PAYLOAD_DIRNAME,
)


@dataclass
class StageResult:
    """Structured envelope returned by :func:`stage_project`.

    ``blocked_by`` is ``"scan"`` or ``"inspect"`` when a gate refused
    the stage; ``None`` otherwise. ``staged_paths`` lists the paths
    git-add was invoked with (or *would have been* invoked with under
    ``--dry-run``), relative to ``project_dir``.
    """

    success: bool
    dry_run: bool
    project_dir: str
    staged_paths: List[str] = field(default_factory=list)
    blocked_by: Optional[str] = None
    error: Optional[str] = None
    scan_exit_code: Optional[int] = None
    inspect_exit_code: Optional[int] = None
    #: Absolute path of the git repository the index belongs to, as
    #: returned by ``git rev-parse --show-toplevel``. Populated once
    #: the repo check passes; ``None`` when the project is not inside
    #: a git repo (the verb refuses in that case).
    repo_root: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "dry_run": self.dry_run,
            "project_dir": self.project_dir,
            "staged_paths": list(self.staged_paths),
            "blocked_by": self.blocked_by,
            "error": self.error,
            "scan_exit_code": self.scan_exit_code,
            "inspect_exit_code": self.inspect_exit_code,
            "repo_root": self.repo_root,
        }


def _default_git(project_dir: str) -> Callable[[List[str]], int]:
    """Build the default git runner — subprocess into the project dir."""

    def _run(argv: List[str]) -> int:
        return subprocess.call(["git", "-C", project_dir, *argv])

    return _run


def _default_git_repo_root(project_dir: str) -> Optional[str]:
    """Return the enclosing repo's toplevel, or None if not in a repo.

    Uses ``git rev-parse --show-toplevel`` from inside ``project_dir``
    so git walks upward to find ``.git`` — that means a SHIPS project
    nested inside a monorepo correctly resolves to the monorepo root,
    not the project directory.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except OSError:
        # git not on PATH — treat as "no repo" so the caller surfaces a
        # clean error rather than an exception.
        return None
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return root or None


def stage_project(
    project_dir: str,
    *,
    run_scan: Callable[[str], int],
    run_inspect: Callable[[str], int],
    dry_run: bool = False,
    git: Optional[Callable[[List[str]], int]] = None,
    git_repo_root: Optional[Callable[[str], Optional[str]]] = None,
) -> StageResult:
    """Stage SHIPS-owned paths after gating on scan + inspect.

    Args:
        project_dir: SHIPS project root containing ``ships.yaml``.
        run_scan: Callable that runs ``ships scan`` against the
            project and returns its exit code.
        run_inspect: Callable that runs ``ships inspect`` against
            the project and returns its exit code.
        dry_run: When True, list the paths that would be staged and
            return without touching the index. Scan + inspect still
            run — the gate is the point of the verb, dry-run only
            suppresses the final ``git add``.
        git: Override the git runner — accepts ``argv`` (without the
            leading ``git -C <project>``) and returns the exit code.
            Defaults to ``subprocess.call``. The tests pass a stub.
        git_repo_root: Override the repo-detection helper — accepts
            ``project_dir`` and returns the enclosing repo's absolute
            toplevel, or ``None`` if not in a repo. Defaults to
            ``git rev-parse --show-toplevel``. The tests pass a stub.

    Returns:
        A :class:`StageResult`. ``success`` is True only when the repo
        check + both gates passed and either the dry-run report or a
        ``git add`` completed cleanly.
    """
    project_dir = os.path.abspath(project_dir)

    # -- Project marker gate --
    if not os.path.isfile(os.path.join(project_dir, SHIPS_YAML_FILENAME)):
        return StageResult(
            success=False,
            dry_run=dry_run,
            project_dir=project_dir,
            error=(
                f"Not a SHIPS project: {SHIPS_YAML_FILENAME} not found in {project_dir}"
            ),
        )

    # -- Git-repo gate --
    # Fail fast: if the project is not inside any git repo, neither
    # scan nor inspect changes that fact and a downstream ``git add``
    # will only emit a generic "fatal: not a git repository" line.
    # Detect it upfront so the operator gets a clear next step.
    repo_root_fn = (
        git_repo_root if git_repo_root is not None else _default_git_repo_root
    )
    repo_root = repo_root_fn(project_dir)
    if repo_root is None:
        return StageResult(
            success=False,
            dry_run=dry_run,
            project_dir=project_dir,
            error=(
                f"Not a git repository: {project_dir} is not inside any git "
                "repo. Run `git init` here, or move the SHIPS project inside "
                "an existing repo, then re-run `ships stage`."
            ),
        )

    # -- Gate 1: scan --
    scan_rc = run_scan(project_dir)
    if scan_rc != 0:
        return StageResult(
            success=False,
            dry_run=dry_run,
            project_dir=project_dir,
            blocked_by="scan",
            scan_exit_code=scan_rc,
            repo_root=repo_root,
            error=(
                f"`ships scan` failed (exit {scan_rc}); index unchanged. "
                "Fix the reported token issues and re-run `ships stage`."
            ),
        )

    # -- Gate 2: inspect --
    inspect_rc = run_inspect(project_dir)
    if inspect_rc != 0:
        return StageResult(
            success=False,
            dry_run=dry_run,
            project_dir=project_dir,
            blocked_by="inspect",
            scan_exit_code=scan_rc,
            inspect_exit_code=inspect_rc,
            repo_root=repo_root,
            error=(
                f"`ships inspect` failed (exit {inspect_rc}); index unchanged. "
                "Fix the reported Coding Discipline violations and re-run."
            ),
        )

    # -- Resolve which SHIPS-owned paths actually exist --
    # A freshly-scaffolded project always has all three; partial
    # projects (e.g. ships.yaml-only fixtures in tests) get whatever
    # subset is on disk. Staging never fabricates paths.
    paths = [
        p for p in SHIPS_OWNED_PATHS if os.path.exists(os.path.join(project_dir, p))
    ]

    if not paths:
        # ships.yaml passed the marker check above, so this branch is
        # genuinely impossible in practice — but keeping it explicit
        # means a future refactor that loosens the marker check can't
        # silently emit an empty ``git add``.
        return StageResult(
            success=False,
            dry_run=dry_run,
            project_dir=project_dir,
            scan_exit_code=scan_rc,
            inspect_exit_code=inspect_rc,
            repo_root=repo_root,
            error="No SHIPS-owned paths found to stage",
        )

    if dry_run:
        return StageResult(
            success=True,
            dry_run=True,
            project_dir=project_dir,
            staged_paths=paths,
            scan_exit_code=scan_rc,
            inspect_exit_code=inspect_rc,
            repo_root=repo_root,
        )

    # -- Stage --
    git_runner = git if git is not None else _default_git(project_dir)
    rc = git_runner(["add", "--", *paths])
    if rc != 0:
        return StageResult(
            success=False,
            dry_run=False,
            project_dir=project_dir,
            staged_paths=paths,
            scan_exit_code=scan_rc,
            inspect_exit_code=inspect_rc,
            repo_root=repo_root,
            error=f"`git add` failed (exit {rc}); index may be partially staged",
        )

    return StageResult(
        success=True,
        dry_run=False,
        project_dir=project_dir,
        staged_paths=paths,
        scan_exit_code=scan_rc,
        inspect_exit_code=inspect_rc,
        repo_root=repo_root,
    )
