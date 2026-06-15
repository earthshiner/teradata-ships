"""
test_prefix_tokeniser.py — Tests for the identifier-aware prefix
tokeniser (Model B, issue #309).

Covers
------
* Leading-segment and standalone references are both substituted in
  one pass.
* The malformed-token bug from the original literal-substring
  ``--token-map`` mechanism is regressed against: ``{{PREFIX_`` must
  never appear in any output.
* Identifier boundaries are enforced: ``XCallCentre``,
  ``MyCallCentreThing``, and ``callcentrex`` are not matched.
* The function is idempotent — a second pass over already-tokenised
  text makes no further changes.
* Multi-reference view bodies tokenise every reference once.
* :func:`tokenise_prefixes` applies the longest prefix first so a
  shorter prefix cannot shadow a longer one.
* Case-insensitive matching is the default but can be turned off.
* Argument validation rejects empty inputs and malformed token names.
"""

from __future__ import annotations

import pytest

from td_release_packager.token_engine import (
    build_prefix_pattern,
    tokenise_prefix,
    tokenise_prefixes,
)


# ---------------------------------------------------------------------------
# Single-prefix happy path
# ---------------------------------------------------------------------------


class TestTokeniseSinglePrefix:
    def test_leading_segment_and_standalone(self):
        src = "create database CallCentre_DOM_STD_T from CallCentre as perm=0;"
        out, n = tokenise_prefix(src, "CallCentre", "PREFIX")
        assert out == "create database {{PREFIX}}_DOM_STD_T from {{PREFIX}} as perm=0;"
        assert n == 2

    def test_view_body_multiple_refs(self):
        src = (
            "replace view CallCentre_DOM_BUS_V.V as "
            "select * from CallCentre_SEM_STD_V.T;"
        )
        out, n = tokenise_prefix(src, "CallCentre", "PREFIX")
        assert (
            out == "replace view {{PREFIX}}_DOM_BUS_V.V as "
            "select * from {{PREFIX}}_SEM_STD_V.T;"
        )
        assert n == 2

    def test_standalone_at_end_of_string(self):
        # No trailing whitespace / punctuation — the standalone right
        # boundary must accept end-of-string.
        src = "from CallCentre"
        out, n = tokenise_prefix(src, "CallCentre", "PREFIX")
        assert out == "from {{PREFIX}}"
        assert n == 1

    def test_standalone_at_start_of_string(self):
        # Left boundary must accept start-of-string.
        src = "CallCentre_DOM_STD_T"
        out, n = tokenise_prefix(src, "CallCentre", "PREFIX")
        assert out == "{{PREFIX}}_DOM_STD_T"
        assert n == 1


# ---------------------------------------------------------------------------
# Regression guard: never emit a malformed token
# ---------------------------------------------------------------------------


class TestNoMalformedToken:
    def test_underscore_never_inside_braces(self):
        """The original ``--token-map`` mechanism produced ``{{PREFIX_T}}``
        by absorbing the trailing ``_T``.  The look-ahead in
        :func:`build_prefix_pattern` must prevent that."""
        out, _ = tokenise_prefix("from CallCentre_T;", "CallCentre", "PREFIX")
        assert "{{PREFIX_" not in out
        assert out == "from {{PREFIX}}_T;"

    def test_no_nested_braces_when_idempotent(self):
        """Running the tokeniser twice must not produce ``{{{{PREFIX}}}}``."""
        once, _ = tokenise_prefix("CallCentre_DOM", "CallCentre", "PREFIX")
        twice, _ = tokenise_prefix(once, "CallCentre", "PREFIX")
        assert "{{{{" not in twice
        assert "}}}}" not in twice


# ---------------------------------------------------------------------------
# Identifier boundary enforcement
# ---------------------------------------------------------------------------


class TestBoundariesNotMatched:
    @pytest.mark.parametrize(
        "src",
        [
            "XCallCentre_DOM",
            "MyCallCentreThing",
            "callcentrex",  # interior — right boundary fails
            "1CallCentre",  # digit on the left is an identifier char
            "_CallCentre",  # underscore on the left is an identifier char
        ],
    )
    def test_no_match_inside_identifier(self, src):
        out, n = tokenise_prefix(src, "CallCentre", "PREFIX")
        assert n == 0
        assert out == src

    def test_prefix_followed_by_alpha_no_underscore_not_matched(self):
        # ``CallCentreX`` — the right boundary requires either ``_`` then
        # an identifier char, or a non-identifier char.  Bare alpha is
        # neither.
        out, n = tokenise_prefix("CallCentreX", "CallCentre", "PREFIX")
        assert n == 0
        assert out == "CallCentreX"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_second_pass_no_changes(self):
        once, _ = tokenise_prefix("CallCentre_DOM_STD_T", "CallCentre", "PREFIX")
        twice, n = tokenise_prefix(once, "CallCentre", "PREFIX")
        assert n == 0
        assert twice == once

    def test_second_pass_on_mixed_text_no_changes(self):
        src = (
            "create database CallCentre_DOM_STD_T from CallCentre "
            "as perm=0, spool=1.4E9, fallback;"
        )
        once, n1 = tokenise_prefix(src, "CallCentre", "PREFIX")
        twice, n2 = tokenise_prefix(once, "CallCentre", "PREFIX")
        assert n1 == 2
        assert n2 == 0
        assert twice == once


# ---------------------------------------------------------------------------
# Case sensitivity (default ON)
# ---------------------------------------------------------------------------


class TestCaseInsensitive:
    def test_default_is_case_insensitive(self):
        # Reflected DDL sometimes emits lowercase identifiers — the
        # default must tokenise them consistently with the configured
        # exact-case prefix.
        out, n = tokenise_prefix(
            "create database callcentre_dom_std_t from callcentre as perm=0;",
            "CallCentre",
            "PREFIX",
        )
        assert out == "create database {{PREFIX}}_dom_std_t from {{PREFIX}} as perm=0;"
        assert n == 2

    def test_case_sensitive_opt_out(self):
        out, n = tokenise_prefix(
            "from callcentre",
            "CallCentre",
            "PREFIX",
            case_insensitive=False,
        )
        assert n == 0
        assert out == "from callcentre"


# ---------------------------------------------------------------------------
# Multi-prefix dispatch
# ---------------------------------------------------------------------------


class TestTokenisePrefixes:
    def test_longest_prefix_wins(self):
        # If both ``Call`` and ``CallCentre`` are in the map, the longer
        # one must claim ``CallCentre_DOM`` even though ``Call`` is also
        # a left-anchored identifier-boundary match.
        src = "from CallCentre_DOM and Call_OTHER"
        out, total, per = tokenise_prefixes(
            src,
            {"Call": "CALL", "CallCentre": "CENTRE"},
        )
        assert out == "from {{CENTRE}}_DOM and {{CALL}}_OTHER"
        assert total == 2
        assert per["CallCentre"] == 1
        assert per["Call"] == 1

    def test_empty_map_no_op(self):
        out, total, per = tokenise_prefixes("CallCentre_X", {})
        assert out == "CallCentre_X"
        assert total == 0
        assert per == {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_prefix_rejected(self):
        with pytest.raises(ValueError, match="prefix"):
            tokenise_prefix("anything", "", "PREFIX")

    def test_empty_token_name_rejected(self):
        with pytest.raises(ValueError, match="token_name"):
            tokenise_prefix("anything", "CallCentre", "")

    def test_invalid_token_name_rejected(self):
        with pytest.raises(ValueError, match="not a valid identifier"):
            tokenise_prefix("anything", "CallCentre", "Has Space")

    def test_build_pattern_empty_prefix_rejected(self):
        with pytest.raises(ValueError):
            build_prefix_pattern("")
