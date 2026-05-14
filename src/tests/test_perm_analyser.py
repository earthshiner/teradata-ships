"""
test_perm_analyser.py — Unit tests for td_release_packager.perm_analyser.

Run with:
    PYTHONPATH=src python -m pytest src/tests/test_perm_analyser.py -q

Tests cover:

  - _parse_perm_bytes: unit conversions (raw, K, M, G, T)
  - _strip_comments: line comments and block comments
  - _extract_perm_declarations: CREATE and MODIFY, with and without PERM
  - _extract_database_from_ddl: qualified name extraction
  - analyse_perm_space (integration): full payload walk with a temp dir
    - OK path: declared PERM > estimated floor
    - INSUFFICIENT path: declared PERM < estimated floor
    - WARNING path: headroom < 20% of declared PERM
    - UNKNOWN path: objects but no CREATE in package
    - MODIFY-only path: no CREATE, MODIFY sets effective PERM
    - No objects path: CREATE exists but no space-consuming files
    - Multi-suffix K/M/G parsing in PERM clauses
    - Comment stripping prevents phantom PERM extraction
    - Directory-based database inference when no qualified name present
"""

from __future__ import annotations

import textwrap

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from td_release_packager.perm_analyser import (
    _extract_database_from_ddl,
    _extract_perm_declarations,
    _format_bytes,
    _parse_perm_bytes,
    analyse_perm_space,
)

# _strip_comments delegates to sql_text — test comment stripping via
# _extract_perm_declarations (which calls it internally) rather than
# calling the private wrapper directly.
from td_release_packager.sql_text import (
    strip_comments_and_string_literals as _strip_comments,
)


# ---------------------------------------------------------------------------
# Helpers for building temporary payload directories
# ---------------------------------------------------------------------------


def _write(tmp_path, rel_path: str, content: str) -> str:
    """Write *content* to *tmp_path / rel_path*, creating parents as needed."""
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(target)


# ===========================================================================
# _parse_perm_bytes
# ===========================================================================


class TestParsePermanentBytes:
    """Unit tests for _parse_perm_bytes."""

    def test_raw_bytes(self):
        """Integer value with no suffix is taken as raw bytes."""
        assert _parse_perm_bytes("1000000", "") == 1_000_000

    def test_kilobytes(self):
        """K suffix multiplies by 1024."""
        assert _parse_perm_bytes("512", "K") == 512 * 1024

    def test_megabytes(self):
        """M suffix multiplies by 1024^2."""
        assert _parse_perm_bytes("500", "M") == 500 * 1024**2

    def test_gigabytes(self):
        """G suffix multiplies by 1024^3."""
        assert _parse_perm_bytes("2", "G") == 2 * 1024**3

    def test_terabytes(self):
        """T suffix multiplies by 1024^4."""
        assert _parse_perm_bytes("1", "T") == 1024**4

    def test_lowercase_suffix(self):
        """Suffix matching is case-insensitive."""
        assert _parse_perm_bytes("4", "g") == 4 * 1024**3

    def test_decimal_megabytes(self):
        """Decimal values are supported (rounded to int)."""
        result = _parse_perm_bytes("1.5", "M")
        assert result == int(1.5 * 1024**2)


# ===========================================================================
# _strip_comments
# ===========================================================================


class TestStripComments:
    """Unit tests for _strip_comments."""

    def test_removes_line_comment(self):
        """Line comments (--) are replaced with whitespace."""
        content = "CREATE DATABASE Foo -- this is a comment\nPERM = 100;"
        stripped = _strip_comments(content)
        assert "this is a comment" not in stripped
        assert "PERM = 100" in stripped

    def test_removes_block_comment(self):
        """Block comments (/* */) are replaced with whitespace."""
        content = "CREATE DATABASE /* comment */ Foo PERM = 200;"
        stripped = _strip_comments(content)
        assert "comment" not in stripped
        assert "PERM = 200" in stripped

    def test_multiline_block_comment(self):
        """Multi-line block comments are fully removed."""
        content = (
            "/*\n  Description of the database\n*/\nCREATE DATABASE Foo PERM = 300;"
        )
        stripped = _strip_comments(content)
        assert "Description" not in stripped
        assert "PERM = 300" in stripped

    def test_perm_inside_comment_not_extracted(self):
        """A PERM value inside a comment must not be parsed as live DDL."""
        content = "-- PERM = 9999999\nCREATE DATABASE Foo PERM = 100;"
        stripped = _strip_comments(content)
        # After stripping, only one PERM= appears (the live one)
        assert stripped.count("PERM") == 1


# ===========================================================================
# _extract_perm_declarations
# ===========================================================================


class TestExtractPermDeclarations:
    """Unit tests for _extract_perm_declarations."""

    def test_create_database_perm(self):
        """PERM from CREATE DATABASE is extracted as a non-modify declaration."""
        content = "CREATE DATABASE MyDb AS PERM = 1000000;"
        decls = _extract_perm_declarations("/some/path/MyDb.db", content)
        assert len(decls) == 1
        assert decls[0].container_name == "MyDb"
        assert decls[0].perm_bytes == 1_000_000
        assert decls[0].is_modify is False

    def test_create_user_perm(self):
        """PERM from CREATE USER is extracted."""
        content = "CREATE USER MyUser AS PERM = 500M;"
        decls = _extract_perm_declarations("/some/path/MyUser.usr", content)
        assert len(decls) == 1
        assert decls[0].container_name == "MyUser"
        assert decls[0].perm_bytes == 500 * 1024**2
        assert decls[0].is_modify is False

    def test_modify_database_perm(self):
        """PERM from MODIFY DATABASE is extracted as a modify declaration."""
        content = "MODIFY DATABASE MyDb AS PERM = 2G;"
        decls = _extract_perm_declarations("/some/path/MyDb.db", content)
        assert len(decls) == 1
        assert decls[0].is_modify is True
        assert decls[0].perm_bytes == 2 * 1024**3

    def test_no_perm_clause(self):
        """Files without a PERM clause return an empty list."""
        content = "CREATE DATABASE MyDb AS DEFAULT JOURNAL TABLE = DBC.AccLogTbl;"
        decls = _extract_perm_declarations("/some/path/MyDb.db", content)
        assert decls == []

    def test_multiple_statements(self):
        """A file with both CREATE and MODIFY yields two declarations."""
        content = (
            "CREATE DATABASE MyDb AS PERM = 100M;\n"
            "MODIFY DATABASE MyDb AS PERM = 200M;\n"
        )
        decls = _extract_perm_declarations("/some/path/MyDb.db", content)
        assert len(decls) == 2
        assert decls[0].is_modify is False
        assert decls[1].is_modify is True

    def test_perm_with_spaces_around_equals(self):
        """PERM = nnn with spaces around = is parsed correctly."""
        content = "CREATE DATABASE SpaceDb AS PERM = 256 M;"
        decls = _extract_perm_declarations("/some/path/SpaceDb.db", content)
        # The M suffix should be parsed from the regex
        assert len(decls) == 1
        assert decls[0].perm_bytes == 256 * 1024**2


# ===========================================================================
# _extract_database_from_ddl
# ===========================================================================


class TestExtractDatabaseFromDdl:
    """Unit tests for _extract_database_from_ddl."""

    def test_qualified_table(self):
        """Database name is extracted from a qualified CREATE TABLE."""
        content = "CREATE MULTISET TABLE MyDb.MyTable, NO FALLBACK (col1 INTEGER);"
        assert _extract_database_from_ddl(content) == "MyDb"

    def test_qualified_view(self):
        """Database name is extracted from a qualified REPLACE VIEW."""
        content = "REPLACE VIEW MyDb.MyView AS SELECT 1 AS Col1;"
        assert _extract_database_from_ddl(content) == "MyDb"

    def test_quoted_names(self):
        """Quoted database and object names are handled."""
        content = 'CREATE TABLE "MyDb"."MyTable" (col1 INTEGER);'
        assert _extract_database_from_ddl(content) == "MyDb"

    def test_unqualified_returns_none(self):
        """Unqualified object names return None."""
        content = "CREATE TABLE MyTable (col1 INTEGER);"
        assert _extract_database_from_ddl(content) is None


# ===========================================================================
# _format_bytes
# ===========================================================================


class TestFormatBytes:
    """Unit tests for _format_bytes."""

    def test_bytes(self):
        assert _format_bytes(512) == "512.0 B"

    def test_kilobytes(self):
        assert _format_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _format_bytes(5 * 1024**2) == "5.0 MB"

    def test_gigabytes(self):
        assert _format_bytes(3 * 1024**3) == "3.0 GB"


# ===========================================================================
# analyse_perm_space — integration tests using tmp_path
# ===========================================================================


class TestAnalysePermSpace:
    """Integration tests for analyse_perm_space using a temporary payload tree."""

    # ------------------------------------------------------------------
    # Helper: PERM floor constants (must match perm_analyser.py values)
    # ------------------------------------------------------------------
    FLOOR_TABLE = 512 * 1024
    FLOOR_PROCEDURE = 128 * 1024

    def test_ok_sufficient_perm(self, tmp_path):
        """Status is OK when declared PERM exceeds estimated floor by >20%."""
        # 500 MB PERM, one table (512 KB floor) → well above threshold
        _write(
            tmp_path,
            "MyDb/pre-requisites/databases/MyDb.db",
            "CREATE DATABASE MyDb AS PERM = 500M;",
        )
        _write(
            tmp_path,
            "MyDb/DDL/tables/Customers.tbl",
            "CREATE MULTISET TABLE MyDb.Customers, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "MyDb")
        assert finding.status == "OK"
        assert finding.estimated_floor == self.FLOOR_TABLE
        assert result.has_insufficient is False

    def test_insufficient_perm(self, tmp_path):
        """Status is INSUFFICIENT when estimated floor exceeds declared PERM."""
        # 100 KB PERM, one table (512 KB floor) → insufficient
        _write(
            tmp_path,
            "SmallDb/pre-requisites/databases/SmallDb.db",
            "CREATE DATABASE SmallDb AS PERM = 100K;",
        )
        _write(
            tmp_path,
            "SmallDb/DDL/tables/Orders.tbl",
            "CREATE MULTISET TABLE SmallDb.Orders, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "SmallDb")
        assert finding.status == "INSUFFICIENT"
        assert result.has_insufficient is True

    def test_warning_headroom_below_20pct(self, tmp_path):
        """Status is WARNING when headroom is below 20% of declared PERM."""
        # 600 KB PERM, one table (512 KB floor) → 88 KB headroom ≈ 14.6%
        _write(
            tmp_path,
            "TightDb/pre-requisites/databases/TightDb.db",
            "CREATE DATABASE TightDb AS PERM = 600K;",
        )
        _write(
            tmp_path,
            "TightDb/DDL/tables/Products.tbl",
            "CREATE MULTISET TABLE TightDb.Products, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "TightDb")
        assert finding.status == "WARNING"

    def test_unknown_no_create_in_package(self, tmp_path):
        """Status is UNKNOWN when objects exist but no CREATE DATABASE is in the package."""
        # No .db file — database is assumed to already exist on the target
        _write(
            tmp_path,
            "ExistingDb/DDL/tables/Widget.tbl",
            "CREATE MULTISET TABLE ExistingDb.Widget, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "ExistingDb")
        assert finding.status == "UNKNOWN"
        assert finding.declared_perm is None

    def test_modify_updates_effective_perm(self, tmp_path):
        """A MODIFY DATABASE statement changes the effective PERM."""
        # CREATE 100M, MODIFY to 500M → effective should be 500M
        _write(
            tmp_path,
            "FlexDb/pre-requisites/databases/FlexDb.db",
            "CREATE DATABASE FlexDb AS PERM = 100M;\nMODIFY DATABASE FlexDb AS PERM = 500M;",
        )
        _write(
            tmp_path,
            "FlexDb/DDL/tables/Logs.tbl",
            "CREATE MULTISET TABLE FlexDb.Logs, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "FlexDb")
        assert finding.effective_perm == 500 * 1024**2
        assert finding.status == "OK"

    def test_no_space_consuming_objects(self, tmp_path):
        """A database with only views/macros has OK status and zero floor."""
        _write(
            tmp_path,
            "MetaDb/pre-requisites/databases/MetaDb.db",
            "CREATE DATABASE MetaDb AS PERM = 10M;",
        )
        # Only a view — not space-consuming
        _write(
            tmp_path,
            "MetaDb/DDL/views/SummaryView.viw",
            "REPLACE VIEW MetaDb.SummaryView AS SELECT 1 AS Col1;",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "MetaDb")
        assert finding.estimated_floor == 0
        assert finding.status == "OK"

    def test_procedure_counted_as_space_consuming(self, tmp_path):
        """Stored procedures (.spl) are counted and add to the estimated floor."""
        _write(
            tmp_path,
            "ProcDb/pre-requisites/databases/ProcDb.db",
            "CREATE DATABASE ProcDb AS PERM = 50M;",
        )
        _write(
            tmp_path,
            "ProcDb/DDL/procedures/MyProc.spl",
            "REPLACE PROCEDURE ProcDb.MyProc () BEGIN END;",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "ProcDb")
        assert finding.estimated_floor == self.FLOOR_PROCEDURE
        assert "PROCEDURE" in finding.object_count

    def test_multiple_tables_accumulate_floor(self, tmp_path):
        """Multiple tables accumulate their individual floors."""
        _write(
            tmp_path,
            "MultiDb/pre-requisites/databases/MultiDb.db",
            "CREATE DATABASE MultiDb AS PERM = 100M;",
        )
        for i in range(3):
            _write(
                tmp_path,
                f"MultiDb/DDL/tables/Table{i}.tbl",
                f"CREATE MULTISET TABLE MultiDb.Table{i}, NO FALLBACK (Id INTEGER);",
            )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "MultiDb")
        assert finding.estimated_floor == 3 * self.FLOOR_TABLE
        assert finding.object_count.get("TABLE") == 3

    def test_perm_comment_not_extracted(self, tmp_path):
        """PERM values inside comments are not parsed as live declarations."""
        # The comment contains PERM = 9999G — should not affect declared_perm
        _write(
            tmp_path,
            "CommentDb/pre-requisites/databases/CommentDb.db",
            "-- PERM = 9999G  (old value, do not use)\n"
            "CREATE DATABASE CommentDb AS PERM = 10M;",
        )
        _write(
            tmp_path,
            "CommentDb/DDL/tables/T.tbl",
            "CREATE MULTISET TABLE CommentDb.T, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        finding = next(f for f in result.findings if f.database_name == "CommentDb")
        # Effective PERM should be 10M, not 9999G
        assert finding.declared_perm == 10 * 1024**2

    def test_result_to_dict_serialisable(self, tmp_path):
        """PermAnalysisResult.to_dict() produces a JSON-serialisable structure."""
        import json

        _write(
            tmp_path,
            "SerialDb/pre-requisites/databases/SerialDb.db",
            "CREATE DATABASE SerialDb AS PERM = 50M;",
        )
        _write(
            tmp_path,
            "SerialDb/DDL/tables/T.tbl",
            "CREATE MULTISET TABLE SerialDb.T, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        # Must not raise
        as_dict = result.to_dict()
        json_str = json.dumps(as_dict)
        assert "findings" in json_str
        assert "SerialDb" in json_str

    def test_directory_inference_when_no_qualified_name(self, tmp_path):
        """Database name is inferred from directory when DDL has no qualified name."""
        _write(
            tmp_path,
            "InferDb/pre-requisites/databases/InferDb.db",
            "CREATE DATABASE InferDb AS PERM = 50M;",
        )
        # Table DDL uses unqualified name — database must be inferred from path
        _write(
            tmp_path,
            "InferDb/DDL/tables/Unqualified.tbl",
            "CREATE MULTISET TABLE Unqualified, NO FALLBACK (Id INTEGER);",
        )

        result = analyse_perm_space(str(tmp_path))
        # The object should still be attributed to InferDb via directory inference
        infer_finding = next(
            (f for f in result.findings if f.database_name == "InferDb"), None
        )
        assert infer_finding is not None
        assert infer_finding.estimated_floor >= self.FLOOR_TABLE
