"""
test_new_anchors.py — Tests for the 8 new structural anchors
added to analyser.py (anchors 11–18, bringing total to 19).

These tests validate reference extraction via _scan_references()
for each new anchor pattern. They can be merged into the existing
test_analyser.py or run standalone.

Anchor coverage:
    11. COLLECT [SUMMARY] STATISTICS ... ON db.table
    12. CALL db.procedure
    13. EXEC[UTE] db.macro  (excludes EXECUTE IMMEDIATE)
    14. LOCKING [TABLE] db.table FOR {mode}
    15. CREATE [UNIQUE] [JOIN|HASH] INDEX ... ON db.table
    16. RENAME TABLE db.old TO|AS db.new
    17. DROP TABLE|VIEW|... db.name
    18. COMMENT ON TABLE|COLUMN db.name
"""

# -- Import the function under test --
# Adjust the import path to match your project structure.
from td_release_packager.analyser import _scan_references


# -- Helper: known databases used across all tests --
KNOWN_DBS = {"A_D01_STD", "A_D01_VIW", "A_D01_SEM"}


def _refs(ddl: str, obj_type: str = "PROCEDURE", own: str = "A_D01_SEM.self"):
    """
    Convenience wrapper around _scan_references.

    Returns:
        Tuple of (internal upper-cased set, external upper-cased set).
    """
    internal, external = _scan_references(
        ddl,
        obj_type,
        own,
        KNOWN_DBS,
    )
    return (
        {r.upper() for r in internal},
        {r.upper() for r in external},
    )


# =================================================================
# 11. COLLECT [SUMMARY] STATISTICS ... ON
# =================================================================


class TestCollectStatisticsOn:
    """Tests for _COLLECT_STATS_ON_RE."""

    def test_single_column_stats(self):
        """Single COLLECT STATISTICS with one COLUMN clause."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Stats()
        BEGIN
            COLLECT STATISTICS COLUMN (Cust_Id)
            ON A_D01_STD.Customer;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal

    def test_multi_column_stats(self):
        """COLLECT STATISTICS with multiple COLUMN clauses."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Stats()
        BEGIN
            COLLECT STATISTICS
                COLUMN (Cust_Id)
               ,COLUMN (Cust_Status)
            ON A_D01_STD.Customer;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal

    def test_multiple_collect_blocks(self):
        """Multiple COLLECT STATISTICS blocks — each ON captured."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_RefreshStats()
        BEGIN
            COLLECT STATISTICS COLUMN (Cust_Id) ON A_D01_STD.Customer;
            COLLECT STATISTICS COLUMN (Prod_Id) ON A_D01_STD.Product;
            COLLECT STATISTICS COLUMN (Order_Id) ON A_D01_STD.Orders;
            COLLECT STATISTICS COLUMN (Order_Id) ON A_D01_STD.OrderLine;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal
        assert "A_D01_STD.PRODUCT" in internal
        assert "A_D01_STD.ORDERS" in internal
        assert "A_D01_STD.ORDERLINE" in internal

    def test_collect_summary_statistics(self):
        """COLLECT SUMMARY STATISTICS variant."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Stats()
        BEGIN
            COLLECT SUMMARY STATISTICS ON A_D01_STD.Customer;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal

    def test_collect_stats_using_sample(self):
        """COLLECT STATISTICS USING SAMPLE variant."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Stats()
        BEGIN
            COLLECT STATISTICS USING SAMPLE 10 PERCENT
                COLUMN (Cust_Id) ON A_D01_STD.Customer;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal

    def test_collect_stats_index_clause(self):
        """COLLECT STATISTICS INDEX clause."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Stats()
        BEGIN
            COLLECT STATISTICS INDEX (idx_CustName)
            ON A_D01_STD.Customer;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal


# =================================================================
# 12. CALL (procedure invocation)
# =================================================================


class TestCall:
    """Tests for _CALL_RE."""

    def test_call_with_parens(self):
        """CALL db.procedure() — empty args."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Wrapper()
        BEGIN
            CALL A_D01_SEM.sp_RefreshStats();
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_Wrapper")
        assert "A_D01_SEM.SP_REFRESHSTATS" in internal

    def test_call_with_args(self):
        """CALL db.procedure(arg1, arg2)."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Wrapper()
        BEGIN
            CALL A_D01_SEM.sp_Process(100, 'ABC');
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_Wrapper")
        assert "A_D01_SEM.SP_PROCESS" in internal

    def test_call_no_parens(self):
        """CALL db.procedure — no parentheses (valid Teradata)."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Wrapper()
        BEGIN
            CALL A_D01_SEM.sp_Cleanup;
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_Wrapper")
        assert "A_D01_SEM.SP_CLEANUP" in internal

    def test_call_self_excluded(self):
        """CALL to self is excluded (no self-dependency)."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Recursive()
        BEGIN
            CALL A_D01_SEM.sp_Recursive();
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_Recursive")
        assert "A_D01_SEM.SP_RECURSIVE" not in internal


# =================================================================
# 13. EXEC / EXECUTE (macro invocation)
# =================================================================


class TestExec:
    """Tests for _EXEC_RE."""

    def test_exec_macro(self):
        """EXEC db.macro."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_RunReport()
        BEGIN
            EXEC A_D01_SEM.mc_CustomerReport;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_SEM.MC_CUSTOMERREPORT" in internal

    def test_execute_macro(self):
        """EXECUTE db.macro (full keyword)."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_RunReport()
        BEGIN
            EXECUTE A_D01_SEM.mc_ProductSales;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_SEM.MC_PRODUCTSALES" in internal

    def test_execute_immediate_excluded(self):
        """EXECUTE IMMEDIATE is dynamic SQL — not a static ref."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Dynamic()
        BEGIN
            EXECUTE IMMEDIATE 'SELECT 1';
        END
        """
        internal, _ = _refs(ddl)
        # Should NOT capture 'SELECT' or any other token
        assert len(internal) == 0


# =================================================================
# 14. LOCKING ... FOR
# =================================================================


class TestLocking:
    """Tests for _LOCKING_RE."""

    def test_locking_for_access(self):
        """LOCKING db.table FOR ACCESS."""
        ddl = """
        REPLACE VIEW A_D01_VIW.Customer AS
        LOCKING A_D01_STD.Customer FOR ACCESS
        SELECT * FROM A_D01_STD.Customer;
        """
        internal, _ = _refs(ddl, obj_type="VIEW", own="A_D01_VIW.Customer")
        assert "A_D01_STD.CUSTOMER" in internal

    def test_locking_table_keyword(self):
        """LOCKING TABLE db.table FOR WRITE."""
        ddl = """
        REPLACE MACRO A_D01_SEM.mc_Update AS (
        LOCKING TABLE A_D01_STD.Orders FOR WRITE
        UPDATE A_D01_STD.Orders SET Order_Status = 'C';
        );
        """
        internal, _ = _refs(ddl, obj_type="MACRO", own="A_D01_SEM.mc_Update")
        assert "A_D01_STD.ORDERS" in internal

    def test_locking_different_table_than_query(self):
        """LOCKING on parent, query on child — both captured."""
        ddl = """
        REPLACE VIEW A_D01_VIW.OrderView AS
        LOCKING A_D01_STD.Customer FOR ACCESS
        SELECT o.* FROM A_D01_STD.Orders o;
        """
        internal, _ = _refs(ddl, obj_type="VIEW", own="A_D01_VIW.OrderView")
        assert "A_D01_STD.CUSTOMER" in internal
        assert "A_D01_STD.ORDERS" in internal


# =================================================================
# 15. CREATE INDEX ON parent table
# =================================================================


class TestIndexOn:
    """Tests for _INDEX_ON_RE."""

    def test_create_join_index_on(self):
        """CREATE JOIN INDEX ... ON db.table."""
        ddl = """
        CREATE JOIN INDEX A_D01_STD.ji_CustOrders AS
        SELECT c.Cust_Id, o.Order_Id
        FROM A_D01_STD.Customer c
        INNER JOIN A_D01_STD.Orders o
        ON c.Cust_Id = o.Cust_Id
        PRIMARY INDEX (Cust_Id)
        ON A_D01_STD.Customer;
        """
        internal, _ = _refs(ddl, obj_type="JOIN_INDEX", own="A_D01_STD.ji_CustOrders")
        assert "A_D01_STD.CUSTOMER" in internal

    def test_create_hash_index_on(self):
        """CREATE HASH INDEX ... ON db.table."""
        ddl = """
        CREATE HASH INDEX A_D01_STD.hi_OrderDt
        (Order_Dt) ON A_D01_STD.Orders
        BY (Order_Dt);
        """
        internal, _ = _refs(ddl, obj_type="HASH_INDEX", own="A_D01_STD.hi_OrderDt")
        assert "A_D01_STD.ORDERS" in internal

    def test_create_unique_index_on(self):
        """CREATE UNIQUE INDEX ... ON db.table."""
        ddl = """
        CREATE UNIQUE INDEX idx_CustEmail (Email)
        ON A_D01_STD.Customer;
        """
        internal, _ = _refs(ddl, obj_type="INDEX", own="A_D01_STD.idx_CustEmail")
        assert "A_D01_STD.CUSTOMER" in internal

    def test_create_secondary_index_on(self):
        """CREATE INDEX (no UNIQUE) ... ON db.table."""
        ddl = """
        CREATE INDEX idx_OrderStatus (Order_Status)
        ON A_D01_STD.Orders;
        """
        internal, _ = _refs(ddl, obj_type="INDEX", own="A_D01_STD.idx_OrderStatus")
        assert "A_D01_STD.ORDERS" in internal


# =================================================================
# 16. RENAME TABLE
# =================================================================


class TestRenameTable:
    """Tests for _RENAME_TABLE_RE."""

    def test_rename_table_to(self):
        """RENAME TABLE db.old TO db.new — both captured."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Migrate()
        BEGIN
            RENAME TABLE A_D01_STD.Customer TO A_D01_STD.Customer_Archive;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.CUSTOMER" in internal
        assert "A_D01_STD.CUSTOMER_ARCHIVE" in internal

    def test_rename_table_as(self):
        """RENAME TABLE db.old AS db.new — AS variant."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Swap()
        BEGIN
            RENAME TABLE A_D01_STD.Orders AS A_D01_STD.Orders_Backup;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.ORDERS" in internal
        assert "A_D01_STD.ORDERS_BACKUP" in internal


# =================================================================
# 17. DROP object (in SPL bodies)
# =================================================================


class TestDropObject:
    """Tests for _DROP_OBJECT_RE."""

    def test_drop_table(self):
        """DROP TABLE db.name."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Cleanup()
        BEGIN
            DROP TABLE A_D01_STD.TempStage;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.TEMPSTAGE" in internal

    def test_drop_view(self):
        """DROP VIEW db.name."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Cleanup()
        BEGIN
            DROP VIEW A_D01_VIW.OldView;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_VIW.OLDVIEW" in internal

    def test_drop_join_index(self):
        """DROP JOIN INDEX db.name."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Cleanup()
        BEGIN
            DROP JOIN INDEX A_D01_STD.ji_CustOrders;
        END
        """
        internal, _ = _refs(ddl)
        assert "A_D01_STD.JI_CUSTORDERS" in internal

    def test_drop_procedure(self):
        """DROP PROCEDURE db.name."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Cleanup()
        BEGIN
            DROP PROCEDURE A_D01_SEM.sp_OldProc;
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_Cleanup")
        assert "A_D01_SEM.SP_OLDPROC" in internal


# =================================================================
# 18. COMMENT ON
# =================================================================


class TestCommentOn:
    """Tests for _COMMENT_ON_RE."""

    def test_comment_on_table(self):
        """COMMENT ON TABLE db.table IS '...'."""
        ddl = """
        COMMENT ON TABLE A_D01_STD.Customer IS 'Customer master';
        """
        # COMMENT ON files don't have a standard object type —
        # scan as generic SQL.
        internal, _ = _refs(ddl, obj_type="SQL", own="COMMENT.A_D01_STD.Customer")
        assert "A_D01_STD.CUSTOMER" in internal

    def test_comment_on_column(self):
        """COMMENT ON COLUMN db.table.col — captures db.table."""
        ddl = """
        COMMENT ON COLUMN A_D01_STD.Customer.Cust_Name IS 'Name';
        """
        internal, _ = _refs(
            ddl, obj_type="SQL", own="COMMENT.A_D01_STD.Customer.Cust_Name"
        )
        assert "A_D01_STD.CUSTOMER" in internal


# =================================================================
# Edge cases and interaction tests
# =================================================================


class TestAnchorInteractions:
    """Cross-anchor and edge case tests."""

    def test_procedure_with_multiple_anchor_types(self):
        """
        Procedure using COLLECT STATS, CALL, FROM, and INSERT.

        All references should be captured from their respective
        structural anchors.
        """
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_FullRefresh()
        BEGIN
            COLLECT STATISTICS COLUMN (Cust_Id) ON A_D01_STD.Customer;

            INSERT INTO A_D01_STD.AuditLog
            SELECT 'REFRESH', CURRENT_TIMESTAMP
            FROM A_D01_STD.Orders;

            CALL A_D01_SEM.sp_RefreshStats();
        END
        """
        internal, _ = _refs(ddl, own="A_D01_SEM.sp_FullRefresh")
        assert "A_D01_STD.CUSTOMER" in internal  # COLLECT STATS ON
        assert "A_D01_STD.AUDITLOG" in internal  # INSERT INTO
        assert "A_D01_STD.ORDERS" in internal  # FROM
        assert "A_D01_SEM.SP_REFRESHSTATS" in internal  # CALL

    def test_tokenised_database_names(self):
        """{{TOKEN}} placeholders in database names."""
        known = {"{{STD_DATABASE}}", "{{SEM_DATABASE}}"}
        ddl = """
        REPLACE PROCEDURE {{SEM_DATABASE}}.sp_Stats()
        BEGIN
            COLLECT STATISTICS COLUMN (Cust_Id)
            ON {{STD_DATABASE}}.Customer;
        END
        """
        internal, _ = _scan_references(
            ddl,
            "PROCEDURE",
            "{{SEM_DATABASE}}.sp_Stats",
            known,
        )
        internal_upper = {r.upper() for r in internal}
        assert "{{STD_DATABASE}}.CUSTOMER" in internal_upper

    def test_system_db_refs_excluded(self):
        """References to DBC, SYSLIB, etc. are never dependencies."""
        ddl = """
        REPLACE PROCEDURE A_D01_SEM.sp_Info()
        BEGIN
            SELECT * FROM DBC.TablesV;
            CALL SYSLIB.MonitorVproc();
        END
        """
        internal, external = _refs(ddl)
        all_refs = internal | external
        assert not any("DBC." in r for r in all_refs)
        assert not any("SYSLIB." in r for r in all_refs)

    def test_collect_stats_does_not_match_join_on(self):
        """
        JOIN ... ON should not trigger the COLLECT STATS anchor.

        The COLLECT STATS regex requires COLLECT STATISTICS
        before the ON keyword. A JOIN ... ON clause does not
        have this prefix and should only be caught by _JOIN_RE.
        """
        ddl = """
        REPLACE VIEW A_D01_VIW.CustOrders AS
        SELECT c.Cust_Id, o.Order_Id
        FROM A_D01_STD.Customer c
        INNER JOIN A_D01_STD.Orders o
        ON c.Cust_Id = o.Cust_Id;
        """
        internal, _ = _refs(ddl, obj_type="VIEW", own="A_D01_VIW.CustOrders")
        # Both should be captured via FROM and JOIN, not COLLECT STATS
        assert "A_D01_STD.CUSTOMER" in internal
        assert "A_D01_STD.ORDERS" in internal
