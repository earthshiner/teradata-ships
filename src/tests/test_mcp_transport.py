"""
test_mcp_transport.py — Tests for SHIPS MCP server transport configuration.

The mcp package is not installed in the test environment. All tests mock
the entire mcp module tree in sys.modules before importing ships_mcp, so
the production code path (argument parsing -> settings mutation -> mcp.run())
can be exercised without a real mcp install.

Covers:
  - All three transports are accepted by the argument parser
  - HTTP-only flags are rejected when transport=stdio
  - Host, port, path, stateless, and log-level flags are applied to
    mcp.settings before run() is called
  - The startup banner is emitted for HTTP transports
  - Unknown transport values are rejected by argparse
"""

from __future__ import annotations

import sys
import types
from typing import List
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fake mcp module tree
# ---------------------------------------------------------------------------


class FakeSettings:
    """Minimal replica of FastMCP Settings for test assertions."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 8000
        self.log_level = "INFO"
        self.stateless_http = False
        self.streamable_http_path = "/mcp"
        self.sse_path = "/sse"


class FakeFastMCP:
    """Minimal replica of FastMCP for testing main()."""

    def __init__(self, *args, **kwargs):
        self.settings = FakeSettings()
        self._run = MagicMock()
        # Stub so tests that inspect real FastMCP internals don't AttributeError
        self._tool_manager = MagicMock()
        self._tool_manager._tools = {}

    def tool(self):
        """No-op decorator so @mcp.tool() works at ships_mcp module level."""

        def decorator(fn):
            return fn

        return decorator

    def run(self, transport="stdio"):
        self._run(transport=transport)


def _install_fake_mcp() -> FakeFastMCP:
    """
    Inject a fake mcp module tree into sys.modules and return the
    FakeFastMCP instance that ships_mcp will receive.
    """
    instance = FakeFastMCP()

    fake_fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    # The lambda ignores construction args and returns our shared instance.
    fake_fastmcp_mod.FastMCP = lambda *a, **kw: instance  # type: ignore

    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp_mod

    return instance


# ---------------------------------------------------------------------------
# Per-test fixture: fresh fake instance + fresh ships_mcp import
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_ships_mcp():
    """
    For each test: install a fresh FakeFastMCP, remove any cached ships_mcp,
    reimport it so the module-level mcp = FastMCP(...) gets our fake instance,
    then tear down — restoring any mcp.* sys.modules entries to their
    pre-test state so subsequent test files that use the real mcp package
    are not contaminated by the fakes.
    """
    MCP_KEYS = ["mcp", "mcp.server", "mcp.server.fastmcp"]

    # Snapshot the originals before we touch anything (None = didn't exist)
    originals = {k: sys.modules.get(k) for k in MCP_KEYS}
    ships_mcp_original = sys.modules.pop("ships_mcp", None)

    _install_fake_mcp()

    import ships_mcp  # noqa: F401

    yield

    # Teardown: drop ships_mcp and restore every mcp.* entry
    sys.modules.pop("ships_mcp", None)
    for key, original_value in originals.items():
        if original_value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original_value

    # Restore ships_mcp if it existed before the test (unlikely but correct)
    if ships_mcp_original is not None:
        sys.modules["ships_mcp"] = ships_mcp_original


def _run_main(argv: List[str]) -> None:
    """Invoke ships_mcp.main() with argv, restoring sys.argv after."""
    import ships_mcp as m

    old_argv = sys.argv
    try:
        sys.argv = ["ships_mcp"] + argv
        m.main()
    finally:
        sys.argv = old_argv


def _settings():
    """Return the FakeSettings attached to the current ships_mcp.mcp instance."""
    import ships_mcp as m

    return m.mcp.settings


def _run_mock():
    """Return the MagicMock tracking mcp.run() calls."""
    import ships_mcp as m

    return m.mcp._run


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------


class TestTransportSelection:
    """main() passes the correct transport string to mcp.run()."""

    def test_default_transport_is_stdio(self):
        """No --transport flag -> stdio."""
        _run_main([])
        _run_mock().assert_called_once_with(transport="stdio")

    def test_explicit_stdio(self):
        """--transport stdio calls mcp.run(transport='stdio')."""
        _run_main(["--transport", "stdio"])
        _run_mock().assert_called_once_with(transport="stdio")

    def test_streamable_http_transport(self):
        """--transport streamable-http calls mcp.run(transport='streamable-http')."""
        _run_main(["--transport", "streamable-http"])
        _run_mock().assert_called_once_with(transport="streamable-http")

    def test_sse_transport(self):
        """--transport sse calls mcp.run(transport='sse')."""
        _run_main(["--transport", "sse"])
        _run_mock().assert_called_once_with(transport="sse")

    def test_unknown_transport_rejected(self):
        """An unrecognised transport value causes SystemExit."""
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--transport", "websocket"])
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# HTTP-only flag validation
# ---------------------------------------------------------------------------


class TestHttpOnlyFlagValidation:
    """HTTP-only flags must be rejected when transport=stdio."""

    def test_host_with_stdio_raises(self):
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--host", "0.0.0.0"])
        assert exc_info.value.code != 0

    def test_port_with_stdio_raises(self):
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--port", "9000"])
        assert exc_info.value.code != 0

    def test_path_with_stdio_raises(self):
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--path", "/custom"])
        assert exc_info.value.code != 0

    def test_stateless_with_stdio_raises(self):
        with pytest.raises(SystemExit) as exc_info:
            _run_main(["--stateless"])
        assert exc_info.value.code != 0

    def test_host_with_streamable_http_accepted(self):
        """--host with streamable-http does not raise."""
        _run_main(["--transport", "streamable-http", "--host", "0.0.0.0"])

    def test_host_with_sse_accepted(self):
        """--host with sse does not raise."""
        _run_main(["--transport", "sse", "--host", "0.0.0.0"])


# ---------------------------------------------------------------------------
# Settings mutation
# ---------------------------------------------------------------------------


class TestSettingsMutation:
    """CLI flags are correctly applied to mcp.settings before run()."""

    def test_host_applied(self):
        _run_main(["--transport", "streamable-http", "--host", "192.168.1.10"])
        assert _settings().host == "192.168.1.10"

    def test_port_applied(self):
        _run_main(["--transport", "streamable-http", "--port", "9999"])
        assert _settings().port == 9999

    def test_log_level_applied(self):
        _run_main(["--transport", "streamable-http", "--log-level", "DEBUG"])
        assert _settings().log_level == "DEBUG"

    def test_stateless_applied(self):
        _run_main(["--transport", "streamable-http", "--stateless"])
        assert _settings().stateless_http is True

    def test_path_applied_to_streamable_http(self):
        _run_main(["--transport", "streamable-http", "--path", "/api/mcp"])
        assert _settings().streamable_http_path == "/api/mcp"

    def test_path_applied_to_sse(self):
        _run_main(["--transport", "sse", "--path", "/events"])
        assert _settings().sse_path == "/events"

    def test_no_flags_leave_defaults(self):
        _run_main([])
        s = _settings()
        assert s.host == "127.0.0.1"
        assert s.port == 8000
        assert s.stateless_http is False

    def test_combined_http_flags(self):
        """All HTTP flags applied together."""
        _run_main(
            [
                "--transport",
                "streamable-http",
                "--host",
                "10.0.0.5",
                "--port",
                "7777",
                "--path",
                "/ships/mcp",
                "--stateless",
                "--log-level",
                "WARNING",
            ]
        )
        s = _settings()
        assert s.host == "10.0.0.5"
        assert s.port == 7777
        assert s.streamable_http_path == "/ships/mcp"
        assert s.stateless_http is True
        assert s.log_level == "WARNING"

    def test_streamable_http_path_does_not_affect_sse_path(self):
        """--path with streamable-http must not change sse_path."""
        _run_main(["--transport", "streamable-http", "--path", "/custom"])
        assert _settings().streamable_http_path == "/custom"
        assert _settings().sse_path == "/sse"

    def test_sse_path_does_not_affect_streamable_http_path(self):
        """--path with sse must not change streamable_http_path."""
        _run_main(["--transport", "sse", "--path", "/my-sse"])
        assert _settings().sse_path == "/my-sse"
        assert _settings().streamable_http_path == "/mcp"


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


class TestStartupBanner:
    """HTTP transports emit a startup log line; stdio does not."""

    def test_streamable_http_emits_banner(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ships_mcp"):
            _run_main(["--transport", "streamable-http", "--port", "8888"])
        assert any("streamable-http" in r.message for r in caplog.records)
        assert any("8888" in r.message for r in caplog.records)

    def test_sse_emits_banner(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ships_mcp"):
            _run_main(["--transport", "sse", "--port", "7070"])
        assert any("sse" in r.message for r in caplog.records)

    def test_stdio_emits_no_banner(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ships_mcp"):
            _run_main([])
        assert not any(
            any(t in r.message for t in ("streamable-http", "sse", "endpoint"))
            for r in caplog.records
        )
