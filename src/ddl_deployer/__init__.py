"""
ddl_deployer — Idempotent Teradata DDL Deployment with Restartability
=====================================================================

Deploys all Teradata DDL object types idempotently:

    Tables        — Backup, create, schema compare, data migration.
    Join Indexes  — DROP if exists, CREATE.
    Hash Indexes  — DROP if exists, CREATE.
    Sec. Indexes  — DROP INDEX if exists, CREATE INDEX.
    Triggers      — DROP if exists, CREATE.
    Views         — REPLACE VIEW (inherently idempotent).
    Macros        — REPLACE MACRO (inherently idempotent).
    Procedures    — REPLACE PROCEDURE (inherently idempotent).
    Functions     — REPLACE FUNCTION (inherently idempotent).

Mandatory pre-flight validation checks permissions, perm space,
and database existence before any DDL is executed.

Runs as a standalone CLI or as an MCP Server tool.
"""

__version__ = "2.0.0"

from ddl_deployer.models import (
    ObjectType,
    DeployStrategy,
    DeployState,
    ColumnInfo,
    CompatibilityResult,
    PreflightCheck,
    PreflightResult,
    ParsedDDL,
    ObjectDeployResult,
    PackageDeployResult,
)
from ddl_deployer.deployer import (
    deploy_single,
    deploy_package,
    resume_package,
    rollback_package,
)
