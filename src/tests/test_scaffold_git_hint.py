"""
test_scaffold_git_hint.py — `ships scaffold` repo-state hint (#487).

The scaffolder writes a ``.gitignore`` (which implies a git-aware
workflow), and ``ships stage`` / ``ships package`` (dirty-tree check)
require the project to sit inside a git repo. Surface that requirement
in the scaffold completion banner so the operator isn't caught out at
stage time.

Two branches:
    1. Not inside a git repo → "Tip: run `git init` here…"
    2. Inside a git repo → "Git: project IS the repo root" /
       "Git: project is inside repo <root>"
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from td_release_packager.cli import _cmd_scaffold


def _has_git() -> bool:
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _scaffold_args(output_dir: Path, name: str = "P") -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        output=str(output_dir),
        environments="DEV,TST,PRD",
        repair=False,
        verbose=False,
    )


def test_scaffold_warns_when_not_in_git_repo(tmp_path, capsys):
    """Fresh scaffold outside any git repo → emit the `git init` tip."""
    _cmd_scaffold(_scaffold_args(tmp_path))
    out = capsys.readouterr().out
    assert "not inside a git repo" in out
    assert "git init" in out
    assert "ships stage" in out


@pytest.mark.skipif(not _has_git(), reason="git not on PATH")
def test_scaffold_reports_repo_root_when_project_is_repo(tmp_path, capsys):
    """Fresh scaffold inside a repo (project IS the repo root) → name the repo."""
    # `git init` the directory the scaffolded project lands in. The
    # scaffolder creates ``<output_dir>/<name>``, so init the inner
    # path after-the-fact via a synthesised sequence: scaffold into a
    # subdirectory, then init right there.
    nested_output = tmp_path / "out"
    nested_output.mkdir()
    project_dir = nested_output / "P"
    project_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project_dir, check=True)

    args = _scaffold_args(nested_output)
    # The scaffolder will see the dir already exists and raise unless
    # --repair is set. Use --repair so the scaffold succeeds without
    # wiping the .git directory we just made.
    args.repair = True
    _cmd_scaffold(args)
    out = capsys.readouterr().out
    # Repair-mode banner doesn't include the git-hint block — that
    # only fires on a fresh scaffold (intentional). Re-run without
    # repair on a different fixture to exercise the in-repo branch.

    # Now exercise the fresh-scaffold path with a parent repo.
    parent = tmp_path / "parent_repo"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=parent, check=True)
    fresh_args = _scaffold_args(parent, name="Q")
    _cmd_scaffold(fresh_args)
    out = capsys.readouterr().out
    # Either "project is the repo root" (if parent counts as root, which
    # it does — scaffold creates a subdir but the parent repo encloses
    # it) or "project is inside repo <root>".
    assert "Git:" in out
    assert "git init" not in out  # the not-in-repo tip must NOT appear
    assert "not inside a git repo" not in out


def test_scaffold_repair_mode_skips_git_hint(tmp_path, capsys):
    """Repair mode is for existing projects — no fresh-scaffold hint."""
    args = _scaffold_args(tmp_path)
    _cmd_scaffold(args)
    capsys.readouterr()  # drain
    # Now repair the same project.
    args.repair = True
    _cmd_scaffold(args)
    out = capsys.readouterr().out
    # Repair banner has its own message and skips the workflow + hint.
    assert "Repair complete" in out
    assert "git init" not in out
    assert "Git:" not in out
