# Deploy Intents Reference

Authoritative reference for the SHIPS deployment pipeline's preflight checks,
deploy intent matrix, and wave ordering rules.

---

## Preflight Checks

Preflight checks run **before any DDL is executed** against the target database.
ERROR-severity checks prevent deployment entirely. WARNING-severity checks are
reported but do not block execution.

| Check code            | Severity             | Description                                                                 | Gap      |
|-----------------------|----------------------|-----------------------------------------------------------------------------|----------|
| `ddl_parse`           | ERROR                | Every DDL file must parse and classify successfully.                        | Core     |
| `database_exists`     | ERROR                | All target databases must exist (unless created by this package).           | Core     |
| `access_rights`       | ERROR                | Deploy user must hold required CREATE/DROP rights on each target database.  | Core     |
| `perm_space`          | ERROR / WARNING      | Target databases must have sufficient free permanent space.                 | Core     |
| `jar_alias_coverage`  | ERROR                | Every Java procedure's JAR alias must be installed by a script in the package. | Core  |
| `package_hash`        | ERROR                | Release ZIP must match its SHA-256 sidecar before any DDL executes.         | GAP-001  |
| `env_lock`            | ERROR                | Package's `target_env` must match the `--env` flag supplied to Ship.        | GAP-002  |
| `change_ref_present`  | ERROR (conditional)  | Change ticket reference required when `require_change_ref: true` in ships.yaml. | GAP-004 |
| `package_signature`   | ERROR (conditional)  | HMAC-SHA256 signature verified when `.hmac` sidecar is present or `require_signature: true`. | GAP-005 |
| `mpa_approval`        | ERROR (conditional)  | 4-eyes approval code verified when `require_approvals: 2` in ships.yaml. | GAP-006 |
| `excess_privilege`    | WARNING              | Deploy user should not hold elevated rights (GD, SA, CA, AL, or DBC rights). | GAP-010 |
| `package_age`         | WARNING (configurable) | Package exceeds `package_max_age_days` threshold stamped from ships.yaml. | GAP-012 |

---

## Deploy Intent Matrix

| DDL Verb in Source   | Resolved Intent      | Behaviour                                                    |
|----------------------|----------------------|--------------------------------------------------------------|
| `CREATE TABLE`       | `IDEMPOTENT_DEPLOY`  | Drop-if-exists then create (data-safe schema comparison).    |
| `REPLACE VIEW`       | `REPLACE_IN_PLACE`   | Execute `REPLACE VIEW` directly.                             |
| `CREATE VIEW`        | `CREATE_ONLY`        | Fail if the object already exists.                           |
| `CREATE DATABASE`    | `DIRECT_EXECUTE`     | Execute as-is.                                               |
| `CREATE USER`        | `DIRECT_EXECUTE`     | Execute as-is.                                               |
| `REPLACE PROCEDURE`  | `REPLACE_IN_PLACE`   | Execute `REPLACE PROCEDURE` directly.                        |
| `REPLACE MACRO`      | `REPLACE_IN_PLACE`   | Execute `REPLACE MACRO` directly.                            |
| `GRANT` / `REVOKE`   | `DIRECT_EXECUTE`     | Execute as-is.                                               |
| Join / Hash Indexes  | `DROP_AND_CREATE`    | Drop unconditionally, then recreate.                         |
| Triggers             | `DROP_AND_CREATE`    | Drop unconditionally, then recreate.                         |
| System objects       | `SKIP_IF_EXISTS`     | No-op if already present.                                    |

---

## Wave Ordering

Objects are deployed in strict phase order:

1. **System** — Maps, Roles, Profiles, Authorisations, Foreign Servers (`SKIP_IF_EXISTS`).
2. **Pre-requisites** — `CREATE DATABASE`, `CREATE USER`.
3. **DCL** — Grants and revokes.
4. **DDL** — Tables → Views → Macros / Procedures / Functions → Triggers.
5. **DML** — Reference data loads, seed data, configuration inserts.
6. **Post-install** — Validation queries, statistics collection, smoke tests.

Within the DDL phase, the order is:
Tables → Join Indexes → Hash Indexes → Secondary Indexes → Views → Macros →
JAR installs → Procedures → Functions → Script Table Operators → Triggers.
