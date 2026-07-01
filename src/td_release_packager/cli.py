"""
cli.py — Command-line interface for the Teradata Release Packager.

Commands:
    scaffold          Create a new project from template.
    harvest           Import raw DDL files into a project.
    inspect           Check DDL against Coding Discipline + validate grants.
    package           Build a release package for a target environment.
    scan              Scan source files and report all tokens found.
    stage             Stage SHIPS-owned paths into git (scan + inspect gates + git add).
    analyze           Analyse DDL dependencies, generate waves, export graph.
    import-legacy     Import a pre-SHIPS sed substitution script and
                      emit a .conf file plus a migration sed.
    migrate-source    Apply a SHIPS tokenisation config to a source
                      tree (Windows-safe; no sed binary required).
    decompose-names   Infer composition roots from literal database
                      names and emit a cascade-form .conf file.

Usage:
    python -m td_release_packager scaffold --name MortgagePlatform --output /projects
    python -m td_release_packager build --source . --env DEV --name create_objects --env-config config/env/DEV.conf
    python -m td_release_packager fix --project .
    python -m td_release_packager inspect --project .
    python -m td_release_packager scan --source .
    python -m td_release_packager analyze --source . --graph ./output/
    python -m td_release_packager analyze --source . --graph . --formats dot,json,openlineage
    python -m td_release_packager import-legacy --script legacy.sh --env DEV --output-dir ./config
    python -m td_release_packager import-legacy --scan-source ./src --env DEV --output-dir ./config
    python -m td_release_packager migrate-source --tokenise-config config/tokenise.conf --source ./src
    python -m td_release_packager decompose-names token_map.conf --env DEV --output-dir ./config
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from td_release_packager.builder import build_package
from td_release_packager.build_counter import read_build_number
from td_release_packager.ingest import ingest_directory
from td_release_packager.package_history import RULE_NAME as _PACKAGE_HISTORY_RULE
from td_release_packager.token_engine import (
    read_token_map,
    write_token_map,
    generate_token_map,
)
from td_release_packager.models import BuildConfig
from td_release_packager.scaffolder import scaffold_project
from td_release_packager.ships_cmd import install_hint, run_from_hint, ships_cmd
from td_release_packager.token_engine import (
    read_env_config,
    scan_tokens_in_directory,
    validate_tokens,
)
from td_release_packager.validate import (
    read_inspect_config,
    resolve_inspect_root,
    validate_directory,
)
from td_release_packager.version_args import add_version_argument
from td_release_packager.validate_grants import (
    validate_grants,
    format_report as format_grant_report,
)

logger = logging.getLogger(__name__)

_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"


def _colour(text: str, colour: str) -> str:
    """Colour terminal status glyphs when output is a TTY."""
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return text
    return f"{colour}{text}{_ANSI_RESET}"


def _status_icon(ok: bool) -> str:
    """Return a coloured pass/fail icon for terminal status lines."""
    return _colour("✓", _ANSI_GREEN) if ok else _colour("✗", _ANSI_RED)


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


def _format_grant_recap(
    grant_result,
    max_items: int = 10,
    extra_grants_severity: str = "ERROR",
    external_grants_severity: str = "INFO",
) -> str:
    """
    Produce a recap of grant validation failures. Returns empty
    string if grant_result is None or all grantees are consistent.
    """
    if grant_result is None or grant_result.passed:
        return ""

    extra_grants_severity = extra_grants_severity.upper()
    external_grants_severity = external_grants_severity.upper()

    drifted = [
        status
        for status in grant_result.drifted
        if status.missing_privs or extra_grants_severity != "OFF"
    ]
    missing = grant_result.missing
    external = [] if external_grants_severity == "OFF" else grant_result.orphaned
    total = len(drifted) + len(missing) + len(external)

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
        lines.append(f"      {status.grantee}  [missing DCL]")
        shown += 1
    for status in external:
        if shown >= max_items:
            break
        lines.append(f"      {status.grantee}  [external grant]")
        shown += 1

    if shown < total:
        lines.append("")
        lines.append(f"    + {total - shown} more — full details listed above.")
    return "\n".join(lines)


def main():
    """CLI entry point."""
    try:
        _main()
    except KeyboardInterrupt:
        print(
            "\n\n  SHIPS interrupted — pipeline cancelled by user.\n"
            "  Any stages that completed before the interrupt were recorded\n"
            "  in ships.decisions.json and can be reviewed with 'ships explain'.",
            file=sys.stderr,
        )
        sys.exit(1)


def _main():
    """Inner entry point — separated so KeyboardInterrupt wraps everything cleanly."""
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
    elif args.command == "fix":
        sys.exit(_cmd_fix(args))
    elif args.command == "package":
        _cmd_build(args)
    elif args.command == "deploy":
        sys.exit(_cmd_deploy(args))
    elif args.command == "repackage":
        _cmd_repackage(args)
    elif args.command == "scan":
        sys.exit(_cmd_scan(args))
    elif args.command in ("analyze", "analyse"):
        _cmd_analyze(args)
    elif args.command == "changeset":
        sys.exit(_cmd_changeset(args))
    elif args.command == "plan":
        sys.exit(_cmd_plan(args))
    elif args.command == "wizard":
        sys.exit(_cmd_wizard(args))
    elif args.command == "metadata":
        sys.exit(_cmd_metadata(args))
    elif args.command == "import-legacy":
        _cmd_import_legacy(args)
    elif args.command == "migrate-source":
        _cmd_migrate_source(args)
    elif args.command == "decompose-names":
        _cmd_decompose_names(args)
    elif args.command == "bootstrap-env-config":
        _cmd_bootstrap_env_config(args)
    elif args.command == "generate":
        _cmd_generate(args)
    elif args.command == "process":
        _cmd_process(args)
    elif args.command == "demo":
        sys.exit(_cmd_demo(args))
    elif args.command == "explain":
        _cmd_explain(args)
    elif args.command == "verify":
        _cmd_verify(args)
    elif args.command == "onboard":
        _cmd_onboard(args)
    elif args.command == "decisions":
        _cmd_decisions(args)
    elif args.command == "rollback":
        _cmd_rollback(args)
    elif args.command == "keygen":
        _cmd_keygen(args)
    elif args.command == "clean":
        _cmd_clean(args)
    elif args.command == "stage":
        sys.exit(_cmd_stage(args))
    elif args.command == "notebook":
        sys.exit(_cmd_notebook(args))
    elif args.command == "fix-package-integrity":
        sys.exit(_cmd_fix_package_integrity(args))
    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------
# Orchestrator integration helpers
# ---------------------------------------------------------------
#
# Build-order item 4: every stage opens a ``ships.decisions.json`` and
# records its run. Two concerns colliding here:
#
#   1. We don't want a ships.decisions.json appearing in random
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

    Used to decide whether a stage should write ships.decisions.json.
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


def _cmd_deploy(args) -> int:
    """Deploy a SHIPS zip, extracted package, or release-group directory."""
    from td_release_packager.deploy_launcher import launch_deploy

    try:
        return launch_deploy(
            args.target,
            args.deploy_args,
            role=args.role,
            work_dir=args.work_dir,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_notebook(args) -> int:
    """Render a Clearscape Experience deployment notebook from a project.

    Reads the project's analysis result (objects + wave ordering) and
    its env config, then writes a self-contained .ipynb that deploys
    every object via inline DDL. See
    :mod:`td_release_packager.clearscape_notebook` for the notebook
    shape and design rationale.
    """
    from pathlib import Path

    from td_release_packager.analyser import analyse_project
    from td_release_packager.clearscape_notebook import (
        render_notebook,
        write_notebook,
    )
    from td_release_packager.token_engine import read_env_config

    project_dir = Path(args.project).expanduser().resolve()
    if not project_dir.is_dir():
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        return 1

    env_config_path = Path(args.env_config).expanduser().resolve()
    if not env_config_path.is_file():
        print(
            f"ERROR: Env config file not found: {env_config_path}",
            file=sys.stderr,
        )
        return 1

    package_name = args.name or project_dir.name
    env_values = read_env_config(str(env_config_path))
    analysis = analyse_project(str(project_dir))

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        output_path = project_dir / "output" / f"{package_name}.clearscape.ipynb"

    notebook = render_notebook(
        analysis,
        package_name=package_name,
        env_values=env_values,
        env_name=args.env_name,
    )
    written = write_notebook(notebook, output_path)

    print("=" * 64)
    print("  SHIPS Clearscape Notebook")
    print("=" * 64)
    print(f"  Project:    {project_dir}")
    print(f"  Env config: {env_config_path}")
    print(f"  Notebook:   {written}")
    print(f"  Waves:      {len(analysis.waves)}")
    print(f"  Objects:    {len(analysis.objects)}")
    print(f"  Cells:      {len(notebook['cells'])}")
    print("=" * 64)
    return 0


def _cmd_demo(args) -> int:
    """Run the low-friction SHIPS demo workflow."""
    _gh_tmp: list = []
    _resolve_github_source(args, _gh_tmp)

    try:
        from td_release_packager.demo import run_demo

        result = run_demo(
            source=args.source,
            name=args.name,
            work_dir=args.work_dir,
            output_dir=args.output_dir,
            env=args.env,
            env_prefix=args.env_prefix,
            root_parent=args.root_parent,
            package=not args.prepare_only,
            deploy=args.deploy,
            deploy_args=_strip_remainder_separator(args.deploy_args),
            source_commit=getattr(args, "commit", "") or "",
            author=args.author or "",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for tmp in _gh_tmp:
            tmp.cleanup()

    print(f"\n{'=' * 64}")
    print("  SHIPS Demo Mode")
    print(f"{'=' * 64}")
    print(f"  Source:      {result.source_dir}")
    print(f"  Project:     {result.project_dir}")
    print(f"  Env config:  {result.env_config}")
    print(f"  Token map:   {result.token_map}")
    print(f"  Classified:  {result.classified}")
    print(f"  Unclassified:{result.unclassified}")
    print(
        f"  Lint:        {result.lint_errors} error(s), {result.lint_warnings} warning(s)"
    )
    print(
        f"  Analysis:    {result.analysis_objects} object(s), "
        f"{result.analysis_waves} wave(s)"
    )
    if result.root_parent_injections:
        print(
            f"  Root parent: {result.root_parent_injections} parentless prereq(s) updated"
        )
    if result.archive_path:
        print(f"  Archive:     {result.archive_path}")
    if result.companion_archive_path:
        print(f"  Companion:   {result.companion_archive_path}")
    for report_path in result.report_paths:
        print(f"  Report:      {report_path}")
    if result.release_group:
        print(f"  Release grp: {result.release_group}")
    if result.deploy_exit_code is not None:
        print(f"  Deploy exit: {result.deploy_exit_code}")
    print(f"{'=' * 64}\n")

    if result.deploy_exit_code not in (None, 0):
        return result.deploy_exit_code
    return 0


def _strip_remainder_separator(args: list[str]) -> list[str]:
    """Drop a leading ``--`` from argparse.REMAINDER passthrough args."""
    if args and args[0] == "--":
        return args[1:]
    return args


def _apply_root_parent_option(project_dir: str, root_parent: str | None) -> int:
    """Apply ``--root-parent`` to parentless project prerequisite DDL."""
    if root_parent is None:
        return 0
    from td_release_packager.root_parent import inject_root_parent

    return inject_root_parent(Path(project_dir), root_parent)


class _NullStageRecorder:
    """
    Drop-in for ``StageRecorder`` that ignores every call.

    Used by ``_stage_recording`` when the target directory isn't a
    SHIPS project. Lets the same stage code run end-to-end without
    branching on whether ships.decisions.json is being written.
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
        details=None,
    ) -> None:
        pass


def _stage_recording(project_dir: str, stage_name: str):
    """
    Context manager: yield a stage recorder for ``stage_name`` rooted
    at ``project_dir``.

    If ``project_dir`` looks like a SHIPS project, opens
    ``<project_dir>/ships.decisions.json`` and yields a real
    ``StageRecorder``. Otherwise yields a ``_NullStageRecorder`` so
    ad-hoc one-off invocations don't litter the filesystem.

    Also emits an OpenTelemetry span named ``ships.<stage_name>`` when
    ``opentelemetry-api`` is installed and an SDK is configured. When
    OTel is not available this is a zero-overhead no-op.

    Usage::

        with _stage_recording(args.source, "scan") as stage:
            stage.set_config_resolved(...)
            ...

    The yielded object always supports the StageRecorder interface
    so call sites don't need to branch.
    """
    from contextlib import contextmanager

    from td_release_packager.orchestrator import DecisionsManifest
    from td_release_packager.otel import ships_span
    from td_release_packager.project_paths import decisions_json_path

    @contextmanager
    def _ctx():
        with ships_span(
            f"ships.{stage_name}",
            {"ships.project_dir": project_dir, "ships.stage": stage_name},
        ) as otel_span:
            if not _looks_like_ships_project(project_dir):
                yield _NullStageRecorder()
                return

            manifest_path = decisions_json_path(project_dir)
            manifest = DecisionsManifest(manifest_path)
            with manifest.run(stage_name) as run:
                with run.stage(stage_name) as stage:
                    yield stage

            # Stage complete — propagate status and key outputs to OTel.
            # Accessing stage._entry is safe: the inner with block has
            # exited and RunRecorder.__exit__ has already finalised it.
            _propagate_stage_to_otel_span(stage, otel_span)

            # #517 — heartbeat: these four refresh calls (project index,
            # actions, policy, pipeline report) can add several seconds on
            # large projects and are the last silent stretch of a harvest
            # run. Announce them as a group so the operator sees the
            # process is still moving rather than hung.
            logger.info(
                "Refreshing project index, actions, policy, and pipeline report..."
            )

            # Refresh ships.project.json and ships.project_actions.json
            # after every stage so an agent opening the project sees the
            # latest lifecycle state, recommended next actions, and
            # allowed/blocked/approval action vocabulary (#271, #273).
            # Best-effort: errors here must never break the stage
            # recording path.
            try:
                from td_release_packager.project_index import write_project_index

                write_project_index(project_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("project_index refresh failed: %s", exc)
            try:
                from td_release_packager.project_actions import (
                    write_project_actions,
                )

                write_project_actions(project_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("project_actions refresh failed: %s", exc)
            try:
                from td_release_packager.project_policy import (
                    write_project_policy,
                )

                write_project_policy(project_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("project_policy refresh failed: %s", exc)

            # Refresh the pre-package pipeline HTML report after every step
            # so a human can see what happened at each stage before a
            # package is built (#324). Best-effort: a reporting failure must
            # never break the stage recording path.
            try:
                from td_release_packager.reporting import regenerate_reports

                regenerate_reports(project_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("pipeline report refresh failed: %s", exc)

    return _ctx()


def _propagate_stage_to_otel_span(stage, otel_span) -> None:
    """Copy key stage attributes to the OTel span after the stage closes."""
    try:
        entry = getattr(stage, "_entry", {})
        status = entry.get("status", "unknown")
        otel_span.set_attribute("ships.stage.status", status)

        # Propagate scalar outputs as span attributes
        for key, value in entry.get("outputs", {}).items():
            if isinstance(value, (str, int, float, bool)):
                otel_span.set_attribute(f"ships.output.{key}", value)

        # Propagate issue counts
        issues = entry.get("issues", [])
        errors = sum(1 for i in issues if i.get("severity") == "error")
        warnings = sum(1 for i in issues if i.get("severity") == "warning")
        otel_span.set_attribute("ships.issues.errors", errors)
        otel_span.set_attribute("ships.issues.warnings", warnings)

        # Mark the OTel span status on error
        if status == "error":
            try:
                from opentelemetry.trace import StatusCode

                otel_span.set_status(
                    StatusCode.ERROR, f"Stage '{stage._entry.get('stage', '')}' failed"
                )
            except ImportError:
                pass
    except Exception:
        # OTel propagation must never break the recording path
        pass


# ---------------------------------------------------------------
# Process meta-verb recording infrastructure
# ---------------------------------------------------------------


class _NullRunRecorder:
    """
    Drop-in for ``RunRecorder`` for non-project ``process`` runs.

    Yields ``_NullStageRecorder`` instances so the caller never needs
    to branch on whether ships.decisions.json is being written.
    """

    from contextlib import contextmanager as _cm

    @property
    def run_id(self) -> str:
        return "(no-op)"

    def stage(self, name: str):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield _NullStageRecorder()

        return _ctx()


def _process_recording(project_dir: str):
    """
    Context manager for the ``process`` meta-verb.

    Opens a single run in ``ships.decisions.json`` and yields the
    ``RunRecorder`` so the caller can open individual stages within
    it.  One run with multiple stages gives a clean end-to-end audit
    trail across the whole pipeline — distinct from the per-stage
    single-run pattern used by individual commands.

    Yields a ``_NullRunRecorder`` when ``project_dir`` is not a SHIPS
    project, so ad-hoc runs don't litter the filesystem.
    """
    from contextlib import contextmanager

    from td_release_packager.orchestrator import DecisionsManifest
    from td_release_packager.otel import ships_span
    from td_release_packager.project_paths import decisions_json_path

    @contextmanager
    def _ctx():
        with ships_span(
            "ships.process",
            {"ships.project_dir": project_dir, "ships.stage": "process"},
        ):
            if not _looks_like_ships_project(project_dir):
                yield _NullRunRecorder()
                return

            manifest_path = decisions_json_path(project_dir)
            manifest = DecisionsManifest(manifest_path)
            with manifest.run("process") as run:
                yield run

    return _ctx()


# ---------------------------------------------------------------
# Harvest "Next Steps" banner
# ---------------------------------------------------------------


def _project_has_env_config(project_dir: str) -> bool:
    """True if ``<project>/config/env/`` contains any
    ``*.conf`` file. Used to pick between the 'bootstrap'
    and 'verify' wording in the harvest banner."""
    env_dir = os.path.join(project_dir, "config", "env")
    if not os.path.isdir(env_dir):
        return False
    return any(f.endswith(".conf") for f in os.listdir(env_dir))


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
         dance entirely and goes straight to bootstrap-env-config.

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
            bootstrap-env-config.
    """
    from typing import List

    print("=" * 64)
    print("  Next Steps")
    print("=" * 64)

    project = args.project
    has_props = _project_has_env_config(project)

    # Lead with stage label + state line so the user knows where
    # they are before they see the steps. Same shape across all
    # four flows.
    print()
    print("  You are here:  [H] Harvest complete")
    if already_tokenised:
        state = "source already tokenised; .conf not yet defined"
    elif generated_token_map_path:
        state = "literals scanned; token map written; substitutions NOT applied"
    elif substitutions_applied:
        state = "source tokenised via --token-map; substitutions applied"
    else:
        state = "source ingested; no token activity this run"
    print(f"  Project state: {state}")
    # Per-verb cwd orientation cue (#403) — answers "where do I run
    # these from?" before the snippets so users don't have to guess.
    print(f"  {run_from_hint()}")
    print()

    steps: List[str] = []

    # Pick the friendliest invocation that resolves on this shell so
    # printed snippets can be copy-pasted as-is — bare ``ships`` when on
    # PATH, ``uv run ships`` inside a uv project, else
    # ``python -m td_release_packager`` (#403).
    cmd = ships_cmd()

    # Quality-gate block — appears in every flow before packaging.
    # 'inspect' is part of the canonical S-H-I-P-S workflow;
    # 'analyze' produces dependency waves for parallel deploy
    # (optional but recommended); 'scan' catches {{TOKEN}}
    # references that have no value in the .conf file.
    def _quality_gates_step(num: int) -> str:
        return (
            f"{num}. Validate the harvested DDL before packaging:\n"
            f"\n"
            f"     {cmd} inspect \\\n"
            f"         --source {project}\n"
            f"\n"
            f"     {cmd} analyze \\\n"
            f"         --source {project}            "
            f"# optional, deploy waves\n"
            f"\n"
            f"     {cmd} scan \\\n"
            f"         --source {project} \\\n"
            f"         --env-config config/env/DEV.conf\n"
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
            f"     {cmd} package \\\n"
            f"         --source {project} --env DEV --name <name> \\\n"
            f"         --env-config config/env/DEV.conf \\\n"
            f"         --output releases/"
        )

    if already_tokenised:
        # Flow D — source already uses {{TOKEN}} references. Skip
        # the token map entirely and bootstrap properties directly
        # from the tokens the source already references.
        bootstrap_cmd_parts = [
            f"     {cmd} bootstrap-env-config \\\n"
            f"         --source {project} \\\n"
            f"         --env DEV"
        ]
        if not has_props:
            steps.append(
                f"1. Bootstrap a .conf file from the tokens the\n"
                f"   source already references:\n"
                f"\n"
                f"{bootstrap_cmd_parts[0]}\n"
                f"\n"
                f"   Output: a 7-section .conf scaffold under\n"
                f"   {project}\\config\\env\\DEV.conf\n"
                f"   with every {{{{TOKEN}}}} parked in section 8\n"
                f"   for you to re-section by cut-and-paste."
            )
        else:
            steps.append(
                f"1. (Optional) Refresh the existing .conf scaffold\n"
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
                f"2. Bootstrap a .conf file from the token map:\n"
                f"\n"
                f"     {cmd} decompose-names \\\n"
                f"         {generated_token_map_path} \\\n"
                f"         --env DEV \\\n"
                f"         --output-dir {project}\\config\n"
                f"\n"
                f"   Output: a 7-section .conf scaffold under\n"
                f"   {project}\\config\\env\\DEV.conf\n"
                f"   plus a decomposition_report.md with confidence\n"
                f"   ratings and outliers."
            )
            next_num = 3
        else:
            next_num = 2
        steps.append(
            f"{next_num}. Re-harvest with the token map applied:\n"
            f"\n"
            f"     {cmd} harvest \\\n"
            f"         --source <legacy_src> \\\n"
            f"         --project {project} \\\n"
            f"         --token-map {generated_token_map_path}\n"
            f"\n"
            f"   This rewrites the staged DDL to use {{{{TOKEN}}}} form. "
            f"The default clean-payload mode wipes the previous run's "
            f"un-tokenised files first; pass --keep-existing if you "
            f"need legacy overlay behaviour."
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
# Onboarding wizard
# ---------------------------------------------------------------


def _onboard_scan(source_dir: str) -> dict:
    """Walk source_dir and classify what placeholder style is in use."""
    import re as _re

    from td_release_packager.legacy_placeholders import find_legacy_placeholders
    from td_release_packager.discovery import resolve_harvest_extensions

    _SHIPS_TOKEN_RE = _re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")

    extensions = resolve_harvest_extensions(project_dir=source_dir)
    sql_files = []
    legacy_files = set()
    token_files = set()
    legacy_count = 0

    for root, dirs, files in os.walk(source_dir):
        dirs.sort()
        for fname in sorted(files):
            if os.path.splitext(fname)[1].lower() not in extensions:
                continue
            path = os.path.join(root, fname)
            sql_files.append(path)
            try:
                content = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            findings = find_legacy_placeholders(content, path)
            if findings:
                legacy_files.add(path)
                legacy_count += len(findings)
            if _SHIPS_TOKEN_RE.search(content):
                token_files.add(path)

    return {
        "sql_files": len(sql_files),
        "legacy_files": len(legacy_files),
        "legacy_count": legacy_count,
        "token_files": len(token_files),
    }


def _onboard_classify(scan: dict, source_dir: str) -> str:
    """Return a state label based on what was found."""
    has_legacy = scan["legacy_files"] > 0
    has_tokens = scan["token_files"] > 0
    has_config = bool(
        next(
            (
                p
                for p in [
                    os.path.join(source_dir, "config", "env"),
                    os.path.join(source_dir, "env"),
                ]
                if os.path.isdir(p) and any(f.endswith(".conf") for f in os.listdir(p))
            ),
            None,
        )
    )
    if has_legacy:
        return "LEGACY"
    if has_tokens and has_config:
        return "READY"
    if has_tokens:
        return "TOKENS_NO_CONFIG"
    return "CLEAN"


def _cmd_onboard(args):
    """Scan a source directory and recommend the SHIPS onboarding path."""
    source = os.path.abspath(args.source)
    auto = getattr(args, "auto", False)
    env = getattr(args, "env", None) or "DEV"

    if not os.path.isdir(source):
        print(f"ERROR: source directory not found: {source}", file=sys.stderr)
        sys.exit(1)

    print("\n  SHIPS Onboarding Wizard")
    print(f"  {'=' * 56}")
    print(f"  Scanning: {source}")

    scan = _onboard_scan(source)
    state = _onboard_classify(scan, source)

    print("\n  Source summary")
    print(f"    SQL/DDL files found : {scan['sql_files']}")
    print(
        f"    Legacy markers ($VAR, &&VAR&&) : {scan['legacy_count']} in {scan['legacy_files']} file(s)"
    )
    print(f"    SHIPS {{{{TOKEN}}}} forms : {scan['token_files']} file(s)")
    print()

    _print_onboard_recommendation(state, source, env, scan)

    if auto:
        _onboard_run_auto(state, source, env, args)


def _print_onboard_recommendation(state: str, source: str, env: str, scan: dict):
    """Print the recommended command sequence for the detected state."""
    # Pick the friendliest invocation that resolves on this shell —
    # bare ``ships`` when on PATH, then ``uv run ships`` inside a uv
    # project, then ``python -m td_release_packager`` as fallback (#403).
    module = ships_cmd()
    # Per-verb cwd orientation cue — answers "where do I run these
    # from?" before the snippets so users don't have to guess.
    print(f"  {run_from_hint()}")
    print()

    if state == "LEGACY":
        print("  Detected: legacy placeholder markers ($VAR / &&VAR&&)")
        print("  Recommended path: import-legacy → migrate-source → harvest\n")
        print("  Step 1 — discover all legacy markers and generate the migration sed:")
        print(f"    {module} import-legacy \\")
        print(f"      --scan-source {source} \\")
        print(f"      --env {env} \\")
        print("      --output-dir ./config\n")
        print("  Step 2 — fill in token values in config/env/DEV.conf, then apply:")
        print(f"    {module} migrate-source \\")
        print("      --tokenise-config config/tokenise.conf \\")
        print(f"      --source {source}\n")
        print("  Step 3 — harvest the migrated source into a SHIPS project:")
        print(f"    {module} harvest --source {source} --project <project_dir>")

    elif state == "TOKENS_NO_CONFIG":
        print("  Detected: SHIPS {{TOKEN}} markers, no env config yet")
        print("  Recommended path: bootstrap-env-config → fill values → harvest\n")
        print("  Step 1 — generate a config scaffold from existing tokens:")
        print(f"    {module} bootstrap-env-config \\")
        print("      --source <project_dir> \\")
        print(f"      --env {env}\n")
        print("  Step 2 — fill in token values in config/env/DEV.conf")
        print("  Step 3 — harvest:")
        print(f"    {module} harvest --source {source} --project <project_dir>")

    elif state == "READY":
        print("  Detected: SHIPS {{TOKEN}} markers with env config present")
        print("  Source looks ready — proceed with harvest:\n")
        print(f"    {module} harvest --source {source} --project <project_dir>")

    else:  # CLEAN
        print("  Detected: no placeholder markers found")
        print("  Recommended path: harvest → decompose-names → bootstrap-env-config\n")
        print("  Step 1 — harvest (token candidates will be reported):")
        print(f"    {module} harvest --source {source} --project <project_dir>\n")
        print("  Step 2 — decompose literal database names into {{TOKEN}} form:")
        print(f"    {module} decompose-names token_map.conf --env {env}\n")
        print("  Step 3 — bootstrap env config from the token map:")
        print(f"    {module} bootstrap-env-config --source <project_dir> --env {env}")

    hint = install_hint()
    if hint is not None:
        print()
        for line in hint.splitlines():
            print(f"  {line}")
    print()


def _onboard_run_auto(state: str, source: str, env: str, args):
    """Run the first automatable step for the detected state."""
    if state == "LEGACY":
        print("  --auto: running import-legacy --scan-source ...\n")
        from td_release_packager.legacy_importer import main as il_main

        output_dir = getattr(args, "output_dir", None) or "./config"
        il_main(["--scan-source", source, "--env", env, "--output-dir", output_dir])
    elif state == "TOKENS_NO_CONFIG":
        print("  --auto requires a project directory for bootstrap-env-config.")
        print(f"  Run manually: {ships_cmd()} bootstrap-env-config ...")
    elif state == "READY":
        print("  --auto: source is ready — run harvest manually.")
    else:
        print("  --auto: harvest detects literal names; run harvest manually first.")


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
    """Dispatch to td_release_packager.legacy_importer.main().

    Two input modes (mutually exclusive at the argparse layer):
    ``--script`` consumes an existing sed substitution script;
    ``--scan-source`` walks a source DDL tree and auto-discovers
    placeholders. Either resolves into the same shape of artefacts
    (``env/<env>.conf`` + ``tokenise.conf``) -- the latter mode
    additionally writes ``scan_report.md``.
    """
    from td_release_packager.legacy_importer import main as importer_main

    argv: list = []
    if args.script:
        argv.extend(["--script", args.script])
    else:
        argv.extend(["--scan-source", args.scan_source])
        if args.project:
            argv.extend(["--project", args.project])
    argv.extend(["--env", args.env, "--output-dir", args.output_dir])
    if args.verbose:
        argv.append("-v")
    sys.exit(importer_main(argv))


def _cmd_migrate_source(args):
    """Apply a tokenisation config to a source tree (Windows-safe)."""
    from td_release_packager.source_migrator import main as migrator_main

    argv = ["--tokenise-config", args.tokenise_config, "--source", args.source]
    if args.project:
        argv.extend(["--project", args.project])
    if args.dry_run:
        argv.append("--dry-run")
    if args.verbose:
        argv.append("--verbose")
    sys.exit(migrator_main(argv))


def _cmd_decompose_names(args):
    """Dispatch to td_release_packager.decomposer.main()."""
    from td_release_packager.decomposer import main as decomposer_main

    argv = [args.input, "--env", args.env, "--output-dir", args.output_dir]
    if args.verbose:
        argv.append("-v")
    sys.exit(decomposer_main(argv))


def _cmd_bootstrap_env_config(args):
    """Dispatch to td_release_packager.env_config_bootstrapper.main()."""
    from td_release_packager.env_config_bootstrapper import main as bootstrap_main

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


def _add_github_source_args(parser, mutually_exclusive_with: str = "--source") -> None:
    """Add --source-github / --source-ref / --github-token to a subparser."""
    parser.add_argument(
        "--source-github",
        metavar="OWNER/REPO",
        dest="source_github",
        default=None,
        help="Fetch DDL source from a GitHub repository (e.g. 'myorg/myrepo'). "
        "Downloads via the GitHub REST API first, then falls back to local "
        f"git clone credentials. Mutually exclusive with {mutually_exclusive_with}.",
    )
    parser.add_argument(
        "--source-ref",
        metavar="REF",
        dest="source_ref",
        default="main",
        help="Branch, tag, or commit SHA to fetch when using --source-github "
        "(default: main).",
    )
    parser.add_argument(
        "--github-token",
        metavar="TOKEN",
        dest="github_token",
        default="",
        help="GitHub personal access token for private repositories.  "
        "Falls back to the GITHUB_TOKEN environment variable.  "
        "Public repositories work without a token.",
    )


def _resolve_github_source(args, tmp_dir_holder: list) -> None:
    """If --source-github is set, fetch the repo and set args.source.

    ``tmp_dir_holder`` is a one-element list; the caller appends the
    ``TemporaryDirectory`` object so it stays alive for the pipeline run
    and is cleaned up when the caller disposes it.

    Raises SystemExit on validation errors.
    """
    import tempfile

    from td_release_packager.remote_source import fetch_github_source

    source_github = getattr(args, "source_github", None)
    if not source_github:
        return

    if getattr(args, "source", None):
        print(
            "ERROR: --source and --source-github are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)

    if "/" not in source_github or source_github.count("/") != 1:
        print(
            f"ERROR: --source-github must be in 'owner/repo' format, "
            f"got: {source_github!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    ref = getattr(args, "source_ref", "main") or "main"
    token = getattr(args, "github_token", "") or ""

    print(f"\n  Fetching source: github.com/{source_github} @ {ref}")

    tmp = tempfile.TemporaryDirectory(prefix="ships_gh_source_")
    tmp_dir_holder.append(tmp)

    try:
        commit_sha = fetch_github_source(source_github, ref, tmp.name, token)
        args.source = tmp.name
    except ValueError as api_exc:
        print(
            "  GitHub API fetch failed; trying local git clone credentials...",
            file=sys.stderr,
        )
        clone_dir = os.path.join(tmp.name, "repo")
        try:
            commit_sha = _clone_github_source(source_github, ref, clone_dir)
            args.source = clone_dir
        except ValueError as clone_exc:
            print(
                f"\nERROR: {api_exc}\n\n"
                f"Git clone fallback also failed:\n  {clone_exc}\n\n"
                "Tip: set GITHUB_TOKEN for API access, or make sure "
                "`git clone https://github.com/<owner>/<repo>.git` works "
                "from this shell.",
                file=sys.stderr,
            )
            tmp.cleanup()
            sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}", file=sys.stderr)
        tmp.cleanup()
        sys.exit(1)

    # Record the resolved SHA as the commit unless the user already passed --commit
    if not getattr(args, "commit", None):
        args.commit = commit_sha

    print(f"  Resolved commit : {commit_sha[:12]}")
    print(f"  Extracted to    : {args.source}\n")


def _clone_github_source(owner_repo: str, ref: str, dest_dir: str) -> str:
    """Clone a GitHub source repository using the local git credential stack."""
    url = f"https://github.com/{owner_repo}.git"
    clone_cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        ref,
        url,
        dest_dir,
    ]
    result = subprocess.run(
        clone_cmd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise ValueError(message or f"git clone exited {result.returncode}")

    sha_result = subprocess.run(
        ["git", "-C", dest_dir, "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if sha_result.returncode != 0:
        message = (sha_result.stderr or sha_result.stdout or "").strip()
        raise ValueError(message or "git rev-parse HEAD failed after clone")
    return sha_result.stdout.strip()


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


def _parse_prefix_token_args(values: List[str]) -> Optional[Dict[str, str]]:
    """Parse ``--prefix-token SOURCE=TOKEN`` CLI values into a dict.

    Each value is split on the first ``=``.  Both sides are stripped.
    Empty or malformed entries exit with a friendly error so a typo
    surfaces at argparse time rather than producing silently-wrong
    tokenisation.

    :param values: List of ``--prefix-token`` strings (may be empty).
    :returns:      ``None`` if ``values`` is empty, else a dict.
    """
    if not values:
        return None
    mapping: Dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            print(
                f"ERROR: --prefix-token expects SOURCE=TOKEN, got {raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        src, _, tok = raw.partition("=")
        src = src.strip()
        tok = tok.strip()
        if not src or not tok:
            print(
                f"ERROR: --prefix-token has empty source or token: {raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        mapping[src] = tok
    return mapping


def _load_project_legacy_migration_rules(project_dir: str, stage=None):
    """Load project-local tokenisation rules when present.

    Reads ``config/tokenise.conf`` from the project. Harvest and process
    honour that project contract automatically so any matched markers
    are normalised to ``{{TOKEN}}`` form before classification and
    packaging.
    """
    from td_release_packager.project_paths import tokenise_conf_path

    migration_path = tokenise_conf_path(project_dir)
    has_config = os.path.isfile(migration_path)

    if stage is not None:
        stage.set_config_resolved(
            "tokenise_config",
            migration_path if has_config else None,
            "layer-3",
            "project config",
        )

    if not has_config:
        return []

    from td_release_packager.source_migrator import parse_migration_sed

    with open(migration_path, encoding="utf-8") as f:
        content = f.read()
    rules, skipped = parse_migration_sed(content)
    if skipped:
        for line in skipped:
            message = f"Skipped unparseable legacy migration rule: {line}"
            if stage is not None:
                from td_release_packager.orchestrator import issue_codes

                stage.add_issue(
                    "warning",
                    issue_codes.HARVEST_CLASSIFICATION_WARNING,
                    message,
                )
            print(f"  WARN: {message}")

    if not rules:
        print(
            f"  WARN: legacy migration file exists but has no parseable rules: "
            f"{migration_path}"
        )
        return []

    return rules


# ---------------------------------------------------------------
# Commands
# ---------------------------------------------------------------


def _cmd_scaffold(args):
    """Create a new project from template, or repair an existing one."""
    envs = [e.strip().upper() for e in args.environments.split(",")]
    repair = getattr(args, "repair", False)

    # Run scaffold first — the project directory must exist before
    # _stage_recording can detect it as a SHIPS project and open
    # ships.decisions.json. Errors are fatal so they exit before recording starts.
    try:
        project_dir = scaffold_project(
            project_name=args.name,
            output_dir=args.output,
            environments=envs,
            repair=repair,
        )
    except FileExistsError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(
            "  Tip: use --repair to add missing directories and files", file=sys.stderr
        )
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Project exists — record the scaffold decisions and print the banner.
    with _stage_recording(project_dir, "scaffold") as stage:
        stage.set_config_resolved("name", args.name, "layer-5", "cli")
        stage.set_config_resolved("output", args.output, "layer-5", "cli")
        stage.set_config_resolved("environments", envs, "layer-5", "cli")
        stage.set_config_resolved("repair", repair, "layer-5", "cli")
        stage.set_outputs(
            project_dir=project_dir,
            environment_count=len(envs),
            action="repair" if repair else "scaffold",
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
            # Friendliest verb available on this shell (#403).
            _cmd = ships_cmd()
            print("\n  SHIPS workflow — next steps:")
            print("    [S] Scaffold  ✓ Done")
            print(f"    [H] Harvest   {_cmd} harvest \\")
            print(f"                    --source /raw/ddl/ --project {project_dir}")
            print(f"    [I] Inspect   {_cmd} inspect \\")
            print(f"                    --source {project_dir}")
            print(f"    [P] Package   {_cmd} package \\")
            print(
                f"                    --source {project_dir} --env DEV --name {args.name} \\"
            )
            print("                    --env-config config/env/DEV.conf")
            print("    [S] Ship      python deploy.py --host <host> --user <user>")
            _hint = install_hint()
            if _hint is not None:
                print()
                for _line in _hint.splitlines():
                    print(f"    {_line}")

            # Repo-state hint (#487) — `ships stage`, `ships package`
            # (dirty-tree check), and `ships changeset` all rely on the
            # project sitting inside a git repo. Surface that requirement
            # at scaffold time so the operator isn't surprised later. Two
            # branches:
            #   - Already inside a repo → tell them which one (matters
            #     when the project is nested in a monorepo).
            #   - Not in a repo → suggest `git init` and explain why.
            from td_release_packager.stager import _default_git_repo_root

            _repo_root = _default_git_repo_root(project_dir)
            print()
            if _repo_root:
                if os.path.abspath(_repo_root) == os.path.abspath(project_dir):
                    print(f"    Git: project is the repo root ({_repo_root}).")
                else:
                    print(f"    Git: project is inside repo {_repo_root}.")
            else:
                print(
                    "    Tip: this project is not inside a git repo. "
                    "Run `git init` here\n"
                    "         if you plan to use `ships stage` or "
                    "version-control the payload."
                )

        print(f"{'=' * 64}\n")


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

    from td_release_packager.orchestrator import issue_codes as _ic

    try:
        with _stage_recording(args.project, "harvest") as stage:
            try:
                exit_code = _run_ingest(args, stage, _ic, apply_tokens)
            except FileNotFoundError as inner:
                # #495 — without this, the stage recorder marks the stage
                # ``status="error"`` (via its BaseException handler) but never
                # records WHY, leaving the decisions ledger and the pipeline
                # report with a red stage and a misleading "No issues
                # recorded." message. Stamp the offending path into the
                # ledger before re-raising so the existing exit path runs
                # unchanged.
                stage.add_issue("error", _ic.HARVEST_SOURCE_NOT_FOUND, str(inner))
                raise

            # #501/#505 — root-parent injection runs HERE rather than at
            # package time so the intervening ``ships inspect`` call (in
            # Detailed mode) sees the corrected payload. Quick mode's
            # ``ships process`` already does this between its harvest and
            # inspect substeps; this branch covers the Detailed-mode
            # operator who runs the verbs separately. The package-stage
            # injection on cli.py:3304 is now a no-op (idempotent — the
            # injector skips statements that already have a FROM clause).
            if exit_code == 0 and getattr(args, "root_parent", None):
                rp_injections = _apply_root_parent_option(
                    args.project, args.root_parent
                )
                if rp_injections:
                    print(
                        f"  Root parent: {rp_injections} parentless prereq(s) updated"
                    )
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)


def _run_ingest(args, stage, issue_codes, apply_tokens) -> int:
    """
    Body of the harvest command, factored out so ``_cmd_ingest``
    can wrap it in ``_stage_recording`` without indenting 250 lines.

    ``stage`` is either a real ``StageRecorder`` (project mode) or a
    ``_NullStageRecorder`` (ad-hoc mode); the call sites don't branch.

    Returns:
        The exit code for the shell. The caller calls ``sys.exit`` AFTER
        the recorder context closes — for the same reason as inspect
        (calling sys.exit inside trips the BaseException handler).
    """
    legacy_migration_rules = _load_project_legacy_migration_rules(args.project, stage)

    # -- Auto-tokenise: detect, derive, and apply in one pass --
    # When --auto-tokenise is set (item 9), skip the manual two-step
    # (harvest → review token_map.conf → re-harvest) by automatically
    # generating and applying the token map in a single run.
    auto_tokenise = getattr(args, "auto_tokenise", False)
    if auto_tokenise and apply_tokens is None:
        # Pass 1: detect only — no substitution
        detection = ingest_directory(
            source_dir=args.source,
            project_dir=args.project,
            detect_tokens=True,
            apply_tokens=None,
            force=args.force,
            clean_payload=not getattr(args, "keep_existing", False),
            legacy_migration_rules=legacy_migration_rules,
            remove_view_type_affixes=getattr(args, "remove_view_type_affixes", False),
        )
        if detection.token_candidates:
            env_prefix = getattr(args, "env_prefix", None)
            apply_tokens = generate_token_map(detection.token_candidates, env_prefix)
            stage.set_decisions(
                auto_tokenise=True,
                auto_derived_tokens=len(apply_tokens),
                env_prefix=env_prefix,
            )
            print(
                f"\n  Auto-tokenise: detected {len(detection.token_candidates)} "
                f"literal name(s) — derived {len(apply_tokens)} token(s)."
            )
        else:
            # Already tokenised — nothing to do
            stage.set_decisions(auto_tokenise=True, auto_derived_tokens=0)

    # -- Record resolved CLI configuration (Layer 5) --
    stage.set_config_resolved("source", args.source, "layer-5", "cli")
    stage.set_config_resolved("project", args.project, "layer-5", "cli")
    token_map_path = (
        _resolve_path(args.token_map, relative_to=args.project, label="--token-map")
        if hasattr(args, "token_map") and args.token_map
        else None
    )
    stage.set_config_resolved("token_map", token_map_path, "layer-5", "cli")
    stage.set_config_resolved(
        "apply_tokens_mode",
        "auto-tokenise"
        if auto_tokenise
        else (
            "token-map" if token_map_path else ("inline" if apply_tokens else "none")
        ),
        "layer-5",
        "cli",
    )
    stage.set_config_resolved(
        "clean_payload", not getattr(args, "keep_existing", False), "layer-5", "cli"
    )
    stage.set_config_resolved(
        "legacy_migration_rules",
        len(legacy_migration_rules),
        "layer-3" if legacy_migration_rules else "default",
        "project config" if legacy_migration_rules else "none",
    )
    stage.set_config_resolved(
        "remove_view_type_affixes",
        getattr(args, "remove_view_type_affixes", False),
        "layer-5",
        "cli",
    )

    # -- Parse --prefix-token SOURCE=TOKEN flags (Model B, issue #309) --
    prefix_tokens = _parse_prefix_token_args(getattr(args, "prefix_token", None) or [])
    if prefix_tokens:
        stage.set_config_resolved(
            "prefix_tokens",
            ", ".join(f"{src}={tok}" for src, tok in prefix_tokens.items()),
            "layer-5",
            "cli",
        )

    result = ingest_directory(
        source_dir=args.source,
        project_dir=args.project,
        detect_tokens=True,
        apply_tokens=apply_tokens,
        force=args.force,
        clean_payload=not args.keep_existing,
        legacy_migration_rules=legacy_migration_rules,
        remove_view_type_affixes=getattr(args, "remove_view_type_affixes", False),
        prefix_tokens=prefix_tokens,
    )

    if prefix_tokens and result.prefix_token_substitutions:
        print(
            f"  Prefix tokenised: "
            f"{', '.join(f'{src} -> {{{{{tok}}}}}' for src, tok in prefix_tokens.items())} "
            f"({result.prefix_token_substitutions} substitution(s) "
            f"across {result.prefix_token_files} file(s)). "
            f"Remember to define each token in every env config."
        )

    # -- Record inputs and outputs --
    stage.set_inputs(
        source_dir=args.source,
        total_files=result.total_files,
    )
    stage.set_outputs(
        classified=result.classified,
        unclassified=result.unclassified,
        files_placed=len(result.files_placed),
        multiset_injected=result.multiset_injected,
        token_candidates=len(result.token_candidates),
        cleaned=result.cleaned,
        binaries_placed=len(result.binaries_placed),
        legacy_migration_files=result.legacy_migration_files,
        legacy_migration_substitutions=result.legacy_migration_substitutions,
        placement_index_dir=result.placement_index_dir,
        placement_index_files=result.placement_index_files,
        view_type_affix_renames=result.view_type_affix_renames,
    )

    # -- Record issues --
    for f in result.unclassified_files:
        stage.add_issue("warning", issue_codes.HARVEST_UNCLASSIFIED, f)
    for w in result.classification_warnings:
        stage.add_issue("warning", issue_codes.HARVEST_CLASSIFICATION_WARNING, w)
    for db_name, files in result.token_candidates.items():
        stage.add_issue(
            "info",
            issue_codes.HARVEST_TOKEN_CANDIDATE,
            f"{db_name} ({len(files)} reference(s))",
        )

    # Stage-status rollup (#499) — mirror what _run_inspect does at the
    # bottom of its body. Without this, a harvest run with N warnings
    # leaves the stage at the default ``success`` status and the report
    # paints a ✓ next to the "N warnings" badge, which is visually
    # contradictory in the same way #495 was for the empty-issues case.
    if result.unclassified_files or result.classification_warnings:
        stage.set_status("warning")

    print(f"\n{'=' * 64}")
    print("  DDL Harvest Results")
    print(f"{'=' * 64}")
    print(f"  Source:           {args.source}")
    print(f"  Project:          {args.project}")
    if args.keep_existing:
        print("  Mode:             KEEP-EXISTING (overlay)")
        if args.force:
            print("                    + FORCE (overwrite collisions)")
    else:
        print("  Mode:             CLEAN (default — payload wiped first)")
    if result.cleaned:
        print(f"  Cleaned:          {result.cleaned} stale file(s)")
    if legacy_migration_rules:
        print(f"  Legacy rules:     {len(legacy_migration_rules)}")
    print(f"  Files scanned:    {result.total_files}")
    print(f"  Classified:       {result.classified}")
    if result.overwritten:
        print(f"  Overwritten:      {result.overwritten}")
    if result.skipped_existing:
        print(f"  Skipped (exist):  {result.skipped_existing}")
    print(f"  Unclassified:     {result.unclassified}")
    print(f"  MULTISET inject:  {result.multiset_injected}")
    if result.multi_table_targets:
        print(f"  Multi-table DML:  {len(result.multi_table_targets)} file(s)")
    if result.view_type_affix_renames:
        print(f"  View affix clean: {result.view_type_affix_renames} rename(s)")
    if result.legacy_migration_substitutions:
        print(
            "  Legacy migrated: "
            f"{result.legacy_migration_substitutions} substitution(s) "
            f"in {result.legacy_migration_files} file(s)"
        )
    if result.placement_index_dir:
        print(f"  Placement mirror: {result.placement_index_dir}")
        print(
            "                    grouped by owning database/token with "
            "plain-English placement hints"
        )

    if apply_tokens:
        print(f"  Tokens applied:   {len(apply_tokens)} mappings")

    if result.files_placed:
        print("\n  Files placed:")
        for src, dest, obj_type in result.files_placed:
            print(f"    {obj_type:15s} {src}")
            print(f"    {'':15s} → {dest}")

    if result.multi_table_targets:
        print("\n  Multi-table DML (kept together — order preserved):")
        for dest, targets in sorted(result.multi_table_targets.items()):
            print(f"    {dest}")
            for tgt in targets:
                print(f"      target → {tgt}")

    if result.unclassified_files:
        print("\n  Unclassified files (manual review needed):")
        for f in result.unclassified_files:
            print(f"    ⚠  {f}")

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

        print()
        print("  +-- Token map ----------------------------------------------+")
        print(f"  |   Path:     {map_path}")
        print(f"  |   Mappings: {len(token_map)}")
        if env_prefix:
            print(f"  |   Prefix:   {env_prefix} (stripped from token names)")
        else:
            print("  |   Prefix:   none (full names used as tokens)")
        print("  +------------------------------------------------------------+")

        CAP = 10
        sorted_mappings = sorted(token_map.items())
        print(
            f"\n  Sample mappings (showing {min(CAP, len(token_map))} of {len(token_map)}):"
        )
        for literal, token in sorted_mappings[:CAP]:
            files = result.token_candidates.get(literal, [])
            print(f"    {literal} → {token}  ({len(files)} refs)")
        if len(token_map) > CAP:
            print(f"    ... {len(token_map) - CAP} more — see the file above.")

        print(f"\n  ✓ Token map written to: {map_path}")

    elif generate_map and not result.token_candidates:
        print(
            "\n  ✓ No hardcoded database names detected.\n"
            "    The source DDL appears to be already tokenised — you're at\n"
            "    the end-state most projects have to work toward. Skip the\n"
            "    token map and go straight to .conf bootstrap below."
        )

    elif result.token_candidates and not apply_tokens:
        print("\n  Token candidates (hardcoded database names):")
        for db_name, files in sorted(result.token_candidates.items()):
            print(f"    '{db_name}' ({len(files)} refs)")
        # These names were left literal — no tokenisation ran this harvest.
        # Spell out why and how to fix it now, so a hardcoded payload is not
        # first discovered two stages later as inspect 'hardcoded_name'
        # warnings (issue #409).
        if legacy_migration_rules:
            print(
                "\n  ⚠  config/tokenise.conf was applied, but the names above "
                "matched no rule —\n"
                "    check its prefix/pattern. They will surface as "
                "'hardcoded_name' warnings in inspect."
            )
        else:
            print(
                "\n  ⚠  No tokenisation ran — the names above are still literal "
                "and will surface\n"
                "    as 'hardcoded_name' warnings in inspect. To tokenise them, "
                "do one of:\n"
                "      • add config/tokenise.conf (the SHIPS Navigator writes "
                "one) and re-harvest\n"
                "      • re-run harvest with --auto-tokenise [--env-prefix "
                "<PREFIX>]\n"
                "      • re-run with --generate-token-map --env-prefix <PREFIX> "
                "for a reviewable map\n"
                "    If the names are intentionally fixed, set "
                "'hardcoded_name = OFF' in config/inspect.conf."
            )

    if result.warnings:
        print("\n  Warnings:")
        for w in result.warnings:
            print(f"    ⚠  {w}")

    if result.classification_warnings:
        print("\n  Classification warnings:")
        for w in result.classification_warnings:
            print(f"    ⚠  {w}")

    if result.subtypes:
        from collections import Counter

        subtype_counts = Counter(result.subtypes.values())
        print("\n  Sub-types detected:")
        for subtype, count in sorted(subtype_counts.items()):
            print(f"    {subtype:20s} {count}")

    if result.external_references:
        print("\n  External references discovered:")
        for staged_path, refs in sorted(result.external_references.items())[:5]:
            print(f"    {staged_path}")
            for ref in refs:
                print(f"        → {ref}")
        extra = len(result.external_references) - 5
        if extra > 0:
            print(f"    ... and {extra} more file(s) with externals.")

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

    if result.legacy_placeholders:
        from td_release_packager.legacy_placeholders import (
            format_legacy_placeholders_report,
        )

        print(
            format_legacy_placeholders_report(
                result.legacy_placeholders,
                source_dir=args.source,
                project_dir_hint=args.project,
            )
        )

    _print_harvest_next_steps(
        args=args,
        generated_token_map_path=generated_token_map_path,
        substitutions_applied=bool(apply_tokens),
        already_tokenised=(generate_map and not result.token_candidates),
    )

    return 0


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

    Runs three steps:
        Step 0 — Token format check (token_engine.scan_malformed)
        Step 1 — Per-file DDL lint (validate.py)
        Step 2 — Cross-file grant validation (validate_grants.py)

    The overall result is PASSED only if all enabled steps pass.

    Refactored onto the orchestrator (build-order item 4b): wraps the
    existing logic in ``_stage_recording`` so projects with a SHIPS
    layout grow a ``ships.decisions.json`` entry per inspect run while ad-
    hoc invocations against arbitrary directories see identical
    stdout and zero filesystem litter.

    The actual exit code is computed inside ``_run_inspect`` and
    surfaced AFTER the recording context manager closes — calling
    ``sys.exit`` inside ``with _stage_recording`` would trip the
    recorder's BaseException handler and force every run to record
    status="error", swamping the manifest with false errors.
    """
    from td_release_packager.orchestrator import issue_codes

    try:
        with _stage_recording(args.project, "inspect") as stage:
            exit_code = _run_inspect(args, stage, issue_codes)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)


def _run_inspect(args, stage, issue_codes) -> int:
    """
    Body of the inspect command, factored out so ``_cmd_validate``
    can wrap it in ``_stage_recording`` without indenting 250 lines.

    ``stage`` is either a real ``StageRecorder`` (project mode) or a
    ``_NullStageRecorder`` (ad-hoc mode); the call sites don't have
    to branch.

    Returns:
        The exit code to surface to the shell. The caller is
        responsible for calling ``sys.exit`` AFTER the recorder
        context closes — see ``_cmd_validate`` for why.
    """
    from pathlib import Path

    try:
        # -- Record the resolved CLI configuration (Layer 5) --
        # Future cascade work plugs in additional layers without
        # changing how the call sites read config.
        stage.set_config_resolved("source", args.project, "layer-5", "cli")
        stage.set_config_resolved(
            "config", getattr(args, "config", None), "layer-5", "cli"
        )
        stage.set_config_resolved(
            "strict", getattr(args, "strict", False), "layer-5", "cli"
        )
        stage.set_config_resolved(
            "skip_grants", getattr(args, "skip_grants", False), "layer-5", "cli"
        )

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
            payload_dir = _find_payload_dir(args.project)
        except FileNotFoundError:
            # No payload dir — fall back to scanning the project root.
            # Hidden/underscore-prefixed files are skipped by the
            # scanner's own rules, so this is safe even if args.project
            # is broader than expected.
            payload_dir = args.project

        token_findings = scan_malformed_tokens_in_directory(payload_dir)
        token_ok = not token_findings

        token_icon = _status_icon(token_ok)
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
            # Record one issue per malformed marker so explain can
            # group findings by file. Each finding is a dict with
            # line/column/marker keys (see token_engine.find_malformed_tokens).
            for file_path, findings in token_findings.items():
                for finding in findings:
                    stage.add_issue(
                        "error",
                        issue_codes.INSPECT_TOKEN_MALFORMED,
                        (
                            f"Malformed token marker '{finding['marker']}' "
                            f"in {file_path} at line {finding['line']}, "
                            f"col {finding['column']}"
                        ),
                        location=f"{file_path}:{finding['line']}:{finding['column']}",
                    )
        else:
            print("  All {{TOKEN}} markers are well-formed.")

        # ==============================================================
        # Step 0b — Token coverage check (PR2)
        # ==============================================================
        # PR2 invariant (HANDOVER-ships-deterministic-deploy.md §PR2):
        # no payload should pass inspect and fail package on the
        # undefined-token scan. Inspect runs without a specific target
        # env, so we cross-check every ``config/env/*.conf`` and treat
        # any token undefined in any env as a coverage failure here.
        # The same helper, ``validate_payload_token_coverage``, will be
        # the entry point package's build can adopt in a follow-up
        # rather than maintaining a separate per-env scan.
        from td_release_packager.token_engine import (
            validate_payload_token_coverage,
        )

        # Coverage failures are tracked separately from malformed-token
        # findings (issue #385): the two are distinct Step 0 outcomes and
        # the final summary must not describe a coverage failure using
        # malformed-marker counters. Hoisted here so they survive past the
        # coverage block into the overall-result summary below.
        coverage_undefined: Set[str] = set()
        coverage_envs_failed = 0

        coverage_by_env = validate_payload_token_coverage(payload_dir, args.project)
        if not coverage_by_env:
            # No env configs found — coverage cannot be verified.
            # Surface as an INFO note rather than a silent pass.
            print(
                "  ℹ Token coverage not verified — no env configs "
                "discovered under config/env/. Add at least one "
                "<ENV>.conf to gate undefined tokens at inspect time."
            )
            stage.add_issue(
                "info",
                issue_codes.TOKEN_UNDEFINED,
                "Token coverage check skipped: no config/env/*.conf "
                "files discovered under the project. Inspect cannot "
                "guarantee package will not fail on undefined tokens.",
            )
        else:
            for env_name, summary in sorted(coverage_by_env.items()):
                if not summary["undefined"]:
                    continue
                coverage_undefined.update(summary["undefined"])
                coverage_envs_failed += 1
                print()
                print(
                    f"  ✗ Token coverage [{env_name}]: "
                    f"{len(summary['undefined'])} undefined token(s) "
                    f"in {summary['config_path']}"
                )
                for tok in summary["undefined"]:
                    refs = summary["token_files"].get(tok, [])
                    fn_refs = summary["filename_tokens"].get(tok, [])
                    print(f"    {{{{{tok}}}}}")
                    for ref in refs:
                        try:
                            rel = os.path.relpath(ref, args.project)
                        except ValueError:
                            rel = ref
                        print(f"      content -> {rel}")
                    for rel in fn_refs:
                        print(f"      filename -> {rel}")
                    # One issue per (env, token) — matches package's
                    # one-per-token granularity so explain views show
                    # parity between the two surfaces.
                    location = refs[0] if refs else (fn_refs[0] if fn_refs else None)
                    stage.add_issue(
                        "error",
                        issue_codes.TOKEN_UNDEFINED,
                        (
                            f"Token '{{{{{tok}}}}}' is referenced in the "
                            f"payload but not defined in {env_name} "
                            f"({summary['config_path']}). Package would "
                            f"fail at substitution for this env."
                        ),
                        location=location,
                    )
                token_ok = False
            if not coverage_undefined:
                print(
                    f"  All payload tokens are defined across "
                    f"{len(coverage_by_env)} env config(s)."
                )

        # ==============================================================
        # Step 1 — Per-file DDL lint
        # ==============================================================

        # -- Load rules config --
        rules_config = None
        if hasattr(args, "config") and args.config:
            config_path = _resolve_path(
                args.config,
                relative_to=args.project,
                label="--config",
            )
            rules_config = read_inspect_config(config_path)
        else:
            # Auto-detect config in project's config/ directory
            auto_config = os.path.join(args.project, "config", "inspect.conf")
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

        # NOTE: `ships inspect` no longer performs any payload writes.
        # The auto-fixers for `ddl_terminator` and `non_ascii` moved to
        # `ships fix` in #522 — see td_release_packager.fixers. `ships
        # process` runs fix as a pipeline stage once #523 lands; for now,
        # run `ships fix` before `ships inspect` if you want auto-fixes
        # applied prior to the lint pass.
        # -- Load custom lint policy (issue #167) --
        # Fail closed in strict mode; warn-and-ignore a structurally broken
        # policy in developer mode (rule-level errors are skipped by the
        # loader itself). Patterns are matched as data — never executed.
        from td_release_packager.lint_policy import (
            POLICY_FILENAME,
            LintPolicyError,
            load_lint_policy,
        )

        custom_rules = []
        try:
            custom_rules = load_lint_policy(args.project, strict=args.strict)
        except LintPolicyError as exc:
            if args.strict:
                print(
                    f"\n  ✗ Custom lint policy invalid (strict mode): {exc}",
                    file=sys.stderr,
                )
                stage.add_issue(
                    "error",
                    issue_codes.INSPECT_LINT_VIOLATION,
                    f"Custom lint policy invalid: {exc}",
                    location=f"config/{POLICY_FILENAME}",
                )
                return 1
            print(f"\n  ⚠  Custom lint policy ignored — {exc}", file=sys.stderr)
            stage.add_issue(
                "warning",
                issue_codes.INSPECT_LINT_VIOLATION,
                f"Custom lint policy ignored: {exc}",
                location=f"config/{POLICY_FILENAME}",
            )
            custom_rules = []

        if custom_rules:
            print(
                f"  Custom lint rules: {len(custom_rules)} loaded from "
                f"config/{POLICY_FILENAME}"
            )

        lint_result = validate_directory(
            source_dir=resolve_inspect_root(args.project),
            rules_config=rules_config,
            strict=args.strict,
            custom_rules=custom_rules,
        )

        # -- Non-linear package history (issue #168) --
        # Project-level check over <project>/releases/. Folded into the lint
        # result so its findings flow through the same pass/fail accounting,
        # console recap, and ships.decisions.json recording as the per-file
        # rules. Severity honours config/inspect.conf (+ --strict promotion).
        ph_severity = rules_config.get("non_linear_package_history", "WARNING")
        if ph_severity == "WARN":
            ph_severity = "WARNING"
        if args.strict and ph_severity == "WARNING":
            ph_severity = "ERROR"
        if ph_severity != "OFF":
            from td_release_packager.package_history import check_package_history

            ph_issues = check_package_history(args.project, severity=ph_severity)
            if ph_issues:
                lint_result.issues.extend(ph_issues)

        # -- Unreferenced databases (issues #475, #479) --
        # Project-level informational note over payload/database/pre-requisites/.
        # Surfaces database/user declarations the rest of the payload doesn't
        # reference. Most often a legitimate empty container; occasionally a
        # naming-convention crossfire where two CREATE DATABASE statements name
        # the same logical role under different identifiers and only one ends
        # up populated.
        ud_severity = rules_config.get("unreferenced_database", "WARNING")
        if ud_severity == "WARN":
            ud_severity = "WARNING"
        if args.strict and ud_severity == "WARNING":
            ud_severity = "ERROR"
        if ud_severity != "OFF":
            from td_release_packager.unreferenced_database import (
                check_unreferenced_databases,
            )

            ud_issues = check_unreferenced_databases(args.project, severity=ud_severity)
            if ud_issues:
                lint_result.issues.extend(ud_issues)

        # -- Backward-incompatible contract changes (issue #171) --
        # Project-level: compares current payload contracts against the
        # captured baseline (.ships/contracts.baseline.json). No-op until a
        # baseline exists. --update-contract-baseline (re)captures it instead.
        from td_release_packager import contract as _contract
        from td_release_packager.project_paths import contracts_baseline_path

        _inspect_root = resolve_inspect_root(args.project)
        if getattr(args, "update_contract_baseline", False):
            _bpath = contracts_baseline_path(args.project)
            _contract.write_baseline(_bpath, _contract.build_contracts(_inspect_root))
            print(f"\n  ✎ Contract baseline updated: {_bpath}")
        else:
            cc_severity = rules_config.get("contract_change", "WARNING")
            if cc_severity == "WARN":
                cc_severity = "WARNING"
            if args.strict and cc_severity == "WARNING":
                cc_severity = "ERROR"
            if cc_severity != "OFF":
                cc_issues = _contract.check_contract_changes(
                    args.project, _inspect_root, severity=cc_severity
                )
                if cc_issues:
                    lint_result.issues.extend(cc_issues)

        # Recompute counts after folding in project-level findings (#168/#171).
        lint_result.errors = sum(1 for i in lint_result.issues if i.severity == "ERROR")
        lint_result.warnings = sum(
            1 for i in lint_result.issues if i.severity == "WARNING"
        )

        lint_icon = _status_icon(lint_result.passed)
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

        # Fixable-finding lookup (issue #524). A finding is "fixable" when
        # a fixer is registered in td_release_packager.fixers.FIX_REGISTRY
        # for its rule id AND the rule is not a custom-policy rule (which
        # never has a built-in fixer). Compute once so both the human
        # print loop and the per-finding stage.add_issue calls agree.
        from td_release_packager.fixers import FIX_REGISTRY

        custom_rule_names = {r.name for r in custom_rules}

        def _is_fixable(rule_id: str) -> bool:
            return rule_id in FIX_REGISTRY and rule_id not in custom_rule_names

        if lint_result.issues:
            # Group by file
            by_file = {}
            for issue in lint_result.issues:
                by_file.setdefault(issue.file, []).append(issue)

            print("\n  Issues by file:")
            for file, issues in sorted(by_file.items()):
                err_count = sum(1 for i in issues if i.severity == "ERROR")
                file_icon = "✗" if err_count > 0 else "⚠"
                print(f"\n    {file_icon}  {file}")
                for issue in issues:
                    if issue.severity == "ERROR":
                        sev = "✗"
                    elif issue.severity == "WARNING":
                        sev = "⚠"
                    else:
                        sev = "ℹ"
                    # Inline "— fixable (run `ships fix`)" tag for findings
                    # whose rule has a registered fixer. Signals the
                    # cheapest remediation path at the finding line rather
                    # than making the reader consult docs.
                    fix_tag = (
                        "  — fixable (run `ships fix`)"
                        if _is_fixable(issue.rule)
                        else ""
                    )
                    print(f"      {sev}  [{issue.rule}] {issue.message}{fix_tag}")

            # Fixable-count summary line — mirrors the report tone and
            # gives the reader a one-command remediation for the trivial
            # cases without having to eyeball every finding.
            fixable_count = sum(1 for i in lint_result.issues if _is_fixable(i.rule))
            if fixable_count:
                total = len(lint_result.issues)
                print(
                    f"\n  {total} finding(s), {fixable_count} auto-fixable — "
                    f"run `ships fix` to apply."
                )
            else:
                total = len(lint_result.issues)
                print(
                    f"\n  {total} finding(s), none auto-fixable — see "
                    "remediation notes above."
                )

            # Record one ships.decisions.json issue per lint finding. The
            # rule name is carried in the message so explain can
            # group by rule even though the issue code is coarse.
            # Custom-policy findings (issue #167) use a distinct code so
            # agents/CI can tell them apart from built-in Coding Discipline
            # violations; both may carry remediation metadata in details
            # (e.g. the built-in destructive_change rule, issue #169).
            for issue in lint_result.issues:
                # Map validate.py severities into the recorder's
                # vocabulary: ERROR/WARNING/INFO → error/warning/info.
                rec_severity = issue.severity.lower()
                if rec_severity not in ("error", "warning", "info"):
                    rec_severity = "warning"
                location = issue.file
                if issue.line is not None:
                    location = f"{issue.file}:{issue.line}"
                remediation = getattr(issue, "remediation", None)
                # Three buckets of finding flow through this list:
                #   1. Per-file Coding Discipline rules → INSPECT_LINT_VIOLATION
                #   2. Custom lint policy rules → INSPECT_CUSTOM_POLICY
                #   3. Project-level package-integrity check over releases/
                #      → INSPECT_PACKAGE_INTEGRITY (issue #452, not lint)
                if issue.rule == _PACKAGE_HISTORY_RULE:
                    code = issue_codes.INSPECT_PACKAGE_INTEGRITY
                elif issue.rule in custom_rule_names:
                    code = issue_codes.INSPECT_CUSTOM_POLICY
                else:
                    code = issue_codes.INSPECT_LINT_VIOLATION
                # Enrich the details dict with fixability metadata
                # (#524). Merges cleanly with any existing per-rule
                # remediation payload so custom rules that carry their
                # own details are preserved.
                details = dict(remediation) if remediation else {}
                if _is_fixable(issue.rule):
                    details["fixable"] = True
                    details["fixer_rule_id"] = issue.rule
                stage.add_issue(
                    rec_severity,
                    code,
                    f"[{issue.rule}] {issue.message}",
                    location=location,
                    details=details or None,
                )

        if lint_result.passed:
            print("\n  All files conform to the Teradata Engineering Discipline.")

        print(f"{'=' * 64}")

        # ==============================================================
        # Step 2 — Cross-file grant validation
        # ==============================================================

        skip_grants = getattr(args, "skip_grants", False)
        # #526 removed --fix-grants from `ships inspect` — the fixer lives in
        # the `ships fix` verb now. Inspect is strictly read-only. Any caller
        # still setting fix_grants=True on a synthesised Namespace (e.g. an
        # older process pipeline) gets a validate-only pass here.
        dcl_dir = None
        if hasattr(args, "dcl_dir") and args.dcl_dir:
            dcl_dir = Path(args.dcl_dir)

        project_dir = Path(args.project).resolve()
        grant_result = None

        # Read grant-drift severities from inspect.conf. These are severity
        # settings, not booleans:
        #   ERROR        blocks package trust
        #   WARNING/WARN reports but does not block
        #   OFF          suppresses the finding
        #
        # warn_extra_grants applies only to extra-only drift. Drift that also
        # has missing inferred privileges remains a hard error because required
        # access is absent from the DCL.
        from td_release_packager.validate import read_severity_from_inspect_config

        warn_external_grants_severity = read_severity_from_inspect_config(
            rules_config,
            "warn_external_grants",
            strict=getattr(args, "strict", False),
        )
        warn_extra_grants_severity = read_severity_from_inspect_config(
            rules_config,
            "warn_extra_grants",
            strict=getattr(args, "strict", False),
        )

        def _grant_issue_enabled(severity: str) -> bool:
            """Return true when a configured grant severity should be emitted."""
            return severity != "OFF"

        def _grant_issue_blocks(severity: str) -> bool:
            """Return true when a configured grant severity should block trust."""
            return severity == "ERROR"

        def _effective_grant_passed(result) -> bool:
            """Compute grant_ok using severity-valued grant drift settings."""
            for status in result.statuses:
                # Missing .dcl files and missing inferred privileges are always
                # hard errors. The warn_* settings control extra-only drift and
                # orphaned DCL files only.
                if status.missing:
                    return False
                if status.drifted and status.missing_privs:
                    return False
                if (
                    status.drifted
                    and status.extra_privs
                    and _grant_issue_blocks(warn_extra_grants_severity)
                ):
                    return False
                if status.orphaned and _grant_issue_blocks(
                    warn_external_grants_severity
                ):
                    return False
            return True

        def _rel_grant_path(status) -> str:
            """Return ``status.file_path`` relative to the project root."""
            try:
                project_root = Path(args.project).resolve()
                return os.path.relpath(status.file_path, project_root)
            except (ValueError, AttributeError):
                return str(getattr(status, "file_path", ""))

        def _privs_summary(privs_map: Dict[str, Set[str]]) -> str:
            """Render a ``{grantor: {privs}}`` mapping as prose.

            Output is ``priv, priv on object; priv on object`` — no
            wrapping brackets. Used inside larger sentences in the
            user-facing grant report messages.
            """
            if not privs_map:
                return "(none)"
            return "; ".join(
                f"{', '.join(sorted(privs))} on {grantor}"
                for grantor, privs in sorted(privs_map.items())
            )

        def _format_missing_grant(status) -> str:
            """Render a ``GranteeStatus`` for a missing-grant finding.

            The dataclass ``repr`` includes ``WindowsPath`` and
            ``defaultdict(<class 'set'>, …)`` noise that's unreadable
            in a report. Render the salient fields directly: the
            grantee, what was expected to be granted, and the relative
            path of the .grt file SHIPS expected to find.
            """
            expected = _privs_summary(getattr(status, "expected_grants", {}))
            return (
                f"{status.grantee} expects {expected} "
                f"but no entry exists at {_rel_grant_path(status)}"
            )

        def _format_drifted_grant(status) -> str:
            """Render a ``GranteeStatus`` for a drifted-grant finding.

            Produces user-friendly prose without the previous square-
            bracket dump. Missing privileges are reported as "Required
            by the package payload but absent from the .dcl"; extras
            are reported as "specified but not required by the package
            payload" — emphasising that the operator added them
            beyond what SHIPS infers.
            """
            parts = [f"{status.grantee} at {_rel_grant_path(status)}"]
            missing = getattr(status, "missing_privs", {}) or {}
            extras = getattr(status, "extra_privs", {}) or {}
            if missing:
                parts.append(
                    "required by the package payload but absent from the .dcl: "
                    f"{_privs_summary(missing)}"
                )
            if extras:
                parts.append(
                    "grants specified but not required by the package payload: "
                    f"{_privs_summary(extras)}"
                )
            return ". ".join(parts)

        def _format_external_grant(status) -> str:
            """Render a ``GranteeStatus`` for an external-grant finding.

            "External" because the grantee (role, database, or user)
            lives outside the package's DDL intent — i.e. SHIPS found
            no DDL in this package that would require access to be
            granted to that grantee. Often legitimate (roles managed
            by a DBA, IGA system, or another package) and so reported
            at INFO by default; promote ``warn_external_grants`` to
            ERROR in inspect.conf for self-contained packages where
            every grant must be traceable to in-package DDL.
            """
            return (
                f"{status.grantee} at {_rel_grant_path(status)} — "
                f"granted access by this package but no DDL in the package "
                f"implies the grant"
            )

        def _grant_suffix(result) -> str:
            """Build a parenthetical suffix describing non-error grant modes."""
            parts = []
            if result.drifted_extra_only and warn_extra_grants_severity != "ERROR":
                parts.append(
                    f"{len(result.drifted_extra_only)} extra-only drift "
                    f"({warn_extra_grants_severity.lower()} — warn_extra_grants)"
                )
            if result.orphaned and warn_external_grants_severity != "ERROR":
                parts.append(
                    f"{len(result.orphaned)} external "
                    f"({warn_external_grants_severity.lower()} — warn_external_grants)"
                )
            return f"  [{', '.join(parts)}]" if parts else ""

        grants_files_written = 0
        if skip_grants:
            print("\n  ℹ Grant validation skipped (--skip-grants)")
        else:
            # -- Validate mode: compare and report (read-only) --
            # The fixer moved to `ships fix` (default-on) in #526. Inspect
            # never writes to payload/ under any argument combination now.
            grant_result = validate_grants(
                project_dir,
                dcl_dir=dcl_dir,
                verbose=args.verbose,
            )

            _grant_passed = _effective_grant_passed(grant_result)
            grant_icon = _status_icon(_grant_passed)
            grant_status = "PASSED" if _grant_passed else "FAILED"

            print(f"\n{'=' * 64}")
            print(
                f"  {grant_icon} Step 2: Grant Validation — {grant_status}"
                f"{_grant_suffix(grant_result)}"
            )
            print(f"{'=' * 64}")
            print(
                format_grant_report(
                    grant_result,
                    extra_grants_severity=warn_extra_grants_severity,
                    external_grants_severity=warn_external_grants_severity,
                )
            )
            print(f"{'=' * 64}")

        # ==============================================================
        # Step 3 — Static database hierarchy PERM capacity
        # ==============================================================

        from td_release_packager.hierarchy_perm_analyser import (
            analyse_hierarchy_perm_capacity,
            format_hierarchy_perm_report,
        )

        hierarchy_result = analyse_hierarchy_perm_capacity(payload_dir)
        hierarchy_ok = hierarchy_result.passed
        hierarchy_icon = _status_icon(hierarchy_ok)
        hierarchy_status = "PASSED" if hierarchy_ok else "FAILED"

        print(f"\n{'=' * 64}")
        print(
            f"  {hierarchy_icon} Step 3: Database Hierarchy PERM Capacity — "
            f"{hierarchy_status}"
        )
        print(f"{'=' * 64}")
        print(format_hierarchy_perm_report(hierarchy_result))
        print(f"{'=' * 64}")

        for finding in hierarchy_result.findings:
            if finding.passed:
                continue
            stage.add_issue(
                "error",
                issue_codes.INSPECT_LINT_VIOLATION,
                f"[database_hierarchy_perm_capacity] {finding.message}",
                location=finding.parent_source_file or finding.parent_name,
            )

        # ==============================================================
        # Overall result
        # ==============================================================

        lint_ok = lint_result.passed
        grant_ok = (
            True if grant_result is None else _effective_grant_passed(grant_result)
        )
        overall_ok = token_ok and lint_ok and grant_ok and hierarchy_ok

        overall_icon = _status_icon(overall_ok)
        overall_status = "PASSED" if overall_ok else "FAILED"

        print(f"\n{'=' * 64}")
        print(f"  {overall_icon} SHIPS Inspect — {overall_status}")
        print(f"{'=' * 64}")

        # -- Step 0 line: token format + coverage checks --
        # Step 0 has two independent sub-checks — malformed-marker format
        # and per-env coverage. Report whichever actually failed; a
        # coverage failure must not be described with malformed-marker
        # counters (issue #385).
        if token_ok:
            print("  Step 0 (Tokens): PASSED")
        else:
            reasons = []
            if token_findings:
                n_files = len(token_findings)
                n_issues = sum(len(v) for v in token_findings.values())
                reasons.append(f"{n_issues} malformed marker(s) in {n_files} file(s)")
            if coverage_undefined:
                reasons.append(
                    f"{len(coverage_undefined)} undefined token(s) across "
                    f"{coverage_envs_failed} env config(s)"
                )
            print(f"  Step 0 (Tokens): FAILED — {'; '.join(reasons)}")

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
            suffix = _grant_suffix(grant_result) if grant_result else ""
            print(f"  Step 2 (Grants): PASSED — {n} grantees consistent{suffix}")
        else:
            d = len(
                [
                    status
                    for status in grant_result.drifted
                    if status.missing_privs or warn_extra_grants_severity != "OFF"
                ]
            )
            m = len(grant_result.missing)
            o = (
                0
                if warn_external_grants_severity == "OFF"
                else len(grant_result.orphaned)
            )
            print(f"  Step 2 (Grants): FAILED — {d} drifted, {m} missing, {o} external")

        # -- Step 3 line
        if hierarchy_ok:
            print(
                "  Step 3 (Hierarchy PERM): PASSED — "
                f"{len(hierarchy_result.findings)} parent container(s) checked"
            )
        else:
            print(
                "  Step 3 (Hierarchy PERM): FAILED — "
                f"{hierarchy_result.errors} insufficient parent allocation(s)"
            )

        # -- Top-failures recap: keeps actionable detail visible even
        #    when the long per-file output has scrolled off the terminal.
        if not lint_ok:
            recap = _format_lint_recap(lint_result)
            if recap:
                print()
                print(recap)

        if not grant_ok:
            recap = _format_grant_recap(
                grant_result,
                extra_grants_severity=warn_extra_grants_severity,
                external_grants_severity=warn_external_grants_severity,
            )
            if recap:
                print()
                print(recap)

        print(f"{'=' * 64}\n")

        # ==============================================================
        # ships.decisions.json — record grants, inputs, outputs, status
        # ==============================================================
        # Done after the human-facing report so an interruption mid-
        # report still leaves the printed output intact. The recorder
        # itself was set up at the top of this function — we only
        # need to attach the per-step findings now.
        if grant_result is not None:
            # #526 removed the "auto-generated N .grt files" info line
            # from this stage — the fixer moved to `ships fix`. Inspect
            # only reports drift and orphans now.
            # Drifted entries — severity depends on whether all drift is
            # extra-only (manually added grants) or includes missing privs
            # (inferred grants absent from the .dcl file).
            for entry in grant_result.drifted:
                if entry.missing_privs:
                    # Missing-privs drift: the DDL implies a grant that is
                    # absent from the .dcl file. Always a hard error.
                    stage.add_issue(
                        "error",
                        issue_codes.INSPECT_GRANT_DRIFT,
                        f"Drifted grant: {_format_drifted_grant(entry)}",
                    )
                elif _grant_issue_enabled(warn_extra_grants_severity):
                    # Extra-only drift is controlled by warn_extra_grants.
                    stage.add_issue(
                        warn_extra_grants_severity.lower(),
                        issue_codes.INSPECT_GRANT_DRIFT,
                        f"Drifted grant: {_format_drifted_grant(entry)}",
                    )
            for entry in grant_result.missing:
                # Missing grants are derivable from intent — the DDL
                # implies the grant, the operator just hasn't written
                # the matching .grt entry. Since #526 removed inspect's
                # --fix-grants flag, `ships fix` is the only auto-generator.
                stage.add_issue(
                    "info",
                    issue_codes.INSPECT_GRANT_MISSING,
                    f"Missing grant: {_format_missing_grant(entry)}. "
                    f"Run `ships fix` to auto-generate.",
                )
            for entry in grant_result.orphaned:
                if _grant_issue_enabled(warn_external_grants_severity):
                    stage.add_issue(
                        warn_external_grants_severity.lower(),
                        issue_codes.INSPECT_GRANT_EXTERNAL,
                        f"External grant: {_format_external_grant(entry)}",
                    )

        stage.set_inputs(
            source_dir=args.project,
            payload_dir=payload_dir,
            files_scanned=lint_result.files_scanned,
            grant_validation_skipped=skip_grants,
            # Inspect is read-only after #526 — the grants fixer moved to
            # `ships fix`, so this is always False now.
            grant_validation_fix_mode=False,
        )
        stage.set_outputs(
            token_format_passed=token_ok,
            lint_passed=lint_ok,
            grants_passed=grant_ok,
            hierarchy_perm_passed=hierarchy_ok,
            hierarchy_perm_errors=hierarchy_result.errors,
            overall_passed=overall_ok,
            lint_errors=lint_result.errors,
            lint_warnings=lint_result.warnings,
            files_with_issues=lint_result.files_with_issues,
        )

        # The recorder auto-rolls up "error" issues into the run's
        # final_status. Warnings need an explicit set_status to
        # surface in ships.decisions.json — otherwise a clean run that
        # only emitted warnings would still report status="success".
        if not overall_ok:
            stage.set_status("error")
        elif lint_result.warnings or (grant_result and not grant_ok):
            stage.set_status("warning")
        else:
            stage.set_status("success")

        # Return — DO NOT sys.exit inside the recording context.
        # See _cmd_validate's docstring for why.
        return 0 if overall_ok else 1

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


def _cmd_repackage(args):
    """Rebuild an edited extracted package directory."""
    try:
        from td_release_packager.builder import (
            _resolve_repackage_package_dir,
            repackage_package_dir,
        )

        resolved_package_dir = _resolve_repackage_package_dir(args.package_dir)
        archive_path, manifest = repackage_package_dir(
            args.package_dir,
            strict=getattr(args, "strict", False),
        )
        print("\nSHIPS repackage complete")
        print(f"  Package dir: {resolved_package_dir}")
        if resolved_package_dir != args.package_dir:
            print(f"  Input path:  {args.package_dir}")
        print(f"  Archive:     {archive_path}")
        print(f"  Checksum:    {archive_path}.sha256")
        from td_release_packager.trust import STATUS_BLOCKED, load_trust_result

        trust_doc = load_trust_result(resolved_package_dir) or {}
        trust_status = trust_doc.get("status", "UNKNOWN")
        print(f"  Trust:       {trust_status}")
        if trust_status == STATUS_BLOCKED:
            print("\nPackage remains BLOCKED.")
            print(
                "  Replace DBA placeholders in the generated payload files, then rerun:"
            )
            print(
                f'  {ships_cmd()} repackage --package-dir "{args.package_dir}" --strict'
            )
            sys.exit(1 if getattr(args, "strict", False) else 0)
        sys.exit(0)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_fix(args) -> int:
    """Apply registered auto-fixes to a SHIPS project's payload/ / config/.

    Dispatches to :data:`td_release_packager.fixers.FIX_REGISTRY`, resolving
    the rule set from the flags: ``--rules a,b,c`` explicit; ``--all`` every
    registered fixer; nothing → the default-on subset.

    Exit codes follow the ruff convention:

    * ``0`` — success (either nothing to do, or writes applied cleanly).
    * ``1`` — ``--dry-run`` and changes would have been made (CI gate signal).
    * ``2`` — a fixer raised or an argument was invalid (unknown rule id,
      missing project, etc.). Errors that individual fixers absorb into
      :attr:`FixResult.errors` don't bump the exit code by themselves, but
      any non-empty ``errors`` list is surfaced in the report.
    """
    import json

    from td_release_packager.fixers import FIX_REGISTRY, default_on_rules

    project = args.project
    if not os.path.isdir(project):
        _emit_fix_error(args, f"project directory does not exist: {project!r}")
        return 2

    # Resolve the rule set. --rules and --all are mutually exclusive at
    # argparse level (mutually_exclusive_group), so at most one is set here.
    if getattr(args, "all_rules", False):
        selected = sorted(FIX_REGISTRY.keys())
    elif getattr(args, "rules", None):
        raw = [r.strip() for r in args.rules.split(",") if r.strip()]
        unknown = [r for r in raw if r not in FIX_REGISTRY]
        if unknown:
            available = ", ".join(sorted(FIX_REGISTRY.keys())) or "(none)"
            _emit_fix_error(
                args,
                f"unknown rule id(s) in --rules: {', '.join(sorted(unknown))}. "
                f"Registered fixers: {available}",
            )
            return 2
        selected = raw
    else:
        selected = default_on_rules()

    # Run each selected fixer. Any exception from a fixer aborts the run
    # with exit code 2 — that's an infrastructure failure (I/O, malformed
    # source, walker crash), not the kind of per-file error a fixer should
    # be recording in FixResult.errors and continuing past.
    dry_run = bool(getattr(args, "dry_run", False))
    results = []
    for rule_id in selected:
        spec = FIX_REGISTRY[rule_id]
        try:
            result = spec.apply(project, dry_run)
        except Exception as exc:
            _emit_fix_error(args, f"fixer {rule_id!r} failed: {exc}")
            return 2
        results.append((spec, result))

    # Report. --json emits one envelope to stdout; the human path prints
    # a per-rule block + a summary line.
    if getattr(args, "json", False):
        json.dump(_fix_json_envelope(project, dry_run, selected, results), sys.stdout)
        sys.stdout.write("\n")
    else:
        _print_fix_human_report(project, dry_run, selected, results)

    # Exit code — 1 iff dry-run and there is pending work; else 0. Per-file
    # errors don't bump the exit code because a fixer that continued past
    # them may still have produced useful writes; the report surfaces them.
    total_changes = sum(len(result.files_changed) for _, result in results)
    if dry_run and total_changes > 0:
        return 1
    return 0


def _emit_fix_error(args, message: str) -> None:
    """Emit a `ships fix` error to the right stream in the right shape.

    Under ``--json`` we still emit a machine-readable envelope so a caller
    parsing stdout does not have to also parse stderr. Under the human
    path we mirror the tone of the rest of the CLI: a stderr line
    prefixed with ``ships fix:``.
    """
    import json

    if getattr(args, "json", False):
        json.dump({"success": False, "error": message}, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(f"ships fix: {message}", file=sys.stderr)


def _fix_json_envelope(
    project: str, dry_run: bool, selected: list, results: list
) -> dict:
    """Build the ``--json`` payload — one envelope per run.

    Shape mirrors the MCP ``ships_fix`` tool where per-rule fields overlap
    (``rule_id`` / ``dry_run`` / ``files_scanned`` / ``files_changed`` /
    ``files``) so callers can consume either surface with the same
    parser. The CLI-only wrapper adds ``project``, the ``rules`` list
    (what was actually run vs. what was requested), and an aggregate
    ``totals`` block across every rule.
    """
    rule_payloads = []
    total_scanned = 0
    total_changed = 0
    total_errors = 0
    for spec, result in results:
        d = result.to_dict()
        total_scanned += d.get("files_scanned", 0)
        total_changed += d.get("files_changed_count", 0)
        total_errors += len(d.get("errors", []))
        rule_payloads.append(d)
    return {
        "success": True,
        "project": project,
        "dry_run": dry_run,
        "rules_requested": selected,
        "rules_run": [spec.rule_id for spec, _ in results],
        "totals": {
            "files_scanned": total_scanned,
            "files_changed": total_changed,
            "errors": total_errors,
        },
        "rules": rule_payloads,
    }


def _print_fix_human_report(
    project: str, dry_run: bool, selected: list, results: list
) -> None:
    """Print the per-rule human report used when ``--json`` is not set.

    Layout is one indented block per rule (matches the inspect-report
    aesthetic elsewhere in the CLI). Rules with no matches print
    ``no changes needed`` on their own line rather than showing an
    empty file list — reduces vertical noise in a clean-tree run.
    """
    from td_release_packager.fixers import FIX_REGISTRY

    mode = "dry-run" if dry_run else "apply"
    print(f"\nships fix ({mode}) — project: {project}\n")

    for spec, result in results:
        default_note = "" if spec.default_on else "  (opt-in)"
        print(f"  {spec.rule_id}{default_note}")

        if not result.files_changed and not result.errors:
            print("    no changes needed")
            print()
            continue

        for entry in result.files_changed:
            # Per-file line — path + a compact "+N" counter for whichever
            # rule-specific total is under details. Falls back to "+1" so
            # the row still says "something happened here" for fixers whose
            # per-file details dict is empty.
            counter = _summarise_file_details(entry.details)
            print(f"    {entry.file}  {counter}")

        for err in result.errors:
            print(f"    ✗  {err.get('file', '?')}  — {err.get('error', 'error')}")

        verb = "would be rewritten" if dry_run else "rewritten"
        summary_bits = [f"{len(result.files_changed)} file(s) {verb}"]
        for key, value in result.totals.items():
            if value:
                summary_bits.append(f"{value} {key.replace('_', ' ')}")
        icon = "→" if dry_run else "✓"
        print(f"    {icon}  " + ", ".join(summary_bits))
        print()

    # Aggregate summary. Under --dry-run the phrasing matches the exit
    # code semantics (exit 1 => "N file(s) would change").
    total_scanned = sum(r.files_scanned for _, r in results)
    total_changed = sum(len(r.files_changed) for _, r in results)
    total_errors = sum(len(r.errors) for _, r in results)

    # If the caller passed rule ids that don't exist, we exit before we
    # get here — no need to mention that. But if the *default-on* set
    # happened to be empty because the operator set default_on=False on
    # every fixer, note it so they don't wonder why nothing ran.
    if not selected:
        print(
            "  (no fixers selected — the default-on set is empty. "
            "Pass --all or --rules to run something.)"
        )
        return

    if not results:
        # Every selected rule was known but produced no run — currently
        # unreachable, but defensive against a future filter step.
        return

    _ = FIX_REGISTRY  # keep the import used above; helps readability
    if total_changed == 0 and total_errors == 0:
        print(f"  ✓  Nothing to do ({total_scanned} file(s) scanned).")
    elif dry_run:
        print(
            f"  →  {total_changed} file(s) would be rewritten "
            f"({total_scanned} scanned"
            + (f", {total_errors} error(s)" if total_errors else "")
            + "). Re-run without --dry-run to apply."
        )
    else:
        print(
            f"  ✓  {total_changed} file(s) rewritten "
            f"({total_scanned} scanned"
            + (f", {total_errors} error(s)" if total_errors else "")
            + ")."
        )


def _summarise_file_details(details: dict) -> str:
    """Compact per-file counter for the human report.

    Prefers well-known numeric keys (``statements_fixed``,
    ``chars_substituted``, ``total_chars_substituted``) so the visible
    counter matches the language a reader already knows from inspect
    findings. Falls back to a plain ``+1`` when the fixer records no
    such counter — still enough for the eye to register the row.
    """
    for key in ("statements_fixed", "total_chars_substituted", "chars_substituted"):
        if key in details and isinstance(details[key], int):
            return f"+{details[key]}"
    # Detail dict may carry a mapping like the per-codepoint substitution
    # map from non_ascii — sum those values to get a total.
    for value in details.values():
        if isinstance(value, dict) and all(isinstance(v, int) for v in value.values()):
            return f"+{sum(value.values())}"
    return "+1"


def _cmd_build(args):
    """Build a release package."""
    # -- Materialise remote source if --source-github was given --
    # _resolve_github_source sets args.source; bridge to args.project for package.
    _gh_tmp: list = []
    _resolve_github_source(args, _gh_tmp)
    if getattr(args, "source", None) and not args.project:
        args.project = args.source

    if not args.project:
        print("ERROR: --project or --source-github is required.", file=sys.stderr)
        sys.exit(1)

    try:
        _cmd_build_impl(args)
    finally:
        for tmp in _gh_tmp:
            tmp.cleanup()


def _cmd_build_impl(args):
    """Build a release package (inner — after source is resolved)."""
    # -- Resolve properties file path --
    env_config_path = _resolve_path(
        args.env_config,
        relative_to=args.project,
        label="--env-config",
    )
    args.env_config = env_config_path

    # -- Cross-check: --env must match SHIPS_ENV in properties file --
    # The properties file declares its own environment via SHIPS_ENV.
    # This prevents building a DEV-labelled package with PROD tokens.
    if args.env_config and os.path.isfile(args.env_config):
        env_upper = args.env.upper()
        declared_env = None
        try:
            props = read_env_config(args.env_config)
            declared_env = props.get("SHIPS_ENV", "").upper()
        except Exception:
            pass  # File read errors handled later by build_package

        if declared_env and declared_env != env_upper:
            print(
                f"\nERROR: Environment mismatch.\n"
                f"  --env        = {env_upper}\n"
                f"  SHIPS_ENV    = {declared_env} "
                f"(declared in {os.path.basename(args.env_config)})\n\n"
                f"  The SHIPS_ENV property inside the file must match --env.\n"
                f"  Either change --env to {declared_env}, or use the correct\n"
                f"  properties file for {env_upper}.",
                file=sys.stderr,
            )
            sys.exit(1)
        elif not declared_env:
            print(
                f"  ⚠  No SHIPS_ENV declared in {os.path.basename(args.env_config)} "
                f"— environment cross-check skipped.",
            )

    from td_release_packager.orchestrator import issue_codes as _ic

    try:
        with _stage_recording(args.project, "package") as stage:
            exit_code = _run_build(args, stage, _ic)
    except (FileNotFoundError, ValueError) as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)


def _run_build(args, stage, issue_codes) -> int:
    """
    Body of the package command, factored out so ``_cmd_build``
    can wrap it in ``_stage_recording`` without indenting the body.

    Returns:
        Exit code for the shell.
    """
    stage.set_config_resolved("source", args.project, "layer-5", "cli")
    stage.set_config_resolved("env", args.env.upper(), "layer-5", "cli")
    stage.set_config_resolved("name", args.name, "layer-5", "cli")
    stage.set_config_resolved("env_config", args.env_config, "layer-5", "cli")
    stage.set_config_resolved("output", getattr(args, "output", None), "layer-5", "cli")
    stage.set_config_resolved(
        "root_parent", getattr(args, "root_parent", None), "layer-5", "cli"
    )
    stage.set_config_resolved(
        "format", getattr(args, "format", "zip"), "layer-5", "cli"
    )
    stage.set_inputs(source_dir=args.project)

    root_parent_injections = _apply_root_parent_option(
        args.project,
        getattr(args, "root_parent", None),
    )
    if root_parent_injections:
        print(f"  Root parent: {root_parent_injections} parentless prereq(s) updated")

    # Resolve build number: explicit, no-increment, or auto-increment
    build_number = args.build_number  # None if not specified

    if build_number is not None:
        print(f"  Build number: {build_number} (explicit)")
    elif args.no_increment:
        # Reuse current build number — same source, different env
        try:
            build_number = read_build_number(args.project)
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
            current = read_build_number(args.project)
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

    # #115: changeset-scoped packaging. When --since-tag / --since-commit /
    # --objects is given, resolve the changed object set + dependants and
    # stage a filtered copy of the project so the normal build pipeline runs
    # unchanged over just that subset. The build number is fixed up-front
    # (the staged copy is throwaway) so it stays continuous with the project.
    source_dir = args.project
    staged_dir = None
    changeset_meta = None
    _since = getattr(args, "since_tag", None) or getattr(args, "since_commit", None)
    _objects = getattr(args, "objects", None)
    if _since or _objects:
        from td_release_packager import changeset as _changeset

        if _objects:
            requested = {o.strip() for o in _objects.split(",") if o.strip()}
            result = _changeset.changeset_from_objects(args.project, requested)
            _base = "objects"
        else:
            result = _changeset.detect_changeset(args.project, since=_since)
            _base = _since
        if result.note:
            print(f"  Changeset: {result.note}")
        if result.mode == "none":
            print(f"ERROR: {result.note}", file=sys.stderr)
            return 1
        if not result.selected:
            print("  Changeset: no changed objects detected — nothing to package.")
            return 0
        if build_number is None:
            from td_release_packager.build_counter import next_build_number

            build_number = next_build_number(args.project)
        staged_dir = _changeset.stage_changeset_payload(args.project, result.selected)
        source_dir = staged_dir
        changeset_meta = {
            "mode": result.mode,
            "base": _base,
            "objects": sorted(result.selected),
            "changed": sorted(result.changed),
            "dependants": sorted(result.dependants),
        }
        print(
            f"  Changeset: {len(result.changed)} changed + "
            f"{len(result.dependants)} dependants = "
            f"{len(result.selected)} object(s) packaged ({result.mode})"
        )

    # #489: auto-resolve source_commit from the local git repo when the
    # operator didn't pass --commit and the source isn't a github tarball
    # (the github path already stamps args.commit during fetch). Without
    # this, the most common packaging path — local source, no --commit
    # — silently builds a package with empty source_commit, losing
    # provenance. The helper is fail-soft: not-a-repo / no-git returns
    # "" and the build proceeds exactly as it does today.
    if not getattr(args, "commit", None):
        from td_release_packager.builder import _resolve_local_commit

        _auto_sha = _resolve_local_commit(args.project)
        if _auto_sha:
            args.commit = _auto_sha
            print(f"  Resolved local commit : {_auto_sha[:12]}")

    # #397: snapshot the redacted top-level invocation so the package can
    # answer "what command + args built this?" after distribution, when
    # the project-side ships.decisions.json is no longer reachable. Derive
    # from sys.argv (not args.command) because `process` runs the package
    # stage via a synthesised namespace — sys.argv still holds the real
    # subcommand the operator typed (`package` or `process`).
    from td_release_packager import build_invocation as _build_invocation

    _argv = sys.argv[1:]
    _subcommand = _argv[0] if _argv else "package"
    _invocation = _build_invocation.snapshot(
        command=f"ships {_subcommand}",
        args=_argv[1:],
        cwd=os.getcwd(),
        env_config=getattr(args, "env_config", None),
    )

    config = BuildConfig(
        source_dir=source_dir,
        environment=args.env.upper(),
        package_name=args.name,
        env_config_file=args.env_config,
        build_number=build_number,
        output_dir=args.output,
        archive_format=args.format,
        author=args.author or "",
        description=args.description or "",
        source_commit=args.commit or "",
        allow_dirty=getattr(args, "allow_dirty", False),
        change_ref=getattr(args, "change_ref", None),
        build_invocation=_invocation,
        changeset=changeset_meta,
    )

    try:
        (main_pair, companion_pair) = build_package(config)
    finally:
        # #115: the staged changeset copy is throwaway — remove it whether
        # the build succeeds or raises.
        if staged_dir is not None:
            import shutil

            shutil.rmtree(staged_dir, ignore_errors=True)
    archive_path, manifest = main_pair

    # -- GAP-005: sign the package archive(s) if a key is available --
    _signing_key_path = getattr(args, "signing_key", None)
    try:
        from database_package_deployer.signing import sign_package as _sign

        hmac_path = _sign(archive_path, _signing_key_path)
        if hmac_path:
            print(f"  Signed:      {hmac_path}")
        if companion_pair is not None:
            companion_hmac = _sign(companion_pair[0], _signing_key_path)
            if companion_hmac:
                print(f"  Signed:      {companion_hmac}")
    except Exception as _sign_exc:
        logger.warning("Package signing failed (non-fatal): %s", _sign_exc)

    # -- Ed25519 asymmetric signing (Option C) --
    # Coexists with HMAC signing; both sidecar files may be present.
    _asym_key_path = getattr(args, "asymmetric_key", None)
    try:
        from database_package_deployer import asym_signing as _asym

        _private_pem = _asym.resolve_private_key_pem(_asym_key_path)
        if _private_pem:
            sig_path = _asym.sign_zip(archive_path, _private_pem)
            print(f"  Sig (Ed25519): {sig_path}")
            if companion_pair is not None:
                companion_sig = _asym.sign_zip(companion_pair[0], _private_pem)
                print(f"  Sig (Ed25519): {companion_sig}")
        else:
            logger.debug(
                "asym_signing: no private key available — skipping Ed25519 signing."
            )
    except ImportError:
        logger.debug(
            "asym_signing: cryptography not installed — Ed25519 signing skipped."
        )
    except Exception as _asym_exc:
        logger.warning("Ed25519 signing failed (non-fatal): %s", _asym_exc)

    # -- Record outputs and issues --
    stage.set_outputs(
        archive_path=archive_path,
        environment=manifest.environment,
        build_number=manifest.build_number,
        file_count=manifest.file_count,
        token_count=manifest.token_count,
        has_companion=companion_pair is not None,
        companion_archive_path=(
            companion_pair[0] if companion_pair is not None else None
        ),
        root_parent_injections=root_parent_injections,
    )
    for w in manifest.warnings:
        stage.add_issue("warning", issue_codes.PACKAGE_WARNING, w)

    # -- #145: emit per-stage result files into every built archive --
    # When the package was built via ``ships process`` the run already
    # carries every stage and ``_cmd_process`` writes them at run-end.
    # Standalone builds (``ships package`` after individual
    # harvest/inspect/analyse runs) need the same per-stage results so
    # an agent inspecting the archive never has to scrape decisions.json.
    #
    # The two writer helpers below mutate the archive's bytes, so they
    # also refresh the ``.sha256`` sidecar (#450) — otherwise the
    # sidecar that ``build_package`` wrote stays frozen at the
    # pre-append hash and the next ``ships inspect`` reports
    # ``INSPECT_PACKAGE_INTEGRITY`` mismatches across every release.
    try:
        archive_paths_for_stage_results = [archive_path]
        if companion_pair is not None:
            archive_paths_for_stage_results.insert(0, companion_pair[0])
        stage_results = _build_standalone_stage_results(
            args.project,
            current_stage_entry=getattr(stage, "_stage_entry", None),
        )
        if stage_results:
            for _arc in archive_paths_for_stage_results:
                if not _arc or not os.path.exists(_arc):
                    continue
                if _arc.endswith(".zip"):
                    _write_process_results_to_zip(_arc, stage_results)
                elif _arc.endswith(".tar.gz"):
                    _write_process_results_to_tar_gz(_arc, stage_results)
    except Exception as _stage_results_exc:  # pragma: no cover - defensive
        # Per-stage results are an enrichment, not a blocker. A failure
        # here must not abort the build.
        logger.warning(
            "stage result artefacts not produced: %s — package builds without them.",
            _stage_results_exc,
        )

    print(f"\n{'=' * 64}")
    print("  ✓ Package built successfully")
    print(f"{'=' * 64}")

    if companion_pair is not None:
        prereqs_archive, prereqs_manifest = companion_pair
        print(
            "  Auto-split: this source contains both CREATE DATABASE/USER\n"
            "  statements and objects that depend on them. Two archives\n"
            "  were emitted so deploy --explain can validate each cleanly."
        )
        print()
        print("  Deploy order:")
        print(f"    1. {os.path.basename(prereqs_archive)}")
        print(f"    2. {os.path.basename(archive_path)}")
        print()
        print(f"  release_group: {manifest.release_group}")
        print()

    print(f"  Archive:     {archive_path}")
    print(f"  Environment: {manifest.environment}")
    print(f"  Build:       {manifest.build_number}")
    print(f"  Files:       {manifest.file_count}")
    print(f"  Tokens:      {manifest.token_count} substitutions")
    print(f"{'=' * 64}")

    for phase, count in sorted(manifest.phase_inventory.items()):
        print(f"    {phase}: {count} file(s)")

    if companion_pair is not None:
        prereqs_archive, prereqs_manifest = companion_pair
        print()
        print(f"  Companion (deploy first): {prereqs_archive}")
        print(f"  Files:                    {prereqs_manifest.file_count}")
        print(f"{'=' * 64}")
        for phase, count in sorted(prereqs_manifest.phase_inventory.items()):
            print(f"    {phase}: {count} file(s)")

    if manifest.warnings:
        print("\n  Warnings:")
        for w in manifest.warnings:
            print(f"    ⚠  {w}")

    print()
    return 0


# ---------------------------------------------------------------
# Per-stage result derivation (#145)
# ---------------------------------------------------------------

STAGE_RESULT_SCHEMA = "teradata-ships/stage-result/v1"

# Default next-action suggestions for an agent or CI/CD tool that just
# consumed a stage result. The map covers the canonical pipeline
# (harvest → inspect → analyse → package → ship → verify). Override
# semantics: an explicit ``next_action`` field on the stage entry's
# ``decisions`` wins; otherwise the rule below applies based on the
# stage name and status. The rule is intentionally simple — a richer
# state machine (e.g. "remediate on inspect errors") can be layered on
# without changing the result-file shape.
_STAGE_NEXT_ACTIONS: Dict[str, Dict[str, str]] = {
    "harvest": {
        "success": "Run `ships inspect` to validate the harvested payload.",
        "error": "Resolve the harvest errors before re-running.",
    },
    "inspect": {
        "success": "Run `ships analyse` to compute the deployment dependency graph.",
        "error": "Remediate inspect findings — see context/ships.rules.json for per-rule guidance.",
    },
    "analyse": {
        "success": "Run `ships package` to build a deployment archive.",
        "error": "Resolve dependency / cycle issues before packaging.",
    },
    "package": {
        "success": "Run `ships ship` (or hand the archive to a deployer) to deploy the package.",
        "error": "Resolve packaging errors and rebuild.",
    },
    "ship": {
        "success": "Run `ships verify` to confirm the deployed state matches the package.",
        "error": "Investigate deployment failures via context/stages/ship.result.json and decisions.json.",
    },
    "verify": {
        "success": "Deployment complete — archive the package and decisions for the audit trail.",
        "error": "Investigate drift between expected and deployed state.",
    },
    "scan": {
        "success": "Review the scan summary; proceed with harvest when ready.",
        "error": "Resolve scan errors before harvesting.",
    },
}


def _next_action_for(stage_entry: Dict[str, Any]) -> str:
    """Derive a one-line ``next_action`` hint for ``stage_entry``."""
    decisions = stage_entry.get("decisions") or {}
    explicit = decisions.get("next_action") if isinstance(decisions, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    name = stage_entry.get("stage") or ""
    status = str(stage_entry.get("status", "success")).lower()
    bucket = _STAGE_NEXT_ACTIONS.get(name, {})
    if status == "success":
        return bucket.get("success", "")
    # Any non-success status (error/warning/partial/failed) falls through
    # to the error suggestion.
    return bucket.get("error") or bucket.get("success", "")


def _normalise_stage_result(stage_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return a package-local JSON-safe stage result document."""
    return {
        "schema": STAGE_RESULT_SCHEMA,
        "stage": stage_entry.get("stage"),
        "status": stage_entry.get("status"),
        "started_at": stage_entry.get("started_at"),
        "finished_at": stage_entry.get("finished_at"),
        "duration_ms": stage_entry.get("duration_ms", 0),
        "inputs": stage_entry.get("inputs", {}),
        "outputs": stage_entry.get("outputs", {}),
        "decisions": stage_entry.get("decisions", {}),
        "issues": stage_entry.get("issues", []),
        "issue_counts": _count_stage_issues(stage_entry),
        "next_action": _next_action_for(stage_entry),
    }


def _collect_latest_stage_entries(
    decisions_data: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Return the most-recent stage entry per stage name across all runs.

    Walks ``decisions_data["runs"]`` from newest to oldest. The first
    occurrence of each stage name wins, so a re-run of ``inspect``
    overrides the prior entry. Stages without a ``stage`` key or
    without a ``finished_at`` timestamp are skipped — partial /
    interrupted runs do not override completed ones.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for run in reversed(decisions_data.get("runs", []) or []):
        for stage_entry in run.get("stages", []) or []:
            name = stage_entry.get("stage")
            if not isinstance(name, str):
                continue
            if not stage_entry.get("finished_at"):
                continue
            if name in latest:
                continue
            latest[name] = stage_entry
    return latest


def _build_standalone_stage_results(
    project_dir: str,
    current_stage_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return per-stage result documents for the most recent run of each
    stage in the project's ``ships.decisions.json``.

    When ``current_stage_entry`` is provided it overrides any existing
    entry for the same stage name — used so the in-flight ``package``
    stage shows up in the freshly-built archive even though
    ``DecisionsManifest.save()`` runs slightly after the archive write.
    """
    from td_release_packager.project_paths import decisions_json_path

    results: Dict[str, Dict[str, Any]] = {}
    decisions_path = decisions_json_path(project_dir)
    decisions_data: Dict[str, Any] = {}
    if os.path.exists(decisions_path):
        try:
            with open(decisions_path, "r", encoding="utf-8") as f:
                decisions_data = json.load(f)
        except (OSError, ValueError):
            decisions_data = {}

    latest = _collect_latest_stage_entries(decisions_data)
    if current_stage_entry and isinstance(current_stage_entry.get("stage"), str):
        latest[current_stage_entry["stage"]] = current_stage_entry

    for name, entry in latest.items():
        results[f"{name}.result.json"] = _normalise_stage_result(entry)
    return results


def _count_stage_issues(stage_entry: Dict[str, Any]) -> Dict[str, int]:
    """Count issues by severity for a stage entry."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in stage_entry.get("issues", []):
        severity = str(issue.get("severity", "info")).lower()
        counts[severity if severity in counts else "info"] += 1
    return counts


def _build_package_process_results(
    project_dir: str,
    run_entry: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Build package-local current-run result documents.

    The project-level ``ships.decisions.json`` remains the full append-only
    history.  These files are a compact, package-local snapshot of the current
    process run so an extracted archive is useful to agents without copying the
    whole project history into every package.
    """
    from td_release_packager.project_paths import decisions_json_path

    stages = run_entry.get("stages", [])
    stage_summaries = [
        {
            "stage": s.get("stage"),
            "status": s.get("status"),
            "started_at": s.get("started_at"),
            "finished_at": s.get("finished_at"),
            "duration_ms": s.get("duration_ms", 0),
            "issue_counts": _count_stage_issues(s),
        }
        for s in stages
    ]
    results: Dict[str, Dict[str, Any]] = {
        "process.result.json": {
            "schema": "teradata-ships/process-result/v1",
            "run_id": run_entry.get("run_id"),
            "command": run_entry.get("command"),
            "final_status": run_entry.get("final_status"),
            "started_at": run_entry.get("started_at"),
            "finished_at": run_entry.get("finished_at"),
            "duration_ms": run_entry.get("duration_ms", 0),
            "project_decisions_path": decisions_json_path(project_dir),
            "package_local": True,
            "stages": stage_summaries,
        }
    }
    for stage in stages:
        name = stage.get("stage")
        if name:
            results[f"{name}.result.json"] = _normalise_stage_result(stage)
    return results


def _archive_root_for_zip(archive_path: str) -> str:
    """Return the top-level directory prefix inside a zip archive."""
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            parts = info.filename.split("/", 1)
            if parts and parts[0]:
                return parts[0]
    return os.path.splitext(os.path.basename(archive_path))[0]


def _write_process_results_to_zip(
    archive_path: str,
    results: Dict[str, Dict[str, Any]],
) -> None:
    """Append package-local process/stage result files to a zip archive.

    Mutates the archive's bytes — the ``.sha256`` sidecar that
    ``build_package`` wrote against the pre-append archive is stale
    after this call, so we refresh it here. Keeping that refresh in
    the mutator means every caller stays consistent without having to
    remember to re-sidecar (#450).
    """
    package_root = _archive_root_for_zip(archive_path)
    with zipfile.ZipFile(
        archive_path, "a", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for filename, payload in sorted(results.items()):
            arcname = f"{package_root}/context/stages/{filename}"
            archive.writestr(
                arcname,
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n",
            )
    _refresh_archive_sidecar(archive_path)


def _write_process_results_to_tar_gz(
    archive_path: str,
    results: Dict[str, Dict[str, Any]],
) -> None:
    """Inject package-local process/stage result files into a tar.gz archive.

    Mutates the archive's bytes (rebuilds the .tar.gz from a
    re-extracted tree with the stage files added). Refreshes the
    ``.sha256`` sidecar — see ``_write_process_results_to_zip`` for
    the rationale (#450).
    """
    with tempfile.TemporaryDirectory(prefix="ships_process_results_") as tmp_dir:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(tmp_dir, filter="data")
        roots = [
            name
            for name in os.listdir(tmp_dir)
            if os.path.isdir(os.path.join(tmp_dir, name))
        ]
        package_root = (
            roots[0] if roots else os.path.splitext(os.path.basename(archive_path))[0]
        )
        stages_dir = os.path.join(tmp_dir, package_root, "context", "stages")
        os.makedirs(stages_dir, exist_ok=True)
        for filename, payload in sorted(results.items()):
            with open(os.path.join(stages_dir, filename), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.write("\n")

        rebuilt = shutil.make_archive(
            base_name=os.path.join(tmp_dir, "rebuilt"),
            format="gztar",
            root_dir=tmp_dir,
            base_dir=package_root,
        )
        shutil.copyfile(rebuilt, archive_path)
    _refresh_archive_sidecar(archive_path)


def _refresh_archive_sidecar(archive_path: str) -> None:
    """Regenerate the ``.sha256`` sidecar for an archive after mutation.

    Best-effort — if the original archive had no sidecar (e.g. a
    caller used these helpers outside the standard build flow) this
    still creates one with the current bytes, matching the build
    flow's invariant that every released archive has a sidecar
    consistent with its bytes.
    """
    from td_release_packager.builder import _generate_checksum

    try:
        _generate_checksum(archive_path)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "could not refresh .sha256 sidecar for %s: %s — "
            "downstream INSPECT_PACKAGE_INTEGRITY checks may report a mismatch.",
            archive_path,
            exc,
        )


def _write_package_run_context_to_archives(
    project_dir: str,
    archive_paths: list[str],
    run,
) -> list[str]:
    """Write package-local current-run context into each generated archive."""
    run_entry = getattr(run, "_run_entry", None)
    if not isinstance(run_entry, dict):
        return []

    results = _build_package_process_results(project_dir, run_entry)
    written: list[str] = []
    for archive_path in archive_paths:
        if not archive_path or not os.path.exists(archive_path):
            continue
        if archive_path.endswith(".zip"):
            _write_process_results_to_zip(archive_path, results)
            written.append(archive_path)
        elif archive_path.endswith(".tar.gz"):
            _write_process_results_to_tar_gz(archive_path, results)
            written.append(archive_path)
        else:
            logger.warning(
                "Skipping package-local process context for unsupported archive: %s",
                archive_path,
            )
    return written


def _cmd_process(args):
    """
    [S-H-I-P-S] Run the full pipeline in sequence.

    Item 5 of the orchestrator build order. Runs:
      harvest → generate → inspect → analyse → [package]

    All stages write into a single ``process`` run in ``ships.decisions.json``
    so the audit trail is one coherent record rather than five separate
    run entries.

    Developer mode (default): continues past warnings; only hard errors
    abort the run.
    Platform mode (``--strict``): any stage that finishes with
    ``status=error`` aborts the pipeline immediately.
    """
    # -- Materialise remote source if --source-github was given --
    _gh_tmp: list = []
    _resolve_github_source(args, _gh_tmp)

    try:
        _cmd_process_impl(args)
    finally:
        for tmp in _gh_tmp:
            tmp.cleanup()


def _apply_process_defaults_from_ships_yaml(args, project_dir: str) -> None:
    """Fill missing ``process`` args from ``ships.yaml`` (#384 single front door).

    Makes ``ships process --project .`` a near-zero-arg common case by
    deriving the package-stage inputs when the operator did not pass them.
    Precedence is always **CLI arg > ``packaging:`` block > convention**;
    a value the operator supplied is never overridden.

    **Opt-in by design.** Derivation only happens when ships.yaml carries a
    ``packaging:`` block (even an empty one). Without it, ``process`` behaves
    exactly as before — packaging runs only when ``--env``/``--env-config``/
    ``--name`` are all passed explicitly. This keeps the "package by default"
    behaviour from surprising existing projects; opting in is a deliberate
    one-time act (the SHIPS Navigator wizard writes this block — #382).

    Derivation (only when the packaging block is present):
        - ``name``       ← ``packaging.name`` else ``project``.
        - ``env``        ← ``packaging.default_env`` else ``environments[0]``.
        - ``env_config`` ← ``packaging.env_config`` else the conventional
          ``config/env/<ENV>.conf`` — but only when that file actually
          exists, so a missing config skips packaging cleanly rather than
          failing the run.
        - ``source``     ← ``packaging.source`` (no convention fallback —
          omitting source legitimately means "use the existing payload").

    The ``packaging:`` block is the same profile the SHIPS Navigator wizard
    persists (#382). No-ops silently when ``ships.yaml`` is absent or
    unreadable — ``process`` then behaves exactly as before.

    Mutates ``args`` in place and prints the derived values for transparency.
    """
    ships_yaml_path = os.path.join(project_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml_path):
        return

    from td_release_packager.orchestrator import ships_yaml as _sy

    try:
        data = _sy.load(ships_yaml_path)
    except Exception:
        # A malformed ships.yaml is surfaced by the stages that require it;
        # defaulting must never be the thing that aborts the run.
        return
    if not isinstance(data, dict):
        return

    packaging = data.get("packaging")
    if not isinstance(packaging, dict):
        # No packaging profile → opt-out: leave process behaviour unchanged.
        return

    derived = []

    if not getattr(args, "name", None):
        name = packaging.get("name") or data.get("project")
        if isinstance(name, str) and name.strip():
            args.name = name
            derived.append(f"name={name}")

    if not getattr(args, "env", None):
        environments = data.get("environments") or []
        env = packaging.get("default_env") or (
            environments[0] if isinstance(environments, list) and environments else None
        )
        if isinstance(env, str) and env.strip():
            args.env = env
            derived.append(f"env={env}")

    if not getattr(args, "env_config", None) and getattr(args, "env", None):
        explicit = packaging.get("env_config")
        if isinstance(explicit, str) and explicit.strip():
            # An explicit packaging.env_config is honoured verbatim — the
            # author asked for this path; existence is the package stage's
            # problem to report, not ours to second-guess.
            args.env_config = explicit
            derived.append(f"env-config={explicit}")
        else:
            # Convention fallback: only adopt config/env/<ENV>.conf when it
            # actually exists, so a missing file skips packaging cleanly
            # rather than failing the run on an invented path.
            candidate = os.path.join("config", "env", f"{args.env}.conf")
            if os.path.isfile(os.path.join(project_dir, candidate)):
                args.env_config = candidate
                derived.append(f"env-config={candidate}")

    if not getattr(args, "source", None):
        source = packaging.get("source")
        if isinstance(source, str) and source.strip():
            args.source = source
            derived.append(f"source={source}")

    # #501 — packaging.root_parent feeds args.root_parent so `ships process
    # --project .` argless picks up the configured root parent and the
    # CREATE DATABASE/USER injection runs without a manual --root-parent flag.
    # The CLI value (if passed) wins; the profile is the convention default.
    if not getattr(args, "root_parent", None):
        root_parent = packaging.get("root_parent")
        if isinstance(root_parent, str) and root_parent.strip():
            args.root_parent = root_parent
            derived.append(f"root-parent={root_parent}")

    if derived:
        print(f"  Defaults from ships.yaml: {', '.join(derived)}")


def _cmd_process_impl(args):
    """Run the full pipeline (inner — after source is resolved)."""
    from td_release_packager.orchestrator import issue_codes as _ic

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # #384: single front door — fill missing source/env/env-config/name
    # from ships.yaml so the common case is near-zero-arg.
    _apply_process_defaults_from_ships_yaml(args, project_dir)

    strict = getattr(args, "strict", False)

    # -- Pre-build the apply_tokens dict once for reuse by harvest --
    apply_tokens = None
    if hasattr(args, "token_map") and args.token_map:
        token_map_path = _resolve_path(
            args.token_map, relative_to=project_dir, label="--token-map"
        )
        apply_tokens = read_token_map(token_map_path)

    print(f"\n{'=' * 64}")
    print("  SHIPS Process Pipeline")
    mode_label = "STRICT" if strict else "DEVELOPER"
    print(f"  Mode: {mode_label}")
    print(f"  Project: {project_dir}")
    print(f"{'=' * 64}\n")

    failed_stages = []
    package_ran = False
    package_archive_paths: list[str] = []
    process_run = None

    with _process_recording(project_dir) as run:
        process_run = run
        # ---- [H] Harvest ----------------------------------------
        if args.source:
            print("  [H] Harvest …")
            harvest_args = _build_process_namespace(
                args,
                source=args.source,
                project=project_dir,
                force=False,
                keep_existing=False,
            )
            with run.stage("harvest") as stage:
                _run_ingest(harvest_args, stage, _ic, apply_tokens)
            if stage.status == "error":
                failed_stages.append("harvest")
                if strict:
                    _print_process_aborted("harvest", strict)
                    sys.exit(1)
            _maybe_pause("harvest", stage.status, args)
        else:
            print("  [H] Harvest … skipped (no --source provided)")

        # ---- [G] Generate ---------------------------------------
        if not getattr(args, "skip_generate", False):
            print("  [G] Generate …")
            gen_args = _build_process_namespace(args, source=project_dir)
            with run.stage("generate") as stage:
                _run_generate(gen_args, stage, _ic)
            if stage.status == "error":
                failed_stages.append("generate")
                if strict:
                    _print_process_aborted("generate", strict)
                    sys.exit(1)
            _maybe_pause("generate", stage.status, args)
        else:
            print("  [G] Generate … skipped (--skip-generate)")

        root_parent_injections = _apply_root_parent_option(
            project_dir,
            getattr(args, "root_parent", None),
        )
        if root_parent_injections:
            print(
                "  [R] Root parent … "
                f"{root_parent_injections} parentless prereq(s) updated"
            )

        # ---- [I] Inspect ----------------------------------------
        print("  [I] Inspect …")
        inspect_args = _build_process_namespace(
            args,
            source=project_dir,
            strict=strict,
            config=getattr(args, "inspect_config", None),
            skip_grants=True,
            skip_tokens=False,
            skip_keywords=False,
            skip_commas=False,
            dcl_dir=None,
            verbose=False,
        )
        with run.stage("inspect") as stage:
            _run_inspect(inspect_args, stage, _ic)
        if stage.status == "error":
            failed_stages.append("inspect")
            if strict:
                _print_process_aborted("inspect", strict)
                sys.exit(1)
        _maybe_pause("inspect", stage.status, args)

        # ---- [A] Analyse ----------------------------------------
        print("  [A] Analyse …")
        analyse_args = _build_process_namespace(
            args,
            source=project_dir,
            output=None,
            overwrite=True,
            graph=None,
        )
        with run.stage("analyse") as stage:
            _run_analyze(analyse_args, stage, _ic)
        if stage.status == "error":
            failed_stages.append("analyse")
            if strict:
                _print_process_aborted("analyse", strict)
                sys.exit(1)
        _maybe_pause("analyse", stage.status, args)

        # ---- [P] Package ----------------------------------------
        # Only runs when --env + --env-config + --name are all provided.
        if args.env and args.env_config and args.name:
            print("  [P] Package …")
            env_config_path = _resolve_path(
                args.env_config, relative_to=project_dir, label="--env-config"
            )
            pkg_args = _build_process_namespace(
                args,
                source=project_dir,
                env=args.env,
                env_config=env_config_path,
                name=args.name,
                # Default to <project>/releases when unset (#384 single front
                # door; matches the package-output convention from #411). A
                # None output_dir would otherwise crash the build at path join.
                output=getattr(args, "output", None)
                or os.path.join(project_dir, "releases"),
                format=getattr(args, "format", "zip"),
                author=getattr(args, "author", ""),
                description=getattr(args, "description", ""),
                commit=getattr(args, "commit", ""),
                build_number=None,
                no_increment=False,
                root_parent=None,
            )
            try:
                with run.stage("package") as stage:
                    _run_build(pkg_args, stage, _ic)
                package_outputs = getattr(stage, "_entry", {}).get("outputs", {})
                for output_key in ("archive_path", "companion_archive_path"):
                    output_path = package_outputs.get(output_key)
                    if output_path:
                        package_archive_paths.append(output_path)
                if stage.status == "error":
                    failed_stages.append("package")
                    if strict:
                        _print_process_aborted("package", strict)
                        sys.exit(1)
                _maybe_pause("package", stage.status, args)
                package_ran = True
            except (FileNotFoundError, ValueError) as e:
                print(f"\n  ✗ Package failed: {e}", file=sys.stderr)
                failed_stages.append("package")
                if strict:
                    sys.exit(1)
        else:
            print(
                "  [P] Package … skipped (need env + env-config + name — pass "
                "--env/--env-config/--name or set them in ships.yaml; "
                "env-config must exist on disk)"
            )

    package_context_archives = []
    if package_archive_paths and process_run is not None:
        package_context_archives = _write_package_run_context_to_archives(
            project_dir,
            package_archive_paths,
            process_run,
        )

    # -- Summary banner -------------------------------------------
    print(f"\n{'=' * 64}")
    if failed_stages:
        print(f"  Process completed with errors in: {', '.join(failed_stages)}")
        from td_release_packager.project_paths import (
            decisions_json_path as _decisions_json_path,
        )

        decisions_path = _decisions_json_path(project_dir)
        print(f"  Review {decisions_path} for full process detail.")
        if package_context_archives:
            print(
                "  Package-local run context written to context/stages/ in generated archive(s)."
            )
        print(f"{'=' * 64}\n")
        sys.exit(1)
    else:
        stages_run = [
            "harvest" if args.source else None,
            "generate" if not getattr(args, "skip_generate", False) else None,
            "inspect",
            "analyse",
            "package" if package_ran else None,
        ]
        stages_run = [s for s in stages_run if s]
        print(f"  ✓ Process complete: {' → '.join(stages_run)}")
        if package_context_archives:
            print(
                "  Package-local run context written to context/stages/ in generated archive(s)."
            )
        print(f"{'=' * 64}\n")
        sys.exit(0)


def _build_process_namespace(base_args, **overrides):
    """Build a thin Namespace for stage runners called from _cmd_process.

    Copies every attribute from base_args then applies overrides.  This
    lets each stage runner access its expected args without duplicating
    the full argparser surface on the process subcommand.
    """
    from argparse import Namespace

    d = vars(base_args).copy()
    d.update(overrides)
    return Namespace(**d)


def _print_process_aborted(stage_name: str, strict: bool) -> None:
    """Print the pipeline-aborted banner."""
    print(
        f"\n  ✗ Process aborted after {stage_name} (--strict mode — "
        f"errors block continuation).",
        file=sys.stderr,
    )


def _maybe_pause(stage_name: str, stage_status: str, args) -> None:
    """
    Item 10 — Pause-point UX for the ``process`` meta-verb.

    When ``--pause`` is set and the process is running interactively
    (not in CI), print a brief stage summary and prompt the operator
    to decide whether to continue.

    Silently returns if:
      - ``--pause`` was not passed (default developer-mode behaviour).
      - Running non-interactively (``CI``, ``SHIPS_CI``, or
        ``NO_PROMPT`` env var is set, or stdout is not a TTY).

    Prompts:
      ``[Enter] / y`` — continue to the next stage.
      ``n``           — abort (sys.exit(1)).
      ``q``           — quit cleanly (sys.exit(0)).

    Args:
        stage_name:   Name of the stage that just completed.
        stage_status: Status string ("success", "warning", "error", …).
        args:         The parsed process args (checked for ``--pause``).
    """
    if not getattr(args, "pause", False):
        return

    # Suppress in CI / non-interactive environments
    ci_vars = ("CI", "SHIPS_CI", "NO_PROMPT")
    if any(os.environ.get(v) for v in ci_vars):
        return
    if not sys.stdout.isatty():
        return

    icon = _STATUS_ICONS.get(stage_status, "?")
    print(f"\n  ── Pause after {stage_name} [{icon} {stage_status}] ──")
    try:
        response = input("  Continue? [Y/n/q] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if response in ("n", "no"):
        print("  Aborted by operator.", file=sys.stderr)
        sys.exit(1)
    if response in ("q", "quit"):
        print("  Quit by operator.")
        sys.exit(0)
    # Y, enter, or anything else → continue


# ---------------------------------------------------------------
# explain — human-readable read-only view of ships.decisions.json
# ---------------------------------------------------------------

#: Status badge colouring for terminal output.
_STATUS_ICONS = {
    "success": "✓",
    "warning": "⚠",
    "error": "✗",
    "skipped": "○",
    "no-op": "–",
}


def _cmd_rollback(args):
    """Build a rollback package from a git tag — closes #37."""
    from td_release_packager.rollback import build_rollback_package

    project = os.path.abspath(args.project)
    tag = args.to_tag
    env = args.env.upper()
    on_drift = getattr(args, "on_drift", "continue")

    env_config_path = _resolve_path(
        args.env_config,
        relative_to=project,
        label="--env-config",
    )
    output_dir = os.path.abspath(
        getattr(args, "output", os.path.join(project, "releases"))
    )
    os.makedirs(output_dir, exist_ok=True)

    pkg_name = getattr(args, "name", None) or os.path.basename(project)

    print("\n  SHIPS Rollback")
    print(f"  {'=' * 56}")
    print(f"  Tag:         {tag}")
    print(f"  Environment: {env}")
    print(f"  Project:     {project}")
    print(f"  Output:      {output_dir}")
    print()

    try:
        (main_pair, companion_pair) = build_rollback_package(
            project_dir=project,
            tag=tag,
            environment=env,
            env_config_file=env_config_path,
            package_name=pkg_name,
            output_dir=output_dir,
            archive_format=getattr(args, "format", "zip"),
            author=getattr(args, "author", "") or "",
            description=getattr(args, "description", "") or "",
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    archive_path, manifest = main_pair

    print("  ✓ Rollback package built")
    print(f"    Archive:      {os.path.basename(archive_path)}")
    print(f"    Build:        {manifest.build_number}")
    print(f"    Commit:       {manifest.source_commit[:12]}")
    if companion_pair:
        companion_archive, _ = companion_pair
        print(f"    Companion:    {os.path.basename(companion_archive)}")

    print()
    print("  Next steps")
    print(f"  {'─' * 54}")
    if companion_pair:
        companion_archive, _ = companion_pair
        print("  1. Extract and deploy the prereqs archive first:")
        print(
            f"       python deploy.py --host <host> --user <user> --on-drift {on_drift}"
        )
        print(f"     (inside: {os.path.basename(companion_archive)})")
        print()
        print("  2. Then deploy the main archive:")
    else:
        print("  1. Extract the rollback package:")
        print(f"       unzip {os.path.basename(archive_path)}")
        print()
        print("  2. Verify integrity:")
        print("       python deploy.py integrity-check")
        print()
        print("  3. Dry run (recommended):")
        print("       python deploy.py --dry-run --host <host> --user <user>")
        print()
        print("  4. Deploy:")
    print(f"       python deploy.py --host <host> --user <user> --on-drift {on_drift}")
    print()
    if on_drift == "continue":
        print("  ⚠  --on-drift continue is set: any schema changes made after")
        print("     the broken deploy will be overwritten by this rollback.")
        print("     Use --on-drift skip to preserve a specific hotfix.")
    print(f"  {'=' * 56}")


def _cmd_decisions(args):
    """Dispatch decisions sub-commands."""
    sub = getattr(args, "decisions_subcommand", None)
    if sub == "prune":
        _cmd_decisions_prune(args)
    else:
        # No sub-command — print usage
        print(
            "Usage: decisions prune --keep-runs N | --keep-days N [--yes] [--dry-run]"
        )
        sys.exit(1)


def _cmd_decisions_prune(args):
    """Prune old run entries from ships.decisions.json."""
    from td_release_packager.orchestrator.decisions import prune_decisions

    from td_release_packager.project_paths import decisions_json_path

    project = args.project
    decisions_path = decisions_json_path(project)

    if not os.path.isfile(decisions_path):
        print(
            f"ERROR: ships.decisions.json not found at {decisions_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    keep_runs = getattr(args, "keep_runs", None)
    keep_days = getattr(args, "keep_days", None)
    yes = getattr(args, "yes", False)
    dry_run = getattr(args, "dry_run", False)

    # Dry-run preview
    preview = prune_decisions(
        decisions_path, keep_runs=keep_runs, keep_days=keep_days, dry_run=True
    )

    print("\n  ships.decisions.json prune preview")
    print(f"  {'=' * 44}")
    print(f"  Total runs  : {preview.total_runs}")
    print(f"  To keep     : {preview.kept_runs}")
    print(f"  To prune    : {preview.pruned_runs}")

    if preview.pruned_runs == 0:
        print("\n  Nothing to prune.")
        return

    print()
    for rid, ts in zip(preview.pruned_run_ids[:20], preview.pruned_started_at[:20]):
        print(f"  - {ts[:19]}  {rid}")
    if preview.pruned_runs > 20:
        print(f"  ... and {preview.pruned_runs - 20} more")
    print()

    if dry_run:
        print("  --dry-run: no changes written.")
        return

    if not yes:
        try:
            answer = (
                input(f"  Prune {preview.pruned_runs} run(s)? [y/N] ").strip().lower()
            )
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("  Aborted.")
            return

    prune_decisions(
        decisions_path, keep_runs=keep_runs, keep_days=keep_days, dry_run=False
    )
    print(
        f"  Pruned {preview.pruned_runs} run(s). {preview.kept_runs} run(s) retained."
    )


def _cmd_explain(args):
    """
    Item 6a — explain: human-readable report of a prior process run.

    Reads ships.decisions.json without modifying it.  Finds the most recent
    run (or the run specified by ``--run-id``) and prints a concise
    report showing: run metadata, per-stage status + key outputs, and
    a full issues table.  Designed as a pre-promotion checklist — the
    DBA reads this before promoting from DEV to TST.
    """
    from td_release_packager.orchestrator import DecisionsManifest
    from td_release_packager.project_paths import decisions_json_path

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    manifest_path = decisions_json_path(project_dir)
    if not os.path.exists(manifest_path):
        print(
            f"ERROR: No ships.decisions.json found at {manifest_path}.\n"
            "  Run the pipeline first:  ships process --project <dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = DecisionsManifest(manifest_path)
    runs = manifest.data.get("runs", [])
    if not runs:
        print("  No runs recorded yet.", file=sys.stderr)
        sys.exit(1)

    # Select run — last run matching --command filter, or --run-id
    run_id_filter = getattr(args, "run_id", None)
    cmd_filter = getattr(args, "command_filter", None)

    if run_id_filter:
        selected = next(
            (r for r in reversed(runs) if r["run_id"] == run_id_filter), None
        )
        if selected is None:
            print(f"ERROR: Run ID {run_id_filter!r} not found.", file=sys.stderr)
            sys.exit(1)
    elif cmd_filter:
        selected = next(
            (r for r in reversed(runs) if r.get("command") == cmd_filter), None
        )
        if selected is None:
            print(f"ERROR: No run found with command={cmd_filter!r}.", file=sys.stderr)
            sys.exit(1)
    else:
        selected = runs[-1]

    # ---- Render ------------------------------------------------
    _print_explain_report(selected, project_dir)

    final = selected.get("final_status", "unknown")
    sys.exit(0 if final in ("success", "warning") else 1)


def _print_explain_report(run: dict, project_dir: str) -> None:
    """Render one run from ships.decisions.json as a human-readable report."""
    final = run.get("final_status", "unknown")
    icon = _STATUS_ICONS.get(final, "?")
    duration_ms = run.get("duration_ms", 0)
    duration_s = f"{duration_ms / 1000:.1f}s" if duration_ms else "—"

    print(f"\n{'=' * 64}")
    print("  SHIPS Explain")
    print(f"{'=' * 64}")
    print(f"  Run:      {run.get('run_id', '—')}")
    print(f"  Command:  {run.get('command', '—')}")
    print(f"  Status:   {icon} {final.upper()}")
    print(f"  Started:  {run.get('started_at', '—')}")
    print(f"  Duration: {duration_s}")

    stages = run.get("stages", [])
    if stages:
        print(f"\n  {'Stage':<12} {'Status':<10} {'Dur':>6}  Issues   Key output")
        print(f"  {'─' * 60}")
        for s in stages:
            s_icon = _STATUS_ICONS.get(s.get("status", ""), "?")
            s_dur_ms = s.get("duration_ms", 0)
            s_dur = f"{s_dur_ms / 1000:.1f}s" if s_dur_ms else "—"
            issue_counts = _count_issues(s.get("issues", []))
            issues_str = _format_issue_counts(issue_counts)
            key_out = _key_output_line(s)
            print(
                f"  {s['stage']:<12} {s_icon} {s.get('status', '?'):<8} "
                f"{s_dur:>6}  {issues_str:<8} {key_out}"
            )

    # ---- Issues table ----------------------------------------
    all_issues = [(s["stage"], i) for s in stages for i in s.get("issues", [])]
    if all_issues:
        print(f"\n  {'─' * 64}")
        print("  Issues:")
        for stage_name, issue in all_issues:
            sev = issue.get("severity", "?")
            sev_icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(sev, "?")
            code = issue.get("code", "?")
            msg = issue.get("message", "")
            loc = issue.get("location", "")
            loc_str = f" [{loc}]" if loc else ""
            print(f"    {sev_icon} [{stage_name}] {code}{loc_str}")
            # Wrap long messages
            for line in _wrap(msg, 56, "      "):
                print(line)
    else:
        print("\n  ✓ No issues recorded.")

    print(f"\n{'=' * 64}\n")


def _count_issues(issues: list) -> dict:
    counts: dict = {}
    for i in issues:
        sev = i.get("severity", "?")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _format_issue_counts(counts: dict) -> str:
    parts = []
    for sev, icon in [("error", "✗"), ("warning", "⚠"), ("info", "ℹ")]:
        if counts.get(sev, 0):
            parts.append(f"{icon}{counts[sev]}")
    return " ".join(parts) if parts else "—"


def _key_output_line(stage: dict) -> str:
    """Return a short summary of the most interesting output for a stage."""
    name = stage.get("stage", "")
    out = stage.get("outputs", {})
    if name == "harvest":
        return f"{out.get('classified', '?')} classified, {out.get('unclassified', 0)} unclassified"
    if name == "generate":
        lv = out.get("locking_views_written", 0)
        bv = out.get("business_views_rewritten", 0)
        return f"{lv} locking views, {bv} business views"
    if name == "inspect":
        return ""
    if name == "analyse":
        return (
            f"{out.get('object_count', '?')} objects, "
            f"{out.get('wave_count', '?')} waves, "
            f"{out.get('cycle_count', 0)} cycles"
        )
    if name == "package":
        arch = out.get("archive_path", "")
        return os.path.basename(arch) if arch else ""
    return ""


def _wrap(text: str, width: int, indent: str) -> list:
    words = text.split()
    lines = []
    current = indent
    for w in words:
        if len(current) + len(w) + 1 > width:
            lines.append(current.rstrip())
            current = indent + w + " "
        else:
            current += w + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines


# ---------------------------------------------------------------
# verify — pre-deploy sanity check against ships.decisions.json
# ---------------------------------------------------------------


def _cmd_verify(args):
    """
    Item 6b — verify: pre-deploy sanity check from ships.decisions.json.

    Reads ships.decisions.json and finds the most recent package stage.
    Checks: the archive file still exists on disk, no PACKAGE_WARNING
    issues were recorded, and the build looks complete.  Intended as
    the final gate before an operator runs ``deploy``.
    """
    from td_release_packager.orchestrator import DecisionsManifest
    from td_release_packager.project_paths import decisions_json_path

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    manifest_path = decisions_json_path(project_dir)
    if not os.path.exists(manifest_path):
        print(
            f"ERROR: No ships.decisions.json found at {manifest_path}.",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = DecisionsManifest(manifest_path)
    runs = manifest.data.get("runs", [])

    # Find the last stage named "package" across all runs
    pkg_stage = None
    pkg_run = None
    for run in reversed(runs):
        for stage in reversed(run.get("stages", [])):
            if stage.get("stage") == "package":
                pkg_stage = stage
                pkg_run = run
                break
        if pkg_stage:
            break

    if pkg_stage is None:
        print(
            "  No package stage found in ships.decisions.json.\n"
            "  Run the pipeline with packaging enabled:\n"
            "    ships process ... --env DEV --env-config ... --name ...",
            file=sys.stderr,
        )
        sys.exit(1)

    out = pkg_stage.get("outputs", {})
    issues = pkg_stage.get("issues", [])
    archive_path = out.get("archive_path", "")
    archive_exists = bool(archive_path) and os.path.exists(archive_path)

    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]

    # ---- Checklist -----------------------------------------------
    print(f"\n{'=' * 64}")
    print("  SHIPS Verify — Package Readiness")
    print(f"{'=' * 64}")
    print(f"  Run:         {pkg_run.get('run_id', '—')}")
    print(f"  Archive:     {archive_path or '—'}")
    print(f"  Environment: {out.get('environment', '—')}")
    print(f"  Build:       {out.get('build_number', '—')}")
    print(f"  Files:       {out.get('file_count', '—')}")
    print(f"  Tokens:      {out.get('token_count', '—')} substitutions")

    print("\n  Checklist:")
    checks = []

    # 1. Archive exists
    if archive_exists:
        print("    ✓ Archive exists on disk")
        checks.append(True)
    else:
        print(f"    ✗ Archive NOT found: {archive_path}")
        checks.append(False)

    # 2. Package stage issues — errors block deployment; warnings are informational
    if not errors and not warnings:
        print("    ✓ No package issues recorded")
        checks.append(True)
    else:
        # Errors are blocking — show with ✗ and fail the check
        for i in errors:
            print(f"    ✗ {i.get('code', '?')}: {i.get('message', '')}")
        if errors:
            checks.append(False)

        # Warnings are informational — show with ⚠ but do not block deployment
        for i in warnings:
            print(f"    ⚠  {i.get('code', '?')}: {i.get('message', '')}")
        if warnings and not errors:
            print(
                f"    ↳ {len(warnings)} warning(s) above are informational "
                f"and do not block deployment."
            )
            checks.append(True)

    # 3. Package stage status
    pkg_status = pkg_stage.get("status", "unknown")
    if pkg_status == "success":
        print("    ✓ Package stage status: success")
        checks.append(True)
    else:
        print(f"    ✗ Package stage status: {pkg_status}")
        checks.append(False)

    # 4. Companion (prereqs) awareness
    if out.get("has_companion"):
        companion = out.get("companion_archive_path", "")
        companion_exists = bool(companion) and os.path.exists(companion)
        if companion_exists:
            print("    ✓ Companion (prereqs) archive exists")
        else:
            print(f"    ✗ Companion archive NOT found: {companion}")
            checks.append(False)

    ready = all(checks)
    verdict = "READY" if ready else "NOT READY"
    verdict_icon = "✓" if ready else "✗"

    print(f"\n  {verdict_icon} Verdict: {verdict}")
    print(f"{'=' * 64}\n")

    sys.exit(0 if ready else 1)


def _cmd_generate(args):
    """
    Generate view-layer DDL from the harvested table payload.

    Orchestrator wrapper for ``td_release_packager.view_layer_generator``.
    Wired onto ``_stage_recording`` so every run is captured in
    ``ships.decisions.json``.
    """
    from td_release_packager.orchestrator import issue_codes as _ic

    source_dir = args.project
    if not os.path.isdir(source_dir):
        print(f"ERROR: Project directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    with _stage_recording(source_dir, "generate") as stage:
        exit_code = _run_generate(args, stage, _ic)

    sys.exit(exit_code)


def _run_generate(args, stage, issue_codes) -> int:
    """
    Body of the generate command.

    Calls ``view_layer_generator.run()`` and records the result into
    the stage recorder.  Returns the exit code for the shell.

    Args:
        args:        Parsed CLI arguments.
        stage:       StageRecorder or _NullStageRecorder.
        issue_codes: The issue_codes module (injected for testability).

    Returns:
        0 on success, 1 if the generator reported errors.
    """
    from td_release_packager.view_layer_generator import run as generate_views

    source_dir = args.project
    dry_run = getattr(args, "dry_run", False)
    modules_arg = getattr(args, "modules", None)
    project_path = Path(source_dir)
    requested_modules = (
        {m.strip().upper() for m in modules_arg.split(",") if m.strip()}
        if modules_arg
        else None
    )
    config_files = _resolve_generate_config_files(project_path)

    stage.set_config_resolved("source", source_dir, "layer-5", "cli")
    stage.set_config_resolved("dry_run", dry_run, "layer-5", "cli")
    stage.set_config_resolved("modules", modules_arg or "(all)", "layer-5", "cli")
    for config_file in config_files:
        stage.set_config_resolved(
            config_file["key"],
            config_file["path"] if config_file["exists"] else None,
            config_file["source"],
            config_file["source_path"],
        )
    stage.set_inputs(source_dir=source_dir)

    result = generate_views(
        project_root=project_path,
        requested_modules=requested_modules,
        dry_run=dry_run,
    )

    stage.set_outputs(
        locking_views_written=result.locking_views_written,
        locking_views_unchanged=result.locking_views_unchanged,
        business_views_rewritten=result.business_views_rewritten,
        business_views_unchanged=result.business_views_unchanged,
        databases_written=result.databases_written,
        grants_written=result.grants_written,
        config_files=config_files,
    )

    for w in result.warnings:
        stage.add_issue("warning", issue_codes.GENERATE_WARNING, w)
    for e in result.errors:
        stage.add_issue("error", issue_codes.GENERATE_ERROR, e)
    # Generate is opt-in: when the payload does not use the paired
    # token convention the generator can't do anything, but that is
    # informational, not a failure.
    if result.skipped and result.skip_reason:
        stage.add_issue("info", issue_codes.GENERATE_WARNING, result.skip_reason)

    print(f"\n{'=' * 64}")
    print("  View Layer Generation")
    print(f"{'=' * 64}")
    print(f"  Source:           {source_dir}")
    if dry_run:
        print("  Mode:             DRY RUN — no files written")
    if requested_modules:
        print(f"  Modules:          {', '.join(sorted(requested_modules))}")
    print("  Convention:       payload filename convention (*_T → *_V)")
    print("  Configuration:")
    for config_file in config_files:
        status = "found" if config_file["exists"] else "missing"
        used = "used here" if config_file["used_by_generate"] else "not read here"
        print(f"    - {config_file['label']}: {status}, {used} — {config_file['path']}")

    print(
        f"  Locking views:    {result.locking_views_written} written"
        f" / {result.locking_views_unchanged} unchanged"
    )
    print(
        f"  Business views:   {result.business_views_rewritten} rewritten"
        f" / {result.business_views_unchanged} unchanged"
    )
    print(f"  Databases:        {result.databases_written} written")
    print(f"  Grants:           {result.grants_written} written")

    if result.skipped and result.skip_reason:
        print("\n  Skipped (informational):")
        print(f"    ℹ {result.skip_reason}")

    if result.warnings:
        print("\n  Warnings:")
        for w in result.warnings:
            print(f"    ⚠  {w}")

    if result.errors:
        print("\n  Errors:")
        for e in result.errors:
            print(f"    ✗ {e}")

    print(f"{'=' * 64}\n")

    return 1 if result.errors else 0


def _resolve_generate_config_files(project_path: Path) -> list[dict[str, object]]:
    """
    Return config-file provenance shown by ``generate`` and recorded
    in ships.decisions.json.

    ``generate`` is intentionally payload-driven: it uses the payload
    filename convention to derive table/view companions, for example
    ``{{DB_DOMAIN_T}}`` and ``{{DB_DOMAIN_V}}``. The neighbouring config
    files still matter to users because they govern how those payload files
    are harvested and validated, so the banner names them explicitly.
    """
    candidates = [
        {
            "key": "object_placement_config",
            "label": "object placement",
            "path": project_path / "object_placement.yaml",
            "source": "layer-3",
            "source_path": "project/object_placement.yaml",
            "used_by_generate": False,
            "purpose": "Placement policy used by inspect; generate uses payload filename convention.",
        },
        {
            "key": "inspect_config",
            "label": "inspect rules",
            "path": project_path / "config" / "inspect.conf",
            "source": "layer-3",
            "source_path": "project/config/inspect.conf",
            "used_by_generate": False,
            "purpose": "Validation rule severities used by inspect after generation.",
        },
        {
            "key": "token_map",
            "label": "token map",
            "path": project_path / "config" / "token_map.conf",
            "source": "layer-3",
            "source_path": "project/config/token_map.conf",
            "used_by_generate": False,
            "purpose": "Harvest token substitutions; re-harvest after changing it.",
        },
    ]
    resolved: list[dict[str, object]] = []
    for candidate in candidates:
        path = candidate["path"]
        assert isinstance(path, Path)
        resolved.append(
            {
                **candidate,
                "path": str(path),
                "exists": path.is_file(),
            }
        )
    return resolved


def _cmd_scan(args):
    """
    Scan payload files for token references.

    Enhanced with --all-envs (sweep all env configs), --show-map
    (reverse token-to-file index), --format json (machine-readable),
    and --fail-on-orphan (CI gate for dead config entries).
    """
    import glob as _glob
    import json as _json

    from td_release_packager.orchestrator import issue_codes

    source_dir = args.project
    if not os.path.isdir(source_dir):
        print(f"ERROR: Project directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "env_config", None) and getattr(args, "all_envs", False):
        print(
            "ERROR: --env-config and --all-envs are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve payload directory
    scan_dir = source_dir
    for candidate in ["payload/database", "payload"]:
        path = os.path.join(source_dir, candidate)
        if os.path.isdir(path):
            scan_dir = path
            break

    # Collect env config files
    env_configs: list[str] = []
    if getattr(args, "all_envs", False):
        pattern = os.path.join(source_dir, "config", "env", "*.conf")
        env_configs = sorted(_glob.glob(pattern))
        if not env_configs:
            print(
                f"  ⚠  No *.conf files found in {os.path.join(source_dir, 'config', 'env')}"
            )
    elif getattr(args, "env_config", None):
        env_configs = [args.env_config]

    fmt = getattr(args, "format", "text")
    show_map = getattr(args, "show_map", False)
    fail_on_orphan = getattr(args, "fail_on_orphan", False)

    with _stage_recording(source_dir, "scan") as stage:
        stage.set_config_resolved("source", source_dir, "layer-5", "cli")
        stage.set_config_resolved(
            "env_config", args.env_config or None, "layer-5", "cli"
        )
        stage.set_config_resolved(
            "all_envs", getattr(args, "all_envs", False), "layer-5", "cli"
        )

        usage = scan_tokens_in_directory(scan_dir)

        all_tokens: set[str] = set()
        for tokens in usage.values():
            all_tokens.update(tokens)

        # Build reverse map: token → sorted list of relative file paths
        token_map: dict[str, list[str]] = {}
        for token in sorted(all_tokens):
            files = sorted(
                os.path.relpath(f, scan_dir)
                for f, toks in usage.items()
                if token in toks
            )
            token_map[token] = files

        # Per-env validation results
        env_results: dict[str, dict] = {}
        has_any_error = False
        has_any_orphan = False

        for cfg_path in env_configs:
            env_name = os.path.splitext(os.path.basename(cfg_path))[0]
            try:
                values = read_env_config(cfg_path)
                errors, warnings = validate_tokens(values, usage, config_file=cfg_path)
                orphan_count = len(warnings)
                env_results[env_name] = {
                    "config": cfg_path,
                    "undefined": errors,
                    "orphans": warnings,
                    "status": "error" if errors else ("warning" if warnings else "ok"),
                }
                for e in errors:
                    stage.add_issue("error", issue_codes.TOKEN_UNDEFINED, e)
                for w in warnings:
                    stage.add_issue("warning", issue_codes.TOKEN_UNUSED, w)
                if errors:
                    has_any_error = True
                if orphan_count:
                    has_any_orphan = True
            except FileNotFoundError:
                env_results[env_name] = {
                    "config": cfg_path,
                    "undefined": [],
                    "orphans": [],
                    "status": "error",
                    "error": "config file not found",
                }
                stage.add_issue(
                    "error",
                    issue_codes.PROPERTIES_NOT_FOUND,
                    f"Config file not found: {cfg_path}",
                )
                has_any_error = True
            except ValueError as exc:
                message = (
                    "[ConfigError] Could not read environment config "
                    f"{cfg_path}.\n\n{exc}\n\n"
                    "Suggested action: check for merged KEY=VALUE lines, "
                    "unresolved {{TOKEN}} references, or copied values with "
                    "stray braces."
                )
                env_results[env_name] = {
                    "config": cfg_path,
                    "undefined": [],
                    "orphans": [],
                    "status": "error",
                    "error": message,
                }
                stage.add_issue("error", issue_codes.PROPERTIES_INVALID, message)
                has_any_error = True

        stage.set_inputs(scan_directory=scan_dir, files_with_tokens=len(usage))
        stage.set_outputs(
            unique_tokens=len(all_tokens),
            tokens=sorted(all_tokens),
            env_results={k: v["status"] for k, v in env_results.items()},
        )

        if has_any_error:
            stage.set_status("error")
        elif has_any_orphan:
            stage.set_status("warning")

        # ── Output ──────────────────────────────────────────────────
        if fmt == "json":
            out = {
                "scan_dir": scan_dir,
                "unique_tokens": len(all_tokens),
                "files_with_tokens": len(usage),
                "token_map": {
                    t: {"count": len(fs), "files": fs} for t, fs in token_map.items()
                },
                "validation": env_results,
            }
            print(_json.dumps(out, indent=2))
        else:
            _scan_print_text(
                scan_dir, all_tokens, token_map, env_results, show_map, env_configs
            )

    # Return exit code: 1 on errors; 1 on orphans when --fail-on-orphan.
    # The CLI dispatcher calls sys.exit() with this value so that direct
    # callers (tests, library code) are not interrupted by SystemExit.
    if has_any_error or (fail_on_orphan and has_any_orphan):
        return 1
    return 0


def _scan_print_text(
    scan_dir: str,
    all_tokens: set,
    token_map: dict,
    env_results: dict,
    show_map: bool,
    env_configs: list,
) -> None:
    """Print scan results in human-readable text format."""
    W = 64
    print(f"\n{'=' * W}")
    print("  Token Scan")
    print(f"  {scan_dir}")
    print(f"{'=' * W}")
    print(f"  Unique tokens      : {len(all_tokens)}")
    print(f"  Files with tokens  : {sum(1 for fs in token_map.values() if fs)}")

    if not all_tokens:
        print("\n  No {{TOKEN}} references found.")

    # Token inventory
    elif show_map:
        print("\n  Token → file map:")
        for token, files in token_map.items():
            print(f"\n    {{{{{token}}}}}  ({len(files)} reference(s))")
            for f in files[:10]:
                print(f"        {f}")
            if len(files) > 10:
                print(f"        … and {len(files) - 10} more")
    else:
        print("\n  Tokens found:")
        for token, files in token_map.items():
            print(f"    {{{{{token}}}}} — {len(files)} file(s)")

    # Per-environment validation
    if env_results:
        print(f"\n  {'─' * (W - 2)}")
        multi = len(env_results) > 1
        for env_name, result in env_results.items():
            label = f"[{env_name}]" if multi else ""
            status = result.get("status", "?")
            icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(status, "?")

            if result.get("error"):
                print(f"\n  {icon} {label} {result['error']}")
                continue

            undef = result.get("undefined", [])
            orphans = result.get("orphans", [])

            if not undef and not orphans:
                print(
                    f"\n  {icon} {label} All tokens resolved — no undefined or orphan tokens"
                )
            else:
                if undef:
                    print(
                        f"\n  {icon} {label} UNDEFINED tokens (referenced but not defined):"
                    )
                    for e in undef:
                        print(f"      {e}")
                    # Recovery hint: when many tokens are undefined this is
                    # almost always payload / env-config misalignment (e.g.
                    # scaffolded with one naming convention, harvested with
                    # another). bootstrap-env-config regenerates the .conf
                    # to match the actual token references in the payload.
                    _unique_undef_tokens = {e for e in undef if not e.startswith("  →")}
                    if len(_unique_undef_tokens) >= 3:
                        print(
                            f"\n      ℹ Many tokens undefined — likely a "
                            f"payload/env-config naming mismatch. To "
                            f"regenerate {label} matching the actual "
                            f"payload references, run:"
                        )
                        print(
                            f"        {ships_cmd()} "
                            f"bootstrap-env-config --source <project> "
                            f"--env <env> --force"
                        )
                if orphans:
                    print(
                        f"\n  ⚠  {label} ORPHAN tokens (defined but never referenced):"
                    )
                    for w in orphans:
                        print(f"      {w}")

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
    source_dir = args.project
    if not os.path.isdir(source_dir):
        print(f"ERROR: Project directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    from td_release_packager.orchestrator import issue_codes as _ic

    with _stage_recording(source_dir, "analyse") as stage:
        exit_code = _run_analyze(args, stage, _ic)

    sys.exit(exit_code)


def _run_analyze(args, stage, issue_codes) -> int:
    """
    Body of the analyse command, factored out so ``_cmd_analyze``
    can wrap it in ``_stage_recording`` without indenting the body.

    Returns:
        Exit code for the shell.
    """
    from td_release_packager.analyser import analyse_project, format_summary

    source_dir = args.project

    stage.set_config_resolved("source", source_dir, "layer-5", "cli")
    stage.set_config_resolved("output", getattr(args, "output", None), "layer-5", "cli")
    stage.set_config_resolved(
        "overwrite", getattr(args, "overwrite", False), "layer-5", "cli"
    )

    result = analyse_project(source_dir)

    # Count total external refs across all objects
    external_ref_count = sum(len(v) for v in result.external_deps.values())

    stage.set_inputs(source_dir=source_dir)
    stage.set_outputs(
        object_count=len(result.objects),
        wave_count=len(result.waves),
        dependency_count=sum(len(v) for v in result.dependencies.values()),
        cycle_count=len(result.cycles),
        external_ref_count=external_ref_count,
    )

    for cycle in result.cycles:
        stage.add_issue(
            "error",
            issue_codes.ANALYSE_CYCLE,
            " → ".join(cycle),
        )
    for obj_name, ext_refs in result.external_deps.items():
        for ref in ext_refs:
            stage.add_issue(
                "info",
                issue_codes.ANALYSE_EXTERNAL_REF,
                f"{obj_name} references external object {ref}",
            )

    print(f"\n{'=' * 64}")
    print("  SHIPS Dependency Analysis")
    print(f"{'=' * 64}")
    print(format_summary(result))

    if not result.objects:
        print("  No DDL objects found. Check the payload directory.")
        print()
        return 0

    # -- Write _waves.txt -----------------------------------------
    if args.output:
        waves_path = args.output
    else:
        from td_release_packager.project_paths import (
            ensure_ships_state_dir,
            waves_txt_path,
        )

        ensure_ships_state_dir(source_dir)
        waves_path = waves_txt_path(source_dir)

    if result.waves:
        # ``.ships/_waves.txt`` is a regenerable artefact, not user-edited
        # state. Always rewrite it on analyse so downstream consumers
        # (pipeline_report Payload tab, packaging) reflect the current
        # source state — keeping the previous run's file frozen made the
        # Payload tab render 'waves not computed yet' after every source
        # edit (#449). ``--overwrite`` remains as a no-op flag for
        # backwards compatibility.
        with open(waves_path, "w", encoding="utf-8") as f:
            f.write(result.waves_file_content)
        stage.set_outputs(waves_path=waves_path)
        print(f"\n  ✓ Wave file written: {waves_path}")
        print(f"    {len(result.waves)} waves, {len(result.objects)} objects")

    if result.cycles:
        print(
            f"\n  ⚠  {len(result.cycles)} cycle(s) detected — review before deploying"
        )

    # -- Export graph (if requested) -------------------------------
    if args.graph:
        _export_graph(result, args)

    print(f"{'=' * 64}\n")
    return 0


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


def _cmd_changeset(args) -> int:
    """Preview the changed-object set + dependants since a reference point.

    Detection mode is git-native when a ``--since-tag`` / ``--since-commit``
    ref is given inside a git repo, falling back to a content-hash baseline
    under ``.ships/`` otherwise. ``--update-baseline`` captures the current
    payload state for the next git-less run. Issue #114.
    """
    from td_release_packager.changeset import (
        detect_changeset,
        write_changeset_baseline,
    )
    from td_release_packager.validate import resolve_inspect_root

    project = args.project
    if not os.path.isdir(project):
        print(f"ERROR: project directory not found: {project}")
        return 1

    if getattr(args, "update_baseline", False):
        payload_dir = resolve_inspect_root(project)
        path = write_changeset_baseline(project, payload_dir)
        print(f"Captured changeset baseline: {path}")
        return 0

    since = getattr(args, "since_tag", None) or getattr(args, "since_commit", None)
    result = detect_changeset(project, since=since)

    if result.mode == "none":
        print(result.note)
        return 1

    print(f"Detection mode : {result.mode}")
    print(f"Changed files  : {len(result.changed_files)}")
    print(f"Changed objects: {len(result.changed)}")
    print(f"Dependants     : {len(result.dependants)}")
    print(f"Total selected : {len(result.selected)}")
    if result.changed:
        print("\nChanged objects:")
        for qn in sorted(result.changed):
            print(f"  + {qn}")
    if result.dependants:
        print("\nDependants pulled in:")
        for qn in sorted(result.dependants):
            print(f"  ~ {qn}")
    return 0


def _cmd_plan(args) -> int:
    """Detect-and-recommend: inspect a source tree and emit a packaging plan.

    Auto-answers the detectable questions (source type, tokenised, atomic),
    overlays any CLI overrides on top, then prints the recommended ``ships``
    command sequence + rationale and (optionally) writes plan.json. Issue #379.
    """
    import json as _json

    from td_release_packager import packaging_plan as _pp
    from td_release_packager import plan_detect as _pd

    source = args.source
    if not os.path.isdir(source):
        print(f"ERROR: source directory not found: {source}", file=sys.stderr)
        return 1

    detection = _pd.detect_answers(source)

    overrides = {
        "project.dir": getattr(args, "project", None) or source,
        "package.name": getattr(args, "name", None),
        "mode.style": getattr(args, "mode", None),
        "envs": getattr(args, "env", None),
    }
    if getattr(args, "strict", False):
        overrides["process.strict"] = True
    if getattr(args, "scaffolded", False):
        overrides["project.scaffolded"] = True
    if getattr(args, "no_generate", False):
        overrides["generate.enabled"] = "no"

    answers = _pd.merge_answers(detection.answers, overrides)
    plan = _pp.build_plan(answers)

    print("SHIPS packaging plan")
    print("=" * 60)
    print("\nDetected:")
    for f in detection.findings:
        print(f"  - {f}")
    if plan.notes:
        print("\nNotes:")
        for n in plan.notes:
            print(f"  ! {n}")

    print()
    print(_pp.format_plan(plan))

    out_path = getattr(args, "json", None)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            _json.dump(plan.plan_json, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"\nWrote plan.json: {out_path}")

    return 0


def _cmd_wizard(args) -> int:
    """Interactive terminal wizard over the decision model (#381).

    Walks the decision-tree questions, optionally pre-seeded from source
    detection, then emits the recommended plan + optional plan.json. A plain
    stdin/stdout loop, so it works over SSH.
    """
    import json as _json

    from td_release_packager import packaging_plan as _pp
    from td_release_packager import plan_detect as _pd
    from td_release_packager import wizard as _wiz

    seed: dict = {}
    source = getattr(args, "source", None)
    if source:
        if not os.path.isdir(source):
            print(f"ERROR: source directory not found: {source}", file=sys.stderr)
            return 1
        detection = _pd.detect_answers(source)
        seed = detection.answers
        print("Detected from source:")
        for f in detection.findings:
            print(f"  - {f}")

    try:
        answers = _wiz.run_wizard(answers=seed)
    except (EOFError, KeyboardInterrupt):
        print("\nWizard cancelled.", file=sys.stderr)
        return 1

    plan = _pp.build_plan(answers)
    print("\n" + "=" * 60)
    print(_pp.format_plan(plan))

    out_path = getattr(args, "json", None)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            _json.dump(plan.plan_json, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"\nWrote plan.json: {out_path}")

    return 0


def _cmd_metadata(args) -> int:
    """Dispatch `ships metadata` sub-commands (#244)."""
    sub = getattr(args, "metadata_subcommand", None)
    if sub == "export-alation":
        return _export_catalogue(args, "alation")
    if sub == "export-collibra":
        return _export_catalogue(args, "collibra")
    if sub == "export-datahub":
        return _export_catalogue(args, "datahub")
    print(
        "Usage: ships metadata export-alation | export-collibra | export-datahub "
        "--package-dir DIR --output DIR [--include-internal] [--strict]"
    )
    return 1


def _export_catalogue(args, catalogue: str) -> int:
    """Extract product metadata from a package and render a catalogue bundle."""
    import json as _json
    from datetime import datetime, timezone

    from td_release_packager.metadata_export import (
        RENDERERS,
        MetadataExtractError,
        extract_product_metadata,
    )

    try:
        meta = extract_product_metadata(
            args.package_dir, include_internal=getattr(args, "include_internal", False)
        )
    except MetadataExtractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "strict", False) and meta.warnings:
        print("ERROR: --strict and metadata is incomplete:", file=sys.stderr)
        for w in meta.warnings:
            print(f"  - {w}", file=sys.stderr)
        return 1

    generated_at = datetime.now(timezone.utc).isoformat()
    bundle = RENDERERS[catalogue](meta, generated_at)

    out_dir = os.path.join(args.output, catalogue)
    os.makedirs(out_dir, exist_ok=True)
    for filename, obj in bundle.items():
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as fh:
            _json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    print(f"{catalogue.title()} metadata bundle written to: {out_dir}")
    print(
        f"  files: {len(bundle)}  interfaces: {len(meta.interfaces)}  "
        f"assets: {len(meta.physical_assets)}  columns: {len(meta.columns)}"
    )
    if meta.warnings:
        print("  warnings:")
        for w in meta.warnings:
            print(f"    ! {w}")
    return 0


# ---------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------


def _cmd_keygen(args):
    """Generate an Ed25519 keypair for asymmetric package signing.

    Writes two PEM files to *output_dir*:
        ships_signing_private.pem  — keep secret; store in CI/CD secrets
        ships_signing_public.pem   — commit to your project repository

    Requires the ``cryptography`` package (pip install cryptography>=42.0).
    """
    try:
        from database_package_deployer import asym_signing
    except ImportError as exc:
        print(f"ERROR: could not import asym_signing: {exc}", flush=True)
        sys.exit(1)

    try:
        private_pem, public_pem = asym_signing.generate_keypair()
    except ImportError as exc:
        print(f"ERROR: {exc}", flush=True)
        sys.exit(1)

    output_dir = getattr(args, "output_dir", ".") or "."
    os.makedirs(output_dir, exist_ok=True)

    private_path = os.path.join(output_dir, "ships_signing_private.pem")
    public_path = os.path.join(output_dir, "ships_signing_public.pem")

    import pathlib

    pathlib.Path(private_path).write_text(private_pem, encoding="utf-8")
    pathlib.Path(public_path).write_text(public_pem, encoding="utf-8")

    print()
    print("Ed25519 keypair generated.")
    print()
    print(f"  Private key: {private_path}")
    print("    ACTION REQUIRED — keep this file secret:")
    print("      - Store it as a CI/CD secret (SHIPS_PRIVATE_KEY_PATH env var).")
    print("      - Never commit it to source control.")
    print("      - Never copy it to developer workstations.")
    print()
    print(f"  Public key:  {public_path}")
    print("    Safe to share — commit this file to your project repository.")
    print(
        "    DBAs use it to verify package signatures without needing the private key."
    )
    print()
    print("  Usage:")
    print("    ships package ... --asymmetric-key ships_signing_private.pem")
    print("    ships deploy <pkg_dir> --public-key ships_signing_public.pem ...")
    print()


def _cmd_clean(args) -> None:
    """Wipe prior pipeline output for a SHIPS project (CLI surface).

    Thin wrapper around :func:`td_release_packager.cleaner.clean_project`
    so the CLI and the ``ships_clean`` MCP tool share a single code path.
    Defaults to dry-run; pass ``--apply`` to actually delete.
    """
    from td_release_packager.cleaner import clean_project

    result = clean_project(
        project=args.project,
        scope=args.scope,
        dry_run=not args.apply,
    )

    if not result.get("success"):
        print(f"ERROR: {result.get('error', 'clean failed')}", file=sys.stderr)
        sys.exit(1)

    mode = "Would remove" if result["dry_run"] else "Removed"
    print(
        f"{mode} {result['removed_files']} file(s) across "
        f"{len(result['targets'])} target(s) (scope={result['scope']})."
    )
    for target in result["targets"]:
        marker = "·" if target["exists"] else " "
        print(f"  {marker} {target['path']} ({target['kind']})")

    if not result["dry_run"]:
        state = result.get("lifecycle_state_after")
        if state:
            print(f"  lifecycle_state: {state}")
        if result["dry_run"] is False and not state:
            # State lookup failed but the deletion itself succeeded;
            # surface a hint so the caller knows it's informational.
            print("  lifecycle_state: <unavailable>")


def _cmd_stage(args) -> int:
    """Gate on scan + inspect, then ``git add`` SHIPS-owned paths (issue #487).

    Thin CLI wrapper around
    :func:`td_release_packager.stager.stage_project`. The stager itself
    is decoupled from the CLI — it takes scan + inspect callables, so
    tests can pass stubs and this function is the only place that
    knows how to wire the real gates.
    """
    import argparse as _argparse

    from td_release_packager.stager import stage_project

    def _run_scan(project_dir: str) -> int:
        # ``--all-envs`` is the meaningful default: without it ``ships
        # scan`` only catches malformed-token format errors and returns
        # 0 even with undefined env tokens, which would make the gate
        # toothless. ``--all-envs`` against a project with no
        # ``config/env/`` is a no-op (prints a warning, returns 0).
        scan_ns = _argparse.Namespace(
            project=project_dir,
            env_config=None,
            all_envs=True,
            show_map=False,
            format="text",
            fail_on_orphan=False,
            verbose=getattr(args, "verbose", False),
        )
        return _cmd_scan(scan_ns)

    def _run_inspect_gate(project_dir: str) -> int:
        from td_release_packager.orchestrator import issue_codes

        # ``fix_*`` defaults are False here on purpose — the stage gate
        # must not rewrite files. The operator runs ``ships inspect``
        # explicitly when they want fixes applied, then re-runs stage.
        inspect_ns = _argparse.Namespace(
            project=project_dir,
            config=None,
            strict=getattr(args, "strict", False),
            update_contract_baseline=False,
            skip_tokens=False,
            skip_keywords=False,
            skip_commas=False,
            skip_grants=False,
            dcl_dir=None,
            verbose=getattr(args, "verbose", False),
        )
        try:
            with _stage_recording(project_dir, "inspect") as stage:
                return _run_inspect(inspect_ns, stage, issue_codes)
        except FileNotFoundError as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            return 1

    result = stage_project(
        args.project,
        run_scan=_run_scan,
        run_inspect=_run_inspect_gate,
        dry_run=getattr(args, "dry_run", False),
    )

    print(f"\n{'=' * 64}")
    print("  ships stage")
    print(f"{'=' * 64}")

    if not result.success:
        if result.blocked_by:
            print(f"  ✗ BLOCKED by `ships {result.blocked_by}`")
        else:
            print("  ✗ FAILED")
        print(f"  {result.error}")
        return 1

    verb = "Would stage" if result.dry_run else "Staged"
    print(f"  ✓ {verb} {len(result.staged_paths)} SHIPS-owned path(s):")
    for p in result.staged_paths:
        print(f"      {p}")
    if result.repo_root:
        # When the project IS the repo root these two paths match; for
        # a project nested inside a monorepo, showing the repo path
        # explicitly is the only way the operator sees which index was
        # touched.
        print(f"\n  Repo: {result.repo_root}")
    if result.dry_run:
        print("  Re-run without --dry-run to update the git index.")
    else:
        print("  Next: `git commit -m '<message>'`")
    return 0


def _cmd_fix_package_integrity(args) -> int:
    """Regenerate ``.sha256`` sidecars across a project's ``releases/`` tree.

    Walks every release group under ``<project>/releases/``, recomputes
    each ``*.zip``'s SHA-256 from its current bytes, and writes the
    matching ``<zip>.sha256`` sidecar. Used to repair legacy mismatches
    left by builds produced before the per-stage-result append bug
    (#463) was fixed — those builds shipped sidecars that disagreed
    with their on-disk archives.

    Does NOT touch the archives themselves — only the sidecars. The
    archive bytes are trusted as-is (they have to be; we can't
    reconstruct what the original generator intended). The goal is to
    restore the invariant that every released archive has a sidecar
    consistent with its bytes, so ``ships inspect`` stops reporting
    ``INSPECT_PACKAGE_INTEGRITY`` on historical builds.

    ``--dry-run`` previews the work without writing anything.

    Returns:
        Exit code — 0 on success, 1 if the project is malformed.
    """
    import hashlib

    project = os.path.abspath(args.project)
    releases_dir = os.path.join(project, "releases")
    if not os.path.isdir(releases_dir):
        print(f"No releases/ directory under {project} — nothing to fix.")
        return 0

    dry_run = bool(args.dry_run)
    fixed = 0
    matched = 0
    no_sidecar_created = 0
    skipped = 0
    rows: List[str] = []

    for group in sorted(os.listdir(releases_dir)):
        group_dir = os.path.join(releases_dir, group)
        if not os.path.isdir(group_dir):
            continue
        for filename in sorted(os.listdir(group_dir)):
            if not filename.lower().endswith(".zip"):
                continue
            archive = os.path.join(group_dir, filename)
            sidecar = archive + ".sha256"
            try:
                with open(archive, "rb") as fh:
                    live = hashlib.sha256(fh.read()).hexdigest()
            except OSError as exc:
                rows.append(f"  ⚠  {group}/{filename}: cannot read — {exc}")
                skipped += 1
                continue

            recorded = None
            if os.path.isfile(sidecar):
                try:
                    recorded = (
                        open(sidecar, encoding="utf-8")
                        .readline()
                        .strip()
                        .split()[0]
                        .lower()
                    )
                except (OSError, IndexError):
                    recorded = None

            if recorded == live:
                matched += 1
                continue

            if dry_run:
                if recorded is None:
                    rows.append(
                        f"  ✎ {group}/{filename}: would create sidecar (live={live[:12]}…)"
                    )
                else:
                    rows.append(
                        f"  ✎ {group}/{filename}: would refresh sidecar "
                        f"(recorded={recorded[:12]}… → live={live[:12]}…)"
                    )
                fixed += 1
                continue

            with open(sidecar, "w", encoding="utf-8") as fh:
                fh.write(f"{live}  {filename}\n")
            if recorded is None:
                rows.append(f"  + {group}/{filename}: created sidecar")
                no_sidecar_created += 1
            else:
                rows.append(
                    f"  ✓ {group}/{filename}: refreshed sidecar "
                    f"({recorded[:12]}… → {live[:12]}…)"
                )
            fixed += 1

    print(f"\n{'=' * 64}")
    if dry_run:
        print(f"  ships fix-package-integrity (dry-run) — {project}")
    else:
        print(f"  ships fix-package-integrity — {project}")
    print(f"{'=' * 64}")
    if rows:
        for row in rows:
            print(row)
    print(
        f"\n  {matched} archive(s) already consistent · "
        f"{fixed} {'would be ' if dry_run else ''}refreshed"
        + (f" ({no_sidecar_created} newly created)" if no_sidecar_created else "")
        + (f" · {skipped} skipped" if skipped else "")
    )
    if dry_run and fixed:
        print("\n  Re-run without --dry-run to apply.")
    print(f"{'=' * 64}\n")
    return 0


def _build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="td_release_packager",
        description="SHIPS — Scaffold, Harvest, Inspect, Package, Ship. "
        "Standardised Teradata DDL deployment methodology.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    add_version_argument(parser, prog="td_release_packager")

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
        help="[DEPRECATED — prefer config/tokenise.conf] "
        "Path to token_map.conf — applies literal → {{TOKEN}} "
        "substitutions during harvest. Still works; new projects "
        "should use config/tokenise.conf (regex-based, more "
        "powerful) authored via the SHIPS Navigator wizard, "
        "the ships_author_token_map MCP tool, or by hand. "
        "Generate one with --generate-token-map first, "
        "review it, then pass it here.",
    )
    ig.add_argument(
        "--generate-token-map",
        action="store_true",
        help="[DEPRECATED — prefer config/tokenise.conf] "
        "Scan for hardcoded database names and write a "
        "token_map.conf to the project's config/ directory. "
        "Requires --env-prefix to derive token names. "
        "There is no equivalent auto-generator for tokenise.conf "
        "yet — until there is, this flag remains the only "
        "auto-discovery path; new projects should still prefer "
        "hand-authored tokenise.conf where the token set is known.",
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
        "--prefix-token",
        action="append",
        default=None,
        metavar="SOURCE=TOKEN",
        help="Identifier-aware prefix tokenisation (Model B). "
        "Rewrites a database-name PREFIX to a single {{TOKEN}} while "
        "preserving the structural remainder. "
        "E.g. '--prefix-token CallCentre=PREFIX' turns "
        "'CallCentre_DOM_STD_T' into '{{PREFIX}}_DOM_STD_T' and a "
        "standalone 'CallCentre' into '{{PREFIX}}'. Repeatable; "
        "longest prefix wins. Different from --token-map "
        "(literal/substring) and --env-prefix (strip + per-database).",
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
        "Redundant under the default clean-payload mode "
        "(nothing pre-exists to overwrite); only meaningful "
        "alongside --keep-existing, where it governs "
        "per-file collisions during overlay re-harvest.",
    )
    ig.add_argument(
        "--keep-existing",
        action="store_true",
        help="Skip the pre-harvest payload clean and overlay "
        "new files on top of whatever is already in "
        "payload/database/. The default behaviour wipes "
        "harvest-owned files first (preserving .gitkeep and "
        "control files starting with '_' like a user-curated "
        "_order.txt) so the payload always reflects current "
        "source state without orphaned artefacts.",
    )
    ig.add_argument(
        "--auto-tokenise",
        action="store_true",
        dest="auto_tokenise",
        help="Auto-detect hardcoded database names and apply token "
        "substitutions in a single pass — no manual token_map.conf "
        "review step required. The token map is derived automatically "
        "from detected candidates (optionally stripped with "
        "--env-prefix) and applied immediately. Use in developer "
        "mode when speed matters more than reviewing every token.",
    )
    ig.add_argument(
        "--remove-view-type-affixes",
        action="store_true",
        dest="remove_view_type_affixes",
        help="Remove redundant view object type affixes during harvest "
        "(leading v_ and trailing _v) and update qualified references "
        "before writing payload files.",
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
    ig.add_argument(
        "--root-parent",
        help=(
            "Root database/user parent for parentless CREATE DATABASE/USER "
            "prerequisites (#501/#505). Injection runs immediately after "
            "harvest so inspect and downstream verbs see the corrected "
            "payload. Existing FROM clauses are preserved. The same flag is "
            "available on `ships package` and `ships process` — passing it "
            "on harvest in Detailed mode is required because the "
            "package-stage injection happens too late for the intervening "
            "inspect call."
        ),
    )

    # -- generate --
    gn = subs.add_parser(
        "generate",
        help="[G] Generate — build view-layer DDL from harvested tables.",
    )
    gn.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing the harvested payload.",
    )
    gn.add_argument(
        "--modules",
        default=None,
        help="Comma-separated module names to generate (e.g. 'DOM,SEM'). "
        "Omit to generate all discovered modules.",
    )
    gn.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Parse and validate without writing any files.",
    )

    # -- inspect --
    vl = subs.add_parser(
        "inspect", help="[I] Inspect — check DDL against Coding Discipline."
    )
    vl.add_argument(
        "--project", required=True, help="SHIPS project directory to inspect."
    )
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
        "--update-contract-baseline",
        action="store_true",
        dest="update_contract_baseline",
        default=False,
        help="Capture the current source object contracts (view columns, "
        "procedure parameters, table columns) as the baseline under "
        ".ships/ for the contract_change rule (#171), instead of comparing "
        "against it. Run this once you accept the current contracts.",
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
    # --fix-ddl-terminators / --fix-non-ascii / --fix-grants all moved to
    # `ships fix` in #522 (ddl_terminator, non_ascii) and #526 (grants).
    # `ships inspect` is now strictly read-only. The fixers live in
    # td_release_packager.fixers and are invoked via `ships fix` (the
    # default-on subset covers ddl_terminator and grants_derivation),
    # `ships fix --rules non_ascii` for the opt-in case, or `ships fix
    # --all` for the whole registry.
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

    # -- fix -- (#521)
    # Runs registered fixers from td_release_packager.fixers.FIX_REGISTRY.
    # Writes by default; --dry-run reports without writing and exits 1
    # if changes would have been made (ruff convention for CI gates).
    fx = subs.add_parser(
        "fix",
        help=(
            "[F] Fix — apply registered auto-fixes from the inspect rules "
            "catalogue (ddl_terminator, non_ascii, …)."
        ),
        description=(
            "Applies automated fixes to a SHIPS project's payload/ (and "
            "config/ for a small number of fixers). Every rule that has an "
            "auto-fix is registered in td_release_packager.fixers.FIX_REGISTRY; "
            "run without --rules / --all to apply the default-on subset, or "
            "restrict with --rules a,b,c. Pass --dry-run in CI to fail the "
            "build (exit code 1) if there is anything to fix."
        ),
    )
    fx.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory (the parent of payload/).",
    )
    _fx_selector = fx.add_mutually_exclusive_group()
    _fx_selector.add_argument(
        "--rules",
        default=None,
        help=(
            "Comma-separated list of rule ids to apply — e.g. "
            "`ddl_terminator,non_ascii`. Omit to apply the default-on "
            "subset. Mutually exclusive with --all."
        ),
    )
    _fx_selector.add_argument(
        "--all",
        dest="all_rules",
        action="store_true",
        help=(
            "Apply every registered fixer, including opt-in ones "
            "(non_ascii). Mutually exclusive with --rules."
        ),
    )
    fx.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what would change without writing. Exit code 1 when "
            "any file would have been rewritten — use in CI as a "
            "pre-merge gate."
        ),
    )
    fx.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a machine-readable summary to stdout instead of the "
            "human report. Same envelope shape as the MCP ships_fix tool."
        ),
    )

    # -- package --
    bp = subs.add_parser("package", help="[P] Package — build a release package.")
    bp.add_argument(
        "--project",
        required=False,
        default=None,
        help="SHIPS project directory.  Mutually exclusive with --source-github.",
    )
    _add_github_source_args(bp, mutually_exclusive_with="--project")
    bp.add_argument(
        "--env",
        required=True,
        help="Target environment (e.g. DEV, TST, SIT, UAT, PRD).",
    )
    bp.add_argument(
        "--name", required=True, help="Package name (e.g. 'create_objects')."
    )
    bp.add_argument(
        "--env-config", required=True, help="Path to environment .conf file."
    )
    bp.add_argument(
        "--root-parent",
        help=(
            "Root database/user parent for parentless CREATE DATABASE/USER "
            "prerequisites. Existing FROM clauses are preserved."
        ),
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
    bp.add_argument(
        "--allow-dirty",
        action="store_true",
        dest="allow_dirty",
        default=False,
        help="Build even if the working tree has uncommitted changes. "
        "Stamps source_dirty=true in ships.build.json so the Trust Report "
        "flags the package as READY_WITH_CAVEATS.",
    )
    bp.add_argument(
        "--signing-key",
        dest="signing_key",
        default=None,
        metavar="KEY_FILE",
        help=(
            "Path to a file containing the HMAC-SHA256 signing key. "
            "When supplied (or SHIPS_SIGNING_KEY env var is set), a "
            ".hmac sidecar is written alongside the archive (GAP-005)."
        ),
    )
    bp.add_argument(
        "--change-ref",
        dest="change_ref",
        default=None,
        metavar="TICKET_ID",
        help=(
            "Change management ticket reference (e.g. CHG0012345). "
            "Written to ships.build.json as change_ref. Required when the "
            "target environment has require_change_ref: true in ships.yaml."
        ),
    )
    bp.add_argument(
        "--asymmetric-key",
        dest="asymmetric_key",
        default=None,
        metavar="KEY_FILE",
        help=(
            "Path to an Ed25519 private key PEM file. When supplied "
            "(or SHIPS_PRIVATE_KEY_PATH is set), a .sig sidecar is written "
            "alongside the archive. Requires the cryptography package."
        ),
    )
    # #115: changeset-scoped packaging — build a minimal package of only
    # changed objects + their dependants instead of the whole payload.
    bp.add_argument(
        "--since-tag",
        dest="since_tag",
        default=None,
        metavar="TAG",
        help="Build a changeset package scoped to objects changed since this "
        "git tag/ref (plus their dependants). See `ships changeset` (#114).",
    )
    bp.add_argument(
        "--since-commit",
        dest="since_commit",
        default=None,
        metavar="COMMIT",
        help="Build a changeset package scoped to objects changed since this "
        "git commit (plus their dependants).",
    )
    bp.add_argument(
        "--objects",
        dest="objects",
        default=None,
        metavar="DB.Obj,DB.Obj",
        help="Build a changeset package scoped to this explicit comma-separated "
        "object list (plus their dependants). For agent-driven partial deploys.",
    )

    # -- deploy --
    dp = subs.add_parser(
        "deploy",
        help="[S] Ship — deploy a package zip, extracted package, or release group.",
        description=(
            "Deploy a SHIPS package without manually extracting archives or "
            "navigating into generated package directories. TARGET may be a "
            ".zip package, an extracted package directory, or a release-group "
            "directory containing release_group.json. Arguments after TARGET "
            "are forwarded unchanged to the generated deploy.py."
        ),
    )
    dp.add_argument(
        "--role",
        default="main",
        help=(
            "Release-group package role to run (default: main). "
            "Ignored when TARGET is a single package zip or extracted package."
        ),
    )
    dp.add_argument(
        "--work-dir",
        default=None,
        help=(
            "Directory used for automatic extraction. Defaults to a short "
            ".ships-work directory beside TARGET so logs and manifests persist."
        ),
    )
    dp.add_argument(
        "target",
        help=("Package .zip, extracted package directory, or release-group directory."),
    )
    dp.add_argument(
        "deploy_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to generated deploy.py, e.g. --host srv --user dbc.",
    )

    # -- demo --
    dm = subs.add_parser(
        "demo",
        help="[D] Demo — stage, inspect, analyse, package, and optionally deploy plain SQL demos.",
        description=(
            "Low-friction demo mode for SQL repositories that are not already "
            "SHIPS projects. It discovers common demo layouts such as "
            "workspace/src/<product>, stages SQL into a generated SHIPS project, "
            "auto-tokenises literal database names, runs relaxed inspection and "
            "dependency analysis, then builds a package unless --prepare-only is set."
        ),
    )
    demo_source = dm.add_mutually_exclusive_group(required=True)
    demo_source.add_argument(
        "--source",
        help="Local demo repository or SQL directory.",
    )
    demo_source.add_argument(
        "--source-github",
        metavar="OWNER/REPO",
        dest="source_github",
        default=None,
        help="Fetch demo source from a GitHub repository.",
    )
    dm.add_argument(
        "--source-ref",
        metavar="REF",
        dest="source_ref",
        default="main",
        help="Branch, tag, or commit SHA to fetch with --source-github (default: main).",
    )
    dm.add_argument(
        "--github-token",
        metavar="TOKEN",
        dest="github_token",
        default="",
        help="GitHub token for private repositories. Falls back to GITHUB_TOKEN.",
    )
    dm.add_argument(
        "--name",
        help="Demo package/project name. Defaults to the source directory name.",
    )
    dm.add_argument(
        "--work-dir",
        default=".ships-demo",
        help="Parent directory for generated demo projects (default: .ships-demo).",
    )
    dm.add_argument(
        "--output",
        "--output-dir",
        dest="output_dir",
        help=(
            "Directory for generated release packages. Defaults to "
            "<work-dir>/<name>/releases."
        ),
    )
    dm.add_argument(
        "--env",
        default="DEV",
        help="Environment name for generated config and package (default: DEV).",
    )
    dm.add_argument(
        "--env-prefix",
        help="Optional literal prefix to strip when deriving token names.",
    )
    dm.add_argument(
        "--root-parent",
        help=(
            "Root database/user parent for parentless CREATE DATABASE/USER "
            "demo prerequisites. Existing FROM clauses are preserved."
        ),
    )
    dm.add_argument(
        "--prepare-only",
        action="store_true",
        help="Stage, tokenise, inspect, and analyse only; skip package build.",
    )
    dm.add_argument("--author", help="Builder's name stamped into package metadata.")
    dm.add_argument(
        "--deploy",
        action="store_true",
        help="Deploy the generated package or release group after packaging.",
    )
    dm.add_argument(
        "deploy_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to generated deploy.py when --deploy is set.",
    )

    # -- repackage --
    rp = subs.add_parser(
        "repackage",
        help="Rebuild an edited extracted SHIPS package directory.",
    )
    rp.add_argument(
        "--package-dir",
        required=True,
        help="Extracted package directory to repackage, for example the edited _00_environment_prereqs directory.",
    )
    rp.add_argument(
        "--strict",
        action="store_true",
        help="Fail if the package remains blocked, for example because DBA placeholders remain.",
    )

    # -- scan --
    sp = subs.add_parser(
        "scan",
        help="Scan source for token references — validate, map, and audit tokens.",
    )
    sp.add_argument("--project", required=True, help="SHIPS project directory to scan.")
    sp.add_argument(
        "--env-config",
        help="Validate all tokens against this env .conf file.  "
        "Mutually exclusive with --all-envs.",
    )
    sp.add_argument(
        "--all-envs",
        action="store_true",
        dest="all_envs",
        default=False,
        help="Validate against every *.conf file found in config/env/ "
        "and report per-environment results in a single pass.  "
        "Mutually exclusive with --env-config.",
    )
    sp.add_argument(
        "--show-map",
        action="store_true",
        dest="show_map",
        default=False,
        help="Print the full token → file reverse index: for each token, "
        "list every payload file that references it.",
    )
    sp.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).  Use 'json' for agent or CI consumption.",
    )
    sp.add_argument(
        "--fail-on-orphan",
        action="store_true",
        dest="fail_on_orphan",
        default=False,
        help="Exit 1 when any defined token is never referenced in the payload "
        "(orphan token).  Useful as a CI gate to keep env configs clean.",
    )

    # -- analyze --
    az = subs.add_parser(
        "analyze",
        aliases=["analyse"],
        help="Analyse DDL dependencies, generate waves, and export dependency graph.",
    )
    az.add_argument(
        "--project", required=True, help="SHIPS project directory to analyse."
    )
    az.add_argument(
        "--output",
        help="Output path for _waves.txt (default: <source>/.ships/_waves.txt).",
    )
    az.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Deprecated no-op — analyse always rewrites _waves.txt now "
            "that the previous skip-if-exists behaviour caused stale "
            "wave data to outlive source edits (#449). Kept so existing "
            "scripts that pass --overwrite continue to parse."
        ),
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

    # -- changeset --
    cs = subs.add_parser(
        "changeset",
        help="Preview the changed-object set + dependants since a ref (#114).",
    )
    cs.add_argument(
        "--project", required=True, help="SHIPS project directory to inspect."
    )
    cs.add_argument(
        "--since-tag",
        help="Git tag/ref to diff HEAD against (git-native detection).",
    )
    cs.add_argument(
        "--since-commit",
        help="Git commit to diff HEAD against (git-native detection).",
    )
    cs.add_argument(
        "--update-baseline",
        action="store_true",
        help="Capture the current payload content hashes as the changeset "
        "baseline (for git-less detection) and exit.",
    )

    # -- plan --
    pl = subs.add_parser(
        "plan",
        help="Detect-and-recommend — inspect a source tree and emit an ordered "
        "command plan + plan.json (#379).",
    )
    pl.add_argument(
        "--source", required=True, help="Raw source DDL directory to inspect."
    )
    pl.add_argument(
        "--project",
        help="Target SHIPS project directory (default: the source directory).",
    )
    pl.add_argument("--env", help="Target environments, e.g. DEV,TST,PRD.")
    pl.add_argument("--name", help="Package name (default: create_objects).")
    pl.add_argument(
        "--mode",
        choices=["quick", "detailed"],
        default="quick",
        help="quick = one `ships process` per env; detailed = each step (default: quick).",
    )
    pl.add_argument(
        "--strict", action="store_true", help="Recommend --strict on process."
    )
    pl.add_argument(
        "--scaffolded",
        action="store_true",
        help="Project is already scaffolded — omit the scaffold step.",
    )
    pl.add_argument(
        "--no-generate",
        dest="no_generate",
        action="store_true",
        help="Skip the view-layer generate step.",
    )
    pl.add_argument(
        "--json", metavar="PATH", help="Write the machine-readable plan.json here."
    )

    # -- wizard --
    wz = subs.add_parser(
        "wizard",
        help="Interactive terminal wizard over the decision model — works over "
        "SSH (#381).",
    )
    wz.add_argument(
        "--source",
        help="Optional raw source DDL directory to pre-seed answers via detection.",
    )
    wz.add_argument(
        "--json", metavar="PATH", help="Write the machine-readable plan.json here."
    )

    # -- metadata (catalogue export) --
    md = subs.add_parser(
        "metadata",
        help="Export AI-native data-product metadata for an enterprise catalogue "
        "(Alation / Collibra) (#244).",
    )
    md_subs = md.add_subparsers(dest="metadata_subcommand")
    for _cat, _help in (
        ("export-alation", "Export an Alation-ready metadata bundle."),
        ("export-collibra", "Export a Collibra-ready metadata bundle."),
        ("export-datahub", "Export a DataHub MCP ingestion bundle."),
    ):
        _mp = md_subs.add_parser(_cat, help=_help)
        _mp.add_argument(
            "--package-dir",
            required=True,
            help="Root of an unpacked SHIPS package or release-group directory.",
        )
        _mp.add_argument(
            "--output", required=True, help="Output directory for the bundle."
        )
        _mp.add_argument(
            "--include-internal",
            dest="include_internal",
            action="store_true",
            help="Include internal implementation objects as interfaces too.",
        )
        _mp.add_argument(
            "--strict",
            action="store_true",
            help="Fail if any required product metadata is missing.",
        )

    # -- import-legacy --
    il = subs.add_parser(
        "import-legacy",
        help="Bootstrap a SHIPS project from legacy substitutions. "
        "Two input modes: --script (existing sed file) or "
        "--scan-source (auto-discover placeholders in a source tree).",
        description="Bootstrap a SHIPS project from legacy "
        "substitutions. Two mutually exclusive input modes: "
        "--script consumes an existing sed substitution script "
        "(s/$VAR/value/g rules); --scan-source walks a source DDL "
        "tree and auto-discovers $VAR / ${VAR} / &&VAR&& "
        "placeholders. Both modes emit a .conf file (token "
        "values) and a sed migration script (legacy markers → "
        "{{TOKEN}}). --scan-source additionally writes "
        "scan_report.md, an audit detail of every discovered token.",
    )
    il_mode = il.add_mutually_exclusive_group(required=True)
    il_mode.add_argument(
        "--script",
        metavar="SED_FILE",
        help="Path to a legacy sed substitution script. Use this "
        "when your project's pre-SHIPS build harness already has a "
        "sed file defining (marker, value) pairs.",
    )
    il_mode.add_argument(
        "--scan-source",
        metavar="SOURCE_DIR",
        help="Walk a source DDL directory and auto-discover non-SHIPS "
        "placeholders ($VAR, ${VAR}, &&VAR&&). Use this when the "
        "project has placeholders embedded in source but no sed "
        "file to point at -- the .conf values come out empty "
        "for you to fill in, and the migration sed converts every "
        "discovered marker to its {{TOKEN}} form. NOTE: expects a "
        "DIRECTORY (the root of your source DDL), not a single file.",
    )
    il.add_argument(
        "--project",
        help="Optional SHIPS project root. When supplied, the discovery "
        "resolver consults the project's ships.yaml for any extra "
        "extensions to scan. Only meaningful with --scan-source.",
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
        "<output-dir>/env/<env>.conf and "
        "<output-dir>/tokenise.conf. In --scan-source mode an "
        "additional <output-dir>/scan_report.md is also written.",
    )

    # -- migrate-source --
    ms = subs.add_parser(
        "migrate-source",
        help="Apply a SHIPS tokenisation config to a source DDL tree "
        "(Windows-safe; no sed binary required).",
        description="Apply a SHIPS tokenisation config to every "
        "SQL-bearing file in a source tree. The config accepts two "
        "rule forms: literal ``s/LHS/RHS/g`` (the form "
        "``import-legacy`` generates for ``$VAR`` / ``&&VAR&&`` "
        "substitution) and full regex ``regex::PATTERN:=REPLACEMENT`` "
        "with capture-group back-references (``$1..$9``). "
        "Run with ``--dry-run`` first to see what would change.",
    )
    ms.add_argument(
        "--tokenise-config",
        dest="tokenise_config",
        required=True,
        metavar="CONFIG_FILE",
        help="Path to the tokenisation config "
        "(canonical name: ``config/tokenise.conf``).",
    )
    ms.add_argument(
        "--source",
        required=True,
        metavar="SOURCE_DIR",
        help="Root of the source DDL tree to migrate. Files are updated in place.",
    )
    ms.add_argument(
        "--project",
        metavar="PROJECT_DIR",
        help="Optional SHIPS project root. Consulted by the discovery "
        "resolver for ships.yaml extension overrides.",
    )
    ms.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without writing any files.",
    )

    # -- decompose-names --
    dn = subs.add_parser(
        "decompose-names",
        help="Decompose literal database names against the SHIPS "
        "naming grammar and emit a cascade-form .conf file.",
        description="Read a list of literal Teradata database names "
        "(from a token_map.conf or a plain names file) and decompose "
        "them against the SHIPS grammar "
        "{ENV_PREFIX}_{SHIPS_ENV}_{INSTANCE}_{LAYER}_{SECURITY_TIER}_{KIND}. "
        "Emits a sectioned .conf file with composition roots "
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
        "<output-dir>/env/<env>.conf and "
        "<output-dir>/decomposition_report.md.",
    )

    # -- bootstrap-env-config --
    bp = subs.add_parser(
        "bootstrap-env-config",
        help="Generate a .conf scaffold for an already-tokenised "
        "project. Use when the source already references "
        "{{TOKEN}} but no .conf file exists yet.",
        description="Scan an already-tokenised SHIPS project for "
        "{{TOKEN}} references and emit a 7-section .conf "
        "scaffold with every referenced token parked in section 8 "
        "for the user to re-section. Closes the third bootstrap "
        "path: when there's nothing to convert (no literals, no "
        "legacy script) you just need a starting .conf skeleton.",
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
        help="Output directory; .conf written under "
        "<output-dir>/env/<env>.conf. Defaults to "
        "<source>/config.",
    )
    bp.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .conf file at the target. "
        "Without this, the tool refuses to clobber.",
    )

    # -- process --
    pr = subs.add_parser(
        "process",
        help="[S-H-I-P-S] Run the full pipeline: harvest → generate → "
        "inspect → analyse → [package].",
        description="Orchestrate the complete SHIPS pipeline in a single "
        "command, recording all stage decisions into one run entry in "
        "ships.decisions.json.\n\n"
        "Developer mode (default): continues past warnings; hard errors "
        "are reported but do not abort.\n"
        "Platform mode (--strict): any stage error immediately aborts "
        "the pipeline.",
    )
    pr.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory (must already be scaffolded).",
    )
    pr.add_argument(
        "--source",
        default=None,
        help="Raw DDL source directory.  If omitted, the harvest stage "
        "is skipped and the existing payload is used.  "
        "Mutually exclusive with --source-github.",
    )
    _add_github_source_args(pr)
    pr.add_argument(
        "--token-map",
        default=None,
        help="[DEPRECATED — prefer config/tokenise.conf] "
        "Path to token_map.conf for harvest token substitution. "
        "Still works; prefer config/tokenise.conf for new projects.",
    )
    pr.add_argument(
        "--auto-tokenise",
        action="store_true",
        dest="auto_tokenise",
        default=False,
        help="Auto-detect and apply token substitutions in one pass "
        "(harvest stage). Equivalent to --auto-tokenise on harvest.",
    )
    pr.add_argument(
        "--env-prefix",
        default=None,
        help="Env prefix for auto-tokenise token derivation.",
    )
    pr.add_argument(
        "--prefix-token",
        action="append",
        default=None,
        metavar="SOURCE=TOKEN",
        help="Identifier-aware prefix tokenisation (Model B). "
        "Same surface as on `harvest` — see its --help for details. "
        "Repeatable; longest prefix wins.",
    )
    pr.add_argument(
        "--remove-view-type-affixes",
        action="store_true",
        dest="remove_view_type_affixes",
        default=False,
        help="Harvest stage: remove redundant view object type affixes "
        "(leading v_ and trailing _v) and update qualified references.",
    )
    pr.add_argument(
        "--skip-generate",
        action="store_true",
        dest="skip_generate",
        default=False,
        help="Skip the generate stage (for projects that do not use "
        "the SHIPS view-layer generator).",
    )
    pr.add_argument(
        "--inspect-config",
        default=None,
        help="Path to inspect.conf (passed to the inspect stage).",
    )
    pr.add_argument(
        "--env",
        default=None,
        help="Target environment (e.g. DEV). Enables the package stage. "
        "If omitted, defaults to packaging.default_env or the first entry "
        "in ships.yaml environments (#384).",
    )
    pr.add_argument(
        "--env-config",
        default=None,
        help="Path to the .conf file for token resolution. If omitted, "
        "defaults to packaging.env_config or config/env/<ENV>.conf when "
        "that file exists (#384).",
    )
    pr.add_argument(
        "--root-parent",
        default=None,
        help=(
            "Root database/user parent for parentless CREATE DATABASE/USER "
            "prerequisites. Applied after harvest/generate and before inspect."
        ),
    )
    pr.add_argument(
        "--name",
        default=None,
        help="Package name. If omitted, defaults to packaging.name or the "
        "ships.yaml project name (#384).",
    )
    pr.add_argument(
        "--output",
        default=None,
        help="Output directory for the built package archive.",
    )
    pr.add_argument(
        "--format",
        default="zip",
        choices=["zip", "tar.gz"],
        help="Archive format for the package (default: zip).",
    )
    pr.add_argument(
        "--author",
        default="",
        help="Author metadata stamped into the package manifest.",
    )
    pr.add_argument(
        "--description",
        default="",
        help="Description metadata stamped into the package manifest.",
    )
    pr.add_argument(
        "--commit",
        default="",
        help="Source commit hash stamped into the package manifest.",
    )
    pr.add_argument(
        "--strict",
        action="store_true",
        help="Platform mode: abort the pipeline on the first stage that "
        "finishes with errors. Without --strict, all stages run and "
        "errors are summarised at the end.",
    )
    pr.add_argument(
        "--pause",
        action="store_true",
        help="Pause after each stage and prompt before continuing. "
        "Useful for supervised runs where you want to inspect output "
        "before proceeding. Suppressed automatically in CI environments "
        "(CI, SHIPS_CI, NO_PROMPT env vars) or when stdout is not a TTY.",
    )

    # -- explain --
    ex = subs.add_parser(
        "explain",
        help="[E] Explain — human-readable report of a prior pipeline run.",
        description="Read ships.decisions.json and render a concise report of the "
        "most recent (or specified) run: stage statuses, key outputs, "
        "and full issues table. Use before promoting to the next environment.",
    )
    ex.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing ships.decisions.json.",
    )
    ex.add_argument(
        "--run-id",
        default=None,
        dest="run_id",
        help="Report a specific run by ID. Defaults to the last run.",
    )
    ex.add_argument(
        "--command",
        default=None,
        dest="command_filter",
        help="Filter by command name (e.g. 'process', 'harvest'). "
        "Selects the last run of that type.",
    )

    # -- verify --
    vr = subs.add_parser(
        "verify",
        help="[V] Verify — pre-deploy package readiness check.",
        description="Read ships.decisions.json, locate the most recent package stage, "
        "and confirm the archive exists on disk and the build was clean. "
        "Exit code 0 = READY, 1 = NOT READY.",
    )
    vr.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing ships.decisions.json.",
    )
    vr.add_argument(
        "--run-id",
        default=None,
        dest="run_id",
        help="Locate the package stage in a specific run.",
    )

    # -- onboard --
    ob = subs.add_parser(
        "onboard",
        help="Scan a legacy source directory and recommend the SHIPS "
        "onboarding path (import-legacy / bootstrap / harvest).",
        description="Scans a source DDL directory for legacy placeholder "
        "markers ($VAR, ${VAR}, &&VAR&&), SHIPS {{TOKEN}} forms, and "
        "env config files, then recommends the correct onboarding sequence "
        "with ready-to-run commands. Pass --auto to execute the first "
        "automatable step immediately.",
    )
    ob.add_argument(
        "--source",
        required=True,
        metavar="SOURCE_DIR",
        help="Raw DDL source directory to scan.",
    )
    ob.add_argument(
        "--env",
        default="DEV",
        help="Target environment name used in generated commands (default: DEV).",
    )
    ob.add_argument(
        "--output-dir",
        default="./config",
        dest="output_dir",
        help="Output directory for --auto mode (default: ./config).",
    )
    ob.add_argument(
        "--auto",
        action="store_true",
        help="Run the first automatable step of the recommended sequence.",
    )

    # -- decisions --
    # -- rollback --
    rb = subs.add_parser(
        "rollback",
        help="[R] Rollback — build a rollback package from a git tag.",
        description=(
            "Build a release package from a previous git tag and print the "
            "deploy command to restore that version.  The rollback package is "
            "a normal SHIPS package — integrity-checked, trust-scored, and "
            "deployable via deploy.py.  Recommended: deploy with "
            "--on-drift continue to overwrite any out-of-band changes made "
            "after the broken deploy."
        ),
    )
    rb.add_argument(
        "--to-tag",
        required=True,
        metavar="TAG",
        dest="to_tag",
        help="Git tag to roll back to (e.g. v1.2.3).  Must exist locally; "
        "run 'git fetch --tags' first if the tag is only on the remote.",
    )
    rb.add_argument(
        "--env",
        required=True,
        help="Target environment (e.g. PRD).  Must match SHIPS_ENV in --env-config.",
    )
    rb.add_argument(
        "--env-config",
        required=True,
        help="Path to the environment .conf file.  Use the CURRENT file — "
        "token values come from today's environment, not the tag.",
    )
    rb.add_argument(
        "--name",
        default=None,
        help="Package name (default: project directory name).",
    )
    rb.add_argument(
        "--project",
        default=".",
        help="Project directory containing .build_counter and git repo "
        "(default: current directory).",
    )
    rb.add_argument(
        "--output",
        default=None,
        help="Output directory for the rollback package "
        "(default: <project>/releases/).",
    )
    rb.add_argument(
        "--format",
        choices=["zip", "tar.gz"],
        default="zip",
        help="Archive format (default: zip).",
    )
    rb.add_argument(
        "--on-drift",
        choices=["abort", "skip", "continue"],
        default="continue",
        dest="on_drift",
        help="Action when schema drift is detected during the subsequent deploy "
        "(default: continue — rollback overwrites out-of-band changes).",
    )
    rb.add_argument("--author", help="Author metadata for ships.build.json.")
    rb.add_argument(
        "--description",
        help="Release description (default: 'Rollback to <tag>').",
    )

    dc = subs.add_parser(
        "decisions",
        help="Manage the ships.decisions.json audit trail.",
        description="Sub-commands for inspecting and maintaining ships.decisions.json.",
    )
    dc_subs = dc.add_subparsers(dest="decisions_subcommand")

    dp = dc_subs.add_parser(
        "prune",
        help="Remove old run entries from ships.decisions.json.",
        description="Prune stale run entries, keeping the most recent N runs or "
        "runs from the last N days. Always shows a preview before writing. "
        "Use --yes to skip the confirmation prompt (for CI/scripts).",
    )
    dp.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing ships.decisions.json.",
    )
    dp_mode = dp.add_mutually_exclusive_group(required=True)
    dp_mode.add_argument(
        "--keep-runs",
        type=int,
        metavar="N",
        dest="keep_runs",
        help="Retain the N most recent runs; prune everything older.",
    )
    dp_mode.add_argument(
        "--keep-days",
        type=int,
        metavar="N",
        dest="keep_days",
        help="Retain runs started within the last N days; prune older ones.",
    )
    dp.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and prune immediately.",
    )
    dp.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be pruned without writing any changes.",
    )

    # -- keygen --
    kg = subs.add_parser(
        "keygen",
        help="Generate an Ed25519 keypair for asymmetric package signing.",
    )
    kg.add_argument(
        "--output-dir",
        dest="output_dir",
        default=".",
        metavar="DIR",
        help=(
            "Directory to write ships_signing_private.pem and "
            "ships_signing_public.pem (default: current directory)."
        ),
    )

    # -- clean --
    # Explicit "reset prior pipeline output" subcommand. Synchronous,
    # dry-run by default — pass --apply to actually delete. Backed by
    # td_release_packager.cleaner.clean_project so it shares one code
    # path with the ships_clean MCP tool.
    cln = subs.add_parser(
        "clean",
        help="[C] Clean — wipe prior pipeline output by subtree (rmtree).",
    )
    cln.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory (must contain ships.yaml).",
    )
    cln.add_argument(
        "--scope",
        default="payload",
        choices=["runs", "payload", "releases", "reports", "decisions", "all"],
        help=(
            "Subtree to remove. Default 'payload' clears "
            "payload/database/ for a clean re-harvest surface. "
            "'all' resets to scaffolded state (leaves .build_counter "
            "intact)."
        ),
    )
    cln.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually delete. Without this flag, runs as a dry-run "
            "and reports what would be removed."
        ),
    )

    # -- stage -- (#487) — bounded git-staging gate for SHIPS-owned
    # paths. Runs scan + inspect; refuses on failure; on pass, stages
    # exactly ``ships.yaml`` + ``config/`` + ``payload/`` and stops.
    # Deliberately does not commit, sign, or touch hooks (see issue
    # comment thread — keep the surface tight).
    st = subs.add_parser(
        "stage",
        help=(
            "Stage SHIPS-owned paths (ships.yaml, config/, payload/) "
            "after gating on scan + inspect. Does not commit."
        ),
    )
    st.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory to stage.",
    )
    st.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Run the scan + inspect gates and print the paths that "
            "would be staged without touching the git index."
        ),
    )
    st.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Promote inspect WARNING rules to ERROR while gating, so "
            "warnings also block staging."
        ),
    )

    # -- fix-package-integrity -- (#465, root cause of #450 history)
    # Recovery utility: regenerate ``.sha256`` sidecars under
    # ``<project>/releases/`` so historical mismatches from pre-#463
    # builds stop showing up in ``ships inspect`` output.
    fpi = subs.add_parser(
        "fix-package-integrity",
        help=(
            "[*] Recovery — regenerate .sha256 sidecars across "
            "releases/ so historical mismatches stop firing in inspect."
        ),
    )
    fpi.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing releases/.",
    )
    fpi.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview the work — report which sidecars would be "
            "refreshed without writing anything."
        ),
    )

    # -- notebook --
    nb = subs.add_parser(
        "notebook",
        help=(
            "[N] Notebook — render a Clearscape Experience deployment "
            "notebook (.ipynb) from a SHIPS project."
        ),
        description=(
            "Produces a self-contained Jupyter notebook that deploys "
            "every object in the project via inline DDL, with one code "
            "cell per analysed wave. Intended for Teradata Clearscape "
            "Experience demo sandboxes — non-production."
        ),
    )
    nb.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing payload/ and ships.yaml.",
    )
    nb.add_argument(
        "--env-config",
        required=True,
        help="Env config file (e.g. config/env/DEV.conf) used to resolve tokens.",
    )
    nb.add_argument(
        "--output",
        help=(
            "Output path for the .ipynb. Default: "
            "<project>/output/<name>.clearscape.ipynb."
        ),
    )
    nb.add_argument(
        "--name",
        help="Package display name. Default: the project directory's basename.",
    )
    nb.add_argument(
        "--env-name",
        default="DEV",
        help="Logical environment label stamped into the intro cell (default: DEV).",
    )

    for name, subparser in subs.choices.items():
        add_version_argument(subparser, prog=f"td_release_packager {name}")

    return parser


if __name__ == "__main__":
    main()
