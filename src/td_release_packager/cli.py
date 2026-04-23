"""
cli.py — Command-line interface for the Teradata Release Packager.

Commands:
    scaffold   Create a new project from template.
    harvest    Import raw DDL files into a project.
    inspect    Check DDL against Coding Discipline.
    package    Build a release package for a target environment.
    scan       Scan source files and report all tokens found.
    analyze    Analyse DDL dependencies, generate waves, export graph.

Usage:
    python -m td_release_packager scaffold --name MortgagePlatform --output /projects
    python -m td_release_packager build --source . --env DEV --name create_objects --properties config/properties/DEV.properties
    python -m td_release_packager scan --source .
    python -m td_release_packager analyze --source . --graph ./output/
    python -m td_release_packager analyze --source . --graph . --formats dot,json,openlineage
"""

import argparse
import logging
import os
import sys

from td_release_packager.builder import build_package
from td_release_packager.build_counter import read_build_number
from td_release_packager.ingest import ingest_directory
from td_release_packager.token_engine import (
    read_token_map,
    write_token_map,
    generate_token_map,
)
from td_release_packager.models import BuildConfig
from td_release_packager.scaffolder import scaffold_project
from td_release_packager.token_engine import (
    read_properties,
    scan_tokens_in_directory,
    validate_tokens,
)
from td_release_packager.validate import validate_directory, read_inspect_config

logger = logging.getLogger(__name__)

# -- Graph format registry (name → file extension) ---------------
_GRAPH_FORMATS = {
    "dot": ".gv",
    "mermaid": ".mmd",
    "json": ".json",
    "csv": ".csv",
    "openlineage": ".openlineage.json",
}
_ALL_FORMATS = ",".join(_GRAPH_FORMATS.keys())


def main():
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "scaffold":
        _cmd_scaffold(args)
    elif args.command == "harvest":
        _cmd_ingest(args)
    elif args.command == "inspect":
        _cmd_validate(args)
    elif args.command == "package":
        _cmd_build(args)
    elif args.command == "scan":
        _cmd_scan(args)
    elif args.command == "analyze":
        _cmd_analyze(args)
    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------
# Commands
# ---------------------------------------------------------------

def _cmd_scaffold(args):
    """Create a new project from template, or repair an existing one."""
    envs = [e.strip().upper() for e in args.environments.split(',')]
    repair = getattr(args, 'repair', False)

    try:
        project_dir = scaffold_project(
            project_name=args.name,
            output_dir=args.output,
            environments=envs,
            repair=repair,
        )

        action = "repaired" if repair else "scaffolded"
        icon = "✓"

        print(f"\n{'=' * 64}")
        print(f"  {icon} Project {action}: {args.name}")
        print(f"{'=' * 64}")
        print(f"  Location:     {project_dir}")
        print(f"  Environments: {', '.join(envs)}")

        if repair:
            print(f"\n  Repair complete. Missing directories and files have")
            print(f"  been created. Existing files were NOT overwritten.")
        else:
            print(f"\n  SHIPS workflow — next steps:")
            print(f"    [S] Scaffold  ✓ Done")
            print(f"    [H] Harvest   python -m td_release_packager harvest \\")
            print(f"                    --source /raw/ddl/ --project {project_dir}")
            print(f"    [I] Inspect   python -m td_release_packager inspect \\")
            print(f"                    --source {project_dir}")
            print(f"    [P] Package   python -m td_release_packager package \\")
            print(f"                    --source {project_dir} --env DEV --name {args.name} \\")
            print(f"                    --properties config/properties/DEV.properties")
            print(f"    [S] Ship      python deploy.py --host <host> --user <user>")

        print(f"{'=' * 64}\n")

    except FileExistsError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(f"  Tip: use --repair to add missing directories and files", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_ingest(args):
    """Import raw DDL files into a project."""
    # -- Build apply_tokens dict from available sources --
    apply_tokens = None

    # Option 1: --token-map file (preferred)
    if hasattr(args, 'token_map') and args.token_map:
        apply_tokens = read_token_map(args.token_map)

    # Option 2: --apply-tokens inline pairs (legacy)
    elif hasattr(args, 'apply_tokens') and args.apply_tokens:
        apply_tokens = {}
        for pair in args.apply_tokens.split(','):
            if '=' not in pair:
                continue
            literal, token = pair.split('=', 1)
            apply_tokens[literal.strip()] = token.strip()

    try:
        result = ingest_directory(
            source_dir=args.source,
            project_dir=args.project,
            detect_tokens=True,
            apply_tokens=apply_tokens,
            force=args.force,
        )

        print(f"\n{'=' * 64}")
        print(f"  DDL Harvest Results")
        print(f"{'=' * 64}")
        print(f"  Source:           {args.source}")
        print(f"  Project:          {args.project}")
        if args.force:
            print(f"  Mode:             FORCE (overwrite existing)")
        print(f"  Files scanned:    {result.total_files}")
        print(f"  Classified:       {result.classified}")
        if result.overwritten:
            print(f"  Overwritten:      {result.overwritten}")
        if result.skipped_existing:
            print(f"  Skipped (exist):  {result.skipped_existing}")
        print(f"  Unclassified:     {result.unclassified}")
        print(f"  MULTISET inject:  {result.multiset_injected}")

        if apply_tokens:
            print(f"  Tokens applied:   {len(apply_tokens)} mappings")

        if result.files_placed:
            print(f"\n  Files placed:")
            for src, dest, obj_type in result.files_placed:
                print(f"    {obj_type:15s} {src}")
                print(f"    {'':15s} → {dest}")

        if result.unclassified_files:
            print(f"\n  Unclassified files (manual review needed):")
            for f in result.unclassified_files:
                print(f"    ⚠ {f}")

        # -- Generate token map if requested --
        env_prefix = getattr(args, 'env_prefix', None)
        generate_map = getattr(args, 'generate_token_map', False)

        if generate_map and result.token_candidates:
            token_map = generate_token_map(
                result.token_candidates, env_prefix
            )
            map_path = os.path.join(
                args.project, "config", "token_map.conf"
            )
            write_token_map(
                map_path, token_map,
                result.token_candidates, env_prefix or "(none)"
            )
            print(f"\n  Token map generated: {map_path}")
            print(f"  Mappings:           {len(token_map)}")
            if env_prefix:
                print(f"  Prefix stripped:    {env_prefix}")
            else:
                print(f"  No --env-prefix:    full names used as tokens")
            for literal, token in sorted(token_map.items()):
                files = result.token_candidates.get(literal, [])
                print(f"    {literal} → {token}  ({len(files)} refs)")
            print(f"\n  To apply: re-harvest with --token-map {map_path}")

        elif result.token_candidates and not apply_tokens:
            print(f"\n  Token candidates (hardcoded database names):")
            for db_name, files in sorted(result.token_candidates.items()):
                print(f"    '{db_name}' ({len(files)} refs)")
            if not generate_map:
                print(f"\n  Tip: re-run with --generate-token-map --env-prefix <PREFIX>")
                print(f"  to auto-generate a token mapping file.")

        if result.warnings:
            print(f"\n  Warnings:")
            for w in result.warnings:
                print(f"    ⚠ {w}")

        if result.errors:
            print(f"\n  Errors:")
            for e in result.errors:
                print(f"    ✗ {e}")

        print(f"{'=' * 64}\n")

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_validate(args):
    """Validate DDL files against the Coding Discipline."""
    try:
        # -- Load rules config --
        rules_config = None
        if hasattr(args, 'config') and args.config:
            rules_config = read_inspect_config(args.config)
        else:
            # Auto-detect config in project's config/ directory
            auto_config = os.path.join(args.source, "config", "inspect.conf")
            if os.path.exists(auto_config):
                rules_config = read_inspect_config(auto_config)

        # -- Apply legacy --skip-* flags as overrides --
        if rules_config is None:
            from td_release_packager.validate import DEFAULT_RULES
            rules_config = dict(DEFAULT_RULES)

        if hasattr(args, 'skip_tokens') and args.skip_tokens:
            rules_config["hardcoded_name"] = "OFF"
        if hasattr(args, 'skip_keywords') and args.skip_keywords:
            rules_config["keyword_case"] = "OFF"
        if hasattr(args, 'skip_commas') and args.skip_commas:
            rules_config["leading_commas"] = "OFF"

        result = validate_directory(
            source_dir=args.source,
            rules_config=rules_config,
            strict=args.strict,
        )

        icon = "✓" if result.passed else "✗"
        status = "PASSED" if result.passed else "FAILED"
        mode = " (strict)" if args.strict else ""

        print(f"\n{'=' * 64}")
        print(f"  {icon} Coding Discipline Validation — {status}{mode}")
        print(f"{'=' * 64}")
        print(f"  Files scanned:    {result.files_scanned}")
        print(f"  Files passed:     {result.files_passed}")
        print(f"  Files with issues:{result.files_with_issues}")
        print(f"  Errors:           {result.errors}")
        print(f"  Warnings:         {result.warnings}")

        if result.issues:
            # Group by file
            by_file = {}
            for issue in result.issues:
                by_file.setdefault(issue.file, []).append(issue)

            print(f"\n  Issues by file:")
            for file, issues in sorted(by_file.items()):
                err_count = sum(1 for i in issues if i.severity == "ERROR")
                file_icon = "✗" if err_count > 0 else "⚠"
                print(f"\n    {file_icon} {file}")
                for issue in issues:
                    if issue.severity == "ERROR":
                        sev = "✗"
                    elif issue.severity == "WARNING":
                        sev = "⚠"
                    else:
                        sev = "ℹ"
                    print(f"      {sev} [{issue.rule}] {issue.message}")

        if result.passed:
            print(f"\n  All files conform to the Teradata Engineering Discipline.")

        print(f"{'=' * 64}\n")
        sys.exit(0 if result.passed else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_build(args):
    """Build a release package."""
    # -- Cross-check: --env must match SHIPS_ENV in properties file --
    # The properties file declares its own environment via SHIPS_ENV.
    # This prevents building a DEV-labelled package with PROD tokens.
    if args.properties and os.path.isfile(args.properties):
        env_upper = args.env.upper()
        declared_env = None
        try:
            props = read_properties(args.properties)
            declared_env = props.get("SHIPS_ENV", "").upper()
        except Exception:
            pass  # File read errors handled later by build_package

        if declared_env and declared_env != env_upper:
            print(
                f"\nERROR: Environment mismatch.\n"
                f"  --env        = {env_upper}\n"
                f"  SHIPS_ENV    = {declared_env} "
                f"(declared in {os.path.basename(args.properties)})\n\n"
                f"  The SHIPS_ENV property inside the file must match --env.\n"
                f"  Either change --env to {declared_env}, or use the correct\n"
                f"  properties file for {env_upper}.",
                file=sys.stderr,
            )
            sys.exit(1)
        elif not declared_env:
            print(
                f"  ⚠ No SHIPS_ENV declared in {os.path.basename(args.properties)} "
                f"— environment cross-check skipped.",
            )

    # Resolve build number: explicit, no-increment, or auto-increment
    build_number = args.build_number  # None if not specified

    if build_number is not None:
        print(f"  Build number: {build_number} (explicit)")
    elif args.no_increment:
        # Reuse current build number — same source, different env
        try:
            build_number = read_build_number(args.source)
            print(f"  Build number: {build_number} (--no-increment, same source)")
        except FileNotFoundError:
            print(
                "ERROR: No .build_counter file found — cannot use --no-increment.\n"
                "  Run a normal build first to establish the build number.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Preview what the auto-increment will produce
        try:
            current = read_build_number(args.source)
            print(f"  Build number: {current + 1} (auto-increment from .build_counter)")
        except FileNotFoundError:
            print(
                "ERROR: No .build_counter file found. Either:\n"
                "  - Run 'td_release_packager scaffold' to create a project, or\n"
                "  - Pass --build-number explicitly, or\n"
                "  - Create .build_counter containing '0'",
                file=sys.stderr,
            )
            sys.exit(1)

    config = BuildConfig(
        source_dir=args.source,
        environment=args.env.upper(),
        package_name=args.name,
        properties_file=args.properties,
        build_number=build_number,
        output_dir=args.output,
        archive_format=args.format,
        author=args.author or "",
        description=args.description or "",
        source_commit=args.commit or "",
    )

    try:
        archive_path, manifest = build_package(config)

        print(f"\n{'=' * 64}")
        print(f"  ✓ Package built successfully")
        print(f"{'=' * 64}")
        print(f"  Archive:     {archive_path}")
        print(f"  Environment: {manifest.environment}")
        print(f"  Build:       {manifest.build_number}")
        print(f"  Files:       {manifest.file_count}")
        print(f"  Tokens:      {manifest.token_count} substitutions")
        print(f"{'=' * 64}")

        for phase, count in sorted(manifest.phase_inventory.items()):
            print(f"    {phase}: {count} file(s)")

        if manifest.warnings:
            print(f"\n  Warnings:")
            for w in manifest.warnings:
                print(f"    ⚠ {w}")

        print()

    except (FileNotFoundError, ValueError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_scan(args):
    """Scan payload files for token references."""
    source_dir = args.source
    if not os.path.isdir(source_dir):
        print(f"ERROR: Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # Only scan the payload — not config/, releases/, README, etc.
    scan_dir = source_dir
    for candidate in ['payload/database', 'payload']:
        path = os.path.join(source_dir, candidate)
        if os.path.isdir(path):
            scan_dir = path
            break

    if scan_dir == source_dir:
        print(f"  ⚠ No payload/ directory found — scanning entire project")

    usage = scan_tokens_in_directory(scan_dir)

    # Collect all unique tokens
    all_tokens = set()
    for tokens in usage.values():
        all_tokens.update(tokens)

    print(f"\n{'=' * 64}")
    print(f"  Token Scan: {scan_dir}")
    print(f"{'=' * 64}")
    print(f"  Files with tokens: {len(usage)}")
    print(f"  Unique tokens:     {len(all_tokens)}")

    if all_tokens:
        print(f"\n  Tokens found:")
        for t in sorted(all_tokens):
            files = [f for f, tokens in usage.items() if t in tokens]
            print(f"    {{{{{t}}}}} — used in {len(files)} file(s)")

    # Validate against properties if provided
    if args.properties:
        try:
            values = read_properties(args.properties)
            errors, warnings = validate_tokens(values, usage)

            if errors:
                print(f"\n  Validation ERRORS:")
                for e in errors:
                    print(f"    ✗ {e}")

            if warnings:
                print(f"\n  Validation WARNINGS:")
                for w in warnings:
                    print(f"    ⚠ {w}")

            if not errors and not warnings:
                print(f"\n  ✓ All tokens validated against {args.properties}")

        except FileNotFoundError:
            print(f"\n  ⚠ Properties file not found: {args.properties}")

    print()


# ---------------------------------------------------------------
# analyze command — dependency analysis + graph export
# ---------------------------------------------------------------

def _cmd_analyze(args):
    """
    Analyse DDL dependencies and generate wave ordering.

    Optionally exports the dependency graph in one or more
    portable formats (DOT, Mermaid, JSON, CSV, OpenLineage)
    when --graph is specified.
    """
    from td_release_packager.analyser import analyse_project, format_summary

    source_dir = args.source
    if not os.path.isdir(source_dir):
        print(f"ERROR: Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    result = analyse_project(source_dir)

    print(f"\n{'=' * 64}")
    print(f"  SHIPS Dependency Analysis")
    print(f"{'=' * 64}")
    print(format_summary(result))

    if not result.objects:
        print("  No DDL objects found. Check the payload directory.")
        print()
        return

    # -- Write _waves.txt -----------------------------------------
    if args.output:
        waves_path = args.output
    else:
        waves_path = os.path.join(source_dir, "_waves.txt")

    if result.waves:
        if os.path.exists(waves_path) and not args.overwrite:
            print(f"\n  ⚠ {waves_path} already exists. Use --overwrite to replace.")
        else:
            with open(waves_path, 'w', encoding='utf-8') as f:
                f.write(result.waves_file_content)
            print(f"\n  ✓ Wave file written: {waves_path}")
            print(f"    {len(result.waves)} waves, {len(result.objects)} objects")

    if result.cycles:
        print(f"\n  ⚠ {len(result.cycles)} cycle(s) detected — review before deploying")

    # -- Export graph (if requested) -------------------------------
    if args.graph:
        _export_graph(result, args)

    print(f"{'=' * 64}\n")


def _export_graph(result, args):
    """
    Export the dependency graph in the requested formats.

    Called by _cmd_analyze when --graph is specified.  Imports
    individual export functions from graph_export and dispatches
    based on --formats.

    Args:
        result: The AnalysisResult from analyse_project.
        args:   Parsed CLI arguments containing graph, formats,
                namespace, project_name, and base_name.
    """
    from td_release_packager.graph_export import (
        export_dot,
        export_mermaid,
        export_json,
        export_csv,
        export_openlineage,
    )

    output_dir = args.graph
    os.makedirs(output_dir, exist_ok=True)

    # -- Parse requested formats ----------------------------------
    requested = {
        f.strip().lower()
        for f in args.formats.split(',')
    }

    # Validate format names
    unknown = requested - set(_GRAPH_FORMATS.keys())
    if unknown:
        print(
            f"  ✗ Unknown graph format(s): "
            f"{', '.join(sorted(unknown))}\n"
            f"    Available: {_ALL_FORMATS}",
        )
        return

    # -- Dispatch to export functions -----------------------------
    # Map format name to its export function.
    # OpenLineage is handled separately (extra parameters).
    exporters = {
        "dot":     export_dot,
        "mermaid": export_mermaid,
        "json":    export_json,
        "csv":     export_csv,
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

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        written.append((fmt, filepath))
        logger.info("Exported %s → %s", fmt, filepath)

    # -- Print export summary -------------------------------------
    count = len(written)
    print(
        f"\n  Graph exported "
        f"({count} format{'s' if count != 1 else ''}):"
    )
    for fmt, filepath in written:
        size_kb = os.path.getsize(filepath) / 1024
        print(f"    ✓ {fmt:<14s} → {filepath} ({size_kb:.1f} KB)")


# ---------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------

def _build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="td_release_packager",
        description="SHIPS — Scaffold, Harvest, Inspect, Package, Ship. "
                    "Standardised Teradata DDL deployment methodology.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    subs = parser.add_subparsers(dest="command")

    # -- scaffold --
    sc = subs.add_parser("scaffold",
                         help="[S] Scaffold — create a new project from template.")
    sc.add_argument("--name", required=True,
                    help="Project name (used as directory name).")
    sc.add_argument("--output", default=".",
                    help="Parent directory (default: current).")
    sc.add_argument("--environments", default="DEV,TST,PRD",
                    help="Comma-separated environment names "
                         "(default: DEV,TST,PRD).")
    sc.add_argument("--repair", action="store_true",
                    help="Repair an existing project — add missing "
                         "directories and files without overwriting "
                         "existing configuration. Use after upgrading "
                         "SHIPS to pick up new directory structure.")

    # -- harvest --
    ig = subs.add_parser("harvest",
                         help="[H] Harvest — import raw DDL files into a project.")
    ig.add_argument("--source", required=True,
                    help="Directory containing raw DDL files.")
    ig.add_argument("--project", required=True,
                    help="Target project directory (must be scaffolded).")
    ig.add_argument("--token-map",
                    help="Path to token_map.conf — applies literal → {{TOKEN}} "
                         "substitutions during harvest. Generate one with "
                         "--generate-token-map first, review it, then pass "
                         "it here.")
    ig.add_argument("--generate-token-map", action="store_true",
                    help="Scan for hardcoded database names and write a "
                         "token_map.conf to the project's config/ directory. "
                         "Requires --env-prefix to derive token names.")
    ig.add_argument("--env-prefix",
                    help="Optional environment prefix to strip when deriving "
                         "token names (e.g. 'A_D01'). Used with "
                         "--generate-token-map to turn 'A_D01_OMR_STD' into "
                         "'{{OMR_STD}}'. If omitted, the full database name "
                         "becomes the token (e.g. 'CORE_STD' → '{{CORE_STD}}').")
    ig.add_argument("--apply-tokens",
                    help="(Legacy) Comma-separated name=token pairs. "
                         "Prefer --token-map instead. "
                         "E.g. 'DEV01_STD={{STD_DATABASE}},DEV01_SEM={{SEM_DATABASE}}'")
    ig.add_argument("--no-detect-tokens", action="store_true",
                    help="Skip hardcoded name detection.")
    ig.add_argument("--force", action="store_true",
                    help="Overwrite existing files in the payload. "
                         "Use when re-harvesting after editing source "
                         "DDL. Warns if overwriting tokenised files "
                         "with non-tokenised content — pass the same "
                         "--token-map to preserve tokenisation.")
    # -- inspect --
    vl = subs.add_parser("inspect",
                         help="[I] Inspect — check DDL against Coding Discipline.")
    vl.add_argument("--source", required=True,
                    help="Directory to validate.")
    vl.add_argument("--config",
                    help="Path to inspect.conf rules configuration file. "
                         "If not specified, auto-detects config/inspect.conf "
                         "within the source project.")
    vl.add_argument("--strict", action="store_true",
                    help="Strict mode: all WARNING rules promoted to ERROR. "
                         "OFF rules remain off.")
    vl.add_argument("--skip-tokens", action="store_true",
                    help="Disable hardcoded name checks (legacy; "
                         "prefer inspect.conf).")
    vl.add_argument("--skip-keywords", action="store_true",
                    help="Disable keyword case checks (legacy; "
                         "prefer inspect.conf).")
    vl.add_argument("--skip-commas", action="store_true",
                    help="Disable leading comma checks (legacy; "
                         "prefer inspect.conf).")

    # -- package --
    bp = subs.add_parser("package", help="[P] Package — build a release package.")
    bp.add_argument("--source", required=True,
                    help="Source project directory.")
    bp.add_argument("--env", required=True,
                    help="Target environment (e.g. DEV, TST, SIT, UAT, PRD).")
    bp.add_argument("--name", required=True,
                    help="Package name (e.g. 'create_objects').")
    bp.add_argument("--properties", required=True,
                    help="Path to environment .properties file.")
    bp.add_argument("--build-number", type=int, default=None,
                    help="Build number (default: auto-increment from .build_counter).")
    bp.add_argument("--no-increment", action="store_true",
                    help="Reuse current build number without incrementing. "
                         "Use when building the same source for a different "
                         "environment (e.g. DEV then PROD).")
    bp.add_argument("--output", default=".",
                    help="Output directory (default: current).")
    bp.add_argument("--format", choices=["zip", "tar.gz"], default="zip",
                    help="Archive format (default: zip).")
    bp.add_argument("--author", help="Builder's name.")
    bp.add_argument("--description", help="Release description.")
    bp.add_argument("--commit", help="Git commit hash.")

    # -- scan --
    sp = subs.add_parser("scan",
                         help="Scan source for token references (part of Inspect).")
    sp.add_argument("--source", required=True,
                    help="Source project directory to scan.")
    sp.add_argument("--properties",
                    help="Optional properties file to validate against.")

    # -- analyze --
    az = subs.add_parser("analyze",
                         help="Analyse DDL dependencies, generate waves, "
                              "and export dependency graph.")
    az.add_argument("--source", required=True,
                    help="Project directory to analyse.")
    az.add_argument("--output",
                    help="Output path for _waves.txt "
                         "(default: <source>/_waves.txt).")
    az.add_argument("--overwrite", action="store_true",
                    help="Overwrite existing _waves.txt.")
    az.add_argument("--graph",
                    metavar="OUTPUT_DIR",
                    help="Export dependency graph to OUTPUT_DIR in one or "
                         "more formats.  Creates the directory if needed.")
    az.add_argument("--formats",
                    default=_ALL_FORMATS,
                    help=f"Comma-separated graph export formats "
                         f"(default: {_ALL_FORMATS}).")
    az.add_argument("--base-name",
                    default="ships_dependencies",
                    help="Base filename for exported graph files "
                         "(default: ships_dependencies).")
    az.add_argument("--namespace",
                    default="teradata://ships-analysis",
                    help="OpenLineage dataset namespace URI.  For a live "
                         "system use teradata://hostname:1025 "
                         "(default: teradata://ships-analysis).")
    az.add_argument("--project-name",
                    default="ships-project",
                    help="OpenLineage job namespace / project name "
                         "(default: ships-project).")

    return parser


if __name__ == "__main__":
    main()
