"""ships_logging.py — SHIPS MCP server logging bootstrap.

Provides a single entry point, :func:`configure_logging`, that wires up a
rotating file handler plus an stderr handler for the SHIPS MCP server, and
returns the resolved log path so the startup banner in :mod:`ships_mcp` can
advertise it.

Design rules
------------
* stdout is reserved for the JSON-RPC transport.  NOTHING in this module
  may write to ``sys.stdout`` — doing so corrupts the MCP stream and
  disconnects the client.  ``configure_logging`` defensively detaches any
  pre-existing handler whose stream is ``sys.stdout``.
* The log path is resolved once and returned, so the startup banner can
  never drift from where logs are actually written.
* Repeated identical messages (e.g. the sqlglot parser-fallback warnings
  emitted by harvest → inspect → analyse → package on the same DDL) are
  collapsed by :class:`_DeduplicatingFilter`.  The filter keeps the first
  record in any run of duplicates and emits a summary line once a
  different record arrives, so the *fact* that duplicates occurred is
  preserved without flooding the log.

Default log directory
---------------------
The default per-user log directory follows platform convention and is
overridable via the ``SHIPS_LOG_DIR`` environment variable:

* Windows: ``%LOCALAPPDATA%\\SHIPS\\logs\\ships-mcp.log``
* POSIX:   ``~/.local/state/ships/logs/ships-mcp.log``

A rotating handler keeps the active log under 5 MB and retains the five
most recent rotations.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Logger name used throughout the SHIPS code base.  All SHIPS modules
#: should obtain their logger as ``logging.getLogger("ships.<module>")``
#: so the same handlers cover them; the root logger is also configured
#: so third-party libraries (sqlglot, anyio, mcp) roll through.
LOGGER_NAME = "ships"

#: Per-file size limit before rotation, in bytes (5 MiB).
MAX_BYTES = 5 * 1024 * 1024

#: Number of rotated backups retained (``ships-mcp.log.1`` … ``.5``).
BACKUP_COUNT = 5

#: Environment variable that overrides the default log directory.
LOG_DIR_ENV = "SHIPS_LOG_DIR"

#: Sentinel attribute used to mark dedupe summary records so they are
#: never themselves treated as duplicates by the filter.
_SUMMARY_ATTR = "_ships_dedupe_summary"


class _DeduplicatingFilter(logging.Filter):
    """Collapse runs of identical log records.

    When the same ``(level, message)`` pair repeats, only the first is
    emitted; once a different record arrives, the filter emits a single
    summary line on the same logger explaining how many duplicates were
    suppressed.

    The filter is intentionally stateful per instance — apply one
    instance per handler so the suppression state is bound to that
    output sink.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_key: tuple[int, str] | None = None
        self._suppressed: int = 0

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``True`` if ``record`` should be emitted.

        Summary records produced by this filter are tagged with
        :data:`_SUMMARY_ATTR` and always pass through untouched so they
        cannot recurse.
        """
        if getattr(record, _SUMMARY_ATTR, False):
            self._last_key = None
            self._suppressed = 0
            return True

        key = (record.levelno, record.getMessage())
        if key == self._last_key:
            self._suppressed += 1
            return False

        # A new (level, message) — flush any pending summary first.
        if self._suppressed > 0 and self._last_key is not None:
            summary_level = self._last_key[0]
            summary_message = (
                f"(previous message repeated {self._suppressed} more time(s))"
            )
            self._suppressed = 0
            logger = logging.getLogger(record.name)
            summary = logger.makeRecord(
                record.name,
                summary_level,
                record.pathname,
                record.lineno,
                summary_message,
                args=(),
                exc_info=None,
            )
            setattr(summary, _SUMMARY_ATTR, True)
            logger.handle(summary)

        self._last_key = key
        return True


def default_log_dir() -> Path:
    """Return the per-user log directory for the current platform.

    Honours :data:`LOG_DIR_ENV` when set.  On Windows defaults to
    ``%LOCALAPPDATA%\\SHIPS\\logs``; on POSIX to
    ``~/.local/state/ships/logs``.
    """
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(base) / "SHIPS" / "logs"
    return Path.home() / ".local" / "state" / "ships" / "logs"


def configure_logging(level: int = logging.INFO) -> Path:
    """Configure SHIPS MCP logging and return the resolved log file path.

    Attaches a :class:`~logging.handlers.RotatingFileHandler` and an
    stderr :class:`~logging.StreamHandler` to the root logger so that
    library logs (sqlglot, anyio, mcp, third-party) are captured
    alongside SHIPS code.  Defensively removes any pre-existing handler
    whose stream is :data:`sys.stdout` — stdout is the JSON-RPC channel
    and a stray handler there would corrupt the MCP transport.

    Safe to call more than once: previously installed SHIPS handlers
    are detached before new ones are added, so re-invocation yields a
    clean configuration rather than stacked handlers.

    :param level: Root logging level.  Defaults to ``logging.INFO``.
    :returns: Absolute path to the active rotating log file.
    """
    log_dir = default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "ships-mcp.log").resolve()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_DeduplicatingFilter())
    setattr(file_handler, "_ships_owned", True)

    # CRITICAL: stderr, never stdout — stdout is the JSON-RPC channel.
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(_DeduplicatingFilter())
    setattr(stderr_handler, "_ships_owned", True)

    root = logging.getLogger()

    # Drop any handler that targets stdout (defensive) and any SHIPS
    # handlers installed by a previous call so re-invocation is idempotent.
    for handler in list(root.handlers):
        targets_stdout = (
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, "stream", None) is sys.stdout
        )
        if targets_stdout or getattr(handler, "_ships_owned", False):
            root.removeHandler(handler)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    # Route ``warnings.warn(...)`` calls through logging too, so any
    # DeprecationWarning lands in the rotating file instead of stderr only.
    logging.captureWarnings(True)

    return log_path


def banner_lines(log_path: Path) -> list[str]:
    """Return the SHIPS-logging banner block for inclusion in the
    composite startup banner emitted by :mod:`ships_mcp`.

    Kept as a pure function so the caller controls how / where the
    block is rendered; the caller MUST emit to stderr (not stdout).

    :param log_path: The resolved path returned by
        :func:`configure_logging`.
    :returns: A list of banner lines, no trailing newline on each.
    """
    return [
        f"  Log file  : {log_path}",
        f"  Log dir   : {log_path.parent}  (override via ${LOG_DIR_ENV})",
        f"  Rotation  : {MAX_BYTES // (1024 * 1024)} MiB × {BACKUP_COUNT} backups",
    ]
