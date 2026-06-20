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
    7. Generate context/ships.build.json manifest.
    8. Generate deploy.py (DBA entry point).
    9. Generate README.txt (DBA instructions).
   10. Archive as .zip or .tar.gz under a release-group directory.

Package output layout:
    releases/{{ENV}}_{{PACKAGE_NAME}}_BUILD_{{BUILD_NO}}_{{TIMESTAMP}}/
        {{ENV}}_{{PACKAGE_NAME}}_BUILD_{{BUILD_NO}}_{{TIMESTAMP}}_01_main.zip
        {{ENV}}_{{PACKAGE_NAME}}_BUILD_{{BUILD_NO}}_{{TIMESTAMP}}_01_main.zip.sha256
        release_group.json
        README.txt

Multi-package release groups may also include:
    *_00_environment_prereqs.zip
    *_01_prereqs.zip
    *_02_main.zip
"""

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
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
from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    Stage,
    Status,
)
from td_release_packager.context_artifacts import write_context_artifacts
from td_release_packager.environment_prereqs import (
    analyse_environment_parent_requirements,
    find_dba_placeholders,
    has_dba_placeholders,
    write_environment_prereq_context,
    write_environment_prereq_payload,
)


CONTEXT_DIR = "context"


def _context_file(pkg_dir: str, filename: str) -> str:
    """Return the canonical package path for a SHIPS JSON metadata file."""
    context_dir = os.path.join(pkg_dir, CONTEXT_DIR)
    os.makedirs(context_dir, exist_ok=True)
    return os.path.join(context_dir, filename)


logger = logging.getLogger(__name__)


def _package_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    """Ignore transient or non-release artefacts when cloning package trees.

    Package directories are cloned during split/finalize operations and the
    embedded deployer is copied into each package. On Windows, `.pyc` files
    under `__pycache__` can disappear while tests or import machinery are
    running, which makes `shutil.copytree` fail with a transient WinError 3.
    Backup/editor artefacts are equally unsafe to ship because they can expose
    stale code and confuse downstream agents. Package report viewer pages are
    regenerated after cloning, so copying the old hidden viewer directory only
    adds avoidable Windows filesystem race surface.
    """
    ignored_names = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".package_report_code",
    }
    ignored_suffixes = (
        ".pyc",
        ".pyo",
        ".bak",
        ".tmp",
        ".old",
        ".orig",
        ".rej",
        ".swp",
        ".swo",
    )
    return {
        name
        for name in names
        if name in ignored_names
        or name.startswith("~")
        or name.lower().endswith(ignored_suffixes)
    }


def _context_relpath(filename: str) -> str:
    """Return the package-relative path for a SHIPS JSON metadata file."""
    return os.path.join(CONTEXT_DIR, filename).replace(os.sep, "/")


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
# Uses local DDL-name extraction so the builder can safely derive package
# filenames from tokenised SHIPS payloads without depending on deploy-time
# parser behaviour.

_NAME_PART_RE = r'(?:"[^"]+"|\{\{[A-Za-z_][A-Za-z0-9_]*\}\}|[A-Za-z_][A-Za-z0-9_$#]*)'
_QUALIFIED_NAME_RE = rf"{_NAME_PART_RE}(?:\s*\.\s*{_NAME_PART_RE})?"
_NAME_END_RE = r"(?=$|\s|[;(])"

# Ordered only for deterministic tie-breaking when two patterns start at the
# same position. The extraction function primarily sorts by match position so
# an outer CREATE/REPLACE PROCEDURE is preferred over dynamic DDL strings later
# in the body.
_EPONYMOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "DATABASE",
        re.compile(
            rf"\bCREATE\s+DATABASE\s+(?P<name>{_NAME_PART_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "USER",
        re.compile(
            rf"\bCREATE\s+USER\s+(?P<name>{_NAME_PART_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "TABLE",
        re.compile(
            rf"\bCREATE\s+(?:MULTISET\s+|SET\s+)?"
            rf"(?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?(?:TRACE\s+)?TABLE\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "JOIN_INDEX",
        re.compile(
            rf"\bCREATE\s+JOIN\s+INDEX\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "VIEW",
        re.compile(
            rf"\b(?:CREATE|REPLACE)\s+VIEW\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "PROCEDURE",
        re.compile(
            rf"\b(?:CREATE|REPLACE)\s+PROCEDURE\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "MACRO",
        re.compile(
            rf"\b(?:CREATE|REPLACE)\s+MACRO\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "FUNCTION",
        re.compile(
            rf"\b(?:CREATE|REPLACE)\s+(?:FUNCTION|SPECIFIC\s+FUNCTION)\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
    (
        "TRIGGER",
        re.compile(
            rf"\b(?:CREATE|REPLACE)\s+TRIGGER\s+"
            rf"(?P<name>{_QUALIFIED_NAME_RE}){_NAME_END_RE}",
            re.IGNORECASE,
        ),
    ),
)


def _mask_comments_and_string_literals(sql_text: str) -> str:
    """Return SQL with comments and single-quoted literals blanked out.

    The returned string keeps the original length so regex match positions still
    map to the original statement. Double-quoted identifiers are preserved.
    This prevents dynamic SQL inside procedure bodies, such as
    ``'replace view db.v as ...'``, from being considered an earlier DDL object.
    """
    chars = list(sql_text)
    i = 0
    length = len(chars)
    while i < length:
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < length else ""

        if ch == "-" and nxt == "-":
            start = i
            i += 2
            while i < length and chars[i] not in "\r\n":
                i += 1
            for j in range(start, i):
                chars[j] = " "
            continue

        if ch == "/" and nxt == "*":
            start = i
            i += 2
            while i + 1 < length and not (chars[i] == "*" and chars[i + 1] == "/"):
                i += 1
            i = min(i + 2, length)
            for j in range(start, i):
                chars[j] = " "
            continue

        if ch == "'":
            start = i
            i += 1
            while i < length:
                if chars[i] == "'":
                    if i + 1 < length and chars[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            for j in range(start, i):
                chars[j] = " "
            continue

        i += 1

    return "".join(chars)


def _normalise_identifier_spacing(name: str) -> str:
    """Normalize whitespace around dots and strip identifier quotes.

    Double-quoted identifiers are legal Teradata names, but package filenames
    should remain eponymous without carrying the SQL delimiter quotes, matching
    the historical builder behaviour. SHIPS token placeholders are preserved.
    """
    normalized = re.sub(r"\s*\.\s*", ".", name.strip())
    parts = []
    for part in normalized.split("."):
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            parts.append(part[1:-1].replace('""', '"'))
        else:
            parts.append(part)
    return ".".join(parts)


def _extract_eponymous_name(sql_text: str) -> Optional[tuple[str, str, str]]:
    """Extract the first real DDL object name from SQL text.

    Supports SHIPS token placeholders as identifier parts, for example
    ``{{GCFR_P_UT}}.GCFR_UT_BKEY_S_K_NextId_Log_CT``.

    Returns:
        ``(object_name, qualified_name, object_type)`` when a DDL object is
        found, otherwise ``None``.
    """
    cleaned = _mask_comments_and_string_literals(sql_text)
    candidates: list[tuple[int, int, str, str]] = []

    for priority, (obj_type, pattern) in enumerate(_EPONYMOUS_PATTERNS):
        match = pattern.search(cleaned)
        if match is None:
            continue
        name = _normalise_identifier_spacing(
            sql_text[match.start("name") : match.end("name")]
        )
        candidates.append((match.start(), priority, obj_type, name))

    if not candidates:
        return None

    _start, _priority, obj_type, qualified = min(
        candidates, key=lambda item: (item[0], item[1])
    )
    object_name = qualified.rsplit(".", 1)[-1]
    return object_name, qualified, obj_type


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

    For files where a qualified name cannot be extracted, and for DCL,
    ordered SQL, or co-artefacts (.dcl, .grt, .osql, .c, .h), the
    original filename is returned unchanged.

    Args:
        original_filename:  The source filename.
        resolved_content:   The DDL content after token substitution.

    Returns:
        The resolved eponymous filename.
    """
    # Preserve extension from the original filename
    ext = os.path.splitext(original_filename)[1]

    # Skip DCL, ordered SQL, and non-DDL files. These filenames already
    # carry source/package intent; re-parsing content can mistake inner
    # DDL or privilege names for the artefact identity.
    if ext.lower() in (".dcl", ".grt", ".osql", ".c", ".h", ".jar", ".zip", ".gz"):
        return original_filename

    # Skip hidden/underscore-prefixed files
    if original_filename.startswith(".") or original_filename.startswith("_"):
        return original_filename

    # Extract the earliest real DDL object name from the resolved content.
    # Comments and string literals are masked first so dynamic DDL inside
    # stored procedures does not drive the package filename.
    result = _extract_eponymous_name(resolved_content)
    if result is None:
        return original_filename

    eponymous_name, qualified, obj_type = result

    # Use the extracted name but preserve the original extension.
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

    dirty_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not dirty_lines:
        return False

    summary = "\n".join(f"  {line}" for line in dirty_lines[:10])
    if len(dirty_lines) > 10:
        summary += f"\n  ... and {len(dirty_lines) - 10} more"

    if not allow_dirty:
        raise ValueError(
            f"Working tree has uncommitted changes — package not built.\n"
            f"{summary}\n\n"
            f"Commit or stash your changes, or pass --allow-dirty to override.\n"
            f"Note: --allow-dirty stamps source_dirty=true in context/ships.build.json so the\n"
            f"Trust Report will flag this package as READY_WITH_CAVEATS."
        )

    logger.warning(
        "Building from dirty working tree (--allow-dirty). "
        "source_dirty=true will be stamped in context/ships.build.json.\n%s",
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
    # A build may emit one or more package archives (environment prereqs,
    # application prereqs, main).  Treat the shared release identity as a
    # first-class output directory so every related archive, checksum, and
    # group manifest stays together even when the group contains only one
    # package.
    pkg_name = f"{config.environment}_{config.package_name}_BUILD_{build_no}_{ts_str}"
    release_group_dir = os.path.join(config.output_dir, pkg_name)
    pkg_dir = os.path.join(release_group_dir, pkg_name)

    os.makedirs(release_group_dir, exist_ok=True)

    logger.info("Building package group: %s", pkg_name)

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
    #
    # PR2 follow-up: package now drives the same coverage helper
    # inspect Step 0 uses. The earlier ``validate_tokens`` flow only
    # saw content tokens; a payload referencing a token only via a
    # tokenised filename (e.g. ``{{DB_PREFIX}}_BUS_V.dcl``) slipped
    # through package even after PR2 made inspect catch it. The
    # shared helper scans filenames too, so the package-side gate
    # cannot now be looser than the inspect-side one.
    from td_release_packager.token_engine import validate_payload_against_env

    coverage = validate_payload_against_env(
        payload_dir,
        config.source_dir,
        config.env_config_file,
    )
    undefined = coverage["undefined"]
    unreferenced = coverage["unreferenced"]
    token_to_files = coverage["token_files"]
    filename_tokens = coverage["filename_tokens"]
    # Build the warnings list the build manifest used to receive from
    # ``validate_tokens``. Preserves the on-disk shape for downstream
    # tooling (trust report, decisions ledger, stage_results) — the
    # manifest's ``warnings`` field has always been a list of
    # human-readable strings, not a structured undefined-set.
    _cfg = f" in {config.env_config_file}" if config.env_config_file else ""
    warnings = [
        f"Token '{{{{{t}}}}}' is defined in the env config{_cfg} "
        "but never referenced in any payload file."
        for t in unreferenced
    ]

    if undefined or unreferenced:
        # -- Print structured report --
        print(f"\n{'=' * 64}")
        print("  Token Validation")
        print(f"{'=' * 64}")

        if undefined:
            print()
            print("  ERRORS — tokens referenced in DDL but not defined")
            print("  in the env config (must be resolved before packaging):")
            print()

            for token in undefined:
                print(f"    {{{{{token}}}}}")
                # Content references — show paths relative to source.
                for fpath in token_to_files.get(token, []):
                    rel = os.path.relpath(fpath, config.source_dir)
                    print(f"      content  -> {rel}")
                # Filename references — already relative to payload_dir.
                for rel in filename_tokens.get(token, []):
                    print(f"      filename -> {rel}")
                print()

            print("  Action: add these tokens to your .conf file,")
            print("  or update token_map.conf and re-harvest.")

        if unreferenced:
            print()
            print("  WARNINGS — tokens defined in the env config but never")
            print("  referenced in any payload file (informational — safe to ignore):")
            print()
            # Compact display — wrap token names
            token_list = ", ".join(f"{{{{{t}}}}}" for t in unreferenced)
            print(f"    {token_list}")
            print()
            print("  Tip: if these have been replaced by _T/_V variants,")
            print(f"  remove the old flat tokens from: {config.env_config_file}")

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
                f"the env config: {config.env_config_file}"
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

    # -- Phase 6c: Backfill inferred inter-database grants into the package --
    # Inspect --fix-grants can persist these into the project payload, but a
    # package must be self-contained even when the source tree has not been
    # pre-repaired.  Infer against the resolved package SQL and write only
    # missing generated DCL into the package staging directory.
    generated_dcl = _backfill_missing_inferred_dcl(pkg_dir)
    if generated_dcl:
        file_count += generated_dcl
        phase_inventory[DeployPhase.DCL.value] = (
            phase_inventory.get(DeployPhase.DCL.value, 0) + generated_dcl
        )

    # -- Phase 6d: Backfill role DDL needed by role/user grant scripts --
    # Grants under 02_dcl/roles and 02_dcl/users fail on Teradata if the
    # grantee role does not already exist.  Materialise missing CREATE ROLE
    # scripts in 00_system/roles so the package remains deployable without
    # requiring a separate hand-written role payload.
    generated_roles = _backfill_missing_role_ddl(pkg_dir)
    if generated_roles:
        file_count += generated_roles
        phase_inventory[DeployPhase.SYSTEM.value] = (
            phase_inventory.get(DeployPhase.SYSTEM.value, 0) + generated_roles
        )
    _ensure_system_role_waves_include_package_roles(pkg_dir)

    # -- Phase 7: Embed deployment engine --
    _embed_deployer(pkg_dir)

    # -- Phase 8: Generate context/ships.build.json --
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
        # Option C: Ed25519 asymmetric signature enforcement.
        require_asymmetric_signature=_read_bool_env_setting(
            config.source_dir, config.environment, "require_asymmetric_signature"
        ),
        # Option C: public key PEM embedded in the package (from ships.yaml signing.public_key).
        ships_public_key=_read_signing_public_key(config.source_dir),
    )

    # -- Phase 8a: Write provenance document (v2) --
    # Records the full filename-transformation chain (source →
    # eponymous → token-resolved → package) for every payload file.
    # This must be written BEFORE the trust report is computed so
    # provenance_complete can validate the actual package artefact.
    provenance_path = _context_file(pkg_dir, "ships.provenance.json")
    provenance_doc.write(provenance_path)
    logger.info(
        "Provenance document (v%d): %d entries → %s",
        provenance_doc.version,
        len(provenance_doc.entries),
        provenance_path,
    )

    # Write an initial manifest before trust computation.  The trust
    # build_reproducible signal reads source_dirty from context/ships.build.json;
    # the manifest is overwritten below after the trust block is stamped.
    manifest_path = _context_file(pkg_dir, "ships.build.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)

    # -- Phase 8b: Compute and stamp Phase 1 Trust Report --
    # Discrete signals (inspect results + package context artefacts) derive
    # a status (READY / READY_WITH_CAVEATS / BLOCKED) that tells a DBA or
    # deployment agent whether this package is safe to promote.
    from td_release_packager.trust import (
        TRUST_RESULT_REF,
        compute_trust_report,
        format_trust_banner,
        write_trust_result,
    )

    trust_report = compute_trust_report(config.source_dir, pkg_dir)
    write_trust_result(pkg_dir, trust_report)
    manifest.trust = {"trust_ref": TRUST_RESULT_REF}

    # -- Phase 8b.2: Stamp action controls --
    # Derive the machine-readable action vocabulary (deploy / dry_run /
    # modify_payload / repackage / verify_integrity / rollback /
    # forward_to_human) from trust + role so downstream agents do not
    # have to re-derive what is allowed, blocked, or approval-gated.
    from td_release_packager.actions import (
        ACTIONS_RESULT_REF,
        compute_actions_report,
        write_actions_result,
    )

    actions_report = compute_actions_report(
        trust=trust_report.to_dict(),
        role=manifest.role or "",
        has_dba_placeholders=has_dba_placeholders(pkg_dir),
    )
    write_actions_result(pkg_dir, actions_report)
    manifest.actions_ref = ACTIONS_RESULT_REF

    # -- Phase 8b.3: Stamp capability flags (issue #149) --
    from td_release_packager.capabilities import (
        CAPABILITIES_RESULT_REF,
        compute_capabilities_report,
        write_capabilities_result,
    )

    capabilities_report = compute_capabilities_report(
        {
            "require_change_ref": manifest.require_change_ref,
            "require_signature": manifest.require_signature,
            "require_asymmetric_signature": manifest.require_asymmetric_signature,
            "require_approvals": manifest.require_approvals,
        }
    )
    write_capabilities_result(pkg_dir, capabilities_report)
    manifest.capabilities_ref = CAPABILITIES_RESULT_REF

    # -- Phase 8b.4: Stamp the formal agent policy (issue #151) --
    from td_release_packager.policy import (
        POLICY_RESULT_REF,
        compute_agent_policy,
        write_policy_result,
    )

    agent_policy = compute_agent_policy(
        trust=trust_report.to_dict(),
        governance={
            "require_change_ref": manifest.require_change_ref,
            "require_signature": manifest.require_signature,
            "require_asymmetric_signature": manifest.require_asymmetric_signature,
            "require_approvals": manifest.require_approvals,
            "require_tls": manifest.require_tls,
        },
    )
    write_policy_result(pkg_dir, agent_policy)
    manifest.policy_ref = POLICY_RESULT_REF

    # -- Phase 8b.5: Stamp the agent-readable dependency graph (#150) --
    # Wraps analyse_project + export_json so an agent reading the
    # package gets the same JSON shape produced by ``analyze --formats
    # json``, without having to parse _waves.txt or run the analyser.
    from td_release_packager.dependencies import (
        DEPENDENCIES_RESULT_REF,
        write_dependencies_result,
    )

    try:
        write_dependencies_result(pkg_dir, config.source_dir)
        manifest.dependencies_ref = DEPENDENCIES_RESULT_REF
    except Exception as _exc:  # pragma: no cover - defensive
        # The analyser can raise on malformed projects; the package
        # should still build. Surface the failure in the build log
        # but do not abort.
        logger.warning(
            "dependencies artefact not produced: %s — package builds without it.",
            _exc,
        )

    # -- Phase 8b.6: Stamp the agent-readable rules catalogue (#144) --
    # Per-rule remediation metadata (safe_fix_available, automation_level,
    # recommended_action, risk, requires_human_review) so an agent
    # consuming ships.decisions.json findings can resolve each finding
    # to an actionable remediation plan.
    from td_release_packager.rules_catalogue import (
        RULES_RESULT_REF,
        write_rules_result,
    )

    write_rules_result(pkg_dir)
    manifest.rules_ref = RULES_RESULT_REF

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)

    # -- Phase 8c: Write agent-facing context artefacts --
    # These files are a compact handoff contract for humans, CI/CD,
    # MCP tools, and autonomous agents.  They reference context/ships.build.json
    # and context/ships.provenance.json rather than duplicating detailed evidence.
    write_context_artifacts(pkg_dir, manifest, config)

    # Print trust banner to CLI
    print(format_trust_banner(trust_report))

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
            pkg_dir, manifest, config.archive_format, token_values=token_values
        )
        main_archive, main_manifest = main_pair
        prereqs_archive, _prereqs_manifest = prereqs_pair
        logger.info("Auto-split: prereqs → %s", prereqs_archive)
        logger.info("Auto-split: main    → %s", main_archive)
        return (main_pair, prereqs_pair)

    # -- Phase 13 (single-package path): finalise the release group,
    # archive the _01_main package, and write release_group.json.
    archive_path, manifest = _finalize_single_package(
        pkg_dir, manifest, config.archive_format
    )

    logger.info("Package built: %s", archive_path)
    logger.info("Release group:  %s", os.path.dirname(archive_path))

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

# Phases that go in the prereqs zip when a split happens.
#
# ``00_system`` is foundational platform/application setup: roles,
# profiles, maps, authorizations, foreign servers, and similar system-
# scope artefacts. These must exist before any DCL or DDL waves run.
# ``01_pre_requisites`` contains application-owned databases/users.
_PREREQ_PHASES = (
    "00_system",
    "01_pre_requisites",
)

# Phases that stay in the main zip on a split. Listed explicitly
# rather than computed-by-exclusion so a future phase added to
# DeployPhase doesn't silently change split behaviour — adding it
# here is a deliberate decision.
_MAIN_PHASES = (
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

    Split when any prerequisite phase (``00_system`` or
    ``01_pre_requisites``) is populated AND at least one main phase
    directory is populated. Either condition
    alone keeps the original single-zip flow:

      - No prereqs/system in package → nothing to split off.
      - No main payload in package   → splitting would leave an
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
    context/ships.build.json in each archive reports exactly what that archive
    contains.
    """
    inventory: Dict[str, int] = {}
    payload_dir = os.path.join(pkg_dir, "payload")
    if not os.path.isdir(payload_dir):
        return inventory
    # Sort the phase list, the directory walk, and the file iteration.
    # PR1 invariant: every output-affecting traversal in the deterministic
    # core must be stable run-to-run, regardless of filesystem traversal
    # order. os.listdir / os.walk return entries in OS-dependent order
    # (NTFS happens to be alphabetical on Windows; ext4 is not on Linux).
    for phase in sorted(os.listdir(payload_dir)):
        phase_path = os.path.join(payload_dir, phase)
        if not os.path.isdir(phase_path):
            continue
        count = 0
        for _root, dirs, files in os.walk(phase_path):
            dirs.sort()
            for f in sorted(files):
                if f.startswith(".") or f == ".gitkeep":
                    continue
                count += 1
        if count:
            inventory[phase] = count
    return inventory


def _filter_provenance_to_present_payload(pkg_dir: str) -> None:
    """Keep provenance entries only for payload files present in ``pkg_dir``.

    Auto-split starts by cloning the full package, so the copied
    ``context/ships.provenance.json`` initially describes the pre-split union.
    After payload phases are emptied for each half, filter the provenance
    document so package report drill-downs and agent context only reference
    files that physically exist in that archive.
    """
    provenance_path = _context_file(pkg_dir, "ships.provenance.json")
    if not os.path.isfile(provenance_path):
        return

    with open(provenance_path, encoding="utf-8") as f:
        document = json.load(f)

    entries = document.get("entries", {})
    if not isinstance(entries, dict):
        return

    filtered = {}
    for package_path, chain in entries.items():
        normalized = str(package_path).replace("\\", "/").lstrip("/")
        parts = normalized.split("/")
        candidates = [
            os.path.join(pkg_dir, *parts),
            os.path.join(pkg_dir, "payload", *parts),
        ]
        if any(os.path.isfile(candidate) for candidate in candidates):
            filtered[package_path] = chain

    document["entries"] = filtered
    with open(provenance_path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, ensure_ascii=False)
        f.write("\n")


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


def _create_environment_prereqs_package_if_needed(
    *,
    prereqs_pkg_dir: str,
    manifest: BuildManifest,
    release_group: str,
    archive_format: str,
    known_external_parents: Optional[set] = None,
) -> Optional[Tuple[str, BuildManifest]]:
    """Emit a _00_environment_prereqs package when external parents exist.

    The generated package is deliberately review-gated.  It contains a DBA
    review script plus JSON requirement/evidence metadata under
    ``context/prerequisites/``.  It does not silently invent the ultimate
    platform parent for a missing database such as ``GCFR_MAIN``.

    Args:
        prereqs_pkg_dir: Post-split prereqs package directory containing only
            CREATE DATABASE/USER payload.
        manifest: Main package manifest used as the metadata source.
        release_group: Shared release-group identity.
        archive_format: Archive format, ``zip`` or ``tar.gz``.

    Returns:
        ``(archive_path, manifest)`` for the generated environment prereq
        package, or ``None`` when no external parent requirements exist.
    """
    requirements = analyse_environment_parent_requirements(
        prereqs_pkg_dir,
        known_external_parents=known_external_parents,
    )
    if not requirements:
        return None

    parent_dir = os.path.dirname(prereqs_pkg_dir)
    archive_ext = "tar.gz" if archive_format == "tar.gz" else "zip"
    env_basename = f"{release_group}_00_environment_prereqs"
    env_pkg_dir = os.path.join(parent_dir, env_basename)
    env_archive_filename = f"{env_basename}.{archive_ext}"

    if os.path.exists(env_pkg_dir):
        _rmtree_robust(env_pkg_dir)

    _create_package_structure(env_pkg_dir)
    _embed_deployer(env_pkg_dir)
    payload_paths = write_environment_prereq_payload(env_pkg_dir, requirements)

    total_required_perm = sum(req.minimum_required_perm_bytes for req in requirements)
    env_manifest = BuildManifest(
        build_number=manifest.build_number,
        environment=manifest.environment,
        package_name=manifest.package_name,
        package_filename=env_archive_filename,
        timestamp=manifest.timestamp,
        author=manifest.author,
        description=(
            "Environment prerequisite package generated by SHIPS. "
            "DBA review is required before execution."
        ),
        source_commit=manifest.source_commit,
        source_dirty=manifest.source_dirty,
        token_count=0,
        file_count=0,
        phase_inventory={},
        tokens_resolved=dict(manifest.tokens_resolved),
        warnings=[
            "Environment parent database/user prerequisites require DBA review.",
            "Review context/prerequisites/create_missing_parents.review.sql before execution.",
        ],
        release_group=release_group,
        role="environment_prereqs",
        requires=[],
        discovery=dict(manifest.discovery),
        baseline_dir=manifest.baseline_dir,
        target_env=manifest.target_env,
        change_ref=manifest.change_ref,
        require_change_ref=manifest.require_change_ref,
        require_signature=manifest.require_signature,
        require_approvals=manifest.require_approvals,
        require_tls=manifest.require_tls,
        package_built_at=manifest.package_built_at,
        package_max_age_days=manifest.package_max_age_days,
        package_age_violation_level=manifest.package_age_violation_level,
        require_asymmetric_signature=manifest.require_asymmetric_signature,
        ships_public_key=manifest.ships_public_key,
    )
    from td_release_packager.trust import (
        STATUS_BLOCKED,
        TRUST_FAIL,
        TRUST_RESULT_REF,
        TrustReport,
        TrustSignal,
        write_trust_result,
    )

    env_trust_report = TrustReport(
        status=STATUS_BLOCKED,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        signals={
            "environment_prereq_requires_dba_review": TrustSignal(
                status=TRUST_FAIL,
                message=(
                    "Missing external parent database/user prerequisites were "
                    "detected. DBA review and execution evidence are required."
                ),
                issues=[req.parent_name for req in requirements],
                evidence_paths=["context/ships.build.json"],
            )
        },
    )

    env_manifest.phase_inventory = _compute_phase_inventory(env_pkg_dir)
    env_manifest.file_count = sum(env_manifest.phase_inventory.values())

    write_trust_result(env_pkg_dir, env_trust_report)
    env_manifest.trust = {"trust_ref": TRUST_RESULT_REF}

    from td_release_packager.actions import (
        ACTIONS_RESULT_REF,
        compute_actions_report,
        write_actions_result,
    )

    env_actions_report = compute_actions_report(
        trust=env_trust_report.to_dict(),
        role=env_manifest.role or "",
        has_dba_placeholders=has_dba_placeholders(env_pkg_dir),
    )
    write_actions_result(env_pkg_dir, env_actions_report)
    env_manifest.actions_ref = ACTIONS_RESULT_REF

    from td_release_packager.capabilities import (
        CAPABILITIES_RESULT_REF,
        compute_capabilities_report,
        write_capabilities_result,
    )

    env_capabilities = compute_capabilities_report(
        {
            "require_change_ref": env_manifest.require_change_ref,
            "require_signature": env_manifest.require_signature,
            "require_asymmetric_signature": env_manifest.require_asymmetric_signature,
            "require_approvals": env_manifest.require_approvals,
        }
    )
    write_capabilities_result(env_pkg_dir, env_capabilities)
    env_manifest.capabilities_ref = CAPABILITIES_RESULT_REF

    from td_release_packager.policy import (
        POLICY_RESULT_REF,
        compute_agent_policy,
        write_policy_result,
    )

    env_policy = compute_agent_policy(
        trust=env_trust_report.to_dict(),
        governance={
            "require_change_ref": env_manifest.require_change_ref,
            "require_signature": env_manifest.require_signature,
            "require_asymmetric_signature": env_manifest.require_asymmetric_signature,
            "require_approvals": env_manifest.require_approvals,
            "require_tls": env_manifest.require_tls,
        },
    )
    write_policy_result(env_pkg_dir, env_policy)
    env_manifest.policy_ref = POLICY_RESULT_REF

    manifest_path = _context_file(env_pkg_dir, "ships.build.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(env_manifest.__dict__, f, indent=2, ensure_ascii=False)

    # Minimal provenance document: this package carries generated DBA-reviewed
    # payload rather than user-authored source DDL.
    provenance_path = _context_file(env_pkg_dir, "ships.provenance.json")
    with open(provenance_path, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "entries": {}}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    write_environment_prereq_context(
        env_pkg_dir,
        requirements,
        release_group=release_group,
        package_filename=env_archive_filename,
        payload_paths=payload_paths,
    )
    write_context_artifacts(env_pkg_dir, env_manifest)
    _generate_deploy_script(env_pkg_dir, env_manifest)
    _generate_readme(env_pkg_dir, env_manifest)
    _generate_shell_wrappers(env_pkg_dir)

    from td_release_packager.package_report import generate_package_report

    generate_package_report(env_pkg_dir, env_manifest.__dict__)
    _generate_integrity_file(env_pkg_dir)
    archive = _archive_package(env_pkg_dir, archive_format)
    _generate_checksum(archive)

    logger.info(
        "Auto-split: environment prereqs → %s (%d missing parent(s), %s minimum PERM)",
        archive,
        len(requirements),
        total_required_perm,
    )
    instruction_path = os.path.join(
        env_pkg_dir,
        "context",
        "prerequisites",
        "DBA_INSTRUCTIONS.md",
    )
    payload_summary = (
        ", ".join(payload_paths) if payload_paths else "payload/01_pre_requisites/"
    )
    extracted_payload_summary = ", ".join(
        os.path.join(env_pkg_dir, p.replace("/", os.sep)) for p in payload_paths
    )
    if not extracted_payload_summary:
        extracted_payload_summary = os.path.join(
            env_pkg_dir, "payload", "01_pre_requisites"
        )
    print(
        "\n"
        "================================================================\n"
        "  Environment prerequisite package created but BLOCKED\n"
        "================================================================\n"
        f"  Package: {env_archive_filename}\n"
        f"  DBA instructions: {instruction_path}\n"
        f"  DBA must amend inside extracted package: {extracted_payload_summary}\n"
        f"  Package-local payload path(s): {payload_summary}\n"
        "  Do not edit the source project payload or the zip file directly.\n"
        "  Then run:\n"
        f'    python -m td_release_packager repackage --package-dir "{env_pkg_dir}" --strict\n'
        "================================================================\n"
    )
    return (archive, env_manifest)


def _split_into_paired_packages(
    pkg_dir: str,
    manifest: BuildManifest,
    archive_format: str,
    token_values: Optional[Dict[str, str]] = None,
) -> Tuple[Tuple[str, BuildManifest], Tuple[str, BuildManifest]]:
    """Partition a fully-built package into a prereqs + main pair.

    Both halves get a complete copy of the package infrastructure
    (config/, lib/, deploy.py, README.txt) so each is independently
    deployable. Only the payload phases are partitioned: the prereqs
    zip keeps ``00_system`` and ``01_pre_requisites`` and empties the
    main phases; the main zip does the inverse.

    Both context/ships.build.json manifests are rewritten to:
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
    release_group = os.path.basename(pkg_dir)

    # Keep the shared release identity at the start of both split archive
    # names, and put the deploy-order/role suffix at the end. This makes
    # Windows Explorer, shells, humans, and agents sort the pair together
    # in deployment order:
    #   <release_group>_01_prereqs.zip
    #   <release_group>_02_main.zip
    main_basename = f"{release_group}_02_main"
    prereqs_basename = f"{release_group}_01_prereqs"

    main_pkg_dir = os.path.join(parent_dir, main_basename)
    prereqs_pkg_dir = os.path.join(parent_dir, prereqs_basename)

    # The pre-split package directory is named with the release_group only.
    # Rename it to the final MAIN package directory before archiving so the
    # folder inside the zip and the zip filename both carry the _02_main role
    # suffix. The release_group itself remains the unsuffixed pair identity.
    #
    # os.rename() is not used here because on Windows it raises PermissionError
    # when the OS, antivirus, or search indexer holds a transient handle on the
    # directory that was just written.  copytree + _rmtree_robust is equivalent
    # and survives those transient holds via its retry loop.
    if os.path.exists(main_pkg_dir):
        _rmtree_robust(main_pkg_dir)
    if os.path.exists(prereqs_pkg_dir):
        _rmtree_robust(prereqs_pkg_dir)
    shutil.copytree(pkg_dir, main_pkg_dir, ignore=_package_copy_ignore)
    _rmtree_robust(pkg_dir)
    pkg_dir = main_pkg_dir

    # 1. Clone the main package wholesale → prereqs sibling. Then we
    #    selectively empty payload phases on each side.
    shutil.copytree(pkg_dir, prereqs_pkg_dir, ignore=_package_copy_ignore)

    # 2. Main: drop the prereq phases.
    for phase in _PREREQ_PHASES:
        _empty_phase_subtree(pkg_dir, phase)

    # 3. Prereqs: drop the dependant phases.
    for phase in _MAIN_PHASES:
        _empty_phase_subtree(prereqs_pkg_dir, phase)

    # 4. Recompute inventories for each half so context/ships.build.json reflects
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

    # 7. Detect external parent database/user dependencies and, when present,
    #    emit a _00_environment_prereqs package in the same release group.
    #
    # The application prereqs package contains CREATE DATABASE/USER statements
    # such as ``CREATE DATABASE CHILD FROM PARENT``.  If PARENT is neither
    # created by this package nor a known platform root (DBC), deployment will
    # fail before the child can be created.  Rather than leaving a loose DBA
    # script outside the audit trail, SHIPS emits a sibling package carrying
    # a review script and machine-readable requirement/evidence contract.
    # PR5a: feed env-config's EXTERNAL_PARENTS declaration through so
    # the build's environment-prereq gate knows about platform-owned
    # parents the package depends on but does not create itself.
    # Without this, every reverse-harvested product whose root sits
    # under a real database (e.g. DATAPRODUCTS) trips an
    # environment-prereqs package and forces a manual DBA amendment.
    # Union with the default DBC so we keep the existing safety net
    # for the universal platform root.
    from td_release_packager.environment_prereqs import (
        _DEFAULT_KNOWN_EXTERNAL_PARENTS,
        parse_external_parents_from_env,
    )

    declared_external_parents = (
        parse_external_parents_from_env(token_values) if token_values else set()
    )
    effective_external_parents = (
        set(_DEFAULT_KNOWN_EXTERNAL_PARENTS) | declared_external_parents
        if declared_external_parents
        else None
    )
    env_prereq_pair = _create_environment_prereqs_package_if_needed(
        prereqs_pkg_dir=prereqs_pkg_dir,
        manifest=manifest,
        release_group=release_group,
        archive_format=archive_format,
        known_external_parents=effective_external_parents,
    )
    if env_prereq_pair is not None:
        env_archive, _env_manifest = env_prereq_pair
        prereqs_manifest.requires = [os.path.basename(env_archive)]

    # 8. Re-write per-package metadata on both sides.
    #
    # The pre-split package report and provenance describe the union of
    # prereqs + main payload.  After the payload phases are partitioned,
    # regenerate every package-local metadata artefact that depends on the
    # physical payload contents.  Otherwise the prereqs report can list main
    # objects and create dead links to files that are not in that archive.
    from td_release_packager.package_report import generate_package_report

    for target_pkg_dir, target_manifest in (
        (pkg_dir, manifest),
        (prereqs_pkg_dir, prereqs_manifest),
    ):
        manifest_path = _context_file(target_pkg_dir, "ships.build.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(target_manifest.__dict__, f, indent=2, ensure_ascii=False)

        _filter_provenance_to_present_payload(target_pkg_dir)
        write_context_artifacts(target_pkg_dir, target_manifest)
        generate_package_report(target_pkg_dir, target_manifest.__dict__)

    # 8. Integrity fingerprints, then archive both. Prereqs first so
    #    the on-disk creation order matches the deploy order.
    _generate_integrity_file(prereqs_pkg_dir)
    prereqs_archive = _archive_package(prereqs_pkg_dir, archive_format)
    _generate_checksum(prereqs_archive)
    _generate_integrity_file(pkg_dir)
    main_archive = _archive_package(pkg_dir, archive_format)
    _generate_checksum(main_archive)

    group_archives: list[tuple[str, BuildManifest]] = []
    if env_prereq_pair is not None:
        group_archives.append(env_prereq_pair)
    group_archives.extend(
        [
            (prereqs_archive, prereqs_manifest),
            (main_archive, manifest),
        ]
    )
    _write_release_group_files(
        group_dir=parent_dir,
        release_group=release_group,
        manifests_and_archives=group_archives,
    )

    return ((main_archive, manifest), (prereqs_archive, prereqs_manifest))


def _archive_ext(archive_format: str) -> str:
    """Return the file extension used for an archive format."""
    return "tar.gz" if archive_format == "tar.gz" else "zip"


def _write_manifest_json(pkg_dir: str, manifest: BuildManifest) -> None:
    """Write the canonical package build manifest under context/."""
    manifest_path = _context_file(pkg_dir, "ships.build.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)


def _role_sequence(role: str) -> int:
    """Return the deploy-order sequence for a package role."""
    return {
        "environment_prereqs": 0,
        "prereqs": 1,
        "main": 2,
    }.get(role, 1)


def _checksum_filename_for(archive_path: str) -> str:
    """Return the checksum sidecar basename for an archive path."""
    return os.path.basename(archive_path) + ".sha256"


def _write_release_group_files(
    *,
    group_dir: str,
    release_group: str,
    manifests_and_archives: list[tuple[str, BuildManifest]],
) -> None:
    """Write release-group level manifest and README.

    The package-level context remains inside each archive.  This group-level
    file is intentionally small: it lets humans, CI/CD, and agents discover
    all sibling archives and their deploy order without scanning unrelated
    files in the wider releases directory.
    """
    ordered = sorted(
        manifests_and_archives,
        key=lambda item: (_role_sequence(item[1].role), os.path.basename(item[0])),
    )
    packages = []
    for archive_path, manifest in ordered:
        archive_name = os.path.basename(archive_path)
        packages.append(
            {
                "sequence": _role_sequence(manifest.role),
                "role": manifest.role or "main",
                "archive": archive_name,
                "checksum": _checksum_filename_for(archive_path),
                "context_entrypoint": f"{archive_name}!/context/ships.index.json",
                "requires": list(manifest.requires),
            }
        )

    group_doc = {
        "schema_version": "1.0",
        "release_group": release_group,
        "environment": ordered[0][1].environment if ordered else "",
        "package_name": ordered[0][1].package_name if ordered else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deploy_order": [pkg["archive"] for pkg in packages],
        "packages": packages,
    }

    os.makedirs(group_dir, exist_ok=True)
    group_manifest_path = os.path.join(group_dir, "release_group.json")
    with open(group_manifest_path, "w", encoding="utf-8") as f:
        json.dump(group_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")

    readme_path = os.path.join(group_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(f"SHIPS release group: {release_group}\n")
        f.write("=" * (22 + len(release_group)) + "\n\n")
        f.write("Deploy packages in this order:\n\n")
        for pkg in packages:
            f.write(f"  {pkg['sequence']}. {pkg['archive']} ({pkg['role']})\n")
        f.write(
            "\nEach package is self-contained and has its own context/ships.index.json.\n"
        )
        f.write(
            "\nConvenience launcher (no manual extraction required):\n"
            "  python deploy_release.py --host <teradata_host> --user <username>\n"
        )

    launcher_path = os.path.join(group_dir, "deploy_release.py")
    launcher = '''#!/usr/bin/env python3
"""Deploy this SHIPS release group without manual archive extraction."""

import os
import subprocess
import sys


def main() -> int:
    group_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [
        sys.executable,
        "-m",
        "td_release_packager",
        "deploy",
        group_dir,
        *sys.argv[1:],
    ]
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''
    with open(launcher_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(launcher)
    os.chmod(launcher_path, 0o755)


def _finalize_single_package(
    pkg_dir: str,
    manifest: BuildManifest,
    archive_format: str,
) -> Tuple[str, BuildManifest]:
    """Finalize and archive a one-package release group.

    Even when there is only one package, SHIPS now writes it under a release
    group directory and uses the explicit ``_01_main`` role suffix.  This keeps
    the filesystem contract consistent with multi-package release groups.
    """
    group_dir = os.path.dirname(pkg_dir)
    release_group = os.path.basename(pkg_dir)
    archive_ext = _archive_ext(archive_format)
    main_basename = f"{release_group}_01_main"
    main_pkg_dir = os.path.join(group_dir, main_basename)
    main_archive_filename = f"{main_basename}.{archive_ext}"

    if os.path.exists(main_pkg_dir):
        _rmtree_robust(main_pkg_dir)
    shutil.copytree(pkg_dir, main_pkg_dir, ignore=_package_copy_ignore)
    _rmtree_robust(pkg_dir)

    manifest.package_filename = main_archive_filename
    manifest.release_group = release_group
    manifest.role = "main"
    manifest.requires = []
    manifest.phase_inventory = _compute_phase_inventory(main_pkg_dir)
    manifest.file_count = sum(manifest.phase_inventory.values())

    _write_manifest_json(main_pkg_dir, manifest)
    write_context_artifacts(main_pkg_dir, manifest)

    from td_release_packager.package_report import generate_package_report

    generate_package_report(main_pkg_dir, manifest.__dict__)
    _generate_integrity_file(main_pkg_dir)
    archive_path = _archive_package(main_pkg_dir, archive_format)
    _generate_checksum(archive_path)
    _write_release_group_files(
        group_dir=group_dir,
        release_group=release_group,
        manifests_and_archives=[(archive_path, manifest)],
    )
    return archive_path, manifest


def _build_manifest_from_dict(data: dict) -> BuildManifest:
    """Create a BuildManifest from a JSON dictionary, ignoring unknown keys."""
    from dataclasses import fields

    allowed = {field.name for field in fields(BuildManifest)}
    return BuildManifest(
        **{key: value for key, value in data.items() if key in allowed}
    )


def _package_manifest_path(pkg_dir: str) -> str:
    """Return the canonical manifest path for an extracted package root."""
    return os.path.join(pkg_dir, CONTEXT_DIR, "ships.build.json")


def _find_package_root_ancestor(path: str) -> str | None:
    """Find the nearest ancestor that looks like an extracted package root."""
    current = os.path.abspath(path)
    while True:
        if os.path.isfile(_package_manifest_path(current)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _resolve_repackage_package_dir(package_dir: str) -> str:
    """Resolve a user-supplied repackage path to the extracted package root."""
    package_dir = os.path.abspath(package_dir)
    package_root = _find_package_root_ancestor(package_dir)
    return package_root or package_dir


def _repackage_output_group_dir(package_dir: str, manifest: BuildManifest) -> str:
    """Return the release-group directory that should receive the archive."""
    package_parent = os.path.dirname(package_dir)
    if os.path.basename(package_parent).lower() == ".ships-work":
        release_group_dir = os.path.dirname(package_parent)
        if (
            not manifest.release_group
            or os.path.basename(release_group_dir) == manifest.release_group
        ):
            return release_group_dir
    return package_parent


def _read_manifest_from_package_dir(pkg_dir: str) -> BuildManifest:
    """Read context/ships.build.json from an extracted package directory."""
    manifest_path = _package_manifest_path(pkg_dir)
    if not os.path.isfile(manifest_path):
        package_root = _find_package_root_ancestor(pkg_dir)
        hint = ""
        if package_root:
            hint = f" Use the extracted package root instead: {package_root}."
        raise FileNotFoundError(
            f"Package manifest not found: {manifest_path}. "
            "Expected an extracted SHIPS package directory containing "
            f"{CONTEXT_DIR}/ships.build.json, not a payload subdirectory."
            f"{hint}"
        )
    with open(manifest_path, "r", encoding="utf-8") as f:
        return _build_manifest_from_dict(json.load(f))


def _read_manifest_from_archive(archive_path: str) -> BuildManifest | None:
    """Read context/ships.build.json from a sibling zip archive if possible."""
    import zipfile

    if not archive_path.lower().endswith(".zip") or not os.path.isfile(archive_path):
        return None
    try:
        with zipfile.ZipFile(archive_path) as zf:
            matches = [
                name
                for name in zf.namelist()
                if name.replace("\\", "/").endswith("/context/ships.build.json")
            ]
            if not matches:
                return None
            with zf.open(matches[0]) as fh:
                return _build_manifest_from_dict(json.loads(fh.read().decode("utf-8")))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def _remove_transient_python_cache(root_dir: str) -> None:
    """Remove Python runtime cache artefacts before integrity/archive work."""
    for current_root, dirs, files in os.walk(root_dir):
        for dirname in list(dirs):
            if dirname == "__pycache__":
                _rmtree_robust(os.path.join(current_root, dirname))
                dirs.remove(dirname)
        for filename in files:
            if filename.endswith((".pyc", ".pyo")):
                try:
                    os.remove(os.path.join(current_root, filename))
                except OSError:
                    pass


def _refresh_environment_prereq_trust(pkg_dir: str, manifest: BuildManifest) -> None:
    """Refresh trust state for a reviewed environment prerequisite package."""
    if manifest.role != "environment_prereqs":
        return

    from td_release_packager.trust import (
        STATUS_BLOCKED,
        STATUS_CAVEATS,
        TRUST_FAIL,
        TRUST_RESULT_REF,
        TRUST_WARN,
        TrustReport,
        TrustSignal,
        write_trust_result,
    )

    now = datetime.now(timezone.utc).isoformat()
    if has_dba_placeholders(pkg_dir):
        report = TrustReport(
            status=STATUS_BLOCKED,
            evaluated_at=now,
            signals={
                "environment_prereq_requires_dba_values": TrustSignal(
                    status=TRUST_FAIL,
                    message=(
                        "DBA placeholders remain in environment prerequisite "
                        "payload/context. Replace <DBA_SELECTED_PARENT> and "
                        "<DBA_REVIEWED_PERM>, then repackage."
                    ),
                    evidence_paths=["payload/", "context/ships.build.json"],
                )
            },
        )
    else:
        report = TrustReport(
            status=STATUS_CAVEATS,
            evaluated_at=now,
            signals={
                "environment_prereq_dba_reviewed": TrustSignal(
                    status=TRUST_WARN,
                    message=(
                        "Environment prerequisite payload no longer contains DBA "
                        "placeholders. Deploy only after DBA approval and target "
                        "preflight verification."
                    ),
                    evidence_paths=["payload/", "context/ships.build.json"],
                )
            },
        )

    write_trust_result(pkg_dir, report)
    manifest.trust = {"trust_ref": TRUST_RESULT_REF}

    from td_release_packager.actions import (
        ACTIONS_RESULT_REF,
        compute_actions_report,
        write_actions_result,
    )

    refreshed_actions = compute_actions_report(
        trust=report.to_dict(),
        role=manifest.role or "",
        has_dba_placeholders=has_dba_placeholders(pkg_dir),
    )
    write_actions_result(pkg_dir, refreshed_actions)
    manifest.actions_ref = ACTIONS_RESULT_REF

    from td_release_packager.capabilities import (
        CAPABILITIES_RESULT_REF,
        compute_capabilities_report,
        write_capabilities_result,
    )

    refreshed_capabilities = compute_capabilities_report(
        {
            "require_change_ref": manifest.require_change_ref,
            "require_signature": manifest.require_signature,
            "require_asymmetric_signature": manifest.require_asymmetric_signature,
            "require_approvals": manifest.require_approvals,
        }
    )
    write_capabilities_result(pkg_dir, refreshed_capabilities)
    manifest.capabilities_ref = CAPABILITIES_RESULT_REF

    from td_release_packager.policy import (
        POLICY_RESULT_REF,
        compute_agent_policy,
        write_policy_result,
    )

    refreshed_policy = compute_agent_policy(
        trust=report.to_dict(),
        governance={
            "require_change_ref": manifest.require_change_ref,
            "require_signature": manifest.require_signature,
            "require_asymmetric_signature": manifest.require_asymmetric_signature,
            "require_approvals": manifest.require_approvals,
            "require_tls": manifest.require_tls,
        },
    )
    write_policy_result(pkg_dir, refreshed_policy)
    manifest.policy_ref = POLICY_RESULT_REF


def _collect_release_group_archives(
    group_dir: str, current_archive: str, current_manifest: BuildManifest
) -> list[tuple[str, BuildManifest]]:
    """Collect current and sibling package archives for release_group.json."""
    collected: dict[str, tuple[str, BuildManifest]] = {
        os.path.basename(current_archive): (current_archive, current_manifest)
    }
    release_group = current_manifest.release_group or os.path.basename(group_dir)
    for filename in os.listdir(group_dir):
        if not filename.startswith(release_group) or not filename.endswith(".zip"):
            continue
        path = os.path.join(group_dir, filename)
        if os.path.abspath(path) == os.path.abspath(current_archive):
            continue
        manifest = _read_manifest_from_archive(path)
        if manifest is not None:
            collected[filename] = (path, manifest)
    return list(collected.values())


def repackage_package_dir(
    package_dir: str, *, strict: bool = False
) -> tuple[str, BuildManifest]:
    """Repackage an edited extracted SHIPS package directory.

    This is intended for DBA-reviewed _00_environment_prereqs packages. It
    recalculates package-local metadata after the DBA edits generated payload,
    recreates the archive/checksum, and refreshes the release-group manifest.

    Args:
        package_dir: Extracted package directory to repackage.
        strict: If True, raise ValueError when DBA placeholders remain.

    Returns:
        Tuple of archive path and refreshed BuildManifest.
    """
    package_dir = _resolve_repackage_package_dir(package_dir)
    if not os.path.isdir(package_dir):
        raise FileNotFoundError(f"Package directory does not exist: {package_dir}")

    manifest = _read_manifest_from_package_dir(package_dir)
    _remove_transient_python_cache(package_dir)
    manifest.phase_inventory = _compute_phase_inventory(package_dir)
    manifest.file_count = sum(manifest.phase_inventory.values())
    manifest.package_built_at = datetime.now(timezone.utc).isoformat()

    _refresh_environment_prereq_trust(package_dir, manifest)
    from td_release_packager.trust import STATUS_BLOCKED, load_trust_result

    refreshed_trust = load_trust_result(package_dir) or {}
    if strict and refreshed_trust.get("status") == STATUS_BLOCKED:
        placeholders = find_dba_placeholders(package_dir)
        details = ""
        if placeholders:
            rendered = "\n".join(
                f"  - {path}:{line_no} contains {marker}"
                for path, line_no, marker in placeholders[:10]
            )
            remaining = len(placeholders) - 10
            if remaining > 0:
                rendered += f"\n  - ... and {remaining} more"
            details = f"\nExecutable placeholders still present:\n{rendered}"
        raise ValueError(
            "Package remains BLOCKED. Replace DBA placeholders in the generated "
            "environment prerequisite payload, then run repackage again."
            f"{details}"
        )

    _write_manifest_json(package_dir, manifest)
    write_context_artifacts(package_dir, manifest)

    from td_release_packager.package_report import generate_package_report

    generate_package_report(package_dir, manifest.__dict__)
    _generate_integrity_file(package_dir)

    group_dir = _repackage_output_group_dir(package_dir, manifest)
    archive_format = (
        "tar.gz" if manifest.package_filename.endswith(".tar.gz") else "zip"
    )
    archive_path = os.path.join(group_dir, manifest.package_filename)
    if os.path.exists(archive_path):
        os.remove(archive_path)
    checksum_path = archive_path + ".sha256"
    if os.path.exists(checksum_path):
        os.remove(checksum_path)

    local_archive_path = _archive_package(package_dir, archive_format)
    if os.path.abspath(local_archive_path) != os.path.abspath(archive_path):
        shutil.move(local_archive_path, archive_path)
    else:
        archive_path = local_archive_path
    _generate_checksum(archive_path)

    release_group = manifest.release_group or os.path.basename(group_dir)
    _write_release_group_files(
        group_dir=group_dir,
        release_group=release_group,
        manifests_and_archives=_collect_release_group_archives(
            group_dir, archive_path, manifest
        ),
    )
    return archive_path, manifest


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

    Directories created:
      lib/               — embedded database_package_deployer library
      payload/00_system  — system-scope objects (Maps, Roles, etc.)
      payload/01_pre_requisites — CREATE DATABASE / CREATE USER
      payload/02_dcl     — Grants and Revokes
      payload/03_ddl     — Tables, Views, Procedures, Macros, etc.
      payload/04_dml     — INSERT / UPDATE / DELETE / MERGE scripts

    Note: config/ and logs/ are NOT created inside the package.
      - config/ belongs to the SHIPS project, not the release artefact.
        Token values are already resolved into the payload; there is
        nothing environment-specific left to configure at deploy time.
      - logs/ is written by the deployer at runtime to the operator's
        own working directory, not inside the (potentially read-only)
        extracted package.

    Args:
        pkg_dir: Root of the package being built.
    """
    dirs = [
        "lib",
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

    # PR1 invariant: deterministic copy order. Sort both the directory
    # walk (in-place to influence os.walk's own descent) and the file
    # iteration so the order files are copied, tokens substituted, and
    # the provenance document is built is byte-stable run-to-run.
    for root, dirs, files in os.walk(source_payload):
        dirs.sort()
        for filename in sorted(files):
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
                # is recorded as it runs so the v2 ships.provenance.json
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


def _backfill_missing_inferred_dcl(pkg_dir: str) -> int:
    """Write missing inferred inter-database grant DCL into a package.

    This is deliberately package-local. It reads the resolved SQL in the
    package staging directory and writes only missing generated grants under
    ``payload/02_dcl/inter_db``. Existing .dcl files are left untouched so
    hand-authored DCL remains authoritative.
    """
    from td_release_packager.infer_grants import (
        generate_grt_content,
        grantee_filename,
    )
    from td_release_packager.validate_grants import _infer_expected_grants

    package_dir = Path(pkg_dir)
    dcl_dir = package_dir / "payload" / DeployPhase.DCL.value / "inter_db"
    consolidated, raw_results, _ddl_count = _infer_expected_grants(package_dir)
    if not consolidated:
        return 0

    written = 0
    for grantee in sorted(consolidated):
        target = dcl_dir / grantee_filename(grantee)
        if target.exists():
            continue

        sources = [result for result in raw_results if result["grantee"] == grantee]
        content = generate_grt_content(
            grantee,
            consolidated[grantee],
            sources,
            project_name=package_dir.name,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1

    if written:
        logger.info(
            "Generated %d inferred DCL grant file(s) into package payload",
            written,
        )
    return written


def _backfill_missing_role_ddl(pkg_dir: str) -> int:
    """Write missing ``CREATE ROLE`` scripts for role grantees in package DCL.

    The source project may contain hand-authored grants under
    ``payload/database/DCL/roles`` or ``payload/database/DCL/users`` without
    companion role DDL.  Package builds must be self-contained, so materialise
    those role objects in the package's system phase before any DCL executes.
    """
    from td_release_packager.validate_grants import _role_grantees_in_file

    package_dir = Path(pkg_dir)
    dcl_root = package_dir / "payload" / DeployPhase.DCL.value
    dcl_dirs = [dcl_root / "roles", dcl_root / "users"]

    roles = set()
    for dcl_dir in dcl_dirs:
        if not dcl_dir.is_dir():
            continue
        for entry in sorted(dcl_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() in {".dcl", ".grt"}:
                roles.update(_role_grantees_in_file(entry))

    if not roles:
        return 0

    role_dir = package_dir / "payload" / DeployPhase.SYSTEM.value / "roles"
    role_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for role in sorted(roles):
        role_file = role_dir / f"{role}.rol"
        if role_file.exists():
            continue
        role_file.write_text(f"CREATE ROLE {role};\n", encoding="utf-8")
        written += 1

    if written:
        logger.info(
            "Generated %d inferred role DDL file(s) into package payload",
            written,
        )
    return written


def _ensure_system_role_waves_include_package_roles(pkg_dir: str) -> None:
    """Ensure package-generated role files are present in system waves.

    Project-level ``_waves.txt`` can pre-date package-local generated role
    files.  When that happens, deploy follows the stale wave file and never
    executes the generated ``CREATE ROLE`` scripts.  Add any missing
    ``roles/*.rol`` entries to wave 1 so roles exist before DCL phases run.
    """
    system_dir = Path(pkg_dir) / "payload" / DeployPhase.SYSTEM.value
    roles_dir = system_dir / "roles"
    if not roles_dir.is_dir():
        return

    role_entries = [
        f"roles/{path.name}"
        for path in sorted(roles_dir.glob("*.rol"), key=lambda p: p.name.lower())
    ]
    if not role_entries:
        return

    waves_file = system_dir / "_waves.txt"
    if not waves_file.exists():
        header = [
            "# _waves.txt - package-local system wave file",
            "# Generated by SHIPS package build for system roles.",
            "",
            "# Wave 1: roles",
            *role_entries,
            "",
        ]
        waves_file.write_text("\n".join(header), encoding="utf-8")
        return

    lines = waves_file.read_text(encoding="utf-8").splitlines()
    listed = {
        line.strip().replace("\\", "/")
        for line in lines
        if line.strip() and not line.strip().startswith("#") and line.strip() != "---"
    }
    missing = [entry for entry in role_entries if entry not in listed]
    if not missing:
        return

    insert_at = next(
        (idx for idx, line in enumerate(lines) if line.strip() == "---"),
        len(lines),
    )
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1

    updated = lines[:insert_at] + missing + lines[insert_at:]
    waves_file.write_text("\n".join(updated) + "\n", encoding="utf-8")
    logger.info(
        "Package: added %d generated role file(s) to payload/%s/_waves.txt",
        len(missing),
        DeployPhase.SYSTEM.value,
    )


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
    # PR1 invariant: stable walk order so the sequence of copy2 calls
    # is run-to-run identical. _order.txt files themselves are the
    # control for downstream deploy order, so any reordering during
    # the copy stage that affected which file wins a collision would
    # be a silent semantic change.
    for root, dirs, files in os.walk(source_payload):
        dirs.sort()
        for filename in sorted(files):
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
    shutil.copytree(deployer_src, dest, ignore=_package_copy_ignore)

    logger.debug("Embedded database_package_deployer from %s", deployer_src)


# ---------------------------------------------------------------
# Internal — Generated files
# ---------------------------------------------------------------


def _generate_deploy_script(pkg_dir: str, manifest: BuildManifest):
    """
    Generate deploy.py — the DBA's single entry point.

    This script bootstraps the embedded database_package_deployer, reads the
    context/ships.build.json for context, and orchestrates the deployment with
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


def _has_package_metadata(path):
    """Return True when a directory looks like a SHIPS package root."""
    return os.path.isfile(os.path.join(path, "context", "ships.build.json"))


def _normalise_package_dir(path):
    """Return the actual package root, tolerating double-nested extraction.

    Some unzip workflows create <pkg>/<pkg>/... rather than <pkg>/... .
    The deployer should still find package metadata and payload in that layout.
    """
    if not path:
        return None

    path = os.path.abspath(path)
    if _has_package_metadata(path):
        return path

    nested = os.path.join(path, os.path.basename(path))
    if _has_package_metadata(nested):
        return nested

    return None


def _release_group_candidates(current_package_dir):
    """Yield likely release group directories for companion package lookup."""
    current_package_dir = os.path.abspath(current_package_dir)
    seen = set()

    candidates = [os.path.dirname(current_package_dir)]

    # Double-nested extraction: <release_group>/<pkg>/<pkg>.
    parent = os.path.dirname(current_package_dir)
    if os.path.basename(parent) == os.path.basename(current_package_dir):
        candidates.append(os.path.dirname(parent))

    # Release-group zip layout: package directory name starts with the group id.
    build_json = os.path.join(current_package_dir, "context", "ships.build.json")
    if os.path.exists(build_json):
        try:
            with open(build_json, encoding="utf-8") as _f:
                build_data = json.load(_f)
            release_group = build_data.get("release_group")
            if release_group:
                probe = current_package_dir
                for _ in range(4):
                    if os.path.basename(probe) == release_group:
                        candidates.append(probe)
                        break
                    probe = os.path.dirname(probe)
        except Exception:
            pass

    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if candidate not in seen:
            seen.add(candidate)
            yield candidate


def _find_companion_package(current_package_dir, required_archive_name):
    """Find a required companion package as a release-group sibling.

    Supports extracted package directories, double-nested extracted directories,
    and a sibling zip file with the required archive name.
    """
    required_base = os.path.splitext(os.path.basename(required_archive_name))[0]

    for release_group_dir in _release_group_candidates(current_package_dir):
        candidates = [
            os.path.join(release_group_dir, required_base),
            os.path.join(release_group_dir, required_base, required_base),
        ]

        for candidate in candidates:
            package_root = _normalise_package_dir(candidate)
            if package_root:
                return package_root

        zip_candidate = os.path.join(release_group_dir, required_archive_name)
        if os.path.isfile(zip_candidate):
            return zip_candidate

    return None


def main():
    """Main deployment entry point for the DBA."""
    args = parse_args()

    # -- Configure logging --
    log_dir = os.path.join(SCRIPT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"deploy_{{ts}}.log")

    log_level = logging.DEBUG if args.verbose else logging.INFO
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING if args.quiet else log_level)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler, console_handler],
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
    # Read the trust block computed at build time and surface the label and
    # per-signal status before any database connection is opened.
    #
    # BLOCKED packages are normally rejected here.  One signal is handled
    # differently: ``environment_prereq_requires_dba_review``.  That signal
    # fires when SHIPS detected parent databases/users that were not in the
    # package at build time, because it cannot know whether those objects
    # already exist in the target environment.  If they *do* exist the block
    # is vacuously satisfied, so the deployer defers the exit, establishes a
    # connection, and queries DBC to verify existence.  All other BLOCKED
    # signals are still hard exits.
    _trust_json = os.path.join(SCRIPT_DIR, "context", "ships.trust.json")
    _prereq_objects_to_verify: list[str] = []   # populated when deferring
    if os.path.exists(_trust_json):
        with open(_trust_json, encoding="utf-8") as _f:
            _trust = json.load(_f)
        if _trust:
            _status = _trust.get("status", "UNKNOWN")
            _icons = {{"READY": "\\u2713", "READY_WITH_CAVEATS": "\\u26a0", "BLOCKED": "\\u2717"}}
            _licon = _icons.get(_status, "?")
            logger.info("=" * 64)
            logger.info("  Package Trust: %s %s", _licon, _status)
            for _sname, _sig in _trust.get("signals", {{}}).items():
                _sicons = {{"pass": "\\u2713", "warn": "\\u26a0", "fail": "\\u2717", "unknown": "?"}}
                _sicon = _sicons.get(_sig.get("status", "unknown"), "?")
                logger.info("  %s %-28s %s", _sicon, _sname, _sig.get("message", ""))
            logger.info("=" * 64)
            if _status == "BLOCKED":
                _signals = _trust.get("signals", {{}})
                _blocking = [
                    _sname for _sname, _sig in _signals.items()
                    if _sig.get("status") == "fail"
                ]
                _is_prereq_only = (
                    _blocking == ["environment_prereq_requires_dba_review"]
                )
                if _is_prereq_only and not args.dry_run:
                    # Sole blocking signal is the environment prereq check.
                    # Capture the listed objects and defer the hard exit until
                    # after the database connection is established.
                    _prereq_objects_to_verify = list(
                        _signals["environment_prereq_requires_dba_review"].get("issues", [])
                    )
                    logger.info(
                        "Package is BLOCKED on environment_prereq_requires_dba_review. "
                        "Will verify %d object(s) exist in the target database before "
                        "proceeding: %s",
                        len(_prereq_objects_to_verify),
                        ", ".join(_prereq_objects_to_verify),
                    )
                else:
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

        # -- Live verification of deferred environment prereq objects --
        # Performed immediately after the connection is established so that
        # a clear, prominent outcome is logged before any further work.
        if _prereq_objects_to_verify:
            logger.info("=" * 64)
            logger.info(
                "  TRUST RESOLUTION — environment_prereq_requires_dba_review"
            )
            logger.info(
                "  Package was BLOCKED at build time because the following"
            )
            logger.info(
                "  parent database(s)/user(s) were not in the package payload."
            )
            logger.info(
                "  Querying %s to verify they exist now.", args.host
            )
            logger.info("=" * 64)
            _missing_prereqs: list[str] = []
            _verified_prereqs: list[str] = []
            for _obj_name in _prereq_objects_to_verify:
                try:
                    _cur = cursor.cursor() if hasattr(cursor, "cursor") else cursor
                    _cur.execute(
                        "SELECT 1 FROM DBC.DatabasesV WHERE DatabaseName = ?"
                        " UNION ALL "
                        "SELECT 1 FROM DBC.UsersV WHERE UserName = ?",
                        [_obj_name, _obj_name],
                    )
                    _row = _cur.fetchone()
                    if _row:
                        logger.info("  \\u2713 VERIFIED   %s exists in target", _obj_name)
                        _verified_prereqs.append(_obj_name)
                    else:
                        logger.error("  \\u2717 MISSING    %s does not exist in target", _obj_name)
                        _missing_prereqs.append(_obj_name)
                except Exception as _ve:
                    logger.error(
                        "  \\u2717 ERROR      Could not verify %s: %s", _obj_name, _ve
                    )
                    _missing_prereqs.append(_obj_name)
            logger.info("=" * 64)
            if _missing_prereqs:
                logger.error(
                    "BLOCKED signal NOT resolved: %d of %d prerequisite object(s) "
                    "do not exist in the target database.\\n"
                    "  Missing: %s\\n"
                    "  Deploy the companion _00_environment_prereqs package first, "
                    "or create the missing objects manually.",
                    len(_missing_prereqs),
                    len(_prereq_objects_to_verify),
                    ", ".join(_missing_prereqs),
                )
                sys.exit(1)
            else:
                logger.info(
                    "TRUST RESOLVED — All %d prerequisite object(s) verified present "
                    "in target database. The build-time BLOCKED signal "
                    "environment_prereq_requires_dba_review is satisfied by live "
                    "verification. Deployment authorised.",
                    len(_verified_prereqs),
                )
            logger.info("=" * 64)

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
    # When this package's context/ships.build.json has a non-empty ``requires`` list, a
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
    build_json_path = os.path.join(SCRIPT_DIR, "context", "ships.build.json")
    requires = []
    if os.path.exists(build_json_path):
        with open(build_json_path, encoding="utf-8") as _f:
            _build_data = json.load(_f)
        requires = _build_data.get("requires", [])

    if requires and not args.dry_run:
        _prereqs_zip_name = requires[0]
        _prereqs_basename = os.path.splitext(os.path.basename(_prereqs_zip_name))[0]
        _prereqs_location = _find_companion_package(SCRIPT_DIR, _prereqs_zip_name)

        if not _prereqs_location:
            _searched = []
            for _rg in _release_group_candidates(SCRIPT_DIR):
                _searched.extend([
                    os.path.join(_rg, _prereqs_basename),
                    os.path.join(_rg, _prereqs_basename, _prereqs_basename),
                    os.path.join(_rg, _prereqs_zip_name),
                ])
            logger.error(
                "Deploy chaining: companion prereqs package not found.\\n"
                "  Required: %s\\n"
                "  Searched:\\n    %s\\n"
                "  Extract '%s' as a sibling under the release group directory and retry.",
                _prereqs_zip_name,
                "\\n    ".join(_searched),
                _prereqs_zip_name,
            )
            sys.exit(1)

        if os.path.isfile(_prereqs_location) and _prereqs_location.lower().endswith(".zip"):
            logger.error(
                "Deploy chaining: companion prereqs package is present only as a zip: %s\\n"
                "  Extract it under the release group directory before deploying this package.",
                _prereqs_location,
            )
            sys.exit(1)

        _prereqs_dir = _normalise_package_dir(_prereqs_location)
        if not _prereqs_dir:
            logger.error(
                "Deploy chaining: companion prereqs package has no context/ships.build.json: %s",
                _prereqs_location,
            )
            sys.exit(1)

        logger.info("=" * 64)
        logger.info("  Deploy chaining — companion prereqs")
        logger.info("  Prereqs: %s", _prereqs_basename)
        logger.info("  Path:    %s", _prereqs_dir)
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
    serial_prefix_files = []
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

        if phase_dir_name == "00_system":
            # System-scope artefacts are intentionally kept outside the wave
            # executor.  Roles, profiles, maps, authorizations, foreign
            # servers, and similar catalogue-level objects must complete
            # serially before any dependency wave grants/DDL can start.
            if os.path.exists(order_file):
                phase_files = read_order_file(order_file, phase_path)
            else:
                phase_files = discover_files(phase_path)
            if phase_files:
                serial_prefix_files.extend(phase_files)
                all_files.extend(phase_files)
                logger.info("  %d serial system object(s)", len(phase_files))
            continue

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
            if phase_files:
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
                ordered_files=all_files if not use_waves else serial_prefix_files,
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
            if args.quiet:
                print("✓ EXPLAIN validation %s" % status.lower())
                print("  %d passed · %d failed · %d not applicable" % (
                    result.completed,
                    result.failed,
                    result.skipped,
                ))
                if result.report_path:
                    print("  Report: %s" % result.report_path)
                print("  Log:    %s" % log_file)

            sys.exit(0 if result.failed == 0 else 1)

        elif use_waves:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                ordered_files=serial_prefix_files,
                waves=all_waves,
                num_streams=num_streams,
                connect_fn=make_cursor if num_streams > 1 else None,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
                table_trigger_action="recreate" if args.recreate_table_triggers else "fail",
            )
        else:
            result = deploy_package(
                cursor=cursor,
                package_dir=os.path.join(SCRIPT_DIR, "logs"),
                ordered_files=all_files,
                stop_on_failure=not args.continue_on_error,
                dry_run=args.dry_run,
                skip_preflight=no_connection,
                table_trigger_action="recreate" if args.recreate_table_triggers else "fail",
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
        if args.quiet:
            icon = "✓" if result.success else "✗"
            print("%s Deployment %s" % (icon, status.lower()))
            print("  %d total · %d deployed · %d skipped · %d failed" % (
                result.total,
                result.completed,
                result.skipped,
                result.failed,
            ))
            if result.report_path:
                print("  Report: %s" % result.report_path)
            print("  Log:    %s" % log_file)

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

    Recomputes SHA-256 over every file under payload/ and lib/, derives
    the combined package_hash, and compares against ships.integrity.json.
    Aborts the process on any mismatch.

    Including lib/ means an attacker who edits the embedded deployer code
    (e.g. preflight.py) to bypass security checks will be detected here.

    __pycache__ directories and .pyc/.pyo bytecode files are excluded —
    Python regenerates them on first import after extraction and must
    never be treated as package content.

    Returns the package_hash string so callers can embed it in the
    query band.  Returns 'SKIPPED' when --skip-integrity-check is set.
    """
    integrity_file = os.path.join(script_dir, "context", "ships.integrity.json")

    if skip:
        logger.warning("Integrity check SKIPPED (--skip-integrity-check).")
        return "SKIPPED"

    if not os.path.exists(integrity_file):
        logger.error(
            "INTEGRITY CHECK FAILED: context/ships.integrity.json not found — "
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

    _EXCLUDE_DIRS = {{"__pycache__"}}
    _EXCLUDE_SUFFIXES = {{".pyc", ".pyo"}}

    computed_files = {{}}
    payload_dir = os.path.join(script_dir, "payload")
    for root, dirs, files in os.walk(payload_dir):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS)
        for fname in sorted(files):
            if os.path.splitext(fname)[1] in _EXCLUDE_SUFFIXES:
                continue
            fpath = os.path.join(root, fname)
            rel = pathlib.Path(os.path.relpath(fpath, script_dir)).as_posix()
            with open(fpath, "rb") as fh:
                computed_files[rel] = hashlib.sha256(fh.read()).hexdigest()

    lib_dir = os.path.join(script_dir, "lib")
    if os.path.isdir(lib_dir):
        for root, dirs, files in os.walk(lib_dir):
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS)
            for fname in sorted(files):
                if os.path.splitext(fname)[1] in _EXCLUDE_SUFFIXES:
                    continue
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
    params = {{"host": args.host, "user": args.user}}
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
    p.add_argument("--recreate-table-triggers", action="store_true",
                   help="For existing tables with defined triggers, SHOW and "
                        "DROP the triggers, perform the table replacement, "
                        "then recreate the triggers from SHOW output. "
                        "Default is to fail and report the blockers.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging.")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Show a compact console summary; full logs are still written to logs/.")
    p.add_argument("-V", "--version", action="version",
                   version="deploy.py {manifest.build_number}",
                   help="Show version and exit.")
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

    if args.verbose and args.quiet:
        p.error("--verbose and --quiet are mutually exclusive")

    return args


if __name__ == "__main__":
    main()
'''

    deploy_path = os.path.join(pkg_dir, "deploy.py")
    with open(deploy_path, "w", encoding="utf-8") as f:
        f.write(script)

    # Make executable
    os.chmod(deploy_path, 0o755)


def _environment_prereq_payload_paths(pkg_dir: str) -> list[str]:
    """Return generated environment prerequisite payload paths relative to package root."""
    root = os.path.join(pkg_dir, "payload", "01_pre_requisites")
    if not os.path.isdir(root):
        return []
    paths: list[str] = []
    for current_root, _dirs, files in os.walk(root):
        for filename in files:
            if filename.lower().endswith((".db", ".usr")):
                full_path = os.path.join(current_root, filename)
                paths.append(os.path.relpath(full_path, pkg_dir).replace(os.sep, "/"))
    return sorted(paths)


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
  READ FIRST
================================================================

  Agents, CI/CD jobs, MCP clients and operators should read:

    context/ships.index.json

  context/ships.index.json is the canonical package entrypoint. It lists
  the SHIPS metadata files, describes each file, gives the recommended
  read order, and carries agent instructions for safe downstream action.

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

    if manifest.role == "environment_prereqs":
        payload_paths = _environment_prereq_payload_paths(pkg_dir)
        payload_listing = "\n".join(f"    {path}" for path in payload_paths)
        if not payload_listing:
            payload_listing = (
                "    payload/01_pre_requisites/databases/<missing_parent>.db"
            )
        readme += f"""
================================================================
  ACTION REQUIRED — DBA REVIEW NEEDED
================================================================

  This _00_environment_prereqs package is generated for missing
  platform/environment parent databases or users. It is BLOCKED
  until a DBA reviews and amends generated payload placeholders.

  Read the DBA instructions here:

    context/prerequisites/DBA_INSTRUCTIONS.md

  DBA must amend the generated payload file(s) inside this extracted
  _00_environment_prereqs package, not the project payload and not
  the _01_prereqs package:

{payload_listing}

  Replace these placeholders with DBA-approved values:

    <DBA_SELECTED_PARENT>
    <DBA_REVIEWED_PERM>

  Then regenerate integrity, trust metadata, package report, zip,
  checksum sidecar, and release_group.json with:

    python -m td_release_packager repackage --package-dir "<extracted_00_environment_prereqs_dir>" --strict

  Do not deploy this package until the repackage command completes
  successfully and the regenerated archive is used.

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


def _rmtree_robust(path: str, retries: int = 5, delay: float = 0.2) -> None:
    """Remove a directory tree reliably on Windows and POSIX.

    On Windows, antivirus scanners, search indexers, and the OS itself
    can hold transient handles on files or directories that were just
    written.  A plain ``shutil.rmtree`` raises ``PermissionError`` in
    these cases.  This helper:

    1. Uses an ``onerror`` callback to clear the read-only flag on any
       file that resists deletion (a common Windows cause), then retries
       the remove operation.
    2. If the tree still exists after the per-file retry, sleeps briefly
       and retries the whole ``shutil.rmtree`` call up to *retries* times.

    On POSIX the overhead is negligible — the onerror path is never
    reached in normal operation.

    Args:
        path:    Path to the directory tree to remove.
        retries: Maximum number of whole-tree retry attempts.
        delay:   Seconds to sleep between whole-tree retries.
    """

    def _on_error(func, error_path, exc_info):
        """onerror callback: clear read-only flag and retry the remove."""
        try:
            os.chmod(error_path, stat.S_IWRITE)
            func(error_path)
        except Exception:
            pass  # Let the outer retry loop handle persistent failures.

    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_error)
        except Exception:
            pass
        if not os.path.exists(path):
            return
        if attempt < retries - 1:
            time.sleep(delay)

    # Final attempt — allow the exception to propagate if still failing.
    shutil.rmtree(path, onerror=_on_error)


def _archive_package(pkg_dir: str, archive_format: str) -> str:
    """Archive the package directory as .zip or .tar.gz.

    For zip format this uses :mod:`zipfile` directly rather than
    :func:`shutil.make_archive` so that every directory entry is written
    with POSIX-style forward-slash separators.  This is important on
    Windows where shutil.make_archive can produce zip entries with
    backslash separators or inconsistent directory metadata that causes
    :meth:`zipfile.ZipFile.extractall` to fail when the target path
    traverses a dot-prefixed directory such as .ships-work — a
    documented SHIPS DBA workflow directory.

    The archive is created alongside the package directory, and the
    unarchived directory is removed after successful archiving.

    Args:
        pkg_dir:        Path to the package directory.
        archive_format: zip or tar.gz.

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
        import pathlib
        import zipfile as _zipfile

        archive_path = pkg_dir + ".zip"
        root_dir = os.path.dirname(pkg_dir)

        # Suffixes excluded from the archive — these match _package_copy_ignore
        # and _generate_integrity_file exclusions.  __pycache__ directories are
        # pruned from the os.walk in-place so their contents are never visited.
        _SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        _SKIP_SUFFIXES = (
            ".pyc",
            ".pyo",
            ".bak",
            ".tmp",
            ".old",
            ".orig",
            ".rej",
            ".swp",
            ".swo",
        )

        with _zipfile.ZipFile(archive_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            for current_root, dirs, files in os.walk(pkg_dir):
                # Prune excluded directories in-place so os.walk skips them.
                dirs[:] = sorted(
                    d for d in dirs if d not in _SKIP_DIRS and not d.startswith("~")
                )

                # Write a directory entry with a POSIX-style path so that
                # extractall() on any platform — including Windows paths that
                # traverse dot-named directories such as .ships-work — can
                # recreate the full directory tree before writing file content.
                rel_dir = pathlib.Path(current_root).relative_to(root_dir)
                dir_entry = rel_dir.as_posix() + "/"
                if dir_entry != "./":
                    zf.mkdir(dir_entry)

                for fname in sorted(files):
                    if fname.startswith("~") or fname.lower().endswith(_SKIP_SUFFIXES):
                        continue
                    fpath = os.path.join(current_root, fname)
                    arcname = (rel_dir / fname).as_posix()
                    zf.write(fpath, arcname)

    # Remove the unarchived directory robustly — on Windows a bare
    # shutil.rmtree can race with antivirus or search-index handles.
    _rmtree_robust(pkg_dir)
    logger.info("Archived and cleaned up: %s", archive_path)

    return archive_path


def _generate_integrity_file(pkg_dir: str) -> str:
    """Compute a SHA-256 fingerprint over every payload and lib/ file.

    Walks ``payload/`` and ``lib/`` recursively (sorted), hashes each file,
    then derives a single ``package_hash`` as SHA-256 of the sorted
    ``"rel/path:filehash\\n"`` concatenation.  Writes the result to
    ``context/ships.integrity.json`` in the package so the embedded
    ``deploy.py`` can verify the package has not been tampered with
    before any database connection is opened.

    Including ``lib/`` means that an attacker who edits the embedded
    deployer code (e.g. ``lib/database_package_deployer/preflight.py``)
    to bypass security checks will be detected before any DDL executes.

    ``__pycache__`` directories and ``.pyc`` bytecode files are excluded
    from the manifest.  Python regenerates them on first import after
    the package is extracted — including them causes spurious integrity
    failures on the first dry-run or deploy invocation.

    Args:
        pkg_dir: Package root directory (not yet archived).

    Returns:
        The hex package_hash.
    """
    import pathlib

    # Directories and file suffixes excluded from the integrity manifest.
    # __pycache__ / .pyc are regenerated by the Python interpreter on
    # first import and must never be hashed.
    _EXCLUDE_DIRS = {"__pycache__"}
    _EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

    payload_dir = os.path.join(pkg_dir, "payload")
    lib_dir = os.path.join(pkg_dir, "lib")
    file_hashes: dict = {}

    for root, dirs, files in os.walk(payload_dir):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS)
        for fname in sorted(files):
            if os.path.splitext(fname)[1] in _EXCLUDE_SUFFIXES:
                continue
            fpath = os.path.join(root, fname)
            rel = pathlib.Path(os.path.relpath(fpath, pkg_dir)).as_posix()
            with open(fpath, "rb") as f:
                file_hashes[rel] = hashlib.sha256(f.read()).hexdigest()

    if os.path.isdir(lib_dir):
        for root, dirs, files in os.walk(lib_dir):
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS)
            for fname in sorted(files):
                if os.path.splitext(fname)[1] in _EXCLUDE_SUFFIXES:
                    continue
                fpath = os.path.join(root, fname)
                rel = pathlib.Path(os.path.relpath(fpath, pkg_dir)).as_posix()
                with open(fpath, "rb") as f:
                    file_hashes[rel] = hashlib.sha256(f.read()).hexdigest()

    combined = "".join(f"{k}:{v}\n" for k, v in sorted(file_hashes.items()))
    package_hash = hashlib.sha256(combined.encode()).hexdigest()

    integrity = {
        "schema_version": "1.0",
        "algorithm": "SHA-256",
        "package_hash": package_hash,
        "file_count": len(file_hashes),
        "files": file_hashes,
    }

    out_path = _context_file(pkg_dir, "ships.integrity.json")
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


def _read_signing_public_key(source_dir: str) -> str:
    """Read the Ed25519 public key PEM from ships.yaml signing.public_key.

    Returns an empty string when ships.yaml is absent or the key is not
    configured.  The returned value is stamped into context/ships.build.json so the
    deployer can verify signatures without a separate --public-key argument.

    Args:
        source_dir: Project root directory containing ships.yaml.

    Returns:
        PEM string, or empty string if not configured.
    """
    ships_yaml_path = os.path.join(source_dir, "ships.yaml")
    if not os.path.isfile(ships_yaml_path):
        return ""
    try:
        from td_release_packager.orchestrator import ships_yaml as _sy

        data = _sy.load(ships_yaml_path)
        signing_cfg = data.get("signing", {})
        return str(signing_cfg.get("public_key", "")).strip()
    except Exception:
        return ""


def _generate_checksum(archive_path: str) -> str:
    """
    Generate a SHA-256 checksum sidecar file for the package archive.

    Writes a `.sha256` file alongside the archive in the standard
    format used by sha256sum(1):

        <hex_digest>  <filename>

    The DBA verifies the package with a single command:

        cd releases/DEV_SHIPS_TEST_BUILD_0008_20260515120000
        sha256sum -c DEV_SHIPS_TEST_BUILD_0008_20260515120000_01_main.zip.sha256
        # DEV_SHIPS_TEST_BUILD_0008_20260515120000_01_main.zip: OK

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
