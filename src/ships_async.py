"""ships_async.py — Async helpers that keep the MCP event loop responsive.

The SHIPS tool bodies (harvest / generate / inspect / analyse / package /
process / deploy / deploy_explain / rollback / describe_package) are CPU-
and IO-bound and were previously executed inline on the asyncio event
loop by FastMCP — which calls a sync tool function directly via
``return fn(**...)`` rather than via :func:`anyio.to_thread.run_sync`.
While such a sync handler ran, the stdio transport could not be
serviced and the client treated the request as dropped after its
internal timeout (typically 4 minutes), surfacing as
``Server disconnected (connection)``.

This module's :func:`run_blocking_with_heartbeat` runs the blocking
callable on a worker thread (via :func:`anyio.to_thread.run_sync`) so
the event loop stays free to service the transport, and emits an
optional progress notification every ``heartbeat_seconds`` so the
client's request-timeout timer keeps resetting.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Optional

import anyio

logger = logging.getLogger("ships.async")

#: Default heartbeat interval when callers do not specify one.  Picked to
#: comfortably beat MCP client timeouts (Claude Desktop's default is
#: 60 s; many clients are higher).
DEFAULT_HEARTBEAT_SECONDS: float = 15.0


async def run_blocking_with_heartbeat(
    func: Callable[..., Any],
    *args: Any,
    report: Optional[Callable[[int], Awaitable[None]]] = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    **kwargs: Any,
) -> Any:
    """Run a blocking callable on a worker thread, emitting heartbeats.

    The event loop stays free to service the stdio (or HTTP) transport
    while ``func`` runs, and ``report`` — if supplied — is awaited every
    ``heartbeat_seconds`` until the work signals completion, resetting
    the client's request-timeout timer.

    :param func:              Blocking (synchronous) callable to execute.
    :param args:              Positional arguments forwarded to ``func``.
    :param report:            Optional async progress callback.  Receives
                              the heartbeat tick count (1-based int).
                              Pass :pyattr:`mcp.server.fastmcp.Context.report_progress`
                              for FastMCP clients.
    :param heartbeat_seconds: Interval between heartbeats.  Must be > 0.
    :param kwargs:            Keyword arguments forwarded to ``func``.
    :returns:                 Whatever ``func`` returns.

    .. note::
        The worker thread is launched with ``abandon_on_cancel=True``,
        so a cancelled task does not block shutdown waiting for the
        blocking call to notice cancellation.  This is the right
        trade-off for an MCP server — responsiveness over thread-cleanup
        purity — but it means a runaway tool body can leak a worker
        thread until the process exits.
    """
    if heartbeat_seconds <= 0:
        raise ValueError(f"heartbeat_seconds must be > 0, got {heartbeat_seconds!r}")

    call = functools.partial(func, *args, **kwargs)
    done = anyio.Event()

    async def _heartbeat() -> None:
        """Emit a progress tick every ``heartbeat_seconds`` until done."""
        ticks = 0
        while not done.is_set():
            with anyio.move_on_after(heartbeat_seconds):
                await done.wait()
            if done.is_set():
                return
            if report is None:
                continue
            ticks += 1
            try:
                await report(ticks)
            except Exception:
                # Heartbeats are best-effort.  If the client's progress
                # channel is broken, do not crash the main work.
                logger.debug("heartbeat callback raised; suppressing", exc_info=True)

    async with anyio.create_task_group() as tg:
        if report is not None:
            tg.start_soon(_heartbeat)
        try:
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        finally:
            done.set()
