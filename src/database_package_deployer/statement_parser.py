"""
statement_parser.py — Teradata DDL parser with object type detection.

Parses DDL files to extract:
    - Qualified database.object name
    - Object type (TABLE, JOIN_INDEX, VIEW, etc.)
    - Deployment strategy

Also handles MULTISET injection: if a CREATE TABLE lacks an
explicit SET or MULTISET qualifier, MULTISET is injected. This
prevents the Teradata default of SET tables, which impose
duplicate-row checking overhead.

Supported DDL patterns:
    CREATE [MULTISET|SET] TABLE db.tbl ...
    CREATE JOIN INDEX db.ji AS ...
    CREATE [UNIQUE] INDEX name (cols) ON db.tbl
    CREATE HASH INDEX db.hi (cols) ON db.tbl ORDER BY ...
    REPLACE VIEW db.vw AS ...
    REPLACE MACRO db.mcr AS ...
    REPLACE PROCEDURE db.proc ...
    REPLACE [SPECIFIC] FUNCTION db.func ...
    CREATE TRIGGER db.trg ...
"""

import os
import re
from typing import Tuple, Optional

from database_package_deployer.models import (
    ObjectType,
    DeployIntent,
    DeployStrategy,
    ParsedStatement,
    STRATEGY_MAP,
)


# ---------------------------------------------------------------
# Regex patterns — one per object type
# ---------------------------------------------------------------

# Qualified name capture: DB.OBJ or just OBJ.
#
# The dot-separated form allows optional horizontal whitespace and a
# single newline between the database name and the dot. This handles
# the Teradata style where the name is split across two lines:
#
#   REPLACE PROCEDURE MyDB
#       .MyProc (...)
#
# [ \t]*  — optional spaces/tabs before the dot (same line)
# \n?     — optional single newline
# [ \t]*  — optional indent after the newline
#
# A bare identifier name (no dot) is also captured for objects that
# do not require a qualifier (DATABASE, USER, etc.) — those use a
# narrower single-part pattern below.
_NP = r'(?:"[^"]+"|[A-Za-z_]\w*)'  # one name part
_QNAME = rf"({_NP}(?:[ \t]*\n?[ \t]*\.{_NP})?)"  # DB.OBJ or OBJ

# CREATE [MULTISET|SET] [VOLATILE|GLOBAL TEMPORARY] [TRACE] TABLE db.tbl
_TABLE_RE = re.compile(
    r"""
    CREATE\s+
    (?:(?:MULTISET|SET)\s+)?
    (?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?
    (?:TRACE\s+)?
    TABLE\s+
    """
    + _QNAME,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE JOIN INDEX db.ji_name AS ...
_JOIN_INDEX_RE = re.compile(
    r"CREATE\s+JOIN\s+INDEX\s+" + _QNAME,
    re.IGNORECASE,
)

# CREATE HASH INDEX db.hi_name (cols) ON db.tbl ...
_HASH_INDEX_RE = re.compile(
    r"CREATE\s+HASH\s+INDEX\s+" + _QNAME,
    re.IGNORECASE,
)

# CREATE [UNIQUE] INDEX idx_name (cols) ON db.tbl
# Note: secondary indexes are named but the ON clause identifies the table.
# We capture the index name as the object, and extract the table from ON.
_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+" + rf"({_NP})" + r"\s*\(",
    re.IGNORECASE,
)

# The ON clause for secondary indexes — captures db.table
_INDEX_ON_RE = re.compile(
    r"\bON\s+" + _QNAME,
    re.IGNORECASE,
)

# REPLACE VIEW / CREATE VIEW
_VIEW_RE = re.compile(
    r"(?:REPLACE|CREATE)\s+VIEW\s+" + _QNAME,
    re.IGNORECASE,
)

# REPLACE MACRO / CREATE MACRO
_MACRO_RE = re.compile(
    r"(?:REPLACE|CREATE)\s+MACRO\s+" + _QNAME,
    re.IGNORECASE,
)

# REPLACE PROCEDURE / CREATE PROCEDURE
_PROCEDURE_RE = re.compile(
    r"(?:REPLACE|CREATE)\s+PROCEDURE\s+" + _QNAME,
    re.IGNORECASE,
)

# REPLACE [SPECIFIC] FUNCTION / CREATE [SPECIFIC] FUNCTION
_FUNCTION_RE = re.compile(
    r"(?:REPLACE|CREATE)\s+(?:SPECIFIC\s+)?FUNCTION\s+" + _QNAME,
    re.IGNORECASE,
)

# CREATE TRIGGER / REPLACE TRIGGER
_TRIGGER_RE = re.compile(
    r"(?:CREATE|REPLACE)\s+TRIGGER\s+" + _QNAME,
    re.IGNORECASE,
)

# CREATE DATABASE
_DATABASE_RE = re.compile(
    r"""
    CREATE\s+DATABASE\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE USER
_USER_RE = re.compile(
    r"""
    CREATE\s+USER\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE PROFILE
_PROFILE_RE = re.compile(
    r"""
    CREATE\s+PROFILE\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE ROLE
_ROLE_RE = re.compile(
    r"""
    CREATE\s+ROLE\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GRANT ... (capture the target object/database for identification)
_GRANT_RE = re.compile(
    r"""
    \bGRANT\s+\w+
    """,
    re.IGNORECASE | re.VERBOSE,
)

# REVOKE ... (capture the target object/database for identification)
_REVOKE_RE = re.compile(
    r"""
    \bREVOKE\s+\w+
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE MAP
_MAP_RE = re.compile(
    r"""
    CREATE\s+MAP\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE AUTHORIZATION
_AUTHORIZATION_RE = re.compile(
    r"""
    CREATE\s+AUTHORIZATION\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CREATE FOREIGN SERVER
_FOREIGN_SERVER_RE = re.compile(
    r"""
    CREATE\s+FOREIGN\s+SERVER\s+
    ((?:"[^"]+"|[A-Za-z_]\w*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# REPLACE/CREATE SCRIPT TABLE OPERATOR (uses FUNCTION syntax
# with RETURNS TABLE and EXTERNAL NAME referencing a script)
_SCRIPT_TABLE_OPERATOR_RE = re.compile(
    r"(?:REPLACE|CREATE)\s+(?:SPECIFIC\s+)?FUNCTION\s+"
    + _QNAME
    + r".*?TABLE\s+OPERATOR",
    re.IGNORECASE | re.DOTALL,
)

# JAR installation via SQLJ.INSTALL_JAR / SQLJ.REPLACE_JAR
_JAR_INSTALL_RE = re.compile(
    r"""
    CALL\s+SQLJ\s*\.\s*(?:INSTALL_JAR|REPLACE_JAR)\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)

# DML — INSERT INTO / UPDATE / DELETE FROM / MERGE INTO.
# Matched anywhere in the script (after comment stripping) so that
# single- and multi-statement DML files both classify cleanly. The
# first matched target supplies a representative database.table for
# the report; the unique manifest key is derived from the filename.
_DML_RE = re.compile(
    r"""
    \b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+
    ((?:"[^"]+"|[A-Za-z_]\w*)(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# -- Detect whether SET/MULTISET is already specified --
_HAS_SET_MULTISET_RE = re.compile(
    r"""
    CREATE\s+
    (MULTISET|SET)\s+
    (?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?
    (?:TRACE\s+)?
    TABLE\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# -- Pattern for injecting MULTISET after CREATE --
_INJECT_MULTISET_RE = re.compile(
    r"""
    (CREATE\s+)
    ((?:(?:VOLATILE|GLOBAL\s+TEMPORARY)\s+)?(?:TRACE\s+)?TABLE\b)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------
# Internal — Comment stripping (for classification only)
# ---------------------------------------------------------------


def _strip_sql_comments(text: str) -> str:
    """
    Remove SQL comments from text for safe regex classification.

    Strips both block comments (/* ... */) and line comments (-- ...).
    The result is used only for object type detection and deploy
    intent classification — the original text (with comments) is
    preserved for execution and storage.

    Args:
        text: Raw DDL text potentially containing comments.

    Returns:
        Text with all SQL comments removed.
    """
    # Remove block comments first (may span multiple lines)
    stripped = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    # Remove line comments
    stripped = re.sub(r"--[^\n]*", " ", stripped)
    return stripped


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------


def parse_statement_file(file_path: str) -> ParsedStatement:
    """
    Parse a DDL file: detect object type, extract name, inject MULTISET.

    Reads the file, classifies the DDL statement, extracts the
    qualified database.object name, and injects MULTISET for
    CREATE TABLE statements that lack a SET/MULTISET qualifier.

    Args:
        file_path: Path to the DDL file.

    Returns:
        ParsedStatement with all extracted metadata.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the DDL cannot be parsed or lacks a database qualifier.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        original_text = f.read()

    if not original_text.strip():
        raise ValueError(f"DDL file is empty: {file_path}")

    return parse_statement_text(original_text, file_path=file_path)


def parse_statement_text(ddl_text: str, file_path: str = "<inline>") -> ParsedStatement:
    """
    Parse DDL text: detect object type, extract name, inject MULTISET.

    Args:
        ddl_text:   The raw DDL statement.
        file_path:  Source file path (for error messages).

    Returns:
        ParsedStatement with all extracted metadata.

    Raises:
        ValueError: If the DDL cannot be parsed or classified.
    """
    original_text = ddl_text

    # Strip comments for classification — prevents false matches
    # from DDL keywords in comments (e.g. "-- uses CREATE DATABASE IF").
    # The original text (with comments) is preserved for execution.
    clean_text = _strip_sql_comments(ddl_text)

    object_type, qualified_raw = _detect_object_type(clean_text)

    if object_type == ObjectType.UNKNOWN:
        raise ValueError(
            f"Could not classify DDL in {file_path}. "
            "Expected CREATE/REPLACE TABLE, VIEW, MACRO, PROCEDURE, "
            "FUNCTION, TRIGGER, or CREATE DATABASE, JOIN INDEX, INDEX, "
            "USER, ROLE, PROFILE, MAP, AUTHORIZATION, FOREIGN SERVER, "
            "GRANT, REVOKE, CALL SQLJ.INSTALL_JAR, or "
            "INSERT/UPDATE/DELETE/MERGE."
        )

    # -- Extract database and object name --
    db_name, obj_name = _split_qualified_name(qualified_raw)

    # System-scope objects, pre-requisites, and DCL use single-part
    # names — no database qualifier required.
    _SINGLE_NAME_TYPES = {
        # System-scope
        ObjectType.MAP,
        ObjectType.ROLE,
        ObjectType.PROFILE,
        ObjectType.AUTHORIZATION,
        ObjectType.FOREIGN_SERVER,
        # Pre-requisites
        ObjectType.DATABASE,
        ObjectType.USER,
        # DCL and procedural
        ObjectType.GRANT,
        ObjectType.REVOKE,
        ObjectType.JAR,
        # DML — qualified name comes from the filename, not the
        # captured first target (a multi-target DML file would
        # otherwise collide with another file sharing that target).
        ObjectType.DML,
    }

    if db_name is None and object_type not in _SINGLE_NAME_TYPES:
        raise ValueError(
            f"DDL in {file_path} does not include a database qualifier. "
            f"Use 'Database.ObjectName' syntax."
        )

    # -- For overloaded functions, use the SPECIFIC name to avoid
    # -- duplicate qualified names in the manifest
    if object_type == ObjectType.FUNCTION:
        specific_match = re.search(
            r'SPECIFIC\s+(?:"?[A-Za-z_]\w*"?\.)?("?[A-Za-z_]\w*"?)',
            clean_text,
            re.IGNORECASE,
        )
        if specific_match:
            obj_name = specific_match.group(1).replace('"', "")

    # -- Inject MULTISET for tables if not specified --
    multiset_injected = False
    if object_type == ObjectType.TABLE:
        ddl_text, multiset_injected = _inject_multiset_if_missing(ddl_text)

    # -- Detect deploy intent from the DDL verb --
    deploy_intent = _detect_deploy_intent(clean_text, object_type)

    # -- Derive strategy from intent (overrides type-based default) --
    if deploy_intent == DeployIntent.DIRECT_EXECUTE:
        strategy = DeployStrategy.DIRECT_EXECUTE
    elif deploy_intent == DeployIntent.CREATE_ONLY:
        strategy = DeployStrategy.CREATE_ONLY
    elif deploy_intent == DeployIntent.REPLACE_WITH_BACKUP:
        strategy = DeployStrategy.REPLACE_IN_PLACE
    elif deploy_intent == DeployIntent.IDEMPOTENT_DEPLOY:
        strategy = DeployStrategy.IDEMPOTENT_DEPLOY
    elif deploy_intent == DeployIntent.SKIP_IF_EXISTS:
        strategy = DeployStrategy.SKIP_IF_EXISTS
    elif deploy_intent == DeployIntent.NOT_DEPLOYED:
        strategy = DeployStrategy.NOT_DEPLOYED
    else:
        strategy = STRATEGY_MAP.get(object_type, DeployStrategy.DROP_AND_CREATE)

    # For single-part names (system-scope, pre-requisites, DCL),
    # the qualified_name IS the object name — no DB prefix.
    if db_name is None:
        if object_type in _SINGLE_NAME_TYPES:
            # For GRANT/REVOKE, the DDL has no unique object name.
            # Derive a unique identifier from the filename so each
            # GRANT file gets its own manifest entry.
            if object_type in (ObjectType.GRANT, ObjectType.REVOKE) and file_path:
                basename = os.path.splitext(os.path.basename(file_path))[0]
                qualified_name = f"{object_type.value}:{basename}"
                obj_name = basename
            else:
                qualified_name = obj_name or ""
            db_name = ""
        else:
            # Shouldn't reach here (caught above), but be safe
            db_name = obj_name or ""
            qualified_name = f"{db_name}.{obj_name}" if obj_name else db_name
    else:
        qualified_name = f"{db_name}.{obj_name}" if obj_name else db_name

    # DML: every .dml file gets a filename-derived manifest key so
    # multi-target scripts don't collide on a shared first target.
    # The first DML target's database (already in db_name) is kept
    # for the report so the operator can see which schema the load
    # writes to.
    if object_type == ObjectType.DML and file_path:
        basename = os.path.splitext(os.path.basename(file_path))[0]
        qualified_name = f"DML:{basename}"
        if not obj_name:
            obj_name = basename
        if db_name is None:
            db_name = ""

    return ParsedStatement(
        file_path=file_path,
        ddl_text=ddl_text,
        original_text=original_text,
        database_name=db_name,
        object_name=obj_name,
        object_type=object_type,
        strategy=strategy,
        qualified_name=qualified_name,
        multiset_injected=multiset_injected,
        deploy_intent=deploy_intent,
    )


def _detect_deploy_intent(ddl_text: str, object_type: ObjectType) -> DeployIntent:
    """
    Infer the developer's deployment intent from the DDL verb.

    The DDL verb IS the intent. SHIPS does not second-guess it.

    Args:
        ddl_text:     The original DDL text.
        object_type:  The classified object type.

    Returns:
        DeployIntent enum value.
    """
    # Tables always use the idempotent strategy regardless of verb
    if object_type == ObjectType.TABLE:
        return DeployIntent.IDEMPOTENT_DEPLOY

    # System-scope objects — skip silently if already present
    if object_type in (
        ObjectType.MAP,
        ObjectType.ROLE,
        ObjectType.PROFILE,
        ObjectType.AUTHORIZATION,
        ObjectType.FOREIGN_SERVER,
    ):
        return DeployIntent.SKIP_IF_EXISTS

    # Pre-requisites and DCL — execute as-is, no strategy
    if object_type in (
        ObjectType.DATABASE,
        ObjectType.USER,
        ObjectType.GRANT,
        ObjectType.REVOKE,
        ObjectType.JAR,
        # DML scripts (INSERT/UPDATE/DELETE/MERGE) execute as-is.
        # _execute_ddl already handles multi-statement bodies.
        ObjectType.DML,
    ):
        return DeployIntent.DIRECT_EXECUTE

    # JIs, hash indexes, secondary indexes — always DROP_AND_CREATE
    # (Teradata has no REPLACE for these types)
    if object_type in (ObjectType.JOIN_INDEX, ObjectType.HASH_INDEX, ObjectType.INDEX):
        return DeployIntent.DROP_AND_CREATE

    # Replaceable types: check if developer used REPLACE or CREATE
    # Teradata supports REPLACE for views, macros, procedures,
    # functions, triggers, AND script table operators.
    _replace_re = re.compile(
        r"REPLACE\s+(?:SPECIFIC\s+)?(?:VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER)",
        re.IGNORECASE,
    )

    if _replace_re.search(ddl_text):
        return DeployIntent.REPLACE_WITH_BACKUP

    # Developer wrote CREATE without REPLACE — they want CREATE_ONLY
    return DeployIntent.CREATE_ONLY


def parse_index_parent_table(ddl_text: str) -> Optional[Tuple[str, str]]:
    """
    Extract the parent table from a CREATE INDEX ... ON db.table DDL.

    Secondary indexes are bound to a table via the ON clause.
    This function extracts that parent table reference.

    Args:
        ddl_text: The CREATE INDEX DDL statement.

    Returns:
        Tuple of (database_name, table_name), or None if no ON clause.
    """
    match = _INDEX_ON_RE.search(ddl_text)
    if not match:
        return None
    return _split_qualified_name(match.group(1))


# ---------------------------------------------------------------
# Internal — Object type detection
# ---------------------------------------------------------------


def _detect_object_type(ddl_text: str) -> Tuple[ObjectType, str]:
    """
    Detect the object type and extract the qualified name.

    Tests patterns in specificity order: more specific patterns
    (JOIN INDEX, HASH INDEX) before general ones (TABLE, INDEX).

    Args:
        ddl_text: The DDL statement.

    Returns:
        Tuple of (ObjectType, raw_qualified_name_string).
        Returns (UNKNOWN, '') if no pattern matches.
    """
    # Order matters: test more specific patterns first
    patterns = [
        (_JOIN_INDEX_RE, ObjectType.JOIN_INDEX),
        (_HASH_INDEX_RE, ObjectType.HASH_INDEX),
        (_INDEX_RE, ObjectType.INDEX),
        (_SCRIPT_TABLE_OPERATOR_RE, ObjectType.SCRIPT_TABLE_OPERATOR),
        (_TABLE_RE, ObjectType.TABLE),
        (_VIEW_RE, ObjectType.VIEW),
        (_MACRO_RE, ObjectType.MACRO),
        (_PROCEDURE_RE, ObjectType.PROCEDURE),
        (_FUNCTION_RE, ObjectType.FUNCTION),
        (_TRIGGER_RE, ObjectType.TRIGGER),
        (_DATABASE_RE, ObjectType.DATABASE),
        (_USER_RE, ObjectType.USER),
        (_MAP_RE, ObjectType.MAP),
        (_PROFILE_RE, ObjectType.PROFILE),
        (_ROLE_RE, ObjectType.ROLE),
        (_AUTHORIZATION_RE, ObjectType.AUTHORIZATION),
        (_FOREIGN_SERVER_RE, ObjectType.FOREIGN_SERVER),
        (_JAR_INSTALL_RE, ObjectType.JAR),
        (_GRANT_RE, ObjectType.GRANT),
        (_REVOKE_RE, ObjectType.REVOKE),
        # DML last — comes after every CREATE/REPLACE/GRANT/REVOKE
        # form so a procedure body containing INSERT/UPDATE never
        # classifies as DML. A pure DML script reaches this rung.
        (_DML_RE, ObjectType.DML),
    ]

    for pattern, obj_type in patterns:
        match = pattern.search(ddl_text)
        if match:
            # For secondary indexes, the captured name is the index
            # name, not qualified. We need special handling.
            if obj_type == ObjectType.INDEX:
                return (obj_type, _get_index_qualified_name(ddl_text, match))
            # GRANT/REVOKE/JAR don't have a standard qualified name
            if obj_type in (ObjectType.GRANT, ObjectType.REVOKE, ObjectType.JAR):
                return (obj_type, obj_type.value)
            try:
                return (obj_type, match.group(1))
            except IndexError:
                return (obj_type, obj_type.value)

    return (ObjectType.UNKNOWN, "")


def _get_index_qualified_name(ddl_text: str, idx_match) -> str:
    """
    Build a qualified name for a secondary index.

    Secondary indexes are named but their identity includes the
    parent table. We use 'db.index_name' as the qualified name,
    extracting the database from the ON clause.

    Args:
        ddl_text:   The CREATE INDEX DDL.
        idx_match:  Regex match from the INDEX pattern.

    Returns:
        Qualified name string 'database.index_name'.
    """
    index_name = idx_match.group(1).strip('"')
    parent = parse_index_parent_table(ddl_text)
    if parent and parent[0]:
        return f"{parent[0]}.{index_name}"
    return index_name


# ---------------------------------------------------------------
# Internal — MULTISET injection
# ---------------------------------------------------------------


def _inject_multiset_if_missing(ddl_text: str) -> Tuple[str, bool]:
    """
    Inject MULTISET into CREATE TABLE if neither SET nor MULTISET
    is specified.

    Teradata defaults to SET tables, which impose duplicate-row
    checking overhead. MULTISET is the safer, more performant default.

    Args:
        ddl_text: The CREATE TABLE DDL statement.

    Returns:
        Tuple of (modified_ddl, was_injected).
        If SET or MULTISET was already present, returns the
        original DDL unchanged with was_injected=False.
    """
    if _HAS_SET_MULTISET_RE.search(ddl_text):
        return (ddl_text, False)

    # Inject 'MULTISET ' between 'CREATE ' and 'TABLE' (or 'VOLATILE TABLE')
    modified = _INJECT_MULTISET_RE.sub(r"\1MULTISET \2", ddl_text, count=1)

    if modified != ddl_text:
        return (modified, True)

    return (ddl_text, False)


# ---------------------------------------------------------------
# Internal — Name splitting (shared with v1)
# ---------------------------------------------------------------


def _split_qualified_name(qualified: str) -> Tuple[Optional[str], str]:
    """
    Split 'DB.Object' into (database, object). Handles quotes.

    Args:
        qualified: The raw qualified name string.

    Returns:
        Tuple of (database_name_or_None, object_name).
    """
    parts = _split_dot_respecting_quotes(qualified)
    if len(parts) == 2:
        return (_strip_quotes(parts[0]), _strip_quotes(parts[1]))
    elif len(parts) == 1:
        return (None, _strip_quotes(parts[0]))
    else:
        raise ValueError(f"Unexpected qualified name: '{qualified}'.")


def _split_dot_respecting_quotes(text: str) -> list:
    """Split on '.' but not inside double quotes."""
    parts, current, in_quotes = [], [], False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "." and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _strip_quotes(identifier: str) -> str:
    """Remove surrounding double quotes from an identifier."""
    s = identifier.strip()
    return s[1:-1] if s.startswith('"') and s.endswith('"') else s
