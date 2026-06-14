"""
ships_mcp.py — SHIPS MCP Server.

Exposes all SHIPS pipeline stages as MCP tools so any MCP-compatible
client (Claude Code, Claude Desktop, Cursor, custom agents) can drive
the full deployment workflow without subprocess invocation.

Transport options
-----------------
stdio (default) — subprocess transport for local clients:

    python -m ships_mcp

    Register in Claude Desktop / Claude Code:
    {
        "mcpServers": {
            "ships": {
                "command": "uv",
                "args": ["run", "python", "-m", "ships_mcp"],
                "cwd": "/path/to/teradata-ships"
            }
        }
    }

streamable-http — enterprise HTTP transport (MCP 2025-03-26 spec).
Runs as a standalone service; clients connect over HTTP/HTTPS:

    python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000

    # Stateless mode for serverless / load-balanced deployments:
    python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000 --stateless

    # Custom endpoint path:
    python -m ships_mcp --transport streamable-http --port 8000 --path /api/mcp

sse — legacy SSE transport (MCP 2024-11-05 spec) for clients that have
not yet migrated to streamable-http:

    python -m ships_mcp --transport sse --host 0.0.0.0 --port 8000

Environment variables
---------------------
All HTTP settings may also be supplied via FASTMCP_* environment
variables (FASTMCP_HOST, FASTMCP_PORT, FASTMCP_LOG_LEVEL, etc.).
CLI flags take precedence over environment variables.

Design principles
-----------------
  - Stateless per invocation: each tool call is independent.
  - Durable state lives on the filesystem (ships.decisions.json, releases/).
  - Pipeline tools (scaffold through package) work fully offline.
  - Deployment tools (deploy, explain, rollback) require a live connection.
  - All tools return JSON-serialisable dicts. On failure: {"error": ...}.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Optional

from mcp.server.fastmcp import FastMCP

from td_release_packager._version import __version__ as SHIPS_VERSION

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "SHIPS",
    instructions=(
        "SHIPS (Scaffold → Harvest → Inspect → Package → Ship) is the Teradata "
        "database deployment framework. Use these tools to build and deploy "
        "Teradata DDL packages. Pipeline tools (scaffold through package) are "
        "offline. Deployment tools (deploy, explain, rollback) require a live "
        "Teradata connection via host/user/password."
    ),
)


def _load_legacy_migration_rules(project: str):
    """Return parsed project-local tokenisation rules, if present."""
    migration_path = os.path.join(project, "config", "tokenise.conf")
    if not os.path.isfile(migration_path):
        return []

    from td_release_packager.source_migrator import parse_migration_sed

    with open(migration_path, encoding="utf-8") as f:
        rules, _skipped = parse_migration_sed(f.read())
    return rules


# ---------------------------------------------------------------
# [S] Scaffold
# ---------------------------------------------------------------


@mcp.tool()
def ships_scaffold(
    name: str,
    output: str = ".",
    environments: str = "DEV,TST,PRD",
    repair: bool = False,
) -> dict:
    """Create a new SHIPS project structure (or repair an existing one).

    Creates the canonical directory layout under output/name/:
    payload/database/, config/env/, releases/, ships.yaml, .build_counter.

    Args:
        name: Project name (used as directory name).
        output: Parent directory (default: current directory).
        environments: Comma-separated environment names (default: DEV,TST,PRD).
        repair: Add missing directories/files without overwriting existing config.

    Returns:
        {"project_dir": str, "environments": list, "action": str}
    """
    try:
        from td_release_packager.scaffolder import scaffold_project

        envs = [e.strip().upper() for e in environments.split(",")]
        project_dir = scaffold_project(
            project_name=name,
            output_dir=output,
            environments=envs,
            repair=repair,
        )
        return {
            "success": True,
            "project_dir": project_dir,
            "environments": envs,
            "action": "repair" if repair else "scaffold",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [H] Harvest
# ---------------------------------------------------------------


def _parse_prefix_token_kv(spec: Optional[str]) -> Optional[dict]:
    """Parse a ``"SOURCE=TOKEN[,SOURCE2=TOKEN2,...]"`` spec into a dict.

    Returns ``None`` for an empty / whitespace-only input.  Raises
    :class:`ValueError` with a friendly message on any malformed
    entry so the MCP tool returns ``success=False`` instead of
    silently tokenising nothing.
    """
    if not spec or not spec.strip():
        return None
    mapping: dict = {}
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(f"prefix_token expects SOURCE=TOKEN, got {entry!r}")
        src, _, tok = entry.partition("=")
        src = src.strip()
        tok = tok.strip()
        if not src or not tok:
            raise ValueError(f"prefix_token has empty source or token: {entry!r}")
        mapping[src] = tok
    return mapping or None


@mcp.tool()
def ships_harvest(
    source: str,
    project: str,
    token_map: Optional[str] = None,
    auto_tokenise: bool = False,
    env_prefix: Optional[str] = None,
    remove_view_type_affixes: bool = False,
    prefix_token: Optional[str] = None,
) -> dict:
    """Harvest raw DDL files from a source directory into a SHIPS project.

    Classifies each file, injects MULTISET where missing, renames to the
    eponymous convention (DB.Object.ext), and places files in the correct
    payload subdirectory. Optionally applies token substitution.

    Args:
        source: Directory containing raw DDL files.
        project: Target SHIPS project directory (must be scaffolded).
        token_map: Path to token_map.conf to apply literal→{{TOKEN}} substitution.
        auto_tokenise: Auto-detect and apply token substitution in one pass
                       (no manual review step). Combine with env_prefix.
        env_prefix: Environment prefix to strip when deriving token names
                    (e.g. 'A_D01' turns 'A_D01_OMR_STD' into '{{OMR_STD}}').
        remove_view_type_affixes: Remove redundant view object affixes
                                  (leading v_ and trailing _v) and update
                                  qualified references during harvest.
        prefix_token: Identifier-aware prefix tokenisation (Model B, issue #309).
                      One or more ``SOURCE=TOKEN`` pairs separated by commas.
                      Rewrites the database-name PREFIX to ``{{TOKEN}}`` while
                      preserving the structural remainder.  E.g.
                      ``"CallCentre=PREFIX"`` turns ``CallCentre_DOM_STD_T``
                      into ``{{PREFIX}}_DOM_STD_T`` and a standalone
                      ``CallCentre`` into ``{{PREFIX}}``.  Distinct from
                      ``token_map`` (literal substring) and ``env_prefix``
                      (strip + per-database).

    Returns:
        {"classified": int, "unclassified": int, "files_placed": int,
         "token_candidates": int, "warnings": list, "unclassified_files": list,
         "prefix_token_substitutions": int, "prefix_token_files": int}
    """
    try:
        from td_release_packager.ingest import ingest_directory
        from td_release_packager.token_engine import (
            read_token_map,
            generate_token_map,
        )

        prefix_tokens = _parse_prefix_token_kv(prefix_token)
        legacy_migration_rules = _load_legacy_migration_rules(project)
        apply_tokens = None
        if token_map:
            apply_tokens = read_token_map(token_map)
        elif auto_tokenise:
            detect = ingest_directory(
                source,
                project,
                detect_tokens=True,
                apply_tokens=None,
                legacy_migration_rules=legacy_migration_rules,
                remove_view_type_affixes=remove_view_type_affixes,
                prefix_tokens=prefix_tokens,
            )
            if detect.token_candidates:
                apply_tokens = generate_token_map(detect.token_candidates, env_prefix)

        result = ingest_directory(
            source_dir=source,
            project_dir=project,
            detect_tokens=True,
            apply_tokens=apply_tokens,
            legacy_migration_rules=legacy_migration_rules,
            remove_view_type_affixes=remove_view_type_affixes,
            prefix_tokens=prefix_tokens,
        )
        return {
            "success": True,
            "classified": result.classified,
            "unclassified": result.unclassified,
            "files_placed": len(result.files_placed),
            "token_candidates": len(result.token_candidates),
            "multiset_injected": result.multiset_injected,
            "legacy_migration_files": result.legacy_migration_files,
            "legacy_migration_substitutions": result.legacy_migration_substitutions,
            "placement_index_dir": result.placement_index_dir,
            "placement_index_files": result.placement_index_files,
            "view_type_affix_renames": result.view_type_affix_renames,
            "prefix_token_substitutions": result.prefix_token_substitutions,
            "prefix_token_files": result.prefix_token_files,
            "warnings": result.warnings,
            "classification_warnings": result.classification_warnings,
            "unclassified_files": result.unclassified_files,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [G] Generate
# ---------------------------------------------------------------


@mcp.tool()
def ships_generate(
    project: str,
    modules: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Generate view-layer DDL from harvested tables (SHIPS topology projects).

    Uses the Object Placement Standard view-layer generator to create
    1:1 locking views and business views from table definitions.

    Args:
        project: SHIPS project directory containing harvested payload.
        modules: Comma-separated module names to generate (default: all).
        dry_run: Validate without writing files.

    Returns:
        {"locking_views_written": int, "business_views_rewritten": int,
         "databases_written": int, "grants_written": int,
         "warnings": list, "errors": list}
    """
    try:
        from pathlib import Path as _Path
        from td_release_packager.view_layer_generator import run as generate_views

        requested = (
            {m.strip().upper() for m in modules.split(",") if m.strip()}
            if modules
            else None
        )
        result = generate_views(
            project_root=_Path(project),
            requested_modules=requested,
            dry_run=dry_run,
        )
        return {
            "success": not result.errors,
            "locking_views_written": result.locking_views_written,
            "locking_views_unchanged": result.locking_views_unchanged,
            "business_views_rewritten": result.business_views_rewritten,
            "business_views_unchanged": result.business_views_unchanged,
            "databases_written": result.databases_written,
            "grants_written": result.grants_written,
            "warnings": result.warnings,
            "errors": result.errors,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [I] Inspect
# ---------------------------------------------------------------


@mcp.tool()
def ships_inspect(
    project: str,
    config: Optional[str] = None,
    strict: bool = False,
    skip_grants: bool = False,
) -> dict:
    """Inspect payload DDL against Coding Discipline rules.

    Runs three inspection steps:
      0. Token format check (malformed {{...}} markers)
      1. Lint rules (db_qualifier, set_multiset, deploy_intent, etc.)
      2. Grant validation (cross-file grant set consistency)

    Args:
        project: SHIPS project directory to inspect.
        config: Path to inspect.conf (default: auto-detect in project).
        strict: Promote all WARNING rules to ERROR.
        skip_grants: Skip grant validation step.

    Returns:
        {"passed": bool, "error_count": int, "warning_count": int,
         "findings": [{"rule": str, "severity": str, "file": str, "message": str}]}
    """
    try:
        from td_release_packager.validate import (
            validate_directory,
            read_inspect_config,
            DEFAULT_RULES,
        )

        # Load rule config from file or use defaults
        if config:
            rules_config = read_inspect_config(config)
        else:
            # Auto-detect config/inspect.conf in the project
            auto_config = os.path.join(project, "config", "inspect.conf")
            if os.path.exists(auto_config):
                rules_config = read_inspect_config(auto_config)
            else:
                rules_config = dict(DEFAULT_RULES)

        result = validate_directory(
            source_dir=project,
            rules_config=rules_config,
            strict=strict,
        )
        findings = [
            {
                "rule": f.rule,
                "severity": f.severity,
                "file": f.file,
                "message": f.message,
                "line": f.line,
            }
            for f in result.issues
        ]
        return {
            "success": result.passed,
            "passed": result.passed,
            "error_count": result.errors,
            "warning_count": result.warnings,
            "files_scanned": result.files_scanned,
            "findings": findings,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [A] Analyse
# ---------------------------------------------------------------


@mcp.tool()
def ships_analyse(
    project: str,
    overwrite: bool = True,
) -> dict:
    """Analyse DDL dependencies and generate wave ordering.

    Builds a directed dependency graph of all objects in the payload
    and produces a topologically sorted wave ordering (_waves.txt).

    Args:
        project: SHIPS project directory to analyse.
        overwrite: Overwrite existing _waves.txt (default: True).

    Returns:
        {"object_count": int, "wave_count": int, "dependency_count": int,
         "cycle_count": int, "cycles": list, "waves_path": str}
    """
    try:
        from td_release_packager.analyser import analyse_project

        result = analyse_project(project)

        waves_path = None
        if result.waves and overwrite:
            waves_path = os.path.join(project, "_waves.txt")
            with open(waves_path, "w", encoding="utf-8") as f:
                f.write(result.waves_file_content)

        return {
            "success": True,
            "object_count": len(result.objects),
            "wave_count": len(result.waves),
            "dependency_count": sum(len(v) for v in result.dependencies.values()),
            "cycle_count": len(result.cycles),
            "cycles": result.cycles,
            "waves_path": waves_path,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [P] Package
# ---------------------------------------------------------------


@mcp.tool()
def ships_package(
    project: str,
    env: str,
    name: str,
    env_config: str,
    output: Optional[str] = None,
    author: str = "",
    description: str = "",
    commit: str = "",
) -> dict:
    """Build a release package for a target environment.

    Resolves all {{TOKEN}} references in the payload using the env config,
    assembles a self-contained archive with the deployment engine, and
    stamps context/ships.build.json with provenance, integrity hash, and trust report.

    Args:
        project: SHIPS project directory.
        env: Target environment name (e.g. DEV, TST, PRD).
        name: Package name (logical identifier).
        env_config: Path to the environment .conf file.
        output: Output directory for the archive (default: project/releases/).
        author: Builder identifier for provenance.
        description: Release description for provenance.
        commit: Git commit hash for traceability.

    Returns:
        {"archive_path": str, "build_number": int, "file_count": int,
         "token_count": int, "trust_status": str, "warnings": list}
    """
    try:
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        env_config_abs = _resolve_path(env_config, project)
        output_dir = output or os.path.join(project, "releases")
        os.makedirs(output_dir, exist_ok=True)

        config = BuildConfig(
            source_dir=project,
            environment=env.upper(),
            package_name=name,
            env_config_file=env_config_abs,
            output_dir=output_dir,
            author=author,
            description=description,
            source_commit=commit,
        )

        (main_arc, manifest), companion = build_package(config)

        trust_doc = _find_trust_json(main_arc) or {}
        result = {
            "success": True,
            "archive_path": main_arc,
            "environment": manifest.environment,
            "build_number": manifest.build_number,
            "file_count": manifest.file_count,
            "token_count": manifest.token_count,
            "trust_status": trust_doc.get("status", "UNKNOWN"),
            "warnings": manifest.warnings,
        }
        result.update(_ships_context_response(main_arc))
        if companion:
            result["companion_archive"] = companion[0]
            result["companion_context_entrypoint"] = _archive_member_ref(
                companion[0], "context/ships.index.json"
            )
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [S] Process (full pipeline)
# ---------------------------------------------------------------


@mcp.tool()
def ships_process(
    project: str,
    source: Optional[str] = None,
    token_map: Optional[str] = None,
    auto_tokenise: bool = False,
    env_prefix: Optional[str] = None,
    env: Optional[str] = None,
    env_config: Optional[str] = None,
    name: Optional[str] = None,
    skip_generate: bool = False,
    strict: bool = False,
    prefix_token: Optional[str] = None,
) -> dict:
    """Run the full SHIPS pipeline: harvest → generate → inspect → analyse → [package].

    Orchestrates all pipeline stages in sequence. The package stage only
    runs when env, env_config, and name are all provided.

    Args:
        project: SHIPS project directory (must already be scaffolded).
        source: Raw DDL source directory. Harvest is skipped if omitted.
        token_map: Path to token_map.conf for harvest substitution.
        auto_tokenise: Auto-detect and apply tokens in one pass.
        env_prefix: Env prefix for auto-tokenise token derivation.
        env: Target environment (enables package stage).
        env_config: Environment config file (enables package stage).
        name: Package name (enables package stage).
        skip_generate: Skip the view-layer generate stage.
        strict: Abort on first stage error (developer mode: continue).
        prefix_token: Identifier-aware prefix tokenisation (Model B, issue #309).
                      Same shape as on ``ships_harvest`` — see its docstring.
                      E.g. ``"CallCentre=PREFIX"``.

    Returns:
        {"stages": {"harvest": {...}, "inspect": {...}, ...}, "label": str}
    """
    stages: dict = {}
    failed: list = []

    # Harvest
    if source:
        r = ships_harvest(
            source=source,
            project=project,
            token_map=token_map,
            auto_tokenise=auto_tokenise,
            env_prefix=env_prefix,
            prefix_token=prefix_token,
        )
        stages["harvest"] = r
        if not r.get("success") and strict:
            return {"success": False, "stages": stages, "aborted_at": "harvest"}
        if not r.get("success"):
            failed.append("harvest")

    # Generate
    if not skip_generate:
        r = ships_generate(project=project)
        stages["generate"] = r
        if not r.get("success") and strict:
            return {"success": False, "stages": stages, "aborted_at": "generate"}
        if not r.get("success"):
            failed.append("generate")

    # Inspect
    r = ships_inspect(project=project)
    stages["inspect"] = r
    if not r.get("success") and strict:
        return {"success": False, "stages": stages, "aborted_at": "inspect"}
    if not r.get("success"):
        failed.append("inspect")

    # Analyse
    r = ships_analyse(project=project)
    stages["analyse"] = r
    if not r.get("success") and strict:
        return {"success": False, "stages": stages, "aborted_at": "analyse"}
    if not r.get("success"):
        failed.append("analyse")

    # Package (optional)
    if env and env_config and name:
        r = ships_package(project=project, env=env, env_config=env_config, name=name)
        stages["package"] = r
        if not r.get("success"):
            failed.append("package")

    return {
        "success": not failed,
        "stages": stages,
        "failed_stages": failed,
    }


# ---------------------------------------------------------------
# [S] Ship — Deploy
# ---------------------------------------------------------------


@mcp.tool()
def ships_deploy(
    package_dir: str,
    host: str,
    user: str,
    password: str,
    logmech: str = "TD2",
    dry_run: bool = False,
    streams: int = 1,
    continue_on_error: bool = False,
) -> dict:
    """Deploy a SHIPS package to a Teradata system.

    Runs mandatory pre-flight validation (permissions, space, object existence)
    then deploys all objects in dependency-ordered waves. Generates an HTML
    deployment report.

    Args:
        package_dir: Extracted package directory (containing deploy.py).
        host: Teradata hostname.
        user: Teradata username.
        password: Teradata password.
        logmech: Logon mechanism (TD2, LDAP, TDNEGO). Default: TD2.
        dry_run: Simulate without executing DDL. Pre-flight still runs.
        streams: Number of parallel deployment streams (1–8).
        continue_on_error: Continue past individual object failures.

    Returns:
        {"success": bool, "completed": int, "failed": int, "skipped": int,
         "report_path": str, "deployment_id": str, "objects": [...]}
    """
    try:
        import teradatasql
        from database_package_deployer.deployer import deploy_package

        cursor = teradatasql.connect(
            host=host,
            user=user,
            password=password,
            logmech=logmech,
            encryptdata=True,
        ).cursor()
        # Instruct Teradata to treat this session's string data as
        # Unicode so that UTF-8 content in DML seed files is stored
        # correctly.  Without this the server-side LATIN default causes
        # mojibake for any non-ASCII characters (e.g. em-dashes).
        cursor.execute("SET SESSION CHARACTER SET UNICODE")
        try:
            all_waves, all_files, use_waves = _collect_package_files(package_dir)
            logs_dir = os.path.join(package_dir, "logs")
            os.makedirs(logs_dir, exist_ok=True)

            result = deploy_package(
                cursor=cursor,
                package_dir=logs_dir,
                ordered_files=all_files if not use_waves else None,
                waves=all_waves if use_waves else None,
                num_streams=min(max(streams, 1), 8),
                stop_on_failure=not continue_on_error,
                dry_run=dry_run,
            )
            d = _package_result_to_dict(result)
            d["success"] = result.success
            d.update(_ships_context_response(package_dir, extracted_dir=package_dir))
            return d
        finally:
            cursor.close()
            cursor.connection.close()
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_deploy_explain(
    package_dir: str,
    host: str,
    user: str,
    password: str,
    logmech: str = "TD2",
) -> dict:
    """Run EXPLAIN validation on a SHIPS package against a live Teradata target.

    Validates all DDL using Teradata's EXPLAIN keyword without executing any
    statements. Identifies objects whose DDL would fail or behave unexpectedly
    on the target. Requires the parent databases to exist on the target.

    Args:
        package_dir: Extracted package directory.
        host: Teradata hostname.
        user: Teradata username.
        password: Teradata password.
        logmech: Logon mechanism. Default: TD2.

    Returns:
        {"passed": int, "failed": int, "skipped": int, "report_path": str, "objects": [...]}
    """
    try:
        import teradatasql
        from database_package_deployer.deployer import explain_package

        cursor = teradatasql.connect(
            host=host,
            user=user,
            password=password,
            logmech=logmech,
            encryptdata=True,
        ).cursor()
        # Instruct Teradata to treat this session's string data as
        # Unicode so that UTF-8 content in DML seed files is stored
        # correctly.  Without this the server-side LATIN default causes
        # mojibake for any non-ASCII characters (e.g. em-dashes).
        cursor.execute("SET SESSION CHARACTER SET UNICODE")
        try:
            all_waves, all_files, use_waves = _collect_package_files(package_dir)
            logs_dir = os.path.join(package_dir, "logs")
            os.makedirs(logs_dir, exist_ok=True)

            result = explain_package(
                cursor=cursor,
                package_dir=logs_dir,
                ordered_files=all_files if not use_waves else None,
                waves=all_waves if use_waves else None,
            )
            d = _package_result_to_dict(result)
            d["passed"] = result.completed
            return d
        finally:
            cursor.close()
            cursor.connection.close()
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_rollback(
    manifest_path: str,
    host: str,
    user: str,
    password: str,
    logmech: str = "TD2",
    wave: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Roll back a deployment, restoring objects to their pre-deployment state.

    For tables: renames backup table back to original.
    For views/procedures/macros: drops current version and re-executes
    the SHOW DDL captured before deployment.

    Args:
        manifest_path: Path to .deploy_manifest.json in the package logs/.
        host: Teradata hostname (not needed for --dry-run).
        user: Teradata username (not needed for --dry-run).
        password: Teradata password (not needed for --dry-run).
        logmech: Logon mechanism. Default: TD2.
        wave: Roll back only objects from this wave number. Omit for full rollback.
        dry_run: Preview what would be rolled back without executing DDL.
                 Works offline — no connection needed.

    Returns:
        {"rolled_back": int, "failed": int, "objects": [...]}
    """
    try:
        from database_package_deployer.deployer import rollback_package

        if dry_run:
            cursor = None
        else:
            import teradatasql

            cursor = teradatasql.connect(
                host=host,
                user=user,
                password=password,
                logmech=logmech,
                encryptdata=True,
            ).cursor()
            # Instruct Teradata to treat this session's string data as
            # Unicode so that UTF-8 content in DML seed files is stored
            # correctly.  Without this the server-side LATIN default
            # causes mojibake for any non-ASCII characters (e.g. em-dashes).
            cursor.execute("SET SESSION CHARACTER SET UNICODE")

        try:
            result = rollback_package(
                cursor=cursor,
                manifest_path=manifest_path,
                dry_run=dry_run,
                wave_number=wave,
            )
            d = _package_result_to_dict(result)
            d["success"] = dry_run or result.success
            return d
        finally:
            if cursor is not None:
                cursor.close()
                cursor.connection.close()
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# Read-only consumers
# ---------------------------------------------------------------


@mcp.tool()
def ships_decisions(project: str, run_id: Optional[str] = None) -> dict:
    """Read the ships.decisions.json audit trail for a SHIPS project.

    Returns the last pipeline run (or a specific run by ID). Shows
    stage statuses, config provenance, outputs, and issues for each stage.

    Args:
        project: SHIPS project directory containing ships.decisions.json.
        run_id: Specific run ID to return. Omit for the last run.

    Returns:
        The run record from ships.decisions.json, or {"runs_count": N} if
        no specific run is requested and the file has multiple runs.
    """
    try:
        decisions_path = os.path.join(project, "ships.decisions.json")
        if not os.path.exists(decisions_path):
            return {
                "success": False,
                "error": "ships.decisions.json not found in project",
            }

        with open(decisions_path, encoding="utf-8") as f:
            data = json.load(f)

        runs = data.get("runs", [])
        if not runs:
            return {"success": True, "runs_count": 0, "runs": []}

        if run_id:
            run = next((r for r in runs if r.get("run_id") == run_id), None)
            if not run:
                return {"success": False, "error": f"Run ID {run_id!r} not found"}
            return {"success": True, "run": run}

        return {"success": True, "run": runs[-1], "total_runs": len(runs)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_verify(project: str) -> dict:
    """Check whether the last built package is ready to deploy.

    Reads the trust block from context/ships.build.json in the releases directory
    and checks: archive exists, no package warnings, package stage
    succeeded. Returns READY / NOT READY with a per-check breakdown.

    Args:
        project: SHIPS project directory.

    Returns:
        {"ready": bool, "trust_status": str, "checks": [...], "archive_path": str}
    """
    try:
        decisions_path = os.path.join(project, "ships.decisions.json")
        if not os.path.exists(decisions_path):
            return {
                "success": False,
                "ready": False,
                "error": "ships.decisions.json not found — run the pipeline first",
            }

        with open(decisions_path, encoding="utf-8") as f:
            data = json.load(f)

        # Find the last package stage
        pkg_stage = None
        pkg_run = None
        for run in reversed(data.get("runs", [])):
            for stage in reversed(run.get("stages", [])):
                if stage.get("stage") == "package":
                    pkg_stage = stage
                    pkg_run = run
                    break
            if pkg_stage:
                break

        if not pkg_stage:
            return {
                "success": True,
                "ready": False,
                "error": "No package stage found — run ships_package first",
            }

        out = pkg_stage.get("outputs", {})
        archive = out.get("archive_path", "")
        archive_exists = bool(archive) and os.path.exists(archive)
        warnings = [
            i
            for i in pkg_stage.get("issues", [])
            if i.get("severity") in ("warning", "error")
        ]
        pkg_status = pkg_stage.get("status", "unknown")

        checks = [
            {"check": "archive_exists", "passed": archive_exists},
            {"check": "no_package_issues", "passed": not warnings},
            {"check": "stage_status_success", "passed": pkg_status == "success"},
        ]
        ready = all(c["passed"] for c in checks)

        # Read trust status from the canonical context/ships.trust.json
        trust_status = "UNKNOWN"
        if archive_exists:
            trust_doc = _find_trust_json(archive)
            if trust_doc:
                trust_status = trust_doc.get("status", "UNKNOWN")

        response = {
            "success": True,
            "ready": ready,
            "trust_status": trust_status,
            "archive_path": archive,
            "checks": checks,
            "run_id": pkg_run.get("run_id") if pkg_run else None,
        }
        if archive_exists:
            response.update(_ships_context_response(archive))
        return response
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_explain_run(
    project: str,
    run_id: Optional[str] = None,
    command_filter: Optional[str] = None,
) -> dict:
    """Read and explain a prior pipeline run from ships.decisions.json.

    Formats the run record as a structured summary: stage statuses,
    key outputs, and the full issues list. Use before promoting a
    package to verify no blocking issues remain.

    Args:
        project: SHIPS project directory.
        run_id: Report a specific run by ID. Default: last run.
        command_filter: Filter to the last run of this command type
                        (e.g. 'process', 'inspect', 'package').

    Returns:
        {"run_id": str, "command": str, "final_status": str,
         "duration_ms": int, "stages": [...], "issues_summary": {...}}
    """
    try:
        decisions_path = os.path.join(project, "ships.decisions.json")
        if not os.path.exists(decisions_path):
            return {"success": False, "error": "ships.decisions.json not found"}

        with open(decisions_path, encoding="utf-8") as f:
            data = json.load(f)

        runs = data.get("runs", [])
        if not runs:
            return {"success": False, "error": "No runs recorded yet"}

        if run_id:
            run = next((r for r in runs if r.get("run_id") == run_id), None)
        elif command_filter:
            run = next(
                (r for r in reversed(runs) if r.get("command") == command_filter), None
            )
        else:
            run = runs[-1]

        if not run:
            return {"success": False, "error": "Requested run not found"}

        # Build issues summary
        all_issues = [
            {"stage": s["stage"], **i}
            for s in run.get("stages", [])
            for i in s.get("issues", [])
        ]
        issues_by_sev: dict = {}
        for i in all_issues:
            sev = i.get("severity", "unknown")
            issues_by_sev.setdefault(sev, []).append(i)

        stages_summary = [
            {
                "stage": s.get("stage"),
                "status": s.get("status"),
                "duration_ms": s.get("duration_ms"),
                "issue_count": len(s.get("issues", [])),
                "key_outputs": _key_outputs(s),
            }
            for s in run.get("stages", [])
        ]

        return {
            "success": True,
            "run_id": run.get("run_id"),
            "command": run.get("command"),
            "final_status": run.get("final_status"),
            "duration_ms": run.get("duration_ms"),
            "stages": stages_summary,
            "issues_summary": {k: len(v) for k, v in issues_by_sev.items()},
            "all_issues": all_issues,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [A] Authoring (Phase A of #291)
#
# Diff-first, hash-gated authoring tools.  Each authoring tool
# returns a proposal — current_content, proposed_content, unified
# diff, expected_hash, and validation — without touching the
# filesystem.  ``ships_apply_diff`` re-hashes the file and writes
# only if the hash matches, so concurrent edits cannot be silently
# overwritten.
# ---------------------------------------------------------------


@mcp.tool()
def ships_validate_ships_yaml(project: str) -> dict:
    """Validate the project's ships.yaml against the SHIPS schema.

    Thin wrapper over
    ``td_release_packager.orchestrator.ships_yaml.{load,validate}``.

    Args:
        project: SHIPS project directory containing ships.yaml.

    Returns:
        {"success": bool, "valid": bool, "exists": bool,
         "path": str, "errors": [{"path": str, "message": str}]}
    """
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy

        path = os.path.join(project, "ships.yaml")
        if not os.path.exists(path):
            return {
                "success": True,
                "exists": False,
                "valid": False,
                "path": path,
                "errors": [
                    {"path": "", "message": "ships.yaml not found at project root"}
                ],
            }

        data = _sy.load(path)
        errors = _sy.validate(data)
        return {
            "success": True,
            "exists": True,
            "valid": not errors,
            "path": path,
            "errors": [{"path": e.path, "message": e.message} for e in errors],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_author_ships_yaml(
    project: str,
    action: str,
    project_name: Optional[str] = None,
    environments: Optional[list] = None,
    version: Optional[str] = "1.0",
    changes: Optional[dict] = None,
    unset_keys: Optional[list] = None,
) -> dict:
    """Propose a change to ships.yaml without writing to disc.

    Returns a proposal envelope describing the change.  Apply via
    ``ships_apply_diff`` with the returned ``expected_hash``.

    Actions:
        create   Generate a fresh ships.yaml.  Requires ``project_name``
                 and ``environments``.  Fails if the file already exists.
        set      Apply ``changes`` (a flat dotted-key dict) to the
                 existing ships.yaml.  Each value replaces whatever is
                 at that key, creating intermediate dicts as needed.
        unset    Remove the dotted keys listed in ``unset_keys`` from
                 the existing ships.yaml.

    The proposed content is schema-validated before return; any
    validation errors are included in the envelope so the caller can
    self-correct without re-prompting.

    Args:
        project:       SHIPS project directory.
        action:        One of "create", "set", "unset".
        project_name:  Project identifier (create only).
        environments:  Non-empty list of env names (create only).
        version:       Project version string for create (default "1.0").
        changes:       Flat dotted-key dict for set, e.g.
                       {"stages.inspect.strict": true}.
        unset_keys:    List of dotted keys to remove for unset.

    Returns:
        {"success": bool, "path": str, "current_content": str,
         "proposed_content": str, "diff": str, "expected_hash": str,
         "validation": {"valid": bool, "errors": [...]},
         "unchanged": bool}
    """
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy
        from td_release_packager import mcp_authoring as _ma

        path = os.path.join(project, "ships.yaml")
        action_l = (action or "").lower()
        if action_l not in {"create", "set", "unset"}:
            return {
                "success": False,
                "error": f"unknown action {action!r}; expected create/set/unset",
            }

        if action_l == "create":
            if os.path.exists(path):
                return {
                    "success": False,
                    "error": f"ships.yaml already exists at {path}; "
                    "use action=set to modify",
                }
            if not project_name or not environments:
                return {
                    "success": False,
                    "error": "action=create requires project_name and environments",
                }
            try:
                data = _sy.generate_default(
                    project_name=project_name,
                    environments=list(environments),
                    version=version,
                )
            except ValueError as ve:
                return {"success": False, "error": str(ve)}

        else:
            # set or unset — must read existing file
            if not os.path.exists(path):
                return {
                    "success": False,
                    "error": f"ships.yaml not found at {path}; use action=create first",
                }
            data = _sy.load(path)

            if action_l == "set":
                if not isinstance(changes, dict) or not changes:
                    return {
                        "success": False,
                        "error": "action=set requires non-empty changes dict",
                    }
                try:
                    for dotted_key, value in changes.items():
                        _ma.set_dotted(data, dotted_key, value)
                except (TypeError, ValueError) as ke:
                    return {"success": False, "error": str(ke)}

            else:  # unset
                if not isinstance(unset_keys, list) or not unset_keys:
                    return {
                        "success": False,
                        "error": "action=unset requires non-empty unset_keys list",
                    }
                try:
                    for dotted_key in unset_keys:
                        _ma.unset_dotted(data, dotted_key)
                except ValueError as ke:
                    return {"success": False, "error": str(ke)}

        proposed_content = _ma.dump_yaml(data)
        validation_errors = [
            {"path": e.path, "message": e.message} for e in _sy.validate(data)
        ]
        proposal = _ma.build_proposal(
            path,
            proposed_content,
            validation_errors=validation_errors,
        )
        proposal["success"] = True
        return proposal
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_apply_diff(
    path: str,
    proposed_content: str,
    expected_hash: str,
) -> dict:
    """Hash-gated apply of a proposal returned by an authoring tool.

    Re-hashes the current file (or detects absence) and refuses to
    write if the hash does not match ``expected_hash``.  Apply uses
    ``proposed_content`` directly — the unified diff returned with the
    proposal is a display artifact, not the apply payload.

    Pass ``expected_hash`` exactly as returned by the authoring tool.
    For creating a new file, the authoring tool returns the sentinel
    ``"absent"`` — pass it through unchanged.

    Args:
        path:             Target filesystem path.
        proposed_content: Full file contents to write.
        expected_hash:    Hash of the current file (or "absent" if new).

    Returns:
        {"success": bool, "applied": bool, "path": str,
         "created": bool, "new_hash": str, "error": str?}
    """
    try:
        from td_release_packager import mcp_authoring as _ma

        try:
            result = _ma.safe_write(path, proposed_content, expected_hash)
        except _ma.HashMismatchError as hme:
            return {
                "success": False,
                "applied": False,
                "error": str(hme),
                "code": "hash_mismatch",
            }
        return {"success": True, "applied": True, **result}
    except Exception as e:
        return {"success": False, "applied": False, "error": str(e)}


# ---------------------------------------------------------------
# [A] Authoring — Phase B of #291 (#293)
#
# .conf authoring tools.  Same diff-first / hash-gated flow as
# Phase A; structure-preserving editor preserves comments and
# blank lines in hand-curated files.  Apply via ships_apply_diff.
# ---------------------------------------------------------------


def _env_config_path(project: str, env: str) -> str:
    return os.path.join(project, "config", "env", f"{env}.conf")


def _inspect_config_path(project: str) -> str:
    return os.path.join(project, "config", "inspect.conf")


def _propose_conf_edit(
    path: str,
    action: str,
    changes: Optional[dict],
    unset_keys: Optional[list],
    *,
    create_header: str,
    validator,
) -> dict:
    """Shared core for ships_author_env_config / inspect_config.

    Returns a proposal envelope.  ``validator`` is a callable taking
    the proposed content and returning a list of {path, message} dicts
    (empty if valid).
    """
    from td_release_packager import mcp_authoring as _ma

    action_l = (action or "").lower()
    if action_l not in {"create", "set", "unset"}:
        return {
            "success": False,
            "error": f"unknown action {action!r}; expected create/set/unset",
        }

    if action_l == "create":
        if os.path.exists(path):
            return {
                "success": False,
                "error": f"file already exists at {path}; use action=set to modify",
            }
        conf = _ma.ConfFile.parse(create_header)
        if isinstance(changes, dict):
            try:
                for key, value in changes.items():
                    conf.set(str(key), str(value))
            except ValueError as ve:
                return {"success": False, "error": str(ve)}
    else:
        if not os.path.exists(path):
            return {
                "success": False,
                "error": f"file not found at {path}; use action=create first",
            }
        current = _ma.read_or_empty(path)
        conf = _ma.ConfFile.parse(current)

        if action_l == "set":
            if not isinstance(changes, dict) or not changes:
                return {
                    "success": False,
                    "error": "action=set requires non-empty changes dict",
                }
            try:
                for key, value in changes.items():
                    conf.set(str(key), str(value))
            except ValueError as ve:
                return {"success": False, "error": str(ve)}
        else:
            if not isinstance(unset_keys, list) or not unset_keys:
                return {
                    "success": False,
                    "error": "action=unset requires non-empty unset_keys list",
                }
            try:
                for key in unset_keys:
                    conf.unset(str(key))
            except ValueError as ve:
                return {"success": False, "error": str(ve)}

    proposed_content = conf.dump()
    validation_errors = validator(proposed_content)
    proposal = _ma.build_proposal(
        path,
        proposed_content,
        validation_errors=validation_errors,
    )
    proposal["success"] = True
    return proposal


def _validate_env_conf_content(content: str) -> list:
    """Run env-config content through read_env_config; collect errors."""
    from td_release_packager.token_engine import read_env_config

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".conf", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        tmp = tf.name
    try:
        try:
            read_env_config(tmp)
            return []
        except ValueError as ve:
            return [{"path": "", "message": str(ve)}]
        except Exception as e:
            return [{"path": "", "message": f"{type(e).__name__}: {e}"}]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _validate_inspect_conf_content(content: str) -> list:
    """Validate inspect.conf content against severity / domain vocab."""
    from td_release_packager.validate import (
        _DOMAIN_VALUE_RULES,
        _RULE_LOG_LEVEL_KEY,
        _VALID_SEVERITIES,
    )

    errors: list = []
    for lineno, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            errors.append(
                {"path": f"line {lineno}", "message": f"missing '=': {stripped!r}"}
            )
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            errors.append({"path": f"line {lineno}", "message": "empty rule name"})
            continue

        if name in _DOMAIN_VALUE_RULES:
            allowed = _DOMAIN_VALUE_RULES[name]
            if value not in allowed:
                errors.append(
                    {
                        "path": name,
                        "message": (
                            f"value {value!r} not in allowed domain values "
                            f"{sorted(allowed)}"
                        ),
                    }
                )
        elif name in _RULE_LOG_LEVEL_KEY.values() or name not in _RULE_LOG_LEVEL_KEY:
            # severity-valued rule (including comma_log_level and any
            # rule not in the domain-value or log-level companion maps)
            if value.upper() not in _VALID_SEVERITIES:
                errors.append(
                    {
                        "path": name,
                        "message": (
                            f"severity {value!r} not in {sorted(_VALID_SEVERITIES)}"
                        ),
                    }
                )
    return errors


@mcp.tool()
def ships_validate_env_config(project: str, env: str) -> dict:
    """Validate a per-environment config file (config/env/<ENV>.conf).

    Wraps ``td_release_packager.token_engine.read_env_config`` — token
    format, internal {{TOKEN}} resolution, and value-character checks
    are reused unchanged.

    Args:
        project: SHIPS project directory.
        env:     Environment name (e.g. ``DEV``, ``TST``, ``PRD``).

    Returns:
        {"success": bool, "exists": bool, "valid": bool, "path": str,
         "errors": [{"path": str, "message": str}]}
    """
    try:
        path = _env_config_path(project, env)
        if not os.path.exists(path):
            return {
                "success": True,
                "exists": False,
                "valid": False,
                "path": path,
                "errors": [{"path": "", "message": f"env config not found at {path}"}],
            }
        with open(path, "r", encoding="utf-8") as f:
            errors = _validate_env_conf_content(f.read())
        return {
            "success": True,
            "exists": True,
            "valid": not errors,
            "path": path,
            "errors": errors,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_validate_inspect_config(project: str) -> dict:
    """Validate config/inspect.conf against the SHIPS rule vocabulary.

    Checks every key/value against ``_VALID_SEVERITIES`` and the
    domain-value vocabulary (``comma_style``).  Unknown rule names
    are accepted (future-proofing for custom rules); invalid
    severities or domain values are flagged.

    Args:
        project: SHIPS project directory.

    Returns:
        {"success": bool, "exists": bool, "valid": bool, "path": str,
         "errors": [{"path": str, "message": str}]}
    """
    try:
        path = _inspect_config_path(project)
        if not os.path.exists(path):
            return {
                "success": True,
                "exists": False,
                "valid": False,
                "path": path,
                "errors": [
                    {"path": "", "message": f"inspect.conf not found at {path}"}
                ],
            }
        with open(path, "r", encoding="utf-8") as f:
            errors = _validate_inspect_conf_content(f.read())
        return {
            "success": True,
            "exists": True,
            "valid": not errors,
            "path": path,
            "errors": errors,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


_ENV_CONF_HEADER = (
    "# ===================================================================\n"
    "# <ENV>.conf — per-environment token values for this SHIPS project.\n"
    "#\n"
    "# Format:  TOKEN_NAME=value         (one per line)\n"
    "# Lines starting with '#' are comments.\n"
    "# {{TOKEN}} references inside values are resolved at package time.\n"
    "# ===================================================================\n"
    "\n"
)

_INSPECT_CONF_HEADER = (
    "# ===================================================================\n"
    "# inspect.conf — SHIPS Coding Discipline rule severities.\n"
    "#\n"
    "# Format:  rule_name=SEVERITY        (ERROR|WARNING|WARN|INFO|OFF)\n"
    "# Special: comma_style=leading|trailing|as-per-source\n"
    "# Only override rules that need to differ from the defaults in\n"
    "# td_release_packager.validate.DEFAULT_RULES.\n"
    "# ===================================================================\n"
    "\n"
)


@mcp.tool()
def ships_author_env_config(
    project: str,
    env: str,
    action: str,
    changes: Optional[dict] = None,
    unset_keys: Optional[list] = None,
) -> dict:
    """Propose a change to a per-environment config file.

    Returns a proposal envelope describing the change.  Apply via
    ``ships_apply_diff`` with the returned ``expected_hash``.  The
    underlying editor preserves comments and blank lines on every
    line not touched by ``changes`` / ``unset_keys``.

    Actions:
        create   Generate ``config/env/<ENV>.conf`` with the stock
                 header.  Optionally seeds initial keys from ``changes``.
                 Fails if the file already exists.
        set      Apply ``changes`` to the existing file (replace in
                 place, append at end if absent).
        unset    Remove the listed keys.

    Args:
        project:    SHIPS project directory.
        env:        Environment name (DEV / TST / PRD / ...).
        action:     One of "create", "set", "unset".
        changes:    {KEY: value} dict for set / create.
        unset_keys: List of keys for unset.

    Returns:
        Standard proposal envelope (see ships_author_ships_yaml).
    """
    try:
        path = _env_config_path(project, env)
        header = _ENV_CONF_HEADER.replace("<ENV>", env)
        return _propose_conf_edit(
            path,
            action,
            changes,
            unset_keys,
            create_header=header,
            validator=_validate_env_conf_content,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_author_inspect_config(
    project: str,
    action: str,
    changes: Optional[dict] = None,
    unset_keys: Optional[list] = None,
) -> dict:
    """Propose a change to config/inspect.conf.

    Returns a proposal envelope.  Apply via ``ships_apply_diff``.  The
    editor preserves comments and blank lines on every line not
    touched by ``changes`` / ``unset_keys``.

    Actions:
        create   Generate inspect.conf with the stock header.
                 Optionally seeds initial overrides from ``changes``.
                 Fails if the file already exists.
        set      Apply rule-severity overrides (and ``comma_style``).
        unset    Remove rule overrides (reverts them to defaults).

    Args:
        project:    SHIPS project directory.
        action:     One of "create", "set", "unset".
        changes:    {rule_name: SEVERITY|domain_value} dict.
        unset_keys: List of rule names to remove from overrides.

    Returns:
        Standard proposal envelope.
    """
    try:
        path = _inspect_config_path(project)
        return _propose_conf_edit(
            path,
            action,
            changes,
            unset_keys,
            create_header=_INSPECT_CONF_HEADER,
            validator=_validate_inspect_conf_content,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [A] Authoring — Phase B.5 of #291 (#295)
#
# token_map analyser + author.  Read-only payload scan surfaces
# candidates; structural clustering helps the author see DRY
# opportunities; ships_author_token_map writes via the same
# diff-first / hash-gate pattern.
# ---------------------------------------------------------------


_TOKEN_MAP_HEADER = (
    "# ===================================================================\n"
    "# token_map.conf — Literal database name → {{TOKEN}} mapping.\n"
    "#\n"
    "# Format:  LITERAL_DB_NAME={{TOKEN_NAME}}\n"
    "# Run `ships harvest` to (re-)generate the baseline, or use\n"
    "# `ships_analyse_token_candidates` to inspect candidates and\n"
    "# structure this file for DRY before adding entries by hand.\n"
    "# ===================================================================\n"
    "\n"
)


def _token_map_path(project: str) -> str:
    return os.path.join(project, "config", "token_map.conf")


_TOKEN_VALUE_RE = re.compile(r"^\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}$")


def _validate_token_map_content(content: str) -> list:
    """Inline validator for token_map.conf content.

    Returns a list of {path, message} dicts. Empty == valid.
    Catches: missing '=', empty key, value not in ``{{TOKEN_NAME}}``
    shape, and duplicate keys.
    """
    errors: list = []
    seen: dict = {}
    for lineno, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            errors.append(
                {"path": f"line {lineno}", "message": f"missing '=': {stripped!r}"}
            )
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            errors.append({"path": f"line {lineno}", "message": "empty literal name"})
            continue
        if key in seen:
            errors.append(
                {
                    "path": key,
                    "message": (f"duplicate entry (also defined at line {seen[key]})"),
                }
            )
        else:
            seen[key] = lineno
        if not _TOKEN_VALUE_RE.match(value):
            errors.append(
                {
                    "path": key,
                    "message": (
                        f"value {value!r} is not in {{{{TOKEN_NAME}}}} form "
                        "(letters / digits / underscore, must start with "
                        "a letter or underscore)"
                    ),
                }
            )
    return errors


@mcp.tool()
def ships_analyse_token_candidates(project: str) -> dict:
    """Read-only analysis of database-literal candidates for tokenisation.

    Walks ``<project>/payload/`` (without modifying anything), extracts
    qualified database names from every DDL file, filters out system
    databases, and groups the remaining literals by structural
    similarity so the author can spot DRY opportunities before editing
    ``config/token_map.conf``.

    Cross-references each candidate against:
      - the existing token_map.conf (already mapped → in ``mapped``)
      - per-env config tokens (token name already defined → in
        ``defined_tokens``).

    Args:
        project: SHIPS project directory.

    Returns:
        {"success": bool,
         "literal_count": int,
         "literals": [{"name": str, "ref_count": int, "files": [...],
                       "already_mapped_to": str|None}],
         "prefix_clusters": [{"prefix": str, "members": [...], "count": int}],
         "suffix_clusters": [{"suffix": str, "members": [...], "count": int}],
         "existing_token_map": {literal: token, ...},
         "defined_env_tokens": [token_name, ...]}
    """
    try:
        from td_release_packager import mcp_authoring as _ma
        from td_release_packager.ingest import _build_token_candidates
        from td_release_packager.token_engine import read_env_config, read_token_map

        raw_db_names = _ma.scan_payload_databases(project)
        filtered = _build_token_candidates(raw_db_names)
        view = _ma.cluster_token_candidates(filtered)

        # Cross-reference: existing token_map.conf
        existing_map: dict = {}
        tm_path = _token_map_path(project)
        if os.path.exists(tm_path):
            try:
                existing_map = read_token_map(tm_path)
            except Exception as e:
                logger.warning("Could not read token_map.conf: %s", e)

        for entry in view["literals"]:
            entry["already_mapped_to"] = existing_map.get(entry["name"])

        # Cross-reference: env-conf token names already defined.
        defined_tokens: set = set()
        env_dir = os.path.join(project, "config", "env")
        if os.path.isdir(env_dir):
            for f in os.listdir(env_dir):
                if not f.endswith(".conf"):
                    continue
                try:
                    defined_tokens.update(read_env_config(os.path.join(env_dir, f)))
                except Exception as e:
                    logger.warning("Could not read %s: %s", f, e)

        return {
            "success": True,
            "literal_count": view["literal_count"],
            "literals": view["literals"],
            "prefix_clusters": view["prefix_clusters"],
            "suffix_clusters": view["suffix_clusters"],
            "existing_token_map": existing_map,
            "defined_env_tokens": sorted(defined_tokens),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_validate_token_map(project: str) -> dict:
    """Validate config/token_map.conf against the SHIPS token-map schema.

    Surfaces: missing ``=`` lines, empty keys, duplicate keys, and
    values that are not in ``{{TOKEN_NAME}}`` shape.  Token-name
    characters: letters / digits / underscore, must start with a
    letter or underscore (matches the parser used elsewhere).

    Args:
        project: SHIPS project directory.

    Returns:
        {"success": bool, "exists": bool, "valid": bool, "path": str,
         "errors": [{"path": str, "message": str}]}
    """
    try:
        path = _token_map_path(project)
        if not os.path.exists(path):
            return {
                "success": True,
                "exists": False,
                "valid": False,
                "path": path,
                "errors": [
                    {"path": "", "message": f"token_map.conf not found at {path}"}
                ],
            }
        with open(path, "r", encoding="utf-8") as f:
            errors = _validate_token_map_content(f.read())
        return {
            "success": True,
            "exists": True,
            "valid": not errors,
            "path": path,
            "errors": errors,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_author_token_map(
    project: str,
    action: str,
    changes: Optional[dict] = None,
    unset_keys: Optional[list] = None,
) -> dict:
    """Propose a change to config/token_map.conf.

    Same diff-first / hash-gated flow as the other authoring tools.
    The structure-preserving editor keeps comments and reference-count
    annotations intact on every line not directly touched.

    Actions:
        create   Generate token_map.conf with the stock header.
                 Optionally seeds initial mappings from ``changes``.
                 Fails if the file already exists.
        set      Apply ``{LITERAL: "{{TOKEN_NAME}}"}`` pairs. Values
                 must be in ``{{TOKEN_NAME}}`` shape — invalid values
                 surface in the proposal envelope's validation list.
        unset    Remove the listed literal keys.

    Args:
        project:    SHIPS project directory.
        action:     One of "create", "set", "unset".
        changes:    {LITERAL: "{{TOKEN_NAME}}"} dict.
        unset_keys: List of literal keys to remove.

    Returns:
        Standard proposal envelope.
    """
    try:
        path = _token_map_path(project)
        return _propose_conf_edit(
            path,
            action,
            changes,
            unset_keys,
            create_header=_TOKEN_MAP_HEADER,
            validator=_validate_token_map_content,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [R] Repair — Phase C of #291 (#297)
#
# Wraps the rules-catalogue remediation metadata and the existing
# tree-level fixers (fix_ddl_terminators, fix_non_ascii) so an
# agent can: (a) explain any inspect finding, (b) discover which
# rules have automated fixes, (c) dry-run or apply those fixes
# through MCP without leaving the conversation.
# ---------------------------------------------------------------


def _fix_registry() -> dict:
    """Map inspect rule_code → registered tree-level fixer.

    Built lazily so importing ships_mcp does not pull in
    ``validate``'s heavyweight dependency graph at module load.
    """
    from td_release_packager.validate import fix_ddl_terminators, fix_non_ascii

    return {
        "ddl_terminator": fix_ddl_terminators,
        "non_ascii": fix_non_ascii,
    }


@mcp.tool()
def ships_explain_violation(rule_id: str) -> dict:
    """Return the full remediation profile for an inspect rule.

    Wraps ``rules_catalogue.remediation_for``.  The catalogue is
    hand-curated metadata: description, default severity, whether a
    safe fix exists, automation level (auto / guided / manual),
    recommended action, risk, and whether human review is required.

    Use after ``ships_inspect`` returns findings: pass the
    ``finding.rule`` value to this tool to learn what to do about it.

    Args:
        rule_id: Rule code from a ``ships_inspect`` finding
                 (e.g. ``"db_qualifier"``).

    Returns:
        {"success": bool, "rule_id": str, "found": bool,
         "automated_fix_available": bool,
         "remediation": {description, default_severity,
                         safe_fix_available, automation_level,
                         recommended_action, risk,
                         requires_human_review}}
    """
    try:
        from td_release_packager.rules_catalogue import remediation_for

        meta = remediation_for(rule_id)
        if meta is None:
            return {
                "success": True,
                "rule_id": rule_id,
                "found": False,
                "automated_fix_available": False,
                "error": f"unknown rule {rule_id!r}",
            }
        return {
            "success": True,
            "rule_id": rule_id,
            "found": True,
            "automated_fix_available": rule_id in _fix_registry(),
            "remediation": meta,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_list_fixable_rules() -> dict:
    """List inspect rules with an MCP-dispatchable automated fix.

    Combines the rules-catalogue entries with the in-process fix
    registry, so only rules that ``ships_fix`` can actually act on
    appear here.  Each entry carries the catalogue's
    ``automation_level``, ``recommended_action``, and ``risk`` so an
    agent can decide whether to call ``ships_fix`` unattended or
    surface the diff first.

    Returns:
        {"success": bool, "rules": [{"rule_id": str,
                                     "automation_level": str,
                                     "recommended_action": str,
                                     "risk": str,
                                     "default_severity": str}]}
    """
    try:
        from td_release_packager.rules_catalogue import remediation_for

        out = []
        for rule_id in _fix_registry().keys():
            meta = remediation_for(rule_id)
            if meta is None:
                continue
            out.append(
                {
                    "rule_id": rule_id,
                    "automation_level": meta.get("automation_level"),
                    "recommended_action": meta.get("recommended_action"),
                    "risk": meta.get("risk"),
                    "default_severity": meta.get("default_severity"),
                }
            )
        out.sort(key=lambda r: r["rule_id"])
        return {"success": True, "rules": out}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def ships_fix(project: str, rule_id: str, dry_run: bool = True) -> dict:
    """Run a registered automated fix for an inspect rule.

    Dispatches to the tree-level fixer registered for ``rule_id``.
    Defaults to dry-run: counts what *would* change without writing
    so an agent can preview safely.  Pass ``dry_run=False`` to apply.

    Idempotent: a second apply on a clean tree reports
    ``files_changed=0``.

    Args:
        project: SHIPS project directory (the fixer walks
                 ``payload/`` and other DDL-bearing roots inside it).
        rule_id: Rule code listed by ``ships_list_fixable_rules``.
        dry_run: When True (default) no file is written; the result
                 reports what would change.

    Returns:
        {"success": bool, "rule_id": str, "dry_run": bool,
         "files_scanned": int, "files_changed": int,
         "files": [{"file": str, ...rule-specific counts...}]}
    """
    try:
        registry = _fix_registry()
        if rule_id not in registry:
            return {
                "success": False,
                "error": (
                    f"no automated fix registered for rule {rule_id!r}; "
                    "call ships_list_fixable_rules for the full list"
                ),
            }
        fixer = registry[rule_id]
        result = fixer(source_dir=project, dry_run=dry_run)
        summary = result.to_dict()
        return {
            "success": True,
            "rule_id": rule_id,
            "dry_run": dry_run,
            "files_scanned": summary.get("files_scanned", 0),
            "files_changed": summary.get("files_written", 0),
            "files": summary.get("files", []),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# [G] Guidance — Phase D of #291 (#299)
#
# Read-only navigation: where am I in the pipeline (ships_status)
# and what's actually in the package I built (ships_describe_package).
# Wraps the existing project_index / project_actions / package-context
# substrate; no new state is invented.
# ---------------------------------------------------------------


@mcp.tool()
def ships_status(project: str) -> dict:
    """Report where the project sits in the SHIPS pipeline.

    Wraps ``project_index.compute_project_index`` and
    ``project_actions.compute_project_actions``.  Read-only; nothing
    is written.

    Use as the entry point for an agent that's just been asked to
    "look at this project" — the lifecycle state tells you what
    already happened, ``next_recommended_actions`` tells you what
    to do next, and the allowed / blocked / approval-required lists
    tell you what's currently safe to call.

    Args:
        project: SHIPS project directory.

    Returns:
        {"success": bool,
         "project_name": str,
         "project_dir": str,
         "lifecycle_state": str (scaffolded|harvested|inspected|analysed|packaged),
         "next_recommended_actions": [str],
         "evaluated_at": str,
         "references": {...},
         "discovery_flags": {...},
         "allowed_actions": [str],
         "blocked_actions": [{action, reason, evidence_ref, instruction}],
         "requires_human_approval": [{action, reason, ...}]}
    """
    try:
        from td_release_packager.project_actions import compute_project_actions
        from td_release_packager.project_index import compute_project_index

        if not os.path.isdir(project):
            return {
                "success": False,
                "error": f"project directory not found: {project}",
            }

        index = compute_project_index(project).to_dict()
        actions = compute_project_actions(project).to_dict()
        return {
            "success": True,
            "project_name": index.get("project_name"),
            "project_dir": index.get("project_dir"),
            "lifecycle_state": index.get("lifecycle_state"),
            "next_recommended_actions": index.get("next_recommended_actions", []),
            "evaluated_at": index.get("evaluated_at"),
            "references": index.get("references", {}),
            "discovery_flags": actions.get("discovery_flags", {}),
            "allowed_actions": actions.get("allowed_actions", []),
            "blocked_actions": actions.get("blocked_actions", []),
            "requires_human_approval": actions.get("requires_human_approval", []),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _latest_package_archive(project: str) -> Optional[str]:
    """Return the newest *.zip under ``<project>/releases/`` by mtime."""
    releases = os.path.join(project, "releases")
    if not os.path.isdir(releases):
        return None
    candidates: list = []
    for root, _dirs, files in os.walk(releases):
        for name in files:
            if name.lower().endswith(".zip"):
                full = os.path.join(root, name)
                try:
                    candidates.append((os.path.getmtime(full), full))
                except OSError:
                    continue
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def _summarise_package(
    archive_path: str,
    build: Optional[dict],
    trust: Optional[dict],
    manifest: Optional[dict],
) -> dict:
    """Flatten the trio of context JSONs into a small summary dict."""
    summary: dict = {
        "archive_path": archive_path,
        "archive_name": os.path.basename(archive_path),
        "size_bytes": None,
    }
    try:
        summary["size_bytes"] = os.path.getsize(archive_path)
    except OSError:
        pass

    if build:
        summary["project_name"] = build.get("project_name") or build.get("name")
        summary["target_env"] = build.get("target_env") or build.get("env")
        summary["build_counter"] = build.get("build_counter")
        summary["built_at"] = build.get("built_at") or build.get("build_timestamp")
        summary["source_commit"] = build.get("source_commit")
        summary["ships_version"] = build.get("ships_version")

    if trust:
        summary["trust_status"] = trust.get("status") or trust.get("trust_status")
        summary["trust_label"] = trust.get("label")
        signals = trust.get("signals") or trust.get("checks")
        if signals:
            summary["trust_signals"] = signals

    if manifest:
        objects = manifest.get("objects") or manifest.get("entries")
        if isinstance(objects, list):
            summary["object_count"] = len(objects)
            kinds: dict = {}
            for obj in objects:
                kind = (obj or {}).get("kind") or (obj or {}).get("type") or "unknown"
                kinds[kind] = kinds.get(kind, 0) + 1
            summary["object_counts_by_kind"] = kinds
        waves = manifest.get("waves")
        if isinstance(waves, list):
            summary["wave_count"] = len(waves)

    return summary


def _render_summary_text(summary: dict) -> str:
    """Render a short human-readable header for review-before-deploy."""
    lines = []
    name = summary.get("project_name") or summary.get("archive_name", "package")
    env = summary.get("target_env") or "unspecified env"
    lines.append(f"Package {name} for {env}")
    trust = summary.get("trust_status")
    if trust:
        lines.append(f"Trust: {trust}")
    obj_count = summary.get("object_count")
    waves = summary.get("wave_count")
    if obj_count is not None and waves is not None:
        lines.append(f"Contents: {obj_count} object(s) across {waves} wave(s)")
    elif obj_count is not None:
        lines.append(f"Contents: {obj_count} object(s)")
    if summary.get("built_at"):
        lines.append(f"Built at: {summary['built_at']}")
    return "\n".join(lines)


@mcp.tool()
def ships_describe_package(project: str, archive: Optional[str] = None) -> dict:
    """Summarise a built package for review-before-deploy.

    Auto-discovers the newest ``*.zip`` under ``<project>/releases/``
    when ``archive`` is omitted, then reads ``context/ships.build.json``,
    ``context/ships.trust.json``, and (if present)
    ``context/ships.manifest.json`` from inside the archive.  Returns
    a structured summary plus ``summary_text`` — a short
    human-readable header suitable for surfacing in a chat reply.

    Read-only; never extracts the archive.

    Args:
        project: SHIPS project directory.
        archive: Optional explicit archive path; overrides
                 latest-archive auto-discovery.

    Returns:
        {"success": bool, "archive_path": str, "summary_text": str,
         "summary": {project_name, target_env, build_counter,
                     trust_status, object_count, wave_count, ...},
         "context": {...standard ships context handoff fields...}}
    """
    try:
        if archive:
            archive_path = archive
        else:
            archive_path = _latest_package_archive(project)
        if not archive_path or not os.path.isfile(archive_path):
            return {
                "success": False,
                "error": (
                    f"no archive found at {archive!r}"
                    if archive
                    else f"no package archive found under {project}/releases/"
                ),
            }

        build = _find_build_json(archive_path)
        trust = _find_trust_json(archive_path)
        manifest = _find_archive_json(archive_path, "context/ships.manifest.json")
        summary = _summarise_package(archive_path, build, trust, manifest)
        return {
            "success": True,
            "archive_path": archive_path,
            "summary_text": _render_summary_text(summary),
            "summary": summary,
            "context": _ships_context_response(archive_path),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------


def _resolve_path(path: str, relative_to: str) -> str:
    """Resolve a path relative to a project directory."""
    if os.path.isabs(path):
        return path
    return os.path.join(relative_to, path)


def _collect_package_files(package_dir: str):
    """Collect ordered deployment files from a package directory."""
    from database_package_deployer.wave_parser import parse_waves_file

    payload_dir = os.path.join(package_dir, "payload")
    all_waves, all_files, use_waves = [], [], False

    if not os.path.isdir(payload_dir):
        return all_waves, all_files, use_waves

    for phase_name in sorted(
        d
        for d in os.listdir(payload_dir)
        if os.path.isdir(os.path.join(payload_dir, d))
    ):
        phase_path = os.path.join(payload_dir, phase_name)
        waves_file = os.path.join(phase_path, "_waves.txt")
        order_file = os.path.join(phase_path, "_order.txt")

        if os.path.exists(waves_file):
            use_waves = True
            phase_waves = parse_waves_file(waves_file, phase_path)
            all_waves.extend(phase_waves)
            for w in phase_waves:
                all_files.extend(w)
        elif os.path.exists(order_file):
            with open(order_file, encoding="utf-8") as f:
                phase_files = [
                    os.path.join(phase_path, line.strip())
                    for line in f
                    if line.strip() and not line.startswith("#")
                ]
            all_waves.append(phase_files)
            all_files.extend(phase_files)
        else:
            import glob as _glob

            phase_files = sorted(
                _glob.glob(os.path.join(phase_path, "**", "*.*"), recursive=True)
            )
            phase_files = [f for f in phase_files if os.path.isfile(f)]
            if phase_files:
                all_waves.append(phase_files)
                all_files.extend(phase_files)

    return all_waves, all_files, use_waves


def _package_result_to_dict(result) -> dict:
    """Convert a PackageDeployResult to a dict."""
    d = {
        "deployment_id": result.deployment_id,
        "manifest_path": result.manifest_path,
        "report_path": result.report_path,
        "total": result.total,
        "completed": result.completed,
        "skipped": result.skipped,
        "failed": result.failed,
        "rolled_back": result.rolled_back,
    }
    if result.results:
        d["objects"] = [
            {
                "qualified_name": f"{r.database_name}.{r.object_name}",
                "object_type": r.object_type.value if r.object_type else None,
                "state": r.state.value if r.state else None,
                "message": r.message,
                "error": r.error,
                "wave_number": r.wave_number,
                "dry_run": r.dry_run,
            }
            for r in result.results
        ]
    return d


def _archive_member_ref(archive_path: str, filename: str) -> Optional[str]:
    """Return a stable archive member reference when *filename* exists."""
    import zipfile

    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if name.endswith(filename):
                    return f"{archive_path}::{name}"
    except Exception:
        return None
    return None


def _ships_context_response(
    package_ref: str, extracted_dir: Optional[str] = None
) -> dict:
    """Return the standard SHIPS context handoff fields for tool responses."""
    context_names = (
        "context/ships.index.json",
        "context/ships.handoff.json",
        "context/ships.context.json",
        "context/ships.build.json",
        "context/ships.manifest.json",
        "context/ships.trust.json",
        "context/ships.integrity.json",
        "context/ships.provenance.json",
        "context/ships.decisions.json",
    )
    if extracted_dir:
        entrypoint = os.path.join(extracted_dir, "context", "ships.index.json")
        reads = [
            os.path.join(extracted_dir, *name.split("/")) for name in context_names
        ]
    else:
        entrypoint = _archive_member_ref(package_ref, "context/ships.index.json")
        reads = [_archive_member_ref(package_ref, name) for name in context_names]
        reads = [r for r in reads if r]
    return {
        "package_type": "teradata-ships",
        "context_entrypoint": entrypoint,
        "required_next_reads": reads,
        "agent_instruction": "Read context/ships.index.json first, then follow its recommended_read_order before deploying, approving, modifying, or summarising this package.",
    }


def _find_build_json(archive_path: str) -> Optional[dict]:
    """Extract context/ships.build.json from a package zip, or None if not found."""
    return _find_archive_json(archive_path, "context/ships.build.json")


def _find_trust_json(archive_path: str) -> Optional[dict]:
    """Extract context/ships.trust.json from a package zip, or None if not found."""
    return _find_archive_json(archive_path, "context/ships.trust.json")


def _find_archive_json(archive_path: str, member_path: str) -> Optional[dict]:
    """Return a JSON archive member as a dict, or None if absent / unreadable."""
    import zipfile

    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/").endswith(member_path):
                    return json.loads(zf.read(name).decode("utf-8"))
    except Exception:
        pass
    return None


def _key_outputs(stage: dict) -> dict:
    """Extract the most meaningful outputs for a stage."""
    name = stage.get("stage", "")
    out = stage.get("outputs", {})
    if name == "harvest":
        return {
            "classified": out.get("classified"),
            "unclassified": out.get("unclassified"),
        }
    if name == "inspect":
        issue_count = len(stage.get("issues", []))
        return {"issue_count": issue_count}
    if name == "analyse":
        return {
            "object_count": out.get("object_count"),
            "wave_count": out.get("wave_count"),
            "cycle_count": out.get("cycle_count"),
        }
    if name == "package":
        return {
            "file_count": out.get("file_count"),
            "token_count": out.get("token_count"),
        }
    return {}


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and start the SHIPS MCP server.

    Supports three transports:

    stdio (default) — classic subprocess transport for Claude Desktop,
    Claude Code, and any MCP client that launches the server as a child
    process.  No network port is opened.

        python -m ships_mcp

    streamable-http — HTTP/1.1 transport with chunked responses defined
    in the MCP 2025-03-26 specification.  Required for enterprise
    deployments where the server runs as a standalone service and clients
    connect over the network rather than via subprocess.

        python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000

    sse (legacy) — Server-Sent Events transport from the MCP 2024-11-05
    specification.  Supported for backward compatibility with clients that
    have not yet migrated to streamable-http.

        python -m ships_mcp --transport sse --host 0.0.0.0 --port 8000

    For streamable-http and sse, all settings may also be supplied via
    environment variables prefixed with FASTMCP_ (e.g. FASTMCP_HOST,
    FASTMCP_PORT, FASTMCP_LOG_LEVEL).  CLI flags take precedence.

    Enterprise TLS note: terminate TLS at a reverse proxy (nginx, API
    Gateway, etc.) in front of the server.  The MCP server itself speaks
    plain HTTP; TLS is the responsibility of the network layer.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="ships_mcp",
        description="SHIPS MCP Server — Teradata deployment pipeline over MCP.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=None,
        help=(
            "MCP transport to use. "
            "'stdio' (default) for subprocess clients (Claude Desktop, Claude Code). "
            "'streamable-http' for enterprise HTTP deployments (MCP 2025-03-26). "
            "'sse' for legacy SSE clients (MCP 2024-11-05)."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Host address to bind for HTTP transports (default: 127.0.0.1). "
            "Use 0.0.0.0 to accept connections from all interfaces — "
            "only do this behind a network-layer access control."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on for HTTP transports (default: 8000).",
    )
    parser.add_argument(
        "--path",
        default=None,
        dest="http_path",
        help=(
            "URL path for the MCP endpoint "
            "(default: /mcp for streamable-http, /sse for sse)."
        ),
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=False,
        help=(
            "Enable stateless HTTP mode: create a new transport session per request. "
            "Suitable for serverless / load-balanced deployments. "
            "Only applies to streamable-http transport."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Log level for the MCP server (default: INFO).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a ships.yaml file whose 'mcp:' block supplies defaults "
            "for --transport / --host / --port / --path / --stateless / "
            "--log-level (default: ./ships.yaml if present). "
            "Precedence: CLI flag > FASTMCP_* env var > ships.yaml > built-in."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"ships_mcp {SHIPS_VERSION}",
        help="Show version and exit.",
    )

    # -- Auth flags (HTTP transports only) ----------------------------------
    auth_group = parser.add_argument_group(
        "authentication",
        "JWT/Bearer token authentication. Requires --transport streamable-http or sse. "
        "SHIPS acts as an OAuth 2.0 Resource Server — it validates tokens issued by "
        "your identity provider (Azure AD, Okta, AWS Cognito, Keycloak, etc.).",
    )
    auth_group.add_argument(
        "--auth-jwks-uri",
        metavar="URL",
        default=None,
        help=(
            "JWKS endpoint URL for JWT signature verification. "
            "Examples: "
            "Azure AD: https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys  "
            "Okta: https://{domain}/oauth2/default/v1/keys  "
            "AWS Cognito: https://cognito-idp.{region}.amazonaws.com/{pool}/.well-known/jwks.json  "
            "Enabling this flag activates Bearer token enforcement on all HTTP endpoints."
        ),
    )
    auth_group.add_argument(
        "--auth-issuer",
        metavar="URL",
        default=None,
        help=(
            "Expected JWT issuer (iss claim). Must match the token exactly. "
            "Examples: "
            "Azure AD: https://login.microsoftonline.com/{tenant}/v2.0  "
            "Okta: https://{domain}/oauth2/default"
        ),
    )
    auth_group.add_argument(
        "--auth-audience",
        metavar="VALUE",
        default=None,
        help=(
            "Expected JWT audience (aud claim). "
            "Typically the Application ID URI or client_id of this service. "
            "Example: api://ships-mcp"
        ),
    )
    auth_group.add_argument(
        "--auth-required-scopes",
        metavar="SCOPES",
        default=None,
        help=(
            "Comma-separated list of OAuth scopes that every caller must hold. "
            "Requests with tokens missing any required scope receive HTTP 403. "
            "Example: ships.deploy,ships.read"
        ),
    )
    auth_group.add_argument(
        "--auth-resource-url",
        metavar="URL",
        default=None,
        help=(
            "Public base URL of this MCP server. Required when --auth-jwks-uri is set. "
            "Used in WWW-Authenticate response headers per RFC 9728 "
            "(OAuth 2.0 Protected Resource Metadata). "
            "Example: http://ships-mcp.internal:8000"
        ),
    )

    args = parser.parse_args()

    # -- Load ships.yaml mcp: block (if present) ------------------------
    # Precedence applied below: CLI flag > FASTMCP_* env var > ships.yaml
    # > FastMCP built-in default.  The block is only consulted for keys
    # not already supplied by a higher-precedence source.
    yaml_mcp: dict = {}
    yaml_config_path: Optional[str] = None
    candidate_path = args.config or os.path.join(os.getcwd(), "ships.yaml")
    if os.path.exists(candidate_path):
        try:
            from td_release_packager.orchestrator import ships_yaml as _sy

            doc = _sy.load(candidate_path)
            yaml_errors = _sy.validate(doc)
            mcp_errors = [e for e in yaml_errors if e.path.startswith("mcp")]
            if mcp_errors:
                parser.error(
                    "ships.yaml mcp: block is invalid:\n  "
                    + "\n  ".join(f"{e.path}: {e.message}" for e in mcp_errors)
                )
            yaml_mcp = doc.get("mcp") or {}
            yaml_config_path = candidate_path
        except Exception as e:
            # Bad YAML when --config was explicit is fatal; otherwise warn.
            if args.config:
                parser.error(f"--config {candidate_path}: {e}")
            logger.warning("Could not read %s: %s", candidate_path, e)

    # Resolve transport: CLI > ships.yaml > built-in "stdio".
    if args.transport is None:
        args.transport = yaml_mcp.get("transport", "stdio")

    # -- Validate: HTTP-only flags must not be used with stdio ----------
    http_flags_set = any([args.host, args.port, args.http_path, args.stateless])
    if args.transport == "stdio" and http_flags_set:
        parser.error(
            "--host, --port, --path, and --stateless are only valid with "
            "--transport streamable-http or --transport sse."
        )

    # -- Validate: auth flags require HTTP transport --------------------
    auth_flags_set = any(
        [
            args.auth_jwks_uri,
            args.auth_issuer,
            args.auth_audience,
            args.auth_required_scopes,
            args.auth_resource_url,
        ]
    )
    if auth_flags_set and args.transport == "stdio":
        parser.error(
            "--auth-* flags are only valid with "
            "--transport streamable-http or --transport sse."
        )
    if args.auth_jwks_uri and not args.auth_resource_url:
        parser.error(
            "--auth-resource-url is required when --auth-jwks-uri is set. "
            "It identifies this MCP server in WWW-Authenticate headers."
        )

    # -- Apply settings to the FastMCP instance -------------------------
    # mcp.settings is a mutable Pydantic model; update it before run().
    # Precedence (highest first):
    #   1. CLI flag (args.*)
    #   2. FASTMCP_* env var (already folded in by pydantic-settings)
    #   3. ships.yaml mcp.* block (applied here)
    #   4. FastMCP built-in default (left untouched)
    def _yaml_if_env_absent(env_name: str, yaml_key: str):
        if env_name in os.environ:
            return None
        return yaml_mcp.get(yaml_key)

    yaml_host = _yaml_if_env_absent("FASTMCP_HOST", "host")
    yaml_port = _yaml_if_env_absent("FASTMCP_PORT", "port")
    yaml_log_level = _yaml_if_env_absent("FASTMCP_LOG_LEVEL", "log_level")
    yaml_stateless = _yaml_if_env_absent("FASTMCP_STATELESS_HTTP", "stateless")

    if args.host is not None:
        mcp.settings.host = args.host
    elif yaml_host is not None:
        mcp.settings.host = yaml_host

    if args.port is not None:
        mcp.settings.port = args.port
    elif yaml_port is not None:
        mcp.settings.port = yaml_port

    if args.log_level is not None:
        mcp.settings.log_level = args.log_level
    elif yaml_log_level is not None:
        mcp.settings.log_level = yaml_log_level

    if args.stateless:
        mcp.settings.stateless_http = True
    elif yaml_stateless:
        mcp.settings.stateless_http = True

    # Apply custom path: CLI flag wins, else ships.yaml mcp.path.
    yaml_path = yaml_mcp.get("path")
    chosen_path = args.http_path if args.http_path is not None else yaml_path
    if chosen_path is not None:
        if args.transport == "streamable-http":
            mcp.settings.streamable_http_path = chosen_path
        elif args.transport == "sse":
            mcp.settings.sse_path = chosen_path

    # -- Configure JWT/Bearer authentication ---------------------------
    # Auth is applied to HTTP transports only; wired by setting
    # mcp._token_verifier and mcp.settings.auth before mcp.run().
    # FastMCP builds the ASGI app (and wires auth middleware) lazily
    # inside run(), so both attributes must be set beforehand.
    if args.auth_jwks_uri:
        from ships_mcp_auth import JWTTokenVerifier

        mcp._token_verifier = JWTTokenVerifier(
            jwks_uri=args.auth_jwks_uri,
            issuer=args.auth_issuer or None,
            audience=args.auth_audience or None,
        )

        try:
            from mcp.server.auth.settings import AuthSettings
            from pydantic import AnyHttpUrl

            required_scopes = (
                [s.strip() for s in args.auth_required_scopes.split(",") if s.strip()]
                if args.auth_required_scopes
                else None
            )

            mcp.settings.auth = AuthSettings(
                issuer_url=AnyHttpUrl(args.auth_issuer or args.auth_resource_url),
                resource_server_url=AnyHttpUrl(args.auth_resource_url),
                required_scopes=required_scopes,
            )
        except ImportError:  # pragma: no cover
            # mcp package not available — auth settings not applied,
            # but JWTTokenVerifier is still set and will be called
            pass

        logger.info(
            "JWT authentication enabled — jwks_uri=%s  issuer=%s  audience=%s  "
            "required_scopes=%s",
            args.auth_jwks_uri,
            args.auth_issuer or "(not validated)",
            args.auth_audience or "(not validated)",
            args.auth_required_scopes or "(none)",
        )

    # -- Configure logging --------------------------------------------------
    # Wire up the rotating file handler + stderr handler BEFORE printing the
    # banner so the banner can advertise the resolved log path, and nothing
    # on stdout corrupts the JSON-RPC transport.
    import sys as _sys

    from ships_logging import banner_lines as _log_banner_lines
    from ships_logging import configure_logging as _configure_logging

    _log_path = _configure_logging()

    # -- Emit startup banner (every transport, on stderr) -------------------
    # stdout is reserved for JSON-RPC.  Print to stderr regardless of
    # transport so operators always see where the server is, where its log
    # file lives, and which ships.yaml mcp: block (if any) is in effect.
    if args.transport == "stdio":
        endpoint = "stdio (subprocess transport — no network port)"
    else:
        endpoint = (
            f"http://{mcp.settings.host}:{mcp.settings.port}"
            f"{mcp.settings.streamable_http_path if args.transport == 'streamable-http' else mcp.settings.sse_path}"
            f"{'  [stateless]' if mcp.settings.stateless_http else ''}"
        )

    if yaml_config_path:
        config_line = f"ships.yaml: {yaml_config_path}  (mcp: block in effect)"
    else:
        config_line = (
            "no ships.yaml found — using FastMCP built-in defaults "
            "(pass --config <path> to point at one)"
        )

    override_hint = (
        "CLI flag > FASTMCP_* env var > ships.yaml mcp: block "
        "(see --help for full key list)"
    )

    # Reconstruct the launch command so a restart doesn't require
    # the operator to remember the exact flags.  shlex.join quotes
    # only what needs quoting, so the line stays readable.
    import shlex as _shlex

    _command = "python -m ships_mcp"
    if len(_sys.argv) > 1:
        _command = f"{_command} {_shlex.join(_sys.argv[1:])}"

    banner = [
        "",
        "=" * 72,
        f"  SHIPS MCP server v{SHIPS_VERSION} — STARTED",
        f"  Transport : {args.transport}",
        f"  Endpoint  : {endpoint}",
        f"  Config    : {config_line}",
        f"  Override  : {override_hint}",
        f"  Command   : {_command}",
        *_log_banner_lines(_log_path),
        "=" * 72,
        "",
    ]
    print("\n".join(banner), file=_sys.stderr, flush=True)

    try:
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        # Clean stderr shutdown banner — beats a SIGINT traceback for the
        # operator and gives logging.shutdown() a chance to flush rotations.
        shutdown = [
            "",
            "=" * 72,
            f"  SHIPS MCP server v{SHIPS_VERSION} — SHUTDOWN (Ctrl+C)",
            "=" * 72,
            "",
        ]
        print("\n".join(shutdown), file=_sys.stderr, flush=True)
    finally:
        logging.shutdown()


if __name__ == "__main__":
    main()
