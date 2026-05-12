"""
test_type_coverage.py — Enforces that every DDL type the packager can
classify is also understood by the deployer.

If a new file type is added to td_release_packager.classifier (e.g. a new
extension and BASE_TYPE), these tests will fail until a corresponding
ObjectType is added to database_package_deployer.models with entries in
STRATEGY_MAP, SCOPE_MAP, and DEPLOY_ORDER.

This prevents the silent-skip bug seen with .stt files: the packager
placed COLLECT STATISTICS scripts into packages, but the deployer did not
recognise the type and skipped every statistics script at deploy time.

Intentional exclusions (NOT_DEPLOYED types):
    C_SOURCE, C_HEADER — C/C++ source files compiled into JARs before
    deployment. They are present in packages for traceability only and
    are never executed against Teradata.
"""

from __future__ import annotations

import pytest

from td_release_packager.classifier import BASE_TYPES
from database_package_deployer.models import (
    DeployStrategy,
    DEPLOY_ORDER,
    ObjectType,
    SCOPE_MAP,
    STRATEGY_MAP,
)


# ---------------------------------------------------------------------------
# Types intentionally not deployed to Teradata
# ---------------------------------------------------------------------------

# These types are recognised by the packager but are NOT SQL that can be
# executed against Teradata. They must have ObjectType entries in the
# deployer with strategy NOT_DEPLOYED, but are excluded from the
# "must be deployable" assertion.
NOT_DEPLOYED_TYPES: frozenset[str] = frozenset(
    {
        "C_SOURCE",   # C source file — compiled into a JAR, not executed
        "C_HEADER",   # C header file — compiled into a JAR, not executed
    }
)


# ---------------------------------------------------------------------------
# Helper: packager canonical types
# ---------------------------------------------------------------------------


def _canonical_packager_types() -> set[str]:
    """Return the set of canonical (non-alias) types from the packager.

    BASE_TYPES contains both canonical types and sub-type aliases
    (e.g. FUNCTION_SQL → FUNCTION). We want only the canonical ones —
    types that can appear as file extension prefixes in a real package.
    """
    # BASE_TYPES is a set of strings for canonical types.
    # Anything that's not a pure string key in BASE_TYPES is an alias
    # and is resolved to a canonical type by the classifier.
    return {t for t in BASE_TYPES if isinstance(t, str)}


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------


class TestTypeCoverage:
    """Every packager type must have a corresponding deployer ObjectType."""

    def test_all_packager_types_have_deployer_object_type(self):
        """Every BASE_TYPE in the packager maps to an ObjectType in the deployer.

        Failure here means a new packager type was added without a
        corresponding ObjectType in database_package_deployer.models.
        Add the ObjectType and ensure it appears in STRATEGY_MAP,
        SCOPE_MAP, and DEPLOY_ORDER.
        """
        deployer_values = {ot.value for ot in ObjectType}
        packager_types = _canonical_packager_types()

        missing = packager_types - deployer_values - {"UNKNOWN"}
        assert not missing, (
            f"Packager types missing from deployer ObjectType: {sorted(missing)}\n"
            "Add each missing type to database_package_deployer/models.py "
            "with entries in STRATEGY_MAP, SCOPE_MAP, and DEPLOY_ORDER."
        )

    def test_all_packager_types_in_strategy_map(self):
        """Every packager type (except UNKNOWN) has a STRATEGY_MAP entry.

        This ensures the deployer knows HOW to deploy each type, not just
        that the type exists.
        """
        packager_types = _canonical_packager_types()
        strategy_keys = {ot.value for ot in STRATEGY_MAP}

        missing = packager_types - strategy_keys - {"UNKNOWN"}
        assert not missing, (
            f"Packager types missing from STRATEGY_MAP: {sorted(missing)}\n"
            "Add a DeployStrategy entry for each type."
        )

    def test_all_packager_types_in_scope_map(self):
        """Every packager type (except UNKNOWN) has a SCOPE_MAP entry."""
        packager_types = _canonical_packager_types()
        scope_keys = {ot.value for ot in SCOPE_MAP}

        missing = packager_types - scope_keys - {"UNKNOWN"}
        assert not missing, (
            f"Packager types missing from SCOPE_MAP: {sorted(missing)}\n"
            "Add a DeployScope entry for each type."
        )

    def test_all_packager_types_in_deploy_order(self):
        """Every packager type (except UNKNOWN) has a DEPLOY_ORDER entry."""
        packager_types = _canonical_packager_types()
        order_keys = {ot.value for ot in DEPLOY_ORDER}

        missing = packager_types - order_keys - {"UNKNOWN"}
        assert not missing, (
            f"Packager types missing from DEPLOY_ORDER: {sorted(missing)}\n"
            "Add a numeric order entry for each type."
        )

    def test_not_deployed_types_have_correct_strategy(self):
        """Types in NOT_DEPLOYED_TYPES must use DeployStrategy.NOT_DEPLOYED.

        C_SOURCE and C_HEADER are compiled into JARs — confirming they
        are marked NOT_DEPLOYED prevents them from ever being executed
        against Teradata if they appear in a package.
        """
        for type_name in NOT_DEPLOYED_TYPES:
            ot = ObjectType(type_name)
            strategy = STRATEGY_MAP.get(ot)
            assert strategy == DeployStrategy.NOT_DEPLOYED, (
                f"{type_name} should have strategy NOT_DEPLOYED, got {strategy}"
            )

    def test_deployable_types_are_not_marked_not_deployed(self):
        """No deployable packager type should be silently skipped.

        If a type is deployable (not in NOT_DEPLOYED_TYPES) but has
        strategy NOT_DEPLOYED, it will be silently skipped at deploy time —
        exactly the failure mode this test suite guards against.
        """
        packager_types = _canonical_packager_types()
        deployable_types = packager_types - NOT_DEPLOYED_TYPES - {"UNKNOWN"}

        for type_name in deployable_types:
            ot = ObjectType(type_name)
            strategy = STRATEGY_MAP.get(ot)
            assert strategy != DeployStrategy.NOT_DEPLOYED, (
                f"{type_name} is a deployable type but is marked NOT_DEPLOYED. "
                "Update its STRATEGY_MAP entry or add it to NOT_DEPLOYED_TYPES "
                "with a justification comment."
            )

    def test_deploy_order_is_correct_sequence(self):
        """Verify key ordering invariants are maintained.

        These are the sequencing rules SHIPS relies on for correctness:
        - Indexes deploy after tables (indexes need the table structure)
        - FK alters deploy after indexes (both sides must exist)
        - Statistics deploy after indexes (captures indexed column stats)
        - Views deploy after statistics (use optimiser stats at compile time)
        - Comments deploy after all objects they describe
        - DML runs last
        """
        order = DEPLOY_ORDER
        assert order[ObjectType.INDEX] > order[ObjectType.TABLE], \
            "INDEX must deploy after TABLE"
        assert order[ObjectType.FOREIGN_KEY] >= order[ObjectType.INDEX], \
            "FOREIGN_KEY must deploy after INDEX"
        assert order[ObjectType.STATISTICS] > order[ObjectType.INDEX], \
            "STATISTICS must deploy after INDEX (captures indexed column stats)"
        assert order[ObjectType.VIEW] > order[ObjectType.STATISTICS], \
            "VIEW must deploy after STATISTICS"
        assert order[ObjectType.COMMENT] > order[ObjectType.VIEW], \
            "COMMENT must deploy after VIEW (can describe views)"
        assert order[ObjectType.COMMENT] > order[ObjectType.PROCEDURE], \
            "COMMENT must deploy after PROCEDURE (can describe procedures)"
        assert order[ObjectType.DML] > order[ObjectType.TRIGGER], \
            "DML must deploy after TRIGGER"
