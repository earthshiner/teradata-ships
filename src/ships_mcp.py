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
    """Return parsed project-local legacy migration rules, if present."""
    migration_path = os.path.join(project, "config", "legacy_migration.sed")
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


@mcp.tool()
def ships_harvest(
    source: str,
    project: str,
    token_map: Optional[str] = None,
    auto_tokenise: bool = False,
    env_prefix: Optional[str] = None,
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

    Returns:
        {"classified": int, "unclassified": int, "files_placed": int,
         "token_candidates": int, "warnings": list, "unclassified_files": list}
    """
    try:
        from td_release_packager.ingest import ingest_directory
        from td_release_packager.token_engine import (
            read_token_map,
            generate_token_map,
        )

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
            )
            if detect.token_candidates:
                apply_tokens = generate_token_map(detect.token_candidates, env_prefix)

        result = ingest_directory(
            source_dir=source,
            project_dir=project,
            detect_tokens=True,
            apply_tokens=apply_tokens,
            legacy_migration_rules=legacy_migration_rules,
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
         "token_count": int, "trust_label": str, "warnings": list}
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

        result = {
            "success": True,
            "archive_path": main_arc,
            "environment": manifest.environment,
            "build_number": manifest.build_number,
            "file_count": manifest.file_count,
            "token_count": manifest.token_count,
            "trust_label": manifest.trust.get("label", "UNKNOWN")
            if manifest.trust
            else "UNKNOWN",
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
        {"ready": bool, "trust_label": str, "checks": [...], "archive_path": str}
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

        # Read trust label from context/ships.build.json if available
        trust_label = "UNKNOWN"
        if archive_exists:
            build_json = _find_build_json(archive)
            if build_json:
                trust_label = build_json.get("trust", {}).get("label", "UNKNOWN")

        response = {
            "success": True,
            "ready": ready,
            "trust_label": trust_label,
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
    import zipfile

    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/").endswith("context/ships.build.json"):
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
        default="stdio",
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
    # CLI flags override defaults; FASTMCP_* env vars are already folded
    # in by pydantic-settings at Settings construction time.
    if args.host is not None:
        mcp.settings.host = args.host
    if args.port is not None:
        mcp.settings.port = args.port
    if args.log_level is not None:
        mcp.settings.log_level = args.log_level
    if args.stateless:
        mcp.settings.stateless_http = True

    # Apply custom path for the chosen transport
    if args.http_path is not None:
        if args.transport == "streamable-http":
            mcp.settings.streamable_http_path = args.http_path
        elif args.transport == "sse":
            mcp.settings.sse_path = args.http_path

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

    # -- Emit startup banner for HTTP transports ------------------------
    if args.transport in ("streamable-http", "sse"):
        host = mcp.settings.host
        port = mcp.settings.port
        if args.transport == "streamable-http":
            path = mcp.settings.streamable_http_path
        else:
            path = mcp.settings.sse_path
        logger.info(
            "SHIPS MCP server starting — transport=%s  endpoint=http://%s:%d%s%s",
            args.transport,
            host,
            port,
            path,
            "  [stateless]" if args.stateless else "",
        )

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
