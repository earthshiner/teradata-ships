# Changeset detection & packaging

Issues [#114](https://github.com/earthshiner/teradata-ships/issues/114) (detection)
and [#115](https://github.com/earthshiner/teradata-ships/issues/115) (packaging).

`ships changeset` previews the set of payload objects that changed since a
reference point, plus every object that transitively depends on them.
`ships package --since-tag/--since-commit/--objects` then builds a *minimal*
package scoped to exactly that set.

## Usage

```bash
# Git-native: diff HEAD against a tag or commit (preferred in a git repo)
ships changeset --project . --since-tag v1.4.0
ships changeset --project . --since-commit 9a1b2c3

# Git-less: compare against a captured content-hash baseline
ships changeset --project . --update-baseline   # capture the baseline first
#   ... edit payload ...
ships changeset --project .                      # report what changed since
```

## Detection modes

| Mode | When | How |
|------|------|-----|
| `git` | `--since-tag`/`--since-commit` given **and** the project is a git repo | `git diff --name-only <ref>..HEAD` |
| `baseline` | no usable git ref, baseline present | per-file SHA-256 vs `.ships/changeset.baseline.json` |
| `none` | no git ref and no baseline | reports how to capture one; exit 1 |

The baseline is machine state and lives under `.ships/` (git-ignored), so each
developer / CI lane keeps its own.

## Dependants expansion

Changed files are mapped to qualified object names via the analyser index, then
a **forward breadth-first walk** over the dependency graph adds every object
that transitively depends on a changed one — a changed table pulls in the views
built on it, and the views built on those. The seed (directly changed) objects
are reported as `changed`; the pulled-in objects as `dependants`; the union as
`selected`. Cycles terminate safely and never re-add the seed.

## Output

```
Detection mode : baseline
Changed files  : 1
Changed objects: 1
Dependants     : 1
Total selected : 2

Changed objects:
  + DB.Customer

Dependants pulled in:
  ~ DB.ActiveCust
```

## Changeset-scoped packaging (#115)

`ships package` builds a minimal package from a changeset using the same three
selectors:

```bash
# Scope by git tag / commit (reuses #114 detection)
ships package --project . --env DEV --env-config config/env/DEV.conf \
    --name OMR_changeset --since-tag v1.4.2
ships package ... --since-commit abc1234

# Scope to an explicit object list (agent-driven partial deploy)
ships package ... --objects OMR_STD.Customer,OMR_STD.CustomerSummary
```

How it works:

1. Resolve the changed set (git/baseline) or take the explicit `--objects` list.
2. Forward-BFS the dependency graph to add dependants.
3. Stage a filtered copy of the project containing only those objects' files
   (plus `config/`, `.ships/`, `ships.yaml`), then run the **normal** build
   pipeline over it. The staged copy is throwaway; the project's build counter
   stays continuous.
4. Stamp `ships.build.json` with a `changeset` block:

   ```json
   "changeset": {
     "mode": "git",
     "base": "v1.4.2",
     "objects": ["DB.Customer", "DB.ActiveCust"],
     "changed": ["DB.Customer"],
     "dependants": ["DB.ActiveCust"]
   }
   ```

A changeset package is a first-class SHIPS package — same format, integrity
fingerprint, trust report, and deploy command. The deployer needs no special
handling; it just deploys what's in the payload. If nothing changed, no package
is built.

Scope note: changeset packaging selects DDL **objects** (and their dependants).
It assumes the target databases already exist (the normal case for an
incremental deploy on top of a prior full release); database/role prerequisites
are not auto-added to a changeset package.
