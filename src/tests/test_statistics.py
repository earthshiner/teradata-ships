"""
test_statistics.py — Tests for COLLECT/UPDATE STATISTICS (.stt) support
in the database_package_deployer.

Covers every touch-point:
    1. Models      — ObjectType.STATISTICS strategy, scope, deploy order
    2. Parser      — _detect_object_type, _detect_deploy_intent,
                     parse_statement_text (COLLECT, COLLECT SUMMARY,
                     UPDATE STATISTICS variants; manifest key uniqueness)
"""

from __future__ import annotations

import pytest

from database_package_deployer.models import (
    DeployScope,
    DeployStrategy,
    DEPLOY_ORDER,
    ObjectType,
    SCOPE_MAP,
    STRATEGY_MAP,
)
from database_package_deployer.statement_parser import (
    _detect_object_type,
    _detect_deploy_intent,
    parse_statement_text,
)


# ---------------------------------------------------------------------------
# Fixtures — representative COLLECT/UPDATE STATISTICS statements
# ---------------------------------------------------------------------------

# Teradata canonical forms from the Berka deployment:
_COLLECT_BASIC = (
    "COLLECT STATISTICS ON BerkaLoanRisk_Domain.Account_H COLUMN (account_id);"
)
_COLLECT_SUMMARY = (
    "COLLECT SUMMARY STATISTICS ON BerkaLoanRisk_Domain.Account_H;"
)
_COLLECT_COLUMN_FIRST = (
    "COLLECT STATISTICS COLUMN (account_id, status) "
    "ON BerkaLoanRisk_Domain.Account_H;"
)
_COLLECT_INDEX = (
    "COLLECT STATISTICS INDEX (account_id) ON BerkaLoanRisk_Domain.Account_H;"
)
_UPDATE_STATISTICS = (
    "UPDATE STATISTICS ON BerkaLoanRisk_Domain.Client_H COLUMN (client_id);"
)
_COLLECT_MULTILINE = """
COLLECT STATISTICS
    COLUMN (account_id)
   ,COLUMN (status, open_date)
ON BerkaLoanRisk_Domain.Account_H
;
"""


# ---------------------------------------------------------------------------
# 1. Models
# ---------------------------------------------------------------------------


class TestModelsStatistics:
    """database_package_deployer.models — ObjectType.STATISTICS metadata."""

    def test_statistics_in_object_type(self):
        """ObjectType.STATISTICS exists with value 'STATISTICS'."""
        assert ObjectType.STATISTICS.value == "STATISTICS"

    def test_statistics_strategy_is_direct_execute(self):
        """COLLECT STATISTICS executes as-is — no existence check needed."""
        assert STRATEGY_MAP[ObjectType.STATISTICS] == DeployStrategy.DIRECT_EXECUTE

    def test_statistics_scope_is_environment(self):
        """STATISTICS is environment-scoped."""
        assert SCOPE_MAP[ObjectType.STATISTICS] == DeployScope.ENVIRONMENT

    def test_statistics_deploy_order_after_table(self):
        """STATISTICS deploys after TABLE (must have data to collect stats on)."""
        assert DEPLOY_ORDER[ObjectType.STATISTICS] > DEPLOY_ORDER[ObjectType.TABLE]

    def test_statistics_deploy_order_after_index(self):
        """STATISTICS deploys after indexes — captures indexed column statistics."""
        assert DEPLOY_ORDER[ObjectType.STATISTICS] > DEPLOY_ORDER[ObjectType.INDEX]

    def test_statistics_deploy_order_before_view(self):
        """STATISTICS deploys before VIEW (optimiser stats before view compilation)."""
        assert DEPLOY_ORDER[ObjectType.STATISTICS] < DEPLOY_ORDER[ObjectType.VIEW]


# ---------------------------------------------------------------------------
# 2. Parser — _detect_object_type
# ---------------------------------------------------------------------------


class TestDetectObjectTypeStatistics:
    """statement_parser._detect_object_type — statistics pattern matching."""

    def test_collect_basic_detected(self):
        obj_type, _ = _detect_object_type(_COLLECT_BASIC)
        assert obj_type == ObjectType.STATISTICS

    def test_collect_summary_detected(self):
        """COLLECT SUMMARY STATISTICS variant is recognised."""
        obj_type, _ = _detect_object_type(_COLLECT_SUMMARY)
        assert obj_type == ObjectType.STATISTICS

    def test_collect_column_first_detected(self):
        """COLLECT STATISTICS COLUMN ... ON form is recognised."""
        obj_type, _ = _detect_object_type(_COLLECT_COLUMN_FIRST)
        assert obj_type == ObjectType.STATISTICS

    def test_collect_index_detected(self):
        """COLLECT STATISTICS INDEX ... ON form is recognised."""
        obj_type, _ = _detect_object_type(_COLLECT_INDEX)
        assert obj_type == ObjectType.STATISTICS

    def test_update_statistics_detected(self):
        """UPDATE STATISTICS (Teradata synonym) is recognised."""
        obj_type, _ = _detect_object_type(_UPDATE_STATISTICS)
        assert obj_type == ObjectType.STATISTICS

    def test_multiline_collect_detected(self):
        """Multi-line COLLECT STATISTICS is recognised."""
        obj_type, _ = _detect_object_type(_COLLECT_MULTILINE)
        assert obj_type == ObjectType.STATISTICS

    def test_case_insensitive(self):
        """Pattern matching is case-insensitive."""
        lower = "collect statistics on mydb.t column (id);"
        obj_type, _ = _detect_object_type(lower)
        assert obj_type == ObjectType.STATISTICS

    def test_statistics_not_detected_as_dml(self):
        """COLLECT STATISTICS must not be mis-classified as DML."""
        obj_type, _ = _detect_object_type(_COLLECT_BASIC)
        assert obj_type != ObjectType.DML

    def test_statistics_not_detected_as_unknown(self):
        """COLLECT STATISTICS must not fall through to UNKNOWN."""
        obj_type, _ = _detect_object_type(_COLLECT_BASIC)
        assert obj_type != ObjectType.UNKNOWN


# ---------------------------------------------------------------------------
# 2. Parser — _detect_deploy_intent
# ---------------------------------------------------------------------------


class TestDetectDeployIntentStatistics:
    """statement_parser._detect_deploy_intent — STATISTICS always DIRECT_EXECUTE."""

    def test_collect_intent(self):
        from database_package_deployer.models import DeployIntent
        intent = _detect_deploy_intent(_COLLECT_BASIC, ObjectType.STATISTICS)
        assert intent == DeployIntent.DIRECT_EXECUTE

    def test_update_statistics_intent(self):
        from database_package_deployer.models import DeployIntent
        intent = _detect_deploy_intent(_UPDATE_STATISTICS, ObjectType.STATISTICS)
        assert intent == DeployIntent.DIRECT_EXECUTE


# ---------------------------------------------------------------------------
# 2. Parser — parse_statement_text (end-to-end)
# ---------------------------------------------------------------------------


class TestParseStatementTextStatistics:
    """statement_parser.parse_statement_text — end-to-end statistics parsing."""

    def test_collect_basic_parses(self):
        result = parse_statement_text(_COLLECT_BASIC, file_path="Account_H.stt")
        assert result.object_type == ObjectType.STATISTICS

    def test_collect_strategy(self):
        result = parse_statement_text(_COLLECT_BASIC, file_path="Account_H.stt")
        assert result.strategy == DeployStrategy.DIRECT_EXECUTE

    def test_manifest_key_is_filename_derived(self):
        """qualified_name uses 'STT:<basename>' to prevent multi-script collisions."""
        result = parse_statement_text(
            _COLLECT_BASIC, file_path="BerkaLoanRisk_Domain.Account_H.stt"
        )
        assert result.qualified_name == "STT:BerkaLoanRisk_Domain.Account_H"

    def test_two_stt_scripts_same_table_distinct_keys(self):
        """Two .stt files targeting the same table get distinct manifest keys."""
        r1 = parse_statement_text(
            _COLLECT_BASIC, file_path="BerkaLoanRisk_Domain.Account_H_col.stt"
        )
        r2 = parse_statement_text(
            _COLLECT_BASIC, file_path="BerkaLoanRisk_Domain.Account_H_idx.stt"
        )
        assert r1.qualified_name != r2.qualified_name

    def test_update_statistics_parses(self):
        result = parse_statement_text(_UPDATE_STATISTICS, file_path="Client_H.stt")
        assert result.object_type == ObjectType.STATISTICS

    def test_collect_summary_parses(self):
        result = parse_statement_text(_COLLECT_SUMMARY, file_path="Account_H.stt")
        assert result.object_type == ObjectType.STATISTICS

    def test_no_multiset_injection(self):
        """COLLECT STATISTICS must never have MULTISET injected."""
        result = parse_statement_text(_COLLECT_BASIC, file_path="Account_H.stt")
        assert result.multiset_injected is False
        assert "MULTISET" not in result.ddl_text
