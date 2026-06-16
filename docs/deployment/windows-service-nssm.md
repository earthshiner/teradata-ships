# Windows Service via NSSM

[NSSM](https://nssm.cc/) (the Non-Sucking Service Manager) wraps any
executable as a proper Windows service. It handles restart on crash,
captures stdout/stderr, and sends the right signal on stop. It's the
shortest path from "Python script that works" to "service that survives
a reboot" on Windows.

`sc.exe` works too but the syntax is fiddly and it doesn't forward
stdout/stderr — for a server like SHIPS that prints its banner to
stderr, NSSM is the easier choice.

## Why we use a wrapper `.cmd` file

The install script writes
`C:\SCM\teradata-ships\ships-mcp-launch.cmd` and points NSSM at that
wrapper rather than at `python.exe` directly. Two hard-won reasons:

1. **`ships_mcp` is not a packaged entry point.** It lives at
   `src/ships_mcp.py`, a top-level module under `src/`. When you run
   `python -m ships_mcp` from a fresh PowerShell it works *only*
   because PowerShell inherits whatever PYTHONPATH `uv run` set up.
   Under a service, the inherited PowerShell environment is gone —
   `python.exe` is launched directly by NSSM, doesn't see `src/`,
   and the import fails with `No module named ships_mcp`.
2. **NSSM's `AppEnvironmentExtra` is fragile.** Multi-variable values
   passed through the CLI have prefix-`:` (append) vs. no-prefix
   (replace-entire-environment) semantics that don't survive
   PowerShell's argument splitting cleanly. Setting `PYTHONPATH` that
   way mid-install has subtle failure modes; even when the value
   reaches NSSM, the missing `PATH` / `SYSTEMROOT` in REPLACE mode can
   stop `python.exe` from loading its DLLs.

The wrapper sidesteps both problems: it sets `PYTHONPATH` and
`SHIPS_LOG_DIR` from inside `cmd.exe`, then `exec`s the venv's
`python.exe -m ships_mcp` with whatever flags NSSM passes through.

## Prerequisites

- Python venv set up in the repo (`uv sync` ran successfully).
- NSSM in `PATH` (`choco install nssm` or download from nssm.cc).
- A non-admin local user to run the service under (recommended;
  `LocalSystem` works but doesn't have a profile, which complicates
  `SHIPS_LOG_DIR`).

## Install (with the script)

`samples/install-windows-service.ps1` does the full install in one
go — wrapper, NSSM registration, env, restart policy, stdout/stderr
capture, the lot. Run it from an **elevated PowerShell**:

```powershell
.\docs\deployment\samples\install-windows-service.ps1
```

The script is idempotent — re-running it after fixing a parameter
is safe.

## Install by hand

If you want to know what the script does, or are wiring this onto a
host that doesn't have PowerShell scripting enabled, the equivalent
commands are:

```powershell
$repo    = "C:\SCM\teradata-ships"
$python  = "$repo\.venv\Scripts\python.exe"
$logdir  = "C:\ProgramData\SHIPS\logs"
$wrapper = "$repo\ships-mcp-launch.cmd"

# 1. Log directory writable by the service account
New-Item -ItemType Directory -Force $logdir | Out-Null
icacls $logdir /grant "NT SERVICE\SHIPS-MCP:(OI)(CI)F" | Out-Null

# 2. Wrapper .cmd that injects PYTHONPATH then exec's python
@"
@echo off
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "SHIPS_LOG_DIR=$logdir"
"%~dp0.venv\Scripts\python.exe" -m ships_mcp %*
"@ | Set-Content -Encoding ASCII $wrapper

# 3. Register the wrapper as the service binary.  Note that AppParameters
#    holds ONLY the SHIPS MCP CLI flags — '-m ships_mcp' is the wrapper's
#    job, NOT NSSM's, or you end up with '-m ships_mcp -m ships_mcp …'
#    in the child invocation.
nssm install SHIPS-MCP $wrapper
nssm set SHIPS-MCP AppParameters `
    "--transport streamable-http --host 127.0.0.1 --port 8000"
nssm set SHIPS-MCP AppDirectory $repo
nssm set SHIPS-MCP DisplayName  "SHIPS MCP Server"
nssm set SHIPS-MCP Description  "Teradata SHIPS deployment framework exposed over MCP."

# 4. Auto-start at boot, restart on crash with 5 s back-off
nssm set SHIPS-MCP Start          SERVICE_AUTO_START
nssm set SHIPS-MCP AppExit Default Restart
nssm set SHIPS-MCP AppRestartDelay 5000

# 5. Send Ctrl+C on stop so the SHIPS shutdown banner fires
nssm set SHIPS-MCP AppStopMethodConsole 15000
nssm set SHIPS-MCP AppStopMethodWindow  15000
nssm set SHIPS-MCP AppStopMethodThreads 15000

# 6. Capture stdout / stderr from the wrapped python.  Anything that
#    happens BEFORE the SHIPS logger is configured (import errors,
#    malformed CLI) only shows up here.
nssm set SHIPS-MCP AppStdout "$logdir\nssm-stdout.log"
nssm set SHIPS-MCP AppStderr "$logdir\nssm-stderr.log"

# 7. Start
nssm start SHIPS-MCP
```

## Verify

```powershell
sc.exe query SHIPS-MCP
# STATE should read RUNNING

Get-Content "C:\ProgramData\SHIPS\logs\ships-mcp.log" -Tail 50
# You should see the banner the server prints on startup, including
# the resolved Endpoint and Command lines.

# Smoke test:
Invoke-WebRequest http://localhost:8000/mcp -Method Post `
    -ContentType 'application/json' `
    -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Hardening for production

1. **TLS** — Put IIS / nginx-for-Windows in front, terminating TLS on
   443 and reverse-proxying to `127.0.0.1:8000`. The samples already
   bind to `127.0.0.1` for this reason.
2. **Auth** — Once you have an IdP (Azure AD / Okta / AWS Cognito),
   append the auth flags to `AppParameters`:
   ```powershell
   nssm set SHIPS-MCP AppParameters @"
   --transport streamable-http --host 127.0.0.1 --port 8000
   --auth-jwks-uri https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
   --auth-issuer  https://login.microsoftonline.com/<tenant>/v2.0
   --auth-audience api://ships-mcp
   --auth-resource-url http://ships-mcp.internal:8000
   "@
   nssm restart SHIPS-MCP
   ```
   (Note: still **no** `-m ships_mcp` — the wrapper handles that.)
3. **Service account** — `nssm edit SHIPS-MCP`, *Log on* tab, pick a
   domain or local user instead of `LocalSystem`. The log directory
   ACL above grants the `NT SERVICE\SHIPS-MCP` virtual account; under
   a custom user, grant that user instead.
4. **Firewall** — explicitly allow inbound TCP 443 (proxy) and **deny**
   8000 from anywhere off-host.

## Upgrading an existing install

If you already have a SHIPS-MCP service from an older install (e.g.
where NSSM was pointed at `python.exe` directly and `AppParameters`
includes `-m ships_mcp`), refresh in place from an **elevated
PowerShell**:

```powershell
# Re-run the install script — it rewrites the wrapper and resets all
# NSSM settings idempotently:
.\docs\deployment\samples\install-windows-service.ps1
sc.exe stop SHIPS-MCP
Start-Sleep -Seconds 3
sc.exe start SHIPS-MCP
```

Or, if you want to keep the existing service definition and just patch
the broken settings:

```powershell
$repo    = "C:\SCM\teradata-ships"
$wrapper = "$repo\ships-mcp-launch.cmd"

# Make sure the wrapper exists (re-create it if not).
nssm set SHIPS-MCP Application   $wrapper
nssm set SHIPS-MCP AppParameters "--transport streamable-http --host 127.0.0.1 --port 8000"
nssm set SHIPS-MCP AppDirectory  $repo
nssm set SHIPS-MCP AppEnvironmentExtra ""   # wrapper handles env
sc.exe stop SHIPS-MCP; Start-Sleep 3; sc.exe start SHIPS-MCP
```

## Uninstall

```powershell
nssm stop   SHIPS-MCP
nssm remove SHIPS-MCP confirm
```

The repo, the wrapper `.cmd`, and the log directory are untouched.

## Troubleshooting

The single most useful diagnostic is the captured stderr — anything
that crashes `python.exe` before SHIPS's own logger configures itself
shows up there:

```powershell
Get-Content C:\ProgramData\SHIPS\logs\nssm-stderr.log -Tail 50
```

Common shapes:

| Symptom | Probable cause | Fix |
|---|---|---|
| `Get-Service SHIPS-MCP` returns `Paused`, `nssm status` says `SERVICE_PAUSED` | NSSM is throttling because the wrapped process exited too fast. Read `nssm-stderr.log` for the real error. | Whatever shows in stderr is the actual bug; the rows below cover the common ones. |
| `nssm-stderr.log` contains `No module named ships_mcp` | `PYTHONPATH` doesn't include `<repo>\src`. Wrapper script not in place or NSSM is launching `python.exe` directly. | Confirm `nssm get SHIPS-MCP Application` returns the `.cmd` path. If it's `python.exe`, re-run the install script or follow the "Upgrading an existing install" section. |
| `nssm-stderr.log` contains `ships_mcp: error: unrecognized arguments: -m ships_mcp` | `AppParameters` still has `-m ships_mcp` baked in from an older install. The wrapper now handles `-m`, so AppParameters must be CLI-flags-only. | `nssm set SHIPS-MCP AppParameters "--transport streamable-http --host 127.0.0.1 --port 8000"` then bounce the service. |
| `nssm get SHIPS-MCP AppEnvironmentExtra` shows the first line *without* a leading `:` | NSSM's REPLACE-mode quirk: the first line had no `:` prefix, so the child process loses inherited `PATH`/`SYSTEMROOT`. | Move all env vars into the wrapper `.cmd` (recommended) or set them one at a time with `:` prefixes via `nssm edit SHIPS-MCP` → *Environment* tab. |
| Services GUI: "Windows could not start the SHIPS MCP Server service. The service did not return an error." | `python.exe` exited too fast for the SCM to record an error code. Almost always one of the cases above. | Read `nssm-stderr.log`. |
| Service is `Running`, log shows banner, but HTTP request times out | Bound to wrong host or firewall. | The banner's `Endpoint` line is authoritative — point your HTTP request at exactly that URL. |
| Banner says `Transport : stdio` | `AppParameters` missing `--transport streamable-http`. | `nssm get SHIPS-MCP AppParameters` to inspect; `nssm edit SHIPS-MCP` to fix. |
| `Falling back to parsing as a 'Command'` warnings flood the log | sqlglot Teradata parser noise. | Already mitigated by #303 (downgraded to DEBUG). If you still see them, check the log level setting. |
| Long `ships_deploy` requests fail with `Server disconnected` | Stale build before #302's async resilience landed. | `git pull && uv sync && nssm restart SHIPS-MCP`. |

If `nssm-stderr.log` doesn't exist at all, the service was installed
without stdout/stderr capture — turn it on for next time:

```powershell
nssm set SHIPS-MCP AppStdout "C:\ProgramData\SHIPS\logs\nssm-stdout.log"
nssm set SHIPS-MCP AppStderr "C:\ProgramData\SHIPS\logs\nssm-stderr.log"
sc.exe stop SHIPS-MCP; Start-Sleep 3; sc.exe start SHIPS-MCP
```
