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
