# Legacy Linux daemon via `/etc/init.d`

If you're on a host new enough to have `/etc/systemd/system/`,
**use [linux-systemd.md](linux-systemd.md) instead.** systemd gives
you dependency ordering, restart-on-failure, and a journal-integrated
log without writing any bash; SysV init scripts give you none of that.

The recipe here is for hosts that genuinely don't have systemd:
RHEL ≤ 6, very old Debian / Ubuntu LTS, AIX-leaning Linux distros, or
embedded boxes. The script depends on `start-stop-daemon`, which is
present on every Debian-family and most RHEL-family hosts.

## Prerequisites

- Python venv set up under the repo (`uv sync` ran successfully).
- A dedicated service account (e.g. `ships`).
- Writable log directory (e.g. `/var/log/ships`) owned by that account.
- `start-stop-daemon` available (it is on Debian/Ubuntu; on older RHEL
  install the `daemonize` package if missing).

```bash
sudo useradd --system --home /opt/ships --shell /sbin/nologin ships
sudo install -d -o ships -g ships -m 0755 /var/log/ships
```

## Install

```bash
sudo install -m 0755 docs/deployment/samples/ships-mcp.initd \
    /etc/init.d/ships-mcp

# Wire it to the boot sequence:
# Debian / Ubuntu:
sudo update-rc.d ships-mcp defaults
# RHEL / CentOS / OL ≤ 6:
sudo chkconfig --add ships-mcp
sudo chkconfig ships-mcp on

sudo /etc/init.d/ships-mcp start
sudo /etc/init.d/ships-mcp status
```

The script's full text lives in
[samples/ships-mcp.initd](samples/ships-mcp.initd). The key bits:

- Drops to user `ships` via `start-stop-daemon --chuid`.
- Backgrounds the server with `--background` and writes a PID file to
  `/var/run/ships-mcp.pid`.
- Sends `SIGINT` on stop so the SHIPS shutdown banner +
  `logging.shutdown()` fire (not the default `SIGTERM`).
- Sets `SHIPS_LOG_DIR=/var/log/ships` so the rotating log lands somewhere
  the operator can `tail`.

## Verify

```bash
sudo /etc/init.d/ships-mcp status
tail -f /var/log/ships/ships-mcp.log

curl -sSf -X POST http://localhost:8000/mcp \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Production caveats

| Concern | SysV reality |
|---|---|
| **Auto-restart on crash** | Not included. Add a `cron` watchdog or — preferably — move to systemd. |
| **Log journal** | The init script only knows about the rotating file. Pair with `logrotate` if your distro doesn't auto-rotate `/var/log/`. |
| **Dependency ordering** | The `chkconfig` headers in the sample request `$network`. If you depend on a local PostgreSQL or vault, add it to `Required-Start`. |
| **Reload without restart** | Not implemented; the server doesn't support SIGHUP-style reload. Restart the service to pick up CLI / env changes. |

## Hardening

The same TLS / auth / firewall guidance as the systemd recipe applies.
Edit `DAEMON_OPTS` in the init script to add `--auth-jwks-uri` and
friends. Put nginx in front for TLS.

## Uninstall

```bash
sudo /etc/init.d/ships-mcp stop

# Debian / Ubuntu:
sudo update-rc.d -f ships-mcp remove
# RHEL / CentOS / OL ≤ 6:
sudo chkconfig ships-mcp off
sudo chkconfig --del ships-mcp

sudo rm /etc/init.d/ships-mcp
```

## When to migrate to systemd

If your distro reaches end of life and you upgrade to one with systemd
(RHEL 7+, Debian 8+, Ubuntu 16.04+), retire the SysV script and replace
it with `samples/ships-mcp.service`. Don't leave both in place —
`systemd-sysv-generator` will pick up the init script and you'll end up
with two units fighting over the same port.
