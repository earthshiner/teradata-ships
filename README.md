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

# Inspect against the Coding Discipline
python -m td_release_packager inspect --source ./projects/MyProject

# Analyse dependencies
python -m td_release_packager analyze --source ./projects/MyProject

# Package for an environment
python -m td_release_packager package \
    --source ./projects/MyProject \
    --env DEV --name MyProject \
    --properties config/properties/DEV.properties \
    --output releases/

# Ship (deploy)
python deploy.py --dry-run
python deploy.py --host myserver --user dbc --streams 4
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
[H] Harvest   →  Import raw DDL, classify, tokenise, normalise
    Analyse   →  Build dependency graph, generate wave ordering
[I] Inspect   →  Lint against configurable Coding Discipline rules
[P] Package   →  Resolve tokens, resolve filenames, archive
[S] Ship      →  Pre-flight checks, wave-parallel deployment, rollback
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
| JAR | `.jcl` | Environment |
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
        td_release_packager/    ← Packager pipeline (28 modules)
        ddl_deployer/           ← Deployment engine (14 modules)
        tests/                  ← Test suite (368 tests)
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
