# Tokenisation — canonical surfaces (#383)

SHIPS tokenises a source tree so the same payload can be retargeted to many
environments. This page is the **single source of truth** for *where*
tokenisation logic lives, so consumers call the canonical functions instead of
re-implementing them.

## Canonical config

**`config/tokenise.conf`** is the canonical tokenisation config for a project —
the one file that declares how literals become `{{TOKEN}}`s. It is hand-edited
config (lives under `config/`, not `.ships/`). Resolve its path through
`td_release_packager.project_paths`:

| Use | API |
|-----|-----|
| File access (read/exists) | `tokenise_conf_path(project_dir)` |
| Catalogue / index reference (forward-slash) | `TOKENISE_CONF_RELPATH` (`"config/tokenise.conf"`) |
| Filename only | `TOKENISE_CONF_FILENAME` (`"tokenise.conf"`) |

Never hard-code `os.path.join(project_dir, "config", "tokenise.conf")` — a test
(`test_tokenise_conf_path.py`) fails the build if a join literal reappears.

The deprecated `--auto-tokenise` / `--token-map` flags remain for backward
compatibility but new projects should use `config/tokenise.conf` (regex-based,
applies to both file contents and filenames).

## Canonical logic surfaces

| Concern | Canonical home | Key functions |
|---------|----------------|---------------|
| Prefix tokenisation (`CallCentre_X` → `{{DB_PREFIX}}_X`) | `token_engine` | `tokenise_prefix`, `tokenise_prefixes`, `build_prefix_pattern` |
| Token substitution (resolve `{{TOKEN}}` → value) | `token_engine` | `substitute_tokens`, `substitute_file` |
| Token-map generation / IO | `token_engine` | `derive_token_name`, `generate_token_map`, `read_token_map`, `write_token_map` |
| `tokenise.conf` rule parse + apply | `source_migrator` | `parse_migration_sed`, `apply_migration_rules_to_text` |
| Project-local rule loading | `cli._load_project_legacy_migration_rules` | reads `tokenise_conf_path` → `parse_migration_sed` |
| Eponymous filename derivation | `eponymous_rename` | `derive_filename_from_text` (#365) |
| `--prefix-token SRC=TOK` arg parse | `cli._parse_prefix_token_args` | — |

Harvest (`ingest.py`) is the principal consumer: it calls `tokenise_prefixes`
for prefix mode and `apply_migration_rules_to_text` for `tokenise.conf` mode,
then re-derives eponymous filenames from the substituted body.

## Remaining consolidation candidate (not yet collapsed)

Two modules independently encode "what a tokenised database reference looks
like" as regexes:

- `eponymous_rename.py` — `_QUALIFIED_DDL_RE` / `_SINGLE_NAME_DDL_RE` (parses the
  DB/object out of a `CREATE` header, token-aware: `{{DB_PREFIX}}_DOM_STD_T`).
- `infer_grants.py` — `RE_TOKEN_REF` (matches `{{TOKEN}}[.suffix].Object` cross-
  database references in grant inference).

These are tuned to different contexts (DDL header vs body reference) and merging
them risks behaviour change in two well-tested paths, so they are **left as-is**
for now and flagged here as the next, more delicate consolidation step. Any merge
must keep the golden harvest/grant tests green.

## Why this matters

One canonical config path + one set of canonical functions means a change to the
tokenisation contract happens in one place, and a reader can find *the* place
quickly instead of grepping ten modules.
