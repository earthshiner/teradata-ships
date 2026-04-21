# Changelog

All notable changes to SHIPS are documented in this file.

## [0.2.0] — 2026-04-21

### Added

- **System/Environment scope distinction** — Objects are now classified as SYSTEM (Maps, Roles, Profiles, Authorisations, Foreign Servers) or ENVIRONMENT (everything else). System-scope objects deploy with SKIP_IF_EXISTS semantics and have no database qualifier or tokens.
- **`00_system` deployment phase** — New phase that executes before `01_pre_requisites`, containing system-scope objects.
- **SKIP_IF_EXISTS deploy strategy** — Checks object existence before deploying; skips silently if already present. Used for all system-scope types.
- **Five new object types** — MAP (`.map`), AUTHORIZATION (`.auth`), FOREIGN_SERVER (`.fsvr`), JAR (`.jcl`), SCRIPT_TABLE_OPERATOR (`.sto`).
- **C source co-artefacts** — `.c` and `.h` files are carried in the package alongside function DDL for Teradata's inline compilation via `EXTERNAL NAME`.
- **Configurable validation rules** — `config/inspect.conf` controls which rules the inspector checks and at what severity (ERROR, WARNING, OFF). Scaffolder generates a default config.
- **`--config` flag on inspect** — Explicit config file path. Auto-detects `config/inspect.conf` in the project if not specified.
- **`--strict` mode updated** — Promotes all WARNING rules to ERROR. OFF rules remain off even in strict mode.
- **Filename resolution at package time** — The builder derives filenames from resolved DDL content so that package filenames match the target environment (e.g. `P_CORE.Customer.tbl`, not `DEV01_CORE.Customer.tbl`).
- **Programmatic tokenisation** — `--generate-token-map` and `--env-prefix` flags on the harvest command auto-generate a `config/token_map.conf` mapping file. `--token-map` applies the mapping during harvest. `--env-prefix` is optional — omit it for global databases.
- **System existence queries** — `SYSTEM_EXISTENCE_QUERIES` dict provides existence check SQL for system-scope objects (DBC.RoleInfoV, DBC.MapsV, DBC.ProfileInfoV, DBC.AuthorizationsV, DBC.ForeignServersV).
- **Unit test suite** — 368 tests across 9 test modules covering token engine, ingest, validate, analyser, DDL parser, build counter, deployer models, builder, and configurable rules.

### Changed

- **ROLE and PROFILE deploy strategy** — Changed from DIRECT_EXECUTE to SKIP_IF_EXISTS (system-scope).
- **ROLE and PROFILE file extensions** — Changed from `.sql`/`.db` to `.rol`/`.prf`.
- **USER file extension** — Changed from `.db` to `.usr`.
- **GRANT/REVOKE file extension** — Changed from `.sql` to `.dcl`.
- **Scaffolder directory structure** — System-scope objects moved from DDL subdirectories to `payload/database/system/`. Stale DDL entries (authorizations, foreign_servers, roles, databases, users) removed from DDL phase.
- **Validator** — `_check_db_qualifier` and `_check_hardcoded_names` now skip system-scope objects. `validate_directory()` accepts `rules_config` dict instead of individual boolean flags. The `set_multiset` rule name is aligned between config and check function.
- **Deploy ordering** — System objects deploy before pre-requisites. JARs and STOs added to ordering.
- **DDL_SUBDIR_ORDER** — Cleaned up: removed entries that now live in SYSTEM_SUBDIR_ORDER. JARs and STOs added.

## [0.1.0] — 2026-04-20

### Added

- Initial SHIPS implementation with Scaffold, Harvest, Inspect, Analyse, Package, Ship workflow.
- Intent-aware deployment (DDL verb IS the intent).
- Token interpolation with `{{TOKEN}}` placeholders and `.properties` files.
- Dependency analyser with topological sort and wave ordering.
- `--strict` mode for production builds.
- `--no-increment` flag for same-source environment promotion.
- `--dry-run` deployment without database connection.
- 15 object types supported.
- Logo suite (6 SVGs).
- Pitch deck (16 slides).
