"""Tests for SqlGlotSqlReferenceExtractor (#234 Phase 2)."""

from __future__ import annotations

import pytest

from td_release_packager.infer_grants import strip_sql_comments
from td_release_packager.sql_reference_extractor import (
    ExtractorMismatch,
    PRIV_DELETE,
    PRIV_EXEC,
    PRIV_EXEC_PROC,
    PRIV_INSERT,
    PRIV_UPDATE,
    ReferencedObject,
    RegexSqlReferenceExtractor,
    SqlReferenceExtractor,
    StatementOwner,
    compare_extractors,
)
from td_release_packager.sql_reference_extractor_sqlglot import (
    SqlGlotSqlReferenceExtractor,
    SqlGlotUnavailable,
    _from_sentinel,
    _to_sentinels,
    is_available,
)


sqlglot_required = pytest.mark.skipif(
    not is_available(),
    reason="sqlglot extra not installed; install with `uv pip install -e .[ast]`",
)


# ---------------------------------------------------------------
# Token sentinel round-trip
# ---------------------------------------------------------------


class TestTokenSentinels:
    def test_simple_token_replaced(self):
        assert _to_sentinels("SELECT * FROM {{DOM_T}}.X") == (
            "SELECT * FROM __SHIPS_TKN_DOM_T.X"
        )

    def test_multiple_tokens(self):
        out = _to_sentinels("INSERT INTO {{TGT}}.Y SELECT * FROM {{SRC}}.X")
        assert "{{" not in out
        assert "__SHIPS_TKN_TGT" in out
        assert "__SHIPS_TKN_SRC" in out

    def test_round_trip_identifier(self):
        assert _from_sentinel("__SHIPS_TKN_DOM_T") == "{{DOM_T}}"

    def test_round_trip_passthrough_for_literal(self):
        assert _from_sentinel("MyDatabase") == "MyDatabase"


# ---------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------


def test_is_available_returns_bool():
    assert isinstance(is_available(), bool)


def test_constructor_raises_when_sqlglot_missing(monkeypatch):
    monkeypatch.setattr(
        "td_release_packager.sql_reference_extractor_sqlglot.is_available",
        lambda: False,
    )
    with pytest.raises(SqlGlotUnavailable):
        SqlGlotSqlReferenceExtractor()


# ---------------------------------------------------------------
# AST behaviour parity with regex on the seed corpus
# ---------------------------------------------------------------


@pytest.fixture
def sg() -> SqlReferenceExtractor:
    if not is_available():
        pytest.skip("sqlglot not installed")
    return SqlGlotSqlReferenceExtractor()


@sqlglot_required
class TestExtractStatementOwner:
    def test_view(self, sg):
        sql = strip_sql_comments(
            "REPLACE VIEW {{DOM_V}}.Customer_V AS SELECT 1 FROM {{REF_T}}.Customer;"
        )
        owner = sg.extract_statement_owner(sql)
        assert owner == StatementOwner(
            database="{{DOM_V}}",
            object_name="Customer_V",
            object_type="VIEW",
        )

    def test_procedure_literal(self, sg):
        sql = strip_sql_comments(
            "CREATE PROCEDURE PROC_DB.RefreshFacts () BEGIN SELECT 1; END;"
        )
        owner = sg.extract_statement_owner(sql)
        assert owner is not None
        assert owner.object_type == "PROCEDURE"
        assert owner.database == "PROC_DB"
        assert owner.object_name == "RefreshFacts"

    def test_table_normalised(self, sg):
        sql = strip_sql_comments("CREATE MULTISET TABLE TBL_DB.T1 (id INT);")
        owner = sg.extract_statement_owner(sql)
        assert owner is not None
        assert owner.object_type == "TABLE"


@sqlglot_required
class TestExtractReadSources:
    def test_excludes_subquery_alias_srv_processsumbybusdate(self, sg):
        """The canonical regression — AST should never see the alias as a table."""
        sql = strip_sql_comments(
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
        sources = sg.extract_read_sources(sql)
        names = {(s.database, s.object_name) for s in sources}
        assert ("{{REF_T}}", "ProcessRun") in names
        assert ("{{REF_T}}", "ProcessSum") in names
        # The view's own name and the derived-table aliases must not leak.
        assert not any(s.database == "sRV_ProcessSumByBusDate" for s in sources)
        assert not any(s.object_name == "ProcessStatus_V" for s in sources)

    def test_excludes_cte_name(self, sg):
        sql = strip_sql_comments(
            """
            REPLACE VIEW {{DOM_V}}.Recent_Customers_V AS
            WITH cte_recent AS (
                SELECT customer_id, MAX(order_dt) AS last_order
                FROM {{DOM_T}}.Orders
                GROUP BY customer_id
            )
            SELECT c.customer_id, r.last_order
            FROM cte_recent r
            INNER JOIN {{REF_T}}.Customer c ON c.customer_id = r.customer_id;
            """
        )
        sources = sg.extract_read_sources(sql)
        names = {(s.database, s.object_name) for s in sources}
        assert ("{{DOM_T}}", "Orders") in names
        assert ("{{REF_T}}", "Customer") in names
        assert "cte_recent" not in {s.database for s in sources}


@sqlglot_required
class TestExtractWriteTargets:
    def test_merge_implies_insert_and_update(self, sg):
        sql = strip_sql_comments(
            """
            MERGE INTO {{TGT}}.Customer t
            USING {{SRC}}.Updates s ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET name = s.name
            WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.name);
            """
        )
        targets = sg.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Customer")] == frozenset(
            {PRIV_INSERT, PRIV_UPDATE}
        )

    def test_delete_target(self, sg):
        sql = strip_sql_comments("DELETE FROM {{TGT}}.Stale;")
        targets = sg.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Stale")] == frozenset({PRIV_DELETE})

    def test_update_target(self, sg):
        sql = strip_sql_comments("UPDATE {{TGT}}.Stats SET cnt = cnt + 1;")
        targets = sg.extract_write_targets(sql)
        assert targets[ReferencedObject("{{TGT}}", "Stats")] == frozenset({"UPDATE"})


@sqlglot_required
class TestExtractCallTargetsFallback:
    """CALL / EXEC fall back to the regex extractor — verify behaviour matches."""

    def test_call_delegates_to_regex(self, sg):
        sql = strip_sql_comments("CALL {{PROC_DB}}.Refresh();")
        targets = sg.extract_call_targets(sql)
        assert targets[ReferencedObject("{{PROC_DB}}", "Refresh")] == frozenset(
            {PRIV_EXEC_PROC}
        )

    def test_exec_delegates_to_regex(self, sg):
        sql = strip_sql_comments("EXEC {{MAC_DB}}.RunIt;")
        targets = sg.extract_call_targets(sql)
        assert ReferencedObject("{{MAC_DB}}", "RunIt") in targets
        assert PRIV_EXEC in targets[ReferencedObject("{{MAC_DB}}", "RunIt")]


@sqlglot_required
class TestParseFailureFallback:
    """When sqlglot cannot parse the input, every method falls back to
    the configured regex extractor — not raises."""

    def test_garbled_sql_falls_back(self, sg):
        garbled = "REPLACE VIEW {{DOM_V}}.x AS SELECT * FROM @@@ unparseable"
        owner = sg.extract_statement_owner(garbled)
        # The regex extractor can still find the CREATE; sqlglot can't.
        # Either way, the call must not raise.
        assert owner is None or owner.object_type == "VIEW"

    def test_truly_invalid_sql_returns_empty(self, sg):
        owner = sg.extract_statement_owner("@@@ not sql @@@")
        assert owner is None


# ---------------------------------------------------------------
# compare_extractors
# ---------------------------------------------------------------


@sqlglot_required
class TestCompareExtractors:
    def test_agreement_returns_empty_list(self, sg):
        sql = strip_sql_comments(
            "REPLACE VIEW {{DOM_V}}.V AS SELECT * FROM {{REF_T}}.T;"
        )
        regex = RegexSqlReferenceExtractor()
        assert compare_extractors(regex, sg, sql) == []

    def test_seed_corpus_cases_agree(self, sg):
        """Every seed corpus case must produce identical output from
        both extractors. As Phase 2 widens the corpus this guards
        every newly added case the same way."""
        regex = RegexSqlReferenceExtractor()
        import json
        from pathlib import Path

        corpus_dir = Path(__file__).parent / "sql_reference_corpus"
        for case in sorted(corpus_dir.iterdir()):
            if not case.is_dir():
                continue
            sql = strip_sql_comments((case / "sql.sql").read_text(encoding="utf-8"))
            mismatches = compare_extractors(regex, sg, sql)
            assert not mismatches, f"{case.name}: extractors disagree:\n" + "\n".join(
                f"  {m.method}: regex={m.primary!r} sqlglot={m.secondary!r}"
                for m in mismatches
            )

    def test_mismatch_emits_structured_report(self):
        """A synthetic mismatch surfaces the method name and both
        payloads — Phase 2's diagnostic contract."""

        class _OwnerOnlyExtractor(RegexSqlReferenceExtractor):
            def extract_read_sources(self, sql):  # type: ignore[override]
                return set()  # deliberately wrong

        sql = strip_sql_comments(
            "REPLACE VIEW {{DOM_V}}.V AS SELECT * FROM {{REF_T}}.T;"
        )
        primary = RegexSqlReferenceExtractor()
        secondary = _OwnerOnlyExtractor()
        mismatches = compare_extractors(primary, secondary, sql)
        kinds = {m.method for m in mismatches}
        assert "read_sources" in kinds
        offender = next(m for m in mismatches if m.method == "read_sources")
        assert offender.primary  # primary has refs
        assert offender.secondary == []  # secondary returns nothing
