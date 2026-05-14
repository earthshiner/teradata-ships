"""
deployer.py — Core deployment orchestration.

Handles all Teradata DDL object types via deployment strategies:

    IDEMPOTENT_DEPLOY  — Tables: backup via RENAME, create, schema
                         compare, conditional data migration.
    DROP_AND_CREATE    — Join indexes, hash indexes, secondary
                         indexes, triggers: capture existing via
                         SHOW, DROP, CREATE.
    CREATE_ONLY        — Views, macros, procedures, functions with
                         CREATE verb: capture existing via SHOW,
                         DROP, CREATE. Deployer owns idempotency.
    REPLACE_IN_PLACE   — Views, macros, procedures, functions with
                         REPLACE verb: capture existing via SHOW,
                         execute as-is (REPLACE is idempotent).

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
import json
import logging
import os

from database_package_deployer.package_metadata import package_file
import re
import threading
from datetime import datetime, timezone
from typing import List, Optional

from database_package_deployer.statement_parser import (
    parse_statement_file,
    parse_index_parent_table,
)
from database_package_deployer.manifest import DeploymentManifest
from database_package_deployer.migration_builder import build_migration_sql
from database_package_deployer.models import (
    DeployState,
    DeployStrategy,
    ObjectDeployResult,
    ObjectType,
    PackageDeployResult,
    ParsedStatement,
    PreflightResult,
    DEPLOY_ORDER,
    SHOW_COMMAND_MAP,
    SYSTEM_EXISTENCE_QUERIES,
    TABLE_KIND_MAP,
)
from database_package_deployer.preflight import (
    check_asymmetric_signature,
    check_change_ref_present,
    check_env_lock,
    check_mpa_approval,
    check_package_age,
    check_package_hash,
    check_package_signature,
    check_tls_connection,
    run_preflight,
)
from database_package_deployer.report import generate_report
from database_package_deployer.schema_comparator import (
    compare_schemas,
    get_column_metadata,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Error message formatting
# ---------------------------------------------------------------

# Matches the start of the Go stack trace appended by teradatasql.
# Everything from this point onwards is driver internals — useful
# in log files but not in user-facing output (HTML reports, CLI).
_GO_STACK_RE = re.compile(
    r"\s*\bat\s+gosqldriver/.*",
    re.DOTALL,
)


def _clean_db_error(raw: str) -> str:
    """
    Strip the Go stack trace from teradatasql error messages.

    The teradatasql driver appends a full Go call stack to every
    database error, which is useful for driver debugging but
    alarming in user-facing output.  This function returns only
    the Teradata error portion:

        Before:
            [Error 3707] Syntax error, expected something like
            a 'COLLATION' keyword between the 'SQL' keyword and
            the word 'INLINE'. at gosqldriver/teradatasql.MakeError
            ErrorUtil.go:100 at gosqldriver/teradatasql.formatError
            ErrorUtil.go:106 at ...

        After:
            [Error 3707] Syntax error, expected something like
            a 'COLLATION' keyword between the 'SQL' keyword and
            the word 'INLINE'.

    The full unmodified error is still written to the log file
    via logger.debug() with exc_info=True.

    Args:
        raw: The raw exception string from teradatasql.

    Returns:
        The Teradata error message without the Go stack trace.
    """
    cleaned = _GO_STACK_RE.sub("", raw).strip()
    return cleaned if cleaned else raw


# ---------------------------------------------------------------
# Package metadata helpers
# ---------------------------------------------------------------


def _load_baseline_dir(package_dir: str) -> str:
    """Read ``baseline_dir`` from ships.build.json, or return empty string.

    An empty return means drift detection was not configured in
    ``ships.yaml`` at build time and is therefore disabled.

    Args:
        package_dir: Directory containing ships.build.json.

    Returns:
        Baseline directory path string, or ``""`` if absent.
    """
    build_json = package_file(package_dir, "ships.build.json")
    if not os.path.isfile(build_json):
        return ""
    try:
        with open(build_json, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("baseline_dir", "") or ""
    except Exception:  # noqa: BLE001
        logger.debug("deployer: could not read baseline_dir from ships.build.json")
    return ""


def _run_show_text(
    cursor,
    database_name: str,
    object_name: str,
    object_type,
) -> Optional[str]:
    """Run the appropriate SHOW command and return the raw DDL text.

    Used for both drift comparison (pre-deploy) and baseline capture
    (post-deploy).  Returns ``None`` when no SHOW command is mapped for
    the object type or when the SHOW fails or returns no rows.

    Args:
        cursor:        Active database cursor.
        database_name: Database containing the object.
        object_name:   Object name.
        object_type:   ``ObjectType`` enum value.

    Returns:
        SHOW output as a single string, or ``None``.
    """
    show_cmd = SHOW_COMMAND_MAP.get(object_type)
    if not show_cmd:
        return None
    qualified = f"{database_name}.{object_name}"
    try:
        cursor.execute(f"{show_cmd} {qualified}")
        rows = cursor.fetchall()
        if not rows:
            return None
        lines = [str(row[0]) for row in rows if row and row[0]]
        text = "\n".join(lines)
        return text if text.strip() else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("deployer: SHOW %s failed (non-fatal): %s", qualified, exc)
        return None


def _load_build_extensions(package_dir: str) -> Optional[list]:
    """Return the ``discovery.extensions`` list from ships.build.json, or ``None``.

    Reads the ``discovery.extensions`` field stamped by the packager
    at build time.  Returns ``None`` when ships.build.json is absent, the
    field is missing, or the value is not a list of strings — callers
    should fall back to the hard-coded default set in that case.

    Args:
        package_dir: Directory containing ships.build.json.

    Returns:
        Sorted list of normalised extension strings (e.g.
        ``[".bteq", ".sql", ".tbl", ...]``) or ``None``.
    """
    build_json = package_file(package_dir, "ships.build.json")
    if not os.path.isfile(build_json):
        return None
    try:
        with open(build_json, encoding="utf-8") as f:
            data = json.load(f)
        exts = data.get("discovery", {}).get("extensions")
        if isinstance(exts, list) and all(isinstance(e, str) for e in exts):
            return exts
    except Exception:  # noqa: BLE001
        logger.debug(
            "deployer: could not read discovery.extensions from ships.build.json"
        )
    return None


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
    baseline_dir: Optional[str] = None,
    on_drift: str = "abort",
    deployed_env: str = "",
    approval_code: str = "",
    connection_params: Optional[dict] = None,
    public_key_path: str = "",
) -> PackageDeployResult:
    """
    Deploy all DDL files in a directory idempotently.

    Thin traced wrapper — see ``_deploy_package_impl`` for the full
    implementation.  Emits a ``ships.deploy`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured, and OpenLineage
    ``RunEvent`` messages when ``OPENLINEAGE_URL`` is configured.

    Args:
        baseline_dir: Shared filesystem path for schema drift baselines.
                      When ``None``, reads from ships.build.json (stamped from
                      ``ships.yaml``'s ``deployment.baseline_dir``).
                      Empty string or unconfigured ships.build.json → drift
                      detection disabled.
        on_drift:     Action when drift is detected: ``abort`` (default),
                      ``skip``, or ``continue``.
    """
    from ships_lineage import (
        complete_deploy_run,
        fail_deploy_run,
        start_deploy_run,
    )
    from ships_tracing import stage_span

    # Resolve baseline_dir: explicit arg > ships.build.json > disabled
    _effective_baseline_dir = (
        baseline_dir if baseline_dir is not None else _load_baseline_dir(package_dir)
    )
    if _effective_baseline_dir:
        logger.info(
            "Drift detection active — baseline dir: %s (on_drift=%s)",
            _effective_baseline_dir,
            on_drift,
        )
    else:
        logger.debug(
            "Drift detection disabled — set deployment.baseline_dir in ships.yaml to enable"
        )

    _ol_run_id = start_deploy_run(package_dir, dry_run=dry_run)
    _start_time = __import__("time").monotonic()

    wave_count = len(waves) if waves else 0
    with stage_span(
        "ships.deploy",
        **{
            "ships.package_dir": package_dir,
            "ships.dry_run": dry_run,
            "ships.num_streams": num_streams,
            "ships.wave_count": wave_count,
        },
    ) as _span:
        try:
            result = _deploy_package_impl(
                cursor,
                package_dir,
                file_patterns=file_patterns,
                ordered_files=ordered_files,
                waves=waves,
                num_streams=num_streams,
                connect_fn=connect_fn,
                stop_on_failure=stop_on_failure,
                dry_run=dry_run,
                skip_preflight=skip_preflight,
                baseline_dir=_effective_baseline_dir,
                on_drift=on_drift,
                deployed_env=deployed_env,
                approval_code=approval_code,
                connection_params=connection_params,
                public_key_path=public_key_path,
            )
        except Exception as exc:
            fail_deploy_run(
                _ol_run_id,
                package_dir,
                error=str(exc),
            )
            _duration = __import__("time").monotonic() - _start_time
            try:
                from database_package_deployer.audit import emit_audit_event

                emit_audit_event(
                    package_dir=package_dir,
                    outcome="FAILURE",
                    objects_deployed=0,
                    objects_failed=0,
                    duration_seconds=_duration,
                )
            except Exception as _ae:
                logger.warning("Audit emission failed (non-fatal): %s", _ae)
            raise

        _span.set_attribute("ships.total", result.total)
        _span.set_attribute("ships.completed", result.completed)
        _span.set_attribute("ships.failed", result.failed)
        _span.set_attribute("ships.skipped", result.skipped)
        _span.set_attribute("ships.success", result.success)

        _completed = [
            (r.database_name, r.object_name)
            for r in result.results
            if r.state == DeployState.COMPLETED
        ]
        if result.success:
            complete_deploy_run(_ol_run_id, package_dir, _completed)
        else:
            _failed = [
                (r.database_name, r.object_name, r.error or "")
                for r in result.results
                if r.state == DeployState.FAILED
            ]
            fail_deploy_run(
                _ol_run_id,
                package_dir,
                completed_objects=_completed,
                failed_objects=_failed,
            )

        # GAP-007: emit audit event at Ship completion (success or failure).
        _duration = __import__("time").monotonic() - _start_time
        try:
            from database_package_deployer.audit import emit_audit_event

            emit_audit_event(
                package_dir=package_dir,
                outcome="SUCCESS" if result.success else "FAILURE",
                objects_deployed=result.completed,
                objects_failed=result.failed,
                duration_seconds=_duration,
            )
        except Exception as _ae:
            logger.warning("Audit emission failed (non-fatal): %s", _ae)

        return result


def _deploy_package_impl(
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
    baseline_dir: str = "",
    on_drift: str = "abort",
    deployed_env: str = "",
    approval_code: str = "",
    connection_params: Optional[dict] = None,
    public_key_path: str = "",
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

    # -- Determine file list --
    if waves is not None:
        ddl_files = [f for wave in waves for f in wave]
        preserve_order = True
        logger.info(
            "Wave-parallel mode: %d waves, %d objects, %d streams",
            len(waves),
            len(ddl_files),
            num_streams,
        )
    elif ordered_files is not None:
        ddl_files = ordered_files
        preserve_order = True
        logger.info("Using %d pre-ordered DDL files", len(ddl_files))
    else:
        if file_patterns is None:
            build_exts = _load_build_extensions(package_dir)
            if build_exts is not None:
                file_patterns = [f"*{ext}" for ext in build_exts]
                logger.debug(
                    "deployer: using %d extensions from ships.build.json discovery block",
                    len(file_patterns),
                )
            else:
                # Compile-time fallback for packages built before issue #50.
                file_patterns = [
                    "*.tbl",
                    "*.jix",
                    "*.idx",
                    "*.viw",
                    "*.spl",
                    "*.mcr",
                    "*.fnc",
                    "*.trg",
                    "*.db",
                    "*.dcl",
                    "*.usr",
                    "*.rol",
                    "*.prf",
                    # SQLJ install scripts. Without this pattern, the
                    # binary-harvested .sjr files were silently dropped
                    # from the deploy plan, so JAR-binary procedures
                    # found no installed JAR at deploy time.
                    "*.sjr",
                    "*.sql",
                    # BTEQ-style extensions used by legacy Teradata
                    # codebases that name their pure-SQL CREATE TABLE /
                    # CREATE VIEW scripts ``.bteq`` or ``.btq``.
                    # Without these patterns, glob discovery silently
                    # drops them and the deploy plan ships missing the
                    # underlying objects.
                    "*.bteq",
                    "*.btq",
                    # DML scripts (INSERT/UPDATE/DELETE/MERGE). Without
                    # this pattern the packager-emitted .dml artefacts
                    # would be silently dropped, leaving target tables
                    # unpopulated after deploy.
                    "*.dml",
                ]
        ddl_files = []
        for pattern in file_patterns:
            ddl_files.extend(sorted(glob.glob(os.path.join(package_dir, pattern))))
        ddl_files = sorted(set(ddl_files))
        preserve_order = False

    if not ddl_files:
        raise FileNotFoundError(f"No DDL files found in {package_dir}")

    logger.info("Discovered %d DDL files", len(ddl_files))

    # -- Package-level security checks (GAP-001, GAP-002) --
    # These run unconditionally — skip_preflight does not bypass them.
    pkg_level_checks: List = []

    # GAP-001: verify release ZIP against its SHA-256 sidecar.
    pkg_level_checks.extend(check_package_hash(package_dir))

    # GAP-002: verify package's target_env matches the operator's --env flag.
    pkg_level_checks.extend(check_env_lock(package_dir, deployed_env))

    # GAP-004: verify change ticket reference is present when required.
    pkg_level_checks.extend(check_change_ref_present(package_dir))

    # GAP-005: verify HMAC-SHA256 package signature sidecar.
    pkg_level_checks.extend(check_package_signature(package_dir))

    # GAP-006: verify 4-eyes approval code when require_approvals >= 2.
    pkg_level_checks.extend(check_mpa_approval(package_dir, approval_code))

    # GAP-012: warn or fail if the package exceeds its TTL.
    pkg_level_checks.extend(check_package_age(package_dir))

    # GAP-015: warn if the connection is not using TLS/SSL.
    pkg_level_checks.extend(check_tls_connection(package_dir, connection_params))

    # Option C: verify Ed25519 asymmetric signature sidecar.
    pkg_level_checks.extend(check_asymmetric_signature(package_dir, public_key_path))

    pkg_level_errors = [c for c in pkg_level_checks if not c.passed]
    if pkg_level_errors:
        logger.error(
            "Package-level security check FAILED: %s",
            pkg_level_errors[0].message,
        )
        failed_preflight = PreflightResult(
            passed=False,
            checks=pkg_level_checks,
            errors=len(pkg_level_errors),
        )
        return PackageDeployResult(
            deployment_id="package_check_failed",
            manifest_path="",
            total=len(ddl_files),
            failed=len(pkg_level_errors),
            preflight_result=failed_preflight,
            dry_run=dry_run,
        )

    # -- Pre-flight validation (mandatory) --
    preflight_result = None
    skipped_files = []

    if not skip_preflight:
        preflight_result, parsed_ddls = run_preflight(cursor, ddl_files)
        if not preflight_result.passed:
            logger.error("Pre-flight FAILED: %d errors.", preflight_result.errors)
            pkg_result = PackageDeployResult(
                deployment_id="preflight_failed",
                manifest_path="",
                total=len(ddl_files),
                failed=preflight_result.errors,
                preflight_result=preflight_result,
                dry_run=dry_run,
            )
            # Generate report even on preflight failure so the
            # DBA can see what went wrong in a readable format.
            try:
                report_path = generate_report(pkg_result, package_dir)
                pkg_result.report_path = report_path
                logger.info("Report: %s", report_path)
            except Exception as e:
                logger.warning("Report generation failed (non-fatal): %s", e)
            return pkg_result
    else:
        parsed_ddls = []
        for f in ddl_files:
            try:
                parsed_ddls.append(parse_statement_file(f))
            except (ValueError, FileNotFoundError) as e:
                logger.error("Skipping %s: %s", f, e)
                skipped_files.append((f, str(e)))

    # -- Order (skip if pre-ordered) --
    if not preserve_order:
        parsed_ddls.sort(
            key=lambda p: (
                DEPLOY_ORDER.get(p.object_type, 99),
                p.qualified_name,
            )
        )

    # -- Deployer privilege check --
    # Verifies the deploying user has CREATE + DROP rights on
    # each target database for the object types in the package.
    # Databases being created by this package are skipped
    # (automatic creator rights).
    if not dry_run and not skip_preflight:
        from database_package_deployer.privilege_check import check_deployer_privileges

        created_databases = {
            p.qualified_name
            for p in parsed_ddls
            if p.object_type == ObjectType.DATABASE
        }

        priv_result = check_deployer_privileges(
            cursor=cursor,
            parsed_ddls=parsed_ddls,
            created_databases=created_databases,
            package_name=getattr(preflight_result, "package_name", ""),
            environment=getattr(preflight_result, "environment", ""),
        )

        if not priv_result.passed:
            logger.error(
                "Deployer privilege check FAILED.\n\n"
                "The deploying user '%s' is missing privileges on "
                "%d database(s).\n"
                "Run the following as System Administrator before "
                "deploying:\n\n%s",
                priv_result.user,
                len(priv_result.missing),
                priv_result.script,
            )
            pkg_result = PackageDeployResult(
                deployment_id="privilege_check_failed",
                manifest_path="",
                total=len(parsed_ddls),
                failed=len(priv_result.missing),
                preflight_result=preflight_result,
                dry_run=dry_run,
            )
            try:
                report_path = generate_report(pkg_result, package_dir)
                pkg_result.report_path = report_path
            except Exception as e:
                logger.warning(
                    "Report generation failed (non-fatal): %s",
                    e,
                )
            return pkg_result

    # -- Build lookups --
    parsed_by_path = {p.file_path: p for p in parsed_ddls}
    file_wave_map = {}
    if waves is not None:
        for wave_idx, wave in enumerate(waves):
            for fpath in wave:
                file_wave_map[fpath] = wave_idx + 1

    # -- Initialise manifest with wave numbers --
    manifest = DeploymentManifest(package_dir)

    # -- Verify stale COMPLETED entries against database --
    # If a prior deployment marked objects as COMPLETED but the
    # database was subsequently dropped or cleaned, the manifest
    # would block re-deployment. This check resets any COMPLETED
    # object that no longer exists in the database to PENDING.
    # Skipped in dry-run mode (no live database connection).
    if not dry_run:
        checker = _build_redeploy_checker(manifest)
        reset_names = manifest.prepare_for_redeploy(checker, cursor)
        if reset_names:
            logger.info(
                "Reset %d stale manifest entries — will re-deploy.",
                len(reset_names),
            )

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
            skip_qn,
            DeployState.SKIPPED,
            error=skip_reason,
        )
        skipped_results.append(
            ObjectDeployResult(
                database_name="SKIPPED",
                object_name=skip_name,
                object_type=ObjectType.UNKNOWN,
                state=DeployState.SKIPPED,
                error=skip_reason,
                message=f"Could not classify: {skip_reason}",
            )
        )

    # -- Execute --
    if waves is not None and num_streams > 1 and not dry_run:
        results, wave_summaries = _execute_waves_parallel(
            cursor,
            waves,
            parsed_by_path,
            manifest,
            num_streams,
            connect_fn,
            stop_on_failure,
            baseline_dir=baseline_dir,
            on_drift=on_drift,
        )
    elif waves is not None:
        results, wave_summaries = _execute_waves_sequential(
            cursor,
            waves,
            parsed_by_path,
            manifest,
            stop_on_failure,
            dry_run,
            baseline_dir=baseline_dir,
            on_drift=on_drift,
        )
    else:
        results = _execute_sequential(
            cursor,
            parsed_ddls,
            manifest,
            stop_on_failure,
            dry_run,
            baseline_dir=baseline_dir,
            on_drift=on_drift,
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
        prior_completed=manifest.get_prior_completed(),
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


def _execute_sequential(
    cursor,
    parsed_ddls,
    manifest,
    stop_on_failure,
    dry_run,
    baseline_dir="",
    on_drift="abort",
):
    """Execute objects sequentially (no waves)."""
    results = []
    for parsed in parsed_ddls:
        state = manifest.get_state(parsed.qualified_name)
        if state in (
            DeployState.COMPLETED,
            DeployState.SKIPPED,
            DeployState.ROLLED_BACK,
        ):
            continue
        result = _dispatch_deploy(
            cursor,
            parsed,
            manifest,
            dry_run,
            baseline_dir=baseline_dir,
            on_drift=on_drift,
        )
        results.append(result)
        if result.state == DeployState.FAILED and stop_on_failure:
            manifest.set_package_status("FAILED")
            break
    return results


def _execute_waves_sequential(
    cursor,
    waves,
    parsed_by_path,
    manifest,
    stop_on_failure,
    dry_run,
    baseline_dir="",
    on_drift="abort",
):
    """Execute waves sequentially (1 stream or dry-run), tracking wave numbers."""
    import time
    from database_package_deployer.models import WaveSummary

    results = []
    wave_summaries = []
    failed = False

    for wave_idx, wave_files in enumerate(waves):
        wave_num = wave_idx + 1

        if failed:
            ws = WaveSummary(
                wave_number=wave_num, total=len(wave_files), skipped=len(wave_files)
            )
            wave_summaries.append(ws)
            for fpath in wave_files:
                parsed = parsed_by_path.get(fpath)
                if parsed:
                    manifest.update_state(
                        parsed.qualified_name,
                        DeployState.SKIPPED,
                        error="Skipped — previous wave failed.",
                    )
            continue

        wave_start = time.monotonic()
        w_completed, w_failed, w_skipped = 0, 0, 0

        for fpath in wave_files:
            parsed = parsed_by_path.get(fpath)
            if not parsed:
                continue
            state = manifest.get_state(parsed.qualified_name)
            if state in (
                DeployState.COMPLETED,
                DeployState.SKIPPED,
                DeployState.ROLLED_BACK,
            ):
                continue
            if failed:
                w_skipped += 1
                continue

            result = _dispatch_deploy(
                cursor,
                parsed,
                manifest,
                dry_run,
                baseline_dir=baseline_dir,
                on_drift=on_drift,
            )
            result.wave_number = wave_num
            results.append(result)

            if result.state == DeployState.FAILED:
                w_failed += 1
                if stop_on_failure:
                    failed = True
            else:
                w_completed += 1

        duration = int((time.monotonic() - wave_start) * 1000)
        wave_summaries.append(
            WaveSummary(
                wave_number=wave_num,
                total=len(wave_files),
                completed=w_completed,
                failed=w_failed,
                skipped=w_skipped,
                duration_ms=duration,
            )
        )

        if w_failed > 0:
            logger.error("Wave %d: %d failure(s)", wave_num, w_failed)
            failed = True

        logger.info(
            "Wave %d/%d: %d ok, %d failed, %d skipped (%d ms)",
            wave_num,
            len(waves),
            w_completed,
            w_failed,
            w_skipped,
            duration,
        )

    return results, wave_summaries


def _execute_waves_parallel(
    cursor,
    waves,
    parsed_by_path,
    manifest,
    num_streams,
    connect_fn,
    stop_on_failure,
    baseline_dir="",
    on_drift="abort",
):
    """
    Execute waves in parallel across multiple streams.

    System and DCL operations (GRANT, DATABASE, ROLE, USER,
    PROFILE) are serialised through a single lock to prevent
    Teradata deadlocks (Error 2631) and concurrent change
    conflicts (Error 3598) on system catalogue tables.  DDL
    operations (TABLE, VIEW, MACRO, PROCEDURE, FUNCTION,
    TRIGGER, INDEX) remain fully parallel.
    """
    from database_package_deployer.models import WaveSummary
    from database_package_deployer.wave_executor import WaveExecutor

    if connect_fn is None:
        raise ValueError(
            "connect_fn required for parallel deployment (num_streams > 1)."
        )

    # Object types that must run one-at-a-time to avoid deadlocks
    # on Teradata system catalogue tables.  These are infrastructure
    # and access-control operations — they're sub-second each, so
    # serialising has negligible impact on total deployment time.
    _SERIALISE_TYPES = frozenset(
        {
            ObjectType.GRANT,
            ObjectType.DATABASE,
            ObjectType.USER,
            ObjectType.ROLE,
            ObjectType.PROFILE,
        }
    )
    _dcl_lock = threading.Lock()

    # Build the deploy function for each stream
    def deploy_fn(stream_cursor, file_path):
        parsed = parsed_by_path.get(file_path)
        if not parsed:
            return {
                "file": file_path,
                "state": "FAILED",
                "error": f"No parsed DDL for {file_path}",
            }
        state = manifest.get_state(parsed.qualified_name)
        if state in (
            DeployState.COMPLETED,
            DeployState.SKIPPED,
            DeployState.ROLLED_BACK,
        ):
            return {"file": file_path, "state": state.value}

        # Serialise system/DCL to prevent deadlocks
        if parsed.object_type in _SERIALISE_TYPES:
            with _dcl_lock:
                result = _dispatch_deploy(
                    stream_cursor,
                    parsed,
                    manifest,
                    False,
                    baseline_dir=baseline_dir,
                    on_drift=on_drift,
                )
        else:
            result = _dispatch_deploy(
                stream_cursor,
                parsed,
                manifest,
                False,
                baseline_dir=baseline_dir,
                on_drift=on_drift,
            )

        return {"file": file_path, "state": result.state.value, "result": result}

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
            wave_number=w.wave_number,
            total=w.total,
            completed=w.completed,
            failed=w.failed,
            skipped=w.skipped,
            duration_ms=w.duration_ms,
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
    baseline_dir: str = "",
    on_drift: str = "abort",
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

    # Verify stale COMPLETED entries against the database before
    # picking up resumable work. The same replay bug that affects
    # deploy_package — manifest claims COMPLETED but the database
    # was dropped/cleaned between runs — applies here. Without this
    # check, resume would silently skip objects that no longer exist.
    # Skipped in dry-run mode (no live cursor) and when no cursor
    # was supplied (defensive — resume normally requires a connection).
    if not dry_run and cursor is not None:
        checker = _build_redeploy_checker(manifest)
        reset_names = manifest.prepare_for_redeploy(checker, cursor)
        if reset_names:
            logger.info(
                "Reset %d stale manifest entries — will re-deploy.",
                len(reset_names),
            )

    resumable = manifest.get_pending_or_failed()

    # Reset cascade-skipped objects — these were never attempted,
    # they were only skipped because a prior wave failed. On resume,
    # they should be retried now that the user has (presumably)
    # fixed the root cause.
    for qn in list(manifest.data.get("objects", {}).keys()):
        record = manifest.get_record(qn)
        if not record:
            continue
        state = record.get("state")
        error = record.get("error", "")
        if state == DeployState.SKIPPED.value and "previous wave" in error:
            manifest.update_state(qn, DeployState.PENDING)
            logger.info("  Reset cascade-skipped: %s → PENDING", qn)
            if qn not in resumable:
                resumable.append(qn)

    logger.info("Resuming — %d objects to process", len(resumable))

    results = []
    for qualified_name in resumable:
        record = manifest.get_record(qualified_name)
        ddl_file = os.path.join(package_dir, record["ddl_file"])

        if not os.path.exists(ddl_file):
            logger.error("DDL file missing for %s: %s", qualified_name, ddl_file)
            manifest.update_state(
                qualified_name,
                DeployState.FAILED,
                error=f"DDL file not found: {ddl_file}",
            )
            continue

        parsed = parse_statement_file(ddl_file)

        # For FAILED tables, reconcile with database state
        if manifest.get_state(qualified_name) == DeployState.FAILED:
            if parsed.object_type == ObjectType.TABLE:
                _reconcile_table_state(cursor, qualified_name, record, manifest)

        result = _dispatch_deploy(
            cursor,
            parsed,
            manifest,
            dry_run,
            baseline_dir=baseline_dir,
            on_drift=on_drift,
        )
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
        prior_completed=manifest.get_prior_completed(),
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


def rollback_package(
    cursor,
    manifest_path: str,
    dry_run: bool = False,
    wave_number: Optional[int] = None,
) -> PackageDeployResult:
    """
    Roll back a deployment, restoring objects to pre-deployment state.

    Processes rollback candidates in reverse order. For tables:
    drops new table and renames backup. For replaceable objects
    (views, procedures, macros, functions): drops the current object
    and re-executes the SHOW DDL captured before deployment. For
    newly created objects with no prior state: drops the object.

    Args:
        cursor:         Active Teradata database cursor.
        manifest_path:  Path to the .deploy_manifest.json file.
        dry_run:        If True, report what *would* be rolled back
                        without executing any DDL or mutating the
                        manifest. The manifest is read but never
                        written; the returned results carry
                        ``dry_run=True`` and describe the planned
                        action for each candidate.
        wave_number:    When supplied, restrict rollback to objects
                        deployed in this specific wave only. Objects
                        in other waves are untouched. The package-
                        level status is set to ``PARTIALLY_ROLLED_BACK``
                        instead of ``ROLLED_BACK``.

    Returns:
        PackageDeployResult with rollback outcomes (or planned
        actions when dry_run=True).
    """
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    package_dir = os.path.dirname(manifest_path)
    manifest = DeploymentManifest(package_dir)

    if not dry_run:
        manifest.set_package_status("ROLLING_BACK")

    candidates = manifest.get_rollback_candidates(wave_number=wave_number)
    scope = f"wave {wave_number}" if wave_number is not None else "package"
    logger.info(
        "%s %d objects (%s)",
        "[DRY RUN] Would roll back" if dry_run else "Rolling back",
        len(candidates),
        scope,
    )

    results = []
    for qualified_name in candidates:
        record = manifest.get_record(qualified_name)
        ddl_file = os.path.join(package_dir, record["ddl_file"])

        try:
            parsed = parse_statement_file(ddl_file)
        except Exception:
            # Can't parse — try table rollback as fallback
            parsed = None

        result = _rollback_single(
            cursor, qualified_name, parsed, manifest, dry_run=dry_run
        )
        results.append(result)

    if not dry_run:
        # Wave rollback leaves the rest of the deployment intact;
        # use PARTIALLY_ROLLED_BACK to signal that only a subset of
        # objects was rolled back.
        final_status = (
            "PARTIALLY_ROLLED_BACK" if wave_number is not None else "ROLLED_BACK"
        )
        manifest.set_package_status(final_status)

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
    from database_package_deployer.statement_parser import parse_statement_text

    parsed = parse_statement_text(ddl_text)

    strategy = parsed.strategy

    if strategy == DeployStrategy.IDEMPOTENT_DEPLOY:
        return _deploy_table(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.DROP_AND_CREATE:
        return _deploy_drop_and_create(cursor, parsed, None, dry_run)
    elif strategy == DeployStrategy.REPLACE_IN_PLACE:
        return _deploy_replace_in_place(cursor, parsed, None, dry_run)
    elif strategy == DeployStrategy.DIRECT_EXECUTE:
        return _deploy_direct_execute(cursor, parsed, dry_run)
    elif strategy == DeployStrategy.CREATE_ONLY:
        return _deploy_create_only(cursor, parsed, None, dry_run)
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
# Public API — EXPLAIN validation
# ---------------------------------------------------------------

# Object types where EXPLAIN is not applicable.
# PROCEDURE: Teradata cannot EXPLAIN multi-statement procedure
# bodies (REPLACE PROCEDURE ... BEGIN ... END).  EXPLAIN only
# validates single SQL statements — procedure bodies contain
# multiple statements separated by semicolons, which the
# EXPLAIN parser rejects with Error 3706 "Invalid SQL Statement".
# Functions, views, tables, triggers, macros, and all other
# DDL types support EXPLAIN normally.
# Object types that are exempt from EXPLAIN validation. Two reasons
# an object type ends up here:
#
#   TECHNICAL:  Teradata EXPLAIN rejects the SQL form the deployer
#               produces. Stored procedures compile as a unit via
#               ``EXPLAIN PROCEDURE`` (not ``EXPLAIN CREATE PROCEDURE``)
#               and the full body contains multiple statements separated
#               by semicolons that EXPLAIN rejects with Error 3706.
#
#   STRUCTURAL: Validating via EXPLAIN would always produce a false
#               failure because the database state the check depends on
#               cannot exist during a dry run.
#
#               DATABASE and USER creation is the canonical case:
#               ``CREATE DATABASE CHILD FROM PARENT`` requires PARENT
#               to exist on the target at EXPLAIN time. When both
#               PARENT and CHILD are being created by the same package
#               (a common hierarchy), PARENT will not yet exist on the
#               target — but deploying it to make EXPLAIN work would
#               break the dry-run contract (DDL is auto-commit;
#               there is no rollback path). The result would be a
#               guaranteed false failure for every child in the
#               hierarchy, making the report untrustworthy.
#
#               Preflight already validates the meaningful checks for
#               DATABASE/USER: the deploying user has CREATE
#               DATABASE/USER rights on the parent, and the parent
#               exists (flagging "will be created by this package"
#               when appropriate). EXPLAIN adds nothing beyond what
#               preflight already covers and what the topological
#               ordering in _order.txt guarantees.
#
# These objects appear as ``PREREQ_EXEMPT`` in the EXPLAIN report
# rather than FAILED or SKIPPED, so the DBA can see at a glance
# that they were intentionally not EXPLAINed and why.
_EXPLAIN_SKIP_TYPES = {
    ObjectType.PROCEDURE,  # technical — see comment above
}
_PREREQ_EXEMPT_TYPES = {
    # Hierarchy-circular: EXPLAIN requires the parent/grantee to exist.
    # DATABASE and USER form hierarchies (CREATE x FROM parent); GRANT
    # and ROLE statements reference databases that don't yet exist when
    # the prereqs haven't been deployed. Preflight validates rights;
    # _order.txt guarantees sequence.
    ObjectType.DATABASE,
    ObjectType.USER,
    ObjectType.ROLE,  # CREATE ROLE has no EXPLAIN form (Error 3706)
    ObjectType.GRANT,  # GRANTs on in-package databases always fail
    # EXPLAIN (Error 3802 "database does not exist")
    # when the database is itself created by this package
}


def explain_package(
    cursor,
    package_dir: str,
    ordered_files: List[str] = None,
    waves: List[List[str]] = None,
) -> PackageDeployResult:
    """
    Validate all DDL files by running EXPLAIN against the live system.

    Thin traced wrapper — see ``_explain_package_impl`` for the full
    implementation.  Emits a ``ships.explain`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.
    """
    from ships_tracing import stage_span

    wave_count = len(waves) if waves else 0
    with stage_span(
        "ships.explain",
        **{
            "ships.package_dir": package_dir,
            "ships.wave_count": wave_count,
        },
    ) as _span:
        result = _explain_package_impl(
            cursor,
            package_dir,
            ordered_files=ordered_files,
            waves=waves,
        )
        _span.set_attribute("ships.total", result.total)
        _span.set_attribute("ships.completed", result.completed)
        _span.set_attribute("ships.failed", result.failed)
        _span.set_attribute("ships.success", result.success)
        return result


def _explain_package_impl(
    cursor,
    package_dir: str,
    ordered_files: List[str] = None,
    waves: List[List[str]] = None,
) -> PackageDeployResult:
    """
    Validate all DDL files by running EXPLAIN against the live system.

    EXPLAIN compiles each SQL statement against the current database
    state — resolving references, checking permissions, and
    validating types — without executing. This catches errors that
    a dry run (no connection) cannot detect.

    On a fresh deployment, EXPLAIN will fail for objects that
    reference other objects being created in the same package
    (e.g. a view referencing a table that doesn't exist yet).
    These failures are detected by cross-referencing the error
    against the package's object index and reported as
    PASS (dependency in package) rather than FAIL.

    Args:
        cursor:         Active Teradata database cursor.
        package_dir:    Directory containing the package.
        ordered_files:  Pre-ordered list of DDL file paths.
        waves:          List of waves (list of file path lists).

    Returns:
        PackageDeployResult with per-object outcomes.
    """
    from database_package_deployer.statement_parser import parse_statement_file
    from database_package_deployer.report import generate_report

    logger.info("=" * 64)
    logger.info("  EXPLAIN Validation")
    logger.info("=" * 64)

    # -- Collect files --
    if ordered_files:
        ddl_files = ordered_files
    elif waves:
        ddl_files = [f for wave in waves for f in wave]
    else:
        ddl_files = []
        build_exts = _load_build_extensions(package_dir)
        if build_exts is not None:
            # Extensions stamped at build time — honours custom ships.yaml entries.
            ext_set = frozenset(build_exts)
            logger.debug(
                "explain: using %d extensions from ships.build.json discovery block",
                len(ext_set),
            )
        else:
            # Compile-time fallback for packages built before issue #50.
            ext_set = frozenset(
                {
                    ".tbl",
                    ".viw",
                    ".spl",
                    ".mcr",
                    ".fnc",
                    ".trg",
                    ".jix",
                    ".idx",
                    ".dcl",
                    ".db",
                    ".rol",
                    ".prf",
                    ".map",
                    ".auth",
                    ".fsvr",
                    ".sto",
                    ".sjr",
                    ".usr",
                    ".jcl",
                    ".dml",
                    ".sql",
                    ".bteq",
                    ".btq",
                }
            )
        for root, dirs, filenames in os.walk(package_dir):
            dirs.sort()
            for f in sorted(filenames):
                if f.startswith(".") or f.startswith("_"):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in ext_set:
                    ddl_files.append(os.path.join(root, f))

    logger.info("Files to validate: %d", len(ddl_files))

    # -- Phase 1: Parse all files and build an object index --
    # The index lets us distinguish "genuine missing object" from
    # "object that will be created by this same package".
    parsed_files = []
    package_objects = set()  # Qualified names being created

    for ddl_file in ddl_files:
        basename = os.path.basename(ddl_file)
        try:
            parsed = parse_statement_file(ddl_file)
            parsed_files.append((ddl_file, parsed))

            # Index the object and its components for cross-reference
            if parsed.qualified_name:
                package_objects.add(parsed.qualified_name.upper())
            if parsed.database_name:
                package_objects.add(parsed.database_name.upper())
            if parsed.object_name:
                package_objects.add(parsed.object_name.upper())

        except (ValueError, FileNotFoundError) as e:
            logger.error("  ✗ PARSE FAILED: %s — %s", basename, e)
            parsed_files.append((ddl_file, None))

    logger.info(
        "Package object index: %d names from %d files",
        len(package_objects),
        len(parsed_files),
    )
    logger.debug("  Index: %s", sorted(package_objects)[:20])

    # -- Phase 2: EXPLAIN each file --
    results = []
    passed = 0
    failed = 0
    skipped = 0
    dep_pass = 0  # Dependencies in package (expected failures)

    for ddl_file, parsed in parsed_files:
        basename = os.path.basename(ddl_file)

        # Handle parse failures from Phase 1
        if parsed is None:
            results.append(
                ObjectDeployResult(
                    database_name="UNKNOWN",
                    object_name=basename,
                    object_type=ObjectType.UNKNOWN,
                    state=DeployState.FAILED,
                    ddl_file=basename,
                    error="Parse error",
                    message="Could not parse file.",
                )
            )
            failed += 1
            continue

        # -- Skip technical EXPLAIN-incompatible types --
        if parsed.object_type in _EXPLAIN_SKIP_TYPES:
            logger.info(
                "  ○ NOT APPLICABLE: %s %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                basename,
            )
            results.append(
                ObjectDeployResult(
                    database_name=parsed.database_name,
                    object_name=parsed.object_name,
                    object_type=parsed.object_type,
                    state=DeployState.SKIPPED,
                    ddl_file=basename,
                    deploy_intent=parsed.deploy_intent,
                    message=(
                        f"EXPLAIN not applicable to "
                        f"{parsed.object_type.value} — skipped."
                    ),
                )
            )
            skipped += 1
            continue

        # -- Prereq-exempt types (DATABASE, USER) --
        # Running EXPLAIN on CREATE DATABASE CHILD FROM PARENT requires
        # PARENT to exist on the target. When the package creates both
        # (common in hierarchy deployments), PARENT won't exist at
        # EXPLAIN time — resulting in guaranteed false failures.
        # Preflight already validates rights + parent existence.
        # The topological ordering in _order.txt guarantees correct
        # deploy sequence. EXPLAIN here adds false noise, not safety.
        if parsed.object_type in _PREREQ_EXEMPT_TYPES:
            logger.info(
                "  ○ PREREQ_EXEMPT: %s %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                basename,
            )
            results.append(
                ObjectDeployResult(
                    database_name=parsed.database_name,
                    object_name=parsed.object_name,
                    object_type=parsed.object_type,
                    state=DeployState.SKIPPED,
                    ddl_file=basename,
                    deploy_intent=parsed.deploy_intent,
                    message=(
                        f"PREREQ_EXEMPT: {parsed.object_type.value} creation "
                        f"validated by preflight (rights + parent existence). "
                        f"EXPLAIN would fail for in-package hierarchies where "
                        f"the parent does not yet exist on the target — this "
                        f"is a guaranteed false failure, not a real error."
                    ),
                )
            )
            skipped += 1
            continue

        # -- Run EXPLAIN --
        ddl_text = parsed.ddl_text.strip().rstrip(";").strip()
        explain_sql = f"EXPLAIN {ddl_text}"

        try:
            cursor.execute(explain_sql)
            rows = cursor.fetchall()
            plan_preview = ""
            if rows:
                first_row = str(rows[0][0]) if rows[0] else ""
                plan_preview = first_row[:120]

            logger.info(
                "  ✓ PASS: %s %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                basename,
            )
            if plan_preview:
                logger.debug("    Plan: %s...", plan_preview)

            results.append(
                ObjectDeployResult(
                    database_name=parsed.database_name,
                    object_name=parsed.object_name,
                    object_type=parsed.object_type,
                    state=DeployState.COMPLETED,
                    ddl_file=basename,
                    deploy_intent=parsed.deploy_intent,
                    message="EXPLAIN passed — SQL is valid.",
                )
            )
            passed += 1

        except Exception as e:
            err_msg = str(e)

            # "Already exists" errors — SQL is valid, object just
            # exists from a prior deployment.
            #   5612: user, database, role, or zone already exists
            #   3803: table, view, trigger already exists
            if "5612" in err_msg or "3803" in err_msg:
                logger.info(
                    "  ✓ PASS: %s %s [%s] (already exists — SQL is valid)",
                    parsed.object_type.value,
                    parsed.qualified_name,
                    basename,
                )
                results.append(
                    ObjectDeployResult(
                        database_name=parsed.database_name,
                        object_name=parsed.object_name,
                        object_type=parsed.object_type,
                        state=DeployState.COMPLETED,
                        ddl_file=basename,
                        deploy_intent=parsed.deploy_intent,
                        message="EXPLAIN passed — SQL is valid (object already exists).",
                    )
                )
                passed += 1
                continue

            # "Object does not exist" — check if it's a dependency
            # being created in this same package.
            #   3807: Object 'X' does not exist.
            if "3807" in err_msg:
                if _is_package_dependency(err_msg, package_objects):
                    logger.info(
                        "  ✓ PASS: %s %s [%s] "
                        "(references object created by this package)",
                        parsed.object_type.value,
                        parsed.qualified_name,
                        basename,
                    )
                    results.append(
                        ObjectDeployResult(
                            database_name=parsed.database_name,
                            object_name=parsed.object_name,
                            object_type=parsed.object_type,
                            state=DeployState.COMPLETED,
                            ddl_file=basename,
                            deploy_intent=parsed.deploy_intent,
                            message=(
                                "EXPLAIN passed — references object being "
                                "created by this package."
                            ),
                        )
                    )
                    dep_pass += 1
                    passed += 1
                    continue

            # Genuine failure — not an expected error
            clean_msg = _clean_db_error(err_msg)
            logger.error(
                "  ✗ FAIL: %s %s [%s] — %s",
                parsed.object_type.value,
                parsed.qualified_name,
                basename,
                clean_msg,
            )
            logger.debug("Full error detail: %s", err_msg)
            results.append(
                ObjectDeployResult(
                    database_name=parsed.database_name,
                    object_name=parsed.object_name,
                    object_type=parsed.object_type,
                    state=DeployState.FAILED,
                    ddl_file=basename,
                    deploy_intent=parsed.deploy_intent,
                    error=clean_msg,
                    message=f"EXPLAIN failed: {clean_msg}",
                )
            )
            failed += 1

    # -- Build result --
    logger.info("=" * 64)
    logger.info("  EXPLAIN Results")
    logger.info("  Passed:           %d", passed)
    if dep_pass > 0:
        logger.info("    (of which %d reference objects in this package)", dep_pass)
    logger.info("  Failed:           %d", failed)
    logger.info("  Not applicable:   %d", skipped)
    logger.info("=" * 64)

    pkg_result = PackageDeployResult(
        deployment_id=f"explain_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        manifest_path="",
        total=len(ddl_files),
        completed=passed,
        skipped=skipped,
        failed=failed,
        results=results,
        dry_run=False,
    )

    # -- Generate report --
    try:
        report_path = generate_report(pkg_result, package_dir)
        pkg_result.report_path = report_path
        logger.info("Report: %s", report_path)
    except Exception as e:
        logger.warning("Report generation failed (non-fatal): %s", e)

    return pkg_result


def _is_package_dependency(error_msg: str, package_objects: set) -> bool:
    """
    Check if a 3807 "does not exist" error references an object
    that is being created in this same package.

    Teradata Error 3807 includes the missing object name in the
    error message, e.g.:
        "Object 'A_D01_SHIPS_TEST_STD.Customer' does not exist."

    We extract the referenced name and check it against the
    package's object index.

    Args:
        error_msg:       The full Teradata error message string.
        package_objects:  Set of uppercase qualified names, database
                         names, and object names from this package.

    Returns:
        True if the missing object is in this package.
    """
    import re

    # Extract object name from error message
    # Pattern: Object 'name' or Object "name"
    match = re.search(r"[Oo]bject\s+['\"]([^'\"]+)['\"]", error_msg)
    if not match:
        return False

    missing = match.group(1).upper().strip()

    # Check full qualified name
    if missing in package_objects:
        return True

    # Check just the object part (after the dot)
    if "." in missing:
        parts = missing.split(".")
        if any(p in package_objects for p in parts):
            return True

    return False


# ---------------------------------------------------------------
# Internal — Strategy dispatch
# ---------------------------------------------------------------


def _dispatch_deploy(
    cursor,
    parsed: ParsedStatement,
    manifest: DeploymentManifest,
    dry_run: bool,
    baseline_dir: str = "",
    on_drift: str = "abort",
) -> ObjectDeployResult:
    """
    Dispatch deployment to the correct strategy and update manifest.

    Strategy is now determined by deploy_intent (inferred from DDL verb),
    not just object type. Before any destructive operation (REPLACE,
    DROP), the existing definition is captured via SHOW for rollback.

    When ``baseline_dir`` is set, drift detection runs before deployment:
    the current SHOW output is compared to the stored baseline from the
    last SHIPS deploy.  On drift, behaviour is controlled by ``on_drift``:

        ``abort``    — mark the object FAILED and stop (default)
        ``skip``     — mark the object SKIPPED and continue
        ``continue`` — log a warning and deploy anyway (overwrites change)

    After a successful deploy, a new baseline is written for this object.

    Args:
        cursor:       Active database cursor.
        parsed:       Parsed DDL metadata.
        manifest:     Deployment manifest for state persistence.
        dry_run:      If True, simulate without executing.
        baseline_dir: Path to the shared baseline directory.  Empty
                      string disables drift detection.
        on_drift:     Action on drift: ``abort`` | ``skip`` | ``continue``.

    Returns:
        ObjectDeployResult with outcome.
    """
    try:
        logger.info(
            "Deploying: %s %s [%s → %s] from %s",
            parsed.object_type.value,
            parsed.qualified_name,
            parsed.deploy_intent.value if parsed.deploy_intent else "N/A",
            parsed.strategy.value if parsed.strategy else "N/A",
            os.path.basename(parsed.file_path) if parsed.file_path else "inline",
        )

        # -- Drift detection (pre-deploy) --
        # Only runs when baseline_dir is configured and the object type
        # supports SHOW (i.e. is in SHOW_COMMAND_MAP).  Skipped for
        # DIRECT_EXECUTE objects (DATABASE, GRANT, DML) which have no
        # meaningful SHOW output to compare.
        _drift_result = None
        if (
            baseline_dir
            and not dry_run
            and parsed.strategy != DeployStrategy.DIRECT_EXECUTE
            and parsed.object_type in SHOW_COMMAND_MAP
        ):
            from database_package_deployer.drift import check_drift

            _current_show = _run_show_text(
                cursor,
                parsed.database_name,
                parsed.object_name,
                parsed.object_type,
            )
            if _current_show is not None:
                _drift_result = check_drift(
                    baseline_dir,
                    parsed.database_name,
                    parsed.object_name,
                    _current_show,
                )
                if _drift_result.detected:
                    _drift_msg = (
                        f"Schema drift detected on "
                        f"{parsed.database_name}.{parsed.object_name} — "
                        f"object was changed out-of-band since last SHIPS deploy.\n"
                        f"{_drift_result.diff_text}"
                    )
                    if on_drift == "abort":
                        logger.error("  ⚠ DRIFT ABORT: %s", _drift_msg)
                        _fail_result = ObjectDeployResult(
                            database_name=parsed.database_name,
                            object_name=parsed.object_name,
                            object_type=parsed.object_type,
                            state=DeployState.FAILED,
                            error=_drift_msg,
                            drift_detected=True,
                            drift_diff=_drift_result.diff_text,
                        )
                        _fail_result.deploy_intent = parsed.deploy_intent
                        manifest.update_state(
                            parsed.qualified_name,
                            DeployState.FAILED,
                            error=_drift_msg,
                        )
                        return _fail_result
                    elif on_drift == "skip":
                        logger.warning("  ⚠ DRIFT SKIP: %s", _drift_msg)
                        _skip_result = ObjectDeployResult(
                            database_name=parsed.database_name,
                            object_name=parsed.object_name,
                            object_type=parsed.object_type,
                            state=DeployState.SKIPPED,
                            message=_drift_msg,
                            drift_detected=True,
                            drift_diff=_drift_result.diff_text,
                        )
                        _skip_result.deploy_intent = parsed.deploy_intent
                        manifest.update_state(
                            parsed.qualified_name,
                            DeployState.SKIPPED,
                            error=_drift_msg,
                        )
                        return _skip_result
                    else:  # continue
                        logger.warning("  ⚠ DRIFT CONTINUE: %s", _drift_msg)

        if parsed.strategy == DeployStrategy.IDEMPOTENT_DEPLOY:
            result = _deploy_table(cursor, parsed, dry_run)
        elif parsed.strategy == DeployStrategy.CREATE_ONLY:
            result = _deploy_create_only(cursor, parsed, manifest, dry_run)
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

        # Set deploy_intent and source file on the result.
        # Include the parent directory for context (e.g.
        # "databases/MortgagePlatform_Domain_V.db" rather than
        # just "MortgagePlatform_Domain_V.db").
        result.deploy_intent = parsed.deploy_intent
        if parsed.file_path:
            parent = os.path.basename(os.path.dirname(parsed.file_path))
            fname = os.path.basename(parsed.file_path)
            result.ddl_file = os.path.join(parent, fname) if parent else fname
        else:
            result.ddl_file = None

        if result.state == DeployState.COMPLETED:
            logger.info(
                "  ✓ %s %s — %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                result.message or "completed",
                os.path.basename(parsed.file_path) if parsed.file_path else "",
            )
            # -- Baseline capture (post-deploy) --
            # Write/overwrite the baseline so the next run can detect drift
            # against what SHIPS just deployed (rolling horizon: one file
            # per object, overwritten on each successful deploy).
            if baseline_dir and not dry_run and parsed.object_type in SHOW_COMMAND_MAP:
                from database_package_deployer.drift import write_baseline

                _post_show = _run_show_text(
                    cursor,
                    parsed.database_name,
                    parsed.object_name,
                    parsed.object_type,
                )
                if _post_show is not None:
                    write_baseline(
                        baseline_dir,
                        parsed.database_name,
                        parsed.object_name,
                        _post_show,
                    )

            if _drift_result and _drift_result.detected:
                result.drift_detected = True
                result.drift_diff = _drift_result.diff_text

        elif result.state == DeployState.SKIPPED:
            logger.info(
                "  ○ %s %s — %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                result.message or "skipped",
                os.path.basename(parsed.file_path) if parsed.file_path else "",
            )
        elif result.state == DeployState.FAILED:
            logger.error(
                "  ✗ %s %s — %s [%s]",
                parsed.object_type.value,
                parsed.qualified_name,
                result.error or result.message or "failed",
                os.path.basename(parsed.file_path) if parsed.file_path else "",
            )

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
        clean_err = _clean_db_error(str(e))
        # Full traceback to the log file for diagnosis
        logger.debug(
            "Deployment failed for %s — full traceback:",
            parsed.qualified_name,
            exc_info=True,
        )
        # Resolve source file for both console and result
        if parsed.file_path:
            parent = os.path.basename(os.path.dirname(parsed.file_path))
            fname = os.path.basename(parsed.file_path)
            source_file = os.path.join(parent, fname) if parent else fname
        else:
            source_file = None

        # Clean one-liner to the console
        logger.error(
            "  ✗ FAILED: %s (%s) — %s  [%s]",
            parsed.qualified_name,
            parsed.object_type.value,
            clean_err,
            source_file or "unknown",
        )
        manifest.update_state(
            parsed.qualified_name, DeployState.FAILED, error=clean_err
        )
        result = ObjectDeployResult(
            database_name=parsed.database_name,
            object_name=parsed.object_name,
            object_type=parsed.object_type,
            state=DeployState.FAILED,
            deploy_intent=parsed.deploy_intent,
            error=clean_err,
            message=f"Deployment failed: {clean_err}",
        )
        result.ddl_file = source_file
        return result


# ---------------------------------------------------------------
# Strategy: IDEMPOTENT_DEPLOY (tables)
# ---------------------------------------------------------------


def _deploy_table(
    cursor,
    parsed: ParsedStatement,
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
    exists = _object_exists(cursor, db, tbl, "T")

    if not exists:
        if dry_run:
            return ObjectDeployResult(
                database_name=db,
                object_name=tbl,
                object_type=ObjectType.TABLE,
                state=DeployState.COMPLETED,
                message=f"[DRY RUN] Would CREATE {qn} (does not exist).",
                dry_run=True,
            )
        _execute_ddl(cursor, parsed.ddl_text)
        return ObjectDeployResult(
            database_name=db,
            object_name=tbl,
            object_type=ObjectType.TABLE,
            state=DeployState.COMPLETED,
            message=f"Created {qn} (did not previously exist).",
        )

    # -- Check for data --
    has_data = _table_has_data(cursor, db, tbl)

    if not has_data:
        if dry_run:
            return ObjectDeployResult(
                database_name=db,
                object_name=tbl,
                object_type=ObjectType.TABLE,
                state=DeployState.COMPLETED,
                message=f"[DRY RUN] Would DROP and recreate {qn} (exists, empty).",
                dry_run=True,
            )
        _drop_object(cursor, db, tbl, ObjectType.TABLE)
        _execute_ddl(cursor, parsed.ddl_text)
        return ObjectDeployResult(
            database_name=db,
            object_name=tbl,
            object_type=ObjectType.TABLE,
            state=DeployState.COMPLETED,
            message=f"Replaced empty table {qn}.",
        )

    # -- Has data: get schema, backup, create, compare, migrate --
    old_columns = get_column_metadata(cursor, db, tbl)
    backup_name = _generate_backup_name(tbl)

    if dry_run:
        # Simulate by comparing DDL columns — we can't create the
        # new table to query its schema, so report what we know.
        return ObjectDeployResult(
            database_name=db,
            object_name=tbl,
            object_type=ObjectType.TABLE,
            state=DeployState.COMPLETED,
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
    except Exception:
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
            database_name=db,
            object_name=tbl,
            object_type=ObjectType.TABLE,
            state=DeployState.SKIPPED,
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
        logger.debug("Migration error detail: %s", e)
        return ObjectDeployResult(
            database_name=db,
            object_name=tbl,
            object_type=ObjectType.TABLE,
            state=DeployState.FAILED,
            backup_table=backup_name,
            error=_clean_db_error(str(e)),
            message=f"Migration failed for {qn}. Backup preserved.",
            warnings=compatibility.warnings,
        )

    row_count = _count_rows(cursor, db, tbl)

    return ObjectDeployResult(
        database_name=db,
        object_name=tbl,
        object_type=ObjectType.TABLE,
        state=DeployState.COMPLETED,
        backup_table=backup_name,
        rows_migrated=row_count,
        message=(f"Deployed {qn} — migrated {row_count:,} rows from {backup_name}."),
        warnings=compatibility.warnings,
    )


# ---------------------------------------------------------------
# Strategy: DIRECT_EXECUTE (databases, users, profiles, roles, DCL)
# ---------------------------------------------------------------


def _deploy_direct_execute(
    cursor,
    parsed: ParsedStatement,
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Execute DDL as-is with no pre-checks, backup, or rollback.

    Used for pre-requisite objects (CREATE DATABASE, CREATE USER)
    and DCL (GRANT, REVOKE). These are infrastructure DDL that
    precedes the main object deployment.

    For DATABASE and USER types, gracefully handles Teradata
    Error 5612 ("already exists") by treating it as SKIPPED
    rather than FAILED — this makes re-deployments idempotent
    without changing the developer's DDL verb.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    if dry_run:
        logger.info(
            "[DRY RUN] DIRECT_EXECUTE: %s %s",
            obj_type.value,
            qn,
        )
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.COMPLETED,
            message=f"[DRY RUN] Would execute {obj_type.value}: {qn}",
            dry_run=True,
        )

    logger.info(
        "DIRECT_EXECUTE: Executing %s %s...",
        obj_type.value,
        qn,
    )

    try:
        _execute_ddl(cursor, parsed.ddl_text)
    except Exception as e:
        err_str = str(e)
        # Teradata Error 5612: "already exists" — for DATABASE
        # and USER, treat as a successful skip on re-deploy.
        if "5612" in err_str and obj_type in (
            ObjectType.DATABASE,
            ObjectType.USER,
        ):
            logger.info(
                "DIRECT_EXECUTE: %s %s already exists — skipping.",
                obj_type.value,
                qn,
            )
            return ObjectDeployResult(
                database_name=db,
                object_name=obj,
                object_type=obj_type,
                state=DeployState.SKIPPED,
                prior_existed=True,
                message=f"{obj_type.value} {qn} already exists — skipped.",
            )
        # Any other error — propagate
        raise

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,
        message=f"Executed {obj_type.value}: {qn}",
    )


# ---------------------------------------------------------------
# Strategy: SKIP_IF_EXISTS (system-scope: maps, roles, profiles,
#           authorisations, foreign servers)
# ---------------------------------------------------------------


def _deploy_skip_if_exists(
    cursor,
    parsed: ParsedStatement,
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

    if dry_run:
        logger.info(
            "[DRY RUN] SKIP_IF_EXISTS: %s %s — "
            "would check existence then create if missing.",
            obj_type.value,
            obj,
        )
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.COMPLETED,
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
                "Existence check failed for %s %s: %s — proceeding with CREATE.",
                obj_type.value,
                obj,
                e,
            )

    if exists:
        logger.info(
            "SKIP_IF_EXISTS: %s %s already exists — skipping.",
            obj_type.value,
            obj,
        )
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.SKIPPED,
            prior_existed=True,
            message=f"{obj_type.value} {obj} already exists — skipped.",
        )

    # -- Object does not exist — create it --
    _execute_ddl(cursor, parsed.ddl_text)

    logger.info(
        "SKIP_IF_EXISTS: Created %s %s.",
        obj_type.value,
        obj,
    )

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,
        prior_existed=False,
        message=f"Created {obj_type.value}: {obj}",
    )


# ---------------------------------------------------------------
# Strategy: CREATE_ONLY (deployer owns idempotency)
# ---------------------------------------------------------------


def _deploy_create_only(
    cursor,
    parsed: ParsedStatement,
    manifest: Optional[DeploymentManifest],
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy an object that uses CREATE (not REPLACE).

    The deployer owns idempotency — the developer's DDL verb is
    always CREATE. When the object already exists, the deployer:

        1. Captures the existing definition via SHOW (rollback file).
        2. DROPs the existing object.
        3. CREATEs the new definition.
        4. On failure — the rollback file is available for
           package-level rollback to restore the prior state.

    When the object does not exist, a straightforward CREATE is
    executed.

    Args:
        cursor:    Active database cursor.
        parsed:    Parsed DDL metadata.
        manifest:  Deployment manifest for rollback file paths.
                   May be None when called from deploy_single().
        dry_run:   If True, simulate without executing.

    Returns:
        ObjectDeployResult with outcome.
    """
    db = parsed.database_name
    obj = parsed.object_name
    obj_type = parsed.object_type
    qn = parsed.qualified_name

    # -- Check existence --
    table_kind = TABLE_KIND_MAP.get(obj_type)
    exists = _object_exists(cursor, db, obj, table_kind) if table_kind else False

    if dry_run:
        action = "DROP and CREATE (backup existing)" if exists else "CREATE"
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.COMPLETED,
            prior_existed=exists,
            message=f"[DRY RUN] Would {action} {obj_type.value} {qn}.",
            dry_run=True,
        )

    # -- Capture existing definition before DROP --
    rollback_file = None
    snapshot_hash = None
    if exists:
        package_dir = (
            os.path.dirname(manifest.path)
            if manifest and hasattr(manifest, "path")
            else None
        )
        if package_dir:
            rollback_file, snapshot_hash = _capture_existing_definition(
                cursor, db, obj, obj_type, package_dir
            )
        _drop_object(cursor, db, obj, obj_type, parsed.ddl_text)
        logger.info(
            "Dropped existing %s %s (rollback saved to %s)",
            obj_type.value,
            qn,
            rollback_file or "N/A",
        )

    # -- Create --
    _execute_ddl(cursor, parsed.ddl_text)

    msg = f"{'Replaced' if exists else 'Created'} {obj_type.value} {qn}."
    if rollback_file:
        msg += f" Rollback saved: {os.path.basename(rollback_file)}"

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,
        prior_existed=exists,
        rollback_file=rollback_file,
        snapshot_hash=snapshot_hash,
        message=msg,
    )


# ---------------------------------------------------------------
# Strategy: DROP_AND_CREATE (join/hash indexes, sec. indexes, triggers)
# ---------------------------------------------------------------


def _deploy_drop_and_create(
    cursor,
    parsed: ParsedStatement,
    manifest: Optional[DeploymentManifest],
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy an object by dropping (if it exists) then creating.

    Before dropping, the existing definition is captured via SHOW
    and saved to the _rollback/ directory for rollback support.

    Args:
        cursor:    Active database cursor.
        parsed:    Parsed DDL metadata.
        manifest:  Deployment manifest for rollback file paths.
                   May be None when called from deploy_single().
        dry_run:   If True, simulate without executing.

    Returns:
        ObjectDeployResult with outcome.
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
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.COMPLETED,
            prior_existed=exists,
            message=f"[DRY RUN] Would {action} {obj_type.value} {qn}.",
            dry_run=True,
        )

    # -- Capture existing definition before DROP --
    rollback_file = None
    snapshot_hash = None
    if exists:
        package_dir = (
            os.path.dirname(manifest.path)
            if manifest and hasattr(manifest, "path")
            else None
        )
        if package_dir:
            rollback_file, snapshot_hash = _capture_existing_definition(
                cursor, db, obj, obj_type, package_dir
            )
        _drop_object(cursor, db, obj, obj_type, parsed.ddl_text)
        logger.info(
            "Dropped existing %s %s (saved to %s)",
            obj_type.value,
            qn,
            rollback_file or "N/A",
        )

    # -- Create --
    _execute_ddl(cursor, parsed.ddl_text)

    msg = f"{'Replaced' if exists else 'Created'} {obj_type.value} {qn}."
    if rollback_file:
        msg += f" Rollback saved: {os.path.basename(rollback_file)}"

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,
        prior_existed=exists,
        rollback_file=rollback_file,
        snapshot_hash=snapshot_hash,
        message=msg,
    )


# ---------------------------------------------------------------
# Strategy: REPLACE_IN_PLACE (views, macros, procedures, functions)
# ---------------------------------------------------------------


def _deploy_replace_in_place(
    cursor,
    parsed: ParsedStatement,
    manifest: Optional[DeploymentManifest],
    dry_run: bool,
) -> ObjectDeployResult:
    """
    Deploy a replaceable object by executing the DDL directly.

    Before replacing, if the object already exists, its current
    definition is captured via SHOW and saved to the _rollback/
    directory. The REPLACE keyword then handles the actual deployment.

    Args:
        cursor:    Active database cursor.
        parsed:    Parsed DDL metadata.
        manifest:  Deployment manifest for rollback file paths.
                   May be None when called from deploy_single().
        dry_run:   If True, simulate without executing.

    Returns:
        ObjectDeployResult with outcome.
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
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.COMPLETED,
            prior_existed=exists,
            message=f"[DRY RUN] Would {action} {obj_type.value} {qn}.",
            dry_run=True,
        )

    # -- Capture existing definition before REPLACE --
    rollback_file = None
    snapshot_hash = None
    if exists:
        package_dir = (
            os.path.dirname(manifest.path)
            if manifest and hasattr(manifest, "path")
            else None
        )
        if package_dir:
            rollback_file, snapshot_hash = _capture_existing_definition(
                cursor, db, obj, obj_type, package_dir
            )

    # -- Execute REPLACE --
    _execute_ddl(cursor, parsed.ddl_text)

    msg = f"{'Replaced' if exists else 'Created'} {obj_type.value} {qn}."
    if rollback_file:
        msg += f" Rollback saved: {os.path.basename(rollback_file)}"

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,
        prior_existed=exists,
        rollback_file=rollback_file,
        snapshot_hash=snapshot_hash,
        message=msg,
    )


# ---------------------------------------------------------------
# Internal — Rollback
# ---------------------------------------------------------------


def _rollback_single_dry_run(
    db: str,
    obj: str,
    obj_type: "ObjectType",
    qualified_name: str,
    backup_name: Optional[str],
    rollback_file: Optional[str],
) -> "ObjectDeployResult":
    """
    Preview what ``_rollback_single`` *would* do without executing DDL.

    Reads exclusively from the manifest record and checks disk for the
    rollback file — no database connection required. Called by
    ``_rollback_single`` when ``dry_run=True``.
    """
    if obj_type == ObjectType.JAR:
        msg = (
            f"[DRY RUN] CANNOT roll back {qualified_name} — "
            f"JAR binaries are not extractable from Teradata. "
            f"Use 'ships rollback --to-tag <prev-tag>' to restore the "
            f"previous JAR version, or reinstall manually from the previous package."
        )
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.SKIPPED,
            message=msg,
            dry_run=True,
        )

    if obj_type == ObjectType.TABLE:
        if backup_name:
            msg = (
                f"[DRY RUN] Would restore {qualified_name} — "
                f"drop current table and rename backup {backup_name} back to original."
            )
        else:
            msg = (
                f"[DRY RUN] Would drop {qualified_name} — "
                f"newly created table; no backup to restore."
            )
    elif rollback_file:
        if os.path.exists(rollback_file):
            base_msg = (
                f"[DRY RUN] Would restore {qualified_name} — "
                f"drop current {obj_type.value} and re-execute "
                f"{os.path.basename(rollback_file)}."
            )
            # Warn if the rollback DDL references a C external binary
            try:
                with open(rollback_file, encoding="utf-8") as _f:
                    _rb_text = _f.read()
                if _is_c_external(_rb_text):
                    base_msg += (
                        " ⚠ C external routine — DDL will be restored but the "
                        "compiled binary may not match. Consider 'ships rollback "
                        "--to-tag <prev-tag>' for a complete binary rollback."
                    )
            except OSError:
                pass
            msg = base_msg
        else:
            msg = (
                f"[DRY RUN] CANNOT restore {qualified_name} — "
                f"rollback file recorded in manifest but missing from disk: "
                f"{rollback_file}. Manual intervention required."
            )
    else:
        msg = (
            f"[DRY RUN] Would drop {qualified_name} — "
            f"newly created {obj_type.value}; no prior definition to restore."
        )

    return ObjectDeployResult(
        database_name=db,
        object_name=obj,
        object_type=obj_type,
        state=DeployState.COMPLETED,  # represents "would succeed"
        message=msg,
        dry_run=True,
        rollback_file=rollback_file,
    )


def _rollback_single(
    cursor,
    qualified_name: str,
    parsed: Optional[ParsedStatement],
    manifest: DeploymentManifest,
    dry_run: bool = False,
) -> ObjectDeployResult:
    """
    Roll back a single object deployment.

    For tables: drop new, rename backup to original.

    For non-table objects with a rollback file (applies to
    DROP_AND_CREATE *and* REPLACE_IN_PLACE when the object existed
    before deployment — ``_deploy_replace_in_place`` captures a SHOW
    snapshot before executing REPLACE): drop the current object, then
    re-create from the saved rollback DDL to restore the prior
    definition.

    For newly created objects (no prior state): drop the object.

    When ``dry_run=True``, no DDL is executed and the manifest is not
    mutated. The returned ObjectDeployResult describes the *planned*
    action using the manifest's recorded backup_table and rollback_file
    fields, plus a disk-existence check for the rollback file.
    """
    parts = qualified_name.split(".", 1)
    db, obj = parts[0], parts[1]
    record = manifest.get_record(qualified_name)
    backup_name = record.get("backup_table") if record else None
    rollback_file = record.get("rollback_file") if record else None
    obj_type = ObjectType.TABLE  # Default assumption

    if parsed:
        obj_type = parsed.object_type
    elif record and record.get("object_type"):
        # Fall back to manifest-recorded type when DDL file could not be parsed.
        # This matters for dry-run, where the file may be on a different machine.
        try:
            obj_type = ObjectType(record["object_type"])
        except ValueError:
            pass  # keep TABLE default

    # -- Dry-run path: describe planned action without executing anything --
    if dry_run:
        return _rollback_single_dry_run(
            db,
            obj,
            obj_type,
            qualified_name,
            backup_name,
            rollback_file,
        )

    try:
        # -- Binary objects: cannot roll back via SHOW capture --
        # JAR binaries are stored in Teradata but not SQL-queryable.
        # Attempting a rollback would either do nothing (no DROP entry
        # for JAR in _drop_object) or make things worse by removing the
        # JAR entirely. Skip with an actionable message; the correct path
        # is feature rollback via 'ships rollback --to-tag'.
        if obj_type == ObjectType.JAR:
            _jar_msg = (
                f"Skipped rollback of {qualified_name} — JAR binaries are not "
                f"extractable from Teradata and cannot be automatically restored. "
                f"Use 'ships rollback --to-tag <prev-tag>' to rebuild and redeploy "
                f"the previous JAR version, or reinstall manually from the previous "
                f"package archive."
            )
            logger.warning("  ⚠ %s", _jar_msg)
            manifest.update_state(qualified_name, DeployState.SKIPPED, error=_jar_msg)
            return ObjectDeployResult(
                database_name=db,
                object_name=obj,
                object_type=obj_type,
                state=DeployState.SKIPPED,
                message=_jar_msg,
            )

        # Tables use the RENAME-based rollback path
        if obj_type == ObjectType.TABLE:
            return _rollback_table(
                cursor, db, obj, backup_name, qualified_name, manifest
            )

        # Non-table objects: check for a rollback file first
        if rollback_file and os.path.exists(rollback_file):
            # Drop the newly created object
            table_kind = TABLE_KIND_MAP.get(obj_type)
            if _object_exists(cursor, db, obj, table_kind):
                _drop_object(
                    cursor,
                    db,
                    obj,
                    obj_type,
                    parsed.ddl_text if parsed else None,
                )

            # GAP-013: verify snapshot hash before restoring.
            with open(rollback_file, "r", encoding="utf-8") as f:
                rollback_ddl = f.read()

            recorded_hash = record.get("snapshot_hash") if record else None
            if recorded_hash:
                import hashlib as _hl2

                actual_hash = _hl2.sha256(rollback_ddl.encode("utf-8")).hexdigest()
                if actual_hash != recorded_hash:
                    _integrity_msg = (
                        f"rollback integrity failure for '{qualified_name}' — "
                        f"snapshot hash mismatch (expected {recorded_hash[:12]}…, "
                        f"got {actual_hash[:12]}…). Skipping restore for this object."
                    )
                    logger.error("  ✖ %s", _integrity_msg)
                    manifest.update_state(
                        qualified_name, DeployState.FAILED, error=_integrity_msg
                    )
                    return ObjectDeployResult(
                        database_name=db,
                        object_name=obj,
                        object_type=obj_type,
                        state=DeployState.FAILED,
                        error=_integrity_msg,
                        message=_integrity_msg,
                    )
            elif recorded_hash is None:
                logger.warning(
                    "package_age: snapshot_hash absent for '%s' — proceeding "
                    "without integrity check (legacy manifest).",
                    qualified_name,
                )

            # Re-create from the saved rollback definition
            _execute_ddl(cursor, rollback_ddl)

            manifest.update_state(qualified_name, DeployState.ROLLED_BACK)

            # -- C external routines: DDL restored but binary may not match --
            # SHOW PROCEDURE/FUNCTION captures the CREATE statement including
            # LANGUAGE C EXTERNAL NAME '...'. Re-executing it restores the DDL
            # but Teradata stores one compiled binary per routine — if the new
            # deployment replaced the binary, the restored DDL now references
            # the wrong version. This is DDL-only; the binary must be sourced
            # separately or via feature rollback.
            _c_warning = None
            if _is_c_external(rollback_ddl):
                _c_warning = (
                    f"DDL restored for {qualified_name} but this is a C external "
                    f"routine — the compiled binary may not match the restored "
                    f"definition. Verify the binary or use 'ships rollback --to-tag "
                    f"<prev-tag>' to restore the correct version."
                )
                logger.warning("  ⚠ %s", _c_warning)

            return ObjectDeployResult(
                database_name=db,
                object_name=obj,
                object_type=obj_type,
                state=DeployState.ROLLED_BACK,
                message=(
                    f"Rolled back {qualified_name} — restored "
                    f"from {os.path.basename(rollback_file)}."
                    + (f" ⚠ {_c_warning}" if _c_warning else "")
                ),
                warnings=[_c_warning] if _c_warning else [],
            )

        # No rollback file — can only drop the new object
        if _object_exists(cursor, db, obj, TABLE_KIND_MAP.get(obj_type)):
            _drop_object(
                cursor,
                db,
                obj,
                obj_type,
                parsed.ddl_text if parsed else None,
            )
            message = (
                f"Rolled back {qualified_name} — dropped "
                f"{obj_type.value}. No prior definition to restore."
            )
        else:
            message = f"No action for {qualified_name} — object does not exist."

        manifest.update_state(qualified_name, DeployState.ROLLED_BACK)
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.ROLLED_BACK,
            message=message,
        )

    except Exception as e:
        clean_err = _clean_db_error(str(e))
        # Full traceback to the log file for diagnosis
        logger.debug(
            "Rollback failed for %s — full traceback:",
            qualified_name,
            exc_info=True,
        )
        # Clean one-liner to the console
        logger.error(
            "  ✗ ROLLBACK FAILED: %s — %s",
            qualified_name,
            clean_err,
        )
        manifest.update_state(
            qualified_name, DeployState.FAILED, error=f"Rollback failed: {clean_err}"
        )
        return ObjectDeployResult(
            database_name=db,
            object_name=obj,
            object_type=obj_type,
            state=DeployState.FAILED,
            error=clean_err,
            message=f"Rollback failed for {qualified_name}: {clean_err}",
        )


def _rollback_table(cursor, db, tbl, backup_name, qualified_name, manifest):
    """Roll back a table: drop new, rename backup to original."""
    original_exists = _object_exists(cursor, db, tbl, "T")
    backup_exists = (
        _object_exists(cursor, db, backup_name, "T") if backup_name else False
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
        database_name=db,
        object_name=tbl,
        object_type=ObjectType.TABLE,
        state=DeployState.ROLLED_BACK,
        message=message,
    )


def _reconcile_table_state(cursor, qualified_name, record, manifest):
    """Inspect database state and correct manifest for a FAILED table."""
    parts = qualified_name.split(".", 1)
    db, tbl = parts[0], parts[1]
    backup_name = record.get("backup_table")

    original_exists = _object_exists(cursor, db, tbl, "T")
    backup_exists = (
        _object_exists(cursor, db, backup_name, "T") if backup_name else False
    )

    if original_exists and backup_exists:
        manifest.update_state(
            qualified_name, DeployState.CREATED, backup_table=backup_name
        )
    elif not original_exists and backup_exists:
        manifest.update_state(
            qualified_name, DeployState.BACKED_UP, backup_table=backup_name
        )
    elif original_exists and not backup_exists:
        manifest.update_state(qualified_name, DeployState.PENDING)
    else:
        manifest.update_state(
            qualified_name,
            DeployState.FAILED,
            error="Neither original nor backup exists.",
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
) -> tuple:
    """Capture an existing object's DDL via SHOW before replacement (GAP-013).

    Runs the appropriate SHOW command (SHOW VIEW, SHOW MACRO, etc.)
    and saves the output to a _rollback/ directory alongside the
    manifest.  Computes a SHA-256 digest of the saved content for
    rollback integrity verification.

    Args:
        cursor:         Active database cursor.
        database_name:  Database containing the object.
        object_name:    Object name.
        object_type:    ObjectType enum.
        package_dir:    Directory for the _rollback/ output.

    Returns:
        Tuple of (rollback_file_path_or_None, snapshot_hash_or_None).
    """
    show_cmd = SHOW_COMMAND_MAP.get(object_type)
    if not show_cmd:
        logger.warning(
            "No SHOW command mapped for %s — cannot capture rollback",
            object_type.value,
        )
        return (None, None)

    qualified = f"{database_name}.{object_name}"

    try:
        cursor.execute(f"{show_cmd} {qualified}")
        rows = cursor.fetchall()

        if not rows:
            logger.warning("SHOW %s returned no rows", qualified)
            return (None, None)

        # SHOW commands return the DDL as one or more rows of text
        ddl_lines = []
        for row in rows:
            if row and row[0]:
                ddl_lines.append(str(row[0]))

        ddl_text = "\n".join(ddl_lines)

        if not ddl_text.strip():
            return (None, None)

        # Save to _rollback/ directory
        rollback_dir = os.path.join(package_dir, "_rollback")
        os.makedirs(rollback_dir, exist_ok=True)

        # Use the appropriate extension
        from database_package_deployer.models import ObjectType as OT

        ext_map = {
            OT.VIEW: ".viw",
            OT.MACRO: ".mcr",
            OT.PROCEDURE: ".spl",
            OT.FUNCTION: ".fnc",
            OT.TRIGGER: ".trg",
            OT.JOIN_INDEX: ".jix",
            OT.HASH_INDEX: ".idx",
            OT.INDEX: ".idx",
            OT.TABLE: ".tbl",
        }
        ext = ext_map.get(object_type, ".sql")
        filename = f"{database_name}.{object_name}{ext}"
        rollback_path = os.path.join(rollback_dir, filename)

        with open(rollback_path, "w", encoding="utf-8") as f:
            f.write(ddl_text)

        # GAP-013: compute snapshot hash for rollback integrity verification.
        import hashlib as _hl

        snap_hash = _hl.sha256(ddl_text.encode("utf-8")).hexdigest()

        logger.info(
            "Captured rollback: %s → %s (hash: %s…)",
            qualified,
            rollback_path,
            snap_hash[:12],
        )
        return (rollback_path, snap_hash)

    except Exception as e:
        logger.warning(
            "Failed to capture rollback for %s (non-fatal): %s",
            qualified,
            e,
        )
        return (None, None)


# ---------------------------------------------------------------
# Internal — Database operations
# ---------------------------------------------------------------


def _build_redeploy_checker(manifest):
    """
    Build an existence-checking closure for prepare_for_redeploy().

    Returns a function(cursor, qualified_name) → bool that
    inspects the manifest record's object_type to determine
    the correct existence query:

      - GRANT:  Always returns False (idempotent — safe to re-apply).
      - DATABASE, ROLE, PROFILE, USER:  Uses SYSTEM_EXISTENCE_QUERIES.
      - TABLE, VIEW, MACRO, PROCEDURE, FUNCTION, TRIGGER,
        JOIN_INDEX, HASH_INDEX:  Uses DBC.TablesV via TABLE_KIND_MAP.
      - INDEX:  Uses DBC.IndicesV via _index_exists().
      - Unknown:  Returns True (safe default — do not reset).

    Args:
        manifest: The DeploymentManifest to look up object_type
                  for each qualified_name.

    Returns:
        Callable[[cursor, str], bool] suitable for
        manifest.prepare_for_redeploy().
    """

    def checker(cursor, qualified_name):
        record = manifest.get_record(qualified_name)
        if record is None:
            return False

        obj_type_str = record.get("object_type")
        if obj_type_str is None:
            # No type recorded — cannot verify, safe default
            return True

        try:
            obj_type = ObjectType(obj_type_str)
        except ValueError:
            return True  # Unknown type — safe default

        # Grants are idempotent (DIRECT_EXECUTE handles Error 5612
        # for databases/users; grants have no duplicate error).
        # Always re-apply.
        if obj_type == ObjectType.GRANT:
            return False

        # System-scope objects: ROLE, DATABASE, USER, PROFILE, etc.
        existence_query = SYSTEM_EXISTENCE_QUERIES.get(obj_type)
        if existence_query:
            # System objects use unqualified names
            obj_name = (
                qualified_name.split(".", 1)[-1]
                if "." in qualified_name
                else qualified_name
            )
            try:
                cursor.execute(existence_query.format(name=obj_name))
                return cursor.fetchone() is not None
            except Exception:
                return True  # Check failed — safe default

        # Secondary indexes: DBC.IndicesV (no TableKind)
        if obj_type == ObjectType.INDEX and "." in qualified_name:
            db_name, obj_name = qualified_name.split(".", 1)
            return _index_exists(cursor, db_name, obj_name)

        # Database-qualified objects: DBC.TablesV
        if "." in qualified_name:
            db_name, obj_name = qualified_name.split(".", 1)
            table_kind = TABLE_KIND_MAP.get(obj_type)
            if table_kind:
                return _object_exists(cursor, db_name, obj_name, table_kind)

        # Fallback — cannot determine, assume exists
        return True

    return checker


def _object_exists(
    cursor, database_name: str, object_name: str, table_kind: str
) -> bool:
    """Check if an object exists in DBC.TablesV by TableKind."""
    if cursor is None:
        return False  # Dry-run without connection — assume not exists
    try:
        cursor.execute(
            "SELECT 1 FROM DBC.TablesV "
            "WHERE DatabaseName = ? AND TableName = ? AND TableKind = ?",
            [database_name, object_name, table_kind],
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
            "SELECT 1 FROM DBC.IndicesV WHERE DatabaseName = ? AND IndexName = ?",
            [database_name, index_name],
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _table_has_data(cursor, database_name: str, table_name: str) -> bool:
    """Check if a table contains any rows (TOP 1 for efficiency)."""
    cursor.execute(f'SELECT TOP 1 1 FROM "{database_name}"."{table_name}"')
    return cursor.fetchone() is not None


def _count_rows(cursor, database_name: str, table_name: str) -> int:
    """Count total rows in a table."""
    cursor.execute(
        f'SELECT CAST(COUNT(*) AS BIGINT) FROM "{database_name}"."{table_name}"'
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def _execute_ddl(cursor, ddl_text: str):
    """
    Execute one or more DDL statements.

    Handles multi-statement content (e.g. .grt files with multiple
    GRANT statements) by splitting on semicolons -- comment- and
    string-literal-safe, so semicolons inside SQL comments or VALUES
    strings don't cause false splits.

    Each statement is executed individually. This avoids Teradata
    Error 3932 ('Only an ET or null statement is legal after a DDL
    Statement') which occurs when multiple DDL statements are sent
    in a single execute() call in ANSI session mode.

    Transient lock errors are retried up to 3 times with
    exponential backoff:

      - Error 3598: "Concurrent change conflict on database --
        try again." Backoff: 0.5s, 1s, 2s.

      - Error 2631: "Transaction ABORTed due to deadlock."
        Backoff: 2s, 4s, 8s.
    """
    import re
    import time

    # --- Split multi-statement content (comment + string-literal safe) ---
    # Build a sanitised copy for semicolon detection, preserving character
    # positions so offsets map back to the original text exactly.
    stripped = ddl_text

    # Replace block comments with same-length whitespace
    for match in re.finditer(r"/\*.*?\*/", stripped, flags=re.DOTALL):
        stripped = (
            stripped[: match.start()]
            + " " * len(match.group())
            + stripped[match.end() :]
        )

    # Replace single-line comments with same-length whitespace
    for match in re.finditer(r"--[^\n]*", stripped):
        stripped = (
            stripped[: match.start()]
            + " " * len(match.group())
            + stripped[match.end() :]
        )

    # Replace string literals with same-length whitespace so that a
    # semicolon inside a VALUES string (e.g. 'Fixed rate; stable payments')
    # is not mistaken for a statement terminator.
    # Pattern mirrors sql_text._STRING_LITERAL_RE (Teradata single-quoted,
    # doubled-quote escape). Comments are already blanked above, so any
    # stray quote inside a comment won't start a spurious literal match.
    for match in re.finditer(r"'(?:[^']|'')*'", stripped, flags=re.DOTALL):
        stripped = (
            stripped[: match.start()]
            + " " * len(match.group())
            + stripped[match.end() :]
        )

    # Find semicolon positions in the sanitised version
    semi_positions = [i for i, c in enumerate(stripped) if c == ";"]

    # Extract individual statements from original text
    statements = []
    start = 0
    for pos in semi_positions:
        chunk = ddl_text[start:pos].strip()
        start = pos + 1
        if chunk:
            statements.append(chunk)

    # Trailing content after last semicolon
    trailing = ddl_text[start:].strip()
    if trailing:
        statements.append(trailing)

    # Fallback: if no semicolons found, use the whole text
    if not statements:
        statements = [ddl_text.strip()]

    # Filter out comment-only or whitespace-only chunks
    clean_statements = []
    for s in statements:
        # Strip comments and check if anything remains
        check = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
        check = re.sub(r"--[^\n]*", "", check).strip()
        if check:
            clean_statements.append(s)
    statements = clean_statements

    if not statements:
        logger.debug("No executable statements found in DDL text.")
        return

    # --- Execute each statement individually ---
    # Retryable Teradata errors -- code -> (label, base_delay_secs)
    _RETRYABLE = {
        "3598": ("concurrent change conflict", 0.5),
        "2631": ("deadlock", 2.0),
    }

    for stmt in statements:
        clean = stmt.strip().rstrip(";").strip()
        if not clean:
            continue

        # Log a preview (not the full DDL, which can be very long)
        preview = clean[:200] + ("..." if len(clean) > 200 else "")
        logger.debug("Executing SQL: %s", preview)

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                cursor.execute(clean)
                if attempt > 0:
                    logger.info(
                        "SQL succeeded on retry %d.",
                        attempt,
                    )
                break
            except Exception as e:
                err_str = str(e)

                # Check for retryable errors
                if attempt < max_retries:
                    for code, (label, base_delay) in _RETRYABLE.items():
                        if code in err_str:
                            delay = base_delay * (2**attempt)
                            logger.warning(
                                "Error %s (%s) -- retry %d/%d in %.1fs.",
                                code,
                                label,
                                attempt + 1,
                                max_retries,
                                delay,
                            )
                            time.sleep(delay)
                            break
                    else:
                        # No retryable error matched
                        pass

                    # If we matched a retryable error, continue the loop
                    if any(code in err_str for code in _RETRYABLE):
                        continue

                # Non-retryable error, or final retry exhausted
                clean_err = _clean_db_error(err_str)
                logger.error(
                    "SQL execution failed.\n  Error:  %s\n  SQL:    %s",
                    clean_err,
                    clean,
                )
                # Full Go stack trace at DEBUG level only
                logger.debug("Full driver error: %s", e)
                raise


def _rename_table(cursor, database_name: str, old_name: str, new_name: str):
    """Rename a table within the same database."""
    cursor.execute(
        f'RENAME TABLE "{database_name}"."{old_name}" TO "{database_name}"."{new_name}"'
    )


_C_EXTERNAL_RE = __import__("re").compile(
    r"\bLANGUAGE\s+C\b", __import__("re").IGNORECASE
)


def _is_c_external(ddl_text: str) -> bool:
    """Return True when the DDL defines a C/C++ external routine.

    Detects ``LANGUAGE C`` (which covers both C and C++) so the rollback
    path can warn that the compiled binary may not match after DDL-only
    restoration.

    Args:
        ddl_text: DDL text from the rollback capture file.

    Returns:
        True if the routine is C/C++ external, False otherwise.
    """
    return bool(_C_EXTERNAL_RE.search(ddl_text))


def _drop_object(
    cursor,
    database_name: str,
    object_name: str,
    object_type: ObjectType,
    ddl_text: str = None,
):
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
            drop_sql = f'DROP INDEX "{object_name}" ON "{parent[0]}"."{parent[1]}"'
        else:
            drop_sql = drop_statements.get(ObjectType.TABLE, "")
    else:
        drop_sql = drop_statements.get(object_type, "")

    if drop_sql:
        cursor.execute(drop_sql)
        logger.debug("Dropped %s %s.%s", object_type.value, database_name, object_name)


def _generate_backup_name(table_name: str) -> str:
    """Generate a timestamped backup name (max 128 chars)."""
    suffix = datetime.now(timezone.utc).strftime("_bkp_%Y%m%d%H%M%S")
    max_base = 128 - len(suffix)
    base = table_name[:max_base] if len(table_name) > max_base else table_name
    return base + suffix
