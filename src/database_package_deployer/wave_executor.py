"""
wave_executor.py — Parallel wave-based deployment executor.

Executes deployment waves across multiple parallel database
connections (streams). Each wave is a synchronisation barrier:
wave N+1 does not start until every object in wave N has
completed successfully.

Architecture:
    - A connection pool of N connections (streams), each with
      its own cursor and query band tagging (STREAM=1..N).
    - ThreadPoolExecutor drives parallelism — teradatasql
      releases the GIL during network I/O, so threads are
      effective.
    - On any failure within a wave: drain-and-stop. Running
      streams finish their current object, but no new objects
      are submitted in this wave, and subsequent waves are
      skipped.

Limits:
    - Minimum streams: 1 (sequential execution).
    - Maximum streams: 8 (prevents AMP contention and TPA
      lock conflicts on smaller Teradata systems).
    - Default: 4.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -- Stream limits --
MIN_STREAMS = 1
MAX_STREAMS = 8
DEFAULT_STREAMS = 4


class WaveExecutor:
    """
    Execute deployment waves in parallel across multiple streams.

    Each stream is an independent database connection that can
    execute DDL/DCL/DML concurrently. Waves enforce ordering
    barriers — all objects in wave N must complete before wave
    N+1 begins.

    Attributes:
        num_streams:    Number of parallel connections.
        connect_fn:     Callable that returns a new database cursor.
        build_number:   For query band tagging.
        package_name:   For query band tagging.
        environment:    For query band tagging.
    """

    def __init__(
        self,
        num_streams: int,
        connect_fn: Callable[[], Any],
        build_number: str = "",
        package_name: str = "",
        environment: str = "",
    ):
        """
        Initialise the wave executor.

        Args:
            num_streams:    Number of parallel streams (1–8).
            connect_fn:     Function that returns a new database cursor.
                            Called once per stream during pool creation.
            build_number:   Build number for query band.
            package_name:   Package name for query band.
            environment:    Environment name for query band.

        Raises:
            ValueError: If num_streams is outside 1–8 range.
        """
        if num_streams < MIN_STREAMS or num_streams > MAX_STREAMS:
            raise ValueError(
                f"Stream count must be {MIN_STREAMS}–{MAX_STREAMS}, got {num_streams}."
            )

        self.num_streams = num_streams
        self.connect_fn = connect_fn
        self.build_number = build_number
        self.package_name = package_name
        self.environment = environment

        self._cursors: List[Any] = []
        self._cursor_locks: List[threading.Lock] = []
        self._pool_created = False

    def create_pool(self):
        """
        Create the connection pool.

        Opens num_streams connections and sets a stream-specific
        query band on each.
        """
        logger.info("Creating connection pool: %d streams", self.num_streams)

        for stream_id in range(1, self.num_streams + 1):
            cursor = self.connect_fn()

            # Set stream-specific query band
            try:
                band = (
                    f"BUILD={self.build_number};"
                    f"PKG={self.package_name};"
                    f"ENV={self.environment};"
                    f"STREAM={stream_id};"
                    f"DEPLOYER=database_package_deployer_v2;"
                )
                cursor.execute(f"SET QUERY_BAND = '{band}' FOR SESSION")
                logger.debug("Stream %d: query band set", stream_id)
            except Exception as e:
                logger.warning(
                    "Stream %d: query band failed (non-fatal): %s", stream_id, e
                )

            self._cursors.append(cursor)
            self._cursor_locks.append(threading.Lock())

        self._pool_created = True
        logger.info("Connection pool ready: %d streams", len(self._cursors))

    def close_pool(self):
        """Close all connections in the pool."""
        for i, cursor in enumerate(self._cursors, 1):
            try:
                cursor.execute("SET QUERY_BAND = NONE FOR SESSION")
            except Exception:
                pass
            try:
                cursor.close()
                cursor.connection.close()
            except Exception:
                pass
            logger.debug("Stream %d: closed", i)

        self._cursors.clear()
        self._cursor_locks.clear()
        self._pool_created = False
        logger.info("Connection pool closed.")

    def execute_waves(
        self,
        waves: List[List[str]],
        deploy_fn: Callable[[Any, str], dict],
        on_complete: Optional[Callable[[str, dict], None]] = None,
        prior_completed: Optional[List[dict]] = None,
    ) -> "WaveExecutionResult":
        """
        Execute all waves with parallel streams.

        Each object in a wave is submitted to a stream for
        deployment. The deploy_fn receives a cursor and a file
        path, and returns a result dict. The on_complete callback
        is called after each object finishes (for manifest updates).

        Error strategy: drain-and-stop. If any object in a wave
        fails, running objects in that wave are allowed to finish,
        but no new objects are started and subsequent waves are
        skipped.

        Args:
            waves:            List of waves, each a list of file paths.
            deploy_fn:        Function(cursor, file_path) → result dict.
            on_complete:      Optional callback(file_path, result) called
                              after each object completes.
            prior_completed:  Optional list of manifest records for objects
                              that were COMPLETED in a prior run and not
                              re-deployed. Passed through to the result
                              so the report can distinguish 'nothing new
                              to deploy' from 'nothing was processed'.

        Returns:
            WaveExecutionResult with per-wave and per-object outcomes.
        """
        if not self._pool_created:
            self.create_pool()

        total_objects = sum(len(w) for w in waves)
        logger.info(
            "Executing %d waves, %d objects, %d streams",
            len(waves),
            total_objects,
            self.num_streams,
        )

        wave_results = []
        all_results = []
        failed = False
        completed_count = 0

        for wave_idx, wave in enumerate(waves):
            wave_num = wave_idx + 1

            if failed:
                # Skip remaining waves after a failure
                logger.warning(
                    "Skipping wave %d/%d (previous wave failed)", wave_num, len(waves)
                )
                # Record skipped objects
                for fpath in wave:
                    result = {
                        "file": fpath,
                        "state": "SKIPPED",
                        "wave": wave_num,
                        "message": "Skipped — previous wave failed.",
                    }
                    all_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)

                wave_results.append(
                    WaveResult(
                        wave_number=wave_num,
                        total=len(wave),
                        completed=0,
                        failed=0,
                        skipped=len(wave),
                        duration_ms=0,
                    )
                )
                continue

            # Execute this wave — wrapped in an OTel span when configured
            from ships_tracing import stage_span

            wave_start = time.monotonic()
            with stage_span(
                "ships.deploy.wave",
                **{
                    "ships.wave_number": wave_num,
                    "ships.wave_total": len(waves),
                    "ships.wave_objects": len(wave),
                    "ships.num_streams": self.num_streams,
                },
            ) as _wspan:
                w_result = self._execute_single_wave(
                    wave_num, wave, deploy_fn, on_complete
                )
                w_result.duration_ms = int((time.monotonic() - wave_start) * 1000)
                _wspan.set_attribute("ships.wave_completed", w_result.completed)
                _wspan.set_attribute("ships.wave_failed", w_result.failed)
                _wspan.set_attribute("ships.wave_duration_ms", w_result.duration_ms)

            wave_results.append(w_result)
            completed_count += w_result.completed

            # Collect per-object results
            all_results.extend(w_result.object_results)

            if w_result.failed > 0:
                logger.error(
                    "Wave %d: %d failure(s) — stopping after this wave.",
                    wave_num,
                    w_result.failed,
                )
                failed = True

            logger.info(
                "Wave %d/%d complete: %d ok, %d failed (%d ms)",
                wave_num,
                len(waves),
                w_result.completed,
                w_result.failed,
                w_result.duration_ms,
            )

        return WaveExecutionResult(
            total_waves=len(waves),
            total_objects=total_objects,
            completed=completed_count,
            failed=sum(w.failed for w in wave_results),
            skipped=sum(w.skipped for w in wave_results),
            waves=wave_results,
            object_results=all_results,
            prior_completed=prior_completed or [],
        )

    def _execute_single_wave(
        self,
        wave_num: int,
        file_paths: List[str],
        deploy_fn: Callable[[Any, str], dict],
        on_complete: Optional[Callable] = None,
    ) -> "WaveResult":
        """
        Execute a single wave across parallel streams.

        Uses ThreadPoolExecutor to dispatch objects to available
        cursors. Drain-and-stop on failure: if a future raises,
        running futures complete but no new submissions occur.

        Args:
            wave_num:    Wave number (1-based, for logging).
            file_paths:  Files to deploy in this wave.
            deploy_fn:   Deployment function.
            on_complete: Per-object completion callback.

        Returns:
            WaveResult with outcomes.
        """
        completed = 0
        failed_count = 0
        object_results = []

        # For sequential mode (1 stream), skip the thread overhead
        if self.num_streams == 1:
            for fpath in file_paths:
                cursor = self._cursors[0]
                self._update_stream_query_band(cursor, 1, wave_num, fpath)
                try:
                    result = deploy_fn(cursor, fpath)
                    result["wave"] = wave_num
                    result["stream"] = 1

                    if result.get("state") == "FAILED":
                        failed_count += 1
                    else:
                        completed += 1

                    object_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)

                    if failed_count > 0:
                        break  # Drain-and-stop (nothing else running)

                except Exception as e:
                    failed_count += 1
                    result = {
                        "file": fpath,
                        "state": "FAILED",
                        "wave": wave_num,
                        "stream": 1,
                        "error": str(e),
                    }
                    object_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)
                    break

            return WaveResult(
                wave_number=wave_num,
                total=len(file_paths),
                completed=completed,
                failed=failed_count,
                skipped=len(file_paths) - completed - failed_count,
                object_results=object_results,
            )

        # Parallel execution
        # Assign cursors round-robin; cap at num_streams
        effective_streams = min(self.num_streams, len(file_paths))
        error_occurred = False

        with ThreadPoolExecutor(max_workers=effective_streams) as executor:
            # Submit all objects, assigning stream IDs round-robin
            futures: Dict[Future, Tuple[str, int]] = {}

            for i, fpath in enumerate(file_paths):
                if error_occurred:
                    # Drain-and-stop: don't submit new work
                    result = {
                        "file": fpath,
                        "state": "SKIPPED",
                        "wave": wave_num,
                        "message": "Skipped — error in this wave.",
                    }
                    object_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)
                    continue

                stream_id = i % effective_streams
                cursor = self._cursors[stream_id]

                future = executor.submit(
                    self._deploy_on_stream,
                    cursor,
                    self._cursor_locks[stream_id],
                    stream_id + 1,
                    wave_num,
                    fpath,
                    deploy_fn,
                )
                futures[future] = (fpath, stream_id + 1)

            # Collect results as they complete
            for future in as_completed(futures):
                fpath, stream_id = futures[future]

                try:
                    result = future.result()
                    result["wave"] = wave_num
                    result["stream"] = stream_id

                    if result.get("state") == "FAILED":
                        failed_count += 1
                        error_occurred = True
                    else:
                        completed += 1

                    object_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)

                except Exception as e:
                    failed_count += 1
                    error_occurred = True
                    result = {
                        "file": fpath,
                        "state": "FAILED",
                        "wave": wave_num,
                        "stream": stream_id,
                        "error": str(e),
                    }
                    object_results.append(result)
                    if on_complete:
                        on_complete(fpath, result)

        skipped = len(file_paths) - completed - failed_count
        return WaveResult(
            wave_number=wave_num,
            total=len(file_paths),
            completed=completed,
            failed=failed_count,
            skipped=skipped,
            object_results=object_results,
        )

    def _deploy_on_stream(
        self,
        cursor,
        cursor_lock: threading.Lock,
        stream_id: int,
        wave_num: int,
        file_path: str,
        deploy_fn: Callable,
    ) -> dict:
        """
        Deploy a single object on a specific stream.

        Updates the query band with the current file, then
        delegates to the deploy_fn.

        Args:
            cursor:     Stream's database cursor.
            cursor_lock:
                        Per-cursor lock. A cursor must never be used by two
                        threads at once; teradatasql cursor/result handles are
                        connection-local and not thread-safe.
            stream_id:  Stream number (1-based).
            wave_num:   Wave number (1-based).
            file_path:  File to deploy.
            deploy_fn:  Deployment function.

        Returns:
            Result dict from deploy_fn.
        """
        with cursor_lock:
            self._update_stream_query_band(cursor, stream_id, wave_num, file_path)
            return deploy_fn(cursor, file_path)

    def _update_stream_query_band(
        self, cursor, stream_id: int, wave_num: int, file_path: str
    ):
        """Update the query band with current wave and file."""
        try:
            fname = os.path.basename(file_path)
            band = (
                f"BUILD={self.build_number};"
                f"PKG={self.package_name};"
                f"ENV={self.environment};"
                f"STREAM={stream_id};"
                f"WAVE={wave_num};"
                f"FILE={fname};"
            )
            cursor.execute(f"SET QUERY_BAND = '{band}' FOR SESSION")
        except Exception:
            pass  # Non-fatal


# ---------------------------------------------------------------
# Result classes
# ---------------------------------------------------------------


class WaveResult:
    """Outcome of a single wave execution."""

    def __init__(
        self,
        wave_number: int,
        total: int,
        completed: int = 0,
        failed: int = 0,
        skipped: int = 0,
        duration_ms: int = 0,
        object_results: List[dict] = None,
    ):
        self.wave_number = wave_number
        self.total = total
        self.completed = completed
        self.failed = failed
        self.skipped = skipped
        self.duration_ms = duration_ms
        self.object_results = object_results or []

    @property
    def success(self) -> bool:
        """True if all objects in this wave completed."""
        return self.failed == 0 and self.skipped == 0


class WaveExecutionResult:
    """
    Aggregate outcome of all waves.

    Attributes:
        prior_completed: List of manifest records for objects that
            were COMPLETED in a prior deployment run and not
            re-deployed in this run. Enables the report to
            distinguish 'nothing new to deploy' (all objects
            validly completed from a previous run) from 'nothing
            was processed' (a genuine problem).
    """

    def __init__(
        self,
        total_waves: int,
        total_objects: int,
        completed: int = 0,
        failed: int = 0,
        skipped: int = 0,
        waves: List[WaveResult] = None,
        object_results: List[dict] = None,
        prior_completed: List[dict] = None,
    ):
        self.total_waves = total_waves
        self.total_objects = total_objects
        self.completed = completed
        self.failed = failed
        self.skipped = skipped
        self.waves = waves or []
        self.object_results = object_results or []
        self.prior_completed = prior_completed or []

    @property
    def success(self) -> bool:
        """True if all objects across all waves completed."""
        return self.failed == 0 and self.skipped == 0

    @property
    def is_noop_redeploy(self) -> bool:
        """
        True if this run deployed nothing new but has prior
        completed objects — i.e. a re-run of an already-deployed
        package where all objects still exist in the database.
        """
        return self.total_objects == 0 and len(self.prior_completed) > 0
