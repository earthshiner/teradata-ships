"""
test_decisions_prune.py — Tests for decisions.json pruning.

Covers:
    - keep_runs: retains N most recent, removes older
    - keep_days: retains runs within window, removes older
    - dry_run: computes result without writing
    - edge cases: empty file, zero keep_runs, all within window
    - validation: neither/both criteria raises ValueError
    - missing file raises FileNotFoundError
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from td_release_packager.orchestrator.decisions import (
    PruneResult,
    prune_decisions,
)


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------


def _make_run(run_id: str, started_at: str, command: str = "inspect") -> dict:
    return {
        "run_id": run_id,
        "command": command,
        "started_at": started_at,
        "finished_at": started_at,
        "duration_ms": 100,
        "final_status": "success",
        "stages": [],
    }


def _write_decisions(path: Path, runs: list) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "project": {}, "runs": runs}),
        encoding="utf-8",
    )


def _read_runs(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["runs"]


def _days_ago(n: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.isoformat()


# ---------------------------------------------------------------
# keep_runs
# ---------------------------------------------------------------


class TestPruneByRunCount:
    def test_keeps_n_most_recent(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(10 - i)) for i in range(5)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_runs=3)

        assert result.total_runs == 5
        assert result.kept_runs == 3
        assert result.pruned_runs == 2
        assert len(_read_runs(f)) == 3

    def test_oldest_two_are_removed(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(10 - i)) for i in range(5)]
        _write_decisions(f, runs)

        prune_decisions(str(f), keep_runs=3)

        remaining_ids = {r["run_id"] for r in _read_runs(f)}
        assert remaining_ids == {"r2", "r3", "r4"}

    def test_keep_runs_zero_removes_all(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(i)) for i in range(3)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_runs=0)

        assert result.pruned_runs == 3
        assert _read_runs(f) == []

    def test_keep_more_than_exist_keeps_all(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(i)) for i in range(3)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_runs=100)

        assert result.pruned_runs == 0
        assert len(_read_runs(f)) == 3

    def test_empty_file_no_error(self, tmp_path):
        f = tmp_path / "decisions.json"
        _write_decisions(f, [])

        result = prune_decisions(str(f), keep_runs=10)

        assert result.total_runs == 0
        assert result.pruned_runs == 0


# ---------------------------------------------------------------
# keep_days
# ---------------------------------------------------------------


class TestPruneByAge:
    def test_removes_runs_older_than_cutoff(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [
            _make_run("recent", _days_ago(1)),
            _make_run("borderline", _days_ago(29)),
            _make_run("old", _days_ago(91)),
        ]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_days=30)

        assert result.pruned_runs == 1
        remaining = {r["run_id"] for r in _read_runs(f)}
        assert "old" not in remaining
        assert "recent" in remaining

    def test_keep_days_zero_removes_all(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(i + 1)) for i in range(3)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_days=0)

        assert result.pruned_runs == 3


# ---------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(i)) for i in range(5)]
        _write_decisions(f, runs)
        original_content = f.read_text(encoding="utf-8")

        result = prune_decisions(str(f), keep_runs=2, dry_run=True)

        assert result.dry_run is True
        assert result.pruned_runs == 3
        assert f.read_text(encoding="utf-8") == original_content

    def test_dry_run_returns_correct_ids(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(5 - i)) for i in range(5)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_runs=2, dry_run=True)

        assert set(result.pruned_run_ids) == {"r0", "r1", "r2"}


# ---------------------------------------------------------------
# Validation / error handling
# ---------------------------------------------------------------


class TestPruneValidation:
    def test_neither_criterion_raises(self, tmp_path):
        f = tmp_path / "decisions.json"
        _write_decisions(f, [])
        with pytest.raises(ValueError, match="exactly one"):
            prune_decisions(str(f))

    def test_both_criteria_raises(self, tmp_path):
        f = tmp_path / "decisions.json"
        _write_decisions(f, [])
        with pytest.raises(ValueError, match="exactly one"):
            prune_decisions(str(f), keep_runs=5, keep_days=30)

    def test_negative_keep_runs_raises(self, tmp_path):
        f = tmp_path / "decisions.json"
        _write_decisions(f, [])
        with pytest.raises(ValueError, match="keep_runs"):
            prune_decisions(str(f), keep_runs=-1)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            prune_decisions(str(tmp_path / "nonexistent.json"), keep_runs=5)

    def test_result_fields_populated(self, tmp_path):
        f = tmp_path / "decisions.json"
        runs = [_make_run(f"r{i}", _days_ago(i)) for i in range(4)]
        _write_decisions(f, runs)

        result = prune_decisions(str(f), keep_runs=2)

        assert isinstance(result, PruneResult)
        assert len(result.pruned_run_ids) == 2
        assert len(result.pruned_started_at) == 2
        assert result.dry_run is False
