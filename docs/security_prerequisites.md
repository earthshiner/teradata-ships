# Security Deployment Prerequisites

This document covers the network controls, encryption requirements, and access
controls that operators must satisfy before deploying SHIPS release packages to
production environments.

---

## Required Network Controls

### Production Ship (recommended configuration)

- **VPN or bastion host:** Production Ship commands must originate from a host
  within the approved network segment.  A direct internet-to-Teradata path
  should not be used for production deployments.
- **Bastion host access:** If a bastion host is used, the SHIPS_SIGNING_KEY and
  any Vault tokens must be injected into the deployment session via the CI/CD
  platform's secrets management — never stored in plaintext on the bastion.

### Non-production environments

- VPN is recommended but not mandated.
- Non-production environments should still be isolated from direct internet
  access to prevent accidental exposure of DDL payloads.

---

## TLS Requirement

All Teradata connections made by Ship must use TLS/SSL encryption.

**teradatasql connection parameter:**

```python
teradatasql.connect(host="myhost", user="ships_user", encryptdata="true")
```

Or via the CLI:

```bash
ships deploy /path/to/package/ --host myhost --user ships_user --encryptdata true
```

**Enforce via ships.yaml:**

```yaml
environments:
  PRD:
    require_tls: true   # Ship exits non-zero if no TLS params detected
```

When `require_tls: true` is set, the `tls_connection` preflight check is
promoted from WARNING to ERROR.

---

## SHIPS_SIGNING_KEY Access Controls

The SHIPS_SIGNING_KEY shared secret is used for:

- **Package signing** (`--signing-key` on Package, or `SHIPS_SIGNING_KEY` env var)
- **MPA approval code generation** (`ships approve <package_zip>`)
- **MPA approval code verification** (at Ship time)

### Recommended OS-level access controls

1. Store the key in a file with permissions `0400` (read-only by owner):
   ```bash
   chmod 0400 /etc/ships/signing.key
   chown ships_deploy /etc/ships/signing.key
   ```
2. Prefer injecting the key via the CI/CD platform's secrets management
   (GitLab CI variables, GitHub Actions secrets, HashiCorp Vault).
3. Never commit the key to source control.
4. Rotate the key when any team member with access leaves.
5. Use a Vault reference in your CI configuration to avoid the key ever
   touching the filesystem:
   ```yaml
   SHIPS_SIGNING_KEY: ${{ secrets.SHIPS_SIGNING_KEY }}
   ```

---

## Vault Integration (GAP-011)

Token map values can reference HashiCorp Vault secrets using the `vault:` prefix:

```ini
DB_PASSWORD = vault:secret/data/ships/prd#password
```

Required environment variables on the Harvest host:

| Variable      | Description                              |
|---------------|------------------------------------------|
| `VAULT_ADDR`  | Vault server URL (e.g. `https://vault.example.com`) |
| `VAULT_TOKEN` | Token with read access to the secret path |

SHIPS uses the Vault KV v2 API.  The `hvac` Python package is used when
installed; otherwise `urllib.request` provides a fallback.

---

## Summary Checklist

Before deploying to production, confirm:

- [ ] Network path goes through VPN or bastion host
- [ ] Teradata connection uses TLS/SSL (`encryptdata=true` or `sslmode=require`)
- [ ] `SHIPS_SIGNING_KEY` is injected via secrets management (not in plaintext)
- [ ] Signing key file (if used) has `0400` permissions
- [ ] Change reference (`--change-ref CHG…`) is present when `require_change_ref: true`
- [ ] 4-eyes approval code is available when `require_approvals: 2`
- [ ] Package is within its `package_max_age_days` threshold
