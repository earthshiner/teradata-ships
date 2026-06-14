"""Tests for the SqlReferenceExtractor abstraction (#234, ADR 0015, Phase 1)."""

from __future__ import annotations

import pytest

from td_release_packager.infer_grants import strip_sql_comments
from td_release_packager.sql_reference_extractor import (
    PRIV_DELETE,
    PRIV_EXEC,
    PRIV_EXEC_PROC,
    PRIV_INSERT,
    PRIV_SELECT,
    PRIV_UPDATE,
    ReferencedObject,
    RegexSqlReferenceExtractor,
    SqlReferenceExtractor,
    StatementOwner,
    default_extractor,
)


@pytest.fixture
def regex() -> SqlReferenceExtractor:
    return RegexSqlReferenceExtractor()


def _strip(sql: str) -> str:
    """Comment-strip per the contract the regex extractor expects."""
    return strip_sql_comments(sql)


# ---------------------------------------------------------------
# extract_statement_owner
# ---------------------------------------------------------------


class TestExtractStatementOwner:
    def test_create_view_token(self, regex):
        sql = _strip("CREATE VIEW {{DOM_V}}.Customer_V AS SELECT 1;")
        owner = regex.extract_statement_owner(sql)
        assert owner == StatementOwner(
            database="{{DOM_V}}",
            object_name="Customer_V",
            object_type="VIEW",
        )

    def test_create_procedure_literal(self, regex):
        sql = _strip("CREATE PROCEDURE PROC_DB.RefreshFacts () BEGIN SELECT 1; END;")
        owner = regex.extract_statement_owner(sql)
        assert owner is not None
        assert owner.object_type == "PROCEDURE"
        assert owner.database == "PROC_DB"
        assert owner.object_name == "RefreshFacts"

    def test_replace_macro(self, regex):
        sql = _strip("REPLACE MACRO MAC_DB.RunIt AS (SELECT 1);")
        owner = regex.extract_statement_owner(sql)
        assert owner is not None
        assert owner.object_type == "MACRO"

    def test_compound_table_types_normalised(self, regex):
        for variant in (
            "CREATE SET TABLE TBL_DB.T1 (id INT);",
            "CREATE MULTISET TABLE TBL_DB.T1 (id INT);",
            "CREATE VOLATILE TABLE TBL_DB.T1 (id INT);",
        ):
            owner = regex.extract_statement_owner(_strip(variant))
            assert owner is not None, variant
            assert owner.object_type == "TABLE", variant

    def test_no_create_returns_none(self, regex):
        owner = regex.extract_statement_owner(_strip("SELECT 1;"))
        assert owner is None


# ---------------------------------------------------------------
# extract_read_sources
# ---------------------------------------------------------------


class TestExtractReadSources:
    def test_simple_view_body(self, regex):
        sql = _strip("SELECT * FROM {{DOM_T}}.Customer")
        sources = regex.extract_read_sources(sql)
        assert ReferencedObject("{{DOM_T}}", "Customer") in sources

    def test_join_branches(self, regex):
        sql = _strip(
            """
            SELECT a.id
            FROM {{DOM_T}}.Customer a
            INNER JOIN {{REF_T}}.Address b ON a.id = b.id
            LEFT JOIN {{REF_T}}.Phone p ON a.id = p.id
            """
        )
        sources = regex.extract_read_sources(sql)
        names = {(s.database, s.object_name) for s in sources}
        assert ("{{DOM_T}}", "Customer") in names
        assert ("{{REF_T}}", "Address") in names
        assert ("{{REF_T}}", "Phone") in names

    def test_excludes_cte_names(self, regex):
        sql = _strip(
            """
            WITH cte_recent AS (
                SELECT * FROM {{DOM_T}}.Orders WHERE order_dt > DATE
            )
            SELECT * FROM cte_recent JOIN {{REF_T}}.Customer c ON 1=1;
            """
        )
        sources = regex.extract_read_sources(sql)
        names = {s.database for s in sources}
        assert "{{DOM_T}}" in names
        assert "{{REF_T}}" in names
        # The CTE alias was never qualified, so it cannot appear, but
        # nothing called "cte_recent" should leak as a database.
        assert "cte_recent" not in names

    def test_excludes_derived_table_alias_srv_processsumbybusdate(self, regex):
        """The canonical regression case from ADR 0015.

        The derived table is aliased ``sRV_ProcessSumByBusDate``;
        inside its body the alias is referenced through itself in a
        CASE expression. The historical regex scanner mistook the
        alias for a database name and emitted
        ``GRANT SELECT ON sRV_ProcessSumByBusDate``. The fix added a
        balanced-paren derived-alias collector — the abstraction must
        keep that behaviour.
        """
        sql = _strip(
            """
            REPLACE VIEW {{DOM_V}}.ProcessStatus_V AS
            SELECT main.business_dt, main.status
            FROM (
                SELECT business_dt, status
                FROM {{REF_T}}.ProcessRun
                WHERE status IN (
                    SELECT Min(sRV_ProcessSumByBusDate.Process_State)
                    FROM (
                        SELECT business_dt, Process_State
                        FROM {{REF_T}}.ProcessSum
                    ) sRV_ProcessSumByBusDate
                )
            ) main;
            """
        )
        sources = regex.extract_read_sources(sql)
        databases = {s.database for s in sources}
        assert "sRV_ProcessSumByBusDate" not in databases
        # The legitimate refs survive.
        assert "{{REF_T}}" in databases

    def test_excludes_system_databases(self, regex):
        sql = _strip("SELECT * FROM DBC.TablesV;")
        sources = regex.extract_read_sources(sql)
        assert all(s.database.upper() != "DBC" for s in sources)


# ---------------------------------------------------------------
# extract_write_targets
# ---------------------------------------------------------------


class TestExtractWriteTargets:
    def test_insert_target(self, regex):
        sql = _strip("INSERT INTO {{TGT}}.Log VALUES (1);")
        targets = regex.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Log")] == frozenset({PRIV_INSERT})

    def test_update_target(self, regex):
        sql = _strip("UPDATE {{TGT}}.Stats SET cnt = cnt + 1;")
        targets = regex.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Stats")] == frozenset({PRIV_UPDATE})

    def test_delete_target(self, regex):
        sql = _strip("DELETE FROM {{TGT}}.Stale;")
        targets = regex.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Stale")] == frozenset({PRIV_DELETE})

    def test_merge_target_implies_insert_and_update(self, regex):
        sql = _strip(
            """
            MERGE INTO {{TGT}}.Customer t
            USING {{SRC}}.Updates s ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET name = s.name
            WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.name);
            """
        )
        targets = regex.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Customer")] == frozenset(
            {PRIV_INSERT, PRIV_UPDATE}
        )

    def test_excludes_system_databases(self, regex):
        sql = _strip("INSERT INTO DBC.UserStatsV VALUES (1);")
        targets = regex.extract_write_targets(sql)
        assert all(ref.database.upper() != "DBC" for ref in targets)


# ---------------------------------------------------------------
# extract_call_targets
# ---------------------------------------------------------------


class TestExtractCallTargets:
    def test_call_implies_execute_procedure(self, regex):
        sql = _strip("CALL {{PROC_DB}}.Refresh();")
        targets = regex.extract_call_targets(sql)
        assert targets[ReferencedObject("{{PROC_DB}}", "Refresh")] == frozenset(
            {PRIV_EXEC_PROC}
        )

    def test_exec_implies_execute(self, regex):
        sql = _strip("EXEC {{MAC_DB}}.RunIt;")
        targets = regex.extract_call_targets(sql)
        assert targets[ReferencedObject("{{MAC_DB}}", "RunIt")] == frozenset(
            {PRIV_EXEC}
        )

    def test_execute_alias_for_exec(self, regex):
        sql = _strip("EXECUTE {{MAC_DB}}.RunIt;")
        targets = regex.extract_call_targets(sql)
        assert ReferencedObject("{{MAC_DB}}", "RunIt") in targets


# ---------------------------------------------------------------
# default_extractor
# ---------------------------------------------------------------


class TestDefaultExtractor:
    def test_returns_a_sql_reference_extractor(self):
        assert isinstance(default_extractor(), SqlReferenceExtractor)

    def test_env_var_regex_forces_regex(self, monkeypatch):
        monkeypatch.setenv("SHIPS_SQL_EXTRACTOR", "regex")
        assert isinstance(default_extractor(), RegexSqlReferenceExtractor)

    def test_env_var_sqlglot_forces_sqlglot(self, monkeypatch):
        from td_release_packager.sql_reference_extractor_sqlglot import (
            SqlGlotSqlReferenceExtractor,
            is_available,
        )

        if not is_available():
            pytest.skip("sqlglot not installed")
        monkeypatch.setenv("SHIPS_SQL_EXTRACTOR", "sqlglot")
        assert isinstance(default_extractor(), SqlGlotSqlReferenceExtractor)

    def test_env_var_ast_alias_forces_sqlglot(self, monkeypatch):
        from td_release_packager.sql_reference_extractor_sqlglot import (
            SqlGlotSqlReferenceExtractor,
            is_available,
        )

        if not is_available():
            pytest.skip("sqlglot not installed")
        monkeypatch.setenv("SHIPS_SQL_EXTRACTOR", "ast")
        assert isinstance(default_extractor(), SqlGlotSqlReferenceExtractor)

    def test_auto_prefers_sqlglot_when_available(self, monkeypatch):
        from td_release_packager.sql_reference_extractor_sqlglot import (
            SqlGlotSqlReferenceExtractor,
            is_available,
        )

        if not is_available():
            pytest.skip("sqlglot not installed")
        monkeypatch.setenv("SHIPS_SQL_EXTRACTOR", "auto")
        assert isinstance(default_extractor(), SqlGlotSqlReferenceExtractor)

    def test_auto_falls_back_when_sqlglot_unavailable(self, monkeypatch):
        monkeypatch.setenv("SHIPS_SQL_EXTRACTOR", "auto")
        monkeypatch.setattr(
            "td_release_packager.sql_reference_extractor_sqlglot.is_available",
            lambda: False,
        )
        assert isinstance(default_extractor(), RegexSqlReferenceExtractor)


# ---------------------------------------------------------------
# analyse_file accepts the extractor for injection
# ---------------------------------------------------------------


class TestExtractAllReferences:
    """The default ``extract_all_references`` should be the union of
    reads, writes, and calls minus the statement owner (#234 Phase
    3b)."""

    def test_union_of_reads_writes_calls(self, regex):
        sql = _strip(
            """
            CREATE PROCEDURE {{PROC_DB}}.LoadFacts ()
            BEGIN
                INSERT INTO {{TGT}}.Facts
                SELECT * FROM {{SRC}}.Source;
                CALL {{OTHER_DB}}.Helper();
            END;
            """
        )
        refs = regex.extract_all_references(sql)
        databases = {r.database for r in refs}
        assert "{{TGT}}" in databases
        assert "{{SRC}}" in databases
        assert "{{OTHER_DB}}" in databases

    def test_owner_excluded(self, regex):
        sql = _strip(
            "REPLACE VIEW {{DOM_V}}.Customer_V AS SELECT * FROM {{REF_T}}.Customer;"
        )
        refs = regex.extract_all_references(sql)
        databases = {r.database for r in refs}
        # The view's own object is not a read source.
        assert "{{DOM_V}}" not in databases
        assert "{{REF_T}}" in databases

    def test_empty_for_isolated_select(self, regex):
        sql = _strip("SELECT 1;")
        assert regex.extract_all_references(sql) == set()


class TestAnalyseFileAcceptsExtractor:
    """A second extractor (compare-mode, AST, mock) can be wired into
    ``analyse_file`` without touching its body. Phase-2 lever."""

    def test_custom_extractor_used(self, tmp_path):
        from td_release_packager.infer_grants import analyse_file

        spy_calls: list[str] = []

        class _SpyExtractor(RegexSqlReferenceExtractor):
            def extract_read_sources(self, sql):
                spy_calls.append("read_sources")
                return super().extract_read_sources(sql)

        view_file = tmp_path / "DOM_V.Customer_V.viw"
        view_file.write_text(
            "REPLACE VIEW {{DOM_V}}.Customer_V AS SELECT * FROM {{REF_T}}.Customer;\n",
            encoding="utf-8",
        )

        result = analyse_file(view_file, extractor=_SpyExtractor())
        assert result is not None
        assert spy_calls, (
            "custom extractor.extract_read_sources should have been called"
        )
        assert "{{REF_T}}" in result["grants"]
