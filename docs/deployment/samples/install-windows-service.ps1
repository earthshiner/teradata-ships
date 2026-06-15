<#
.SYNOPSIS
    Install the SHIPS MCP Server as a Windows Service via NSSM.

.DESCRIPTION
    See docs/deployment/windows-service-nssm.md for the full
    walkthrough.  Run this script from an elevated PowerShell.  Edit
    the constants in the param block before running.

.NOTES
    Requires NSSM in PATH (choco install nssm or download from
    https://nssm.cc/).  Re-runs are safe: NSSM 'install' is a no-op if
    the service already exists, and the 'set' commands are idempotent.
#>

param(
    [string] $ServiceName  = "SHIPS-MCP",
    [string] $DisplayName  = "SHIPS MCP Server",
    [string] $Repo         = "C:\SCM\teradata-ships",
    [string] $Python       = "C:\SCM\teradata-ships\.venv\Scripts\python.exe",
    [string] $LogDir       = "C:\ProgramData\SHIPS\logs",
    [string] $BindHost     = "127.0.0.1",
    [int]    $Port         = 8000
)

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    throw "nssm.exe not found in PATH.  Install it (choco install nssm) and re-run."
}
if (-not (Test-Path $Python)) {
    throw "Python venv not found at $Python — run 'uv sync' under $Repo first."
}

Write-Host "Creating log directory $LogDir"
New-Item -ItemType Directory -Force $LogDir | Out-Null
# Grant the virtual service account write access to the log directory.
# Once the service is installed, the SID 'NT SERVICE\<ServiceName>' is
# created and ICACLS will resolve it.
icacls $LogDir /grant "NT SERVICE\${ServiceName}:(OI)(CI)F" | Out-Null

Write-Host "Installing service $ServiceName"
nssm install $ServiceName $Python | Out-Null

$AppParameters = "-m ships_mcp --transport streamable-http --host $BindHost --port $Port"

nssm set $ServiceName AppParameters         $AppParameters             | Out-Null
nssm set $ServiceName AppDirectory          $Repo                      | Out-Null
nssm set $ServiceName DisplayName           $DisplayName               | Out-Null
nssm set $ServiceName Description           "Teradata SHIPS deployment framework exposed over MCP." | Out-Null
nssm set $ServiceName AppEnvironmentExtra   "SHIPS_LOG_DIR=$LogDir"    | Out-Null

# Auto-start at boot; restart on crash with 5 s back-off.
nssm set $ServiceName Start                 SERVICE_AUTO_START         | Out-Null
nssm set $ServiceName AppExit Default       Restart                    | Out-Null
nssm set $ServiceName AppRestartDelay       5000                       | Out-Null

# Send Ctrl+C on stop so the SHIPS shutdown banner fires.
nssm set $ServiceName AppStopMethodConsole  15000 | Out-Null
nssm set $ServiceName AppStopMethodWindow   15000 | Out-Null
nssm set $ServiceName AppStopMethodThreads  15000 | Out-Null

Write-Host "Starting $ServiceName"
nssm start $ServiceName | Out-Null

# Smoke test
Start-Sleep -Seconds 3
$status = (sc.exe query $ServiceName | Select-String "STATE").Line.Trim()
Write-Host "Service state: $status"
Write-Host "Banner location: $LogDir\ships-mcp.log"
Write-Host ""
Write-Host "Smoke test:"
Write-Host "  Invoke-WebRequest http://${BindHost}:${Port}/mcp -Method Post ``"
Write-Host "      -ContentType 'application/json' ``"
Write-Host "      -Body '{`"jsonrpc`":`"2.0`",`"id`":1,`"method`":`"tools/list`"}'"
