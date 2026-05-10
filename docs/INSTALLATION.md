# Installation Guide

## Prerequisites

- **Python 3.13+** (the project targets 3.13; earlier versions are not tested)
- **Git** (for version control and repository cloning)
- **teradatasql** (required only for live deployment — the Ship phase)
- **mcp** (required only for the MCP server — installed automatically by `uv sync`)

The Scaffold, Harvest, Inspect, Generate, Analyse, and Package phases run entirely offline — no database connection needed until deployment.

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/teradata/teradata-deployment-agent.git
cd teradata-deployment-agent
```

### 2. Create a Virtual Environment (Recommended)

```bash
# Linux / macOS
python -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Windows (Command Prompt)
python -m venv .venv
.venv\Scripts\activate.bat
```

### 3. Install Dependencies

SHIPS uses `uv` for dependency management (recommended):

```bash
# Install uv if you don't have it
pip install uv

# Install all dependencies (runtime + dev)
uv sync
```

Or with plain pip:

```bash
pip install teradatasql pyyaml mcp
```

### 3a. Optional extras

Install optional extras only when you need them:

```bash
# OpenTelemetry tracing — emit spans to Jaeger, Grafana Tempo, Datadog, etc.
uv pip install -e ".[otel]"

# OpenLineage client — richer transport options beyond stdlib HTTP/file
uv pip install -e ".[lineage]"

# Both
uv pip install -e ".[otel,lineage]"
```

The core pipeline (Scaffold, Harvest, Inspect, Analyse, Package, Deploy) works without either extra. OTel and OpenLineage emit a no-op when their packages are not installed **or** when their environment variables are not set — no configuration required to keep them silent.

### 4. Verify the Installation

```bash
# Check the pipeline CLI loads
uv run python -m td_release_packager --help

# Check the deployment CLI loads
uv run python -m database_package_deployer --help

# Check the MCP server loads (optional — requires mcp)
uv run python src/ships_mcp.py --help

# Run the test suite
uv run pytest
```

Expected output from the test suite: all tests pass. If any test fails on a clean install, please open an issue.

## Running from Any Directory

SHIPS is designed to be run from the `src/` directory within the repository. If you prefer to run it from anywhere, add the `src/` directory to your `PYTHONPATH`:

```bash
# Linux / macOS — add to ~/.bashrc or ~/.zshrc
export PYTHONPATH="/path/to/teradata-deployment-agent/src:$PYTHONPATH"

# Windows — add to system environment variables
set PYTHONPATH=C:\path\to\teradata-deployment-agent\src;%PYTHONPATH%
```

Alternatively, on Windows you can create a convenience wrapper:

```powershell
# Save as ships.ps1 in a directory on your PATH
$env:PYTHONPATH = "C:\Tools\teradata-deployment-agent\src"
python -m td_release_packager $args
```

Then invoke as:

```powershell
ships scaffold --name MyProject --output C:\Projects
```

## MCP Server

The `mcp` package is included in the standard `uv sync` and enables any MCP-compatible client to drive SHIPS without subprocess invocation.

```bash
# Start the MCP server manually (for testing)
uv run python src/ships_mcp.py
```

For Claude Code, Claude Desktop, or other clients, see **[docs/MCP_GUIDE.md](./MCP_GUIDE.md)** for the full setup instructions and tool reference.

---

## Teradata Connectivity

The `teradatasql` driver is required only for the Ship (deploy) phase. All other phases work offline.

### Verifying Connectivity

```bash
python -c "
import teradatasql
conn = teradatasql.connect(host='your-server', user='your-user', password='your-pass')
cur = conn.cursor()
cur.execute('SELECT CURRENT_TIMESTAMP')
print('Connected:', cur.fetchone()[0])
conn.close()
"
```

### Logon Mechanisms

SHIPS supports all `teradatasql` logon mechanisms via the `--logmech` flag:

```bash
python deploy.py --host myserver --user dbc --logmech LDAP
python deploy.py --host myserver --user dbc --logmech TD2
```

## Upgrading

```bash
cd teradata-deployment-agent
git pull origin main
uv sync
uv run pytest src/tests/ -q
```

Always run the test suite after upgrading to confirm compatibility.

## Troubleshooting

### ImportError: No module named 'td_release_packager'

Your working directory must be `src/`, or `src/` must be on `PYTHONPATH`. See the "Running from Any Directory" section above.

### ImportError: cannot import name 'X' from 'module'

Stale bytecode cache. Clear it:

```powershell
# Windows
Get-ChildItem -Path . -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

# Linux / macOS
find . -type d -name __pycache__ -exec rm -rf {} +
```

### teradatasql not found

The `teradatasql` package is required only for live deployment. Install it:

```bash
pip install teradatasql
```

If you are only using the Scaffold, Harvest, Inspect, or Package phases, you do not need it.
