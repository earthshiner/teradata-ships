# PR: Windows Service + Linux daemon recipes — closes #313

## Summary

Documents how to run the SHIPS MCP server as a long-lived service on
either Windows or Linux, using the infrastructure that already shipped
on `main`:

- Rotating log file + banner (#301).
- Off-loop tool execution with progress heartbeats (#302).
- Clean `KeyboardInterrupt`-driven shutdown (#301).
- `ships.yaml` `mcp:` block for declarative defaults (#299).
- JWT auth via `--auth-jwks-uri` and friends (#292).

## What's in the box

- **`docs/deployment/README.md`** — picks platform, flags the
  "stdio doesn't work as a daemon" gotcha, lists the production
  checklist (transport / log dir / auth / TLS / service account),
  smoke test.
- **`docs/deployment/windows-service-nssm.md`** — NSSM walkthrough +
  hardening + uninstall + troubleshooting matrix.
- **`docs/deployment/linux-systemd.md`** — systemd unit walkthrough +
  sandboxing notes + hardening + uninstall + troubleshooting matrix.
- **`docs/deployment/linux-sysv-initd.md`** — legacy `init.d` recipe
  (only when systemd isn't an option).
- **`docs/deployment/samples/ships-mcp.service`** — drop-in systemd
  unit (`KillSignal=SIGINT` so the shutdown banner fires).
- **`docs/deployment/samples/ships-mcp.initd`** — drop-in SysV init
  script (uses `start-stop-daemon`, signals `SIGINT` on stop).
- **`docs/deployment/samples/install-windows-service.ps1`** —
  parameterised NSSM install script.

## Docs-only

No production code touched. Existing tests untouched.

## Out of scope

- Containerisation (Docker / Podman / Kubernetes) — separate concern.
- Specific IdP setup (Azure AD / Okta / Cognito) — the existing
  `--auth-jwks-uri` flag docs cover the surface.

Closes #313.
