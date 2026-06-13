"""
sql_reference_extractor_sqlglot.py — AST-backed SqlReferenceExtractor (#234 Phase 2).

Implements :class:`SqlGlotSqlReferenceExtractor`, an AST-based
SqlReferenceExtractor backed by SQLGlot's Teradata dialect. Solves the
trust-sensitive false positives that pure regex scanning produces on
nested derived tables, CTEs, and comma joins by relying on SQLGlot's
scope-aware parse tree.

Per ADR 0015 Phase 2, this implementation is opt-in:

* Installed via the ``[ast]`` extra (``uv pip install -e ".[ast]"``).
* Selected explicitly by callers / tests; the package default remains
  the regex extractor until Phase 3 flips the default after the
  regression corpus is fully green.

When SQLGlot cannot parse the input — Teradata-specific syntax it does
not yet support — :meth:`SqlGlotSqlReferenceExtractor.extract_*` falls
back to the regex implementation. The fallback is observable via
:func:`compare_extractors`, which emits a structured diagnostic naming
which extractor saw what.

Implementation notes
====================

* SHIPS tokens (``{{TOKEN}}``) are not valid SQL identifiers. They are
  rewritten to a sentinel form (``__SHIPS_TKN_TOKEN``) before parsing
  and reconstructed on the way out.
* CALL / EXEC fall through to the regex implementation because
  SQLGlot's Teradata dialect parses them as opaque ``Command`` nodes
  and the procedure / macro name is not structurally available.
* Read sources exclude the CREATE/REPLACE owner so a view's own table
  reference (e.g. ``REPLACE VIEW DB.V`` produces a Table node for
  ``DB.V``) does not leak into its own grant set.

See also :mod:`td_release_packager.sql_reference_extractor` for the
abstraction and Phase-1 regex implementation, and ``docs/adr/0015-…``
for the migration plan.
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, Optional, Set

from td_release_packager.sql_reference_extractor import (
    PRIV_DELETE,
    PRIV_INSERT,
    PRIV_UPDATE,
    ReferencedObject,
    RegexSqlReferenceExtractor,
    SqlReferenceExtractor,
    StatementOwner,
)


# ---------------------------------------------------------------
# Token sentinel
# ---------------------------------------------------------------
#
# SQLGlot rejects ``{{TOKEN}}`` as a valid identifier. We replace each
# occurrence with an underscore-prefixed sentinel that SQL parsers
# treat as a normal identifier, then reconstruct the brace form when
# emitting :class:`ReferencedObject`.

_TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}", re.IGNORECASE)
_SENTINEL_PREFIX = "__SHIPS_TKN_"
_SENTINEL_RE = re.compile(
    rf"^{re.escape(_SENTINEL_PREFIX)}([A-Z][A-Z0-9_]*)$", re.IGNORECASE
)


def _to_sentinels(sql: str) -> str:
    return _TOKEN_RE.sub(lambda m: f"{_SENTINEL_PREFIX}{m.group(1)}", sql)


def _from_sentinel(identifier: str) -> str:
    """Reverse :func:`_to_sentinels` on a single identifier; pass-through
    when the identifier is not a token sentinel."""
    match = _SENTINEL_RE.match(identifier)
    if match is None:
        return identifier
    return f"{{{{{match.group(1)}}}}}"


# ---------------------------------------------------------------
# Optional-dependency guard
# ---------------------------------------------------------------


def is_available() -> bool:
    """Return True when SQLGlot is importable."""
    try:  # noqa: SIM105 — explicit import for the guard
        import sqlglot  # noqa: F401

        return True
    except ImportError:
        return False


class SqlGlotUnavailable(RuntimeError):
    """Raised when ``SqlGlotSqlReferenceExtractor`` is constructed
    without the ``[ast]`` extra installed."""


# ---------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------


class SqlGlotSqlReferenceExtractor(SqlReferenceExtractor):
    """AST-backed extractor for SHIPS SQL reference inference.

    The class falls back to a regex extractor on parse failure or for
    Teradata syntax SQLGlot cannot model structurally (currently
    CALL / EXEC). Callers that want to observe whether a fallback
    happened should use :func:`compare_extractors`.
    """

    DIALECT = "teradata"

    def __init__(self, fallback: Optional[SqlReferenceExtractor] = None) -> None:
        if not is_available():
            raise SqlGlotUnavailable(
                "sqlglot is not installed. Install the [ast] extra: "
                'uv pip install -e ".[ast]"'
            )
        self._fallback = fallback or RegexSqlReferenceExtractor()

    # -----------------------------------------------------------
    # AST helpers
    # -----------------------------------------------------------

    def _parse(self, sql: str):
        import sqlglot

        return sqlglot.parse_one(_to_sentinels(sql), dialect=self.DIALECT)

    @staticmethod
    def _table_to_ref(table) -> Optional[ReferencedObject]:
        db = (table.db or "").strip()
        name = (table.name or "").strip()
        if not db or not name:
            return None
        from td_release_packager.infer_grants import _is_excluded_db_ref

        db_out = _from_sentinel(db)
        if _is_excluded_db_ref(db_out):
            return None
        return ReferencedObject(database=db_out, object_name=name)

    # -----------------------------------------------------------
    # extract_statement_owner
    # -----------------------------------------------------------

    def extract_statement_owner(self, sql: str) -> Optional[StatementOwner]:
        try:
            tree = self._parse(sql)
        except Exception:
            return self._fallback.extract_statement_owner(sql)

        from sqlglot import exp

        create = tree.find(exp.Create)
        if create is None:
            return self._fallback.extract_statement_owner(sql)

        kind = (create.args.get("kind") or "").upper().strip()
        if "TABLE" in kind:
            object_type = "TABLE"
        elif kind:
            object_type = kind
        else:
            return self._fallback.extract_statement_owner(sql)

        target = create.this
        # The CREATE target may be a Table, a Schema (TABLE definitions
        # wrap columns), or a UserDefinedFunction (PROCEDURE/FUNCTION).
        # All three expose ``.db`` and ``.name`` via the same accessor
        # chain when they wrap an Identifier.
        if target is None:
            return self._fallback.extract_statement_owner(sql)

        owner_table = (
            target if isinstance(target, exp.Table) else target.find(exp.Table)
        )
        if owner_table is None:
            return self._fallback.extract_statement_owner(sql)

        db = _from_sentinel((owner_table.db or "").strip())
        name = (owner_table.name or "").strip()
        if not db or not name:
            return self._fallback.extract_statement_owner(sql)
        return StatementOwner(
            database=db,
            object_name=name,
            object_type=object_type,
        )

    # -----------------------------------------------------------
    # extract_read_sources
    # -----------------------------------------------------------

    def extract_read_sources(self, sql: str) -> Set[ReferencedObject]:
        try:
            tree = self._parse(sql)
        except Exception:
            return self._fallback.extract_read_sources(sql)

        from sqlglot import exp

        owner = self.extract_statement_owner(sql)
        result: Set[ReferencedObject] = set()
        for table in tree.find_all(exp.Table):
            parent = table.parent
            if isinstance(parent, exp.UserDefinedFunction):
                continue  # owner of a PROCEDURE / FUNCTION
            if isinstance(parent, exp.Create):
                continue  # owner of a VIEW / MACRO
            if isinstance(parent, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                # MERGE target is the .this of the Merge node, but a
                # USING subquery's FROM is parented to a From node, so
                # this check correctly excludes the write target from
                # reads.
                if parent.args.get("this") is table:
                    continue
            ref = self._table_to_ref(table)
            if ref is None:
                continue
            if owner is not None and (
                ref.database == owner.database and ref.object_name == owner.object_name
            ):
                continue
            result.add(ref)
        return result

    # -----------------------------------------------------------
    # extract_write_targets
    # -----------------------------------------------------------

    def extract_write_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        try:
            tree = self._parse(sql)
        except Exception:
            return self._fallback.extract_write_targets(sql)

        from sqlglot import exp

        accumulated: Dict[ReferencedObject, Set[str]] = {}

        def _add(node, privileges: FrozenSet[str]) -> None:
            target = node.args.get("this") if node else None
            if target is None:
                return
            table = target if isinstance(target, exp.Table) else target.find(exp.Table)
            if table is None:
                return
            ref = self._table_to_ref(table)
            if ref is None:
                return
            accumulated.setdefault(ref, set()).update(privileges)

        for node in tree.find_all(exp.Insert):
            _add(node, frozenset({PRIV_INSERT}))
        for node in tree.find_all(exp.Update):
            _add(node, frozenset({PRIV_UPDATE}))
        for node in tree.find_all(exp.Delete):
            _add(node, frozenset({PRIV_DELETE}))
        for node in tree.find_all(exp.Merge):
            _add(node, frozenset({PRIV_INSERT, PRIV_UPDATE}))

        return {key: frozenset(value) for key, value in accumulated.items()}

    # -----------------------------------------------------------
    # extract_call_targets
    # -----------------------------------------------------------

    def extract_call_targets(self, sql: str) -> Dict[ReferencedObject, FrozenSet[str]]:
        # SQLGlot's Teradata dialect parses CALL / EXEC / EXECUTE as
        # opaque Command nodes; the procedure / macro name is not
        # structurally available. Phase 2 delegates to the regex impl.
        # When sqlglot grows real CALL parsing, this method swaps
        # without changing the abstraction.
        return self._fallback.extract_call_targets(sql)
