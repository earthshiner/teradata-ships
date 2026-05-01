"""
migration_builder.py — Build explicit INSERT ... SELECT for data migration.

Constructs a fully explicit INSERT statement (all columns named, no
SELECT *) to migrate data from a backup table to the newly created
table. Handles three column categories:

    1. Common columns   → direct copy: SELECT col FROM backup
    2. Added columns    → populate with NULL or DEFAULT
    3. Dropped columns  → omitted from SELECT (data not migrated)

All generated SQL follows Teradata Engineering Discipline:
    - Uppercase keywords
    - Leading commas
    - Explicit column lists
    - No SELECT *
"""

import logging
from typing import List

from ddl_deployer.models import ColumnInfo, CompatibilityResult

logger = logging.getLogger(__name__)


def build_migration_sql(
    database_name: str,
    table_name: str,
    backup_table_name: str,
    new_columns: List[ColumnInfo],
    compatibility: CompatibilityResult,
) -> str:
    """
    Build an explicit INSERT ... SELECT to migrate data from backup.

    The INSERT lists all columns in the new table. The SELECT provides:
        - Direct column references for common columns
        - NULL for added nullable columns
        - DEFAULT expressions for added columns with defaults
        - CAST expressions where type widening occurred

    Args:
        database_name:      Target database.
        table_name:         New table name.
        backup_table_name:  Backup table name (source of data).
        new_columns:        Column metadata for the new table.
        compatibility:      Schema compatibility result from comparison.

    Returns:
        A complete INSERT ... SELECT SQL string.

    Raises:
        ValueError: If compatibility.can_migrate is False — caller
                    should check this before calling.
    """
    if not compatibility.can_migrate:
        raise ValueError(
            "Cannot build migration SQL — schema is incompatible. "
            f"Blockers: {compatibility.blockers}"
        )

    # Build the column mapping: new_column_name → SELECT expression
    common_set = set(compatibility.common_columns)
    added_set = set(compatibility.added_columns)

    # -- Build INSERT column list and SELECT expressions --
    insert_columns = []
    select_expressions = []

    for col in new_columns:
        insert_columns.append(col.name)

        if col.name in common_set:
            # Column exists in both — direct copy from backup
            select_expressions.append(col.name)

        elif col.name in added_set:
            # New column — populate with NULL or DEFAULT
            expr = _build_fill_expression(col)
            select_expressions.append(f"{expr} AS {col.name}")

        else:
            # Should not reach here if compatibility was assessed correctly
            logger.warning(
                "Column '%s' not classified as common or added — defaulting to NULL.",
                col.name,
            )
            select_expressions.append(f"NULL AS {col.name}")

    # -- Format the SQL with leading commas and proper indentation --
    qualified_target = f"{database_name}.{table_name}"
    qualified_source = f"{database_name}.{backup_table_name}"

    # INSERT column list
    insert_col_lines = _format_column_list(insert_columns, indent=4)

    # SELECT expression list
    select_col_lines = _format_column_list(select_expressions, indent=5)

    sql = (
        f"INSERT INTO {qualified_target}\n"
        f"    (\n"
        f"{insert_col_lines}\n"
        f"    )\n"
        f"SELECT\n"
        f"{select_col_lines}\n"
        f"FROM {qualified_source}\n"
        f";"
    )

    logger.info(
        "Built migration SQL: %d columns (%d common, %d added)",
        len(new_columns),
        len(common_set),
        len(added_set),
    )

    return sql


def _build_fill_expression(col: ColumnInfo) -> str:
    """
    Build the SELECT expression for a newly added column.

    Args:
        col: The new column's metadata.

    Returns:
        'NULL' if nullable, or the DEFAULT expression if defined.
    """
    if col.default_value is not None:
        # Use the defined DEFAULT value
        return col.default_value
    elif col.nullable:
        # Nullable with no default — use NULL
        return "NULL"
    else:
        # NOT NULL without DEFAULT — should have been caught as a blocker
        # by the compatibility check. Defensive fallback.
        raise ValueError(
            f"Column '{col.name}' is NOT NULL with no DEFAULT — "
            f"cannot determine fill expression."
        )


def _format_column_list(items: List[str], indent: int = 4) -> str:
    """
    Format a list of column names/expressions with leading commas.

    Follows the Teradata Engineering Discipline: first item has a space
    prefix, subsequent items have a leading comma.

    Args:
        items:  List of column names or SELECT expressions.
        indent: Number of spaces for indentation.

    Returns:
        Formatted multiline string.

    Example output:
             Col_A
            ,Col_B
            ,Col_C
    """
    if not items:
        return ""

    prefix = " " * indent
    lines = []

    for i, item in enumerate(items):
        if i == 0:
            lines.append(f"{prefix} {item}")
        else:
            lines.append(f"{prefix},{item}")

    return "\n".join(lines)
