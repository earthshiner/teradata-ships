"""
test_wave_executor.py — concurrency safety for wave deployment streams.
"""

import threading
import time

from database_package_deployer.wave_executor import WaveExecutor


class GuardedCursor:
    """Cursor stub that raises if used concurrently."""

    def __init__(self, name: str):
        self.name = name
        self.connection = self
        self._state_lock = threading.Lock()
        self._in_use = False

    def _enter(self):
        with self._state_lock:
            if self._in_use:
                raise AssertionError(f"{self.name} used concurrently")
            self._in_use = True

    def _exit(self):
        with self._state_lock:
            self._in_use = False

    def execute(self, _sql):
        self._enter()
        try:
            time.sleep(0.005)
        finally:
            self._exit()

    def close(self):
        pass

    def deploy_work(self, file_path: str):
        self._enter()
        try:
            # Make the first stream-1 task outlive stream 2, forcing the
            # executor to schedule another stream-1 task while the first is
            # still running unless per-cursor locking is working.
            time.sleep(0.08 if file_path == "f0.sql" else 0.01)
        finally:
            self._exit()


def test_parallel_wave_never_uses_same_cursor_concurrently():
    cursors = [GuardedCursor("stream-1"), GuardedCursor("stream-2")]
    next_cursor = iter(cursors)

    executor = WaveExecutor(num_streams=2, connect_fn=lambda: next(next_cursor))

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql", "f4.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 5
