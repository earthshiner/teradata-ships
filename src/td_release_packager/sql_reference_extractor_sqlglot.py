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

import hashlib
import logging
import re
from typing import Any, Dict, FrozenSet, Optional, Set

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
# Known-unsupported Teradata DDL shapes (#303)
# ---------------------------------------------------------------
#
# sqlglot's Teradata dialect emits a WARNING when it cannot model a
# statement structurally and falls back to a generic ``Command`` node:
#
#     'create database ... as perm = 0' contains unsupported syntax.
#     Falling back to parsing as a 'Command'.
#
# The two shapes below dominate the noise on real Teradata payloads.
# Recognising them BEFORE calling sqlglot stops the parser from
# emitting the WARNING and short-circuits straight to the regex
# fallback extractor, which already classifies both correctly.

#: ``COLLECT STATISTICS [COLUMN | INDEX] ( ... ) ON db.obj``
_COLLECT_STATS_RE = re.compile(
    r"^\s*COLLECT\s+STATISTICS\b",
    re.IGNORECASE,
)

#: ``CREATE DATABASE name FROM parent AS PERM = n [SPOOL = n] [FALLBACK]``
#: — including signed-scientific-notation numerics and token sentinels.
_CREATE_DATABASE_AS_PERM_RE = re.compile(
    r"^\s*CREATE\s+(?:DATABASE|USER)\b[\s\S]*?\bAS\s+PERM\b",
    re.IGNORECASE,
)

_KNOWN_UNSUPPORTED_RES: tuple[re.Pattern[str], ...] = (
    _COLLECT_STATS_RE,
    _CREATE_DATABASE_AS_PERM_RE,
)


class _KnownUnsupported(Exception):
    """Raised by :meth:`SqlGlotSqlReferenceExtractor._parse` when the
    input matches a known-unsupported shape, so the existing
    ``except Exception`` fallback paths in the extractor pick up the
    regex extractor without sqlglot ever being invoked.  Subclass of
    :class:`Exception` so it threads through unmodified."""


def _is_known_unsupported(sql: str) -> bool:
    """Return ``True`` when ``sql`` is one of the Teradata DDL shapes
    that sqlglot's dialect cannot model structurally."""
    for pattern in _KNOWN_UNSUPPORTED_RES:
        if pattern.search(sql):
            return True
    return False


# ---------------------------------------------------------------
# sqlglot WARNING downgrade
# ---------------------------------------------------------------
#
# Any residual ``Falling back to parsing as a 'Command'`` warnings —
# from statement shapes not covered by ``_KNOWN_UNSUPPORTED_RES`` —
# are downgraded to DEBUG.  We do NOT drop them: under a DEBUG handler
# they remain visible for diagnosis.

_SQLGLOT_FALLBACK_MSG_RE = re.compile(
    r"Falling back to parsing as a 'Command'", re.IGNORECASE
)


class _SqlGlotFallbackFilter(logging.Filter):
    """Logging filter that downgrades sqlglot's parser-fallback
    WARNINGs to DEBUG.  Attached to the ``sqlglot`` logger at import
    time of this module."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING and _SQLGLOT_FALLBACK_MSG_RE.search(
            record.getMessage()
        ):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


# Module-level idempotent install (attaches at most one filter per
# process even if this module is re-imported).
_sqlglot_logger = logging.getLogger("sqlglot")
if not any(isinstance(f, _SqlGlotFallbackFilter) for f in _sqlglot_logger.filters):
    _sqlglot_logger.addFilter(_SqlGlotFallbackFilter())


# ---------------------------------------------------------------
# Per-process parse-once cache (#303)
# ---------------------------------------------------------------
#
# Harvest → inspect → analyse → package each re-parse the same DDL,
# accounting for the ~5x duplication in the noise budget.  A single
# in-memory cache keyed on the content hash lets every later phase
# reuse the parse tree.  The cache is in-process only and is bounded
# by ``_PARSE_CACHE_MAX`` to keep memory predictable on huge payloads.

_PARSE_CACHE_MAX = 4096
_parse_cache: Dict[str, Any] = {}


def _cache_key(dialect: str, sql: str) -> str:
    return f"{dialect}:{hashlib.sha256(sql.encode('utf-8')).hexdigest()}"


def clear_parse_cache() -> None:
    """Empty the parse-once cache.  Used by tests; harmless to call
    from production code if memory pressure becomes a concern."""
    _parse_cache.clear()


# ---------------------------------------------------------------
# Token sentinel
# ---------------------------------------------------------------
#
# SQLGlot rejects ``{{TOKEN}}`` as a valid identifier. We replace each
# occurrence with an underscore-prefixed sentinel that SQL parsers
# treat as a normal identifier, then reconstruct the brace form when
# emitting :class:`ReferencedObject`.

_TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}", re.IGNORECASE)

# Sentinel encoding for ``{{TOKEN}}`` — bracketed on both sides so the
# boundary is unambiguous even when the token is followed by literal
# characters. Earlier versions used an open-ended ``__SHIPS_TKN_TOKEN``
# form, which collapsed a compound identifier such as
# ``{{DB_PREFIX}}_SEM_STD_V`` into ``__SHIPS_TKN_DB_PREFIX_SEM_STD_V``
# during encoding — SQLGlot could not tell where the token ended, and
# decoding wrapped the whole compound as a single token
# (``{{DB_PREFIX_SEM_STD_V}}``). See issue #390.
_SENTINEL_PREFIX = "__SHIPS_TKB__"  # token-begin marker
_SENTINEL_SUFFIX = "__SHIPS_TKE__"  # token-end marker
_SENTINEL_RE = re.compile(
    rf"{re.escape(_SENTINEL_PREFIX)}([A-Z][A-Z0-9_]*?){re.escape(_SENTINEL_SUFFIX)}",
    re.IGNORECASE,
)


def _to_sentinels(sql: str) -> str:
    return _TOKEN_RE.sub(
        lambda m: f"{_SENTINEL_PREFIX}{m.group(1)}{_SENTINEL_SUFFIX}", sql
    )


def _from_sentinel(identifier: str) -> str:
    """Reverse :func:`_to_sentinels` on an identifier, including compound
    forms — a ``{{TOKEN}}`` followed by a literal suffix encodes as
    ``__SHIPS_TKB__TOKEN__SHIPS_TKE___SUFFIX`` and must decode back to
    ``{{TOKEN}}_SUFFIX`` (the prefix-tokenisation shape). Non-sentinel
    text is passed through unchanged."""
    return _SENTINEL_RE.sub(lambda m: f"{{{{{m.group(1)}}}}}", identifier)


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
        """Parse ``sql`` with sqlglot, using a process-wide content-keyed
        cache and short-circuiting on Teradata DDL shapes the parser is
        known not to model structurally.

        Raises :class:`_KnownUnsupported` (a plain :class:`Exception`) for
        ``COLLECT STATISTICS`` and ``CREATE DATABASE … AS PERM …``, so
        every caller's existing ``except Exception`` falls through to
        the regex extractor without sqlglot ever logging a fallback
        WARNING — and without re-parsing the same statement when the
        next pipeline phase asks again.
        """
        if _is_known_unsupported(sql):
            raise _KnownUnsupported(
                "known-unsupported Teradata DDL shape (see "
                "_KNOWN_UNSUPPORTED_RES) — fall back to regex"
            )

        key = _cache_key(self.DIALECT, sql)
        cached = _parse_cache.get(key)
        if cached is not None:
            return cached

        import sqlglot

        tree = sqlglot.parse_one(_to_sentinels(sql), dialect=self.DIALECT)

        # Bound the cache so a long-running server doesn't grow it
        # unboundedly.  Eviction policy: drop the oldest insertion
        # (Python 3.7+ dicts preserve insertion order).
        if len(_parse_cache) >= _PARSE_CACHE_MAX:
            try:
                oldest_key = next(iter(_parse_cache))
                _parse_cache.pop(oldest_key, None)
            except StopIteration:  # pragma: no cover — cache empty
                pass
        _parse_cache[key] = tree
        return tree

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
        write_verb_nodes = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
        for table in tree.find_all(exp.Table):
            parent = table.parent
            if isinstance(parent, exp.UserDefinedFunction):
                continue  # owner of a PROCEDURE / FUNCTION
            if isinstance(parent, exp.Create):
                continue  # owner of a VIEW / MACRO
            if isinstance(parent, write_verb_nodes):
                # MERGE target is the .this of the Merge node, USING
                # subquery's FROM is parented to a From node — this
                # check excludes the write target itself.
                if parent.args.get("this") is table:
                    continue
            elif isinstance(parent, exp.Schema) and isinstance(
                parent.parent, write_verb_nodes
            ):
                # ``INSERT INTO db.t (col, col)`` wraps the Table in a
                # Schema whose parent is the Insert. The Table is still
                # the write target — not a read source.
                if (
                    parent.args.get("this") is table
                    and parent.parent.args.get("this") is parent
                ):
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

        def _target_tables(node) -> list:
            """Return the candidate target ``exp.Table`` nodes for a
            DML node. SQLGlot exposes the write target through
            ``this`` for INSERT / UPDATE / MERGE / classical DELETE,
            but the Teradata ``DEL`` abbreviation (no FROM) sets
            ``this=False`` and puts the table in ``tables``."""
            candidates = []
            this = node.args.get("this")
            if isinstance(this, exp.Table):
                candidates.append(this)
            elif this is not None and not isinstance(this, bool):
                # ``this`` may be a Schema(table=..., expressions=[cols]).
                nested = this.find(exp.Table) if hasattr(this, "find") else None
                if nested is not None:
                    candidates.append(nested)
            for entry in node.args.get("tables") or []:
                if isinstance(entry, exp.Table):
                    candidates.append(entry)
            return candidates

        def _add(node, privileges: FrozenSet[str]) -> None:
            for table in _target_tables(node):
                ref = self._table_to_ref(table)
                if ref is None:
                    continue
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
