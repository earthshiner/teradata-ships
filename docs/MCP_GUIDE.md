# SHIPS MCP Server Guide
### For Agents and MCP-Compatible Clients

---

## What this is

SHIPS exposes its full deployment pipeline as an MCP (Model Context Protocol) server. Any MCP-compatible client — Claude Code, Claude Desktop, Cursor, or a custom agent framework — can discover and invoke SHIPS tools without CLI access, shell scripting, or knowledge of the internal code structure.

The agent calls a tool. SHIPS does the work. Durable state (decisions.json, packages, deployment manifests) lives on the filesystem between calls.

---

## Prerequisites

- Python 3.13+
- `uv` (recommended) or plain `pip`
- `teradatasql` (required only for deployment tools)

Install dependencies:
```bash
uv sync   # installs mcp and all other dependencies
```

---

## Starting the server

The MCP server communicates over stdio. You do not start it manually — the MCP client starts it as a subprocess based on your configuration.

**Verify the server starts:**
```bash
uv run python src/ships_mcp.py --help
# or just:
uv run python src/ships_mcp.py
# → starts the server and waits for MCP protocol messages
```

---

## Configuration

### Claude Code

Add to your project's `.claude/settings.json` or your user settings:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src/ships_mcp.py"],
      "cwd": "/path/to/teradata-deployment-agent"
    }
  }
}
```

After saving, run `/mcp` in Claude Code to verify the server is connected and the tools appear.

### Claude Desktop (macOS)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src/ships_mcp.py"],
      "cwd": "/path/to/teradata-deployment-agent"
    }
  }
}
```

### Claude Desktop (Windows)

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src\\ships_mcp.py"],
      "cwd": "C:\\path\\to\\teradata-deployment-agent"
    }
  }
}
```

### Generic MCP client

Any client that supports the MCP stdio transport can connect:

```python
import subprocess, json

proc = subprocess.Popen(
    ["uv", "run", "python", "src/ships_mcp.py"],
    cwd="/path/to/teradata-deployment-agent",
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
# Send MCP protocol messages via proc.stdin / proc.stdout
```

---

## Tool reference

### Pipeline tools (offline — no database connection needed)

---

#### `ships_scaffold`

Create a new SHIPS project structure, or repair an existing one.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Project name (used as directory name) |
| `output` | string | no | Parent directory (default: `.`) |
| `environments` | string | no | Comma-separated env names (default: `DEV,TST,PRD`) |
| `repair` | bool | no | Add missing files without overwriting existing config |

**Returns:** `{"success": bool, "project_dir": str, "environments": list, "action": str}`

**Example:**
```
Tool: ships_scaffold
Args: {"name": "MortgagePlatform", "output": "/projects", "environments": "DEV,TST,PRD"}
```

---

#### `ships_harvest`

Harvest raw DDL files from a source directory into a SHIPS project. Classifies each file by DDL content, injects MULTISET where missing, renames to the eponymous convention, and places files in the correct payload subdirectory.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source` | string | yes | Directory containing raw DDL files |
| `project` | string | yes | Target SHIPS project directory |
| `token_map` | string | no | Path to `token_map.conf` for substitution |
| `auto_tokenise` | bool | no | Detect and apply tokens in one pass |
| `env_prefix` | string | no | Env prefix to strip (e.g. `A_D01`) |

**Returns:** `{"success": bool, "classified": int, "unclassified": int, "files_placed": int, "token_candidates": int, "warnings": list, "unclassified_files": list}`

**Example (auto-tokenise):**
```
Tool: ships_harvest
Args: {"source": "/raw/ddl/", "project": "/projects/MortgagePlatform",
       "auto_tokenise": true, "env_prefix": "A_D01"}
```

---

#### `ships_generate`

Generate view-layer DDL from harvested tables (SHIPS Object Placement Standard topology). Creates 1:1 locking views and business views.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `modules` | string | no | Comma-separated modules (default: all) |
| `dry_run` | bool | no | Validate without writing files |

**Returns:** `{"success": bool, "locking_views_written": int, "business_views_rewritten": int, "warnings": list, "errors": list}`

---

#### `ships_inspect`

Inspect payload DDL against Coding Discipline rules (token format, lint violations, grant drift).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `config` | string | no | Path to `inspect.conf` |
| `strict` | bool | no | Promote all WARNING rules to ERROR |

**Returns:** `{"success": bool, "passed": bool, "error_count": int, "warning_count": int, "findings": [{"rule": str, "severity": str, "file": str, "message": str}]}`

---

#### `ships_analyse`

Build the DDL dependency graph and generate wave ordering (`_waves.txt`).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `overwrite` | bool | no | Overwrite existing `_waves.txt` (default: true) |

**Returns:** `{"success": bool, "object_count": int, "wave_count": int, "cycle_count": int, "cycles": list, "waves_path": str}`

---

#### `ships_package`

Build a release package for a target environment. Resolves all `{{TOKEN}}` references, assembles a self-contained archive, and stamps `BUILD.json` with provenance, integrity hash, and Trust Report.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `env` | string | yes | Target environment (DEV, TST, PRD) |
| `name` | string | yes | Package name |
| `env_config` | string | yes | Path to environment `.conf` file |
| `output` | string | no | Output directory |
| `author` | string | no | Builder identifier for provenance |
| `description` | string | no | Release description |
| `commit` | string | no | Git commit hash |

**Returns:** `{"success": bool, "archive_path": str, "build_number": int, "file_count": int, "token_count": int, "trust_label": str, "warnings": list}`

The `trust_label` field is `READY`, `READY-WITH-CAVEATS`, or `BLOCKED`. An agent should check this before proceeding to deployment.

---

#### `ships_process`

Run the full pipeline in one call: harvest → generate → inspect → analyse → [package].

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `source` | string | no | Raw DDL source (harvest skipped if omitted) |
| `token_map` | string | no | Token substitution map |
| `auto_tokenise` | bool | no | Auto-detect and apply tokens |
| `env_prefix` | string | no | Env prefix for auto-tokenise |
| `env` | string | no | Target environment (enables package stage) |
| `env_config` | string | no | Env config file (enables package stage) |
| `name` | string | no | Package name (enables package stage) |
| `skip_generate` | bool | no | Skip view-layer generate stage |
| `strict` | bool | no | Abort on first stage error |

**Returns:** `{"success": bool, "stages": {"harvest": {...}, "inspect": {...}, ...}, "failed_stages": list}`

---

### Deployment tools (require Teradata connection)

All deployment tools accept `host`, `user`, `password`, and `logmech` (default: `TD2`) as connection parameters.

---

#### `ships_deploy`

Deploy a package to a Teradata system. Runs mandatory pre-flight validation then deploys all objects in wave order.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `package_dir` | string | yes | Extracted package directory |
| `host` | string | yes | Teradata hostname |
| `user` | string | yes | Teradata username |
| `password` | string | yes | Teradata password |
| `logmech` | string | no | Logon mechanism (default: TD2) |
| `dry_run` | bool | no | Simulate without executing DDL |
| `streams` | int | no | Parallel deployment streams (1–8) |
| `continue_on_error` | bool | no | Continue past individual failures |

**Returns:** `{"success": bool, "completed": int, "failed": int, "skipped": int, "report_path": str, "deployment_id": str, "objects": [...]}`

---

#### `ships_deploy_explain`

Run EXPLAIN validation on a package against a live Teradata target. Validates DDL without executing. Requires parent databases to exist on target (use deploy chaining or deploy prereqs first).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `package_dir` | string | yes | Extracted package directory |
| `host` | string | yes | Teradata hostname |
| `user` | string | yes | Teradata username |
| `password` | string | yes | Teradata password |
| `logmech` | string | no | Logon mechanism |

**Returns:** `{"passed": int, "failed": int, "skipped": int, "report_path": str, "objects": [...]}`

---

#### `ships_rollback`

Roll back a deployment, restoring objects to their pre-deployment state. Supports wave-scoped rollback and offline dry-run.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `manifest_path` | string | yes | Path to `.deploy_manifest.json` |
| `host` | string | yes* | Teradata hostname (*not needed for dry_run) |
| `user` | string | yes* | Teradata username |
| `password` | string | yes* | Teradata password |
| `wave` | int | no | Roll back only objects from this wave number |
| `dry_run` | bool | no | Preview without executing (offline) |

**Returns:** `{"success": bool, "rolled_back": int, "failed": int, "objects": [...]}`

---

### Read-only consumers (no connection needed)

---

#### `ships_decisions`

Read the `decisions.json` audit trail for a project.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `run_id` | string | no | Specific run ID (default: last run) |

**Returns:** `{"success": bool, "run": {run record}, "total_runs": int}`

---

#### `ships_verify`

Check whether the last built package is ready to deploy.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |

**Returns:** `{"success": bool, "ready": bool, "trust_label": str, "archive_path": str, "checks": [...]}`

An agent should call this before `ships_deploy` to confirm the package is in a deployable state.

---

#### `ships_explain_run`

Format a prior pipeline run as a structured summary for review.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `run_id` | string | no | Specific run ID |
| `command_filter` | string | no | Filter by command type (e.g. `process`) |

**Returns:** `{"success": bool, "run_id": str, "final_status": str, "stages": [...], "issues_summary": {...}, "all_issues": [...]}`

---

## Agentic workflows

### Workflow 1 — Onboard a legacy codebase

An agent receives a directory of unstructured Teradata DDL. No human intervention after handing off the source.

```python
# Step 1: Create the project
result = ships_scaffold(name="OMR", output="/projects")
project = result["project_dir"]

# Step 2: Fill in config/env/DEV.conf (agent writes this from known topology)

# Step 3: Harvest with auto-tokenise
result = ships_harvest(
    source="/legacy/ddl/",
    project=project,
    auto_tokenise=True,
    env_prefix="A_D01",
)
# Check for unclassified files and fix them
if result["unclassified"] > 0:
    # Investigate result["unclassified_files"]
    ...

# Step 4: Run the full pipeline
result = ships_process(
    project=project,
    env="DEV",
    env_config="config/env/DEV.conf",
    name="OMR",
    skip_generate=True,
    strict=True,
)

# Step 5: Check trust
if result.get("stages", {}).get("package", {}).get("trust_label") == "BLOCKED":
    # Inspect result["stages"]["inspect"]["findings"]
    ...

# Step 6: Verify and report
verify = ships_verify(project=project)
if verify["ready"]:
    print(f"Package ready: {verify['archive_path']}")
```

### Workflow 2 — CI/CD pipeline

A CI pipeline commits to main, which triggers the agent.

```python
result = ships_process(
    project=project_dir,
    source="src/ddl/",
    token_map="config/token_map.conf",
    env="DEV",
    env_config="config/env/DEV.conf",
    name=package_name,
    strict=True,       # abort on first error in CI
    auto_tokenise=False,  # token map is reviewed and committed
)

if not result["success"]:
    # Report failures to CI; exit non-zero
    failed = result["failed_stages"]
    # Read decisions for detail
    decisions = ships_decisions(project=project_dir)
    raise RuntimeError(f"Pipeline failed at: {failed}")

# Archive the package
archive = result["stages"]["package"]["archive_path"]
# Upload to artefact store
```

### Workflow 3 — Chat-driven packaging

A developer asks an agent: *"Package the OMR project for TST."*

The agent:
1. Calls `ships_process` with the TST environment config
2. Calls `ships_verify` to confirm readiness
3. Returns the archive path and trust label to the developer
4. Optionally calls `ships_explain_run` to summarise what the pipeline found

### Workflow 4 — Autonomous environment promotion

A package approved in DEV is automatically promoted to TST:

```python
# Build the TST package from the same source — same build number, different env
result = ships_package(
    project=project_dir,
    env="TST",
    env_config="config/env/TST.conf",
    name=package_name,
    # no build_number → auto-increment disabled via --no-increment equivalent
)

verify = ships_verify(project=project_dir)
if verify["ready"] and verify["trust_label"] == "READY":
    # Deploy
    deploy_result = ships_deploy(
        package_dir=extracted_package_dir,
        host=tst_host,
        user=deploy_user,
        password=deploy_pass,
    )
```

### Workflow 5 — Investigate a wave failure and roll back

Wave 3 failed during deployment. The agent rolls it back surgically without touching waves 1 and 2.

```python
# Dry-run first to see what would happen
dry_result = ships_rollback(
    manifest_path="logs/.deploy_manifest_20260509.json",
    host="", user="", password="",  # not needed for dry-run
    wave=3,
    dry_run=True,
)
# Review dry_result["objects"] to confirm planned actions

# If acceptable, execute
rollback_result = ships_rollback(
    manifest_path="logs/.deploy_manifest_20260509.json",
    host=prod_host,
    user=deploy_user,
    password=deploy_pass,
    wave=3,
)
```

---

## Trust Report in agentic context

Every `ships_package` call stamps a Trust Report into `BUILD.json`. The `trust_label` field is the primary gate for agent decision-making:

| Label | Meaning | Agent action |
|---|---|---|
| `READY` | All signals pass | Safe to deploy |
| `READY-WITH-CAVEATS` | Warnings present (inspect warnings, provenance absent) | Deploy with awareness; log caveats |
| `BLOCKED` | At least one signal failed (malformed tokens, lint errors, grant drift) | Do not deploy; investigate `all_issues` from `ships_explain_run` |

**Reading trust in a workflow:**
```python
package_result = ships_package(...)
trust_label = package_result["trust_label"]

if trust_label == "BLOCKED":
    # Read the specific failures
    explain = ships_explain_run(project=project_dir, command_filter="package")
    blocking_issues = [
        i for i in explain["all_issues"]
        if i.get("severity") == "error"
    ]
    raise RuntimeError(f"Package BLOCKED: {blocking_issues}")
```

---

## Decisions.json for agents

`ships_decisions` exposes the full pipeline audit trail. An agent can read stage outcomes, issue codes, and output counts without parsing human-readable text:

```python
decisions = ships_decisions(project=project_dir)
run = decisions["run"]

# Find all error-severity issues across all stages
errors = [
    {"stage": s["stage"], "code": i["code"], "message": i["message"]}
    for s in run.get("stages", [])
    for i in s.get("issues", [])
    if i.get("severity") == "error"
]

if errors:
    # Take corrective action based on issue codes
    for err in errors:
        if err["code"] == "INSPECT_TOKEN_MALFORMED":
            # Re-harvest with correct token map
            ...
        elif err["code"] == "ANALYSE_CYCLE":
            # Break the circular dependency
            ...
```

All issue codes are defined in `td_release_packager/orchestrator/issue_codes.py` and documented in `docs/AGENT_INTEGRATION.md`.

---

## Error handling

All tools return `{"success": False, "error": "..."}` on failure — no exceptions propagate to the agent. An agent should always check `result["success"]` before proceeding.

```python
result = ships_harvest(source=source_dir, project=project_dir)
if not result["success"]:
    logger.error("Harvest failed: %s", result["error"])
    sys.exit(1)
```

---

## Troubleshooting

**Server not appearing in Claude Code after configuration change**

Restart Claude Code or run `/mcp` to reload the server list.

**`ModuleNotFoundError: No module named 'ships_mcp'`**

The `cwd` in the MCP config must point to the `teradata-deployment-agent` directory, and you must use `uv run` so the virtual environment is activated.

**`ModuleNotFoundError: No module named 'teradatasql'`**

Deployment tools (ships_deploy, ships_deploy_explain, ships_rollback) require `teradatasql`. Install it: `uv add teradatasql` or `pip install teradatasql`.

**Tool returns `{"success": false, "error": "..."}` immediately**

The tool caught an exception. The `error` field contains the full exception message. Common causes: directory not found, missing `.conf` file, insufficient permissions, invalid token in DDL.

**Trust label is BLOCKED but inspect appears clean**

The Trust Report reads `decisions.json` for the last inspect stage. If the last inspect run predates the current payload, run `ships_inspect` again before `ships_package`.
