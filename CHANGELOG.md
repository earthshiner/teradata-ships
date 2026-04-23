# Changelog

All notable changes to SHIPS are documented in this file.

## [0.3.0] — 2026-04-23

### Added

- **Structural-anchor reference scanner** — Replaced the broad `_QUALIFIED_REF_RE` pattern matcher with 19 structural-anchor regexes that only detect object references in SQL positions where object names are expected. Eliminates false positives from column aliases, DDL noise, and dot-separated tokens.
- **8 new structural anchors** — COLLECT STATISTICS ON, CALL (procedure), EXEC/EXECUTE (macro, excludes EXECUTE IMMEDIATE), LOCKING ... FOR, CREATE INDEX ON (covers JOIN/HASH/secondary), RENAME TABLE (captures both old and new names), DROP object (all DDL types), COMMENT ON TABLE/COLUMN.
- **Graph export module** (`graph_export.py`) — Five portable export formats for the dependency graph: DOT (`.gv`), Mermaid (`.mmd`), JSON (`.json`), CSV (`.csv`), OpenLineage NDJSON (`.openlineage.json`). Edge direction: deployment flow (dependency → dependent).
- **`--graph` flag on analyse** — Generates dependency graphs alongside wave ordering. `--formats` controls output formats. `--base-name`, `--namespace`, `--project-name` for OpenLineage customisation.
- **`--force` flag on harvest** — Overwrites existing files during re-harvest. Token regression warning when overwriting tokenised files with non-tokenised content.
- **Manifest re-deployment verification** (`prepare_for_redeploy`) — Verifies COMPLETED objects against the live database before re-deployment. Stale entries (e.g. after a DROP DATABASE) are automatically reset to PENDING. Prevents the "manifest says COMPLETED but database is empty" bug.
- **Thread-safe manifest I/O** — `_save()` uses `tempfile.mkstemp()` for unique temporary files per write, with `threading.Lock` on all mutating methods. Eliminates file-level collisions under parallel streams on Windows and Linux.
- **DCL serialisation lock** — GRANT, DATABASE, USER, ROLE, and PROFILE operations are serialised via `_dcl_lock` in the parallel executor to prevent Teradata deadlocks (Error 2631) on system catalogue tables. DDL remains fully parallel.
- **Transient error retry** — `_execute_ddl()` retries Error 3598 (concurrent change conflict, 0.5s/1s/2s backoff) and Error 2631 (deadlock, 2s/4s/8s backoff) up to 3 times with exponential backoff.
- **Deployer privilege check** (`privilege_check.py`) — Verifies the deploying user has CREATE + DROP rights on all target databases. Uses Teradata compound GRANT keywords (TABLE, VIEW, MACRO, PROCEDURE, FUNCTION, TRIGGER). Generates a prerequisite GRANT script for the System Administrator if privileges are missing. Databases being created by the package are skipped (automatic creator rights).
- **`WaveExecutionResult.prior_completed`** — Tracks objects completed in a prior deployment run. `is_noop_redeploy` property distinguishes "nothing new to deploy" from "nothing was processed".
- **`register_object()` state-aware re-registration** — PENDING/FAILED objects get metadata refreshed on re-registration. COMPLETED objects log at INFO with actionable guidance. Eliminates spurious "Duplicate qualified name" warnings.
- **Test suite additions** — `test_graph_export.py` (51 tests), `test_new_anchors.py` (35 tests), `test_analyser.py` expanded (31 → 53 tests). Suite total: 474 tests, all passing.

### Changed

- **EXPLAIN skip types** — Added PROCEDURE to `_EXPLAIN_SKIP_TYPES`. Teradata cannot EXPLAIN multi-statement procedure bodies (Error 3706). Functions support EXPLAIN (Error 3524 was a permissions issue, not an EXPLAIN limitation).
- **Error message cleanup** — `_execute_ddl` shows clean error at ERROR level, full Go trace at DEBUG only. CLI `_connect` strips Go stack trace, shows structured host/user/error output.
- **`_save()` exception handling** — Broadened from `PermissionError` to `OSError` to handle `WinError 2` (FileNotFoundError) from antivirus scanners and file indexers on Windows.
- **DOT export format** — Flat, minimal attributes for maximum Gephi compatibility. Only `label` on nodes (object type + qualified name baked in). No subgraphs, no global attribute blocks.
- **OpenLineage format** — NDJSON (one RunEvent per line, no wrapping array). `SQLJobFacet` (all-caps SQL per spec).

### Removed

- **`_QUALIFIED_REF_RE`** — Broad pattern matcher replaced by structural-anchor scanner.
- **`_IGNORE_PREFIXES` / `_IGNORE_SUFFIXES`** — No longer needed; structural anchors eliminate false positives at source.

## [0.2.0] — 2026-04-21

### Added

- **System/Environment scope distinction** — Objects are now classified as SYSTEM (Maps, Roles, Profiles, Authorisations, Foreign Servers) or ENVIRONMENT (everything else). System-scope objects deploy with SKIP_IF_EXISTS semantics and have no database qualifier or tokens.
- **`00_system` deployment phase** — New phase that executes before `01_pre_requisites`, containing system-scope objects.
- **SKIP_IF_EXISTS deploy strategy** — Checks object existence before deploying; skips silently if already present. Used for all system-scope types.
- **Five new object types** — MAP (`.map`), AUTHORIZATION (`.auth`), FOREIGN_SERVER (`.fsvr`), JAR (`.jar`), SCRIPT_TABLE_OPERATOR (`.sto`).
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
