# Linux daemon via systemd

systemd is the right tool for this on any modern Linux distribution
(RHEL ≥ 7, Debian ≥ 8, Ubuntu ≥ 16.04, SUSE 12+). It gives you
dependency ordering, restart-on-failure, a journal-integrated log,
and a clean stop signal — none of which the SysV `init.d` recipe
ships with for free.

If you're on a host new enough to have `/etc/systemd/system/`, use
this guide. The SysV recipe in [linux-sysv-initd.md](linux-sysv-initd.md)
is only there for legacy fleets.

## Prerequisites

- Python venv set up under the repo (`uv sync` ran successfully).
- A dedicated service account (e.g. `ships`).
- Writable log directory (e.g. `/var/log/ships`) owned by that account.

```bash
sudo useradd --system --home /opt/ships --shell /sbin/nologin ships
sudo install -d -o ships -g ships -m 0755 /var/log/ships
```

## Install

Drop the sample unit file into place:

```bash
sudo install -m 0644 docs/deployment/samples/ships-mcp.service \
    /etc/systemd/system/ships-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now ships-mcp
sudo systemctl status ships-mcp
```

The unit file (full text in
[samples/ships-mcp.service](samples/ships-mcp.service)) is short enough
to inline:

```ini
[Unit]
Description=SHIPS MCP Server (Teradata deployment over MCP)
Documentation=https://github.com/earthshiner/teradata-ships
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ships
Group=ships
WorkingDirectory=/opt/ships/teradata-ships
Environment="SHIPS_LOG_DIR=/var/log/ships"
ExecStart=/opt/ships/teradata-ships/.venv/bin/python -m ships_mcp \
    --transport streamable-http --host 127.0.0.1 --port 8000

# Clean shutdown — SIGINT fires the SHIPS shutdown banner +
# logging.shutdown() flush.  Without this systemd would send SIGTERM
# and skip the KeyboardInterrupt handler.
KillSignal=SIGINT
TimeoutStopSec=15

# Restart on crash with 5 s back-off; never restart on a clean exit.
Restart=on-failure
RestartSec=5

# Lightweight sandboxing (enable progressively once your environment
# is known good).  These don't help against compromise in the SHIPS
# code path; they just contain accidental damage from a misbehaving
# tool body.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

## Verify

```bash
systemctl status ships-mcp                 # active (running)
journalctl -u ships-mcp -f                 # banner + tool logs
tail -f /var/log/ships/ships-mcp.log       # the rotating file

# Smoke test
curl -sSf -X POST http://localhost:8000/mcp \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

## Hardening for production

1. **TLS** — Run nginx (or HAProxy / Caddy / Traefik) on the same host,
   terminating TLS on 443 and reverse-proxying to `127.0.0.1:8000`. The
   sample unit already binds to `127.0.0.1` for this reason.
2. **Auth** — Append `--auth-jwks-uri` and friends to `ExecStart`:
   ```ini
   ExecStart=/opt/ships/teradata-ships/.venv/bin/python -m ships_mcp \
       --transport streamable-http --host 127.0.0.1 --port 8000 \
       --auth-jwks-uri  https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys \
       --auth-issuer    https://login.microsoftonline.com/<tenant>/v2.0 \
       --auth-audience  api://ships-mcp \
       --auth-resource-url http://ships-mcp.internal:8000
   ```
   `systemctl daemon-reload && systemctl restart ships-mcp`.
3. **Resource limits** — add to `[Service]`:
   ```ini
   LimitNOFILE=8192
   MemoryMax=2G
   CPUQuota=200%
   ```
4. **Drop sandboxing in stages** — `NoNewPrivileges`, `PrivateTmp`,
   `ProtectSystem=full`, `ProtectHome` are safe defaults. Stricter
   variants (`ProtectSystem=strict`, `ReadWritePaths=...`,
   `RestrictAddressFamilies=...`) are worth setting once you've
   validated the deploy / package workflows still work.

## Uninstall

```bash
sudo systemctl disable --now ships-mcp
sudo rm /etc/systemd/system/ships-mcp.service
sudo systemctl daemon-reload
```

The repo, log directory, and service account are untouched.

## Troubleshooting

| Symptom | Probable cause | Fix |
|---|---|---|
| `systemctl status` reports `active (exited)` immediately | `Type=simple` but the process actually forks (it shouldn't). Check the `ExecStart`. | Make sure `python -m ships_mcp` is the foreground process. |
| Banner missing from journal | stderr is being eaten somewhere. | systemd captures stderr by default; check `journalctl -u ships-mcp` (not `--unit`). |
| `Address already in use` | Another instance is running; possibly the dev one on stdio. | `ss -tlnp \| grep :8000` to find the owner; kill or pick another port. |
| Stop hangs for 15 s, then service is killed | `KillSignal=SIGINT` removed accidentally; default `SIGTERM` skipped the shutdown handler. | Re-add `KillSignal=SIGINT`. |
| `Permission denied` on `/var/log/ships/ships-mcp.log` | Log dir not owned by the service account. | `chown -R ships:ships /var/log/ships`. |
