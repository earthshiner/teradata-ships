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

## Asymmetric Package Signing (Ed25519)

Asymmetric signing binds each package to a private key that lives only in the CI/CD
platform. A DBA or attacker with full access to the extracted package cannot forge
a valid signature — they do not have the private key.

### Infrastructure required

No certificate authority, HSM, or PKI is needed. The only requirement is a key pair.

```bash
ships keygen
```

This writes two files:
- `ships_signing_private.pem` — the Ed25519 private key
- `ships_signing_public.pem` — the corresponding public key

### Private key: CI/CD secret

Store the private key as a secret in your CI/CD platform. Never commit it to source
control.

| Platform | Variable name |
|---|---|
| GitHub Actions | `SHIPS_PRIVATE_KEY_PATH` (path to temp file) or `SHIPS_ASYMMETRIC_KEY` (inline PEM) |
| GitLab CI | `SHIPS_ASYMMETRIC_KEY` (masked variable) |
| HashiCorp Vault | Inject at runtime into `SHIPS_ASYMMETRIC_KEY` |

Use it at package time:

```bash
ships package \
    --source /projects/OMR \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --asymmetric-key /run/secrets/ships_private.pem
```

Or via the environment variable:

```bash
export SHIPS_ASYMMETRIC_KEY="$(cat /run/secrets/ships_private.pem)"
ships package ...
```

### Public key: committed to the project repository

`ships_signing_public.pem` is a public key — it is safe to commit and share.

```bash
git add ships_signing_public.pem
git commit -m "chore: add SHIPS Ed25519 public key"
```

Configure it in `ships.yaml` so every deploy automatically verifies the signature:

```yaml
signing:
  public_key: |
    -----BEGIN PUBLIC KEY-----
    MCowBQYDK2VdAyEA...
    -----END PUBLIC KEY-----
```

Or reference the file:

```yaml
signing:
  public_key_file: ships_signing_public.pem
```

### Key rotation

1. Run `ships keygen` again to generate a new pair
2. Update the CI/CD platform secret with the new private key
3. Commit the new `ships_signing_public.pem` in the same PR as the secret rotation
4. Packages signed with the old key will fail verification after the public key is updated — ensure all in-flight packages are deployed before rotating

### lib/ integrity

`context/ships.integrity.json` now covers both `payload/` and `lib/` — the embedded deployer
code is hashed alongside the DDL payload. Any modification to the deployer files
changes the package hash and aborts deployment before any database connection is made.
This closes the attack vector of editing `lib/` to bypass security checks; combined
with Ed25519 asymmetric signing it also prevents the forged-hash attack described in
ADR 0011.

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
- [ ] Ed25519 private key is stored in CI/CD secrets (`SHIPS_PRIVATE_KEY_PATH` or `SHIPS_ASYMMETRIC_KEY`) — never on disk or in source control
- [ ] `ships_signing_public.pem` is committed to the project repository and referenced in `ships.yaml`
