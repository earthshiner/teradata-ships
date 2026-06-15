"""
ingest.py — DDL onboarding from any source.

Takes raw DDL files from any origin (extracted, generated, hand-coded,
migrated) and normalises them into a release project structure:

    1. Classify each file by DDL content (table, view, JI, etc.)
    2. Sort into correct payload subdirectories
    3. Scan for hardcoded database/user names → suggest {{TOKENS}}
    4. Optionally apply token replacements
    5. Inject MULTISET where missing (skip for SHOW-extracted DDL
       which always includes SET/MULTISET)
    6. Rename files to eponymous convention (DB.ObjectName.ext)
    7. Report anything unclassifiable

Usage:
    python -m td_release_packager ingest \\
        --source /path/to/raw/ddl/ \\
        --project /path/to/project/ \\
        --detect-tokens
"""

import logging
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from td_release_packager.classifier import (
    TYPE_TO_EXTENSION as _TYPE_TO_EXT,
    TYPE_TO_SUBDIR as _TYPE_TO_SUBDIR,
)
from td_release_packager.legacy_placeholders import (
    LegacyPlaceholderFinding,
    find_legacy_placeholders,
)
from td_release_packager.source_migrator import (
    MigrationRule,
    apply_migration_rules_to_text,
)
from td_release_packager.token_engine import _TOKEN_RE, find_malformed_tokens

logger = logging.getLogger(__name__)


# -- Classification patterns: removed --
#
# Historic duplicate of classifier.py's pattern table. ingest no
# longer references it -- every classification call goes through
# ``td_release_packager.classifier.classify`` (see _classify_ddl
# below). Removing the duplicate eliminates the risk of the two
# tables drifting out of sync (which they did, until the
# start-of-statement anchoring fix exposed the divergence).

# -- MULTI_TABLE_DML marker --
# A source author can place ``-- MULTI_TABLE_DML`` near the top of a
# DML file to force the harvester to keep the entire script as a
# single ``<source>.multi_table.dml`` artefact regardless of whether
# the harvester would otherwise have aggregated chunks eponymously.
# Mirrors the existing ``-- LOCKING VIEW`` marker convention used by
# the view-layer generator. See docs/design-rationale/dml-naming.md
# for the policy this enforces.
_MULTI_TABLE_DML_MARKER_RE = re.compile(
    r"--\s*MULTI_TABLE_DML\b",
    re.IGNORECASE,
)


def _has_multi_table_dml_marker(content: str) -> bool:
    """Return True if the source content carries the
    ``-- MULTI_TABLE_DML`` opt-in marker."""
    return bool(_MULTI_TABLE_DML_MARKER_RE.search(content))


# -- Qualified name extraction patterns --
# Matches both literal names (Database.Object) and tokenised names
# ({{TOKEN}}.Object or {{TOKEN}}) in DDL statements.
_NAME_PART = r'(?:\{\{[A-Z][A-Z0-9_]*\}\}|"?[A-Za-z_]\w*"?)'
_QUALIFIED_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX|DATABASE|USER|PROFILE|ROLE)\s+"
    rf"({_NAME_PART}(?:\.{_NAME_PART})?)",
    re.IGNORECASE | re.MULTILINE,
)

# -- Name extraction for COMMENT ON statements --
# Matches every Teradata COMMENT ON variant SHIPS handles. Each
# kind has the qualified target as the first whitespace-separated
# token after the kind keyword:
#   COMMENT ON TABLE      {{DB}}.table       IS '...'
#   COMMENT ON VIEW       {{DB}}.view        IS '...'
#   COMMENT ON MACRO      {{DB}}.macro       IS '...'
#   COMMENT ON PROCEDURE  {{DB}}.proc        IS '...'
#   COMMENT ON FUNCTION   {{DB}}.fn          IS '...'
#   COMMENT ON TRIGGER    {{DB}}.trg         IS '...'
#   COMMENT ON COLUMN     {{DB}}.table.col   IS '...'
#   COMMENT ON DATABASE   {{DB}}             IS '...'
#   COMMENT ON USER       user               IS '...'
#   COMMENT ON ROLE       role               IS '...'
#   COMMENT ON PROFILE    profile            IS '...'
# The capture group always points at the qualified target. For
# COLUMN it captures db.table.col (3 parts) and the caller drops
# the column segment so multiple comments on the same table
# aggregate into one .cmt file.
_COMMENT_ON_NAME_RE = re.compile(
    r"COMMENT\s+ON\s+"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|COLUMN|"
    r"DATABASE|USER|ROLE|PROFILE)\s+"
    rf"({_NAME_PART}(?:\.{_NAME_PART}){{0,2}})",
    re.IGNORECASE,
)

# -- Name extraction for COLLECT / UPDATE STATISTICS statements --
# Matches: COLLECT STATISTICS [COLUMN (...)] ON {{DB}}.table
#          COLLECT STATISTICS ON {{DB}}.table COLUMN (...)
#          UPDATE STATISTICS ON {{DB}}.table  (Teradata synonym)
_COLLECT_STATS_NAME_RE = re.compile(
    r"(?:COLLECT|UPDATE)\s+STATISTICS\b.*?\bON\s+"
    rf"({_NAME_PART}\.{_NAME_PART})",
    re.IGNORECASE | re.DOTALL,
)

# -- Name extraction for DML statements (INSERT / UPDATE / DELETE / MERGE) --
# Captures the qualified target object so the harvested file can be
# named ``<db>.<table>.dml``. All four verbs follow the same shape:
# the target is the first qualified name after the leading verb.
#   INSERT INTO {{DB}}.t (...) VALUES (...)
#   UPDATE      {{DB}}.t SET ... [FROM ... WHERE ...]
#   DELETE FROM {{DB}}.t WHERE ...
#   MERGE  INTO {{DB}}.t USING ... ON ... WHEN MATCHED THEN ...
# The line-start anchor mirrors the classifier; UPDATE STATISTICS is
# already handled by ``_COLLECT_STATS_NAME_RE`` above.
_DML_NAME_RE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+"
    rf"({_NAME_PART}(?:\.{_NAME_PART})?)",
    re.IGNORECASE | re.MULTILINE,
)

# -- Name extraction for GRANT / REVOKE statements --
# Captures the qualified object the privilege applies to so the
# harvested file can be named eponymously (e.g. ``<db>.dcl`` for a
# database-level grant or ``<db>.<table>.dcl`` for a table-level
# grant). Aggregates multiple grants on the same object into one
# .dcl file rather than the source-filename fallback.
#   GRANT  privileges  ON  [TABLE|VIEW|...] {{DB}}[.t]  TO    grantee
#   REVOKE privileges  ON  [TABLE|VIEW|...] {{DB}}[.t]  FROM  grantee
# The ``ON`` clause is the unambiguous marker; the privilege list
# between the verb and ON varies (``SELECT``, ``ALL PRIVILEGES``,
# ``CREATE TABLE``, ``EXECUTE PROCEDURE``, etc.) and is consumed
# by ``.*?``.  The optional object-kind token after ON is deliberately
# skipped so grants such as ``ON PROCEDURE SQLJ.INSTALL_JAR`` are named
# from the target object rather than producing ``Procedure.dcl``.
_GRANT_REVOKE_NAME_RE = re.compile(
    r"^\s*(?:GRANT|REVOKE)\b.*?\bON\s+"
    r"(?:TABLE\s+|VIEW\s+|MACRO\s+|PROCEDURE\s+|"
    r"EXTERNAL\s+PROCEDURE\s+|FUNCTION\s+|DATABASE\s+|"
    r"USER\s+|ROLE\s+|PROFILE\s+)?"
    rf"({_NAME_PART}(?:\.{_NAME_PART})?)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

# -- Detect REPLACE VIEW vs CREATE VIEW --
_HAS_REPLACE_VIEW_RE = re.compile(
    r"REPLACE\s+VIEW",
    re.IGNORECASE,
)
_CREATE_VIEW_RE = re.compile(
    r"CREATE\s+VIEW\b",
    re.IGNORECASE,
)
_DATABASE_CONTEXT_RE = re.compile(r"^\s*DATABASE\s+[^;]+;?\s*$", re.IGNORECASE)
_SQLJ_JAR_CALL_STMT_RE = re.compile(
    r"\bCALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|CREATE_JAR|REPLACE_JAR)\s*\(",
    re.IGNORECASE,
)
_SQLJ_JAR_ALIAS_RE = re.compile(
    r"\bCALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|CREATE_JAR|REPLACE_JAR)\s*\("
    r"\s*'(?:[^']|'')*'\s*,\s*'(?P<alias>(?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)

# -- MULTISET detection --
_HAS_SET_MULTISET_RE = re.compile(
    r"CREATE\s+(?:MULTISET|SET)\s+",
    re.IGNORECASE,
)
_INJECT_MULTISET_RE = re.compile(
    r"(CREATE\s+)((?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?(?:TRACE\s+)?TABLE\b)",
    re.IGNORECASE,
)


@dataclass
class IngestResult:
    """
    Outcome of ingesting DDL files into a project.

    Attributes:
        total_files:           Files scanned in source directory.
        classified:            Successfully classified and placed.
        unclassified:          Could not determine object type.
        token_candidates:      Hardcoded names detected as token candidates.
        files_placed:          List of (source, destination, type) tuples.
        warnings:              Non-fatal issues.
        errors:                Fatal issues.
        classification_warnings:
                               Per-file diagnostics from the rich
                               classifier — filename mismatches,
                               unrecognised externals, etc. Surfaced
                               in the harvest banner so users can act
                               on them at harvest time.
        external_references:   Map of staged-file path to the list of
                               external file references discovered
                               (C source/header paths for FUNCTION_C,
                               JAR aliases for PROCEDURE_JAVA). The
                               deployer can use this to bundle / order
                               dependencies.
        subtypes:              Map of staged-file path to its rich
                               sub-type (FUNCTION_C, PROCEDURE_JAVA,
                               etc.). Files without a sub-type are
                               omitted; the base type lives in
                               files_placed.
    """

    total_files: int = 0
    classified: int = 0
    unclassified: int = 0
    overwritten: int = 0
    skipped_existing: int = 0
    cleaned: int = 0
    token_candidates: Dict[str, List[str]] = field(default_factory=dict)
    files_placed: List[Tuple[str, str, str]] = field(default_factory=list)
    multiset_injected: int = 0
    #: Identifier-aware prefix tokenisation tallies (issue #309).
    prefix_token_substitutions: int = 0
    prefix_token_files: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    unclassified_files: List[str] = field(default_factory=list)
    classification_warnings: List[str] = field(default_factory=list)
    external_references: Dict[str, List[str]] = field(default_factory=dict)
    subtypes: Dict[str, str] = field(default_factory=dict)
    #: Multi-target DML files kept together: maps the placed
    #: ``<source>.multi_table.dml`` destination path to the list
    #: of distinct targets observed across the source's statements.
    #: Honours the per-source-author intent that statement order
    #: in a multi-target script is meaningful (FK ordering,
    #: sequenced operations, transactional grouping). See
    #: ``docs/design-rationale/dml-naming.md``.
    multi_table_targets: Dict[str, List[str]] = field(default_factory=dict)
    #: Binary artefacts physically copied into the payload —
    #: list of (source_abs_path, dest_rel_path, kind) tuples.
    #: kind is JAR_BINARY / C_SOURCE / C_HEADER / etc.
    binaries_placed: List[Tuple[str, str, str]] = field(default_factory=list)
    #: Non-SHIPS substitution placeholders detected in source
    #: ($VAR, ${VAR}, &&VAR&&). Populated by harvest so callers can
    #: surface a banner naming the syntax + remediation tool. Empty
    #: list when the source uses only SHIPS {{TOKEN}} form (or no
    #: substitution at all).
    legacy_placeholders: List["LegacyPlaceholderFinding"] = field(default_factory=list)
    #: Tokenisation substitutions applied in memory before
    #: classification, via config/tokenise.conf or an explicit
    #: caller-supplied migration rule set.
    legacy_migration_files: int = 0
    legacy_migration_substitutions: int = 0
    #: Human-facing harvest mirror grouped by database/token. This is
    #: generated outside payload so deploy/package stages keep using the
    #: canonical SHIPS tree.
    placement_index_dir: Optional[str] = None
    placement_index_files: int = 0
    view_type_affix_renames: int = 0


def ingest_directory(
    source_dir: str,
    project_dir: str,
    detect_tokens: bool = True,
    apply_tokens: Optional[Dict[str, str]] = None,
    file_patterns: List[str] = None,
    force: bool = False,
    clean_payload: bool = True,
    legacy_migration_rules: Optional[List[MigrationRule]] = None,
    remove_view_type_affixes: bool = False,
    prefix_tokens: Optional[Dict[str, str]] = None,
) -> IngestResult:
    """
    Ingest raw DDL files from a source directory into a project.

    Thin traced wrapper — see ``_ingest_directory_impl`` for the full
    implementation.  Emits a ``ships.ingest`` OpenTelemetry span when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is configured.
    """
    from ships_tracing import stage_span

    with stage_span(
        "ships.ingest",
        **{"ships.source_dir": source_dir, "ships.project_dir": project_dir},
    ) as _span:
        result = _ingest_directory_impl(
            source_dir,
            project_dir,
            detect_tokens=detect_tokens,
            apply_tokens=apply_tokens,
            file_patterns=file_patterns,
            force=force,
            clean_payload=clean_payload,
            legacy_migration_rules=legacy_migration_rules,
            remove_view_type_affixes=remove_view_type_affixes,
            prefix_tokens=prefix_tokens,
        )
        _span.set_attribute("ships.files_total", result.total_files)
        _span.set_attribute("ships.files_classified", result.classified)
        _span.set_attribute("ships.files_unclassified", result.unclassified)
        _span.set_attribute("ships.warnings", len(result.warnings))
        _span.set_attribute("ships.errors", len(result.errors))
        return result


def _ingest_directory_impl(
    source_dir: str,
    project_dir: str,
    detect_tokens: bool = True,
    apply_tokens: Optional[Dict[str, str]] = None,
    file_patterns: List[str] = None,
    force: bool = False,
    clean_payload: bool = True,
    legacy_migration_rules: Optional[List[MigrationRule]] = None,
    remove_view_type_affixes: bool = False,
    prefix_tokens: Optional[Dict[str, str]] = None,
) -> IngestResult:
    """
    Ingest raw DDL files from a source directory into a project.

    Scans every file, classifies by DDL content, normalises
    (MULTISET injection, REPLACE VIEW injection), renames to
    eponymous convention, and copies to the correct payload
    subdirectory.

    Args:
        source_dir:     Directory containing raw DDL files.
        project_dir:    Target project root (must exist, with
                        payload/database/ structure).
        detect_tokens:  If True, scan for hardcoded database/user
                        names and report them as token candidates.
        apply_tokens:   Optional dict of name → {{TOKEN}} to apply
                        during ingest (e.g. {'DEV01_STD': '{{STD_DATABASE}}'}).
        file_patterns:  Glob patterns to scan (default: common SQL
                        extensions). Pass None to scan all files.
        force:          If True, overwrite existing files in the
                        payload.  Warns if overwriting a tokenised
                        file with non-tokenised content (i.e. a
                        previous harvest applied a token-map but
                        this one did not). Redundant when
                        ``clean_payload`` is True (the default) —
                        the payload starts empty.
        clean_payload:  If True (default), wipe harvest-owned
                        files from ``payload/database/`` before
                        scanning source — preserves ``.gitkeep``
                        and control files (filenames starting with
                        ``_``, e.g. user-curated ``_order.txt``).
                        Guarantees the payload reflects current
                        source state with no orphaned artefacts
                        from prior runs. Set to False for the
                        legacy overlay behaviour where existing
                        files are kept and collisions are governed
                        by ``force``.
        legacy_migration_rules:
                        Optional parsed rules from
                        ``tokenise.conf``. When supplied,
                        source-side markers are converted to SHIPS
                        ``{{TOKEN}}`` form in memory before
                        classification, naming, token scanning, and
                        payload writing.
        remove_view_type_affixes:
                        If True, remove redundant view object name affixes
                        (leading ``v_`` and trailing ``_v``) from view
                        definitions and qualified references before payload
                        placement.

    Returns:
        IngestResult with per-file outcomes and token candidates.

    Raises:
        FileNotFoundError: If source or project directory missing.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    if not os.path.isdir(project_dir):
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    payload_base = _find_payload_base(project_dir)
    result = IngestResult()

    # -- Pre-harvest payload clean --
    # Wipe harvest-owned content from the payload before placing
    # any new files. Preserves .gitkeep (so empty dirs stay tracked
    # in git) and control files starting with ``_`` (so user-curated
    # _order.txt files in DDL/, DCL/, etc. survive). The harvest
    # itself rewrites pre-requisites/_order.txt from scratch each
    # run, so leaving that intact is harmless.
    if clean_payload:
        result.cleaned = _clean_payload_tree(payload_base)
        if result.cleaned:
            logger.info(
                "Payload cleaned: removed %d stale file(s) from %s",
                result.cleaned,
                payload_base,
            )

    # -- Discover source files --
    # Pass project_dir so _discover_files can resolve any
    # ships.yaml ``discovery.extensions`` overrides — sites that
    # use convention-specific extensions (.bteq2, .tdsql, etc.)
    # configure them once in ships.yaml rather than per-call.
    source_files = _discover_files(source_dir, file_patterns, project_dir=project_dir)
    result.total_files = len(source_files)
    logger.info("Found %d files in %s", len(source_files), source_dir)

    # -- Pre-scan: build object kind index for kind-aware tokenisation --
    # When apply_tokens is supplied, scan all source files first to build
    # a {db.obj → kind} index.  The main loop uses this index to emit
    # {{TOKEN_T}} vs {{TOKEN_V}} per reference rather than a monolithic token.
    kind_index: Optional[Dict[str, str]] = None
    prefix_mode_literals: Set[str] = set()
    if apply_tokens:
        kind_index = _build_source_kind_index(source_files)
        # Classify each token_map literal as prefix vs full-DB shape so
        # the kind-aware machinery never injects a ``_T``/``_V`` suffix
        # into the braces of a prefix-shape token (see issue #311).
        prefix_mode_literals = _detect_prefix_mode_literals(source_files, apply_tokens)
        if prefix_mode_literals:
            logger.info(
                "Prefix-mode token-map entries (no kind suffix): %s",
                ", ".join(sorted(prefix_mode_literals)),
            )

    view_affix_renames: Dict[Tuple[str, str], str] = {}
    if remove_view_type_affixes:
        view_affix_renames = _build_view_type_affix_renames(
            source_files, legacy_migration_rules
        )
        result.view_type_affix_renames = len(view_affix_renames)

    # -- Track all database names seen (for token detection) --
    all_db_names: Dict[str, List[str]] = defaultdict(list)

    # -- Track aggregating-type destinations first-touched THIS run --
    # COMMENT/STATISTICS/DML files accumulate multiple statements
    # within a single harvest. The first time we touch such a file
    # in this run, we truncate any leftover content from a previous
    # run (otherwise --force re-harvest produces files with both
    # the old untokenised AND new tokenised statements). Subsequent
    # touches within the same run append normally.
    first_touch_aggregating: Set[str] = set()

    for src_path in source_files:
        try:
            raw_content = _read_file(src_path)
            if raw_content is None:
                continue  # Binary file

            if legacy_migration_rules:
                migrated_content, hits = apply_migration_rules_to_text(
                    raw_content,
                    legacy_migration_rules,
                )
                if hits:
                    result.legacy_migration_files += 1
                    result.legacy_migration_substitutions += sum(hits.values())
                    raw_content = migrated_content

            # -- Detect non-SHIPS placeholders ($VAR, &&VAR&&, etc.)
            # Recorded into the result so the harvest banner can
            # surface them; does NOT block the harvest itself.
            # Detection runs against the raw file (not per-statement)
            # so a placeholder appearing only in a header survives
            # to the banner.
            result.legacy_placeholders.extend(
                find_legacy_placeholders(raw_content, src_path)
            )

            # -- Strip BTEQ control commands from raw content --
            # Legacy codebases wrap SQL of all types (GRANT, CREATE
            # DATABASE, CREATE USER, etc.) in BTEQ flow-control
            # scaffolding (.IF ERRORCODE, .GOTO ERR, .LABEL, etc.).
            # SHIPS deploys SQL directly and owns error handling —
            # these commands have no meaning here and prevent the
            # classifier from recognising the real SQL statement.
            # Stripping before the split applies universally,
            # ensuring that DCL (.dcl), prereqs (.db, .usr), and
            # any other BTEQ-wrapped file type classifies correctly.
            raw_content, n_bteq = _strip_bteq_commands(raw_content)
            if n_bteq:
                result.classification_warnings.append(
                    f"{os.path.relpath(src_path, source_dir)}: "
                    f"stripped {n_bteq} BTEQ command line(s) "
                    f"(.IF/.GOTO flow control) — SHIPS deploys "
                    f"SQL directly and handles errors through its "
                    f"own mechanisms."
                )

            if view_affix_renames:
                raw_content = _apply_view_type_affix_renames(
                    raw_content, view_affix_renames
                )

            # -- Apply prefix tokenisation (Model B, issue #309) --
            # Rewrite a database-name prefix to a single {{TOKEN}} while
            # preserving the structural remainder.  Runs BEFORE statement
            # splitting and literal-token substitution so every downstream
            # code path (single-statement, multi-target DML, ordered SQL)
            # sees pre-tokenised content.
            if prefix_tokens:
                from td_release_packager.token_engine import tokenise_prefixes

                raw_content, _pfx_total, _pfx_per = tokenise_prefixes(
                    raw_content, prefix_tokens
                )
                if _pfx_total:
                    result.prefix_token_substitutions += _pfx_total
                    result.prefix_token_files += 1

            # -- Split multi-statement files into individual DDL --
            # A file like create_databases.sql with 5 CREATE DATABASE
            # statements becomes 5 individual statements, each
            # processed and placed as a separate eponymous file.
            statements = _split_multi_sqlj_jar_script(
                raw_content, src_path
            ) or _split_multi_statement(raw_content, src_path)

            from td_release_packager.classifier import classify, base_type

            # -- Pre-pass: detect multi-target DML and mixed DCL choreography --
            # A source file that interleaves GRANT/REVOKE with non-DCL
            # statements is an ordered executable script, not independent
            # phase-bucket artefacts. Keep it together so temporary
            # privilege changes stay wrapped around the actions that need them.
            #
            # When all classifiable chunks in a source file are DML and
            # they target more than one distinct table, the source's
            # statement order is meaningful (FK ordering, sequenced
            # operations like INSERT staging → UPDATE control →
            # DELETE staging, transactional grouping). Splitting these
            # into separate eponymous .dml files would silently destroy
            # that intent. Default policy: keep the entire source as a
            # single ``<source_basename>.multi_table.dml`` artefact.
            # See docs/design-rationale/dml-naming.md.
            #
            # The ``-- MULTI_TABLE_DML`` header marker forces this
            # treatment even for DML files where every chunk would
            # otherwise have aggregated to the same eponymous target —
            # useful when the author wants statement order preserved
            # across multiple INSERTs into a single history table.
            dml_targets: Set[Tuple[Optional[str], Optional[str]]] = set()
            non_dml_classified = False
            saw_grant = False
            saw_revoke = False
            saw_non_dcl = False
            for chunk in statements:
                chunk_clean = _strip_comments(chunk)
                pre_cls = classify(path=src_path, content=chunk_clean)
                pre_type = base_type(pre_cls.type)
                if pre_type == "GRANT":
                    saw_grant = True
                elif pre_type == "REVOKE":
                    saw_revoke = True
                elif pre_type is not None:
                    saw_non_dcl = True
                if pre_type == "DML":
                    chunk_db, chunk_obj = _extract_qualified_name(chunk_clean)
                    dml_targets.add((chunk_db, chunk_obj))
                elif pre_type is not None:
                    non_dml_classified = True

            if saw_grant and saw_revoke and saw_non_dcl:
                _place_ordered_sql(
                    raw_content=raw_content,
                    src_path=src_path,
                    source_dir=source_dir,
                    project_dir=project_dir,
                    payload_base=payload_base,
                    apply_tokens=apply_tokens,
                    kind_index=kind_index,
                    result=result,
                    prefix_mode_literals=prefix_mode_literals,
                )
                continue  # next source file

            force_marker = _has_multi_table_dml_marker(raw_content)
            multi_target_detected = not non_dml_classified and len(dml_targets) > 1
            keep_as_multi_table = multi_target_detected or (
                force_marker and not non_dml_classified
            )

            if keep_as_multi_table:
                _place_multi_table_dml(
                    raw_content=raw_content,
                    src_path=src_path,
                    source_dir=source_dir,
                    project_dir=project_dir,
                    payload_base=payload_base,
                    apply_tokens=apply_tokens,
                    kind_index=kind_index,
                    dml_targets=dml_targets,
                    result=result,
                    prefix_mode_literals=prefix_mode_literals,
                )
                continue  # next source file

            for content in statements:
                # Strip leading source-structure comments (file
                # banner, section letter, execution-order notes) —
                # these reference the original layout that no longer
                # exists once SHIPS renames each statement
                # eponymously, so they're noise in the package.
                # Inline / trailing comments inside the statement
                # are preserved.
                content = _strip_leading_chunk_comments(content)

                # Strip comments for safe classification and name
                # extraction — prevents false matches from DDL keywords
                # in comments (e.g. 'CREATE DATABASE IF NOT EXISTS' in
                # a comment would otherwise classify as DATABASE 'IF').
                # Original content is preserved for file output.
                clean = _strip_comments(content)

                # -- Classify (rich) --
                # Pass the source path so the classifier can detect
                # filename-vs-content mismatches and surface them in
                # the per-file warnings.
                classification = classify(path=src_path, content=clean)
                obj_subtype = classification.type
                obj_type = base_type(obj_subtype)
                # Surface filename-mismatch and unrecognised-external
                # warnings to the harvest banner for user attention.
                rel_path = os.path.relpath(src_path, source_dir)
                for w in classification.warnings:
                    result.classification_warnings.append(f"{rel_path}: {w}")

                if obj_type is None:
                    result.unclassified += 1
                    result.unclassified_files.append(rel_path)
                    result.warnings.append(
                        f"Could not classify: {os.path.basename(src_path)}"
                    )
                    continue

                # -- Extract qualified name --
                db_name, obj_name = _extract_qualified_name(clean)

                # -- Track database names for token detection --
                if db_name and detect_tokens:
                    all_db_names[db_name].append(os.path.basename(src_path))

                # -- Normalise content --
                # MULTISET injection (tables only, skip if already has SET/MULTISET)
                if obj_type == "TABLE":
                    content, injected = _inject_multiset(content)
                    if injected:
                        result.multiset_injected += 1

                # NOTE: We deliberately do NOT inject REPLACE VIEW here.
                # The developer's DDL verb (CREATE vs REPLACE) is their
                # deployment intent. SHIPS respects it — the validate step
                # will inform them of the implications.

                # -- Apply token substitutions if provided --
                # Kind-aware path: emit {{TOKEN_T}} for table contexts,
                # {{TOKEN_V}} for view contexts, etc. based on a pre-built
                # package-wide object index and this file's classified type.
                # Substitution is position-aware (uses a scratch copy with
                # comments and string literals blanked) so semicolons, dots,
                # or identifiers inside literals never confuse the rewriter.
                if apply_tokens:
                    from td_release_packager.kind_suffix import TYPE_TO_KIND

                    file_kind = TYPE_TO_KIND.get(obj_type, "T")
                    content = _apply_kind_aware_tokens(
                        content,
                        file_kind,
                        apply_tokens,
                        kind_index or {},
                        prefix_mode_literals=prefix_mode_literals,
                    )

                    # Defense in depth: if substitution produced any
                    # malformed {{...}} markers (orphan braces, double-
                    # tokenisation), surface them as warnings so the
                    # developer sees them at harvest time rather than
                    # finding them at build or — worse — at deploy.
                    bad = find_malformed_tokens(content)
                    if bad:
                        result.warnings.append(
                            f"Malformed tokens after substitution in "
                            f"{os.path.basename(src_path)} "
                            f"({len(bad)} marker(s)). The build will "
                            f"reject this file — re-check token_map.conf "
                            f"and the source content."
                        )

                # -- Determine destination --
                subdir = _TYPE_TO_SUBDIR.get(obj_type, "DDL")
                ext = _TYPE_TO_EXT.get(obj_type, ".sql")

                # For overloaded functions, use the SPECIFIC name
                # to avoid filename collisions between overloads
                if obj_type == "FUNCTION":
                    specific_name = _extract_specific_function_name(content)
                    if specific_name:
                        obj_name = specific_name
                elif obj_type == "JAR":
                    jar_alias = _extract_sqlj_jar_alias(content)
                    if jar_alias:
                        obj_name = jar_alias

                # Build eponymous filename
                if db_name and obj_name:
                    dest_name = f"{db_name}.{obj_name}{ext}"
                elif obj_name:
                    dest_name = f"{obj_name}{ext}"
                else:
                    # Fallback: use original filename with correct extension
                    base = os.path.splitext(os.path.basename(src_path))[0]
                    dest_name = f"{base}{ext}"

                dest_dir = os.path.join(payload_base, subdir)
                os.makedirs(dest_dir, exist_ok=True)

                # Remove .gitkeep if present
                gitkeep = os.path.join(dest_dir, ".gitkeep")
                if os.path.exists(gitkeep):
                    try:
                        os.remove(gitkeep)
                    except OSError as exc:
                        logger.debug("Could not remove %s: %s", gitkeep, exc)

                dest_path = os.path.join(dest_dir, dest_name)

                aggregating_types = ("COMMENT", "STATISTICS", "DML", "GRANT", "REVOKE")

                # -- Handle existing files --------------------------------
                if os.path.exists(dest_path):
                    # COMMENT, STATISTICS, DML, and DCL aggregate by
                    # appending multiple statements for the same target
                    # into one eponymous file. For DCL this preserves
                    # legacy scripts that revoke then grant permissions
                    # back in a deliberate sequence. The FIRST touch of
                    # this file in this harvest run truncates any leftover
                    # content from a PREVIOUS run when --force is set.
                    # Subsequent touches within the same run append
                    # normally to build the aggregate.
                    if obj_type in aggregating_types:
                        if force and dest_path not in first_touch_aggregating:
                            mode = "w"
                            result.overwritten += 1
                        else:
                            mode = "a"
                        first_touch_aggregating.add(dest_path)
                        with open(dest_path, mode, encoding="utf-8") as f:
                            if mode == "a":
                                f.write("\n")
                            f.write(content)
                            f.write("\n")
                        result.classified += 1
                        result.files_placed.append(
                            (
                                os.path.relpath(src_path, source_dir),
                                os.path.relpath(dest_path, project_dir),
                                obj_type,
                            )
                        )
                        continue

                    elif not force:
                        # Default: skip, warn, count
                        result.skipped_existing += 1
                        result.warnings.append(
                            f"Exists: {dest_name} — skipped "
                            f"(source: {os.path.basename(src_path)}). "
                            f"Use --force to overwrite."
                        )
                        continue
                    else:
                        # Force mode: overwrite, but check for
                        # tokenisation regression first.
                        existing_content = _read_file(dest_path)
                        if existing_content is not None:
                            existing_has_tokens = bool(
                                _TOKEN_RE.search(existing_content)
                            )
                            new_has_tokens = bool(_TOKEN_RE.search(content))
                            if existing_has_tokens and not new_has_tokens:
                                result.warnings.append(
                                    f"Token regression: {dest_name} "
                                    f"contains {{{{TOKENS}}}} but the "
                                    f"replacement does not. Apply "
                                    f"--token-map to preserve "
                                    f"tokenisation."
                                )
                        result.overwritten += 1
                        logger.info(
                            "Overwriting: %s (--force)",
                            dest_name,
                        )

                # -- Harvest binary dependencies BEFORE writing --
                # FUNCTION_C and JAR install scripts reference
                # binary files (.c/.h, .jar) that the deployer
                # needs alongside. Resolve, copy, and rewrite the
                # paths so the deployed script's references are
                # sibling-relative.
                if (
                    obj_subtype in ("FUNCTION_C", "PROCEDURE_CPP", "JAR")
                    and classification.related_files
                ):
                    from td_release_packager.binary_harvester import (
                        harvest_binaries,
                    )

                    harvest_dest_dir = os.path.dirname(dest_path)
                    bh_result = harvest_binaries(
                        content=content,
                        related_paths=classification.related_files,
                        source_file_path=src_path,
                        destination_dir=harvest_dest_dir,
                    )
                    content = bh_result.rewritten_content

                    # Track each successfully copied binary
                    for dep in bh_result.copied:
                        rel_bin_dest = os.path.relpath(
                            dep.destination_path, project_dir
                        )
                        result.binaries_placed.append(
                            (dep.source_path, rel_bin_dest, dep.kind)
                        )

                    # Surface any missing-binary warnings
                    for w in bh_result.warnings:
                        result.classification_warnings.append(
                            f"{os.path.relpath(src_path, source_dir)}: {w}"
                        )

                # -- Write normalised content --
                with open(dest_path, "w", encoding="utf-8") as f:
                    f.write(content)

                # Aggregating types need to know that this dest_path has
                # already been first-touched in this run, so a SECOND
                # statement targeting the same file appends instead of
                # triggering the --force truncate path.
                if obj_type in aggregating_types:
                    first_touch_aggregating.add(dest_path)

                result.classified += 1
                rel_dest = os.path.relpath(dest_path, project_dir)
                result.files_placed.append(
                    (
                        os.path.relpath(src_path, source_dir),
                        rel_dest,
                        obj_type,
                    )
                )
                # Record sub-type + external refs against the staged
                # destination path so downstream stages (deployer,
                # explain) can resolve them. Only persist sub-types
                # when they differ from the base — keeps the dict
                # focused on dialect distinctions.
                if obj_subtype is not None and obj_subtype != obj_type:
                    result.subtypes[rel_dest] = obj_subtype
                if classification.related_files:
                    result.external_references[rel_dest] = list(
                        classification.related_files
                    )

                logger.debug(
                    "Ingested: %s → %s (%s)",
                    os.path.basename(src_path),
                    dest_name,
                    obj_type,
                )

        except Exception as e:
            result.errors.append(f"Error processing {os.path.basename(src_path)}: {e}")

    # -- Build token candidate report --
    if detect_tokens:
        result.token_candidates = _build_token_candidates(all_db_names)

    # -- Emit pre-requisite deployment order --
    # After all DATABASE and USER files are placed, compute a
    # topological ordering based on their FROM <parent> dependencies
    # and write _order.txt so the deployer processes parents first.
    # Without this, a child database or user can be deployed before
    # its parent exists on the target, causing the deploy to fail.
    prereq_placed = any(
        obj_type in ("DATABASE", "USER") for _, _, obj_type in result.files_placed
    )
    if prereq_placed:
        prereq_dir = os.path.join(payload_base, "pre-requisites")
        if os.path.isdir(prereq_dir):
            prereq_result = _emit_prereq_order(prereq_dir)
            if prereq_result.ordered:
                logger.info(
                    "Pre-requisites: dependency-ordered %d file(s) → "
                    "pre-requisites/_order.txt",
                    len(prereq_result.ordered),
                )
            # Surface unresolvable files as harvest warnings.
            # We cannot guarantee alphabetical order is correct for
            # files whose FROM <parent> clause is missing or
            # unreadable. The DBA must verify these manually.
            for rel, reason in prereq_result.unresolvable:
                result.classification_warnings.append(
                    f"pre-requisites/{rel}: deployment order UNRESOLVED "
                    f"({reason}). Cannot guarantee this file deploys after "
                    f"its parent. Add a FROM <parent_db_or_user> clause to "
                    f"the source DDL to enable automatic ordering."
                )

    result.placement_index_dir, result.placement_index_files = (
        _emit_database_placement_mirror(project_dir, result.files_placed)
    )

    logger.info(
        "Ingest complete: %d classified, %d unclassified, "
        "%d overwritten, %d skipped (existing), "
        "%d MULTISET injected",
        result.classified,
        result.unclassified,
        result.overwritten,
        result.skipped_existing,
        result.multiset_injected,
    )

    return result


# ---------------------------------------------------------------
# Internal — File discovery
# ---------------------------------------------------------------


def _discover_files(
    source_dir: str,
    file_patterns: List[str] = None,
    project_dir: Optional[str] = None,
) -> List[str]:
    """
    Discover SQL/DDL files in a directory tree.

    The default extension set lives in ``td_release_packager.discovery``
    and is project-overridable via ``ships.yaml``'s
    ``discovery.extensions`` list. Pass ``file_patterns`` explicitly
    to bypass both defaults and project config (used by tests and
    callers that already know exactly what they want).

    Args:
        source_dir:    Root directory to scan.
        file_patterns: Optional explicit extension list. When
                       supplied, neither the canonical defaults nor
                       ships.yaml are consulted — the caller's list
                       wins outright.
        project_dir:   Optional project root for ships.yaml
                       resolution. Only consulted when
                       ``file_patterns`` is None. Pass ``None`` to
                       skip ships.yaml lookups entirely (e.g. for
                       harvest invocations where ``source_dir`` is
                       outside any SHIPS project).

    Returns:
        Sorted list of file paths.
    """
    if file_patterns is None:
        from td_release_packager.discovery import resolve_harvest_extensions

        extensions = resolve_harvest_extensions(project_dir=project_dir)
    else:
        extensions = {ext.lower() for ext in file_patterns}

    files = []
    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith(".") or f.startswith("_"):
                continue
            ext = os.path.splitext(f)[1].lower()
            # Strict whitelist: only process files with a known extension.
            # An empty extension set is treated as "nothing passes" rather
            # than "everything passes" — the latter would silently harvest
            # .exclude, .bak, and other bypass files if the resolver ever
            # returned an empty set due to a misconfiguration.
            if ext in extensions:
                files.append(os.path.join(root, f))
    return files


def _read_file(path: str) -> Optional[str]:
    """Read a text file, returning None for binary files."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        return None


def _find_payload_base(project_dir: str) -> str:
    """Locate the payload/database directory."""
    for candidate in ["payload/database", "payload"]:
        path = os.path.join(project_dir, candidate)
        if os.path.isdir(path):
            return path
    raise FileNotFoundError(
        f"No payload/database directory found in {project_dir}. "
        "Run 'td_release_packager scaffold' first."
    )


def _place_multi_table_dml(
    *,
    raw_content: str,
    src_path: str,
    source_dir: str,
    project_dir: str,
    payload_base: str,
    apply_tokens: Optional[Dict[str, str]],
    kind_index: Optional[Dict[str, str]],
    dml_targets: Set[Tuple[Optional[str], Optional[str]]],
    result: "IngestResult",
    prefix_mode_literals: Optional[Set[str]] = None,
) -> None:
    """Place a multi-target DML source file as a single
    ``<source_basename>.multi_table.dml`` artefact.

    Used when the harvester's pre-pass detects that a multi-statement
    DML source touches more than one distinct target table (or when
    a ``-- MULTI_TABLE_DML`` marker is present). The whole-source
    write preserves statement order — which carries the source
    author's intent (FK ordering, sequenced operations,
    transactional grouping). Per-statement splitting would silently
    destroy that intent.

    Token substitutions are applied to the whole content. Leading
    file-banner comments are stripped before write so the deployable
    artefact does not carry source-layout noise; inline / trailing
    comments are preserved.

    The destination path and the set of distinct targets observed
    in the source are recorded on ``result.multi_table_targets`` so
    the manifest and harvest banner can surface them.
    """
    source_basename = os.path.splitext(os.path.basename(src_path))[0]
    dest_name = f"{source_basename}.multi_table.dml"
    dest_dir = os.path.join(payload_base, "DML")
    os.makedirs(dest_dir, exist_ok=True)

    gitkeep = os.path.join(dest_dir, ".gitkeep")
    if os.path.exists(gitkeep):
        os.remove(gitkeep)

    dest_path = os.path.join(dest_dir, dest_name)

    content = _strip_leading_chunk_comments(raw_content)

    if apply_tokens:
        # Multi-table DML targets are always tables (_T kind).
        content = _apply_kind_aware_tokens(
            content,
            "T",
            apply_tokens,
            kind_index or {},
            prefix_mode_literals=prefix_mode_literals,
        )
        bad = find_malformed_tokens(content)
        if bad:
            result.warnings.append(
                f"Malformed tokens after substitution in "
                f"{os.path.basename(src_path)} "
                f"({len(bad)} marker(s))."
            )

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(content)

    rel_dest = os.path.relpath(dest_path, project_dir)
    result.classified += 1
    result.files_placed.append(
        (
            os.path.relpath(src_path, source_dir),
            rel_dest,
            "DML",
        )
    )
    targets_pretty = sorted(
        f"{db}.{obj}" if db else (obj or "?") for db, obj in dml_targets
    )
    result.multi_table_targets[rel_dest] = targets_pretty


def _place_ordered_sql(
    *,
    raw_content: str,
    src_path: str,
    source_dir: str,
    project_dir: str,
    payload_base: str,
    apply_tokens: Optional[Dict[str, str]],
    kind_index: Optional[Dict[str, str]],
    result: IngestResult,
    prefix_mode_literals: Optional[Set[str]] = None,
) -> None:
    """Place a mixed DCL/non-DCL source as one ordered SQL artefact.

    These scripts encode choreography such as GRANT -> action -> REVOKE.
    Splitting them into ordinary phase files changes their semantics, so
    SHIPS keeps the whole source together and deploys it via DIRECT_EXECUTE.
    """
    source_basename = os.path.splitext(os.path.basename(src_path))[0]
    dest_name = f"{source_basename}.ordered.osql"
    dest_dir = os.path.join(payload_base, "DML")
    os.makedirs(dest_dir, exist_ok=True)

    gitkeep = os.path.join(dest_dir, ".gitkeep")
    if os.path.exists(gitkeep):
        os.remove(gitkeep)

    content = raw_content
    if apply_tokens:
        content = _apply_kind_aware_tokens(
            content,
            "T",
            apply_tokens,
            kind_index or {},
            prefix_mode_literals=prefix_mode_literals,
        )

    dest_path = os.path.join(dest_dir, dest_name)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(content)

    rel_dest = os.path.relpath(dest_path, project_dir)
    result.classified += 1
    result.files_placed.append(
        (
            os.path.relpath(src_path, source_dir),
            rel_dest,
            "ORDERED_SQL",
        )
    )


def _clean_payload_tree(payload_base: str) -> int:
    """
    Remove harvest-owned files from a payload tree.

    Preserves:
        - ``.gitkeep`` markers (so empty directories stay tracked
          in git after the wipe).
        - Files whose name begins with ``_`` (control files such as
          user-curated ``_order.txt``). The pre-requisites
          ``_order.txt`` is regenerated by harvest and so could be
          truncated either way; preserving it covers the
          hand-curated copies in DDL/DCL/DML/post-install.

    Directory structure is left intact.

    Args:
        payload_base: Absolute path to ``payload/database`` (or
                      ``payload``) returned by ``_find_payload_base``.

    Returns:
        Number of files removed.
    """
    if not os.path.isdir(payload_base):
        return 0

    removed = 0
    for root, _dirs, files in os.walk(payload_base):
        for fname in files:
            if fname == ".gitkeep" or fname.startswith("_"):
                continue
            fpath = os.path.join(root, fname)
            try:
                os.remove(fpath)
                removed += 1
            except OSError as exc:
                logger.warning("Could not remove %s: %s", fpath, exc)
    return removed


def _emit_database_placement_mirror(
    project_dir: str,
    files_placed: List[Tuple[str, str, str]],
) -> Tuple[Optional[str], int]:
    """
    Build a human-facing copy of harvested files grouped by database/token.

    The canonical deployable payload remains unchanged under
    ``payload/database/...``. This mirror lives under
    ``.ships/harvest/by_database`` so developers can quickly spot
    placement mistakes such as authored views landing in ``*_T`` table
    databases without changing package semantics.

    Returns:
        Tuple of ``(relative mirror directory, files copied)``. The
        directory is returned even when no files are copied so callers can
        surface a stable location.
    """
    project_root = Path(project_dir)
    mirror_root = project_root / ".ships" / "harvest" / "by_database"

    if mirror_root.exists():
        shutil.rmtree(mirror_root)
    mirror_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    index_lines = [
        "# Harvest Placement Mirror",
        "",
        "Generated by harvest. Do not edit these copies; fix source and re-harvest.",
        "",
    ]

    for _src_rel, dest_rel, obj_type in files_placed:
        db_name = _database_from_payload_relpath(dest_rel)
        if not db_name:
            continue

        source_path = project_root / dest_rel
        if not source_path.is_file():
            continue

        db_dir = mirror_root / _placement_dir_name(db_name)
        kind_dir = db_dir / _placement_kind_dir(obj_type)
        kind_dir.mkdir(parents=True, exist_ok=True)

        dest_path = kind_dir / source_path.name
        shutil.copy2(source_path, dest_path)
        copied += 1

        kind_dir_name = _placement_kind_dir(obj_type)
        interpretation = _placement_interpretation(db_name, obj_type)
        index_lines.append(
            f"- {db_name} / {kind_dir_name} / {source_path.name} "
            f"-> {dest_rel.replace(os.sep, '/')}"
            f" — {interpretation}"
        )

    (mirror_root / "README.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8"
    )

    return os.path.relpath(mirror_root, project_dir), copied


def _database_from_payload_relpath(rel_path: str) -> Optional[str]:
    """Extract the database/token component from an eponymous payload filename."""
    filename = Path(rel_path).name
    if filename.startswith("{{"):
        end = filename.find("}}.")
        if end != -1:
            return filename[: end + 2]

    if "." not in filename:
        return None
    return filename.split(".", 1)[0]


def _placement_dir_name(database_name: str) -> str:
    """Return a filesystem-safe directory name for a database/token."""
    name = database_name.strip('"')
    if name.startswith("{{") and name.endswith("}}"):
        name = name[2:-2]
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _placement_kind_dir(obj_type: str) -> str:
    """Map a SHIPS object type to a readable mirror subdirectory."""
    mapping = {
        "TABLE": "tables",
        "VIEW": "views",
        "MACRO": "macros",
        "PROCEDURE": "procedures",
        "FUNCTION": "functions",
        "TRIGGER": "triggers",
        "JOIN_INDEX": "indexes",
        "HASH_INDEX": "indexes",
        "STATISTICS": "statistics",
        "COMMENT": "comments",
        "DML": "dml",
        "GRANT": "dcl",
        "REVOKE": "dcl",
        "DATABASE": "databases",
        "USER": "users",
    }
    return mapping.get(obj_type, obj_type.lower())


def _placement_interpretation(database_name: str, obj_type: str) -> str:
    """
    Return a plain-English placement hint for the harvest mirror index.

    These hints are advisory only. They intentionally focus on common OPS
    table/view-layer mistakes rather than trying to validate every possible
    site-specific placement convention.
    """
    token = database_name.strip()
    is_tables_db = token.endswith("_T}}") or token.upper().endswith("_T")
    is_views_db = token.endswith("_V}}") or token.upper().endswith("_V")

    if obj_type == "VIEW" and is_tables_db:
        return (
            "view is currently owned by a table-layer database; move the "
            "view owner to the matching _V token if this is a business view"
        )
    if obj_type == "VIEW" and is_views_db:
        return "view is owned by a view-layer database"
    if obj_type == "TABLE" and is_tables_db:
        return "table is owned by a table-layer database"
    if obj_type == "TABLE" and is_views_db:
        return (
            "table is currently owned by a view-layer database; check whether "
            "the owner token should be the matching _T token"
        )
    return "placement grouped by owning database/token"


# ---------------------------------------------------------------
# Internal — Multi-statement splitting
# ---------------------------------------------------------------


def _split_multi_statement(
    content: str,
    src_path: str,
) -> List[str]:
    """
    Split a file containing multiple DDL statements into individual
    statements.

    Strips comments before locating semicolons, so semicolons inside
    comments (e.g. '-- source->job->target;') do not cause false
    splits. The original content (with comments) is preserved for
    each output statement by tracking character positions.

    Handles all DDL, DCL, and metadata statement types:
        CREATE, REPLACE, DROP, ALTER — DDL objects
        GRANT, REVOKE               — DCL
        COMMENT ON                  — object/column metadata
        COLLECT STATISTICS          — statistics collection

    Does NOT split files containing stored procedures, functions,
    or macros — these have embedded semicolons within BEGIN...END
    blocks or parenthesised bodies that would be incorrectly split.

    Args:
        content:  The raw file content.
        src_path: Source file path (for logging).

    Returns:
        List of individual DDL statement strings. Returns a single-
        element list if the file contains only one statement or if
        splitting is not safe for this DDL type.
    """
    # Don't split files with procedure/function bodies (BEGIN...END)
    if re.search(r"\bBEGIN\b", content, re.IGNORECASE):
        return [content]

    # Don't split macros — body is parenthesised with internal semicolons
    if re.search(r"(?:CREATE|REPLACE)\s+MACRO\b", content, re.IGNORECASE):
        return [content]

    # --- Build a split-safe version of the content ---
    # Strip comments AND string literals (replaced by spaces with
    # newlines preserved so positions / line numbers survive). Without
    # the string strip, a literal like ``'AML/CTF Act; H rating ...'``
    # would split mid-string at the first ``;``, leaving an unterminated
    # quote and a corrupt second chunk in the output.
    from td_release_packager.sql_text import strip_comments_and_string_literals

    clean = strip_comments_and_string_literals(content)

    # --- Find statement-terminating semicolons in the clean version ---
    # A plain ``;`` scan corrupts Teradata triggers. Trigger bodies are
    # parenthesised and may contain inner SQL statements such as
    # ``INSERT ...;`` before the trigger's real terminator ``);``. Split
    # only on top-level semicolons so the inner statement terminator stays
    # with the trigger body instead of truncating the final closing ``);``.
    semi_positions = _top_level_semicolon_positions(clean)

    if len(semi_positions) <= 1:
        # Zero or one statement terminators — single statement, return as-is
        return [content]

    # --- Extract statements using original content + semicolon positions ---
    statements = []
    start = 0
    for pos in semi_positions:
        # Extract from original content (preserves comments)
        chunk = content[start : pos + 1].strip()
        start = pos + 1

        if not chunk:
            continue

        # Only keep chunks that contain a DDL/DCL/metadata verb
        # (checked against comment-stripped version to avoid matching
        # verbs inside comments)
        clean_chunk = _strip_comments(chunk)
        if re.search(
            r"\b(?:CREATE|REPLACE|DROP|ALTER|GRANT|REVOKE"
            r"|COMMENT\s+ON|COLLECT\s+STATISTICS"
            r"|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\b",
            clean_chunk,
            re.IGNORECASE,
        ):
            statements.append(chunk)

    # Trailing content after last semicolon (shouldn't happen but be safe)
    trailing = content[start:].strip()
    if trailing:
        clean_trailing = _strip_comments(trailing)
        if re.search(
            r"\b(?:CREATE|REPLACE|DROP|ALTER|GRANT|REVOKE"
            r"|COMMENT\s+ON|COLLECT\s+STATISTICS"
            r"|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\b",
            clean_trailing,
            re.IGNORECASE,
        ):
            statements.append(trailing)

    # Single statement or nothing — return original content unchanged
    if len(statements) <= 1:
        return [content]

    logger.info(
        "Multi-statement file split: %s -> %d statements",
        os.path.basename(src_path),
        len(statements),
    )
    return statements


def _split_multi_sqlj_jar_script(content: str, src_path: str) -> Optional[List[str]]:
    """Split a multi-alias SQLJ JAR script into one script per active alias.

    Legacy Teradata projects often keep several ``CALL SQLJ.*_JAR`` statements
    in one ``.ddl`` file with a leading ``DATABASE target_db;`` context. SHIPS
    treats each JAR alias as its own deployable object, so harvest fans those
    files out into atomic ``.sjr`` scripts while preserving the database context
    above each call.
    """
    from td_release_packager.sql_text import strip_comments_and_string_literals

    clean = strip_comments_and_string_literals(content)
    semi_positions = _top_level_semicolon_positions(clean)
    if len(semi_positions) <= 1:
        return None

    chunks: List[Tuple[str, str]] = []
    start = 0
    for pos in semi_positions:
        original = content[start : pos + 1].strip()
        clean_chunk = clean[start : pos + 1].strip()
        start = pos + 1
        if original:
            chunks.append((original, clean_chunk))

    trailing = content[start:].strip()
    if trailing:
        chunks.append((trailing, clean[start:].strip()))

    database_context = ""
    jar_calls: List[str] = []
    for original, clean_chunk in chunks:
        if not database_context and _DATABASE_CONTEXT_RE.match(clean_chunk):
            database_context = original
        elif _SQLJ_JAR_CALL_STMT_RE.search(clean_chunk):
            jar_calls.append(original)

    if len(jar_calls) <= 1:
        return None

    statements = [
        f"{database_context}\n\n{call}".strip() if database_context else call
        for call in jar_calls
    ]
    logger.info(
        "Multi-JAR script split: %s -> %d SQLJ scripts",
        os.path.basename(src_path),
        len(statements),
    )
    return statements


def _top_level_semicolon_positions(clean_sql: str) -> List[int]:
    """Return semicolon offsets that terminate top-level statements.

    ``clean_sql`` must have comments and string literals blanked while
    preserving character positions. Parentheses are still present, so this
    helper can avoid splitting on semicolons inside Teradata compound DDL
    bodies such as triggers::

        create trigger db.trg ... for each row (
            insert into db.log (...) values (...);
        );

    The semicolon after the inner ``insert`` is at parenthesis depth > 0
    and is not a statement boundary. The final semicolon after ``)`` is at
    depth 0 and is the split point.
    """
    positions: List[int] = []
    depth = 0

    for idx, char in enumerate(clean_sql):
        if char == "(":
            depth += 1
        elif char == ")":
            if depth > 0:
                depth -= 1
        elif char == ";" and depth == 0:
            positions.append(idx)

    return positions


# ---------------------------------------------------------------
# Internal — Comment stripping (for classification safety)
# ---------------------------------------------------------------


def _strip_leading_chunk_comments(chunk: str) -> str:
    """
    Strip leading whitespace and SQL comment blocks from a chunk.

    Source files typically have header comments above each
    statement that describe the file structure, section letter,
    or execution order — e.g.::

        -- ===========================================
        -- D. MEMORY  -  CHANGE LOG
        -- ===========================================

        INSERT INTO ...

    Once SHIPS classifies and renames the file eponymously, these
    headers reference a layout that no longer exists in the
    package. They survive only as noise. This helper drops every
    blank line and ``--`` / ``/* ... */`` comment block above the
    first non-comment statement and returns the rest of the chunk
    unchanged. Inline / trailing comments inside or below the
    statement are preserved.

    Args:
        chunk: A single statement (post-split, pre-write).

    Returns:
        The chunk with leading comments and blank lines removed.
        Returns the original chunk unchanged if it contains only
        comments and whitespace (defensive — avoids producing an
        empty file).
    """
    lines = chunk.splitlines(keepends=True)
    i = 0
    in_block = False
    while i < len(lines):
        stripped = lines[i].strip()
        if in_block:
            # Already inside a block comment from a previous line
            if "*/" in stripped:
                in_block = False
            i += 1
            continue
        if not stripped:
            # Blank line
            i += 1
            continue
        if stripped.startswith("--"):
            # Single-line comment
            i += 1
            continue
        if stripped.startswith("/*"):
            # Block comment — may span multiple lines
            if "*/" not in stripped[2:]:
                in_block = True
            i += 1
            continue
        # First non-blank, non-comment line — stop here
        break

    # All lines were comments / whitespace — return original to
    # avoid producing an empty .dml / .cmt / .dcl file.
    if i >= len(lines):
        return chunk

    return "".join(lines[i:])


def _strip_comments(content: str) -> str:
    """
    Strip SQL comments from content before classification.

    Prevents false matches from DDL keywords appearing inside
    comments (e.g. '-- uses CREATE DATABASE IF NOT EXISTS pattern'
    would otherwise match as CREATE DATABASE IF).

    Strips:
        - Single-line comments: -- to end of line
        - Block comments: /* ... */ (non-nested)

    Args:
        content: Raw DDL file content.

    Returns:
        Content with comments replaced by whitespace.
    """
    # Block comments first (non-greedy)
    result = re.sub(r"/\*.*?\*/", " ", content, flags=re.DOTALL)
    # Single-line comments
    result = re.sub(r"--[^\n]*", " ", result)
    return result


# ---------------------------------------------------------------
# Internal — Classification
# ---------------------------------------------------------------


def _classify_ddl(content: str) -> Optional[str]:
    """
    Classify DDL content by object type — backward-compat shim.

    Delegates to ``td_release_packager.classifier.classify`` and
    returns the BASE type so existing call sites that index into
    ``_TYPE_TO_SUBDIR`` / ``_TYPE_TO_EXT`` keep working. Callers
    that want sub-types (FUNCTION_C, PROCEDURE_JAVA), confidence,
    external references, or filename-mismatch warnings should call
    ``classifier.classify(path, content)`` directly.

    Args:
        content: The DDL file content.

    Returns:
        Base object type string (TABLE, VIEW, FUNCTION, PROCEDURE,
        ...), or None if unclassifiable.
    """
    from td_release_packager.classifier import classify, base_type

    result = classify(path="", content=content)
    return base_type(result.type)


def _extract_qualified_name(content: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract database.object name from DDL content.

    Tries multiple patterns in order:
        1. Standard DDL: CREATE/REPLACE ... Database.Object
        2. COMMENT ON TABLE/COLUMN Database.Object[.column]
        3. COLLECT STATISTICS ... ON Database.Object
        4. INSERT / UPDATE / DELETE / MERGE — DML target
        5. GRANT / REVOKE ... ON [object-kind] Database[.Object]

    For COMMENT ON COLUMN, the column name is ignored — only the
    database.table portion is used for eponymous naming.

    String literals are blanked before matching so that text inside
    an IS-clause (``COMMENT ON TABLE x IS '... COMMENT ON TABLE
    other'``) or a sql_template column in an INSERT cannot
    masquerade as a real statement and produce nonsense filenames
    like ``and.dml``.

    Args:
        content: The DDL file content (already comment-stripped).

    Returns:
        Tuple of (database_name, object_name), either may be None.
    """
    from td_release_packager.sql_text import (
        strip_string_literals_preserving_positions,
    )

    # Blank string literals so embedded SQL-looking text inside
    # quoted values (CHANGE_LOG descriptions, sql_template columns,
    # COMMENT ON IS clauses) cannot be mistaken for real statements.
    # Position-preserving so capture groups still align with the
    # original characters when needed.
    scan = strip_string_literals_preserving_positions(content)

    # --- Standard DDL (CREATE/REPLACE) ---
    # Anchored to statement start so DCL like
    # ``GRANT CREATE TABLE ON db TO role`` is not mistaken for
    # ``CREATE TABLE ON`` and harvested as ``ON.dcl``.
    match = _QUALIFIED_NAME_RE.search(scan)
    if match:
        qualified = match.group(1)
        parts = qualified.replace('"', "").split(".")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        elif len(parts) == 1:
            return (None, parts[0].strip())

    # --- COMMENT ON {TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|
    #                 COLUMN|DATABASE|USER|ROLE|PROFILE} ---
    match = _COMMENT_ON_NAME_RE.search(scan)
    if match:
        qualified = match.group(1)
        parts = qualified.replace('"', "").split(".")
        # COMMENT ON TABLE / VIEW / MACRO / ... db.object → 2 parts
        # COMMENT ON COLUMN db.table.column           → 3 parts
        #   (column dropped so all comments on the same table
        #   aggregate into one .cmt file)
        # COMMENT ON DATABASE / USER / ROLE / PROFILE name → 1 part
        #   (system-scope object — no database qualifier)
        if len(parts) >= 2:
            return (parts[0].strip(), parts[1].strip())
        elif len(parts) == 1:
            return (None, parts[0].strip())

    # --- COLLECT / UPDATE STATISTICS ... ON ---
    match = _COLLECT_STATS_NAME_RE.search(scan)
    if match:
        qualified = match.group(1)
        parts = qualified.replace('"', "").split(".")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())

    # --- INSERT / UPDATE / DELETE / MERGE → DML target ---
    match = _DML_NAME_RE.search(scan)
    if match:
        qualified = match.group(1)
        parts = qualified.replace('"', "").split(".")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        elif len(parts) == 1:
            return (None, parts[0].strip())

    # --- GRANT / REVOKE ... ON object ---
    # Last because GRANT/REVOKE statements may appear alongside
    # other patterns; the ON clause is the unambiguous marker.
    match = _GRANT_REVOKE_NAME_RE.search(scan)
    if match:
        qualified = match.group(1)
        parts = qualified.replace('"', "").split(".")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        elif len(parts) == 1:
            return (None, parts[0].strip())

    return (None, None)


# -- SPECIFIC function name for overloaded functions --
_SPECIFIC_NAME_RE = re.compile(
    r'SPECIFIC\s+("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)


def _extract_specific_function_name(
    content: str,
) -> Optional[str]:
    """
    Extract the SPECIFIC name from a function DDL body.

    Teradata supports function overloading — same function name,
    different parameter signatures. Each overload has a unique
    SPECIFIC name declared in the DDL body:

        REPLACE FUNCTION db.fn_name (param1 INT)
        RETURNS INT
        SPECIFIC db.specific_name
        ...

    If a SPECIFIC clause is found, returns the object name part
    (without database qualifier). This is used as the eponymous
    filename to avoid collisions between overloads.

    Args:
        content: The function DDL content.

    Returns:
        The specific name, or None if no SPECIFIC clause found.
    """
    match = _SPECIFIC_NAME_RE.search(content)
    if not match:
        return None

    qualified = match.group(1).replace('"', "")
    parts = qualified.split(".")
    # Return just the object name (last part)
    return parts[-1].strip()


def _extract_sqlj_jar_alias(content: str) -> Optional[str]:
    """Extract the SQLJ JAR alias so harvested JAR scripts are eponymous."""
    match = _SQLJ_JAR_ALIAS_RE.search(_strip_comments(content))
    if not match:
        return None
    alias = match.group("alias").replace("''", "'").strip()
    return alias or None


# ---------------------------------------------------------------
# Internal — Normalisation
# ---------------------------------------------------------------


# BTEQ command line pattern: a dot as the first non-whitespace
# character on a line, followed immediately by a letter.
#
# BTEQ commands (.IF, .GOTO, .LABEL, .LOGON, .LOGOFF, .QUIT,
# .EXIT, .RUN, .IMPORT, .EXPORT, .REMARK, .WIDTH, .SET, ...)
# are all-uppercase keywords after the dot. SQL never starts a
# statement with a leading dot so the pattern is unambiguous.
#
# These lines are generated by legacy BTEQ-driven deployment
# scripts to provide flow control (error checking, session
# management). SHIPS deploys SQL directly and handles errors
# through its own mechanisms — BTEQ control flow has no meaning
# in the SHIPS context and must be stripped before packaging so
# the deployer can parse the underlying SQL.
_BTEQ_LINE_RE = re.compile(r"^\s*\.[A-Za-z][^\n]*$", re.MULTILINE)

# -- Pre-requisite parent extraction ----------------------------
# Teradata syntax:
#   CREATE DATABASE x FROM y AS ...
#   CREATE USER     x FROM y AS ...
# The FROM clause names the parent database/user, which must
# physically exist on the target BEFORE x can be created. Multiple
# objects may form a hierarchy (y1 → y2 → y3 → DBC), so the
# deployer must process them in dependency order. This regex
# extracts the names so we can topologically sort the payload files.
_PREREQ_FROM_RE = re.compile(
    r"^\s*CREATE\s+(?:DATABASE|USER)\s+"
    r"(\{\{[A-Za-z_]\w*\}\}|['\"]?[A-Za-z_]\w*['\"]?)"  # name: {{TOKEN}}, 'quoted', or bare
    r"\s+FROM\s+"
    r"(\{\{[A-Za-z_]\w*\}\}|['\"]?[A-Za-z_]\w*['\"]?)",  # parent: same
    re.IGNORECASE | re.MULTILINE,
)


def _extract_prereq_parent(content: str) -> Optional[Tuple[str, str]]:
    """Extract (object_name, parent_name) from a CREATE DATABASE/USER statement.

    Teradata creates databases and users under a parent via the ``FROM``
    clause. The parent must already exist on the target. This function
    extracts both names so harvest can compute the correct deployment
    order across all files in the prereqs phase.

    Args:
        content: Cleaned (BTEQ-stripped) content of a ``.db`` or ``.usr``
                 file.

    Returns:
        ``(name, parent)`` tuple, both as upper-cased strings so
        comparison is case-insensitive (Teradata identifiers are
        case-insensitive by default). ``None`` when no FROM clause is
        found (e.g. the file may have incomplete DDL).
    """
    m = _PREREQ_FROM_RE.search(content)
    if not m:
        return None
    name = m.group(1).strip("'\"").upper()
    parent = m.group(2).strip("'\"").upper()
    return (name, parent)


def _emit_prereq_order(prereq_dir: str) -> Any:
    """Topologically sort pre-requisite files by their FROM dependencies
    and write ``_order.txt`` into ``prereq_dir``.

    Reads every ``.db`` and ``.usr`` file in ``prereq_dir/databases/``
    and ``prereq_dir/users/``, extracts the ``CREATE ... FROM <parent>``
    dependency for each, and produces a topological ordering where every
    parent deploys before its child.

    **Ordering guarantee:** only files whose ``FROM <parent>`` clause is
    parseable are guaranteed to deploy after their parent. Files where
    the dependency cannot be determined (missing or unreadable ``FROM``
    clause) are placed first in alphabetical order — which may or may
    not be correct. The caller receives a list of unresolvable files in
    ``result.unresolvable`` so the harvest banner can warn the user.

    You cannot rely on alphabetical ordering being correct: site naming
    conventions vary and naming a child after its parent (so it sorts
    later) is a convention, not something SHIPS can enforce.

    The ordering is written to ``prereq_dir/_order.txt`` with relative
    paths (``databases/X.db``, ``users/Y.usr``) compatible with the
    deployer's ``read_order_file`` helper.

    Args:
        prereq_dir: Absolute path to the ``pre-requisites/`` directory
                    inside the SHIPS project payload.

    Returns:
        ``PrereqOrderResult`` with the ordered paths and any files
        whose dependencies could not be resolved. Empty when no
        prereq files were found.
    """
    from dataclasses import dataclass as _dc, field as _field

    @_dc
    class PrereqOrderResult:
        ordered: list = _field(default_factory=list)
        unresolvable: list = _field(default_factory=list)  # (rel_path, reason)

    result = PrereqOrderResult()

    # Collect relative_path, upper-cased name, upper-cased parent
    entries: list = []
    for subdir in ("databases", "users"):
        sub_path = os.path.join(prereq_dir, subdir)
        if not os.path.isdir(sub_path):
            continue
        for filename in sorted(os.listdir(sub_path)):
            if filename.startswith("_") or filename.startswith("."):
                continue
            fp = os.path.join(sub_path, filename)
            try:
                content = Path(fp).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            dep = _extract_prereq_parent(content)
            rel_path = f"{subdir}/{filename}"
            if dep:
                name, parent = dep
                entries.append((rel_path, name, parent))
            else:
                # No FROM clause found — we cannot determine the
                # deployment dependency for this file. It will be
                # placed first (alphabetically relative to other
                # unresolvable files) but there is no guarantee this
                # is correct.
                result.unresolvable.append(
                    (
                        rel_path,
                        "no CREATE DATABASE/USER FROM <parent> clause found",
                    )
                )
                entries.append((rel_path, None, None))

    if not entries:
        return result

    # Build name → relative-path index
    name_to_rel: dict = {name: rel for rel, name, _ in entries if name is not None}

    # Build adjacency (rel_path → rel_path_of_parent, if in-package)
    parent_map: dict = {}
    for rel, name, parent in entries:
        if parent is not None and parent in name_to_rel:
            parent_map[rel] = name_to_rel[parent]

    # DFS-based topological sort — visits parent before child
    ordered: list = []
    visited: set = set()

    def _visit(rel: str) -> None:
        if rel in visited:
            return
        visited.add(rel)
        if rel in parent_map:
            _visit(parent_map[rel])
        ordered.append(rel)

    for rel, _, _ in entries:
        _visit(rel)

    result.ordered = ordered

    # Write _order.txt
    order_path = os.path.join(prereq_dir, "_order.txt")
    with open(order_path, "w", encoding="utf-8") as fh:
        fh.write("# _order.txt — pre-requisites deployment order\n")
        fh.write("# Generated by SHIPS harvest: parents before children\n")
        fh.write("# (derived from CREATE DATABASE/USER FROM <parent>)\n")
        if result.unresolvable:
            fh.write("#\n")
            fh.write("# WARNING: the following files have no parseable FROM clause.\n")
            fh.write("# Their position in this file is alphabetical and may be\n")
            fh.write("# INCORRECT if they depend on other prereqs in this package.\n")
            fh.write("# Verify manually or add the FROM <parent> clause to source.\n")
            for rel, _ in result.unresolvable:
                fh.write(f"#   {rel}\n")
        fh.write("#\n")
        for rel in ordered:
            fh.write(rel + "\n")

    logger.info(
        "Pre-requisites order: wrote _order.txt (%d files, %d unresolvable)",
        len(ordered),
        len(result.unresolvable),
    )
    return result


def _strip_bteq_commands(content: str) -> Tuple[str, int]:
    """Remove BTEQ control commands from SQL content.

    Identifies and removes any line where the first non-whitespace
    character is a ``.`` followed by a letter — the universal
    signature of a BTEQ command. Multiple consecutive blank lines
    left behind are collapsed to a single blank line.

    Args:
        content: Raw file content possibly containing BTEQ commands.

    Returns:
        ``(cleaned_content, lines_stripped)`` — the cleaned SQL and
        the count of BTEQ lines removed.  If no BTEQ lines were
        found, ``(content, 0)`` is returned unchanged.
    """
    lines_stripped = 0

    def _mark_and_remove(m: "re.Match") -> str:
        nonlocal lines_stripped
        lines_stripped += 1
        return ""

    cleaned = _BTEQ_LINE_RE.sub(_mark_and_remove, content)

    if lines_stripped == 0:
        return (content, 0)

    # Collapse runs of three or more newlines left behind after
    # stripping to a single blank line so the output reads cleanly.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return (cleaned.strip() + "\n", lines_stripped)


def _inject_multiset(content: str) -> Tuple[str, bool]:
    """Inject MULTISET if neither SET nor MULTISET is specified.

    Comment-stripped content is used for BOTH the detection and the
    injection-position lookup. Without stripping, a header comment
    containing ``CREATE TABLE`` would match before the real DDL and
    MULTISET would get injected into the comment instead of into
    the actual CREATE statement.
    """
    from td_release_packager.sql_text import (
        strip_comments_and_string_literals,
    )

    # Strip BOTH comments AND string literals. Procedures sometimes
    # build dynamic SQL like ``'CREATE TABLE '||...`` — we must not
    # match the keyword inside the literal as if it were real DDL.
    cleaned = strip_comments_and_string_literals(content)

    if _HAS_SET_MULTISET_RE.search(cleaned):
        return (content, False)

    m = _INJECT_MULTISET_RE.search(cleaned)
    if m is None:
        return (content, False)

    # Position-preserving strip means non-comment characters in
    # cleaned occupy the same offsets as in the original — so the
    # match span [m.start():m.end()] in cleaned identifies the
    # exact CREATE...TABLE substring in the original. Apply the
    # substitution to that span, leaving comments untouched.
    head = content[: m.start()]
    matched = content[m.start() : m.end()]
    tail = content[m.end() :]
    new_matched = _INJECT_MULTISET_RE.sub(r"\1MULTISET \2", matched, count=1)
    modified = head + new_matched + tail
    return (modified, modified != content)


def _inject_replace_view(content: str) -> Tuple[str, bool]:
    """
    Convert CREATE VIEW to REPLACE VIEW for idempotency.

    If the DDL already uses REPLACE VIEW, no change is made.

    Comment-stripped content is used for detection and position
    lookup so that ``CREATE VIEW`` appearing inside a header
    comment doesn't get rewritten to ``REPLACE VIEW`` (corrupting
    the comment and missing the real CREATE further down).

    Args:
        content: The view DDL content.

    Returns:
        Tuple of (modified_content, was_injected).
    """
    from td_release_packager.sql_text import (
        strip_comments_and_string_literals,
    )

    cleaned = strip_comments_and_string_literals(content)

    if _HAS_REPLACE_VIEW_RE.search(cleaned):
        return (content, False)

    m = _CREATE_VIEW_RE.search(cleaned)
    if m is None:
        return (content, False)

    head = content[: m.start()]
    matched = content[m.start() : m.end()]
    tail = content[m.end() :]
    new_matched = _CREATE_VIEW_RE.sub("REPLACE VIEW", matched, count=1)
    modified = head + new_matched + tail
    if modified != content:
        return (modified, True)

    return (content, False)


# ---------------------------------------------------------------
# Internal — Token candidate detection
# ---------------------------------------------------------------


#: Pattern matching a fully-formed ``{{TOKEN}}`` reference. Names
#: that already match this shape are not "candidates" — they're
#: the end-state token-candidate detection is supposed to lead to.
_ALREADY_TOKEN_RE = re.compile(r"^\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}$")


def _build_token_candidates(
    db_names: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    Build a report of hardcoded database/user names that should
    become tokens.

    Groups by name, reports which files reference each.
    Filters out:
      - Known system databases (DBC, SYSUDTLIB, etc.) that should
        remain hardcoded.
      - Already-tokenised references (``{{NAME}}`` shape) — these
        are not candidates, they're the goal state.

    Args:
        db_names: Dict of database_name → list of files referencing it.

    Returns:
        Dict of database_name → list of files (excluding system DBs
        and existing tokens).
    """
    # System databases that should remain hardcoded
    system_dbs = {
        "DBC",
        "SYSUDTLIB",
        "SYSLIB",
        "SYSJDBC",
        "SYSBAR",
        "SYSTEMFE",
        "SYSSPATIAL",
        "TD_SYSFNLIB",
        "TD_SYSXML",
        "TDSTATS",
        "TDWM",
        "TD_SYSGPL",
        "ALL",
        "DEFAULT",
        "PUBLIC",
        "EXTUSER",
    }

    candidates = {}
    for db_name, files in sorted(db_names.items()):
        if db_name.upper() in system_dbs:
            continue
        if _ALREADY_TOKEN_RE.match(db_name):
            # {{TOKEN}} references are not candidates; they're the
            # goal. Filtering them out lets the harvest banner detect
            # "already tokenised" cleanly.
            continue
        candidates[db_name] = files

    return candidates


# ---------------------------------------------------------------
# Internal — View name affix normalisation
# ---------------------------------------------------------------


def _remove_view_type_affix(object_name: str) -> str:
    """
    Remove redundant view type affixes from an object name.

    Only the explicit type affixes are removed:
      - leading ``v_`` / ``V_``
      - trailing ``_v`` / ``_V``

    Meaningful domain text containing ``v`` is left alone.
    """
    cleaned = re.sub(r"^v_", "", object_name, count=1, flags=re.IGNORECASE)
    cleaned = re.sub(r"_v$", "", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned or object_name


def _build_view_type_affix_renames(
    source_files: List[str],
    legacy_migration_rules: Optional[List[MigrationRule]],
) -> Dict[Tuple[str, str], str]:
    """
    Build view object rename rules from source definitions.

    The result maps ``(database, old_object_name)`` to ``new_object_name``.
    Only VIEW definitions participate; table/procedure/etc. objects are
    deliberately out of scope.
    """
    from td_release_packager.classifier import classify, base_type

    renames: Dict[Tuple[str, str], str] = {}
    for src_path in source_files:
        raw_content = _read_file(src_path)
        if raw_content is None:
            continue
        if legacy_migration_rules:
            raw_content, _hits = apply_migration_rules_to_text(
                raw_content, legacy_migration_rules
            )
        raw_content, _n_bteq = _strip_bteq_commands(raw_content)
        clean_file = _strip_comments(raw_content)
        if not (
            _HAS_REPLACE_VIEW_RE.search(clean_file)
            or _CREATE_VIEW_RE.search(clean_file)
        ):
            continue
        try:
            statements = _split_multi_sqlj_jar_script(
                raw_content, src_path
            ) or _split_multi_statement(raw_content, src_path)
        except Exception:
            statements = [raw_content]

        for statement in statements:
            clean = _strip_comments(statement)
            try:
                classification = classify(path=src_path, content=clean)
                if base_type(classification.type) != "VIEW":
                    continue
            except Exception:
                continue

            db_name, obj_name = _extract_qualified_name(clean)
            if not db_name or not obj_name:
                continue
            new_name = _remove_view_type_affix(obj_name)
            if new_name != obj_name:
                renames[(db_name, obj_name)] = new_name

    return renames


def _apply_view_type_affix_renames(
    content: str,
    renames: Dict[Tuple[str, str], str],
) -> str:
    """
    Apply qualified view object renames outside comments and string literals.
    """
    if not renames:
        return content

    from td_release_packager.sql_text import strip_comments_and_string_literals

    scan = strip_comments_and_string_literals(content)
    replacements: List[Tuple[int, int, str]] = []

    for (db_name, old_obj), new_obj in renames.items():
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(db_name)}\s*\.\s*"
            rf"{re.escape(old_obj)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(scan):
            replacements.append((match.start(), match.end(), f"{db_name}.{new_obj}"))

    if not replacements:
        return content

    replacements.sort(key=lambda item: item[0], reverse=True)
    out = content
    for start, end, replacement in replacements:
        out = out[:start] + replacement + out[end:]
    return out


# ---------------------------------------------------------------
# Kind-aware token substitution (Phase 1 + 2)
# ---------------------------------------------------------------


def _build_source_kind_index(source_files: List[str]) -> Dict[str, str]:
    """Build a ``"db.obj" → kind_suffix`` index from a list of source files.

    Called before the main ingest loop so that cross-reference rewrites
    in Layer B know the kind of every object defined in the source set.

    Multi-statement files are split; each statement contributes its
    (db, obj, kind) independently.  Classification errors in individual
    statements are silently skipped — unresolvable references will fall
    back to ``EXTERNAL_KIND_DEFAULT`` at substitution time.

    Args:
        source_files: Paths to the raw source files to pre-scan.

    Returns:
        Dict mapping lowercased ``"db.obj"`` keys to kind-suffix letters.
    """
    from td_release_packager.classifier import classify, base_type
    from td_release_packager.kind_suffix import TYPE_TO_KIND

    kind_index: Dict[str, str] = {}

    for src_path in source_files:
        content = _read_file(src_path)
        if content is None:
            continue
        try:
            statements = _split_multi_statement(content, src_path)
        except Exception:
            statements = [content]

        for stmt in statements:
            clean = _strip_comments(stmt)
            try:
                cls = classify(path=src_path, content=clean)
                bt = base_type(cls.type)
                kind = TYPE_TO_KIND.get(bt)
                if kind is None:
                    continue
                db_name, obj_name = _extract_qualified_name(clean)
                if db_name and obj_name:
                    key = f"{db_name.lower()}.{obj_name.lower()}"
                    kind_index[key] = kind
            except Exception:
                pass  # classification errors handled in main loop

    return kind_index


def _detect_prefix_mode_literals(
    source_files: List[str], apply_tokens: Dict[str, str]
) -> Set[str]:
    """Classify each ``apply_tokens`` literal as prefix or full-DB shape.

    A literal qualifies as **prefix mode** when it appears as the
    leading identifier segment (``literal`` followed by ``_`` and at
    least one identifier character) anywhere in ``source_files``.
    Such literals are routed to identifier-aware substitution in
    :func:`_apply_kind_aware_tokens`, which emits the token value
    verbatim and never injects a ``_T`` / ``_V`` suffix inside the
    braces — eliminating the ``{{PREFIX_T}}`` malformation reported
    against the original substring-based ``--token-map`` behaviour
    (see issue #311).

    Args:
        source_files: Raw DDL source paths to scan.
        apply_tokens: Token-map dict (literal → ``"{{TOKEN}}"``).

    Returns:
        Set of literals that should use prefix mode.
    """
    if not apply_tokens:
        return set()

    # Literals that the user has explicitly mapped as a longer
    # extension of another mapped literal (e.g. ``A_B`` and ``A_B_V``
    # in the same token map) are full-DB entries, not prefix shapes —
    # the user intends each to match its own full name.  Exclude the
    # shorter literal from prefix-mode classification so the kind-
    # aware path is preserved for it.
    literals = list(apply_tokens)
    extended_by_another: Set[str] = set()
    for short in literals:
        for longer in literals:
            if longer is short:
                continue
            if longer.lower().startswith(short.lower() + "_"):
                extended_by_another.add(short)
                break

    patterns = {
        literal: re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(literal) + r"_[A-Za-z0-9]",
            re.IGNORECASE,
        )
        for literal in apply_tokens
        if literal not in extended_by_another
    }

    found: Set[str] = set()
    pending = set(patterns)
    for src_path in source_files:
        if not pending:
            break
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        for literal in list(pending):
            if patterns[literal].search(content):
                found.add(literal)
                pending.discard(literal)
    return found


def _apply_kind_aware_tokens(
    content: str,
    file_kind: str,
    apply_tokens: Dict[str, str],
    kind_index: Dict[str, str],
    prefix_mode_literals: Optional[Set[str]] = None,
) -> str:
    """Apply token substitution to DDL content.

    Two modes per ``apply_tokens`` entry:

    * **prefix mode** — the literal appears as the *leading segment*
      of identifiers in the payload (e.g. ``CallCentre`` in
      ``CallCentre_DOM_STD_T``).  Substitution is identifier-aware and
      emits the token value **verbatim** with no ``_T`` / ``_V``
      suffix.  This is the mode required by Model B (issue #309 / #311)
      and the one that fixes the ``{{PREFIX_T}}`` malformation bug.
    * **full-DB / kind-aware mode** — every other literal.  Each
      qualified ``DB.ObjectName`` reference picks its kind suffix from
      ``kind_index`` (``EXTERNAL_KIND_DEFAULT`` for external refs);
      bare DB references use ``file_kind``.

    All matching is done against a scratch copy with SQL comments and
    string literals blanked so that semicolons, dots, and identifiers
    inside literals or comments are never mistaken for SQL structure.
    Existing ``{{TOKEN}}`` markers in the content are also blanked in
    the scratch copy so re-running harvest does not double-tokenise.

    Replacements are collected as ``(start, end, new_text)`` tuples and
    applied in reverse position order so earlier positions remain valid
    after each substitution (length changes after each replacement).

    Args:
        content:              Raw DDL text to rewrite.
        file_kind:            Kind suffix for this file's owner clause
                              (``'T'``, ``'V'``, etc.).  Derived from
                              the classified type.
        apply_tokens:         Dict of ``{literal_db_name: "{{BASE_TOKEN}}"}``
                              as produced by ``token_engine.read_token_map()``.
        kind_index:           Dict of ``{"db.obj" (lowercased): kind_suffix}``
                              as produced by ``_build_source_kind_index()``.
        prefix_mode_literals: Optional set of literals that should use
                              identifier-aware prefix substitution
                              instead of kind-aware substitution.  When
                              ``None`` or empty, every entry uses the
                              kind-aware path (the pre-#311 behaviour).

    Returns:
        Rewritten content with kind-suffixed tokens in place of literals.
    """
    prefix_mode_literals = prefix_mode_literals or set()
    from td_release_packager.sql_text import strip_comments_and_string_literals
    from td_release_packager.kind_suffix import (
        EXTERNAL_KIND_DEFAULT,
        SYSTEM_DATABASES,
        has_kind_suffix,
    )

    # Build a scratch copy with comments, string literals, and existing
    # {{TOKEN}} markers blanked so only real SQL structure is visible.
    scratch = strip_comments_and_string_literals(content)
    # Blank existing {{TOKEN}} markers (position-preserving) so a
    # second harvest run doesn't double-tokenise already-rewritten text.
    scratch = re.sub(
        r"\{\{[A-Za-z_][A-Za-z0-9_-]*\}\}",
        lambda m: " " * len(m.group()),
        scratch,
    )

    replacements: List[tuple] = []

    for literal, token_with_braces in apply_tokens.items():
        # Extract base token name: "{{MortgagePlatform_Domain}}" → "MortgagePlatform_Domain"
        base_token = token_with_braces.strip("{}")

        # ---- Prefix mode (issue #311) ----
        # The literal appears as the leading segment of identifiers in
        # the payload; emit the token verbatim and let the structural
        # remainder (``_DOM_STD_T`` etc.) stay literal outside the
        # braces.  The right-edge look-ahead is non-consuming so no
        # adjacent character ever lands inside ``{{ }}``.
        if literal in prefix_mode_literals:
            prefix_re = re.compile(
                r"(?<![A-Za-z0-9_])"
                + re.escape(literal)
                + r"(?=_[A-Za-z0-9]|[^A-Za-z0-9_]|$)",
                re.IGNORECASE,
            )
            for m in prefix_re.finditer(scratch):
                replacements.append((m.start(), m.end(), token_with_braces))
            continue

        # ---- Compatibility / fall-through guards ----
        # If the literal is a system DB (DBC, SYSLIB, etc.), or if the
        # literal or base token already carry a kind suffix (_T, _V, etc.),
        # use plain word-boundary substitution — adding a second kind suffix
        # would produce double-encoded tokens ({{SEM_DATABASE_V_V}}).
        # This preserves full backward compatibility with token_map.conf
        # files that were written before kind-aware tokenisation.
        if (
            literal.upper() in SYSTEM_DATABASES
            or has_kind_suffix(literal)
            or has_kind_suffix(base_token)
        ):
            plain_re = re.compile(r"\b" + re.escape(literal) + r"\b", re.IGNORECASE)
            for m in plain_re.finditer(scratch):
                replacements.append((m.start(), m.end(), token_with_braces))
            continue

        # ---- Layer B: qualified references (DB.ObjectName) ----
        # Replace only the DB portion; leave ".ObjectName" untouched.
        qual_re = re.compile(
            r"\b" + re.escape(literal) + r"\.(\w+)",
            re.IGNORECASE,
        )
        qual_starts: set = set()
        for m in qual_re.finditer(scratch):
            obj_name = m.group(1)
            key = f"{literal.lower()}.{obj_name.lower()}"
            kind = kind_index.get(key, EXTERNAL_KIND_DEFAULT)
            db_start = m.start()
            db_end = db_start + len(literal)
            kind_token = "{{" + base_token + "_" + kind + "}}"
            replacements.append((db_start, db_end, kind_token))
            qual_starts.add(db_start)

        # ---- Layer A: bare DB references (no ".ObjectName") ----
        # Used for: GRANT ON <database>, comments referencing the DB, etc.
        # Use the containing file's kind (owner clause context).
        bare_re = re.compile(
            r"\b" + re.escape(literal) + r"\b",
            re.IGNORECASE,
        )
        for m in bare_re.finditer(scratch):
            if m.start() in qual_starts:
                continue  # already captured as a qualified reference
            # Skip if immediately followed by a dot (edge case where
            # qual_re didn't match because \w+ failed — e.g. quoted name)
            if m.end() < len(scratch) and scratch[m.end()] == ".":
                continue
            kind_token = "{{" + base_token + "_" + file_kind + "}}"
            replacements.append((m.start(), m.end(), kind_token))

    if not replacements:
        return content

    # Apply in reverse position order so earlier positions stay valid.
    replacements.sort(key=lambda r: r[0], reverse=True)
    result = content
    for start, end, new_text in replacements:
        result = result[:start] + new_text + result[end:]

    return result
