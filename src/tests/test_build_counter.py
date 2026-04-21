"""
test_build_counter.py — Tests for the SHIPS build number management.

Covers:
    - Reading the current build number
    - Incrementing (next_build_number)
    - Atomic write (temp-then-rename)
    - Reset functionality
    - Error handling (missing file, invalid content)
    - --no-increment promotion scenario
"""

import os
import pytest

from td_release_packager.build_counter import (
    read_build_number,
    next_build_number,
    reset_build_number,
    _write_counter,
    COUNTER_FILENAME,
)


# ---------------------------------------------------------------
# read_build_number
# ---------------------------------------------------------------

class TestReadBuildNumber:
    """Tests for reading the current build number."""

    def test_read_zero(self, tmp_path):
        """Freshly scaffolded counter reads 0."""
        (tmp_path / COUNTER_FILENAME).write_text("0\n", encoding="utf-8")

        assert read_build_number(str(tmp_path)) == 0

    def test_read_positive_number(self, tmp_path):
        """Counter with a positive integer reads correctly."""
        (tmp_path / COUNTER_FILENAME).write_text("42\n", encoding="utf-8")

        assert read_build_number(str(tmp_path)) == 42

    def test_read_strips_whitespace(self, tmp_path):
        """Leading/trailing whitespace is stripped."""
        (tmp_path / COUNTER_FILENAME).write_text("  15  \n", encoding="utf-8")

        assert read_build_number(str(tmp_path)) == 15

    def test_missing_counter_raises(self, tmp_path):
        """Missing .build_counter raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Build counter"):
            read_build_number(str(tmp_path))

    def test_invalid_content_raises(self, tmp_path):
        """Non-integer content raises ValueError."""
        (tmp_path / COUNTER_FILENAME).write_text("not_a_number\n", encoding="utf-8")

        with pytest.raises(ValueError, match="invalid"):
            read_build_number(str(tmp_path))

    def test_empty_file_raises(self, tmp_path):
        """Empty counter file raises ValueError."""
        (tmp_path / COUNTER_FILENAME).write_text("", encoding="utf-8")

        with pytest.raises(ValueError):
            read_build_number(str(tmp_path))


# ---------------------------------------------------------------
# next_build_number
# ---------------------------------------------------------------

class TestNextBuildNumber:
    """Tests for incrementing the build counter."""

    def test_increment_from_zero(self, tmp_path):
        """First increment: 0 → 1."""
        (tmp_path / COUNTER_FILENAME).write_text("0\n", encoding="utf-8")

        result = next_build_number(str(tmp_path))

        assert result == 1
        assert read_build_number(str(tmp_path)) == 1

    def test_increment_from_existing(self, tmp_path):
        """Increment from existing value: 11 → 12."""
        (tmp_path / COUNTER_FILENAME).write_text("11\n", encoding="utf-8")

        result = next_build_number(str(tmp_path))

        assert result == 12

    def test_successive_increments(self, tmp_path):
        """Multiple increments produce monotonically increasing numbers."""
        (tmp_path / COUNTER_FILENAME).write_text("0\n", encoding="utf-8")

        n1 = next_build_number(str(tmp_path))
        n2 = next_build_number(str(tmp_path))
        n3 = next_build_number(str(tmp_path))

        assert n1 == 1
        assert n2 == 2
        assert n3 == 3

    def test_counter_persisted_to_disk(self, tmp_path):
        """Counter value is persisted — survives re-read."""
        (tmp_path / COUNTER_FILENAME).write_text("5\n", encoding="utf-8")

        next_build_number(str(tmp_path))

        # Re-read from disk
        content = (tmp_path / COUNTER_FILENAME).read_text(encoding="utf-8").strip()
        assert content == "6"


# ---------------------------------------------------------------
# _write_counter
# ---------------------------------------------------------------

class TestWriteCounter:
    """Tests for the atomic write mechanism."""

    def test_write_creates_file(self, tmp_path):
        """Writing to a new location creates the counter file."""
        _write_counter(str(tmp_path), 99)

        assert read_build_number(str(tmp_path)) == 99

    def test_write_overwrites_existing(self, tmp_path):
        """Writing overwrites the existing counter value."""
        (tmp_path / COUNTER_FILENAME).write_text("10\n", encoding="utf-8")

        _write_counter(str(tmp_path), 20)

        assert read_build_number(str(tmp_path)) == 20

    def test_no_temp_file_left(self, tmp_path):
        """No .tmp file remains after successful write."""
        _write_counter(str(tmp_path), 5)

        tmp_file = tmp_path / (COUNTER_FILENAME + ".tmp")
        assert not tmp_file.exists()


# ---------------------------------------------------------------
# reset_build_number
# ---------------------------------------------------------------

class TestResetBuildNumber:
    """Tests for build counter reset."""

    def test_reset_to_zero(self, tmp_path):
        """Reset sets counter back to 0."""
        (tmp_path / COUNTER_FILENAME).write_text("42\n", encoding="utf-8")

        reset_build_number(str(tmp_path))

        assert read_build_number(str(tmp_path)) == 0

    def test_reset_to_specific_value(self, tmp_path):
        """Reset to a specific value."""
        (tmp_path / COUNTER_FILENAME).write_text("42\n", encoding="utf-8")

        reset_build_number(str(tmp_path), value=100)

        assert read_build_number(str(tmp_path)) == 100


# ---------------------------------------------------------------
# Promotion scenario (--no-increment)
# ---------------------------------------------------------------

class TestPromotionScenario:
    """
    Tests for the same-source promotion pattern.

    When promoting from DEV → PRD with --no-increment, the
    build number should remain the same. This is modelled by
    reading without calling next_build_number.
    """

    def test_no_increment_reads_same_number(self, tmp_path):
        """Reading without incrementing returns the same number."""
        (tmp_path / COUNTER_FILENAME).write_text("0\n", encoding="utf-8")

        # DEV build: increment
        dev_build = next_build_number(str(tmp_path))
        assert dev_build == 1

        # PRD promotion: read only (--no-increment)
        prd_build = read_build_number(str(tmp_path))
        assert prd_build == 1  # Same number, not incremented
