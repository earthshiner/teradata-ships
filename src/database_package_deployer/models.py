"""
models.py — Data models for the DDL Deployer.

Defines the object type classification, deployment strategies,
state machine, column metadata structures, schema compatibility
results, pre-flight check outcomes, and deployment result records.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, List


class DeployScope(Enum):
    """
    Deployment scope — system-level vs environment-level.

    SYSTEM objects (Maps, Roles, Profiles, Authorisations,
    Foreign Servers) are identical across all environments on a
    Teradata system. They have no database qualifier, no tokens,
    and deploy once per system using SKIP_IF_EXISTS semantics.

    ENVIRONMENT objects (Databases, Tables, Views, Grants, etc.)
    are environment-specific, token-substituted, and deploy once
    per target environment.
    """

    SYSTEM = "SYSTEM"
    ENVIRONMENT = "ENVIRONMENT"


class ObjectType(Enum):
    """
    Classification of Teradata DDL objects by deployment strategy.
    """

    # -- Environment-scoped DDL objects --
    TABLE = "TABLE"
    JOIN_INDEX = "JOIN_INDEX"
    HASH_INDEX = "HASH_INDEX"
    INDEX = "INDEX"
    VIEW = "VIEW"
    MACRO = "MACRO"
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    TRIGGER = "TRIGGER"
    DATABASE = "DATABASE"
    USER = "USER"
    GRANT = "GRANT"
    REVOKE = "REVOKE"
    JAR = "JAR"
    SCRIPT_TABLE_OPERATOR = "SCRIPT_TABLE_OPERATOR"
    # Data Manipulation scripts: INSERT / UPDATE / DELETE / MERGE.
    # Carried in the package (typically under ``DML/``) and executed
    # after all DDL so the target tables exist before data is loaded.
    DML = "DML"
    # Ordered mixed SQL scripts preserve source choreography such as
    # GRANT -> action -> REVOKE and always execute as written.
    ORDERED_SQL = "ORDERED_SQL"

    # Foreign key constraint scripts: ALTER TABLE ... ADD FOREIGN KEY.
    # Carried under ``DDL/alters/`` and executed after all tables and
    # indexes so both the referencing and referenced tables exist.
    FOREIGN_KEY = "FOREIGN_KEY"

    # Statistics collection scripts: COLLECT [SUMMARY] STATISTICS ... ON db.table
    # or UPDATE STATISTICS (Teradata synonym). Carried under ``DDL/statistics/``
    # and executed after tables AND indexes so the optimiser has accurate
    # data (including indexed column statistics) before views compile.
    STATISTICS = "STATISTICS"

    # COMMENT ON TABLE/VIEW/COLUMN/MACRO/PROCEDURE/FUNCTION scripts.
    # Carried under ``DDL/comments/`` and executed after all objects
    # exist so every target of a COMMENT ON statement is present.
    COMMENT = "COMMENT"

    # C source and header files for C/C++ UDFs. These are compiled into
    # a JAR before deployment — they are NOT executed directly against
    # Teradata. Carried for traceability only.
    C_SOURCE = "C_SOURCE"
    C_HEADER = "C_HEADER"

    # -- System-scoped objects (no database qualifier, no tokens) --
    MAP = "MAP"
    ROLE = "ROLE"
    PROFILE = "PROFILE"
    AUTHORIZATION = "AUTHORIZATION"
    FOREIGN_SERVER = "FOREIGN_SERVER"

    UNKNOWN = "UNKNOWN"


class DeployStrategy(Enum):
    """
    Deployment strategy derived from ObjectType.

    IDEMPOTENT_DEPLOY — Full backup/migrate flow (tables only).
    DROP_AND_CREATE   — DROP if exists, then CREATE (join/hash
                        indexes, secondary indexes).
    REPLACE_IN_PLACE  — Execute as-is; the REPLACE keyword handles
                        idempotency (views, macros, procedures,
                        functions).
    CREATE_ONLY       — Execute CREATE; fail if the object already
                        exists. Developer intent: this object is new.
    DIRECT_EXECUTE    — Execute the DDL as-is with no pre-checks.
                        Used for pre-requisites (databases, users)
                        and DCL (grants, revokes).
    SKIP_IF_EXISTS    — Check existence first; skip silently if
                        already present. Used for system-scope
                        objects (maps, roles, profiles,
                        authorisations, foreign servers) that are
                        identical across environments.
    NOT_DEPLOYED      — Carried in the package for traceability
                        but not executed by SHIPS. Used for
                        supporting artefacts (.c, .h source files)
                        referenced by EXTERNAL NAME clauses.
    """

    IDEMPOTENT_DEPLOY = "IDEMPOTENT_DEPLOY"
    DROP_AND_CREATE = "DROP_AND_CREATE"
    REPLACE_IN_PLACE = "REPLACE_IN_PLACE"
    CREATE_ONLY = "CREATE_ONLY"
    DIRECT_EXECUTE = "DIRECT_EXECUTE"
    SKIP_IF_EXISTS = "SKIP_IF_EXISTS"
    NOT_DEPLOYED = "NOT_DEPLOYED"


class DeployIntent(Enum):
    """
    The developer's deployment intent, inferred from the DDL verb.
    """

    CREATE_ONLY = "CREATE_ONLY"
    REPLACE_WITH_BACKUP = "REPLACE_WITH_BACKUP"
    IDEMPOTENT_DEPLOY = "IDEMPOTENT_DEPLOY"
    DROP_AND_CREATE = "DROP_AND_CREATE"
    DIRECT_EXECUTE = "DIRECT_EXECUTE"
    SKIP_IF_EXISTS = "SKIP_IF_EXISTS"
    NOT_DEPLOYED = "NOT_DEPLOYED"


# -- SHOW commands for capturing existing definitions before replacement --
SHOW_COMMAND_MAP = {
    ObjectType.VIEW: "SHOW VIEW",
    ObjectType.MACRO: "SHOW MACRO",
    ObjectType.PROCEDURE: "SHOW PROCEDURE",
    ObjectType.FUNCTION: "SHOW SPECIFIC FUNCTION",
    ObjectType.JOIN_INDEX: "SHOW JOIN INDEX",
    ObjectType.TRIGGER: "SHOW TRIGGER",
    ObjectType.TABLE: "SHOW TABLE",
    ObjectType.HASH_INDEX: "SHOW HASH INDEX",
    ObjectType.INDEX: "SHOW INDEX",
}


# -- Map each object type to its deployment strategy --
STRATEGY_MAP = {
    # Environment-scoped objects
    ObjectType.TABLE: DeployStrategy.IDEMPOTENT_DEPLOY,
    ObjectType.JOIN_INDEX: DeployStrategy.DROP_AND_CREATE,
    ObjectType.HASH_INDEX: DeployStrategy.DROP_AND_CREATE,
    ObjectType.INDEX: DeployStrategy.DROP_AND_CREATE,
    ObjectType.VIEW: DeployStrategy.REPLACE_IN_PLACE,
    ObjectType.MACRO: DeployStrategy.REPLACE_IN_PLACE,
    ObjectType.PROCEDURE: DeployStrategy.REPLACE_IN_PLACE,
    ObjectType.FUNCTION: DeployStrategy.REPLACE_IN_PLACE,
    ObjectType.TRIGGER: DeployStrategy.DROP_AND_CREATE,
    ObjectType.DATABASE: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.USER: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.GRANT: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.REVOKE: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.JAR: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.SCRIPT_TABLE_OPERATOR: DeployStrategy.REPLACE_IN_PLACE,
    ObjectType.DML: DeployStrategy.DIRECT_EXECUTE,
    ObjectType.ORDERED_SQL: DeployStrategy.DIRECT_EXECUTE,
    # FK alters are executed as-is.
    ObjectType.FOREIGN_KEY: DeployStrategy.DIRECT_EXECUTE,
    # COLLECT / UPDATE STATISTICS execute as-is after tables exist.
    ObjectType.STATISTICS: DeployStrategy.DIRECT_EXECUTE,
    # COMMENT ON executes as-is after all objects exist.
    ObjectType.COMMENT: DeployStrategy.DIRECT_EXECUTE,
    # C source/header files are compiled into JARs — not executed against Teradata.
    ObjectType.C_SOURCE: DeployStrategy.NOT_DEPLOYED,
    ObjectType.C_HEADER: DeployStrategy.NOT_DEPLOYED,
    # System-scoped objects — skip silently if already present
    ObjectType.MAP: DeployStrategy.SKIP_IF_EXISTS,
    ObjectType.ROLE: DeployStrategy.SKIP_IF_EXISTS,
    ObjectType.PROFILE: DeployStrategy.SKIP_IF_EXISTS,
    ObjectType.AUTHORIZATION: DeployStrategy.SKIP_IF_EXISTS,
    ObjectType.FOREIGN_SERVER: DeployStrategy.SKIP_IF_EXISTS,
}

# -- Map each object type to its deployment scope --
SCOPE_MAP = {
    # System-level: identical across environments, no tokens
    ObjectType.MAP: DeployScope.SYSTEM,
    ObjectType.ROLE: DeployScope.SYSTEM,
    ObjectType.PROFILE: DeployScope.SYSTEM,
    ObjectType.AUTHORIZATION: DeployScope.SYSTEM,
    ObjectType.FOREIGN_SERVER: DeployScope.SYSTEM,
    # Environment-level: token-substituted, per-environment
    ObjectType.DATABASE: DeployScope.ENVIRONMENT,
    ObjectType.USER: DeployScope.ENVIRONMENT,
    ObjectType.TABLE: DeployScope.ENVIRONMENT,
    ObjectType.JOIN_INDEX: DeployScope.ENVIRONMENT,
    ObjectType.HASH_INDEX: DeployScope.ENVIRONMENT,
    ObjectType.INDEX: DeployScope.ENVIRONMENT,
    ObjectType.VIEW: DeployScope.ENVIRONMENT,
    ObjectType.MACRO: DeployScope.ENVIRONMENT,
    ObjectType.PROCEDURE: DeployScope.ENVIRONMENT,
    ObjectType.FUNCTION: DeployScope.ENVIRONMENT,
    ObjectType.TRIGGER: DeployScope.ENVIRONMENT,
    ObjectType.GRANT: DeployScope.ENVIRONMENT,
    ObjectType.REVOKE: DeployScope.ENVIRONMENT,
    ObjectType.JAR: DeployScope.ENVIRONMENT,
    ObjectType.SCRIPT_TABLE_OPERATOR: DeployScope.ENVIRONMENT,
    ObjectType.DML: DeployScope.ENVIRONMENT,
    ObjectType.ORDERED_SQL: DeployScope.ENVIRONMENT,
    ObjectType.FOREIGN_KEY: DeployScope.ENVIRONMENT,
    ObjectType.STATISTICS: DeployScope.ENVIRONMENT,
    ObjectType.COMMENT: DeployScope.ENVIRONMENT,
    # C source/header files are system-level build artefacts, not environment-specific.
    ObjectType.C_SOURCE: DeployScope.SYSTEM,
    ObjectType.C_HEADER: DeployScope.SYSTEM,
}

# -- Deployment ordering: objects deployed in this sequence --
# System-level objects first, then pre-requisites, then DDL objects.
DEPLOY_ORDER = {
    # System scope (00_system phase)
    ObjectType.MAP: -10,
    ObjectType.ROLE: -9,
    ObjectType.PROFILE: -9,
    ObjectType.AUTHORIZATION: -8,
    ObjectType.FOREIGN_SERVER: -7,
    # Environment pre-requisites (01_pre_requisites)
    ObjectType.DATABASE: -3,
    ObjectType.USER: -2,
    # DCL (02_dcl)
    ObjectType.GRANT: -1,
    ObjectType.REVOKE: -1,
    # DDL (03_ddl)
    ObjectType.TABLE: 0,
    ObjectType.JOIN_INDEX: 1,
    ObjectType.HASH_INDEX: 1,
    ObjectType.INDEX: 1,
    # FK alters deploy after tables and indexes.
    ObjectType.FOREIGN_KEY: 1,
    # COLLECT STATISTICS runs after tables AND indexes (order 2 > 1) so
    # the optimiser captures indexed column statistics, not just row-level.
    # Must complete before views compile (view query plans use statistics).
    ObjectType.STATISTICS: 2,
    ObjectType.VIEW: 3,
    ObjectType.MACRO: 4,
    ObjectType.PROCEDURE: 4,
    ObjectType.FUNCTION: 4,
    ObjectType.JAR: 4,
    ObjectType.SCRIPT_TABLE_OPERATOR: 5,
    ObjectType.TRIGGER: 6,
    # COMMENT ON runs after every object it might describe exists.
    ObjectType.COMMENT: 7,
    # DML runs last so every target table, view, and trigger that
    # the data load depends on has already been deployed.
    ObjectType.DML: 8,
    # Ordered mixed SQL is explicit source choreography. Keep it with
    # late direct-execute work so the script controls its own sequence.
    ObjectType.ORDERED_SQL: 8,
    # C source/header files are not deployed — order is irrelevant but
    # must be present for coverage enforcement. Use 98 so UNKNOWN (99)
    # remains the definitive last entry.
    ObjectType.C_SOURCE: 98,
    ObjectType.C_HEADER: 98,
    ObjectType.UNKNOWN: 99,
}

# -- DBC.TablesV TableKind codes for existence checks --
TABLE_KIND_MAP = {
    ObjectType.TABLE: "T",
    ObjectType.JOIN_INDEX: "I",
    ObjectType.HASH_INDEX: "N",
    ObjectType.VIEW: "V",
    ObjectType.MACRO: "M",
    ObjectType.PROCEDURE: "P",
    ObjectType.FUNCTION: "F",
    ObjectType.TRIGGER: "G",
    ObjectType.JAR: "D",
}

# -- System-level existence check queries --
# These objects live outside DBC.TablesV and require
# specialised existence checks.
SYSTEM_EXISTENCE_QUERIES = {
    ObjectType.ROLE: ("SELECT 1 FROM DBC.RoleInfoV WHERE RoleName = '{name}'"),
    ObjectType.PROFILE: ("SELECT 1 FROM DBC.ProfileInfoV WHERE ProfileName = '{name}'"),
    ObjectType.MAP: ("SELECT 1 FROM DBC.MapsV WHERE MapName = '{name}'"),
    ObjectType.AUTHORIZATION: (
        "SELECT 1 FROM DBC.AuthorizationsV WHERE AuthorizationName = '{name}'"
    ),
    ObjectType.FOREIGN_SERVER: (
        "SELECT 1 FROM DBC.ForeignServersV WHERE ServerName = '{name}'"
    ),
}

# -- Teradata access rights codes needed per object type --
# Each tuple: (right_code, description).
REQUIRED_RIGHTS = {
    ObjectType.TABLE: [
        ("CT", "CREATE TABLE"),
        ("DT", "DROP TABLE"),
        ("R ", "SELECT"),
        ("I ", "INSERT"),
    ],
    ObjectType.JOIN_INDEX: [
        ("CT", "CREATE TABLE"),  # JIs use CT right
        ("DT", "DROP TABLE"),  # JIs use DT right
        ("R ", "SELECT"),
    ],
    ObjectType.HASH_INDEX: [
        ("CT", "CREATE TABLE"),
        ("DT", "DROP TABLE"),
    ],
    ObjectType.INDEX: [
        ("IX", "CREATE/DROP INDEX"),  # Index-specific right
    ],
    ObjectType.VIEW: [
        ("CV", "CREATE VIEW"),
    ],
    ObjectType.MACRO: [
        ("CM", "CREATE MACRO"),
    ],
    ObjectType.PROCEDURE: [
        ("CP", "CREATE PROCEDURE"),
    ],
    ObjectType.FUNCTION: [
        ("CF", "CREATE FUNCTION"),
    ],
    ObjectType.TRIGGER: [
        ("CT", "CREATE TABLE"),  # Triggers use CT/DT
        ("DT", "DROP TABLE"),
    ],
}


class DeployState(Enum):
    """
    State machine for a single object deployment.

    State flows vary by DeployStrategy — see class docstrings
    on DeployStrategy and ObjectType for details.
    """

    PENDING = "PENDING"
    BACKED_UP = "BACKED_UP"  # Original renamed to backup (tables only)
    DROPPED = "DROPPED"  # Existing object dropped
    CREATED = "CREATED"  # New DDL executed successfully
    MIGRATED = "MIGRATED"  # Data copied from backup to new table
    COMPLETED = "COMPLETED"  # Fully deployed, verified
    SKIPPED = "SKIPPED"  # Incompatible schema — user alerted
    FAILED = "FAILED"  # Error occurred — needs attention
    ROLLED_BACK = "ROLLED_BACK"  # Compensating actions applied


VALID_NEXT_STATES = {
    DeployState.PENDING: {
        DeployState.BACKED_UP,
        DeployState.DROPPED,
        DeployState.CREATED,
        DeployState.FAILED,
    },
    DeployState.BACKED_UP: {
        DeployState.CREATED,
        DeployState.FAILED,
        DeployState.ROLLED_BACK,
    },
    DeployState.DROPPED: {
        DeployState.CREATED,
        DeployState.FAILED,
    },
    DeployState.CREATED: {
        DeployState.MIGRATED,
        DeployState.COMPLETED,
        DeployState.SKIPPED,
        DeployState.FAILED,
        DeployState.ROLLED_BACK,
    },
    DeployState.MIGRATED: {
        DeployState.COMPLETED,
        DeployState.FAILED,
    },
    DeployState.COMPLETED: set(),
    DeployState.SKIPPED: set(),
    DeployState.FAILED: {
        DeployState.BACKED_UP,
        DeployState.DROPPED,
        DeployState.CREATED,
        DeployState.MIGRATED,
        DeployState.COMPLETED,
        DeployState.ROLLED_BACK,
    },
    DeployState.ROLLED_BACK: set(),
}


@dataclass
class ColumnInfo:
    """Metadata for a single column, sourced from DBC.ColumnsV."""

    name: str
    column_type: str
    column_length: int
    nullable: bool
    default_value: Optional[str]
    column_id: int


@dataclass
class CompatibilityResult:
    """Outcome of comparing an old schema (backup) to a new schema."""

    can_migrate: bool
    common_columns: list = field(default_factory=list)
    added_columns: list = field(default_factory=list)
    dropped_columns: list = field(default_factory=list)
    changed_columns: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class PreflightCheck:
    """
    Outcome of a single pre-flight validation check.

    Attributes:
        check_name:  Short identifier (e.g. 'perm_space', 'ct_right').
        passed:      True if the check passed.
        database:    Target database this check applies to.
        message:     Human-readable result description.
        severity:    'ERROR' (blocks deployment) or 'WARNING' (advisory).
    """

    check_name: str
    passed: bool
    database: str
    message: str
    severity: str = "ERROR"


@dataclass
class PreflightResult:
    """
    Aggregate outcome of all pre-flight validation checks.

    Attributes:
        passed:         True if no ERROR-level checks failed.
        checks:         List of individual PreflightCheck results.
        databases:      Set of unique target databases discovered.
        object_count:   Count of DDL objects by ObjectType.
        errors:         Count of ERROR-level failures.
        warnings:       Count of WARNING-level advisories.
    """

    passed: bool
    checks: list = field(default_factory=list)
    databases: list = field(default_factory=list)
    object_count: dict = field(default_factory=dict)
    errors: int = 0
    warnings: int = 0


@dataclass
class ParsedStatement:
    """
    Result of parsing a single DDL file.

    Attributes:
        file_path:          Path to the DDL file.
        ddl_text:           The DDL text (with MULTISET injected if needed).
        original_text:      The unmodified DDL text as read from disc.
        database_name:      Extracted database name.
        object_name:        Extracted object name.
        object_type:        Classified ObjectType.
        strategy:           Derived DeployStrategy.
        qualified_name:     'Database.ObjectName' identifier.
        multiset_injected:  True if MULTISET was auto-injected.
    """

    file_path: str
    ddl_text: str
    original_text: str
    database_name: str
    object_name: str
    object_type: ObjectType
    strategy: DeployStrategy
    qualified_name: str
    multiset_injected: bool = False
    deploy_intent: Optional[DeployIntent] = None


@dataclass
class ObjectDeployResult:
    """
    Outcome of deploying a single DDL object.

    Replaces the previous TableDeployResult — now handles all
    object types with an object_type field.
    """

    database_name: str
    object_name: str
    object_type: ObjectType
    state: DeployState
    deploy_intent: Optional[DeployIntent] = None
    ddl_file: Optional[str] = None
    prior_existed: bool = False
    rollback_file: Optional[str] = None
    # GAP-013: SHA-256 hex digest of the rollback snapshot content.
    # Computed at capture time; verified before restore.
    snapshot_hash: Optional[str] = None
    backup_table: Optional[str] = None
    rows_migrated: int = 0
    message: str = ""
    error: Optional[str] = None
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    dry_run: bool = False
    wave_number: Optional[int] = None
    stream_id: Optional[int] = None
    drift_detected: bool = False
    drift_diff: str = ""


@dataclass
class WaveSummary:
    """
    Summary of a single wave's execution.

    Attributes:
        wave_number:  Wave number (1-based).
        total:        Objects in this wave.
        completed:    Completed count.
        failed:       Failed count.
        skipped:      Skipped count.
        duration_ms:  Wall-clock time for this wave.
    """

    wave_number: int
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: int = 0


@dataclass
class PackageDeployResult:
    """Aggregate outcome of deploying a directory of DDL files."""

    deployment_id: str
    manifest_path: str
    total: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    rolled_back: int = 0
    results: list = field(default_factory=list)
    preflight_result: Optional[PreflightResult] = None
    dry_run: bool = False
    report_path: Optional[str] = None
    num_streams: int = 1
    wave_summaries: List[WaveSummary] = field(default_factory=list)
    prior_completed: list = field(default_factory=list)
    privilege_result: Optional[Any] = None

    @property
    def success(self) -> bool:
        """True if all objects completed or were intentionally skipped."""
        return self.failed == 0 and self.rolled_back == 0

    @property
    def is_wave_parallel(self) -> bool:
        """True if this deployment used wave-parallel execution."""
        return len(self.wave_summaries) > 0

    @property
    def is_noop_redeploy(self) -> bool:
        """
        True if this run deployed nothing new but has prior
        completed objects — i.e. a re-run of an already-deployed
        package where all objects still exist in the database.
        """
        return len(self.results) == 0 and len(self.prior_completed) > 0
