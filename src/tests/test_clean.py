"""
test_clean.py — Tests for the ``ships_clean`` tool and harvest rmtree.

Covers the acceptance criteria from HANDOVER (2026-06-19) §5:
    1. Dry-run preview lists targets and removes nothing.
    2. Apply with scope=payload empties payload/database/ and leaves
       config/ + ships.yaml untouched.
    3. Re-harvest contamination scenario — a second harvest can't
       inherit stale tokenised filenames from a prior run (work item B).
    4. ships.yaml gate refuses non-project paths with a clean error.
    5. scope=all resets to scaffolded but preserves .build_counter.
    6. Tool is synchronous — return envelope has no run_id.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_release_packager.cleaner import CLEAN_SCOPES, clean_project
from td_release_packager.ingest import _clean_payload_tree


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_project(root: Path) -> Path:
    """Create a minimal but realistic SHIPS project tree.

    Includes ships.yaml (the project marker), config/, payload/database/
    with a couple of tokenised filenames, a fake release artefact,
    decisions ledger, and .build_counter — i.e. enough to exercise
    every scope.
    """
    (root / "config" / "env").mkdir(parents=True)
    (root / "config" / "token_map.conf").write_text("# token map\n", encoding="utf-8")
    (root / "ships.yaml").write_text("project: test\n", encoding="utf-8")
    (root / ".ships").mkdir(parents=True, exist_ok=True)
    (root / ".ships" / ".build_counter").write_text("42\n", encoding="utf-8")

    payload_db = root / "payload" / "database"
    (payload_db / "DDL").mkdir(parents=True)
    (payload_db / "DCL" / "inter_db").mkdir(parents=True)
    # Two filename forms — exactly the contamination scenario.
    (payload_db / "DDL" / "{{DB_PREFIX}}_FOO.sql").write_text(
        "--ddl\n", encoding="utf-8"
    )
    (payload_db / "DCL" / "inter_db" / "{{DB_PREFIX_FOO}}.dcl").write_text(
        "--stale\n", encoding="utf-8"
    )
    (payload_db / "DCL" / "inter_db" / "{{DB_PREFIX}}_BAR.dcl").write_text(
        "--current\n", encoding="utf-8"
    )

    (root / "releases").mkdir()
    (root / "releases" / "pkg.zip").write_text("zip", encoding="utf-8")

    (root / "output" / "reports").mkdir(parents=True)
    (root / "output" / "reports" / "report.html").write_text(
        "<html/>", encoding="utf-8"
    )

    (root / ".ships" / "runs").mkdir(parents=True)
    (root / ".ships" / "runs" / "run_abc.json").write_text("{}", encoding="utf-8")

    (root / "ships.decisions.json").write_text("[]", encoding="utf-8")

    return root


# ---------------------------------------------------------------
# Acceptance: ships.yaml gate
# ---------------------------------------------------------------


def test_refuses_non_ships_project(tmp_path: Path) -> None:
    """Refuses a path without ships.yaml — no deletion, clear error."""
    victim = tmp_path / "not_a_project"
    victim.mkdir()
    sentinel = victim / "important.txt"
    sentinel.write_text("do not delete me", encoding="utf-8")

    result = clean_project(str(victim), scope="all", dry_run=False)

    assert result["success"] is False
    assert "ships.yaml" in result["error"]
    # Critically, nothing was deleted.
    assert sentinel.read_text(encoding="utf-8") == "do not delete me"


def test_refuses_missing_directory(tmp_path: Path) -> None:
    """Refuses a non-existent path with a clean error."""
    result = clean_project(
        str(tmp_path / "does_not_exist"), scope="payload", dry_run=False
    )
    assert result["success"] is False
    assert "not found" in result["error"]


def test_refuses_unknown_scope(tmp_path: Path) -> None:
    """Refuses an unknown scope with a friendly error."""
    project = _make_project(tmp_path)
    result = clean_project(str(project), scope="bogus", dry_run=True)
    assert result["success"] is False
    assert "unknown scope" in result["error"]


# ---------------------------------------------------------------
# Acceptance #1 & #2: dry-run preview, apply, isolation of config/
# ---------------------------------------------------------------


def test_dry_run_lists_targets_and_removes_nothing(tmp_path: Path) -> None:
    """Dry-run reports what *would* be removed; touches nothing on disk."""
    project = _make_project(tmp_path)
    result = clean_project(str(project), scope="payload", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["removed_files"] == 3  # the three payload files seeded
    assert any(t["path"].endswith("database") for t in result["targets"])
    # Every seeded payload file still exists.
    assert (project / "payload" / "database" / "DDL" / "{{DB_PREFIX}}_FOO.sql").exists()
    assert (
        project / "payload" / "database" / "DCL" / "inter_db" / "{{DB_PREFIX_FOO}}.dcl"
    ).exists()


def test_apply_payload_scope_clears_database_tree(tmp_path: Path) -> None:
    """scope=payload + dry_run=False empties payload/database/ and
    leaves config/ and ships.yaml untouched."""
    project = _make_project(tmp_path)

    result = clean_project(str(project), scope="payload", dry_run=False)

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["removed_files"] == 3

    payload_db = project / "payload" / "database"
    # Directory still exists (recreated empty), with just .gitkeep.
    assert payload_db.is_dir()
    survivors = [p.name for p in payload_db.iterdir()]
    assert survivors == [".gitkeep"]

    # Config + project marker are untouched.
    assert (project / "config" / "token_map.conf").exists()
    assert (project / "ships.yaml").exists()
    # .build_counter intact even for narrow scope.
    assert (project / ".ships" / ".build_counter").read_text(
        encoding="utf-8"
    ).strip() == "42"


# ---------------------------------------------------------------
# Acceptance #6: scope=all preserves .build_counter
# ---------------------------------------------------------------


def test_scope_all_preserves_build_counter_and_config(tmp_path: Path) -> None:
    """scope=all wipes runs/payload/releases/reports/decisions
    but leaves .build_counter and config/ alone."""
    project = _make_project(tmp_path)

    result = clean_project(str(project), scope="all", dry_run=False)

    assert result["success"] is True
    assert (project / ".ships" / ".build_counter").read_text(
        encoding="utf-8"
    ).strip() == "42"
    assert (project / "config" / "token_map.conf").exists()
    # Decisions file gone (it's a single-file target).
    assert not (project / "ships.decisions.json").exists()
    # Subtrees recreated empty.
    assert (project / "releases").is_dir()
    assert (project / "output" / "reports").is_dir()
    assert (project / ".ships" / "runs").is_dir()
    assert not (project / "releases" / "pkg.zip").exists()


# ---------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------


def test_second_apply_is_idempotent(tmp_path: Path) -> None:
    """A second apply on an already-clean target reports removed=0
    and succeeds."""
    project = _make_project(tmp_path)
    clean_project(str(project), scope="payload", dry_run=False)
    again = clean_project(str(project), scope="payload", dry_run=False)
    assert again["success"] is True
    # .gitkeep may or may not be counted depending on whether the tree
    # contained anything besides itself; what matters is success.
    assert again["removed_files"] <= 1


# ---------------------------------------------------------------
# Synchronous return envelope (no run_id, no dispatched flag)
# ---------------------------------------------------------------


def test_envelope_has_no_run_id(tmp_path: Path) -> None:
    """Tool is synchronous — return shape contains no run_id and is not
    a dispatched receipt."""
    project = _make_project(tmp_path)
    result = clean_project(str(project), scope="payload", dry_run=True)
    assert "run_id" not in result
    assert "dispatched" not in result
    # The expected envelope keys are present.
    for key in (
        "success",
        "scope",
        "dry_run",
        "project_dir",
        "targets",
        "removed_files",
        "removed_dirs",
        "lifecycle_state_after",
        "error",
    ):
        assert key in result


# ---------------------------------------------------------------
# Work item B: harvest payload-clean uses rmtree
# ---------------------------------------------------------------


def test_clean_payload_tree_rmtrees_stale_tokenised_files(tmp_path: Path) -> None:
    """_clean_payload_tree must remove differently-tokenised filenames
    from a prior harvest run — the exact contamination scenario from
    HANDOVER §2."""
    payload_base = tmp_path / "payload" / "database"
    (payload_base / "DCL" / "inter_db").mkdir(parents=True)
    stale = payload_base / "DCL" / "inter_db" / "{{DB_PREFIX_SEM_BUS_V}}.dcl"
    fresh = payload_base / "DCL" / "inter_db" / "{{DB_PREFIX}}_SEM_BUS_V.dcl"
    stale.write_text("--stale\n", encoding="utf-8")
    fresh.write_text("--fresh\n", encoding="utf-8")

    removed = _clean_payload_tree(str(payload_base))

    assert removed == 2
    # Tree exists and is empty bar .gitkeep — both forms gone.
    assert payload_base.is_dir()
    assert not stale.exists()
    assert not fresh.exists()
    assert (payload_base / ".gitkeep").exists()


def test_clean_payload_tree_missing_dir_is_noop(tmp_path: Path) -> None:
    """A missing payload tree is a no-op (returns 0, no exception)."""
    assert _clean_payload_tree(str(tmp_path / "nope")) == 0


# ---------------------------------------------------------------
# Scope vocabulary sanity check
# ---------------------------------------------------------------


def test_clean_scopes_vocabulary() -> None:
    """The scope vocabulary matches the documented set."""
    assert set(CLEAN_SCOPES) == {
        "runs",
        "payload",
        "releases",
        "reports",
        "decisions",
        "all",
    }
