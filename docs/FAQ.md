# SHIPS FAQ

Answers to the most common questions. Organised by topic — jump to the section that matches your problem.

---

- [Tokenisation and database names](#tokenisation-and-database-names)
- [Harvest and classification](#harvest-and-classification)
- [Inspect errors and warnings](#inspect-errors-and-warnings)
- [Packaging and trust](#packaging-and-trust)
- [Deployment](#deployment)
- [Dependency ordering and waves](#dependency-ordering-and-waves)
- [Environment promotion](#environment-promotion)
- [Schema drift detection](#schema-drift-detection)
- [Audit trail and decisions.json](#audit-trail-and-decisionsjson)
- [General](#general)

---

## Tokenisation and database names

### My DDL still has hardcoded database names after harvest. Nothing was tokenised.

Two things to check:

**1. Did you pass `--token-map`?**

Harvest does not apply tokens unless you tell it which map to use:

```bash
python -m td_release_packager harvest \
    --source /my/sql/ \
    --project /my/project/ \
    --token-map config/token_map.conf   ← required
```

If `token_map.conf` does not exist yet, generate it first with `--generate-token-map`:

```bash
python -m td_release_packager harvest \
    --source /my/sql/ \
    --project /my/project/ \
    --generate-token-map \
    --env-prefix A_D01
```

**2. Is the database name in `token_map.conf`?**

Open `config/token_map.conf`. If the hardcoded name is not listed there, SHIPS does not know to tokenise it. Add an entry:

```
A_D01_MY_DB={{MY_DB}}
```

Then re-run harvest with `--token-map`.

---

### I ran `--generate-token-map` but my database name is not in the map.

SHIPS only generates entries for names it finds in SQL positions where a database qualifier is expected (FROM clause, table references, etc.). Possible causes:

- The file uses a non-standard DDL verb SHIPS does not recognise — check for `UNCLASSIFIED` warnings in the harvest output.
- The database name contains only two parts (e.g. `MyDB`) and SHIPS could not distinguish it from an object name — add it manually to `token_map.conf`.
- The file had a syntax error and was not parsed — check the harvest output for parse warnings.

---

### Where do I define what a token resolves to for each environment?

In `config/env/<ENV>.conf`. For example, `config/env/DEV.conf`:

```properties
SHIPS_ENV=DEV
ENV_PREFIX=A_D01
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
```

And `config/env/PRD.conf`:

```properties
SHIPS_ENV=PRD
ENV_PREFIX=P
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
```

The token `{{OMR_STD}}` resolves to `A_D01_OMR_STD` in DEV and `P_OMR_STD` in PRD. Same source, different values.

---

### `{{TOKEN}}` is appearing in the deployed DDL — it was not replaced.

This means the token was not resolved at package time. Three causes:

**1. The token is not in the env config file.**

Open `config/env/DEV.conf` (or whichever env you packaged for). Check that `MY_TOKEN=<value>` is present. Add it if missing, re-run `ships process`.

**2. The token was mistyped — wrong case, spaces inside the braces.**

`{{MY_TOKEN}}` is valid. `{{ MY_TOKEN }}` (with spaces), `{{my_token}}` (wrong case), and `{MY_TOKEN}` (single braces) are not. SHIPS is case-sensitive and whitespace-sensitive.

Run a scan to find all undefined tokens before packaging:

```bash
python -m td_release_packager scan \
    --source /my/project/ \
    --env-config config/env/DEV.conf
```

Exit 0 = all tokens resolved. Exit 1 = unresolved tokens listed.

**3. You packaged without specifying `--env-config`.**

The package command requires `--env-config` to resolve tokens:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env DEV \
    --env-config config/env/DEV.conf \   ← required
    --name MyProject
```

---

### I want to skip tokenisation for now. Can I?

Yes. Just omit `--token-map` from harvest. Your DDL will be copied into the payload with its original hardcoded database names. Inspect will flag them as `hardcoded_name` warnings. You can suppress that rule in `config/inspect.conf` while you transition:

```properties
hardcoded_name=OFF
```

The package will still build and deploy — you just lose environment portability.

---

### What is the difference between `token_map.conf` and the env config files?

They serve two different purposes:

| File | Purpose | Example |
|---|---|---|
| `config/token_map.conf` | Maps a **literal database name** in source to a **token placeholder** | `A_D01_OMR_STD={{OMR_STD}}` |
| `config/env/DEV.conf` | Maps a **token** to its **resolved value** for a specific environment | `OMR_STD=A_D01_OMR_STD` |

Harvest uses `token_map.conf` to replace literals with `{{...}}`. Package uses the env config to replace `{{...}}` with real values. The two-step split is what allows the same source to target multiple environments.

---

### My `decompose-names` output looks wrong. How do I fix it?

`decompose-names` tries to infer composition roots (`ENV_PREFIX`, `SHIPS_PROJECT`) from database names. If the inference is wrong, edit the generated `.conf` file directly — it is just a starting point. The tool is heuristic; your naming convention may not match its assumptions.

---

## Harvest and classification

### SHIPS says my file is unclassified. Why?

SHIPS classifies files by looking for a `CREATE` or `REPLACE` statement near the top of the file. Common causes of unclassification:

- **No DDL statement**: the file is a BTEQ session script, a comment-only file, or contains only `SELECT` statements. Exclude it from `--source`.
- **Missing database qualifier**: `CREATE TABLE my_table` (no `DB.` prefix) is not classified as a TABLE. Add the qualifier.
- **Encoding issue**: the file is not UTF-8. SHIPS reads files as UTF-8 — convert the encoding.
- **Long comment block before the DDL**: SHIPS scans the first portion of the file. Move the `CREATE` statement before multi-paragraph comment blocks.
- **Non-standard Teradata syntax**: SHIPS recognises standard `CREATE`/`REPLACE` verbs. Vendor-specific DDL extensions may not be recognised.

Run harvest without `--token-map` to see which files are classified and which are not:

```bash
python -m td_release_packager harvest --source /my/sql/ --project /my/project/
```

---

### Harvest split my multi-statement file, but I wanted them to stay together.

Harvest splits files that contain multiple DDL objects — one object per file is a SHIPS requirement (eponymous naming). If you have a single logical operation that touches multiple tables in a specific order (e.g. a seed-data INSERT sequence), use a `.dml` file marked with `-- MULTI_TABLE_DML` at the top. SHIPS will not split it.

---

### Harvest keeps overwriting my manually edited payload files.

By default, harvest wipes and rebuilds the payload from source on every run (clean-payload mode). This is intentional — the payload is not a hand-curated artefact; it is always derived from source.

If you need to overlay new files without wiping, use `--keep-existing`:

```bash
python -m td_release_packager harvest --keep-existing ...
```

Use this sparingly. The recommended pattern is: keep all source in version control and let harvest rebuild the payload cleanly each time.

---

## Inspect errors and warnings

### What does `db_qualifier ERROR` mean?

Your DDL creates an object without a database prefix:

```sql
-- Wrong:
CREATE TABLE Customer (Id INTEGER);

-- Correct:
CREATE TABLE {{OMR_STD}}.Customer (Id INTEGER);
```

Every object in the payload must be fully qualified. SHIPS enforces this because an unqualified name would deploy into whatever database the DBA happens to be connected to — unpredictable and ungovernable.

---

### What does `type_suffix WARNING` mean?

Your object name encodes the type in the name:

```
VW_Customer.viw   ← type already in the extension (.viw = VIEW)
SP_ProcessOrder.spl  ← same problem
```

SHIPS encodes the type in the file extension, not the name. Rename the object to `Customer.viw` and `ProcessOrder.spl`. The type is always unambiguous from the extension.

---

### What does `hardcoded_name WARNING` mean?

The DDL body still contains a literal database name that was not tokenised:

```
A_D01_OMR_SEM appears in payload/database/DDL/views/OMR_STD.MySummary.viw
```

Add `A_D01_OMR_SEM={{OMR_SEM}}` to `config/token_map.conf` and re-run harvest with `--token-map`. If this is an intentional constant (e.g. a shared system database that never changes), you can suppress the rule in `config/inspect.conf`:

```properties
hardcoded_name=OFF
```

---

### How do I suppress inspect rules I do not want?

Edit `config/inspect.conf` in your project:

```properties
# Values: ERROR | WARNING | OFF
keyword_case=OFF
leading_commas=OFF
deploy_intent=WARNING
db_qualifier=ERROR        # keep this one — it matters
```

Rules set to `OFF` are not checked. Rules set to `WARNING` are reported but do not block packaging (unless you use `--strict`).

---

### Inspect has too many errors. How do I tackle them?

Start with the `ERROR`-severity rules — these block packaging. Fix them first, then address warnings.

A common pattern for onboarding a legacy codebase:

1. Turn `hardcoded_name` to `WARNING` (not `OFF`) so you see what needs tokenising but it does not block you
2. Fix `db_qualifier` errors (all objects must be qualified)
3. Fix `type_suffix` warnings at your own pace
4. Fix `deploy_intent` warnings (prefer `REPLACE VIEW` over `CREATE VIEW`)

You do not need to fix everything before you can package.

---

## Packaging and trust

### My package says `READY-WITH-CAVEATS`. Should I worry?

`READY-WITH-CAVEATS` means at least one warning-level signal fired but no error-level signals. Common causes:

- `source_dirty=true` — you built with `--allow-dirty`. The package was built from an uncommitted working tree.
- `inspect_grants` warning — some grants are missing or mismatched.
- `provenance_complete` — `_provenance.json` was not produced (unusual).

Review the per-signal breakdown in the trust banner or the `BUILD.json` `trust.signals` field. For most warning-level signals, proceed with deployment but investigate the underlying issue before the next release.

---

### My package is `BLOCKED`. What do I fix?

`BLOCKED` means at least one `ERROR`-level signal fired. The package will not deploy unless you use `--skip-trust-check` (development only). Fix the underlying problem and rebuild.

Common causes:

| Signal | Fix |
|---|---|
| `inspect_token_format` | Malformed `{{TOKEN}}` in payload — find it with `grep -r '{{' payload/` |
| `inspect_lint` | ERROR-severity lint rule violation — run `ships inspect` for details |
| `inspect_grants` | Grant drift detected at ERROR level — run `ships inspect --fix-grants` |

---

### The build counter keeps incrementing. How do I reuse the same build number for a different environment?

Use `--no-increment`:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env TST \
    --env-config config/env/TST.conf \
    --name OMR \
    --no-increment    ← reuses current build number
```

This is the correct pattern for promoting a DEV package to TST or PRD — same source, same build number, different environment tokens.

---

### The `--allow-dirty` flag — when should I use it?

Only in development. `--allow-dirty` lets you build a package from an uncommitted working tree. It stamps `source_dirty=true` in `BUILD.json` and degrades the trust label to `READY-WITH-CAVEATS`. Never use it for a package you intend to promote to production — the package cannot be reproducibly rebuilt from source because the uncommitted changes are not in version control.

---

## Deployment

### The deployer says `Error 3523: No privilege`. What do I grant?

The DBA account running `deploy.py` is missing a privilege on one of the target databases. The deployment report's pre-flight section lists exactly which databases and which privilege types are missing.

SHIPS also generates a prerequisite GRANT script during pre-flight. Look in the HTML report for the "Pre-flight Results" section — click the failing check to see the exact `GRANT` statement needed.

Common grants:

```sql
GRANT CREATE TABLE ON MyDB TO deploy_user;
GRANT DROP TABLE   ON MyDB TO deploy_user;
GRANT CREATE VIEW  ON MyDB TO deploy_user;
```

After granting, re-run with `resume`:

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json --host srv --user dba
```

---

### The deployment failed halfway. Do I have to start over?

No. Re-run the same command. The manifest records each object's state, and the deployer skips anything already in `COMPLETED` state:

```bash
python deploy.py --host srv --user dba   ← same command
```

Or explicitly resume from the manifest:

```bash
python deploy.py resume logs/.deploy_manifest_<id>.json --host srv --user dba
```

---

### A table was dropped and recreated but the data is gone.

SHIPS's table deployment strategy backs up the table (renames it to `<TableName>_bk_<timestamp>`) before creating the new version. If the schema was compatible, data was migrated. If not, the backup table is still there.

To check:

```bash
python deploy.py status logs/.deploy_manifest_<id>.json
```

Look for `backup_table` in the object record. If it is set, you can query `SELECT * FROM <backup_table>` to recover data.

---

### I need to deploy a package built before schema drift detection was added. Will drift detection break it?

No. If `BUILD.json` has no `baseline_dir` field (older package), drift detection is silently disabled. `_load_baseline_dir()` returns an empty string and the deployment proceeds normally.

---

### What does `--on-drift continue / skip / abort` mean?

When schema drift is detected (an object changed out-of-band since the last SHIPS deploy):

| Mode | Behaviour |
|---|---|
| `abort` | FAIL the object — stop deployment (default — safe) |
| `skip` | SKIP the object — leave the out-of-band change in place, deploy everything else |
| `continue` | COMPLETE as normal — SHIPS overwrites the out-of-band change |

For rollbacks (`ships rollback --to-tag`), `continue` is the default because the point of rollback is to restore a known-good state.

---

### How do I roll back a deployment?

**Technical rollback** (undo the current deployment using pre-captured DDL):

```bash
# Roll back everything
python deploy.py rollback logs/.deploy_manifest_<id>.json --host srv --user dba

# Roll back only wave 3
python deploy.py rollback logs/.deploy_manifest_<id>.json --wave 3 --host srv --user dba
```

**Feature rollback** (re-deploy from a previous git tag):

```bash
python -m td_release_packager rollback \
    --to-tag v1.2.3 \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR
```

Then deploy the produced package with `--on-drift continue`.

Note: JARs (`.sjr`) and C external routines (`LANGUAGE C`) cannot be restored by technical rollback — the compiled binary is not recoverable from Teradata. Feature rollback handles these correctly because the old binary is in the tagged source.

---

## Dependency ordering and waves

### Objects are deploying in the wrong order. A view fails because its base table does not exist yet.

Run the analyser before packaging. The analyser builds the dependency graph and writes `_waves.txt`, which the deployer uses to enforce ordering:

```bash
python -m td_release_packager analyze --source /my/project/
```

Or use `ships process` which runs all stages including analyse automatically.

If you are deploying a manually assembled package without `_waves.txt`, the deployer falls back to type-based ordering (tables before views before procedures). This is usually correct but may fail if you have cross-database dependencies or unusual object type dependencies.

---

### The analyser says I have a circular dependency. How do I fix it?

Two objects depend on each other — for example, View A references View B, and View B references View A. Teradata cannot create them in any order without the other existing.

Common fixes:

- **Split the view**: extract the common columns into a base view that both reference, breaking the cycle.
- **Use a table instead of a view**: for performance reasons, materialise one of the views as a table.
- **Deploy one with a placeholder**: create View A first with a stub definition that does not reference View B, create View B, then replace View A with the full definition.

The analyser reports which objects form the cycle — use that to identify which is easiest to restructure.

---

### I have objects that must deploy in a specific order not captured by dependency analysis.

You can override the wave ordering by writing a `_waves.txt` file manually in the project root:

```
payload/03_ddl/tables/DB.FirstTable.tbl
payload/03_ddl/tables/DB.SecondTable.tbl
---
payload/03_ddl/views/DB.MyView.viw
```

Objects separated by `---` barriers are in different waves. The deployer honours this ordering exactly.

---

## Environment promotion

### How do I take the DEV package to TST without rebuilding from source?

Build a TST package from the same source using `--no-increment`:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env TST \
    --env-config config/env/TST.conf \
    --name OMR \
    --no-increment    ← same build number as DEV
```

The DDL structure is identical. Only the token resolution differs — `{{OMR_STD}}` resolves to `T_OMR_STD` instead of `A_D01_OMR_STD`. Always verify the `environment` field in BUILD.json before handing off to the DBA.

---

### The DBA is asking which package to deploy to PRD. How do I verify it is the right one?

```bash
python -c "
import zipfile, json
with zipfile.ZipFile('OMR_PRD_BUILD_0042.zip') as z:
    name = next(n for n in z.namelist() if n.endswith('BUILD.json'))
    b = json.loads(z.read(name))
print('Package: ', b['package_name'])
print('Build:   ', b['build_number'])
print('Env:     ', b['environment'])
print('Commit:  ', b['source_commit'])
print('Trust:   ', b['trust']['label'])
"
```

Confirm `environment = PRD`, the build number matches the approved DEV build, and `trust.label` is `READY`.

---

## Schema drift detection

### Drift detection is disabled. The log says "set deployment.baseline_dir in ships.yaml".

Drift detection requires a shared filesystem path configured in `ships.yaml`:

```yaml
# ships.yaml
deployment:
  baseline_dir: /shared/nfs/ships-baselines/OMR/
```

This path must be accessible from every machine that deploys this package. After adding it, rebuild the package — the path is stamped into `BUILD.json` and travels with the package automatically. Operators do not need to set any flags.

If no shared path is available, use a local directory for development:

```bash
python deploy.py --host srv --user dba --baseline-dir /tmp/ships-baselines/
```

This works per-machine but does not share baselines across operators.

---

### Drift was detected on an object I know is correct. How do I reset the baseline?

The simplest reset is to deploy with `--on-drift continue` — this runs the deployment as normal and writes a new baseline from the post-deploy SHOW output:

```bash
python deploy.py --host srv --user dba --on-drift continue
```

After this run, the baseline reflects what SHIPS just deployed. Future drift detection will compare against the new baseline.

---

## Audit trail and decisions.json

### What is `decisions.json`?

An append-only audit log written by every SHIPS pipeline run. Every stage records what config it used, what it processed, what it produced, and any issues it found. It is the machine-readable history of every `ships harvest`, `ships inspect`, `ships analyse`, `ships package`, and `ships process` run on a project.

Read it in human form:

```bash
python -m td_release_packager explain --project /my/project/ --command process
```

Or query the last run from Python:

```python
import json, pathlib
decisions = json.loads(pathlib.Path("decisions.json").read_text())
last_run = decisions["runs"][-1]
print(last_run["final_status"])
```

---

### `decisions.json` is getting very large. How do I trim it?

```bash
# Keep the 50 most recent runs
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-runs 50

# Keep runs from the last 90 days
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-days 90

# Preview first (no changes written)
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-runs 50 \
    --dry-run
```

---

### How do I find a deployment in Teradata DBQL?

Every SQL statement SHIPS executes carries a query band with the build number, package name, environment, and package hash. Query it with `GetQueryBandValue`:

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD') AS DeployDate,
    t1.UserName,
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')   AS Environment,
    COUNT(*)                                     AS Statements
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '0042'
  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = 'OMR'
GROUP BY DeployDate, UserName, Build, Environment
ORDER BY DeployDate;
```

The SHIPS Deployment Dashboard also generates this query for you — see the Compliance tab on the package detail page.

---

## General

### Where do I start with an existing codebase?

Run the onboarding wizard. It scans your source directory and recommends the exact steps based on what it finds:

```bash
python -m td_release_packager onboard --source /path/to/legacy/sql/ --env DEV
```

It will tell you whether you need `import-legacy` (old `$VAR` markers), `bootstrap-env-config` (SHIPS tokens but no config yet), or can go straight to `harvest`.

---

### What is the difference between `ships process` and running each command separately?

`ships process` runs all stages under a single `decisions.json` entry — harvest → generate → inspect → analyse → package — and stops on the first error (with `--strict`) or collects all errors and summarises at the end (default). It is the recommended daily driver.

Running stages individually gives you more control: you can inspect the output of each stage before proceeding, or re-run just one stage without running the whole pipeline.

---

### SHIPS is processing files slowly. How do I speed it up?

- **Harvest**: fast by design. If slow, check for very large files — SHIPS reads each file fully.
- **Inspect**: scales with the number of files. For 1000+ files expect a few seconds.
- **Analyse**: scales with the number of objects and dependencies. Complex dependency graphs with many external references take longer.
- **Deploy**: use `--streams 4` (or higher) for large packages. Wave-parallel deployment significantly reduces total time for packages with 50+ objects.

---

### Can multiple developers work on the same SHIPS project simultaneously?

Yes. Put the project directory (including `payload/`) in version control. Each developer:

1. Pulls the latest project
2. Harvests their source changes
3. Commits the updated payload

Because harvest rebuilds from source cleanly each time, the payload always reflects what is in source control. Merge conflicts in the payload are resolved the same way as any other code conflict — by looking at the source, not the payload.

---

### How do I add a new DDL object type that SHIPS does not recognise?

SHIPS supports 20+ Teradata object types out of the box. If you have a custom extension (e.g. `.tdsql`), add it to `ships.yaml`:

```yaml
discovery:
  extensions:
    - .tdsql
```

This tells harvest and the deployer to include files with that extension. The extension is stamped into `BUILD.json` so the deployer honours it automatically. You will still need to ensure your DDL files parse correctly — SHIPS classifies by looking for `CREATE`/`REPLACE` verbs regardless of extension.

---

### Is there a way to see the package contents without extracting it?

Yes — open `package_report.html` from inside the archive, or use the Python zipfile module:

```python
import zipfile, json

with zipfile.ZipFile("OMR_DEV_BUILD_0042.zip") as z:
    # List all files
    print("\n".join(z.namelist()))

    # Read BUILD.json
    name = next(n for n in z.namelist() if n.endswith("BUILD.json"))
    build = json.loads(z.read(name))
    print(f"Trust: {build['trust']['label']}")
    print(f"Files: {build['file_count']}")
```

Or use the SHIPS Deployment Dashboard (`ships_dashboard.py`) which reads BUILD.json from every archive in your `releases/` directory without extracting.
