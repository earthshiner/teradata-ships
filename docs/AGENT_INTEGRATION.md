# SHIPS Agent Integration Guide

## Primary integration: MCP server

The recommended way for an agent to operate SHIPS is via the MCP server. It exposes all pipeline stages as tools over the Model Context Protocol, which any MCP-compatible client (Claude Code, Claude Desktop, Cursor, custom agent frameworks) can discover and invoke directly.

**See [docs/MCP_GUIDE.md](./MCP_GUIDE.md) for the complete MCP server reference** — setup, tool schemas, agentic workflows, and Trust Report integration.

---

## Secondary integration: subprocess CLI

When MCP is not available (e.g. a headless CI environment without MCP client support), agents can drive SHIPS via subprocess calls. Every command is deterministic and returns a structured exit code.

---

## Why SHIPS is built for agents

Most database deployment tooling was designed for humans: interactive prompts, manual file editing, tribal knowledge baked into runbooks. An agent trying to operate those tools hits ambiguity at every step.

SHIPS inverts this. Every operation is a deterministic CLI command. Every output is a structured artefact. Every decision is recorded in a machine-readable audit trail (`decisions.json`). An agent driving SHIPS has the same capabilities as a human — and in some respects more, because agents do not skip steps, do not misremember environment names, and do not get impatient with validation warnings.

The guiding principle: **an agent should be able to take a folder of raw SQL and produce a deployment-ready, governance-compliant package for any target environment — from scratch, without human intervention.**

---

## The single-command pipeline

The `process` command is the primary agentic primitive for the CLI path. It runs the full SHIPS pipeline — harvest → generate → inspect → analyse → package — in one invocation, writing all stage decisions into a single run entry in `decisions.json`:

```bash
python -m td_release_packager process \
    --project /projects/OMR \
    --source /raw/ddl/ \
    --auto-tokenise \
    --env-prefix A_D01 \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name OMR \
    --strict
```

`--auto-tokenise` detects hardcoded database names and applies token substitution in one pass — no manual review step. `--strict` makes the pipeline abort immediately on any stage error, which is the correct behaviour for unattended operation.

Exit code 0 = package produced and all stages passed. Exit code 1 = something failed; read `decisions.json` for detail.

---

## Zero-configuration tokenisation

```bash
# Agent knows only the environment prefix; SHIPS does the rest
python -m td_release_packager process \
    --project /projects/OMR \
    --source /raw/ddl/ \
    --auto-tokenise \
    --env-prefix A_D01 \
    --strict
```

For codebases without an environment prefix (global or shared databases), omit `--env-prefix`. SHIPS derives `{{SHARED_DB}}` from `SHARED_DB` — the full name becomes the token.

---

## Machine-readable audit trail: `decisions.json`

Every SHIPS pipeline run writes to `decisions.json` in the project directory. This is the agent's primary feedback channel — a structured, append-only record of what every stage did, what config it used, what it produced, and what issues it found.

### Schema

```json
{
  "schema_version": 1,
  "project": { "name": "OMR" },
  "runs": [
    {
      "run_id": "2026-05-09T14:30:00Z-a3f8",
      "command": "process",
      "started_at": "2026-05-09T14:30:00+00:00",
      "finished_at": "2026-05-09T14:30:12+00:00",
      "duration_ms": 12340,
      "final_status": "success",
      "stages": [
        {
          "stage": "harvest",
          "status": "success",
          "config_resolved": { "source": { "value": "/raw/ddl/", ... } },
          "inputs": { "source_dir": "/raw/ddl/", "total_files": 47 },
          "outputs": { "classified": 45, "unclassified": 2 },
          "decisions": { "auto_tokenise": true, "auto_derived_tokens": 3 },
          "issues": [
            { "severity": "warning", "code": "HARVEST_UNCLASSIFIED", "message": "session_setup.sql" }
          ]
        }
      ]
    }
  ]
}
```

### Reading `decisions.json` in agent code

```python
import json, pathlib, subprocess

result = subprocess.run(
    ["python", "-m", "td_release_packager", "process",
     "--project", project_dir, "--source", source_dir,
     "--auto-tokenise", "--env-prefix", env_prefix, "--strict"],
    capture_output=True, text=True
)

if result.returncode != 0:
    decisions = json.loads(
        pathlib.Path(project_dir, "decisions.json").read_text()
    )
    last_run = decisions["runs"][-1]
    failed_stages = [s for s in last_run["stages"] if s["status"] == "error"]
    for stage in failed_stages:
        for issue in stage["issues"]:
            print(f"[{stage['stage']}] {issue['severity']}: {issue['code']} — {issue['message']}")
```

---

## Trust Report

Every `ships package` call (or `process` with package stage enabled) stamps a Trust Report into `BUILD.json`. The `trust.label` field is the primary deployment gate:

| Label | Meaning |
|---|---|
| `READY` | All signals pass — safe to deploy |
| `READY-WITH-CAVEATS` | Warnings present — deploy with awareness |
| `BLOCKED` | At least one signal failed — do not deploy |

**Phase 1 signals** (computable at build time):

| Signal | BLOCKED when |
|---|---|
| `inspect_token_format` | Any malformed `{{TOKEN}}` marker found |
| `inspect_lint` | Any Coding Discipline ERROR-severity violation |
| `inspect_grants` | Any grant drift ERROR detected |
| `provenance_complete` | `_provenance.json` absent from payload |

**Reading trust in a CLI pipeline:**
```bash
# Check trust label from BUILD.json inside the archive
python -c "
import zipfile, json, sys
with zipfile.ZipFile('$ARCHIVE_PATH') as z:
    name = next(n for n in z.namelist() if n.endswith('BUILD.json'))
    build = json.loads(z.read(name))
label = build['trust']['label']
print(f'Trust: {label}')
sys.exit(0 if label in ('READY', 'READY-WITH-CAVEATS') else 1)
"
```

---

## Structured gate commands

Two commands are designed specifically as agentic decision gates.

### `explain` — what did the last run do?

```bash
python -m td_release_packager explain \
    --project /projects/OMR \
    --command process
```

Exit 0 if the last process run had status success or warning. Exit 1 if it had errors or the file is missing.

### `verify` — is the package ready to deploy?

```bash
python -m td_release_packager verify --project /projects/OMR
```

Exit 0 = READY. Exit 1 = NOT READY. Checks: archive exists, no package issues, package stage succeeded, trust label not BLOCKED.

---

## Agentic deployment scenarios (CLI path)

### Scenario 1 — Chat-driven packaging

```python
import subprocess, json, pathlib

def ships_cli(cmd: list) -> tuple[int, str]:
    r = subprocess.run(["python", "-m", "td_release_packager"] + cmd,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

# Create project
ships_cli(["scaffold", "--name", "OMR", "--output", "/projects"])

# Fill in config/env/DEV.conf from known topology (agent writes this)

# Run full pipeline
rc, out = ships_cli([
    "process", "--project", "/projects/OMR",
    "--source", "/raw/ddl/",
    "--auto-tokenise", "--env-prefix", "A_D01",
    "--env", "DEV", "--env-config", "config/env/DEV.conf",
    "--name", "OMR", "--strict",
])

if rc != 0:
    raise RuntimeError(f"Pipeline failed:\n{out}")

# Verify
rc, _ = ships_cli(["verify", "--project", "/projects/OMR"])
if rc == 0:
    print("Package ready for deployment")
```

### Scenario 2 — CI/CD pipeline

```bash
# .github/workflows/ships.yml excerpt
- name: Run SHIPS pipeline
  run: |
    uv run python -m td_release_packager process \
        --project $PROJECT_DIR \
        --source src/ddl/ \
        --token-map config/token_map.conf \
        --env DEV \
        --env-config config/env/DEV.conf \
        --name $PACKAGE_NAME \
        --commit $GITHUB_SHA \
        --author "ci-pipeline" \
        --strict

- name: Verify package readiness
  run: uv run python -m td_release_packager verify --project $PROJECT_DIR
```

### Scenario 3 — Autonomous environment promotion

```bash
# DEV package approved; promote to TST with same build number
python -m td_release_packager package \
    --source /projects/OMR \
    --env TST \
    --name OMR \
    --env-config config/env/TST.conf \
    --output releases/ \
    --no-increment \
    --commit $APPROVED_SHA

python -m td_release_packager verify --project /projects/OMR
```

---

## All exit codes

| Code | Command | Meaning |
|---|---|---|
| `0` | Any | Success or success-with-warnings |
| `1` | `process --strict` | A stage failed and pipeline was aborted |
| `1` | `package` | Trust label is BLOCKED (unless `--skip-trust-check`) |
| `1` | `verify` | Package is NOT READY |
| `1` | `explain` | Last run had error status or file missing |
| `1` | `deploy` | One or more objects failed |
| `1` | `rollback` | One or more rollback operations failed |

---

## Structured artefacts for agent consumption

| Artefact | Location | What an agent reads |
|---|---|---|
| `decisions.json` | `<project>/decisions.json` | Stage outcomes, issue codes, config provenance, output counts |
| `BUILD.json` | Inside the package `.zip` | Build number, environment, file list, token count, integrity hash, **trust report** |
| `_waves.txt` | `<project>/_waves.txt` | Topologically sorted deployment order |
| `config/token_map.conf` | `<project>/config/token_map.conf` | Literal → `{{TOKEN}}` mapping |
| `config/env/*.conf` | `<project>/config/env/` | Token → resolved value per environment |
| Deploy manifest | `<package>/logs/.deploy_manifest_*.json` | Per-object deployment outcomes, wave timing |

---

## Issue codes

Every issue recorded in `decisions.json` carries a stable code. Agents branch on codes, not free-text messages.

| Domain | Code | Severity | Meaning |
|---|---|---|---|
| Harvest | `HARVEST_UNCLASSIFIED` | warning | File not classified |
| Harvest | `HARVEST_TOKEN_CANDIDATE` | info | Hardcoded DB name detected |
| Inspect | `INSPECT_TOKEN_MALFORMED` | error | Malformed `{{TOKEN}}` marker |
| Inspect | `INSPECT_LINT_VIOLATION` | varies | Coding Discipline rule fired |
| Inspect | `INSPECT_GRANT_VIOLATION` | varies | Grant drift detected |
| Analyse | `ANALYSE_CYCLE` | error | Circular dependency |
| Analyse | `ANALYSE_EXTERNAL_REF` | info | Reference not in package |
| Generate | `GENERATE_ERROR` | error | View generator failed |
| Package | `PACKAGE_WARNING` | warning | Build-time anomaly |
| Token | `TOKEN_UNDEFINED` | error | `{{TOKEN}}` has no value in env config |

---

## MCP server

For agents that prefer tool calls over subprocess invocation, the SHIPS MCP server exposes all pipeline stages as FastMCP tools. See **[docs/MCP_GUIDE.md](./MCP_GUIDE.md)** for the full reference.
