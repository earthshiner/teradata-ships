"""
builder.py — Release package builder.

The developer's tool. Takes a source directory containing DDL, DCL,
and DML files with {{TOKEN}} placeholders, resolves tokens for a
target environment, and produces a self-contained release package
that a DBA can deploy without any knowledge of the build process.

Build process:
    1. Read environment properties (token values).
    2. Scan source files for {{TOKEN}} references.
    3. Validate: all referenced tokens must be defined.
    4. Create package directory structure.
    5. Copy and resolve payload files (substitute tokens).
    6. Embed the deployment engine (ddl_deployer library).
    7. Generate BUILD.json manifest.
    8. Generate deploy.py (DBA entry point).
    9. Generate README.txt (DBA instructions).
   10. Archive as .zip or .tar.gz.

Package naming:
    {{ENV}}_{{PACKAGE_NAME}}_BUILD_{{BUILD_NO}}_{{TIMESTAMP}}.zip
"""

import glob
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from td_release_packager.models import (
    BuildConfig,
    BuildManifest,
    DeployPhase,
    SOURCE_DIR_MAP,
    DDL_SUBDIR_ORDER,
    DCL_SUBDIR_ORDER,
)
from td_release_packager.token_engine import (
    read_properties,
    scan_tokens_in_directory,
    substitute_file,
    substitute_tokens,
    validate_tokens,
)

logger = logging.getLogger(__name__)

# -- Regex for MULTISET injection (duplicated from ddl_deployer --
# to avoid import dependency at build time)
import re

_HAS_SET_MULTISET_RE = re.compile(
    r'CREATE\s+(MULTISET|SET)\s+(?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?TABLE\b',
    re.IGNORECASE,
)
_INJECT_MULTISET_RE = re.compile(
    r'(CREATE\s+)((?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?TABLE\b)',
    re.IGNORECASE,
)

# -- Qualified name extraction for filename resolution --
# Extracts Database.ObjectName from resolved DDL content.
# Used to derive the eponymous filename after token substitution.
_QUALIFIED_NAME_RE = re.compile(
    r'(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?'
    r'(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?'
    r'(?:TRACE\s+)?'
    r'(?:SPECIFIC\s+)?'
    r'(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|'
    r'JOIN\s+INDEX|HASH\s+INDEX)\s+'
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)


def _resolve_filename(
    original_filename: str,
    resolved_content: str,
) -> str:
    """
    Derive the correct eponymous filename from resolved DDL content.

    After token substitution, the DDL contains the environment-specific
    database name (e.g. P_CORE.Customer). The package filename should
    match this — not the harvested source name (e.g. DEV01_CORE.Customer).

    For files where a qualified name cannot be extracted (grants, revokes,
    .c/.h co-artefacts, system-scope objects), the original filename is
    returned unchanged.

    Args:
        original_filename:  The source filename (e.g. 'DEV01_CORE.Customer.tbl').
        resolved_content:   The DDL content after token substitution.

    Returns:
        The resolved filename (e.g. 'P_CORE.Customer.tbl').
    """
    # Preserve extension from the original filename
    ext = os.path.splitext(original_filename)[1]

    # Skip non-DDL files (.c, .h, .jar, etc.)
    if ext.lower() in ('.c', '.h', '.jar', '.zip', '.gz'):
        return original_filename

    # Skip hidden/underscore-prefixed files
    if original_filename.startswith('.') or original_filename.startswith('_'):
        return original_filename

    # Extract the qualified name from the resolved content
    match = _QUALIFIED_NAME_RE.search(resolved_content)
    if not match:
        return original_filename

    qualified = match.group(1).replace('"', '')
    new_filename = f"{qualified}{ext}"

    if new_filename != original_filename:
        logger.info(
            "Filename resolved: %s → %s",
            original_filename, new_filename
        )

    return new_filename


def build_package(config: BuildConfig) -> Tuple[str, BuildManifest]:
    """
    Build a release package from source files and environment properties.

    Args:
        config: BuildConfig with source directory, environment,
                package name, build number, and properties file.

    Returns:
        Tuple of (archive_path, BuildManifest).

    Raises:
        FileNotFoundError: If source directory or properties file missing.
        ValueError: If token validation fails (undefined tokens).
    """
    timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%d%H%M%S")

    # -- Resolve build number --
    if config.build_number is None:
        # Auto-increment from .build_counter in source project
        from td_release_packager.build_counter import next_build_number
        build_int = next_build_number(config.source_dir)
        logger.info("Auto-incremented build number: %d", build_int)
    else:
        build_int = config.build_number

    build_no = f"{build_int:04d}"

    # -- Package naming --
    pkg_name = (
        f"{config.environment}_{config.package_name}"
        f"_BUILD_{build_no}_{ts_str}"
    )
    pkg_dir = os.path.join(config.output_dir, pkg_name)

    logger.info("Building package: %s", pkg_name)

    # -- Validate source directory --
    if not os.path.isdir(config.source_dir):
        raise FileNotFoundError(
            f"Source directory not found: {config.source_dir}"
        )

    # -- Phase 1: Read token values --
    token_values = read_properties(config.properties_file)
    logger.info("Loaded %d token values from %s",
                len(token_values), config.properties_file)

    # -- Phase 2: Scan source for token references --
    payload_dir = _find_payload_dir(config.source_dir)
    token_usage = scan_tokens_in_directory(payload_dir)
    logger.info("Scanned %d files with token references", len(token_usage))

    # -- Phase 3: Validate tokens --
    errors, warnings = validate_tokens(token_values, token_usage)
    for w in warnings:
        logger.warning("Token: %s", w)

    if errors:
        for e in errors:
            logger.error("Token: %s", e)
        raise ValueError(
            f"Token validation failed: {len(errors)} error(s). "
            "All referenced tokens must be defined in the properties file."
        )

    # -- Phase 4: Create package structure --
    _create_package_structure(pkg_dir)

    # -- Phase 5: Copy and resolve payload files --
    total_subs, file_count, phase_inventory = _copy_payload(
        payload_dir, pkg_dir, token_values
    )
    logger.info(
        "Resolved %d tokens across %d files",
        total_subs, file_count
    )

    # -- Phase 6: Copy deployment order files if present --
    _copy_order_files(payload_dir, pkg_dir)
    _copy_waves_file(config.source_dir, payload_dir, pkg_dir)

    # -- Phase 7: Embed deployment engine --
    _embed_deployer(pkg_dir)

    # -- Phase 8: Generate BUILD.json --
    manifest = BuildManifest(
        build_number=build_no,
        environment=config.environment,
        package_name=config.package_name,
        package_filename=f"{pkg_name}.{config.archive_format}",
        timestamp=timestamp.isoformat(),
        author=config.author,
        description=config.description,
        source_commit=config.source_commit,
        token_count=total_subs,
        file_count=file_count,
        phase_inventory=phase_inventory,
        tokens_resolved={k: v for k, v in sorted(token_values.items())},
        warnings=warnings,
    )

    manifest_path = os.path.join(pkg_dir, "BUILD.json")
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)

    # -- Phase 9: Generate deploy.py --
    _generate_deploy_script(pkg_dir, manifest)

    # -- Phase 10: Generate README.txt --
    _generate_readme(pkg_dir, manifest)

    # -- Phase 11: Generate shell wrappers --
    _generate_shell_wrappers(pkg_dir)

    # -- Phase 12: Archive --
    archive_path = _archive_package(pkg_dir, config.archive_format)
    logger.info("Package built: %s", archive_path)

    return (archive_path, manifest)


# ---------------------------------------------------------------
# Internal — Source discovery
# ---------------------------------------------------------------

def _find_payload_dir(source_dir: str) -> str:
    """
    Locate the payload directory within the source project.

    Looks for a 'payload' subdirectory. If not found, checks for
    a 'database' subdirectory (Paul's original structure).

    Args:
        source_dir: Root of the source project.

    Returns:
        Absolute path to the payload directory.

    Raises:
        FileNotFoundError: If no payload directory is found.
    """
    for candidate in ['payload', 'database', 'payload/database']:
        path = os.path.join(source_dir, candidate)
        if os.path.isdir(path):
            return path

    raise FileNotFoundError(
        f"No payload directory found in {source_dir}. "
        "Expected 'payload/' or 'database/' subdirectory."
    )


# ---------------------------------------------------------------
# Internal — Package structure
# ---------------------------------------------------------------

def _create_package_structure(pkg_dir: str):
    """
    Create the package directory skeleton.

    Args:
        pkg_dir: Root of the package being built.
    """
    dirs = [
        "config",
        "lib",
        "logs",
        "payload/01_pre_requisites",
        "payload/02_dcl",
        "payload/03_ddl",
        "payload/04_dml",
        "payload/05_post_install",
    ]

    for d in dirs:
        os.makedirs(os.path.join(pkg_dir, d), exist_ok=True)


# ---------------------------------------------------------------
# Internal — Payload copying with token substitution
# ---------------------------------------------------------------

def _copy_payload(
    source_payload: str,
    pkg_dir: str,
    token_values: Dict[str, str],
) -> Tuple[int, int, Dict[str, int]]:
    """
    Copy source payload files to the package, substituting tokens
    and resolving filenames.

    Maps source directory structure to the numbered phase structure.
    Files within each phase preserve their sub-directory hierarchy.
    Filenames are derived from the resolved DDL content so that the
    package filename matches the environment-specific database name
    (e.g. P_CORE.Customer.tbl, not DEV01_CORE.Customer.tbl).

    Args:
        source_payload: Path to the source payload directory.
        pkg_dir:        Package root directory.
        token_values:   Token name → value dictionary.

    Returns:
        Tuple of (total_substitutions, file_count, phase_inventory).
    """
    total_subs = 0
    file_count = 0
    phase_inventory = {}

    for root, dirs, files in os.walk(source_payload):
        for filename in files:
            src_file = os.path.join(root, filename)
            rel_path = os.path.relpath(src_file, source_payload)

            # Determine which phase this file belongs to
            phase, sub_path = _map_to_phase(rel_path)

            if phase is None:
                logger.warning(
                    "File '%s' does not map to any deployment phase — skipping.",
                    rel_path
                )
                continue

            # Read and resolve content, then resolve filename
            try:
                with open(src_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Substitute tokens in content
                resolved_content, subs = substitute_tokens(content, token_values)
                total_subs += subs

                # Resolve filename from the resolved DDL content
                resolved_filename = _resolve_filename(filename, resolved_content)

                # Build destination path with resolved filename
                # Replace the original filename in sub_path
                sub_dir = os.path.dirname(sub_path)
                dest_file = os.path.join(
                    pkg_dir, "payload", phase.value, sub_dir, resolved_filename
                )

                # Write resolved content
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                with open(dest_file, 'w', encoding='utf-8') as f:
                    f.write(resolved_content)

                # Inject MULTISET for table DDL files if missing
                if resolved_filename.endswith('.tbl'):
                    _inject_multiset_in_file(dest_file)

                file_count += 1
                phase_key = phase.value
                phase_inventory[phase_key] = phase_inventory.get(phase_key, 0) + 1

            except UnicodeDecodeError:
                # Binary file — copy without substitution or rename
                dest_file = os.path.join(
                    pkg_dir, "payload", phase.value, sub_path
                )
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                file_count += 1
                phase_key = phase.value
                phase_inventory[phase_key] = phase_inventory.get(phase_key, 0) + 1

    return (total_subs, file_count, phase_inventory)


def _inject_multiset_in_file(file_path: str):
    """
    Inject MULTISET into a .tbl file if neither SET nor MULTISET is specified.

    Modifies the file in place. Called at BUILD time so the packaged
    DDL shows exactly what will be deployed — no surprises for the DBA.

    Args:
        file_path: Path to the resolved .tbl file.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if _HAS_SET_MULTISET_RE.search(content):
        return  # Already has SET or MULTISET

    modified = _INJECT_MULTISET_RE.sub(r'\1MULTISET \2', content, count=1)

    if modified != content:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(modified)
        logger.info(
            "MULTISET injected at build time: %s",
            os.path.basename(file_path)
        )


def _map_to_phase(rel_path: str) -> Tuple[Optional[DeployPhase], str]:
    """
    Map a source-relative path to a deployment phase.

    Examines the path components to find a match in SOURCE_DIR_MAP.
    Returns the phase and the remaining sub-path.

    Args:
        rel_path: Path relative to the payload root.

    Returns:
        Tuple of (DeployPhase_or_None, remaining_sub_path).
    """
    parts = rel_path.replace('\\', '/').split('/')

    for i, part in enumerate(parts):
        if part in SOURCE_DIR_MAP:
            phase = SOURCE_DIR_MAP[part]
            sub_path = '/'.join(parts[i + 1:])
            return (phase, sub_path)

    return (None, rel_path)


def _copy_order_files(source_payload: str, pkg_dir: str):
    """
    Copy any _order.txt files from source to the package payload.

    These files define topological deployment order within a phase.

    Args:
        source_payload: Source payload directory.
        pkg_dir:        Package root directory.
    """
    for root, dirs, files in os.walk(source_payload):
        for filename in files:
            if filename == '_order.txt':
                src = os.path.join(root, filename)
                rel = os.path.relpath(src, source_payload)
                phase, sub = _map_to_phase(rel)
                if phase:
                    dest = os.path.join(pkg_dir, "payload", phase.value, sub)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src, dest)


def _copy_waves_file(project_dir: str, source_payload: str, pkg_dir: str):
    """
    Copy _waves.txt from the project root into the package payload,
    transforming paths from source-relative to package-relative.

    The analyser writes _waves.txt at the project root with paths
    like 'payload/database/DDL/tables/DB.Table.tbl'. The deploy.py
    template expects _waves.txt inside each phase directory with
    paths relative to that phase (e.g. 'tables/DB.Table.tbl').

    Args:
        project_dir:     Project root directory.
        source_payload:  Source payload directory (payload/database/).
        pkg_dir:         Package root directory.
    """
    waves_src = os.path.join(project_dir, "_waves.txt")
    if not os.path.exists(waves_src):
        return

    # Read and transform paths, grouping by phase
    # All DDL objects go into one phase (03_ddl typically)
    phase_lines = {}  # phase_value → list of transformed lines

    with open(waves_src, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.rstrip('\n').rstrip('\r')

            # Comments and blank lines — copy to all phases
            if not stripped or stripped.startswith('#') or stripped == '---':
                for phase_val in phase_lines:
                    phase_lines[phase_val].append(stripped)
                # If no phases seen yet, buffer for later
                if not phase_lines:
                    phase_lines.setdefault('_buffer', []).append(stripped)
                continue

            # File path — transform from source-relative to package-relative
            # Strip 'payload/database/' or 'payload\database\' prefix
            path_normalised = stripped.replace('\\', '/')
            rel_to_payload = path_normalised
            for prefix in ['payload/database/', 'payload\\database\\']:
                norm_prefix = prefix.replace('\\', '/')
                if path_normalised.startswith(norm_prefix):
                    rel_to_payload = path_normalised[len(norm_prefix):]
                    break

            # Map to phase
            phase, sub_path = _map_to_phase(rel_to_payload)
            if phase is None:
                logger.warning(
                    "_waves.txt: could not map path to phase: %s",
                    stripped,
                )
                continue

            phase_val = phase.value
            if phase_val not in phase_lines:
                # Flush buffer (comments from before first file)
                phase_lines[phase_val] = phase_lines.pop('_buffer', [])

            phase_lines[phase_val].append(sub_path)

    # Remove any unused buffer
    phase_lines.pop('_buffer', None)

    # Write one _waves.txt per phase
    for phase_val, lines in phase_lines.items():
        dest = os.path.join(pkg_dir, "payload", phase_val, "_waves.txt")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        logger.info("Copied _waves.txt → payload/%s/_waves.txt", phase_val)


# ---------------------------------------------------------------
# Internal — Embed the deployment engine
# ---------------------------------------------------------------

def _embed_deployer(pkg_dir: str):
    """
    Copy the ddl_deployer package into the package's lib/ directory.

    The deploy.py script adds lib/ to sys.path so the DBA does not
    need to install ddl_deployer separately.

    Args:
        pkg_dir: Package root directory.
    """
    # Find ddl_deployer package location
    import ddl_deployer
    deployer_src = os.path.dirname(ddl_deployer.__file__)

    dest = os.path.join(pkg_dir, "lib", "ddl_deployer")
    shutil.copytree(deployer_src, dest)

    logger.debug("Embedded ddl_deployer from %s", deployer_src)


# ---------------------------------------------------------------
# Internal — Generated files
# ---------------------------------------------------------------

def _generate_deploy_script(pkg_dir: str, manifest: BuildManifest):
    """
    Generate deploy.py — the DBA's single entry point.

    This script bootstraps the embedded ddl_deployer, reads the
    BUILD.json for context, and orchestrates the deployment with
    query banding and logging.

    Args:
        pkg_dir:  Package root directory.
        manifest: Build manifest for embedding metadata.
    """
    script = f'''#!/usr/bin/env python3
"""
Deployment script for: {manifest.package_filename}

Built:       {manifest.timestamp}
Environment: {manifest.environment}
Build:       {manifest.build_number}
Author:      {manifest.author}
Description: {manifest.description}

Usage:
    python deploy.py --host <teradata_host> --user <username>
    python deploy.py --host <teradata_host> --user <username> --dry-run
    python deploy.py --help

Requirements:
    Python 3.9+
    teradatasql  (pip install teradatasql)
"""

import argparse
import json
import logging
import os
import sys
import glob
from datetime import datetime, timezone

# -- Bootstrap: add embedded lib/ to path --
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))

from ddl_deployer.deployer import deploy_package
from ddl_deployer.wave_parser import parse_waves_file

# -- Build metadata --
BUILD_NUMBER = "{manifest.build_number}"
PACKAGE_NAME = "{manifest.package_name}"
ENVIRONMENT = "{manifest.environment}"


def main():
    """Main deployment entry point for the DBA."""
    args = parse_args()

    # -- Configure logging --
    log_dir = os.path.join(SCRIPT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"deploy_{{ts}}.log")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("deploy")

    logger.info("=" * 64)
    logger.info("  Package:     %s", PACKAGE_NAME)
    logger.info("  Environment: %s", ENVIRONMENT)
    logger.info("  Build:       %s", BUILD_NUMBER)
    logger.info("  Mode:        %s", "DRY RUN" if args.dry_run else "DEPLOY")
    logger.info("  Streams:     %d", min(max(args.streams, 1), 8))
    logger.info("=" * 64)

    # Clamp streams to 1–8
    num_streams = min(max(args.streams, 1), 8)

    # -- Connect (skip in dry-run — no database needed) --
    cursor = None
    make_cursor = None

    if not args.dry_run:
        cursor = connect(args)

        # -- Connection factory for parallel streams --
        def make_cursor():
            return connect(args)

        # -- Set query band --
        try:
            band = (
                f"BUILD={{BUILD_NUMBER}};PKG={{PACKAGE_NAME}};"
                f"ENV={{ENVIRONMENT}};DEPLOYER=ddl_deployer_v2;"
            )
            cursor.execute(f"SET QUERY_BAND = '{{band}}' FOR SESSION")
            logger.info("Query band set: %s", band)
        except Exception as e:
            logger.warning("Query band failed (non-fatal): %s", e)
    else:
        logger.info("Dry run — no database connection required")

    # -- Collect files per phase, building waves where defined --
    payload_dir = os.path.join(SCRIPT_DIR, "payload")
    phases = sorted(
        [d for d in os.listdir(payload_dir)
         if os.path.isdir(os.path.join(payload_dir, d))],
    )

    all_waves = []
    all_files = []
    use_waves = False

    for phase_dir_name in phases:
        phase_path = os.path.join(payload_dir, phase_dir_name)
        logger.info("Phase: %s", phase_dir_name)

        try:
            if cursor:
                band = (
                    f"BUILD={{BUILD_NUMBER}};PKG={{PACKAGE_NAME}};"
                    f"ENV={{ENVIRONMENT}};PHASE={{phase_dir_name}};"
                )
                cursor.execute(f"SET QUERY_BAND = '{{band}}' FOR SESSION")
        except Exception:
            pass

        waves_file = os.path.join(phase_path, "_waves.txt")
        order_file = os.path.join(phase_path, "_order.txt")

        if os.path.exists(waves_file):
            use_waves = True
            phase_waves = parse_waves_file(waves_file, phase_path)
            all_waves.extend(phase_waves)
            for w in phase_waves:
                all_files.extend(w)
            logger.info("  %d wave(s), %d objects",
                        len(phase_waves), sum(len(w) for w in phase_waves))
        elif os.path.exists(order_file):
            phase_files = read_order_file(order_file, phase_path)
            all_waves.append(phase_files)
            all_files.extend(phase_files)
        else:
            phase_files = discover_files(phase_path)
            if phase_files:
                all_waves.append(phase_files)
                all_files.extend(phase_files)

    if not all_files:
        logger.warning("No deployable files found in payload.")
        return

    logger.info("Total: %d objects across %d waves", len(all_files), len(all_waves))

    # -- Run deployment --
    # Skip pre-flight when no database connection (dry-run)
    # Phases provide sequential barriers via directory ordering.
    # Waves provide dependency-ordered sub-grouping within a phase
    # (generated by: python -m td_release_packager analyze).
    no_connection = cursor is None
    try:
        if use_waves:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                waves=all_waves,
                num_streams=num_streams,
                connect_fn=make_cursor if num_streams > 1 else None,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
            )
        else:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                ordered_files=all_files,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
            )

        # Print summary
        status = "PASSED" if result.success else "FAILED"
        logger.info("=" * 64)
        logger.info("  Deployment %s", status)
        logger.info("  Completed:  %d", result.completed)
        logger.info("  Skipped:    %d", result.skipped)
        logger.info("  Failed:     %d", result.failed)
        if result.report_path:
            logger.info("  Report:     %s", result.report_path)
        logger.info("  Log:        %s", log_file)
        logger.info("=" * 64)

        sys.exit(0 if result.success else 1)

    except Exception as e:
        logger.exception("Deployment failed: %s", e)
        sys.exit(1)
    finally:
        # Clear query band and close connection
        if cursor:
            try:
                cursor.execute("SET QUERY_BAND = NONE FOR SESSION")
            except Exception:
                pass
            cursor.close()
            cursor.connection.close()


def discover_files(phase_path):
    """Discover all SQL files in a phase directory, ordered by sub-directory then name."""
    files = []
    for root, dirs, filenames in os.walk(phase_path):
        dirs.sort()  # Alphabetical sub-directory traversal
        for f in sorted(filenames):
            if f.startswith("_") or f.startswith("."):
                continue  # Skip control files
            full = os.path.join(root, f)
            files.append(full)
    return files


def read_order_file(order_path, base_dir):
    """Read an _order.txt file listing filenames in deployment order."""
    files = []
    with open(order_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            full = os.path.join(base_dir, stripped)
            if os.path.exists(full):
                files.append(full)
            else:
                logging.warning("Order file references missing file: %s", stripped)
    return files


def connect(args):
    """Establish Teradata connection."""
    import teradatasql
    params = {{"host": args.host, "user": args.user}}
    if args.password:
        params["password"] = args.password
    if args.logmech:
        params["logmech"] = args.logmech
    conn = teradatasql.connect(**params)
    return conn.cursor()


def parse_args():
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description=(
            f"Deploy {{PACKAGE_NAME}} (build {{BUILD_NUMBER}}) "
            f"to {{ENVIRONMENT}}."
        ),
    )
    p.add_argument("--host", help="Teradata host (required unless --dry-run).")
    p.add_argument("--user", help="Teradata user (required unless --dry-run).")
    p.add_argument("--password", help="Teradata password.")
    p.add_argument("--logmech", help="Logon mechanism.")
    p.add_argument("--dry-run", action="store_true",
                   help="Simulate without executing DDL (no connection needed).")
    p.add_argument("--streams", type=int, default=4,
                   help="Parallel deployment streams (1-8, default: 4).")
    p.add_argument("--continue-on-error", action="store_true",
                   help="Continue past failures.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging.")
    args = p.parse_args()

    # Validate: --host and --user are required unless --dry-run
    if not args.dry_run:
        if not args.host or not args.user:
            p.error("--host and --user are required (use --dry-run for offline simulation)")

    return args


if __name__ == "__main__":
    main()
'''

    deploy_path = os.path.join(pkg_dir, "deploy.py")
    with open(deploy_path, 'w', encoding='utf-8') as f:
        f.write(script)

    # Make executable
    os.chmod(deploy_path, 0o755)


def _generate_readme(pkg_dir: str, manifest: BuildManifest):
    """Generate a README.txt with deployment instructions for the DBA."""
    readme = f"""================================================================
  TERADATA RELEASE PACKAGE
================================================================

  Package:     {manifest.package_name}
  Environment: {manifest.environment}
  Build:       {manifest.build_number}
  Built:       {manifest.timestamp}
  Author:      {manifest.author}
  Description: {manifest.description}

================================================================
  PREREQUISITES
================================================================

  1. Python 3.9 or later
  2. teradatasql driver:  pip install teradatasql
  3. Network access to the target Teradata system
  4. A Teradata user with sufficient privileges (CREATE TABLE,
     CREATE VIEW, GRANT, etc. on the target databases)

================================================================
  DEPLOYMENT
================================================================

  Linux/Mac:
    ./deploy.sh --host <teradata_host> --user <username>

  Windows:
    deploy.bat --host <teradata_host> --user <username>

  Direct Python:
    python deploy.py --host <teradata_host> --user <username>

  Options:
    --password <pwd>    Teradata password (prompted if omitted)
    --logmech <mech>    Logon mechanism (LDAP, TD2, etc.)
    --streams <n>       Parallel deployment streams (1-8, default: 4)
    --dry-run           Simulate without executing any SQL
    --continue-on-error Continue past failures
    -v, --verbose       Debug-level logging

================================================================
  PARALLEL DEPLOYMENT
================================================================

  DDL phases with a _waves.txt file deploy objects in parallel
  across multiple database connections (streams). Each wave is
  a synchronisation barrier — wave N+1 starts only after all
  objects in wave N succeed.

    python deploy.py --host myserver --user dbc --streams 6

  Default: 4 streams. Maximum: 8. Use --streams 1 for sequential.

================================================================
  DRY RUN (RECOMMENDED FIRST)
================================================================

  Run with --dry-run to validate everything without making changes:

    python deploy.py --host myserver --user dbc --dry-run

  This runs pre-flight checks (permissions, space) and reports
  what would happen, without executing any DDL/DCL/DML.

================================================================
  LOGS AND REPORTS
================================================================

  All deployment logs are written to the logs/ directory:
    logs/deploy_YYYYMMDD_HHMMSS.log    — full execution log
    logs/.deploy_manifest.json          — machine-readable state
    logs/.deploy_report_*.html          — branded HTML report

  The HTML report is the recommended artefact for sign-off.

================================================================
  RESTARTABILITY
================================================================

  If the deployment fails partway through, fix the issue and
  re-run the same command. The deployment engine tracks which
  objects have been completed and resumes from the failure point.

================================================================
  QUERY BAND AUDIT TRAIL
================================================================

  All SQL executed during deployment carries a query band:
    BUILD={manifest.build_number};PKG={manifest.package_name};ENV={manifest.environment};PHASE=...;

  Query DBC.DBQLogTbl to review the deployment audit trail:
    SELECT QueryBand, StartTime, UserName, StatementType
    FROM DBC.DBQLogTbl
    WHERE GetQueryBandValue(QueryBand, 0, 'BUILD') = '{manifest.build_number}'
    ORDER BY StartTime;

================================================================
  CONTENTS
================================================================

"""

    # List phase inventories
    for phase, count in sorted(manifest.phase_inventory.items()):
        readme += f"  {phase}: {count} file(s)\n"

    readme += f"""
================================================================
  SUPPORT
================================================================

  For issues with this package, contact: {manifest.author or 'the development team'}

================================================================
"""

    readme_path = os.path.join(pkg_dir, "README.txt")
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write(readme)


def _generate_shell_wrappers(pkg_dir: str):
    """Generate deploy.sh and deploy.bat wrappers."""
    # Linux/Mac
    sh_content = '#!/bin/bash\ncd "$(dirname "$0")" && python3 deploy.py "$@"\n'
    sh_path = os.path.join(pkg_dir, "deploy.sh")
    with open(sh_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(sh_content)
    os.chmod(sh_path, 0o755)

    # Windows
    bat_content = '@echo off\r\ncd /d "%~dp0"\r\npython deploy.py %*\r\n'
    bat_path = os.path.join(pkg_dir, "deploy.bat")
    with open(bat_path, 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(bat_content)


# ---------------------------------------------------------------
# Internal — Archiving
# ---------------------------------------------------------------

def _archive_package(pkg_dir: str, archive_format: str) -> str:
    """
    Archive the package directory as .zip or .tar.gz.

    The archive is created alongside the package directory,
    and the unarchived directory is removed after successful
    archiving.

    Args:
        pkg_dir:        Path to the package directory.
        archive_format: 'zip' or 'tar.gz'.

    Returns:
        Path to the archive file.
    """
    if archive_format == "tar.gz":
        archive_path = shutil.make_archive(
            base_name=pkg_dir,
            format='gztar',
            root_dir=os.path.dirname(pkg_dir),
            base_dir=os.path.basename(pkg_dir),
        )
    else:
        archive_path = shutil.make_archive(
            base_name=pkg_dir,
            format='zip',
            root_dir=os.path.dirname(pkg_dir),
            base_dir=os.path.basename(pkg_dir),
        )

    # Remove the unarchived directory
    shutil.rmtree(pkg_dir)
    logger.info("Archived and cleaned up: %s", archive_path)

    return archive_path
