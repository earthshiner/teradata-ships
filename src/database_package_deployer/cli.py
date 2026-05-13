"""
cli.py — Standalone command-line interface for the DDL Deployer.

Commands:
    deploy     Deploy all DDL files in a directory.
    analyze    Analyse dependencies and export graph.
    resume     Resume a previously failed deployment.
    rollback   Roll back a deployment to pre-deployment state.
    status     Show the current state of a deployment manifest.

Usage:
    python -m database_package_deployer deploy /path/to/ddl/ --host myserver --user dbc
    python -m database_package_deployer deploy /path/to/ddl/ --dry-run
    python -m database_package_deployer analyze /path/to/project/ --graph /path/to/output/
    python -m database_package_deployer analyze /path/to/project/ --graph . --formats dot,json
    python -m database_package_deployer resume /path/to/ddl/.deploy_manifest.json --host myserver
    python -m database_package_deployer rollback /path/to/ddl/.deploy_manifest.json --host myserver
    python -m database_package_deployer status /path/to/ddl/.deploy_manifest.json
"""

import argparse
import json
import logging
import os
import sys
import tempfile

from database_package_deployer.deployer import (
    deploy_package,
    resume_package,
    rollback_package,
)
from database_package_deployer.models import DeployState

# -- Graph format registry (name -> file extension) ---------------
_GRAPH_FORMATS = {
    "dot": ".gv",
    "mermaid": ".mmd",
    "json": ".json",
    "csv": ".csv",
    "openlineage": ".openlineage.json",
}
_ALL_FORMATS = ",".join(_GRAPH_FORMATS.keys())

logger = logging.getLogger(__name__)


def main():
    """CLI entry point."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s \u2014 %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "deploy":
        _cmd_deploy(args)
    elif args.command == "analyze":
        _cmd_analyze(args)
    elif args.command == "resume":
        _cmd_resume(args)
    elif args.command == "rollback":
        _cmd_rollback(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "approve":
        _cmd_approve(args)
    elif args.command == "audit-grants":
        _cmd_audit_grants(args)
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_deploy(args):
    """Execute the 'deploy' command with mandatory pre-flight."""
    from database_package_deployer.otel import deployer_span

    # -- Optional: download package from GitHub Release before connecting --
    _gh_tmp_dir: str = ""
    if getattr(args, "from_github", None):
        _gh_tmp_dir = tempfile.mkdtemp(prefix="ships_deploy_")
        try:
            _gh_tmp_dir = _download_github_package(args, _gh_tmp_dir)
        except Exception as exc:
            print(f"\nERROR: GitHub download failed: {exc}", file=sys.stderr)
            sys.exit(1)
    elif not getattr(args, "package_dir", None):
        print(
            "ERROR: package_dir is required unless --from-github is supplied.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build connection params before connecting so they can be passed to the TLS check.
    _conn_params = _build_connection_params(args)
    cursor = _connect(args)

    # Parse file patterns from comma-separated string
    patterns = [p.strip() for p in args.pattern.split(",")]

    # Read ordered file list if provided
    ordered_files = None
    if args.order_file:
        ordered_files = _read_order_file(args.order_file, args.package_dir)

    try:
        with deployer_span(
            "ships.deploy",
            {
                "ships.package_dir": args.package_dir,
                "ships.dry_run": args.dry_run,
            },
        ) as otel_span:
            result = deploy_package(
                cursor=cursor,
                package_dir=args.package_dir,
                file_patterns=patterns,
                ordered_files=ordered_files,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                deployed_env=getattr(args, "env", "") or "",
                approval_code=getattr(args, "approval_code", "") or "",
                connection_params=_conn_params,
                public_key_path=getattr(args, "public_key", "") or "",
            )
            otel_span.set_attribute("ships.deploy.completed", result.completed)
            otel_span.set_attribute("ships.deploy.failed", result.failed)
            otel_span.set_attribute("ships.deploy.skipped", result.skipped)
            otel_span.set_attribute("ships.deploy.success", result.success)
        _print_preflight_result(result.preflight_result)
        _print_package_result(result)
        sys.exit(0 if result.success else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cursor.close()
        cursor.connection.close()
        if _gh_tmp_dir and os.path.isdir(_gh_tmp_dir):
            import shutil

            try:
                shutil.rmtree(_gh_tmp_dir, ignore_errors=True)
                logger.debug("github_source: cleaned up temp dir %s", _gh_tmp_dir)
            except Exception as _clean_exc:
                logger.warning(
                    "github_source: could not clean up temp dir '%s': %s",
                    _gh_tmp_dir,
                    _clean_exc,
                )


def _download_github_package(args, tmp_dir: str) -> str:
    """Download a SHIPS package from a GitHub Release into *tmp_dir*.

    Validates that ``--release-tag`` and ``--asset`` are supplied, then
    calls ``github_source.download_release_assets`` and
    ``github_source.extract_zip_to_dir``.  Redirects ``args.package_dir``
    to the extracted package directory.

    Args:
        args:    Parsed CLI arguments (must have from_github, release_tag, asset).
        tmp_dir: Temporary directory for downloads.

    Returns:
        The *tmp_dir* root (the extracted package dir is set on args).

    Raises:
        SystemExit: When required flags are missing.
        Exception:  Propagates download/extraction errors to the caller.
    """
    from database_package_deployer.github_source import (
        download_release_assets,
        extract_zip_to_dir,
    )

    release_tag = getattr(args, "release_tag", None)
    asset_name = getattr(args, "asset", None)

    if not release_tag:
        print(
            "ERROR: --release-tag is required when --from-github is supplied.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not asset_name:
        print(
            "ERROR: --asset is required when --from-github is supplied.",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info(
        "Downloading '%s' from %s release %s ...",
        asset_name,
        args.from_github,
        release_tag,
    )
    zip_path = download_release_assets(
        owner_repo=args.from_github,
        release_tag=release_tag,
        asset_name=asset_name,
        dest_dir=tmp_dir,
    )

    pkg_dir = extract_zip_to_dir(zip_path, tmp_dir)
    args.package_dir = pkg_dir
    logger.info("github_source: package directory set to '%s'", pkg_dir)
    return tmp_dir


# ---------------------------------------------------------------
# analyze command -- dependency analysis + graph export
# ---------------------------------------------------------------


def _cmd_analyze(args):
    """
    Execute the 'analyze' command.

    Runs the SHIPS dependency analyser against a project directory,
    prints a summary, and optionally exports the dependency graph
    in one or more portable formats (DOT, Mermaid, JSON, CSV,
    OpenLineage).

    No database connection required -- purely static analysis.
    """
    # -- Import analyser (from td_release_packager) ---------------
    try:
        from td_release_packager.analyser import (
            analyse_project,
            format_summary,
        )
    except ImportError:
        print(
            "ERROR: td_release_packager package is required for "
            "analysis.\n"
            "Ensure td_release_packager is installed or on "
            "PYTHONPATH.",
            file=sys.stderr,
        )
        sys.exit(1)

    project_dir = args.project_dir

    if not os.path.isdir(project_dir):
        print(
            f"ERROR: Not a directory: {project_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Run analysis ---------------------------------------------
    print(f"\n  Analysing {project_dir} ...")
    result = analyse_project(project_dir)

    # -- Print summary --------------------------------------------
    print(f"\n{'=' * 64}")
    print("  Dependency Analysis")
    print(f"{'=' * 64}")
    print(format_summary(result))
    print(f"{'=' * 64}")

    # -- Export graph (if requested) -------------------------------
    if args.graph:
        _export_graph(result, args)

    # -- Exit code: non-zero if cycles detected -------------------
    sys.exit(1 if result.cycles else 0)


def _export_graph(result, args):
    """
    Export the dependency graph in the requested formats.

    Called by _cmd_analyze when --graph is specified.  Imports
    individual export functions from td_release_packager.graph_export
    and dispatches based on --formats.

    Args:
        result: The AnalysisResult from analyse_project.
        args:   Parsed CLI arguments containing graph, formats,
                namespace, project_name, and base_name.
    """
    # -- Import graph exporter ------------------------------------
    try:
        from td_release_packager.graph_export import (
            export_dot,
            export_mermaid,
            export_json,
            export_csv,
            export_openlineage,
        )
    except ImportError:
        print(
            "ERROR: td_release_packager.graph_export module not "
            "found.\n"
            "Ensure graph_export.py is in the td_release_packager "
            "package.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = args.graph
    os.makedirs(output_dir, exist_ok=True)

    # -- Parse requested formats ----------------------------------
    requested = {f.strip().lower() for f in args.formats.split(",")}

    # Validate format names
    unknown = requested - set(_GRAPH_FORMATS.keys())
    if unknown:
        print(
            f"ERROR: Unknown graph format(s): "
            f"{', '.join(sorted(unknown))}\n"
            f"Available: {_ALL_FORMATS}",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Dispatch to export functions -----------------------------
    # Map format name to its export function.
    # OpenLineage is handled separately (extra parameters).
    exporters = {
        "dot": export_dot,
        "mermaid": export_mermaid,
        "json": export_json,
        "csv": export_csv,
    }

    base = args.base_name
    written = []

    for fmt in sorted(requested):
        ext = _GRAPH_FORMATS[fmt]
        filepath = os.path.join(output_dir, f"{base}{ext}")

        if fmt == "openlineage":
            # OpenLineage needs namespace and project name
            content = export_openlineage(
                result,
                namespace=args.namespace,
                project_name=args.project_name,
            )
        else:
            content = exporters[fmt](result)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        written.append((fmt, filepath))
        logger.info("Exported %s -> %s", fmt, filepath)

    # -- Print export summary -------------------------------------
    count = len(written)
    print(f"\n  Graph exported ({count} format{'s' if count != 1 else ''}):")
    for fmt, filepath in written:
        size_kb = os.path.getsize(filepath) / 1024
        print(f"    {fmt:<14s} -> {filepath} ({size_kb:.1f} KB)")
    print()


# ---------------------------------------------------------------
# resume, rollback, status commands (unchanged)
# ---------------------------------------------------------------


def _cmd_resume(args):
    """Execute the 'resume' command."""
    cursor = _connect(args)

    try:
        result = resume_package(
            cursor=cursor,
            manifest_path=args.manifest_path,
            stop_on_failure=not args.continue_on_error,
            dry_run=args.dry_run,
        )
        _print_package_result(result)
        sys.exit(0 if result.success else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cursor.close()
        cursor.connection.close()


def _cmd_rollback(args):
    """Execute the 'rollback' command."""
    dry_run = getattr(args, "dry_run", False)
    wave_number = getattr(args, "wave", None)

    # Dry-run reads only from the manifest and disk — no DB connection needed.
    cursor = None if dry_run else _connect(args)

    try:
        result = rollback_package(
            cursor=cursor,
            manifest_path=args.manifest_path,
            dry_run=dry_run,
            wave_number=wave_number,
        )
        _print_package_result(result)
        sys.exit(0 if result.success else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if cursor is not None:
            cursor.close()
            cursor.connection.close()


def _cmd_status(args):
    """Display manifest status (no database connection needed)."""
    manifest_path = args.manifest_path

    if not os.path.exists(manifest_path):
        print(
            f"Manifest not found: {manifest_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"\n{'=' * 64}")
    print(f"  Deployment: {data['deployment_id']}")
    print(f"  Package:    {data['package_dir']}")
    print(f"  Status:     {data['status']}")
    print(f"  Started:    {data['started_at']}")
    print(f"  Updated:    {data['updated_at']}")
    print(f"{'=' * 64}")

    counts = {}
    for record in data["objects"].values():
        state = record["state"]
        counts[state] = counts.get(state, 0) + 1

    print(f"\n  Objects: {len(data['objects'])}")
    for state, count in sorted(counts.items()):
        print(f"    {state:15s}: {count}")

    print(f"\n  {'Object':<40s} {'State':<15s} {'Rows':<10s} Backup")
    print(f"  {'-' * 40} {'-' * 15} {'-' * 10} {'-' * 30}")

    for name, record in data["objects"].items():
        rows = record.get("rows_migrated", 0)
        backup = record.get("backup_table", "\u2014") or "\u2014"
        state = record["state"]
        print(f"  {name:<40s} {state:<15s} {rows:<10d} {backup}")

        if record.get("error"):
            print(f"    ERROR: {record['error']}")
        if record.get("blockers"):
            for blocker in record["blockers"]:
                print(f"    BLOCKER: {blocker}")

    print()


# ---------------------------------------------------------------
# approve command (GAP-006)
# ---------------------------------------------------------------


def _cmd_audit_grants(args):
    """Compare declared vs live grants and report drift."""
    from database_package_deployer.grant_audit import audit_grants

    cursor = _connect(args)
    try:
        report = audit_grants(cursor, args.package_dir)
    finally:
        cursor.close()
        cursor.connection.close()

    # JSON output to stdout
    print(json.dumps(report, indent=2, default=list))

    # Human-readable summary to stderr
    drift = report["drift"]
    matched = len(report["MATCHED"])
    missing = len(report["MISSING"])
    undeclared = len(report["UNDECLARED"])
    print(
        f"\nGrant audit: {matched} matched, {missing} missing, {undeclared} undeclared.",
        file=sys.stderr,
    )
    if drift:
        print("DRIFT DETECTED — review the report above.", file=sys.stderr)
    else:
        print("No drift detected.", file=sys.stderr)

    sys.exit(1 if drift else 0)


def _cmd_approve(args):
    """Generate a time-limited 4-eyes approval code for a package."""
    from database_package_deployer.mpa import generate_approval_code

    code = generate_approval_code(args.package_zip)
    if code is None:
        print(
            "\nERROR: SHIPS_SIGNING_KEY is not set. "
            "Set the environment variable to generate an approval code.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(code)


# ---------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------


def _print_preflight_result(preflight_result):
    """Display pre-flight validation results."""
    if preflight_result is None:
        return

    pf = preflight_result
    status_icon = "\u2713" if pf.passed else "\u2717"

    print(f"\n{'\u2500' * 64}")
    print(f"  {status_icon} Pre-flight Validation")
    print(f"{'\u2500' * 64}")

    if pf.object_count:
        parts = [f"{v} {k}(s)" for k, v in sorted(pf.object_count.items())]
        print(f"  Objects:    {', '.join(parts)}")

    if pf.databases:
        print(f"  Databases:  {', '.join(pf.databases)}")

    print(f"  Errors:     {pf.errors}")
    print(f"  Warnings:   {pf.warnings}")

    # Show failures and warnings
    for check in pf.checks:
        if not check.passed and check.severity == "ERROR":
            print(f"    \u2717 [{check.database}] {check.message}")
        elif check.severity == "WARNING":
            print(f"    \u26a0 [{check.database}] {check.message}")

    print(f"{'\u2500' * 64}")


def _print_package_result(result):
    """Display deployment results."""
    status_icon = "\u2713" if result.success else "\u2717"
    mode = " (DRY RUN)" if result.dry_run else ""

    print(f"\n{'=' * 64}")
    print(f"  {status_icon} Deployment{mode}: {result.deployment_id}")
    if result.manifest_path:
        print(f"  Manifest:   {result.manifest_path}")
    if result.report_path:
        print(f"  Report:     {result.report_path}")
    print(f"{'=' * 64}")
    print(f"  Total:       {result.total}")
    print(f"  Completed:   {result.completed}")
    print(f"  Skipped:     {result.skipped}")
    print(f"  Failed:      {result.failed}")
    print(f"  Rolled back: {result.rolled_back}")
    print(f"{'=' * 64}")

    for obj_result in result.results:
        icon = {
            DeployState.COMPLETED: "\u2713",
            DeployState.SKIPPED: "\u26a0",
            DeployState.FAILED: "\u2717",
            DeployState.ROLLED_BACK: "\u21a9",
        }.get(obj_result.state, "?")

        type_label = obj_result.object_type.value
        print(
            f"\n  {icon} [{type_label}] "
            f"{obj_result.database_name}."
            f"{obj_result.object_name}"
        )
        print(f"    {obj_result.message}")

        if obj_result.warnings:
            for w in obj_result.warnings:
                print(f"    \u26a0 {w}")
        if obj_result.blockers:
            for b in obj_result.blockers:
                print(f"    \u2717 {b}")

    print()


# ---------------------------------------------------------------
# Order file parsing
# ---------------------------------------------------------------


def _read_order_file(
    order_file_path: str,
    package_dir: str,
) -> list:
    """
    Read a text file listing DDL filenames in deployment order.

    Each line is a filename (not a full path). Blank lines and
    lines starting with '#' are ignored. Filenames are resolved
    relative to the package directory.

    Args:
        order_file_path:  Path to the order file.
        package_dir:      Base directory for resolving filenames.

    Returns:
        List of absolute file paths in the listed order.
    """
    if not os.path.exists(order_file_path):
        print(
            f"ERROR: Order file not found: {order_file_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(order_file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    ordered = []
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue
        # Resolve relative to package_dir
        full_path = os.path.join(package_dir, stripped)
        if not os.path.exists(full_path):
            print(
                f"ERROR: File listed in order file line {lineno} "
                f"not found: {stripped} "
                f"(resolved: {full_path})",
                file=sys.stderr,
            )
            sys.exit(1)
        ordered.append(full_path)

    if not ordered:
        print(
            f"ERROR: Order file is empty: {order_file_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    return ordered


# ---------------------------------------------------------------
# Connection
# ---------------------------------------------------------------


def _build_connection_params(args) -> dict:
    """Build the connection params dict from CLI args (GAP-015).

    Returns the dict that would be passed to teradatasql.connect().
    Used by the TLS check to inspect encryption settings before or
    alongside the actual connection.
    """
    host = getattr(args, "host", None) or os.environ.get("TD_HOST", "")
    user = getattr(args, "user", None) or os.environ.get("TD_USER", "")
    password = getattr(args, "password", None) or os.environ.get("TD_PASSWORD", "")
    logmech = getattr(args, "logmech", None) or os.environ.get("TD_LOGMECH", "")
    encryptdata = getattr(args, "encryptdata", None) or os.environ.get(
        "TD_ENCRYPTDATA", ""
    )
    sslmode = getattr(args, "sslmode", None) or os.environ.get("TD_SSLMODE", "")

    params = {}
    if host:
        params["host"] = host
    if user:
        params["user"] = user
    if password:
        params["password"] = password
    if logmech:
        params["logmech"] = logmech
    if encryptdata:
        params["encryptdata"] = encryptdata
    if sslmode:
        params["sslmode"] = sslmode
    return params


def _connect(args):
    """Establish a Teradata database connection."""
    try:
        import teradatasql
    except ImportError:
        print(
            "ERROR: teradatasql package is required.\n"
            "Install with: pip install teradatasql",
            file=sys.stderr,
        )
        sys.exit(1)

    host = args.host or os.environ.get("TD_HOST")
    user = args.user or os.environ.get("TD_USER")
    password = args.password or os.environ.get("TD_PASSWORD")
    logmech = args.logmech or os.environ.get("TD_LOGMECH")

    if not host:
        print(
            "ERROR: --host or TD_HOST required.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not user:
        print(
            "ERROR: --user or TD_USER required.",
            file=sys.stderr,
        )
        sys.exit(1)

    params = {"host": host, "user": user}
    if password:
        params["password"] = password
    if logmech:
        params["logmech"] = logmech

    try:
        conn = teradatasql.connect(**params)
        cursor = conn.cursor()
        # teradatasql's "charset" JSON field is not recognised by the Go-side
        # parser in current shipped versions — setting it via a post-connect
        # session statement is the safe cross-version alternative.
        cursor.execute("SET SESSION CHARACTER SET UNICODE")
        return cursor
    except Exception as e:
        # Strip the Go stack trace from teradatasql errors.
        # Show user-friendly message with just the Teradata
        # error code and description.
        err = str(e)
        import re

        clean = re.sub(
            r"\s*\bat\s+gosqldriver/.*",
            "",
            err,
            flags=re.DOTALL,
        ).strip()
        print(
            f"ERROR: Connection failed.\n"
            f"  Host:  {host}\n"
            f"  User:  {user}\n"
            f"  Error: {clean}",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="database_package_deployer",
        description=(
            "Idempotent Teradata DDL Deployment with "
            "Restartability. Handles tables, join indexes, hash "
            "indexes, secondary indexes, triggers, views, macros, "
            "procedures, and functions with mandatory pre-flight "
            "validation."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    subs = parser.add_subparsers(
        dest="command",
        help="Available commands",
    )

    # -- deploy --
    dp = subs.add_parser(
        "deploy",
        help="Deploy DDL files in a directory.",
    )
    dp.add_argument(
        "package_dir",
        nargs="?",
        default=None,
        help=(
            "Directory containing DDL files. "
            "Omit when using --from-github (the directory is resolved automatically)."
        ),
    )
    dp.add_argument(
        "--from-github",
        dest="from_github",
        default=None,
        metavar="OWNER/REPO",
        help=(
            "Download the package from this GitHub repository before deploying. "
            "Requires --release-tag and --asset. "
            "Uses GITHUB_TOKEN for private repositories."
        ),
    )
    dp.add_argument(
        "--release-tag",
        dest="release_tag",
        default=None,
        metavar="TAG",
        help=(
            "GitHub Release tag to download from (e.g. v1.2.3, or 'latest'). "
            "Required with --from-github."
        ),
    )
    dp.add_argument(
        "--asset",
        dest="asset",
        default=None,
        metavar="FILENAME",
        help=(
            "Name of the ZIP asset in the GitHub Release (e.g. my_package_PRD_0042.zip). "
            "Required with --from-github."
        ),
    )
    dp.add_argument(
        "--pattern",
        default=("*.tbl,*.jix,*.idx,*.viw,*.spl,*.mcr,*.fnc,*.trg"),
        help=("Comma-separated file glob patterns (default: all DDL types)."),
    )
    dp.add_argument(
        "--order-file",
        help=(
            "Path to a text file listing DDL filenames in "
            "deployment order (one per line). Bypasses glob "
            "discovery and type-based reordering."
        ),
    )
    dp.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate deployment without executing any DDL.",
    )
    dp.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue past failures.",
    )
    dp.add_argument(
        "--env",
        metavar="ENV",
        default="",
        help=(
            "Target environment name (e.g. PRD, DEV). When supplied, the "
            "package's target_env field is verified to match before any DDL "
            "executes (env_lock check, GAP-002). Omit to skip this check."
        ),
    )
    dp.add_argument(
        "--approval-code",
        dest="approval_code",
        default="",
        metavar="CODE",
        help=(
            "4-eyes approval code produced by 'ships approve <package_zip>' "
            "(GAP-006). Required when the target environment has "
            "require_approvals: 2 in ships.yaml."
        ),
    )
    dp.add_argument(
        "--public-key",
        dest="public_key",
        default="",
        metavar="KEY_FILE",
        help=(
            "Path to an Ed25519 public key PEM file for verifying the .sig "
            "sidecar (Option C). Falls back to SHIPS_PUBLIC_KEY_PATH env var, "
            "the key embedded in ships.build.json, and SHIPS_PUBLIC_KEY env var."
        ),
    )
    _add_conn_args(dp)

    # -- analyze --
    az = subs.add_parser(
        "analyze",
        help="Analyse DDL dependencies and export the graph.",
    )
    az.add_argument(
        "project_dir",
        help="Path to the SHIPS project root.",
    )
    az.add_argument(
        "--graph",
        metavar="OUTPUT_DIR",
        help=(
            "Export the dependency graph to OUTPUT_DIR. "
            "Creates the directory if it does not exist."
        ),
    )
    az.add_argument(
        "--formats",
        default=_ALL_FORMATS,
        help=(f"Comma-separated list of export formats (default: {_ALL_FORMATS})."),
    )
    az.add_argument(
        "--base-name",
        default="ships_dependencies",
        help=("Base filename for exported files (default: ships_dependencies)."),
    )
    az.add_argument(
        "--namespace",
        default="teradata://ships-analysis",
        help=(
            "OpenLineage dataset namespace URI. "
            "For a live system use teradata://hostname:1025 "
            "(default: teradata://ships-analysis)."
        ),
    )
    az.add_argument(
        "--project-name",
        default="ships-project",
        help=("OpenLineage job namespace / project name (default: ships-project)."),
    )

    # -- resume --
    rp = subs.add_parser(
        "resume",
        help="Resume a failed deployment.",
    )
    rp.add_argument(
        "manifest_path",
        help="Path to .deploy_manifest.json.",
    )
    rp.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate remaining deployments.",
    )
    rp.add_argument(
        "--continue-on-error",
        action="store_true",
    )
    _add_conn_args(rp)

    # -- rollback --
    rb = subs.add_parser(
        "rollback",
        help="Roll back a deployment.",
    )
    rb.add_argument(
        "manifest_path",
        help="Path to .deploy_manifest.json.",
    )
    rb.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be rolled back without executing any DDL "
        "or mutating the manifest. Reads the manifest and checks disk "
        "for rollback files — no database connection required.",
    )
    rb.add_argument(
        "--wave",
        type=int,
        default=None,
        metavar="N",
        help="Roll back only objects deployed in wave N. Objects in other "
        "waves are untouched. The package status becomes "
        "PARTIALLY_ROLLED_BACK. Objects with no wave assignment (serial "
        "prereqs phase) are excluded from wave-scoped rollback.",
    )
    _add_conn_args(rb)

    # -- status --
    st = subs.add_parser(
        "status",
        help="Show deployment manifest status.",
    )
    st.add_argument(
        "manifest_path",
        help="Path to .deploy_manifest.json.",
    )

    # -- audit-grants (GAP-014) --
    ag = subs.add_parser(
        "audit-grants",
        help=(
            "[GAP-014] Compare declared vs live grants and report drift. "
            "Exit 0 = no drift, exit 1 = drift detected."
        ),
    )
    ag.add_argument(
        "package_dir",
        help="Extracted package directory (contains payload/02_dcl/).",
    )
    _add_conn_args(ag)

    # -- approve (GAP-006) --
    ap = subs.add_parser(
        "approve",
        help=(
            "[GAP-006] Generate a time-limited 4-eyes approval code for a package. "
            "Requires SHIPS_SIGNING_KEY to be set."
        ),
    )
    ap.add_argument(
        "package_zip",
        help="Path to the release ZIP archive to approve.",
    )

    return parser


def _add_conn_args(parser):
    """Add Teradata connection arguments."""
    parser.add_argument(
        "--host",
        help="Teradata host (or TD_HOST).",
    )
    parser.add_argument(
        "--user",
        help="Teradata user (or TD_USER).",
    )
    parser.add_argument(
        "--password",
        help="Teradata password (or TD_PASSWORD).",
    )
    parser.add_argument(
        "--logmech",
        help="Logon mechanism (or TD_LOGMECH).",
    )
    parser.add_argument(
        "--encryptdata",
        default="",
        help=(
            "Enable TLS encryption (e.g. 'true'). "
            "Passed to teradatasql as encryptdata. (or TD_ENCRYPTDATA)."
        ),
    )
    parser.add_argument(
        "--sslmode",
        default="",
        help=(
            "TLS SSL mode (e.g. 'require', 'verify-ca'). "
            "Passed to teradatasql as sslmode. (or TD_SSLMODE)."
        ),
    )


if __name__ == "__main__":
    main()
