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
                      emit a .conf file plus a migration sed.
    migrate-source    Apply a legacy_migration.sed to a source tree
                      (Windows-safe; no sed binary required).
    decompose-names   Infer composition roots from literal database
                      names and emit a cascade-form .conf file.

Usage:
    python -m td_release_packager scaffold --name MortgagePlatform --output /projects
    python -m td_release_packager build --source . --env DEV --name create_objects --env-config config/env/DEV.conf
    python -m td_release_packager inspect --source . --fix-grants
    python -m td_release_packager scan --source .
    python -m td_release_packager analyze --source . --graph ./output/
    python -m td_release_packager analyze --source . --graph . --formats dot,json,openlineage
    python -m td_release_packager import-legacy --script legacy.sh --env DEV --output-dir ./config
    python -m td_release_packager import-legacy --scan-source ./src --env DEV --output-dir ./config
    python -m td_release_packager migrate-source --sed config/legacy_migration.sed --source ./src
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
    read_env_config,
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
    elif args.command == "explain":
        _cmd_explain(args)
    elif args.command == "verify":
        _cmd_verify(args)
    elif args.command == "onboard":
        _cmd_onboard(args)
    elif args.command == "decisions":
        _cmd_decisions(args)
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

    from td_release_packager.orchestrator import (
        DECISIONS_FILENAME,
        DecisionsManifest,
    )
    from td_release_packager.otel import ships_span

    @contextmanager
    def _ctx():
        with ships_span(
            f"ships.{stage_name}",
            {"ships.project_dir": project_dir, "ships.stage": stage_name},
        ) as otel_span:
            if not _looks_like_ships_project(project_dir):
                yield _NullStageRecorder()
                return

            manifest_path = os.path.join(project_dir, DECISIONS_FILENAME)
            manifest = DecisionsManifest(manifest_path)
            with manifest.run(stage_name) as run:
                with run.stage(stage_name) as stage:
                    yield stage

            # Stage complete — propagate status and key outputs to OTel.
            # Accessing stage._entry is safe: the inner with block has
            # exited and RunRecorder.__exit__ has already finalised it.
            _propagate_stage_to_otel_span(stage, otel_span)

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
    to branch on whether decisions.json is being written.
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

    Opens a single run in ``decisions.json`` and yields the
    ``RunRecorder`` so the caller can open individual stages within
    it.  One run with multiple stages gives a clean end-to-end audit
    trail across the whole pipeline — distinct from the per-stage
    single-run pattern used by individual commands.

    Yields a ``_NullRunRecorder`` when ``project_dir`` is not a SHIPS
    project, so ad-hoc runs don't litter the filesystem.
    """
    from contextlib import contextmanager

    from td_release_packager.orchestrator import (
        DECISIONS_FILENAME,
        DecisionsManifest,
    )

    from td_release_packager.otel import ships_span

    @contextmanager
    def _ctx():
        with ships_span(
            "ships.process",
            {"ships.project_dir": project_dir, "ships.stage": "process"},
        ):
            if not _looks_like_ships_project(project_dir):
                yield _NullRunRecorder()
                return

            manifest_path = os.path.join(project_dir, DECISIONS_FILENAME)
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
    print()

    steps: List[str] = []

    # Quality-gate block — appears in every flow before packaging.
    # 'inspect' is part of the canonical S-H-I-P-S workflow;
    # 'analyze' produces dependency waves for parallel deploy
    # (optional but recommended); 'scan' catches {{TOKEN}}
    # references that have no value in the .conf file.
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
            f"     python -m td_release_packager package \\\n"
            f"         --source {project} --env DEV --name <name> \\\n"
            f"         --env-config config/env/DEV.conf \\\n"
            f"         --output releases/"
        )

    if already_tokenised:
        # Flow D — source already uses {{TOKEN}} references. Skip
        # the token map entirely and bootstrap properties directly
        # from the tokens the source already references.
        bootstrap_cmd_parts = [
            f"     python -m td_release_packager bootstrap-env-config \\\n"
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
                f"     python -m td_release_packager decompose-names \\\n"
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
            f"     python -m td_release_packager harvest \\\n"
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
            findings = find_legacy_placeholders(content)
            if findings:
                legacy_files.add(path)
                legacy_count += sum(len(f.occurrences) for f in findings)
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

    print(f"\n  SHIPS Onboarding Wizard")
    print(f"  {'=' * 56}")
    print(f"  Scanning: {source}")

    scan = _onboard_scan(source)
    state = _onboard_classify(scan, source)

    print(f"\n  Source summary")
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
    module = "python -m td_release_packager"

    if state == "LEGACY":
        print("  Detected: legacy placeholder markers ($VAR / &&VAR&&)")
        print("  Recommended path: import-legacy → migrate-source → harvest\n")
        print("  Step 1 — discover all legacy markers and generate the migration sed:")
        print(f"    {module} import-legacy \\")
        print(f"      --scan-source {source} \\")
        print(f"      --env {env} \\")
        print(f"      --output-dir ./config\n")
        print("  Step 2 — fill in token values in config/env/DEV.conf, then apply:")
        print(f"    {module} migrate-source \\")
        print(f"      --sed config/legacy_migration.sed \\")
        print(f"      --source {source}\n")
        print("  Step 3 — harvest the migrated source into a SHIPS project:")
        print(f"    {module} harvest --source {source} --project <project_dir>")

    elif state == "TOKENS_NO_CONFIG":
        print("  Detected: SHIPS {{TOKEN}} markers, no env config yet")
        print("  Recommended path: bootstrap-env-config → fill values → harvest\n")
        print("  Step 1 — generate a config scaffold from existing tokens:")
        print(f"    {module} bootstrap-env-config \\")
        print(f"      --source <project_dir> \\")
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
        print("  Run manually: python -m td_release_packager bootstrap-env-config ...")
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
    (``.conf`` + ``legacy_migration.sed``) -- the latter mode
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
    """Apply a legacy_migration.sed to a source tree (Windows-safe)."""
    from td_release_packager.source_migrator import main as migrator_main

    argv = ["--sed", args.sed, "--source", args.source]
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

    # Run scaffold first — the project directory must exist before
    # _stage_recording can detect it as a SHIPS project and open
    # decisions.json. Errors are fatal so they exit before recording starts.
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
            print("                    --env-config config/env/DEV.conf")
            print("    [S] Ship      python deploy.py --host <host> --user <user>")

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
            exit_code = _run_ingest(args, stage, _ic, apply_tokens)
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

    result = ingest_directory(
        source_dir=args.source,
        project_dir=args.project,
        detect_tokens=True,
        apply_tokens=apply_tokens,
        force=args.force,
        clean_payload=not args.keep_existing,
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
        if not generate_map:
            print("\n  Tip: re-run with --generate-token-map --env-prefix <PREFIX>")
            print("  to auto-generate a token mapping file.")

    if result.warnings:
        print("\n  Warnings:")
        for w in result.warnings:
            print(f"    ⚠ {w}")

    if result.classification_warnings:
        print("\n  Classification warnings:")
        for w in result.classification_warnings:
            print(f"    ⚠ {w}")

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
    layout grow a ``decisions.json`` entry per inspect run while ad-
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
        with _stage_recording(args.source, "inspect") as stage:
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
        stage.set_config_resolved("source", args.source, "layer-5", "cli")
        stage.set_config_resolved(
            "config", getattr(args, "config", None), "layer-5", "cli"
        )
        stage.set_config_resolved(
            "strict", getattr(args, "strict", False), "layer-5", "cli"
        )
        stage.set_config_resolved(
            "skip_grants", getattr(args, "skip_grants", False), "layer-5", "cli"
        )
        stage.set_config_resolved(
            "fix_grants", getattr(args, "fix_grants", False), "layer-5", "cli"
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

            # Record one decisions.json issue per lint finding. The
            # rule name is carried in the message so explain can
            # group by rule even though the issue code is coarse.
            for issue in lint_result.issues:
                # Map validate.py severities into the recorder's
                # vocabulary: ERROR/WARNING/INFO → error/warning/info.
                rec_severity = issue.severity.lower()
                if rec_severity not in ("error", "warning", "info"):
                    rec_severity = "warning"
                location = issue.file
                if issue.line is not None:
                    location = f"{issue.file}:{issue.line}"
                stage.add_issue(
                    rec_severity,
                    issue_codes.INSPECT_LINT_VIOLATION,
                    f"[{issue.rule}] {issue.message}",
                    location=location,
                )

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

        # ==============================================================
        # decisions.json — record grants, inputs, outputs, status
        # ==============================================================
        # Done after the human-facing report so an interruption mid-
        # report still leaves the printed output intact. The recorder
        # itself was set up at the top of this function — we only
        # need to attach the per-step findings now.
        if grant_result is not None and not grant_ok:
            for entry in getattr(grant_result, "drifted", []):
                stage.add_issue(
                    "error",
                    issue_codes.INSPECT_GRANT_VIOLATION,
                    f"Drifted grant: {entry}",
                )
            for entry in getattr(grant_result, "missing", []):
                stage.add_issue(
                    "error",
                    issue_codes.INSPECT_GRANT_VIOLATION,
                    f"Missing grant (intent has it, .grt does not): {entry}",
                )
            for entry in getattr(grant_result, "orphaned", []):
                stage.add_issue(
                    "error",
                    issue_codes.INSPECT_GRANT_VIOLATION,
                    f"Orphaned grant (.grt has it, intent does not): {entry}",
                )

        stage.set_inputs(
            source_dir=args.source,
            payload_dir=payload_dir,
            files_scanned=lint_result.files_scanned,
            grant_validation_skipped=skip_grants,
            grant_validation_fix_mode=do_fix,
        )
        stage.set_outputs(
            token_format_passed=token_ok,
            lint_passed=lint_ok,
            grants_passed=grant_ok,
            overall_passed=overall_ok,
            lint_errors=lint_result.errors,
            lint_warnings=lint_result.warnings,
            files_with_issues=lint_result.files_with_issues,
        )

        # The recorder auto-rolls up "error" issues into the run's
        # final_status. Warnings need an explicit set_status to
        # surface in decisions.json — otherwise a clean run that
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


def _cmd_build(args):
    """Build a release package."""
    # -- Resolve properties file path --
    env_config_path = _resolve_path(
        args.env_config,
        relative_to=args.source,
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
                f"  ⚠ No SHIPS_ENV declared in {os.path.basename(args.env_config)} "
                f"— environment cross-check skipped.",
            )

    from td_release_packager.orchestrator import issue_codes as _ic

    try:
        with _stage_recording(args.source, "package") as stage:
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
    stage.set_config_resolved("source", args.source, "layer-5", "cli")
    stage.set_config_resolved("env", args.env.upper(), "layer-5", "cli")
    stage.set_config_resolved("name", args.name, "layer-5", "cli")
    stage.set_config_resolved("env_config", args.env_config, "layer-5", "cli")
    stage.set_config_resolved("output", getattr(args, "output", None), "layer-5", "cli")
    stage.set_config_resolved(
        "format", getattr(args, "format", "zip"), "layer-5", "cli"
    )
    stage.set_inputs(source_dir=args.source)

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
        env_config_file=args.env_config,
        build_number=build_number,
        output_dir=args.output,
        archive_format=args.format,
        author=args.author or "",
        description=args.description or "",
        source_commit=args.commit or "",
        allow_dirty=getattr(args, "allow_dirty", False),
    )

    (main_pair, companion_pair) = build_package(config)
    archive_path, manifest = main_pair

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
    )
    for w in manifest.warnings:
        stage.add_issue("warning", issue_codes.PACKAGE_WARNING, w)

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
            print(f"    ⚠ {w}")

    print()
    return 0


def _cmd_process(args):
    """
    [S-H-I-P-S] Run the full pipeline in sequence.

    Item 5 of the orchestrator build order. Runs:
      harvest → generate → inspect → analyse → [package]

    All stages write into a single ``process`` run in ``decisions.json``
    so the audit trail is one coherent record rather than five separate
    run entries.

    Developer mode (default): continues past warnings; only hard errors
    abort the run.
    Platform mode (``--strict``): any stage that finishes with
    ``status=error`` aborts the pipeline immediately.
    """
    from td_release_packager.orchestrator import issue_codes as _ic

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

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

    with _process_recording(project_dir) as run:
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

        # ---- [I] Inspect ----------------------------------------
        print("  [I] Inspect …")
        inspect_args = _build_process_namespace(
            args,
            source=project_dir,
            strict=strict,
            config=getattr(args, "inspect_config", None),
            skip_grants=True,
            fix_grants=False,
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
                output=getattr(args, "output", None),
                format=getattr(args, "format", "zip"),
                author=getattr(args, "author", ""),
                description=getattr(args, "description", ""),
                commit=getattr(args, "commit", ""),
                build_number=None,
                no_increment=False,
            )
            try:
                with run.stage("package") as stage:
                    _run_build(pkg_args, stage, _ic)
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
                "  [P] Package … skipped (provide --env --env-config --name to enable)"
            )

    # -- Summary banner -------------------------------------------
    print(f"\n{'=' * 64}")
    if failed_stages:
        print(f"  Process completed with errors in: {', '.join(failed_stages)}")
        print("  Review decisions.json for full detail.")
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
# explain — human-readable read-only view of decisions.json
# ---------------------------------------------------------------

#: Status badge colouring for terminal output.
_STATUS_ICONS = {
    "success": "✓",
    "warning": "⚠",
    "error": "✗",
    "skipped": "○",
    "no-op": "–",
}


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
    """Prune old run entries from decisions.json."""
    from td_release_packager.orchestrator.decisions import prune_decisions

    project = args.project
    decisions_path = os.path.join(project, "decisions.json")

    if not os.path.isfile(decisions_path):
        print(f"ERROR: decisions.json not found in {project}", file=sys.stderr)
        sys.exit(1)

    keep_runs = getattr(args, "keep_runs", None)
    keep_days = getattr(args, "keep_days", None)
    yes = getattr(args, "yes", False)
    dry_run = getattr(args, "dry_run", False)

    # Dry-run preview
    preview = prune_decisions(
        decisions_path, keep_runs=keep_runs, keep_days=keep_days, dry_run=True
    )

    print(f"\n  decisions.json prune preview")
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

    Reads decisions.json without modifying it.  Finds the most recent
    run (or the run specified by ``--run-id``) and prints a concise
    report showing: run metadata, per-stage status + key outputs, and
    a full issues table.  Designed as a pre-promotion checklist — the
    DBA reads this before promoting from DEV to TST.
    """
    from td_release_packager.orchestrator import DECISIONS_FILENAME, DecisionsManifest

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    manifest_path = os.path.join(project_dir, DECISIONS_FILENAME)
    if not os.path.exists(manifest_path):
        print(
            f"ERROR: No decisions.json found in {project_dir}.\n"
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
    """Render one run from decisions.json as a human-readable report."""
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
# verify — pre-deploy sanity check against decisions.json
# ---------------------------------------------------------------


def _cmd_verify(args):
    """
    Item 6b — verify: pre-deploy sanity check from decisions.json.

    Reads decisions.json and finds the most recent package stage.
    Checks: the archive file still exists on disk, no PACKAGE_WARNING
    issues were recorded, and the build looks complete.  Intended as
    the final gate before an operator runs ``deploy``.
    """
    from td_release_packager.orchestrator import DECISIONS_FILENAME, DecisionsManifest

    project_dir = args.project
    if not os.path.isdir(project_dir):
        print(f"ERROR: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    manifest_path = os.path.join(project_dir, DECISIONS_FILENAME)
    if not os.path.exists(manifest_path):
        print(
            f"ERROR: No decisions.json found in {project_dir}.",
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
            "  No package stage found in decisions.json.\n"
            "  Run the pipeline with packaging enabled:\n"
            "    ships process ... --env DEV --env-config ... --name ...",
            file=sys.stderr,
        )
        sys.exit(1)

    out = pkg_stage.get("outputs", {})
    issues = pkg_stage.get("issues", [])
    archive_path = out.get("archive_path", "")
    archive_exists = bool(archive_path) and os.path.exists(archive_path)

    warnings = [i for i in issues if i.get("severity") in ("warning", "error")]

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

    print(f"\n  Checklist:")
    checks = []

    # 1. Archive exists
    if archive_exists:
        print(f"    ✓ Archive exists on disk")
        checks.append(True)
    else:
        print(f"    ✗ Archive NOT found: {archive_path}")
        checks.append(False)

    # 2. No errors / warnings in package stage
    if not warnings:
        print(f"    ✓ No package issues recorded")
        checks.append(True)
    else:
        for i in warnings:
            sev_icon = "✗" if i.get("severity") == "error" else "⚠"
            print(f"    {sev_icon} {i.get('code', '?')}: {i.get('message', '')}")
        checks.append(False)

    # 3. Package stage status
    pkg_status = pkg_stage.get("status", "unknown")
    if pkg_status == "success":
        print(f"    ✓ Package stage status: success")
        checks.append(True)
    else:
        print(f"    ✗ Package stage status: {pkg_status}")
        checks.append(False)

    # 4. Companion (prereqs) awareness
    if out.get("has_companion"):
        companion = out.get("companion_archive_path", "")
        companion_exists = bool(companion) and os.path.exists(companion)
        if companion_exists:
            print(f"    ✓ Companion (prereqs) archive exists")
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
    ``decisions.json``.
    """
    from td_release_packager.orchestrator import issue_codes as _ic

    source_dir = args.source
    if not os.path.isdir(source_dir):
        print(f"ERROR: Source directory not found: {source_dir}", file=sys.stderr)
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
    from pathlib import Path

    from td_release_packager.view_layer_generator import run as generate_views

    source_dir = args.source
    dry_run = getattr(args, "dry_run", False)
    modules_arg = getattr(args, "modules", None)
    requested_modules = (
        {m.strip().upper() for m in modules_arg.split(",") if m.strip()}
        if modules_arg
        else None
    )

    stage.set_config_resolved("source", source_dir, "layer-5", "cli")
    stage.set_config_resolved("dry_run", dry_run, "layer-5", "cli")
    stage.set_config_resolved("modules", modules_arg or "(all)", "layer-5", "cli")
    stage.set_inputs(source_dir=source_dir)

    result = generate_views(
        project_root=Path(source_dir),
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
    )

    for w in result.warnings:
        stage.add_issue("warning", issue_codes.GENERATE_WARNING, w)
    for e in result.errors:
        stage.add_issue("error", issue_codes.GENERATE_ERROR, e)

    print(f"\n{'=' * 64}")
    print("  View Layer Generation")
    print(f"{'=' * 64}")
    print(f"  Source:           {source_dir}")
    if dry_run:
        print("  Mode:             DRY RUN — no files written")
    if requested_modules:
        print(f"  Modules:          {', '.join(sorted(requested_modules))}")

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

    if result.warnings:
        print("\n  Warnings:")
        for w in result.warnings:
            print(f"    ⚠ {w}")

    if result.errors:
        print("\n  Errors:")
        for e in result.errors:
            print(f"    ✗ {e}")

    print(f"{'=' * 64}\n")

    return 1 if result.errors else 0


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
            "env_config",
            args.env_config or None,
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
        if args.env_config:
            try:
                values = read_env_config(args.env_config)
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
                    print(f"\n  ✓ All tokens validated against {args.env_config}")

                # Stage status: error issues auto-upgrade to "error"
                # via the recorder; warnings need an explicit set.
                if warnings and not errors:
                    stage.set_status("warning")

            except FileNotFoundError:
                print(f"\n  ⚠ Config file not found: {args.env_config}")
                stage.add_issue(
                    "error",
                    issue_codes.PROPERTIES_NOT_FOUND,
                    f"Config file not found: {args.env_config}",
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
    source_dir = args.source
    if not os.path.isdir(source_dir):
        print(f"ERROR: Source directory not found: {source_dir}", file=sys.stderr)
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

    source_dir = args.source

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
        waves_path = os.path.join(source_dir, "_waves.txt")

    if result.waves:
        if os.path.exists(waves_path) and not args.overwrite:
            print(f"\n  ⚠ {waves_path} already exists. Use --overwrite to replace.")
        else:
            with open(waves_path, "w", encoding="utf-8") as f:
                f.write(result.waves_file_content)
            stage.set_outputs(waves_path=waves_path)
            print(f"\n  ✓ Wave file written: {waves_path}")
            print(f"    {len(result.waves)} waves, {len(result.objects)} objects")

    if result.cycles:
        print(f"\n  ⚠ {len(result.cycles)} cycle(s) detected — review before deploying")

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

    # -- generate --
    gn = subs.add_parser(
        "generate",
        help="[G] Generate — build view-layer DDL from harvested tables.",
    )
    gn.add_argument(
        "--source",
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
        "--env-config", required=True, help="Path to environment .conf file."
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
        "Stamps source_dirty=true in BUILD.json so the Trust Report "
        "flags the package as READY-WITH-CAVEATS.",
    )

    # -- scan --
    sp = subs.add_parser(
        "scan", help="Scan source for token references (part of Inspect)."
    )
    sp.add_argument("--source", required=True, help="Source project directory to scan.")
    sp.add_argument(
        "--env-config", help="Optional properties file to validate against."
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
        "<output-dir>/legacy_migration.sed. In --scan-source mode an "
        "additional <output-dir>/scan_report.md is also written.",
    )

    # -- migrate-source --
    ms = subs.add_parser(
        "migrate-source",
        help="Apply a legacy_migration.sed to a source DDL tree "
        "(Windows-safe; no sed binary required).",
        description="Apply a ``legacy_migration.sed`` (generated by "
        "``import-legacy``) to every SQL-bearing file in a source "
        "tree, converting legacy substitution markers "
        "(``$VAR``, ``${VAR}``, ``&&VAR&&``) to SHIPS ``{{TOKEN}}`` "
        "form. Understands only the ``s/LHS/RHS/g`` subset that "
        "``import-legacy`` emits -- not full sed syntax. "
        "Run with ``--dry-run`` first to see what would change.",
    )
    ms.add_argument(
        "--sed",
        required=True,
        metavar="SED_FILE",
        help="Path to the ``legacy_migration.sed`` produced by "
        "``import-legacy``. Can also be any sed script containing "
        "``s/LHS/RHS/g`` substitution rules.",
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
        "decisions.json.\n\n"
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
        help="Raw DDL source directory. If omitted, the harvest stage "
        "is skipped and the existing payload is used.",
    )
    pr.add_argument(
        "--token-map",
        default=None,
        help="Path to token_map.conf for harvest token substitution.",
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
        help="Target environment (e.g. DEV). Required to run the package stage.",
    )
    pr.add_argument(
        "--env-config",
        default=None,
        help="Path to the .conf file for token resolution. Required "
        "to run the package stage.",
    )
    pr.add_argument(
        "--name",
        default=None,
        help="Package name. Required to run the package stage.",
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
        description="Read decisions.json and render a concise report of the "
        "most recent (or specified) run: stage statuses, key outputs, "
        "and full issues table. Use before promoting to the next environment.",
    )
    ex.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing decisions.json.",
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
        description="Read decisions.json, locate the most recent package stage, "
        "and confirm the archive exists on disk and the build was clean. "
        "Exit code 0 = READY, 1 = NOT READY.",
    )
    vr.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing decisions.json.",
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
    dc = subs.add_parser(
        "decisions",
        help="Manage the decisions.json audit trail.",
        description="Sub-commands for inspecting and maintaining decisions.json.",
    )
    dc_subs = dc.add_subparsers(dest="decisions_subcommand")

    dp = dc_subs.add_parser(
        "prune",
        help="Remove old run entries from decisions.json.",
        description="Prune stale run entries, keeping the most recent N runs or "
        "runs from the last N days. Always shows a preview before writing. "
        "Use --yes to skip the confirmation prompt (for CI/scripts).",
    )
    dp.add_argument(
        "--project",
        required=True,
        help="SHIPS project directory containing decisions.json.",
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

    return parser


if __name__ == "__main__":
    main()
