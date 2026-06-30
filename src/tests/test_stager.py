"""
test_stager.py — Tests for the ``ships stage`` gate (issue #487).

Covers the acceptance criteria from the issue:

    1. Scan failure → exit non-zero, index unchanged, ``blocked_by="scan"``.
    2. Inspect failure → exit non-zero, index unchanged, ``blocked_by="inspect"``.
    3. Both gates pass → exactly the SHIPS-owned paths are staged.
    4. ``--dry-run`` reports the path list and does NOT call git add.
    5. Unrelated working-tree files are not staged.
    6. Non-project paths (no ``ships.yaml``) are refused with a clean
       error before any gate runs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

import pytest

from td_release_packager.stager import (
    SHIPS_OWNED_PATHS,
    StageResult,
    stage_project,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_project(root: Path) -> Path:
    """Create a minimal SHIPS project (ships.yaml + config/ + payload/)."""
    (root / "ships.yaml").write_text("project: stage_test\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "tokenise.conf").write_text("# tokens\n", encoding="utf-8")
    (root / "payload" / "database" / "DDL").mkdir(parents=True)
    (root / "payload" / "database" / "DDL" / "t.sql").write_text(
        "CREATE TABLE x (i INTEGER);\n", encoding="utf-8"
    )
    return root


class _RecordingGit:
    """Capture every git invocation for assertion."""

    def __init__(self, return_code: int = 0) -> None:
        self.calls: List[List[str]] = []
        self.return_code = return_code

    def __call__(self, argv: List[str]) -> int:
        self.calls.append(list(argv))
        return self.return_code


def _ok(_project_dir: str) -> int:
    return 0


def _scan_fail(_project_dir: str) -> int:
    return 1


def _inspect_fail(_project_dir: str) -> int:
    return 1


# Most tests want the repo gate to PASS so they can exercise the rest
# of the flow — this stub treats the project directory as the repo
# root (the common case for a scaffolded project that someone has
# `git init`-ed). Tests for the not-in-repo branch override it.
def _repo_is_project(p: str) -> str:
    return p


def _not_in_repo(_p: str):  # returns None — "not a git repo"
    return None


# ---------------------------------------------------------------
# Project marker gate
# ---------------------------------------------------------------


def test_refuses_non_ships_project(tmp_path: Path) -> None:
    """Without ships.yaml, the stager refuses before running any gate."""
    git = _RecordingGit()
    scan_calls = []
    inspect_calls = []

    def _scan(p):
        scan_calls.append(p)
        return 0

    def _inspect(p):
        inspect_calls.append(p)
        return 0

    result = stage_project(
        str(tmp_path),
        run_scan=_scan,
        run_inspect=_inspect,
        git=git,
    )

    assert result.success is False
    assert "ships.yaml" in (result.error or "")
    # Critically, no gate ran and no git invocation happened.
    assert scan_calls == []
    assert inspect_calls == []
    assert git.calls == []


# ---------------------------------------------------------------
# Git-repo gate
# ---------------------------------------------------------------


def test_refuses_when_project_not_in_git_repo(tmp_path: Path) -> None:
    """Project exists but is not inside any git repo → clean error, no gates run."""
    project = _make_project(tmp_path)
    git = _RecordingGit()
    scan_calls: list[str] = []
    inspect_calls: list[str] = []

    def _scan(p):
        scan_calls.append(p)
        return 0

    def _inspect(p):
        inspect_calls.append(p)
        return 0

    result = stage_project(
        str(project),
        run_scan=_scan,
        run_inspect=_inspect,
        git=git,
        git_repo_root=_not_in_repo,
    )

    assert result.success is False
    assert "git repository" in (result.error or "").lower()
    assert "git init" in (result.error or "")
    assert result.repo_root is None
    # Neither gate fired — the repo check is genuinely fail-fast.
    assert scan_calls == []
    assert inspect_calls == []
    assert git.calls == []


def test_repo_root_can_differ_from_project_dir(tmp_path: Path) -> None:
    """Monorepo case: project nested under a different repo root surfaces both paths."""
    monorepo = tmp_path / "monorepo"
    project_dir = monorepo / "projects" / "X"
    project_dir.mkdir(parents=True)
    project = _make_project(project_dir)
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=git,
        git_repo_root=lambda _p: str(monorepo),
    )

    assert result.success is True
    assert result.repo_root == str(monorepo)
    assert result.project_dir == str(project)
    assert result.repo_root != result.project_dir


# ---------------------------------------------------------------
# Gate 1: scan
# ---------------------------------------------------------------


def test_scan_failure_blocks_with_blocked_by_scan(tmp_path: Path) -> None:
    """Scan exit != 0 → success=False, blocked_by='scan', git not called."""
    project = _make_project(tmp_path)
    git = _RecordingGit()
    inspect_ran = [False]

    def _inspect(_):
        inspect_ran[0] = True
        return 0

    result = stage_project(
        str(project),
        run_scan=_scan_fail,
        run_inspect=_inspect,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is False
    assert result.blocked_by == "scan"
    assert result.scan_exit_code == 1
    assert result.repo_root == str(project)  # repo gate passed before scan blocked
    # Inspect is short-circuited and git stays untouched.
    assert inspect_ran[0] is False
    assert git.calls == []


# ---------------------------------------------------------------
# Gate 2: inspect
# ---------------------------------------------------------------


def test_inspect_failure_blocks_with_blocked_by_inspect(tmp_path: Path) -> None:
    """Inspect exit != 0 → success=False, blocked_by='inspect', git not called."""
    project = _make_project(tmp_path)
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_inspect_fail,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is False
    assert result.blocked_by == "inspect"
    assert result.scan_exit_code == 0
    assert result.inspect_exit_code == 1
    assert git.calls == []


# ---------------------------------------------------------------
# Clean pass
# ---------------------------------------------------------------


def test_clean_pass_stages_exactly_ships_owned_paths(tmp_path: Path) -> None:
    """Both gates pass → git add called once with the canonical path list."""
    project = _make_project(tmp_path)
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is True
    assert result.blocked_by is None
    assert result.dry_run is False
    # Only the SHIPS-owned paths that actually exist on disk; in this
    # fixture all three do.
    assert result.staged_paths == list(SHIPS_OWNED_PATHS)

    assert len(git.calls) == 1
    argv = git.calls[0]
    assert argv[0] == "add"
    assert argv[1] == "--"
    # Exactly the SHIPS-owned set, in canonical order, follows ``--``.
    assert argv[2:] == list(SHIPS_OWNED_PATHS)


# ---------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------


def test_dry_run_reports_paths_without_calling_git(tmp_path: Path) -> None:
    """dry_run=True → success=True, staged_paths populated, git untouched."""
    project = _make_project(tmp_path)
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        dry_run=True,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.staged_paths == list(SHIPS_OWNED_PATHS)
    assert result.repo_root == str(project)
    # Gates still ran (the gate IS the point), but git did not.
    assert result.scan_exit_code == 0
    assert result.inspect_exit_code == 0
    assert git.calls == []


# ---------------------------------------------------------------
# Unrelated working-tree files
# ---------------------------------------------------------------


def test_unrelated_files_are_not_in_staged_paths(tmp_path: Path) -> None:
    """An unrelated file at the project root is not handed to git add."""
    project = _make_project(tmp_path)
    (project / "scratch.txt").write_text("temporary notes\n", encoding="utf-8")
    (project / "releases").mkdir()
    (project / "releases" / "pkg.zip").write_text("zip", encoding="utf-8")
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is True
    assert "scratch.txt" not in result.staged_paths
    assert "releases" not in result.staged_paths
    # And the actual git argv must not mention them either.
    argv = git.calls[0]
    assert "scratch.txt" not in argv
    assert "releases" not in argv


# ---------------------------------------------------------------
# Existence filter — missing optional dirs
# ---------------------------------------------------------------


def test_missing_payload_dir_is_skipped_not_fabricated(tmp_path: Path) -> None:
    """If payload/ does not exist, it is not handed to git add."""
    project = _make_project(tmp_path)
    shutil.rmtree(project / "payload")
    git = _RecordingGit()

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is True
    assert "payload" not in result.staged_paths
    assert "payload" not in git.calls[0]


# ---------------------------------------------------------------
# git failure surfaces
# ---------------------------------------------------------------


def test_git_add_failure_is_surfaced(tmp_path: Path) -> None:
    """A non-zero git exit code propagates as an error envelope."""
    project = _make_project(tmp_path)
    git = _RecordingGit(return_code=128)

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=git,
        git_repo_root=_repo_is_project,
    )

    assert result.success is False
    assert "git add" in (result.error or "")
    assert result.scan_exit_code == 0
    assert result.inspect_exit_code == 0


# ---------------------------------------------------------------
# Result envelope shape
# ---------------------------------------------------------------


def test_result_envelope_keys_are_stable(tmp_path: Path) -> None:
    """as_dict() exposes the documented keys — agents depend on this shape."""
    project = _make_project(tmp_path)
    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=_RecordingGit(),
        git_repo_root=_repo_is_project,
    )
    envelope = result.as_dict()
    for key in (
        "success",
        "dry_run",
        "project_dir",
        "staged_paths",
        "blocked_by",
        "error",
        "scan_exit_code",
        "inspect_exit_code",
        "repo_root",
    ):
        assert key in envelope


# ---------------------------------------------------------------
# Integration: real git repo
# ---------------------------------------------------------------


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _git_available(), reason="git not on PATH")
def test_real_git_only_stages_ships_owned_paths(tmp_path: Path) -> None:
    """End-to-end: in a real git repo, only SHIPS-owned paths land in the index."""
    project = _make_project(tmp_path)
    (project / "scratch.txt").write_text("not for staging\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    # Identity is needed only if we commit; here we just stage.

    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
    )
    assert result.success is True

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    # Every staged path must live under a SHIPS-owned root.
    for path in staged:
        head = path.split("/", 1)[0]
        assert head in SHIPS_OWNED_PATHS, f"unexpected staged path: {path}"

    assert "scratch.txt" not in staged
    # And at least one SHIPS-owned file is in fact staged.
    assert any(p == "ships.yaml" for p in staged)


# ---------------------------------------------------------------
# Type / docstring sanity
# ---------------------------------------------------------------


def test_stage_result_is_dataclass_instance(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    result = stage_project(
        str(project),
        run_scan=_ok,
        run_inspect=_ok,
        git=_RecordingGit(),
        git_repo_root=_repo_is_project,
    )
    assert isinstance(result, StageResult)
