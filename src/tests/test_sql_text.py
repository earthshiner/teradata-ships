"""
test_sql_text.py — Tests for the shared SQL-text utilities module
(``td_release_packager.sql_text``).

Pins the position-preserving comment stripper. Three modules
depend on these guarantees: ``validate``, ``ingest``, ``builder``.
"""

from __future__ import annotations

from td_release_packager.sql_text import (
    strip_comments_preserving_positions as strip,
)


class TestStripCommentsPreservingPositions:
    def test_block_comment_blanked(self):
        result = strip("/* hello */CREATE TABLE x (a INT);")
        # Block comment characters become spaces, same length
        assert result == "           CREATE TABLE x (a INT);"
        assert len(result) == len("/* hello */CREATE TABLE x (a INT);")

    def test_line_comment_blanked(self):
        original = "CREATE TABLE x (a INT); -- trailing note"
        result = strip(original)
        # The space BEFORE -- is not part of the comment, so it stays.
        # The "-- trailing note" itself (16 chars) becomes 16 spaces.
        assert len(result) == len(original)
        assert result.startswith("CREATE TABLE x (a INT);")
        # Comment content is gone
        assert "trailing note" not in result
        # Padded out with spaces to original length
        assert result.endswith(" " * 16)

    def test_newlines_preserved_in_block_comment(self):
        text = "/* line 1\nline 2\nline 3 */CREATE TABLE x;"
        result = strip(text)
        # Newlines stay intact so line numbers don't shift
        assert result.count("\n") == text.count("\n") == 2
        # Length preserved
        assert len(result) == len(text)
        # Real DDL untouched
        assert result.endswith("CREATE TABLE x;")

    def test_dml_keywords_in_block_comment_blanked(self):
        """Specifically the case from GCFR_FF_IMGTableDelta_Create:
        a /* */ header containing 'CREATE TABLE' inside its
        natural-language description."""
        text = (
            "CREATE PROCEDURE x.foo()\n"
            "/* purpose: truncates the temp tables\n"
            "   then runs CREATE TABLE for the staging area */\n"
            "BEGIN\n"
            "    SET v = 1;\n"
            "END;"
        )
        result = strip(text)
        # The CREATE PROCEDURE survives (not in a comment)
        assert "CREATE PROCEDURE x.foo()" in result
        # The CREATE TABLE inside the comment is BLANKED
        assert "CREATE TABLE" not in result
        # The 'truncates' word is also blanked
        assert "truncates" not in result
        # BEGIN block survives
        assert "BEGIN" in result
        # Length and newline count preserved
        assert len(result) == len(text)
        assert result.count("\n") == text.count("\n")

    def test_line_comment_inside_block_comment_handled(self):
        """A -- inside /* */ should be blanked along with the
        surrounding block comment, not treated separately."""
        text = "/* before -- still in block */ CREATE TABLE x;"
        result = strip(text)
        # The -- and surrounding text all become spaces
        assert "--" not in result
        # Real DDL preserved
        assert "CREATE TABLE x;" in result

    def test_no_comments_unchanged(self):
        text = "CREATE TABLE x (a INT, b VARCHAR(10));"
        assert strip(text) == text

    def test_empty_input(self):
        assert strip("") == ""

    def test_combined_stripper_blanks_both(self):
        """The convenience ``strip_comments_and_string_literals``
        blanks both kinds in one pass, which is what every
        rule-check site actually wants."""
        from td_release_packager.sql_text import (
            strip_comments_and_string_literals,
        )

        text = (
            "/* purpose: builds CREATE TABLE dynamically */\n"
            "SET sql = 'CREATE MULTISET TABLE foo (a INT)';\n"
        )
        result = strip_comments_and_string_literals(text)
        # Comment AND string-literal contents are gone
        assert "purpose: builds CREATE TABLE" not in result
        assert "CREATE MULTISET TABLE" not in result
        # Length preserved
        assert len(result) == len(text)
        # Real surrounding code survives
        assert "SET sql =" in result

    def test_string_literal_stripped(self):
        """String literals like ``'CREATE TABLE foo'`` get blanked
        so regex content scans don't see the keyword inside."""
        from td_release_packager.sql_text import (
            strip_string_literals_preserving_positions as strip_lit,
        )

        text = "SET v = 'CREATE TABLE x.y (a INT)' || ',other';"
        result = strip_lit(text)
        # Literal content gone
        assert "CREATE TABLE" not in result
        assert "other" not in result
        # Surrounding code intact
        assert "SET v =" in result
        assert "||" in result
        # Length preserved
        assert len(result) == len(text)

    def test_string_literal_with_doubled_quotes_handled(self):
        """Teradata's ``'it''s a test'`` doubled-quote escape is
        a single literal, not two separate ones."""
        from td_release_packager.sql_text import (
            strip_string_literals_preserving_positions as strip_lit,
        )

        text = "SET v = 'it''s a test'; CREATE TABLE x.y (a INT);"
        result = strip_lit(text)
        # The literal 'it''s a test' is fully blanked
        assert "it" not in result
        assert "test" not in result
        # CREATE TABLE outside any literal survives
        assert "CREATE TABLE" in result

    def test_multiline_string_literal_blanked(self):
        from td_release_packager.sql_text import (
            strip_string_literals_preserving_positions as strip_lit,
        )

        text = (
            "SET v = 'line one\n"
            "CREATE TABLE inside literal\n"
            "line three';\n"
            "CREATE TABLE real.t (a INT);"
        )
        result = strip_lit(text)
        # The CREATE TABLE inside the multi-line literal is gone
        # but the one outside survives
        assert result.count("CREATE TABLE") == 1
        # Newlines preserved (line numbers stay accurate)
        assert result.count("\n") == text.count("\n")

    def test_position_preservation_enables_match_alignment(self):
        """The contract: a regex match position in the cleaned
        content also identifies the same span in the original.
        This is what makes surgical injection work without
        clobbering surrounding comments."""
        import re

        original = (
            "/* see related CREATE TABLE for staging */\n"
            "CREATE TABLE MyDB.Real (a INT);"
        )
        cleaned = strip(original)

        # Find the REAL CREATE TABLE in the cleaned content
        m = re.search(r"CREATE\s+TABLE\b", cleaned)
        assert m is not None

        # The same span in the original should also be 'CREATE TABLE'
        # (because position-preserving means non-comment chars at the
        # same offsets in both)
        assert original[m.start() : m.end()] == cleaned[m.start() : m.end()]
        assert original[m.start() : m.end()].upper() == "CREATE TABLE"
