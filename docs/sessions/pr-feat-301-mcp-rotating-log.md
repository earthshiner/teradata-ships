# PR: rotating file logging + startup banner — Workstream A (#301)

## Summary

The SHIPS MCP server now writes a rotating log file at a stable per-user
path and prints a stderr startup banner advertising where it is. Output
to stdout — the JSON-RPC channel — is defensively suppressed. Repeated
identical records (e.g. the sqlglot parser-fallback warnings emitted by
harvest → inspect → analyse → package on the same DDL) collapse to one
record plus one summary line so the log stays readable.

This is the first of three coordinated PRs addressing the in-call MCP
hangs documented in the resilience handoff. It is a pure-infra change:
no tool handler is touched and no behaviour visible to clients changes
besides the new stderr banner block.

## What's in the box

- **`src/ships_logging.py`** (new) — `configure_logging()`,
  `default_log_dir()`, `banner_lines()`, and `_DeduplicatingFilter`.
- **`src/ships_mcp.py`** — `main()` calls `configure_logging()` before
  starting the transport, prints a unified stderr banner on every
  transport (including stdio), wraps `mcp.run()` with `KeyboardInterrupt`
  handling so Ctrl+C prints a clean shutdown block instead of a
  traceback, and runs `logging.shutdown()` in `finally` so rotation
  flushes cleanly.
- **`src/tests/test_ships_logging.py`** (new) — 14 tests covering env
  override, idempotency, no stdout handler, rotating + stderr handlers
  attached, messages reach the file, dedupe filter behaviour, and
  banner content.
- **`src/tests/test_mcp_transport.py`** — existing banner tests updated
  to the stderr-on-every-transport contract and assert the new `Log file:`
  line.

## Default log paths

| Platform | Path |
|----------|------|
| Windows  | `%LOCALAPPDATA%\SHIPS\logs\ships-mcp.log` |
| POSIX    | `~/.local/state/ships/logs/ships-mcp.log` |
| Override | `$SHIPS_LOG_DIR` env var (always wins) |

Rotation: 5 MiB × 5 backups.

## Banner sample

```
========================================================================
  SHIPS MCP server v1.1.8 — STARTED
  Transport : stdio
  Endpoint  : stdio (subprocess transport — no network port)
  Log file  : C:\Users\<user>\AppData\Local\SHIPS\logs\ships-mcp.log
  Log dir   : C:\Users\<user>\AppData\Local\SHIPS\logs  (override via $SHIPS_LOG_DIR)
  Rotation  : 5 MiB × 5 backups
========================================================================
```

## Test plan

- [x] `uv run pytest src/tests/test_ships_logging.py src/tests/test_mcp_transport.py -q` — 39 passed.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: launch via `python -m ships_mcp` and confirm the
      banner shows on stderr and `ships-mcp.log` appears under the
      override path.

## Out of scope

- Async resilience / event-loop offload — see #302.
- Parser-side fix for the unsupported DDL shapes — see #303.

Closes #301.
