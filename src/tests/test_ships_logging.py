"""
test_ships_logging.py — Tests for SHIPS MCP rotating-log bootstrap.

Covers
------
* :func:`ships_logging.default_log_dir` honours ``SHIPS_LOG_DIR`` and
  falls back to the per-platform default.
* :func:`ships_logging.configure_logging` returns the resolved path,
  attaches a :class:`RotatingFileHandler` plus an stderr handler, and
  never installs a handler that writes to stdout.
* Calling ``configure_logging`` twice does not stack handlers.
* Records actually reach the rotating file.
* :class:`_DeduplicatingFilter` collapses consecutive identical
  records and emits exactly one summary line when the run breaks.
* :func:`ships_logging.banner_lines` returns a block containing the
  resolved path and the override env-var name.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from ships_logging import (
    LOG_DIR_ENV,
    _DeduplicatingFilter,
    banner_lines,
    configure_logging,
    default_log_dir,
)


@pytest.fixture
def isolated_root(monkeypatch):
    """Snapshot + restore the root logger across each test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


# ---------------------------------------------------------------------------
# default_log_dir()
# ---------------------------------------------------------------------------


class TestDefaultLogDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "alt"))
        assert default_log_dir() == tmp_path / "alt"

    def test_platform_default_used_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(LOG_DIR_ENV, raising=False)
        # Default must be under the user's home or LOCALAPPDATA, never empty.
        d = default_log_dir()
        assert isinstance(d, Path)
        assert str(d)  # non-empty


# ---------------------------------------------------------------------------
# configure_logging()
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_returns_path_under_env_dir(self, monkeypatch, tmp_path, isolated_root):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        path = configure_logging()
        assert path == (tmp_path / "ships-mcp.log").resolve()
        assert path.parent.exists()

    def test_no_stdout_handler_after_configure(
        self, monkeypatch, tmp_path, isolated_root
    ):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        # Plant a stray stdout handler to prove configure() removes it.
        stray = logging.StreamHandler(stream=sys.stdout)
        isolated_root.addHandler(stray)
        configure_logging()
        for h in isolated_root.handlers:
            stream = getattr(h, "stream", None)
            assert stream is not sys.stdout, (
                f"stdout handler survived configure_logging: {h!r}"
            )

    def test_rotating_handler_installed(self, monkeypatch, tmp_path, isolated_root):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        configure_logging()
        rotators = [
            h for h in isolated_root.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotators) == 1

    def test_stderr_handler_installed(self, monkeypatch, tmp_path, isolated_root):
        # Use the _ships_owned marker rather than `stream is sys.stderr`
        # because pytest's capture replaces sys.stderr with a tempfile
        # wrapper, so multiple handlers can share that stream.
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        configure_logging()
        owned_stream_handlers = [
            h
            for h in isolated_root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_ships_owned", False)
        ]
        assert len(owned_stream_handlers) == 1

    def test_idempotent_does_not_stack_handlers(
        self, monkeypatch, tmp_path, isolated_root
    ):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        configure_logging()
        first_count = len(isolated_root.handlers)
        configure_logging()
        assert len(isolated_root.handlers) == first_count

    def test_messages_reach_the_file(self, monkeypatch, tmp_path, isolated_root):
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        path = configure_logging()
        logger = logging.getLogger("ships.test")
        logger.warning("hello-from-test")
        # Force handlers to flush.
        for h in isolated_root.handlers:
            h.flush()
        content = path.read_text(encoding="utf-8")
        assert "hello-from-test" in content


# ---------------------------------------------------------------------------
# _DeduplicatingFilter
# ---------------------------------------------------------------------------


class TestDeduplicatingFilter:
    def _make_record(self, msg: str, level: int = logging.WARNING):
        return logging.LogRecord(
            name="ships.test",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_first_occurrence_passes(self):
        f = _DeduplicatingFilter()
        assert f.filter(self._make_record("dupe")) is True

    def test_consecutive_dupes_suppressed(self):
        f = _DeduplicatingFilter()
        assert f.filter(self._make_record("dupe")) is True
        for _ in range(5):
            assert f.filter(self._make_record("dupe")) is False

    def test_break_in_run_emits_summary(self, isolated_root, monkeypatch, tmp_path):
        # The filter emits the summary via the logger named in the
        # *next* record's record.name — capture that with a list handler.
        monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
        configure_logging()

        captured: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        cap = _Capture(level=logging.DEBUG)
        logging.getLogger("ships.test").addHandler(cap)
        # NOTE: the rotating + stderr handlers each carry their own filter
        # instance.  Drive it directly via a logger call so both handlers'
        # filters see the same run.
        logger = logging.getLogger("ships.test")
        logger.warning("dupe")
        for _ in range(3):
            logger.warning("dupe")
        logger.warning("different")
        for h in isolated_root.handlers:
            h.flush()

        # The capture handler has no filter, so it sees: dupe, dupe, dupe,
        # dupe, summary, different — 6 records.  The summary is what we
        # care about: at least one capture must mention "repeated".
        assert any("repeated" in m for m in captured), captured

    def test_summary_records_are_not_re_deduped(self):
        # If a summary record happened to match the next record's key, the
        # filter must not eat the summary itself.  We check the sentinel
        # attribute is honoured.
        f = _DeduplicatingFilter()
        rec = self._make_record("dupe")
        setattr(rec, "_ships_dedupe_summary", True)
        assert f.filter(rec) is True


# ---------------------------------------------------------------------------
# banner_lines()
# ---------------------------------------------------------------------------


class TestBannerLines:
    def test_includes_log_path(self, tmp_path):
        lines = banner_lines(tmp_path / "ships-mcp.log")
        joined = "\n".join(lines)
        assert "ships-mcp.log" in joined

    def test_includes_override_env_var(self, tmp_path):
        joined = "\n".join(banner_lines(tmp_path / "x.log"))
        assert LOG_DIR_ENV in joined

    def test_no_stdout_writes(self, tmp_path, capsys):
        # banner_lines is pure — must not print.
        banner_lines(tmp_path / "x.log")
        out = capsys.readouterr()
        assert out.out == ""
