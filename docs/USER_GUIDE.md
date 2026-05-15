# SHIPS User Guide
### For SQL Developers

---

## The short version

You write SQL the way you always have. SHIPS takes that SQL, figures out what it is, puts it in the right place, replaces the hardcoded database names with environment tokens, validates it, and hands the DBA a self-contained package they can deploy to any environment without asking you a single question.

That's it. The rest of this guide is how.

---

## What changes for you

Here is the honest before-and-after.

| Before | With SHIPS |
|---|---|
| Email scripts to the DBA and hope for the best | Hand off a package with a trust score — the DBA knows exactly what they're deploying |
| "Hardcode DEV database names and manually fix for PRD" | Write `{{MY_DATABASE}}` once; the package resolves it per environment |
| One giant `create_everything.sql` | One file per object — SHIPS enforces it and names the file for you |
| No idea what deployed where | `decisions.json` and `context/ships.build.json` tell you what ran, when, from what source |
| "The DBA handles deployment" | You still just write SQL — SHIPS is what turns your SQL into the package the DBA deploys |
| Fix forward — patch live DDL | Fix in source, re-harvest, re-package — the audit trail is intact |

**What you do not have to change:** how you write SQL. Your CREATE TABLE, REPLACE VIEW, REPLACE PROCEDURE — all fine as-is. SHIPS reads them and figures the rest out.

---

## Installation

```bash
pip install ships   # or however your organisation distributes it
python -m td_release_packager --version
```

See `docs/INSTALLATION.md` for organisation-specific setup.

---

## The five-minute start

If you want to see SHIPS work before reading anything else:

```bash
# 1. Create a project
python -m td_release_packager scaffold --name MyProject --output C:\Projects

# 2. Drop your SQL files in a folder, then harvest them
python -m td_release_packager harvest \
    --source C:\MySQL \
    --project C:\Projects\MyProject

# 3. Run the full pipeline (inspect + analyse + optional package)
python -m td_release_packager process \
    --project C:\Projects\MyProject \
    --source C:\MySQL
```

SHIPS will classify everything, tell you what it found and what needs fixing, and leave you with a project ready to package.

---

## Core concepts

### One object, one file

SHIPS requires each DDL object to live in its own file. If you have a `create_everything.sql` that creates five tables, SHIPS splits it automatically on first harvest. Each table gets its own file, named for the object it contains.

**You do not have to do this split yourself.** Harvest handles it.

### Eponymous naming

Every file in the payload is named `Database.ObjectName.ext`. This makes the payload human-readable — you can see exactly what is in it without opening a file.

Examples:
```
{{STD_DATABASE}}.Customer.tbl
{{STD_DATABASE}}.v_ActiveCustomers.viw
{{STD_DATABASE}}.sp_ProcessOrder.spl
```

The `{{STD_DATABASE}}` part is a token — a placeholder that gets resolved to the real database name at package time. This is how SHIPS makes the same source work across DEV, TST, and PRD.

### Tokens replace hardcoded database names

If your SQL says `FROM A_D01_OMR_STD.Customer`, that is a hardcoded DEV database name. It will not work in PRD (`P_OMR_STD.Customer`) or TST (`T_OMR_STD.Customer`).

SHIPS detects this and can replace it automatically with `{{OMR_STD}}` — a token that each environment resolves to its own value. You write SQL once; SHIPS handles environment promotion.

**The token map** (`config/token_map.conf`) declares the mapping:
```
A_D01_OMR_STD={{OMR_STD}}
```

**The environment config** (`config/env/DEV.conf`) declares the resolution:
```
OMR_STD=A_D01_OMR_STD
```

In PRD: `OMR_STD=P_OMR_STD`. Same source, different value, no manual find-and-replace.

### The deployment phase

The DBA does not get your SQL files. They get a **package** — a `.zip` that contains:
- Fully resolved DDL (tokens replaced, no `{{...}}` remaining)
- A deployment manifest listing every object, its type, and its deployment strategy
- A SHA-256 fingerprint that proves the package was not tampered with
- A `deploy.py` script — the DBA runs `python deploy.py --host myserver --user dbc`
- Three agent-facing context artefacts (`context/ships.context.json`, `context/ships.manifest.json`, `context/ships.handoff.json`) for autonomous agent and CI/CD handoff

You never touch the deployment. You produce the package; the DBA runs it.

---

## How to: start a new project from scratch

### Step 1 — Scaffold

```bash
python -m td_release_packager scaffold \
    --name OMR \
    --output C:\Projects \
    --environments DEV,TST,PRD
```

This creates the project directory with the right folder structure and an initial `ships.yaml`, three empty `config/env/` files, and a `.build_counter`.

```
OMR/
    config/
        env/
            DEV.conf
            TST.conf
            PRD.conf
        inspect.conf
        token_map.conf      ← not yet created; harvest generates it
    payload/database/
        pre-requisites/databases/
        pre-requisites/users/
        DDL/tables/
        DDL/views/
        DDL/procedures/
        ...
    releases/
    ships.yaml
    .build_counter
```

### Step 2 — Write your SQL normally

Drop your SQL files in a source folder. Name them whatever you like. Use your existing hardcoded database names. You'll deal with tokenisation in a moment.

Example source folder:
```
C:\MySQL\
    create_customer_table.sql
    create_order_table.sql
    customer_view.sql
    sp_ProcessOrder.sql
```

### Step 3 — First harvest (detect mode)

Run harvest without a token map to see what SHIPS finds:

```bash
python -m td_release_packager harvest \
    --source C:\MySQL \
    --project C:\Projects\OMR
```

SHIPS will:
- Classify every file (TABLE, VIEW, PROCEDURE, etc.)
- Split multi-statement files
- Inject `MULTISET` on tables that are missing it
- Report hardcoded database names it found
- Report any files it could not classify

Read the output. If there are unclassified files, they usually have syntax SHIPS doesn't recognise — open them and check they are valid Teradata DDL.

### Step 4 — Generate a token map

If your SQL uses hardcoded database names (most legacy SQL does), generate a mapping:

```bash
python -m td_release_packager harvest \
    --source C:\MySQL \
    --project C:\Projects\OMR \
    --generate-token-map \
    --env-prefix A_D01
```

`--env-prefix A_D01` tells SHIPS to strip `A_D01_` from the front of database names when deriving token names. So `A_D01_OMR_STD` becomes `{{OMR_STD}}`.

If your databases do not have an environment prefix (e.g. they are global shared databases), omit `--env-prefix`. The full name becomes the token: `SHARED_DB` → `{{SHARED_DB}}`.

This writes `config/token_map.conf`:
```
# 14 references across 8 files
A_D01_OMR_STD={{OMR_STD}}

# 9 references across 6 files
A_D01_OMR_SEM={{OMR_SEM}}
```

**Review the token map.** If a name is a genuine constant (e.g. `DBC`) and should not be tokenised, remove its entry. If you want a different token name, rename it. This file goes in Git — the whole team shares one mapping.

### Step 5 — Fill in the environment configs

Open `config/env/DEV.conf` and fill it in:

```properties
SHIPS_ENV=DEV
ENV_PREFIX=A_D01
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
OMR_SEM={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
```

The `{{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD` pattern means you define the prefix once and everything else composes from it. Change `ENV_PREFIX` for each environment:

```properties
# config/env/TST.conf
SHIPS_ENV=TST
ENV_PREFIX=T
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
OMR_SEM={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
```

Same source, different values. DEV gets `A_D01_OMR_STD`; TST gets `T_OMR_STD`.

### Step 6 — Re-harvest with the token map

```bash
python -m td_release_packager harvest \
    --source C:\MySQL \
    --project C:\Projects\OMR \
    --token-map config/token_map.conf
```

Now every occurrence of `A_D01_OMR_STD` in the payload becomes `{{OMR_STD}}`.

### Step 7 — Run the full pipeline

```bash
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\MySQL \
    --token-map config/token_map.conf \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR
```

This runs harvest → generate (view layer, if applicable) → inspect → analyse → package in one command, writing the full audit trail to `decisions.json`.

The result is a release-group directory under `releases/`. The directory contains one or more package archives, checksum sidecars, `release_group.json`, and a group-level `README.txt` ready for the DBA.

---

## How to: one command for everything (auto-tokenise)

If you do not want to manually review the token map, use `--auto-tokenise`:

```bash
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\MySQL \
    --auto-tokenise \
    --env-prefix A_D01 \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR
```

SHIPS detects the literal database names, derives tokens automatically, applies them, and proceeds straight to inspect and package. No intermediate `token_map.conf` review step.

Use `--auto-tokenise` when speed matters more than reviewing every token — for example, in a first-pass onboarding or a developer sandbox. For production pipelines, generate the token map once, review it, commit it, and use `--token-map` on subsequent runs.

---

## How to: onboard an existing codebase with the wizard

For a first-time assessment of any source directory, the onboarding wizard scans your SQL files and recommends the exact command sequence to follow:

```bash
python -m td_release_packager onboard --source C:\Legacy\SQL --env DEV
```

SHIPS inspects the source for:
- SQL/DDL file count
- Legacy placeholder markers (`$VAR`, `&&VAR&&`) from sed-based build harnesses
- Existing SHIPS `{{TOKEN}}` markers
- Whether an env config is already present

It then prints a tailored recommended path — one of four tracks:

| Detected state | Recommended path |
|---|---|
| **Legacy markers** (`$VAR`, `&&VAR&&`) | `import-legacy` → `migrate-source` → `harvest` |
| **SHIPS tokens, no env config** | `bootstrap-env-config` → fill values → `harvest` |
| **SHIPS tokens + env config** | `harvest` directly |
| **No markers at all** (hardcoded names) | `harvest` → `decompose-names` → `bootstrap-env-config` |

To run the first automatable step immediately after the assessment, add `--auto`:

```bash
python -m td_release_packager onboard --source C:\Legacy\SQL --env DEV --auto
```

The wizard is read-only without `--auto` — it never modifies your source files, only prints recommendations.

## How to: onboard an existing codebase manually

You have years of scripts in a folder. They have hardcoded database names, inconsistent naming, maybe multiple statements per file. Here is the fastest path to a SHIPS project.

### Option A — Auto-tokenise in one pass

```bash
# 1. Scaffold
python -m td_release_packager scaffold --name OMR --output C:\Projects

# 2. Fill in config/env/DEV.conf first (SHIPS_ENV, ENV_PREFIX, etc.)

# 3. Process with auto-tokenise
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\Legacy\SQL \
    --auto-tokenise \
    --env-prefix A_D01 \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR
```

Check the output. Inspect errors tell you what to fix. Unclassified files tell you what SHIPS could not understand.

### Option B — Gradual review

If you want to review the token map before applying it (recommended for large codebases or production onboarding):

```bash
# Step 1 — detect mode
python -m td_release_packager harvest \
    --source C:\Legacy\SQL \
    --project C:\Projects\OMR \
    --generate-token-map --env-prefix A_D01

# Step 2 — review config/token_map.conf, edit it, commit it

# Step 3 — apply
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\Legacy\SQL \
    --token-map config/token_map.conf \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR
```

### What about files SHIPS could not classify?

Harvest will list them under "Unclassified files (manual review needed)." Common causes:

| Symptom | Cause | Fix |
|---|---|---|
| File listed as unclassified | File contains no classifiable DDL/DML | Open it; if it's not object DDL (e.g. it's a session banding script or a BTEQ macro), exclude it from `--source` |
| `CREATE TABLE` classified as unknown | Missing database qualifier (`CREATE TABLE my_table` with no `DB.`) | Add the qualifier; SHIPS requires it |
| Procedure not found | Stored procedure uses non-standard header comment that hides the `CREATE PROCEDURE` | Move the CREATE statement above the comment block |

---

## How to: add a new object to an existing project

### The short version

Add your SQL file to the source directory and re-run harvest. SHIPS handles the rest.

```bash
# Add your new SQL to the source folder, then:
python -m td_release_packager harvest \
    --source C:\MySQL \
    --project C:\Projects\OMR \
    --token-map config/token_map.conf
```

The clean-payload mode (default) wipes harvest-managed files and rebuilds from source. Your new object appears in the right place; removed objects disappear cleanly. No manual payload editing.

### What if my new object references a new database?

If you have added a reference to a database that is not yet in `config/token_map.conf`:

1. Harvest will list it under "Token candidates"
2. Add an entry to `config/token_map.conf`: `NEW_DB={{NEW_DB}}`
3. Add `NEW_DB=<actual_name>` to each `config/env/*.conf`
4. Re-run harvest with `--token-map`

---

## How to: fix inspect errors

Run inspect to see what SHIPS's linter found:

```bash
python -m td_release_packager inspect --source C:\Projects\OMR
```

### Common errors and fixes

**`db_qualifier` — ERROR**
```
OMR/payload/database/DDL/tables/Customer.tbl: missing database qualifier
```
Your `CREATE TABLE` statement has no database prefix. Change `CREATE TABLE Customer` to `CREATE TABLE {{STD_DATABASE}}.Customer`. Every object must be qualified.

**`type_suffix` — ERROR**
```
OMR/payload/database/DDL/views/VW_ActiveCustomers.viw: type suffix in object name
```
Object names like `VW_CustomerSales` or `SP_ProcessOrder` encode the type in the name. SHIPS enforces the encoding in the file extension (`.viw`, `.spl`) — not the name. Rename to `ActiveCustomers.viw` and `ProcessOrder.spl`. The type is obvious from the extension.

**`hardcoded_name` — WARNING**
```
OMR/payload/database/DDL/tables/{{STD_DATABASE}}.Customer.tbl: hardcoded reference to A_D01_OMR_SEM
```
The file body still contains a literal database name. Add an entry to `token_map.conf` and re-run harvest with `--token-map`. This warning becomes an error if you use `--strict`.

**`deploy_intent` — WARNING**
```
OMR/payload/database/DDL/views/{{STD_DATABASE}}.MyView.viw: Uses REPLACE — consider CREATE instead
```
`REPLACE` is permitted and fully supported by the deployer, which captures a pre-flight rollback snapshot before executing either verb. `CREATE` is the preferred SHIPS convention because it makes deployment intent explicit and lets the deployer own idempotency via DROP+CREATE. This rule is advisory (WARNING) by default. To enforce CREATE-only across your project, set `deploy_intent=ERROR` in `inspect.conf`.

**`eponymous` — WARNING**
```
OMR/payload/database/DDL/tables/{{STD_DATABASE}}.Orders.tbl: filename says Orders but DDL creates {{STD_DATABASE}}.Customers
```
The file's name does not match the object it contains. This usually means harvest placed the file before tokenisation, or the source file was renamed without updating the DDL. Open the file and correct the object name.

### Suppressing rules you do not want

Edit `config/inspect.conf` in your project:

```properties
# Set to OFF to silence, WARNING for advisory, ERROR to block packaging
keyword_case=OFF
leading_commas=OFF
deploy_intent=WARNING
db_qualifier=ERROR
```

---

## How to: promote a build from DEV to TST

You have a DEV build that has been approved. You want to produce the TST package from the same source, same build number.

```bash
python -m td_release_packager package \
    --source C:\Projects\OMR \
    --env TST \
    --name OMR \
    --env-config config/env/TST.conf \
    --output releases/ \
    --no-increment
```

`--no-increment` reuses the current build counter value — the same build number, different environment. The package is written under a release-group directory such as `releases/TST_OMR_BUILD_0005_<timestamp>/`, with the package archive named `TST_OMR_BUILD_0005_<timestamp>_01_main.zip`.

The token resolution is now driven by `config/env/TST.conf`. No source changes, no manual find-and-replace. The same `{{OMR_STD}}` in the DDL resolves to `T_OMR_STD` instead of `A_D01_OMR_STD`.

---


### Release group directory layout

Every package build writes to a release-group directory, even when there is only one package archive.

```text
releases/
    DEV_OMR_BUILD_0005_20260509/
        DEV_OMR_BUILD_0005_20260509_01_main.zip
        DEV_OMR_BUILD_0005_20260509_01_main.zip.sha256
        release_group.json
        README.txt
```

When SHIPS detects environment prerequisites and application prerequisites, the same directory may contain ordered package roles:

```text
releases/
    DEV_GCFR_BUILD_0012_20260515144900/
        DEV_GCFR_BUILD_0012_20260515144900_00_environment_prereqs.zip
        DEV_GCFR_BUILD_0012_20260515144900_01_prereqs.zip
        DEV_GCFR_BUILD_0012_20260515144900_02_main.zip
        release_group.json
        README.txt
```

The group directory is the unit to hand to a DBA, CI/CD job, or deployment agent.

## How to: verify a package is ready before handing it off

```bash
python -m td_release_packager verify --project C:\Projects\OMR
```

This checks:
- The archive file exists on disk
- No package warnings were recorded
- The package stage completed successfully
- If the build auto-split (contains a prereqs companion archive), the companion exists too

Exit code 0 = READY. Exit code 1 = something to fix first.

Typical output:
```
================================================================
  SHIPS Verify — Package Readiness
================================================================
  Release group: releases/DEV_OMR_BUILD_0005_20260509/
  Archive:       releases/DEV_OMR_BUILD_0005_20260509/DEV_OMR_BUILD_0005_20260509_01_main.zip
  Environment: DEV
  Build:       5
  Files:       47
  Tokens:      134 substitutions

  Checklist:
    ✓ Archive exists on disk
    ✓ No package issues recorded
    ✓ Package stage status: success

  ✓ Verdict: READY
================================================================
```

---

## How to: explain what the last pipeline run did

```bash
python -m td_release_packager explain --project C:\Projects\OMR
```

This reads `decisions.json` and produces a human-readable report of the last run: stage statuses, key outputs, and any issues. Use it to see what happened before deciding whether to promote.

To see the last `process` run specifically:
```bash
python -m td_release_packager explain --project C:\Projects\OMR --command process
```

---

## How to: work with stored procedures

Stored procedures are classified as `PROCEDURE` (`.spl` extension). SHIPS handles them like any other DDL object with a few considerations.

### Procedures that call other procedures

If `sp_ProcessOrder` calls `sp_ValidateCustomer`, the analyser will detect this dependency and ensure `sp_ValidateCustomer` deploys first. You do not need to document this manually.

### Procedures that reference external databases

If your procedure references a database not in your payload (e.g. a system database or another project's database), the analyser will flag it as an external dependency. This is normal — SHIPS notes it but does not block packaging. The DBA will need that database to exist on the target server.

### Java stored procedures and JARs

If your procedure uses `LANGUAGE JAVA`, SHIPS expects the JAR file to be present alongside the SQL. On harvest, SHIPS copies the JAR into the payload next to the install script. Ensure your source directory contains both the `.sql` install script and the `.jar` binary.

---

## How to: work with DML (INSERT / UPDATE / DELETE)

Seed data and reference data loads are supported as DML files (`.dml` extension).

SHIPS places them in `payload/database/DML/` and deploys them after all DDL — the target tables exist before the data loads.

```sql
-- ReferenceData.dml
INSERT INTO {{STD_DATABASE}}.AmortisationType (code, name)
VALUES ('FRM', 'Fixed Rate Mortgage');

INSERT INTO {{STD_DATABASE}}.AmortisationType (code, name)
VALUES ('ARM', 'Adjustable Rate Mortgage');
```

Notes:
- Semicolons inside string literals (e.g. in description columns) are handled correctly — SHIPS will not split on them.
- Multiple DML statements that target different tables in a specific order should be kept in a single file and marked with `-- MULTI_TABLE_DML` at the top — this preserves the statement order.

---

## How to: work with grants

SHIPS infers grants from your DDL. If you have a view `{{STD_DATABASE}}.CustomerSummary`, SHIPS can generate a grant to the appropriate role automatically via the `generate` command.

If you want to manage grants explicitly, place them as `.dcl` files in `payload/database/DCL/inter_db/`.

The inspect rule `skip_grants` is `true` by default in the `process` command — grant validation is advisory. In a stricter pipeline, run inspect with `--fix-grants` to generate missing grants.

---

## The daily workflow

Once a project is set up, your day-to-day is:

1. **Write or modify SQL** in your source folder
2. **Run process** to validate and package:
   ```bash
   python -m td_release_packager process \
       --project C:\Projects\OMR \
       --source C:\MySQL \
       --token-map config/token_map.conf \
       --env DEV \
       --env-config config/env/DEV.conf \
       --name OMR
   ```
3. **Check the output** — inspect errors must be resolved before a package is produced
4. **Run verify** if handing off to a DBA:
   ```bash
   python -m td_release_packager verify --project C:\Projects\OMR
   ```
5. **Hand the release-group directory** from `releases/` to the DBA

That is the complete cycle. Steps 2–4 take seconds for most projects.

---

## The `process` command — all stages in one

`process` is the recommended way to run the pipeline. It runs all stages in sequence under one `decisions.json` run entry:

```
harvest → generate → inspect → analyse → [package]
```

```bash
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\MySQL \
    --token-map config/token_map.conf \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR \
    [--strict]              # abort on first error (default: continue + summarise)
    [--skip-generate]       # skip view-layer generation
    [--pause]               # pause after each stage for interactive review
    [--auto-tokenise]       # detect and apply tokens in one pass
```

**Package stage is optional.** If you omit `--env`, `--env-config`, and `--name`, the pipeline stops after analyse. Use this for a quick lint-and-dependency-check without building a package.

### `--strict` vs default

Without `--strict` (default, developer mode): all stages run; errors are summarised at the end. You see everything that needs fixing in one pass.

With `--strict` (platform mode): the pipeline aborts the moment any stage reports an error. Use this in CI/CD pipelines where a partial run is worse than no run.

### `--pause` for supervised runs

```bash
python -m td_release_packager process ... --pause
```

After each stage, SHIPS pauses and asks:

```
  ── Pause after inspect [⚠ warning] ──
  Continue? [Y/n/q]
```

Press Enter or `y` to continue, `n` to abort, `q` to quit cleanly. Suppressed automatically when running in CI (`CI=true`) or when output is not a terminal.

---

## Package Trust Report

Every package built with `ships package` or `ships process` (with packaging enabled) carries a **Trust Report** in `context/ships.build.json`. It tells you — and any agent or CI pipeline — whether the package is safe to promote.

The trust label is also surfaced in the agent-facing context artefacts (`context/ships.context.json`, `context/ships.manifest.json`, `context/ships.handoff.json`) that are written alongside `context/ships.build.json` into every package. Agents read the context artefacts first; the full trust detail is in `context/ships.build.json`.

### Labels

| Label | Meaning |
|---|---|
| **READY ✓** | All signals pass — package is clean |
| **READY-WITH-CAVEATS ⚠** | Warnings present (lint warnings, provenance missing) — deploy with awareness |
| **BLOCKED ✗** | At least one critical signal failed — fix before deploying |

The label is printed in the `ships package` banner and again in `deploy.py` before any database connection is opened.

### Signals (Phase 1)

| Signal | Fails when |
|---|---|
| `inspect_token_format` | A `{{TOKEN}}` marker is malformed |
| `inspect_lint` | An inspect ERROR-severity lint violation exists |
| `inspect_grants` | Grant drift is detected at ERROR level |
| `provenance_complete` | `context/ships.provenance.json` is absent from the payload |

### What to do when BLOCKED

Run `ships inspect` to see the specific errors, fix them in source, and re-run the pipeline. The Trust Report reads `decisions.json` — inspect must run before package for the signals to be accurate.

---

## Verifying a package before handoff

```bash
ships verify --project C:\Projects\OMR
```

Checks: archive exists on disk, no package warnings, stage succeeded, Trust label not BLOCKED. Exit 0 = READY. Use this as your final gate before sending the package to the DBA.

---

## Reading prior run results

```bash
# Show the last pipeline run in a human-readable format
ships explain --project C:\Projects\OMR --command process
```

Shows stage statuses, key outputs, and all issues. Use before promoting to confirm no blocking issues remain.

---

## Command reference

### `ships scaffold`

Create a new project structure.

```bash
python -m td_release_packager scaffold \
    --name OMR \
    --output C:\Projects \
    --environments DEV,TST,PRD
```

| Flag | Required | Description |
|---|---|---|
| `--name` | Yes | Project name (used as directory name) |
| `--output` | No | Parent directory (default: current) |
| `--environments` | No | Comma-separated env names (default: DEV,TST,PRD) |
| `--repair` | No | Add missing directories/files without overwriting |

---

### `ships harvest`

Import and tokenise raw DDL.

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project C:\Projects\OMR \
    --token-map config/token_map.conf
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes* | Source directory of raw DDL (*not required with `--reconcile`) |
| `--project` | Yes | Target project directory |
| `--token-map` | No | Path to `token_map.conf` — applies substitutions |
| `--generate-token-map` | No | Scan and write `config/token_map.conf` |
| `--env-prefix` | No | Prefix to strip when deriving token names |
| `--auto-tokenise` | No | Detect and apply tokens in one pass (no manual review) |
| `--keep-existing` | No | Overlay new files without wiping payload first |
| `--force` | No | Overwrite collisions in overlay mode |

---

### `ships generate`

Generate view-layer DDL from harvested tables (SHIPS topology projects).

```bash
python -m td_release_packager generate \
    --source C:\Projects\OMR \
    --modules DOM,SEM
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Project directory containing harvested payload |
| `--modules` | No | Comma-separated modules to generate (default: all) |
| `--dry-run` | No | Validate without writing files |

---

### `ships inspect`

Lint payload DDL against configurable rules.

```bash
python -m td_release_packager inspect --source C:\Projects\OMR
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Project directory to inspect |
| `--config` | No | Path to `inspect.conf` (default: auto-detect in project) |
| `--strict` | No | Promote all WARNING rules to ERROR |
| `--skip-grants` | No | Skip grant validation |
| `--fix-grants` | No | Re-generate missing grant files |

---

### `ships analyse`

Build the dependency graph and wave ordering.

```bash
python -m td_release_packager analyze --source C:\Projects\OMR
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Project directory to analyse |
| `--output` | No | Output path for `_waves.txt` (default: `<source>/_waves.txt`) |
| `--overwrite` | No | Overwrite existing `_waves.txt` |

---

### `ships package`

Build a release archive for a specific environment.

```bash
python -m td_release_packager package \
    --source C:\Projects\OMR \
    --env DEV \
    --name OMR \
    --env-config config/env/DEV.conf \
    --output releases/
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Project directory |
| `--env` | Yes | Target environment (DEV / TST / PRD) |
| `--name` | Yes | Package name |
| `--env-config` | Yes | Path to environment `.conf` file |
| `--output` | No | Output directory (default: current) |
| `--no-increment` | No | Reuse current build number (for promotion) |
| `--build-number` | No | Explicit build number |
| `--format` | No | `zip` or `tar.gz` (default: `zip`) |
| `--author` | No | Author metadata |
| `--description` | No | Release description |
| `--commit` | No | Git commit hash for traceability. Set automatically when using `--source-github`. |
| `--allow-dirty` | No | Build from a working tree with uncommitted changes. Stamps `source_dirty=true` in context/ships.build.json; Trust Report shows **READY-WITH-CAVEATS**. For development use only. |
| `--source-github OWNER/REPO` | No | Fetch DDL source directly from a GitHub repository. Mutually exclusive with `--source`. |
| `--source-ref REF` | No | Branch, tag, or commit SHA to fetch (default: `main`). Used with `--source-github`. |
| `--github-token TOKEN` | No | GitHub PAT for private repositories. Falls back to `GITHUB_TOKEN` env var. Public repositories work without a token. |

---

### `ships process`

Run the full pipeline in one command.

```bash
python -m td_release_packager process \
    --project C:\Projects\OMR \
    --source C:\MySQL \
    --token-map config/token_map.conf \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR
```

| Flag | Required | Description |
|---|---|---|
| `--project` | Yes | Project directory (must already be scaffolded) |
| `--source` | No | Raw DDL source directory (harvest is skipped if omitted) |
| `--token-map` | No | Token substitution map |
| `--auto-tokenise` | No | Detect and apply tokens in one pass |
| `--env-prefix` | No | Env prefix for auto-tokenise derivation |
| `--skip-generate` | No | Skip the generate stage |
| `--env` | No | Target environment (package stage requires this) |
| `--env-config` | No | Environment config file (package stage requires this) |
| `--name` | No | Package name (package stage requires this) |
| `--strict` | No | Abort on first stage error |
| `--pause` | No | Pause after each stage for interactive review |
| `--author` | No | Package author metadata |
| `--description` | No | Package description |
| `--commit` | No | Source commit hash. Set automatically when using `--source-github`. |
| `--source-github OWNER/REPO` | No | Fetch DDL source directly from a GitHub repository. Mutually exclusive with `--source`. |
| `--source-ref REF` | No | Branch, tag, or commit SHA to fetch (default: `main`). Used with `--source-github`. |
| `--github-token TOKEN` | No | GitHub PAT for private repositories. Falls back to `GITHUB_TOKEN` env var. |

---

### `ships scan`

Scan payload files for token references, validate against environment configs, and audit for orphan tokens.

```bash
# Basic — list all tokens found in the payload
python -m td_release_packager scan --source C:\Projects\OMR

# Validate against a single env config
python -m td_release_packager scan \
    --source C:\Projects\OMR \
    --env-config config/env/DEV.conf

# Sweep all environments in one pass (recommended pre-promotion check)
python -m td_release_packager scan \
    --source C:\Projects\OMR \
    --all-envs

# CI gate: fail if any token is undefined OR orphaned in any environment
python -m td_release_packager scan \
    --source C:\Projects\OMR \
    --all-envs \
    --fail-on-orphan

# Machine-readable output for agents and pipelines
python -m td_release_packager scan \
    --source C:\Projects\OMR \
    --all-envs \
    --format json
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Source project directory to scan |
| `--env-config FILE` | No | Validate tokens against this env `.conf` file. Mutually exclusive with `--all-envs`. |
| `--all-envs` | No | Discover every `*.conf` in `config/env/` and validate tokens against each. Shows per-environment status. Exits 1 if any env has undefined tokens. |
| `--show-map` | No | Print the full token → file reverse index: for each token, list every payload file that references it. Useful for blast-radius analysis before renaming a token. |
| `--format` | No | Output format: `text` (default, human-readable) or `json` (machine-readable, suitable for agent/CI consumption). |
| `--fail-on-orphan` | No | Exit 1 when any token is defined in the env config but never referenced in the payload (orphan token). Use as a CI gate to keep config files clean. |

**Recommended daily-driver pattern** — run this before every `ships package`:

```bash
ships scan --source C:\Projects\OMR --all-envs --fail-on-orphan
```

One command confirms every token resolves in every environment and no dead config entries have accumulated.

---

### `ships explain`

Human-readable report of a prior pipeline run from `decisions.json`.

```bash
python -m td_release_packager explain --project C:\Projects\OMR
python -m td_release_packager explain --project C:\Projects\OMR --command process
python -m td_release_packager explain --project C:\Projects\OMR --run-id 2026-05-09T14:30:00Z-abcd
```

| Flag | Required | Description |
|---|---|---|
| `--project` | Yes | Project directory |
| `--command` | No | Filter to last run of this command (e.g. `process`) |
| `--run-id` | No | Report a specific run by ID |

Exit 0 if the run status was success or warning. Exit 1 on error status or missing `decisions.json`.

---

### `ships verify`

Pre-deploy package readiness check.

```bash
python -m td_release_packager verify --project C:\Projects\OMR
```

| Flag | Required | Description |
|---|---|---|
| `--project` | Yes | Project directory |

Exit 0 = READY. Exit 1 = NOT READY. Checks: archive exists, no package warnings, stage status success.

---

### `ships onboard`

Scan a source directory and recommend the SHIPS onboarding path.

```bash
python -m td_release_packager onboard \
    --source C:\Legacy\SQL \
    --env DEV \
    [--auto]
```

| Flag | Required | Description |
|---|---|---|
| `--source` | Yes | Source directory to scan |
| `--env` | No | Target environment name (default: `DEV`) |
| `--auto` | No | Run the first automatable step immediately after assessment |

Prints a tailored recommendation and, with `--auto`, runs the first step automatically. Read-only without `--auto`.

---

### `ships keygen`

Generate an Ed25519 key pair for asymmetric package signing.

```bash
python -m td_release_packager keygen
python -m td_release_packager keygen --output-dir /etc/ships/keys
```

| Flag | Required | Description |
|---|---|---|
| `--output-dir` | No | Directory to write the key files (default: current directory) |

Writes `ships_signing_private.pem` (keep secret) and `ships_signing_public.pem`
(commit to repository). Run this once per project; rotate by running it again and
updating the CI secret and committed public key.

---

### `ships approve`

Generate a 4-eyes approval code for a package (second-operator sign-off).

```bash
ships approve /path/to/package.zip --signing-key /etc/ships/signing.key
```

The printed approval code is passed to the DBA who runs:

```bash
ships deploy /path/to/package/ --host myhost --user ships_dba --approval-code CODE
```

---

### `ships audit-grants`

Compare declared GRANT statements in a package against the live grant state in
Teradata, and report drift.

```bash
ships audit-grants /path/to/package_dir \
    --host myhost \
    --user ships_dba
```

Output categories:

| Category | Meaning |
|---|---|
| `MATCHED` | Grant declared in DCL and present in Teradata |
| `MISSING` | Grant declared in DCL but not present in Teradata |
| `UNDECLARED` | Grant present in Teradata but not in DCL |

Exit 0 = no drift. Exit 1 = drift detected. Use this as a post-deployment gate
or a standing compliance check.

---

### `ships decisions prune`

Remove old run entries from `decisions.json` to keep the audit file manageable over time.

```bash
python -m td_release_packager decisions prune \
    --project C:\Projects\OMR \
    --keep-runs 50

python -m td_release_packager decisions prune \
    --project C:\Projects\OMR \
    --keep-days 90
```

| Flag | Required | Description |
|---|---|---|
| `--project` | Yes | Project directory containing `decisions.json` |
| `--keep-runs N` | Either/or | Keep only the N most recent runs |
| `--keep-days N` | Either/or | Keep only runs from the last N days |
| `--dry-run` | No | Preview what would be pruned without writing changes |
| `--yes` | No | Skip the confirmation prompt (for CI use) |

Running without `--dry-run` or `--yes` shows a preview and prompts for confirmation. Always preview first on a large file.

---

### `ships decompose-names`

Decompose literal database names against the SHIPS naming grammar and emit a cascade-form `.conf` file.

Useful when onboarding a new codebase — pass your `token_map.conf` and SHIPS will try to infer the composition roots (`ENV_PREFIX`, `SHIPS_ENV`, `INSTANCE`, etc.) and write a starter `DEV.conf`.

```bash
python -m td_release_packager decompose-names config/token_map.conf \
    --env DEV \
    --output-dir config
```

---

### `ships bootstrap-env-config`

Bootstrap an environment config from a decomposed token map. Alternative entry point to `decompose-names` for sites that have already identified their composition roots.

---

### `ships import-legacy`

Bootstrap from a legacy sed substitution script. Use when your pre-SHIPS build harness already has a sed file defining marker-to-value pairs.

---

## Project layout reference

```
MyProject/
    ships.yaml                  ← Orchestrator config and stage settings
    .build_counter              ← Auto-incremented build number
    decisions.json              ← Audit trail: every pipeline run recorded here
    config/
        env/
            DEV.conf            ← Token values for DEV environment
            TST.conf
            PRD.conf
        inspect.conf            ← Lint rule configuration
        token_map.conf          ← Literal → {{TOKEN}} mapping
    payload/database/
        system/                 ← 00_system: maps, roles, profiles, authorisations
            maps/
            roles/
            profiles/
            authorizations/
            foreign_servers/
        pre-requisites/         ← 01_pre_requisites: databases and users
            databases/
            users/
        DCL/                    ← 02_dcl: grants and revokes
            inter_db/
        DDL/                    ← 03_ddl: all object DDL
            tables/             {{DB}}.ObjectName.tbl
            views/              {{DB}}.ObjectName.viw
            macros/             {{DB}}.ObjectName.mcr
            procedures/         {{DB}}.ObjectName.spl
            functions/          {{DB}}.ObjectName.fnc
            triggers/           {{DB}}.ObjectName.trg
            join_indexes/       {{DB}}.ObjectName.jix
            jar_install/        {{DB}}.ObjectName.sjr + binary.jar
            script_table_operators/  {{DB}}.ObjectName.sto
            comments/           {{DB}}.ObjectName.cmt
            statistics/         {{DB}}.ObjectName.stt
        DML/                    ← 04_dml: seed and reference data
        post-install/           ← 05_post_install: post-deployment scripts
    releases/                   ← Release-group directories containing package archives
    _waves.txt                  ← Deployment wave order (generated by analyse)
```

### Adding custom file extensions

SHIPS harvests a built-in set of Teradata file extensions by default (`.tbl`, `.viw`, `.spl`, etc.). If your codebase uses a non-standard extension (for example, legacy scripts named `.tdsql` or `.bteq`), add it to `ships.yaml`:

```yaml
discovery:
  extensions:
    - .tdsql
    - .bteq
```

This extends — not replaces — the built-in set. The resolved extension list is stamped into `context/ships.build.json` at package time under `discovery.extensions`, so the embedded deployer honours the same set at deploy time without any manual synchronisation.

---

### File extensions

| Extension | Object type |
|---|---|
| `.tbl` | Table |
| `.viw` | View |
| `.mcr` | Macro |
| `.spl` | Stored procedure |
| `.fnc` | Function |
| `.trg` | Trigger |
| `.jix` | Join index |
| `.idx` | Hash / secondary index |
| `.sjr` | SQLJ JAR install script |
| `.sto` | Script table operator |
| `.dcl` | Grant / revoke |
| `.cmt` | Comment on |
| `.stt` | Collect statistics |
| `.dml` | DML (INSERT / UPDATE / DELETE / MERGE) |
| `.db` | Create database |
| `.usr` | Create user |
| `.rol` | Create role |

---

## Security

SHIPS has a layered security model that spans the packaging step (developer or CI), the
handoff, and the deployment step (DBA). Developers interact with the signing and tagging
features; DBAs interact with the verification and approval features.

### Generating a key pair

```bash
ships keygen
```

Writes `ships_signing_private.pem` and `ships_signing_public.pem` in the current
directory. Commit the public key to the repository; store the private key in your
CI/CD platform's secrets manager.

### Package signing

Two modes are available, depending on your threat model:

**HMAC (symmetric — shared team key)**

```bash
ships package \
    --source /projects/OMR \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --signing-key /etc/ships/signing.key
```

The same key signs and verifies. Suitable when the signing team and the verifying DBA
both have access to the shared secret. See `docs/security_prerequisites.md` for
key storage requirements.

**Ed25519 (asymmetric — CI-only private key)**

```bash
ships package \
    --source /projects/OMR \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --asymmetric-key /run/secrets/ships_private.pem
```

Only the CI pipeline holds the private key. DBAs verify using the public key committed
to the repository. This is the recommended mode for production pipelines — a DBA with
full access to the extracted package cannot forge a valid signature.

### Change ticket reference

For environments that require a change ticket before production deployment:

```bash
ships package \
    --source /projects/OMR \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --change-ref CHG0012345
```

Add `require_change_ref: true` under the environment block in `ships.yaml` to enforce
this at the Ship preflight — the deploy will fail if the package has no change
reference.

### Sensitivity classification

Add a `.cls` companion file alongside any DDL file to tag its sensitivity level:

```
payload/database/DDL/tables/{{STD_DATABASE}}.Customer.tbl
payload/database/DDL/tables/{{STD_DATABASE}}.Customer.cls   ← companion
```

`.cls` file format:
```
PII
```

Configure how SHIPS responds in `config/inspect.conf`:
```properties
sensitivity_class=WARNING   # or ERROR to block packaging
```

### What the DBA sees

The preflight checks visible to the DBA include package hash verification, HMAC/Ed25519
signature verification, environment lock (prevents a PRD package deploying to DEV),
change ticket presence check, excess privilege warning, TLS enforcement, and package
age check. These run before any database connection is opened. See
`docs/OPERATIONS_GUIDE.md` for the full preflight reference.

---

## Deploying directly from GitHub Releases

Once CI publishes the package as a GitHub Release, DBAs deploy without file transfer:

```bash
ships deploy \
    --from-github org/repo \
    --release-tag v1.2.3 \
    --asset PRD_Pkg_BUILD_0001_20260515120000_01_main.zip \
    --host myhost \
    --user ships_dba
```

SHIPS downloads the ZIP and all available sidecar files (`.sha256`, `.hmac`, `.sig`)
from the release, verifies them, and proceeds with normal deployment. Set the
`GITHUB_TOKEN` environment variable for private repositories.

See `docs/OPERATIONS_GUIDE.md` for the full `--from-github` workflow.

---

## FAQ

**Q: My table already exists in DEV — will SHIPS drop it?**

No. Deployment strategy is controlled by the DDL verb in your file. `REPLACE TABLE` performs `DROP + CREATE`. `CREATE TABLE` will fail if the object already exists (which is intentional — it is saying "create it new"). To make a table re-runnable without data loss, use backup-and-replace strategy via deploy intent configuration.

**Q: I changed a column — how do I re-package?**

Edit the source SQL, re-run harvest, and re-run process. The payload is rebuilt clean from source each time. The DBA deploys the new package, which will detect the existing object and apply the correct strategy (backup, recreate, migrate data).

**Q: Can I skip the token map if I handle environments myself?**

Yes. If you are not ready to tokenise, omit `--token-map` from harvest. SHIPS will place the hardcoded-name files in the payload as-is. Inspect will flag them as `hardcoded_name` warnings. You can suppress that rule in `config/inspect.conf` while you transition.

**Q: The analyser says I have a circular dependency. What does that mean?**

Two or more objects depend on each other — for example, View A references View B, and View B references View A. Teradata cannot create them in any order without the other already existing. You need to restructure one of them, typically by introducing an interim base view that breaks the cycle.

**Q: How do I handle objects that must exist before my DDL (e.g. CREATE DATABASE)?**

SHIPS auto-detects objects that create databases/users and the objects that depend on them. It writes related archives into one release-group directory. By default, missing external parent databases/users are reported in the `_01_prereqs` package for DBA action, and SHIPS produces the normal `_01_prereqs` and `_02_main` pair. If you explicitly run package/process with `--generate-environment-prereqs`, the group may also contain a review-gated `_00_environment_prereqs` archive. Deploy archives in the order shown in `release_group.json` and the group-level `README.txt`.

**Q: I got a `MULTISET` inject notice — what does that mean?**

SHIPS automatically adds `MULTISET` to `CREATE TABLE` statements that have neither `SET` nor `MULTISET`. This is the safe Teradata default. If you intentionally want a SET table, add `CREATE SET TABLE` explicitly — SHIPS will not override it.

**Q: Can multiple developers work on the same project?**

Yes. The project directory (including `payload/`) should be in version control. Each developer harvests their own source into the shared project. Because harvest wipes and rebuilds from source, the payload always reflects what is in source control — it is not a hand-curated artefact.

**Q: What is `decisions.json`?**

It is an append-only audit trail written by every SHIPS pipeline run. Every stage records what config it used, what it processed, what it produced, and any issues it found. Run `ships explain --project .` at any time to see a formatted report of the last run.

---

## Troubleshooting

**Harvest produces no files / all files unclassified**

Check that your SQL files contain valid Teradata DDL with `CREATE` or `REPLACE` statements. A file of only `SELECT` statements or only comments will not classify. Files that contain only whitespace after comment stripping are skipped silently.

**`KeyError: 'force'` or `AttributeError: Namespace has no attribute...`**

You are using an old version of SHIPS that predates the `process` command. Update to the latest version.

**Token `{{MY_TOKEN}}` appears in deployed DDL**

The token was not resolved. Check:
1. `MY_TOKEN` is defined in `config/env/DEV.conf`
2. You passed `--env-config config/env/DEV.conf` to the package command
3. There is no whitespace inside the braces (`{{ MY_TOKEN }}` is invalid — must be `{{MY_TOKEN}}`)

Run `ships scan --source . --all-envs` to identify all undefined tokens across every environment before packaging. Add `--fail-on-orphan` to also catch dead config entries.

**Build counter keeps resetting to 0**

Do not delete `.build_counter`. If it is missing, SHIPS cannot auto-increment and will fail. Create a file named `.build_counter` containing `0` to restart from build 1.

**Inspect fails with `db_qualifier` on system objects**

System-scope objects (Maps, Roles, Profiles, Authorisations, Foreign Servers) are automatically exempt from the `db_qualifier` rule — they live in `system/` and do not have a database owner. If you are seeing this error on a non-system object, the object is probably in the wrong payload subdirectory.
