"""
test_foreign_key.py — Tests for *.fk (ALTER TABLE ... ADD FOREIGN KEY) support.

Covers every touch-point introduced by the fk-extension-support feature:

    1. Classifier  — .fk extension and content pattern detection
    2. kind_suffix — .fk and FOREIGN_KEY map to kind 'T'
    3. Models      — ObjectType.FOREIGN_KEY strategy, scope, deploy order
    4. Parser      — _detect_object_type, _detect_deploy_intent,
                     parse_statement_text (qualified name, manifest key,
                     quoted identifiers, single-part name rejection)
"""

from __future__ import annotations

import pytest

from td_release_packager import classifier as cls
from td_release_packager.kind_suffix import (
    TYPE_TO_KIND,
    EXTENSION_TO_KIND,
    kind_for_type,
    kind_for_extension,
)
from database_package_deployer.models import (
    ObjectType,
    DeployStrategy,
    DeployScope,
    STRATEGY_MAP,
    SCOPE_MAP,
    DEPLOY_ORDER,
)
from database_package_deployer.statement_parser import (
    _detect_object_type,
    _detect_deploy_intent,
    parse_statement_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# The example script supplied by the developer.
_EXAMPLE_FK = (
    'ALTER TABLE "Berka_Staging"."fin_card" ADD FOREIGN KEY ( disp_id ) '
    "REFERENCES WITH NO CHECK OPTION Berka_Staging.fin_disp ;"
)

# Minimal unquoted variant.
_SIMPLE_FK = (
    "ALTER TABLE MyDB.my_table ADD FOREIGN KEY (col_id) "
    "REFERENCES MyDB.other_table ;"
)

# Multi-column FK.
_MULTI_COL_FK = (
    "ALTER TABLE Sales.Orders ADD FOREIGN KEY (cust_id, region_id) "
    "REFERENCES Sales.Customers (cust_id, region_id) ;"
)


# ---------------------------------------------------------------------------
# 1. Classifier
# ---------------------------------------------------------------------------


class TestClassifierForeignKey:
    """td_release_packager.classifier — FK content and extension handling."""

    def test_example_fk_classified_as_foreign_key(self):
        """Developer-supplied example script classifies as FOREIGN_KEY."""
        r = cls.classify("Berka_Staging_fin_card.fk", _EXAMPLE_FK)
        assert r.type == "FOREIGN_KEY"

    def test_simple_fk_classified(self):
        """Unquoted minimal ALTER TABLE ADD FOREIGN KEY classifies as FOREIGN_KEY."""
        r = cls.classify("MyDB_my_table.fk", _SIMPLE_FK)
        assert r.type == "FOREIGN_KEY"

    def test_multi_col_fk_classified(self):
        """Multi-column FK constraint classifies as FOREIGN_KEY."""
        r = cls.classify("Sales_Orders.fk", _MULTI_COL_FK)
        assert r.type == "FOREIGN_KEY"

    def test_fk_extension_expected_set(self):
        """EXTENSION_TO_EXPECTED maps .fk exclusively to FOREIGN_KEY."""
        assert cls.EXTENSION_TO_EXPECTED[".fk"] == {"FOREIGN_KEY"}

    def test_type_to_extension_maps_fk(self):
        """TYPE_TO_EXTENSION maps FOREIGN_KEY to .fk."""
        assert cls.TYPE_TO_EXTENSION["FOREIGN_KEY"] == ".fk"

    def test_type_to_subdir_maps_alters(self):
        """TYPE_TO_SUBDIR places FOREIGN_KEY under DDL/alters."""
        assert cls.TYPE_TO_SUBDIR["FOREIGN_KEY"] == "DDL/alters"

    def test_fk_in_base_types(self):
        """FOREIGN_KEY appears in BASE_TYPES."""
        assert "FOREIGN_KEY" in cls.BASE_TYPES

    def test_fk_not_classified_as_dml(self):
        """ALTER TABLE ADD FOREIGN KEY must not fall through to DML."""
        r = cls.classify("x.fk", _SIMPLE_FK)
        assert r.type != "DML"

    def test_fk_confidence_high_with_matching_extension(self):
        """A .fk file containing FOREIGN_KEY content gets HIGH confidence."""
        r = cls.classify("MyDB_my_table.fk", _SIMPLE_FK)
        assert r.confidence == "HIGH"

    def test_fk_confidence_low_on_extension_mismatch(self):
        """A .tbl file containing FK content triggers a mismatch warning."""
        r = cls.classify("MyDB_my_table.tbl", _SIMPLE_FK)
        assert r.confidence == "LOW"
        assert any("Filename mismatch" in w for w in r.warnings)

    def test_generic_sql_extension_accepted(self):
        """A .sql file containing FK content classifies without a mismatch warning."""
        r = cls.classify("migrate.sql", _SIMPLE_FK)
        assert r.type == "FOREIGN_KEY"
        assert not any("Filename mismatch" in w for w in r.warnings)

    def test_case_insensitive_keywords(self):
        """Pattern matching is case-insensitive."""
        lower = "alter table mydb.t add foreign key (c) references mydb.r ;"
        r = cls.classify("t.fk", lower)
        assert r.type == "FOREIGN_KEY"

    def test_create_table_with_inline_foreign_key_not_classified_fk(self):
        """An inline FOREIGN KEY inside CREATE TABLE must not classify as FOREIGN_KEY.

        The CREATE TABLE pattern precedes the FK pattern in _CLASSIFY_PATTERNS,
        so the CREATE TABLE wins and the inline constraint is ignored.
        """
        create_with_fk = (
            "CREATE MULTISET TABLE MyDB.Orders (\n"
            "    order_id INTEGER NOT NULL,\n"
            "    cust_id  INTEGER,\n"
            "    FOREIGN KEY (cust_id) REFERENCES MyDB.Customers\n"
            ") PRIMARY INDEX (order_id);"
        )
        r = cls.classify("Orders.tbl", create_with_fk)
        assert r.type == "TABLE"


# ---------------------------------------------------------------------------
# 2. kind_suffix
# ---------------------------------------------------------------------------


class TestKindSuffixForeignKey:
    """td_release_packager.kind_suffix — FK maps to kind 'T'."""

    def test_type_to_kind_foreign_key(self):
        """TYPE_TO_KIND maps FOREIGN_KEY to 'T'."""
        assert TYPE_TO_KIND["FOREIGN_KEY"] == "T"

    def test_extension_to_kind_fk(self):
        """EXTENSION_TO_KIND maps .fk to 'T'."""
        assert EXTENSION_TO_KIND[".fk"] == "T"

    def test_kind_for_type_helper(self):
        """kind_for_type('FOREIGN_KEY') returns 'T'."""
        assert kind_for_type("FOREIGN_KEY") == "T"

    def test_kind_for_extension_helper(self):
        """kind_for_extension('.fk') returns 'T'."""
        assert kind_for_extension(".fk") == "T"

    def test_kind_for_extension_case_insensitive(self):
        """kind_for_extension is case-insensitive."""
        assert kind_for_extension(".FK") == "T"


# ---------------------------------------------------------------------------
# 3. Models
# ---------------------------------------------------------------------------


class TestModelsForeignKey:
    """database_package_deployer.models — ObjectType.FOREIGN_KEY metadata."""

    def test_foreign_key_in_object_type(self):
        """ObjectType.FOREIGN_KEY exists."""
        assert ObjectType.FOREIGN_KEY.value == "FOREIGN_KEY"

    def test_foreign_key_strategy_is_direct_execute(self):
        """FOREIGN_KEY uses DIRECT_EXECUTE — no existence pre-check needed."""
        assert STRATEGY_MAP[ObjectType.FOREIGN_KEY] == DeployStrategy.DIRECT_EXECUTE

    def test_foreign_key_scope_is_environment(self):
        """FOREIGN_KEY is environment-scoped (token-substituted per environment)."""
        assert SCOPE_MAP[ObjectType.FOREIGN_KEY] == DeployScope.ENVIRONMENT

    def test_foreign_key_deploy_order_after_table(self):
        """FOREIGN_KEY deploys after TABLE (order > 0)."""
        assert DEPLOY_ORDER[ObjectType.FOREIGN_KEY] > DEPLOY_ORDER[ObjectType.TABLE]

    def test_foreign_key_deploy_order_before_view(self):
        """FOREIGN_KEY deploys before VIEW (constraint setup before view layer)."""
        assert DEPLOY_ORDER[ObjectType.FOREIGN_KEY] < DEPLOY_ORDER[ObjectType.VIEW]

    def test_foreign_key_deploy_order_same_as_index(self):
        """FOREIGN_KEY shares deploy order slot with secondary indexes."""
        assert DEPLOY_ORDER[ObjectType.FOREIGN_KEY] == DEPLOY_ORDER[ObjectType.INDEX]


# ---------------------------------------------------------------------------
# 4. Parser — _detect_object_type
# ---------------------------------------------------------------------------


class TestDetectObjectTypeForeignKey:
    """statement_parser._detect_object_type — FK pattern matching."""

    def test_example_fk_detected(self):
        """Developer-supplied example detects as FOREIGN_KEY."""
        obj_type, qualified = _detect_object_type(_EXAMPLE_FK)
        assert obj_type == ObjectType.FOREIGN_KEY

    def test_example_fk_extracts_database(self):
        """Quoted database name is extracted from ALTER TABLE clause."""
        _, qualified = _detect_object_type(_EXAMPLE_FK)
        # qualified_raw is the full matched group — may include db.table
        assert "Berka_Staging" in qualified or "fin_card" in qualified

    def test_simple_fk_detected(self):
        """Unquoted ALTER TABLE ... ADD FOREIGN KEY detects as FOREIGN_KEY."""
        obj_type, _ = _detect_object_type(_SIMPLE_FK)
        assert obj_type == ObjectType.FOREIGN_KEY

    def test_multi_col_fk_detected(self):
        """Multi-column FK detects as FOREIGN_KEY."""
        obj_type, _ = _detect_object_type(_MULTI_COL_FK)
        assert obj_type == ObjectType.FOREIGN_KEY

    def test_fk_not_detected_as_dml(self):
        """FK scripts must not be classified as DML."""
        obj_type, _ = _detect_object_type(_SIMPLE_FK)
        assert obj_type != ObjectType.DML

    def test_case_insensitive_detection(self):
        """Detection is case-insensitive."""
        lower = "alter table mydb.t add foreign key (c) references mydb.r ;"
        obj_type, _ = _detect_object_type(lower)
        assert obj_type == ObjectType.FOREIGN_KEY


# ---------------------------------------------------------------------------
# 4. Parser — _detect_deploy_intent
# ---------------------------------------------------------------------------


class TestDetectDeployIntentForeignKey:
    """statement_parser._detect_deploy_intent — FK always DIRECT_EXECUTE."""

    def test_example_fk_intent(self):
        """FOREIGN_KEY intent is DIRECT_EXECUTE."""
        from database_package_deployer.models import DeployIntent
        intent = _detect_deploy_intent(_EXAMPLE_FK, ObjectType.FOREIGN_KEY)
        assert intent == DeployIntent.DIRECT_EXECUTE

    def test_simple_fk_intent(self):
        """Unquoted FK intent is DIRECT_EXECUTE."""
        from database_package_deployer.models import DeployIntent
        intent = _detect_deploy_intent(_SIMPLE_FK, ObjectType.FOREIGN_KEY)
        assert intent == DeployIntent.DIRECT_EXECUTE


# ---------------------------------------------------------------------------
# 4. Parser — parse_statement_text (full parse)
# ---------------------------------------------------------------------------


class TestParseStatementTextForeignKey:
    """statement_parser.parse_statement_text — end-to-end FK parsing."""

    def test_example_fk_parses(self):
        """Developer-supplied example parses without raising."""
        result = parse_statement_text(
            _EXAMPLE_FK,
            file_path="Berka_Staging_fin_card.fk",
        )
        assert result.object_type == ObjectType.FOREIGN_KEY

    def test_example_fk_strategy(self):
        """Parsed FK uses DIRECT_EXECUTE strategy."""
        result = parse_statement_text(
            _EXAMPLE_FK,
            file_path="Berka_Staging_fin_card.fk",
        )
        assert result.strategy == DeployStrategy.DIRECT_EXECUTE

    def test_example_fk_database_name(self):
        """Database name extracted from quoted ALTER TABLE identifier."""
        result = parse_statement_text(
            _EXAMPLE_FK,
            file_path="Berka_Staging_fin_card.fk",
        )
        assert result.database_name == "Berka_Staging"

    def test_example_fk_object_name(self):
        """Object name extracted from quoted ALTER TABLE identifier."""
        result = parse_statement_text(
            _EXAMPLE_FK,
            file_path="Berka_Staging_fin_card.fk",
        )
        assert result.object_name == "fin_card"

    def test_fk_manifest_key_is_filename_derived(self):
        """qualified_name uses 'FK:<basename>' so multiple FK scripts on
        the same table do not collide in the manifest."""
        result = parse_statement_text(
            _EXAMPLE_FK,
            file_path="Berka_Staging_fin_card.fk",
        )
        assert result.qualified_name == "FK:Berka_Staging_fin_card"

    def test_two_fk_scripts_same_table_produce_distinct_manifest_keys(self):
        """Two FK files targeting the same table get distinct qualified_names."""
        r1 = parse_statement_text(_SIMPLE_FK, file_path="MyDB_my_table_fk1.fk")
        r2 = parse_statement_text(_SIMPLE_FK, file_path="MyDB_my_table_fk2.fk")
        assert r1.qualified_name != r2.qualified_name

    def test_simple_fk_database_name(self):
        """Unquoted database name extracted correctly."""
        result = parse_statement_text(_SIMPLE_FK, file_path="MyDB_my_table.fk")
        assert result.database_name == "MyDB"

    def test_simple_fk_object_name(self):
        """Unquoted object (table) name extracted correctly."""
        result = parse_statement_text(_SIMPLE_FK, file_path="MyDB_my_table.fk")
        assert result.object_name == "my_table"

    def test_fk_without_database_qualifier_raises(self):
        """A single-part table name (no database qualifier) must raise ValueError."""
        no_qualifier = (
            "ALTER TABLE unqualified_table ADD FOREIGN KEY (c) "
            "REFERENCES other_table ;"
        )
        with pytest.raises(ValueError, match="database qualifier"):
            parse_statement_text(no_qualifier, file_path="unqualified_table.fk")

    def test_fk_no_multiset_injection(self):
        """FK scripts must never have MULTISET injected into them."""
        result = parse_statement_text(_SIMPLE_FK, file_path="MyDB_my_table.fk")
        assert result.multiset_injected is False
        assert "MULTISET" not in result.ddl_text
