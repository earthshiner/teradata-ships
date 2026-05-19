"""
test_wave_executor.py — concurrency safety for wave deployment streams.
"""

import threading
import time

import pytest

from database_package_deployer.wave_executor import WaveExecutor
from td_release_packager.wave_executor import WaveExecutor as PackagedWaveExecutor


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


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_parallel_wave_never_uses_same_cursor_concurrently(executor_cls):
    cursors = [GuardedCursor("stream-1"), GuardedCursor("stream-2")]
    next_cursor = iter(cursors)

    executor = executor_cls(num_streams=2, connect_fn=lambda: next(next_cursor))

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql", "f4.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 5


class SharedDriverGuardCursor(GuardedCursor):
    """Cursor stub that raises if any cursor enters the driver concurrently."""

    _driver_state_lock = threading.Lock()
    _driver_in_use = False

    @classmethod
    def reset_driver(cls):
        with cls._driver_state_lock:
            cls._driver_in_use = False

    def _enter(self):
        super()._enter()
        with self._driver_state_lock:
            if self._driver_in_use:
                super()._exit()
                raise AssertionError("driver used concurrently")
            self.__class__._driver_in_use = True

    def _exit(self):
        with self._driver_state_lock:
            self.__class__._driver_in_use = False
        super()._exit()


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_parallel_wave_serializes_driver_calls_across_cursors(executor_cls):
    SharedDriverGuardCursor.reset_driver()
    cursors = [
        SharedDriverGuardCursor("stream-1"),
        SharedDriverGuardCursor("stream-2"),
    ]
    next_cursor = iter(cursors)

    executor = executor_cls(num_streams=2, connect_fn=lambda: next(next_cursor))

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql", "f4.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 5
