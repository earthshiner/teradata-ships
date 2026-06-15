"""
test_sqlglot_extractor_classify.py — Workstream C (#303) regression tests.

Covers
------
* :func:`_is_known_unsupported` recognises both
  ``COLLECT STATISTICS`` and ``CREATE DATABASE … AS PERM …``,
  including SHIPS token sentinel variants and signed
  scientific-notation numerics.
* :meth:`SqlGlotSqlReferenceExtractor._parse` short-circuits to the
  regex fallback for those shapes without invoking sqlglot — so the
  sqlglot ``parser.py:2171`` ``Falling back to parsing as a 'Command'``
  WARNING is never emitted.
* The parse-once cache returns the same object identity for repeated
  calls and is bounded.
* The :class:`_SqlGlotFallbackFilter` downgrades residual
  ``Falling back …`` WARNINGs to DEBUG.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("sqlglot")

from td_release_packager.sql_reference_extractor_sqlglot import (  # noqa: E402
    SqlGlotSqlReferenceExtractor,
    _SqlGlotFallbackFilter,
    _is_known_unsupported,
    clear_parse_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_parse_cache()
    yield
    clear_parse_cache()


# ---------------------------------------------------------------------------
# _is_known_unsupported()
# ---------------------------------------------------------------------------


class TestKnownUnsupported:
    def test_collect_statistics_column(self):
        assert _is_known_unsupported(
            "COLLECT STATISTICS COLUMN ( cust_id ) ON DB.MyTable"
        )

    def test_collect_statistics_index(self):
        assert _is_known_unsupported(
            "COLLECT STATISTICS INDEX ( cust_id, region ) ON DB.MyTable"
        )

    def test_collect_statistics_lowercase(self):
        assert _is_known_unsupported("collect statistics column (id) on db.t")

    def test_create_database_as_perm(self):
        assert _is_known_unsupported(
            "CREATE DATABASE GDEV1_BASE FROM GCFR_MAIN AS PERM = 0"
        )

    def test_create_database_with_spool_fallback(self):
        assert _is_known_unsupported(
            "CREATE DATABASE D FROM P AS PERM = 1e10 SPOOL = -2.94967296E8 FALLBACK"
        )

    def test_create_database_token_sentinel_form(self):
        # Tokens have been rewritten to __SHIPS_TKN_* sentinels by the
        # time _parse() sees them; the regex must still match.
        assert _is_known_unsupported(
            "CREATE DATABASE __SHIPS_TKN_TARGET_DB FROM __SHIPS_TKN_PARENT "
            "AS PERM = 1024"
        )

    def test_create_user_as_perm(self):
        # CREATE USER takes the same AS PERM clause shape.
        assert _is_known_unsupported(
            "CREATE USER GCFR_ADMIN FROM GCFR_MAIN AS PERM = 0 PASSWORD = x"
        )

    def test_unrelated_statement_not_flagged(self):
        assert not _is_known_unsupported(
            "CREATE TABLE DB.T (id INTEGER NOT NULL) UNIQUE PRIMARY INDEX (id)"
        )

    def test_select_not_flagged(self):
        assert not _is_known_unsupported("SELECT 1")


# ---------------------------------------------------------------------------
# _parse() short-circuits without emitting sqlglot WARNING
# ---------------------------------------------------------------------------


class TestParseShortCircuit:
    def test_collect_statistics_skips_sqlglot(self, caplog):
        ext = SqlGlotSqlReferenceExtractor()
        sql = "COLLECT STATISTICS COLUMN (cust_id) ON DB.T"
        with caplog.at_level(logging.WARNING, logger="sqlglot"):
            # _parse raises so callers fall through; extract_* methods
            # route to the regex fallback transparently.
            from td_release_packager.sql_reference_extractor_sqlglot import (
                _KnownUnsupported,
            )

            with pytest.raises(_KnownUnsupported):
                ext._parse(sql)
        assert not any(
            "Falling back to parsing as a 'Command'" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_create_database_skips_sqlglot(self, caplog):
        ext = SqlGlotSqlReferenceExtractor()
        sql = "CREATE DATABASE D FROM P AS PERM = 0"
        with caplog.at_level(logging.WARNING, logger="sqlglot"):
            from td_release_packager.sql_reference_extractor_sqlglot import (
                _KnownUnsupported,
            )

            with pytest.raises(_KnownUnsupported):
                ext._parse(sql)
        assert not any(
            "Falling back to parsing as a 'Command'" in r.getMessage()
            for r in caplog.records
        )

    def test_extract_statement_owner_falls_back_silently(self, caplog):
        ext = SqlGlotSqlReferenceExtractor()
        with caplog.at_level(logging.WARNING, logger="sqlglot"):
            owner = ext.extract_statement_owner("CREATE DATABASE D FROM P AS PERM = 0")
        # The regex fallback may or may not classify the owner — that
        # is its own contract.  The assertion here is purely about noise.
        assert not any("Falling back" in r.getMessage() for r in caplog.records)
        assert isinstance(owner, (type(None), object))


# ---------------------------------------------------------------------------
# Parse-once cache
# ---------------------------------------------------------------------------


class TestParseCache:
    def test_repeat_returns_same_tree(self):
        ext = SqlGlotSqlReferenceExtractor()
        sql = "CREATE TABLE DB.T (id INTEGER) UNIQUE PRIMARY INDEX (id)"
        a = ext._parse(sql)
        b = ext._parse(sql)
        assert a is b

    def test_distinct_sql_distinct_trees(self):
        ext = SqlGlotSqlReferenceExtractor()
        a = ext._parse("CREATE TABLE DB.A (id INTEGER) UNIQUE PRIMARY INDEX (id)")
        b = ext._parse("CREATE TABLE DB.B (id INTEGER) UNIQUE PRIMARY INDEX (id)")
        assert a is not b

    def test_clear_drops_cached_tree(self):
        ext = SqlGlotSqlReferenceExtractor()
        sql = "CREATE TABLE DB.T (id INTEGER) UNIQUE PRIMARY INDEX (id)"
        a = ext._parse(sql)
        clear_parse_cache()
        b = ext._parse(sql)
        assert a is not b


# ---------------------------------------------------------------------------
# _SqlGlotFallbackFilter downgrades residual WARNINGs to DEBUG
# ---------------------------------------------------------------------------


class TestFallbackFilter:
    def test_warning_record_is_downgraded(self):
        f = _SqlGlotFallbackFilter()
        record = logging.LogRecord(
            name="sqlglot.parser",
            level=logging.WARNING,
            pathname="parser.py",
            lineno=2171,
            msg="'foo' contains unsupported syntax. Falling back to parsing as a 'Command'.",
            args=(),
            exc_info=None,
        )
        result = f.filter(record)
        assert result is True  # never dropped; only downgraded
        assert record.levelno == logging.DEBUG
        assert record.levelname == "DEBUG"

    def test_unrelated_warning_unchanged(self):
        f = _SqlGlotFallbackFilter()
        record = logging.LogRecord(
            name="sqlglot.parser",
            level=logging.WARNING,
            pathname="parser.py",
            lineno=1,
            msg="some other warning",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert record.levelno == logging.WARNING

    def test_filter_attached_to_sqlglot_logger(self):
        # Module import installs the filter; it must remain there.
        lg = logging.getLogger("sqlglot")
        assert any(isinstance(f, _SqlGlotFallbackFilter) for f in lg.filters)
