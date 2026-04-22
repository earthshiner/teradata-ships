"""
deployer.py — Core deployment orchestration.

Handles all Teradata DDL object types via three deployment strategies:

    IDEMPOTENT_DEPLOY  — Tables: backup, create, schema compare,
                         conditional data migration.
    DROP_AND_CREATE    — Join indexes, hash indexes, secondary
                         indexes, triggers: DROP if exists, CREATE.
    REPLACE_IN_PLACE   — Views, macros, procedures, functions:
                         execute as-is (REPLACE keyword is idempotent).

All deployments follow this sequence:
    1. Pre-flight validation (mandatory) — parse DDL, check
       permissions, check perm space, verify databases.
    2. Order objects — tables first, then indexes/JIs, then
       replaceable objects, then triggers.
    3. Deploy each object via its strategy.
    4. Persist state to manifest after every transition.

Dry-run mode performs steps 1-2 plus per-object existence and
schema checks, but does not execute any DDL/DML.
"""

import glob
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from ddl_deployer.ddl_parser import parse_ddl_file, parse_index_parent_table
from ddl_deployer.manifest import DeploymentManifest
from ddl_deployer.migration_builder import build_migration_sql
from ddl_deployer.models import (
    DeployIntent,
    DeployState,
    DeployStrategy,
    ObjectDeployResult,
    ObjectType,
    PackageDeployResult,
    ParsedDDL,
    DEPLOY_ORDER,
    SHOW_COMMAND_MAP,
    STRATEGY_MAP,
    SYSTEM_EXISTENCE_QUERIES,
    TABLE_KIND_MAP,
)
from ddl_deployer.preflight import run_preflight
from ddl_deployer.report import generate_report
from ddl_deployer.schema_comparator import compare_schemas, get_column_metadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------

def deploy_package(
    cursor,
    package_dir: str,
    file_patterns: List[str] = None,
    ordered_files: List[str] = None,
    waves: List[List[str]] = None,
    num_streams: int = 1,
    connect_fn=None,
    stop_on_failure: bool = True,
    dry_run: bool = False,
    skip_preflight: bool = False,
) -> PackageDeployResult:
    """
    Deploy all DDL files in a directory idempotently.

    Supports three modes:
        1. Glob discovery with type-based ordering (default).
        2. Explicit ordered file list (ordered_files).
        3. Wave-parallel deployment (waves + num_streams > 1).

    When waves are provided, objects within each wave execute in
    parallel across num_streams database connections. Wave barriers
    enforce dependency ordering. The manifest tracks wave_number
    per object for reporting and restartability.

    Args:
        cursor:           Active Teradata database cursor.
        package_dir:      Directory for manifest/report output.
        file_patterns:    Glob patterns. Ignored if ordered_files/waves set.
        ordered_files:    Explicit ordered file list. Ignored if waves set.
        waves:            List of waves, each a list of file paths.
        num_streams:      Parallel connections (1-8, default: 1).
        connect_fn:       Callable returning a new cursor. Required
                          when num_streams > 1.
        stop_on_failure:  Stop at the first failure.
        dry_run:          Simulate without executing DDL.
        skip_preflight:   Skip pre-flight checks.

    Returns:
        PackageDeployResult with per-object outcomes, wave summaries,
        and report path.
    """
    from ddl_deployer.models import WaveSummary

    # -- Determine file list --
    if waves is not None:
        ddl_files = [f for wave in waves for f in wave]
        preserve_order = True
        logger.info(
            "Wave-parallel mode: %d waves, %d objects, %d streams",
            len(waves), len(ddl_files), num_streams
        )
    elif ordered_files is not None:
        ddl_files = ordered_files
        preserve_order = True
        logger.info("Using %d pre-ordered DDL files", len(ddl_files))
    else:
        if file_patterns is None:
            file_patterns = ['*.tbl', '*.jix', '*.idx', '*.viw', '*.spl',
                             '*.mcr', '*.fnc', '*.trg', '*.db', '*.dcl',
                             '*.usr', '*.rol', '*.prf', '*.sql']
        ddl_files = []
        for pattern in file_patterns:
            ddl_files.extend(sorted(glob.glob(os.path.join(package_dir, pattern))))
        ddl_files = sorted(set(ddl_files))
        preserve_order = False

    if not ddl_files:
        raise FileNotFoundError(f"No DDL files found in {package_dir}")

    logger.info("Discovered %d DDL files", len(ddl_files))

    # -- Pre-flight validation (mandatory) --
    preflight_result = None
    skipped_files = []

    if not skip_preflight:
        preflight_result, parsed_ddls = run_preflight(cursor, ddl_files)
        if not preflight_result.passed:
            logger.error("Pre-flight FAILED: %d errors.", preflight_result.errors)
            return PackageDeployResult(
                deployment_id="preflight_failed", manifest_path="",
                total=len(ddl_files), failed=preflight_result.errors,
                preflight_result=preflight_result, dry_run=dry_run,
            )
    else:
        parsed_ddls = []
        for f in ddl_files:
            try:
                parsed_ddls.append(parse_ddl_file(f))
            except (ValueError, FileNotFoundError) as e:
                logger.error("Skipping %s: %s", f, e)
                skipped_files.append((f, str(e)))

    # -- Order (skip if pre-ordered) --
    if not preserve_order:
        parsed_ddls.sort(key=lambda p: (
            DEPLOY_ORDER.get(p.object_type, 99), p.qualified_name,
        ))

    # -- Build lookups --
    parsed_by_path = {p.file_path: p for p in parsed_ddls}
    file_wave_map = {}
    if waves is not None:
        for wave_idx, wave in enumerate(waves):
            for fpath in wave:
                file_wave_map[fpath] = wave_idx + 1

    # -- Initialise manifest with wave numbers --
    manifest = DeploymentManifest(package_dir)
    for parsed in parsed_ddls:
        intent_str = parsed.deploy_intent.value if parsed.deploy_intent else None
        manifest.register_object(
            parsed.qualified_name,
            os.path.basename(parsed.file_path),
            wave_number=file_wave_map.get(parsed.file_path),
            deploy_intent=intent_str,
            object_type=parsed.object_type.value,
        )

    # Register skipped files so they appear in the report
    skipped_results = []
    for skip_path, skip_reason in skipped_files:
        skip_name = os.path.basename(skip_path)
        skip_qn = f"SKIPPED.{skip_name}"
        manifest.register_object(skip_qn, skip_name, deploy_intent="SKIPPED")
        manifest.update_state(
            skip_qn, DeployState.SKIPPED,
            error=skip_reason,
        )
        skipped_results.append(ObjectDeployResult(
            database_name="SKIPPED",
            object_name=skip_name,
            object_type=ObjectType.UNKNOWN,
            state=DeployState.SKIPPED,
            error=skip_reason,
            message=f"Could not classify: {skip_reason}",
        ))

    # -- Execute --
    if waves is not None and num_streams > 1 and not dry_run:
        results, wave_summaries = _execute_waves_parallel(
            cursor, waves, parsed_by_path, manifest,
            num_streams, connect_fn, stop_on_failure,
        )
    elif waves is not None:
        results, wave_summaries = _execute_waves_sequential(
            cursor, waves, parsed_by_path, manifest,
            stop_on_failure, dry_run,
        )
    else:
        results = _execute_sequential(
            cursor, parsed_ddls, manifest, stop_on_failure, dry_run,
        )
        wave_summaries = []

    # -- Build result --
    # Include skipped (unclassifiable) files in results
    all_results = skipped_results + results
    summary = manifest.summary()
    pkg_result = PackageDeployResult(
        deployment_id=manifest.deployment_id,
        manifest_path=manifest.path,
        total=len(parsed_ddls) + len(skipped_results),
        completed=summary.get(DeployState.COMPLETED.value, 0),
        skipped=summary.get(DeployState.SKIPPED.value, 0),
        failed=summary.get(DeployState.FAILED.value, 0),
        rolled_back=summary.get(DeployState.ROLLED_BACK.value, 0),
        results=all_results,
        preflight_result=preflight_result,
        dry_run=dry_run,
        num_streams=num_streams,
        wave_summaries=wave_summaries,
    )

    if dry_run:
        manifest.set_package_status("DRY_RUN_COMPLETE")
    elif pkg_result.success:
        manifest.set_package_status("COMPLETED")
    elif pkg_result.failed == 0 and pkg_result.skipped > 0:
        manifest.set_package_status("PARTIALLY_COMPLETED")

    try:
        report_path = generate_report(pkg_result, package_dir)
        pkg_result.report_path = report_path
        logger.info("Report: %s", report_path)
    except Exception as e:
        logger.warning("Report generation failed (non-fatal): %s", e)

    return pkg_result


# ---------------------------------------------------------------
# Internal — Execution modes
# ---------------------------------------------------------------

def _execute_sequential(cursor, parsed_ddls, manifest, stop_on_failure, dry_run):
    """Execute objects sequentially (no waves)."""
    results = []
    for parsed in parsed_ddls:
        state = manifest.get_state(parsed.qualified_name)
        if state in (DeployState.COMPLETED, DeployState.SKIPPED,
                     DeployState.ROLLED_BACK):
            continue
        result = _dispatch_deploy(cursor, parsed, manifest, dry_run)
        results.append(result)
        if result.state == DeployState.FAILED and stop_on_failure:
            manifest.set_package_status("FAILED")
            break
    return results


def _execute_waves_sequential(cursor, waves, parsed_by_path, manifest,
                              stop_on_failure, dry_run):
    """Execute waves sequentially (1 stream or dry-run), tracking wave numbers."""
    import time
    from ddl_deployer.models import WaveSummary

    results = []
    wave_summaries = []
    failed = False

    for wave_idx, wave_files in enumerate(waves):
        wave_num = wave_idx + 1

        if failed:
            ws = WaveSummary(wave_number=wave_num, total=len(wave_files),
                             skipped=len(wave_files))
            wave_summaries.append(ws)
            for fpath in wave_files:
                parsed = parsed_by_path.get(fpath)
                if parsed:
                    manifest.update_state(parsed.qualified_name,
                                          DeployState.SKIPPED,
                                          error="Skipped — previous wave failed.")
            continue

        wave_start = time.monotonic()
        w_completed, w_failed, w_skipped = 0, 0, 0

        for fpath in wave_files:
            parsed = parsed_by_path.get(fpath)
            if not parsed:
                continue
            state = manifest.get_state(parsed.qualified_name)
            if state in (DeployState.COMPLETED, DeployState.SKIPPED,
                         DeployState.ROLLED_BACK):
                continue
            if failed:
                w_skipped += 1
                continue

            result = _dispatch_deploy(cursor, parsed, manifest, dry_run)
            result.wave_number = wave_num
            results.append(result)

            if result.state == DeployState.FAILED:
                w_failed += 1
                if stop_on_failure:
                    failed = True
            else:
                w_completed += 1

        duration = int((time.monotonic() - wave_start) * 1000)
        wave_summaries.append(WaveSummary(
            wave_number=wave_num, total=len(wave_files),
            completed=w_completed, failed=w_failed, skipped=w_skipped,
            duration_ms=duration,
        ))

        if w_failed > 0:
            logger.error("Wave %d: %d failure(s)", wave_num, w_failed)
            failed = True

        logger.info("Wave %d/%d: %d ok, %d failed, %d skipped (%d ms)",
                     wave_num, len(waves), w_completed, w_failed, w_skipped, duration)

    return results, wave_summaries


def _execute_waves_parallel(cursor, waves, parsed_by_path, manifest,
                            num_streams, connect_fn, stop_on_failure):
    """Execute waves in parallel across multiple streams."""
    import time
    from ddl_deployer.models import WaveSummary
    from ddl_deployer.wave_executor import WaveExecutor

    if connect_fn is None:
        raise ValueError(
            "connect_fn required for parallel deployment (num_streams > 1)."
        )

    # Build the deploy function for each stream
    def deploy_fn(stream_cursor, file_path):
        parsed = parsed_by_path.get(file_path)
        if not parsed:
            return {"file": file_path, "state": "FAILED",
                    "error": f"No parsed DDL for {file_path}"}
        state = manifest.get_state(parsed.qualified_name)
        if state in (DeployState.COMPLETED, DeployState.SKIPPED,
                     DeployState.ROLLED_BACK):
            return {"file": file_path, "state": state.value}
        result = _dispatch_deploy(stream_cursor, parsed, manifest, False)
        return {"file": file_path, "state": result.state.value,
                "result": result}

    all_results = []
    def on_complete(file_path, wave_result):
        if "result" in wave_result and wave_result["result"] is not None:
            all_results.append(wave_result["result"])

    executor = WaveExecutor(
        num_streams=min(max(num_streams, 1), 8),
        connect_fn=connect_fn,
    )

    try:
        exec_result = executor.execute_waves(waves, deploy_fn, on_complete)
    finally:
        executor.close_pool()

    # Build wave summaries
    wave_summaries = [
        WaveSummary(
            wave_number=w.wave_number, total=w.total,
            completed=w.completed, failed=w.failed,
            skipped=w.skipped, duration_ms=w.duration_ms,
        )
        for w in exec_result.waves
    ]

    # Assign wave numbers to results
    file_wave_map = {}
    for wi, wave_files in enumerate(waves):
        for fp in wave_files:
            file_wave_map[fp] = wi + 1
    for r in all_results:
        for p in parsed_by_path.values():
            if p.database_name == r.database_name and p.object_name == r.object_name:
                r.wave_number = file_wave_map.get(p.file_path)
                break

    return all_results, wave_summaries

def resume_package(
    cursor,
    manifest_path: str,
    stop_on_failure: bool = True,
    dry_run: bool = False,
) -> PackageDeployResult:
    """
    Resume a previously failed or interrupted deployment.

    Loads the existing manifest, identifies objects in PENDING or
    FAILED states, and attempts to deploy them. Already COMPLETED
    or SKIPPED objects are left untouched.

    Pre-flight is skipped on resume — it was validated on the
    initial run. For FAILED objects, the resume logic inspects
    actual database state to determine the correct re-entry point.

    Args:
        cursor:           Active Teradata database cursor.
        manifest_path:    Path to the .deploy_manifest.json file.
        stop_on_failure:  If True, stop at the first failure.
        dry_run:          If True, simulate without executing DDL.

    Returns:
        PackageDeployResult with per-object outcomes.
    """
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    package_dir = os.path.dirname(manifest_path)
    manifest = DeploymentManifest(package_dir)
    manifest.set_package_status("IN_PROGRESS")

    resumable = manifest.get_pending_or_failed()
    logger.info("Resuming — %d objects to process", len(resumable))

    results = []
    for qualified_name in resumable:
        record = manifest.get_record(qualified_name)
        ddl_file = os.path.join(package_dir, record["ddl_file"])

        if not os.path.exists(ddl_file):
            logger.error("DDL file missing for %s: %s", qualified_name, ddl_file)
            manifest.update_state(
                qualified_name, DeployState.FAILED,
                error=f"DDL file not found: {ddl_file}"
            )
            continue

        parsed = parse_ddl_file(ddl_file)

        # For FAILED tables, reconcile with database state
        if manifest.get_state(qualified_name) == DeployState.FAILED:
            if parsed.object_type == ObjectType.TABLE:
                _reconcile_table_state(cursor, qualified_name, record, manifest)

        result = _dispatch_deploy(cursor, parsed, manifest, dry_run)
        results.append(result)

        if result.state == DeployState.FAILED and stop_on_failure:
            manifest.set_package_status("FAILED")
            break

    summary = manifest.summary()
    pkg_result = PackageDeployResult(
        deployment_id=manifest.deployment_id,
        manifest_path=manifest.path,
        total=len(manifest.data["objects"]),
        completed=summary.get(DeployState.COMPLETED.value, 0),
        skipped=summary.get(DeployState.SKIPPED.value, 0),
        failed=summary.get(DeployState.FAILED.value, 0),
        rolled_back=summary.get(DeployState.ROLLED_BACK.value, 0),
        results=results,
        dry_run=dry_run,
    )

    if pkg_result.success:
        manifest.set_package_status("COMPLETED")

    # -- Generate deployment report --
    try:
        report_path = generate_report(pkg_result, os.path.dirname(manifest_path))
        pkg_result.report_path = report_path
    except Exception as e:
        logger.warning("Report generation failed (non-fatal): %s", e)

    return pkg_result


def rollback_package(cursor, manifest_path: str) -> PackageDeployResult:
    """
    Roll back a deployment, restoring objects to pre-deployment state.

    Processes rollback candidates in reverse order. For tables:
    drops new table and renames backup. For indexes/JIs: drops the
    newly created object (original was already dropped — cannot
    restore, but the table data is intact). For replaceable objects:
    no rollback possible (REPLACE overwrites in place).

    Args:
        cursor:         Active Teradata database cursor.
        manifest_path:  Path to the .deploy_manifest.json file.

    Returns:
        PackageDeployResult with rollback outcomes.
    """
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    package_dir = os.path.dirname(manifest_path)
    manifest = DeploymentManifest(package_dir)
    manifest.set_package_status("ROLLING_BACK")

    candidates = manifest.get_rollback_candidates()
    logger.info("Rolling back %d objects", len(candidates))

    results = []
    for qualified_name in candidates:
        record = manifest.get_record(qualified_name)
        ddl_file = os.path.join(package_dir, record["ddl_file"])

        try:
            parsed = parse_ddl_file(ddl_file)
        except Exception:
            # Can't parse — try table rollback as fallback
            parsed = None

        result = _rollback_single(cursor, qualified_name, parsed, manifest)
        results.append(result)

    manifest.set_package_status("ROLLED_BACK")

    summary = manifest.summary()
    pkg_result = PackageDeployResult(
        deployment_id=manifest.deployment_id,
        manifest_path=manifest.path,
        total=len(manifest.data["objects"]),
        completed=summary.get(DeployState.COMPLETED.value, 0),
        skipped=summary.get(DeployState.SKIPPED.value, 0),
        failed=summary.get(DeployState.FAILED.value, 0),
        rolled_back=summary.get(DeployState.ROLLED_BACK.value, 0),
        results=results,
    )

    # -- Generate deployment report --
    try:
        report_path = generate_report(pkg_result, package_dir)
        pkg_result.report_path = report_path
    except Exception as e:
        logger.warning("Report generation failed (non-fatal): %s", e)

    return pkg_result


def deploy_single(cursor, ddl_text: str, dry_run: bool = False) -> ObjectDeployResult:
    """
    Deploy a single object from DDL text (no manifest).

    Convenience function for single-object deployment via MCP tool.

    Args:
        cursor:    Active Teradata database cursor.
        ddl_text:  The full DDL statement.
        dry_run:   If True, simulate without executing.

    Returns:
        ObjectDeployResult with deployment outcome.
    """
    parsed = parse_ddl_file.__wrapped__(ddl_text) if hasattr(parse_ddl_file, '__wrapped__') else None
    # Use parse_ddl_text for inline DDL
    from ddl_deployer.ddl_parser import parse_ddl_text
    parsed = parse_ddl_text(ddl_text)

    strategy = parsed.strategy

    if strategy == DeployStrategy.IDEMPOTENT_DEPLOY:
        return _deploy_table(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.DROP_AND_CREATE:
        return _deploy_drop_and_create(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.REPLACE_IN_PLACE:
        return _deploy_replace_in_place(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.DIRECT_EXECUTE:
        return _deploy_direct_execute(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.CREATE_ONLY:
        return _deploy_create_only(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.SKIP_IF_EXISTS:
        return _deploy_skip_if_exists(cursor, parsed, dry_run)
    else:
        return ObjectDeployResult(
            database_name=parsed.database_name,
            object_name=parsed.object_name,
            object_type=parsed.object_type,
            state=DeployState.FAILED,
            error=f"Unknown strategy for {parsed.object_type.value}",
        )


# ---------------------------------------------------------------
# Internal — Strategy dispatch
# ---------------------------------------------------------------

def _dispatch_deploy(
    cursor,
    parsed: ParsedDDL,
    manifest: DeploymentManifest,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Dispatch deployment to the correct strategy and update manifest.

    Strategy is now determined by deploy_intent (inferred from DDL verb),
    not just object type. Before any destructive operation (REPLACE,
    DROP), the existing definition is captured via SHOW for rollback.

    Args:
        cursor:    Active database cursor.
        parsed:    Parsed DDL metadata.
        manifest:  Deployment manifest for state persistence.
        dry_run:   If True, simulate without executing.

    Returns:
        ObjectDeployResult with outcome.
    """
    try:
        if parsed.strategy == DeployStrategy.IDEMPOTENT_DEPLOY:
            result = _deploy_table(cursor, parsed, dry_run)
        elif parsed.strategy == DeployStrategy.CREATE_ONLY:
            result = _deploy_create_only(cursor, parsed, dry_run)
        elif parsed.strategy == DeployStrategy.DROP_AND_CREATE:
            result = _deploy_drop_and_create(cursor, parsed, manifest, dry_run)
        elif parsed.strategy == DeployStrategy.REPLACE_IN_PLACE:
            result = _deploy_replace_in_place(cursor, parsed, manifest, dry_run)
        elif parsed.strategy == DeployStrategy.DIRECT_EXECUTE:
            result = _deploy_direct_execute(cursor, parsed, dry_run)
        elif parsed.strategy == DeployStrategy.SKIP_IF_EXISTS:
            result = _deploy_skip_if_exists(cursor, parsed, dry_run)
        else:
            result = ObjectDeployResult(
                database_name=parsed.database_name,
                object_name=parsed.object_name,
                object_type=parsed.object_type,
                state=DeployState.FAILED,
                error=f"No strategy for {parsed.object_type.value}",
            )

        # Set deploy_intent on the result
        result.deploy_intent = parsed.deploy_intent

        manifest.update_state(
            parsed.qualified_name,
            result.state,
            backup_table=result.backup_table,
            rows_migrated=result.rows_migrated,
            error=result.error,
            blockers=result.blockers,
            warnings=result.warnings,
            prior_existed=result.prior_existed,
            rollback_file=result.rollback_file,
        )
        return result

    except Exception as e:
        logger.exception("Deployment failed for %s", parsed.qualified_name)
        manifest.update_state(
            parsed.qualified_name, DeployState.FAILED, error=str(e)
        )
        return ObjectDeployResult(
            database_name=parsed.database_name,
            object_name=parsed.object_name,
            object_type=parsed.object_type,
            state=DeployState.FAILED,
            deploy_intent=parsed.deploy_intent,
            error=str(e),
            message=f"Deployment failed: {e}",
        )


# ---------------------------------------------------------------
# Strategy: IDEMPOTENT_DEPLOY (tables)
# ---------------------------------------------------------------

def _deploy_table(
    cursor,
    parsed: ParsedDDL,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy a table with full idempotent backup/migrate flow.

    Flow: check exists → check data → backup → create →
          compare schemas → migrate or skip → report.
    """
    db = parsed.database_name
    tbl = parsed.object_name
    qn = parsed.qualified_name

    # -- Check existence --
    exists = _object_exists(cursor, db, tbl, 'T')

    if not exists:
        if dry_run:
            return ObjectDeployResult(
                database_name=db, object_name=tbl,
                object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
                message=f"[DRY RUN] Would CREATE {qn} (does not exist).",
                dry_run=True,
            )
        _execute_ddl(cursor, parsed.ddl_text)
        return ObjectDeployResult(
            database_name=db, object_name=tbl,
            object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
            message=f"Created {qn} (did not previously exist).",
        )

    # -- Check for data --
    has_data = _table_has_data(cursor, db, tbl)

    if not has_data:
        if dry_run:
            return ObjectDeployResult(
                database_name=db, object_name=tbl,
                object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
                message=f"[DRY RUN] Would DROP and recreate {qn} (exists, empty).",
                dry_run=True,
            )
        _drop_object(cursor, db, tbl, ObjectType.TABLE)
        _execute_ddl(cursor, parsed.ddl_text)
        return ObjectDeployResult(
            database_name=db, object_name=tbl,
            object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
            message=f"Replaced empty table {qn}.",
        )

    # -- Has data: get schema, backup, create, compare, migrate --
    old_columns = get_column_metadata(cursor, db, tbl)
    backup_name = _generate_backup_name(tbl)

    if dry_run:
        # Simulate by comparing DDL columns — we can't create the
        # new table to query its schema, so report what we know.
        return ObjectDeployResult(
            database_name=db, object_name=tbl,
            object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
            backup_table=backup_name,
            message=(
                f"[DRY RUN] Would RENAME {qn} → {backup_name}, "
                f"CREATE new table, and attempt data migration "
                f"({_count_rows(cursor, db, tbl):,} rows to migrate). "
                f"Schema compatibility cannot be fully assessed "
                f"until the new table is created."
            ),
            dry_run=True,
        )

    # Rename to backup
    _rename_table(cursor, db, tbl, backup_name)

    # Create new table
    try:
        _execute_ddl(cursor, parsed.ddl_text)
    except Exception as e:
        # DDL failed — roll back the rename
        logger.error("DDL creation failed for %s — rolling back.", qn)
        try:
            _rename_table(cursor, db, backup_name, tbl)
        except Exception as rb_err:
            logger.error("CRITICAL: Rollback rename failed: %s", rb_err)
        raise

    # Compare schemas
    new_columns = get_column_metadata(cursor, db, tbl)
    compatibility = compare_schemas(old_columns, new_columns)

    if not compatibility.can_migrate:
        return ObjectDeployResult(
            database_name=db, object_name=tbl,
            object_type=ObjectType.TABLE, state=DeployState.SKIPPED,
            backup_table=backup_name,
            message=(
                f"Created {qn} but cannot migrate data. "
                f"Backup preserved as {db}.{backup_name}."
            ),
            blockers=compatibility.blockers,
            warnings=compatibility.warnings,
        )

    # Migrate data
    migration_sql = build_migration_sql(
        db, tbl, backup_name, new_columns, compatibility
    )

    try:
        cursor.execute(migration_sql)
    except Exception as e:
        return ObjectDeployResult(
            database_name=db, object_name=tbl,
            object_type=ObjectType.TABLE, state=DeployState.FAILED,
            backup_table=backup_name, error=str(e),
            message=f"Migration failed for {qn}. Backup preserved.",
            warnings=compatibility.warnings,
        )

    row_count = _count_rows(cursor, db, tbl)

    return ObjectDeployResult(
        database_name=db, object_name=tbl,
        object_type=ObjectType.TABLE, state=DeployState.COMPLETED,
        backup_table=backup_name, rows_migrated=row_count,
        message=(
            f"Deployed {qn} — migrated {row_count:,} rows from {backup_name}."
        ),
        warnings=compatibility.warnings,
    )


# ---------------------------------------------------------------
# Strategy: DIRECT_EXECUTE (databases, users, profiles, roles, DCL)
# ---------------------------------------------------------------

def _deploy_direct_execute(
    cursor,
    parsed: ParsedDDL,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Execute DDL as-is with no pre-checks, backup, or rollback.

    Used for pre-requisite objects (CREATE DATABASE, CREATE USER,
    CREATE PROFILE, CREATE ROLE) and DCL (GRANT, REVOKE). These
    are infrastructure DDL that precedes the main object deployment.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    if dry_run:
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.COMPLETED,
            message=f"[DRY RUN] Would execute {obj_type.value}: {qn}",
            dry_run=True,
        )

    _execute_ddl(cursor, parsed.ddl_text)

    return ObjectDeployResult(
        database_name=db, object_name=obj,
        object_type=obj_type, state=DeployState.COMPLETED,
        message=f"Executed {obj_type.value}: {qn}",
    )


# ---------------------------------------------------------------
# Strategy: SKIP_IF_EXISTS (system-scope: maps, roles, profiles,
#           authorisations, foreign servers)
# ---------------------------------------------------------------

def _deploy_skip_if_exists(
    cursor,
    parsed: ParsedDDL,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Check existence first; skip silently if already present.

    Used for system-scope objects (Maps, Roles, Profiles,
    Authorisations, Foreign Servers) that are identical across
    environments and only need creating once per Teradata system.

    Existence is checked via SYSTEM_EXISTENCE_QUERIES — each
    object type has a specialised query against the appropriate
    DBC system view.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    if dry_run:
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.COMPLETED,
            message=f"[DRY RUN] Would create {obj_type.value}: {obj} "
                    f"(skip if already exists)",
            dry_run=True,
        )

    # -- Check existence via system view --
    exists = False
    existence_query = SYSTEM_EXISTENCE_QUERIES.get(obj_type)

    if existence_query:
        try:
            check_sql = existence_query.format(name=obj)
            cursor.execute(check_sql)
            row = cursor.fetchone()
            exists = row is not None
        except Exception as e:
            logger.warning(
                "Existence check failed for %s %s: %s — "
                "proceeding with CREATE.",
                obj_type.value, obj, e,
            )

    if exists:
        logger.info(
            "SKIP_IF_EXISTS: %s %s already exists — skipping.",
            obj_type.value, obj,
        )
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.SKIPPED,
            prior_existed=True,
            message=f"{obj_type.value} {obj} already exists — skipped.",
        )

    # -- Object does not exist — create it --
    _execute_ddl(cursor, parsed.ddl_text)

    logger.info(
        "SKIP_IF_EXISTS: Created %s %s.",
        obj_type.value, obj,
    )

    return ObjectDeployResult(
        database_name=db, object_name=obj,
        object_type=obj_type, state=DeployState.COMPLETED,
        prior_existed=False,
        message=f"Created {obj_type.value}: {obj}",
    )


# ---------------------------------------------------------------
# Strategy: CREATE_ONLY (developer wrote CREATE, not REPLACE)
# ---------------------------------------------------------------

def _deploy_create_only(
    cursor,
    parsed: ParsedDDL,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy an object with CREATE semantics — fail if it already exists.

    The developer wrote CREATE (not REPLACE), indicating this object
    is expected to be new. If it already exists, that is an error —
    something is wrong and the developer needs to know.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    # -- Check existence --
    table_kind = TABLE_KIND_MAP.get(obj_type)
    exists = _object_exists(cursor, db, obj, table_kind) if table_kind else False

    if dry_run:
        if exists:
            return ObjectDeployResult(
                database_name=db, object_name=obj,
                object_type=obj_type, state=DeployState.FAILED,
                prior_existed=True,
                message=(
                    f"[DRY RUN] {obj_type.value} {qn} already exists. "
                    f"CREATE_ONLY intent would fail."
                ),
                error=f"{qn} already exists (CREATE_ONLY intent).",
                dry_run=True,
            )
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.COMPLETED,
            prior_existed=False,
            message=f"[DRY RUN] Would CREATE {obj_type.value} {qn}.",
            dry_run=True,
        )

    if exists:
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.FAILED,
            prior_existed=True,
            error=(
                f"{obj_type.value} {qn} already exists. "
                f"Developer intent is CREATE_ONLY (used CREATE, not REPLACE). "
                f"If this object should be replaced, change the DDL verb to REPLACE."
            ),
        )

    # -- Create --
    _execute_ddl(cursor, parsed.ddl_text)

    return ObjectDeployResult(
        database_name=db, object_name=obj,
        object_type=obj_type, state=DeployState.COMPLETED,
        prior_existed=False,
        message=f"Created {obj_type.value} {qn} (new object).",
    )


# ---------------------------------------------------------------
# Strategy: DROP_AND_CREATE (join/hash indexes, sec. indexes, triggers)
# ---------------------------------------------------------------

def _deploy_drop_and_create(
    cursor,
    parsed: ParsedDDL,
    manifest: DeploymentManifest,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy an object by dropping (if it exists) then creating.

    Before dropping, the existing definition is captured via SHOW
    and saved to the _rollback/ directory for rollback support.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    # -- Check existence --
    if obj_type == ObjectType.INDEX:
        exists = _index_exists(cursor, db, obj)
    else:
        table_kind = TABLE_KIND_MAP.get(obj_type)
        exists = _object_exists(cursor, db, obj, table_kind) if table_kind else False

    if dry_run:
        action = "DROP and CREATE" if exists else "CREATE"
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.COMPLETED,
            prior_existed=exists,
            message=f"[DRY RUN] Would {action} {obj_type.value} {qn}.",
            dry_run=True,
        )

    # -- Capture existing definition before DROP --
    rollback_file = None
    if exists:
        rollback_file = _capture_existing_definition(
            cursor, db, obj, obj_type, os.path.dirname(manifest.path)
        )
        _drop_object(cursor, db, obj, obj_type, parsed.ddl_text)
        logger.info("Dropped existing %s %s (saved to %s)", obj_type.value, qn, rollback_file or "N/A")

    # -- Create --
    _execute_ddl(cursor, parsed.ddl_text)

    msg = f"{'Replaced' if exists else 'Created'} {obj_type.value} {qn}."
    if rollback_file:
        msg += f" Rollback saved: {os.path.basename(rollback_file)}"

    return ObjectDeployResult(
        database_name=db, object_name=obj,
        object_type=obj_type, state=DeployState.COMPLETED,
        prior_existed=exists,
        rollback_file=rollback_file,
        message=msg,
    )


# ---------------------------------------------------------------
# Strategy: REPLACE_IN_PLACE (views, macros, procedures, functions)
# ---------------------------------------------------------------

def _deploy_replace_in_place(
    cursor,
    parsed: ParsedDDL,
    manifest: DeploymentManifest,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy a replaceable object by executing the DDL directly.

    Before replacing, if the object already exists, its current
    definition is captured via SHOW and saved to the _rollback/
    directory. The REPLACE keyword then handles the actual deployment.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    # -- Check existence for rollback capture --
    table_kind = TABLE_KIND_MAP.get(obj_type)
    exists = _object_exists(cursor, db, obj, table_kind) if table_kind else False

    if dry_run:
        action = "REPLACE" if exists else "CREATE (via REPLACE)"
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.COMPLETED,
            prior_existed=exists,
            message=f"[DRY RUN] Would {action} {obj_type.value} {qn}.",
            dry_run=True,
        )

    # -- Capture existing definition before REPLACE --
    rollback_file = None
    if exists:
        rollback_file = _capture_existing_definition(
            cursor, db, obj, obj_type, os.path.dirname(manifest.path)
        )

    # -- Execute REPLACE --
    _execute_ddl(cursor, parsed.ddl_text)

    msg = f"{'Replaced' if exists else 'Created'} {obj_type.value} {qn}."
    if rollback_file:
        msg += f" Rollback saved: {os.path.basename(rollback_file)}"

    return ObjectDeployResult(
        database_name=db, object_name=obj,
        object_type=obj_type, state=DeployState.COMPLETED,
        prior_existed=exists,
        rollback_file=rollback_file,
        message=msg,
    )


# ---------------------------------------------------------------
# Internal — Rollback
# ---------------------------------------------------------------

def _rollback_single(
    cursor,
    qualified_name: str,
    parsed: Optional[ParsedDDL],
    manifest: DeploymentManifest,
) -> ObjectDeployResult:
    """
    Roll back a single object deployment.

    For tables: drop new, rename backup to original.
    For DROP_AND_CREATE objects: drop the newly created object.
        Note: the previous version was already dropped and cannot
        be restored — but the underlying table data is intact.
    For REPLACE_IN_PLACE: cannot roll back (REPLACE overwrites).
    """
    parts = qualified_name.split(".", 1)
    db, obj = parts[0], parts[1]
    record = manifest.get_record(qualified_name)
    backup_name = record.get("backup_table") if record else None
    obj_type = ObjectType.TABLE  # Default assumption

    if parsed:
        obj_type = parsed.object_type

    try:
        if STRATEGY_MAP.get(obj_type) == DeployStrategy.REPLACE_IN_PLACE:
            manifest.update_state(qualified_name, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=db, object_name=obj,
                object_type=obj_type, state=DeployState.ROLLED_BACK,
                message=(
                    f"Cannot roll back REPLACE {obj_type.value} {qualified_name} — "
                    f"previous version was overwritten in place."
                ),
                warnings=["REPLACE objects cannot be rolled back."],
            )

        if obj_type == ObjectType.TABLE:
            return _rollback_table(cursor, db, obj, backup_name, qualified_name, manifest)
        else:
            # DROP_AND_CREATE objects: just drop the new one
            if _object_exists(cursor, db, obj, TABLE_KIND_MAP.get(obj_type)):
                _drop_object(cursor, db, obj, obj_type,
                             parsed.ddl_text if parsed else None)
                message = f"Rolled back {qualified_name} — dropped {obj_type.value}."
            else:
                message = f"No action for {qualified_name} — object does not exist."

            manifest.update_state(qualified_name, DeployState.ROLLED_BACK)
            return ObjectDeployResult(
                database_name=db, object_name=obj,
                object_type=obj_type, state=DeployState.ROLLED_BACK,
                message=message,
            )

    except Exception as e:
        logger.exception("Rollback failed for %s", qualified_name)
        manifest.update_state(
            qualified_name, DeployState.FAILED,
            error=f"Rollback failed: {e}"
        )
        return ObjectDeployResult(
            database_name=db, object_name=obj,
            object_type=obj_type, state=DeployState.FAILED,
            error=str(e),
            message=f"Rollback failed for {qualified_name}: {e}",
        )


def _rollback_table(cursor, db, tbl, backup_name, qualified_name, manifest):
    """Roll back a table: drop new, rename backup to original."""
    original_exists = _object_exists(cursor, db, tbl, 'T')
    backup_exists = (
        _object_exists(cursor, db, backup_name, 'T') if backup_name else False
    )

    if original_exists and backup_exists:
        _drop_object(cursor, db, tbl, ObjectType.TABLE)
        _rename_table(cursor, db, backup_name, tbl)
        message = f"Rolled back {qualified_name} — restored from {backup_name}."
    elif original_exists and not backup_exists:
        _drop_object(cursor, db, tbl, ObjectType.TABLE)
        message = f"Rolled back {qualified_name} — dropped newly created table."
    elif not original_exists and backup_exists:
        _rename_table(cursor, db, backup_name, tbl)
        message = f"Rolled back {qualified_name} — restored from {backup_name}."
    else:
        message = f"No action for {qualified_name} — neither table nor backup exist."

    manifest.update_state(qualified_name, DeployState.ROLLED_BACK)
    return ObjectDeployResult(
        database_name=db, object_name=tbl,
        object_type=ObjectType.TABLE, state=DeployState.ROLLED_BACK,
        message=message,
    )


def _reconcile_table_state(cursor, qualified_name, record, manifest):
    """Inspect database state and correct manifest for a FAILED table."""
    parts = qualified_name.split(".", 1)
    db, tbl = parts[0], parts[1]
    backup_name = record.get("backup_table")

    original_exists = _object_exists(cursor, db, tbl, 'T')
    backup_exists = (
        _object_exists(cursor, db, backup_name, 'T') if backup_name else False
    )

    if original_exists and backup_exists:
        manifest.update_state(qualified_name, DeployState.CREATED,
                              backup_table=backup_name)
    elif not original_exists and backup_exists:
        manifest.update_state(qualified_name, DeployState.BACKED_UP,
                              backup_table=backup_name)
    elif original_exists and not backup_exists:
        manifest.update_state(qualified_name, DeployState.PENDING)
    else:
        manifest.update_state(
            qualified_name, DeployState.FAILED,
            error="Neither original nor backup exists."
        )


# ---------------------------------------------------------------
# Internal — Rollback capture (SHOW-based backup)
# ---------------------------------------------------------------

def _capture_existing_definition(
    cursor,
    database_name: str,
    object_name: str,
    object_type: ObjectType,
    package_dir: str,
) -> Optional[str]:
    """
    Capture an existing object's DDL via SHOW before replacement.

    Runs the appropriate SHOW command (SHOW VIEW, SHOW MACRO, etc.)
    and saves the output to a _rollback/ directory alongside the
    manifest. This DDL can be re-executed to restore the previous
    definition on rollback.

    Args:
        cursor:         Active database cursor.
        database_name:  Database containing the object.
        object_name:    Object name.
        object_type:    ObjectType enum.
        package_dir:    Directory for the _rollback/ output.

    Returns:
        Path to the saved rollback file, or None if capture failed.
    """
    show_cmd = SHOW_COMMAND_MAP.get(object_type)
    if not show_cmd:
        logger.warning(
            "No SHOW command mapped for %s — cannot capture rollback",
            object_type.value,
        )
        return None

    qualified = f"{database_name}.{object_name}"

    try:
        cursor.execute(f"{show_cmd} {qualified}")
        rows = cursor.fetchall()

        if not rows:
            logger.warning("SHOW %s returned no rows", qualified)
            return None

        # SHOW commands return the DDL as one or more rows of text
        ddl_lines = []
        for row in rows:
            if row and row[0]:
                ddl_lines.append(str(row[0]))

        ddl_text = "\n".join(ddl_lines)

        if not ddl_text.strip():
            return None

        # Save to _rollback/ directory
        rollback_dir = os.path.join(package_dir, "_rollback")
        os.makedirs(rollback_dir, exist_ok=True)

        # Use the appropriate extension
        from ddl_deployer.models import ObjectType as OT
        ext_map = {
            OT.VIEW: ".viw", OT.MACRO: ".mcr",
            OT.PROCEDURE: ".spl", OT.FUNCTION: ".fnc",
            OT.TRIGGER: ".trg", OT.JOIN_INDEX: ".jix",
            OT.HASH_INDEX: ".idx", OT.INDEX: ".idx",
            OT.TABLE: ".tbl",
        }
        ext = ext_map.get(object_type, ".sql")
        filename = f"{database_name}.{object_name}{ext}"
        rollback_path = os.path.join(rollback_dir, filename)

        with open(rollback_path, 'w', encoding='utf-8') as f:
            f.write(ddl_text)

        logger.info(
            "Captured rollback: %s → %s",
            qualified, rollback_path,
        )
        return rollback_path

    except Exception as e:
        logger.warning(
            "Failed to capture rollback for %s (non-fatal): %s",
            qualified, e,
        )
        return None


# ---------------------------------------------------------------
# Internal — Database operations
# ---------------------------------------------------------------

def _object_exists(cursor, database_name: str, object_name: str,
                   table_kind: str) -> bool:
    """Check if an object exists in DBC.TablesV by TableKind."""
    if cursor is None:
        return False  # Dry-run without connection — assume not exists
    try:
        cursor.execute(
            "SELECT 1 FROM DBC.TablesV "
            "WHERE DatabaseName = ? AND TableName = ? AND TableKind = ?",
            [database_name, object_name, table_kind]
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _index_exists(cursor, database_name: str, index_name: str) -> bool:
    """Check if a named secondary index exists in DBC.IndicesV."""
    if cursor is None:
        return False  # Dry-run without connection — assume not exists
    try:
        cursor.execute(
            "SELECT 1 FROM DBC.IndicesV "
            "WHERE DatabaseName = ? AND IndexName = ?",
            [database_name, index_name]
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _table_has_data(cursor, database_name: str, table_name: str) -> bool:
    """Check if a table contains any rows (TOP 1 for efficiency)."""
    cursor.execute(
        f'SELECT TOP 1 1 FROM "{database_name}"."{table_name}"'
    )
    return cursor.fetchone() is not None


def _count_rows(cursor, database_name: str, table_name: str) -> int:
    """Count total rows in a table."""
    cursor.execute(
        f'SELECT CAST(COUNT(*) AS BIGINT) FROM "{database_name}"."{table_name}"'
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def _execute_ddl(cursor, ddl_text: str):
    """Execute a DDL statement (strips trailing semicolons)."""
    clean = ddl_text.strip().rstrip(';').strip()
    cursor.execute(clean)


def _rename_table(cursor, database_name: str, old_name: str, new_name: str):
    """Rename a table within the same database."""
    cursor.execute(
        f'RENAME TABLE "{database_name}"."{old_name}" '
        f'TO "{database_name}"."{new_name}"'
    )


def _drop_object(cursor, database_name: str, object_name: str,
                 object_type: ObjectType, ddl_text: str = None):
    """
    Drop an object using the correct DROP syntax per type.

    Args:
        cursor:         Active database cursor.
        database_name:  Database containing the object.
        object_name:    Object name.
        object_type:    ObjectType determining DROP syntax.
        ddl_text:       Original DDL (needed for INDEX to extract
                        the ON clause for DROP INDEX ... ON ...).
    """
    drop_statements = {
        ObjectType.TABLE: f'DROP TABLE "{database_name}"."{object_name}"',
        ObjectType.JOIN_INDEX: f'DROP JOIN INDEX "{database_name}"."{object_name}"',
        ObjectType.HASH_INDEX: f'DROP HASH INDEX "{database_name}"."{object_name}"',
        ObjectType.TRIGGER: f'DROP TRIGGER "{database_name}"."{object_name}"',
        ObjectType.VIEW: f'DROP VIEW "{database_name}"."{object_name}"',
        ObjectType.MACRO: f'DROP MACRO "{database_name}"."{object_name}"',
        ObjectType.PROCEDURE: f'DROP PROCEDURE "{database_name}"."{object_name}"',
        ObjectType.FUNCTION: f'DROP FUNCTION "{database_name}"."{object_name}"',
    }

    if object_type == ObjectType.INDEX and ddl_text:
        # Secondary index: DROP INDEX name ON db.table
        parent = parse_index_parent_table(ddl_text)
        if parent and parent[0]:
            drop_sql = (
                f'DROP INDEX "{object_name}" '
                f'ON "{parent[0]}"."{parent[1]}"'
            )
        else:
            drop_sql = drop_statements.get(ObjectType.TABLE, '')
    else:
        drop_sql = drop_statements.get(object_type, '')

    if drop_sql:
        cursor.execute(drop_sql)
        logger.debug("Dropped %s %s.%s", object_type.value, database_name, object_name)


def _generate_backup_name(table_name: str) -> str:
    """Generate a timestamped backup name (max 128 chars)."""
    suffix = datetime.now(timezone.utc).strftime("_bkp_%Y%m%d%H%M%S")
    max_base = 128 - len(suffix)
    base = table_name[:max_base] if len(table_name) > max_base else table_name
    return base + suffix
