# Deploy Intents, Wave Ordering & Preflight Reference

---

## Deploy Intent Resolution

Intent is inferred from the DDL verb in the tokenised payload. The manifest records the resolved
intent for every object. Ship executes each object according to its manifest intent.

| DDL Verb in Payload       | Resolved Intent      | Ship Behaviour                                                           |
|---------------------------|----------------------|--------------------------------------------------------------------------|
| `CREATE TABLE`            | `IDEMPOTENT_DEPLOY`  | Check for existing data; drop-and-recreate if empty or --force set       |
| `REPLACE VIEW`            | `REPLACE_IN_PLACE`   | Execute REPLACE VIEW directly (no snapshot)                              |
| `CREATE VIEW`             | `CREATE_ONLY`        | Fail if the view already exists (by design; use REPLACE VIEW to update)  |
| `CREATE DATABASE`         | `DIRECT_EXECUTE`     | Execute as-is                                                            |
| `CREATE USER`             | `DIRECT_EXECUTE`     | Execute as-is                                                            |
| `REPLACE PROCEDURE`       | `REPLACE_IN_PLACE`   | Execute REPLACE PROCEDURE directly                                       |
| `REPLACE MACRO`           | `REPLACE_IN_PLACE`   | Execute REPLACE MACRO directly                                           |
| `GRANT` / `REVOKE`        | `DIRECT_EXECUTE`     | Execute as-is                                                            |
| `CREATE JOIN INDEX`       | `DROP_AND_CREATE`    | `DROP JOIN INDEX IF EXISTS`, then `CREATE JOIN INDEX`                    |
| `CREATE TRIGGER`          | `DROP_AND_CREATE`    | `DROP TRIGGER IF EXISTS`, then `CREATE TRIGGER`                          |
| Any `system`-scope object | `SKIP_IF_EXISTS`     | Query DBC to confirm existence; skip entirely if already present         |

### IDEMPOTENT_DEPLOY Detail

For tables, IDEMPOTENT_DEPLOY is NOT the same as DROP_AND_CREATE. Ship checks for row count > 0
before dropping. If the table contains data and `--force` is not set, Ship halts with
`TableNotEmpty` error. This protects against accidental data loss.

### Full DeployIntent Enum Values

| Value               | Description                                            |
|---------------------|--------------------------------------------------------|
| `CREATE_ONLY`       | New object; fail if it exists                          |
| `REPLACE_WITH_BACKUP` | Full backup/migrate — tables only (legacy path)      |
| `IDEMPOTENT_DEPLOY` | Backup/migrate approach; safe to rerun                 |
| `DROP_AND_CREATE`   | Unconditional drop then create (indexes, triggers)     |
| `DIRECT_EXECUTE`    | Execute as-is (DDL, DCL, prerequisites)                |
| `SKIP_IF_EXISTS`    | Skip silently if present (system-scope objects)        |
| `NOT_DEPLOYED`      | Carried for traceability; not executed                 |

---

## Wave Ordering

Ship executes objects in priority order derived from their object type. Within a group, objects
can run in parallel. A group does not begin until all objects in the preceding group have
completed successfully (or been skipped via SKIP_IF_EXISTS).

Wave order is controlled by a `_waves.txt` file in the project. The deployer also applies an
internal priority ranking per object type:

### Deployment Priority (lowest to highest execution order)

| Priority | Object Types                                      | Strategy          |
|----------|---------------------------------------------------|-------------------|
| −10      | Maps                                              | SKIP_IF_EXISTS    |
| −9       | Roles, Profiles                                   | SKIP_IF_EXISTS    |
| −8       | Authorisations                                    | SKIP_IF_EXISTS    |
| −7       | Foreign Servers                                   | SKIP_IF_EXISTS    |
| −3       | Databases                                         | DIRECT_EXECUTE    |
| −2       | Users                                             | DIRECT_EXECUTE    |
| −1       | Grants / Revokes (DCL)                            | DIRECT_EXECUTE    |
|  0       | Tables                                            | IDEMPOTENT_DEPLOY |
|  1       | Join / Hash / Secondary Indexes                   | DROP_AND_CREATE   |
|  2       | Views                                             | REPLACE_IN_PLACE  |
|  3       | Macros / Procedures / Functions / JARs            | REPLACE_IN_PLACE  |
|  5       | Triggers                                          | DROP_AND_CREATE   |
|  6       | DML (INSERT / UPDATE / DELETE)                    | DIRECT_EXECUTE    |
| 99       | Unknown                                           | —                 |

### Wave Ordering Rules

1. A database MUST be deployed before any object it owns.
2. A grant MUST reference only databases and roles already deployed.
3. A view MUST reference only tables or views deployed in an earlier group or earlier within
   the same group. Inspect detects view-to-view dependencies and orders them.
4. Join indexes MUST reference only tables — never views.
5. Triggers MUST reference their target table.

---

## Package Trust Score

Five discrete signals evaluated after each pipeline run. Produces a label, not a percentage.

### Trust Signals

| Signal                  | Status: fail when                                         |
|-------------------------|-----------------------------------------------------------|
| `inspect_token_format`  | INSPECT_TOKEN_MALFORMED error raised by inspect stage     |
| `inspect_lint`          | INSPECT_LINT_VIOLATION error raised by inspect stage      |
| `inspect_grants`        | Any INSPECT_GRANT_* error raised by inspect stage         |
| `provenance_complete`   | `_provenance.json` is absent from the payload             |
| `build_reproducible`    | `source_dirty` flag is set in BUILD.json                  |

### Trust Label

| Label               | Condition                                          |
|---------------------|----------------------------------------------------|
| `READY`             | All signals pass                                   |
| `READY-WITH-CAVEATS`| One or more signals warn; none fail                |
| `BLOCKED`           | Any signal has status fail                         |

A `BLOCKED` package must not be deployed. Resolve the failing signal and rebuild.

---

## Preflight Checks (Ship phase)

Ship runs preflight before executing any DDL. All ERROR-level preflight checks must pass before
execution begins. WARNING-level checks are reported but do not block execution.

### Actual Preflight Check Names

| Check name           | Severity | What it verifies                                                          |
|----------------------|----------|---------------------------------------------------------------------------|
| `ddl_parse`          | ERROR    | All DDL files parse cleanly and are classifiable by object type           |
| `database_exists`    | ERROR    | Every target database exists on the Teradata system                       |
| `ct_right`           | ERROR    | User has CREATE TABLE right on each target database                       |
| `dt_right`           | ERROR    | User has DROP TABLE right on each target database                         |
| `r_right`            | ERROR    | User has SELECT right on each target database                             |
| `ix_right`           | ERROR    | User has CREATE INDEX / DROP INDEX right (index deployments only)         |
| `cv_right`           | ERROR    | User has CREATE VIEW right (view deployments only)                        |
| `cm_right`           | ERROR    | User has CREATE MACRO right (macro deployments only)                      |
| `cp_right`           | ERROR    | User has CREATE PROCEDURE right (procedure deployments only)              |
| `cf_right`           | ERROR    | User has CREATE FUNCTION right (function deployments only)                |
| `perm_space`         | WARNING  | Target databases have ≥ 10% free PERM space                               |
| `jar_alias_coverage` | ERROR    | Every `PROCEDURE LANGUAGE JAVA` references an installed JAR alias         |

If any ERROR-level preflight check fails, Ship exits non-zero and no DDL is executed.

---

## Connection Configuration

All Teradata connections in `database_package_deployer` MUST use `tmode=TERA`. ANSI mode changes
transaction semantics and NULL handling in ways that are incompatible with the deployed DDL.

Connection parameters are resolved in this order:
1. CLI flags (`--host`, `--user`, `--password`, `--logmech`)
2. Environment variables (`TD_HOST`, `TD_USER`, `TD_PASSWORD`, `TD_LOGMECH`)

Passwords MUST NOT appear in env config files as plaintext (`PROPS_NO_PLAINTEXT_CRED` rule).

---

## Rollback and Resume Behaviour

Ship captures a per-object snapshot before executing each object's DDL. On failure:

- Objects in the current group that have not yet executed are skipped.
- The failed object's error is recorded in the deploy manifest (`.deploy_manifest.json`).

**Resume from a failed deployment** (continues from where it stopped):
```bash
python -m database_package_deployer resume <path/to/.deploy_manifest.json>
```

**Roll back deployed objects** (replays pre-execution snapshots):
```bash
python -m database_package_deployer rollback <path/to/.deploy_manifest.json>

# Roll back a specific wave only
python -m database_package_deployer rollback <manifest> --wave 3
```

**Check per-object status:**
```bash
python -m database_package_deployer status <path/to/.deploy_manifest.json>
```

Notes:
- `IDEMPOTENT_DEPLOY` tables that were empty and dropped are not automatically restored —
  manual data recovery is required if the table was dropped before the failure.
- Rollback does not execute automatically — it is initiated by the operator after reviewing
  the deploy manifest.
