# SHIPS — Teradata Deployment Agent

**S**caffold · **H**arvest · **I**nspect · **P**ackage · **S**hip

An autonomous deployment agent for Teradata. SHIPS takes raw DDL from any source — extracted, generated, hand-coded, migrated — and produces self-contained, environment-specific release packages that a DBA can deploy without any knowledge of the build process.

Equally usable by humans at the command line, CI/CD pipelines, and autonomous AI agents.

---

## Quick Start

```bash
# Install (choose one)
pip install -r requirements.txt        # legacy requirements file
pip install -e .                        # modern pyproject.toml install
uv sync                                 # uv-managed install (uv.lock present)

# Scaffold a new project
python -m td_release_packager scaffold --name MyProject --output ./projects

# Harvest raw DDL into the project
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project ./projects/MyProject \
    --generate-token-map --env-prefix DEV01

# Review and apply the token map
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project ./projects/MyProject \
    --token-map config/token_map.conf

# Re-harvest with --force to overwrite existing files
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project ./projects/MyProject \
    --token-map config/token_map.conf \
    --force

# Inspect against the Coding Discipline
python -m td_release_packager inspect --source ./projects/MyProject

# Analyse dependencies and generate graphs
python -m td_release_packager analyze --source ./projects/MyProject \
    --graph --formats dot,mermaid,json,csv,openlineage

# Package for an environment
python -m td_release_packager package \
    --source ./projects/MyProject \
    --env DEV --name MyProject \
    --properties config/properties/DEV.properties \
    --output releases/

# Ship (deploy)
python -m ddl_deployer deploy ./releases/MyProject_DEV_b0001 --dry-run                       # No database — validates pipeline, parsing, wave ordering
python -m ddl_deployer deploy ./releases/MyProject_DEV_b0001 --host myserver --user dbc      # Connect and execute DDL
python -m ddl_deployer resume   ./releases/MyProject_DEV_b0001/.deploy_manifest.json --host myserver --user dbc   # Resume an interrupted deployment
python -m ddl_deployer status   ./releases/MyProject_DEV_b0001/.deploy_manifest.json         # Show manifest state
```

## Architecture

SHIPS consists of two Python packages:

| Package | Purpose |
|---|---|
| `td_release_packager` | Developer-side tooling: scaffold, harvest, inspect, analyse, package |
| `ddl_deployer` | DBA-side deployment engine: parse, pre-flight, deploy, rollback, report |

### The SHIPS Workflow

```
[S] Scaffold  →  Create project structure, properties files, inspect.conf
[H] Harvest   →  Import raw DDL, classify, tokenise, normalise (--force to overwrite)
    Analyse   →  Build dependency graph, generate wave ordering, export graphs
[I] Inspect   →  Lint against configurable Coding Discipline rules
[P] Package   →  Resolve tokens, resolve filenames, archive
[S] Ship      →  Pre-flight checks, privilege verification, wave-parallel deployment,
                  rollback, report
```

### Deployment Scope

SHIPS distinguishes between two scopes of object:

| Scope | Phase | Objects | Strategy |
|---|---|---|---|
| **System** | `00_system` | Maps, Roles, Profiles, Authorisations, Foreign Servers | SKIP_IF_EXISTS |
| **Environment** | `01`–`05` | Databases, Tables, Views, Procedures, Grants, etc. | Intent-driven |

System-scope objects are identical across environments — no database qualifier, no tokens, deployed once per Teradata system. Environment-scope objects are token-substituted and deployed per environment.

### Intent-Aware Deployment

The DDL verb IS the deployment intent. SHIPS does not second-guess the developer.

| Developer writes | Intent | Strategy |
|---|---|---|
| `CREATE TABLE` | IDEMPOTENT_DEPLOY | Backup → compare → migrate |
| `REPLACE VIEW` | REPLACE_WITH_BACKUP | SHOW capture → REPLACE |
| `CREATE VIEW` | CREATE_ONLY | Fail if exists |
| `CREATE JOIN INDEX` | DROP_AND_CREATE | SHOW capture → DROP → CREATE |
| `CREATE DATABASE` | DIRECT_EXECUTE | Execute as-is |
| `CREATE MAP` | SKIP_IF_EXISTS | Check existence → skip if present |

### Deployment Modes

The deployer has two CLI-exposed modes plus an EXPLAIN engine and an automatic REPLAY behaviour. Each produces an HTML report.

**Dry-run** (`--dry-run`) runs the entire deployment pipeline with no database connection. It parses every DDL file, classifies objects, determines deploy intent and strategy, builds wave ordering, and runs preflight validation — then produces a report showing exactly what *would* happen. No SQL is sent to Teradata. Use this as a first pass after harvest or package to answer: *"is this package well-formed?"*

**Deploy** (default, no flag) connects and executes. Wave-parallel across multiple streams, with manifest restartability, rollback capture, and a full HTML report.

**REPLAY** is automatic. When you re-run `deploy` against a package whose manifest already records every object as `COMPLETED`, SHIPS verifies each object against the live database (resetting any stale entries to `PENDING`) and, if there's nothing new to deploy, produces a `REPLAY Report` rather than a misleading `DEPLOYMENT Report`. The summary cards switch to `Verified (prior)` / `Deployed (this run)=0` so a DBA reading the report can tell at a glance that this run did no work.

**Explain** is implemented at the engine level (`ddl_deployer.deployer.explain_package`) — it wraps each DDL in Teradata's `EXPLAIN` to validate syntax, object resolution, column types, and permissions against the live catalogue without modifying anything. It is **not currently wired to the `ddl_deployer` subcommand list**; call it programmatically if you need it before that lands.

| | Dry-run | Deploy | REPLAY (auto) |
|---|---|---|---|
| Database connection | No | Yes | Yes (verification only) |
| Validates | Pipeline, parsing, classification, wave ordering, preflight rules | Everything — then executes | Existence of prior `COMPLETED` objects |
| Catches | Wrong extensions, missing tokens, duplicate objects, wave cycles | Runtime errors (locks, space, concurrency) | Stale manifest entries after a `DROP DATABASE` |
| Modifies database | No | Yes | No |
| Typical trigger | After harvest/package | Production deploy | Re-run of an already-deployed package |

```bash
# Recommended workflow
python -m ddl_deployer deploy ./releases/MyProject_DEV_b0001 --dry-run                  # Pipeline validation
python -m ddl_deployer deploy ./releases/MyProject_DEV_b0001 --host myserver --user dbc # Execute
```

### Dependency Analysis

The analyser uses 19 structural-anchor regexes to detect object references only in SQL positions where object names are expected, eliminating false positives from column aliases and DDL noise.

| Category | Anchors |
|---|---|
| Sources | FROM, JOIN (all variants) |
| Targets | INSERT INTO, UPDATE, DELETE, MERGE INTO, USING |
| DDL refs | Trigger event ON, FK REFERENCES, CREATE INDEX ON, RENAME TABLE, DROP object, COMMENT ON |
| SPL refs | CALL (procedure), EXEC/EXECUTE (macro), COLLECT STATISTICS ON |
| Access | LOCKING ... FOR |

Teradata SQL abbreviations (SEL, INS, UPD, DEL) are recognised. Dependencies feed the topological sort which generates `_waves.txt` for parallel deployment.

### Graph Export

Five portable export formats for the dependency graph:

| Format | Extension | Consumers |
|---|---|---|
| DOT | `.gv` | Graphviz, Gephi, yEd, vis.js |
| Mermaid | `.mmd` | GitHub markdown, Confluence, VS Code |
| JSON | `.json` | D3, vis.js, cytoscape.js, Graph Discipline |
| CSV | `.csv` | Excel, Neo4j, Gephi, pandas |
| OpenLineage | `.openlineage.json` | Marquez, DataHub, Atlan, GCP Lineage |

Edge direction in all formats: deployment flow (dependency → dependent). `TABLE → VIEW`, not `VIEW → TABLE`.

### Deployment Resilience

The deployer is designed for production reliability:

- **Manifest restartability** — state persisted after every transition; resume from exact failure point.
- **Manifest verification** — COMPLETED objects verified against the live database before re-deployment. Stale entries (e.g. after a DROP DATABASE) are automatically reset to PENDING.
- **Thread-safe manifest I/O** — unique temporary files per write with `threading.Lock` on all mutating operations. Safe under 6+ parallel streams on Windows and Linux.
- **DCL serialisation** — GRANT, DATABASE, USER, ROLE, and PROFILE operations are serialised to prevent Teradata deadlocks (Error 2631) on system catalogue tables. DDL remains fully parallel.
- **Transient error retry** — Error 3598 (concurrent change conflict) and Error 2631 (deadlock) are retried with exponential backoff as a safety net for external contention.
- **Privilege pre-flight** — verifies deployer user has CREATE + DROP rights on all target databases. Generates a prerequisite GRANT script with compound keywords (TABLE, VIEW, MACRO, PROCEDURE, FUNCTION, TRIGGER) if any are missing.

### Supported Object Types

| Type | Extension | Scope |
|---|---|---|
| Table | `.tbl` | Environment |
| View | `.viw` | Environment |
| Macro | `.mcr` | Environment |
| Procedure | `.spl` | Environment |
| Function | `.fnc` | Environment |
| Trigger | `.trg` | Environment |
| Join Index | `.jix` | Environment |
| Hash Index | `.idx` | Environment |
| Secondary Index | `.idx` | Environment |
| Database | `.db` | Environment |
| User | `.usr` | Environment |
| Grant / Revoke | `.dcl` | Environment |
| JAR | `.jar` | Environment |
| Script Table Operator | `.sto` | Environment |
| Map | `.map` | System |
| Role | `.rol` | System |
| Profile | `.prf` | System |
| Authorisation | `.auth` | System |
| Foreign Server | `.fsvr` | System |
| C Source (co-artefact) | `.c` / `.h` | — |

## Documentation

- **[Installation Guide](docs/INSTALLATION.md)** — Prerequisites, setup, verification
- **[User Guide](docs/USER_GUIDE.md)** — Complete workflow walkthrough
- **[Agent Integration](docs/AGENT_INTEGRATION.md)** — Autonomous agent and MCP tool usage

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest src/tests/ -v --tb=short
```

See [src/tests/README.md](src/tests/README.md) for the full test guide including argument explanations and subset execution.

## Project Structure

```
teradata-deployment-agent/
    src/
        td_release_packager/    ← Packager pipeline
            orchestrator/       ← Orchestrator foundation (ships.yaml, cascade, decisions)
        ddl_deployer/           ← Deployment engine
        tests/                  ← Test suite
    tools/                      ← Standalone utilities and demos
        migrate_view_references.py
        object_placement.yaml
        orchestrator_demo.py    ← Runnable smoke trace of the orchestrator foundation
    docs/
        INSTALLATION.md
        USER_GUIDE.md
        AGENT_INTEGRATION.md
    pyproject.toml              ← Modern build/dependency declaration
    uv.lock                     ← uv-managed lockfile
    requirements.txt            ← Legacy pip-style dependency list
    requirements-dev.txt
    .gitignore
    CHANGELOG.md
    README.md
```

## Licence

Internal use. Teradata proprietary.
