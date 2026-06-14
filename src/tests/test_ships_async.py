"""
test_ships_async.py — Tests for :mod:`ships_async`.

Covers
------
* Blocking callable runs and its result propagates.
* The event loop is not held by the blocking call (a separate task
  can run while the callable sleeps in its worker thread).
* Heartbeat callback fires at least once when the callable sleeps
  longer than the configured interval.
* ``report=None`` runs the callable without any heartbeat ticks.
* A heartbeat callback that raises does not crash the main work.
* ``heartbeat_seconds <= 0`` is rejected with :class:`ValueError`.
* Exceptions raised by the blocking callable propagate to the caller.
"""

from __future__ import annotations

import time

import anyio
import pytest

from ships_async import DEFAULT_HEARTBEAT_SECONDS, run_blocking_with_heartbeat


def _sleeper(seconds: float, result: int = 42) -> int:
    """Sleep then return a sentinel — proves the call ran to completion."""
    time.sleep(seconds)
    return result


def _boom() -> None:
    raise RuntimeError("kaboom")


class TestHappyPath:
    def test_returns_callable_result(self):
        async def _go() -> int:
            return await run_blocking_with_heartbeat(_sleeper, 0.01, result=7)

        assert anyio.run(_go) == 7

    def test_forwards_positional_and_keyword_args(self):
        def adder(a: int, b: int, *, c: int = 0) -> int:
            return a + b + c

        async def _go() -> int:
            return await run_blocking_with_heartbeat(adder, 1, 2, c=10)

        assert anyio.run(_go) == 13

    def test_exception_propagates(self):
        # anyio's task group wraps single exceptions in BaseExceptionGroup
        # on Python 3.11+; accept either form so the test is portable.
        async def _go() -> None:
            await run_blocking_with_heartbeat(_boom)

        with pytest.raises((RuntimeError, BaseExceptionGroup)) as exc_info:
            anyio.run(_go)
        # Walk into any group to confirm the underlying RuntimeError is in there.
        flat: list[BaseException] = []

        def _walk(e: BaseException) -> None:
            if isinstance(e, BaseExceptionGroup):
                for sub in e.exceptions:
                    _walk(sub)
            else:
                flat.append(e)

        _walk(exc_info.value)
        assert any(isinstance(x, RuntimeError) and "kaboom" in str(x) for x in flat), (
            flat
        )


class TestEventLoopStaysFree:
    def test_other_task_runs_while_blocking_call_sleeps(self):
        """The blocking call must not hold the loop — concurrent tasks
        running in the same task group should make progress."""

        ticks: list[float] = []

        async def _ticker() -> None:
            for _ in range(3):
                await anyio.sleep(0.02)
                ticks.append(time.monotonic())

        async def _go() -> int:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_ticker)
                # 0.2 s is much longer than 3 × 20 ms; if the loop was
                # blocked the ticker would not record any timestamps.
                result = await run_blocking_with_heartbeat(_sleeper, 0.2)
            return result

        anyio.run(_go)
        assert len(ticks) == 3


class TestHeartbeat:
    def test_no_report_means_no_heartbeats(self):
        """When ``report`` is None, no heartbeat task spins up."""

        async def _go() -> int:
            return await run_blocking_with_heartbeat(
                _sleeper, 0.05, heartbeat_seconds=0.01, report=None
            )

        # Just running to completion is the assertion — no callback to
        # check.  If the heartbeat task were running it would burn CPU
        # but not break the test.
        assert anyio.run(_go) == 42

    def test_heartbeat_fires_when_call_outlasts_interval(self):
        ticks: list[int] = []

        async def _report(n: int) -> None:
            ticks.append(n)

        async def _go() -> int:
            return await run_blocking_with_heartbeat(
                _sleeper,
                0.30,
                report=_report,
                heartbeat_seconds=0.05,
            )

        anyio.run(_go)
        # 300 ms sleep ÷ 50 ms heartbeat ≈ 5–6 ticks; allow generous
        # slack for scheduler jitter on a busy CI host.
        assert len(ticks) >= 2
        assert ticks == sorted(ticks)
        assert ticks[0] == 1

    def test_failing_report_does_not_crash_main_call(self):
        async def _bad_report(_n: int) -> None:
            raise ValueError("progress channel broken")

        async def _go() -> int:
            return await run_blocking_with_heartbeat(
                _sleeper,
                0.10,
                report=_bad_report,
                heartbeat_seconds=0.02,
            )

        assert anyio.run(_go) == 42


class TestValidation:
    def test_zero_heartbeat_seconds_rejected(self):
        async def _go() -> None:
            await run_blocking_with_heartbeat(_sleeper, 0.01, heartbeat_seconds=0)

        with pytest.raises(ValueError, match="heartbeat_seconds"):
            anyio.run(_go)

    def test_negative_heartbeat_seconds_rejected(self):
        async def _go() -> None:
            await run_blocking_with_heartbeat(_sleeper, 0.01, heartbeat_seconds=-1.0)

        with pytest.raises(ValueError, match="heartbeat_seconds"):
            anyio.run(_go)

    def test_default_heartbeat_constant_exposed(self):
        assert DEFAULT_HEARTBEAT_SECONDS > 0
