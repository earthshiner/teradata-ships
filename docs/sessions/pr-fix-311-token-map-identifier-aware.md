# PR: identifier-aware `--token-map` — closes Model B defect (#311)

## Summary

Real-data verification of Model B (#309) failed when the user drove
harvest through the existing `--token-map` flag instead of the new
`--prefix-token` one. The defect handover documented:

- `CallCentre_DOM_STD_T` / `_V` left **literal** in tables, views, and
  database DDL.
- Standalone `CallCentre` mangled into the **malformed** `{{PREFIX_T}}`.

Both are direct consequences of how
[`_apply_kind_aware_tokens`](src/td_release_packager/ingest.py) treats
every entry: `\b`-word-boundary matching (misses leading segments) and
unconditional `_T` / `_V` suffix injection inside the braces.

Decision (per Paul): make `--token-map` identifier-aware. Per-entry
mode detection picks the right path; full-DB entries are unchanged,
prefix-shape entries use the new no-suffix tokeniser already on `main`.

## Design

### Per-entry mode classification

New helper
[`_detect_prefix_mode_literals`](src/td_release_packager/ingest.py)
runs once per harvest, scanning every source file for `literal_<id>`
leading-segment patterns. Any literal that matches qualifies as
**prefix mode**. Detection key:

```
(?<![A-Za-z0-9_]) <literal> _[A-Za-z0-9]
```

### Substitution split

`_apply_kind_aware_tokens` accepts an optional
`prefix_mode_literals: Set[str]`. For literals in that set:

```
(?<![A-Za-z0-9_]) <literal> (?=_[A-Za-z0-9]|[^A-Za-z0-9_]|$)
```

— the right-edge look-ahead is **non-consuming**, so the structural
remainder (`_DOM_STD_T` etc.) stays as literal text outside the
braces, and adjacent characters can never land **inside** the braces.
The token value is emitted verbatim, no kind suffix.

Full-DB entries fall through to the original kind-aware path
(unchanged behaviour).

### Candidate-analyser fix

[`scan_payload_databases`](src/td_release_packager/mcp_authoring.py)
under-reported view-target databases because its extension allow-list
contained `.vw` but not `.viw` — the canonical SHIPS view extension.
Updated to mirror `kind_suffix.EXTENSION_TO_KIND`.

## What's in the box

- **`src/td_release_packager/ingest.py`** — `_detect_prefix_mode_literals`,
  `prefix_mode_literals` plumbed through `_place_ordered_sql`,
  `_place_multi_table_dml`, and `_apply_kind_aware_tokens`. One pre-scan
  per harvest run.
- **`src/td_release_packager/mcp_authoring.py`** — `_PAYLOAD_DDL_EXTENSIONS`
  expanded and aligned with `EXTENSION_TO_KIND`.
- **`src/tests/test_token_map_identifier_aware.py`** (new) — 12 tests:
  - 4 `_detect_prefix_mode_literals` classification cases.
  - 6 `_apply_kind_aware_tokens` cases including the 3 real-data
    fixtures from the handoff (`test_create_database_line`,
    `test_create_table_line`, `test_replace_view_line`).
  - 1 end-to-end `ingest_directory` regression — payload-wide
    `{{PREFIX_` guard.
  - 1 candidate-analyser regression — `.viw` extension scanned.

## Acceptance — verbatim from defect handoff §6

| Object | Source | After fix |
|---|---|---|
| `.db` | `create database CallCentre_DOM_STD_T from CallCentre as perm = 0.0 spool = 1.4E9 fallback ;` | `create database {{PREFIX}}_DOM_STD_T from {{PREFIX}} as perm = 0.0 spool = 1.4E9 fallback ;` |
| `.tbl` | `CREATE MULTISET TABLE CallCentre_DOM_STD_T.Call_H ,FALLBACK ,` | `CREATE MULTISET TABLE {{PREFIX}}_DOM_STD_T.Call_H ,FALLBACK ,` |
| `.viw` | `REPLACE VIEW CallCentre_DOM_STD_V.Call_H` | `REPLACE VIEW {{PREFIX}}_DOM_STD_V.Call_H` |
| anywhere | — | **no `{{PREFIX_` anywhere in payload** |

## Test plan

- [x] `uv run pytest src/tests/test_token_map_identifier_aware.py -q` — 12 passed.
- [ ] `uv run pytest src/tests/ -q --ignore=src/tests/test_environment_prereqs.py` — full suite green.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: re-run the user's CallCentre harvest with `--token-map CallCentre={{PREFIX}}` and verify the four acceptance files in the handoff.

## Out of scope

- Removing `--prefix-token`. It remains the explicit, single-purpose
  surface. The shared engine is `tokenise_prefix` in `token_engine`.
- Wholesale rewrite of the kind-suffix machinery; full-DB entries
  retain their `_T` / `_V` behaviour.

Closes #311. Carries forward from #309 / #310.
