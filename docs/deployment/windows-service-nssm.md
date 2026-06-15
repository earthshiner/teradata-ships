# Windows Service via NSSM

[NSSM](https://nssm.cc/) (the Non-Sucking Service Manager) wraps any
executable as a proper Windows service. It handles restart on crash,
captures stdout/stderr, and sends the right signal on stop. It's the
shortest path from "Python script that works" to "service that survives
a reboot" on Windows.

`sc.exe` works too but the syntax is fiddly and it doesn't forward
stdout/stderr — for a server like SHIPS that prints its banner to
stderr, NSSM is the easier choice.

## Prerequisites

- Python venv set up in the repo (`uv sync` ran successfully).
- NSSM in `PATH` (`choco install nssm` or download from nssm.cc).
- A non-admin local user to run the service under (recommended;
  `LocalSystem` works but doesn't have a profile, which complicates
  `SHIPS_LOG_DIR`).

## Install

The `samples/install-windows-service.ps1` script does the full install
in one go. Edit the constants at the top, then run it from an elevated
PowerShell:

```powershell
.\samples\install-windows-service.ps1
```

If you'd rather run the commands by hand:

```powershell
$repo = "C:\SCM\teradata-deployment-agent"
$python = "$repo\.venv\Scripts\python.exe"
$logdir = "C:\ProgramData\SHIPS\logs"

# Ensure the log directory exists and is writable by the service account
New-Item -ItemType Directory -Force $logdir | Out-Null
icacls $logdir /grant "NT SERVICE\SHIPS-MCP:(OI)(CI)F" | Out-Null

# Install the service
nssm install SHIPS-MCP $python
nssm set SHIPS-MCP AppParameters `
    "-m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000"
nssm set SHIPS-MCP AppDirectory $repo
nssm set SHIPS-MCP DisplayName "SHIPS MCP Server"
nssm set SHIPS-MCP Description `
    "Teradata SHIPS deployment framework exposed over MCP."
nssm set SHIPS-MCP AppEnvironmentExtra "SHIPS_LOG_DIR=$logdir"

# Auto-start at boot, restart on crash with 5 s back-off
nssm set SHIPS-MCP Start SERVICE_AUTO_START
nssm set SHIPS-MCP AppExit Default Restart
nssm set SHIPS-MCP AppRestartDelay 5000

# Send Ctrl+C on stop so the SHIPS shutdown banner fires
nssm set SHIPS-MCP AppStopMethodConsole 15000
nssm set SHIPS-MCP AppStopMethodWindow  15000
nssm set SHIPS-MCP AppStopMethodThreads 15000

# Start
nssm start SHIPS-MCP
```

## Verify

```powershell
sc.exe query SHIPS-MCP
# STATE should read RUNNING

Get-Content "C:\ProgramData\SHIPS\logs\ships-mcp.log" -Tail 50
# You should see the banner the server prints on startup, including
# the resolved Endpoint and Command lines.

# Smoke test (PowerShell 7+ has Invoke-WebRequest with -Method Post):
Invoke-WebRequest http://localhost:8000/mcp -Method Post `
    -ContentType 'application/json' `
    -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Hardening for production

1. **TLS** — Put IIS / nginx-for-Windows in front, terminating TLS on
   443 and reverse-proxying to `127.0.0.1:8000`. Then bind the service
   to `--host 127.0.0.1` so it's not reachable directly.
2. **Auth** — Once you have an IdP (Azure AD / Okta / AWS Cognito):
   ```powershell
   nssm set SHIPS-MCP AppParameters @"
   -m ships_mcp --transport streamable-http --host 127.0.0.1 --port 8000
   --auth-jwks-uri https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
   --auth-issuer  https://login.microsoftonline.com/<tenant>/v2.0
   --auth-audience api://ships-mcp
   --auth-resource-url http://ships-mcp.internal:8000
   "@
   nssm restart SHIPS-MCP
   ```
3. **Service account** — `nssm edit SHIPS-MCP`, *Log on* tab, pick a
   domain or local user instead of `LocalSystem`. The log directory
   ACL above grants the `NT SERVICE\SHIPS-MCP` virtual account; under
   a custom user, grant that user instead.
4. **Firewall** — explicitly allow inbound TCP 443 (proxy) and **deny**
   8000 from anywhere off-host.

## Uninstall

```powershell
nssm stop   SHIPS-MCP
nssm remove SHIPS-MCP confirm
```

The repo and log directory are untouched.

## Troubleshooting

| Symptom | Probable cause | Fix |
|---|---|---|
| Service stops immediately after `nssm start` | `--transport stdio` (the default) is being used; stdio needs a parent process. | Make sure `AppParameters` includes `--transport streamable-http`. |
| `SHIPS_LOG_DIR` doesn't appear in the rotating log path | Env var didn't reach the child process. | Use `nssm set SHIPS-MCP AppEnvironmentExtra` (NOT `Set-Variable`); restart the service. |
| `Falling back to parsing as a 'Command'` warnings flood the log | sqlglot Teradata parser noise. | Already mitigated by #303 (downgraded to DEBUG). If you still see them, check the log level setting. |
| Long `ships_deploy` requests fail with `Server disconnected` | Stale build before #302's async resilience landed. | `git pull && uv sync && nssm restart SHIPS-MCP`. |
