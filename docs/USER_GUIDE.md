# User Guide

## Overview

SHIPS automates the packaging and deployment of Teradata DDL across environments. The workflow has five phases, each represented by a letter in the SHIPS acronym:

```
[S] Scaffold  →  One-time project setup
[H] Harvest   →  Import and tokenise raw DDL
    Analyse   →  Dependency analysis and wave ordering
[I] Inspect   →  Lint against configurable rules
[P] Package   →  Build an environment-specific release
[S] Ship      →  Deploy to Teradata
```

All commands use the same entry point:

```bash
python -m td_release_packager <command> [options]
```

---

## [S] Scaffold — Create a New Project

```bash
python -m td_release_packager scaffold --name OMR --output C:\Projects
```

This creates a complete project structure:

```
OMR/
    config/
        properties/
            DEV.conf
            TST.conf
            PRD.conf
        inspect.conf
    payload/database/
        system/
            maps/
            roles/
            profiles/
            authorizations/
            foreign_servers/
        pre-requisites/
            databases/
            users/
        DCL/
            roles/
            users/
            inter_db/
        DDL/
            tables/
            views/
            macros/
            procedures/
            functions/
            triggers/
            join_indexes/
            JARs/
            script_table_operators/
        DML/
        post-install/
    releases/
    .build_counter
    .gitignore
    README.md
```

### Options

| Flag | Description |
|---|---|
| `--name` | Project name (required) |
| `--output` | Parent directory for the project (default: current directory) |
| `--environments` | Comma-separated environment list (default: `DEV,TST,PRD`) |

---

## [H] Harvest — Import Raw DDL

Harvest takes raw DDL files from any source and normalises them into the project structure.

### Basic Harvest

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project C:\Projects\OMR
```

SHIPS will:
1. Classify each file by DDL content (table, view, procedure, etc.)
2. Extract the qualified `Database.ObjectName`
3. Inject `MULTISET` where missing (tables only)
4. Rename to eponymous convention (`Database.ObjectName.ext`)
5. Place in the correct payload subdirectory
6. Report any unclassifiable files
7. Detect hardcoded database names as token candidates

### Programmatic Tokenisation

Tokenisation replaces hardcoded database names with `{{TOKEN}}` placeholders, making DDL portable across environments.

#### Step 1 — Generate a token map

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project C:\Projects\OMR \
    --generate-token-map --env-prefix A_D01
```

This scans all DDL, finds hardcoded database names, strips the environment prefix, and writes `config/token_map.conf`:

```properties
# 14 references across 8 files
A_D01_OMR_STD={{OMR_STD}}

# 9 references across 6 files
A_D01_OMR_SEM={{OMR_SEM}}

# 3 references across 2 files
A_D01_OMR_UTL={{OMR_UTL}}
```

If your databases have no environment prefix (global/shared databases), omit `--env-prefix` and the full name becomes the token:

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project C:\Projects\SHARED \
    --generate-token-map
# CORE_STD → {{CORE_STD}}
```

#### Step 2 — Review

The token map is a plain text file. Review it, rename tokens if needed, remove mappings you want left hardcoded, or add ones SHIPS missed. Commit it to Git so the team shares one mapping.

#### Step 3 — Re-harvest with the mapping

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project C:\Projects\OMR \
    --token-map config/token_map.conf
```

Every file is tokenised automatically. The process is repeatable — re-run it any time the source DDL changes.

### Harvest Options

| Flag | Description |
|---|---|
| `--source` | Directory containing raw DDL files (required) |
| `--project` | Target project directory (required, must be scaffolded) |
| `--token-map` | Path to `token_map.conf` — applies mappings during harvest |
| `--generate-token-map` | Scan for hardcoded names and write `config/token_map.conf` |
| `--env-prefix` | Environment prefix to strip for token name derivation (optional) |
| `--apply-tokens` | Legacy: inline comma-separated `name={{TOKEN}}` pairs |
| `--no-detect-tokens` | Skip hardcoded name detection |

---

## Analyse — Dependency Analysis

```bash
python -m td_release_packager analyze --source C:\Projects\OMR
```

Scans all DDL files, builds a directed dependency graph, and produces a topologically sorted wave ordering. The output `_waves.txt` tells the deployer which objects to deploy in parallel and which must wait.

### What It Handles

- Cross-database references (flagged as external dependencies)
- Function overloads (SPECIFIC name → function group index)
- Table alias filtering (short prefixes like `c.`, `o.` excluded)
- Circular dependency detection
- System database filtering (DBC, SYSLIB, etc.)

### Options

| Flag | Description |
|---|---|
| `--source` | Project directory to analyse (required) |
| `--output` | Output path for `_waves.txt` (default: `<source>/_waves.txt`) |
| `--overwrite` | Overwrite existing `_waves.txt` |

---

## [I] Inspect — Coding Discipline Linter

```bash
python -m td_release_packager inspect --source C:\Projects\OMR
```

Checks DDL files against configurable engineering discipline rules.

### Rules

| Rule | Default | What It Checks |
|---|---|---|
| `db_qualifier` | ERROR | Database qualifier present (`DB.ObjectName`) |
| `set_multiset` | WARNING | `SET` or `MULTISET` specified for tables |
| `deploy_intent` | WARNING | `CREATE` without `REPLACE` for replaceable types |
| `one_object` | WARNING | One DDL statement per file |
| `eponymous` | WARNING | Filename matches DDL content |
| `extension` | WARNING | Correct file extension per object type |
| `type_suffix` | ERROR | No type suffixes (`_V`, `_T`, `VW_`, `SP_`) |
| `hardcoded_name` | WARNING | `{{TOKENS}}` used instead of hardcoded names |
| `keyword_case` | WARNING | SQL keywords in UPPERCASE |
| `leading_commas` | WARNING | Leading comma convention in column lists |

System-scope objects (Maps, Roles, Profiles, Authorisations, Foreign Servers) are automatically excluded from `db_qualifier` and `hardcoded_name` checks.

### Configuring Rules

Create or edit `config/inspect.conf` in your project:

```properties
# Structural rules
db_qualifier=ERROR
set_multiset=WARNING
deploy_intent=WARNING
one_object=WARNING
eponymous=WARNING
extension=WARNING
type_suffix=ERROR

# Style rules — turn off what you don't want
hardcoded_name=WARNING
keyword_case=OFF
leading_commas=OFF
```

Valid severities: `ERROR`, `WARNING`, `OFF`.

SHIPS auto-detects `config/inspect.conf` in the project directory. You can also specify it explicitly:

```bash
python -m td_release_packager inspect --source . --config path/to/inspect.conf
```

### Strict Mode

`--strict` promotes all `WARNING` rules to `ERROR`. Rules set to `OFF` remain off.

```bash
python -m td_release_packager inspect --source . --strict
```

Use for production builds where re-runnability is mandatory.

### Options

| Flag | Description |
|---|---|
| `--source` | Directory to validate (required) |
| `--config` | Path to `inspect.conf` (default: auto-detect in project) |
| `--strict` | Promote all `WARNING` rules to `ERROR` |
| `--skip-tokens` | Legacy: disable hardcoded name checks |
| `--skip-keywords` | Legacy: disable keyword case checks |
| `--skip-commas` | Legacy: disable leading comma checks |

---

## [P] Package — Build a Release

```bash
python -m td_release_packager package \
    --source C:\Projects\OMR \
    --env DEV \
    --name OMR \
    --env-config config/env/DEV.conf \
    --output releases/
```

This produces a self-contained release archive (e.g. `DEV_OMR_BUILD_0012_20260421.zip`) containing:

- Resolved DDL files (all `{{TOKENS}}` replaced with environment values)
- Resolved filenames (e.g. `P_CORE.Customer.tbl`, not `DEV01_CORE.Customer.tbl`)
- The deployment engine (`ddl_deployer` package)
- `BUILD.json` manifest with full traceability
- `deploy.py` entry point for the DBA
- `README.txt` with deployment instructions

### Environment Promotion

Build the same source for a different environment without incrementing the build number:

```bash
# DEV build — increments counter
python -m td_release_packager package \
    --source . --env DEV --name OMR \
    --env-config config/env/DEV.conf \
    --output releases/

# PRD promotion — same build number
python -m td_release_packager package \
    --source . --env PRD --name OMR \
    --env-config config/env/PRD.conf \
    --output releases/ --no-increment
```

### Token Interpolation in Properties

Properties files support `{{TOKEN}}` references within values:

```properties
SHIPS_ENV=PRD
ENV_PREFIX=P
SHIPS_PROJECT=OMR
STD_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
SEM_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
```

Resolution is iterative with circular reference detection.

### Options

| Flag | Description |
|---|---|
| `--source` | Source project directory (required) |
| `--env` | Target environment, e.g. `DEV`, `TST`, `PRD` (required) |
| `--name` | Package name (required) |
| `--env-config` | Path to environment `.conf` file (required) |
| `--output` | Output directory (default: current directory) |
| `--no-increment` | Reuse current build number (for promotion) |
| `--format` | Archive format: `zip` or `tar.gz` (default: `zip`) |
| `--author` | Builder's name |
| `--description` | Release description |
| `--commit` | Git commit hash for traceability |

---

## [S] Ship — Deploy to Teradata

The DBA receives the package, extracts it, and runs:

```bash
# Dry run — simulate without connecting
python deploy.py --dry-run

# Live deployment
python deploy.py --host myserver --user dbc --streams 4

# With options
python deploy.py \
    --host myserver \
    --user dbc \
    --logmech LDAP \
    --streams 4 \
    --continue-on-error
```

### What the Deployer Does

1. **Pre-flight checks** — validates permissions, space, object existence
2. **Wave-parallel execution** — deploys objects in dependency order, parallelising within waves
3. **Intent-aware strategy** — each object is deployed according to the developer's DDL verb
4. **Rollback on failure** — compensating actions for each strategy
5. **Manifest tracking** — records the state of every object in the deployment
6. **HTML report** — generates a deployment report

### Options

| Flag | Description |
|---|---|
| `--host` | Teradata host (not required for `--dry-run`) |
| `--user` | Teradata user (not required for `--dry-run`) |
| `--password` | Teradata password (prompted if not provided) |
| `--logmech` | Logon mechanism (`LDAP`, `TD2`, etc.) |
| `--dry-run` | Simulate without connecting |
| `--streams` | Parallel deployment streams (default: 1) |
| `--continue-on-error` | Continue past individual object failures |

---

## Properties Files

Each environment has a `.conf` file in `config/env/`:

```properties
# config/env/DEV.conf
SHIPS_ENV=DEV
ENV_PREFIX=A_D01
SHIPS_PROJECT=OMR
STD_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
SEM_DATABASE={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_SEM
```

### Reserved Properties

These property names are reserved and excluded from "unused token" warnings:

| Property | Purpose |
|---|---|
| `SHIPS_ENV` | Declares the target environment. Cross-checked against `--env`. |
| `SHIPS_PROJECT` | Project identifier. Available for use in token composition. |
| `ENV_PREFIX` | Environment prefix. The foundation for database name composition. |

---

## Project Layout

After scaffolding and harvesting, a project looks like:

```
OMR/
    config/
        properties/
            DEV.conf
            TST.conf
            PRD.conf
        inspect.conf
        token_map.conf          ← Generated by --generate-token-map
    payload/database/
        system/                 ← System-scope (00_system phase)
            maps/
            roles/
            profiles/
            authorizations/
            foreign_servers/
        pre-requisites/         ← 01_pre_requisites phase
            databases/
            users/
        DCL/                    ← 02_dcl phase
            inter_db/
        DDL/                    ← 03_ddl phase
            tables/
                {{STD_DATABASE}}.Customer.tbl
                {{STD_DATABASE}}.Orders.tbl
            views/
                {{STD_DATABASE}}.ActiveCustomers.viw
            functions/
                {{STD_DATABASE}}.fn_Calc.fnc
                fn_Calc.c       ← C source co-artefact
            ...
        DML/                    ← 04_dml phase
        post-install/           ← 05_post_install phase
    releases/
    _waves.txt                  ← Generated by analyze
    .build_counter
```
