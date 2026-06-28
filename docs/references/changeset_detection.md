# Changeset detection (`ships changeset`)

Issue [#114](https://github.com/earthshiner/teradata-ships/issues/114).

`ships changeset` previews the set of payload objects that changed since a
reference point, plus every object that transitively depends on them. It is the
detection half of the changeset feature; building a minimal package from a
changeset follows in [#115](https://github.com/earthshiner/teradata-ships/issues/115).

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
