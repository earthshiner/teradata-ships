"""
perm_analyser.py — Static perm-space analysis for SHIPS packages.

Inspects DDL files in the payload at *package time* (no live Teradata
connection required) and produces a conservative estimate of the perm
space that the package will consume against each target database or
user container.

Two sources of information are combined:

  1. **Declared PERM** — extracted from CREATE DATABASE / CREATE USER /
     MODIFY DATABASE / MODIFY USER statements in ``.db`` and ``.usr``
     files.  This is the ceiling the package is allocating (or
     changing) for each container.

  2. **Estimated footprint** — derived by counting space-consuming
     objects destined for each database and multiplying by a
     conservative nominal floor per object type.  The floor is a
     per-object single-AMP minimum.  Because the actual number of
     AMPs is unknown at static-analysis time, the estimate is
     intentionally pessimistic: it represents the minimum space
     consumed assuming all skew falls onto one AMP.  The live
     preflight (``preflight.py``) accounts for real skew using
     ``DBC.DiskSpaceV`` and the skew query.

Space-consuming object types (these consume at least one block per AMP
even when empty):

    TABLE       — physical row store; headers consume space
    JOIN_INDEX  — materialised derived table; physical store
    HASH_INDEX  — physical secondary index store
    PROCEDURE   — compiled bytecode stored in DBC
    FUNCTION    — compiled bytecode stored in DBC
    TRIGGER     — compiled body stored in DBC

Not space-consuming:

    VIEW, MACRO, STATISTICS, COMMENT, FOREIGN_KEY, DML, GRANT, REVOKE

Usage:

    from td_release_packager.perm_analyser import analyse_perm_space

    result = analyse_perm_space(payload_dir)
    for finding in result.findings:
        print(finding.summary_line())

The result is also serialisable to a plain dict for embedding in
``inspect_report.json``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from td_release_packager.sql_text import (
    strip_comments_and_string_literals as _strip_sql,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nominal floor constants (bytes per object, conservative single-AMP minimum)
# ---------------------------------------------------------------------------

#: Minimum bytes consumed by a single empty TABLE on one AMP.
#: Teradata allocates at least one full cylinder block per physical
#: table per AMP; 512 KB is the safe conservative minimum for
#: small-to-medium platforms.
_FLOOR_TABLE: int = 512 * 1024  # 512 KB

#: Join and Hash Indexes are materialised structures; same floor as TABLE.
_FLOOR_JOIN_INDEX: int = 512 * 1024  # 512 KB
_FLOOR_HASH_INDEX: int = 512 * 1024  # 512 KB

#: Stored procedures store compiled byte-code in DBC.  Actual size
#: varies with body length; 128 KB is a safe pessimistic floor.
_FLOOR_PROCEDURE: int = 128 * 1024  # 128 KB

#: Functions (SQL and C UDFs) store compiled binary in DBC.
#: C UDFs may be larger but 128 KB is a consistent conservative floor.
_FLOOR_FUNCTION: int = 128 * 1024  # 128 KB

#: Triggers store a compiled representation in DBC; same floor as procedure.
_FLOOR_TRIGGER: int = 128 * 1024  # 128 KB


#: Map from file extension → (label, nominal floor in bytes).
#: Only extensions that consume permanent space are listed here.
#: All others are ignored for footprint estimation.
SPACE_CONSUMING_EXTENSIONS: Dict[str, tuple] = {
    ".tbl": ("TABLE", _FLOOR_TABLE),
    ".jix": ("JOIN_INDEX", _FLOOR_JOIN_INDEX),
    ".idx": ("HASH_INDEX", _FLOOR_HASH_INDEX),
    ".spl": ("PROCEDURE", _FLOOR_PROCEDURE),
    ".fnc": ("FUNCTION", _FLOOR_FUNCTION),
    ".trg": ("TRIGGER", _FLOOR_TRIGGER),
}


# ---------------------------------------------------------------------------
# Regex patterns for extracting PERM values from DDL
# ---------------------------------------------------------------------------

#: Matches PERM = <value> in CREATE/MODIFY DATABASE or USER statements.
#: Captures an optional multiplier suffix (K, M, G, T) for human-friendly
#: values like PERM = 500M.  Values without a suffix are taken as bytes.
#:
#: Pattern handles:
#:   PERM = 1000000
#:   PERM=500000000
#:   PERM = 500M
#:   PERM= 2G
#:   , PERM = 1T   (leading comma from multi-attribute DDL)
_PERM_RE = re.compile(
    r"\bPERM\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)\b",
    re.IGNORECASE,
)

#: Matches the object name in CREATE DATABASE / CREATE USER statements.
#: Handles optional quoting and optional FROM clause.
#:   CREATE DATABASE MyDb FROM ...
#:   CREATE USER "MyUser" AS ...
_CREATE_DB_USER_RE = re.compile(
    r"^\s*CREATE\s+(DATABASE|USER)\s+\"?([A-Za-z0-9_$]+)\"?",
    re.IGNORECASE | re.MULTILINE,
)

#: Matches MODIFY DATABASE / MODIFY USER statements.
#:   MODIFY DATABASE MyDb PERM = nnn;
#:   MODIFY USER "MyUser" AS PERM = nnn;
_MODIFY_DB_USER_RE = re.compile(
    r"^\s*MODIFY\s+(DATABASE|USER)\s+\"?([A-Za-z0-9_$]+)\"?",
    re.IGNORECASE | re.MULTILINE,
)

#: Extracts the database qualifier from a fully qualified object name
#: (DatabaseName.ObjectName) in the first CREATE/REPLACE statement.
#: Extracts the database qualifier from a DDL object definition.
#: Mirrors the pattern used in ``validate.py`` so all pipeline stages
#: recognise the same set of DDL verbs and type modifiers.
#: Anchored to start-of-statement; handles MULTISET/SET, VOLATILE,
#: GLOBAL TEMPORARY, TRACE, SPECIFIC, and all standard DDL types.
_QUALIFIED_NAME_RE = re.compile(
    r"^\s*(?:CREATE|REPLACE)\s+(?:MULTISET\s+|SET\s+)?"
    r"(?:VOLATILE\s+|GLOBAL\s+TEMPORARY\s+)?"
    r"(?:TRACE\s+)?"
    r"(?:SPECIFIC\s+)?"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|"
    r"JOIN\s+INDEX|HASH\s+INDEX)\s+"
    r'\"?([A-Za-z0-9_$]+)\"?\s*\.\s*\"?([A-Za-z0-9_$]+)\"?',
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helper: parse a PERM value string to bytes
# ---------------------------------------------------------------------------


def _parse_perm_bytes(value_str: str, suffix: str) -> int:
    """Convert a PERM value string with optional suffix to bytes.

    Args:
        value_str: Numeric string, possibly with a decimal point.
        suffix:    Optional multiplier suffix: K, M, G, T (case-insensitive).
                   Empty string means raw bytes.

    Returns:
        Integer byte count.
    """
    value = float(value_str)
    multiplier_map = {
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
        "":  1,
    }
    multiplier = multiplier_map.get(suffix.upper(), 1)
    return int(value * multiplier)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PermDeclaration:
    """A PERM declaration extracted from a CREATE or MODIFY statement.

    Attributes:
        container_name: Database or user name the PERM applies to.
        perm_bytes:     Declared PERM value in bytes.
        is_modify:      True when parsed from a MODIFY statement.
        source_file:    The ``.db`` or ``.usr`` file this came from.
    """

    container_name: str
    perm_bytes: int
    is_modify: bool
    source_file: str


@dataclass
class SpaceConsumingObject:
    """A space-consuming DDL object found in the payload.

    Attributes:
        qualified_name: Fully-qualified name (Database.Object).
        database_name:  Target database/user container.
        object_type:    Human label (e.g. 'TABLE', 'PROCEDURE').
        floor_bytes:    Conservative nominal space floor (bytes).
        source_file:    Path to the DDL file.
    """

    qualified_name: str
    database_name: str
    object_type: str
    floor_bytes: int
    source_file: str


@dataclass
class DatabasePermFinding:
    """Perm-space analysis result for a single database/user container.

    Attributes:
        database_name:      Container name.
        declared_perm:      PERM as declared in CREATE DDL (bytes).
                            None when no CREATE is in this package.
        modify_delta:       Net PERM change from MODIFY statements (bytes).
                            0 when no MODIFY is in this package.
        effective_perm:     Resulting PERM after applying modify_delta.
                            None when declared_perm is unknown.
        object_count:       Count of space-consuming objects per type.
        estimated_floor:    Conservative estimated footprint (bytes).
        status:             'OK', 'WARNING', 'UNKNOWN', or 'INSUFFICIENT'.
        notes:              List of human-readable diagnostic notes.
    """

    database_name: str
    declared_perm: Optional[int]
    modify_delta: int
    effective_perm: Optional[int]
    object_count: Dict[str, int] = field(default_factory=dict)
    estimated_floor: int = 0
    status: str = "UNKNOWN"
    notes: List[str] = field(default_factory=list)

    def summary_line(self) -> str:
        """Single-line summary suitable for console output."""
        perm_str = (
            _format_bytes(self.effective_perm)
            if self.effective_perm is not None
            else "UNKNOWN"
        )
        floor_str = _format_bytes(self.estimated_floor)
        return (
            f"[{self.status}] {self.database_name}: "
            f"declared PERM={perm_str}, "
            f"estimated footprint={floor_str}"
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        return {
            "database_name": self.database_name,
            "declared_perm_bytes": self.declared_perm,
            "modify_delta_bytes": self.modify_delta,
            "effective_perm_bytes": self.effective_perm,
            "object_count": self.object_count,
            "estimated_floor_bytes": self.estimated_floor,
            "estimated_floor_human": _format_bytes(self.estimated_floor),
            "effective_perm_human": (
                _format_bytes(self.effective_perm)
                if self.effective_perm is not None
                else None
            ),
            "status": self.status,
            "notes": self.notes,
        }


@dataclass
class PermAnalysisResult:
    """Aggregate result of static perm-space analysis across the payload.

    Attributes:
        findings:          Per-database analysis findings.
        declarations:      All PERM declarations parsed from .db/.usr files.
        objects:           All space-consuming objects found in the payload.
        has_warnings:      True if any finding has status WARNING or INSUFFICIENT.
        has_insufficient:  True if any finding has status INSUFFICIENT
                           (estimated footprint exceeds declared PERM).
    """

    findings: List[DatabasePermFinding] = field(default_factory=list)
    declarations: List[PermDeclaration] = field(default_factory=list)
    objects: List[SpaceConsumingObject] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        """True when any finding requires attention."""
        return any(f.status in ("WARNING", "INSUFFICIENT") for f in self.findings)

    @property
    def has_insufficient(self) -> bool:
        """True when estimated footprint exceeds declared PERM for any container."""
        return any(f.status == "INSUFFICIENT" for f in self.findings)

    def to_dict(self) -> dict:
        """Serialise to a plain dict for embedding in inspect_report.json."""
        return {
            "has_warnings": self.has_warnings,
            "has_insufficient": self.has_insufficient,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Internal — DDL file parsing helpers
# ---------------------------------------------------------------------------


def _read_file(path: str) -> str:
    """Read a file to a string, ignoring decode errors.

    Args:
        path: File path to read.

    Returns:
        File contents as a string.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        logger.debug("perm_analyser: could not read '%s': %s", path, exc)
        return ""


def _strip_comments(content: str) -> str:
    """Strip SQL comments and string literals from DDL content.

    Delegates to the shared
    ``td_release_packager.sql_text.strip_comments_and_string_literals``
    so all pipeline stages use the same position-preserving stripper,
    preventing PERM values inside comments from being parsed as live
    declarations.

    Args:
        content: Raw DDL content.

    Returns:
        Content with comments and string literals replaced by whitespace.
    """
    return _strip_sql(content)

def _extract_perm_declarations(
    file_path: str,
    content: str,
) -> List[PermDeclaration]:
    """Extract all PERM declarations from a .db or .usr file.

    Handles both CREATE and MODIFY forms.  A file may contain multiple
    statements (e.g. CREATE DATABASE followed by MODIFY DATABASE).

    Args:
        file_path: Source file path (for attribution).
        content:   DDL content (comments already stripped).

    Returns:
        List of PermDeclaration instances.
    """
    declarations: List[PermDeclaration] = []

    # Find all CREATE DATABASE/USER statements with a PERM clause.
    for m in _CREATE_DB_USER_RE.finditer(content):
        object_type = m.group(1).upper()  # DATABASE or USER
        container_name = m.group(2)
        # The PERM clause is typically within the same statement.
        # We take a window from the match start to the next semicolon.
        stmt_window = content[m.start(): content.find(";", m.start()) + 1]
        perm_match = _PERM_RE.search(stmt_window)
        if perm_match:
            perm_bytes = _parse_perm_bytes(perm_match.group(1), perm_match.group(2))
            declarations.append(
                PermDeclaration(
                    container_name=container_name,
                    perm_bytes=perm_bytes,
                    is_modify=False,
                    source_file=file_path,
                )
            )
            logger.debug(
                "perm_analyser: CREATE %s '%s' PERM=%d from '%s'",
                object_type, container_name, perm_bytes, os.path.basename(file_path),
            )

    # Find all MODIFY DATABASE/USER statements with a PERM clause.
    for m in _MODIFY_DB_USER_RE.finditer(content):
        object_type = m.group(1).upper()
        container_name = m.group(2)
        stmt_window = content[m.start(): content.find(";", m.start()) + 1]
        perm_match = _PERM_RE.search(stmt_window)
        if perm_match:
            perm_bytes = _parse_perm_bytes(perm_match.group(1), perm_match.group(2))
            declarations.append(
                PermDeclaration(
                    container_name=container_name,
                    perm_bytes=perm_bytes,
                    is_modify=True,
                    source_file=file_path,
                )
            )
            logger.debug(
                "perm_analyser: MODIFY %s '%s' PERM=%d from '%s'",
                object_type, container_name, perm_bytes, os.path.basename(file_path),
            )

    return declarations


def _extract_database_from_ddl(content: str) -> Optional[str]:
    """Extract the database qualifier from a DDL object definition.

    Looks for the first CREATE/REPLACE ... DatabaseName.ObjectName pattern.

    Args:
        content: DDL content (comments stripped).

    Returns:
        Database name string, or None if not determinable.
    """
    m = _QUALIFIED_NAME_RE.search(content)
    if m:
        return m.group(1)
    return None


def _infer_database_from_path(
    file_path: str,
    payload_dir: str,
) -> Optional[str]:
    """Infer database name from the SHIPS payload-relative file path.

    Expected layout:
        <payload_root>/<database>/DDL/<object_type>/<object_file>

    This is used only as a fallback for unqualified DDL object names.
    The inference is based on the path relative to the payload root rather
    than a fixed number of parent-directory hops, so it is not affected by
    how deeply the payload root itself is nested.

    Args:
        file_path: Absolute or relative path to a DDL file.
        payload_dir: Root of the SHIPS payload directory.

    Returns:
        Inferred database name, or None if the file does not match the
        expected payload layout.
    """
    try:
        rel_path = os.path.relpath(file_path, payload_dir)
    except ValueError:
        return None

    parts = rel_path.split(os.sep)

    # Expected package-relative layout:
    #   <database>/DDL/<object_type>/<object_file>
    if len(parts) >= 4 and parts[1].lower() == "ddl":
        database_name = parts[0].strip()
        if database_name and database_name not in {os.curdir, os.pardir}:
            return database_name

    return None


def _walk_payload(payload_dir: str) -> List[str]:
    """Recursively yield all file paths under payload_dir.

    Args:
        payload_dir: Root of the SHIPS payload directory.

    Returns:
        Sorted list of absolute file paths.
    """
    paths: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(payload_dir):
        for filename in filenames:
            paths.append(os.path.join(dirpath, filename))
    return sorted(paths)


# ---------------------------------------------------------------------------
# Internal — byte formatter (shared with preflight.py style)
# ---------------------------------------------------------------------------


def _format_bytes(num_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    Args:
        num_bytes: Byte count.

    Returns:
        Formatted string (e.g. '1.5 GB', '256 MB').
    """
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} EB"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyse_perm_space(payload_dir: str) -> PermAnalysisResult:
    """Perform static perm-space analysis across a SHIPS payload directory.

    Scans all files under *payload_dir* to:

      1. Extract PERM declarations from ``.db`` and ``.usr`` files
         (CREATE DATABASE/USER and MODIFY DATABASE/USER with PERM= clause).
      2. Count space-consuming objects (``.tbl``, ``.jix``, ``.idx``,
         ``.spl``, ``.fnc``, ``.trg``) and determine their target database
         from the DDL's fully-qualified object name.
      3. Estimate a conservative perm footprint per target database.
      4. Compare estimated footprint against declared PERM and produce
         a per-database finding with status OK / WARNING / INSUFFICIENT
         / UNKNOWN.

    Args:
        payload_dir: Root of the SHIPS payload directory to analyse.

    Returns:
        A ``PermAnalysisResult`` containing per-database findings,
        raw declarations, and the list of space-consuming objects.
    """
    result = PermAnalysisResult()
    all_files = _walk_payload(payload_dir)

    # ------------------------------------------------------------------
    # Pass 1: extract all PERM declarations from .db and .usr files.
    # Also collect MODIFY statements from .dml and .sql files where
    # MODIFY DATABASE/USER may appear as operational DDL.
    # ------------------------------------------------------------------
    prereq_extensions = {".db", ".usr", ".dml", ".sql", ".ddl"}

    for file_path in all_files:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in prereq_extensions:
            continue
        content = _strip_comments(_read_file(file_path))
        if not content.strip():
            continue
        declarations = _extract_perm_declarations(file_path, content)
        result.declarations.extend(declarations)

    # ------------------------------------------------------------------
    # Pass 2: find space-consuming objects and their target database.
    # ------------------------------------------------------------------
    for file_path in all_files:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in SPACE_CONSUMING_EXTENSIONS:
            continue
        object_label, floor_bytes = SPACE_CONSUMING_EXTENSIONS[ext]
        content = _strip_comments(_read_file(file_path))
        if not content.strip():
            continue

        db_name = _extract_database_from_ddl(content)
        if not db_name:
            db_name = _infer_database_from_path(file_path, payload_dir)
            if db_name:
                logger.debug(
                    "perm_analyser: inferred database '%s' from payload path for '%s'",
                    db_name, os.path.basename(file_path),
                )
            else:
                logger.warning(
                    "perm_analyser: could not determine target database for '%s'; "
                    "object will not be counted against any container.",
                    file_path,
                )
                continue

        obj_name = os.path.splitext(os.path.basename(file_path))[0]
        result.objects.append(
            SpaceConsumingObject(
                qualified_name=f"{db_name}.{obj_name}",
                database_name=db_name,
                object_type=object_label,
                floor_bytes=floor_bytes,
                source_file=file_path,
            )
        )

    # ------------------------------------------------------------------
    # Aggregate: build per-database PERM baseline from declarations.
    # ------------------------------------------------------------------
    #
    # For each container:
    #   declared_perm  = PERM from the last CREATE DATABASE/USER for that name
    #                    (multiple CREATE statements for the same name are unusual
    #                     but we take the last to be safe)
    #   modify_delta   = sum of all MODIFY PERM changes (may be negative if
    #                     the package is reducing allocation)
    # ------------------------------------------------------------------

    # Gather all container names referenced either in declarations or objects.
    all_containers: set = set()
    for decl in result.declarations:
        all_containers.add(decl.container_name)
    for obj in result.objects:
        all_containers.add(obj.database_name)

    # Build per-container declared PERM and modify delta.
    declared_perm_map: Dict[str, Optional[int]] = {}
    modify_delta_map: Dict[str, int] = {}

    for container in all_containers:
        # Last CREATE wins.
        create_decls = [
            d for d in result.declarations
            if d.container_name == container and not d.is_modify
        ]
        declared_perm_map[container] = create_decls[-1].perm_bytes if create_decls else None

        # Sum of MODIFY deltas.  Each MODIFY sets an absolute new PERM, not a
        # relative change — compute as (new_perm - previous_perm).
        # When no baseline is known the first MODIFY value is treated as the
        # full declaration (not a delta) and we flag it in the notes.
        modify_decls = [
            d for d in result.declarations
            if d.container_name == container and d.is_modify
        ]
        modify_delta_map[container] = 0
        if modify_decls:
            # Teradata MODIFY sets PERM to the new absolute value.
            # If we have a declared baseline, the net delta is:
            #   last_modify_value - declared_baseline
            # If we have multiple MODIFYs, only the final one matters.
            last_modify = modify_decls[-1].perm_bytes
            if declared_perm_map[container] is not None:
                modify_delta_map[container] = last_modify - declared_perm_map[container]
            else:
                # No CREATE in package — treat the MODIFY value as the effective PERM.
                modify_delta_map[container] = 0
                declared_perm_map[container] = last_modify

    # ------------------------------------------------------------------
    # Build findings per container.
    # ------------------------------------------------------------------
    for container in sorted(all_containers):
        declared = declared_perm_map.get(container)
        modify_delta = modify_delta_map.get(container, 0)

        # Effective PERM after MODIFYs.
        effective_perm: Optional[int] = None
        if declared is not None:
            effective_perm = declared + modify_delta

        # Count objects and estimate footprint.
        container_objects = [o for o in result.objects if o.database_name == container]
        object_count: Dict[str, int] = {}
        estimated_floor = 0
        for obj in container_objects:
            object_count[obj.object_type] = object_count.get(obj.object_type, 0) + 1
            estimated_floor += obj.floor_bytes

        # Build notes.
        notes: List[str] = []
        modify_decls = [
            d for d in result.declarations
            if d.container_name == container and d.is_modify
        ]
        if modify_decls:
            last_modify = modify_decls[-1]
            notes.append(
                f"MODIFY from '{os.path.basename(last_modify.source_file)}' sets "
                f"PERM to {_format_bytes(last_modify.perm_bytes)}."
            )
        if len(modify_decls) > 1:
            notes.append(
                f"Multiple MODIFY statements found ({len(modify_decls)}); "
                f"only the last value ({_format_bytes(modify_decls[-1].perm_bytes)}) applies."
            )

        # Determine status.
        if estimated_floor == 0:
            # No space-consuming objects target this container.
            # It may be a parent database that only holds child objects.
            status = "OK"
            notes.append("No space-consuming objects target this container directly.")
        elif effective_perm is None:
            # Container has objects but no PERM declaration in the package.
            # The container must already exist on the target — live preflight
            # will check actual free space.
            status = "UNKNOWN"
            notes.append(
                "No CREATE DATABASE/USER for this container in the package. "
                "Declared PERM is unknown — live preflight will check actual "
                "free space via DBC.DiskSpaceV."
            )
        elif estimated_floor > effective_perm:
            status = "INSUFFICIENT"
            shortfall = estimated_floor - effective_perm
            notes.append(
                f"Estimated footprint ({_format_bytes(estimated_floor)}) exceeds "
                f"declared PERM ({_format_bytes(effective_perm)}) by "
                f"{_format_bytes(shortfall)}. Increase the PERM allocation or "
                "reduce the object count."
            )
        else:
            headroom = effective_perm - estimated_floor
            status = "OK"
            notes.append(
                f"Estimated headroom: {_format_bytes(headroom)} "
                f"({(headroom / effective_perm * 100):.1f}% of declared PERM). "
                "Note: estimate is a conservative single-AMP floor — actual "
                "consumption depends on data volume and AMP count."
            )
            # Warn if headroom is less than 20% of declared PERM.
            if effective_perm > 0 and headroom < effective_perm * 0.20:
                status = "WARNING"
                notes.append(
                    "Headroom is below 20% of declared PERM. Consider increasing "
                    "the PERM allocation before deploying."
                )

        finding = DatabasePermFinding(
            database_name=container,
            declared_perm=declared,
            modify_delta=modify_delta,
            effective_perm=effective_perm,
            object_count=object_count,
            estimated_floor=estimated_floor,
            status=status,
            notes=notes,
        )
        result.findings.append(finding)

        logger.info(
            "perm_analyser: [%s] %s — effective PERM=%s, estimated floor=%s",
            status,
            container,
            _format_bytes(effective_perm) if effective_perm is not None else "UNKNOWN",
            _format_bytes(estimated_floor),
        )

    return result
