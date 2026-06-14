"""
test_wave_executor.py — concurrency safety for wave deployment streams.
"""

import os
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


# ---------------------------------------------------------------
# #211 — --parallel-engine = cursors restores true parallelism
# ---------------------------------------------------------------


class _OverlapCountingCursor(GuardedCursor):
    """Cursor stub that counts the maximum overlap with peers across
    the process-wide driver. Used to prove that ``cursors`` mode lets
    independent cursors run truly concurrently."""

    _driver_state_lock = threading.Lock()
    _in_flight = 0
    _max_in_flight = 0

    @classmethod
    def reset_counters(cls):
        with cls._driver_state_lock:
            cls._in_flight = 0
            cls._max_in_flight = 0

    def deploy_work(self, file_path: str):  # noqa: D401
        self._enter()
        try:
            with self._driver_state_lock:
                self.__class__._in_flight += 1
                if self.__class__._in_flight > self.__class__._max_in_flight:
                    self.__class__._max_in_flight = self.__class__._in_flight
            try:
                time.sleep(0.02)
            finally:
                with self._driver_state_lock:
                    self.__class__._in_flight -= 1
        finally:
            self._exit()


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_engine_validation_rejects_unknown_value(executor_cls):
    with pytest.raises(ValueError, match="Unknown parallel engine"):
        executor_cls(num_streams=2, connect_fn=lambda: GuardedCursor("x"), engine="x")


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_serial_engine_is_the_default(executor_cls):
    executor = executor_cls(num_streams=1, connect_fn=lambda: GuardedCursor("x"))
    assert executor.engine == "serial"


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_cursors_engine_lets_independent_cursors_run_concurrently(executor_cls):
    """The whole point of ``cursors`` mode: two cursors must be able
    to be inside the driver at the same time. With ``serial`` mode
    max_in_flight == 1; with ``cursors`` mode it must be 2."""
    _OverlapCountingCursor.reset_counters()
    cursors = [
        _OverlapCountingCursor("stream-1"),
        _OverlapCountingCursor("stream-2"),
    ]
    next_cursor = iter(cursors)

    executor = executor_cls(
        num_streams=2,
        connect_fn=lambda: next(next_cursor),
        engine="cursors",
    )

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql", "f4.sql", "f5.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 6
    assert _OverlapCountingCursor._max_in_flight >= 2, (
        "cursors engine must allow two cursors to enter the driver concurrently"
    )


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_cursors_engine_still_protects_each_cursor(executor_cls):
    """Even with the process-wide driver lock dropped, a single
    cursor must never be used by two threads at once. The per-cursor
    lock stays in force."""
    cursors = [GuardedCursor("stream-1"), GuardedCursor("stream-2")]
    next_cursor = iter(cursors)

    executor = executor_cls(
        num_streams=2,
        connect_fn=lambda: next(next_cursor),
        engine="cursors",
    )

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql", "f4.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 5


# ---------------------------------------------------------------
# #211 — processes engine (ProcessPoolExecutor path)
# ---------------------------------------------------------------
#
# The processes engine requires picklable module-level callables. The
# helpers below live at module scope so multiprocessing can serialise
# them. They simulate the worker-init / worker-task contract; the
# deployer is expected to provide equivalent module-level callables
# when it wires processes mode end-to-end.


def _process_worker_init_noop(initargs_marker):  # pragma: no cover - subprocess
    """No-op initializer; proves the initializer hook fires.

    The marker propagates via the process state file written by the
    deploy function below. Runs once per worker process.
    """
    import os
    import tempfile

    marker_dir = tempfile.gettempdir()
    pid_marker = os.path.join(marker_dir, f"ships_wave_proc_init_{os.getpid()}.txt")
    with open(pid_marker, "w", encoding="utf-8") as f:
        f.write(str(initargs_marker))


def _process_worker_deploy_echo(file_path):  # pragma: no cover - subprocess
    """Returns a dict echoing the file path and the worker's PID.

    Picklable by definition — only stdlib types in the return.
    """
    import os

    return {
        "file": file_path,
        "state": "COMPLETED",
        "worker_pid": os.getpid(),
    }


def _process_worker_deploy_fail(file_path):  # pragma: no cover - subprocess
    """Always raises — proves worker exceptions are converted to FAILED."""
    raise RuntimeError(f"deliberate failure for {file_path}")


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_processes_engine_requires_worker_fn(executor_cls):
    with pytest.raises(ValueError, match="processes engine requires"):
        executor_cls(
            num_streams=2,
            connect_fn=lambda: GuardedCursor("x"),
            engine="processes",
        )


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_processes_engine_dispatches_to_separate_workers(executor_cls):
    """Each task in the wave runs in a subprocess. The result dict's
    worker_pid is different from the parent process PID, proving the
    work crossed the process boundary."""
    executor = executor_cls(
        num_streams=2,
        connect_fn=lambda: GuardedCursor("unused"),
        engine="processes",
        process_worker_initializer=_process_worker_init_noop,
        process_worker_initargs=("marker-7",),
        process_worker_fn=_process_worker_deploy_echo,
    )

    try:
        result = executor.execute_waves(
            [["a.sql", "b.sql", "c.sql", "d.sql"]],
            deploy_fn=lambda *_: pytest.fail(
                "deploy_fn should not be called in processes mode"
            ),
        )
    finally:
        executor.close_pool()

    assert result.failed == 0
    assert result.completed == 4
    pids = {r.get("worker_pid") for r in result.object_results}
    assert os.getpid() not in pids, "tasks must run in a worker process, not the parent"


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_processes_engine_converts_worker_exception_to_failed(executor_cls):
    executor = executor_cls(
        num_streams=2,
        connect_fn=lambda: GuardedCursor("unused"),
        engine="processes",
        process_worker_fn=_process_worker_deploy_fail,
    )

    try:
        result = executor.execute_waves([["a.sql", "b.sql"]], deploy_fn=lambda *_: None)
    finally:
        executor.close_pool()

    assert result.failed == 2
    assert all(r.get("state") == "FAILED" for r in result.object_results)
    assert all(
        "deliberate failure" in r.get("error", "") for r in result.object_results
    )


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_processes_engine_does_not_open_cursor_pool(executor_cls):
    """processes mode never calls connect_fn — workers manage their
    own connections through the initializer."""
    calls = []

    def _connect():
        calls.append(1)
        return GuardedCursor("x")

    executor = executor_cls(
        num_streams=2,
        connect_fn=_connect,
        engine="processes",
        process_worker_fn=_process_worker_deploy_echo,
    )
    try:
        executor.execute_waves([["a.sql"]], deploy_fn=lambda *_: None)
    finally:
        executor.close_pool()

    assert calls == [], "processes mode must not invoke connect_fn"


@pytest.mark.parametrize("executor_cls", [WaveExecutor, PackagedWaveExecutor])
def test_serial_engine_keeps_max_in_flight_at_one(executor_cls):
    """``serial`` mode must hold the process-wide driver lock,
    serialising every driver entry — guards PR #210's behaviour."""
    _OverlapCountingCursor.reset_counters()
    cursors = [
        _OverlapCountingCursor("stream-1"),
        _OverlapCountingCursor("stream-2"),
    ]
    next_cursor = iter(cursors)

    executor = executor_cls(
        num_streams=2,
        connect_fn=lambda: next(next_cursor),
        engine="serial",
    )

    def deploy_fn(cursor, file_path):
        cursor.deploy_work(file_path)
        return {"file": file_path, "state": "COMPLETED"}

    result = executor.execute_waves(
        [["f0.sql", "f1.sql", "f2.sql", "f3.sql"]],
        deploy_fn,
    )

    assert result.failed == 0
    assert result.completed == 4
    assert _OverlapCountingCursor._max_in_flight == 1, (
        "serial engine must serialise every driver entry"
    )
