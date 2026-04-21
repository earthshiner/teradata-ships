"""
schema_comparator.py — Schema comparison and migration compatibility.

Queries DBC.ColumnsV to retrieve column metadata for two tables
(backup and new), compares them, and produces a CompatibilityResult
indicating whether automatic data migration is safe.

Compatibility Rules:
    - New column is NULLABLE                    → safe, insert NULL
    - New column is NOT NULL with DEFAULT        → safe, insert DEFAULT
    - New column is NOT NULL without DEFAULT      → BLOCKER
    - Column type widened (e.g. VARCHAR(50→100)) → safe
    - Column type narrowed                       → BLOCKER (truncation risk)
    - Column type changed incompatibly           → BLOCKER
    - Column dropped from new schema             → safe, with WARNING (data loss)
"""

import logging
from typing import Dict, List

from ddl_deployer.models import ColumnInfo, CompatibilityResult

logger = logging.getLogger(__name__)

# -- Teradata type codes that share a numeric family (safe to widen) --
# Within a family, migration is safe if the new length >= old length.
_NUMERIC_FAMILY = {'I1', 'I2', 'I', 'I8', 'F', 'D'}     # Integer/float family
_STRING_FAMILY = {'CF', 'CV', 'UC', 'UV', 'LF', 'LV'}    # Char/varchar/clob family
_DATETIME_FAMILY = {'DA', 'TS', 'TZ', 'SZ', 'AT', 'TI'}  # Date/time family
_BYTE_FAMILY = {'BF', 'BV', 'BO'}                         # Byte/varbyte/blob family

_TYPE_FAMILIES = [_NUMERIC_FAMILY, _STRING_FAMILY, _DATETIME_FAMILY, _BYTE_FAMILY]


def get_column_metadata(cursor, database_name: str, table_name: str) -> List[ColumnInfo]:
    """
    Retrieve column metadata from DBC.ColumnsV for a given table.

    Args:
        cursor:         An active Teradata database cursor.
        database_name:  The database containing the table.
        table_name:     The table name.

    Returns:
        List of ColumnInfo objects ordered by column position.

    Raises:
        RuntimeError: If the query returns no rows (table may not exist
                      or user lacks access).
    """
    sql = """
        SELECT
             TRIM(ColumnName)  AS ColumnName
            ,TRIM(ColumnType)  AS ColumnType
            ,ColumnLength
            ,CASE WHEN Nullable = 'Y' THEN 1 ELSE 0 END AS IsNullable
            ,TRIM(DefaultValue) AS DefaultValue
            ,ColumnId
        FROM DBC.ColumnsV
        WHERE DatabaseName = ?
          AND TableName = ?
        ORDER BY ColumnId
    """
    cursor.execute(sql, [database_name, table_name])
    rows = cursor.fetchall()

    if not rows:
        raise RuntimeError(
            f"No columns found for {database_name}.{table_name} in DBC.ColumnsV. "
            "The table may not exist or the user may lack SELECT access."
        )

    columns = []
    for row in rows:
        columns.append(ColumnInfo(
            name=row[0],
            column_type=row[1],
            column_length=row[2],
            nullable=bool(row[3]),
            default_value=row[4] if row[4] else None,
            column_id=row[5],
        ))

    logger.debug(
        "Retrieved %d columns for %s.%s",
        len(columns), database_name, table_name
    )
    return columns


def compare_schemas(
    old_columns: List[ColumnInfo],
    new_columns: List[ColumnInfo],
) -> CompatibilityResult:
    """
    Compare two column sets and assess migration compatibility.

    Produces a CompatibilityResult detailing which columns are common,
    added, dropped, or changed — and whether automatic migration is safe.

    Args:
        old_columns: Column metadata from the backup table.
        new_columns: Column metadata from the newly created table.

    Returns:
        CompatibilityResult with migration feasibility and diagnostics.
    """
    old_by_name: Dict[str, ColumnInfo] = {c.name: c for c in old_columns}
    new_by_name: Dict[str, ColumnInfo] = {c.name: c for c in new_columns}

    old_names = set(old_by_name.keys())
    new_names = set(new_by_name.keys())

    common_names = old_names & new_names
    added_names = new_names - old_names
    dropped_names = old_names - new_names

    # -- Classify common columns: unchanged vs changed --
    common_columns = []
    changed_columns = []

    for name in sorted(common_names):
        old_col = old_by_name[name]
        new_col = new_by_name[name]

        if old_col.column_type == new_col.column_type and old_col.column_length == new_col.column_length:
            common_columns.append(name)
        else:
            changed_columns.append({
                'name': name,
                'old_type': old_col.column_type,
                'old_length': old_col.column_length,
                'new_type': new_col.column_type,
                'new_length': new_col.column_length,
            })

    # -- Assess blockers and warnings --
    blockers = []
    warnings = []
    blocked_column_names = set()  # Track blocked columns explicitly

    # Check added columns for NOT NULL without DEFAULT
    for name in sorted(added_names):
        col = new_by_name[name]
        if not col.nullable and col.default_value is None:
            blockers.append(
                f"Column '{name}' is NOT NULL with no DEFAULT value — "
                f"cannot auto-populate from backup data."
            )

    # Check changed columns for compatibility
    for change in changed_columns:
        name = change['name']
        old_type = change['old_type']
        new_type = change['new_type']
        old_len = change['old_length']
        new_len = change['new_length']

        if old_type == new_type:
            # Same type, check length
            if new_len < old_len:
                blockers.append(
                    f"Column '{name}' narrowed from {old_type}({old_len}) "
                    f"to {old_type}({new_len}) — truncation risk."
                )
                blocked_column_names.add(name)
            else:
                # Widened — safe, but note the change
                warnings.append(
                    f"Column '{name}' widened from {old_type}({old_len}) "
                    f"to {old_type}({new_len})."
                )
        elif _types_in_same_family(old_type, new_type):
            # Same family, different type — might be safe but flag it
            warnings.append(
                f"Column '{name}' type changed within family: "
                f"{old_type}({old_len}) → {new_type}({new_len}). "
                f"Verify data compatibility."
            )
        else:
            # Incompatible type change
            blockers.append(
                f"Column '{name}' type changed incompatibly: "
                f"{old_type}({old_len}) → {new_type}({new_len})."
            )
            blocked_column_names.add(name)

    # Check dropped columns — warning, not blocker
    for name in sorted(dropped_names):
        warnings.append(
            f"Column '{name}' exists in backup but not in new schema — "
            f"data for this column will not be migrated."
        )

    can_migrate = len(blockers) == 0

    # Migratable columns = unchanged + changed-but-not-blocked
    migratable_columns = common_columns + [
        c['name'] for c in changed_columns
        if c['name'] not in blocked_column_names
    ]

    return CompatibilityResult(
        can_migrate=can_migrate,
        common_columns=migratable_columns,
        added_columns=sorted(added_names),
        dropped_columns=sorted(dropped_names),
        changed_columns=changed_columns,
        blockers=blockers,
        warnings=warnings,
    )


def _types_in_same_family(type_a: str, type_b: str) -> bool:
    """
    Check whether two Teradata type codes belong to the same family.

    Types within the same family may be compatible for migration
    (e.g. INTEGER → BIGINT) depending on length.

    Args:
        type_a: First Teradata type code.
        type_b: Second Teradata type code.

    Returns:
        True if both types are in the same family.
    """
    for family in _TYPE_FAMILIES:
        if type_a in family and type_b in family:
            return True
    return False
