"""
mcp_tool.py — MCP Server tool wrapper for DDL deployment.

Two MCP-compatible functions:

    ddl_deployObject   Deploy a single DDL object from text.
    ddl_deployPackage  Deploy/resume/rollback a directory of DDL files.

Both support dry_run mode and return structured JSON results.
"""

import json
import logging
import os
from typing import Any, Dict

from ddl_deployer.deployer import deploy_single, deploy_package, resume_package, rollback_package

logger = logging.getLogger(__name__)


def ddl_deployObject(cursor, ddl_text: str, dry_run: bool = False) -> Dict[str, Any]:
    """
    Deploy a single Teradata DDL object idempotently.

    Detects the object type from the DDL text and applies the
    correct deployment strategy:

        TABLE       — Backup if data exists, create, migrate if
                      schema compatible. MULTISET auto-injected
                      if not specified.
        JOIN_INDEX  — DROP if exists, CREATE.
        HASH_INDEX  — DROP if exists, CREATE.
        INDEX       — DROP INDEX if exists, CREATE INDEX.
        TRIGGER     — DROP if exists, CREATE.
        VIEW        — REPLACE VIEW (inherently idempotent).
        MACRO       — REPLACE MACRO (inherently idempotent).
        PROCEDURE   — REPLACE PROCEDURE (inherently idempotent).
        FUNCTION    — REPLACE FUNCTION (inherently idempotent).

    Parameters:
        ddl_text (str, required): Complete DDL statement with
            database qualifier (e.g. CREATE TABLE Database.Table).

        dry_run (bool, optional): If True, simulate without
            executing any DDL. Default: False.

    Returns:
        JSON with keys: state, database_name, object_name,
        object_type, backup_table, rows_migrated, message,
        blockers, warnings, dry_run.
    """
    try:
        result = deploy_single(cursor, ddl_text, dry_run)
        return _result_to_dict(result)
    except ValueError as e:
        return {"state": "FAILED", "error": str(e), "message": f"Invalid DDL: {e}"}
    except Exception as e:
        logger.exception("ddl_deployObject failed")
        return {"state": "FAILED", "error": str(e), "message": f"Deployment failed: {e}"}


def ddl_deployPackage(
    cursor,
    package_dir: str,
    action: str = "deploy",
    file_patterns: str = "*.tbl,*.jix,*.idx,*.viw,*.spl,*.mcr,*.fnc,*.trg",
    ordered_files: list = None,
    waves: list = None,
    num_streams: int = 1,
    connect_fn=None,
    stop_on_failure: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Deploy, resume, or roll back a directory of Teradata DDL files.

    Mandatory pre-flight validation runs before any deployment:
    checks permissions (DBC.AllRightsV), perm space (DBC.DiskSpaceV),
    and database existence. If pre-flight fails, no DDL is executed.

    Objects deploy in dependency order by default. To override with
    a topologically sorted order, pass ordered_files.

    MULTISET is auto-injected for any CREATE TABLE that lacks an
    explicit SET or MULTISET qualifier.

    A deployment report (.html) is generated automatically.

    Parameters:
        package_dir (str, required): Path to the DDL file directory.

        action (str, optional): One of:
            'deploy'   — Fresh deployment (default).
            'resume'   — Resume from failure point.
            'rollback' — Reverse all deployed objects.

        file_patterns (str, optional): Comma-separated glob patterns.
            Ignored if ordered_files is provided.

        ordered_files (list, optional): Explicit list of DDL file
            paths in topologically sorted deployment order. Bypasses
            glob discovery and type-based reordering.

        stop_on_failure (bool, optional): Halt at first failure.
            Default: True.

        dry_run (bool, optional): Simulate without executing DDL.
            Pre-flight still runs and validates. Default: False.

    Returns:
        JSON with deployment_id, report_path, preflight summary,
        per-object results, and aggregate counts.
    """
    if not os.path.isdir(package_dir):
        return {"state": "FAILED", "error": f"Directory not found: {package_dir}"}

    patterns = [p.strip() for p in file_patterns.split(',')]

    try:
        if action == "deploy":
            result = deploy_package(
                cursor=cursor, package_dir=package_dir,
                file_patterns=patterns,
                ordered_files=ordered_files,
                waves=waves,
                num_streams=num_streams,
                connect_fn=connect_fn,
                stop_on_failure=stop_on_failure, dry_run=dry_run,
            )
        elif action == "resume":
            manifest_path = os.path.join(package_dir, ".deploy_manifest.json")
            result = resume_package(
                cursor=cursor, manifest_path=manifest_path,
                stop_on_failure=stop_on_failure, dry_run=dry_run,
            )
        elif action == "rollback":
            manifest_path = os.path.join(package_dir, ".deploy_manifest.json")
            result = rollback_package(cursor=cursor, manifest_path=manifest_path)
        else:
            return {"state": "FAILED", "error": f"Invalid action: '{action}'."}

        return _package_to_dict(result)

    except FileNotFoundError as e:
        return {"state": "FAILED", "error": str(e)}
    except Exception as e:
        logger.exception("ddl_deployPackage failed")
        return {"state": "FAILED", "error": str(e)}


def _result_to_dict(result) -> Dict[str, Any]:
    """Convert an ObjectDeployResult to a JSON-serialisable dict."""
    return {
        "state": result.state.value,
        "database_name": result.database_name,
        "object_name": result.object_name,
        "object_type": result.object_type.value,
        "backup_table": result.backup_table,
        "rows_migrated": result.rows_migrated,
        "message": result.message,
        "error": result.error,
        "blockers": result.blockers,
        "warnings": result.warnings,
        "dry_run": result.dry_run,
        "wave_number": result.wave_number,
        "stream_id": result.stream_id,
    }


def _package_to_dict(result) -> Dict[str, Any]:
    """Convert a PackageDeployResult to a JSON-serialisable dict."""
    d = {
        "deployment_id": result.deployment_id,
        "manifest_path": result.manifest_path,
        "report_path": result.report_path,
        "success": result.success,
        "total": result.total,
        "completed": result.completed,
        "skipped": result.skipped,
        "failed": result.failed,
        "rolled_back": result.rolled_back,
        "dry_run": result.dry_run,
        "num_streams": result.num_streams,
        "objects": [_result_to_dict(r) for r in result.results],
    }

    if result.wave_summaries:
        d["wave_summaries"] = [
            {
                "wave_number": ws.wave_number,
                "total": ws.total,
                "completed": ws.completed,
                "failed": ws.failed,
                "skipped": ws.skipped,
                "duration_ms": ws.duration_ms,
            }
            for ws in result.wave_summaries
        ]

    if result.preflight_result:
        pf = result.preflight_result
        d["preflight"] = {
            "passed": pf.passed,
            "errors": pf.errors,
            "warnings": pf.warnings,
            "databases": pf.databases,
            "object_count": pf.object_count,
            "checks": [
                {
                    "check": c.check_name,
                    "passed": c.passed,
                    "database": c.database,
                    "message": c.message,
                    "severity": c.severity,
                }
                for c in pf.checks
                if not c.passed or c.severity == "WARNING"
            ],
        }

    return d
