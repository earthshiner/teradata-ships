"""Regression tests for the teradatasql connection character set contract.

Background
----------
teradatasql transmits SQL text as UTF-8 bytes on the wire and does not
accept a ``charset`` connection parameter — passing one raises a driver
error.  SHIPS must therefore never pass ``charset`` to
``teradatasql.connect()``.

However, Teradata's *server-side* session default is LATIN, which causes
it to misinterpret multi-byte UTF-8 sequences (such as em-dashes, bullets,
and arrows commonly found in DML seed files) as Latin-1 characters,
producing mojibake — e.g. em-dash "—" is stored as "â€"".

The fix is a ``SET SESSION CHARACTER SET UNICODE`` statement executed
immediately after the connection is opened.  This is a session-scoped
statement that instructs the Teradata server to treat the session's string
data as Unicode.  It requires no elevated privileges and does not affect
other sessions.

These tests enforce:
  1. ``charset=`` is NEVER passed to teradatasql.connect() (driver rejects it).
  2. ``SET SESSION CHARACTER SET UNICODE`` IS present in the CLI connect path.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# charset= parameter must never appear (driver rejects it)
# ---------------------------------------------------------------------------


def test_generated_deploy_template_does_not_pass_charset():
    text = _text("td_release_packager/builder.py")

    assert '"charset": "UTF8"' not in text, (
        "builder.py must not pass charset= to teradatasql.connect() — "
        "the driver rejects this parameter."
    )
    assert "'charset': 'UTF8'" not in text


def test_deployer_cli_does_not_pass_charset_to_connect():
    """The charset= parameter must not appear in the connect() call.

    Note: the string 'charset' may appear in comments explaining WHY
    it is not used — this test checks for the actual parameter syntax.
    """
    text = _text("database_package_deployer/cli.py")

    assert '"charset": "UTF8"' not in text, (
        "cli.py must not pass charset= to teradatasql.connect() — "
        "the driver rejects this parameter."
    )
    assert "'charset': 'UTF8'" not in text


def test_mcp_connection_paths_do_not_pass_charset():
    text = _text("ships_mcp.py")

    assert 'charset="UTF8"' not in text, (
        "ships_mcp.py must not pass charset= to teradatasql.connect() — "
        "the driver rejects this parameter."
    )
    assert "charset='UTF8'" not in text


# ---------------------------------------------------------------------------
# SET SESSION CHARACTER SET UNICODE must be present in the CLI connect path
# ---------------------------------------------------------------------------


def test_deployer_cli_sets_unicode_session():
    """After connecting, SHIPS must execute SET SESSION CHARACTER SET UNICODE.

    This is required so that Teradata's server-side session treats incoming
    UTF-8 string data as Unicode rather than Latin-1.  Without this, non-ASCII
    characters in DML seed files (em-dashes, bullets, arrows, etc.) are stored
    as mojibake.
    """
    text = _text("database_package_deployer/cli.py")

    assert "SET SESSION CHARACTER SET UNICODE" in text, (
        "cli.py must execute 'SET SESSION CHARACTER SET UNICODE' after "
        "connecting so that UTF-8 content in DML seed files is stored "
        "correctly by Teradata."
    )


def test_mcp_connection_paths_set_unicode_session():
    """All three MCP connect paths must also execute SET SESSION CHARACTER SET UNICODE.

    ships_mcp.py has three independent connect() calls (deploy, explain,
    rollback).  Each must set the Unicode session so that DML seed files
    with non-ASCII characters are stored correctly regardless of which
    MCP tool is invoked.
    """
    text = _text("ships_mcp.py")

    occurrences = text.count("SET SESSION CHARACTER SET UNICODE")
    assert occurrences >= 3, (
        f"Expected at least 3 occurrences of 'SET SESSION CHARACTER SET UNICODE' "
        f"in ships_mcp.py (one per connect path: deploy, explain, rollback), "
        f"but found {occurrences}."
    )
