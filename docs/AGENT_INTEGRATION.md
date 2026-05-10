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

## Token validation gate (`ships scan`)

Before building a package, an agent should confirm every token resolves in every target environment. `ships scan --all-envs --format json` provides a structured gate:

```python
import subprocess, json

result = subprocess.run(
    ["python", "-m", "td_release_packager", "scan",
     "--source", project_dir,
     "--all-envs",
     "--fail-on-orphan",
     "--format", "json"],
    capture_output=True, text=True
)

data = json.loads(result.stdout)
for env, v in data["validation"].items():
    if v["status"] == "error":
        raise RuntimeError(
            f"Token errors in {env}: {v['undefined']}"
        )

# Exit 0 = all tokens defined in all environments, no orphans
if result.returncode != 0:
    raise RuntimeError("Token scan failed — see above")
```

Key flags:

| Flag | Agent use |
|---|---|
| `--all-envs` | Sweep `config/env/*.conf` automatically — no need to know environment names |
| `--format json` | Parse `validation[env].status` and `validation[env].undefined` directly |
| `--fail-on-orphan` | Exit 1 on dead config entries — keeps env files clean |
| `--show-map` | Useful for logging which files reference which tokens before a build |

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

### Scenario 4 — GitHub Actions CI/CD pipeline

The repository is already checked out by `actions/checkout`, so SHIPS runs directly on the workspace. The `--commit` flag records the SHA so every deployed object is traceable back to the exact commit.

```yaml
# .github/workflows/ships.yml
jobs:
  package:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install SHIPS
        run: pip install uv && uv sync

      - name: Token validation gate
        run: |
          uv run python -m td_release_packager scan \
            --source . \
            --all-envs \
            --fail-on-orphan

      - name: Run SHIPS pipeline
        run: |
          uv run python -m td_release_packager process \
            --project . \
            --source src/ddl/ \
            --token-map config/token_map.conf \
            --env DEV \
            --env-config config/env/DEV.conf \
            --name ${{ github.event.repository.name }} \
            --commit ${{ github.sha }} \
            --author "ci-pipeline" \
            --strict

      - name: Verify package readiness
        run: uv run python -m td_release_packager verify --project .

      - name: Upload package artifact
        uses: actions/upload-artifact@v4
        with:
          name: ships-package
          path: releases/*.zip
```

For packaging from a specific branch, tag, or the GitHub API tarball (remote-only access), see the FAQ entry *"Can I package from a GitHub repository directly?"*.

### Scenario 5 — Casual packaging: PoC or demo (no DBA, self-deploy)

```python
# Agent or developer wants to deploy a PoC without formal ceremony
rc, out = ships_cli([
    "process", "--project", "/tmp/poc",
    "--source", "/poc/ddl/",
    "--auto-tokenise",               # detect and apply tokens in one pass
    "--env", "DEV",
    "--env-config", "/poc/config/env/DEV.conf",
    "--name", "poc_demo",
])
# The resulting package is fully auditable (build number, integrity, DBQL trail)
# even though it was built casually — the quality is there whether you need it or not
```

### Scenario 6 — Feature rollback to a previous tag

```python
# A bad deployment went live; roll back to the last known-good tag
ships_cli([
    "rollback",
    "--to-tag", "v1.2.3",
    "--env", "PRD",
    "--env-config", "config/env/PRD.conf",
    "--name", "OMR",
    "--project", "/projects/OMR",
])
# Then deploy the rollback package — on-drift defaults to 'continue'
# (restoring a known-good state should overwrite out-of-band changes)
```

### Scenario 7 — Drift-aware deployment

```python
import os
# Configure once per environment in ships.yaml / deploy.py invocation
os.environ["BASELINE_DIR"] = "/shared/nfs/ships-baselines/OMR/PRD/"

# The agent deploys; drift detection runs automatically
# on_drift="abort" is the default — agent stops and reports any drift
result = deploy_package(
    cursor, package_dir,
    baseline_dir="/shared/nfs/ships-baselines/OMR/PRD/",
    on_drift="abort",       # agent blocks on drift, escalates to human
)

if any(r.drift_detected for r in result.results):
    # Agent surfaces the diff for human review
    for r in result.results:
        if r.drift_detected:
            print(f"Drift on {r.database_name}.{r.object_name}:\n{r.drift_diff}")
```

---

## OpenTelemetry integration

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, every pipeline stage emits a span automatically. No code change required:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://my-collector:4318
export OTEL_SERVICE_NAME=ships-agent
```

The `ships.deploy` span carries: `ships.total`, `ships.completed`, `ships.failed`, `ships.success`. See [docs/OBSERVABILITY.md](./OBSERVABILITY.md) for the full span reference.

---

## OpenLineage integration

When `OPENLINEAGE_URL` is set, `deploy_package` emits START/COMPLETE/FAIL events automatically. No code change required:

```bash
export OPENLINEAGE_URL=http://marquez:5000
export OPENLINEAGE_NAMESPACE=teradata://td-prod.myorg.com:1025
```

For CI/air-gapped environments:

```bash
export OPENLINEAGE_URL=file:///var/log/ships/lineage.ndjson
```

Every successfully deployed object appears as an output dataset in the catalog. The `ShipsRunFacet` links the lineage event to the package build number and commit hash. See [docs/OBSERVABILITY.md](./OBSERVABILITY.md) for the full event schema.

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
| `1` | `rollback --to-tag` | Tag not found, git unavailable, or build failed |

---

## Structured artefacts for agent consumption

| Artefact | Location | What an agent reads |
|---|---|---|
| `decisions.json` | `<project>/decisions.json` | Stage outcomes, issue codes, config provenance, output counts |
| `BUILD.json` | Inside the package `.zip` | Build number, environment, file list, token count, integrity hash, **trust report**, baseline\_dir, discovery extensions |
| `_waves.txt` | `<project>/_waves.txt` | Topologically sorted deployment order |
| `config/token_map.conf` | `<project>/config/token_map.conf` | Literal → `{{TOKEN}}` mapping |
| `config/env/*.conf` | `<project>/config/env/` | Token → resolved value per environment |
| Deploy manifest | `<package>/logs/.deploy_manifest_*.json` | Per-object deployment outcomes, wave timing, drift flags |
| Drift baselines | `<baseline_dir>/<DB>.<OBJ>.baseline` | Last-deployed SHOW output per object — basis for drift detection |
| Lineage NDJSON | `$OPENLINEAGE_URL` path (file transport) | OpenLineage RunEvents: START, COMPLETE, FAIL with output datasets |

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
