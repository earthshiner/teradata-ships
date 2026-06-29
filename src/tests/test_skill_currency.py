"""
test_skill_currency.py — drift guard for the in-repo SHIPS skill.

The skill at ``.claude/skills/ships/`` documents CLI commands and MCP tools.
This test fails the build if the skill references a `td_release_packager`
subcommand or an `ships_*` MCP tool that no longer exists — catching the most
common way the skill silently drifts from the code.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_DIR = _REPO_ROOT / ".claude" / "skills" / "ships"

# `python -m td_release_packager <cmd>` or bare `td_release_packager <cmd>`.
_CLI_RE = re.compile(r"td_release_packager\s+([a-z][a-z-]+)")
# `ships_<tool>` identifiers mentioned in prose / tables / code fences.
_MCP_RE = re.compile(r"\bships_[a-z_]+\b")

# Tokens that look like commands/tools but are not (sub-subcommands, prose).
_CLI_IGNORE = {
    "export-alation",
    "export-collibra",
    "export-datahub",
}  # metadata sub-verbs
_MCP_IGNORE = {
    "ships_yaml",  # the config file, not a tool
    "ships_mcp",  # the server module, not a tool
}


def _skill_files():
    return sorted(_SKILL_DIR.rglob("*.md"))


def test_skill_dir_exists():
    assert _SKILL_DIR.is_dir(), f"SHIPS skill not found at {_SKILL_DIR}"
    assert (_SKILL_DIR / "SKILL.md").is_file()


def _cli_subcommands() -> set:
    from td_release_packager.cli import _build_parser

    parser = _build_parser()
    sub_action = next(
        a for a in parser._subparsers._group_actions if hasattr(a, "choices")
    )
    return set(sub_action.choices.keys())


def _mcp_tool_names() -> set:
    import ships_mcp

    return set(ships_mcp.mcp._tool_manager._tools.keys())


def test_skill_cli_references_are_live():
    valid = _cli_subcommands()
    offenders = {}
    for f in _skill_files():
        text = f.read_text(encoding="utf-8")
        for cmd in _CLI_RE.findall(text):
            if cmd in _CLI_IGNORE or cmd in valid:
                continue
            offenders.setdefault(f.name, set()).add(cmd)
    assert not offenders, (
        "Skill references unknown td_release_packager subcommands "
        f"(update the skill or the CLI): {offenders}"
    )


def test_skill_mcp_tool_references_are_live():
    valid = _mcp_tool_names()
    offenders = {}
    for f in _skill_files():
        text = f.read_text(encoding="utf-8")
        for tool in _MCP_RE.findall(text):
            # Trailing-underscore tokens are wildcard families in prose
            # (e.g. ``ships_author_*`` / ``ships_validate_*``), not real names.
            if tool.endswith("_") or tool in _MCP_IGNORE or tool in valid:
                continue
            offenders.setdefault(f.name, set()).add(tool)
    assert not offenders, (
        "Skill references unknown ships_* MCP tools "
        f"(update the skill or the MCP server): {offenders}"
    )


@pytest.mark.parametrize(
    "tool", ["ships_changeset", "ships_plan", "ships_metadata_export"]
)
def test_latest_tools_are_documented(tool):
    """The capabilities added this cycle must appear in the skill."""
    blob = "\n".join(f.read_text(encoding="utf-8") for f in _skill_files())
    assert tool in blob, f"{tool} missing from the SHIPS skill"
