# SHIPS — Teradata Deployment Agent

**S**caffold · **H**arvest · **I**nspect · **P**ackage · **S**hip

An autonomous deployment agent for Teradata. SHIPS takes raw DDL from any source — extracted, generated, hand-coded, migrated — and produces self-contained, environment-specific release packages that a DBA can deploy without any knowledge of the build process.

Equally usable by humans at the command line, CI/CD pipelines, and autonomous AI agents.

---

## Quick Start

```bash
# Install
pip install -r requirements.txt

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

# Ship (deploy) — three modes, run in order
python deploy.py --dry-run                                          # 1. No database — validates pipeline, parsing, wave ordering
python deploy.py --host myserver --user dbc --streams 4 --explain   # 2. Connects — EXPLAIN validates SQL against live catalogue
python deploy.py --host myserver --user dbc --streams 4             # 3. Connects — executes DDL for real
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

The deployer supports three modes, designed to be run in sequence. Each mode catches a different class of problem, and each produces an HTML report.

**Dry-run** (`--dry-run`) runs the entire deployment pipeline with no database connection. It parses every DDL file, classifies objects, determines deploy intent and strategy, builds wave ordering, and runs preflight validation — then produces a report showing exactly what *would* happen. No SQL is sent to Teradata. Use this as a first pass after harvest or package to answer: *"is this package well-formed?"*

**Explain** (`--explain`) connects to the database and sends each DDL statement wrapped in Teradata's `EXPLAIN`. The database parses the SQL against the live catalogue — validating syntax, resolving object names, checking column types, and verifying permissions — without executing anything or modifying the database. Use this as a pre-deployment gate to answer: *"will this SQL actually work when I run it for real?"*

**Deploy** (default, no flag) connects and executes. Wave-parallel across multiple streams, with manifest restartability, rollback capture, and a full HTML report.

| | Dry-run | Explain | Deploy |
|---|---|---|---|
| Database connection | No | Yes | Yes |
| Validates | Pipeline, parsing, classification, wave ordering, preflight rules | SQL syntax, object resolution, permissions, catalogue state | Everything — then executes |
| Catches | Wrong extensions, missing tokens, duplicate objects, wave cycles | Permission errors, missing parent objects, syntax errors, type mismatches | Runtime errors (locks, space, concurrency) |
| Modifies database | No | No | Yes |
| Typical use | After harvest/package | Before production deploy | Production deploy |

```bash
# Recommended workflow
python deploy.py --dry-run                                          # Pipeline validation
python deploy.py --host myserver --user dbc --streams 4 --explain   # Database validation
python deploy.py --host myserver --user dbc --streams 4             # Execute
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
python -m pytest tests/ -v --tb=short
```

See [tests/README.md](src/tests/README.md) for the full test guide including argument explanations and subset execution.

## Project Structure

```
teradata-deployment-agent/
    src/
        td_release_packager/    ← Packager pipeline
        ddl_deployer/           ← Deployment engine
        tests/                  ← Test suite
    docs/
        INSTALLATION.md
        USER_GUIDE.md
        AGENT_INTEGRATION.md
    requirements.txt
    requirements-dev.txt
    .gitignore
    CHANGELOG.md
    README.md
```

## Licence

Internal use. Teradata proprietary.
