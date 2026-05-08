# Installation Guide

## Prerequisites

- **Python 3.10+** (tested on 3.12 and 3.13)
- **Git** (for version control and repository cloning)
- **teradatasql** (required only for live deployment — the Ship phase)

The Scaffold, Harvest, Inspect, Analyse, and Package phases use only the Python standard library. No database connection is needed until deployment.

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

```bash
# Runtime only
pip install -r requirements.txt

# Runtime + testing
pip install -r requirements-dev.txt
```

### 4. Verify the Installation

```bash
# Check the packager loads
python -m td_release_packager --help

# Check the deployer loads
python -m database_package_deployer --help

# Run the test suite
python -m pytest src/tests/ -v --tb=short
```

Expected output from the test suite:

```
============================= 368 passed in 1.5s ==============================
```

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
pip install -r requirements.txt
python -m pytest src/tests/ -v --tb=short
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
