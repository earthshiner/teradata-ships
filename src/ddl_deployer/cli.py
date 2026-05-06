"""
cli.py — Standalone command-line interface for the DDL Deployer.

Commands:
    deploy     Deploy all DDL files in a directory.
    analyze    Analyse dependencies and export graph.
    resume     Resume a previously failed deployment.
    rollback   Roll back a deployment to pre-deployment state.
    status     Show the current state of a deployment manifest.

Usage:
    python -m ddl_deployer deploy /path/to/ddl/ --host myserver --user dbc
    python -m ddl_deployer deploy /path/to/ddl/ --dry-run
    python -m ddl_deployer analyze /path/to/project/ --graph /path/to/output/
    python -m ddl_deployer analyze /path/to/project/ --graph . --formats dot,json
    python -m ddl_deployer resume /path/to/ddl/.deploy_manifest.json --host myserver
    python -m ddl_deployer rollback /path/to/ddl/.deploy_manifest.json --host myserver
    python -m ddl_deployer status /path/to/ddl/.deploy_manifest.json
"""

import argparse
import json
import logging
import os
import sys

from ddl_deployer.deployer import deploy_package, resume_package, rollback_package
from ddl_deployer.models import DeployState

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
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_deploy(args):
    """Execute the 'deploy' command with mandatory pre-flight."""
    cursor = _connect(args)

    # Parse file patterns from comma-separated string
    patterns = [p.strip() for p in args.pattern.split(",")]

    # Read ordered file list if provided
    ordered_files = None
    if args.order_file:
        ordered_files = _read_order_file(args.order_file, args.package_dir)

    try:
        result = deploy_package(
            cursor=cursor,
            package_dir=args.package_dir,
            file_patterns=patterns,
            ordered_files=ordered_files,
            stop_on_failure=not args.continue_on_error,
            dry_run=args.dry_run,
        )
        _print_preflight_result(result.preflight_result)
        _print_package_result(result)
        sys.exit(0 if result.success else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cursor.close()
        cursor.connection.close()


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
    cursor = _connect(args)

    try:
        result = rollback_package(
            cursor=cursor,
            manifest_path=args.manifest_path,
        )
        _print_package_result(result)
        sys.exit(0 if result.success else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
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

    params = {"host": host, "user": user, "charset": "UTF8"}
    if password:
        params["password"] = password
    if logmech:
        params["logmech"] = logmech

    try:
        conn = teradatasql.connect(**params)
        return conn.cursor()
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
        prog="ddl_deployer",
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
        help="Directory containing DDL files.",
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


if __name__ == "__main__":
    main()
