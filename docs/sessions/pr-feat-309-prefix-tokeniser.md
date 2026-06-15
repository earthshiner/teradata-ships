# PR: identifier-aware prefix tokeniser (Model B) — #309

## Summary

Adds a third substitution mechanism to harvest, alongside the existing
literal `--token-map` (substring) and `--env-prefix` (strip + per-database).

The new **prefix tokeniser** rewrites a database-name *prefix* to a
single `{{TOKEN}}` while preserving the structural remainder, with
identifier-aware boundaries — so `CallCentre_DOM_STD_T` becomes
`{{PREFIX}}_DOM_STD_T` and a standalone `CallCentre` becomes
`{{PREFIX}}`, while `XCallCentre_DOM` and `MyCallCentreThing` stay
untouched.

This closes the gap that produced the malformed token
`{{PREFIX_T}}` on a real harvest of the CallCentre product using the
existing literal `--token-map` mechanism.

## What's in the box

### Commit 1 — pure tokeniser + tests

- `td_release_packager.token_engine`:
  - `build_prefix_pattern(prefix, case_insensitive=True)` — returns
    a compiled `re.Pattern` with a non-consuming right-edge
    look-ahead so the remainder stays as literal text.
  - `tokenise_prefix(text, prefix, token_name, …)` → `(rewritten, n)`.
  - `tokenise_prefixes(text, prefix_map, …)` → `(rewritten, total, per)`
    with longest-prefix-first ordering.
- `src/tests/test_prefix_tokeniser.py` — 22 tests covering happy path,
  the `{{PREFIX_*}}` regression guard, identifier-boundary negatives,
  idempotency, case sensitivity, multi-prefix dispatch, validation.

### Commit 2 — CLI + MCP wiring

- `td_release_packager.ingest`:
  - `ingest_directory(..., prefix_tokens: Optional[Dict[str, str]] = None)`.
  - Substitution runs on `raw_content` before statement splitting, so
    every downstream code path (single-statement, multi-target DML,
    ordered SQL) sees pre-tokenised content.
  - `IngestResult` gains `prefix_token_substitutions` and
    `prefix_token_files` counters.
- `td_release_packager.cli`:
  - New repeatable `--prefix-token SOURCE=TOKEN` on `harvest` and
    `process`.
  - `_parse_prefix_token_args` rejects malformed entries at argparse
    time.
  - Harvest prints a one-line summary of substitutions.
- `ships_mcp`:
  - `ships_harvest` and `ships_process` gain a `prefix_token`
    parameter taking a comma-separated `SOURCE=TOKEN` spec.
  - Malformed spec returns `success=False` with a friendly error.
  - New end-to-end test confirms `{{PREFIX}}_DOM_STD_T` lands in the
    placed `.tbl` file and `{{PREFIX_` never appears.

## Decisions taken (per Paul)

- **`case_insensitive=True` by default** — Teradata identifiers are
  case-insensitive; exact-case matching would tokenise inconsistently
  if reflected DDL ever emits mixed case.
- **No comment/string-span skipping in v1** — the pattern will also
  tokenise the prefix inside `COMMENT ON '…'` strings and
  `-- / /* */` comments. For database-name prefixes this is harmless
  (it renders back). Add skipping only if a real case bites.

## Surface contrast

| Mechanism | Shape | Example |
|---|---|---|
| `--token-map` | Literal substring substitution | `CallCentre={{PREFIX}}` |
| `--env-prefix` | Strips prefix; per-database tokens | `CallCentre_DOM_STD_T` → `{{DOM_STD_T}}` |
| **`--prefix-token`** | Identifier-aware prefix → single token | `CallCentre_DOM_STD_T` → `{{PREFIX}}_DOM_STD_T` |

## Test plan

- [x] `uv run pytest src/tests/test_prefix_tokeniser.py src/tests/test_mcp_server.py -q` — 49 passed.
- [x] `uv run ruff format src/` clean.
- [ ] Manual smoke: `ships_harvest` against the real CallCentre source with
      `prefix_token="CallCentre=PREFIX"` and assert:
      - `…\databases\CallCentre_DOM_STD_T.db` content reads
        `create database {{PREFIX}}_DOM_STD_T from {{PREFIX}} as …`.
      - No file under `payload\database` contains `{{PREFIX_`.
      - `DEV.conf` requires exactly one new token: `PREFIX=CallCentre`.

## Out of scope

- Auto-seeding `PREFIX=<value>` into the active env's `.conf`
  (deferred; `scan` / `package` will flag undefined tokens).
- Comment / string-span skipping (deferred behind real evidence).
- Maintained `ships` skill docs (`outputs/ships/`) — separate
  commit once code lands.

Closes #309.
