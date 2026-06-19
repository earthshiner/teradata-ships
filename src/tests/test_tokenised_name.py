"""
Tests for ``td_release_packager.tokenised_name``.

Covers the shared parser that both the resolution-collision audit and the
dependency analyser will consume. Parsing is the foundation primitive — if
it drifts, both downstream consumers drift with it.
"""

from __future__ import annotations

import pytest

from td_release_packager.tokenised_name import (
    NamePart,
    QualifiedName,
    TokenRef,
    TokenisedNameError,
    extract_tokens,
    iter_token_refs,
    parse_qualified_name,
)


# ---------------------------------------------------------------------
# Pure literals
# ---------------------------------------------------------------------


class TestPureLiteralNames:
    def test_bare_unqualified_name(self):
        q = parse_qualified_name("MyTbl")
        assert q.database is None
        assert q.object.fragments == ("MyTbl",)
        assert q.object.is_pure_literal
        assert q.object.is_pure_token is False
        assert q.tokens == ()

    def test_bare_qualified_name(self):
        q = parse_qualified_name("MyDb.MyTbl")
        assert q.database is not None
        assert q.database.fragments == ("MyDb",)
        assert q.object.fragments == ("MyTbl",)
        assert q.tokens == ()
        assert q.is_qualified


# ---------------------------------------------------------------------
# Canonical {{TOKEN}} forms
# ---------------------------------------------------------------------


class TestTokenForms:
    def test_token_only_object(self):
        q = parse_qualified_name("{{DB}}")
        assert q.database is None
        assert q.object.fragments == (TokenRef("DB"),)
        assert q.object.is_pure_token
        assert q.tokens == ("DB",)

    def test_token_prefix_object(self):
        """The most common SHIPS pattern — prefix-tokenised view names."""
        q = parse_qualified_name("{{PFX}}_SEM_STD_V")
        assert q.object.fragments == (TokenRef("PFX"), "_SEM_STD_V")
        assert q.object.tokens == ("PFX",)
        assert not q.object.is_pure_literal
        assert not q.object.is_pure_token

    def test_token_suffix_object(self):
        q = parse_qualified_name("ENV_{{SUFFIX}}")
        assert q.object.fragments == ("ENV_", TokenRef("SUFFIX"))
        assert q.object.tokens == ("SUFFIX",)

    def test_token_infix_object(self):
        q = parse_qualified_name("pre_{{TOK}}_post")
        assert q.object.fragments == ("pre_", TokenRef("TOK"), "_post")
        assert q.object.tokens == ("TOK",)

    def test_multi_token_object(self):
        q = parse_qualified_name("{{ENV}}_{{SUFFIX}}")
        assert q.object.fragments == (
            TokenRef("ENV"),
            "_",
            TokenRef("SUFFIX"),
        )
        assert q.object.tokens == ("ENV", "SUFFIX")

    def test_qualified_token_on_both_sides(self):
        q = parse_qualified_name("{{DB}}.{{OBJ}}")
        assert q.database is not None and q.database.is_pure_token
        assert q.object.is_pure_token
        assert q.tokens == ("DB", "OBJ")

    def test_qualified_mixed_tokenisation(self):
        q = parse_qualified_name("{{DB_PREFIX}}_SEM.{{DB_PREFIX}}_SEM_STD_V")
        # Same token appearing twice — order preserved, duplicates kept.
        assert q.tokens == ("DB_PREFIX", "DB_PREFIX")
        assert q.database.fragments == (TokenRef("DB_PREFIX"), "_SEM")

    def test_inner_whitespace_in_token(self):
        q = parse_qualified_name("{{  TOK  }}_V")
        assert q.object.tokens == ("TOK",)


# ---------------------------------------------------------------------
# Legacy placeholder forms
# ---------------------------------------------------------------------


class TestLegacyPlaceholders:
    def test_dollar_bare(self):
        q = parse_qualified_name("$DB.MyTbl")
        assert q.database is not None
        assert q.database.fragments == (TokenRef("DB"),)

    def test_dollar_braced(self):
        q = parse_qualified_name("${DB}.MyTbl")
        assert q.database.fragments == (TokenRef("DB"),)

    def test_ampersand(self):
        q = parse_qualified_name("&&DB&&.MyTbl")
        assert q.database.fragments == (TokenRef("DB"),)

    def test_legacy_normalised_to_canonical(self):
        # Both forms should be indistinguishable post-parse.
        a = parse_qualified_name("$DB.X")
        b = parse_qualified_name("{{DB}}.X")
        assert a == b


# ---------------------------------------------------------------------
# Quoted identifiers
# ---------------------------------------------------------------------


class TestQuotedIdentifiers:
    def test_quoted_simple(self):
        q = parse_qualified_name('"My Table"')
        assert q.object.quoted
        assert q.object.fragments == ("My Table",)

    def test_quoted_db_quoted_obj(self):
        q = parse_qualified_name('"My DB"."My Tbl"')
        assert q.database is not None and q.database.quoted
        assert q.object.quoted

    def test_dot_inside_quotes_does_not_qualify(self):
        q = parse_qualified_name('"weird.name"')
        # The dot is inside quotes → no qualification.
        assert q.database is None
        assert q.object.quoted
        assert q.object.fragments == ("weird.name",)

    def test_quoted_supports_canonical_token(self):
        # Quoting still allows {{TOKEN}} substitution at the source level —
        # this matches how SHIPS treats env tokens orthogonally to SQL quoting.
        q = parse_qualified_name('"{{DB}}"')
        assert q.object.quoted
        assert q.object.fragments == (TokenRef("DB"),)

    def test_quoted_does_not_normalise_legacy(self):
        # Inside quotes the dollar form is literal, not a token.
        q = parse_qualified_name('"$NAME"')
        assert q.object.fragments == ("$NAME",)
        assert q.object.tokens == ()


# ---------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------


class TestResolution:
    def test_resolve_pure_literal(self):
        q = parse_qualified_name("MyDb.MyTbl")
        assert q.resolve({}) == "MyDb.MyTbl"

    def test_resolve_token_prefix(self):
        q = parse_qualified_name("{{PFX}}_V")
        assert q.resolve({"PFX": "PROD_X"}) == "PROD_X_V"

    def test_resolve_qualified(self):
        q = parse_qualified_name("{{DB}}.{{OBJ}}")
        assert q.resolve({"DB": "d", "OBJ": "o"}) == "d.o"

    def test_resolve_missing_token_strict_raises(self):
        q = parse_qualified_name("{{PFX}}_V")
        with pytest.raises(KeyError):
            q.resolve({})

    def test_resolve_missing_token_lenient_keeps_literal(self):
        q = parse_qualified_name("{{PFX}}_V")
        assert q.resolve({}, strict=False) == "{{PFX}}_V"

    def test_render_roundtrip(self):
        text = "{{DB_PREFIX}}_SEM.{{DB_PREFIX}}_SEM_STD_V"
        assert parse_qualified_name(text).render() == text


# ---------------------------------------------------------------------
# Convenience extractors
# ---------------------------------------------------------------------


class TestExtractors:
    def test_extract_tokens_canonical(self):
        assert extract_tokens("{{A}}_x.{{B}}") == ("A", "B")

    def test_extract_tokens_legacy_normalised(self):
        assert extract_tokens("$A.${B}.&&C&&") == ("A", "B", "C")

    def test_iter_token_refs_offsets(self):
        text = "x{{A}}y{{B}}"
        refs = list(iter_token_refs(text))
        assert refs == [(1, "A"), (7, "B")]


# ---------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------


class TestErrors:
    def test_empty_string(self):
        with pytest.raises(TokenisedNameError):
            parse_qualified_name("")

    def test_whitespace_only(self):
        with pytest.raises(TokenisedNameError):
            parse_qualified_name("   ")

    def test_none(self):
        with pytest.raises(TokenisedNameError):
            parse_qualified_name(None)  # type: ignore[arg-type]

    def test_trailing_dot(self):
        # "db." — empty object part should fail.
        with pytest.raises(TokenisedNameError):
            parse_qualified_name("db.")


# ---------------------------------------------------------------------
# Drop-in equivalence with analyser._normalise_qualified_name
# ---------------------------------------------------------------------
#
# The analyser today normalises a qualified name by stripping quotes and
# converting legacy placeholders to canonical {{TOKEN}} form. The new
# parser must produce the same string when re-rendered, otherwise it
# cannot replace _normalise_qualified_name in a later PR without changing
# graph identity in the wave analyser.


class TestAnalyserEquivalence:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # bare names pass through unchanged
            ("MyDb.MyTbl", "MyDb.MyTbl"),
            # canonical tokens unchanged
            ("{{DB}}.{{OBJ}}", "{{DB}}.{{OBJ}}"),
            # legacy forms normalised to canonical
            ("$DB.MyTbl", "{{DB}}.MyTbl"),
            ("${DB}.MyTbl", "{{DB}}.MyTbl"),
            ("&&DB&&.MyTbl", "{{DB}}.MyTbl"),
            # prefix-tokenised name normalised, suffix preserved
            ("{{PFX}}_SEM_STD_V", "{{PFX}}_SEM_STD_V"),
            # quoted parts stripped of quotes when rendered without `.quoted`
            # (the analyser does the same — quotes are presentation, not identity)
        ],
    )
    def test_render_matches_analyser_normalisation(self, raw, expected):
        q = parse_qualified_name(raw)
        # Mirror analyser behaviour: unquoted render reconstructs the
        # canonical identifier string used as a graph key.
        parts = []
        if q.database is not None:
            parts.append(q.database.render())
        parts.append(q.object.render())
        assert ".".join(parts) == expected

    def test_quoted_part_renders_inner_text_for_graph_key(self):
        q = parse_qualified_name('"My DB".MyTbl')
        # Graph-identity rendering: quotes removed, inner text preserved.
        assert q.database.render() == "My DB"
