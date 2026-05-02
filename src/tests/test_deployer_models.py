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

from ddl_deployer.models import (
    ObjectType,
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
