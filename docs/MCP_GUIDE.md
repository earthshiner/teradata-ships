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

## Transports

SHIPS MCP supports three transports. Choose based on your deployment model.

| Transport | MCP spec | Use when |
|---|---|---|
| `stdio` | 2024-11-05 | Client launches the server as a subprocess (Claude Desktop, Claude Code) |
| `streamable-http` | 2025-03-26 | Enterprise: server runs as a standalone HTTP service; multiple clients connect over the network |
| `sse` | 2024-11-05 | Legacy SSE clients not yet migrated to streamable-http |

---

## Starting the server

**stdio (default) — subprocess transport:**
```bash
uv run python src/ships_mcp.py
# starts the server and waits for MCP protocol messages on stdin/stdout
```

**streamable-http — enterprise HTTP transport:**
```bash
# Bind to all interfaces, port 8000 (default endpoint: /mcp)
uv run python src/ships_mcp.py --transport streamable-http --host 0.0.0.0 --port 8000

# Stateless mode — new session per request, for serverless / load-balanced deployments
uv run python src/ships_mcp.py --transport streamable-http --host 0.0.0.0 --port 8000 --stateless

# Custom endpoint path
uv run python src/ships_mcp.py --transport streamable-http --port 8000 --path /api/ships
```

**sse — legacy SSE transport:**
```bash
uv run python src/ships_mcp.py --transport sse --host 0.0.0.0 --port 8000
```

**All flags:**

| Flag | Default | Description |
|---|---|---|
| `--transport` | `stdio` | Transport: `stdio`, `streamable-http`, or `sse` |
| `--host` | `127.0.0.1` | Bind address (HTTP transports only) |
| `--port` | `8000` | Listen port (HTTP transports only) |
| `--path` | `/mcp` or `/sse` | Endpoint URL path (HTTP transports only) |
| `--stateless` | off | New session per request — serverless/load-balanced deployments |
| `--log-level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

All HTTP settings may also be supplied via `FASTMCP_*` environment variables (`FASTMCP_HOST`, `FASTMCP_PORT`, `FASTMCP_LOG_LEVEL`, etc.). CLI flags take precedence.

> **TLS note:** Terminate TLS at a reverse proxy (nginx, API Gateway, etc.) in front of the server. The MCP server speaks plain HTTP; TLS is the responsibility of the network layer.

---

## Configuration

### Claude Code (stdio)

Add to your project's `.claude/settings.json` or your user settings:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src/ships_mcp.py"],
      "cwd": "/path/to/teradata-ships"
    }
  }
}
```

After saving, run `/mcp` in Claude Code to verify the server is connected and the tools appear.

### Claude Desktop (macOS) — stdio

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src/ships_mcp.py"],
      "cwd": "/path/to/teradata-ships"
    }
  }
}
```

### Claude Desktop (Windows) — stdio

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ships": {
      "command": "uv",
      "args": ["run", "python", "src\\ships_mcp.py"],
      "cwd": "C:\\path\\to\\teradata-ships"
    }
  }
}
```

### Enterprise deployment — streamable-http

Run the server as a long-lived service (systemd, Docker, Kubernetes, etc.) and point clients at it over HTTP:

```bash
# Start the server (example: all interfaces, port 8000)
uv run python src/ships_mcp.py --transport streamable-http --host 0.0.0.0 --port 8000
```

Client configuration (any MCP client that supports streamable-http):
```json
{
  "mcpServers": {
    "ships": {
      "url": "http://ships-mcp.internal:8000/mcp",
      "transport": "streamable-http"
    }
  }
}
```

For load-balanced or serverless deployments, add `--stateless` so each request gets an independent session without server-side session state.

### Generic stdio client

```python
import subprocess

proc = subprocess.Popen(
    ["uv", "run", "python", "src/ships_mcp.py"],
    cwd="/path/to/teradata-ships",
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
# Send MCP protocol messages via proc.stdin / proc.stdout
```

### Generic streamable-http client

```python
import httpx

# POST to the /mcp endpoint with MCP protocol JSON bodies
response = httpx.post(
    "http://ships-mcp.internal:8000/mcp",
    json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
)
```

---

## Authentication

JWT/Bearer token authentication is supported for HTTP transports (`streamable-http` and `sse`). SHIPS acts as an **OAuth 2.0 Resource Server** — it validates tokens issued by your identity provider using asymmetric key verification via JWKS. SHIPS does not issue tokens.

### Enabling authentication

```bash
python -m ships_mcp \
    --transport streamable-http \
    --host 0.0.0.0 \
    --port 8000 \
    --auth-jwks-uri https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys \
    --auth-issuer  https://login.microsoftonline.com/{tenant}/v2.0 \
    --auth-audience api://ships-mcp \
    --auth-required-scopes ships.read,ships.deploy \
    --auth-resource-url http://ships-mcp.internal:8000
```

Once enabled, every request to the MCP endpoint must include an `Authorization: Bearer <jwt>` header. Requests without a valid token receive HTTP 401; requests with a token lacking the required scopes receive HTTP 403.

### Auth flags

| Flag | Required | Description |
|---|---|---|
| `--auth-jwks-uri` | Yes (to enable auth) | JWKS endpoint for JWT signature verification |
| `--auth-issuer` | Recommended | Expected `iss` claim value |
| `--auth-audience` | Recommended | Expected `aud` claim value |
| `--auth-required-scopes` | No | Comma-separated scopes every caller must hold |
| `--auth-resource-url` | Yes (when `--auth-jwks-uri` set) | This server's public URL — used in `WWW-Authenticate` headers |

### Identity provider JWKS URIs

| Provider | JWKS URI |
|---|---|
| Azure AD / Entra ID | `https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys` |
| Okta | `https://{domain}/oauth2/default/v1/keys` |
| AWS Cognito | `https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json` |
| Keycloak | `https://{host}/realms/{realm}/protocol/openid-connect/certs` |
| Auth0 | `https://{domain}/.well-known/jwks.json` |

### JWT claim mapping

| JWT claim | Mapped to |
|---|---|
| `azp` (preferred) / `client_id` / `sub` | `AccessToken.client_id` |
| `scope` (space-separated string) | `AccessToken.scopes` |
| `scp` (list — Azure AD style) | `AccessToken.scopes` |
| `exp` | `AccessToken.expires_at` |

### Key rotation

SHIPS caches JWKS for 1 hour. On a cache miss (unknown `kid`), the cache is refreshed immediately before failing the request. This handles seamless key rotation by the identity provider without a server restart.

### Dependencies

Authentication requires `PyJWT[crypto]` and `httpx`, both declared in `requirements.txt` and installed by `uv sync`. Neither is required for stdio or unauthenticated HTTP deployments.

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

#### `ships_clean`

Wipe prior pipeline output to give a clean re-run surface. **Synchronous** (returns directly — no `run_id`, no `ships_poll_build`). Removes whole subtrees by `shutil.rmtree` and recreates them empty; never reconstructs per-file paths (which historically left differently-tokenised filenames behind across re-harvests).

Defaults to `dry_run=true` so an agent can preview the targets before applying. Refuses any directory missing `ships.yaml` (returns a clean error, never raises). `config/` and `.build_counter` are never touched.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory (must contain `ships.yaml`) |
| `scope` | string | no | One of `runs`, `payload` (default), `releases`, `reports`, `decisions`, `all` |
| `dry_run` | bool | no | If true (default), preview targets without deleting |

| Scope | Clears |
|---|---|
| `runs` | `.ships/runs/` (dispatch sentinels + logs) |
| `payload` | `payload/database/` (harvested + generated DDL/DCL/DML) |
| `releases` | `releases/` (built archives) |
| `reports` | `output/reports/` |
| `decisions` | `ships.decisions.json` (audit trail) |
| `all` | Everything above (`.build_counter` preserved) |

**Returns:** `{"success": bool, "scope": str, "dry_run": bool, "project_dir": str, "targets": [{"path": str, "kind": str, "exists": bool}], "removed_files": int, "removed_dirs": int, "lifecycle_state_after": str, "error": str}`

**Example (preview then apply):**
```
Tool: ships_clean
Args: {"project": "/projects/MortgagePlatform", "scope": "payload", "dry_run": true}
# review the targets, then
Args: {"project": "/projects/MortgagePlatform", "scope": "payload", "dry_run": false}
```

---

#### `ships_stage`

Stage SHIPS-owned paths (`ships.yaml`, `config/`, `payload/`) into the git index after gating on `ships scan` and `ships inspect` ([#487](https://github.com/earthshiner/teradata-ships/issues/487)). **Synchronous** (returns directly — no `run_id`, no `ships_poll_build`). If either gate reports an error, the index is left untouched and the envelope reports `blocked_by="scan"` or `blocked_by="inspect"`.

Bounded by design — does **not** commit, does **not** invoke `git commit`, configure signing, or skip hooks, and does **not** stage non-SHIPS files. The caller writes the commit message and runs `git commit` separately.

Refuses any directory missing `ships.yaml` (returns a clean error, never raises).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory (must contain `ships.yaml`) |
| `dry_run` | bool | no | If true, run the gates and report the paths that would be staged without touching the git index (default: false) |

**Returns:** `{"success": bool, "dry_run": bool, "project_dir": str, "staged_paths": [str, ...], "blocked_by": "scan" | "inspect" | null, "error": str | null, "scan_exit_code": int | null, "inspect_exit_code": int | null, "repo_root": str | null}`

`repo_root` is the absolute path of the enclosing git repository (matches `project_dir` when the project IS the repo root; differs when the project is nested in a monorepo). The tool refuses with a clean error before scan/inspect run if the project is not inside a git repo.

**Example (gate + stage):**
```
Tool: ships_stage
Args: {"project": "/projects/MortgagePlatform"}
# on success the caller follows up with its own:
#   git commit -m "<message>"
```

---

#### `ships_harvest`

Harvest raw DDL files from a source directory into a SHIPS project. Classifies each file by DDL content, injects MULTISET where missing, renames to the eponymous convention, and places files in the correct payload subdirectory.

> **Note — full-ingest auto-clean.** When `--keep-existing` is not set, harvest now wipes `payload/database/` by `shutil.rmtree` before placing fresh files, rather than diffing against the produced set. This guarantees a re-harvest cannot inherit any artefact of a prior run (e.g. differently-tokenised `.dcl` filenames). Pass `--keep-existing` for overlay/incremental semantics.

> **Note — `prefix_token` requires `auto_tokenise=true`.** Token substitution only runs when auto-tokenise is on. For consistent prefix tokens across DDL **and** DCL, pass `prefix_token=<SOURCE>=<TOKEN>` with `auto_tokenise=true` and **no** `env_prefix` (`env_prefix` drives the whole-name derivation that produces braced whole-name tokens in inter-database grants).


| Parameter | Type | Required | Description |
|---|---|---|---|
| `source` | string | yes | Directory containing raw DDL files |
| `project` | string | yes | Target SHIPS project directory |
| `token_map` | string | no | **[DEPRECATED — prefer `config/tokenise.conf`]** Path to `token_map.conf` for substitution. Still works; see [#388](https://github.com/earthshiner/teradata-ships/issues/388). |
| `auto_tokenise` | bool | no | Detect and apply tokens in one pass |
| `env_prefix` | string | no | Env prefix to strip (e.g. `A_D01`) |
| `remove_view_type_affixes` | bool | no | Remove redundant view object affixes (`v_` prefix and `_v` suffix) and update qualified references |

**Returns:** `{"success": bool, "classified": int, "unclassified": int, "files_placed": int, "token_candidates": int, "placement_index_dir": str, "placement_index_files": int, "view_type_affix_renames": int, "warnings": list, "unclassified_files": list}`

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

Build a release package for a target environment. Resolves all `{{TOKEN}}` references, assembles a self-contained archive, and stamps `context/ships.build.json` with provenance, integrity hash, and Trust Report.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `env` | string | yes | Target environment (DEV, TST, PRD) |
| `name` | string | yes | Package name |
| `env_config` | string | yes | Path to environment `.conf` file |
| `output` | string | no | Output directory |
| `author` | string | no | Builder identifier for provenance |
| `description` | string | no | Release description |
| `commit` | string | no | Git commit hash. Set automatically when using `source_github`. |
| `source_github` | string | no | Fetch DDL source from GitHub: `"owner/repo"`. Mutually exclusive with `project` source DDL. |
| `source_ref` | string | no | Branch, tag, or SHA to fetch (default: `"main"`). Used with `source_github`. |
| `github_token` | string | no | GitHub PAT for private repos. Falls back to `GITHUB_TOKEN` env var. |
| `root_parent` | string | no | Root database/user parent for parentless `CREATE DATABASE`/`USER` prereqs |
| `change_ref` | string | no | Change-management ticket reference (e.g. `CHG0012345`); required when the target env sets `require_change_ref` |
| `since_tag` | string | no | Build a **changeset-scoped** package of objects changed since this git tag/ref (plus dependants). See `ships_changeset` (#115). |
| `since_commit` | string | no | Changeset-scoped package since this git commit. |
| `objects` | string | no | Changeset-scoped package of an explicit comma-separated `DB.Object` list (plus dependants). For agent-driven partial deploys. |

**Returns:** `{"success": bool, "archive_path": str, "build_number": int, "file_count": int, "token_count": int, "trust_label": str, "warnings": list}`

The `trust_label` field is `READY`, `READY-WITH-CAVEATS`, or `BLOCKED`. An agent should check this before proceeding to deployment.

---

#### `ships_changeset`

Preview the changed objects + downstream dependants for a project (#114), so an agent can build a **minimal** package instead of the whole payload. Detection is git-native when `since_tag`/`since_commit` is given inside a git repo, with a content-hash baseline (`.ships/`) fallback; `objects` expands an explicit set by dependants. A forward walk over the dependency graph pulls in every object that transitively depends on a changed one. Feed the result to `ships_package` (same `since_tag`/`since_commit`/`objects`).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `since_tag` | string | no | Git tag/ref to diff HEAD against |
| `since_commit` | string | no | Git commit to diff HEAD against |
| `objects` | string | no | Explicit comma-separated `DB.Object` list |

**Returns:** `{"success": bool, "mode": "git"|"baseline"|"objects"|"none", "changed": [...], "dependants": [...], "selected": [...], "changed_files": [...], "note": str}`

---

#### `ships_plan`

Detect-and-recommend a packaging plan from a raw source tree (#379). Inspects the source read-only, auto-answers the detectable questions (filesystem source, `{{TOKEN}}` present, atomic vs compound files, DCL/DML), and returns the recommended ordered `ships` command sequence, a per-step rationale, and the canonical `plan.json` answers snapshot. This is the non-interactive, agent-facing form of the SHIPS Navigator / CLI wizard — all share one decision model and plan engine.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source` | string | yes | Raw source DDL directory to inspect |
| `project` | string | no | Target SHIPS project dir (default: the source dir) |
| `env` | string | no | Target environments, e.g. `"DEV,TST,PRD"` |
| `name` | string | no | Package name (default: `create_objects`) |
| `mode` | string | no | `"quick"` (one process per env) or `"detailed"` |
| `strict` | bool | no | Recommend `--strict` on process |
| `scaffolded` | bool | no | Project already scaffolded — omit the scaffold step |
| `no_generate` | bool | no | Skip the view-layer generate step |

**Returns:** `{"success": bool, "detected": [...], "notes": [...], "commands": [...], "rationale": [...], "plan": {...}}`

---

#### `ships_process`

Run the full pipeline in one call: harvest → generate → inspect → analyse → [package].

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | SHIPS project directory |
| `source` | string | no | Raw DDL source directory. Mutually exclusive with `source_github`. |
| `source_github` | string | no | Fetch DDL source from GitHub: `"owner/repo"`. Mutually exclusive with `source`. |
| `source_ref` | string | no | Branch, tag, or SHA to fetch (default: `"main"`). Used with `source_github`. |
| `github_token` | string | no | GitHub PAT for private repos. Falls back to `GITHUB_TOKEN` env var. |
| `token_map` | string | no | Token substitution map |
| `auto_tokenise` | bool | no | Auto-detect and apply tokens |
| `env_prefix` | string | no | Env prefix for auto-tokenise |
| `env` | string | no | Target environment. Omit to derive from the `packaging:` profile in `ships.yaml` (#384). |
| `env_config` | string | no | Env config file. Omit to derive from the `packaging:` profile / convention (#384). |
| `name` | string | no | Package name. Omit to derive from the `packaging:` profile / project name (#384). |
| `output` | string | no | Output directory for the built package archive |
| `root_parent` | string | no | Root database/user parent for parentless `CREATE DATABASE`/`USER` prereqs |
| `skip_generate` | bool | no | Skip view-layer generate stage |
| `strict` | bool | no | Abort on first stage error |
| `author` | string | no | Builder identifier for provenance |
| `description` | string | no | Release description |
| `commit` | string | no | Git commit hash |

**Returns:** `{"success": bool, "stages": {"harvest": {...}, "inspect": {...}, ...}, "failed_stages": list}`

**Single front door (#384):** with a `packaging:` profile in `ships.yaml`, `ships_process(project=...)` runs the whole pipeline (including package) with no `env`/`env_config`/`name` — precedence is explicit arg > `packaging:` profile > convention.

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

#### `ships_metadata_export`

Export AI-native data-product metadata for an enterprise catalogue (#244). Extracts a single neutral product-metadata model from a built package (identity, interfaces, physical assets, columns, lineage, trust, provenance, access, decisions) and renders a catalogue-ready bundle — **Alation**, **Collibra**, or **DataHub**. One extraction feeds any catalogue. Conservative: views/macros are approved consumer-facing interfaces, tables internal unless `include_internal`; ownership/glossary/AI-approval are emitted only when present in the package (never fabricated). SQL is parsed as text, never executed.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `package_dir` | string | yes | Root of an unpacked SHIPS package or release-group directory |
| `output` | string | yes | Output directory; bundle written to `<output>/<catalogue>/` |
| `catalogue` | string | no | `"alation"` (default), `"collibra"`, or `"datahub"` |
| `include_internal` | bool | no | Include internal implementation objects as interfaces |
| `strict` | bool | no | Fail if required product metadata is missing |

**Returns:** `{"success": bool, "catalogue": str, "output_dir": str, "files": [...], "interfaces": int, "assets": int, "columns": int, "warnings": [...]}`

---

## Agentic workflows

### AI-native data product discovery

For AI-native data products, MCP clients should discover the product
registry before querying Semantic module metadata or data entrypoints.
The registry contract, SHIPS-compatible DDL template, and recommended
resource shape are defined in
[`docs/references/ai_native_data_product_discovery.md`](references/ai_native_data_product_discovery.md).

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

Every `ships_package` call stamps a Trust Report into `context/ships.build.json`. The `trust_label` field is the primary gate for agent decision-making:

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

The `cwd` in the MCP config must point to the `teradata-ships` directory, and you must use `uv run` so the virtual environment is activated.

**`ModuleNotFoundError: No module named 'teradatasql'`**

Deployment tools (ships_deploy, ships_deploy_explain, ships_rollback) require `teradatasql`. Install it: `uv add teradatasql` or `pip install teradatasql`.

**Tool returns `{"success": false, "error": "..."}` immediately**

The tool caught an exception. The `error` field contains the full exception message. Common causes: directory not found, missing `.conf` file, insufficient permissions, invalid token in DDL.

**Trust label is BLOCKED but inspect appears clean**

The Trust Report reads `decisions.json` for the last inspect stage. If the last inspect run predates the current payload, run `ships_inspect` again before `ships_package`.
