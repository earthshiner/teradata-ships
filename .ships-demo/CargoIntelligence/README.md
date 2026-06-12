# CargoIntelligence

Teradata release project managed by SHIPS (`td_release_packager`).

## SHIPS Workflow

```
[S] Scaffold  →  [H] Harvest  →  [I] Inspect  →  [P] Package  →  [S] Ship
```

## Environments

DEV

Config files: `config/env/<ENV>.conf`

## Project Structure

```
config/env/       — Token values per environment
config/inspect.conf      — Validation rule severities
object_placement.yaml    — Tables/views database separation strategy
payload/database/
  pre-requisites/        — CREATE DATABASE, CREATE USER, CREATE PROFILE
  DCL/                   — GRANT statements (container-level)
  DDL/                   — Tables, views, indexes, procedures, etc.
  DML/                   — Reference data, seed data (use MERGE for idempotency)
  post-install/          — Validation queries, COLLECT STATISTICS, cleanup
releases/                — Built release-group directories containing packages, checksums, release_group.json, and README.txt
```

## Tokens

Use `{{TOKENNAME}}` in any file under `payload/`. Token values
are defined in the environment properties files and resolved at
package time.

Scan for token usage:
```bash
python -m td_release_packager scan --source .
python -m td_release_packager scan --source . --env-config config/env/DEV.conf
```

## Object Placement

`object_placement.yaml` declares how tables and views are separated
across databases. Three strategies:

- **colocated** — tables and views share the same database. Zero
  setup, no enforcement. **This is the scaffolded default.**
- **separated** — pattern-based, e.g. `{BASE}_T` for tables and
  `{BASE}_V` for views. Recommended once you have separate
  databases for the two roles.
- **mapped** — explicit `tables_database` / `views_database` pairs.
  Use when naming is irregular.

The placement-related lint rules in `inspect.conf`
(`object_placement`, `public_grant_on_tables`,
`review_unmapped_grants`) all skip silently under `colocated`, so a
freshly scaffolded project is quiet by default. They get louder as
you opt into stricter placement — see the comments inside
`object_placement.yaml` for the syntax of each strategy.

## Packaging a Release

```bash
python -m td_release_packager package \
    --source . \
    --env DEV \
    --name CargoIntelligence \
    --env-config config/env/DEV.conf \
    --output releases/ \
    --author "Your Name"
```

Build number auto-increments from `.build_counter`.
For same-source promotion to another environment:
```bash
python -m td_release_packager package \
    --source . \
    --env PROD \
    --name CargoIntelligence \
    --env-config config/env/PROD.conf \
    --output releases/ \
    --no-increment
```

## Deploying a Package

Hand the `.zip` to the DBA. They unzip and run:
```bash
python deploy.py --host <teradata_host> --user <username> --dry-run
python deploy.py --host <teradata_host> --user <username>
```

## Deployment Order

Within each phase, files deploy alphabetically by default.
To specify a custom order (e.g. topological sort for table
dependencies), create an `_order.txt` file in the relevant
phase directory listing filenames one per line.
