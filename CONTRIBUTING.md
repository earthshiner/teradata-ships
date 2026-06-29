# Contributing to SHIPS

## Workflow

See `.claude/CLAUDE.md` for the mandatory branching, commit, PR, and testing
policy. In short: never commit to `main`; work on `feat/<issue>-<slug>`; run
`uv run ruff format src/` and `uv run pytest src/tests/ -q` (all green) before
committing; open a PR to `main`.

## Keeping the SHIPS skill current

The agent-facing **SHIPS skill** lives in this repo at `.claude/skills/ships/`
(`SKILL.md` + `references/`). It is the version-controlled source of truth and is
also published to the `anthropic-skills` marketplace.

**When you change the SHIPS surface, update the skill in the same change:**

- A new / renamed / removed **CLI subcommand or flag** (`td_release_packager`)
  → update `SKILL.md` and `references/commands.md`.
- A new / changed **MCP tool** (`ships_mcp.py`) → update `SKILL.md` and
  `references/mcp_tools.md`.
- A new **capability, convention, or canonical surface** (e.g. tokenisation,
  packaging profile, catalogue export) → update the relevant section and the
  in-repo docs under `docs/references/`.

Then mirror the change to the marketplace copy so the published skill doesn't
drift.

### Guards

- `src/tests/test_skill_currency.py` fails the build if the skill references a
  `td_release_packager` subcommand or `ships_*` MCP tool that no longer exists,
  and asserts the latest tools are documented.
- A `Stop` hook (`.claude/hooks/skill_sync_reminder.py`, wired in
  `.claude/settings.json`) prints a non-blocking reminder when you change
  `cli.py` / `ships_mcp.py` without touching `.claude/skills/ships/`.

These are safety nets, not a substitute for updating the skill deliberately.
