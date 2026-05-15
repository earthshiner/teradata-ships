"""Regression tests for the teradatasql connection contract.

The Teradata Python driver always uses UTF8 and rejects a `charset`
connection parameter. SHIPS must not emit that parameter from the
standalone deployer, generated deploy.py template, or MCP paths.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_generated_deploy_template_does_not_pass_charset():
    text = _text("td_release_packager/builder.py")

    assert '"charset": "UTF8"' not in text
    assert "'charset': 'UTF8'" not in text
    assert "SET SESSION CHARACTER SET" not in text


def test_deployer_cli_does_not_set_charset():
    text = _text("database_package_deployer/cli.py")

    assert '"charset": "UTF8"' not in text
    assert "'charset': 'UTF8'" not in text
    assert "SET SESSION CHARACTER SET" not in text


def test_mcp_connection_paths_do_not_pass_charset():
    text = _text("ships_mcp.py")

    assert 'charset="UTF8"' not in text
    assert "charset='UTF8'" not in text
