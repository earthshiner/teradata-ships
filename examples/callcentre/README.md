# CallCentre — Clearscape demo data product

A reference AI-Native Data Product packaged for deployment to a Teradata
**Clearscape Experience** sandbox via a generated Jupyter notebook.
Built for customer-facing and internal demos; **not for production**.

The payload covers the seven standard AI-Native Data Product modules
(DOM, MEM, OBS, PRE, SCH, SEM, plus STG staging), each split into the
three placement databases (`_STD_T` tables, `_STD_V` locking views,
`_BUS_V` business views) — 235 objects across 6 deployment waves.

## Render the Clearscape notebook

```bash
python -m td_release_packager notebook \
    --project examples/callcentre \
    --env-config examples/callcentre/config/env/DEV.conf \
    --name CallCentre
```

Output: `examples/callcentre/output/CallCentre.clearscape.ipynb` —
a self-contained notebook with inline DDL (no network egress beyond
the Teradata connection), one code cell per wave, and a verification
query at the end.

## Run it on Clearscape

1. Sign in to your Clearscape Experience instance and open Jupyter.
2. Upload `CallCentre.clearscape.ipynb`.
3. Run the first cell to `%pip install teradatasql`.
4. Run the connection cell and enter your Clearscape host, username,
   and password when prompted.
5. Run each wave cell in order. Long object lists are collapsed
   behind `<details>` blocks in the wave markdown — expand to see
   what each wave creates.
6. Run the verification cell — it counts objects per database via
   `DBC.TablesV` and prints a summary.

## Regenerating this scaffold from a fresh harvest

This project was scaffolded with:

```bash
python -m td_release_packager demo --prepare-only \
    --source <reflect-harvest>/CallCentre \
    --name callcentre --work-dir examples \
    --root-parent DataProducts --env DEV
```

Re-run with `--prepare-only` against an updated harvest to refresh
the payload. The notebook is regenerated each time you run
`ships notebook`, so it always matches the current payload + env.

---

# SHIPS project reference

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
    --name callcentre \
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
    --name callcentre \
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
