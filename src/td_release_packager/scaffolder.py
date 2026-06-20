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
    ├── ships.yaml                      ← project master config
    ├── config/
    │   └── env/
    │       ├── DEV.conf
    │       ├── TEST.conf
    │       └── PROD.conf
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
from pathlib import Path

logger = logging.getLogger(__name__)


def scaffold_project(
    project_name: str,
    output_dir: str,
    environments: List[str] = None,
    sample_tokens: dict = None,
    repair: bool = False,
) -> str:
    """
    Create a new project directory with the full template structure,
    or repair an existing one by adding missing directories and files.

    In repair mode:
        - Missing directories are created
        - Missing config files are generated
        - Existing files are NEVER overwritten
        - The .build_counter is preserved
        - Reports what was created vs what was skipped

    Args:
        project_name:   Name of the project (used as directory name).
        output_dir:     Parent directory where the project is created.
        environments:   List of environment names (default: DEV, TST, PRD).
        sample_tokens:  Optional dict of sample token names and descriptions
                        to include in the generated properties files.
        repair:         If True, repair an existing project instead of
                        creating a new one. Adds missing structure without
                        overwriting existing files.

    Returns:
        Absolute path to the created/repaired project directory.

    Raises:
        FileExistsError: If the project directory already exists
                         (only in non-repair mode).
        FileNotFoundError: If the project directory does not exist
                           (only in repair mode).
    """
    if environments is None:
        environments = ["DEV", "TST", "PRD"]

    project_dir = os.path.join(output_dir, project_name)

    if repair:
        if not os.path.exists(project_dir):
            raise FileNotFoundError(
                f"Cannot repair — project directory not found: {project_dir}"
            )
        logger.info("Repairing project: %s", project_dir)
    else:
        if os.path.exists(project_dir):
            raise FileExistsError(
                f"Project directory already exists: {project_dir}. "
                f"Use --repair to add missing directories and files."
            )
        logger.info("Scaffolding project: %s", project_dir)

    # -- Ensure the output directory exists --
    Path(project_dir).mkdir(parents=True, exist_ok=True)

    # -- Create directory structure (safe for existing dirs) --
    _create_directories(project_dir)

    # -- Generate files (skip existing in repair mode) --
    _generate_properties(
        project_dir,
        project_name,
        environments,
        sample_tokens,
        skip_existing=repair,
    )
    _generate_inspect_config(project_dir)  # Already skips existing
    _generate_ships_yaml(project_dir, project_name, environments, skip_existing=repair)
    _generate_object_placement_yaml(project_dir, skip_existing=repair)
    _generate_build_counter(project_dir, skip_existing=repair)
    _generate_gitignore(project_dir, skip_existing=repair)
    _generate_readme(project_dir, project_name, environments, skip_existing=repair)
    _generate_sample_order_file(project_dir, skip_existing=repair)

    action = "repaired" if repair else "scaffolded"
    logger.info("Project %s: %s", action, project_dir)
    return project_dir


def _create_directories(project_dir: str):
    """Create the full directory hierarchy."""
    dirs = [
        # Config
        "config/env",
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
        # SQLJ install scripts AND their .jar binaries live together
        # so the install script's CJ! references resolve to siblings
        # (./X.jar). See classifier.TYPE_TO_SUBDIR for the convention.
        "payload/database/DDL/jar_install",
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
            Path(os.path.join(full, ".gitkeep")).touch()


def _generate_properties(
    project_dir: str,
    project_name: str,
    environments: List[str],
    sample_tokens: dict = None,
    skip_existing: bool = False,
):
    """
    Generate a .conf file per environment.

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
        "DEV": "D01",
        "TST": "S01",
        "SIT": "T",
        "UAT": "A",
        "PPR": "R",
        "PRD": "P",
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

        props_path = os.path.join(project_dir, "config", "env", f"{env_upper}.conf")

        if skip_existing and os.path.exists(props_path):
            logger.info("Config file exists — skipping: %s", props_path)
            continue

        with open(props_path, "w", encoding="utf-8") as f:
            # -- Header --
            f.write(f"# {env_upper} Environment — {project_name}\n")
            f.write("#\n")
            f.write("# Usage: python -m td_release_packager package \\\n")
            f.write(f"#            --source . --env {env_upper} \\\n")
            f.write(f"#            --name {project_name} \\\n")
            f.write(f"#            --env-config config/env/{env_upper}.conf\n")
            f.write("#\n\n")

            # -- Section 1: Environment metadata --
            f.write("# ---- Environment ----\n\n")

            f.write("# Logical environment — must match --env at package time\n")
            f.write(f"SHIPS_ENV={env_upper}\n\n")

            f.write("# Physical prefix — maps to database naming convention\n")
            f.write("# (e.g. A_D01, A_S01, A_T, P — adjust per your topology)\n")
            f.write(f"ENV_PREFIX={prefix}\n\n")

            f.write("# Project identifier — used in database/user/role names\n")
            f.write(f"SHIPS_PROJECT={clean_name}\n\n")

            # -- Section 2: Space allocations --
            f.write("# ---- Space allocation ----\n\n")

            f.write("# Permanent space (bytes)\n")
            f.write(f"PERM_SPACE={perm}\n\n")

            f.write("# Spool space (bytes)\n")
            f.write(f"SPOOL_SPACE={spool}\n\n")

            # -- Section 2b: External parents (PR5a) --
            f.write("# ---- External parent databases ----\n")
            f.write(
                "# Comma-separated list of databases/users that already exist on the\n"
                "# target environment and that this package depends on as a CREATE\n"
                "# DATABASE/USER ... FROM <parent> target. Declaring them here lets\n"
                "# the build's environment-prereq gate confirm they're expected to\n"
                "# pre-exist, instead of emitting a DBA_INSTRUCTIONS.md amendment\n"
                "# step.\n"
                "#\n"
                "# DBC is always implicit; do not list it.\n"
                "#\n"
                "# Example (CallCentre reverse-harvested from a DBC export under\n"
                "# DataProducts):\n"
                "#   EXTERNAL_PARENTS=DATAPRODUCTS\n"
                "#\n"
                "# Default (none) preserves the historic DBA-review gate for any\n"
                "# external parent the package's prereqs reference.\n"
                "# EXTERNAL_PARENTS=\n\n"
            )

            # -- Section 3: Project tokens --
            f.write("# ---- Project tokens ----\n")
            f.write("# All values below resolve from ENV_PREFIX and SHIPS_PROJECT.\n")
            f.write("# No changes needed when promoting between environments.\n\n")

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
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(generate_default_config())

    logger.info("Generated: %s", config_path)


def _generate_ships_yaml(
    project_dir: str,
    project_name: str,
    environments: List[str],
    skip_existing: bool = False,
):
    """
    Generate the project's ``ships.yaml`` master config file.

    ``ships.yaml`` is the single entry point for project-level
    pipeline configuration: project name, target environments,
    pointers to the other config files, and per-stage settings.
    It is read by every SHIPS stage (inspect, package, ship, etc.)
    at runtime to resolve project-wide options.

    The generated file is the minimum required — all omitted settings
    fall back to Layer 1 defaults (developer-friendly: lenient
    strictness, continue on error).  The ``inspect`` block is included
    with its key options commented out so developers can see what is
    available without having to consult the documentation.

    Args:
        project_dir:    Project root directory.
        project_name:   Logical project name.
        environments:   List of target environment names.
        skip_existing:  If True, do not overwrite an existing file.
    """
    ships_yaml_path = os.path.join(project_dir, "ships.yaml")

    if skip_existing and os.path.exists(ships_yaml_path):
        logger.info("ships.yaml exists — skipping: %s", ships_yaml_path)
        return

    env_list = ", ".join(f'"{e}"' for e in environments)

    content = f"""\
# ships.yaml — SHIPS project master configuration
#
# This file is the single entry point for project-level pipeline
# settings.  All omitted values fall back to built-in defaults.
#
# Reference: docs/SHIPS_MODULE_ARGS.md

project: {project_name}
version: "1.0"
environments: [{env_list}]

# ---- Config file pointers ----
# Defaults shown — only override if you move the files.
#
# config:
#   inspect:   config/inspect.conf
#   placement: config/object_placement.yaml
#   tokens:    config/token_map.conf

# ---- Inspect-stage options ----
# Grant validation behaviour is configured in config/inspect.conf:
#
#   warn_extra_grants=ERROR     # ERROR blocks, WARNING/WARN reports, OFF suppresses
#                               # extra-only manual grant drift
#   warn_external_grants=INFO   # INFO surfaces (default), WARNING reports, ERROR
#                               # blocks, OFF suppresses external-grantee .dcl files
#                               # (grants to roles/databases outside the package)
"""

    with open(ships_yaml_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("Generated: %s", ships_yaml_path)


def _generate_object_placement_yaml(
    project_dir: str,
    skip_existing: bool = False,
):
    """
    Generate the project's ``object_placement.yaml`` starter file.

    The file controls how tables and views are separated across
    databases — a SHIPS architectural concern. Three strategies are
    available:

        colocated   Tables and views share the same database
                    (zero setup, no enforcement)
        separated   Pattern-based — e.g. {BASE}_T / {BASE}_V
        mapped      Explicit database-to-database pairs

    The starter file uses ``colocated`` — the only strategy that's a
    valid working configuration without any prior database setup.
    Inline comments document the alternatives so users can switch
    when their environment is ready.

    The placement-related lint rules (``object_placement``,
    ``public_grant_on_tables``, ``review_unmapped_grants``) all skip
    silently under ``colocated``, so a freshly scaffolded project is
    quiet by default and gets louder as the user opts into stricter
    placement.

    Args:
        project_dir:    Project root.
        skip_existing:  If True, don't overwrite an existing file
                        (repair mode).
    """
    yaml_path = os.path.join(project_dir, "object_placement.yaml")

    if skip_existing and os.path.exists(yaml_path):
        logger.info("object_placement.yaml exists — skipping.")
        return

    if os.path.exists(yaml_path):
        logger.info("object_placement.yaml already exists — skipping.")
        return

    content = """\
# ================================================================
# object_placement.yaml — Object Placement Strategy
# ================================================================
#
# Defines how tables and views are separated across databases for
# the SHIPS deployment pipeline. Read by:
#
#   - tools/migrate_view_references.py  (rewrites view DDL refs)
#   - src/td_release_packager/validate.py (rules: object_placement,
#     public_grant_on_tables, review_unmapped_grants)
#
# Three strategies are available — pick ONE:
#
#   colocated   Tables and views share the same database.
#               No architectural enforcement. Use this when
#               starting out, or when the project legitimately
#               does not separate tables from views.
#
#   separated   Pattern-based derivation. Tables and views live
#               in distinct databases whose names are related by
#               a shared template (suffix, prefix, midfix).
#
#   mapped      Explicit pairs of tables_database / views_database.
#               Use when the naming is irregular and patterns
#               cannot derive one from the other.


# ----------------------------------------------------------------
# DEFAULT — separated (recommended SHIPS standard)
# ----------------------------------------------------------------
#
# Tables and views live in distinct databases related by the
# suffix convention below. Every table has a 1:1 locking view in
# the sibling views database — the recommended Teradata security
# pattern.
#
# Example: tables in 'D01_PROJECT_DOM_T'; matching views database
# is 'D01_PROJECT_DOM_V'.
#
# Swap to a prefix or midfix variant by editing the patterns:
#   prefix:  database_pattern_tables: "T_{BASE}"
#            database_pattern_views:  "V_{BASE}"
#   midfix:  database_pattern_tables: "{REGION}_T_{DOMAIN}"
#            database_pattern_views:  "{REGION}_V_{DOMAIN}"

strategy: separated
database_pattern_tables: "{BASE}_T"
database_pattern_views: "{BASE}_V"
locking_views: true


# ----------------------------------------------------------------
# Alternative — mapped (explicit pairs)
# ----------------------------------------------------------------
#
# Use when project naming is irregular, or when migrating from a
# legacy schema that doesn't follow a consistent convention.
#
#   strategy: mapped
#   locking_views: true
#   database_map:
#     - tables_database: MyProject_Domain_T
#       views_database: MyProject_Domain_V
#     - tables_database: "{{DOM_DATABASE_T}}"
#       views_database: "{{DOM_DATABASE_V}}"


# ----------------------------------------------------------------
# Alternative — colocated (disable the standard)
# ----------------------------------------------------------------
#
# Tables and views share the same database — no architectural
# enforcement. Reserved for projects that legitimately cannot
# separate tables from views; the placement-related lint rules
# (object_placement, public_grant_on_tables,
# review_unmapped_grants) skip silently under colocated.
#
#   strategy: colocated
#   locking_views: true
"""

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("Generated: %s", yaml_path)


def _generate_build_counter(project_dir: str, skip_existing: bool = False):
    """
    Generate the .build_counter file initialised to 0.

    The builder reads and increments this file on each build.
    The file contains a single integer.

    Args:
        project_dir:    Project root.
        skip_existing:  If True, don't overwrite existing counter.
    """
    counter_path = os.path.join(project_dir, ".build_counter")

    if skip_existing and os.path.exists(counter_path):
        logger.info("Build counter exists — skipping: %s", counter_path)
        return

    with open(counter_path, "w", encoding="utf-8") as f:
        f.write("0\n")

    logger.debug("Generated: %s", counter_path)


def _generate_gitignore(project_dir: str, skip_existing: bool = False):
    """
    Generate a .gitignore appropriate for a release project.

    Ignores built packages, logs, and Python artefacts.
    Tracks the .build_counter (it should be committed).

    Args:
        project_dir:    Project root.
        skip_existing:  If True, don't overwrite existing .gitignore.
    """
    gitignore_path = os.path.join(project_dir, ".gitignore")

    if skip_existing and os.path.exists(gitignore_path):
        logger.info(".gitignore exists — skipping.")
        return

    gitignore = """# Teradata Release Project
# =======================

# Built package release groups (generated by td_release_packager build)
releases/

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
# config/env/*.conf — environment token values
"""

    with open(gitignore_path, "w", encoding="utf-8") as f:
        f.write(gitignore)


def _generate_readme(
    project_dir: str,
    project_name: str,
    environments: List[str],
    skip_existing: bool = False,
):
    """Generate a project README.md with getting-started instructions."""
    readme_path = os.path.join(project_dir, "README.md")

    if skip_existing and os.path.exists(readme_path):
        logger.info("README.md exists — skipping.")
        return

    env_list = ", ".join(environments)

    readme = f"""# {project_name}

Teradata release project managed by SHIPS (`td_release_packager`).

## SHIPS Workflow

```
[S] Scaffold  →  [H] Harvest  →  [I] Inspect  →  [P] Package  →  [S] Ship
```

## Environments

{env_list}

Config files: `config/env/<ENV>.conf`

## Project Structure

```
config/env/       — Token values per environment
config/inspect.conf      — Validation rule severities
object_placement.yaml    — Tables/views database separation strategy
payload/database/
  pre-requisites/        — CREATE DATABASE, CREATE USER, CREATE PROFILE
  DCL/                   — GRANT statements (container-level)
  DDL/                   — Tables, views, indexes, procedures, etc.
  DML/                   — Reference data, seed data (use MERGE for idempotency)
  post-install/          — Validation queries, COLLECT STATISTICS, cleanup
releases/                — Built release-group directories containing packages, checksums, release_group.json, and README.txt
```

## Tokens

Use `{{{{TOKENNAME}}}}` in any file under `payload/`. Token values
are defined in the environment properties files and resolved at
package time.

Scan for token usage:
```bash
python -m td_release_packager scan --source .
python -m td_release_packager scan --source . --env-config config/env/DEV.conf
```

## Object Placement

`object_placement.yaml` declares how tables and views are separated
across databases. Three strategies:

- **colocated** — tables and views share the same database. Zero
  setup, no enforcement. **This is the scaffolded default.**
- **separated** — pattern-based, e.g. `{{BASE}}_T` for tables and
  `{{BASE}}_V` for views. Recommended once you have separate
  databases for the two roles.
- **mapped** — explicit `tables_database` / `views_database` pairs.
  Use when naming is irregular.

The placement-related lint rules in `inspect.conf`
(`object_placement`, `public_grant_on_tables`,
`review_unmapped_grants`) all skip silently under `colocated`, so a
freshly scaffolded project is quiet by default. They get louder as
you opt into stricter placement — see the comments inside
`object_placement.yaml` for the syntax of each strategy.

## Packaging a Release

```bash
python -m td_release_packager package \\
    --source . \\
    --env DEV \\
    --name {project_name} \\
    --env-config config/env/DEV.conf \\
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
    --env-config config/env/PROD.conf \\
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

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme)


def _generate_sample_order_file(project_dir: str, skip_existing: bool = False):
    """
    Generate a sample _order.txt in the DDL/tables directory.

    Shows the developer the format for topological ordering.

    Args:
        project_dir:    Project root.
        skip_existing:  If True, don't overwrite existing file.
    """
    order_path = os.path.join(
        project_dir, "payload", "database", "DDL", "tables", "_order.txt.sample"
    )

    if skip_existing and os.path.exists(order_path):
        logger.info("Sample order file exists — skipping.")
        return

    with open(order_path, "w", encoding="utf-8") as f:
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
