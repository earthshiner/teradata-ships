# PR: classify COLLECT STATISTICS + CREATE DATABASE; parse-once; downgrade fallback warnings — Workstream C (#303)

## Summary

sqlglot's Teradata dialect emits a WARNING and falls back to a generic
`Command` node when it sees two valid Teradata shapes:

```
'create database GDEV1_BASE from GCFR_MAIN as perm = 0' contains
unsupported syntax. Falling back to parsing as a 'Command'.
```

```
'collect statistics column ( cust_id ) on db.t' contains
unsupported syntax. Falling back to parsing as a 'Command'.
```

In one real client session that produced **136 lines** of these
warnings for only **28 unique statements** — ≈5× duplication because
harvest, inspect, analyse, and package each re-parse the same DDL.

This PR closes the loop at three layers:

1. **Pre-classify** the two shapes before invoking sqlglot, so the
   parser is never asked to handle DDL it can't model and the WARNING
   is never emitted in the first place.
2. **Parse-once cache** keyed on `sha256(sql)` so the same statement
   only goes through sqlglot once per process across all phases.
3. **DEBUG downgrade** for any residual `Falling back …` warnings via
   a logging filter installed on the `sqlglot` logger at import time.
   Records are not dropped — they remain visible under a DEBUG handler.

## What's in the box

- **`src/td_release_packager/sql_reference_extractor_sqlglot.py`**:
  - `_KNOWN_UNSUPPORTED_RES` (regexes for COLLECT STATISTICS and
    CREATE DATABASE|USER … AS PERM …, including token sentinels and
    signed scientific-notation numerics).
  - `_KnownUnsupported` private exception that the existing
    `except Exception` paths catch transparently.
  - `_SqlGlotFallbackFilter` installed on `logging.getLogger("sqlglot")`.
  - Per-process `_parse_cache` (bounded at 4096 entries; FIFO eviction)
    + `clear_parse_cache()` for tests.
  - `_parse()` checks pre-classify → cache → sqlglot in that order.
- **`src/tests/test_sqlglot_extractor_classify.py`** (new) — 18 tests
  covering known-unsupported recognition, short-circuit silence,
  parse-once cache identity, and the WARNING downgrade.

## Test plan

- [x] `uv run pytest src/tests/test_sqlglot_extractor_classify.py -q` — 18 passed.
- [x] `uv run pytest src/tests/ -q -k "sqlglot or extractor or sql_reference"` — 83 passed (1 skipped, no regressions).
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: re-run the pipeline on a CallCentre-shape project
      and confirm 0 `Falling back to parsing as a 'Command'` WARNING
      lines in the SHIPS rotating log (#301) for the two named shapes.

## Out of scope

- Mixed `.tbl` statement splitter — the deployer already has its own
  splitter for execution; the analyser sees one statement at a time.
  If a future evidence stream shows mixed `.tbl` files driving parse
  noise, that becomes its own follow-up issue.
- Replacing sqlglot wholesale (it still handles everything else).

Closes #303. Third of three coordinated PRs (#301 / #302 already
open).
