# SHIPS Project — Codex Instructions

## Branching policy (MANDATORY)

**Never commit or push directly to `main`.**

All work — fixes, features, experiments — must go on a feature branch:

```
git checkout -b feat/<issue-number>-<short-slug>
# ... do the work ...
git push -u origin feat/<issue-number>-<short-slug>
gh pr create ...
```

Branch naming convention: `feat/<issue>-<slug>` (e.g. `feat/102-openlineage-deploy-events`).

Before starting any coding task, verify the current branch:

```
git branch --show-current
```

If it returns `main`, create a feature branch first — do not proceed on main.

## Commit hygiene

- One logical change per commit.
- Commit message format: `<type>(<scope>): <summary> — closes #<issue>`
- Run `uv run ruff format src/` before committing (the Stop hook does this automatically).

## PR workflow

- Create the PR with `gh pr create` after pushing the branch.
- Write the PR description to `docs/sessions/pr-<branch>.md` and link from the PR body.
- PRs target `main`; never force-push a PR branch.

## Testing

- Run `uv run pytest src/tests/ -q` before committing.
- All tests must pass — no known failures may be introduced.
