"""
sql_reference_extractor.py — Abstraction for SQL reference inference (#234, ADR 0015).

SHIPS infers inter-database grants and dependency edges by scanning
Teradata SQL for qualified references. Historically that work used
regex patterns scattered across ``infer_grants.py`` and ``analyser.py``.
Real customer code (nested derived tables, CTEs, comma joins, system
database calls) breaks those patterns in trust-sensitive ways — a
spurious ``GRANT SELECT ON sRV_ProcessSumByBusDate TO GDEV1V_OPR``
blocks production deploys.

ADR 0015 proposes migrating reference extraction to an AST-backed
parser (SQLGlot) behind a small internal abstraction so the migration
can land incrementally with a regex fallback. This module is the
abstraction layer: a single :class:`SqlReferenceExtractor` ABC plus
dataclasses for the four return shapes.

Phase 1 (this module + :class:`RegexSqlReferenceExtractor`):
    Mechanical wrap of today's regex logic so every consumer reads
    through one boundary. No behaviour change.

Phase 2:
    Add ``SqlGlotSqlReferenceExtractor`` and a compare-mode harness.

Phase 3+:
    Make AST authoritative, retire regex.

See ``docs/adr/0015-ast-based-sql-reference-inference.md`` for the
full migration plan.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Set


# ---------------------------------------------------------------
# Value types
# ---------------------------------------------------------------


@dataclass(frozen=True)
class ReferencedObject:
    """A database-qualified SQL object reference.

    ``database`` is either a literal Teradata database name
    (``MyDB``) or a SHIPS token in canonical form (``{{DOM_T}}``).
    ``object_name`` is the unqualified object identifier.
    """

    database: str
    object_name: str


@dataclass(frozen=True)
class StatementOwner:
    """The CREATE/REPLACE target of a DDL file.

    ``object_type`` is one of ``VIEW``, ``PROCEDURE``, ``MACRO``,
    ``FUNCTION``, ``TRIGGER``, ``TABLE``. Compound forms (``SET
    TABLE``, ``MULTISET TABLE``, ``VOLATILE TABLE``) are normalised
    to ``TABLE``.
    """

    database: str
    object_name: str
    object_type: str


# ---------------------------------------------------------------
# Canonical privilege vocabulary
# ---------------------------------------------------------------
#
# Mirrors the names ``infer_grants`` uses when assembling .dcl files.
# Centralised here so AST and regex implementations agree.

PRIV_SELECT = "SELECT"
PRIV_INSERT = "INSERT"
PRIV_UPDATE = "UPDATE"
PRIV_DELETE = "DELETE"
PRIV_EXEC_PROC = "EXECUTE PROCEDURE"
PRIV_EXEC = "EXECUTE"


# ---------------------------------------------------------------
# Abstraction
# ---------------------------------------------------------------


class SqlReferenceExtractor(ABC):
    """Extract database-qualified references from Teradata SQL.

    Concrete extractors (regex, SQLGlot) implement the same contract
    so consumers can swap implementations without code change.
    """

    @abstractmethod
    def extract_statement_owner(self, sql: str) -> Optional[StatementOwner]:
        """Return the CREATE/REPLACE target of ``sql`` or ``None``.

        The owner is the object SHIPS is currently building (e.g. the
        view being defined). Used by grant inference to identify the
        grantee database.
        """

    @abstractmethod
    def extract_read_sources(self, sql: str) -> Set[ReferencedObject]:
        """Return every object read by ``sql`` in a FROM / JOIN / USING
        context.

        Excludes derived-table aliases, CTE names, comma-join
        correlation aliases, and system databases (DBC, SYSLIB, etc.).
        """

    @abstractmethod
    def extract_write_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        """Return objects written by ``sql`` mapped to their inferred
        privileges.

        DML write verbs map as follows:
          * ``INSERT`` → ``{INSERT}``
          * ``UPDATE`` → ``{UPDATE}``
          * ``DELETE`` → ``{DELETE}``
          * ``MERGE``  → ``{INSERT, UPDATE}`` (Teradata MERGE may do either)
        """

    @abstractmethod
    def extract_call_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        """Return procedures or macros invoked by ``sql`` mapped to their
        inferred privileges.

          * ``CALL``           → ``{EXECUTE PROCEDURE}``
          * ``EXEC`` / ``EXECUTE`` → ``{EXECUTE}`` (macro)
        """


# ---------------------------------------------------------------
# Regex implementation
# ---------------------------------------------------------------


class RegexSqlReferenceExtractor(SqlReferenceExtractor):
    """Phase-1 implementation that wraps the existing regex scanners.

    Delegates to the helpers in :mod:`td_release_packager.infer_grants`
    so the abstraction is purely a contract refactor — same outputs as
    the historical scanner. Phase 2 adds an AST-backed sibling
    implementation; consumers switch by constructing a different
    extractor.

    SQL passed to any method should already be comment-stripped
    (callers use ``infer_grants.strip_sql_comments``). Implementations
    do not strip a second time.
    """

    def extract_statement_owner(self, sql: str) -> Optional[StatementOwner]:
        # Imported lazily so the abstraction module has no module-level
        # dependency on the regex catalogue — Phase 2's SqlGlot impl
        # must not pull these patterns in.
        from td_release_packager.infer_grants import RE_CREATE_STMT, extract_db_ref

        match = RE_CREATE_STMT.search(sql)
        if match is None:
            return None

        obj_type_raw = match.group(1).upper().strip()
        if "TABLE" in obj_type_raw:
            obj_type = "TABLE"
        else:
            obj_type = obj_type_raw

        database = extract_db_ref(match, token_group=2, literal_group=3)
        object_name = match.group(4)
        return StatementOwner(
            database=database,
            object_name=object_name,
            object_type=obj_type,
        )

    def extract_read_sources(self, sql: str) -> Set[ReferencedObject]:
        from td_release_packager.infer_grants import (
            _is_excluded_db_ref,
            appears_as_read_source,
            find_all_object_references,
        )

        result: Set[ReferencedObject] = set()
        for db_ref, obj_name in find_all_object_references(sql, tokens_only=False):
            if _is_excluded_db_ref(db_ref):
                continue
            if appears_as_read_source(sql, db_ref):
                result.add(ReferencedObject(database=db_ref, object_name=obj_name))
        return result

    def extract_write_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        from td_release_packager.infer_grants import (
            RE_DELETE_TARGET,
            RE_INSERT_TARGET,
            RE_MERGE_TARGET,
            RE_UPDATE_TARGET,
            _is_excluded_db_ref,
            extract_object_ref,
        )

        # (regex, set-of-privileges-it-implies)
        target_specs = (
            (RE_INSERT_TARGET, frozenset({PRIV_INSERT})),
            (RE_UPDATE_TARGET, frozenset({PRIV_UPDATE})),
            (RE_DELETE_TARGET, frozenset({PRIV_DELETE})),
            (RE_MERGE_TARGET, frozenset({PRIV_INSERT, PRIV_UPDATE})),
        )

        accumulated: Dict[ReferencedObject, Set[str]] = {}
        for pattern, privs in target_specs:
            for match in pattern.finditer(sql):
                db, obj = extract_object_ref(match)
                if _is_excluded_db_ref(db):
                    continue
                key = ReferencedObject(database=db, object_name=obj)
                accumulated.setdefault(key, set()).update(privs)
        return {key: frozenset(value) for key, value in accumulated.items()}

    def extract_call_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        from td_release_packager.infer_grants import (
            RE_CALL_TARGET,
            RE_EXEC_TARGET,
            _is_excluded_db_ref,
            extract_object_ref,
        )

        target_specs = (
            (RE_CALL_TARGET, frozenset({PRIV_EXEC_PROC})),
            (RE_EXEC_TARGET, frozenset({PRIV_EXEC})),
        )

        accumulated: Dict[ReferencedObject, Set[str]] = {}
        for pattern, privs in target_specs:
            for match in pattern.finditer(sql):
                db, obj = extract_object_ref(match)
                if _is_excluded_db_ref(db):
                    continue
                key = ReferencedObject(database=db, object_name=obj)
                accumulated.setdefault(key, set()).update(privs)
        return {key: frozenset(value) for key, value in accumulated.items()}


# ---------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------


def default_extractor() -> SqlReferenceExtractor:
    """Return the extractor SHIPS uses by default.

    Phase 1: regex. Phase 2 will add an environment-driven switch so
    operators can opt into the AST implementation; Phase 3 flips the
    default once the regression corpus is green; Phase 5 retires this
    function in favour of constructing the AST extractor directly.
    """
    return RegexSqlReferenceExtractor()
