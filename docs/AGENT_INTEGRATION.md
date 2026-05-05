# Agent Integration Guide

## Overview

SHIPS is designed to be operated by autonomous AI agents as well as humans. Every operation is a deterministic CLI command with structured output. No interactive prompts, no ambiguous steps, no manual file editing required.

An agent's workflow is identical to a human's — the same commands, the same files, the same artefacts. The difference is who drives.

## Agent Workflow

### Single-Command Tokenisation

The token map workflow is the key enabler for autonomous operation. An agent needs one piece of domain knowledge — the environment prefix — and SHIPS does the rest:

```bash
# Step 1: Harvest and generate token map (one command)
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project /projects/OMR \
    --generate-token-map --env-prefix A_D01

# Step 2: Re-harvest with the generated map (one command)
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project /projects/OMR \
    --token-map config/token_map.conf
```

For global databases without an environment prefix:

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project /projects/SHARED \
    --generate-token-map
```

### Full Autonomous Pipeline

```bash
# [S] Scaffold
python -m td_release_packager scaffold --name OMR --output /projects

# [H] Harvest + tokenise
python -m td_release_packager harvest \
    --source /raw/ddl/ --project /projects/OMR \
    --generate-token-map --env-prefix A_D01

python -m td_release_packager harvest \
    --source /raw/ddl/ --project /projects/OMR \
    --token-map config/token_map.conf

# Analyse dependencies
python -m td_release_packager analyze --source /projects/OMR --overwrite

# [I] Inspect
python -m td_release_packager inspect --source /projects/OMR --strict

# [P] Package
python -m td_release_packager package \
    --source /projects/OMR --env DEV --name OMR \
    --env-config config/env/DEV.conf \
    --output releases/

# [S] Ship (dry run first, then live)
python deploy.py --dry-run
python deploy.py --host myserver --user svc_deploy --streams 4
```

Every step is a single command. Every step produces deterministic output. No human intervention required.

## MCP Tool Integration

For agents operating via the Model Context Protocol, SHIPS commands map directly to tool calls:

```json
{
    "tool": "ships_scaffold",
    "parameters": {
        "name": "OMR",
        "output": "/projects"
    }
}
```

```json
{
    "tool": "ships_harvest",
    "parameters": {
        "source": "/raw/ddl/",
        "project": "/projects/OMR",
        "env_prefix": "A_D01",
        "generate_token_map": true
    }
}
```

```json
{
    "tool": "ships_harvest",
    "parameters": {
        "source": "/raw/ddl/",
        "project": "/projects/OMR",
        "token_map": "config/token_map.conf"
    }
}
```

```json
{
    "tool": "ships_inspect",
    "parameters": {
        "source": "/projects/OMR",
        "strict": true
    }
}
```

```json
{
    "tool": "ships_package",
    "parameters": {
        "source": "/projects/OMR",
        "env": "DEV",
        "name": "OMR",
        "env_config": "config/env/DEV.conf",
        "output": "releases/"
    }
}
```

```json
{
    "tool": "ships_deploy",
    "parameters": {
        "host": "myserver",
        "user": "svc_deploy",
        "streams": 4,
        "dry_run": false
    }
}
```

## Contract-Based Operation

Every artefact SHIPS produces is a contract — a deterministic, reviewable, reusable file:

| Artefact | Purpose | Agent uses it to... |
|---|---|---|
| `config/token_map.conf` | Literal → `{{TOKEN}}` mapping | Tokenise DDL without understanding naming conventions |
| `config/inspect.conf` | Rule severity configuration | Know which rules to enforce |
| `config/env/*.properties` | Environment token values | Resolve tokens for any target environment |
| `_waves.txt` | Dependency-ordered deployment sequence | Deploy in the correct order |
| `BUILD.json` | Package manifest | Verify build contents and traceability |

An agent does not need to understand Teradata naming conventions, deployment strategies, or environment topologies. SHIPS encodes all of that into the contracts. The agent just drives the workflow.

## Error Handling for Agents

SHIPS uses standard exit codes:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (missing files, validation failures, deployment errors) |

Agents should check the exit code after each command. If non-zero, the output contains diagnostic information. The inspect command's `ValidationResult.passed` property (reflected in the exit code) is the gate for proceeding to package.

## Observability

The deployment manifest (`BUILD.json`) and deployment report provide full observability:

- What was built, when, by whom, from which commit
- What tokens were resolved to which values
- Which objects were deployed, skipped, failed, or rolled back
- Wave execution timing and parallelism metrics

An agent can read these artefacts to verify deployment outcomes and report status.
