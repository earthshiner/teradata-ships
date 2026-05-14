# SHIPS Operations Guide
### For DBAs and Deployment Agents

---

## Your role in SHIPS

As the operator, you receive a self-contained package `.zip` from the development team and deploy it to Teradata. You do not need to understand the source DDL, the tokenisation model, or how the package was built. Everything you need is inside the package.

**What the package contains:**

```
OMR_DEV_BUILD_0005_20260509.zip
    deploy.py                   ← The only script you run
    context/ships.build.json                  ← Build manifest (provenance, file list, integrity hash)
    context/ships.context.json          ← Durable workflow context for agents, CI/CD, and MCP tools
    context/ships.manifest.json         ← Compact agent-safe package inventory and dependency contract
    context/ships.handoff.json          ← Next-actor instructions (human, CI/CD job, or deployment agent)
    context/ships.integrity.json      ← SHA-256 file hashes for tamper detection
    _waves.txt                  ← Deployment wave ordering
    lib/
        database_package_deployer/  ← Deployment engine (no separate install needed)
    payload/database/
        pre-requisites/databases/   ← Phase 01: CREATE DATABASE / USER
        DCL/inter_db/               ← Phase 02: GRANT / REVOKE
        DDL/tables/                 ← Phase 03: tables, views, procedures, etc.
        DML/                        ← Phase 04: seed data
    _rollback/                  ← Pre-deployment SHOW captures (populated during deploy)
    logs/                       ← Report and manifest written here after deployment
```

**What you run:**

```bash
python deploy.py --host myserver --user dba_user [options]
```

The engine, dependencies, and DDL are all bundled. No package manager required.

---

## Installation

### Python

SHIPS requires Python 3.13+. Check your version:

```bash
python --version
```

If Python is not installed or is older than 3.13, download it from [python.org](https://www.python.org/downloads/). On enterprise systems, ask your platform team for the standard Python 3.13 deployment.

### teradatasql driver

The `teradatasql` driver is the only external dependency. It is NOT bundled in the package (licensing restriction). Install it once per machine:

```bash
pip install teradatasql
```

Or with uv:

```bash
uv pip install teradatasql
```

Verify the driver works:

```bash
python -c "import teradatasql; print('OK')"
```

If installation fails due to network restrictions, obtain the `teradatasql` wheel from your organisation's internal package mirror and install it with:

```bash
pip install teradatasql --no-index --find-links /path/to/wheels
```

### Nothing else required

The deployment engine (`database_package_deployer`) is embedded in the package's `lib/` directory. Python finds it automatically. No `PYTHONPATH` changes, no virtual environment needed.

---

## Preflight checks

Every deployment runs a set of preflight checks before any database connection is
opened. The following checks are performed automatically:

| Check | Default severity | What triggers it |
|---|---|---|
| `package_hash` | ERROR | Archive `.sha256` sidecar mismatch |
| `env_lock` | ERROR | PRD package targeting a non-PRD environment (or vice versa) |
| `secret_scan` | ERROR (configurable) | Embedded credentials in DDL/DML bodies |
| `change_ref_present` | ERROR (when `require_change_ref: true`) | No change ticket on a PRD package |
| `hmac_signature` | ERROR (when key configured) | HMAC signature absent or invalid |
| `asym_signature` | ERROR (when public key configured) | Ed25519 signature absent or invalid |
| `mpa_approval` | ERROR (when `require_approvals: 2`) | No 4-eyes approval code |
| `audit_sink` | WARNING | No `audit_sink` configured in `ships.yaml` |
| `dynamic_sql` | WARNING (configurable) | `EXECUTE IMMEDIATE` in procedures |
| `sensitivity_class` | WARNING (configurable) | No `.cls` companion for PII/PCI objects |
| `excess_privilege` | WARNING | Deploy account has over-broad privileges |
| `package_age` | WARNING | Package older than `package_max_age_days` |
| `rollback_integrity` | ERROR | Rollback snapshot SHA-256 mismatch |
| `grant_drift` | WARNING | Undeclared or missing grants detected |
| `tls_connection` | WARNING (ERROR when `require_tls: true`) | Connection lacks TLS/SSL |

Severity thresholds are configurable in `config/inspect.conf` (for package-build-time
checks) and in `ships.yaml` (for deploy-time checks). ERROR-severity failures abort
the deployment before any DDL is executed.

---

## Before you deploy: the pre-deployment checklist

Run through this before every live deployment. It takes two minutes and prevents most deployment failures.

### 1. Verify package integrity

```bash
python deploy.py integrity-check
```

This recomputes the SHA-256 hash of every file in the payload and compares it to the values in `context/ships.integrity.json`. If any file has been modified since the package was built, this command exits with code 1 and lists the affected files.

**If integrity fails:** do not deploy. Contact the developer to rebuild the package. A tampered package indicates either a transmission error or a deliberate modification — both require investigation before proceeding.

### 2. Read the Package Trust Report

Every SHIPS package carries a Trust Report in `context/ships.build.json`, stamped at build time. The `deploy.py` script reads it and prints the label before connecting to the database:

```
================================================================
  Package Trust: ✓ READY
================================================================
  ✓ inspect_token_format     No malformed token markers found
  ✓ inspect_lint             No lint violations found
  ✓ inspect_grants           Grant validation clean
  ✓ provenance_complete      context/ships.provenance.json present
================================================================
```

| Label | Action |
|---|---|
| **READY** ✓ | Proceed to deployment |
| **READY-WITH-CAVEATS** ⚠ | Proceed but investigate the warnings |
| **BLOCKED** ✗ | Do not deploy — fix the failing signals first |

If the label is **BLOCKED**, `deploy.py` will exit before making a database connection unless you pass `--skip-trust-check` (development override only).

### 4. Check what the SHIPS tool says

If you have access to the SHIPS project directory on the build machine, run:

```bash
python -m td_release_packager verify --project /path/to/project
```

This confirms the package was built cleanly with no warnings. Exit 0 = READY. Exit 1 = something to investigate.

### 5. Dry run

Always run a dry run before the first live deployment:

```bash
python deploy.py --dry-run --host myserver --user dba_user
```

The dry run connects to the database, runs all pre-flight checks (permissions, space, object existence), and validates the wave ordering — but executes no DDL. The HTML report at `logs/` shows exactly what would happen.

### 6. Review the pre-flight report

After the dry run, open `logs/.deploy_report_<id>.html`. Check:

- **Pre-flight section:** any permission failures or space warnings
- **Object Results:** any SKIPPED objects and why
- **Wave Execution:** wave count and estimated parallelism

Address any pre-flight errors before proceeding to live deployment.

### 7. Confirm the companion archive is deployed first (if applicable)

Some packages are auto-split into a `_prereqs_` companion archive and a main archive. If `context/ships.build.json` shows `"role": "main"` and `"requires": ["OMR_prereqs_DEV_BUILD_0005_...zip"]`, deploy the prereqs archive first:

```bash
# Extract and deploy prereqs first
cd OMR_prereqs_DEV_BUILD_0005_20260509/
python deploy.py --host myserver --user dba_user

# Then deploy the main archive
cd ../OMR_DEV_BUILD_0005_20260509/
python deploy.py --host myserver --user dba_user
```

The deploy banner prints the explicit deploy order when a companion exists.

---

## Running a deployment

### Standard deployment

```bash
python deploy.py --host myserver --user dba_user
```

Password is prompted interactively. To pass it non-interactively (for CI/scripted deployments):

```bash
python deploy.py --host myserver --user dba_user --password "$TD_PASS"
```

### Authentication options

```bash
# Teradata 2 (default)
python deploy.py --host myserver --user dba_user --logmech TD2

# LDAP
python deploy.py --host myserver --user dba_user --logmech LDAP

# TD Wallet
python deploy.py --host myserver --user dba_user --logmech TDNEGO
```

### Wave-parallel deployment

By default the deployer uses 1 stream (serial). For large packages, use multiple streams to deploy independent waves in parallel:

```bash
python deploy.py --host myserver --user dba_user --streams 4
```

Use a stream count between 2 and 8. Higher is not always faster — Teradata lock contention increases with streams. For packages under 50 objects, 1–2 streams is typically optimal. For large packages (200+ objects), 4–8 streams can significantly reduce total deployment time.

**What wave-parallel means:** the analyser assigned each DDL object to a wave based on dependencies. Objects in wave 1 have no dependencies; wave 2 depends on wave 1; and so on. Within each wave, objects are deployed in parallel across the configured streams. The overall ordering (wave 1 before wave 2) is always respected.

### Continue-on-error mode

By default, the deployer stops on the first failure. To attempt all objects and collect a full failure list:

```bash
python deploy.py --host myserver --user dba_user --continue-on-error
```

Use this when you want to see everything that fails in one pass rather than iterating. The manifest records each object's final state, so you can resume or re-deploy after fixing the root causes.

### Checking deployment status (no database connection needed)

At any point during or after a deployment, inspect the manifest:

```bash
python deploy.py status logs/.deploy_manifest_<id>.json
```

This reads the manifest file without connecting to Teradata. Useful for checking status on a deployment machine you cannot connect from.

---

## Reading the deployment report

After every deployment (including dry runs), SHIPS writes an HTML report to `logs/`. Open it in any browser.

### Report sections

**Action Items (top)** — FAILED and SKIPPED objects requiring attention. Both groups start collapsed; click to expand. Address FAILED items before re-deploying.

**Deployment Summary** — aggregate counts: total, completed, skipped, failed, rolled back. In REPLAY mode (re-running an already-deployed package), shows "Verified (prior)" count.

**Pre-flight Results** — permission and space checks. ERRORs here explain most deployment failures.

**Wave Execution** — per-wave breakdown: objects, completed, failed, skipped, duration. Available for wave-parallel deployments.

**Wave Graph tab** — visual SVG showing which objects are in which wave and their status. Useful for understanding the dependency structure at a glance.

**Object Results** — per-object table with status badges. For FAILED and SKIPPED objects, shows the source file path and (if provenance is available) a link to the original project source.

### Status badges

| Badge | Meaning |
|---|---|
| **Completed** | DDL executed successfully |
| **Skipped** | Object was not processed — see reason in details |
| **Failed** | DDL execution failed — error message in details |
| **Backed up** | Table data preserved in a backup table before modification |
| **Migrated** | Table data migrated from backup to new structure |
| **Rolled back** | Object restored to pre-deployment state |

### Machine-readable report (for agents)

The HTML report has a companion JSON manifest at `logs/.deploy_manifest_<id>.json`. Agents should read this rather than parsing HTML:

```python
import json, pathlib, glob

manifest_files = sorted(
    pathlib.Path("logs").glob(".deploy_manifest_*.json")
)
manifest = json.loads(manifest_files[-1].read_text())

failed = [
    name for name, rec in manifest["objects"].items()
    if rec["state"] == "FAILED"
]
```

---

## Observability: tracing and data catalog integration

SHIPS integrates with two complementary observability standards. Both are off by default — set an environment variable to enable.

**OpenTelemetry tracing** — emits spans for each pipeline stage, including `deploy_package`. Connect to Jaeger, Grafana Tempo, Datadog, or any OTLP-compatible backend:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://my-collector:4318
```

**OpenLineage** — emits `START`, `COMPLETE`, and `FAIL` events from `deploy_package` carrying the deployed objects as output datasets. Connect to Marquez, DataHub, Apache Atlas, or OpenMetadata:

```bash
# Live push to a catalog backend
export OPENLINEAGE_URL=http://marquez:5000
export OPENLINEAGE_NAMESPACE=teradata://td-prod.myorg.com:1025

# Or write NDJSON to a file for later ingestion
export OPENLINEAGE_URL=file:///var/log/ships/lineage.ndjson
```

A catalog outage never blocks deployment — transport errors are swallowed silently.

See **`docs/OBSERVABILITY.md`** for full setup instructions, backend examples, event schema, and FAQ.

---

## Audit trails

Every SQL statement executed by SHIPS carries a query band embedded in the Teradata session. This creates a permanent, unforgeable link between every DDL statement in DBQL and the exact package that executed it.

### Query band format

```
BUILD=0005;PKG=OMR;ENV=DEV;PKG_HASH=a3f8c2d1e9b74056;DEPLOYER=database_package_deployer_v2;
```

| Field | Value | Purpose |
|---|---|---|
| `BUILD` | Build number | Ties the execution to a specific package build |
| `PKG` | Package name | Identifies the project |
| `ENV` | Environment | DEV / TST / PRD |
| `PKG_HASH` | SHA-256 prefix | Identifies the exact package binary |
| `DEPLOYER` | `database_package_deployer_v2` | Identifies the SHIPS engine version |

### Querying the audit trail in DBQL

Teradata provides `GetQueryBandValue(queryband, session_no, 'key')` for parsing query bands. Use it instead of `LIKE` — it is correct, efficient, and does not produce false positives from partial string matches.

**Important:** `QueryBand` is a column of `DBC.DBQLogTbl`, not `DBC.DBQLSqlTbl`. When you need the SQL text as well, join `DBC.DBQLogTbl` (query band and session metadata) to `DBC.DBQLSqlTbl` (SQL text rows) on `QueryID`.

**Find all deployments by a specific build (metadata only):**

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE) AS LogDate,
    t1.CollectTimeStamp,
    t1.UserName,
    GetQueryBandValue(t1.QueryBand, 0, 'PKG')   AS Package,
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')   AS Environment
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '0005'
  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = 'OMR'
ORDER BY t1.CollectTimeStamp;
```

**Find all deployments by a specific build including the SQL text:**

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE) AS LogDate,
    t1.CollectTimeStamp,
    t1.UserName,
    t2.SqlTextInfo AS QueryText
FROM DBC.DBQLogTbl t1
JOIN DBC.DBQLSqlTbl t2
  ON t1.QueryID           = t2.QueryID
 AND t1.CollectTimeStamp  = t2.CollectTimeStamp
WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '0005'
  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = 'OMR'
ORDER BY t1.CollectTimeStamp;
```

**Find all deployments to PRD in a date range:**

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD')   AS DeployDate,
    GetQueryBandValue(t1.QueryBand, 0, 'PKG')      AS Package,
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD')    AS Build,
    t1.UserName                                     AS DeployUser,
    COUNT(*)                                        AS StatementCount
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'DEPLOYER') = 'database_package_deployer_v2'
  AND GetQueryBandValue(t1.QueryBand, 0, 'ENV')       = 'PRD'
  AND CAST(t1.CollectTimeStamp AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-12-31'
GROUP BY DeployDate, Package, Build, DeployUser
ORDER BY DeployDate, Package;
```

**Confirm a specific package hash was deployed:**

```sql
SELECT COUNT(*) AS Executions
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'PKG_HASH') = 'a3f8c2d1e9b74056';
```

### Reading query band fields

`GetQueryBandValue` returns the value for a named key — far cleaner than string functions:

```sql
SELECT
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD')    AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'PKG')      AS Package,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')      AS Environment,
    GetQueryBandValue(t1.QueryBand, 0, 'PKG_HASH') AS PackageHash
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'DEPLOYER') = 'database_package_deployer_v2'
SAMPLE 10;
```

---

## Schema drift

Schema drift means a Teradata object was changed out-of-band — not by SHIPS — since the last SHIPS deployment. SHIPS detects this by comparing the current `SHOW` output from the live database against a baseline it captured immediately after it last deployed that object. Because both sides are in Teradata's canonical format, the comparison is free of false positives.

### When drift is detected

The deployment report shows a unified diff for every drifted object:

```
⚠ DRIFT ABORT: Schema drift detected on OMR_STD.Customer
  Object was changed out-of-band since last SHIPS deploy.

  --- OMR_STD.Customer (last SHIPS deploy)
  +++ OMR_STD.Customer (current database)
  @@ -3,6 +3,7 @@
       id INTEGER NOT NULL
      ,name VARCHAR(100)
  +   ,region VARCHAR(50)
   )
```

**Step 1 — Understand what changed.** The diff tells you exactly which columns, constraints, or attributes differ. Before deciding anything, you need to know *why*.

**Step 2 — Decide who wins.**

| Situation | Recommended action |
|---|---|
| **Emergency hotfix** (DBA added a column or index to fix a live incident) | `--on-drift skip` — deploy everything else, leave the hotfix in place. Raise a ticket to absorb the hotfix into source DDL for the next release. |
| **Unauthorised change** (schema altered without going through SHIPS — governance violation) | `--on-drift abort` (default) is correct — surface it and force a conscious decision. Once reviewed: if SHIPS is right, re-run with `--on-drift continue` to overwrite; if the change is needed, treat it as a hotfix. |
| **Error / stale state** (manual partial rollback, DBA applied a wrong fix) | `--on-drift continue` — overwrite with what SHIPS knows is correct, reset the baseline. |
| **Rollback to a previous version** | `ships rollback` defaults to `--on-drift continue` — the entire point of rollback is to restore a known-good state; out-of-band changes made after the broken deploy are part of the problem, not something to preserve. |

**Step 3 — Re-run with the chosen mode.**

```bash
# Overwrite the out-of-band change — SHIPS wins
python deploy.py --host myserver --user dba --on-drift continue

# Skip drifted objects — out-of-band change preserved
python deploy.py --host myserver --user dba --on-drift skip

# Stop on first drifted object (default — forces investigation)
python deploy.py --host myserver --user dba --on-drift abort
```

**Step 4 — After deploy completes,** the baseline is updated automatically for every successfully deployed object. No manual step needed.

### Configuring drift detection

Drift detection requires a shared filesystem path that all operators write to. Configure it once in `ships.yaml` — it travels in every package automatically:

```yaml
# ships.yaml — committed to source control
deployment:
  baseline_dir: /shared/nfs/ships-baselines/OMR/
```

Without this, drift detection is disabled and a warning is printed. To override for a single run:

```bash
python deploy.py --host myserver --user dba --baseline-dir /alt/path/
```

### Drift and the audit trail

The baseline files are the record of *what SHIPS last deployed* for each object. They are not a history — each file holds only the most recent deploy's SHOW output (rolling horizon). The full history of what deployed when is in `context/ships.build.json` (per-package) and DBQL (per-statement). The drift diff in the deployment report is the record of *what changed between SHIPS runs* — include it in incident reports when a hotfix is discovered.

---

## Feature rollback

Feature rollback restores the database to a previous known-good version by re-deploying from a git tag. This is distinct from technical rollback (which undoes a failed mid-deploy via the pre-captured SHOW snapshots in `_rollback/`).

```bash
python -m td_release_packager rollback \
    --to-tag v1.2.3 \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --project C:\Projects\OMR
```

SHIPS will:
1. Verify the tag exists in git
2. Extract the tagged source tree
3. Build a rollback package from that source with the current environment config
4. Write the package to `releases/` (or `--output`)
5. Print the exact deploy command to run next

The rollback package is a normal SHIPS package — it goes through the same integrity check, Trust Report, and pre-flight validation as any other package. The DBA reviews it and deploys it with:

```bash
python deploy.py --host myserver --user dba --on-drift continue
```

**Why `--on-drift continue` for rollback?**

Any out-of-band changes made between the broken deploy and the rollback attempt may be part of the problem. The rollback's purpose is to restore v1.2.3 as the authoritative schema — deferring to those changes defeats that purpose. After the rollback completes, the drift baseline is updated to reflect the restored state, so future deployments detect drift correctly from the v1.2.3 baseline.

**What if you want to preserve a hotfix during rollback?**

Use `--on-drift skip` instead of `--on-drift continue`. SHIPS will roll back all other objects and leave the hotfixed object untouched. Include the hotfix in the next release cycle.

### Rollback and the build counter

Rollback packages get a new build number (auto-incremented from the current `.build_counter`). The `source_commit` in `context/ships.build.json` records the tag's commit hash, so the audit trail clearly shows "build 0048 was a rollback to tag v1.2.3 / commit abc1234".

---

## 4-Eyes approval workflow

When `require_approvals: 2` is set in `ships.yaml` for an environment, a second
operator must approve the package before deployment.

**Step 1 — First operator: generate the approval code**

```bash
ships approve /path/to/package.zip --signing-key /etc/ships/signing.key
```

The command prints an approval code. Communicate this to the deploying DBA through
your change management system (not verbally or in plain email).

**Step 2 — Second operator: deploy with the approval code**

```bash
python deploy.py \
    --host myserver \
    --user ships_dba \
    --approval-code CODE_FROM_STEP_1
```

The preflight check verifies the code before opening a database connection. If the
code is absent or invalid, the deployment is blocked.

---

## Grant drift check (`ships audit-grants`)

Use `ships audit-grants` to compare the GRANT statements declared in a package's DCL
files against the live grant state in Teradata. Run it after deployment as a
compliance check, or before deployment to pre-validate.

```bash
ships audit-grants /path/to/package_dir \
    --host myhost \
    --user ships_dba
```

Output:

```
MATCHED      (12) — grants declared and confirmed live
MISSING       (2) — in DCL but not in Teradata — run the missing GRANTs manually
UNDECLARED    (1) — live in Teradata but not declared — investigate and remove or absorb
```

Exit 0 = no drift. Exit 1 = drift detected. Integrate this into the post-deployment
runbook for production environments.

---

## Audit log

Configure `ships.yaml` to write a structured JSON audit event at the end of every
Ship:

```yaml
audit_sink: file:///var/log/ships/audit.jsonl
```

The audit log records the package name, build number, environment, operator, preflight
outcomes, object counts, and package hash. One JSON object per line (NDJSON format).

For syslog forwarding or SIEM integration, configure a remote sink:

```yaml
audit_sink: syslog://loghost:514
```

If no `audit_sink` is configured, a WARNING preflight check fires — deployments still
proceed, but the audit trail gap is flagged.

---

## TLS enforcement

All connections to Teradata should use TLS/SSL encryption. Pass the flag on the deploy
command:

```bash
python deploy.py --host myserver --user ships_dba --encryptdata true
```

Or set `sslmode`:

```bash
python deploy.py --host myserver --user ships_dba --sslmode require
```

To enforce TLS for a specific environment and have the `tls_connection` preflight check
block the deployment if it is absent, add this to `ships.yaml`:

```yaml
environments:
  PRD:
    require_tls: true
```

---

## Deploying from GitHub Releases (`--from-github`)

Once CI publishes the package as a GitHub Release, DBAs can deploy without a file
transfer. SHIPS downloads the ZIP and all available sidecar files (`.sha256`, `.hmac`,
`.sig`) from the release, verifies them, and proceeds with normal deployment.

```bash
python deploy.py \
    --from-github org/repo \
    --release-tag v1.2.3 \
    --asset PRD_Pkg_BUILD_0001.zip \
    --host myserver \
    --user ships_dba
```

**Sidecar verification:** SHIPS automatically downloads and verifies any `.sha256`,
`.hmac`, or `.sig` files published alongside the named asset. All verification steps
run before a database connection is opened.

**Private repositories:** set `GITHUB_TOKEN` in the environment:

```bash
export GITHUB_TOKEN=ghp_...
python deploy.py --from-github org/repo ...
```

**GitHub Enterprise Server:**

```bash
export SHIPS_GITHUB_API_URL=https://github.mycompany.com/api/v3
python deploy.py --from-github org/repo ...
```

This workflow eliminates the file-transfer step from the runbook and ensures the
package deployed is byte-for-byte what CI published — no manual copying, no
accidental version mismatch.

---

## When deployments fail

### Understanding the failure modes

Open the HTML report. Every FAILED object has an error message. Match the error to the appropriate section below.

---

### Space issues

**Symptoms:**
- `Error 2646: No more room in database <dbname>`
- `Error 2644: No more spool space`

**Fix:**

1. Identify which database is out of space:
   ```sql
   SELECT DatabaseName, MaxPerm, CurrentPerm, MaxPerm - CurrentPerm AS FreeSpace
   FROM DBC.DiskSpaceV
   WHERE DatabaseName IN ('<dbname>')
   ORDER BY FreeSpace;
   ```

2. Allocate more space:
   ```sql
   MODIFY DATABASE <dbname> AS PERMANENT = <new_size_bytes>;
   ```

3. Re-run the deployment — the manifest records which objects were already completed, so only the failed objects are re-attempted.

**Re-running after fixing space:**

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json --host myserver --user dba_user
```

`resume` picks up exactly where the deployment left off, skipping all already-completed objects.

---

### Permission errors

**Symptoms:**
- `Error 3523: User does not have <privilege> privilege on <object>`
- `Error 3706: Syntax error — the user does not own this object`

**Fix:**

1. Identify the missing privilege from the error message
2. Grant it to the deployment user:
   ```sql
   GRANT CREATE TABLE ON <database> TO <deploy_user>;
   GRANT DROP TABLE ON <database> TO <deploy_user>;
   ```

3. Common grants needed by the deployer:

   | Operation | Required privilege |
   |---|---|
   | Create tables | `CREATE TABLE ON <db>` |
   | Replace views | `CREATE VIEW ON <db>` |
   | Replace procedures | `CREATE PROCEDURE ON <db>` |
   | Replace macros | `CREATE MACRO ON <db>` |
   | Drop and create | `DROP TABLE / DROP VIEW` on `<db>` |
   | Execute grants | `GRANT OPTION` if granting on behalf |
   | Create databases | `CREATE DATABASE ON DBC` (or parent) |

4. Re-run using `resume`:
   ```bash
   python deploy.py resume logs/.deploy_manifest_<id>.json --host myserver --user dba_user
   ```

---

### Object already exists (unexpected)

**Symptoms:**
- `Error 3807: Object '<name>' does not exist` (for objects expected to already be there)
- `Error 3803: Table '<name>' already exists` (for CREATE TABLE on an existing table)

**Context:** SHIPS uses the DDL verb to determine deployment strategy. `CREATE TABLE` means "create it new — fail if it exists." `REPLACE VIEW` means "create or replace." If you see 3803 on a `CREATE TABLE`, the table already exists and the developer used `CREATE` where they should have used the backup-and-replace strategy.

**Fix for unexpected existing object:**
1. If the object should be replaced: contact the developer to change the DDL verb or the deploy intent configuration
2. If the object should be dropped first manually: `DROP TABLE <db>.<table>;` then re-run with `resume`
3. If the object is correct and the deploy is a no-op: SHIPS will skip it on the next run if configured correctly

---

### Stored procedure compile error

**Symptoms:**
- `Error 3706: Syntax error in stored procedure`
- `Error 5589: SPL procedure <name> was not compiled successfully`

**Fix:**

1. Examine the compile error in the deployment report — it includes the Teradata error message with line number
2. Open the package's DDL file: `payload/database/DDL/procedures/<db>.<proc>.spl`
3. Fix the syntax, re-harvest, and rebuild the package
4. Deploy the new package

There is no "patch and re-run" for procedure compile errors — the fix must come from the source. Resume will not help because the compiled procedure definition cannot be applied without a correct source.

---

### Lock or deadlock errors

**Symptoms:**
- `Error 3598: Concurrent change conflict on database — try again`
- `Error 2631: Transaction ABORTed due to deadlock`

The deployer automatically retries these errors (3598: up to 3 times with 0.5 / 1 / 2s backoff; 2631: up to 3 times with 2 / 4 / 8s backoff). If they appear in the report, the retries were exhausted.

**Fix:**

1. Identify blocking sessions via the lock log (if DBQL lock logging is enabled):
   ```sql
   SELECT CollectTimeStamp, UserName, LockType, LockStatus,
          ObjectDatabaseName, ObjectTableName
   FROM DBC.LockLogShredV
   WHERE LockStatus IN ('L','W')   -- L = locked, W = waiting
   ORDER BY CollectTimeStamp DESC;
   ```
   If `DBC.LockLogShredV` is not available, use Teradata Viewpoint's Session
   Viewer or the `SHOW LOCKS ON DATABASE <dbname>` command from a BTEQ session.

2. Wait for competing sessions to complete, or terminate them if appropriate:
   ```sql
   ABORT 'session <sessionno>';
   ```
3. Re-run using `resume`

---

### Package integrity check failure

**Symptoms:**
- `CRITICAL: Package integrity check FAILED`
- `Files modified since packaging: <filename>`

**Do not deploy.** The package payload does not match what was built and signed.

1. Compare the SHA-256 hashes in `context/ships.integrity.json` against the current files to identify which files changed
2. Obtain a fresh copy of the package from the build system
3. Verify integrity on the fresh copy before deploying

If this error occurs during extraction from a shared drive or email transfer, it likely indicates file corruption in transit. Request a re-transfer using a reliable method (checksummed artefact store or secure file transfer).

---

## Resume: picking up after a failure

When a deployment fails partway through, use `resume` to continue from where it stopped. Resume skips all objects with `COMPLETED` status in the manifest and re-attempts only the remainder.

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json \
    --host myserver \
    --user dba_user
```

**Finding the manifest path:**

After any deployment attempt, the manifest is written to `logs/` inside the extracted package:

```bash
# List manifests in the current package directory
ls logs/.deploy_manifest_*.json
```

**Dry-run a resume:**

Before resuming a live deployment, preview what would be re-attempted:

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json --dry-run \
    --host myserver --user dba_user
```

**Resume with continue-on-error:**

If you want to attempt all remaining objects and collect a complete failure list in one pass:

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json \
    --continue-on-error --host myserver --user dba_user
```

**When NOT to use resume:**

Do not resume if the root cause of the failure affected objects that already completed. For example, if a database was created in phase 01 and the tables in phase 03 deployed incorrectly because the database was created in the wrong space pool, resuming will skip the database creation and leave the problem in place. Use rollback to undo the deployment, fix the source, and re-deploy.

---

## Rollback: undoing a deployment

SHIPS captures the pre-deployment state of modified objects before changing them. The `rollback` command uses these captures to restore the prior state.

### Running a rollback

```bash
python deploy.py rollback logs/.deploy_manifest_<id>.json \
    --host myserver --user dba_user
```

Rollback processes all eligible objects in reverse deployment order. The manifest is updated to reflect `ROLLED_BACK` state for each successfully restored object.

### What can be rolled back

| Object type | Strategy | Rollback mechanism | Coverage |
|---|---|---|---|
| **Table** (existing) | Backup and replace | Pre-deployment RENAME to `<Table>_bk_<timestamp>`; rollback drops new table and renames backup back | ✓ Full — data and definition restored |
| **Table** (new — did not exist) | Create only | Table dropped on rollback | ✓ Definition removed; no prior state to restore |
| **View / Macro** (existing) | DROP_AND_CREATE or REPLACE_IN_PLACE | SHOW DDL captured to `_rollback/` before any change; rollback drops current object and re-executes captured DDL | ✓ Prior definition restored |
| **View / Macro** (new — did not exist) | REPLACE_IN_PLACE creates new | No prior state to capture; rollback drops the object | ✓ Object removed; no prior definition to restore |
| **Procedure / Function** (SQL, not C) | DROP_AND_CREATE or REPLACE_IN_PLACE | SHOW DDL captured; rollback drops and re-executes | ✓ Prior definition restored |
| **Procedure / Function** (C external — `LANGUAGE C`) | Any | SHOW DDL captured and re-executed, **but the compiled binary is not recoverable** from Teradata — the restored DDL may reference the wrong binary version | ⚠ DDL-only — binary may not match. SHIPS reports `ROLLED_BACK` with an explicit warning. Use `ships rollback --to-tag` for a complete rollback including the binary. |
| **SQLJ JAR** (`.sjr`) | Direct execute | JAR binaries are stored in Teradata but are not SQL-queryable — cannot be extracted or restored automatically. SHIPS skips JAR rollback and reports `SKIPPED` with an explanation. | ✗ Not rollbackable via technical rollback. Use `ships rollback --to-tag` to rebuild and redeploy the previous JAR version. |
| **Join / Hash index** | Drop and create | Prior definition captured if SHOW is supported | ~ Partial — depends on SHOW support for index type |
| **CREATE DATABASE / USER / ROLE** | Direct execute | No backup mechanism | ✗ Cannot roll back — manual DROP required |
| **GRANT / REVOKE** | Direct execute | No backup mechanism | ✗ Cannot roll back — manual REVOKE/GRANT required |
| **DML (INSERT / UPDATE / DELETE)** | Direct execute | No row-level backup | ✗ Cannot roll back — data changes are permanent |

**Binary objects (SQLJ JARs and C external routines)** share a fundamental constraint: Teradata stores the compiled binary internally but provides no mechanism to extract it via SQL. This means technical rollback (wave or full) cannot restore binary objects to a previous version. The correct tool for binary rollback is always `ships rollback --to-tag <prev-tag>`, which rebuilds the package from the tagged source — including the old binary — and deploys it through the normal pipeline.

**The key limitation for views and procedures:** rollback restores the prior *definition*, not any data that flowed through it between deployment and rollback. For objects that did not exist before deployment (net-new views, net-new procedures), rollback simply removes them — there is no prior state to restore.

### Wave-level rollback

Use `--wave N` to roll back only the objects deployed in wave N, leaving earlier waves untouched.

```bash
# Roll back only wave-3 objects
python deploy.py rollback logs/.deploy_manifest_<id>.json \
    --wave 3 \
    --host myserver --user dba_user
```

Wave rollback is the natural complement to wave-parallel deployment. When wave 3 fails, `--wave 3` undoes only the objects that changed in that wave — waves 1 and 2 remain intact.

**Package status after wave rollback:** `PARTIALLY_ROLLED_BACK` — indicates that only a subset of objects was rolled back.

**Objects excluded from wave rollback:** Objects with no wave assignment (serial prereqs phase — CREATE DATABASE, CREATE USER) are always excluded. They can only be rolled back via full package rollback.

**Dry-run a wave rollback (offline, no connection needed):**

```bash
python deploy.py rollback logs/.deploy_manifest_<id>.json \
    --wave 3 --dry-run
```

Describes exactly which objects would be rolled back and by what mechanism. Does not execute any DDL and does not mutate the manifest.

### Checking what is eligible for rollback before running it

Inspect the manifest for backup tables and rollback files:

```bash
python deploy.py status logs/.deploy_manifest_<id>.json
```

Objects with a `backup_table` value have a rename-based rollback available. Objects with a `rollback_file` path have a SHOW-DDL capture available.

### After rollback

Once rollback completes:

1. Run `status` to confirm all objects show `ROLLED_BACK`
2. Verify in Teradata that the objects are back to their prior state
3. Contact the developer with the failure details from the deployment report so they can fix the source and rebuild the package

---

## Promoting between environments

### Same build number, different environment

The most important rule of promotion: **do not rebuild for TST or PRD**. Rebuild from source means the package is different — different token resolution, potentially different DDL. Promote the exact same build.

```bash
# The developer produced OMR_DEV_BUILD_0005_20260509.zip
# You need the TST version

# Ask the developer to produce:
python -m td_release_packager package \
    --source /projects/OMR \
    --env TST \
    --name OMR \
    --env-config config/env/TST.conf \
    --output releases/ \
    --no-increment          ← same build number (0005), different env

# You receive: OMR_TST_BUILD_0005_20260509.zip
# Deploy as normal
python deploy.py --host tst-server --user dba_user
```

The `--no-increment` flag reuses the build counter without advancing it. The only difference between the DEV and TST packages is the resolved token values — the DDL structure is identical.

### Verifying environment consistency

Before deploying to TST or PRD, confirm:

1. The build number matches the approved DEV build
2. The package hash in `context/ships.build.json` matches the developer's build record
3. The `SHIPS_ENV` in the package (`context/ships.build.json` → `environment`) matches the target

```bash
python -c "
import json, zipfile
with zipfile.ZipFile('OMR_TST_BUILD_0005_20260509.zip') as z:
    build = json.loads(z.read('context/ships.build.json'))
print('Build:', build['build_number'])
print('Env:  ', build['environment'])
print('Hash: ', build['package_hash'])
"
```

---

## Operational runbook: standard deployment sequence

Copy this checklist for every deployment.

```
[ ] 1. Receive package from developer or artefact store
[ ] 2. Extract to a working directory
[ ] 3. Run: python deploy.py integrity-check
        → Exit 0: proceed
        → Exit 1: stop, contact developer
[ ] 4. Deploy companion prereqs archive first (if applicable)
        Check context/ships.build.json for "requires" field
[ ] 5. Dry run: python deploy.py --dry-run --host ... --user ...
        → Review logs/.deploy_report_*.html
        → Fix any pre-flight errors (space, permissions)
[ ] 6. Live deployment: python deploy.py --host ... --user ... --streams N
[ ] 7. Open deployment report: logs/.deploy_report_*.html
        → FAILED objects: investigate, fix, resume
        → SKIPPED objects: confirm intentional
[ ] 8. Run: python -m td_release_packager verify --project <project_dir>
        (if access to project directory; confirms post-deploy state)
[ ] 9. Record the build number and package hash in your change log
```

---

## Agent deployment patterns

### Standard agentic deployment sequence

```python
import subprocess, json, pathlib, zipfile

package_zip = pathlib.Path("OMR_DEV_BUILD_0005_20260509.zip")
work_dir = pathlib.Path("deploy_work")
work_dir.mkdir(exist_ok=True)

# 1. Extract
import zipfile
with zipfile.ZipFile(package_zip) as z:
    z.extractall(work_dir)

# 2. Integrity check
r = subprocess.run(
    ["python", "deploy.py", "integrity-check"],
    cwd=work_dir, capture_output=True
)
if r.returncode != 0:
    raise RuntimeError(f"Integrity check failed:\n{r.stdout.decode()}")

# 3. Dry run
r = subprocess.run(
    ["python", "deploy.py",
     "--dry-run", "--host", HOST, "--user", USER, "--password", PASS],
    cwd=work_dir, capture_output=True
)
# Parse the manifest for pre-flight failures
manifests = sorted(work_dir.glob("logs/.deploy_manifest_*.json"))
manifest = json.loads(manifests[-1].read_text())
preflight_errors = [
    c for c in manifest.get("preflight", {}).get("checks", [])
    if not c["passed"] and c["severity"] == "ERROR"
]
if preflight_errors:
    raise RuntimeError(f"Pre-flight failed: {preflight_errors}")

# 4. Live deployment
r = subprocess.run(
    ["python", "deploy.py",
     "--host", HOST, "--user", USER, "--password", PASS, "--streams", "4"],
    cwd=work_dir, capture_output=True
)

# 5. Read outcome
manifests = sorted(work_dir.glob("logs/.deploy_manifest_*.json"))
manifest = json.loads(manifests[-1].read_text())
failed = [
    name for name, rec in manifest["objects"].items()
    if rec["state"] == "FAILED"
]
if failed:
    # Attempt rollback
    subprocess.run(
        ["python", "deploy.py", "rollback",
         str(manifests[-1]),
         "--host", HOST, "--user", USER, "--password", PASS],
        cwd=work_dir
    )
    raise RuntimeError(f"Deployment failed for: {failed}")
```

### Exit codes

| Code | Command | Meaning |
|---|---|---|
| `0` | Any | Success — all objects in the expected final state |
| `1` | `deploy` | One or more objects failed |
| `1` | `integrity-check` | Files modified since packaging |
| `1` | `resume` | One or more objects failed on resume |
| `1` | `rollback` | One or more rollback operations failed |
| `1` | `status` | Manifest not found |

### Reading deployment outcomes programmatically

The manifest at `logs/.deploy_manifest_<id>.json` has this structure:

```json
{
    "deployment_id": "deploy_20260509_143000_a3f8",
    "package_dir": "/path/to/extract",
    "status": "COMPLETED",
    "started_at": "2026-05-09T14:30:00+00:00",
    "updated_at": "2026-05-09T14:32:15+00:00",
    "objects": {
        "OMR_STD.Customer": {
            "state": "COMPLETED",
            "ddl_file": "DDL/tables/OMR_STD.Customer.tbl",
            "object_type": "TABLE",
            "backup_table": "OMR_STD.Customer_bk_20260509143012",
            "rollback_file": null,
            "rows_migrated": 0,
            "message": "Table deployed successfully.",
            "error": null
        }
    }
}
```

**Object states:** `PENDING` / `COMPLETED` / `FAILED` / `SKIPPED` / `BACKED_UP` / `MIGRATED` / `ROLLED_BACK`

---

## Troubleshooting quick reference

| Symptom | Likely cause | Action |
|---|---|---|
| `Error 2646: No more room` | Database out of space | MODIFY DATABASE, then `resume` |
| `Error 3523: No privilege` | Missing GRANT on deploy user | Grant privilege, then `resume` |
| `Error 3803: Already exists` | CREATE on existing object | DROP manually or change DDL intent, then `resume` |
| `Error 5589: Procedure not compiled` | Syntax error in SPL | Fix in source, rebuild package |
| `Error 3598 / 2631` | Lock conflict / deadlock | Wait, then `resume` (deployer auto-retries these) |
| `CRITICAL: Integrity check FAILED` | Package tampered / corrupted in transit | Do not deploy; obtain fresh package |
| Objects SKIPPED — `PREREQ_EXEMPT` | Object in wrong package (main vs prereqs) | Deploy companion prereqs archive first |
| Objects SKIPPED — `not applicable` | Object type not in current deploy scope | Expected; no action |
| Report shows prior run — no new objects | Package already fully deployed | Run `status` to confirm; this is a REPLAY run |
| `FileNotFoundError: .deploy_manifest` | Manifest path wrong in `resume`/`rollback` | Check `logs/` directory for the correct filename |
| `teradatasql` import error | Driver not installed | `pip install teradatasql` |
| `Python 3.10` insufficient | Wrong Python version | Install Python 3.13+ |
| Deployment extremely slow | Single stream on large package | Add `--streams 4` |
| Wave graph not showing | Non-wave-parallel deployment | Only appears for packages built with wave analysis |

---

## Common DBQL queries for compliance reporting

### All deployments in a period

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD')          AS DeployDate,
    GetQueryBandValue(t1.QueryBand, 0, 'PKG')             AS Package,
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD')           AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')             AS Environment,
    t1.UserName                                            AS DeployUser,
    COUNT(*)                                               AS Statements
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'DEPLOYER') = 'database_package_deployer_v2'
  AND CAST(t1.CollectTimeStamp AS DATE) BETWEEN DATE '2026-01-01' AND DATE '2026-12-31'
GROUP BY DeployDate, Package, Build, Environment, DeployUser
ORDER BY DeployDate, Package;
```

### Verify a specific build reached PRD

```sql
SELECT COUNT(*) AS StatementsExecuted
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '0005'
  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = 'OMR'
  AND GetQueryBandValue(t1.QueryBand, 0, 'ENV')   = 'PRD';
-- > 0 confirms deployment ran
```

### Find which builds touched a specific table

```sql
SELECT DISTINCT
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')   AS Environment,
    CAST(t1.CollectTimeStamp AS DATE) AS LogDate
FROM DBC.DBQLogTbl t1
JOIN DBC.DBQLSqlTbl t2
  ON t1.QueryID          = t2.QueryID
 AND t1.CollectTimeStamp = t2.CollectTimeStamp
WHERE GetQueryBandValue(t1.QueryBand, 0, 'DEPLOYER') = 'database_package_deployer_v2'
  AND UPPER(t2.SqlTextInfo) LIKE '%OMR_STD.CUSTOMER%'
ORDER BY t1.CollectTimeStamp DESC;
```
