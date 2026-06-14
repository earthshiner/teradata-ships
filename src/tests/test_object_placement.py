"""
Test suite for Object Placement Engine.

Covers:
    - Brace validation (matched, unmatched, empty, nested, invalid names)
    - Token symmetry between patterns
    - Separated strategy (suffix, prefix, midfix, case insensitivity)
    - Colocated strategy (passthrough)
    - Mapped strategy (explicit pairs, duplicates, missing entries)
    - Resolution errors
    - Bulk rewrite operations
    - Edge cases (single token, many tokens, long names)

Run with:  python -m pytest test_object_placement.py -v

Author: Paul / Teradata Field Engineering
"""

import pytest
from td_release_packager.object_placement import (
    ObjectPlacement,
    PlacementConfigError,
    PlacementResolutionError,
    validate_braces,
    validate_token_symmetry,
    compile_pattern,
    substitute_tokens,
)


# ===================================================================
# BRACE VALIDATION
# ===================================================================


class TestValidateBraces:
    """Tests for the validate_braces function."""

    # --- Valid patterns ---

    def test_single_token(self):
        """Single placeholder should return one token name."""
        tokens = validate_braces("{BASE}_T", "test_field")
        assert tokens == ["BASE"]

    def test_two_tokens(self):
        """Two placeholders should return both names in order."""
        tokens = validate_braces("{ENV}_DAT_{MODULE}", "test_field")
        assert tokens == ["ENV", "MODULE"]

    def test_three_tokens(self):
        """Three placeholders — prefix, midfix, suffix positions."""
        tokens = validate_braces("{ENV}_{DISC}_{MODULE}", "test_field")
        assert tokens == ["ENV", "DISC", "MODULE"]

    def test_underscored_token_name(self):
        """Token names with underscores are valid."""
        tokens = validate_braces("{MY_ENV}_T", "test_field")
        assert tokens == ["MY_ENV"]

    def test_token_starting_with_underscore(self):
        """Token names starting with underscore are valid."""
        tokens = validate_braces("{_PRIVATE}_T", "test_field")
        assert tokens == ["_PRIVATE"]

    def test_alphanumeric_token_name(self):
        """Token names with digits are valid (not leading)."""
        tokens = validate_braces("{ENV2}_T", "test_field")
        assert tokens == ["ENV2"]

    # --- Unmatched braces ---

    def test_unmatched_opening_brace(self):
        """Opening brace without closing brace raises error."""
        with pytest.raises(PlacementConfigError, match="Unmatched '{'"):
            validate_braces("{BASE_T", "test_field")

    def test_unmatched_closing_brace(self):
        """Closing brace without opening brace raises error."""
        with pytest.raises(PlacementConfigError, match="Unmatched '}'"):
            validate_braces("BASE}_T", "test_field")

    def test_unmatched_closing_brace_at_start(self):
        """Closing brace at position 0 raises error."""
        with pytest.raises(PlacementConfigError, match="Unmatched '}'"):
            validate_braces("}_T", "test_field")

    def test_extra_closing_brace(self):
        """Extra closing brace after valid pattern raises error."""
        with pytest.raises(PlacementConfigError, match="Unmatched '}'"):
            validate_braces("{BASE}}_T", "test_field")

    def test_opening_without_close_at_end(self):
        """Opening brace at end of string raises error."""
        with pytest.raises(PlacementConfigError, match="Unmatched '{'"):
            validate_braces("BASE_T{", "test_field")

    # --- Empty and nested braces ---

    def test_empty_braces(self):
        """Empty placeholder {} raises error."""
        with pytest.raises(PlacementConfigError, match="Empty placeholder"):
            validate_braces("{}_T", "test_field")

    def test_nested_braces(self):
        """Nested {{ raises error."""
        with pytest.raises(PlacementConfigError, match="Nested"):
            validate_braces("{{BASE}}_T", "test_field")

    # --- Invalid token names ---

    def test_token_starting_with_digit(self):
        """Token name starting with digit raises error."""
        with pytest.raises(PlacementConfigError, match="Invalid token name"):
            validate_braces("{2ENV}_T", "test_field")

    def test_token_with_spaces(self):
        """Token name with spaces raises error."""
        with pytest.raises(PlacementConfigError, match="Invalid token name"):
            validate_braces("{MY ENV}_T", "test_field")

    def test_token_with_hyphens(self):
        """Token name with hyphens raises error."""
        with pytest.raises(PlacementConfigError, match="Invalid token name"):
            validate_braces("{MY-ENV}_T", "test_field")

    def test_token_with_dots(self):
        """Token name with dots raises error."""
        with pytest.raises(PlacementConfigError, match="Invalid token name"):
            validate_braces("{MY.ENV}_T", "test_field")

    # --- Duplicate tokens ---

    def test_duplicate_token_in_same_pattern(self):
        """Same token name used twice in one pattern raises error."""
        with pytest.raises(PlacementConfigError, match="Duplicate token"):
            validate_braces("{ENV}_{ENV}", "test_field")

    # --- Error message quality ---

    def test_error_message_includes_field_label(self):
        """Error message should include the field label."""
        with pytest.raises(PlacementConfigError, match="database_pattern_tables"):
            validate_braces("{BROKEN", "database_pattern_tables")

    def test_error_message_includes_pattern(self):
        """Error message should include the pattern itself."""
        with pytest.raises(PlacementConfigError, match=r"BAD\{PATTERN"):
            validate_braces("BAD{PATTERN", "test_field")


# ===================================================================
# TOKEN SYMMETRY
# ===================================================================


class TestTokenSymmetry:
    """Tests for validate_token_symmetry."""

    def test_matching_tokens(self):
        """Same tokens in both patterns should pass."""
        # Should not raise
        validate_token_symmetry(
            ["ENV", "MODULE"],
            ["ENV", "MODULE"],
            "{ENV}_T_{MODULE}",
            "{ENV}_V_{MODULE}",
        )

    def test_matching_tokens_different_order(self):
        """Same tokens in different order should pass."""
        validate_token_symmetry(
            ["ENV", "MODULE"],
            ["MODULE", "ENV"],
            "{ENV}_T_{MODULE}",
            "{MODULE}_V_{ENV}",
        )

    def test_missing_token_in_views(self):
        """Token in tables but not views raises error."""
        with pytest.raises(PlacementConfigError, match="Token mismatch"):
            validate_token_symmetry(
                ["ENV", "MODULE"],
                ["ENV"],
                "{ENV}_T_{MODULE}",
                "{ENV}_V",
            )

    def test_extra_token_in_views(self):
        """Token in views but not tables raises error."""
        with pytest.raises(PlacementConfigError, match="Token mismatch"):
            validate_token_symmetry(
                ["ENV"],
                ["ENV", "EXTRA"],
                "{ENV}_T",
                "{ENV}_V_{EXTRA}",
            )

    def test_completely_different_tokens(self):
        """Completely different token sets raises error."""
        with pytest.raises(PlacementConfigError, match="Token mismatch"):
            validate_token_symmetry(
                ["FOO"],
                ["BAR"],
                "{FOO}_T",
                "{BAR}_V",
            )


# ===================================================================
# REGEX COMPILATION AND SUBSTITUTION
# ===================================================================


class TestCompilePattern:
    """Tests for compile_pattern and substitute_tokens."""

    def test_suffix_pattern_match(self):
        """Suffix pattern should match and extract correctly."""
        regex = compile_pattern("{BASE}_T", ["BASE"])
        match = regex.match("MORTGAGE_T")
        assert match is not None
        assert match.group("BASE") == "MORTGAGE"

    def test_prefix_pattern_match(self):
        """Prefix pattern should match and extract correctly."""
        regex = compile_pattern("TBL_{BASE}", ["BASE"])
        match = regex.match("TBL_MORTGAGE")
        assert match is not None
        assert match.group("BASE") == "MORTGAGE"

    def test_midfix_pattern_match(self):
        """Midfix pattern should match and extract correctly."""
        regex = compile_pattern("{ENV}_DAT_{MODULE}", ["ENV", "MODULE"])
        match = regex.match("PROD_DAT_MORTGAGE")
        assert match is not None
        assert match.group("ENV") == "PROD"
        assert match.group("MODULE") == "MORTGAGE"

    def test_case_insensitive_match(self):
        """Pattern matching should be case-insensitive."""
        regex = compile_pattern("{BASE}_T", ["BASE"])
        match = regex.match("mortgage_t")
        assert match is not None
        assert match.group("BASE") == "mortgage"

    def test_no_match(self):
        """Non-matching name should return None."""
        regex = compile_pattern("{BASE}_T", ["BASE"])
        match = regex.match("MORTGAGE_V")
        assert match is None

    def test_substitute_tokens_single(self):
        """Single token substitution should work."""
        result = substitute_tokens("{BASE}_V", {"BASE": "MORTGAGE"})
        assert result == "MORTGAGE_V"

    def test_substitute_tokens_multiple(self):
        """Multiple token substitution should work."""
        result = substitute_tokens(
            "{ENV}_ACC_{MODULE}",
            {"ENV": "PROD", "MODULE": "MORTGAGE"},
        )
        assert result == "PROD_ACC_MORTGAGE"

    def test_compound_value_in_suffix_pattern(self):
        """Token value containing underscores should be captured fully."""
        regex = compile_pattern("{BASE}_T", ["BASE"])
        match = regex.match("MORTGAGE_DATA_T")
        assert match is not None
        assert match.group("BASE") == "MORTGAGE_DATA"


# ===================================================================
# SEPARATED STRATEGY
# ===================================================================


class TestSeparatedStrategy:
    """Tests for the separated placement strategy."""

    def _make_separated(self, tables_pat, views_pat, locking=True):
        """Helper to create a separated ObjectPlacement."""
        return ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": tables_pat,
                "database_pattern_views": views_pat,
                "locking_views": locking,
            }
        )

    # --- Suffix convention ---

    def test_suffix_resolve_views_database(self):
        """Suffix: tables → views resolution."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.resolve_views_database("MORTGAGE_T") == "MORTGAGE_V"

    def test_suffix_resolve_tables_database(self):
        """Suffix: views → tables resolution."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.resolve_tables_database("MORTGAGE_V") == "MORTGAGE_T"

    def test_suffix_compound_name(self):
        """Suffix: compound base name with underscores."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.resolve_views_database("D01_MP_DOM_T") == "D01_MP_DOM_V"

    # --- Prefix convention ---

    def test_prefix_resolve_views_database(self):
        """Prefix: tables → views resolution."""
        op = self._make_separated("TBL_{BASE}", "VW_{BASE}")
        assert op.resolve_views_database("TBL_MORTGAGE") == "VW_MORTGAGE"

    def test_prefix_resolve_tables_database(self):
        """Prefix: views → tables resolution."""
        op = self._make_separated("TBL_{BASE}", "VW_{BASE}")
        assert op.resolve_tables_database("VW_MORTGAGE") == "TBL_MORTGAGE"

    # --- Midfix convention ---

    def test_midfix_resolve_views_database(self):
        """Midfix: tables → views resolution."""
        op = self._make_separated("{ENV}_DAT_{MODULE}", "{ENV}_ACC_{MODULE}")
        assert op.resolve_views_database("PROD_DAT_MORTGAGE") == "PROD_ACC_MORTGAGE"

    def test_midfix_resolve_tables_database(self):
        """Midfix: views → tables resolution."""
        op = self._make_separated("{ENV}_DAT_{MODULE}", "{ENV}_ACC_{MODULE}")
        assert op.resolve_tables_database("PROD_ACC_MORTGAGE") == "PROD_DAT_MORTGAGE"

    def test_midfix_three_tokens(self):
        """Three-token pattern with midfix discriminator."""
        op = self._make_separated(
            "{ENV}_DAT_{REGION}_{MODULE}",
            "{ENV}_ACC_{REGION}_{MODULE}",
        )
        result = op.resolve_views_database("PROD_DAT_APAC_MORTGAGE")
        assert result == "PROD_ACC_APAC_MORTGAGE"

    def test_midfix_preserves_all_segments(self):
        """Midfix: all captured segments are preserved."""
        op = self._make_separated("{A}_DAT_{B}_{C}", "{A}_ACC_{B}_{C}")
        result = op.resolve_views_database("PROD_DAT_MORT_DOM")
        assert result == "PROD_ACC_MORT_DOM"

    # --- Case insensitivity ---

    def test_case_insensitive_resolution(self):
        """Resolution should work regardless of input case."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.resolve_views_database("mortgage_t") == "mortgage_V"

    # --- is_tables_database / is_views_database ---

    def test_is_tables_database_true(self):
        """Matching tables database returns True."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.is_tables_database("MORTGAGE_T") is True

    def test_is_tables_database_false(self):
        """Non-matching tables database returns False."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.is_tables_database("MORTGAGE_V") is False

    def test_is_views_database_true(self):
        """Matching views database returns True."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.is_views_database("MORTGAGE_V") is True

    def test_is_views_database_false(self):
        """Non-matching views database returns False."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.is_views_database("MORTGAGE_T") is False

    # --- Resolution errors ---

    def test_resolve_views_no_match(self):
        """Non-matching name raises PlacementResolutionError."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        with pytest.raises(PlacementResolutionError, match="does not match"):
            op.resolve_views_database("MORTGAGE_X")

    def test_resolve_tables_no_match(self):
        """Non-matching name raises PlacementResolutionError."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        with pytest.raises(PlacementResolutionError, match="does not match"):
            op.resolve_tables_database("MORTGAGE_X")

    # --- Config validation errors ---

    def test_missing_tables_pattern(self):
        """Missing tables pattern raises PlacementConfigError."""
        with pytest.raises(PlacementConfigError, match="database_pattern_tables"):
            ObjectPlacement(
                {
                    "strategy": "separated",
                    "database_pattern_views": "{BASE}_V",
                }
            )

    def test_missing_views_pattern(self):
        """Missing views pattern raises PlacementConfigError."""
        with pytest.raises(PlacementConfigError, match="database_pattern_views"):
            ObjectPlacement(
                {
                    "strategy": "separated",
                    "database_pattern_tables": "{BASE}_T",
                }
            )

    def test_no_tokens_in_tables_pattern(self):
        """Pattern with no placeholders raises error."""
        with pytest.raises(PlacementConfigError, match="no placeholders"):
            ObjectPlacement(
                {
                    "strategy": "separated",
                    "database_pattern_tables": "STATIC_NAME",
                    "database_pattern_views": "{BASE}_V",
                }
            )

    def test_mismatched_tokens(self):
        """Mismatched token names between patterns raises error."""
        with pytest.raises(PlacementConfigError, match="Token mismatch"):
            ObjectPlacement(
                {
                    "strategy": "separated",
                    "database_pattern_tables": "{ENV}_T",
                    "database_pattern_views": "{BASE}_V",
                }
            )

    # --- Properties ---

    def test_strategy_property(self):
        """Strategy property returns 'separated'."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        assert op.strategy == "separated"

    def test_locking_views_property(self):
        """Locking views property reflects config."""
        op = self._make_separated("{BASE}_T", "{BASE}_V", locking=True)
        assert op.locking_views is True

    def test_locking_views_false(self):
        """Locking views can be disabled."""
        op = self._make_separated("{BASE}_T", "{BASE}_V", locking=False)
        assert op.locking_views is False

    def test_repr(self):
        """String representation should be informative."""
        op = self._make_separated("{BASE}_T", "{BASE}_V")
        r = repr(op)
        assert "separated" in r
        assert "{BASE}_T" in r
        assert "{BASE}_V" in r


# ===================================================================
# COLOCATED STRATEGY
# ===================================================================


class TestColocatedStrategy:
    """Tests for the colocated placement strategy."""

    def _make_colocated(self, locking=False):
        """Helper to create a colocated ObjectPlacement."""
        return ObjectPlacement(
            {
                "strategy": "colocated",
                "locking_views": locking,
            }
        )

    def test_resolve_views_passthrough(self):
        """Colocated: views database = tables database."""
        op = self._make_colocated()
        assert op.resolve_views_database("MORTGAGE") == "MORTGAGE"

    def test_resolve_tables_passthrough(self):
        """Colocated: tables database = views database."""
        op = self._make_colocated()
        assert op.resolve_tables_database("MORTGAGE") == "MORTGAGE"

    def test_is_tables_database_always_true(self):
        """Colocated: any name is a tables database."""
        op = self._make_colocated()
        assert op.is_tables_database("ANYTHING") is True

    def test_is_views_database_always_true(self):
        """Colocated: any name is a views database."""
        op = self._make_colocated()
        assert op.is_views_database("ANYTHING") is True

    def test_strategy_property(self):
        """Strategy property returns 'colocated'."""
        op = self._make_colocated()
        assert op.strategy == "colocated"

    def test_locking_views_default_false(self):
        """Default locking_views is False."""
        op = self._make_colocated()
        assert op.locking_views is False

    def test_repr(self):
        """String representation mentions colocated."""
        op = self._make_colocated()
        assert "colocated" in repr(op)


# ===================================================================
# MAPPED STRATEGY
# ===================================================================


class TestMappedStrategy:
    """Tests for the mapped placement strategy."""

    def _make_mapped(self, db_map, locking=True):
        """Helper to create a mapped ObjectPlacement."""
        return ObjectPlacement(
            {
                "strategy": "mapped",
                "locking_views": locking,
                "database_map": db_map,
            }
        )

    def test_resolve_views_database(self):
        """Mapped: tables → views resolution."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_MORTGAGE_DATA",
                    "views_database": "PROD_MORTGAGE_ACCESS",
                },
            ]
        )
        assert op.resolve_views_database("PROD_MORTGAGE_DATA") == "PROD_MORTGAGE_ACCESS"

    def test_resolve_tables_database(self):
        """Mapped: views → tables resolution."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_MORTGAGE_DATA",
                    "views_database": "PROD_MORTGAGE_ACCESS",
                },
            ]
        )
        assert (
            op.resolve_tables_database("PROD_MORTGAGE_ACCESS") == "PROD_MORTGAGE_DATA"
        )

    def test_multiple_pairs(self):
        """Mapped: multiple database pairs."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_MORT_DATA",
                    "views_database": "PROD_MORT_ACCESS",
                },
                {
                    "tables_database": "PROD_STG_DATA",
                    "views_database": "PROD_STG_ACCESS",
                },
            ]
        )
        assert op.resolve_views_database("PROD_MORT_DATA") == "PROD_MORT_ACCESS"
        assert op.resolve_views_database("PROD_STG_DATA") == "PROD_STG_ACCESS"

    def test_case_insensitive_lookup(self):
        """Mapped: lookup is case-insensitive."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_DATA",
                    "views_database": "PROD_ACCESS",
                },
            ]
        )
        assert op.resolve_views_database("prod_data") == "PROD_ACCESS"

    def test_is_tables_database(self):
        """Mapped: is_tables_database matches mapped keys."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_DATA",
                    "views_database": "PROD_ACCESS",
                },
            ]
        )
        assert op.is_tables_database("PROD_DATA") is True
        assert op.is_tables_database("PROD_ACCESS") is False
        assert op.is_tables_database("UNKNOWN") is False

    def test_is_views_database(self):
        """Mapped: is_views_database matches mapped values."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_DATA",
                    "views_database": "PROD_ACCESS",
                },
            ]
        )
        assert op.is_views_database("PROD_ACCESS") is True
        assert op.is_views_database("PROD_DATA") is False

    # --- Resolution errors ---

    def test_unknown_tables_database(self):
        """Unknown tables database raises error."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_DATA",
                    "views_database": "PROD_ACCESS",
                },
            ]
        )
        with pytest.raises(PlacementResolutionError, match="not found in database_map"):
            op.resolve_views_database("UNKNOWN_DB")

    def test_unknown_views_database(self):
        """Unknown views database raises error."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "PROD_DATA",
                    "views_database": "PROD_ACCESS",
                },
            ]
        )
        with pytest.raises(PlacementResolutionError, match="not found in database_map"):
            op.resolve_tables_database("UNKNOWN_DB")

    # --- Config validation errors ---

    def test_missing_database_map(self):
        """Missing database_map raises error."""
        with pytest.raises(PlacementConfigError, match="database_map"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                }
            )

    def test_empty_database_map(self):
        """Empty database_map raises error."""
        with pytest.raises(PlacementConfigError, match="database_map"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                    "database_map": [],
                }
            )

    def test_map_entry_not_a_dict(self):
        """Non-dict entry in database_map raises error."""
        with pytest.raises(PlacementConfigError, match="not a mapping"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                    "database_map": ["PROD_DATA:PROD_ACCESS"],
                }
            )

    def test_map_entry_missing_tables_database(self):
        """Entry missing tables_database raises error."""
        with pytest.raises(PlacementConfigError, match="missing.*tables_database"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                    "database_map": [
                        {"views_database": "PROD_ACCESS"},
                    ],
                }
            )

    def test_map_entry_missing_views_database(self):
        """Entry missing views_database raises error."""
        with pytest.raises(PlacementConfigError, match="missing.*views_database"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                    "database_map": [
                        {"tables_database": "PROD_DATA"},
                    ],
                }
            )

    def test_duplicate_tables_database(self):
        """Duplicate tables_database in map raises error."""
        with pytest.raises(PlacementConfigError, match="Duplicate"):
            ObjectPlacement(
                {
                    "strategy": "mapped",
                    "database_map": [
                        {
                            "tables_database": "PROD_DATA",
                            "views_database": "PROD_V1",
                        },
                        {
                            "tables_database": "PROD_DATA",
                            "views_database": "PROD_V2",
                        },
                    ],
                }
            )

    def test_repr(self):
        """String representation shows pair count."""
        op = self._make_mapped(
            [
                {
                    "tables_database": "A",
                    "views_database": "B",
                },
                {
                    "tables_database": "C",
                    "views_database": "D",
                },
            ]
        )
        r = repr(op)
        assert "mapped" in r
        assert "pairs=2" in r


# ===================================================================
# GENERAL CONFIG VALIDATION
# ===================================================================


class TestConfigValidation:
    """Tests for top-level configuration validation."""

    def test_empty_config(self):
        """Empty config raises error."""
        with pytest.raises(PlacementConfigError, match="empty or missing"):
            ObjectPlacement({})

    def test_none_config(self):
        """None config raises error."""
        with pytest.raises(PlacementConfigError, match="empty or missing"):
            ObjectPlacement(None)

    def test_unknown_strategy(self):
        """Unknown strategy raises error with valid options."""
        with pytest.raises(PlacementConfigError, match="Unknown strategy"):
            ObjectPlacement({"strategy": "scattered"})

    def test_strategy_case_insensitive(self):
        """Strategy name should be case-insensitive."""
        op = ObjectPlacement(
            {
                "strategy": "Colocated",
                "locking_views": False,
            }
        )
        assert op.strategy == "colocated"

    def test_strategy_whitespace_stripped(self):
        """Leading/trailing whitespace in strategy should be stripped."""
        op = ObjectPlacement(
            {
                "strategy": "  colocated  ",
                "locking_views": False,
            }
        )
        assert op.strategy == "colocated"

    def test_locking_views_defaults_true(self):
        """locking_views defaults to True when not specified — Teradata
        field standard is to always layer a 1:1 locking view in front
        of every table.  See #307."""
        op = ObjectPlacement(
            {
                "strategy": "colocated",
            }
        )
        assert op.locking_views is True

    def test_locking_views_explicit_false_honoured(self):
        """An explicit ``locking_views: false`` still wins over the
        default.  Existing projects that opted out keep their
        behaviour."""
        op = ObjectPlacement(
            {
                "strategy": "colocated",
                "locking_views": False,
            }
        )
        assert op.locking_views is False


# ===================================================================
# BULK REWRITE
# ===================================================================


class TestRewriteDatabaseReference:
    """Tests for the rewrite_database_reference method."""

    def test_rewrite_matching_reference(self):
        """Matching qualified name should be rewritten."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        result, changed = op.rewrite_database_reference("D01_MP_DOM_T.Mortgage")
        assert result == "D01_MP_DOM_V.Mortgage"
        assert changed is True

    def test_rewrite_non_matching_reference(self):
        """Non-matching qualified name should pass through unchanged."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        result, changed = op.rewrite_database_reference("D01_MP_DOM_V.Mortgage_Current")
        assert result == "D01_MP_DOM_V.Mortgage_Current"
        assert changed is False

    def test_rewrite_unqualified_name(self):
        """Unqualified name (no dot) should pass through unchanged."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        result, changed = op.rewrite_database_reference("Mortgage")
        assert result == "Mortgage"
        assert changed is False

    def test_rewrite_dbc_reference(self):
        """DBC reference should not be rewritten."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        result, changed = op.rewrite_database_reference("DBC.ColumnsV")
        assert result == "DBC.ColumnsV"
        assert changed is False

    def test_rewrite_colocated_passthrough(self):
        """Colocated: rewrite is a no-op (same database)."""
        op = ObjectPlacement(
            {
                "strategy": "colocated",
                "locking_views": False,
            }
        )
        result, changed = op.rewrite_database_reference("MORTGAGE.Some_Table")
        # Colocated always sees the db as a "tables database"
        # and resolves to itself — so technically changed=True
        # but the value is the same. Let's test the value is unchanged.
        assert result == "MORTGAGE.Some_Table"

    def test_rewrite_midfix_pattern(self):
        """Midfix pattern rewrite should work correctly."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{ENV}_DAT_{MODULE}",
                "database_pattern_views": "{ENV}_ACC_{MODULE}",
                "locking_views": True,
            }
        )
        result, changed = op.rewrite_database_reference("PROD_DAT_MORTGAGE.Loan")
        assert result == "PROD_ACC_MORTGAGE.Loan"
        assert changed is True

    def test_rewrite_preserves_object_name(self):
        """Object name (after the dot) must be preserved exactly."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        result, _ = op.rewrite_database_reference("MORT_T.My_Complex.Object")
        # split('.', 1) means everything after first dot is preserved
        assert result == "MORT_V.My_Complex.Object"


# ===================================================================
# EDGE CASES
# ===================================================================


class TestEdgeCases:
    """Edge cases and real-world patterns."""

    def test_single_char_discriminator(self):
        """Single-character discriminator (e.g. _T / _V)."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{X}_T",
                "database_pattern_views": "{X}_V",
                "locking_views": True,
            }
        )
        assert op.resolve_views_database("A_T") == "A_V"

    def test_long_database_name(self):
        """Long database name with many segments."""
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "{BASE}_T",
                "database_pattern_views": "{BASE}_V",
                "locking_views": True,
            }
        )
        long_name = "D01_WESTPAC_BANKING_CORP_DOM_T"
        result = op.resolve_views_database(long_name)
        assert result == "D01_WESTPAC_BANKING_CORP_DOM_V"

    def test_discriminator_only_pattern(self):
        """Pattern that is all discriminator, no shared text."""
        # e.g. tables are "DATA", views are "ACCESS"
        # This is unusual but valid — {X} matches the entire name
        op = ObjectPlacement(
            {
                "strategy": "separated",
                "database_pattern_tables": "DATA_{X}",
                "database_pattern_views": "ACCESS_{X}",
                "locking_views": True,
            }
        )
        assert op.resolve_views_database("DATA_MORTGAGE") == "ACCESS_MORTGAGE"

    def test_mapped_preserves_original_case(self):
        """Mapped resolution preserves the case from the config."""
        op = ObjectPlacement(
            {
                "strategy": "mapped",
                "database_map": [
                    {
                        "tables_database": "Prod_Data",
                        "views_database": "Prod_Access",
                    },
                ],
            }
        )
        # Input is uppercase but output should be as configured
        result = op.resolve_views_database("PROD_DATA")
        assert result == "Prod_Access"
