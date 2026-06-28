# Changelog

All notable changes to SHIPS are documented in this file.

## [Unreleased]

### Added

- **Catalogue metadata export — Alation + Collibra (#244)** — a new `ships metadata export-alation` / `export-collibra` command exports an enterprise-catalogue metadata bundle for an AI-native data product from a built SHIPS package. SHIPS extracts a single neutral `ProductMetadata` model (`td_release_packager.metadata_export`) from the package's context evidence (`ships.build.json` identity/provenance, `ships.dependencies.json` physical assets/interfaces/lineage, `ships.trust.json` + `ships.integrity.json` trust state, payload DDL columns, DCL access grants, design decisions when present), then projects it into per-catalogue JSON bundles — so adding a catalogue is a new renderer, not a re-read. The Alation bundle is the modular file set (`data_product`/`logical_interfaces`/`physical_mappings`/`column_metadata`/`glossary_terms`/`lineage`/`quality_and_trust`/`access_model`/`provenance`/`decisions` + `manifest`); the Collibra bundle is a `collibra_import.json` Community→Domain→Asset resource graph (with relations + lineage) plus governance/provenance/decisions/manifest. The extractor is conservative — views/macros are approved consumer-facing interfaces, tables internal unless `--include-internal`; owners/glossary/AI-approval/classifications are emitted only when present (never fabricated) with a warning otherwise; `--strict` fails on missing metadata. File-only export — direct API publishing is a later enhancement. See `docs/references/catalogue_metadata_export.md`.
- **`ships wizard` — interactive CLI front end (#381)** — a terminal wizard over the same `decision-tree.yaml` model (#378), so it works over SSH where the offline HTML Navigator can't reach. It walks the questions one at a time honouring the model's `show` visibility and `warn` rules (HTML stripped for plain-terminal display), optionally pre-seeds answers from source detection (`--source`, reusing #379), then emits the recommended command sequence + rationale and an optional `plan.json` (`--json`). The question loop is pure (injectable input/output) and fully tested; the same `packaging_plan` engine backs the HTML wizard, `ships plan`, and `ships wizard`, so identical answers yield identical plans.
- **`ships plan` — detect-and-recommend (#379)** — a new non-interactive command that inspects a raw source DDL tree, auto-answers the detectable questions (filesystem source, `{{TOKEN}}` already present, atomic/compound files, DCL/DML presence), and emits the recommended ordered `ships` command sequence, a per-step rationale, and a machine-readable `plan.json`. Detection is read-only and conservative (SQL scanned as text, never run; every decision reported with evidence; ambiguous signals fall back to model defaults), and CLI flags (`--project`/`--env`/`--name`/`--mode`/`--strict`/`--scaffolded`/`--no-generate`) override any detected answer. The command sequencing is a Python port of the Navigator's `renderScript`/`buildPlanJson` (`td_release_packager.packaging_plan`), so the HTML wizard, `ships plan`, and the CLI wizard (#381) produce identical recommendations from identical answers. See `docs/references/plan_command.md`.
- **Declarative decision model `decision-tree.yaml` (#378)** — the SHIPS packaging elicitation tree is now a single declarative source of truth at `tools/navigator/decision-tree.yaml`, shared by the HTML wizard, CLI wizard, and AI skill. Each question is data (`id`, `label`, `hint`, `kind`, `options`, optional `default`) with `show` / `warn` conditions in a small DATA-only DSL (`eq` / `ne` / `truthy` / `all` / `any` / `derived`) so every front end evaluates visibility identically. New `td_release_packager.decision_tree` loads, validates (fail-closed on duplicate id / bad kind / dangling field reference / unknown operator), and evaluates the model (`load_decision_tree`, `is_visible`, `active_warnings`). The offline HTML wizard keeps an inline embedding of the model (it can't read a file at `file://`); a lockstep test fails the build if the YAML and the inline `QUESTIONS` array drift.
- **Changeset-driven packaging (#115)** — `ships package` gained `--since-tag` / `--since-commit` / `--objects` to build a *minimal* package scoped to changed objects plus their dependants, instead of the whole payload. Detection reuses #114 (git diff with content-hash fallback); `--objects DB.A,DB.B` takes an explicit list for agent-driven partial deploys. The selected set is forward-BFS-expanded over the dependency graph, staged into a filtered copy of the project, and run through the normal build pipeline — so a changeset package is a first-class SHIPS package (same format, integrity fingerprint, trust report, deploy command), differing only in scope. `ships.build.json` is stamped with a `changeset` block (`mode`, `base`, `objects`, `changed`, `dependants`) so the DBA/deployer can see it is partial and what drove the scope. The project's build counter stays continuous (the staged copy is throwaway). No changed objects → nothing is packaged.
- **Git-native changeset detection (#114)** — a new `ships changeset --project <dir>` command previews the set of payload objects that changed since a reference point plus their downstream dependants, so a later step can build a minimal package. Detection is git-native when `--since-tag`/`--since-commit` is given inside a git repo (`git diff --name-only <ref>..HEAD`), falling back to a content-hash baseline under `.ships/changeset.baseline.json` otherwise (capture/refresh with `ships changeset --update-baseline`). Changed files map to qualified objects via the analyser index; a forward-BFS over the dependency graph pulls in every object that transitively depends on a changed one (a changed table pulls in the views built on it). Reported as `changed` + `dependants` + `selected`. Detection only — minimal-package builds follow in #115.
- **SHIPS Navigator emits `ships.yaml` packaging profile (#382)** — the offline wizard now generates a `ships.yaml` (new tab + included in the Download-all bundle) with a `packaging:` profile (source, package name, default env, env-config) capturing the wizard's answers, so `ships process --project .` re-runs argless. The generated document validates against the `ships.yaml` schema and is consumed by the single front door (#384).

- **Expensive-change lint rule (#175)** — a new built-in `teradata_expensive_change` rule (WARNING) flags physical changes that are O(table size) and lock-heavy on large tables: `ALTER TABLE … ADD … DEFAULT` (full table rewrite) and `CREATE INDEX` (non-unique secondary index build). Findings carry `requires_live_metadata`, a `recommended_precheck`, `possible_lock_impact`, `possible_spool_or_perm_impact`, and `requires_dba_review`. It deliberately avoids double-flagging the data-correctness cases already covered by `data_dependent_change` (`CREATE UNIQUE INDEX`, `ADD … NOT NULL`, PI/partition changes) and `destructive_change` (`DROP`/recreate).
- **Backward-incompatible contract-change check (#171)** — a new project-level `contract_change` rule (WARNING) captures a baseline of each object's contract (view columns, procedure parameters, table columns) and flags backward-incompatible changes against it: removed/renamed/reordered view columns, removed procedure parameters or changed direction/datatype, dropped or retyped table columns, or an object that disappeared. Added columns/parameters are compatible and not flagged. Capture/refresh the baseline (`.ships/contracts.baseline.json`) with `ships inspect --update-contract-baseline`; no-op until then. Set to ERROR when comparing against a governed baseline / previous release.
- **Token naming-convention rule (#172)** — a new built-in `token_naming` rule (WARNING) checks that a DDL object's database token carries the kind suffix matching its type: tables (and their indexes/triggers) in a `{{*_T}}` token, views in a `{{*_V}}` token. It flags only a clear mismatch (e.g. a view in a `_T` token) and leaves tokens without a kind suffix alone; the site-configurable macro/procedure/function kinds are left to custom lint policy (#167). Findings name the expected and actual suffix. Configurable via `config/inspect.conf`.
- **Dynamic SQL risk categories (#166)** — the `dynamic_sql` rule now classifies each finding into a `risk_category` carried in remediation: `dynamic_sql_execute_immediate`, `dynamic_sql_calls_sys_exec_sql`, `dynamic_sql_concatenates_literal`, and — highest risk — `dynamic_sql_uses_unsanitised_parameter` when a variable/parameter is concatenated (`||`) into executed SQL (possible injection), including cross-line `SET v = '…' || param` assembly that later feeds `EXECUTE IMMEDIATE`/`DBC.SYSEXECSQL`. Every finding instructs agents not to auto-remove dynamic SQL. One `dynamic_sql` config key still controls severity.
- **Transaction-control-in-payload lint rule (#173)** — a new built-in `transaction_control_in_payload` rule (WARNING by default) flags `BT;`/`ET;`, `BEGIN`/`END TRANSACTION`, `COMMIT`, and `ROLLBACK` in payload files. Transaction boundaries are owned by the SHIPS deployer. Detection runs on comment-stripped content (commented DI-tool tokens never fire) and skips procedure/function `BEGIN … END` bodies (exception-handler `ROLLBACK` exempt) while still catching a standalone `BEGIN TRANSACTION`. Findings identify phase, file, line, and statement; `--strict` promotes to ERROR.
- **Non-linear package-history check (#168)** — a new project-level inspect check (`non_linear_package_history`, WARNING by default) scans the built packages under `releases/` and flags a sequence that cannot be trusted: an integrity sidecar (`.sha256`) that no longer matches its archive, a package that `requires` a sibling missing from its release group, an orphaned `prereqs` half with no `main`, a release group whose archives disagree on build number/timestamp, a build number reused across groups with different contents, or an older build number appearing after a newer one. No-op when `releases/` is absent; set to ERROR for release/promotion workflows.
- **Data-dependent-change lint rule (#170)** — a new built-in `data_dependent_change` rule (WARNING by default) flags `ALTER TABLE` / `CREATE UNIQUE INDEX` operations on existing tables whose success depends on the data already present: adding `NOT NULL` without a `DEFAULT`, adding a `UNIQUE` constraint/index, adding a `CHECK` constraint, or changing `PRIMARY INDEX` / partitioning. `CREATE TABLE` (new, empty) is never flagged. Each finding carries `requires_live_metadata: true` and a `recommended_precheck` query so the risk can be assessed against the target environment. Set to ERROR in `config/inspect.conf` where prechecks are mandatory.
- **Destructive-change lint rule (#169)** — a new built-in `destructive_change` rule (ERROR by default) flags explicit destructive DDL in payload files: `DROP <object>`, `DELETE DATABASE`, and `ALTER TABLE … DROP`. The SHIPS deployer owns idempotent `CREATE`, so payloads should never carry destructive statements. Findings name the statement type and object, include the line number, and carry remediation metadata (`requires_human_review: true`, `agent_may_fix: false`) so agents stop rather than auto-fix or deploy. Statements inside a procedure/function `BEGIN … END` body (e.g. dropping a volatile table) are exempt. Configurable via `config/inspect.conf`.
- **Custom lint policy (#167)** — teams can declare project- or organisation-specific Teradata deployment rules as data in `config/ships_lint_policy.yaml`, applied by `ships inspect` alongside the built-in checks. Each rule is a `deny_pattern` / `required_pattern` (with optional `exclude_pattern`), scoped by `object_types` and `phases`, carrying a severity and agent-facing `remediation` metadata. Patterns are matched as text — SQL is never executed. Findings appear in the console and in `ships.decisions.json` (code `INSPECT_CUSTOM_POLICY`, remediation in `details`). A malformed policy fails closed under `--strict`; in developer mode invalid rules are skipped with a warning. See `docs/references/custom_lint_policy.md`.
- **Single front door — `process` packaging profile (#384)** — `ships process` now derives the package-stage inputs (`--name`, `--env`, `--env-config`, `--source`) from an opt-in `packaging:` block in `ships.yaml`, so the common case is `ships process --project .`. Precedence is CLI arg > `packaging:` block > convention (project name, first environment, `config/env/<ENV>.conf`). Without a `packaging:` block, behaviour is unchanged — packaging runs only when `--env`/`--env-config`/`--name` are all passed. `ships_yaml.validate()` gained schema validation for the `packaging:` block.
- **Build invocation provenance (#397)** — Package builds stamp a redacted `build_invocation` block (command, args, cwd, env-config, timestamp, SHIPS/Python versions) into `context/ships.build.json`, so "what command built this?" stays answerable after the package is distributed. The package report's Build Provenance tab falls back to it when the project-side `ships.decisions.json` is not reachable. Secret values (passwords, signing keys) are redacted before the snapshot is written.

### Changed

- **Machine state consolidated under `<project>/.ships/` (#283)** — `.build_counter`, `ships.decisions.json`, and `_waves.txt` live under the git-ignored `.ships/` directory, kept distinct from hand-edited `config/` and `payload/`. Wiping `.ships/` forces a clean rebuild with no risk to authored files. All path resolution flows through `project_paths`, and the `DECISIONS_FILENAME` constant is now single-sourced.

### Fixed

- **Harvest now splits multi-object files containing compound objects (#420)** — the splitter previously bailed out of any file containing `BEGIN` (procedure/function/trigger bodies) or `CREATE/REPLACE MACRO`, collapsing several objects into one and breaking topological (wave) ordering. A new compound-aware scanner tracks parenthesis *and* `BEGIN … END` depth (handling `END IF`/`END WHILE`/`END FOR`/`END LOOP` and `CASE … END`), so multi-object files split into one atomic object per file; genuinely unparseable files are left whole and flagged by `one_object`. The SHIPS Navigator heads-up and FAQ are updated accordingly.
- **Inspect: env-config passed as `--config` (#386)** — `read_inspect_config` now detects an env/token config accidentally passed via `--config` and fails fast with a pointer to `config/inspect.conf`, instead of silently loading `TOKEN=value` lines as invalid rule severities.
- **Inspect: clearer Step 0 failure reporting (#385)** — The Step 0 summary now distinguishes malformed-`{{TOKEN}}`-marker failures from token-coverage failures, rather than reporting a coverage failure with malformed-marker counters.
- **`process` package stage output crash** — Building via `process` without `--output` passed `output=None`, crashing the build at path join; it now defaults to `<project>/releases` (#384).

## [0.4.0] — 2026-05-03

### Fixed

- **Manifest replay bug — resume path** — `resume_package()` now invokes `manifest.prepare_for_redeploy()` before computing the resumable set. Previously, only `deploy_package()` verified stale COMPLETED entries against the database; resume would silently skip objects whose underlying database had been dropped or cleaned between runs. The check is correctly skipped in dry-run mode and when no live cursor is supplied.
- **Report Action Items section misleading on noop replay** — When a package was re-run with no work to do, the Action Items section claimed "all objects deployed successfully" — implying *this run* did the work. It now reads "all N object(s) were already deployed in a previous run and verified as still present in the database. Nothing was processed this run." Singular/plural grammar handled.
- **Report summary stat cards misleading on noop replay** — `result.completed` reflects every COMPLETED row in the manifest, including objects deployed in earlier runs, which made the summary cards look like a fresh deploy had happened. The Summary section is now mode-aware: in `REPLAY` mode it renders Total · Verified (prior) · Deployed (this run)=0 · Skipped · Failed, with a one-line caption explaining that the figures reflect prior-run state. Non-replay rendering is unchanged.

### Added

- **`REPLAY` report mode** — A new mode label, distinct from `DEPLOYMENT` / `DRY RUN` / `EXPLAIN`. Triggered when `PackageDeployResult.is_noop_redeploy` is true (no per-object results and at least one verified prior-completed entry). The header banner reads "REPLAY Report" so the DBA can tell at a glance that this run did not deploy anything.
- **Test coverage for the replay-bug fix** — 34 new unit tests across three modules:
  - `tests/test_manifest_replay.py` (21 tests) — `prepare_for_redeploy()` reset/keep/partial/check-failure paths, artefact clearing on reset, `get_prior_completed()` filtering, `register_object()` on COMPLETED entries, manifest reload, and `resume_package()` invocation of the redeploy check (including dry-run and missing-cursor short-circuits).
  - `tests/test_report_replay.py` (13 tests) — Mode banner (REPLAY vs DEPLOYMENT, with DRY RUN / EXPLAIN precedence), Action Items copy on noop replay, Summary stat cards on REPLAY vs normal mode, Object Results section.
  - `tests/test_deployer_models.py` — `TestIsNoopRedeploy` class added with 4 tests for the property's truth table.
- Suite total: **896 tests, all passing** (was 862).

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
