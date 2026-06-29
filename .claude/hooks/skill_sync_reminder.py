"""
skill_sync_reminder.py — Stop-hook reminder to keep the SHIPS skill in sync.

Fires when the working tree has changes to the SHIPS CLI or MCP surface
(`cli.py` / `ships_mcp.py`) but NOT to the in-repo skill at
`.claude/skills/ships/`. Prints a non-blocking reminder so an agent updates the
skill (and mirrors it to the marketplace) in the same change.

Wired as a `Stop` hook in `.claude/settings.json`. Always exits 0 — advisory
only, never blocks. Silent when there's nothing to remind about.
"""

from __future__ import annotations

import subprocess
import sys

# Changing any of these surfaces is what the skill documents.
_SURFACE_PREFIXES = (
    "src/td_release_packager/cli.py",
    "src/ships_mcp.py",
)
_SKILL_PREFIX = ".claude/skills/ships/"


def _changed_paths() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    # Each line: "XY <path>" (XY = status code). Strip the 3-char prefix.
    return [line[3:].strip() for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    changed = _changed_paths()
    surface_touched = any(p.startswith(_SURFACE_PREFIXES) for p in changed)
    skill_touched = any(p.startswith(_SKILL_PREFIX) for p in changed)
    if surface_touched and not skill_touched:
        print(
            "[skill-sync] Reminder: you changed the SHIPS CLI/MCP surface but not the "
            "skill at .claude/skills/ships/. If a command, MCP tool, flag, or "
            "convention changed, update SKILL.md + references/ in this change "
            "(and mirror to the anthropic-skills marketplace). The "
            "test_skill_currency.py guard checks command/tool existence.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
