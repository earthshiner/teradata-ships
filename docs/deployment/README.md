# Running the SHIPS MCP Server as a service

The SHIPS MCP server is normally launched on demand by an MCP client
(Claude Desktop, Claude Code) via the **stdio** transport — the client
spawns and supervises the server as a child process, and the server
exits when the client disconnects.

To run it as a long-lived background service that survives reboots and
crashes, you need three things:

1. **Pick the HTTP transport.** `stdio` is a subprocess protocol; under
   `systemd` / NSSM / `init.d` it would have no client and exit
   immediately. Use `--transport streamable-http` (or `--transport sse`
   for legacy MCP clients) so the server listens on a TCP port.
2. **Front it with TLS.** The server speaks plain HTTP. Put nginx, IIS,
   or an API Gateway in front of it for TLS termination + access
   control. The banner in `src/ships_mcp.py` says as much.
3. **Turn on authentication.** Once it's network-accessible, anyone
   who can reach the port can call every tool. Set `--auth-jwks-uri`
   (and the related `--auth-issuer` / `--auth-audience` /
   `--auth-required-scopes` / `--auth-resource-url` flags) to require
   JWT auth.

Pick the recipe that matches your host:

| Host | Recipe |
|------|--------|
| Windows Server | [windows-service-nssm.md](windows-service-nssm.md) |
| Linux with systemd (modern: any RHEL ≥ 7, Debian ≥ 8, Ubuntu ≥ 16.04) | [linux-systemd.md](linux-systemd.md) |
| Legacy Linux with SysV init (e.g. RHEL 6, AIX-ish) | [linux-sysv-initd.md](linux-sysv-initd.md) |

Sample unit files and install scripts live in [samples/](samples/).

## What you'll want before starting

| Setting | Production value | Why |
|---|---|---|
| Transport | `streamable-http` | A daemon needs a network endpoint. |
| Bind address | `127.0.0.1` (TLS terminator on same host) or `0.0.0.0` (everywhere) | Don't expose plain HTTP to untrusted networks. |
| Port | `8000` by default; anything ≥ 1024 | `--port <N>`, or set in `ships.yaml` `mcp.port`. |
| `SHIPS_LOG_DIR` | `C:\ProgramData\SHIPS\logs` (Windows) or `/var/log/ships` (Linux) | The default per-user paths don't exist for the service account. |
| Log rotation | 5 MiB × 5 backups (default) | See `src/ships_logging.py`. |
| Auth | `--auth-jwks-uri <jwks>` etc. | The server is OAuth 2.0 Resource Server; validates tokens from your IdP. |
| Service account | A non-login user (e.g. `ships`) | Don't run as `root` or `LocalSystem`. |

## How the existing infra helps under a supervisor

- **Rotating log file (#301):** the server's banner advertises the active
  log path on startup, and rotation happens in-process. Service
  supervisors don't have to manage log files themselves.
- **Async resilience (#302):** heavy MCP tools (harvest / package /
  deploy) run off the event loop with progress heartbeats every 15 s, so
  long requests don't trip client timeouts even when the server is one
  hop behind a reverse proxy.
- **Clean shutdown (#301):** sending `SIGINT` (Ctrl+C / `KillSignal=SIGINT`
  / NSSM's stop signal) triggers a stderr shutdown banner and
  `logging.shutdown()` flush before exit. **Don't use plain `SIGTERM`** —
  it bypasses the `KeyboardInterrupt` handler. The systemd and NSSM
  samples already pin the right signal.
- **`ships.yaml` `mcp:` block (#299):** lets you pin transport / host /
  port / path defaults in a project-local config so the systemd /
  NSSM unit only has to set `--config /path/to/ships.yaml`. CLI flags
  still win.

## Testing the service

```sh
# All recipes end with this smoke test:
curl -sSf -X POST http://localhost:8000/mcp \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The response is a JSON-RPC envelope listing every tool the server
exposes. If you get a TCP refused or a 404, the service didn't bind
the port — check the rotating log.
