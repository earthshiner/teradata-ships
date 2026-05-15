"""Tests for deploy-time hierarchy PERM capacity preflight."""

from unittest.mock import MagicMock

from database_package_deployer.models import (
    DeployStrategy,
    ObjectType,
    ParsedStatement,
)
from database_package_deployer.preflight import _check_parent_dependencies


def _parsed_database(name: str, parent: str, perm: str) -> ParsedStatement:
    ddl = f"create database {name} from {parent} as perm = {perm};"
    return ParsedStatement(
        file_path=f"{name}.db",
        ddl_text=ddl,
        original_text=ddl,
        database_name=name,
        object_name=name,
        object_type=ObjectType.DATABASE,
        strategy=DeployStrategy.DIRECT_EXECUTE,
        qualified_name=name,
    )


def test_internal_parent_perm_capacity_passes():
    """Package-created parents are checked against immediate children."""
    cursor = MagicMock()
    parsed = [
        _parsed_database("PARENT", "DBC", "100e6"),
        _parsed_database("CHILD1", "PARENT", "40e6"),
        _parsed_database("CHILD2", "PARENT", "50e6"),
    ]

    checks = _check_parent_dependencies(cursor, parsed, {"PARENT", "CHILD1", "CHILD2"})
    capacity = [c for c in checks if c.check_name == "database_hierarchy_perm_capacity"]

    assert capacity
    assert capacity[0].passed
    assert capacity[0].severity == "INFO"
    assert "headroom" in capacity[0].message


def test_internal_parent_perm_capacity_fails():
    """An over-allocated package-created parent blocks preflight."""
    cursor = MagicMock()
    parsed = [
        _parsed_database("PARENT", "DBC", "100e6"),
        _parsed_database("CHILD1", "PARENT", "75e6"),
        _parsed_database("CHILD2", "PARENT", "50e6"),
    ]

    checks = _check_parent_dependencies(cursor, parsed, {"PARENT", "CHILD1", "CHILD2"})
    capacity = [c for c in checks if c.check_name == "database_hierarchy_perm_capacity"]

    assert capacity
    assert not capacity[0].passed
    assert capacity[0].severity == "ERROR"
    assert "direct child PERM requires" in capacity[0].message
