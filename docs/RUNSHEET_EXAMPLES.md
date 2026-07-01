# SHIPS Runsheet Examples

This document gives step-by-step examples for turning Teradata DDL, DCL, and DML
from different source shapes into SHIPS release packages, then deploying those
packages with the current release-group/zip deploy launcher.

The examples use `python -m td_release_packager` explicitly. If your site has a
`ships` wrapper on `PATH`, you can replace `python -m td_release_packager` with
`ships`.

## Command Vocabulary

Use these argument meanings throughout the runsheets:

| Argument | Applies to | Meaning |
|---|---|---|
| `--project` | Most project commands | Existing SHIPS project directory. This is where `ships.yaml`, `config/`, `payload/`, `releases/`, and `ships.decisions.json` live. |
| `--source` | `harvest`, `process`, `onboard`, `migrate-source` | Raw SQL/DDL/DCL/DML source directory to ingest. It is not the SHIPS project unless your raw files already live inside the project. |
| `--source-github OWNER/REPO` | `process`, `package` | Fetch source from GitHub by repository name. Mutually exclusive with `--source` or `--project`, depending on the command. |
| `--source-ref REF` | `process`, `package` with `--source-github` | Branch, tag, or commit SHA to fetch. Defaults to `main`. |
| `--github-token TOKEN` | `process`, `package` with `--source-github` | Token for private GitHub repositories. If omitted, SHIPS uses `GITHUB_TOKEN`. |
| `--token-map` | `harvest`, `process` | **[DEPRECATED — prefer `config/tokenise.conf`]** File mapping literal database names to SHIPS tokens, for example `A_D01_OMR_STD={{OMR_STD}}`. Still works; see [#388](https://github.com/earthshiner/teradata-ships/issues/388). |
| `--auto-tokenise` | `harvest`, `process` | Detect hardcoded database names and apply tokens in one pass. Faster, but skips manual token map review. |
| `--env-prefix` | `harvest`, `process` | Prefix to strip when deriving token names, for example `A_D01_OMR_STD` becomes `{{OMR_STD}}` when `--env-prefix A_D01` is used. |
| `--env` | `process`, `package`, `rollback` | Target environment label stamped into the package, for example `DEV`, `TST`, or `PRD`. |
| `--env-config` | `process`, `package`, `scan`, `rollback` | Environment config that resolves `{{TOKEN}}` values for one target environment. |
| `--name` | `process`, `package` | Logical package name used in release artifact names and metadata. |
| `--output` | `process`, `package`, `scaffold` | Parent output directory for generated projects or release groups. |
| `--skip-generate` | `process` | Skip generated view-layer DDL. Use this for hand-written projects that do not use SHIPS view generation. |
| `--strict` | `process`, `inspect` | Treat validation failures as blocking. In `process`, abort on the first error stage. |
| `--allow-dirty` | `package` | Permit packaging from an uncommitted working tree. The Trust Report records the caveat. |
| `--change-ref` | `package` | Change ticket reference stamped into the package. Required when configured in `ships.yaml`. |
| `--signing-key` | `package` | HMAC signing key file. Produces a `.hmac` sidecar. |
| `--asymmetric-key` | `package` | Ed25519 private key. Produces a `.sig` sidecar. |
| `deploy <target>` | `deploy` | Target can be a release-group directory, one package zip, or an extracted package directory. |
| `--work-dir` | `deploy` | Optional extraction directory. Defaults to `.ships-work` beside the target. |
| `--role` | `deploy` | Release-group package role to run. Defaults to `main`. Normally leave this alone. |

Arguments after `deploy <target>` are passed unchanged to the generated package
deployer. Common forwarded deploy arguments include:

| Forwarded deploy argument | Meaning |
|---|---|
| `--dry-run` | Run parse, integrity, trust, and pre-flight checks without executing DDL/DCL/DML. |
| `--host` | Teradata host. |
| `--user` | Teradata deploying user. |
| `--password` | Password for non-interactive runs. Prefer a secret manager or environment variable. |
| `--logmech` | Teradata logon mechanism, for example `TD2`, `LDAP`, or `TDNEGO`. |
| `--streams` | Number of parallel streams for wave-parallel DDL deployment. |
| `--continue-on-error` | Attempt remaining objects after a failure and report all failures. |
| `--on-drift abort|skip|continue` | What to do when SHIPS detects out-of-band schema drift. |
| `--baseline-dir` | Override drift baseline storage location for one deployment. |
| `--encryptdata true` / `--sslmode require` | Request encrypted Teradata connections. |
| `resume <manifest>` | Resume a failed deployment from a manifest. |
| `status <manifest>` | Show manifest state without a database connection. |
| `rollback <manifest>` | Technical rollback using pre-deploy rollback captures. |

## Standard Project Shape

Most runsheets end with this project shape:

```text
C:\Projects\OMR\
  ships.yaml
  config\
    inspect.conf
    token_map.conf
    env\
      DEV.conf
      TST.conf
      PRD.conf
  payload\
    database\
      DDL\
      DCL\
      DML\
  releases\
```

DDL files are object definitions such as tables, views, macros, procedures,
functions, triggers, databases, users, roles, profiles, and SQLJ JAR installs.
DCL files are grants and revokes. DML files are seed or reference data changes
such as `INSERT`, `UPDATE`, `DELETE`, and `MERGE`.

## Runsheet 1: Legacy Codebase With Existing Substitution Script

Use this when the current deployment process already has a sed-style
substitution file or similar marker map, for example `$STD_DB`, `${SEM_DB}`, or
`&&APP_DB&&`.

### Steps

1. Create the SHIPS project.

```powershell
python -m td_release_packager scaffold `
  --name OMR `
  --output C:\Projects
```

Arguments:

| Argument | Explanation |
|---|---|
| `--name OMR` | Creates `C:\Projects\OMR` and uses `OMR` as the project name. |
| `--output C:\Projects` | Parent directory for the new project. |

2. Convert the legacy substitution script into SHIPS config.

```powershell
python -m td_release_packager import-legacy `
  --script C:\Legacy\deploy\substitutions.sed `
  --env DEV `
  --output-dir C:\Projects\OMR\config
```

Arguments:

| Argument | Explanation |
|---|---|
| `--script` | Existing substitution script. SHIPS reads the legacy marker to value mappings. |
| `--env DEV` | Environment name for the generated config. |
| `--output-dir` | Writes `env\DEV.conf` and `tokenise.conf` under this directory. |

3. Preview migration from legacy markers to SHIPS `{{TOKEN}}` markers.

```powershell
python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\tokenise.conf `
  --source C:\Legacy\sql `
  --project C:\Projects\OMR `
  --dry-run
```

Arguments:

| Argument | Explanation |
|---|---|
| `--tokenise-config` | Tokenisation config produced by `import-legacy` (or hand-authored). |
| `--source` | Raw legacy SQL directory to update. |
| `--project` | Lets SHIPS honour any project-specific extension settings in `ships.yaml`. |
| `--dry-run` | Shows what would change without editing files. |

4. Apply the migration after review.

```powershell
python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\tokenise.conf `
  --source C:\Legacy\sql `
  --project C:\Projects\OMR
```

5. Harvest the migrated SQL into the SHIPS project.

```powershell
python -m td_release_packager harvest `
  --source C:\Legacy\sql `
  --project C:\Projects\OMR `
  --token-map C:\Projects\OMR\config\token_map.conf
```

Arguments:

| Argument | Explanation |
|---|---|
| `--source` | Directory containing migrated SQL. |
| `--project` | SHIPS project to receive classified payload files. |
| `--token-map` | Applies any remaining literal database to token substitutions. |

6. Validate tokens across environments.

```powershell
python -m td_release_packager scan `
  --project C:\Projects\OMR `
  --all-envs `
  --fail-on-orphan
```

Arguments:

| Argument | Explanation |
|---|---|
| `--all-envs` | Validates every `config\env\*.conf` file. |
| `--fail-on-orphan` | Fails if a token is defined but never used. |

7. Build the release package.

```powershell
python -m td_release_packager process `
  --project C:\Projects\OMR `
  --skip-generate `
  --env DEV `
  --env-config C:\Projects\OMR\config\env\DEV.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --author "Data Engineering" `
  --description "Legacy SQL migrated to SHIPS"
```

Arguments:

| Argument | Explanation |
|---|---|
| `--skip-generate` | Use when all DDL is hand-authored and no generated view layer is needed. |
| `--env` | Target environment stamped into the package. |
| `--env-config` | Resolves `{{TOKEN}}` values for this package. |
| `--name` | Logical package name used in artifact names. |
| `--output` | Where the release-group directory is written. |
| `--author`, `--description` | Metadata visible in package context and reports. |

8. Verify and deploy.

```powershell
python -m td_release_packager verify --project C:\Projects\OMR

python -m td_release_packager deploy `
  C:\Projects\OMR\releases\DEV_OMR_BUILD_0001_20260519 `
  --dry-run `
  --host td-dev.company.net `
  --user ships_dba

python -m td_release_packager deploy `
  C:\Projects\OMR\releases\DEV_OMR_BUILD_0001_20260519 `
  --host td-dev.company.net `
  --user ships_dba `
  --streams 4
```

## Runsheet 2: Legacy Codebase With No Substitution Script

Use this when the source has legacy placeholders but no reliable substitution
script, or when you are not sure what kind of source you have.

### Steps

1. Ask SHIPS to classify the onboarding path.

```powershell
python -m td_release_packager onboard `
  --source C:\Legacy\sql `
  --env DEV
```

Arguments:

| Argument | Explanation |
|---|---|
| `--source` | Raw legacy SQL source directory to inspect. |
| `--env` | Environment name used in the recommended commands. |

2. If SHIPS reports legacy placeholders, auto-discover them.

```powershell
python -m td_release_packager import-legacy `
  --scan-source C:\Legacy\sql `
  --env DEV `
  --output-dir C:\Projects\OMR\config
```

Arguments:

| Argument | Explanation |
|---|---|
| `--scan-source` | Walks the source tree and discovers `$VAR`, `${VAR}`, and `&&VAR&&` placeholders. |
| `--env` | Environment name for the generated `env\DEV.conf`. |
| `--output-dir` | Writes generated config and `scan_report.md`. |

3. Fill in generated values in `C:\Projects\OMR\config\env\DEV.conf`.

4. Preview and apply the generated migration.

```powershell
python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\tokenise.conf `
  --source C:\Legacy\sql `
  --dry-run

python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\tokenise.conf `
  --source C:\Legacy\sql
```

5. Harvest, validate, package, and deploy using Runsheet 1 steps 5 through 8.

## Runsheet 3: Hand-Coded Untokenised DDL/DCL/DML

Use this when developers wrote SQL with literal database names such as
`A_D01_OMR_STD.Customer`, and you want SHIPS to convert those names into tokens.

### Steps

1. Create the project.

```powershell
python -m td_release_packager scaffold `
  --name OMR `
  --output C:\Projects
```

2. Generate a token map for review.

```powershell
python -m td_release_packager harvest `
  --source C:\Work\omr-sql `
  --project C:\Projects\OMR `
  --generate-token-map `
  --env-prefix A_D01
```

Arguments:

| Argument | Explanation |
|---|---|
| `--generate-token-map` | Scans source for literal database names and writes `config\token_map.conf`. |
| `--env-prefix A_D01` | Strips the DEV prefix when deriving token names. For example `A_D01_OMR_STD` becomes `{{OMR_STD}}`. |

3. Review and edit `C:\Projects\OMR\config\token_map.conf`.

Example:

```text
A_D01_OMR_STD={{OMR_STD}}
A_D01_OMR_SEM={{OMR_SEM}}
A_D01_OMR_APP={{OMR_APP}}
```

4. Fill in environment configs.

Example `config\env\DEV.conf`:

```text
SHIPS_ENV=DEV
ENV_PREFIX=A_D01
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
OMR_SEM={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
OMR_APP={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_APP
```

Example `config\env\PRD.conf`:

```text
SHIPS_ENV=PRD
ENV_PREFIX=P
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
OMR_SEM={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
OMR_APP={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_APP
```

5. Re-harvest with the reviewed token map.

```powershell
python -m td_release_packager harvest `
  --source C:\Work\omr-sql `
  --project C:\Projects\OMR `
  --token-map C:\Projects\OMR\config\token_map.conf
```

6. Generate missing grant files (via `ships fix`), inspect, and analyse dependencies.

```powershell
python -m td_release_packager fix `
  --project C:\Projects\OMR

python -m td_release_packager inspect `
  --project C:\Projects\OMR

python -m td_release_packager analyze `
  --project C:\Projects\OMR `
  --graph C:\Projects\OMR\reports\graph `
  --formats dot,json,mermaid
```

Arguments:

| Argument | Explanation |
|---|---|
| `ships fix` (default-on) | Runs the default-on subset of the fix registry — includes `grants_derivation` (writes inferred `.grt` files under `payload\database\DCL\inter_db\`) and `ddl_terminator`. Pass `--dry-run` for a preview. |
| `--graph` | Directory for dependency graph exports. |
| `--formats` | Graph export formats. |

7. Package for DEV.

```powershell
python -m td_release_packager package `
  --project C:\Projects\OMR `
  --env DEV `
  --env-config C:\Projects\OMR\config\env\DEV.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --author "Data Engineering" `
  --description "Hand-coded DDL/DCL/DML tokenised for DEV"
```

8. Deploy the release group.

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\DEV_OMR_BUILD_0001_20260519 `
  --dry-run `
  --host td-dev.company.net `
  --user ships_dba

python -m td_release_packager deploy `
  C:\Projects\OMR\releases\DEV_OMR_BUILD_0001_20260519 `
  --host td-dev.company.net `
  --user ships_dba
```

## Runsheet 4: Fast Developer Path For Untokenised Source

Use this for a development sandbox where you want a quick package and are
comfortable reviewing the generated payload after the run.

```powershell
python -m td_release_packager process `
  --project C:\Projects\OMR `
  --source C:\Work\omr-sql `
  --auto-tokenise `
  --env-prefix A_D01 `
  --skip-generate `
  --env DEV `
  --env-config C:\Projects\OMR\config\env\DEV.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --description "Developer sandbox package"
```

Arguments:

| Argument | Explanation |
|---|---|
| `--auto-tokenise` | Detects literal database names and applies derived tokens immediately. |
| `--env-prefix` | Controls token naming. |
| `--skip-generate` | Avoids generated view-layer steps for plain hand-coded SQL. |

After this run, inspect `payload\database\` and `ships.decisions.json` before
promoting the approach to shared environments.

## Runsheet 5: Generated Tokenised Codebase With Different Token Style

Use this when a generator already emits tokens, but they do not match your SHIPS
environment config naming convention. For example, the generator emits
`{{STD_DB}}` and your SHIPS project wants `{{OMR_STD}}`.

### Option A: Keep the Existing Token Names

Use this when the generated token names are acceptable and you only need a SHIPS
environment config.

1. Scaffold and harvest without a token map.

```powershell
python -m td_release_packager scaffold `
  --name OMR `
  --output C:\Projects

python -m td_release_packager harvest `
  --source C:\Generated\omr `
  --project C:\Projects\OMR
```

2. Bootstrap the environment config from referenced tokens.

```powershell
python -m td_release_packager bootstrap-env-config `
  --source C:\Projects\OMR `
  --env DEV `
  --output-dir C:\Projects\OMR\config `
  --force
```

Arguments:

| Argument | Explanation |
|---|---|
| `--source` | Already-harvested SHIPS project to scan for `{{TOKEN}}` references. |
| `--env` | Environment config to create. |
| `--output-dir` | Config root. SHIPS writes under `env\DEV.conf`. |
| `--force` | Overwrite the generated config if it already exists. |

3. Fill in the generated `DEV.conf`, then run:

```powershell
python -m td_release_packager process `
  --project C:\Projects\OMR `
  --skip-generate `
  --env DEV `
  --env-config C:\Projects\OMR\config\env\DEV.conf `
  --name OMR `
  --output C:\Projects\OMR\releases
```

### Option B: Rename Tokens To SHIPS Convention

Use this when the token names need to be converted before harvest. Existing
`{{TOKEN}}` markers are protected during normal token-map harvest, so use a
deliberate migration step for token renames.

1. Create a small migration script that maps the generator token style to the
SHIPS token style.

Example `C:\Projects\OMR\config\generated_token_migration.sed`:

```text
s/{{STD_DB}}/{{OMR_STD}}/g
s/{{SEM_DB}}/{{OMR_SEM}}/g
s/{{APP_DB}}/{{OMR_APP}}/g
```

2. Preview and apply the rename against the generated source.

```powershell
python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\generated_token_migration.conf `
  --source C:\Generated\omr `
  --dry-run

python -m td_release_packager migrate-source `
  --tokenise-config C:\Projects\OMR\config\generated_token_migration.conf `
  --source C:\Generated\omr
```

Arguments:

| Argument | Explanation |
|---|---|
| `--tokenise-config` | Any tokenisation config: literal `s/LHS/RHS/g` substitutions and/or regex `regex::PATTERN:=REPLACEMENT` rules with capture groups. |
| `--source` | Generated source directory to update in place. |
| `--dry-run` | Preview first so the token rename is auditable. |

3. Harvest the renamed generated source.

```powershell
python -m td_release_packager harvest `
  --source C:\Generated\omr `
  --project C:\Projects\OMR
```

4. Bootstrap or maintain environment configs using the SHIPS token names.

```powershell
python -m td_release_packager bootstrap-env-config `
  --source C:\Projects\OMR `
  --env DEV `
  --output-dir C:\Projects\OMR\config `
  --force
```

5. Scan all environments before packaging.

```powershell
python -m td_release_packager scan `
  --project C:\Projects\OMR `
  --all-envs `
  --show-map `
  --fail-on-orphan
```

6. Package and deploy as in Runsheet 3.

## Runsheet 6: Build Directly From GitHub Source

Use this when CI or an operator should fetch source from GitHub without a local
clone. This packages from repository source. It is different from deploying a
package asset downloaded from a GitHub Release.

### One-Command Process From GitHub

```powershell
$env:GITHUB_TOKEN = "<token-for-private-repos>"

python -m td_release_packager process `
  --project C:\Projects\OMR `
  --source-github myorg/omr-ddl `
  --source-ref release/2026.05 `
  --token-map C:\Projects\OMR\config\token_map.conf `
  --skip-generate `
  --env TST `
  --env-config C:\Projects\OMR\config\env\TST.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --strict
```

Arguments:

| Argument | Explanation |
|---|---|
| `--source-github myorg/omr-ddl` | Fetches source tarball from GitHub. |
| `--source-ref release/2026.05` | Fetches a branch, tag, or commit SHA. |
| `--github-token` or `GITHUB_TOKEN` | Required for private repositories. |
| `--token-map` | Applies your project token mapping during harvest. |
| `--strict` | Fails the run as soon as a stage has errors. Best for CI. |

The resolved GitHub commit SHA is stamped into package metadata unless you pass
`--commit` yourself.

### Package Existing GitHub-Compatible SHIPS Project

Use this when the repository is already a SHIPS project with `payload/` and
`config/` committed.

```powershell
python -m td_release_packager package `
  --source-github myorg/omr-ships-project `
  --source-ref v2.4.0 `
  --env PRD `
  --env-config C:\SecureConfig\OMR\PRD.conf `
  --name OMR `
  --output C:\Artifacts\OMR `
  --change-ref CHG0012345 `
  --asymmetric-key C:\Secrets\ships_signing_private.pem
```

Arguments:

| Argument | Explanation |
|---|---|
| `--source-github` | Fetches the SHIPS project source. |
| `--env-config` | Can point to a local secure config outside the repository. |
| `--change-ref` | Change ticket stamped into `context/ships.build.json`. |
| `--asymmetric-key` | Signs the package with an Ed25519 private key. |

## Runsheet 7: Deploy A Package Downloaded From A GitHub Release

Use this after CI has already built and published a SHIPS release group or
package zip as release assets.

### Steps

1. Download the release-group directory or package zip using your approved
artifact tooling.

2. Deploy a release group.

```powershell
python -m td_release_packager deploy `
  C:\Downloads\DEV_OMR_BUILD_0042_20260519 `
  --dry-run `
  --host td-dev.company.net `
  --user ships_dba `
  --logmech LDAP

python -m td_release_packager deploy `
  C:\Downloads\DEV_OMR_BUILD_0042_20260519 `
  --host td-dev.company.net `
  --user ships_dba `
  --logmech LDAP `
  --streams 4
```

3. Or deploy a single package zip.

```powershell
python -m td_release_packager deploy `
  C:\Downloads\DEV_OMR_BUILD_0042_20260519_01_main.zip `
  --host td-dev.company.net `
  --user ships_dba
```

Arguments:

| Argument | Explanation |
|---|---|
| `deploy <target>` | Points SHIPS at a release-group directory or zip. No manual extraction is needed. |
| `--dry-run` | Confirms trust, integrity, parseability, and pre-flight checks without executing SQL. |
| `--host`, `--user`, `--logmech` | Forwarded to the generated package deployer. |
| `--streams` | Runs independent wave members in parallel. System artefacts are packaged in `_01_prereqs` and run serially before any main-package waves; DCL remains serialised. |

## Runsheet 8: Explicit DDL, DCL, And DML Project

Use this when a release intentionally contains schema DDL, grants, and seed data.

### Source Layout

```text
C:\Work\customer-release\
  ddl\
    Customer.tbl
    CustomerStatus.viw
  dcl\
    app_role.grt
  dml\
    seed_customer_status.dml
```

The raw layout can be simple. Harvest classifies files into SHIPS payload
folders. If you curate files directly in a SHIPS project, place them under:

```text
payload\database\DDL\
payload\database\DCL\inter_db\
payload\database\DML\
```

### Steps

1. Harvest and tokenise.

```powershell
python -m td_release_packager harvest `
  --source C:\Work\customer-release `
  --project C:\Projects\Customer `
  --token-map C:\Projects\Customer\config\token_map.conf
```

2. Inspect with grant validation.

```powershell
python -m td_release_packager inspect `
  --project C:\Projects\Customer `
  --dcl-dir C:\Projects\Customer\payload\database\DCL\inter_db
```

Arguments:

| Argument | Explanation |
|---|---|
| `--dcl-dir` | Directory containing `.grt` inter-database grant files. Defaults to the project DCL path. |

3. Analyse deployment order and package.

```powershell
python -m td_release_packager analyze `
  --project C:\Projects\Customer `
  --overwrite

python -m td_release_packager package `
  --project C:\Projects\Customer `
  --env UAT `
  --env-config C:\Projects\Customer\config\env\UAT.conf `
  --name Customer `
  --output C:\Projects\Customer\releases
```

Ordering at deploy time:

| Asset type | Typical phase | Notes |
|---|---|---|
| Environment DCL/prereqs | Early serial phase | Databases/users/roles/profiles and required grants are serialised to avoid catalogue deadlocks. |
| DDL | Wave-ordered phase | Tables before dependent views/procedures/functions where dependencies are detected. |
| DCL grants | Serial DCL phase | Grants/revokes are applied after required objects exist. |
| DML | Late phase | Seed/reference data runs after target tables exist. DML is not automatically rollbackable. |

4. Dry-run and live deploy.

```powershell
python -m td_release_packager deploy `
  C:\Projects\Customer\releases\UAT_Customer_BUILD_0007_20260519 `
  --dry-run `
  --host td-uat.company.net `
  --user ships_dba `
  --continue-on-error

python -m td_release_packager deploy `
  C:\Projects\Customer\releases\UAT_Customer_BUILD_0007_20260519 `
  --host td-uat.company.net `
  --user ships_dba `
  --streams 4
```

## Runsheet 9: Promote The Same Build To Another Environment

Use this when you built and tested a package in DEV and want equivalent source
deployed to TST or PRD using different token values.

### Steps

1. Build for DEV normally.

```powershell
python -m td_release_packager package `
  --project C:\Projects\OMR `
  --env DEV `
  --env-config C:\Projects\OMR\config\env\DEV.conf `
  --name OMR `
  --output C:\Projects\OMR\releases
```

2. Build for TST with the same build number.

```powershell
python -m td_release_packager package `
  --project C:\Projects\OMR `
  --env TST `
  --env-config C:\Projects\OMR\config\env\TST.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --no-increment
```

Arguments:

| Argument | Explanation |
|---|---|
| `--no-increment` | Reuses the current build number for another environment package from the same source state. |
| `--env-config` | Changes only token resolution, not source DDL structure. |

3. Deploy the target environment package.

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\TST_OMR_BUILD_0001_20260519 `
  --host td-tst.company.net `
  --user ships_dba `
  --encryptdata true
```

## Runsheet 10: Failed Deployment Resume, Status, And Rollback

Use this during operations after a live deploy fails or is interrupted.

### Check Status

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  status `
  logs\.deploy_manifest_<id>.json
```

Arguments:

| Argument | Explanation |
|---|---|
| `status` | Reads manifest state without connecting to Teradata. |
| `logs\.deploy_manifest_<id>.json` | Manifest written by the deploy run under the package work directory. |

### Resume After Fixing The Root Cause

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  resume `
  logs\.deploy_manifest_<id>.json `
  --host td-prd.company.net `
  --user ships_dba
```

Arguments:

| Argument | Explanation |
|---|---|
| `resume` | Skips objects already marked `COMPLETED` and retries the remainder. |
| `--host`, `--user` | Required because resume connects to Teradata. |

### Technical Rollback

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  rollback `
  logs\.deploy_manifest_<id>.json `
  --host td-prd.company.net `
  --user ships_dba
```

For a wave-scoped rollback:

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  rollback `
  logs\.deploy_manifest_<id>.json `
  --wave 3 `
  --host td-prd.company.net `
  --user ships_dba
```

Notes:

| Item | Explanation |
|---|---|
| Technical rollback | Uses pre-deploy captures from the failed deployment. |
| DML | Row-level DML is not automatically rollbackable. Restore data using application-specific recovery steps. |
| SQLJ JAR / C external routines | Use feature rollback from a known-good tag when compiled binaries must be restored. |

## Runsheet 11: Package Rollback From A Deployment Manifest

Use this when you need to undo a package deployment using the rollback captures
created during that deployment. This is a package-level technical rollback: it
uses the manifest and the package's `_rollback` files to restore objects to
their pre-deploy state where SHIPS has enough information to do so.

This is different from feature rollback from a git tag. Package rollback undoes
the current deployment. Feature rollback builds and deploys an older known-good
version of the source.

### Step 1: Locate The Manifest

The manifest is written under the extracted package work area, usually below
`.ships-work`. If the deploy target was a release group, check beside that
release group.

```powershell
Get-ChildItem `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work `
  -Recurse `
  -Filter ".deploy_manifest_*.json"
```

The manifest path will look similar to:

```text
C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json
```

### Step 2: Inspect Rollback Eligibility

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  status `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json
```

Arguments:

| Argument | Explanation |
|---|---|
| `deploy <release_group>` | Points SHIPS at the same release group that was deployed. |
| `status` | Reads the manifest without connecting to Teradata. |
| Manifest path | The exact `.deploy_manifest_*.json` from the failed or completed deployment. |

Check the status output before rolling back:

| Manifest field | Meaning |
|---|---|
| `backup_table` | Table rollback can rename the backup table back into place. |
| `rollback_file` | SHOW DDL capture exists for restoring a previous object definition. |
| `COMPLETED` | Object was deployed and may be rollback-eligible. |
| `FAILED` / `SKIPPED` | Object may not need rollback, but inspect the report before deciding. |

### Step 3: Dry-Run The Package Rollback

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  rollback `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json `
  --dry-run
```

Arguments:

| Argument | Explanation |
|---|---|
| `rollback` | Runs the generated deployer's technical rollback mode. |
| `--dry-run` | Shows which objects would be rolled back and by what mechanism without connecting or changing Teradata. |

Review the output and the deployment report. Pay particular attention to DML and
binary object notes.

### Step 4: Run The Full Package Rollback

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  rollback `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json `
  --host td-prd.company.net `
  --user ships_dba
```

Arguments:

| Argument | Explanation |
|---|---|
| `--host` | Teradata host to connect to for rollback execution. |
| `--user` | Deploying user. It must have rights to drop, recreate, rename, or grant as required by the rollback actions. |

### Step 5: Roll Back One Failed Wave Only

Use this if waves 1 and 2 succeeded, wave 3 failed, and you want to undo only
objects changed in wave 3.

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  rollback `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json `
  --wave 3 `
  --host td-prd.company.net `
  --user ships_dba
```

Arguments:

| Argument | Explanation |
|---|---|
| `--wave 3` | Restricts rollback to objects assigned to wave 3. Earlier and later waves are left alone. |

### Step 6: Confirm Final State

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519 `
  status `
  C:\Projects\OMR\releases\PRD_OMR_BUILD_0012_20260519\.ships-work\PRD_OMR_BUILD_0012_20260519_01_main\logs\.deploy_manifest_20260519_101530.json
```

Expected rollback outcomes:

| Object type | Rollback behavior |
|---|---|
| Existing table | New table is dropped and backup table is renamed back when a backup was captured. |
| New table | Created table is dropped. |
| Existing view, macro, SQL procedure, SQL function | Current object is dropped or replaced using captured SHOW DDL. |
| New view, macro, SQL procedure, SQL function | Created object is removed. |
| DCL | Rollback support depends on captured deploy intent and generated rollback action. Review the report. |
| DML | Not row-level rollbackable. Use application-specific undo scripts or restore procedures. |
| SQLJ JAR / C external routine | Technical rollback cannot reliably restore binary content. Use feature rollback from a known-good tag. |

## Runsheet 12: Feature Rollback From A Git Tag

Use this when the correct recovery is to redeploy a previous known-good source
version rather than undoing only the failed deployment.

```powershell
python -m td_release_packager rollback `
  --to-tag v2.3.1 `
  --project C:\Projects\OMR `
  --env PRD `
  --env-config C:\Projects\OMR\config\env\PRD.conf `
  --name OMR `
  --output C:\Projects\OMR\releases `
  --on-drift continue `
  --description "Rollback to v2.3.1"
```

Arguments:

| Argument | Explanation |
|---|---|
| `--to-tag` | Git tag containing the known-good source. |
| `--project` | Current project directory containing `.build_counter` and the git repository. |
| `--env-config` | Current environment values. Token values come from today, not from the old tag. |
| `--on-drift continue` | Recommended for rollback because the goal is to restore the tagged schema as authoritative. |

Then deploy the generated rollback release group:

```powershell
python -m td_release_packager deploy `
  C:\Projects\OMR\releases\PRD_OMR_ROLLBACK_v2.3.1_BUILD_0013_20260519 `
  --host td-prd.company.net `
  --user ships_dba `
  --on-drift continue
```

## Pre-Handoff Checklist

Run this before handing a release group to a DBA:

```powershell
python -m td_release_packager scan `
  --project C:\Projects\OMR `
  --all-envs `
  --fail-on-orphan

python -m td_release_packager explain `
  --project C:\Projects\OMR `
  --command process

python -m td_release_packager verify `
  --project C:\Projects\OMR
```

Checklist:

| Check | Expected result |
|---|---|
| Token scan | No undefined tokens for target environments. |
| Inspect | No ERROR severity lint, grant, hierarchy, or security findings unless explicitly waived. |
| Analyse | `_waves.txt` exists or type-based fallback is acceptable. |
| Package | Release group exists under `releases\`. |
| Trust | Package label is `READY` or an approved `READY-WITH-CAVEATS`. |
| DBA command | Uses `python -m td_release_packager deploy <release-group-or-zip> ...`; no manual extraction required. |

## Common Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `--source and --source-github are mutually exclusive` | Both local and GitHub source were supplied. | Choose one source input. |
| Token appears unchanged in deployed DDL | Missing `--env-config` value or mismatched token name. | Run `scan --project <project> --env-config <conf> --show-map`. |
| Hardcoded database names remain after harvest | Missing or incomplete `config/tokenise.conf`. | Author `config/tokenise.conf` via the SHIPS Navigator wizard (`tools/navigator/ships-navigator.html`) or by hand (see `examples/callcentre/config/tokenise.conf`), then re-harvest. (Legacy: `--generate-token-map` + `--token-map` still works — see [#388](https://github.com/earthshiner/teradata-ships/issues/388).) |
| Package deploys to wrong environment | Wrong package or wrong `--env-config` at build time. | Check `context/ships.build.json` inside the package and rebuild for the target environment. |
| Privilege preflight fails | Deploying user lacks required CREATE/DROP/GRANT rights. | Use the generated grant script in the deployment report, then resume. |
| DML needs rollback | SHIPS does not capture row-level undo for DML. | Use application recovery scripts or restore from backup; avoid irreversible DML in high-risk deploys. |
| Release group has multiple zips | Environment prereqs or application prereqs were split out. | Deploy the release-group directory; SHIPS reads `release_group.json` and runs the required packages in order. |


## System artefacts in split packages

When SHIPS creates a split release group, `00_system` payload is treated as prerequisite payload and is placed in the `_01_prereqs` archive, not `_02_main`. This keeps system-level objects such as roles, profiles, maps, authorizations, and foreign servers ahead of role grants and other main-package DCL/DDL waves.

The deployment order is therefore:

1. `_00_environment_prereqs` — DBA-reviewed external parent containers, when required.
2. `_01_prereqs` — `00_system` followed by `01_pre_requisites`, executed serially.
3. `_02_main` — DCL, DDL, DML, and post-install payload, using dependency waves where available.
