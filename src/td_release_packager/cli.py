"""
cli.py — Command-line interface for the Teradata Release Packager.

Commands:
    scaffold          Create a new project from template.
    harvest           Import raw DDL files into a project.
    inspect           Check DDL against Coding Discipline + validate grants.
    package           Build a release package for a target environment.
    scan              Scan source files and report all tokens found.
    analyze           Analyse DDL dependencies, generate waves, export graph.
    import-legacy     Import a pre-SHIPS sed substitution script and
                      emit a .properties file plus a migration sed.
    decompose-names   Infer composition roots from literal database
                      names and emit a cascade-form .properties file.

Usage:
    python -m td_release_packager scaffold --name MortgagePlatform --output /projects
    python -m td_release_packager build --source . --env DEV --name create_objects --properties config/properties/DEV.properties
    python -m td_release_packager inspect --source . --fix-grants
    python -m td_release_packager scan --source .
    python -m td_release_packager analyze --source . --graph ./output/
    python -m td_release_packager analyze --source . --graph . --formats dot,json,openlineage
    python -m td_release_packager import-legacy legacy.sh --env DEV --output-dir ./config
    python -m td_release_packager decompose-names token_map.conf --env DEV --output-dir ./config
"""

import argparse
import logging
import os
import sys
from typing import Dict, Optional

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
from td_release_packager.validate_grants import (
    validate_grants,
    fix_grants,
    format_report as format_grant_report,
)

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


# ---------------------------------------------------------------
# Final-summary helpers
# ---------------------------------------------------------------
#
# The inspect command prints lint output (Step 1) before grant output
# (Step 2). When Step 1 produces many issues, the early output scrolls
# off the terminal by the time the final summary appears. These
# helpers re-emit a compact recap at the bottom so failures are
# always visible at a glance.


def _summarise_lint_by_rule(lint_result) -> str:
    """
    Produce a compact ``rule (count), rule (count)`` breakdown of
    ERROR-level issues. Returns empty string if there are no errors.
    """
    if not lint_result.errors:
        return ""
    counts: Dict[str, int] = {}
    for issue in lint_result.issues:
        if issue.severity == "ERROR":
            counts[issue.rule] = counts.get(issue.rule, 0) + 1
    # Sort by count desc, then rule name for stable output
    sorted_rules = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{rule} ({n})" for rule, n in sorted_rules)


def _format_lint_recap(lint_result, max_items: int = 5) -> str:
    """
    Produce a "top N lint errors" recap block. Each entry is a
    file:line and rule name — compact enough that 5 entries fit in
    a typical terminal viewport.

    Returns empty string if there are no errors.
    """
    errors = [i for i in lint_result.issues if i.severity == "ERROR"]
    if not errors:
        return ""

    total = len(errors)
    shown = min(max_items, total)

    lines = []
    if total == shown:
        lines.append(f"  ✗ Lint errors ({total}):")
    else:
        lines.append(f"  ✗ Top {shown} lint errors ({total} total):")

    # Group by file for readability — same file appears once with its
    # issues listed beneath. Limit total displayed errors to max_items.
    by_file: Dict[str, list] = {}
    displayed = 0
    for issue in errors:
        if displayed >= max_items:
            break
        by_file.setdefault(issue.file, []).append(issue)
        displayed += 1

    for file, issues in by_file.items():
        for issue in issues:
            line_part = f":{issue.line}" if issue.line is not None else ""
            lines.append(f"      {file}{line_part}  [{issue.rule}]")

    if shown < total:
        lines.append("")
        lines.append(
            "    Full messages and remaining issues are listed above "
            "(scroll up, or pipe output to a file)."
        )
    return "\n".join(lines)


def _format_grant_recap(grant_result, max_items: int = 10) -> str:
    """
    Produce a recap of grant validation failures. Returns empty
    string if grant_result is None or all grantees are consistent.
    """
    if grant_result is None or grant_result.passed:
        return ""

    drifted = grant_result.drifted
    missing = grant_result.missing
    orphaned = grant_result.orphaned
    total = len(drifted) + len(missing) + len(orphaned)

    if total == 0:
        return ""

    lines = [f"  ✗ Grant issues ({total}):"]

    shown = 0
    for status in drifted:
        if shown >= max_items:
            break
        lines.append(f"      {status.grantee}  [drift]")
        shown += 1
    for status in missing:
        if shown >= max_items:
            break
        lines.append(f"      {status.grantee}  [missing .grt]")
        shown += 1
    for status in orphaned:
        if shown >= max_items:
            break
        lines.append(f"      {status.grantee}  [orphaned .grt]")
        shown += 1

    if shown < total:
        lines.append("")
        lines.append(f"    + {total - shown} more — full details listed above.")
    return "\n".join(lines)


def main():
    """CLI entry point."""
    # Force UTF-8 on stdout/stderr regardless of platform locale.
    # On Windows the default codepage is cp1252, which cannot
    # represent the Unicode glyphs we use for status output (✓, ✗,
    # ↑, →). Without this reconfigure, any subprocess capture or
    # output redirection raises UnicodeEncodeError. Python 3.7+
    # supports reconfigure(); older versions are not supported.
    # errors='replace' is a belt-and-braces fallback for any glyph
    # we might add later that UTF-8 itself can't round-trip on a
    # legacy console — better to print a '?' than to crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # Stream has no reconfigure (very old Python or a
            # custom wrapper) or is already detached.
            pass

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
    elif args.command == "import-legacy":
        _cmd_import_legacy(args)
    elif args.command == "decompose-names":
        _cmd_decompose_names(args)
    elif args.command == "bootstrap-properties":
        _cmd_bootstrap_properties(args)
    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------
# Orchestrator integration helpers
# ---------------------------------------------------------------
#
# Build-order item 4: every stage opens a ``decisions.json`` and
# records its run. Two concerns colliding here:
#
#   1. We don't want a decisions.json appearing in random
#      directories where the user is doing a one-off scan against
#      a non-project tree (litter on disc, surprise artefact).
#
#   2. We want every stage to use the same construction code so
#      the integration is consistent and the duplication doesn't
#      grow as more stages are refactored.
#
# The helpers below solve both: a project-detection check (1) and a
# single context manager every stage uses (2). When the path isn't
# a project the manager yields a no-op recorder, so call sites stay
# simple — they don't branch on "is this a project".


def _looks_like_ships_project(path: str) -> bool:
    """
    Heuristic: does ``path`` look like a SHIPS project root?

    True if it contains either:
      - ``ships.yaml``  (orchestrator config — definitive marker)
      - ``payload/``    (the canonical scaffolded payload tree)

    Used to decide whether a stage should write decisions.json.
    For ad-hoc invocations against a non-project directory we
    yield a no-op recorder so the file doesn't appear.
    """
    if not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "ships.yaml")):
        return True
    if os.path.isdir(os.path.join(path, "payload")):
        return True
    return False


class _NullStageRecorder:
    """
    Drop-in for ``StageRecorder`` that ignores every call.

    Used by ``_stage_recording`` when the target directory isn't a
    SHIPS project. Lets the same stage code run end-to-end without
    branching on whether decisions.json is being written.
    """

    def set_status(self, status: str) -> None:
        pass

    def set_config_resolved(
        self,
        name: str,
        value,
        source: str,
        source_path: str,
    ) -> None:
        pass

    def set_inputs(self, **fields) -> None:
        pass

    def set_outputs(self, **fields) -> None:
        pass

    def set_decisions(self, **fields) -> None:
        pass

    def add_issue(
        self,
        severity: str,
        code: str,
        message: str,
        location=None,
    ) -> None:
        pass


def _stage_recording(project_dir: str, stage_name: str):
    """
    Context manager: yield a stage recorder for ``stage_name`` rooted
    at ``project_dir``.

    If ``project_dir`` looks like a SHIPS project, opens
    ``<project_dir>/decisions.json`` and yields a real
    ``StageRecorder``. Otherwise yields a ``_NullStageRecorder`` so
    ad-hoc one-off invocations don't litter the filesystem.

    Usage::

        with _stage_recording(args.source, "scan") as stage:
            stage.set_config_resolved(...)
            ...

    The yielded object always supports the StageRecorder interface
    so call sites don't need to branch.
    """
    from contextlib import contextmanager

    from td_release_packager.orchestrator import (
        DECISIONS_FILENAME,
        DecisionsManifest,
    )

    @contextmanager
    def _ctx():
        if not _looks_like_ships_project(project_dir):
            yield _NullStageRecorder()
            return

        manifest_path = os.path.join(project_dir, DECISIONS_FILENAME)
        manifest = DecisionsManifest(manifest_path)
        with manifest.run(stage_name) as run:
            with run.stage(stage_name) as stage:
                yield stage

    return _ctx()


# ---------------------------------------------------------------
# Harvest "Next Steps" banner
# ---------------------------------------------------------------


def _project_has_env_properties(project_dir: str) -> bool:
    """True if ``<project>/config/properties/`` contains any
    ``*.properties`` file. Used to pick between the 'bootstrap'
    and 'verify' wording in the harvest banner."""
    props_dir = os.path.join(project_dir, "config", "properties")
    if not os.path.isdir(props_dir):
        return False
    return any(
        f.endswith(".properties") for f in os.listdir(props_dir)
    )


def _print_harvest_next_steps(
    args,
    *,
    generated_token_map_path: Optional[str],
    substitutions_applied: bool,
    already_tokenised: bool = False,
) -> None:
    """
    Print a context-aware Next Steps banner after harvest.

    Four flows, four distinct next-step lists:

      A. ``--generate-token-map`` was used AND literals were found.
         Token map was written; substitutions not yet applied. User
         needs to review the map, bootstrap properties, re-harvest
         to apply, then validate + package.

      B. ``--token-map`` (or ``--apply-tokens``) was provided.
         Substitutions baked into the source. User just validates
         and packages.

      C. Plain harvest, no token activity. Same as flow B.

      D. ``--generate-token-map`` was used but NO literals found.
         The source is already tokenised — user skips the token-map
         dance entirely and goes straight to bootstrap-properties.

    Args:
        args: The parsed CLI args (used for ``args.project``).
        generated_token_map_path: Path to the token_map.conf that
            ``--generate-token-map`` just wrote, or None if no map
            was generated this run.
        substitutions_applied: True if harvest applied substitutions
            via ``--token-map`` or ``--apply-tokens`` this run.
        already_tokenised: True when ``--generate-token-map`` was
            requested but the source had no literals to map. The
            source is already in the end-state — route to
            bootstrap-properties.
    """
    from typing import List

    print("=" * 64)
    print("  Next Steps")
    print("=" * 64)

    project = args.project
    has_props = _project_has_env_properties(project)

    # Lead with stage label + state line so the user knows where
    # they are before they see the steps. Same shape across all
    # four flows.
    print()
    print("  You are here:  [H] Harvest complete")
    if already_tokenised:
        state = "source already tokenised; .properties not yet defined"
    elif generated_token_map_path:
        state = "literals scanned; token map written; substitutions NOT applied"
    elif substitutions_applied:
        state = "source tokenised via --token-map; substitutions applied"
    else:
        state = "source ingested; no token activity this run"
    print(f"  Project state: {state}")
    print()

    steps: List[str] = []

    # Quality-gate block — appears in every flow before packaging.
    # 'inspect' is part of the canonical S-H-I-P-S workflow;
    # 'analyze' produces dependency waves for parallel deploy
    # (optional but recommended); 'scan' catches {{TOKEN}}
    # references that have no value in the .properties file.
    def _quality_gates_step(num: int) -> str:
        return (
            f"{num}. Validate the harvested DDL before packaging:\n"
            f"\n"
            f"     python -m td_release_packager inspect \\\n"
            f"         --source {project}\n"
            f"\n"
            f"     python -m td_release_packager analyze \\\n"
            f"         --source {project}            "
            f"# optional, deploy waves\n"
            f"\n"
            f"     python -m td_release_packager scan \\\n"
            f"         --source {project} \\\n"
            f"         --properties config/properties/DEV.properties\n"
            f"\n"
            f"   inspect lints the DDL and validates grants;\n"
            f"   analyze produces dependency waves for parallel deploy;\n"
            f"   scan confirms every {{{{TOKEN}}}} in source has a value."
        )

    def _verify_props_step(num: int) -> str:
        return (
            f"{num}. Verify environment properties match your topology:\n"
            f"\n"
            f"     • SHIPS_ENV       matches the target environment\n"
            f"     • ENV_PREFIX      matches your platform topology\n"
            f"     • SHIPS_PROJECT   identifies your project\n"
            f"     • INSTANCE        00 unless deploying in parallel\n"
            f"     • SECURITY_TIER   0 unless handling restricted data\n"
            f"\n"
            f"   All other tokens derive from these roots automatically."
        )

    def _package_step(num: int) -> str:
        return (
            f"{num}. Package for an environment (example: DEV):\n"
            f"\n"
            f"     python -m td_release_packager package \\\n"
            f"         --source {project} --env DEV --name <name> \\\n"
            f"         --properties config/properties/DEV.properties \\\n"
            f"         --output releases/"
        )

    if already_tokenised:
        # Flow D — source already uses {{TOKEN}} references. Skip
        # the token map entirely and bootstrap properties directly
        # from the tokens the source already references.
        bootstrap_cmd_parts = [
            f"     python -m td_release_packager bootstrap-properties \\\n"
            f"         --source {project} \\\n"
            f"         --env DEV"
        ]
        if not has_props:
            steps.append(
                f"1. Bootstrap a .properties file from the tokens the\n"
                f"   source already references:\n"
                f"\n"
                f"{bootstrap_cmd_parts[0]}\n"
                f"\n"
                f"   Output: a 7-section .properties scaffold under\n"
                f"   {project}\\config\\properties\\DEV.properties\n"
                f"   with every {{{{TOKEN}}}} parked in section 8\n"
                f"   for you to re-section by cut-and-paste."
            )
        else:
            steps.append(
                f"1. (Optional) Refresh the existing .properties scaffold\n"
                f"   to pick up any newly-referenced tokens:\n"
                f"\n"
                f"{bootstrap_cmd_parts[0]} --force\n"
                f"\n"
                f"   --force is required because the file already exists.\n"
                f"   Existing values for still-referenced tokens are\n"
                f"   preserved; new tokens are added to section 8."
            )
        steps.append(_quality_gates_step(2))
        steps.append(_verify_props_step(3))
        steps.append(_package_step(4))

    elif generated_token_map_path is not None:
        # Flow A — token map was just written, substitutions not applied
        steps.append(
            f"1. Review the generated token map:\n"
            f"     {generated_token_map_path}\n"
            f"\n"
            f"   Each line is LITERAL_DB_NAME={{{{TOKEN}}}}. Edit\n"
            f"   token names if you'd prefer different conventions.\n"
            f"   Lines you want to skip can be deleted or commented (#)."
        )
        if not has_props:
            steps.append(
                f"2. Bootstrap a .properties file from the token map:\n"
                f"\n"
                f"     python -m td_release_packager decompose-names \\\n"
                f"         {generated_token_map_path} \\\n"
                f"         --env DEV \\\n"
                f"         --output-dir {project}\\config\n"
                f"\n"
                f"   Output: a 7-section .properties scaffold under\n"
                f"   {project}\\config\\properties\\DEV.properties\n"
                f"   plus a decomposition_report.md with confidence\n"
                f"   ratings and outliers."
            )
            next_num = 3
        else:
            next_num = 2
        steps.append(
            f"{next_num}. Re-harvest with the token map applied:\n"
            f"\n"
            f"     python -m td_release_packager harvest \\\n"
            f"         --source <legacy_src> \\\n"
            f"         --project {project} \\\n"
            f"         --token-map {generated_token_map_path} \\\n"
            f"         --force\n"
            f"\n"
            f"   This rewrites the staged DDL to use {{{{TOKEN}}}} form."
        )
        next_num += 1
        steps.append(_quality_gates_step(next_num))
        next_num += 1
        steps.append(_verify_props_step(next_num))
        next_num += 1
        steps.append(_package_step(next_num))

    else:
        # Flow B / C — substitutions applied (B) or no map activity (C).
        # Same steps either way: validate, verify properties, package.
        steps.append(_quality_gates_step(1))
        steps.append(_verify_props_step(2))
        steps.append(_package_step(3))

    for step in steps:
        print()
        # Indent each line with two spaces for the banner block.
        for line in step.splitlines():
            print(f"  {line}" if line else "")

    print(f"\n{'=' * 64}\n")


# ---------------------------------------------------------------
# Legacy-importer / decomposer dispatchers
# ---------------------------------------------------------------
#
# Both tools have a ``main(argv)`` entry point in the package that
# accepts argparse-style argument lists. We reconstruct the argv
# from the parsed top-level args and delegate. Keeping the engines'
# main() functions as the single source of truth means the CLI and
# the standalone tools/ shims behave identically.


def _cmd_import_legacy(args):
    """Dispatch to td_release_packager.legacy_importer.main()."""
    from td_release_packager.legacy_importer import main as importer_main

    argv = [args.input, "--env", args.env, "--output-dir", args.output_dir]
    if args.verbose:
        argv.append("-v")
    sys.exit(importer_main(argv))


def _cmd_decompose_names(args):
    """Dispatch to td_release_packager.decomposer.main()."""
    from td_release_packager.decomposer import main as decomposer_main

    argv = [args.input, "--env", args.env, "--output-dir", args.output_dir]
    if args.verbose:
        argv.append("-v")
    sys.exit(decomposer_main(argv))


def _cmd_bootstrap_properties(args):
    """Dispatch to td_release_packager.properties_bootstrapper.main()."""
    from td_release_packager.properties_bootstrapper import main as bootstrap_main

    argv = ["--source", args.source, "--env", args.env]
    if args.output_dir:
        argv.extend(["--output-dir", args.output_dir])
    if args.force:
        argv.append("--force")
    if args.verbose:
        argv.append("-v")
    sys.exit(bootstrap_main(argv))


# ---------------------------------------------------------------
# Path resolution helper
# ---------------------------------------------------------------


def _resolve_path(
    path: str,
    relative_to: str = None,
    label: str = "file",
) -> str:
    """
    Resolve a file path, trying multiple strategies.

    Resolution order:
        1. The path as given (absolute or relative to CWD)
        2. The path relative to the --project / --source directory
        3. If neither exists, report both locations tried

    Args:
        path:        The path as provided by the user.
        relative_to: A base directory to try if the path is relative
                     and not found at the CWD (e.g. the --project dir).
        label:       A human-readable label for the path (e.g. '--token-map')
                     used in error messages.

    Returns:
        The resolved absolute path.

    Raises:
        SystemExit: If the file is not found at any location tried.
    """
    # Strategy 1: path as given
    if os.path.isfile(path):
        return os.path.abspath(path)

    # Strategy 2: path relative to the project/source directory
    if relative_to and not os.path.isabs(path):
        project_relative = os.path.join(relative_to, path)
        if os.path.isfile(project_relative):
            return os.path.abspath(project_relative)

    # Neither worked — build a helpful error message
    cwd = os.getcwd()
    tried = [f"    {os.path.abspath(path)}"]
    if relative_to and not os.path.isabs(path):
        tried.append(f"    {os.path.abspath(os.path.join(relative_to, path))}")

    print(
        f"\nERROR: {label} file not found: {path}\n"
        f"\n"
        f"  Looked in:\n" + "\n".join(f"  {t}" for t in tried) + f"\n\n"
        f"  Current directory: {cwd}\n"
        f"\n"
        f"  Tip: use an absolute path, or place the file inside\n"
        f"  the project directory and reference it with a relative\n"
        f"  path (e.g. config\\token_map.conf).",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------
# Commands
# ---------------------------------------------------------------


def _cmd_scaffold(args):
    """Create a new project from template, or repair an existing one."""
    envs = [e.strip().upper() for e in args.environments.split(",")]
    repair = getattr(args, "repair", False)

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
            print("\n  Repair complete. Missing directories and files have")
            print("  been created. Existing files were NOT overwritten.")
        else:
            print("\n  SHIPS workflow — next steps:")
            print("    [S] Scaffold  ✓ Done")
            print("    [H] Harvest   python -m td_release_packager harvest \\")
            print(f"                    --source /raw/ddl/ --project {project_dir}")
            print("    [I] Inspect   python -m td_release_packager inspect \\")
            print(f"                    --source {project_dir}")
            print("    [P] Package   python -m td_release_packager package \\")
            print(
                f"                    --source {project_dir} --env DEV --name {args.name} \\"
            )
            print("                    --properties config/properties/DEV.properties")
            print("    [S] Ship      python deploy.py --host <host> --user <user>")

        print(f"{'=' * 64}\n")

    except FileExistsError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(
            "  Tip: use --repair to add missing directories and files", file=sys.stderr
        )
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_ingest(args):
    """Import raw DDL files into a project."""
    # -- Reconcile mode short-circuits the normal harvest pipeline --
    # The user has asked us to clean up twin file pairs, not harvest
    # new DDL. Dispatch and return before any of the ingest logic
    # runs.
    if getattr(args, "reconcile", False):
        _cmd_harvest_reconcile(args)
        return

    # --source is required for normal harvest mode. argparse marks it
    # optional so that --reconcile can run without it; we enforce the
    # requirement here for the non-reconcile path.
    if not args.source:
        print(
            "\nERROR: --source is required for normal harvest mode.\n"
            "  Pass --reconcile to run reconciliation without --source.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Build apply_tokens dict from available sources --
    apply_tokens = None

    # Option 1: --token-map file (preferred)
    if hasattr(args, "token_map") and args.token_map:
        token_map_path = _resolve_path(
            args.token_map,
            relative_to=args.project,
            label="--token-map",
        )
        apply_tokens = read_token_map(token_map_path)

    # Option 2: --apply-tokens inline pairs (legacy)
    elif hasattr(args, "apply_tokens") and args.apply_tokens:
        apply_tokens = {}
        for pair in args.apply_tokens.split(","):
            if "=" not in pair:
                continue
            literal, token = pair.split("=", 1)
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
        print("  DDL Harvest Results")
        print(f"{'=' * 64}")
        print(f"  Source:           {args.source}")
        print(f"  Project:          {args.project}")
        if args.force:
            print("  Mode:             FORCE (overwrite existing)")
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
            print("\n  Files placed:")
            for src, dest, obj_type in result.files_placed:
                print(f"    {obj_type:15s} {src}")
                print(f"    {'':15s} → {dest}")

        if result.unclassified_files:
            print("\n  Unclassified files (manual review needed):")
            for f in result.unclassified_files:
                print(f"    ⚠ {f}")

        # -- Generate token map if requested --
        env_prefix = getattr(args, "env_prefix", None)
        generate_map = getattr(args, "generate_token_map", False)

        # Track the generated token-map path so the Next Steps
        # banner can reference it without re-deriving it. None when
        # --generate-token-map was not used (or produced nothing).
        generated_token_map_path = None

        if generate_map and result.token_candidates:
            token_map = generate_token_map(result.token_candidates, env_prefix)
            map_path = os.path.join(args.project, "config", "token_map.conf")
            write_token_map(
                map_path, token_map, result.token_candidates, env_prefix or "(none)"
            )
            generated_token_map_path = map_path

            # Header — prominent path so it's the first thing the
            # user sees in the token-map block, before any per-
            # mapping listing pushes it off-screen.
            print()
            print("  +-- Token map ----------------------------------------------+")
            print(f"  |   Path:     {map_path}")
            print(f"  |   Mappings: {len(token_map)}")
            if env_prefix:
                print(f"  |   Prefix:   {env_prefix} (stripped from token names)")
            else:
                print("  |   Prefix:   none (full names used as tokens)")
            print("  +------------------------------------------------------------+")

            # Sample of mappings — capped to keep the output short.
            # Anything past the cap stays in the file; the user can
            # cat / open the path printed above to see them all.
            CAP = 10
            sorted_mappings = sorted(token_map.items())
            print(f"\n  Sample mappings (showing {min(CAP, len(token_map))} of {len(token_map)}):")
            for literal, token in sorted_mappings[:CAP]:
                files = result.token_candidates.get(literal, [])
                print(f"    {literal} → {token}  ({len(files)} refs)")
            if len(token_map) > CAP:
                print(f"    ... {len(token_map) - CAP} more — see the file above.")

            # Footer — repeat the path as the LAST line of the block
            # so even if the listing is long the user finds it again
            # right above the Next Steps banner.
            print(f"\n  ✓ Token map written to: {map_path}")

        elif generate_map and not result.token_candidates:
            # User asked for a token map but no hardcoded names were
            # detected. The most common cause is that the source is
            # ALREADY TOKENISED — the end-state most users have to
            # work toward. Tell them clearly, and route them to
            # bootstrap-properties (the third bootstrap path) since
            # they no longer need a token map at all.
            print(
                "\n  ✓ No hardcoded database names detected.\n"
                "    The source DDL appears to be already tokenised — you're at\n"
                "    the end-state most projects have to work toward. Skip the\n"
                "    token map and go straight to .properties bootstrap below."
            )

        elif result.token_candidates and not apply_tokens:
            print("\n  Token candidates (hardcoded database names):")
            for db_name, files in sorted(result.token_candidates.items()):
                print(f"    '{db_name}' ({len(files)} refs)")
            if not generate_map:
                print("\n  Tip: re-run with --generate-token-map --env-prefix <PREFIX>")
                print("  to auto-generate a token mapping file.")

        if result.warnings:
            print("\n  Warnings:")
            for w in result.warnings:
                print(f"    ⚠ {w}")

        # -- Classification warnings (from the rich classifier) --
        # Filename mismatches and unrecognised externals get their
        # own section so they don't drown in the generic warnings
        # list. These are the "you're going to want to act on this"
        # diagnostics — surface them prominently.
        if result.classification_warnings:
            print("\n  Classification warnings:")
            for w in result.classification_warnings:
                print(f"    ⚠ {w}")

        # -- Sub-types detected --
        # Show counts per sub-type so users see at a glance how
        # many C UDFs / Java procedures the harvester recognised.
        if result.subtypes:
            from collections import Counter

            subtype_counts = Counter(result.subtypes.values())
            print("\n  Sub-types detected:")
            for subtype, count in sorted(subtype_counts.items()):
                print(f"    {subtype:20s} {count}")

        # -- External references --
        # FUNCTION_C → .c/.h paths; PROCEDURE_JAVA → JAR alias.
        # Capped to keep banner short — full list is in the
        # decisions.json once item 4 of the orchestrator wires
        # ingest into the recording context.
        if result.external_references:
            print("\n  External references discovered:")
            for staged_path, refs in sorted(
                result.external_references.items()
            )[:5]:
                print(f"    {staged_path}")
                for ref in refs:
                    print(f"        → {ref}")
            extra = len(result.external_references) - 5
            if extra > 0:
                print(f"    ... and {extra} more file(s) with externals.")

        # -- Binary artefacts physically copied into the payload --
        # JAR archives (for SQLJ install) and C source/header files
        # (for C UDFs) get copied alongside their SQL scripts so
        # the deployer can ship them. Show counts per kind plus a
        # sample.
        if result.binaries_placed:
            from collections import Counter

            kind_counts = Counter(k for _, _, k in result.binaries_placed)
            print("\n  Binary artefacts copied into payload:")
            for kind, count in sorted(kind_counts.items()):
                print(f"    {kind:14s} {count}")
            print()
            for src, dest, kind in result.binaries_placed[:5]:
                print(f"    {kind}  {os.path.basename(src)}")
                print(f"      → {dest}")
            extra = len(result.binaries_placed) - 5
            if extra > 0:
                print(f"    ... and {extra} more binary file(s).")

        if result.errors:
            print("\n  Errors:")
            for e in result.errors:
                print(f"    ✗ {e}")

        print(f"{'=' * 64}\n")

        # -- Next Steps banner --
        # Four distinct flows produce four distinct sets of next
        # steps. Get the recommendation right per flow so the user
        # isn't left guessing.
        _print_harvest_next_steps(
            args=args,
            generated_token_map_path=generated_token_map_path,
            substitutions_applied=bool(apply_tokens),
            already_tokenised=(generate_map and not result.token_candidates),
        )

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_harvest_reconcile(args):
    """
    Drive interactive twin-pair reconciliation for the harvested tree.

    Detects literal/tokenised twin file pairs in the project's
    payload/database/DDL/ directory and prompts the user to resolve
    each pair. Both a human-readable summary banner (stdout) and a
    machine-readable JSON audit record are produced.

    A "twin pair" is two DDL files that resolve to the same package
    destination at build time — typically a literal-named survivor
    from a pre-tokenisation harvest sitting alongside its tokenised
    counterpart (e.g. ``MortgagePlatform_Domain_V.X.viw`` next to
    ``{{DOM_DATABASE_V}}.X.viw``). The builder treats these as
    duplicate-path collisions and aborts.

    Exit codes:
        0 — clean completion (no errors, no early quit with pending)
        1 — error during file operations or missing prerequisites
        2 — quit early with pairs still pending, OR non-TTY refusal
    """
    from datetime import datetime, timezone
    from pathlib import Path

    from td_release_packager import reconcile as _reconcile
    from td_release_packager.builder import _find_payload_dir

    project_dir = args.project

    # -- Locate the harvested DDL tree --
    # The reconciler walks payload/database/DDL/ specifically, since
    # twins only exist among DDL artefacts. _find_payload_dir owns
    # discovery of the payload root; we append the DDL subpath.
    try:
        payload_root = Path(_find_payload_dir(project_dir))
    except FileNotFoundError as exc:
        print(
            f"\nERROR: payload directory not found under {project_dir}.\n"
            f"  {exc}\n\n"
            f"  Tip: harvest must be run before reconcile. Run a "
            f"normal harvest first to populate payload/database/DDL/.",
            file=sys.stderr,
        )
        sys.exit(1)

    ddl_dir = payload_root / "database" / "DDL"
    if not ddl_dir.exists():
        print(
            f"\nERROR: DDL tree not found at {ddl_dir}.\n\n"
            f"  Tip: this directory is created by the harvest step. "
            f"Run a normal harvest first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- token_map.conf is required --
    # Reconcile uses it to identify which literal prefixes have a
    # tokenised counterpart. Without the map there's no way to
    # classify a twin pair.
    token_map_path = Path(project_dir) / "config" / "token_map.conf"
    if not token_map_path.exists():
        print(
            f"\n[{_reconcile.ERR_NO_TOKEN_MAP}] token_map.conf not found "
            f"at {token_map_path}.\n"
            f"  Reconciliation requires the token map to identify "
            f"twins. Generate one with:\n"
            f"    td_release_packager harvest --source <raw_dir> "
            f"--project {project_dir} \\\n"
            f"      --generate-token-map --env-prefix <PREFIX>",
            file=sys.stderr,
        )
        sys.exit(1)

    token_map = read_token_map(str(token_map_path))

    # -- Resolve JSON audit destination --
    # Default: <project>/logs/reconcile_<UTC_timestamp>.json. The
    # timestamp uses %Y%m%dT%H%M%SZ (no colons) so the path is safe
    # on Windows filesystems. --json-out overrides; relative paths
    # resolve under --project.
    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = Path(project_dir) / args.json_out
        json_output_path = out_path
    else:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_output_path = Path(project_dir) / "logs" / f"reconcile_{timestamp}.json"

    # -- Drive the session --
    try:
        result = _reconcile.run_interactive_reconciliation(
            project_root=Path(project_dir),
            payload_dir=ddl_dir,
            token_map=token_map,
            token_map_path=token_map_path,
            json_output_path=json_output_path,
        )
    except RuntimeError as exc:
        # Non-TTY refusal — surface the formatted message verbatim
        # (already includes the [E_NOT_INTERACTIVE] reference ID).
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(2)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        # Missing payload dir, three-way collision, etc. The message
        # from reconcile already carries an [E_*] reference ID where
        # applicable.
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # -- Summary --
    print()
    print(_reconcile.format_summary_banner(result))
    print(f"  Audit JSON: {json_output_path}\n")

    # -- Exit code policy --
    # 1 if any delete failed; 2 if user quit with pairs still
    # pending; 0 otherwise. Distinguishing "quit with pending" from
    # "completed cleanly" lets calling scripts decide whether
    # re-running is warranted.
    if result.error_count > 0:
        sys.exit(1)
    if result.quit_early and len(result.resolutions) < len(result.pairs):
        sys.exit(2)


def _cmd_validate(args):
    """
    Validate DDL files against the Coding Discipline.

    Runs two steps:
        Step 1 — Per-file DDL lint (validate.py)
        Step 2 — Cross-file grant validation (validate_grants.py)

    The overall result is PASSED only if both steps pass.
    """
    from pathlib import Path

    try:
        # ==============================================================
        # Step 0 — Token format check
        # ==============================================================
        # Catches malformed {{...}} markers (whitespace inside braces,
        # double-tokenisation from a re-run harvest, orphan braces from
        # editor mishaps) BEFORE downstream rules look at the same files.
        # Malformed tokens silently survive substitution and end up in
        # the deployed SQL — finding them at inspect time means the
        # developer fixes them once, not at every build attempt.
        from td_release_packager.token_engine import (
            scan_malformed_tokens_in_directory,
            format_malformed_tokens_report,
        )
        from td_release_packager.builder import _find_payload_dir

        try:
            payload_dir = _find_payload_dir(args.source)
        except FileNotFoundError:
            # No payload dir — fall back to scanning the source root.
            # Hidden/underscore-prefixed files are skipped by the
            # scanner's own rules, so this is safe even if args.source
            # is broader than expected.
            payload_dir = args.source

        token_findings = scan_malformed_tokens_in_directory(payload_dir)
        token_ok = not token_findings

        token_icon = "✓" if token_ok else "✗"
        token_status = "PASSED" if token_ok else "FAILED"

        print(f"\n{'=' * 64}")
        print(f"  {token_icon} Step 0: Token Format Check — {token_status}")
        print(f"{'=' * 64}")

        if token_findings:
            n_files = len(token_findings)
            n_issues = sum(len(v) for v in token_findings.values())
            print(f"  Files with malformed tokens: {n_files}")
            print(f"  Total malformed markers:     {n_issues}")
            print()
            # The format function emits its own banner+detail block.
            print(format_malformed_tokens_report(token_findings))
        else:
            print("  All {{TOKEN}} markers are well-formed.")

        # ==============================================================
        # Step 1 — Per-file DDL lint
        # ==============================================================

        # -- Load rules config --
        rules_config = None
        if hasattr(args, "config") and args.config:
            config_path = _resolve_path(
                args.config,
                relative_to=args.source,
                label="--config",
            )
            rules_config = read_inspect_config(config_path)
        else:
            # Auto-detect config in project's config/ directory
            auto_config = os.path.join(args.source, "config", "inspect.conf")
            if os.path.exists(auto_config):
                rules_config = read_inspect_config(auto_config)

        # -- Apply legacy --skip-* flags as overrides --
        if rules_config is None:
            from td_release_packager.validate import DEFAULT_RULES

            rules_config = dict(DEFAULT_RULES)

        if hasattr(args, "skip_tokens") and args.skip_tokens:
            rules_config["hardcoded_name"] = "OFF"
        if hasattr(args, "skip_keywords") and args.skip_keywords:
            rules_config["keyword_case"] = "OFF"
        if hasattr(args, "skip_commas") and args.skip_commas:
            rules_config["leading_commas"] = "OFF"

        lint_result = validate_directory(
            source_dir=args.source,
            rules_config=rules_config,
            strict=args.strict,
        )

        lint_icon = "✓" if lint_result.passed else "✗"
        lint_status = "PASSED" if lint_result.passed else "FAILED"
        mode = " (strict)" if args.strict else ""

        print(f"\n{'=' * 64}")
        print(f"  {lint_icon} Step 1: Coding Discipline Lint — {lint_status}{mode}")
        print(f"{'=' * 64}")
        print(f"  Files scanned:    {lint_result.files_scanned}")
        print(f"  Files passed:     {lint_result.files_passed}")
        print(f"  Files with issues:{lint_result.files_with_issues}")
        print(f"  Errors:           {lint_result.errors}")
        print(f"  Warnings:         {lint_result.warnings}")

        if lint_result.issues:
            # Group by file
            by_file = {}
            for issue in lint_result.issues:
                by_file.setdefault(issue.file, []).append(issue)

            print("\n  Issues by file:")
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

        if lint_result.passed:
            print("\n  All files conform to the Teradata Engineering Discipline.")

        print(f"{'=' * 64}")

        # ==============================================================
        # Step 2 — Cross-file grant validation
        # ==============================================================

        skip_grants = getattr(args, "skip_grants", False)
        do_fix = getattr(args, "fix_grants", False)
        dcl_dir = None
        if hasattr(args, "dcl_dir") and args.dcl_dir:
            dcl_dir = Path(args.dcl_dir)

        project_dir = Path(args.source).resolve()
        grant_result = None

        if skip_grants:
            print("\n  ℹ Grant validation skipped (--skip-grants)")
        elif do_fix:
            # -- Fix mode: generate/update .grt files --
            grant_result, files_written = fix_grants(
                project_dir,
                dcl_dir=dcl_dir,
                verbose=args.verbose,
            )

            grant_icon = "✓" if grant_result.passed else "✗"
            grant_status = "PASSED" if grant_result.passed else "FAILED"

            print(f"\n{'=' * 64}")
            print(
                f"  {grant_icon} Step 2: Grant Validation — {grant_status} (--fix-grants)"
            )
            print(f"{'=' * 64}")
            print(f"  .grt files written: {files_written}")
            print(format_grant_report(grant_result))
            print(f"{'=' * 64}")
        else:
            # -- Validate mode: compare and report --
            grant_result = validate_grants(
                project_dir,
                dcl_dir=dcl_dir,
                verbose=args.verbose,
            )

            grant_icon = "✓" if grant_result.passed else "✗"
            grant_status = "PASSED" if grant_result.passed else "FAILED"

            print(f"\n{'=' * 64}")
            print(f"  {grant_icon} Step 2: Grant Validation — {grant_status}")
            print(f"{'=' * 64}")
            print(format_grant_report(grant_result))
            print(f"{'=' * 64}")

        # ==============================================================
        # Overall result
        # ==============================================================

        lint_ok = lint_result.passed
        grant_ok = grant_result.passed if grant_result else True
        overall_ok = token_ok and lint_ok and grant_ok

        overall_icon = "✓" if overall_ok else "✗"
        overall_status = "PASSED" if overall_ok else "FAILED"

        print(f"\n{'=' * 64}")
        print(f"  {overall_icon} SHIPS Inspect — {overall_status}")
        print(f"{'=' * 64}")

        # -- Step 0 line: token format check --
        if token_ok:
            print("  Step 0 (Tokens): PASSED")
        else:
            n_files = len(token_findings)
            n_issues = sum(len(v) for v in token_findings.values())
            print(
                f"  Step 0 (Tokens): FAILED — "
                f"{n_issues} malformed marker(s) in {n_files} file(s)"
            )

        # -- Step 1 line: status, error/warning counts, by-rule breakdown
        if lint_ok:
            warning_note = (
                f" — {lint_result.warnings} warnings" if lint_result.warnings else ""
            )
            print(f"  Step 1 (Lint):   PASSED{warning_note}")
        else:
            print(
                f"  Step 1 (Lint):   FAILED — "
                f"{lint_result.errors} errors, {lint_result.warnings} warnings"
            )
            by_rule = _summarise_lint_by_rule(lint_result)
            if by_rule:
                print(f"                   Errors by rule: {by_rule}")

        # -- Step 2 line
        if skip_grants:
            print("  Step 2 (Grants): SKIPPED")
        elif grant_ok:
            n = len(grant_result.consistent) if grant_result else 0
            print(f"  Step 2 (Grants): PASSED — {n} grantees consistent")
        else:
            d = len(grant_result.drifted)
            m = len(grant_result.missing)
            o = len(grant_result.orphaned)
            print(f"  Step 2 (Grants): FAILED — {d} drifted, {m} missing, {o} orphaned")

        # -- Top-failures recap: keeps actionable detail visible even
        #    when the long per-file output has scrolled off the terminal.
        if not lint_ok:
            recap = _format_lint_recap(lint_result)
            if recap:
                print()
                print(recap)

        if not grant_ok:
            recap = _format_grant_recap(grant_result)
            if recap:
                print()
                print(recap)

        print(f"{'=' * 64}\n")

        sys.exit(0 if overall_ok else 1)

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_build(args):
    """Build a release package."""
    # -- Resolve properties file path --
    properties_path = _resolve_path(
        args.properties,
        relative_to=args.source,
        label="--properties",
    )
    args.properties = properties_path

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
        print("  ✓ Package built successfully")
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
            print("\n  Warnings:")
            for w in manifest.warnings:
                print(f"    ⚠ {w}")

        print()

    except (FileNotFoundError, ValueError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_scan(args):
    """
    Scan payload files for token references.

    Pilot for build-order item 4 — refactored onto the orchestrator
    foundation. ``_stage_recording`` decides whether to open a real
    ``decisions.json`` (when running inside a SHIPS project) or a
    no-op recorder (for ad-hoc scans against arbitrary directories).
    The stdout output is identical in both cases.
    """
    from td_release_packager.orchestrator import issue_codes

    source_dir = args.source
    if not os.path.isdir(source_dir):
        print(f"ERROR: Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # Only scan the payload — not config/, releases/, README, etc.
    scan_dir = source_dir
    for candidate in ["payload/database", "payload"]:
        path = os.path.join(source_dir, candidate)
        if os.path.isdir(path):
            scan_dir = path
            break

    if scan_dir == source_dir:
        print("  ⚠ No payload/ directory found — scanning entire project")

    with _stage_recording(source_dir, "scan") as stage:
        # Cascade for `scan` is trivial today — no ships.yaml,
        # template, or env-properties contributions yet. Every
        # setting comes from CLI (Layer 5). Recording the provenance
        # here so future cascade integration just plugs in
        # additional layers without changing downstream consumers.
        stage.set_config_resolved("source", source_dir, "layer-5", "cli")
        stage.set_config_resolved(
            "properties",
            args.properties or None,
            "layer-5",
            "cli",
        )

        usage = scan_tokens_in_directory(scan_dir)

        all_tokens = set()
        for tokens in usage.values():
            all_tokens.update(tokens)

        stage.set_inputs(
            scan_directory=scan_dir,
            files_with_tokens=len(usage),
        )
        stage.set_outputs(
            unique_tokens=len(all_tokens),
            tokens=sorted(all_tokens),
        )

        print(f"\n{'=' * 64}")
        print(f"  Token Scan: {scan_dir}")
        print(f"{'=' * 64}")
        print(f"  Files with tokens: {len(usage)}")
        print(f"  Unique tokens:     {len(all_tokens)}")

        if all_tokens:
            print("\n  Tokens found:")
            for t in sorted(all_tokens):
                files = [f for f, tokens in usage.items() if t in tokens]
                print(f"    {{{{{t}}}}} — used in {len(files)} file(s)")

        # Validate against properties if provided
        if args.properties:
            try:
                values = read_properties(args.properties)
                errors, warnings = validate_tokens(values, usage)

                for e in errors:
                    stage.add_issue("error", issue_codes.TOKEN_UNDEFINED, e)
                for w in warnings:
                    stage.add_issue("warning", issue_codes.TOKEN_UNUSED, w)

                if errors:
                    print("\n  Validation ERRORS:")
                    for e in errors:
                        print(f"    ✗ {e}")

                if warnings:
                    print("\n  Validation WARNINGS:")
                    for w in warnings:
                        print(f"    ⚠ {w}")

                if not errors and not warnings:
                    print(f"\n  ✓ All tokens validated against {args.properties}")

                # Stage status: error issues auto-upgrade to "error"
                # via the recorder; warnings need an explicit set.
                if warnings and not errors:
                    stage.set_status("warning")

            except FileNotFoundError:
                print(f"\n  ⚠ Properties file not found: {args.properties}")
                stage.add_issue(
                    "error",
                    issue_codes.PROPERTIES_NOT_FOUND,
                    f"Properties file not found: {args.properties}",
                )

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
    print("  SHIPS Dependency Analysis")
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
            with open(waves_path, "w", encoding="utf-8") as f:
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
    requested = {f.strip().lower() for f in args.formats.split(",")}

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
        logger.info("Exported %s → %s", fmt, filepath)

    # -- Print export summary -------------------------------------
    count = len(written)
    print(f"\n  Graph exported ({count} format{'s' if count != 1 else ''}):")
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
    sc = subs.add_parser(
        "scaffold", help="[S] Scaffold — create a new project from template."
    )
    sc.add_argument(
        "--name", required=True, help="Project name (used as directory name)."
    )
    sc.add_argument(
        "--output", default=".", help="Parent directory (default: current)."
    )
    sc.add_argument(
        "--environments",
        default="DEV,TST,PRD",
        help="Comma-separated environment names (default: DEV,TST,PRD).",
    )
    sc.add_argument(
        "--repair",
        action="store_true",
        help="Repair an existing project — add missing "
        "directories and files without overwriting "
        "existing configuration. Use after upgrading "
        "SHIPS to pick up new directory structure.",
    )

    # -- harvest --
    ig = subs.add_parser(
        "harvest", help="[H] Harvest — import raw DDL files into a project."
    )
    ig.add_argument(
        "--source",
        required=False,
        help="Directory containing raw DDL files. "
        "Required for normal harvest; ignored in --reconcile mode.",
    )
    ig.add_argument(
        "--project",
        required=True,
        help="Target project directory (must be scaffolded).",
    )
    ig.add_argument(
        "--token-map",
        help="Path to token_map.conf — applies literal → {{TOKEN}} "
        "substitutions during harvest. Generate one with "
        "--generate-token-map first, review it, then pass "
        "it here.",
    )
    ig.add_argument(
        "--generate-token-map",
        action="store_true",
        help="Scan for hardcoded database names and write a "
        "token_map.conf to the project's config/ directory. "
        "Requires --env-prefix to derive token names.",
    )
    ig.add_argument(
        "--env-prefix",
        help="Optional environment prefix to strip when deriving "
        "token names (e.g. 'A_D01'). Used with "
        "--generate-token-map to turn 'A_D01_OMR_STD' into "
        "'{{OMR_STD}}'. If omitted, the full database name "
        "becomes the token (e.g. 'CORE_STD' → '{{CORE_STD}}').",
    )
    ig.add_argument(
        "--apply-tokens",
        help="(Legacy) Comma-separated name=token pairs. "
        "Prefer --token-map instead. "
        "E.g. 'DEV01_STD={{STD_DATABASE}},DEV01_SEM={{SEM_DATABASE}}'",
    )
    ig.add_argument(
        "--no-detect-tokens", action="store_true", help="Skip hardcoded name detection."
    )
    ig.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files in the payload. "
        "Use when re-harvesting after editing source "
        "DDL. Warns if overwriting tokenised files "
        "with non-tokenised content — pass the same "
        "--token-map to preserve tokenisation.",
    )
    ig.add_argument(
        "--reconcile",
        action="store_true",
        help="Run interactive reconciliation: detect literal/tokenised "
        "twin file pairs in the harvested DDL tree and prompt to "
        "resolve each. Skips the normal harvest pipeline. Requires "
        "--project and config/token_map.conf; --source is ignored.",
    )
    ig.add_argument(
        "--json-out",
        help="Override the default JSON audit destination "
        "(<project>/logs/reconcile_<timestamp>.json) for "
        "--reconcile mode. Relative paths resolve under --project.",
    )
    # -- inspect --
    vl = subs.add_parser(
        "inspect", help="[I] Inspect — check DDL against Coding Discipline."
    )
    vl.add_argument("--source", required=True, help="Directory to validate.")
    vl.add_argument(
        "--config",
        help="Path to inspect.conf rules configuration file. "
        "If not specified, auto-detects config/inspect.conf "
        "within the source project.",
    )
    vl.add_argument(
        "--strict",
        action="store_true",
        help="Strict mode: all WARNING rules promoted to ERROR. OFF rules remain off.",
    )
    vl.add_argument(
        "--skip-tokens",
        action="store_true",
        help="Disable hardcoded name checks (legacy; prefer inspect.conf).",
    )
    vl.add_argument(
        "--skip-keywords",
        action="store_true",
        help="Disable keyword case checks (legacy; prefer inspect.conf).",
    )
    vl.add_argument(
        "--skip-commas",
        action="store_true",
        help="Disable leading comma checks (legacy; prefer inspect.conf).",
    )
    vl.add_argument(
        "--fix-grants",
        action="store_true",
        help="Generate or update .grt files in dcl/ to match "
        "the inferred grant set from DDL intent analysis. "
        "Existing .grt files are overwritten.",
    )
    vl.add_argument(
        "--skip-grants",
        action="store_true",
        help="Skip cross-database grant validation entirely.",
    )
    vl.add_argument(
        "--dcl-dir",
        help="Directory containing inter-database .grt files. "
        "Defaults to <source>/payload/database/DCL/inter_db/. "
        "The DCL directory has three subdirectories: "
        "roles/ (grants to roles), users/ (grants to users), "
        "inter_db/ (grants between databases).",
    )

    # -- package --
    bp = subs.add_parser("package", help="[P] Package — build a release package.")
    bp.add_argument("--source", required=True, help="Source project directory.")
    bp.add_argument(
        "--env",
        required=True,
        help="Target environment (e.g. DEV, TST, SIT, UAT, PRD).",
    )
    bp.add_argument(
        "--name", required=True, help="Package name (e.g. 'create_objects')."
    )
    bp.add_argument(
        "--properties", required=True, help="Path to environment .properties file."
    )
    bp.add_argument(
        "--build-number",
        type=int,
        default=None,
        help="Build number (default: auto-increment from .build_counter).",
    )
    bp.add_argument(
        "--no-increment",
        action="store_true",
        help="Reuse current build number without incrementing. "
        "Use when building the same source for a different "
        "environment (e.g. DEV then PROD).",
    )
    bp.add_argument(
        "--output", default=".", help="Output directory (default: current)."
    )
    bp.add_argument(
        "--format",
        choices=["zip", "tar.gz"],
        default="zip",
        help="Archive format (default: zip).",
    )
    bp.add_argument("--author", help="Builder's name.")
    bp.add_argument("--description", help="Release description.")
    bp.add_argument("--commit", help="Git commit hash.")

    # -- scan --
    sp = subs.add_parser(
        "scan", help="Scan source for token references (part of Inspect)."
    )
    sp.add_argument("--source", required=True, help="Source project directory to scan.")
    sp.add_argument(
        "--properties", help="Optional properties file to validate against."
    )

    # -- analyze --
    az = subs.add_parser(
        "analyze",
        help="Analyse DDL dependencies, generate waves, and export dependency graph.",
    )
    az.add_argument("--source", required=True, help="Project directory to analyse.")
    az.add_argument(
        "--output", help="Output path for _waves.txt (default: <source>/_waves.txt)."
    )
    az.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing _waves.txt."
    )
    az.add_argument(
        "--graph",
        metavar="OUTPUT_DIR",
        help="Export dependency graph to OUTPUT_DIR in one or "
        "more formats.  Creates the directory if needed.",
    )
    az.add_argument(
        "--formats",
        default=_ALL_FORMATS,
        help=f"Comma-separated graph export formats (default: {_ALL_FORMATS}).",
    )
    az.add_argument(
        "--base-name",
        default="ships_dependencies",
        help="Base filename for exported graph files (default: ships_dependencies).",
    )
    az.add_argument(
        "--namespace",
        default="teradata://ships-analysis",
        help="OpenLineage dataset namespace URI.  For a live "
        "system use teradata://hostname:1025 "
        "(default: teradata://ships-analysis).",
    )
    az.add_argument(
        "--project-name",
        default="ships-project",
        help="OpenLineage job namespace / project name (default: ships-project).",
    )

    # -- import-legacy --
    il = subs.add_parser(
        "import-legacy",
        help="Import a pre-SHIPS sed substitution script. "
        "Emits a .properties file (token values) + a sed migration "
        "script (legacy markers → {{TOKEN}}).",
        description="Import a pre-SHIPS sed substitution script and "
        "produce two artefacts that bootstrap a SHIPS project: "
        "(1) a flat .properties file with token values, and "
        "(2) a sed migration script that converts legacy markers "
        "($VAR, ${VAR}, &&VAR&&) in source files to the SHIPS "
        "{{TOKEN}} convention.",
    )
    il.add_argument(
        "input",
        help="Path to the legacy sed substitution script.",
    )
    il.add_argument(
        "--env",
        required=True,
        help="Target environment name (DEV, TST, PRD).",
    )
    il.add_argument(
        "--output-dir",
        default=".",
        help="Output directory (default: current). Files written under "
        "<output-dir>/properties/<env>.properties and "
        "<output-dir>/legacy_migration.sed.",
    )

    # -- decompose-names --
    dn = subs.add_parser(
        "decompose-names",
        help="Decompose literal database names against the SHIPS "
        "naming grammar and emit a cascade-form .properties file.",
        description="Read a list of literal Teradata database names "
        "(from a token_map.conf or a plain names file) and decompose "
        "them against the SHIPS grammar "
        "{ENV_PREFIX}_{SHIPS_ENV}_{INSTANCE}_{LAYER}_{SECURITY_TIER}_{KIND}. "
        "Emits a sectioned .properties file with composition roots "
        "and derived names in cascade form, plus a markdown report.",
    )
    dn.add_argument(
        "input",
        help="Path to a token_map.conf or plain names file (one literal "
        "per line). Format auto-detected.",
    )
    dn.add_argument(
        "--env",
        required=True,
        help="Target environment name (DEV, TST, PRD).",
    )
    dn.add_argument(
        "--output-dir",
        default=".",
        help="Output directory (default: current). Files written under "
        "<output-dir>/properties/<env>.properties and "
        "<output-dir>/decomposition_report.md.",
    )

    # -- bootstrap-properties --
    bp = subs.add_parser(
        "bootstrap-properties",
        help="Generate a .properties scaffold for an already-tokenised "
        "project. Use when the source already references "
        "{{TOKEN}} but no .properties file exists yet.",
        description="Scan an already-tokenised SHIPS project for "
        "{{TOKEN}} references and emit a 7-section .properties "
        "scaffold with every referenced token parked in section 8 "
        "for the user to re-section. Closes the third bootstrap "
        "path: when there's nothing to convert (no literals, no "
        "legacy script) you just need a starting .properties skeleton.",
    )
    bp.add_argument(
        "--source",
        required=True,
        help="SHIPS project directory (with payload/ already harvested).",
    )
    bp.add_argument(
        "--env",
        required=True,
        help="Target environment name (DEV / TST / PRD).",
    )
    bp.add_argument(
        "--output-dir",
        default=None,
        help="Output directory; .properties written under "
        "<output-dir>/properties/<env>.properties. Defaults to "
        "<source>/config.",
    )
    bp.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .properties file at the target. "
        "Without this, the tool refuses to clobber.",
    )

    return parser


if __name__ == "__main__":
    main()
