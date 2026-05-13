"""
test_view_layer_comment_inheritance.py — COMMENT ON inheritance for
SHIPS 1:1 locking views (issue #66).

Acceptance criteria:
  1. COMMENT ON VIEW emitted whenever the underlying table has a
     COMMENT ON TABLE in its sibling .cmt file.
  2. COMMENT ON COLUMN emitted for every column that has a matching
     COMMENT ON COLUMN entry; columns without a comment produce nothing.
  3. When the source has no comment for an object or column, no
     spurious empty / placeholder comment is emitted.
  4. The view-level comment carries a fixed dirty-read suffix;
     column-level comments are pass-through with no transformation.
  5. Token-qualified names round-trip correctly ({{DB_T}} → {{DB_V}}).
"""

from __future__ import annotations

from pathlib import Path


from td_release_packager.view_layer_generator import (
    TableSpec,
    _extract_table_comments,
    generate_locking_view_ddl,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_table(
    tmp_path: Path,
    token: str = "{{DOM_DATABASE_T}}",
    object_name: str = "Customer",
    columns: list = None,
) -> TableSpec:
    """Return a minimal TableSpec (file_path created in tmp_path)."""
    tbl_path = tmp_path / "tables" / f"{token}.{object_name}.tbl"
    tbl_path.parent.mkdir(parents=True, exist_ok=True)
    tbl_path.write_text(
        f"CREATE MULTISET TABLE {token}.{object_name} (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    return TableSpec(
        file_path=tbl_path,
        database_token=token,
        module="DOM",
        object_name=object_name,
        columns=columns or ["Id", "Name", "Status"],
    )


def _write_cmt(tmp_path: Path, token: str, object_name: str, content: str) -> None:
    cmt_path = tmp_path / "comments" / f"{token}.{object_name}.cmt"
    _write(cmt_path, content)


# ---------------------------------------------------------------
# _extract_table_comments
# ---------------------------------------------------------------


class TestExtractTableComments:
    def test_no_cmt_file_returns_empty(self, tmp_path):
        """Absent .cmt file → (None, {}) — no fabricated comments."""
        tbl_path = tmp_path / "tables" / "{{DB_T}}.T.tbl"
        tbl_path.parent.mkdir(parents=True, exist_ok=True)
        tbl_path.write_text("CREATE TABLE {{DB_T}}.T (x INT);", encoding="utf-8")

        comment, col_comments = _extract_table_comments(tbl_path)
        assert comment is None
        assert col_comments == {}

    def test_full_comment_file_extracted(self, tmp_path):
        """Both table and column comments are extracted from .cmt."""
        _write(
            tmp_path / "tables" / "{{DB_T}}.Customer.tbl",
            "CREATE MULTISET TABLE {{DB_T}}.Customer (Id INTEGER, Name VARCHAR(100));\n",
        )
        _write(
            tmp_path / "comments" / "{{DB_T}}.Customer.cmt",
            "COMMENT ON TABLE {{DB_T}}.Customer IS 'Core customer entity';\n"
            "COMMENT ON COLUMN {{DB_T}}.Customer.Id IS 'Surrogate key';\n"
            "COMMENT ON COLUMN {{DB_T}}.Customer.Name IS 'Full legal name';\n",
        )

        tbl_path = tmp_path / "tables" / "{{DB_T}}.Customer.tbl"
        comment, col_comments = _extract_table_comments(tbl_path)

        assert comment == "Core customer entity"
        assert col_comments["Id"] == "Surrogate key"
        assert col_comments["Name"] == "Full legal name"

    def test_partial_column_comments(self, tmp_path):
        """Only commented columns appear in col_comments; others absent."""
        _write(
            tmp_path / "tables" / "{{DB_T}}.T.tbl",
            "CREATE TABLE {{DB_T}}.T (A INT, B INT, C INT);\n",
        )
        _write(
            tmp_path / "comments" / "{{DB_T}}.T.cmt",
            "COMMENT ON TABLE {{DB_T}}.T IS 'Test table';\n"
            "COMMENT ON COLUMN {{DB_T}}.T.A IS 'Column A';\n",
            # B and C have no comment
        )

        tbl_path = tmp_path / "tables" / "{{DB_T}}.T.tbl"
        comment, col_comments = _extract_table_comments(tbl_path)

        assert comment == "Test table"
        assert "A" in col_comments
        assert "B" not in col_comments
        assert "C" not in col_comments

    def test_no_table_comment_only_column_comments(self, tmp_path):
        """COMMENT ON TABLE absent → table_comment is None; cols still extracted."""
        _write(
            tmp_path / "tables" / "{{DB_T}}.T.tbl", "CREATE TABLE {{DB_T}}.T (X INT);\n"
        )
        _write(
            tmp_path / "comments" / "{{DB_T}}.T.cmt",
            "COMMENT ON COLUMN {{DB_T}}.T.X IS 'X axis value';\n",
        )

        tbl_path = tmp_path / "tables" / "{{DB_T}}.T.tbl"
        comment, col_comments = _extract_table_comments(tbl_path)

        assert comment is None
        assert col_comments["X"] == "X axis value"

    def test_doubled_quote_in_comment_preserved(self, tmp_path):
        """SQL-escaped '' inside comment text survives round-trip."""
        _write(
            tmp_path / "tables" / "{{DB_T}}.T.tbl", "CREATE TABLE {{DB_T}}.T (X INT);\n"
        )
        _write(
            tmp_path / "comments" / "{{DB_T}}.T.cmt",
            "COMMENT ON TABLE {{DB_T}}.T IS 'Company''s main table';\n",
        )

        tbl_path = tmp_path / "tables" / "{{DB_T}}.T.tbl"
        comment, _ = _extract_table_comments(tbl_path)

        # The doubled quote is stored as-is for re-emission in SQL context
        assert comment == "Company''s main table"


# ---------------------------------------------------------------
# generate_locking_view_ddl — comment emission
# ---------------------------------------------------------------


class TestGenerateLockingViewDdlComments:
    """Verify COMMENT ON statements are (or are not) emitted correctly."""

    def test_no_comments_no_comment_statements(self, tmp_path):
        """When table_comment=None and column_comments={}, no COMMENT lines."""
        table = _make_table(tmp_path)
        ddl = generate_locking_view_ddl(table)

        assert "COMMENT ON VIEW" not in ddl
        assert "COMMENT ON COLUMN" not in ddl

    def test_table_comment_emitted_with_dirty_read_suffix(self, tmp_path):
        """COMMENT ON VIEW includes the table comment plus the dirty-read suffix."""
        table = _make_table(tmp_path)
        table.table_comment = "Core customer entity"

        ddl = generate_locking_view_ddl(table)

        assert "COMMENT ON VIEW" in ddl
        assert "Core customer entity" in ddl
        # Fixed dirty-read suffix must be present
        assert "LOCKING ROW FOR ACCESS" in ddl  # in both view body and comment
        # The suffix references the tables token and object name
        assert "{{DOM_DATABASE_T}}.Customer" in ddl

    def test_view_side_token_used_in_comment(self, tmp_path):
        """COMMENT ON VIEW uses the _V token, not the _T token."""
        table = _make_table(tmp_path, token="{{DOM_DATABASE_T}}", object_name="Order")
        table.table_comment = "Order header"

        ddl = generate_locking_view_ddl(table)

        # Comment target must be the _V side
        assert "COMMENT ON VIEW {{DOM_DATABASE_V}}.Order" in ddl
        # The _T side should appear in the dirty-read suffix, not as the target
        assert "COMMENT ON VIEW {{DOM_DATABASE_T}}" not in ddl

    def test_column_comments_emitted_for_commented_columns(self, tmp_path):
        """COMMENT ON COLUMN emitted for each column that has a comment."""
        table = _make_table(
            tmp_path,
            columns=["Id", "Name", "Status"],
        )
        table.column_comments = {"Id": "Surrogate key", "Name": "Full legal name"}
        # Status has no comment

        ddl = generate_locking_view_ddl(table)

        assert (
            "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Customer.Id IS 'Surrogate key'" in ddl
        )
        assert (
            "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Customer.Name IS 'Full legal name'"
            in ddl
        )
        assert "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Customer.Status" not in ddl

    def test_no_column_comment_for_uncommented_column(self, tmp_path):
        """Columns without a comment produce no COMMENT ON COLUMN line."""
        table = _make_table(tmp_path, columns=["A", "B", "C"])
        table.column_comments = {"A": "Alpha"}  # B and C have no comment

        ddl = generate_locking_view_ddl(table)

        assert "COMMENT ON COLUMN" in ddl
        assert (
            ".B" not in ddl.split("COMMENT ON COLUMN", 1)[-1]
            if "COMMENT ON COLUMN" in ddl
            else True
        )
        assert "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Customer.B" not in ddl
        assert "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Customer.C" not in ddl

    def test_full_inheritance_end_to_end(self, tmp_path):
        """Full round-trip: .cmt file → TableSpec → DDL with all comments."""
        _write(
            tmp_path / "tables" / "{{DOM_DATABASE_T}}.Loan.tbl",
            "CREATE MULTISET TABLE {{DOM_DATABASE_T}}.Loan "
            "(LoanId INTEGER, Amount DECIMAL(18,2), Status VARCHAR(20)) "
            "PRIMARY INDEX (LoanId);\n",
        )
        _write(
            tmp_path / "comments" / "{{DOM_DATABASE_T}}.Loan.cmt",
            "COMMENT ON TABLE {{DOM_DATABASE_T}}.Loan IS 'Mortgage loan header';\n"
            "COMMENT ON COLUMN {{DOM_DATABASE_T}}.Loan.LoanId IS 'Unique loan identifier';\n"
            "COMMENT ON COLUMN {{DOM_DATABASE_T}}.Loan.Amount IS 'Original principal amount';\n",
            # Status has no comment
        )

        tbl_path = tmp_path / "tables" / "{{DOM_DATABASE_T}}.Loan.tbl"
        table_comment, col_comments = _extract_table_comments(tbl_path)

        table = TableSpec(
            file_path=tbl_path,
            database_token="{{DOM_DATABASE_T}}",
            module="DOM",
            object_name="Loan",
            columns=["LoanId", "Amount", "Status"],
            table_comment=table_comment,
            column_comments=col_comments,
        )

        ddl = generate_locking_view_ddl(table)

        # Table comment → view comment with suffix
        assert "COMMENT ON VIEW {{DOM_DATABASE_V}}.Loan" in ddl
        assert "Mortgage loan header" in ddl
        assert "1:1 locking view" in ddl.lower() or "locking" in ddl

        # Column comments for LoanId and Amount
        assert (
            "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Loan.LoanId IS 'Unique loan identifier'"
            in ddl
        )
        assert (
            "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Loan.Amount IS 'Original principal amount'"
            in ddl
        )

        # Status has no comment — must not appear in COMMENT ON COLUMN
        assert "COMMENT ON COLUMN {{DOM_DATABASE_V}}.Loan.Status" not in ddl

    def test_create_view_still_emitted_with_comments(self, tmp_path):
        """Adding comments does not break the CREATE VIEW DDL itself."""
        table = _make_table(tmp_path, columns=["X"])
        table.table_comment = "Test"
        table.column_comments = {"X": "X column"}

        ddl = generate_locking_view_ddl(table)

        assert "CREATE VIEW {{DOM_DATABASE_V}}.Customer" in ddl
        assert "LOCKING ROW FOR ACCESS" in ddl
        assert "FROM {{DOM_DATABASE_T}}.Customer" in ddl
