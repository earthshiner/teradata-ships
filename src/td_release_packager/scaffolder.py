"""
scaffolder.py — Project template scaffolder.

Creates a new release project directory with the complete
structure, sample properties files, a .gitignore, and placeholder
README. This is the developer's "start here" entry point.

Usage:
    python -m td_release_packager scaffold \
        --name MortgagePlatform \
        --output /path/to/projects \
        --environments DEV,TST,PRD

Produces:
    /path/to/projects/MortgagePlatform/
    ├── .build_counter                  ← auto-incremented by builder
    ├── .gitignore
    ├── README.md
    ├── config/
    │   └── properties/
    │       ├── DEV.properties
    │       ├── TEST.properties
    │       └── PROD.properties
    ├── payload/
    │   └── database/
    │       ├── pre-requisites/
    │       │   └── databases/
    │       ├── DCL/
    │       │   ├── inter_db/
    │       │   ├── roles/
    │       │   └── users/
    │       ├── DDL/
    │       │   ├── tables/
    │       │   ├── views/
    │       │   ├── join_indexes/
    │       │   ├── procedures/
    │       │   ├── macros/
    │       │   ├── functions/
    │       │   ├── triggers/
    │       │   └── ...
    │       ├── DML/
    │       └── post-install/
    └── releases/                        ← built packages land here
"""

import logging
import os
from typing import List

logger = logging.getLogger(__name__)


def scaffold_project(
    project_name: str,
    output_dir: str,
    environments: List[str] = None,
    sample_tokens: dict = None,
) -> str:
    """
    Create a new project directory with the full template structure.

    Args:
        project_name:   Name of the project (used as directory name).
        output_dir:     Parent directory where the project is created.
        environments:   List of environment names (default: DEV, TST, PRD).
        sample_tokens:  Optional dict of sample token names and descriptions
                        to include in the generated properties files.

    Returns:
        Absolute path to the created project directory.

    Raises:
        FileExistsError: If the project directory already exists.
    """
    if environments is None:
        environments = ["DEV", "TST", "PRD"]

    project_dir = os.path.join(output_dir, project_name)

    if os.path.exists(project_dir):
        raise FileExistsError(
            f"Project directory already exists: {project_dir}"
        )

    logger.info("Scaffolding project: %s", project_dir)

    # -- Create directory structure --
    _create_directories(project_dir)

    # -- Generate properties files --
    _generate_properties(project_dir, project_name, environments, sample_tokens)

    # -- Generate inspect.conf --
    _generate_inspect_config(project_dir)

    # -- Generate .build_counter --
    _generate_build_counter(project_dir)

    # -- Generate .gitignore --
    _generate_gitignore(project_dir)

    # -- Generate README.md --
    _generate_readme(project_dir, project_name, environments)

    # -- Generate sample _order.txt --
    _generate_sample_order_file(project_dir)

    logger.info("Project scaffolded: %s", project_dir)
    return project_dir


def _create_directories(project_dir: str):
    """Create the full directory hierarchy."""
    dirs = [
        # Config
        "config/properties",

        # Payload — system-scope objects (00_system phase)
        "payload/database/system/maps",
        "payload/database/system/roles",
        "payload/database/system/profiles",
        "payload/database/system/authorizations",
        "payload/database/system/foreign_servers",

        # Payload — pre-requisites (01_pre_requisites phase)
        "payload/database/pre-requisites/databases",
        "payload/database/pre-requisites/users",

        # Payload — DCL (02_dcl phase)
        "payload/database/DCL/roles",
        "payload/database/DCL/users",
        "payload/database/DCL/inter_db",

        # Payload — DDL (03_ddl phase)
        "payload/database/DDL/functions",
        "payload/database/DDL/JARs",
        "payload/database/DDL/join_indexes",
        "payload/database/DDL/macros",
        "payload/database/DDL/procedures",
        "payload/database/DDL/script_table_operators",
        "payload/database/DDL/tables",
        "payload/database/DDL/triggers",
        "payload/database/DDL/views",

        # Payload — DML (04_dml), post-install (05_post_install)
        "payload/database/DML",
        "payload/database/post-install",

        # Releases output
        "releases",
    ]

    for d in dirs:
        os.makedirs(os.path.join(project_dir, d), exist_ok=True)

    # Place .gitkeep in empty directories so Git tracks them
    for d in dirs:
        full = os.path.join(project_dir, d)
        if not os.listdir(full):
            with open(os.path.join(full, ".gitkeep"), 'w') as f:
                pass


def _generate_properties(
    project_dir: str,
    project_name: str,
    environments: List[str],
    sample_tokens: dict = None,
):
    """
    Generate a .properties file per environment.

    Layout:
        1. Environment metadata (SHIPS_ENV, ENV_PREFIX, SHIPS_PROJECT)
        2. Space allocations (most visible — DBAs need these first)
        3. Project tokens (databases, users, roles)

    All database/user/role tokens reference {{ENV_PREFIX}} and
    {{SHIPS_PROJECT}} so that promoting between environments
    only requires changing ENV_PREFIX (and space allocations).

    Args:
        project_dir:   Project root.
        project_name:  Logical project name.
        environments:  List of environment names.
        sample_tokens: Optional custom tokens (name → description).
    """
    # Clean project name for use in identifiers
    clean_name = project_name.replace(" ", "_").replace("-", "_")

    # Default environment prefix mapping
    env_prefix_map = {
        "DEV": f"D01",
        "TST": f"S01",
        "SIT": f"T",
        "UAT": f"A",
        "PPR": f"R",
        "PRD": f"P",
    }

    # Space allocation per environment
    space_map = {
        "DEV": ("1e9", "1e9"),
        "TST": ("5e9", "5e9"),
        "SIT": ("5e9", "5e9"),
        "UAT": ("10e9", "10e9"),
        "PPR": ("50e9", "50e9"),
        "PRD": ("50e9", "50e9"),
    }

    # Default project tokens
    if sample_tokens is None:
        sample_tokens = {
            "STD_DATABASE": "Standard (landing/staging) database",
            "SEM_DATABASE": "Semantic (business model) database",
            "OBS_DATABASE": "Observability database",
            "MEM_DATABASE": "Memory/state database",
            "CTR_DATABASE": "Contract/governance database",
            "ETL_USER": "ETL batch processing user",
            "DBA_ROLE": "DBA administration role",
        }

    for env in environments:
        env_upper = env.upper()
        prefix = env_prefix_map.get(env_upper, env_upper)
        perm, spool = space_map.get(env_upper, ("1e9", "1e9"))

        props_path = os.path.join(
            project_dir, "config", "properties", f"{env_upper}.properties"
        )

        with open(props_path, 'w', encoding='utf-8') as f:
            # -- Header --
            f.write(f"# {env_upper} Environment — {project_name}\n")
            f.write(f"#\n")
            f.write(f"# Usage: python -m td_release_packager package \\\n")
            f.write(f"#            --source . --env {env_upper} \\\n")
            f.write(f"#            --name {project_name} \\\n")
            f.write(f"#            --properties config/properties/{env_upper}.properties\n")
            f.write(f"#\n\n")

            # -- Section 1: Environment metadata --
            f.write(f"# ---- Environment ----\n\n")

            f.write(f"# Logical environment — must match --env at package time\n")
            f.write(f"SHIPS_ENV={env_upper}\n\n")

            f.write(f"# Physical prefix — maps to database naming convention\n")
            f.write(f"# (e.g. A_D01, A_S01, A_T, P — adjust per your topology)\n")
            f.write(f"ENV_PREFIX={prefix}\n\n")

            f.write(f"# Project identifier — used in database/user/role names\n")
            f.write(f"SHIPS_PROJECT={clean_name}\n\n")

            # -- Section 2: Space allocations --
            f.write(f"# ---- Space allocation ----\n\n")

            f.write(f"# Permanent space (bytes)\n")
            f.write(f"PERM_SPACE={perm}\n\n")

            f.write(f"# Spool space (bytes)\n")
            f.write(f"SPOOL_SPACE={spool}\n\n")

            # -- Section 3: Project tokens --
            f.write(f"# ---- Project tokens ----\n")
            f.write(f"# All values below resolve from ENV_PREFIX and SHIPS_PROJECT.\n")
            f.write(f"# No changes needed when promoting between environments.\n\n")

            for token_name, description in sample_tokens.items():
                value = _sample_value(env_upper, clean_name, token_name)
                f.write(f"# {description}\n")
                f.write(f"{token_name}={value}\n\n")

        logger.debug("Generated: %s", props_path)


def _sample_value(env: str, project_name: str, token_name: str) -> str:
    """
    Generate a sample token value using {{ENV_PREFIX}}_{{SHIPS_PROJECT}}.

    All database, user, and role tokens reference ENV_PREFIX and
    SHIPS_PROJECT so that the project tokens section is identical
    across all environment properties files.

    Args:
        env:           Environment name (DEV, TST, PRD).
        project_name:  Project name.
        token_name:    Token name.

    Returns:
        A sample value string.
    """
    if "DATABASE" in token_name:
        # Extract module suffix (STD, SEM, OBS, etc.)
        module = token_name.replace("_DATABASE", "")
        return "{{ENV_PREFIX}}_{{SHIPS_PROJECT}}_" + module

    elif "USER" in token_name:
        return "{{ENV_PREFIX}}_" + token_name

    elif "ROLE" in token_name:
        return "{{ENV_PREFIX}}_" + token_name

    else:
        return "{{ENV_PREFIX}}_" + token_name + "_VALUE"


def _generate_inspect_config(project_dir: str):
    """
    Generate the default config/inspect.conf file.

    Contains all validation rules with their default severities.
    Users can customise by setting rules to ERROR, WARNING, or OFF.

    Args:
        project_dir: Project root.
    """
    from td_release_packager.validate import generate_default_config

    config_path = os.path.join(project_dir, "config", "inspect.conf")

    # Don't overwrite if it already exists (user may have customised)
    if os.path.exists(config_path):
        logger.info("inspect.conf already exists — skipping.")
        return

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(generate_default_config())

    logger.info("Generated: %s", config_path)


def _generate_build_counter(project_dir: str):
    """
    Generate the .build_counter file initialised to 0.

    The builder reads and increments this file on each build.
    The file contains a single integer.

    Args:
        project_dir: Project root.
    """
    counter_path = os.path.join(project_dir, ".build_counter")
    with open(counter_path, 'w', encoding='utf-8') as f:
        f.write("0\n")

    logger.debug("Generated: %s", counter_path)


def _generate_gitignore(project_dir: str):
    """
    Generate a .gitignore appropriate for a release project.

    Ignores built packages, logs, and Python artefacts.
    Tracks the .build_counter (it should be committed).

    Args:
        project_dir: Project root.
    """
    gitignore = """# Teradata Release Project
# =======================

# Built packages (generated by td_release_packager build)
releases/*.zip
releases/*.tar.gz

# Deployment logs (generated by deploy.py)
logs/

# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
.eggs/
dist/
build/

# OS
.DS_Store
Thumbs.db
*.swp
*~

# IDE
.idea/
.vscode/
*.code-workspace

# DO track these:
# .build_counter   — auto-incremented build number (commit this!)
# config/properties/*.properties — environment token values
"""

    gitignore_path = os.path.join(project_dir, ".gitignore")
    with open(gitignore_path, 'w', encoding='utf-8') as f:
        f.write(gitignore)


def _generate_readme(project_dir: str, project_name: str, environments: List[str]):
    """Generate a project README.md with getting-started instructions."""
    env_list = ", ".join(environments)

    readme = f"""# {project_name}

Teradata release project managed by SHIPS (`td_release_packager`).

## SHIPS Workflow

```
[S] Scaffold  →  [H] Harvest  →  [I] Inspect  →  [P] Package  →  [S] Ship
```

## Environments

{env_list}

Properties files: `config/properties/<ENV>.properties`

## Project Structure

```
config/properties/       — Token values per environment
payload/database/
  pre-requisites/        — CREATE DATABASE, CREATE USER, CREATE PROFILE
  DCL/                   — GRANT statements (container-level)
  DDL/                   — Tables, views, indexes, procedures, etc.
  DML/                   — Reference data, seed data (use MERGE for idempotency)
  post-install/          — Validation queries, COLLECT STATISTICS, cleanup
releases/                — Built packages (.zip / .tar.gz)
```

## Tokens

Use `{{{{TOKENNAME}}}}` in any file under `payload/`. Token values
are defined in the environment properties files and resolved at
package time.

Scan for token usage:
```bash
python -m td_release_packager scan --source .
python -m td_release_packager scan --source . --properties config/properties/DEV.properties
```

## Packaging a Release

```bash
python -m td_release_packager package \\
    --source . \\
    --env DEV \\
    --name {project_name} \\
    --properties config/properties/DEV.properties \\
    --output releases/ \\
    --author "Your Name"
```

Build number auto-increments from `.build_counter`.
For same-source promotion to another environment:
```bash
python -m td_release_packager package \\
    --source . \\
    --env PROD \\
    --name {project_name} \\
    --properties config/properties/PROD.properties \\
    --output releases/ \\
    --no-increment
```

## Deploying a Package

Hand the `.zip` to the DBA. They unzip and run:
```bash
python deploy.py --host <teradata_host> --user <username> --dry-run
python deploy.py --host <teradata_host> --user <username>
```

## Deployment Order

Within each phase, files deploy alphabetically by default.
To specify a custom order (e.g. topological sort for table
dependencies), create an `_order.txt` file in the relevant
phase directory listing filenames one per line.
"""

    readme_path = os.path.join(project_dir, "README.md")
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write(readme)


def _generate_sample_order_file(project_dir: str):
    """
    Generate a sample _order.txt in the DDL/tables directory.

    Shows the developer the format for topological ordering.

    Args:
        project_dir: Project root.
    """
    order_path = os.path.join(
        project_dir, "payload", "database", "DDL", "tables", "_order.txt.sample"
    )

    with open(order_path, 'w', encoding='utf-8') as f:
        f.write("""# Deployment order for tables (topological sort).
# Rename this file to _order.txt to activate.
#
# List one filename per line, in the order they should
# be deployed. Dependencies must come before dependants.
# Blank lines and lines starting with '#' are ignored.
#
# Example:
# {{STD_DATABASE}}.Country.tbl
# {{STD_DATABASE}}.State.tbl
# {{STD_DATABASE}}.Customer.tbl
# {{STD_DATABASE}}.Account.tbl
""")
