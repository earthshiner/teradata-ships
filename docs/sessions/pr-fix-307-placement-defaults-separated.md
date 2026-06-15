# PR: default object_placement to separated + locking_views: true — #307

## Summary

Freshly scaffolded SHIPS projects landed on `strategy: colocated` +
`locking_views: false` — conservative defaults that put new projects
*outside* the Teradata field standard. The standard is to always
separate tables and views into sibling databases and always layer a
1:1 locking view in front of every table.

This PR flips both defaults to the standard for greenfield projects
and tightens the runtime default for the `locking_views` key.

## What's in the box

- **`src/td_release_packager/object_placement.py:324`** —
  `config.get("locking_views", True)` (was `False`).
  Configs that explicitly set `False` keep their behaviour;
  only the missing-key case changes.
- **`src/td_release_packager/scaffolder.py`** — active block in the
  generated `object_placement.yaml`:
  ```yaml
  strategy: separated
  database_pattern_tables: "{BASE}_T"
  database_pattern_views: "{BASE}_V"
  locking_views: true
  ```
  Surrounding comments reorganised: `separated` is now the
  documented DEFAULT; `mapped` and `colocated` are alternatives,
  with `colocated` reframed as "disable the standard".
- **`src/tests/test_object_placement.py`** — `test_locking_views_defaults_false`
  becomes `test_locking_views_defaults_true` with the new contract,
  plus a sibling test that an explicit `locking_views: false`
  still wins.
- **`src/tests/test_scaffolder_placement_defaults.py`** (new) —
  regression tests asserting the scaffolded yaml parses, loads via
  `ObjectPlacement`, resolves `ACME_DOM_T → ACME_DOM_V`, and that
  existing files are not overwritten.

## Test plan

- [x] `uv run pytest src/tests/test_object_placement.py src/tests/test_validate_placement.py src/tests/test_integration.py src/tests/test_grant_rules.py src/tests/test_scaffolder_placement_defaults.py -q` — 274 passed.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: `ships scaffold MyProj` and confirm the resulting
      `object_placement.yaml` matches the new template and is consumed
      by `ships harvest` without error.

## Out of scope

- Migrating existing scaffolded projects on disc — those files are
  user-owned. The scaffolder's existing "never overwrite" guard
  keeps in-flight projects untouched.
- Changing the regex pattern syntax or strategy vocabulary.

Closes #307.
