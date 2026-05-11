"""
builder.py — Release package builder.

The developer's tool. Takes a source directory containing DDL, DCL,
and DML files with {{TOKEN}} placeholders, resolves tokens for a
target environment, and produces a self-contained release package
that a DBA can deploy without any knowledge of the build process.

Build process:
    1. Read environment properties (token values).
    2. Scan source files for {{TOKEN}} references.
    3. Validate: all referenced tokens must be defined.
    4. Create package directory structure.
    5. Copy and resolve payload files (substitute tokens).
    6. Embed the deployment engine (database_package_deployer library).
    7. Generate BUILD.json manifest.
    8. Generate deploy.py (DBA entry point).
    9. Generate README.txt (DBA instructions).
   10. Archive as .zip or .tar.gz.

Package naming:
    {{ENV}}_{{PACKAGE_NAME}}_BUILD_{{BUILD_NO}}_{{TIMESTAMP}}.zip
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from td_release_packager.models import (
    BuildConfig,
    BuildManifest,
    DeployPhase,
    SOURCE_DIR_MAP,
)
from td_release_packager.token_engine import (
    format_malformed_tokens_report,
    read_env_config,
    scan_malformed_tokens_in_directory,
    scan_tokens_in_directory,
    substitute_tokens,
    validate_tokens,
)
from td_release_packager.eponymous_rename import extract_eponymous_name
from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    Stage,
    Status,
)

logger = logging.getLogger(__name__)

# -- Regex for MULTISET injection (duplicated from database_package_deployer --
# to avoid import dependency at build time)

_HAS_SET_MULTISET_RE = re.compile(
    r"CREATE\s+(MULTISET|SET)\s+(?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?TABLE\b",
    re.IGNORECASE,
)
_INJECT_MULTISET_RE = re.compile(
    r"(CREATE\s+)((?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?TABLE\b)",
    re.IGNORECASE,
)

# -- Eponymous filename resolution --
# Delegates to extract_eponymous_name which handles comment
# stripping, {{TOKEN}} patterns, and all object types (including
# DATABASE, USER, and other single-name types that the original
# regex missed).


def _resolve_filename(
    original_filename: str,
    resolved_content: str,
) -> str:
    """
    Derive the correct eponymous filename from resolved DDL content.

    After token substitution, the DDL contains the environment-specific
    database name (e.g. P_CORE.Customer). The package filename should
    match this — not the harvested source name (e.g. DEV01_CORE.Customer).

    Comments are stripped before parsing to avoid false matches from
    DDL keywords in comment text (e.g. '-- uses CREATE DATABASE IF').

    For files where a qualified name cannot be extracted (grants,
    revokes, .c/.h co-artefacts), the original filename is returned
    unchanged.

    Args:
        original_filename:  The source filename.
        resolved_content:   The DDL content after token substitution.

    Returns:
        The resolved eponymous filename.
    """
    # Preserve extension from the original filename
    ext = os.path.splitext(original_filename)[1]

    # Skip non-DDL files (.c, .h, .jar, etc.)
    if ext.lower() in (".c", ".h", ".jar", ".zip", ".gz"):
        return original_filename

    # Skip hidden/underscore-prefixed files
    if original_filename.startswith(".") or original_filename.startswith("_"):
        return original_filename

    # Extract the qualified name from the resolved content.
    # extract_eponymous_name strips comments internally, so DDL
    # keywords in comments won't cause false matches.
    result = extract_eponymous_name(resolved_content)
    if result is None:
        return original_filename

    eponymous_name, qualified, obj_type = result

    # Use the extracted name but preserve the original extension
    # (extract_eponymous_name assigns its own extension based on
    # object type, but the source file's extension should win in
    # case of override conventions like .sql).
    new_filename = f"{qualified}{ext}"

    if new_filename != original_filename:
        logger.info("Filename resolved: %s → %s", original_filename, new_filename)

    return new_filename


def build_package(
    config: BuildConfig,
) -> Tuple[Tuple[str, BuildManifest], Optional[Tuple[str, BuildManifest]]]:
    """
    Build a release package from source files and environment properties.

    Thin traced wrapper — see ``_build_package_impl`` for the full
    implementation.  Emits a ``ships.build`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.
    """
    from ships_tracing import stage_span

    with stage_span(
        "ships.build",
        **{
            "ships.source_dir": config.source_dir,
            "ships.environment": config.environment,
            "ships.package_name": config.package_name,
        },
    ) as _span:
        result = _build_package_impl(config)
        main_arc, main_manifest = result[0]
        _span.set_attribute("ships.package_filename", main_manifest.package_filename)
        _span.set_attribute("ships.file_count", main_manifest.file_count)
        _span.set_attribute("ships.token_count", main_manifest.token_count)
        _span.set_attribute("ships.auto_split", result[1] is not None)
        return result


def _check_working_tree(source_dir: str, allow_dirty: bool) -> bool:
    """
    Check whether the git working tree under ``source_dir`` is clean.

    Returns True if dirty (uncommitted tracked-file changes exist).
    Raises ``ValueError`` when dirty and ``allow_dirty`` is False.
    Emits a warning log when dirty and ``allow_dirty`` is True.
    Returns False (and logs nothing) when git is unavailable or the
    directory is not inside a git repository — those environments
    cannot enforce the gate.

    Args:
        source_dir:  Directory to check (typically the SHIPS project root).
        allow_dirty: When True, permit a dirty tree but stamp the manifest.

    Raises:
        ValueError: Dirty working tree and ``allow_dirty`` is False.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.debug("git not available — skipping dirty-tree check")
        return False

    if result.returncode != 0:
        # Not a git repo, or git error — skip silently
        logger.debug(
            "git status failed (rc=%d) — skipping dirty-tree check", result.returncode
        )
        return False

    dirty_lines = [l for l in result.stdout.splitlines() if l.strip()]
    if not dirty_lines:
        return False

    summary = "\n".join(f"  {l}" for l in dirty_lines[:10])
    if len(dirty_lines) > 10:
        summary += f"\n  ... and {len(dirty_lines) - 10} more"

    if not allow_dirty:
        raise ValueError(
            f"Working tree has uncommitted changes — package not built.\n"
            f"{summary}\n\n"
            f"Commit or stash your changes, or pass --allow-dirty to override.\n"
            f"Note: --allow-dirty stamps source_dirty=true in BUILD.json so the\n"
            f"Trust Report will flag this package as READY-WITH-CAVEATS."
        )

    logger.warning(
        "Building from dirty working tree (--allow-dirty). "
        "source_dirty=true will be stamped in BUILD.json.\n%s",
        summary,
    )
    return True


def _build_package_impl(
    config: BuildConfig,
) -> Tuple[Tuple[str, BuildManifest], Optional[Tuple[str, BuildManifest]]]:
    """
    Build a release package from source files and environment properties.

    Args:
        config: BuildConfig with source directory, environment,
                package name, build number, and properties file.

    Returns:
        ``((main_archive, main_manifest), companion)`` where
        ``companion`` is either ``None`` (single-zip build, no auto-
        split needed) or ``(prereqs_archive, prereqs_manifest)`` for
        an auto-split build.

        The shape is always a 2-tuple — callers always know there is
        either one archive (companion=None) or a paired bundle
        (companion populated). When a companion is returned, deploy
        order is **prereqs first, then main**.

    Raises:
        FileNotFoundError: If source directory or properties file missing.
        ValueError: If token validation fails (undefined tokens).
    """
    timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%d%H%M%S")

    # -- Resolve build number --
    if config.build_number is None:
        # Auto-increment from .build_counter in source project
        from td_release_packager.build_counter import next_build_number

        build_int = next_build_number(config.source_dir)
        logger.info("Auto-incremented build number: %d", build_int)
    else:
        build_int = config.build_number

    build_no = f"{build_int:04d}"

    # -- Package naming --
    pkg_name = f"{config.environment}_{config.package_name}_BUILD_{build_no}_{ts_str}"
    pkg_dir = os.path.join(config.output_dir, pkg_name)

    logger.info("Building package: %s", pkg_name)

    # -- Validate source directory --
    if not os.path.isdir(config.source_dir):
        raise FileNotFoundError(f"Source directory not found: {config.source_dir}")

    # -- Dirty working tree gate --
    source_dirty = _check_working_tree(config.source_dir, config.allow_dirty)

    # -- Phase 1: Read token values --
    token_values = read_env_config(config.env_config_file)
    logger.info(
        "Loaded %d token values from %s", len(token_values), config.env_config_file
    )

    # -- Phase 2: Scan source for token references --
    payload_dir = _find_payload_dir(config.source_dir)
    token_usage = scan_tokens_in_directory(payload_dir)
    logger.info("Scanned %d files with token references", len(token_usage))

    # -- Phase 2b: Catch malformed tokens BEFORE packaging --
    # Malformed {{...}} markers (whitespace inside braces, double-
    # tokenised content from a re-run harvest, orphan braces from
    # editor mishaps) silently survive substitution and end up in
    # the deployed SQL. We fail the build here with a precise
    # file/line report so the developer can fix the source file
    # rather than discover the corruption mid-deploy.
    malformed = scan_malformed_tokens_in_directory(payload_dir)
    if malformed:
        report = format_malformed_tokens_report(malformed)
        print(f"\n{report}", file=sys.stderr)
        raise ValueError(
            f"Build aborted: {sum(len(v) for v in malformed.values())} "
            f"malformed token marker(s) in "
            f"{len(malformed)} file(s). See report above."
        )

    # -- Phase 3: Validate tokens --
    errors, warnings = validate_tokens(token_values, token_usage)

    if errors or warnings:
        # -- Build structured report from raw data --
        # token_usage is {filename: set_of_tokens}
        # Invert to {token: [filenames]} for undefined token reporting
        token_to_files: Dict[str, list] = {}
        for filepath, tokens in token_usage.items():
            for token in tokens:
                token_to_files.setdefault(token, []).append(filepath)

        # Compute sets
        all_referenced = set()
        for tokens in token_usage.values():
            all_referenced.update(tokens)

        defined_tokens = set(token_values.keys())
        undefined = sorted(all_referenced - defined_tokens)
        unreferenced = sorted(defined_tokens - all_referenced)

        # -- Print structured report --
        print(f"\n{'=' * 64}")
        print("  Token Validation")
        print(f"{'=' * 64}")

        if undefined:
            print()
            print("  ERRORS — tokens referenced in DDL but not defined")
            print("  in properties (must be resolved before packaging):")
            print()

            for token in undefined:
                print(f"    {{{{{token}}}}}")
                files = sorted(token_to_files.get(token, []))
                # Show paths relative to source for readability
                for fpath in files:
                    rel = os.path.relpath(fpath, config.source_dir)
                    print(f"      -> {rel}")
                print()

            print("  Action: add these tokens to your .conf file,")
            print("  or update token_map.conf and re-harvest.")

        if unreferenced:
            print()
            print("  WARNINGS — tokens defined in properties but never")
            print("  referenced (informational — safe to ignore):")
            print()
            # Compact display — wrap token names
            token_list = ", ".join(f"{{{{{t}}}}}" for t in unreferenced)
            print(f"    {token_list}")
            print()
            print("  Tip: if these have been replaced by _T/_V variants,")
            print("  remove the old flat tokens from your properties file.")

        print()
        print(f"{'=' * 64}")
        print(
            f"  {len(undefined)} undefined token(s) (ERROR)"
            f" | {len(unreferenced)} unreferenced token(s) (WARNING)"
        )
        print(f"{'=' * 64}\n")

        if undefined:
            raise ValueError(
                f"Token validation failed: {len(undefined)} undefined "
                f"token(s). All referenced tokens must be defined in "
                f"the properties file."
            )

    # -- Phase 4: Create package structure --
    _create_package_structure(pkg_dir)

    # -- Phase 5: Copy and resolve payload files --
    total_subs, file_count, phase_inventory, filename_map, provenance_doc = (
        _copy_payload(payload_dir, pkg_dir, token_values)
    )
    logger.info(
        "Resolved %d tokens across %d files (%d filenames resolved)",
        total_subs,
        file_count,
        len(filename_map),
    )

    # -- Phase 6: Copy deployment order files if present --
    _copy_order_files(payload_dir, pkg_dir)
    _copy_waves_file(
        config.source_dir, payload_dir, pkg_dir, filename_map, token_values
    )

    # -- Phase 6b: Re-emit pre-requisite ordering with resolved names --
    # The harvest may have written a pre-requisites/_order.txt using
    # tokenised filenames (e.g. ``{{BASE_NODE}}.db``).  By this point
    # _copy_payload has resolved all tokens and applied eponymous
    # renaming so the package contains real filenames
    # (e.g. ``PDE_D01_00.db``).  Re-running _emit_prereq_order on the
    # PACKAGE directory overwrites the copied tokenised version with
    # one that references the filenames the deployer will actually find.
    _refresh_prereq_order_in_package(pkg_dir)

    # -- Phase 7: Embed deployment engine --
    _embed_deployer(pkg_dir)

    # -- Phase 8: Generate BUILD.json --
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.orchestrator import ships_yaml as _sy

    resolved_extensions = resolve_harvest_extensions(config.source_dir)

    # Read deployment.baseline_dir from ships.yaml if present
    _baseline_dir = ""
    _ships_yaml_path = os.path.join(config.source_dir, "ships.yaml")
    if os.path.isfile(_ships_yaml_path):
        try:
            _yaml_data = _sy.load(_ships_yaml_path)
            _baseline_dir = (
                _yaml_data.get("deployment", {}).get("baseline_dir", "") or ""
            )
        except Exception:
            pass

    manifest = BuildManifest(
        build_number=build_no,
        environment=config.environment,
        package_name=config.package_name,
        package_filename=f"{pkg_name}.{config.archive_format}",
        timestamp=timestamp.isoformat(),
        author=config.author,
        description=config.description,
        source_commit=config.source_commit,
        source_dirty=source_dirty,
        token_count=total_subs,
        file_count=file_count,
        phase_inventory=phase_inventory,
        tokens_resolved={k: v for k, v in sorted(token_values.items())},
        warnings=warnings,
        discovery={"extensions": sorted(resolved_extensions)},
        baseline_dir=_baseline_dir,
        # GAP-002: environment lock — deployer verifies this matches --env at Ship time.
        target_env=config.environment,
        # GAP-004: change management ticket reference and enforcement flag.
        change_ref=config.change_ref,
        require_change_ref=_read_require_change_ref(
            config.source_dir, config.environment
        ),
        # GAP-005: signature enforcement flag from ships.yaml.
        require_signature=_read_bool_env_setting(
            config.source_dir, config.environment, "require_signature"
        ),
        # GAP-006: 4-eyes approval requirement from ships.yaml.
        require_approvals=_read_int_env_setting(
            config.source_dir, config.environment, "require_approvals", default=1
        ),
        # GAP-015: TLS enforcement.
        require_tls=_read_bool_env_setting(
            config.source_dir, config.environment, "require_tls"
        ),
        # GAP-012: package age TTL.
        package_built_at=timestamp.isoformat(),
        package_max_age_days=_read_int_env_setting(
            config.source_dir, config.environment, "package_max_age_days", default=30
        ),
        package_age_violation_level=_read_str_env_setting(
            config.source_dir,
            config.environment,
            "package_age_violation_level",
            default="warning",
        ),
    )

    # -- Phase 8a: Compute and stamp Phase 1 Trust Report --
    # Discrete signals (inspect results + provenance) derive a label
    # (READY / READY-WITH-CAVEATS / BLOCKED) that tells a DBA or
    # deployment agent whether this package is safe to promote.
    from td_release_packager.trust import compute_trust_report, format_trust_banner

    trust_report = compute_trust_report(config.source_dir, pkg_dir)
    manifest.trust = trust_report.to_dict()

    manifest_path = os.path.join(pkg_dir, "BUILD.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)

    # Print trust banner to CLI
    print(format_trust_banner(trust_report))

    # -- Phase 8b: Write provenance document (v2) --
    # Records the full filename-transformation chain (source →
    # eponymous → token-resolved → package) for every payload file.
    # The HTML report uses this to render a drill-down that shows
    # the DBA exactly where in the pipeline each filename was
    # rewritten — critical for diagnosing mapping bugs that only
    # surface at deploy time.
    provenance_path = os.path.join(pkg_dir, "_provenance.json")
    provenance_doc.write(provenance_path)
    logger.info(
        "Provenance document (v%d): %d entries → %s",
        provenance_doc.version,
        len(provenance_doc.entries),
        provenance_path,
    )

    # -- Phase 9: Generate deploy.py --
    _generate_deploy_script(pkg_dir, manifest)

    # -- Phase 10: Generate README.txt --
    _generate_readme(pkg_dir, manifest)

    # -- Phase 10a: Generate interactive package report --
    # Self-contained HTML file embedded in the package.  Opens from
    # the filesystem (file: URL) — no server, no external requests.
    # Provides: filterable object inventory, wave visualisation,
    # Trust Report breakdown, and pre-filled deploy commands.
    from td_release_packager.package_report import generate_package_report

    generate_package_report(pkg_dir, manifest.__dict__)

    # -- Phase 11: Generate shell wrappers --
    _generate_shell_wrappers(pkg_dir)

    # -- Phase 12: Auto-split decision (intra_package_dependency Phase 2) --
    # When the populated package contains BOTH prerequisite-creation
    # statements (CREATE DATABASE / USER) AND objects that depend on
    # them, emit two archives: a prereqs zip and a main zip. The
    # split closes the EXPLAIN false-error case at the package layer
    # so the user never has to fix it manually.
    if _is_auto_split_needed(pkg_dir):
        (main_pair, prereqs_pair) = _split_into_paired_packages(
            pkg_dir, manifest, config.archive_format
        )
        main_archive, main_manifest = main_pair
        prereqs_archive, _prereqs_manifest = prereqs_pair
        logger.info("Auto-split: prereqs → %s", prereqs_archive)
        logger.info("Auto-split: main    → %s", main_archive)
        return (main_pair, prereqs_pair)

    # -- Phase 13 (single-zip path): Integrity fingerprint + archive + checksum --
    _generate_integrity_file(pkg_dir)
    archive_path = _archive_package(pkg_dir, config.archive_format)
    checksum_path = _generate_checksum(archive_path)

    logger.info("Package built: %s", archive_path)
    logger.info("Checksum:      %s", checksum_path)

    return ((archive_path, manifest), None)


# ---------------------------------------------------------------
# Internal — Source discovery
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# Auto-split for intra-package prereqs (Phase 2)
# ---------------------------------------------------------------
#
# Phase 1 of the intra_package_dependency work added an inspect rule
# that flags packages mixing CREATE DATABASE / USER with objects
# living in those databases. Phase 2 makes the package stage emit
# TWO zips when that pattern is detected — a prereqs zip and a main
# zip — so the user never has to split manually.
#
# Decision is post-build: walk the populated package's payload/ dir
# and check whether 01_pre_requisites/ has files AND any other phase
# also has files. When both are true, split. When either is empty,
# the original single-zip path is preserved with zero overhead.

# Phases that go in the prereqs zip when a split happens. Currently
# only DATABASE / USER (01_pre_requisites). ROLE and other system-
# scope objects stay with the dependants in the main zip — Phase 1
# does not flag role-grant intra-package deps yet, so Phase 2 does
# not split on them either.
_PREREQ_PHASES = ("01_pre_requisites",)

# Phases that stay in the main zip on a split. Listed explicitly
# rather than computed-by-exclusion so a future phase added to
# DeployPhase doesn't silently change split behaviour — adding it
# here is a deliberate decision.
_MAIN_PHASES = (
    "00_system",
    "02_dcl",
    "03_ddl",
    "04_dml",
    "05_post_install",
)


def _phase_has_files(payload_dir: str, phase_name: str) -> bool:
    """True when ``payload/<phase_name>/`` contains at least one file.

    Hidden files and ``.gitkeep`` placeholders are ignored — they exist
    purely to preserve empty directories in source control. Walks
    recursively so files in sub-directories (e.g. ``DDL/tables/X.tbl``)
    count.
    """
    phase_path = os.path.join(payload_dir, phase_name)
    if not os.path.isdir(phase_path):
        return False
    for _root, _dirs, files in os.walk(phase_path):
        for f in files:
            if f.startswith(".") or f == ".gitkeep":
                continue
            return True
    return False


def _is_auto_split_needed(pkg_dir: str) -> bool:
    """Decide whether the populated package warrants an auto-split.

    Split when ``payload/01_pre_requisites/`` is populated AND at
    least one other phase directory is populated. Either condition
    alone keeps the original single-zip flow:

      - No prereqs in the package    → nothing to split off.
      - No dependants in the package → splitting would leave an
                                       empty main zip.

    Both conditions together → emit a paired prereqs + main bundle.
    """
    payload_dir = os.path.join(pkg_dir, "payload")
    has_prereqs = any(_phase_has_files(payload_dir, p) for p in _PREREQ_PHASES)
    has_dependants = any(_phase_has_files(payload_dir, p) for p in _MAIN_PHASES)
    return has_prereqs and has_dependants


def _compute_phase_inventory(pkg_dir: str) -> Dict[str, int]:
    """Recount payload files per phase after the split has moved them.

    The pre-split inventory captured by ``_copy_payload`` covers the
    whole payload, but each half of an auto-split pair only ships a
    subset of phases. Walk the post-split tree and recount so the
    BUILD.json in each archive reports exactly what that archive
    contains.
    """
    inventory: Dict[str, int] = {}
    payload_dir = os.path.join(pkg_dir, "payload")
    if not os.path.isdir(payload_dir):
        return inventory
    for phase in os.listdir(payload_dir):
        phase_path = os.path.join(payload_dir, phase)
        if not os.path.isdir(phase_path):
            continue
        count = 0
        for _root, _dirs, files in os.walk(phase_path):
            for f in files:
                if f.startswith(".") or f == ".gitkeep":
                    continue
                count += 1
        if count:
            inventory[phase] = count
    return inventory


def _empty_phase_subtree(pkg_dir: str, phase_name: str):
    """Remove every file under ``payload/<phase_name>/`` but keep the
    top-level directory so the deployer's phase walk still finds it.

    Used by the auto-split flow: the prereqs zip empties non-prereq
    phases; the main zip empties prereq phases. The empty directories
    are preserved because the deployer iterates known phases by name
    and we don't want a missing dir to look like a corrupted package.
    """
    phase_path = os.path.join(pkg_dir, "payload", phase_name)
    if not os.path.isdir(phase_path):
        return
    for entry in os.listdir(phase_path):
        target = os.path.join(phase_path, entry)
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)


def _split_into_paired_packages(
    pkg_dir: str,
    manifest: BuildManifest,
    archive_format: str,
) -> Tuple[Tuple[str, BuildManifest], Tuple[str, BuildManifest]]:
    """Partition a fully-built package into a prereqs + main pair.

    Both halves get a complete copy of the package infrastructure
    (config/, lib/, deploy.py, README.txt) so each is independently
    deployable. Only the payload phases are partitioned: the prereqs
    zip keeps ``01_pre_requisites`` and empties every other phase;
    the main zip does the inverse.

    Both BUILD.json manifests are rewritten to:
      - share the same ``release_group`` (= the main archive basename)
      - declare their ``role`` ("prereqs" or "main")
      - have the main zip's ``requires`` list name the prereqs zip,
        making the deploy ordering programmatically discoverable
      - report the post-split ``phase_inventory`` and ``file_count``
        so each manifest reflects what its archive actually ships

    Args:
        pkg_dir:        The fully-populated single-package directory.
                        Will be transformed in-place into the MAIN
                        package; a sibling directory is created for
                        the prereqs package.
        manifest:       The single-package manifest produced by the
                        normal build flow. Will be mutated to become
                        the main manifest.
        archive_format: 'zip' or 'tar.gz'. Determines the suffix on
                        the requires reference and on both archives.

    Returns:
        ``((main_archive, main_manifest), (prereqs_archive, prereqs_manifest))``.
        Both archives have a ``.sha256`` sidecar generated. The
        in-memory directories are cleaned up by ``_archive_package``.
    """
    parent_dir = os.path.dirname(pkg_dir)
    main_basename = os.path.basename(pkg_dir)

    # Insert "prereqs" before "BUILD" — keeps both filenames easy to
    # eyeball as a pair in a directory listing.
    if "_BUILD_" not in main_basename:
        # Defensive: should never happen given the canonical naming
        # in build_package, but if it does, fall back to a suffix.
        prereqs_basename = main_basename + "_prereqs"
    else:
        prereqs_basename = main_basename.replace("_BUILD_", "_prereqs_BUILD_", 1)
    prereqs_pkg_dir = os.path.join(parent_dir, prereqs_basename)

    # The release_group ID = the main archive's basename. Derivable
    # from filename (eyeball), embedded in both manifests
    # (programmatic), and the requires list adds the third tie.
    release_group = main_basename

    # 1. Clone the main package wholesale → prereqs sibling. Then we
    #    selectively empty payload phases on each side.
    shutil.copytree(pkg_dir, prereqs_pkg_dir)

    # 2. Main: drop the prereq phases.
    for phase in _PREREQ_PHASES:
        _empty_phase_subtree(pkg_dir, phase)

    # 3. Prereqs: drop the dependant phases.
    for phase in _MAIN_PHASES:
        _empty_phase_subtree(prereqs_pkg_dir, phase)

    # 4. Recompute inventories for each half so BUILD.json reflects
    #    what the archive actually ships, not the pre-split union.
    main_inventory = _compute_phase_inventory(pkg_dir)
    prereqs_inventory = _compute_phase_inventory(prereqs_pkg_dir)

    archive_ext = "tar.gz" if archive_format == "tar.gz" else "zip"
    prereqs_archive_filename = f"{prereqs_basename}.{archive_ext}"
    main_archive_filename = f"{main_basename}.{archive_ext}"

    # 5. Mutate the supplied manifest into the MAIN manifest.
    manifest.package_filename = main_archive_filename
    manifest.phase_inventory = main_inventory
    manifest.file_count = sum(main_inventory.values())
    manifest.release_group = release_group
    manifest.role = "main"
    manifest.requires = [prereqs_archive_filename]

    # 6. Build the PREREQS manifest as a near-copy with role flipped.
    prereqs_manifest = BuildManifest(
        build_number=manifest.build_number,
        environment=manifest.environment,
        package_name=manifest.package_name,
        package_filename=prereqs_archive_filename,
        timestamp=manifest.timestamp,
        author=manifest.author,
        description=manifest.description,
        source_commit=manifest.source_commit,
        source_dirty=manifest.source_dirty,
        token_count=manifest.token_count,  # pre-split count covers both halves
        file_count=sum(prereqs_inventory.values()),
        phase_inventory=prereqs_inventory,
        tokens_resolved=dict(manifest.tokens_resolved),
        warnings=list(manifest.warnings),
        release_group=release_group,
        role="prereqs",
        requires=[],
        discovery=dict(manifest.discovery),
        target_env=manifest.target_env,
        change_ref=manifest.change_ref,
        require_change_ref=manifest.require_change_ref,
        require_signature=manifest.require_signature,
        require_approvals=manifest.require_approvals,
        require_tls=manifest.require_tls,
        package_built_at=manifest.package_built_at,
        package_max_age_days=manifest.package_max_age_days,
        package_age_violation_level=manifest.package_age_violation_level,
    )

    # 7. Re-write BUILD.json on both sides.
    for target_pkg_dir, target_manifest in (
        (pkg_dir, manifest),
        (prereqs_pkg_dir, prereqs_manifest),
    ):
        manifest_path = os.path.join(target_pkg_dir, "BUILD.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(target_manifest.__dict__, f, indent=2, ensure_ascii=False)

    # 8. Integrity fingerprints, then archive both. Prereqs first so
    #    the on-disk creation order matches the deploy order.
    _generate_integrity_file(prereqs_pkg_dir)
    prereqs_archive = _archive_package(prereqs_pkg_dir, archive_format)
    _generate_checksum(prereqs_archive)
    _generate_integrity_file(pkg_dir)
    main_archive = _archive_package(pkg_dir, archive_format)
    _generate_checksum(main_archive)

    return ((main_archive, manifest), (prereqs_archive, prereqs_manifest))


def _find_payload_dir(source_dir: str) -> str:
    """
    Locate the payload directory within the source project.

    Looks for a 'payload' subdirectory. If not found, checks for
    a 'database' subdirectory (Paul's original structure).

    Args:
        source_dir: Root of the source project.

    Returns:
        Absolute path to the payload directory.

    Raises:
        FileNotFoundError: If no payload directory is found.
    """
    for candidate in ["payload", "database", "payload/database"]:
        path = os.path.join(source_dir, candidate)
        if os.path.isdir(path):
            return path

    raise FileNotFoundError(
        f"No payload directory found in {source_dir}. "
        "Expected 'payload/' or 'database/' subdirectory."
    )


# ---------------------------------------------------------------
# Internal — Package structure
# ---------------------------------------------------------------


def _create_package_structure(pkg_dir: str):
    """
    Create the package directory skeleton.

    Args:
        pkg_dir: Root of the package being built.
    """
    dirs = [
        "config",
        "lib",
        "logs",
        "payload/00_system",
        "payload/01_pre_requisites",
        "payload/02_dcl",
        "payload/03_ddl",
        "payload/04_dml",
        "payload/05_post_install",
    ]

    for d in dirs:
        os.makedirs(os.path.join(pkg_dir, d), exist_ok=True)


# ---------------------------------------------------------------
# Internal — Payload copying with token substitution
# ---------------------------------------------------------------


def _copy_payload(
    source_payload: str,
    pkg_dir: str,
    token_values: Dict[str, str],
) -> Tuple[int, int, Dict[str, int], Dict[str, str], ProvenanceDocument]:
    """
    Copy source payload files to the package, substituting tokens
    and resolving filenames.

    Maps source directory structure to the numbered phase structure.
    Files within each phase preserve their sub-directory hierarchy.
    Filenames are derived from the resolved DDL content so that the
    package filename matches the environment-specific database name
    (e.g. P_CORE.Customer.tbl, not DEV01_CORE.Customer.tbl).

    For each file processed, a ProvenanceChain is recorded capturing
    the four pipeline stages: source, eponymous, token_resolved,
    package. The chains are aggregated into a ProvenanceDocument so
    the HTML report can render a drill-down explaining how each
    package path was derived.

    Args:
        source_payload: Path to the source payload directory.
        pkg_dir:        Package root directory.
        token_values:   Token name → value dictionary.

    Returns:
        Tuple of (total_substitutions, file_count, phase_inventory,
        filename_map, provenance_doc). filename_map maps original
        filenames to resolved filenames (only entries where the name
        changed). provenance_doc is a v2 ProvenanceDocument capturing
        the full transformation chain per file.
    """
    total_subs = 0
    file_count = 0
    phase_inventory = {}
    filename_map = {}  # original → resolved (only changed names)
    provenance_doc = ProvenanceDocument()

    for root, dirs, files in os.walk(source_payload):
        for filename in files:
            # Skip scaffolding examples — not deployment artefacts
            if filename.endswith(".sample"):
                continue

            # Skip control files — _order.txt, _waves.txt etc. are
            # handled by _copy_order_files and _copy_waves_file, not
            # by the token substitution pipeline
            if filename.startswith("_"):
                continue

            # Skip git placeholders
            if filename == ".gitkeep":
                continue

            src_file = os.path.join(root, filename)
            rel_path = os.path.relpath(src_file, source_payload)

            # Determine which phase this file belongs to
            phase, sub_path = _map_to_phase(rel_path)

            if phase is None:
                logger.warning(
                    "File '%s' does not map to any deployment phase — skipping.",
                    rel_path,
                )
                continue

            # Read and resolve content, then resolve filename
            try:
                with open(src_file, "r", encoding="utf-8") as f:
                    content = f.read()

                # Substitute tokens in content
                resolved_content, subs = substitute_tokens(content, token_values)
                total_subs += subs

                # Start a provenance chain for this file. Each stage
                # is recorded as it runs so the v2 _provenance.json
                # contains a full audit trail of how the source
                # filename became the package path.
                src_rel_path = rel_path.replace("\\", "/")
                chain = ProvenanceChain()
                chain.add(
                    Stage(
                        stage="source",
                        path=src_rel_path,
                        status=Status.APPLIED,
                    )
                )

                # Stage: eponymous. _resolve_filename extracts the
                # qualified Database.Object from resolved DDL content
                # and renames the file to match. If the DDL has no
                # qualified name (e.g. .db files, .grt files), the
                # filename is returned unchanged → no_op.
                resolved_filename = _resolve_filename(filename, resolved_content)
                eponymous_dir = os.path.dirname(src_rel_path)
                eponymous_path = (
                    f"{eponymous_dir}/{resolved_filename}"
                    if eponymous_dir
                    else resolved_filename
                )

                if resolved_filename != filename:
                    chain.add(
                        Stage(
                            stage="eponymous",
                            path=eponymous_path,
                            status=Status.APPLIED,
                            note=f"Renamed from DDL content (was {filename})",
                        )
                    )
                else:
                    chain.add(
                        Stage(
                            stage="eponymous",
                            path=eponymous_path,
                            status=Status.NO_OP,
                            note=(
                                "Filename unchanged — DDL has no qualified "
                                "Database.Object name to derive from, or "
                                "filename already matches"
                            ),
                        )
                    )

                # Stage: token_resolved. If the eponymous stage left
                # tokens in the filename (e.g. {{DOM_DATABASE_T}}.db),
                # resolve them now using the same token values.
                if "{{" in resolved_filename:
                    pre_token_filename = resolved_filename
                    resolved_filename, _ = substitute_tokens(
                        resolved_filename,
                        token_values,
                    )
                    token_resolved_path = (
                        f"{eponymous_dir}/{resolved_filename}"
                        if eponymous_dir
                        else resolved_filename
                    )
                    chain.add(
                        Stage(
                            stage="token_resolved",
                            path=token_resolved_path,
                            status=Status.APPLIED,
                            note=(
                                f"Substituted tokens in filename "
                                f"(was {pre_token_filename})"
                            ),
                        )
                    )
                else:
                    chain.add(
                        Stage(
                            stage="token_resolved",
                            path=eponymous_path,
                            status=Status.NO_OP,
                            note="No {{TOKEN}} markers in filename",
                        )
                    )

                # Track the mapping (for _waves.txt transformation)
                if resolved_filename != filename:
                    filename_map[filename] = resolved_filename

                # Build destination path with resolved filename
                sub_dir = os.path.dirname(sub_path)

                # Compute final package-relative path
                pkg_rel_path = os.path.join(
                    phase.value, sub_dir, resolved_filename
                ).replace("\\", "/")

                # Stage: package. The file lands in its phase
                # directory. This stage is always 'applied' because
                # every file is placed in the package — it's recorded
                # so the report shows where the file ended up and the
                # chain has a consistent four-stage shape.
                chain.add(
                    Stage(
                        stage="package",
                        path=pkg_rel_path,
                        status=Status.APPLIED,
                        note=f"Placed in phase '{phase.value}'",
                    )
                )

                provenance_doc.add_chain(chain)

                dest_file = os.path.join(
                    pkg_dir, "payload", phase.value, sub_dir, resolved_filename
                )

                # Write resolved content
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                with open(dest_file, "w", encoding="utf-8") as f:
                    f.write(resolved_content)

                # Inject MULTISET for table DDL files if missing
                if resolved_filename.endswith(".tbl"):
                    _inject_multiset_in_file(dest_file)

                file_count += 1
                phase_key = phase.value
                phase_inventory[phase_key] = phase_inventory.get(phase_key, 0) + 1

            except UnicodeDecodeError:
                # Binary file — copy without substitution or rename.
                # Provenance chain still recorded so the report has a
                # complete inventory; the eponymous and token stages
                # are marked 'skipped' with explanatory notes.
                src_rel_path = rel_path.replace("\\", "/")
                pkg_rel_path = os.path.join(phase.value, sub_path).replace("\\", "/")

                bin_chain = ProvenanceChain()
                bin_chain.add(
                    Stage(
                        stage="source",
                        path=src_rel_path,
                        status=Status.APPLIED,
                    )
                )
                bin_chain.add(
                    Stage(
                        stage="eponymous",
                        path=src_rel_path,
                        status=Status.SKIPPED,
                        note="Binary file — eponymous rename not applicable",
                    )
                )
                bin_chain.add(
                    Stage(
                        stage="token_resolved",
                        path=src_rel_path,
                        status=Status.SKIPPED,
                        note="Binary file — token substitution not applicable",
                    )
                )
                bin_chain.add(
                    Stage(
                        stage="package",
                        path=pkg_rel_path,
                        status=Status.APPLIED,
                        note=f"Copied verbatim to phase '{phase.value}'",
                    )
                )
                provenance_doc.add_chain(bin_chain)

                dest_file = os.path.join(pkg_dir, "payload", phase.value, sub_path)
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                file_count += 1
                phase_key = phase.value
                phase_inventory[phase_key] = phase_inventory.get(phase_key, 0) + 1

            except KeyError as e:
                # Undefined token encountered during substitution.
                # This means validate_tokens() missed it — the file
                # was not scanned during Phase 2 but is being
                # processed during Phase 5. Report with full context.
                token_name = str(e).strip("'\"")
                rel_file = os.path.relpath(src_file, source_payload)
                raise ValueError(
                    f"Undefined token found during packaging.\n\n"
                    f"  File:    {rel_file}\n"
                    f"  Token:   {{{{{token_name}}}}}\n\n"
                    f"  This token is referenced in the file but is not\n"
                    f"  defined in the config file.\n\n"
                    f"  To fix, either:\n"
                    f"    1. Add {token_name}=<value> to your .conf\n"
                    f"       file, or\n"
                    f"    2. Add the literal database name to your\n"
                    f"       token_map.conf and re-harvest with --force"
                ) from None

            except KeyError as e:
                # Undefined token found during substitution.
                # The KeyError argument is the bare token name.
                rel = os.path.relpath(src_file, source_payload)
                token_name = str(e).strip("'\"")
                print(
                    f"\n{'=' * 64}\n"
                    f"  ERROR — Undefined token during substitution\n"
                    f"{'=' * 64}\n"
                    f"\n"
                    f"  File:  {rel}\n"
                    f"  Token: {{{{{token_name}}}}}\n"
                    f"\n"
                    f"  This token is referenced in the file above but\n"
                    f"  is not defined in your .conf file.\n"
                    f"\n"
                    f"  To fix, either:\n"
                    f"    1. Add the token to your .conf file:\n"
                    f"       {token_name}=<value>\n"
                    f"\n"
                    f"    2. Add the literal name to your token_map.conf\n"
                    f"       and re-harvest with --force:\n"
                    f"       <literal_db_name>={{{{{token_name}}}}}\n"
                    f"\n"
                    f"  Note: if this token was not reported during\n"
                    f"  validation, the file may have been added or\n"
                    f"  modified after the last harvest.\n"
                    f"{'=' * 64}\n",
                    file=sys.stderr,
                )
                raise ValueError(
                    f"Undefined token {{{{{token_name}}}}} in {rel} — "
                    f"package build aborted."
                ) from None

    return (total_subs, file_count, phase_inventory, filename_map, provenance_doc)


def _inject_multiset_in_file(file_path: str):
    """
    Inject MULTISET into a .tbl file if neither SET nor MULTISET is specified.

    Modifies the file in place. Called at BUILD time so the packaged
    DDL shows exactly what will be deployed — no surprises for the DBA.

    Args:
        file_path: Path to the resolved .tbl file.
    """
    from td_release_packager.sql_text import (
        strip_comments_and_string_literals,
    )

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Strip BOTH comments AND string literals before detection /
    # position lookup. Comment-only stripping wasn't enough — a
    # CHECK constraint like ``CHECK (col IN ('CREATE TABLE'))``
    # or any other literal containing the keyword would otherwise
    # be matched as the injection target.
    cleaned = strip_comments_and_string_literals(content)

    if _HAS_SET_MULTISET_RE.search(cleaned):
        return  # Already has SET or MULTISET

    m = _INJECT_MULTISET_RE.search(cleaned)
    if m is None:
        return

    # Apply the substitution at the exact span of the real DDL,
    # preserving any surrounding comments verbatim.
    head = content[: m.start()]
    matched = content[m.start() : m.end()]
    tail = content[m.end() :]
    new_matched = _INJECT_MULTISET_RE.sub(r"\1MULTISET \2", matched, count=1)
    modified = head + new_matched + tail

    if modified != content:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(modified)
        logger.info("MULTISET injected at build time: %s", os.path.basename(file_path))


def _map_to_phase(rel_path: str) -> Tuple[Optional[DeployPhase], str]:
    """
    Map a source-relative path to a deployment phase.

    Examines the path components to find a match in SOURCE_DIR_MAP.
    Returns the phase and the remaining sub-path.

    Args:
        rel_path: Path relative to the payload root.

    Returns:
        Tuple of (DeployPhase_or_None, remaining_sub_path).
    """
    parts = rel_path.replace("\\", "/").split("/")

    for i, part in enumerate(parts):
        if part in SOURCE_DIR_MAP:
            phase = SOURCE_DIR_MAP[part]
            sub_path = "/".join(parts[i + 1 :])
            return (phase, sub_path)

    return (None, rel_path)


def _copy_order_files(source_payload: str, pkg_dir: str):
    """
    Copy any _order.txt files from source to the package payload.

    These files define topological deployment order within a phase.

    Args:
        source_payload: Source payload directory.
        pkg_dir:        Package root directory.
    """
    for root, dirs, files in os.walk(source_payload):
        for filename in files:
            if filename == "_order.txt":
                src = os.path.join(root, filename)
                rel = os.path.relpath(src, source_payload)
                phase, sub = _map_to_phase(rel)
                if phase:
                    dest = os.path.join(pkg_dir, "payload", phase.value, sub)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src, dest)


def _refresh_prereq_order_in_package(pkg_dir: str) -> None:
    """Re-emit pre-requisites/_order.txt inside the BUILT PACKAGE.

    By the time this runs, ``_copy_payload`` has resolved every
    ``{{TOKEN}}`` and applied eponymous renaming, so the package's
    ``payload/01_pre_requisites/`` directory contains real filenames
    (e.g. ``PDE_D01_00.db``, ``PDE_D01_00_GCFR_API.db``).

    ``_copy_order_files`` may have already copied a harvest-time
    ``_order.txt`` that references tokenised names.  This function
    overwrites it with a fresh version derived from the resolved
    filenames and their ``CREATE DATABASE/USER FROM <parent>``
    dependency clauses, so the deployer's ``read_order_file`` finds
    names that actually exist in the package.

    Args:
        pkg_dir: Root of the fully-built package directory (not yet
                 archived — files are still readable on disk).
    """
    from td_release_packager.ingest import _emit_prereq_order

    prereq_phase_dir = os.path.join(pkg_dir, "payload", "01_pre_requisites")
    if not os.path.isdir(prereq_phase_dir):
        return

    # Only refresh when there are actually prereq files.  An empty
    # phase directory produces an empty ordered list — nothing to do.
    result = _emit_prereq_order(prereq_phase_dir)
    if result.ordered:
        logger.info(
            "Package: refreshed pre-requisites/_order.txt with %d resolved "
            "filename(s) (%d unresolvable)",
            len(result.ordered),
            len(result.unresolvable),
        )


def _copy_waves_file(
    project_dir: str,
    source_payload: str,
    pkg_dir: str,
    filename_map: Dict[str, str] = None,
    token_values: Dict[str, str] = None,
):
    """
    Copy _waves.txt from the project root into the package payload,
    transforming paths from source-relative to package-relative and
    resolving tokenised filenames to their environment-specific names.

    The analyser writes _waves.txt at the project root with paths
    like 'payload/database/DDL/tables/{{DB}}.Table.tbl'. The deploy.py
    template expects _waves.txt inside each phase directory with
    paths relative to that phase and resolved filenames
    (e.g. 'tables/A_D01_STD.Table.tbl').

    Path resolution applies two transformations in order:
        1. Token substitution — {{TOKEN}} placeholders in the path
           are resolved to environment-specific values.
        2. Filename mapping — the basename is looked up in the
           filename_map (from _copy_payload) to match the renamed
           file in the package.

    Args:
        project_dir:     Project root directory.
        source_payload:  Source payload directory (payload/database/).
        pkg_dir:         Package root directory.
        filename_map:    Dict of original_filename → resolved_filename
                         from _copy_payload. Used to transform tokenised
                         filenames in _waves.txt to match the resolved
                         filenames in the package.
        token_values:    Dict of token_name → value for resolving
                         {{TOKEN}} placeholders in wave file paths.
    """
    if filename_map is None:
        filename_map = {}
    if token_values is None:
        token_values = {}

    waves_src = os.path.join(project_dir, "_waves.txt")
    if not os.path.exists(waves_src):
        return

    # Read and transform paths, grouping by phase
    phase_lines = {}  # phase_value → list of transformed lines

    with open(waves_src, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.rstrip("\n").rstrip("\r")

            # Comments and blank lines — copy to all phases
            if not stripped or stripped.startswith("#") or stripped == "---":
                for phase_val in phase_lines:
                    phase_lines[phase_val].append(stripped)
                # If no phases seen yet, buffer for later
                if not phase_lines:
                    phase_lines.setdefault("_buffer", []).append(stripped)
                continue

            # File path — transform from source-relative to package-relative
            # Strip 'payload/database/' or 'payload\database\' prefix
            path_normalised = stripped.replace("\\", "/")
            rel_to_payload = path_normalised
            for prefix in ["payload/database/", "payload\\database\\"]:
                norm_prefix = prefix.replace("\\", "/")
                if path_normalised.startswith(norm_prefix):
                    rel_to_payload = path_normalised[len(norm_prefix) :]
                    break

            # Map to phase
            phase, sub_path = _map_to_phase(rel_to_payload)
            if phase is None:
                logger.warning(
                    "_waves.txt: could not map path to phase: %s",
                    stripped,
                )
                continue

            phase_val = phase.value
            if phase_val not in phase_lines:
                # Flush buffer (comments from before first file)
                phase_lines[phase_val] = phase_lines.pop("_buffer", [])

            # --- Resolve the filename ---
            # Step 1: Token substitution on the path (handles
            # {{TOKEN}}.Object.viw → D01_MP_DOM_V.Object.viw)
            original_filename = os.path.basename(sub_path)
            resolved_filename = original_filename
            if "{{" in resolved_filename and token_values:
                resolved_filename, _ = substitute_tokens(
                    resolved_filename, token_values
                )

            # Step 2: Filename map lookup (handles
            # MortgagePlatform_Domain.Object.viw → D01_MP_DOM_V.Object.viw)
            resolved_filename = filename_map.get(
                original_filename,
                filename_map.get(resolved_filename, resolved_filename),
            )

            # Step 3: Verify the resolved file exists in the package.
            # If steps 1-2 missed (mapping gap), fall back to scanning
            # the target directory for a file with the same object name
            # (the part after the first dot). This handles cases where
            # the source filename format doesn't match the mapping key.
            sub_dir = os.path.dirname(sub_path)
            target_dir = os.path.join(pkg_dir, "payload", phase_val, sub_dir)
            target_path = os.path.join(target_dir, resolved_filename)

            if not os.path.exists(target_path) and os.path.isdir(target_dir):
                ext = os.path.splitext(resolved_filename)[1].lower()
                name_parts = os.path.splitext(resolved_filename)[0].split(".", 1)
                obj_name = name_parts[1] if len(name_parts) == 2 else name_parts[0]

                for candidate in os.listdir(target_dir):
                    if candidate.startswith("_"):
                        continue
                    cand_ext = os.path.splitext(candidate)[1].lower()
                    cand_parts = os.path.splitext(candidate)[0].split(".", 1)
                    cand_obj = cand_parts[1] if len(cand_parts) == 2 else cand_parts[0]

                    if cand_obj == obj_name and cand_ext == ext:
                        logger.info(
                            "_waves.txt: mapped by object name: %s → %s",
                            resolved_filename,
                            candidate,
                        )
                        resolved_filename = candidate
                        break

            if resolved_filename != original_filename:
                sub_path = os.path.join(sub_dir, resolved_filename)
                logger.debug(
                    "_waves.txt: resolved %s → %s",
                    original_filename,
                    resolved_filename,
                )

            phase_lines[phase_val].append(sub_path)

    # Remove any unused buffer
    phase_lines.pop("_buffer", None)

    # Write one _waves.txt per phase
    for phase_val, lines in phase_lines.items():
        dest = os.path.join(pkg_dir, "payload", phase_val, "_waves.txt")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("Copied _waves.txt → payload/%s/_waves.txt", phase_val)


# ---------------------------------------------------------------
# Internal — Embed the deployment engine
# ---------------------------------------------------------------


def _embed_deployer(pkg_dir: str):
    """
    Copy the database_package_deployer package into the package's lib/ directory.

    The deploy.py script adds lib/ to sys.path so the DBA does not
    need to install database_package_deployer separately.

    Args:
        pkg_dir: Package root directory.
    """
    # Find database_package_deployer package location
    import database_package_deployer

    deployer_src = os.path.dirname(database_package_deployer.__file__)

    dest = os.path.join(pkg_dir, "lib", "database_package_deployer")
    shutil.copytree(deployer_src, dest)

    logger.debug("Embedded database_package_deployer from %s", deployer_src)


# ---------------------------------------------------------------
# Internal — Generated files
# ---------------------------------------------------------------


def _generate_deploy_script(pkg_dir: str, manifest: BuildManifest):
    """
    Generate deploy.py — the DBA's single entry point.

    This script bootstraps the embedded database_package_deployer, reads the
    BUILD.json for context, and orchestrates the deployment with
    query banding and logging.

    Args:
        pkg_dir:  Package root directory.
        manifest: Build manifest for embedding metadata.
    """
    script = f'''#!/usr/bin/env python3
"""
Deployment script for: {manifest.package_filename}

Built:       {manifest.timestamp}
Environment: {manifest.environment}
Build:       {manifest.build_number}
Author:      {manifest.author}
Description: {manifest.description}

Usage:
    python deploy.py --host <teradata_host> --user <username>
    python deploy.py --host <teradata_host> --user <username> --explain
    python deploy.py --host <teradata_host> --user <username> --dry-run
    python deploy.py --help

Requirements:
    Python 3.9+
    teradatasql  (pip install teradatasql)
"""

import argparse
import hashlib
import json
import logging
import os
import pathlib
import sys
import glob
from datetime import datetime, timezone

# -- Bootstrap: add embedded lib/ to path --
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))

from database_package_deployer.deployer import deploy_package, explain_package
from database_package_deployer.wave_parser import parse_waves_file
from database_package_deployer.deploy_runtime import discover_files, read_order_file

# -- Build metadata --
BUILD_NUMBER = "{manifest.build_number}"
PACKAGE_NAME = "{manifest.package_name}"
ENVIRONMENT = "{manifest.environment}"


def main():
    """Main deployment entry point for the DBA."""
    args = parse_args()

    # -- Configure logging --
    log_dir = os.path.join(SCRIPT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"deploy_{{ts}}.log")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("deploy")

    logger.info("=" * 64)
    logger.info("  Package:     %s", PACKAGE_NAME)
    logger.info("  Environment: %s", ENVIRONMENT)
    logger.info("  Build:       %s", BUILD_NUMBER)
    logger.info("  Mode:        %s", "DRY RUN" if args.dry_run else ("EXPLAIN" if args.explain else "DEPLOY"))
    logger.info("  Streams:     %d", min(max(args.streams, 1), 8))
    logger.info("=" * 64)

    # Clamp streams to 1–8
    num_streams = min(max(args.streams, 1), 8)

    # -- Package integrity verification (before any database connection) --
    pkg_hash = _verify_integrity(SCRIPT_DIR, logger, args.skip_integrity_check)

    # -- Trust Report banner --
    # Read the trust block computed at build time and surface the
    # label and per-signal status before any database connection is
    # opened.  A BLOCKED label means the package should not be
    # deployed without investigating and resolving the blocking signals.
    _build_json = os.path.join(SCRIPT_DIR, "BUILD.json")
    if os.path.exists(_build_json):
        with open(_build_json, encoding="utf-8") as _f:
            _bdata = json.load(_f)
        _trust = _bdata.get("trust", {{}})
        if _trust:
            _label = _trust.get("label", "UNKNOWN")
            _icons = {{"READY": "\\u2713", "READY-WITH-CAVEATS": "\\u26a0", "BLOCKED": "\\u2717"}}
            _licon = _icons.get(_label, "?")
            logger.info("=" * 64)
            logger.info("  Package Trust: %s %s", _licon, _label)
            for _sname, _sig in _trust.get("signals", {{}}).items():
                _sicons = {{"pass": "\\u2713", "warn": "\\u26a0", "fail": "\\u2717", "unknown": "?"}}
                _sicon = _sicons.get(_sig.get("status", "unknown"), "?")
                logger.info("  %s %-28s %s", _sicon, _sname, _sig.get("message", ""))
            logger.info("=" * 64)
            if _label == "BLOCKED":
                logger.error(
                    "Package trust is BLOCKED. Fix the failing signals before "
                    "deploying. Use --skip-trust-check to override (development only)."
                )
                if not getattr(args, "skip_trust_check", False):
                    sys.exit(1)

    # -- Connect (skip in dry-run — no database needed) --
    cursor = None
    make_cursor = None
    no_connection = args.dry_run

    if not args.dry_run:
        cursor = connect(args)

        # -- Connection factory for parallel streams --
        def make_cursor():
            return connect(args)

        # -- Set query band (skip for explain) --
        if not args.explain:
            try:
                band = (
                    f"BUILD={{BUILD_NUMBER}};PKG={{PACKAGE_NAME}};"
                    f"ENV={{ENVIRONMENT}};PKG_HASH={{pkg_hash[:16]}};"
                    f"DEPLOYER=database_package_deployer_v2;"
                )
                cursor.execute(f"SET QUERY_BAND = '{{band}}' FOR SESSION")
                logger.info("Query band set: %s", band)
            except Exception as e:
                logger.warning("Query band failed (non-fatal): %s", e)
    else:
        logger.info("Dry run — no database connection required")

    # -- Deploy chaining: deploy companion prereqs package first (if any) --
    #
    # When this package's BUILD.json has a non-empty ``requires`` list, a
    # companion prereqs package was auto-generated alongside it (Phase 2 of
    # the intra-package dependency trilogy).  The prereqs package contains
    # CREATE DATABASE / CREATE USER statements that must be deployed before
    # the schema objects in this (main) package.
    #
    # Chaining rule:
    #   - Dry-run: skip chaining (no database connection).
    #   - Explain OR live deploy: always deploy prereqs LIVE first.
    #     Prereqs objects (DATABASE/USER) are SKIP_IF_EXISTS — idempotent
    #     and safe to deploy even before an EXPLAIN run on main.
    #     This ensures parent databases physically exist when EXPLAIN
    #     validates the main package's DDL (fixes issue #53 Option 2).
    #
    build_json_path = os.path.join(SCRIPT_DIR, "BUILD.json")
    requires = []
    if os.path.exists(build_json_path):
        with open(build_json_path, encoding="utf-8") as _f:
            _build_data = json.load(_f)
        requires = _build_data.get("requires", [])

    if requires and not args.dry_run:
        _prereqs_zip_name = requires[0]
        _prereqs_basename = os.path.splitext(_prereqs_zip_name)[0]
        _prereqs_dir = os.path.join(os.path.dirname(SCRIPT_DIR), _prereqs_basename)

        if not os.path.isdir(_prereqs_dir):
            logger.error(
                "Deploy chaining: companion prereqs package not found at: %s\\n"
                "  Extract '%s' alongside this package directory and retry.",
                _prereqs_dir, _prereqs_zip_name,
            )
            sys.exit(1)

        logger.info("=" * 64)
        logger.info("  Deploy chaining — companion prereqs")
        logger.info("  Prereqs: %s", _prereqs_basename)
        logger.info("  (Deploying prereqs live so parent databases exist)")
        logger.info("=" * 64)

        _pre_payload = os.path.join(_prereqs_dir, "payload")
        _pre_waves, _pre_files, _pre_use_waves = [], [], False
        for _ph in sorted(d for d in os.listdir(_pre_payload)
                          if os.path.isdir(os.path.join(_pre_payload, d))):
            _ph_path = os.path.join(_pre_payload, _ph)
            _wf = os.path.join(_ph_path, "_waves.txt")
            _of = os.path.join(_ph_path, "_order.txt")
            if os.path.exists(_wf):
                _pre_use_waves = True
                _pw = parse_waves_file(_wf, _ph_path)
                _pre_waves.extend(_pw)
                for _w in _pw:
                    _pre_files.extend(_w)
            elif os.path.exists(_of):
                _pf = read_order_file(_of, _ph_path)
                _pre_waves.append(_pf)
                _pre_files.extend(_pf)
            else:
                _pf = discover_files(_ph_path)
                if _pf:
                    _pre_waves.append(_pf)
                    _pre_files.extend(_pf)

        if _pre_files:
            _pre_log_dir = os.path.join(_prereqs_dir, "logs")
            os.makedirs(_pre_log_dir, exist_ok=True)
            _pre_result = deploy_package(
                cursor=cursor,
                package_dir=_pre_log_dir,
                ordered_files=_pre_files if not _pre_use_waves else None,
                waves=_pre_waves if _pre_use_waves else None,
                stop_on_failure=True,
                dry_run=False,   # live — prereqs are idempotent (SKIP_IF_EXISTS)
            )
            if not _pre_result.success:
                logger.error(
                    "Companion prereqs deployment FAILED (%d failure(s)). "
                    "Aborting main package deployment.",
                    _pre_result.failed,
                )
                if _pre_result.report_path:
                    logger.error("  Prereqs report: %s", _pre_result.report_path)
                sys.exit(1)
            logger.info(
                "Companion prereqs deployed (%d objects). Proceeding with main.",
                _pre_result.completed,
            )
        else:
            logger.info("Companion prereqs package contains no deployable files — skipping.")

    elif requires and args.dry_run:
        logger.info(
            "Deploy chaining: skipped in dry-run mode "
            "(prereqs '%s' require a live connection).",
            requires[0],
        )

    # -- Collect files per phase, building waves where defined --
    payload_dir = os.path.join(SCRIPT_DIR, "payload")
    phases = sorted(
        [d for d in os.listdir(payload_dir)
         if os.path.isdir(os.path.join(payload_dir, d))],
    )

    all_waves = []
    all_files = []
    use_waves = False

    for phase_dir_name in phases:
        phase_path = os.path.join(payload_dir, phase_dir_name)
        logger.info("Phase: %s", phase_dir_name)

        try:
            if cursor:
                band = (
                    f"BUILD={{BUILD_NUMBER}};PKG={{PACKAGE_NAME}};"
                    f"ENV={{ENVIRONMENT}};PKG_HASH={{pkg_hash[:16]}};"
                    f"PHASE={{phase_dir_name}};"
                )
                cursor.execute(f"SET QUERY_BAND = '{{band}}' FOR SESSION")
        except Exception:
            pass

        waves_file = os.path.join(phase_path, "_waves.txt")
        order_file = os.path.join(phase_path, "_order.txt")

        if os.path.exists(waves_file):
            use_waves = True
            phase_waves = parse_waves_file(waves_file, phase_path)
            all_waves.extend(phase_waves)
            for w in phase_waves:
                all_files.extend(w)
            logger.info("  %d wave(s), %d objects",
                        len(phase_waves), sum(len(w) for w in phase_waves))
        elif os.path.exists(order_file):
            phase_files = read_order_file(order_file, phase_path)
            all_waves.append(phase_files)
            all_files.extend(phase_files)
        else:
            phase_files = discover_files(phase_path)
            if phase_files:
                all_waves.append(phase_files)
                all_files.extend(phase_files)

    if not all_files:
        logger.warning("No deployable files found in payload.")
        return

    logger.info("Total: %d objects across %d waves", len(all_files), len(all_waves))

    # -- Run deployment or explain --
    # Skip pre-flight when no database connection (dry-run)
    # Phases provide sequential barriers via directory ordering.
    # Waves provide dependency-ordered sub-grouping within a phase
    # (generated by: python -m td_release_packager analyze).
    try:
        if args.explain:
            # EXPLAIN mode — validate SQL without executing
            result = explain_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                ordered_files=all_files if not use_waves else None,
                waves=all_waves if use_waves else None,
            )

            # Print summary
            status = "PASSED" if result.failed == 0 else "FAILED"
            logger.info("=" * 64)
            logger.info("  EXPLAIN Validation %s", status)
            logger.info("  Passed:         %d", result.completed)
            logger.info("  Failed:         %d", result.failed)
            logger.info("  Not applicable: %d", result.skipped)
            if result.report_path:
                logger.info("  Report:     %s", result.report_path)
            logger.info("  Log:        %s", log_file)
            logger.info("=" * 64)

            sys.exit(0 if result.failed == 0 else 1)

        elif use_waves:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                waves=all_waves,
                num_streams=num_streams,
                connect_fn=make_cursor if num_streams > 1 else None,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
            )
        else:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                ordered_files=all_files,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
            )

        # Print summary
        status = "PASSED" if result.success else "FAILED"
        logger.info("=" * 64)
        logger.info("  Deployment %s", status)
        logger.info("  Completed:  %d", result.completed)
        logger.info("  Skipped:    %d", result.skipped)
        logger.info("  Failed:     %d", result.failed)
        if result.report_path:
            logger.info("  Report:     %s", result.report_path)
        logger.info("  Log:        %s", log_file)
        logger.info("=" * 64)

        sys.exit(0 if result.success else 1)

    except Exception as e:
        logger.exception("Deployment failed: %s", e)
        sys.exit(1)
    finally:
        # Clear query band and close connection
        if cursor:
            try:
                cursor.execute("SET QUERY_BAND = NONE FOR SESSION")
            except Exception:
                pass
            cursor.close()
            cursor.connection.close()


def _verify_integrity(script_dir, logger, skip=False):
    """Verify the package has not been modified since packaging.

    Recomputes SHA-256 over every file under payload/, derives the
    combined package_hash, and compares against package_integrity.json.
    Aborts the process on any mismatch.

    Returns the package_hash string so callers can embed it in the
    query band.  Returns 'SKIPPED' when --skip-integrity-check is set.
    """
    integrity_file = os.path.join(script_dir, "package_integrity.json")

    if skip:
        logger.warning("Integrity check SKIPPED (--skip-integrity-check).")
        return "SKIPPED"

    if not os.path.exists(integrity_file):
        logger.error(
            "INTEGRITY CHECK FAILED: package_integrity.json not found — "
            "package may be incomplete or corrupted. "
            "Use --skip-integrity-check to override (development only)."
        )
        sys.exit(1)

    with open(integrity_file, encoding="utf-8") as fh:
        stored = json.load(fh)

    stored_hash = stored.get("package_hash", "")
    stored_files = stored.get("files", {{}})
    algorithm = stored.get("algorithm", "SHA-256")
    logger.info("Verifying package integrity (%s) ...", algorithm)

    computed_files = {{}}
    payload_dir = os.path.join(script_dir, "payload")
    for root, dirs, files in os.walk(payload_dir):
        dirs.sort()
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = pathlib.Path(os.path.relpath(fpath, script_dir)).as_posix()
            with open(fpath, "rb") as fh:
                computed_files[rel] = hashlib.sha256(fh.read()).hexdigest()

    errors = []
    for path, expected in sorted(stored_files.items()):
        got = computed_files.get(path)
        if got is None:
            errors.append(f"  MISSING:  {{path}}")
        elif got != expected:
            errors.append(f"  MODIFIED: {{path}}")
    for path in sorted(computed_files):
        if path not in stored_files:
            errors.append(f"  ADDED:    {{path}}")

    if errors:
        logger.error(
            "INTEGRITY CHECK FAILED — %d file(s) changed since packaging:",
            len(errors),
        )
        for e in errors:
            logger.error(e)
        sys.exit(1)

    combined = "".join(
        f"{{k}}:{{v}}\\n" for k, v in sorted(computed_files.items())
    )
    computed_hash = hashlib.sha256(combined.encode()).hexdigest()

    if computed_hash != stored_hash:
        logger.error(
            "INTEGRITY CHECK FAILED — package hash mismatch.\\n"
            "  Expected: %s\\n  Computed: %s",
            stored_hash,
            computed_hash,
        )
        sys.exit(1)

    logger.info(
        "  ✓ Integrity verified: %s... (%d files)",
        computed_hash[:16],
        len(computed_files),
    )
    return computed_hash


def connect(args):
    """
    Establish Teradata connection with user-friendly error handling.

    Catches common connection failures and translates them into
    clear, actionable messages for the DBA.
    """
    import teradatasql
    params = {{"host": args.host, "user": args.user, "charset": "UTF8"}}
    if args.password:
        params["password"] = args.password
    if args.logmech:
        params["logmech"] = args.logmech

    try:
        conn = teradatasql.connect(**params)
        return conn.cursor()
    except teradatasql.OperationalError as e:
        err = str(e)
        # -- Hostname / network errors --
        if "Hostname lookup failed" in err or "no such host" in err:
            print(
                "\\n"
                "  ┌──────────────────────────────────────────────────────┐\\n"
                "  │  CONNECTION FAILED: Host not found                  │\\n"
                "  └──────────────────────────────────────────────────────┘\\n"
                f"\\n"
                f"  Host:   {{args.host}}\\n"
                f"\\n"
                f"  The hostname could not be resolved. Check:\\n"
                f"    1. Is the hostname correct?\\n"
                f"    2. Is your VPN connected?\\n"
                f"    3. Can you ping the host from this machine?\\n"
                f"       ping {{args.host}}\\n"
                f"    4. Is DNS resolving correctly?\\n"
                f"       nslookup {{args.host}}\\n",
                file=sys.stderr,
            )
            sys.exit(1)

        # -- Authentication errors --
        if "Logon failed" in err or "authentication" in err.lower():
            print(
                "\\n"
                "  ┌──────────────────────────────────────────────────────┐\\n"
                "  │  CONNECTION FAILED: Authentication error            │\\n"
                "  └──────────────────────────────────────────────────────┘\\n"
                f"\\n"
                f"  Host:   {{args.host}}\\n"
                f"  User:   {{args.user}}\\n"
                f"  Logmech: {{args.logmech or '(default)'}}\\n"
                f"\\n"
                f"  The username or password was rejected. Check:\\n"
                f"    1. Is the username correct?\\n"
                f"    2. Is the password correct?\\n"
                f"    3. Is the account locked or expired?\\n"
                f"    4. If using LDAP/KRB5, is --logmech set correctly?\\n",
                file=sys.stderr,
            )
            sys.exit(1)

        # -- Connection refused / timeout --
        if "refused" in err.lower() or "timeout" in err.lower() or "timed out" in err.lower():
            print(
                "\\n"
                "  ┌──────────────────────────────────────────────────────┐\\n"
                "  │  CONNECTION FAILED: Connection refused or timed out │\\n"
                "  └──────────────────────────────────────────────────────┘\\n"
                f"\\n"
                f"  Host:   {{args.host}}\\n"
                f"\\n"
                f"  The server did not respond. Check:\\n"
                f"    1. Is the Teradata server running?\\n"
                f"    2. Is port 1025 open from this machine?\\n"
                f"    3. Are there firewall rules blocking access?\\n"
                f"    4. Is the server under maintenance?\\n",
                file=sys.stderr,
            )
            sys.exit(1)

        # -- Fallback for other OperationalErrors --
        print(
            "\\n"
            "  ┌──────────────────────────────────────────────────────┐\\n"
            "  │  CONNECTION FAILED                                   │\\n"
            "  └──────────────────────────────────────────────────────┘\\n"
            f"\\n"
            f"  Host:   {{args.host}}\\n"
            f"  User:   {{args.user}}\\n"
            f"\\n"
            f"  {{err}}\\n",
            file=sys.stderr,
        )
        sys.exit(1)


def parse_args():
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description=(
            f"Deploy {{PACKAGE_NAME}} (build {{BUILD_NUMBER}}) "
            f"to {{ENVIRONMENT}}."
        ),
    )
    p.add_argument("--host", help="Teradata host (required unless --dry-run).")
    p.add_argument("--user", help="Teradata user (required unless --dry-run).")
    p.add_argument("--password", help="Teradata password.")
    p.add_argument("--logmech", help="Logon mechanism.")
    p.add_argument("--dry-run", action="store_true",
                   help="Simulate without executing DDL (no connection needed).")
    p.add_argument("--explain", action="store_true",
                   help="Validate SQL via EXPLAIN (connection required, "
                        "no changes made). Compiles each statement against "
                        "the live schema to catch reference and permission "
                        "errors before deployment.")
    p.add_argument("--streams", type=int, default=4,
                   help="Parallel deployment streams (1-8, default: 4).")
    p.add_argument("--continue-on-error", action="store_true",
                   help="Continue past failures.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging.")
    p.add_argument("--skip-integrity-check", action="store_true",
                   help="Skip package integrity verification (development use only).")
    p.add_argument("--skip-trust-check", action="store_true",
                   help="Deploy even when the Trust Report label is BLOCKED "
                        "(development use only — never use in production).")
    args = p.parse_args()

    # Validate: --host and --user are required unless --dry-run
    if not args.dry_run:
        if not args.host or not args.user:
            p.error("--host and --user are required (use --dry-run for offline simulation)")

    # --explain and --dry-run are mutually exclusive
    if args.dry_run and args.explain:
        p.error("--dry-run and --explain are mutually exclusive "
                "(--explain requires a database connection)")

    return args


if __name__ == "__main__":
    main()
'''

    deploy_path = os.path.join(pkg_dir, "deploy.py")
    with open(deploy_path, "w", encoding="utf-8") as f:
        f.write(script)

    # Make executable
    os.chmod(deploy_path, 0o755)


def _generate_readme(pkg_dir: str, manifest: BuildManifest):
    """Generate a README.txt with deployment instructions for the DBA."""
    readme = f"""================================================================
  TERADATA RELEASE PACKAGE
================================================================

  Package:     {manifest.package_name}
  Environment: {manifest.environment}
  Build:       {manifest.build_number}
  Built:       {manifest.timestamp}
  Author:      {manifest.author}
  Description: {manifest.description}

================================================================
  PREREQUISITES
================================================================

  1. Python 3.9 or later
  2. teradatasql driver:  pip install teradatasql
  3. Network access to the target Teradata system
  4. A Teradata user with sufficient privileges (CREATE TABLE,
     CREATE VIEW, GRANT, etc. on the target databases)

================================================================
  DEPLOYMENT
================================================================

  Linux/Mac:
    ./deploy.sh --host <teradata_host> --user <username>

  Windows:
    deploy.bat --host <teradata_host> --user <username>

  Direct Python:
    python deploy.py --host <teradata_host> --user <username>

  Options:
    --password <pwd>    Teradata password (prompted if omitted)
    --logmech <mech>    Logon mechanism (LDAP, TD2, etc.)
    --streams <n>       Parallel deployment streams (1-8, default: 4)
    --dry-run           Simulate without executing any SQL
    --continue-on-error Continue past failures
    -v, --verbose       Debug-level logging

================================================================
  PARALLEL DEPLOYMENT
================================================================

  DDL phases with a _waves.txt file deploy objects in parallel
  across multiple database connections (streams). Each wave is
  a synchronisation barrier — wave N+1 starts only after all
  objects in wave N succeed.

    python deploy.py --host myserver --user dbc --streams 6

  Default: 4 streams. Maximum: 8. Use --streams 1 for sequential.

================================================================
  DRY RUN (RECOMMENDED FIRST)
================================================================

  Run with --dry-run to validate everything without making changes:

    python deploy.py --host myserver --user dbc --dry-run

  This runs pre-flight checks (permissions, space) and reports
  what would happen, without executing any DDL/DCL/DML.

================================================================
  LOGS AND REPORTS
================================================================

  All deployment logs are written to the logs/ directory:
    logs/deploy_YYYYMMDD_HHMMSS.log    — full execution log
    logs/.deploy_manifest.json          — machine-readable state
    logs/.deploy_report_*.html          — branded HTML report

  The HTML report is the recommended artefact for sign-off.

================================================================
  RESTARTABILITY
================================================================

  If the deployment fails partway through, fix the issue and
  re-run the same command. The deployment engine tracks which
  objects have been completed and resumes from the failure point.

================================================================
  QUERY BAND AUDIT TRAIL
================================================================

  All SQL executed during deployment carries a query band:
    BUILD={manifest.build_number};PKG={manifest.package_name};ENV={manifest.environment};PHASE=...;

  Query DBC.DBQLogTbl to review the deployment audit trail:
    SELECT QueryBand, StartTime, UserName, StatementType
    FROM DBC.DBQLogTbl
    WHERE GetQueryBandValue(QueryBand, 0, 'BUILD') = '{manifest.build_number}'
    ORDER BY StartTime;

================================================================
  CONTENTS
================================================================

"""

    # List phase inventories
    for phase, count in sorted(manifest.phase_inventory.items()):
        readme += f"  {phase}: {count} file(s)\n"

    readme += f"""
================================================================
  SUPPORT
================================================================

  For issues with this package, contact: {manifest.author or "the development team"}

================================================================
"""

    readme_path = os.path.join(pkg_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme)


def _generate_shell_wrappers(pkg_dir: str):
    """Generate deploy.sh and deploy.bat wrappers."""
    # Linux/Mac
    sh_content = '#!/bin/bash\ncd "$(dirname "$0")" && python3 deploy.py "$@"\n'
    sh_path = os.path.join(pkg_dir, "deploy.sh")
    with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(sh_content)
    os.chmod(sh_path, 0o755)

    # Windows
    bat_content = '@echo off\r\ncd /d "%~dp0"\r\npython deploy.py %*\r\n'
    bat_path = os.path.join(pkg_dir, "deploy.bat")
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(bat_content)


# ---------------------------------------------------------------
# Internal — Archiving
# ---------------------------------------------------------------


def _archive_package(pkg_dir: str, archive_format: str) -> str:
    """
    Archive the package directory as .zip or .tar.gz.

    The archive is created alongside the package directory,
    and the unarchived directory is removed after successful
    archiving.

    Args:
        pkg_dir:        Path to the package directory.
        archive_format: 'zip' or 'tar.gz'.

    Returns:
        Path to the archive file.
    """
    if archive_format == "tar.gz":
        archive_path = shutil.make_archive(
            base_name=pkg_dir,
            format="gztar",
            root_dir=os.path.dirname(pkg_dir),
            base_dir=os.path.basename(pkg_dir),
        )
    else:
        archive_path = shutil.make_archive(
            base_name=pkg_dir,
            format="zip",
            root_dir=os.path.dirname(pkg_dir),
            base_dir=os.path.basename(pkg_dir),
        )

    # Remove the unarchived directory
    shutil.rmtree(pkg_dir)
    logger.info("Archived and cleaned up: %s", archive_path)

    return archive_path


def _generate_integrity_file(pkg_dir: str) -> str:
    """Compute a SHA-256 fingerprint over every payload file.

    Walks ``payload/`` recursively (sorted), hashes each file, then
    derives a single ``package_hash`` as SHA-256 of the sorted
    ``"rel/path:filehash\\n"`` concatenation.  Writes the result to
    ``package_integrity.json`` in the package root so the embedded
    ``deploy.py`` can verify the package has not been tampered with
    before any database connection is opened.

    Args:
        pkg_dir: Package root directory (not yet archived).

    Returns:
        The hex package_hash.
    """
    import pathlib

    payload_dir = os.path.join(pkg_dir, "payload")
    file_hashes: dict = {}

    for root, dirs, files in os.walk(payload_dir):
        dirs.sort()
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = pathlib.Path(os.path.relpath(fpath, pkg_dir)).as_posix()
            with open(fpath, "rb") as f:
                file_hashes[rel] = hashlib.sha256(f.read()).hexdigest()

    combined = "".join(f"{k}:{v}\n" for k, v in sorted(file_hashes.items()))
    package_hash = hashlib.sha256(combined.encode()).hexdigest()

    integrity = {
        "algorithm": "SHA-256",
        "package_hash": package_hash,
        "file_count": len(file_hashes),
        "files": file_hashes,
    }

    out_path = os.path.join(pkg_dir, "package_integrity.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(integrity, f, indent=2, ensure_ascii=False)

    logger.info(
        "Integrity fingerprint: %s... (%d files)",
        package_hash[:16],
        len(file_hashes),
    )
    return package_hash


def _read_bool_env_setting(source_dir: str, environment: str, key: str) -> bool:
    """Read a boolean per-environment key from ships.yaml.

    Looks for::

        environments:
          <ENV>:
            <key>: true

    Returns False when ships.yaml is absent, the environment block is
    missing, or the key is not set.

    Args:
        source_dir:  Project root directory containing ships.yaml.
        environment: Target environment name (e.g. 'PRD').
        key:         Setting name (e.g. 'require_change_ref').

    Returns:
        Boolean value of the setting, defaulting to False.
    """
    ships_yaml_path = os.path.join(source_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml_path):
        return False
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy

        data = _sy.load(ships_yaml_path)
        envs = data.get("environments", {})
        env_cfg = envs.get(environment, envs.get(environment.upper(), {}))
        return bool(env_cfg.get(key, False))
    except Exception:
        return False


def _read_require_change_ref(source_dir: str, environment: str) -> bool:
    """Read require_change_ref for *environment* from ships.yaml (GAP-004)."""
    return _read_bool_env_setting(source_dir, environment, "require_change_ref")


def _read_str_env_setting(
    source_dir: str, environment: str, key: str, default: str = ""
) -> str:
    """Read a string per-environment key from ships.yaml.

    Returns *default* when ships.yaml is absent, the environment block is
    missing, or the key is not set.
    """
    ships_yaml_path = os.path.join(source_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml_path):
        return default
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy

        data = _sy.load(ships_yaml_path)
        envs = data.get("environments", {})
        env_cfg = envs.get(environment, envs.get(environment.upper(), {}))
        return str(env_cfg.get(key, default))
    except Exception:
        return default


def _read_int_env_setting(
    source_dir: str, environment: str, key: str, default: int = 0
) -> int:
    """Read an integer per-environment key from ships.yaml.

    Returns *default* when ships.yaml is absent, the environment block is
    missing, or the key is not set.
    """
    ships_yaml_path = os.path.join(source_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml_path):
        return default
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy

        data = _sy.load(ships_yaml_path)
        envs = data.get("environments", {})
        env_cfg = envs.get(environment, envs.get(environment.upper(), {}))
        val = env_cfg.get(key, default)
        return int(val)
    except Exception:
        return default


def _generate_checksum(archive_path: str) -> str:
    """
    Generate a SHA-256 checksum sidecar file for the package archive.

    Writes a `.sha256` file alongside the archive in the standard
    format used by sha256sum(1):

        <hex_digest>  <filename>

    The DBA verifies the package with a single command:

        sha256sum -c DEV_SHIPS_TEST_BUILD_0008.zip.sha256
        # DEV_SHIPS_TEST_BUILD_0008.zip: OK

    Args:
        archive_path: Path to the .zip or .tar.gz archive.

    Returns:
        Path to the generated .sha256 file.
    """
    sha256 = hashlib.sha256()

    with open(archive_path, "rb") as f:
        while True:
            chunk = f.read(65536)  # 64 KB chunks
            if not chunk:
                break
            sha256.update(chunk)

    digest = sha256.hexdigest()
    archive_name = os.path.basename(archive_path)

    # Standard sha256sum format: two-space separator, filename
    checksum_path = archive_path + ".sha256"
    with open(checksum_path, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {archive_name}\n")

    logger.info(
        "SHA-256: %s  %s",
        digest[:16] + "...",
        archive_name,
    )

    return checksum_path
