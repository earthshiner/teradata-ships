# SHIPS skill (repo source of truth)

This directory is the version-controlled source of the **`ships`** Claude skill —
the one that teaches an agent how to drive the SHIPS pipeline
(`td_release_packager` + `database_package_deployer`, the MCP server, and the
guided-packaging / changeset / catalogue-export capabilities).

Claude Code auto-discovers skills under `.claude/skills/`, so the skill is live
when working in this repo. It is also published to the `anthropic-skills`
marketplace; keep the two in sync by editing **here** and mirroring to the
marketplace copy.

## Layout

```
.claude/skills/ships/
  SKILL.md                 # entry point: phases, commands, MCP tools, discipline
  references/
    commands.md            # full CLI flag reference
    mcp_tools.md           # MCP tool params + returns
    inspect_rules.md       # lint rule catalogue
    deploy_intents.md      # intent matrix, wave ordering, trust
    token_map.md           # token resolution (tokenise.conf canonical; token_map legacy)
  agents/
    openai.yaml            # agent manifest
```

## Keeping it current

When a SHIPS capability changes (new CLI command, MCP tool, flag, or convention),
update `SKILL.md` and the relevant `references/*.md` in the same PR as the code
change. The canonical in-repo capability docs live under `docs/references/`
(`plan_command.md`, `changeset_detection.md`, `catalogue_metadata_export.md`,
`tokenisation.md`) and `tools/navigator/` — the skill summarises those.
