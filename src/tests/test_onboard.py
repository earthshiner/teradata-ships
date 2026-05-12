"""
test_onboard.py — Tests for the onboarding wizard pipeline.

Specifically covers _onboard_scan, _onboard_classify, and the
_cmd_onboard CLI entry point, which previously had zero test coverage.

The regression this suite guards:
    find_legacy_placeholders() gained a required file_path argument;
    _onboard_scan() was not updated, causing a TypeError at runtime.
    No existing test caught this because _onboard_scan was untested.

Test strategy:
    - Use tmp_path fixtures to build minimal source trees.
    - Import private helpers directly — they are stable implementation
      details that warrant direct coverage given their CLI criticality.
    - CLI invocation tests use argparse Namespace mocks to avoid
      subprocess overhead while still exercising the full call path.
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from io import StringIO
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers under test — imported directly
# ---------------------------------------------------------------------------

from td_release_packager.cli import _onboard_scan, _onboard_classify


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    """Write content to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _onboard_scan
# ---------------------------------------------------------------------------


class TestOnboardScan:
    """_onboard_scan — walks a source directory and classifies placeholder style."""

    def test_empty_directory_returns_zero_counts(self, tmp_path):
        """An empty directory produces all-zero counts without raising."""
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 0
        assert result["legacy_files"] == 0
        assert result["legacy_count"] == 0
        assert result["token_files"] == 0

    def test_non_sql_files_are_ignored(self, tmp_path):
        """Files with non-SQL extensions (e.g. .txt, .py) are not counted."""
        _write(tmp_path / "readme.txt", "This is not SQL.")
        _write(tmp_path / "helper.py", "SELECT 1")
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 0

    def test_plain_sql_file_counted(self, tmp_path):
        """A plain .sql file with no placeholders increments sql_files only."""
        _write(
            tmp_path / "plain.sql",
            "CREATE MULTISET TABLE MyDB.t (id INTEGER) PRIMARY INDEX (id);",
        )
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 1
        assert result["legacy_files"] == 0
        assert result["legacy_count"] == 0
        assert result["token_files"] == 0

    def test_ships_token_file_counted(self, tmp_path):
        """A file containing {{TOKEN}} style markers is counted in token_files."""
        _write(
            tmp_path / "tokenised.sql",
            "CREATE VIEW {{SEM_DATABASE}}.v AS SELECT 1 AS x;",
        )
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 1
        assert result["token_files"] == 1
        assert result["legacy_files"] == 0

    def test_legacy_dollar_var_counted(self, tmp_path):
        """A file containing $VAR legacy placeholders is counted in legacy_files."""
        _write(
            tmp_path / "legacy.sql",
            "SELECT * FROM $DATABASE.Customer;",
        )
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 1
        assert result["legacy_files"] == 1
        assert result["legacy_count"] >= 1

    def test_legacy_ampersand_var_counted(self, tmp_path):
        """A file containing &&VAR&& legacy placeholders is counted correctly."""
        _write(
            tmp_path / "legacy_amp.sql",
            "SELECT * FROM &&DATABASE&&.Orders;",
        )
        result = _onboard_scan(str(tmp_path))
        assert result["legacy_files"] == 1
        assert result["legacy_count"] >= 1

    def test_multiple_legacy_occurrences_aggregated(self, tmp_path):
        """Multiple legacy markers across files accumulate in legacy_count."""
        _write(tmp_path / "a.sql", "FROM $DB1.t1 JOIN $DB2.t2 ON t1.id = t2.id")
        _write(tmp_path / "b.sql", "FROM $DB3.t3")
        result = _onboard_scan(str(tmp_path))
        assert result["legacy_files"] == 2
        assert result["legacy_count"] >= 3

    def test_mixed_content_directory(self, tmp_path):
        """A directory with a mix of plain, token, and legacy files is counted correctly."""
        _write(tmp_path / "plain.sql", "CREATE TABLE MyDB.t (id INT);")
        _write(tmp_path / "token.tbl", "CREATE TABLE {{STD_DB}}.t (id INT);")
        _write(tmp_path / "legacy.sql", "FROM $OLD_DB.Customer")
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 3
        assert result["token_files"] == 1
        assert result["legacy_files"] == 1

    def test_subdirectory_walked_recursively(self, tmp_path):
        """Files in subdirectories are included in the scan."""
        _write(tmp_path / "sub" / "deep.sql", "SELECT 1;")
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 1

    def test_regression_find_legacy_placeholders_called_with_file_path(
        self, tmp_path, monkeypatch
    ):
        """Regression: _onboard_scan must pass file_path to find_legacy_placeholders.

        Before the fix, find_legacy_placeholders(content) was called without
        the required file_path argument, raising TypeError at runtime.
        This test fails if the call reverts to the one-argument form.
        """
        import td_release_packager.legacy_placeholders as lp

        calls = []
        original = lp.find_legacy_placeholders

        def _spy(content, file_path):
            """Spy wrapper — asserts file_path is always passed."""
            calls.append(file_path)
            return original(content, file_path)

        monkeypatch.setattr(lp, "find_legacy_placeholders", _spy)

        # Import after monkeypatching so cli.py picks up the spy
        from td_release_packager import cli as cli_mod
        monkeypatch.setattr(
            "td_release_packager.legacy_placeholders.find_legacy_placeholders",
            _spy,
        )

        _write(tmp_path / "check.sql", "SELECT * FROM $DB.t;")
        # This must not raise TypeError
        result = _onboard_scan(str(tmp_path))
        assert result["sql_files"] == 1


# ---------------------------------------------------------------------------
# _onboard_classify
# ---------------------------------------------------------------------------


class TestOnboardClassify:
    """_onboard_classify — maps scan results to a state label."""

    def test_no_files_returns_clean(self, tmp_path):
        """Empty source with no markers → CLEAN."""
        scan = {"sql_files": 0, "legacy_files": 0, "legacy_count": 0, "token_files": 0}
        assert _onboard_classify(scan, str(tmp_path)) == "CLEAN"

    def test_plain_files_returns_clean(self, tmp_path):
        """SQL files with no placeholder markers → CLEAN."""
        scan = {"sql_files": 3, "legacy_files": 0, "legacy_count": 0, "token_files": 0}
        assert _onboard_classify(scan, str(tmp_path)) == "CLEAN"

    def test_legacy_markers_returns_legacy(self, tmp_path):
        """Any legacy markers → LEGACY regardless of tokens."""
        scan = {"sql_files": 2, "legacy_files": 1, "legacy_count": 3, "token_files": 0}
        assert _onboard_classify(scan, str(tmp_path)) == "LEGACY"

    def test_tokens_without_config_returns_tokens_no_config(self, tmp_path):
        """SHIPS tokens present but no env config directory → TOKENS_NO_CONFIG."""
        scan = {"sql_files": 1, "legacy_files": 0, "legacy_count": 0, "token_files": 1}
        assert _onboard_classify(scan, str(tmp_path)) == "TOKENS_NO_CONFIG"

    def test_tokens_with_config_returns_ready(self, tmp_path):
        """SHIPS tokens present and a config/env/*.conf exists → READY."""
        conf_dir = tmp_path / "config" / "env"
        conf_dir.mkdir(parents=True)
        (conf_dir / "DEV.conf").write_text("STD_DATABASE=DEV_STD\n", encoding="utf-8")
        scan = {"sql_files": 1, "legacy_files": 0, "legacy_count": 0, "token_files": 1}
        assert _onboard_classify(scan, str(tmp_path)) == "READY"

    def test_legacy_takes_priority_over_tokens(self, tmp_path):
        """If both legacy markers and SHIPS tokens are present, LEGACY wins."""
        scan = {"sql_files": 2, "legacy_files": 1, "legacy_count": 1, "token_files": 1}
        assert _onboard_classify(scan, str(tmp_path)) == "LEGACY"


# ---------------------------------------------------------------------------
# _cmd_onboard — full CLI path
# ---------------------------------------------------------------------------


class TestCmdOnboard:
    """_cmd_onboard — end-to-end CLI handler, exercised via Namespace mock."""

    def _run(self, tmp_path: Path, env: str = "DEV", capsys=None) -> int:
        """Invoke _cmd_onboard against tmp_path and return the exit code."""
        from td_release_packager.cli import _cmd_onboard

        args = Namespace(source=str(tmp_path), env=env, auto=False)
        _cmd_onboard(args)  # raises SystemExit only on error

    def test_empty_source_runs_without_error(self, tmp_path, capsys):
        """onboard against an empty directory completes without raising."""
        self._run(tmp_path)

    def test_plain_sql_source_runs_without_error(self, tmp_path, capsys):
        """onboard against a plain (non-tokenised) SQL source completes."""
        _write(
            tmp_path / "table.tbl",
            "CREATE MULTISET TABLE MyDB.Orders (id INTEGER) PRIMARY INDEX (id);",
        )
        self._run(tmp_path)

    def test_legacy_source_runs_without_error(self, tmp_path, capsys):
        """onboard against a source with legacy $VAR markers completes.

        This is the primary regression guard: the TypeError from calling
        find_legacy_placeholders without file_path would surface here.
        """
        _write(tmp_path / "legacy.sql", "SELECT * FROM $OLD_DB.Customer;")
        self._run(tmp_path)  # must not raise TypeError

    def test_token_source_runs_without_error(self, tmp_path, capsys):
        """onboard against a tokenised source completes."""
        _write(
            tmp_path / "tokenised.sql",
            "CREATE VIEW {{SEM_DB}}.v AS SELECT 1 AS x;",
        )
        self._run(tmp_path)

    def test_invalid_source_exits_nonzero(self, tmp_path):
        """onboard with a non-existent source directory calls sys.exit."""
        from td_release_packager.cli import _cmd_onboard

        args = Namespace(
            source=str(tmp_path / "does_not_exist"), env="DEV", auto=False
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_onboard(args)
        assert exc_info.value.code != 0

    def test_output_includes_sql_file_count(self, tmp_path, capsys):
        """onboard prints the number of SQL files found."""
        _write(tmp_path / "a.sql", "SELECT 1;")
        _write(tmp_path / "b.tbl", "CREATE TABLE MyDB.t (id INT);")
        from td_release_packager.cli import _cmd_onboard

        args = Namespace(source=str(tmp_path), env="DEV", auto=False)
        _cmd_onboard(args)
        out = capsys.readouterr().out
        assert "SQL/DDL files found" in out
        assert "2" in out
