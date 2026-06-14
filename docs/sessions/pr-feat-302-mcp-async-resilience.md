# PR: keep the event loop responsive — Workstream B (#302)

## Summary

FastMCP runs a sync tool function inline on the asyncio event loop
(`return fn(**arguments_parsed_dict)`), so while a heavy SHIPS tool
ran the stdio transport could not be serviced and clients hit their
4-minute timeout (`Server disconnected (connection)`). The process
itself never crashed — across the recorded `mcp-server-ships.log`
window there were zero tracebacks.

This PR moves every heavy tool body onto a worker thread via
`anyio.to_thread.run_sync(..., abandon_on_cancel=True)` and emits an
optional progress heartbeat every 15 s so clients see the request as
live and reset their timeout timers.

## What's in the box

- **`src/ships_async.py`** (new) — `run_blocking_with_heartbeat()`
  and the `DEFAULT_HEARTBEAT_SECONDS=15.0` constant.
- **`src/ships_mcp.py`** — each heavy tool body renamed to
  `_ships_<name>_impl()` (no MCP decorator); a corresponding
  `@mcp.tool(name="ships_<name>") async def ships_<name>(... ctx:
  Optional[Context] = None ...)` wrapper now sits in a dedicated
  "Async tool wrappers" section and forwards `ctx.report_progress`
  to the heartbeat helper.
- **`src/tests/test_ships_async.py`** (new) — happy path / event
  loop stays free / heartbeat fires / `report=None` no-op /
  failing report doesn't crash main work / validation /
  exception propagation.
- **`src/tests/test_mcp_server.py`** — imports rewritten to
  `from ships_mcp import _ships_<name>_impl as ships_<name>` so test
  bodies are unchanged and continue to exercise the underlying
  logic synchronously. Tool registration test is unaffected because
  the async wrappers register under the original public name via
  `@mcp.tool(name=...)`.
- **`src/tests/test_mcp_transport.py`** — fake mcp module gains
  `Context` and accepts arbitrary `@mcp.tool(...)` kwargs.

## Refactored tools

`harvest`, `generate`, `inspect`, `analyse`, `package`, `process`,
`deploy`, `deploy_explain`, `rollback`.

`ships_process` internally now calls the `_impl` siblings directly
so the whole sequence runs in one worker thread per process() call
rather than fanning out per stage.

Light tools stay sync: `scaffold`, `decisions`, `verify`,
`explain_run`, plus the authoring / introspection family. They
return in milliseconds and the off-loop cost would dwarf the work.

## Test plan

- [x] `uv run pytest src/tests/test_ships_async.py src/tests/test_mcp_server.py src/tests/test_mcp_transport.py -q` — 59 passed.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: run `ships_process` against the CallCentre
      project (~170 objects) via the MCP tool and confirm no
      `Server disconnected` and that progress notifications fire.

## Out of scope

- Logging / startup banner advertising the log path — landed in #301.
- Parser-side parse-once cache that would further cut CPU on the
  worker thread — see #303.

Closes #302.
