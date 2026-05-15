"""
database_package_deployer — Idempotent Teradata DDL Deployment with Restartability
=====================================================================

Deploys all Teradata DDL object types idempotently via
DROP-and-CREATE with pre-flight snapshot for rollback:

    Tables        — Backup, create, schema compare, data migration.
    Join Indexes  — DROP if exists, CREATE.
    Hash Indexes  — DROP if exists, CREATE.
    Sec. Indexes  — DROP INDEX if exists, CREATE INDEX.
    Triggers      — DROP if exists, CREATE.
    Views         — DROP if exists, CREATE (snapshot for rollback).
    Macros        — DROP if exists, CREATE (snapshot for rollback).
    Procedures    — DROP if exists, CREATE (snapshot for rollback).
    Functions     — DROP if exists, CREATE (snapshot for rollback).

The deployer owns idempotency — DDL files use CREATE, never REPLACE.
REPLACE provides no rollback path (silently overwrites without backup).

Mandatory pre-flight validation checks permissions, perm space,
and database existence before any DDL is executed.

Runs as a standalone CLI or as an MCP Server tool.
"""

__version__ = "2.0.2"

from database_package_deployer.models import (
    ObjectType,
    DeployStrategy,
    DeployState,
    ColumnInfo,
    CompatibilityResult,
    PreflightCheck,
    PreflightResult,
    ParsedStatement,
    ObjectDeployResult,
    PackageDeployResult,
)
from database_package_deployer.deployer import (
    deploy_single,
    deploy_package,
    resume_package,
    rollback_package,
)

__all__ = [
    # models
    "ObjectType",
    "DeployStrategy",
    "DeployState",
    "ColumnInfo",
    "CompatibilityResult",
    "PreflightCheck",
    "PreflightResult",
    "ParsedStatement",
    "ObjectDeployResult",
    "PackageDeployResult",
    # deployer
    "deploy_single",
    "deploy_package",
    "resume_package",
    "rollback_package",
]
