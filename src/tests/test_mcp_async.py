"""
test_mcp_async.py — Tests for the fire-and-forget MCP dispatch (#319).

Covers the contract that ``ships_harvest`` / ``ships_inspect`` /
``ships_package`` / ``ships_process`` share, plus the new
``ships_poll_build`` tool:

* :func:`_launch_background` spawns a subprocess, writes a sentinel
  JSON containing the live PID, and creates the run log file.
* :func:`_is_process_alive` returns ``True`` for the current
  process and ``False`` for a PID that's guaranteed not to exist.
* :func:`ships_poll_build` reports:
  - ``running`` when the recorded PID is alive,
  - ``done``    when the PID is gone and the log is clean,
  - ``failed``  when the PID is gone and the log contains an error
                signal,
  - ``unknown`` when no sentinel exists.
* When ``run_id`` is omitted, ``ships_poll_build`` selects the
  most-recently-modified sentinel.

The MCP server's heavy fake-module test harness (used by
``test_mcp_transport.py``) is not needed here because we exercise
the helper and the tool function directly — no FastMCP wiring is
under test.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ships_mcp import (
    _atomic_write_json,
    _is_process_alive,
    _launch_background,
    ships_poll_build,
)


# ---------------------------------------------------------------------------
# _launch_background
# ---------------------------------------------------------------------------


class TestLaunchBackground:
    def test_spawns_and_writes_sentinel(self, tmp_path: Path):
        """``_launch_background`` writes a sentinel JSON containing a
        real PID and a run id, and creates the run log file."""
        # Use a tiny, immediate-exit Python -c invocation rather than
        # td_release_packager so the test runs fast and offline.  The
        # helper doesn't care what module it spawns.
        receipt = self._spawn_noop(tmp_path)

        sentinel_path = Path(receipt["sentinel"])
        log_path = Path(receipt["log_path"])

        assert sentinel_path.exists()
        assert log_path.exists()
        data = json.loads(sentinel_path.read_text(encoding="utf-8"))
        assert data["run_id"] == receipt["run_id"]
        assert data["status"] == "running"
        assert data["pid"] == receipt["pid"]
        assert isinstance(receipt["pid"], int)
        assert receipt["pid"] > 0

    def test_log_file_created_inside_runs_dir(self, tmp_path: Path):
        receipt = self._spawn_noop(tmp_path)
        log_path = Path(receipt["log_path"])
        assert log_path.parent == tmp_path / ".ships" / "runs"
        assert log_path.name.startswith("run_")
        assert log_path.name.endswith(".log")

    def test_dispatch_receipt_shape(self, tmp_path: Path):
        receipt = self._spawn_noop(tmp_path)
        assert receipt["dispatched"] is True
        assert {
            "dispatched",
            "run_id",
            "pid",
            "log_path",
            "sentinel",
            "poll_hint",
        } <= set(receipt)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _spawn_noop(project_dir: Path) -> dict:
        """Launch the smallest possible Python child via the helper."""
        # We call _launch_background with a module that exists but is
        # immediately returnable: -c"" via -m is not possible, so we
        # spawn a no-op via the json module's main (``python -m json``
        # with no args prints usage and exits cleanly).  Any
        # fast-exiting module works.
        return _launch_background("json.tool", ["--help"], str(project_dir))


# ---------------------------------------------------------------------------
# _is_process_alive
# ---------------------------------------------------------------------------


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        assert _is_process_alive(os.getpid()) is True

    def test_impossible_pid_is_dead(self):
        # 2**31 - 1 is the maximum PID on Linux and far higher than any
        # real Windows PID.  Even after PID reuse the chance of a
        # collision is negligible in the test window.
        impossible = 2_147_483_646
        assert _is_process_alive(impossible) is False

    def test_zero_returns_none(self):
        assert _is_process_alive(0) is None

    def test_negative_returns_none(self):
        assert _is_process_alive(-1) is None

    def test_none_returns_none(self):
        assert _is_process_alive(None) is None


# ---------------------------------------------------------------------------
# ships_poll_build — sentinel resolution
# ---------------------------------------------------------------------------


def _write_sentinel(
    project: Path,
    run_id: str,
    pid: int,
    log_lines: str = "",
) -> Path:
    """Helper: hand-write a sentinel + matching log file for the
    poll-tool tests.  Bypasses ``_launch_background`` so we can pin
    PIDs to known-alive / known-dead values."""
    runs = project / ".ships" / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    log_path = runs / f"run_{run_id}.log"
    log_path.write_text(log_lines, encoding="utf-8")

    sentinel = runs / f"run_{run_id}.json"
    _atomic_write_json(
        sentinel,
        {
            "run_id": run_id,
            "status": "running",
            "command": "noop --for --testing",
            "started_at": "2026-06-15T00:00:00Z",
            "pid": pid,
            "log_path": str(log_path),
        },
    )
    return sentinel


class TestShipsPollBuild:
    def test_status_running_when_pid_alive(self, tmp_path: Path):
        _write_sentinel(tmp_path, "abc123ab", os.getpid())
        result = ships_poll_build(project=str(tmp_path), run_id="abc123ab")
        assert result["status"] == "running"
        assert result["alive"] is True
        assert "30-60 seconds" in result["next_step"]

    def test_status_done_when_pid_gone_and_log_clean(self, tmp_path: Path):
        _write_sentinel(
            tmp_path,
            "deadbeef",
            pid=2_147_483_646,  # impossible
            log_lines="completed in 7.3 seconds\nfile_count=170\n",
        )
        result = ships_poll_build(project=str(tmp_path), run_id="deadbeef")
        assert result["status"] == "done"
        assert result["alive"] is False
        assert "ships_verify" in result["next_step"]

    def test_status_failed_when_pid_gone_and_log_has_error(self, tmp_path: Path):
        _write_sentinel(
            tmp_path,
            "boomboom",
            pid=2_147_483_646,
            log_lines="step 1 OK\nTraceback (most recent call last):\nFile foo\n",
        )
        result = ships_poll_build(project=str(tmp_path), run_id="boomboom")
        assert result["status"] == "failed"
        assert result["alive"] is False
        assert "Review log_tail" in result["next_step"]

    def test_status_failed_case_insensitive(self, tmp_path: Path):
        _write_sentinel(
            tmp_path,
            "uppercas",
            pid=2_147_483_646,
            log_lines="step 1\nERROR: something blew up\n",
        )
        result = ships_poll_build(project=str(tmp_path), run_id="uppercas")
        assert result["status"] == "failed"

    def test_status_unknown_when_no_sentinel_dir(self, tmp_path: Path):
        result = ships_poll_build(project=str(tmp_path))
        assert result["status"] == "unknown"
        assert "No run sentinels" in result["next_step"]

    def test_status_unknown_when_run_id_missing(self, tmp_path: Path):
        # Create the directory but not the named sentinel.
        (tmp_path / ".ships" / "runs").mkdir(parents=True)
        result = ships_poll_build(project=str(tmp_path), run_id="nope")
        assert result["status"] == "unknown"
        assert "Sentinel not found" in result["next_step"]

    def test_latest_sentinel_selected_when_run_id_omitted(self, tmp_path: Path):
        # Write two sentinels, the second with a clearly later mtime.
        _write_sentinel(tmp_path, "older000", pid=2_147_483_646, log_lines="ok\n")
        time.sleep(0.05)  # ensure mtime ordering on Windows too
        _write_sentinel(tmp_path, "newer000", pid=2_147_483_646, log_lines="ok\n")

        result = ships_poll_build(project=str(tmp_path))
        assert result["run_id"] == "newer000"

    def test_log_tail_returned(self, tmp_path: Path):
        body = "\n".join(f"line {i}" for i in range(60))
        _write_sentinel(
            tmp_path,
            "logsfull",
            pid=2_147_483_646,
            log_lines=body,
        )
        result = ships_poll_build(project=str(tmp_path), run_id="logsfull")
        # 40-line tail
        assert result["log_tail"].count("\n") <= 40
        assert "line 59" in result["log_tail"]
        # And the head shouldn't be there
        assert "line 0\n" not in result["log_tail"]
