"""
test_deployer_models.py — Tests for the DDL deployer data models.

Covers:
    - DeployState valid transitions (state machine)
    - DeployStrategy ↔ ObjectType mapping
    - DeployIntent enum completeness
    - SHOW command mapping
    - Deploy ordering
    - TABLE_KIND mapping
    - PackageDeployResult properties
"""

import pytest

from database_package_deployer.models import (
    ObjectType,
    DeployIntent,
    DeployStrategy,
    DeployState,
    DeployScope,
    VALID_NEXT_STATES,
    STRATEGY_MAP,
    SCOPE_MAP,
    SHOW_COMMAND_MAP,
    DEPLOY_ORDER,
    TABLE_KIND_MAP,
    SYSTEM_EXISTENCE_QUERIES,
    PackageDeployResult,
    WaveSummary,
)


# ---------------------------------------------------------------
# DeployState — State machine transitions
# ---------------------------------------------------------------


class TestDeployStateTransitions:
    """Tests for the deployment state machine."""

    def test_pending_can_transition_to_backed_up(self):
        """PENDING → BACKED_UP is valid (table backup step)."""
        assert DeployState.BACKED_UP in VALID_NEXT_STATES[DeployState.PENDING]

    def test_pending_can_transition_to_created(self):
        """PENDING → CREATED is valid (new object, no backup needed)."""
        assert DeployState.CREATED in VALID_NEXT_STATES[DeployState.PENDING]

    def test_pending_can_transition_to_failed(self):
        """PENDING → FAILED is valid (error during pre-checks)."""
        assert DeployState.FAILED in VALID_NEXT_STATES[DeployState.PENDING]

    def test_backed_up_can_transition_to_created(self):
        """BACKED_UP → CREATED is valid (new table created after backup)."""
        assert DeployState.CREATED in VALID_NEXT_STATES[DeployState.BACKED_UP]

    def test_backed_up_can_transition_to_rolled_back(self):
        """BACKED_UP → ROLLED_BACK is valid (restore backup on failure)."""
        assert DeployState.ROLLED_BACK in VALID_NEXT_STATES[DeployState.BACKED_UP]

    def test_created_can_transition_to_migrated(self):
        """CREATED → MIGRATED is valid (data copied from backup)."""
        assert DeployState.MIGRATED in VALID_NEXT_STATES[DeployState.CREATED]

    def test_created_can_transition_to_completed(self):
        """CREATED → COMPLETED is valid (new object, no migration)."""
        assert DeployState.COMPLETED in VALID_NEXT_STATES[DeployState.CREATED]

    def test_migrated_can_transition_to_completed(self):
        """MIGRATED → COMPLETED is valid (final verification passed)."""
        assert DeployState.COMPLETED in VALID_NEXT_STATES[DeployState.MIGRATED]

    def test_completed_is_terminal(self):
        """COMPLETED has no valid next states (terminal)."""
        assert VALID_NEXT_STATES[DeployState.COMPLETED] == set()

    def test_skipped_is_terminal(self):
        """SKIPPED has no valid next states (terminal)."""
        assert VALID_NEXT_STATES[DeployState.SKIPPED] == set()

    def test_rolled_back_is_terminal(self):
        """ROLLED_BACK has no valid next states (terminal)."""
        assert VALID_NEXT_STATES[DeployState.ROLLED_BACK] == set()

    def test_failed_can_retry(self):
        """FAILED can transition to retry states (backed_up, created, etc.)."""
        allowed = VALID_NEXT_STATES[DeployState.FAILED]
        assert DeployState.BACKED_UP in allowed
        assert DeployState.CREATED in allowed
        assert DeployState.ROLLED_BACK in allowed

    def test_all_states_have_transitions_defined(self):
        """Every DeployState has an entry in VALID_NEXT_STATES."""
        for state in DeployState:
            assert state in VALID_NEXT_STATES, (
                f"DeployState.{state.name} missing from VALID_NEXT_STATES"
            )

    def test_no_transition_to_pending(self):
        """No state transitions back to PENDING (entry state only)."""
        for state, targets in VALID_NEXT_STATES.items():
            assert DeployState.PENDING not in targets, (
                f"{state.name} should not transition to PENDING"
            )


class TestCleanDbError:
    """Tests for user-facing Teradata error cleanup."""

    def test_prefers_inner_spl_compile_error(self):
        from database_package_deployer.deployer import _clean_db_error

        raw = (
            "REPLACE PROCEDURE Failed.  [5526] SPL1027:E(L29), "
            "Missing/Invalid SQL statement"
            "'E(5315):An owner referenced by user does not have SELECT WITH "
            "GRANT OPTION access to GDEV1T_GCFR.GCFR_File_Process.Process_Name.'."
        )

        assert _clean_db_error(raw) == (
            "[Error 5315] An owner referenced by user does not have SELECT WITH "
            "GRANT OPTION access to GDEV1T_GCFR.GCFR_File_Process.Process_Name."
        )


# ---------------------------------------------------------------
# STRATEGY_MAP — ObjectType → DeployStrategy
# ---------------------------------------------------------------


class TestStrategyMap:
    """Tests for object type to strategy mapping."""

    def test_table_is_idempotent(self):
        """TABLE → IDEMPOTENT_DEPLOY."""
        assert STRATEGY_MAP[ObjectType.TABLE] == DeployStrategy.IDEMPOTENT_DEPLOY

    def test_view_is_replace_in_place(self):
        """VIEW → REPLACE_IN_PLACE."""
        assert STRATEGY_MAP[ObjectType.VIEW] == DeployStrategy.REPLACE_IN_PLACE

    def test_macro_is_replace_in_place(self):
        """MACRO → REPLACE_IN_PLACE."""
        assert STRATEGY_MAP[ObjectType.MACRO] == DeployStrategy.REPLACE_IN_PLACE

    def test_procedure_is_replace_in_place(self):
        """PROCEDURE → REPLACE_IN_PLACE."""
        assert STRATEGY_MAP[ObjectType.PROCEDURE] == DeployStrategy.REPLACE_IN_PLACE

    def test_function_is_replace_in_place(self):
        """FUNCTION → REPLACE_IN_PLACE."""
        assert STRATEGY_MAP[ObjectType.FUNCTION] == DeployStrategy.REPLACE_IN_PLACE

    def test_join_index_is_drop_and_create(self):
        """JOIN_INDEX → DROP_AND_CREATE."""
        assert STRATEGY_MAP[ObjectType.JOIN_INDEX] == DeployStrategy.DROP_AND_CREATE

    def test_trigger_is_drop_and_create(self):
        """TRIGGER → DROP_AND_CREATE (Teradata's default pre-REPLACE support)."""
        assert STRATEGY_MAP[ObjectType.TRIGGER] == DeployStrategy.DROP_AND_CREATE

    def test_database_is_direct_execute(self):
        """DATABASE → DIRECT_EXECUTE."""
        assert STRATEGY_MAP[ObjectType.DATABASE] == DeployStrategy.DIRECT_EXECUTE

    def test_grant_is_direct_execute(self):
        """GRANT → DIRECT_EXECUTE."""
        assert STRATEGY_MAP[ObjectType.GRANT] == DeployStrategy.DIRECT_EXECUTE

    def test_ordered_sql_is_direct_execute(self):
        """ORDERED_SQL → DIRECT_EXECUTE."""
        assert STRATEGY_MAP[ObjectType.ORDERED_SQL] == DeployStrategy.DIRECT_EXECUTE

    def test_map_is_skip_if_exists(self):
        """MAP → SKIP_IF_EXISTS."""
        assert STRATEGY_MAP[ObjectType.MAP] == DeployStrategy.SKIP_IF_EXISTS

    def test_role_is_skip_if_exists(self):
        """ROLE → SKIP_IF_EXISTS (changed from DIRECT_EXECUTE)."""
        assert STRATEGY_MAP[ObjectType.ROLE] == DeployStrategy.SKIP_IF_EXISTS

    def test_profile_is_skip_if_exists(self):
        """PROFILE → SKIP_IF_EXISTS (changed from DIRECT_EXECUTE)."""
        assert STRATEGY_MAP[ObjectType.PROFILE] == DeployStrategy.SKIP_IF_EXISTS

    def test_authorization_is_skip_if_exists(self):
        """AUTHORIZATION → SKIP_IF_EXISTS."""
        assert STRATEGY_MAP[ObjectType.AUTHORIZATION] == DeployStrategy.SKIP_IF_EXISTS

    def test_foreign_server_is_skip_if_exists(self):
        """FOREIGN_SERVER → SKIP_IF_EXISTS."""
        assert STRATEGY_MAP[ObjectType.FOREIGN_SERVER] == DeployStrategy.SKIP_IF_EXISTS

    def test_jar_is_direct_execute(self):
        """JAR → DIRECT_EXECUTE."""
        assert STRATEGY_MAP[ObjectType.JAR] == DeployStrategy.DIRECT_EXECUTE

    def test_sto_is_replace_in_place(self):
        """SCRIPT_TABLE_OPERATOR → REPLACE_IN_PLACE."""
        assert (
            STRATEGY_MAP[ObjectType.SCRIPT_TABLE_OPERATOR]
            == DeployStrategy.REPLACE_IN_PLACE
        )

    def test_all_object_types_mapped(self):
        """Every ObjectType (except UNKNOWN) has a strategy mapping."""
        for obj_type in ObjectType:
            if obj_type != ObjectType.UNKNOWN:
                assert obj_type in STRATEGY_MAP, (
                    f"ObjectType.{obj_type.name} missing from STRATEGY_MAP"
                )


# ---------------------------------------------------------------
# SCOPE_MAP — ObjectType → DeployScope
# ---------------------------------------------------------------


class TestScopeMap:
    """Tests for object type to deployment scope mapping."""

    def test_system_scope_objects(self):
        """System-scope objects are correctly mapped."""
        system_types = [
            ObjectType.MAP,
            ObjectType.ROLE,
            ObjectType.PROFILE,
            ObjectType.AUTHORIZATION,
            ObjectType.FOREIGN_SERVER,
        ]
        for obj_type in system_types:
            assert SCOPE_MAP[obj_type] == DeployScope.SYSTEM, (
                f"{obj_type.name} should be SYSTEM scope"
            )

    def test_environment_scope_objects(self):
        """Environment-scope objects are correctly mapped."""
        env_types = [
            ObjectType.TABLE,
            ObjectType.VIEW,
            ObjectType.DATABASE,
            ObjectType.GRANT,
            ObjectType.JAR,
            ObjectType.SCRIPT_TABLE_OPERATOR,
            ObjectType.ORDERED_SQL,
        ]
        for obj_type in env_types:
            assert SCOPE_MAP[obj_type] == DeployScope.ENVIRONMENT, (
                f"{obj_type.name} should be ENVIRONMENT scope"
            )

    def test_all_non_unknown_types_have_scope(self):
        """Every ObjectType (except UNKNOWN) has a scope mapping."""
        for obj_type in ObjectType:
            if obj_type != ObjectType.UNKNOWN:
                assert obj_type in SCOPE_MAP, (
                    f"ObjectType.{obj_type.name} missing from SCOPE_MAP"
                )


# ---------------------------------------------------------------
# SHOW_COMMAND_MAP
# ---------------------------------------------------------------


class TestShowCommandMap:
    """Tests for SHOW command mapping used for backup capture."""

    def test_table_show(self):
        """TABLE uses SHOW TABLE."""
        assert SHOW_COMMAND_MAP[ObjectType.TABLE] == "SHOW TABLE"

    def test_view_show(self):
        """VIEW uses SHOW VIEW."""
        assert SHOW_COMMAND_MAP[ObjectType.VIEW] == "SHOW VIEW"

    def test_function_show_specific(self):
        """FUNCTION uses SHOW SPECIFIC FUNCTION (for overloads)."""
        assert SHOW_COMMAND_MAP[ObjectType.FUNCTION] == "SHOW SPECIFIC FUNCTION"

    def test_trigger_show(self):
        """TRIGGER uses SHOW TRIGGER."""
        assert SHOW_COMMAND_MAP[ObjectType.TRIGGER] == "SHOW TRIGGER"

    def test_jar_table_kind(self):
        """JAR uses TableKind 'D' for existence checks."""
        assert TABLE_KIND_MAP[ObjectType.JAR] == "D"

    def test_system_existence_queries_complete(self):
        """All system-scope types have existence check queries."""
        system_types = [
            ObjectType.MAP,
            ObjectType.ROLE,
            ObjectType.PROFILE,
            ObjectType.AUTHORIZATION,
            ObjectType.FOREIGN_SERVER,
        ]
        for obj_type in system_types:
            assert obj_type in SYSTEM_EXISTENCE_QUERIES, (
                f"{obj_type.name} missing from SYSTEM_EXISTENCE_QUERIES"
            )


# ---------------------------------------------------------------
# DEPLOY_ORDER
# ---------------------------------------------------------------


class TestDeployOrder:
    """Tests for deployment ordering."""

    def test_system_before_databases(self):
        """System objects (maps, roles) deploy before databases."""
        assert DEPLOY_ORDER[ObjectType.MAP] < DEPLOY_ORDER[ObjectType.DATABASE]
        assert DEPLOY_ORDER[ObjectType.ROLE] < DEPLOY_ORDER[ObjectType.DATABASE]
        assert DEPLOY_ORDER[ObjectType.PROFILE] < DEPLOY_ORDER[ObjectType.DATABASE]

    def test_authorization_before_databases(self):
        """Authorisations deploy before databases."""
        assert (
            DEPLOY_ORDER[ObjectType.AUTHORIZATION] < DEPLOY_ORDER[ObjectType.DATABASE]
        )

    def test_foreign_server_before_databases(self):
        """Foreign servers deploy before databases."""
        assert (
            DEPLOY_ORDER[ObjectType.FOREIGN_SERVER] < DEPLOY_ORDER[ObjectType.DATABASE]
        )

    def test_databases_before_tables(self):
        """Databases deploy before tables."""
        assert DEPLOY_ORDER[ObjectType.DATABASE] < DEPLOY_ORDER[ObjectType.TABLE]

    def test_grants_before_tables(self):
        """Grants deploy before tables."""
        assert DEPLOY_ORDER[ObjectType.GRANT] < DEPLOY_ORDER[ObjectType.TABLE]

    def test_tables_before_views(self):
        """Tables deploy before views."""
        assert DEPLOY_ORDER[ObjectType.TABLE] < DEPLOY_ORDER[ObjectType.VIEW]

    def test_tables_before_indexes(self):
        """Tables deploy before indexes."""
        assert DEPLOY_ORDER[ObjectType.TABLE] < DEPLOY_ORDER[ObjectType.JOIN_INDEX]

    def test_views_before_triggers(self):
        """Views deploy before triggers."""
        assert DEPLOY_ORDER[ObjectType.VIEW] < DEPLOY_ORDER[ObjectType.TRIGGER]

    def test_jars_before_triggers(self):
        """JARs deploy before triggers."""
        assert DEPLOY_ORDER[ObjectType.JAR] < DEPLOY_ORDER[ObjectType.TRIGGER]

    def test_unknown_is_last(self):
        """UNKNOWN type deploys last."""
        for obj_type in ObjectType:
            if obj_type != ObjectType.UNKNOWN:
                assert DEPLOY_ORDER.get(obj_type, 0) < DEPLOY_ORDER[ObjectType.UNKNOWN]

    def test_dml_after_all_ddl(self):
        """DML deploys after every DDL type so target tables /
        views / triggers exist before data is loaded."""
        ddl_types = [
            ObjectType.TABLE,
            ObjectType.JOIN_INDEX,
            ObjectType.HASH_INDEX,
            ObjectType.INDEX,
            ObjectType.VIEW,
            ObjectType.MACRO,
            ObjectType.PROCEDURE,
            ObjectType.FUNCTION,
            ObjectType.JAR,
            ObjectType.SCRIPT_TABLE_OPERATOR,
            ObjectType.TRIGGER,
        ]
        for t in ddl_types:
            assert DEPLOY_ORDER[t] < DEPLOY_ORDER[ObjectType.DML], (
                f"{t.name} should deploy before DML"
            )

    def test_dml_uses_direct_execute(self):
        """DML routes through DIRECT_EXECUTE — _execute_ddl already
        handles the multi-statement case."""
        assert STRATEGY_MAP[ObjectType.DML] == DeployStrategy.DIRECT_EXECUTE
        assert STRATEGY_MAP[ObjectType.ORDERED_SQL] == DeployStrategy.DIRECT_EXECUTE

    def test_dml_is_environment_scoped(self):
        """DML is per-environment (data lives in the target system)."""
        assert SCOPE_MAP[ObjectType.DML] == DeployScope.ENVIRONMENT
        assert SCOPE_MAP[ObjectType.ORDERED_SQL] == DeployScope.ENVIRONMENT


# ---------------------------------------------------------------
# PackageDeployResult properties
# ---------------------------------------------------------------


class TestPackageDeployResult:
    """Tests for aggregate deployment result properties."""

    def test_success_when_no_failures(self):
        """success is True when failed == 0 and rolled_back == 0."""
        result = PackageDeployResult(
            deployment_id="test-001",
            manifest_path="/tmp/manifest.json",
            total=5,
            completed=4,
            skipped=1,
            failed=0,
            rolled_back=0,
        )
        assert result.success is True

    def test_failure_when_objects_failed(self):
        """success is False when any objects failed."""
        result = PackageDeployResult(
            deployment_id="test-002",
            manifest_path="/tmp/manifest.json",
            total=5,
            completed=3,
            failed=2,
        )
        assert result.success is False

    def test_failure_when_rolled_back(self):
        """success is False when any objects were rolled back."""
        result = PackageDeployResult(
            deployment_id="test-003",
            manifest_path="/tmp/manifest.json",
            total=5,
            completed=4,
            rolled_back=1,
        )
        assert result.success is False

    def test_wave_parallel_flag(self):
        """is_wave_parallel is True when wave_summaries is non-empty."""
        result = PackageDeployResult(
            deployment_id="test-004",
            manifest_path="/tmp/manifest.json",
            wave_summaries=[WaveSummary(wave_number=1, total=3)],
        )
        assert result.is_wave_parallel is True

    def test_not_wave_parallel_when_empty(self):
        """is_wave_parallel is False when wave_summaries is empty."""
        result = PackageDeployResult(
            deployment_id="test-005",
            manifest_path="/tmp/manifest.json",
        )
        assert result.is_wave_parallel is False


# ---------------------------------------------------------------
# is_noop_redeploy — distinguishes 'replayed an already-deployed
# package' from 'genuine empty run'
# ---------------------------------------------------------------


class TestIsNoopRedeploy:
    """Tests for PackageDeployResult.is_noop_redeploy."""

    def test_true_when_no_results_with_prior(self):
        """Empty results + non-empty prior_completed → True."""
        result = PackageDeployResult(
            deployment_id="noop-1",
            manifest_path="/tmp/manifest.json",
            total=2,
            completed=2,
            results=[],
            prior_completed=[
                {"qualified_name": "DEV01_DB.A", "state": "COMPLETED"},
                {"qualified_name": "DEV01_DB.B", "state": "COMPLETED"},
            ],
        )
        assert result.is_noop_redeploy is True

    def test_false_when_results_present(self):
        """Any per-object results → not a noop redeploy, even with prior."""
        result = PackageDeployResult(
            deployment_id="noop-2",
            manifest_path="/tmp/manifest.json",
            total=2,
            results=[object()],  # any non-empty list
            prior_completed=[{"qualified_name": "DEV01_DB.A"}],
        )
        assert result.is_noop_redeploy is False

    def test_false_when_no_prior(self):
        """Empty results + empty prior → not a noop (just empty)."""
        result = PackageDeployResult(
            deployment_id="noop-3",
            manifest_path="/tmp/manifest.json",
            results=[],
            prior_completed=[],
        )
        assert result.is_noop_redeploy is False

    def test_false_on_default_construction(self):
        """A freshly built result with no fields is not a noop redeploy."""
        result = PackageDeployResult(
            deployment_id="noop-4",
            manifest_path="/tmp/manifest.json",
        )
        assert result.is_noop_redeploy is False


# ---------------------------------------------------------------
# _execute_ddl — statement splitting (comment + string-literal safe)
# ---------------------------------------------------------------


class _RecordingCursor:
    """Minimal cursor stub that records every execute() call."""

    def __init__(self):
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)


class _FailOnceCursor(_RecordingCursor):
    """Recording cursor that raises once, then succeeds."""

    def __init__(self, error_text: str):
        super().__init__()
        self.error_text = error_text
        self._failed = False

    def execute(self, sql):
        super().execute(sql)
        if not self._failed:
            self._failed = True
            raise Exception(self.error_text)


class _FailAlwaysCursor(_RecordingCursor):
    """Recording cursor that always raises."""

    def __init__(self, error_text: str):
        super().__init__()
        self.error_text = error_text

    def execute(self, sql):
        super().execute(sql)
        raise Exception(self.error_text)


class _TableTriggerCursor(_RecordingCursor):
    """Cursor stub for existing-table trigger blocker checks."""

    def __init__(self):
        super().__init__()
        self._fetchone = None
        self._fetchall = []
        self.trigger_ddl = (
            "REPLACE TRIGGER GDEV1T_GCFR.GCFR_TU_CE_System_Audit\n"
            "AFTER UPDATE ON GDEV1T_GCFR.GCFR_CE_System\n"
            "REFERENCING NEW AS n\n"
            "FOR EACH ROW (INSERT INTO GDEV1T_GCFR.AuditLog VALUES (n.Id));"
        )

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "FROM DBC.TablesV" in sql:
            self._fetchone = (1,)
            self._fetchall = []
        elif "FROM DBC.TriggersV" in sql:
            self._fetchone = None
            self._fetchall = [
                (
                    "GDEV1T_GCFR",
                    "GCFR_CE_System",
                    "GCFR_TU_CE_System_Audit",
                    "AFTER",
                    "UPDATE",
                    "ENABLED",
                )
            ]
        elif sql.startswith("SHOW TRIGGER"):
            self._fetchone = None
            self._fetchall = [(self.trigger_ddl,)]
        elif sql.startswith("DROP TRIGGER"):
            self._fetchone = None
            self._fetchall = []
        elif sql.startswith('SELECT TOP 1 1 FROM "GDEV1T_GCFR"."GCFR_CE_System"'):
            self._fetchone = None
            self._fetchall = []
        elif sql.startswith('DROP TABLE "GDEV1T_GCFR"."GCFR_CE_System"'):
            self._fetchone = None
            self._fetchall = []
        elif sql.startswith("CREATE MULTISET TABLE GDEV1T_GCFR.GCFR_CE_System"):
            self._fetchone = None
            self._fetchall = []
        elif sql.startswith("REPLACE TRIGGER GDEV1T_GCFR.GCFR_TU_CE_System_Audit"):
            self._fetchone = None
            self._fetchall = []
        else:
            raise AssertionError(f"Unexpected SQL after trigger blocker: {sql}")

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class TestTableTriggerBlockers:
    """Existing tables with triggers should fail with named blockers."""

    def test_table_trigger_blockers_are_described(self):
        from database_package_deployer.deployer import _table_trigger_blockers

        cur = _TableTriggerCursor()
        blockers = _table_trigger_blockers(cur, "GDEV1T_GCFR", "GCFR_CE_System")

        assert blockers == [
            "GCFR_TU_CE_System_Audit (AFTER UPDATE, ENABLED) "
            "on GDEV1T_GCFR.GCFR_CE_System"
        ]

    def test_deploy_table_stops_before_drop_or_rename_when_triggers_exist(self):
        from database_package_deployer.deployer import _deploy_table
        from database_package_deployer.models import ParsedStatement

        ddl = "CREATE MULTISET TABLE GDEV1T_GCFR.GCFR_CE_System (Id INTEGER);"
        parsed = ParsedStatement(
            file_path="03_ddl/tables/GDEV1T_GCFR.GCFR_CE_System.tbl",
            ddl_text=ddl,
            original_text=ddl,
            database_name="GDEV1T_GCFR",
            object_name="GCFR_CE_System",
            object_type=ObjectType.TABLE,
            strategy=DeployStrategy.IDEMPOTENT_DEPLOY,
            qualified_name="GDEV1T_GCFR.GCFR_CE_System",
            deploy_intent=DeployIntent.IDEMPOTENT_DEPLOY,
        )

        cur = _TableTriggerCursor()
        result = _deploy_table(cur, parsed, dry_run=False)

        assert result.state == DeployState.FAILED
        assert "table has defined triggers" in result.message
        assert result.blockers == [
            "GCFR_TU_CE_System_Audit (AFTER UPDATE, ENABLED) "
            "on GDEV1T_GCFR.GCFR_CE_System"
        ]
        executed_sql = "\n".join(sql for sql, _params in cur.executed)
        assert "RENAME TABLE" not in executed_sql
        assert "DROP TABLE" not in executed_sql
        assert "SELECT TOP 1" not in executed_sql

    def test_deploy_table_can_recreate_triggers_when_explicitly_enabled(self):
        from database_package_deployer.deployer import _deploy_table
        from database_package_deployer.models import ParsedStatement

        ddl = "CREATE MULTISET TABLE GDEV1T_GCFR.GCFR_CE_System (Id INTEGER);"
        parsed = ParsedStatement(
            file_path="03_ddl/tables/GDEV1T_GCFR.GCFR_CE_System.tbl",
            ddl_text=ddl,
            original_text=ddl,
            database_name="GDEV1T_GCFR",
            object_name="GCFR_CE_System",
            object_type=ObjectType.TABLE,
            strategy=DeployStrategy.IDEMPOTENT_DEPLOY,
            qualified_name="GDEV1T_GCFR.GCFR_CE_System",
            deploy_intent=DeployIntent.IDEMPOTENT_DEPLOY,
        )

        cur = _TableTriggerCursor()
        result = _deploy_table(
            cur,
            parsed,
            dry_run=False,
            table_trigger_action="recreate",
        )

        assert result.state == DeployState.COMPLETED
        executed_sql = [sql for sql, _params in cur.executed]
        assert any(sql.startswith("SHOW TRIGGER") for sql in executed_sql)
        assert any(sql.startswith("DROP TRIGGER") for sql in executed_sql)
        assert any(sql.startswith('DROP TABLE "GDEV1T_GCFR"') for sql in executed_sql)
        assert any(
            sql.startswith("CREATE MULTISET TABLE GDEV1T_GCFR.GCFR_CE_System")
            for sql in executed_sql
        )
        assert any(sql.startswith("REPLACE TRIGGER") for sql in executed_sql)
        assert any("Dropped and recreated trigger" in w for w in result.warnings)


class TestExecuteDdl:
    """Unit tests for _execute_ddl's statement-splitting logic.

    Uses a recording cursor stub so no real database connection is needed.
    """

    def _run(self, sql: str) -> list[str]:
        from database_package_deployer.deployer import _execute_ddl

        cur = _RecordingCursor()
        _execute_ddl(cur, sql)
        return cur.executed

    def test_single_statement(self):
        stmts = self._run("SELECT 1;")
        assert stmts == ["SELECT 1"]

    def test_two_statements(self):
        sql = "INSERT INTO t (x) VALUES (1);\nINSERT INTO t (x) VALUES (2);"
        stmts = self._run(sql)
        assert len(stmts) == 2
        assert "VALUES (1)" in stmts[0]
        assert "VALUES (2)" in stmts[1]

    def test_semicolon_inside_string_literal_not_split(self):
        """Semicolons embedded in string literals must not split the statement.

        Regression for issue #73 — the VALUES string contains ';' which was
        previously treated as a statement terminator, producing two broken
        fragments instead of two complete INSERT statements.
        """
        sql = (
            "INSERT INTO t (cd, desc_col) VALUES ('A', 'Fixed rate; stable');\n"
            "INSERT INTO t (cd, desc_col) VALUES ('B', 'ARM; rate resets');"
        )
        stmts = self._run(sql)
        assert len(stmts) == 2, f"Expected 2 statements, got {len(stmts)}: {stmts}"
        assert "Fixed rate; stable" in stmts[0]
        assert "ARM; rate resets" in stmts[1]

    def test_semicolon_inside_block_comment_not_split(self):
        """Semicolons inside block comments must not split the statement."""
        sql = "/* step 1; do this */ INSERT INTO t VALUES (1);"
        stmts = self._run(sql)
        assert len(stmts) == 1
        assert "INSERT INTO t VALUES (1)" in stmts[0]

    def test_semicolon_inside_line_comment_not_split(self):
        """Semicolons on a -- comment line must not split the statement."""
        sql = "INSERT INTO t VALUES (1); -- end; of statement"
        stmts = self._run(sql)
        assert len(stmts) == 1

    def test_doubled_quote_inside_literal(self):
        """Teradata escaped quote (doubled '') inside a literal must not
        prematurely close the string, leaving a trailing semicolon exposed."""
        sql = "INSERT INTO t (x) VALUES ('it''s fine; really');"
        stmts = self._run(sql)
        assert len(stmts) == 1
        assert "it''s fine; really" in stmts[0]

    def test_no_trailing_semicolon(self):
        """Content without a trailing semicolon is still executed."""
        stmts = self._run("INSERT INTO t VALUES (1)")
        assert len(stmts) == 1

    def test_double_hyphen_inside_string_literal_not_treated_as_comment(self):
        """A '--' sequence inside a string literal must not be treated as a
        SQL single-line comment.

        Regression test for the sanitisation order bug: when comments were
        blanked BEFORE string literals, a '--' inside a quoted string caused
        the comment stripper to swallow the rest of the line including the
        closing quote, leaving the string unterminated (Teradata Error 3760
        "String not terminated before end of text").

        The correct order is: blank string literals first, then comments.
        """
        sql = (
            "INSERT INTO {{DB_MEMORY_T}}.Design_Decision\n"
            "( decision_id, title )\n"
            "VALUES\n"
            "( 'DD-001', 'Session strategy - persistent sessions' );\n"
            "\n"
            "INSERT INTO {{DB_MEMORY_T}}.Design_Decision\n"
            "( decision_id, title )\n"
            "VALUES\n"
            "( 'DD-002', 'Retention policy - 90 day sessions' );\n"
        )
        stmts = self._run(sql)
        assert len(stmts) == 2, (
            f"Expected 2 statements but got {len(stmts)}: "
            f"the '--' inside the string literal was incorrectly treated "
            f"as a SQL comment, swallowing the closing quote."
        )
        assert "Session strategy - persistent sessions" in stmts[0]
        assert "Retention policy - 90 day sessions" in stmts[1]

    def test_semicolon_inside_multiline_string_literal_not_split(self):
        """A semicolon inside a multi-line string literal (e.g. a stored SQL
        template in a Query_Cookbook seed row) must not split the statement.

        The sql_template column stores complete SQL queries as string values.
        Without correct sanitisation order, the semicolon terminating the
        embedded query closes the INSERT prematurely, causing Error 3760.
        """
        sql = (
            "INSERT INTO {{DB_MEMORY_T}}.Query_Cookbook\n"
            "( recipe_id, sql_template )\n"
            "VALUES\n"
            "( 'QC-001'\n"
            ", 'SELECT a.agent_name\n"
            "  FROM {{DB_DOMAIN_BUS_V}}.Agent_Current a\n"
            "  ORDER BY a.agent_name;'\n"
            ");\n"
        )
        stmts = self._run(sql)
        assert len(stmts) == 1, (
            f"Expected 1 statement but got {len(stmts)}: "
            f"the semicolon inside the multi-line sql_template string was "
            f"incorrectly treated as a statement terminator."
        )
        assert "ORDER BY a.agent_name;" in stmts[0]

    def test_split_disabled_keeps_procedure_body_as_one_request(self):
        """Stored procedure bodies contain semicolons inside one DDL request."""
        from database_package_deployer.deployer import _execute_ddl

        sql = (
            "CREATE PROCEDURE MyDB.P (OUT oActivity_Count INTEGER)\n"
            "BEGIN\n"
            "    DECLARE vSQL_Text VARCHAR(1000);\n"
            "    SET vSQL_Text = 'UPDATE MyDB.T SET c = 1;';\n"
            "    CALL DBC.SYSEXECSQL(vSQL_Text);\n"
            "    SET oActivity_Count = ACTIVITY_COUNT;\n"
            "END;"
        )

        cur = _RecordingCursor()
        _execute_ddl(cur, sql, split_statements=False)

        assert len(cur.executed) == 1
        assert "DECLARE vSQL_Text" in cur.executed[0]
        assert "CALL DBC.SYSEXECSQL(vSQL_Text);" in cur.executed[0]
        assert "'UPDATE MyDB.T SET c = 1;'" in cur.executed[0]
        assert cur.executed[0].endswith("END;")

    def test_execute_parsed_ddl_keeps_procedure_body_as_one_request(self):
        """Procedure deployment uses object metadata to avoid semicolon splitting."""
        from database_package_deployer.deployer import _execute_parsed_ddl
        from database_package_deployer.models import ParsedStatement

        sql = (
            "CREATE PROCEDURE MyDB.P (OUT oActivity_Count INTEGER)\n"
            "BEGIN\n"
            "    SET oActivity_Count = 0;\n"
            "END;"
        )
        parsed = ParsedStatement(
            file_path="03_ddl/procedures/MyDB.P.spl",
            ddl_text=sql,
            original_text=sql,
            database_name="MyDB",
            object_name="P",
            object_type=ObjectType.PROCEDURE,
            strategy=DeployStrategy.REPLACE_IN_PLACE,
            qualified_name="MyDB.P",
            deploy_intent=DeployIntent.CREATE_ONLY,
        )

        cur = _RecordingCursor()
        _execute_parsed_ddl(cur, parsed)

        assert cur.executed == [sql]

    def test_execute_parsed_ddl_keeps_macro_body_as_one_request(self):
        """Macro deployment uses object metadata to avoid semicolon splitting."""
        from database_package_deployer.deployer import _execute_parsed_ddl
        from database_package_deployer.models import ParsedStatement

        sql = (
            "REPLACE MACRO MyDB.M (Id INTEGER)\n"
            "AS\n"
            "(\n"
            "    SELECT * FROM OtherDB.T WHERE Id = :Id;\n"
            "    UPDATE OtherDB.T SET Updated_Flag = 1 WHERE Id = :Id;\n"
            ");"
        )
        parsed = ParsedStatement(
            file_path="03_ddl/macros/MyDB.M.mcr",
            ddl_text=sql,
            original_text=sql,
            database_name="MyDB",
            object_name="M",
            object_type=ObjectType.MACRO,
            strategy=DeployStrategy.REPLACE_IN_PLACE,
            qualified_name="MyDB.M",
            deploy_intent=DeployIntent.REPLACE_WITH_BACKUP,
        )

        cur = _RecordingCursor()
        _execute_parsed_ddl(cur, parsed)

        assert cur.executed == [sql]


class TestSqljClientFilePathResolution:
    """SQLJ JAR install paths resolve relative to the .sjr script."""

    def test_relative_jar_path_resolves_against_script_directory(self, tmp_path):
        from database_package_deployer.deployer import _resolve_sqlj_client_file_paths

        script = tmp_path / "DDL" / "jar_install" / "install.sjr"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")

        sql = "CALL SQLJ.INSTALL_JAR('CJ!./GCFR_QB.jar', 'GCFR_QB', 0);"
        resolved = _resolve_sqlj_client_file_paths(sql, str(script))

        expected = (script.parent / "GCFR_QB.jar").resolve().as_posix()
        assert f"'CJ!{expected}'" in resolved

    def test_deploy_direct_execute_rewrites_jar_path_only_for_execution(self, tmp_path):
        from database_package_deployer.deployer import _deploy_direct_execute
        from database_package_deployer.models import DeployIntent, ParsedStatement

        script = tmp_path / "DDL" / "jar_install" / "install.sjr"
        script.parent.mkdir(parents=True)
        ddl = "CALL SQLJ.INSTALL_JAR('CJ!./ExecLargeSqlJ.jar', 'JAR_EXECUTE_LARGE_SQL', 0);"

        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="",
            object_name="JAR",
            object_type=ObjectType.JAR,
            strategy=DeployStrategy.DIRECT_EXECUTE,
            qualified_name="JAR",
            deploy_intent=DeployIntent.DIRECT_EXECUTE,
        )

        cur = _RecordingCursor()
        _deploy_direct_execute(cur, parsed, dry_run=False)

        expected = (script.parent / "ExecLargeSqlJ.jar").resolve().as_posix()
        assert cur.executed == [
            f"CALL SQLJ.INSTALL_JAR('CJ!{expected}', 'JAR_EXECUTE_LARGE_SQL', 0)"
        ]

    def test_replace_jar_missing_falls_back_to_install_jar(self, tmp_path):
        from database_package_deployer.deployer import _deploy_direct_execute
        from database_package_deployer.models import DeployIntent, ParsedStatement

        script = tmp_path / "DDL" / "jar_install" / "replace.sjr"
        script.parent.mkdir(parents=True)
        ddl = "CALL SQLJ.REPLACE_JAR('CJ!./GCFR_QB.jar', 'GCFR_QB');"
        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="",
            object_name="JAR",
            object_type=ObjectType.JAR,
            strategy=DeployStrategy.DIRECT_EXECUTE,
            qualified_name="JAR",
            deploy_intent=DeployIntent.DIRECT_EXECUTE,
        )

        cur = _FailOnceCursor("[Error 9999] Jar 'GCFR_QB' does not exist")
        result = _deploy_direct_execute(cur, parsed, dry_run=False)

        expected = (script.parent / "GCFR_QB.jar").resolve().as_posix()
        assert result.state == DeployState.COMPLETED
        assert result.prior_existed is False
        assert cur.executed == [
            f"CALL SQLJ.REPLACE_JAR('CJ!{expected}', 'GCFR_QB')",
            f"CALL SQLJ.INSTALL_JAR('CJ!{expected}', 'GCFR_QB', 0)",
        ]

    def test_replace_jar_fallback_targets_missing_alias_in_multi_call_file(
        self, tmp_path
    ):
        from database_package_deployer.deployer import _deploy_direct_execute
        from database_package_deployer.models import DeployIntent, ParsedStatement

        script = tmp_path / "DDL" / "jar_install" / "replace.sjr"
        script.parent.mkdir(parents=True)
        ddl = "\n".join(
            [
                "DATABASE GDEV1P_UT;",
                "CALL SQLJ.REPLACE_JAR('CJ!./ExecLargeSqlJ.jar', 'JAR_EXECUTE_LARGE_SQL');",
                "CALL SQLJ.REPLACE_JAR('CJ!./ExecLargeNOSSqlJ.jar', 'JAR_EXECUTE_LARGE_NOS_SQL');",
            ]
        )
        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="",
            object_name="JAR",
            object_type=ObjectType.JAR,
            strategy=DeployStrategy.DIRECT_EXECUTE,
            qualified_name="JAR",
            deploy_intent=DeployIntent.DIRECT_EXECUTE,
        )

        class _FailOnMissingAliasCursor(_RecordingCursor):
            def __init__(self):
                super().__init__()
                self._failed = False

            def execute(self, sql):
                super().execute(sql)
                if (
                    not self._failed
                    and "REPLACE_JAR" in sql
                    and "JAR_EXECUTE_LARGE_NOS_SQL" in sql
                ):
                    self._failed = True
                    raise Exception(
                        "[Error 7972] Jar "
                        "'GDEV1P_UT.JAR_EXECUTE_LARGE_NOS_SQL' does not exist."
                    )

        cur = _FailOnMissingAliasCursor()
        result = _deploy_direct_execute(cur, parsed, dry_run=False)

        jar_one = (script.parent / "ExecLargeSqlJ.jar").resolve().as_posix()
        jar_two = (script.parent / "ExecLargeNOSSqlJ.jar").resolve().as_posix()
        assert result.state == DeployState.COMPLETED
        retry_sql = "\n".join(cur.executed[3:])
        assert (
            f"CALL SQLJ.REPLACE_JAR('CJ!{jar_one}', 'JAR_EXECUTE_LARGE_SQL')"
            in retry_sql
        )
        assert (
            f"CALL SQLJ.INSTALL_JAR('CJ!{jar_two}', 'JAR_EXECUTE_LARGE_NOS_SQL', 0)"
            in retry_sql
        )
        assert (
            f"CALL SQLJ.INSTALL_JAR('CJ!{jar_one}', 'JAR_EXECUTE_LARGE_SQL', 0)"
            not in retry_sql
        )

    def test_install_jar_already_exists_still_fails(self, tmp_path):
        from database_package_deployer.deployer import _deploy_direct_execute
        from database_package_deployer.models import DeployIntent, ParsedStatement

        script = tmp_path / "DDL" / "jar_install" / "install.sjr"
        script.parent.mkdir(parents=True)
        ddl = "CALL SQLJ.INSTALL_JAR('CJ!./GCFR_QB.jar', 'GCFR_QB', 0);"
        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="",
            object_name="JAR",
            object_type=ObjectType.JAR,
            strategy=DeployStrategy.DIRECT_EXECUTE,
            qualified_name="JAR",
            deploy_intent=DeployIntent.DIRECT_EXECUTE,
        )

        cur = _FailAlwaysCursor("[Error 7971] Jar 'GCFR_QB' already exists")

        with pytest.raises(Exception, match="already exists"):
            _deploy_direct_execute(cur, parsed, dry_run=False)


class TestExternalNameClientFilePathResolution:
    """C/C++ EXTERNAL NAME paths resolve relative to the deploy script."""

    def test_cpp_external_name_path_resolves_against_script_directory(self, tmp_path):
        from database_package_deployer.deployer import (
            _resolve_external_name_client_file_paths,
        )

        script = tmp_path / "DDL" / "procedures" / "raise.spl"
        script.parent.mkdir(parents=True)
        ddl = (
            "REPLACE PROCEDURE DB.raise_error()\n"
            "LANGUAGE CPP\n"
            "EXTERNAL NAME 'CS!RaiseException!./RaiseException.cpp!F!RaiseException';"
        )

        resolved = _resolve_external_name_client_file_paths(ddl, str(script))

        expected = (script.parent / "RaiseException.cpp").resolve().as_posix()
        assert (
            f"EXTERNAL NAME 'CS!RaiseException!{expected}!F!RaiseException'" in resolved
        )

    def test_deploy_direct_execute_rewrites_cpp_external_path_only_for_execution(
        self, tmp_path
    ):
        from database_package_deployer.deployer import _deploy_direct_execute
        from database_package_deployer.models import DeployIntent, ParsedStatement

        script = tmp_path / "DDL" / "procedures" / "raise.spl"
        script.parent.mkdir(parents=True)
        ddl = (
            "REPLACE PROCEDURE DB.raise_error()\n"
            "LANGUAGE CPP\n"
            "EXTERNAL NAME 'CS!RaiseException!./RaiseException.cpp!F!RaiseException';"
        )
        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="DB",
            object_name="raise_error",
            object_type=ObjectType.PROCEDURE,
            strategy=DeployStrategy.REPLACE_IN_PLACE,
            qualified_name="DB.raise_error",
            deploy_intent=DeployIntent.REPLACE_WITH_BACKUP,
        )

        cur = _RecordingCursor()
        _deploy_direct_execute(cur, parsed, dry_run=False)

        expected = (script.parent / "RaiseException.cpp").resolve().as_posix()
        assert f"CS!RaiseException!{expected}!F!RaiseException" in cur.executed[0]

    def test_execute_parsed_ddl_rewrites_cpp_external_path_for_replace_strategy(
        self, tmp_path
    ):
        from database_package_deployer.deployer import _execute_parsed_ddl
        from database_package_deployer.models import ParsedStatement

        script = tmp_path / "DDL" / "procedures" / "raise.spl"
        script.parent.mkdir(parents=True)
        ddl = (
            "REPLACE PROCEDURE DB.raise_error()\n"
            "LANGUAGE CPP\n"
            "EXTERNAL NAME 'CS!RaiseException!./RaiseException.cpp!F!RaiseException';"
        )
        parsed = ParsedStatement(
            file_path=str(script),
            ddl_text=ddl,
            original_text=ddl,
            database_name="DB",
            object_name="raise_error",
            object_type=ObjectType.PROCEDURE,
            strategy=DeployStrategy.REPLACE_IN_PLACE,
            qualified_name="DB.raise_error",
            deploy_intent=DeployIntent.REPLACE_WITH_BACKUP,
        )

        cur = _RecordingCursor()
        _execute_parsed_ddl(cur, parsed)

        expected = (script.parent / "RaiseException.cpp").resolve().as_posix()
        assert f"CS!RaiseException!{expected}!F!RaiseException" in cur.executed[0]
