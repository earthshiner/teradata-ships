"""
cli.py — Standalone command-line interface for the DDL Deployer.

Commands:
    deploy     Deploy all DDL files in a directory.
    resume     Resume a previously failed deployment.
    rollback   Roll back a deployment to pre-deployment state.
    status     Show the current state of a deployment manifest.

Usage:
    python -m ddl_deployer deploy /path/to/ddl/ --host myserver --user dbc
    python -m ddl_deployer deploy /path/to/ddl/ --dry-run
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


def main():
    """CLI entry point."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "deploy":
        _cmd_deploy(args)
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
    patterns = [p.strip() for p in args.pattern.split(',')]

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
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path, 'r', encoding='utf-8') as f:
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
        backup = record.get("backup_table", "—") or "—"
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
    status_icon = "✓" if pf.passed else "✗"

    print(f"\n{'─' * 64}")
    print(f"  {status_icon} Pre-flight Validation")
    print(f"{'─' * 64}")

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
            print(f"    ✗ [{check.database}] {check.message}")
        elif check.severity == "WARNING":
            print(f"    ⚠ [{check.database}] {check.message}")

    print(f"{'─' * 64}")


def _print_package_result(result):
    """Display deployment results."""
    status_icon = "✓" if result.success else "✗"
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
            DeployState.COMPLETED: "✓",
            DeployState.SKIPPED: "⚠",
            DeployState.FAILED: "✗",
            DeployState.ROLLED_BACK: "↩",
        }.get(obj_result.state, "?")

        type_label = obj_result.object_type.value
        print(f"\n  {icon} [{type_label}] {obj_result.database_name}.{obj_result.object_name}")
        print(f"    {obj_result.message}")

        if obj_result.warnings:
            for w in obj_result.warnings:
                print(f"    ⚠ {w}")
        if obj_result.blockers:
            for b in obj_result.blockers:
                print(f"    ✗ {b}")

    print()


# ---------------------------------------------------------------
# Order file parsing
# ---------------------------------------------------------------

def _read_order_file(order_file_path: str, package_dir: str) -> list:
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
        print(f"ERROR: Order file not found: {order_file_path}", file=sys.stderr)
        sys.exit(1)

    with open(order_file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    ordered = []
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip blank lines and comments
        if not stripped or stripped.startswith('#'):
            continue
        # Resolve relative to package_dir
        full_path = os.path.join(package_dir, stripped)
        if not os.path.exists(full_path):
            print(
                f"ERROR: File listed in order file line {lineno} "
                f"not found: {stripped} (resolved: {full_path})",
                file=sys.stderr,
            )
            sys.exit(1)
        ordered.append(full_path)

    if not ordered:
        print(f"ERROR: Order file is empty: {order_file_path}", file=sys.stderr)
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
        print("ERROR: --host or TD_HOST required.", file=sys.stderr)
        sys.exit(1)
    if not user:
        print("ERROR: --user or TD_USER required.", file=sys.stderr)
        sys.exit(1)

    params = {"host": host, "user": user}
    if password:
        params["password"] = password
    if logmech:
        params["logmech"] = logmech

    try:
        conn = teradatasql.connect(**params)
        return conn.cursor()
    except Exception as e:
        print(f"ERROR: Connection failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ddl_deployer",
        description=(
            "Idempotent Teradata DDL Deployment with Restartability. "
            "Handles tables, join indexes, hash indexes, secondary "
            "indexes, triggers, views, macros, procedures, and "
            "functions with mandatory pre-flight validation."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")

    subs = parser.add_subparsers(dest="command", help="Available commands")

    # -- deploy --
    dp = subs.add_parser("deploy", help="Deploy DDL files in a directory.")
    dp.add_argument("package_dir", help="Directory containing DDL files.")
    dp.add_argument("--pattern", default="*.tbl,*.jix,*.idx,*.viw,*.spl,*.mcr,*.fnc,*.trg",
                    help="Comma-separated file glob patterns (default: all DDL types).")
    dp.add_argument("--order-file",
                    help="Path to a text file listing DDL filenames in deployment "
                         "order (one per line). Bypasses glob discovery and "
                         "type-based reordering — files deploy in the listed order.")
    dp.add_argument("--dry-run", action="store_true",
                    help="Simulate deployment without executing any DDL.")
    dp.add_argument("--continue-on-error", action="store_true",
                    help="Continue past failures.")
    _add_conn_args(dp)

    # -- resume --
    rp = subs.add_parser("resume", help="Resume a failed deployment.")
    rp.add_argument("manifest_path", help="Path to .deploy_manifest.json.")
    rp.add_argument("--dry-run", action="store_true",
                    help="Simulate remaining deployments.")
    rp.add_argument("--continue-on-error", action="store_true")
    _add_conn_args(rp)

    # -- rollback --
    rb = subs.add_parser("rollback", help="Roll back a deployment.")
    rb.add_argument("manifest_path", help="Path to .deploy_manifest.json.")
    _add_conn_args(rb)

    # -- status --
    st = subs.add_parser("status", help="Show deployment manifest status.")
    st.add_argument("manifest_path", help="Path to .deploy_manifest.json.")

    return parser


def _add_conn_args(parser):
    """Add Teradata connection arguments."""
    parser.add_argument("--host", help="Teradata host (or TD_HOST).")
    parser.add_argument("--user", help="Teradata user (or TD_USER).")
    parser.add_argument("--password", help="Teradata password (or TD_PASSWORD).")
    parser.add_argument("--logmech", help="Logon mechanism (or TD_LOGMECH).")


if __name__ == "__main__":
    main()
