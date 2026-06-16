# PR: fire-and-forget execution for long-running MCP tools — closes #319

## Summary

Claude Desktop enforces a hard ~4-minute deadline on every MCP tool
call regardless of `ctx.report_progress` notifications. The heartbeat
helper added in #302 keeps most clients alive past that window but
doesn't satisfy Claude Desktop's particular implementation. On a
CallCentre-scale payload, `ships_package` exceeds the deadline every
time — the MCP transport is killed mid-run and the packager leaves no
artefact, no decisions entry, no trust score, and no recovery path.

This PR replaces the in-process off-loop execution for the four
heavy tools with detached-subprocess dispatch, and adds
`ships_poll_build` for completion checks.

## What's in the box

### `src/ships_mcp.py`

- **New helper `_launch_background(module, args, project_dir)`** —
  spawns `python -m <module> <args>` as a detached subprocess and
  writes:
  - `<project>/.ships/runs/run_<id>.json` — sentinel containing the
    run id, command, start timestamp, PID, and log path.
  - `<project>/.ships/runs/run_<id>.log` — combined stdout/stderr
    (UTF-8 with `errors="replace"`).

  Atomic sentinel writes via `.tmp` + `rename`. Windows uses
  `CREATE_NO_WINDOW | DETACHED_PROCESS`; POSIX uses
  `start_new_session=True`.

- **New helper `_is_process_alive(pid)`** — cross-platform existence
  check. Windows uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`;
  POSIX uses `os.kill(pid, 0)`. Returns `True` / `False` / `None`
  (indeterminate).

- **Four tool bodies replaced** — `ships_harvest`, `ships_inspect`,
  `ships_package`, `ships_process` now dispatch a subprocess and
  return a dispatch receipt:
  ```python
  {
      "dispatched": True,
      "run_id":     "abc12345",
      "pid":        12345,
      "log_path":   "C:/.../run_abc12345.log",
      "sentinel":   "C:/.../run_abc12345.json",
      "poll_hint":  "Call ships_poll_build with this run_id …"
  }
  ```
  Decorators, signatures, and parameter names are unchanged so MCP
  client schemas don't shift.

  The handover patch had `--source` for `package` and `inspect`;
  actual CLI is `--project` for both. Corrected.

- **New tool `ships_poll_build(project, run_id=None)`** — reads the
  sentinel, checks the PID, and returns:
  ```python
  {
      "run_id":     str | None,
      "status":     "running" | "done" | "failed" | "unknown",
      "pid":        int | None,
      "alive":      bool | None,
      "command":    str | None,
      "started_at": str | None,
      "log_path":   str | None,
      "log_tail":   "last 40 lines",
      "next_step":  "human-readable guidance"
  }
  ```
  Failure signal scanning is case-insensitive over
  `error | traceback | failed | exception`. When `run_id` is omitted,
  the most-recently-modified sentinel under
  `<project>/.ships/runs/` is selected.

### `src/tests/test_mcp_async.py` (new — 16 tests)

- 3 × `_launch_background` (spawn + sentinel shape + log file location)
- 5 × `_is_process_alive` (current PID alive, impossible PID dead,
  edge inputs)
- 8 × `ships_poll_build` (alive=running, dead+clean=done, dead+error=
  failed, case-insensitive error detection, no sentinel dir, missing
  run_id, latest-mtime selection, 40-line tail bound)

## Test plan

- [x] `uv run pytest src/tests/test_mcp_async.py -q` — 16 passed.
- [x] `uv run pytest src/tests/test_mcp_server.py src/tests/test_mcp_transport.py src/tests/test_ships_async.py -q` — 129 passed (one pre-existing warning unrelated to this PR).
- [ ] Full suite: pending background run.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: dispatch `ships_package` against the CallCentre project, confirm the call returns within seconds, poll `ships_poll_build` every 30 s until `status=done`, then call `ships_verify`.

## Caller workflow change

Before (broken on Claude Desktop):
```
ships_package(...)  →  4-minute timeout  →  transport killed, no artefact
```

After:
```
result    = ships_package(project=..., env=..., name=..., env_config=...)
run_id    = result["run_id"]

# Poll until done (typically every 30-60 s)
poll      = ships_poll_build(project=..., run_id=run_id)
# … repeat while poll["status"] == "running"

# Once done, use existing tools for evaluation
verify    = ships_verify(project=...)
describe  = ships_describe_package(project=...)
```

## Out of scope

- Subprocess detach for short / read-only tools (scaffold, analyse,
  verify, decisions, status, describe_package, explain_run, the
  authoring family, deployment tools).
- Cross-machine job durability — sentinels are host-local.
- A real job queue (Celery / RQ / etc.). The sentinel approach is
  enough for the single-host MCP use case.
- Removing the heartbeat helper (`run_blocking_with_heartbeat`) from
  the other tools — it still helps any client whose deadline is
  reset by progress events.

## References

- Handover doc: `HANDOVER_ships_mcp_async_patch.md` (provided
  alongside the issue).
- Reference patch: `ships_mcp_async_patch.py`.
- Carries forward from #302 (heartbeat helper, retained for the
  non-dispatch tools).

Closes #319.
