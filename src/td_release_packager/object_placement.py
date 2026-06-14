"""
Object Placement Engine for SHIPS.

Resolves the mapping between tables databases and views databases
using one of three strategies:

    separated  — pattern-based derivation (suffix, prefix, midfix)
    colocated  — tables and views share the same database
    mapped     — explicit database-to-database pairs

Configuration is read from the ``object_placement`` section of
``ships.yml``.

Usage::

    from object_placement import ObjectPlacement

    config = {
        "strategy": "separated",
        "database_pattern_tables": "{BASE}_T",
        "database_pattern_views": "{BASE}_V",
        "locking_views": True,
    }
    placement = ObjectPlacement(config)
    views_db = placement.resolve_views_database("MORTGAGE_T")
    # => "MORTGAGE_V"

Author: Paul / Teradata Field Engineering
"""

import re
from typing import Dict, List, Optional, Tuple


class PlacementConfigError(Exception):
    """Raised when the object_placement configuration is invalid."""

    pass


class PlacementResolutionError(Exception):
    """Raised when a database name cannot be resolved."""

    pass


# ---------------------------------------------------------------------------
# Brace / pattern validation helpers
# ---------------------------------------------------------------------------


def validate_braces(pattern: str, label: str) -> List[str]:
    """
    Validate that curly braces in *pattern* are well-formed.

    Checks performed:
        1. Every ``{`` has a matching ``}``.
        2. No empty ``{}`` tokens.
        3. No nested ``{{...}}``.
        4. No unmatched ``}`` before ``{``.
        5. Token names are valid identifiers (letters, digits, underscores;
           must start with a letter or underscore).

    Args:
        pattern: The pattern string to validate.
        label:   Human-readable label for error messages
                 (e.g. ``"database_pattern_tables"``).

    Returns:
        List of extracted token names in order of appearance.

    Raises:
        PlacementConfigError: If any validation check fails, with a
            user-friendly message describing the problem.
    """
    tokens: List[str] = []
    i = 0
    length = len(pattern)

    while i < length:
        char = pattern[i]

        if char == "}":
            # ---------------------------------------------------------------
            # Unmatched closing brace
            # ---------------------------------------------------------------
            raise PlacementConfigError(
                f"Unmatched '}}' in {label} at position {i}.\n"
                f"  Pattern: {pattern}\n"
                f"  {' ' * (11 + i)}^\n"
                f"  Every '}}' must have a matching '{{' before it."
            )

        if char == "{":
            # ---------------------------------------------------------------
            # Check for nested opening brace  {{
            # ---------------------------------------------------------------
            if i + 1 < length and pattern[i + 1] == "{":
                raise PlacementConfigError(
                    f"Nested '{{{{' detected in {label} at position {i}.\n"
                    f"  Pattern: {pattern}\n"
                    f"  {' ' * (11 + i)}^^\n"
                    f"  Braces cannot be nested. Use a single pair: "
                    f"{{TOKEN_NAME}}"
                )

            # ---------------------------------------------------------------
            # Find the matching closing brace
            # ---------------------------------------------------------------
            close_pos = pattern.find("}", i + 1)
            if close_pos == -1:
                raise PlacementConfigError(
                    f"Unmatched '{{' in {label} at position {i}.\n"
                    f"  Pattern: {pattern}\n"
                    f"  {' ' * (11 + i)}^\n"
                    f"  Every '{{' must have a matching '}}' after it."
                )

            # ---------------------------------------------------------------
            # Extract and validate the token name
            # ---------------------------------------------------------------
            token_name = pattern[i + 1 : close_pos]

            if not token_name:
                raise PlacementConfigError(
                    f"Empty placeholder '{{}}' in {label} at position {i}.\n"
                    f"  Pattern: {pattern}\n"
                    f"  {' ' * (11 + i)}^^\n"
                    f"  Placeholders must contain a name: {{TOKEN_NAME}}"
                )

            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", token_name):
                raise PlacementConfigError(
                    f"Invalid token name '{token_name}' in {label} "
                    f"at position {i}.\n"
                    f"  Pattern: {pattern}\n"
                    f"  Token names must start with a letter or underscore "
                    f"and contain only letters, digits, and underscores."
                )

            # ---------------------------------------------------------------
            # Check for duplicate token names within the same pattern
            # ---------------------------------------------------------------
            if token_name in tokens:
                raise PlacementConfigError(
                    f"Duplicate token '{{{token_name}}}' in {label}.\n"
                    f"  Pattern: {pattern}\n"
                    f"  Each token name must appear exactly once per pattern."
                )

            tokens.append(token_name)
            i = close_pos + 1
        else:
            i += 1

    return tokens


def validate_token_symmetry(
    tokens_tables: List[str],
    tokens_views: List[str],
    pattern_tables: str,
    pattern_views: str,
) -> None:
    """
    Verify that both patterns declare the same set of token names.

    The order may differ (the patterns might place tokens in different
    positions), but the names must match exactly.

    Args:
        tokens_tables:  Token names from the tables pattern.
        tokens_views:   Token names from the views pattern.
        pattern_tables: Raw tables pattern (for error messages).
        pattern_views:  Raw views pattern (for error messages).

    Raises:
        PlacementConfigError: If the token sets do not match.
    """
    set_tables = set(tokens_tables)
    set_views = set(tokens_views)

    if set_tables != set_views:
        only_in_tables = set_tables - set_views
        only_in_views = set_views - set_tables

        parts = ["Token mismatch between patterns."]
        parts.append(f"  Tables pattern: {pattern_tables}")
        parts.append(f"  Views pattern:  {pattern_views}")
        if only_in_tables:
            parts.append(
                f"  Tokens only in tables pattern: {', '.join(sorted(only_in_tables))}"
            )
        if only_in_views:
            parts.append(
                f"  Tokens only in views pattern:  {', '.join(sorted(only_in_views))}"
            )
        parts.append("  Every placeholder in one pattern must appear in the other.")
        raise PlacementConfigError("\n".join(parts))


def _pattern_has_no_tokens(pattern: str) -> bool:
    """Return True if the pattern contains no ``{...}`` placeholders."""
    return "{" not in pattern and "}" not in pattern


# ---------------------------------------------------------------------------
# Regex compilation
# ---------------------------------------------------------------------------


def compile_pattern(pattern: str, tokens: List[str]) -> re.Pattern:
    """
    Convert a placeholder pattern into a compiled regex.

    Each ``{TOKEN}`` becomes a named capture group. Literal text between
    tokens is escaped. The regex is anchored with ``^`` and ``$`` and
    compiled case-insensitive (Teradata identifiers are case-insensitive).

    Args:
        pattern: The placeholder pattern (e.g. ``"{ENV}_DAT_{MODULE}"``).
        tokens:  Token names extracted by :func:`validate_braces`.

    Returns:
        Compiled regex with named groups for each token.
    """
    # Split the pattern on {TOKEN} boundaries, preserving order.
    # We rebuild the regex piece by piece.
    regex_parts = []
    i = 0

    for idx, token in enumerate(tokens):
        placeholder = "{" + token + "}"
        pos = pattern.find(placeholder, i)
        # Literal segment before this token
        literal = pattern[i:pos]
        if literal:
            regex_parts.append(re.escape(literal))
        # Named capture group — non-greedy except for the last token
        if idx < len(tokens) - 1:
            regex_parts.append(f"(?P<{token}>.+?)")
        else:
            regex_parts.append(f"(?P<{token}>.+)")
        i = pos + len(placeholder)

    # Trailing literal after the last token
    trailing = pattern[i:]
    if trailing:
        regex_parts.append(re.escape(trailing))

    return re.compile("^" + "".join(regex_parts) + "$", re.IGNORECASE)


def substitute_tokens(pattern: str, values: Dict[str, str]) -> str:
    """
    Replace ``{TOKEN}`` placeholders with values from *values*.

    Args:
        pattern: The target pattern (e.g. ``"{ENV}_ACC_{MODULE}"``).
        values:  Token name → captured value mapping.

    Returns:
        The resolved database name.
    """
    result = pattern
    for token_name, token_value in values.items():
        result = result.replace("{" + token_name + "}", token_value)
    return result


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class ObjectPlacement:
    """
    Resolves database name mappings between tables and views databases.

    Supports three strategies:

    ``separated``
        Derive the views database from the tables database (and vice
        versa) using a pair of patterns with shared placeholder tokens.

    ``colocated``
        Tables and views share the same database — no mapping needed.

    ``mapped``
        Explicit list of tables-database / views-database pairs.

    Args:
        config: The contents of ``object_placement.yaml`` as a dict.

    Raises:
        PlacementConfigError: If the configuration is invalid.
    """

    # Valid strategy names
    VALID_STRATEGIES = {"separated", "colocated", "mapped"}

    def __init__(self, config: dict) -> None:
        """
        Initialise the placement engine from object_placement.yaml.

        Args:
            config: The contents of ``object_placement.yaml`` as a dict.

        Raises:
            PlacementConfigError: If the configuration is invalid.
        """
        if not config:
            raise PlacementConfigError(
                "object_placement configuration is empty or missing."
            )

        self._strategy: str = config.get("strategy", "").lower().strip()
        if self._strategy not in self.VALID_STRATEGIES:
            raise PlacementConfigError(
                f"Unknown strategy '{config.get('strategy')}'.\n"
                f"  Valid strategies: {', '.join(sorted(self.VALID_STRATEGIES))}"
            )

        # Default to True per the Teradata field standard: every table
        # should have a 1:1 locking view in front of it.  Configs that
        # explicitly set ``locking_views: false`` keep their existing
        # behaviour — only the missing-key case changes.
        self._locking_views: bool = config.get("locking_views", True)

        # ----- separated -----
        self._tables_pattern: Optional[str] = None
        self._views_pattern: Optional[str] = None
        self._tables_regex: Optional[re.Pattern] = None
        self._views_regex: Optional[re.Pattern] = None
        self._tokens_tables: List[str] = []
        self._tokens_views: List[str] = []

        # ----- mapped -----
        self._tables_to_views: Dict[str, str] = {}
        self._views_to_tables: Dict[str, str] = {}

        # Dispatch initialisation by strategy
        if self._strategy == "separated":
            self._init_separated(config)
        elif self._strategy == "mapped":
            self._init_mapped(config)
        # colocated needs no additional initialisation

    def _init_separated(self, config: dict) -> None:
        """
        Initialise the separated strategy — pattern-based derivation.

        Args:
            config: The contents of ``object_placement.yaml`` as a dict.

        Raises:
            PlacementConfigError: If patterns are missing, malformed,
                or have mismatched tokens.
        """
        self._tables_pattern = config.get("database_pattern_tables")
        self._views_pattern = config.get("database_pattern_views")

        if not self._tables_pattern:
            raise PlacementConfigError(
                "Separated strategy requires 'database_pattern_tables'."
            )
        if not self._views_pattern:
            raise PlacementConfigError(
                "Separated strategy requires 'database_pattern_views'."
            )

        # Patterns with no tokens at all are invalid for separated strategy
        if _pattern_has_no_tokens(self._tables_pattern):
            raise PlacementConfigError(
                f"database_pattern_tables has no placeholders: "
                f"'{self._tables_pattern}'.\n"
                f"  Separated strategy requires at least one "
                f"{{TOKEN}} placeholder."
            )
        if _pattern_has_no_tokens(self._views_pattern):
            raise PlacementConfigError(
                f"database_pattern_views has no placeholders: "
                f"'{self._views_pattern}'.\n"
                f"  Separated strategy requires at least one "
                f"{{TOKEN}} placeholder."
            )

        # Validate braces and extract tokens
        self._tokens_tables = validate_braces(
            self._tables_pattern, "database_pattern_tables"
        )
        self._tokens_views = validate_braces(
            self._views_pattern, "database_pattern_views"
        )

        # Verify token symmetry
        validate_token_symmetry(
            self._tokens_tables,
            self._tokens_views,
            self._tables_pattern,
            self._views_pattern,
        )

        # Compile regexes
        self._tables_regex = compile_pattern(self._tables_pattern, self._tokens_tables)
        self._views_regex = compile_pattern(self._views_pattern, self._tokens_views)

    def _init_mapped(self, config: dict) -> None:
        """
        Initialise the mapped strategy — explicit database pairs.

        Args:
            config: The contents of ``object_placement.yaml`` as a dict.

        Raises:
            PlacementConfigError: If ``database_map`` is missing,
                empty, or contains invalid entries.
        """
        db_map = config.get("database_map")
        if not db_map or not isinstance(db_map, list):
            raise PlacementConfigError(
                "Mapped strategy requires 'database_map' as a list of\n"
                "  tables_database / views_database pairs.\n"
                "  Example:\n"
                "    database_map:\n"
                "      - tables_database: PROD_MORTGAGE_DATA\n"
                "        views_database: PROD_MORTGAGE_ACCESS"
            )

        for idx, entry in enumerate(db_map):
            if not isinstance(entry, dict):
                raise PlacementConfigError(
                    f"database_map entry {idx + 1} is not a mapping.\n"
                    f"  Each entry must have 'tables_database' and "
                    f"'views_database' keys."
                )

            tbl_db = entry.get("tables_database", "").strip()
            viw_db = entry.get("views_database", "").strip()

            if not tbl_db:
                raise PlacementConfigError(
                    f"database_map entry {idx + 1} is missing 'tables_database'."
                )
            if not viw_db:
                raise PlacementConfigError(
                    f"database_map entry {idx + 1} is missing 'views_database'."
                )

            # Case-insensitive lookup — Teradata identifiers are CI
            tbl_upper = tbl_db.upper()
            viw_upper = viw_db.upper()

            if tbl_upper in self._tables_to_views:
                raise PlacementConfigError(
                    f"Duplicate tables_database '{tbl_db}' in database_map."
                )

            self._tables_to_views[tbl_upper] = viw_db
            self._views_to_tables[viw_upper] = tbl_db

    # -------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------

    @property
    def strategy(self) -> str:
        """Return the active placement strategy name."""
        return self._strategy

    @property
    def locking_views(self) -> bool:
        """Return whether the 1:1 locking view layer is enabled."""
        return self._locking_views

    # -------------------------------------------------------------------
    # Resolution methods
    # -------------------------------------------------------------------

    def resolve_views_database(self, tables_database: str) -> str:
        """
        Given a tables database name, return the corresponding views
        database name.

        Args:
            tables_database: The name of the tables database.

        Returns:
            The corresponding views database name.

        Raises:
            PlacementResolutionError: If the name cannot be resolved.
        """
        if self._strategy == "colocated":
            return tables_database

        if self._strategy == "mapped":
            key = tables_database.upper()
            if key not in self._tables_to_views:
                raise PlacementResolutionError(
                    f"Tables database '{tables_database}' not found in "
                    f"database_map.\n"
                    f"  Known tables databases: "
                    f"{', '.join(sorted(self._tables_to_views.keys()))}"
                )
            return self._tables_to_views[key]

        # separated — pattern match
        return self._resolve_via_pattern(
            tables_database,
            self._tables_regex,
            self._views_pattern,
            "tables",
        )

    def resolve_tables_database(self, views_database: str) -> str:
        """
        Given a views database name, return the corresponding tables
        database name.

        Args:
            views_database: The name of the views database.

        Returns:
            The corresponding tables database name.

        Raises:
            PlacementResolutionError: If the name cannot be resolved.
        """
        if self._strategy == "colocated":
            return views_database

        if self._strategy == "mapped":
            key = views_database.upper()
            if key not in self._views_to_tables:
                raise PlacementResolutionError(
                    f"Views database '{views_database}' not found in "
                    f"database_map.\n"
                    f"  Known views databases: "
                    f"{', '.join(sorted(self._views_to_tables.keys()))}"
                )
            return self._views_to_tables[key]

        # separated — pattern match
        return self._resolve_via_pattern(
            views_database,
            self._views_regex,
            self._tables_pattern,
            "views",
        )

    def is_tables_database(self, db_name: str) -> bool:
        """
        Check whether *db_name* matches the tables database pattern.

        Args:
            db_name: Database name to check.

        Returns:
            True if the name matches the tables pattern/map.
        """
        if self._strategy == "colocated":
            return True
        if self._strategy == "mapped":
            return db_name.upper() in self._tables_to_views
        return bool(self._tables_regex.match(db_name))

    def is_views_database(self, db_name: str) -> bool:
        """
        Check whether *db_name* matches the views database pattern.

        Args:
            db_name: Database name to check.

        Returns:
            True if the name matches the views pattern/map.
        """
        if self._strategy == "colocated":
            return True
        if self._strategy == "mapped":
            return db_name.upper() in self._views_to_tables
        return bool(self._views_regex.match(db_name))

    def _resolve_via_pattern(
        self,
        db_name: str,
        source_regex: re.Pattern,
        target_pattern: str,
        source_label: str,
    ) -> str:
        """
        Match *db_name* against *source_regex*, extract tokens,
        substitute into *target_pattern*.

        Args:
            db_name:        The database name to resolve.
            source_regex:   Compiled regex for the source pattern.
            target_pattern: The target pattern with ``{TOKEN}`` placeholders.
            source_label:   ``'tables'`` or ``'views'`` for error messages.

        Returns:
            The resolved database name.

        Raises:
            PlacementResolutionError: If the name does not match the
                source pattern.
        """
        match = source_regex.match(db_name)
        if not match:
            raise PlacementResolutionError(
                f"Database name '{db_name}' does not match the "
                f"{source_label} pattern.\n"
                f"  Pattern: {source_regex.pattern}\n"
                f"  Check the database name and "
                f"object_placement configuration."
            )
        return substitute_tokens(target_pattern, match.groupdict())

    # -------------------------------------------------------------------
    # Bulk operations
    # -------------------------------------------------------------------

    def rewrite_database_reference(
        self,
        qualified_name: str,
    ) -> Tuple[str, bool]:
        """
        Rewrite a fully-qualified object reference from a tables
        database to the corresponding views database.

        Expects the format ``DATABASE.OBJECT_NAME``.

        Args:
            qualified_name: A database-qualified object name
                            (e.g. ``"PROD_DAT_MORT.Mortgage"``).

        Returns:
            Tuple of (rewritten_name, was_changed). If the database
            part does not match the tables pattern, the original name
            is returned unchanged with ``was_changed=False``.
        """
        if "." not in qualified_name:
            return qualified_name, False

        db_part, obj_part = qualified_name.split(".", 1)

        if not self.is_tables_database(db_part):
            return qualified_name, False

        try:
            new_db = self.resolve_views_database(db_part)
            return f"{new_db}.{obj_part}", True
        except PlacementResolutionError:
            return qualified_name, False

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        if self._strategy == "separated":
            return (
                f"ObjectPlacement(strategy=separated, "
                f"tables='{self._tables_pattern}', "
                f"views='{self._views_pattern}', "
                f"locking_views={self._locking_views})"
            )
        if self._strategy == "mapped":
            count = len(self._tables_to_views)
            return (
                f"ObjectPlacement(strategy=mapped, "
                f"pairs={count}, "
                f"locking_views={self._locking_views})"
            )
        return (
            f"ObjectPlacement(strategy=colocated, locking_views={self._locking_views})"
        )
