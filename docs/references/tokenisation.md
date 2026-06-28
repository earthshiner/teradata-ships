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

## Token-reference grammar (`token_ref`)

The shape of a *tokenised database/object reference* — a `{{TOKEN}}`, a literal
name, a prefix-token + literal suffix (`{{DB_PREFIX}}_DOM_STD_T`), or a literal +
token suffix — is defined once in `td_release_packager.token_ref` as a set of
building blocks (`TOKEN_ATOM`, `QUOTED_IDENT`, `BARE_IDENT`, `OBJECT_NAME`,
`NAME_SEGMENT`, `DB_TOKEN_PART`, `DB_LITERAL_PART`). Both consumers compose their
context-specific regexes from this vocabulary instead of re-deriving the grammar:

- `eponymous_rename.py` — `_QUALIFIED_DDL_RE` / `_SINGLE_NAME_DDL_RE` build their
  name capture from `NAME_SEGMENT` (statement-anchored `CREATE` header parse).
- `infer_grants.py` — `RE_TOKEN_REF` and the DML/CREATE matchers build from
  `DB_TOKEN_PART` / `DB_LITERAL_PART` / `OBJECT_NAME` (mid-line cross-database
  references).

Each keeps its own anchoring and grouping (the *contexts* differ), but the
"what a tokenised reference looks like" grammar is no longer duplicated. The
golden harvest/grant test suites guard the consolidation.

## Why this matters

One canonical config path + one set of canonical functions means a change to the
tokenisation contract happens in one place, and a reader can find *the* place
quickly instead of grepping ten modules.
