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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from td_release_packager.token_engine import _TOKEN_RE

logger = logging.getLogger(__name__)


# -- Extension mapping by object type --
_TYPE_TO_EXT = {
    # Environment-scoped DDL objects
    "TABLE": ".tbl",
    "JOIN_INDEX": ".jix",
    "HASH_INDEX": ".idx",
    "INDEX": ".idx",
    "VIEW": ".viw",
    "MACRO": ".mcr",
    "PROCEDURE": ".spl",
    "FUNCTION": ".fnc",
    "TRIGGER": ".trg",
    "DATABASE": ".db",
    "USER": ".usr",
    "GRANT": ".dcl",
    "REVOKE": ".dcl",
    "JAR": ".jcl",
    "SCRIPT_TABLE_OPERATOR": ".sto",
    # System-scoped objects
    "MAP": ".map",
    "ROLE": ".rol",
    "PROFILE": ".prf",
    "AUTHORIZATION": ".auth",
    "FOREIGN_SERVER": ".fsvr",
    # Supporting artefacts (not deployed)
    "C_SOURCE": ".c",
    "C_HEADER": ".h",
}

# -- Subdirectory mapping by object type --
_TYPE_TO_SUBDIR = {
    # Environment-scoped DDL objects
    "TABLE": "DDL/tables",
    "JOIN_INDEX": "DDL/join_indexes",
    "HASH_INDEX": "DDL/join_indexes",
    "INDEX": "DDL/join_indexes",
    "VIEW": "DDL/views",
    "MACRO": "DDL/macros",
    "PROCEDURE": "DDL/procedures",
    "FUNCTION": "DDL/functions",
    "TRIGGER": "DDL/triggers",
    "DATABASE": "pre-requisites/databases",
    "USER": "pre-requisites/users",
    "GRANT": "DCL/inter_db",
    "REVOKE": "DCL/inter_db",
    "JAR": "DDL/JARs",
    "SCRIPT_TABLE_OPERATOR": "DDL/script_table_operators",
    # System-scoped objects (00_system phase)
    "MAP": "system/maps",
    "ROLE": "system/roles",
    "PROFILE": "system/profiles",
    "AUTHORIZATION": "system/authorizations",
    "FOREIGN_SERVER": "system/foreign_servers",
    # Supporting artefacts travel with their parent
    "C_SOURCE": "DDL/functions",
    "C_HEADER": "DDL/functions",
}

# -- Classification patterns (order matters — specific before general) --
_CLASSIFY_PATTERNS = [
    # Indexes (most specific first)
    (re.compile(r'CREATE\s+JOIN\s+INDEX\b', re.I), "JOIN_INDEX"),
    (re.compile(r'CREATE\s+HASH\s+INDEX\b', re.I), "HASH_INDEX"),
    (re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\b', re.I), "INDEX"),
    # Script Table Operator (before FUNCTION — it uses FUNCTION syntax
    # but with TABLE OPERATOR in the body)
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:SPECIFIC\s+)?FUNCTION\b.*?TABLE\s+OPERATOR', re.I | re.DOTALL), "SCRIPT_TABLE_OPERATOR"),
    # Standard DDL objects
    (re.compile(r'(?:CREATE|REPLACE)\s+(?:MULTISET|SET)?\s*(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?(?:TRACE\s+)?TABLE\b', re.I), "TABLE"),
    (re.compile(r'(?:CREATE|REPLACE)\s+VIEW\b', re.I), "VIEW"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)MACRO\b', re.I), "MACRO"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)PROCEDURE\b', re.I), "PROCEDURE"),
    (re.compile(r'(?:CREATE\s+|REPLACE\s+)(?:SPECIFIC\s+)?FUNCTION\b', re.I), "FUNCTION"),
    (re.compile(r'(?:CREATE|REPLACE)\s+TRIGGER\b', re.I), "TRIGGER"),
    # Pre-requisites (environment-scoped)
    (re.compile(r'CREATE\s+DATABASE\b', re.I), "DATABASE"),
    (re.compile(r'CREATE\s+USER\b', re.I), "USER"),
    # System-scoped objects
    (re.compile(r'CREATE\s+MAP\b', re.I), "MAP"),
    (re.compile(r'CREATE\s+PROFILE\b', re.I), "PROFILE"),
    (re.compile(r'CREATE\s+ROLE\b', re.I), "ROLE"),
    (re.compile(r'CREATE\s+AUTHORIZATION\b', re.I), "AUTHORIZATION"),
    (re.compile(r'CREATE\s+FOREIGN\s+SERVER\b', re.I), "FOREIGN_SERVER"),
    # JAR installation (CALL SQLJ.INSTALL_JAR / SQLJ.REPLACE_JAR)
    (re.compile(r'CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(', re.I), "JAR"),
    # DCL (least specific — GRANT/REVOKE match single keywords)
    (re.compile(r'\bGRANT\b', re.I), "GRANT"),
    (re.compile(r'\bREVOKE\b', re.I), "REVOKE"),
]

# -- Qualified name extraction patterns --
_QUALIFIED_NAME_RE = re.compile(
    r'(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?'
    r'(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?'
    r'(?:TRACE\s+)?'
    r'(?:SPECIFIC\s+)?'
    r'(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|'
    r'JOIN\s+INDEX|HASH\s+INDEX|DATABASE|USER|PROFILE|ROLE)\s+'
    r'("?[A-Za-z_]\w*"?(?:\."?[A-Za-z_]\w*"?)?)',
    re.IGNORECASE,
)

# -- Detect REPLACE VIEW vs CREATE VIEW --
_HAS_REPLACE_VIEW_RE = re.compile(
    r'REPLACE\s+VIEW',
    re.IGNORECASE,
)
_CREATE_VIEW_RE = re.compile(
    r'CREATE\s+VIEW\b',
    re.IGNORECASE,
)

# -- MULTISET detection --
_HAS_SET_MULTISET_RE = re.compile(
    r'CREATE\s+(?:MULTISET|SET)\s+',
    re.IGNORECASE,
)
_INJECT_MULTISET_RE = re.compile(
    r'(CREATE\s+)((?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?(?:TRACE\s+)?TABLE\b)',
    re.IGNORECASE,
)


@dataclass
class IngestResult:
    """
    Outcome of ingesting DDL files into a project.

    Attributes:
        total_files:       Files scanned in source directory.
        classified:        Successfully classified and placed.
        unclassified:      Could not determine object type.
        token_candidates:  Hardcoded names detected as token candidates.
        files_placed:      List of (source, destination, type) tuples.
        warnings:          Non-fatal issues.
        errors:            Fatal issues.
    """

    total_files: int = 0
    classified: int = 0
    unclassified: int = 0
    token_candidates: Dict[str, List[str]] = field(default_factory=dict)
    files_placed: List[Tuple[str, str, str]] = field(default_factory=list)
    multiset_injected: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    unclassified_files: List[str] = field(default_factory=list)


def ingest_directory(
    source_dir: str,
    project_dir: str,
    detect_tokens: bool = True,
    apply_tokens: Optional[Dict[str, str]] = None,
    file_patterns: List[str] = None,
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

    # -- Discover source files --
    source_files = _discover_files(source_dir, file_patterns)
    result.total_files = len(source_files)
    logger.info("Found %d files in %s", len(source_files), source_dir)

    # -- Track all database names seen (for token detection) --
    all_db_names: Dict[str, List[str]] = defaultdict(list)

    for src_path in source_files:
        try:
            content = _read_file(src_path)
            if content is None:
                continue  # Binary file

            # -- Classify --
            obj_type = _classify_ddl(content)
            if obj_type is None:
                result.unclassified += 1
                result.unclassified_files.append(
                    os.path.relpath(src_path, source_dir)
                )
                result.warnings.append(
                    f"Could not classify: {os.path.basename(src_path)}"
                )
                continue

            # -- Extract qualified name --
            db_name, obj_name = _extract_qualified_name(content)

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
            if apply_tokens:
                for literal, token in apply_tokens.items():
                    content = content.replace(literal, token)

            # -- Determine destination --
            subdir = _TYPE_TO_SUBDIR.get(obj_type, "DDL")
            ext = _TYPE_TO_EXT.get(obj_type, ".sql")

            # For overloaded functions, use the SPECIFIC name
            # to avoid filename collisions between overloads
            if obj_type == "FUNCTION":
                specific_name = _extract_specific_function_name(content)
                if specific_name:
                    obj_name = specific_name

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
                os.remove(gitkeep)

            dest_path = os.path.join(dest_dir, dest_name)

            # -- Handle duplicates --
            if os.path.exists(dest_path):
                result.warnings.append(
                    f"Duplicate: {dest_name} already exists — "
                    f"source '{os.path.basename(src_path)}' skipped."
                )
                continue

            # -- Write normalised content --
            with open(dest_path, 'w', encoding='utf-8') as f:
                f.write(content)

            result.classified += 1
            result.files_placed.append((
                os.path.relpath(src_path, source_dir),
                os.path.relpath(dest_path, project_dir),
                obj_type,
            ))

            logger.debug(
                "Ingested: %s → %s (%s)",
                os.path.basename(src_path), dest_name, obj_type
            )

        except Exception as e:
            result.errors.append(
                f"Error processing {os.path.basename(src_path)}: {e}"
            )

    # -- Build token candidate report --
    if detect_tokens:
        result.token_candidates = _build_token_candidates(all_db_names)

    logger.info(
        "Ingest complete: %d classified, %d unclassified, "
        "%d MULTISET injected",
        result.classified, result.unclassified,
        result.multiset_injected,
    )

    return result


# ---------------------------------------------------------------
# Internal — File discovery
# ---------------------------------------------------------------

def _discover_files(source_dir: str, file_patterns: List[str] = None) -> List[str]:
    """
    Discover SQL/DDL files in a directory tree.

    Args:
        source_dir:     Root directory to scan.
        file_patterns:  Extensions to include (default: common SQL).

    Returns:
        Sorted list of file paths.
    """
    if file_patterns is None:
        file_patterns = [
            '.sql', '.tbl', '.viw', '.spl', '.mcr', '.fnc',
            '.trg', '.jix', '.idx', '.db', '.ddl', '.dcl', '.dml',
            '.map', '.rol', '.prf', '.auth', '.fsvr',
            '.sto', '.jcl', '.usr',
            '.c', '.h',
        ]

    files = []
    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for f in sorted(filenames):
            if f.startswith('.') or f.startswith('_'):
                continue
            ext = os.path.splitext(f)[1].lower()
            if not file_patterns or ext in file_patterns:
                files.append(os.path.join(root, f))
    return files


def _read_file(path: str) -> Optional[str]:
    """Read a text file, returning None for binary files."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        return None


def _find_payload_base(project_dir: str) -> str:
    """Locate the payload/database directory."""
    for candidate in ['payload/database', 'payload']:
        path = os.path.join(project_dir, candidate)
        if os.path.isdir(path):
            return path
    raise FileNotFoundError(
        f"No payload/database directory found in {project_dir}. "
        "Run 'td_release_packager scaffold' first."
    )


# ---------------------------------------------------------------
# Internal — Classification
# ---------------------------------------------------------------

def _classify_ddl(content: str) -> Optional[str]:
    """
    Classify DDL content by object type.

    Tests patterns in specificity order.

    Args:
        content: The DDL file content.

    Returns:
        Object type string, or None if unclassifiable.
    """
    for pattern, obj_type in _CLASSIFY_PATTERNS:
        if pattern.search(content):
            return obj_type
    return None


def _extract_qualified_name(content: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract database.object name from DDL content.

    Args:
        content: The DDL file content.

    Returns:
        Tuple of (database_name, object_name), either may be None.
    """
    match = _QUALIFIED_NAME_RE.search(content)
    if not match:
        return (None, None)

    qualified = match.group(1)
    parts = qualified.replace('"', '').split('.')
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

    qualified = match.group(1).replace('"', '')
    parts = qualified.split('.')
    # Return just the object name (last part)
    return parts[-1].strip()


# ---------------------------------------------------------------
# Internal — Normalisation
# ---------------------------------------------------------------

def _inject_multiset(content: str) -> Tuple[str, bool]:
    """Inject MULTISET if neither SET nor MULTISET is specified."""
    if _HAS_SET_MULTISET_RE.search(content):
        return (content, False)
    modified = _INJECT_MULTISET_RE.sub(r'\1MULTISET \2', content, count=1)
    return (modified, modified != content)


def _inject_replace_view(content: str) -> Tuple[str, bool]:
    """
    Convert CREATE VIEW to REPLACE VIEW for idempotency.

    If the DDL already uses REPLACE VIEW,
    no change is made.

    Args:
        content: The view DDL content.

    Returns:
        Tuple of (modified_content, was_injected).
    """
    if _HAS_REPLACE_VIEW_RE.search(content):
        return (content, False)

    if _CREATE_VIEW_RE.search(content):
        modified = _CREATE_VIEW_RE.sub('REPLACE VIEW', content, count=1)
        return (modified, True)

    return (content, False)


# ---------------------------------------------------------------
# Internal — Token candidate detection
# ---------------------------------------------------------------

def _build_token_candidates(
    db_names: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    Build a report of hardcoded database/user names that should
    become tokens.

    Groups by name, reports which files reference each.
    Filters out known system databases (DBC, SYSUDTLIB, etc.)
    that should remain hardcoded.

    Args:
        db_names: Dict of database_name → list of files referencing it.

    Returns:
        Dict of database_name → list of files (excluding system DBs).
    """
    # System databases that should remain hardcoded
    system_dbs = {
        'DBC', 'SYSUDTLIB', 'SYSLIB', 'SYSJDBC', 'SYSBAR',
        'SYSTEMFE', 'SYSSPATIAL', 'TD_SYSFNLIB',
        'TD_SYSXML', 'TDSTATS', 'TDWM', 'TD_SYSGPL',
        'ALL', 'DEFAULT', 'PUBLIC', 'EXTUSER',
    }

    candidates = {}
    for db_name, files in sorted(db_names.items()):
        if db_name.upper() in system_dbs:
            continue
        candidates[db_name] = files

    return candidates
