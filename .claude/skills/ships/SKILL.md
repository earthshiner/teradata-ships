---
name: ships
description: >
  SHIPS (Scaffold → Harvest → Inspect → Package → Ship) Teradata DDL/DCL/DML deployment pipeline
  in the teradata-ships repo. Use whenever working on any SHIPS phase or component — scaffold,
  harvest, inspect, package, ship, td_release_packager, database_package_deployer, token maps,
  deploy intents, wave ordering, Package Trust Score, inspect violations, Coding Discipline rules,
  preflight, rollback, FK scripts, dependency graphs, changeset/incremental deploys, detect-and-
  recommend planning, the SHIPS Navigator / CLI wizard, or catalogue metadata export, or any data
  product deployment task. Trigger on: SHIPS, td_release_packager, database_package_deployer,
  token map, tokenise.conf, deploy intent, wave ordering, Package Trust Score, inspect report,
  ships.yaml, preflight, decisions.json, onboard, ships changeset, ships plan, ships wizard,
  SHIPS Navigator, decision-tree.yaml, ships metadata export, Alation / Collibra / DataHub export,
  AI-native data product metadata. When a dependency graph or wave ordering visualisation is
  requested, delegate to the graph-discipline skill after collecting analysis data.
---

# SHIPS Skill

**SHIPS** = **S**caffold → **H**arvest → **I**nspect → **P**ackage → **S**hip

Five-phase Teradata DDL/DCL/DML deployment pipeline. Two Python packages:

| Package | Role |
|---|---|
| `td_release_packager` | Developer-side: scaffold, harvest, inspect, analyse, package |
| `database_package_deployer` | DBA-side: preflight, deploy, rollback, report |

**Run tests:** `uv run pytest src/tests/ -q` (the full suite, including the MCP tools). Format first with `uv run ruff format src/`.

---

## Pre-Flight Checklist

Before writing code or advising on any SHIPS task, confirm:

- [ ] Which phase is in scope? (Scaffold / Harvest / Inspect / Package / Ship)
- [ ] What is the project name and environment target?
- [ ] Is the token map applied and complete? (Harvest must run before Inspect)
- [ ] Have Inspect violations been cleared before attempting Package?
- [ ] Is `tmode=TERA` set on all Teradata connections in `database_package_deployer`?

---

## Repository Layout

```
src/
  td_release_packager/       ← Packaging pipeline
    classifier.py            ← Content-based DDL type detection
    analyser.py              ← Dependency graph; structural anchors; wave ordering
    context_artifacts.py     ← Agent-facing context artefacts (ships.context/manifest/handoff.json)
    ingest.py                ← Harvest pipeline; file splitting; token detection
    kind_suffix.py           ← Extension → kind suffix (_T, _V, _M, _P, _F)
    cli.py                   ← All CLI command handlers (_cmd_* functions)
    orchestrator/            ← SHIPS YAML, cascade decisions, issue codes
  database_package_deployer/ ← Deployment engine
    models.py                ← ObjectType, DeployStrategy, DEPLOY_ORDER, STRATEGY_MAP
    statement_parser.py      ← DDL parsing; name extraction; intent detection
    deployer.py              ← Core deployment execution engine
    preflight.py             ← Pre-flight space and permission checks
  tests/                     ← Full test suite (pytest, PYTHONPATH=src)
  ships_mcp.py               ← MCP server surface
docs/
  USER_GUIDE.md              ← Developer-facing command reference
  OPERATIONS_GUIDE.md        ← DBA/deployment reference
  MCP_GUIDE.md               ← Agent/MCP tool reference
```

---

## Phase Immutability Rules

Each phase reads from the prior phase's output only:

```
source/ ──[Harvest]──► payload/ ──[Inspect]──► report ──[Package]──► releases/ ──[Ship]──► Teradata
```

- Harvest does **not** modify `source/`
- Inspect reads `payload/` — **never** `source/`
- Package reads inspected `payload/` — **not** `source/`
- Ship reads the release ZIP — **not** `payload/`

To update any artefact, re-run its producing phase. Never hand-edit a downstream artefact.

---

## Phase Reference

### [S] Scaffold

Create the project directory tree. Idempotent — safe to re-run.

```bash
python -m td_release_packager scaffold --name MyProject --output ./projects
```

Output tree:
```
projects/MyProject/
├── config/
│   ├── env/              # DEV.conf, TST.conf, PRD.conf (TOKEN=value per env)
│   └── inspect.conf      # lint rule severities
├── payload/              # Tokenised DDL written by Harvest (payload/database/…)
├── releases/             # Packaged ZIPs written by Package
├── object_placement.yaml # object → database/layer placement rules
└── ships.yaml            # project master config (incl. optional packaging: profile)
```

Raw DDL is **not** a project subdir — it lives wherever your source is and is passed to Harvest/Process via `--source` (or `--source-github`). `config/tokenise.conf` (the canonical tokenisation config) is created on demand by Harvest/`migrate-source`, not by scaffold.

---

### [H] Harvest

Read raw DDL from `source/`, apply token substitution, write to `payload/`.

```bash
# Prefix tokenisation — turn a product prefix into {{DB_PREFIX}} in content + filenames
python -m td_release_packager harvest \
    --source /raw/ddl/ --project ./projects/MyProject \
    --prefix-token CustomerDNA=DB_PREFIX

# Or per-database tokenisation via the canonical config/tokenise.conf (regex rules)
python -m td_release_packager harvest \
    --source /raw/ddl/ --project ./projects/MyProject

# Re-harvest: always add --force to overwrite existing payload files
python -m td_release_packager harvest \
    --source /raw/ddl/ --project ./projects/MyProject --force
```

Token format in source/payload: `{{TOKEN_NAME}}`. **`config/tokenise.conf` is the canonical tokenisation config** (#383) — Harvest auto-applies it. The legacy `--token-map config/token_map.conf` and `--auto-tokenise`/`--generate-token-map` paths still work but are deprecated; prefer `--prefix-token` or `tokenise.conf` for new projects. After re-harvest, always re-run Inspect before Package. See `references/token_map.md` and `docs/references/tokenisation.md`.

---

### [I] Inspect

Lint the tokenised `payload/`. Exits non-zero on any violation.

```bash
python -m td_release_packager inspect --project ./projects/MyProject
```

Outputs: `inspect_report.json` (machine-readable) · `inspect_report.html` (human-readable). Auto-loads `config/inspect.conf` when `--config` is omitted.

No package may be built until Inspect exits zero. See `references/inspect_rules.md`.

---

### Scan (Token Validation)

Validate `{{TOKEN}}` references resolve against env configs.

```bash
python -m td_release_packager scan --project ./projects/MyProject --all-envs --fail-on-orphan
python -m td_release_packager scan --project ./projects/MyProject \
    --env-config config/env/DEV.conf --show-map
```

> **Note:** `scan` validates `{{TOKEN}}` references in the payload. To discover what to tokenise in raw source, prefer `ships plan` (auto-detects tokenisation state) or `harvest --prefix-token`; the legacy `harvest --generate-token-map` literal scan still works.

---

### [P] Package

Assemble the inspected payload into a versioned release artefact.

```bash
python -m td_release_packager package \
    --project ./projects/MyProject --env DEV --name MyProject \
    --env-config config/env/DEV.conf --output releases/

# GitHub as source (CI/CD pattern)
python -m td_release_packager package \
    --source-github myorg/myrepo --source-ref main \
    --env PRD --name MyProject --env-config config/env/PRD.conf
```

**Package Trust Score signals:**

| Signal | Fails when |
|---|---|
| `inspect_token_format` | INSPECT_TOKEN_MALFORMED error in inspect stage |
| `inspect_lint` | INSPECT_LINT_VIOLATION error in inspect stage |
| `inspect_grants` | INSPECT_GRANT_VIOLATION error in inspect stage |
| `provenance_complete` | `context/ships.provenance.json` absent from the package |
| `build_reproducible` | `source_dirty` flag set in `context/ships.build.json` |

**Trust label:** `READY` (all pass) · `READY-WITH-CAVEATS` (warnings only) · `BLOCKED` (any fail — do not deploy).

**Changeset-scoped (incremental) packaging** — build a *minimal* package of only changed objects plus their dependants instead of the whole payload (#114/#115):

```bash
# Preview what changed (git diff, or content-hash baseline fallback) + dependants
python -m td_release_packager changeset --project ./projects/MyProject --since-tag v1.4.0

# Build a package scoped to that changeset
python -m td_release_packager package --project ./projects/MyProject \
    --env DEV --name MyProject --env-config config/env/DEV.conf \
    --since-tag v1.4.0          # or --since-commit <sha>, or --objects DB.A,DB.B
```

`changeset` resolves changed files (git `diff <ref>..HEAD`, or a `.ships/changeset.baseline.json` hash baseline when not a git repo — capture with `--update-baseline`), maps them to qualified objects, then forward-walks the dependency graph to add every object that transitively depends on a changed one. A changeset package is a first-class SHIPS package (same format, trust report, deploy command); `ships.build.json` carries a `changeset` block recording the scope.

---

### Plan (Detect-and-Recommend)

Inspect a raw source tree, auto-answer the detectable questions, and emit the recommended `ships` command sequence + rationale + `plan.json` — non-interactive (#379).

```bash
python -m td_release_packager plan --source /raw/ddl --project ./projects/MyProject \
    --env DEV,TST --name MyProject --json plan.json
```

Auto-detects: filesystem source, whether the source is already `{{TOKEN}}`-tokenised, atomic vs compound (multi-object) files, and DCL/DML presence. CLI flags override any detected/defaulted answer. SQL is read as text, never executed.

### Wizard (Interactive CLI)

An interactive terminal front end over the same decision model — works over SSH where the offline HTML Navigator can't run (#381).

```bash
python -m td_release_packager wizard --source /raw/ddl --json plan.json
```

Walks the `decision-tree.yaml` questions (honouring show/warn rules), optionally pre-seeded from source detection, then emits the same plan as `ships plan`.

> The **SHIPS Navigator** (`tools/navigator/ships-navigator.html`) is an offline single-file HTML wizard producing the same plan. `ships plan`, `ships wizard`, and the Navigator all share one declarative model (`tools/navigator/decision-tree.yaml`, #378) and one plan-emission engine (`td_release_packager.packaging_plan`), so identical answers yield identical recommendations.

---

### [S-H-I-P-S] Process (Full Pipeline)

Primary entry point for CI/CD pipelines.

```bash
python -m td_release_packager process \
    --project ./projects/MyProject --source /raw/ddl/ \
    --env DEV --env-config config/env/DEV.conf --name MyProject
```

Key flags: `--auto-tokenise` · `--source-github myorg/repo` · `--strict` · `--pause` · `--skip-generate`.

**Single front door (#384):** with a `packaging:` profile in `ships.yaml` (source, package name, default env, env-config), `process --project .` runs the whole pipeline including package with no `--env`/`--env-config`/`--name` — precedence is explicit flag > `packaging:` profile > convention. The SHIPS Navigator / `ships plan` emit this `ships.yaml` for you.

---

### [S] Ship (Deploy)

Execute the package against live Teradata in wave order. Per-object snapshots captured for rollback.

```bash
python -m database_package_deployer deploy --dry-run <package_dir>
python -m database_package_deployer deploy --host myserver --user dbc <package_dir>
python -m database_package_deployer resume <path/to/.deploy_manifest.json>
python -m database_package_deployer rollback <path/to/.deploy_manifest.json>
python -m database_package_deployer status <path/to/.deploy_manifest.json>
```

Wave execution order: (1) System — Maps, Roles, Profiles, Authorisations, Foreign Servers; (2) Databases/Users; (3) Grants; (4) DDL — Tables → Views → Procedures/Macros/JARs → Indexes/FK → Triggers → DML.

A failed Ship does not require re-running Harvest, Inspect, or Package. See `references/deploy_intents.md`.

---

## Catalogue Metadata Export (AI-Native Data Products)

Export AI-native data-product metadata from a built package to an enterprise catalogue (#244). SHIPS extracts one **neutral product-metadata model** (identity, logical interfaces, physical assets, columns, lineage, trust state, provenance, access, design decisions) from the package's `context/*.json` + payload, then renders a catalogue-specific bundle. Adding a catalogue is a new renderer, not a re-read.

```bash
python -m td_release_packager metadata export-alation  --package-dir <unpacked-pkg> --output ./metadata
python -m td_release_packager metadata export-collibra --package-dir <unpacked-pkg> --output ./metadata
python -m td_release_packager metadata export-datahub  --package-dir <unpacked-pkg> --output ./metadata
```

| Catalogue | Primary artefact |
|---|---|
| **Alation** | modular JSON set (`data_product`, `logical_interfaces`, `physical_mappings`, `column_metadata`, `glossary_terms`, `lineage`, `quality_and_trust`, `access_model`, `provenance`, `decisions`) + `manifest` |
| **Collibra** | `collibra_import.json` — a Community → Domain → Asset resource graph (attributes + relations + lineage) + governance/provenance/manifest |
| **DataHub** | `datahub_mcps.json` — MetadataChangeProposals (dataset entities with `datasetProperties`/`subTypes`/`schemaMetadata`, `upstreamLineage`, a `dataProduct` entity) for DataHub's file source |

Conservative by design: views/macros are approved consumer-facing interfaces, tables internal unless `--include-internal`; ownership / glossary / AI-approval / classifications are emitted **only** when present in the package (never fabricated), with a warning otherwise. `--strict` fails on missing metadata. File-only export — no live catalogue API calls. SQL is parsed as text, never executed. See `docs/references/catalogue_metadata_export.md`.

---

## Deploy Intent Matrix

| DDL Verb in Source | Resolved Intent | Behaviour |
|---|---|---|
| `CREATE TABLE` | `IDEMPOTENT_DEPLOY` | Check for data; drop-and-recreate if empty |
| `REPLACE VIEW` | `REPLACE_IN_PLACE` | Execute REPLACE VIEW directly |
| `CREATE VIEW` | `CREATE_ONLY` | Fail if object already exists |
| `CREATE DATABASE` / `CREATE USER` | `DIRECT_EXECUTE` | Execute as-is |
| `REPLACE PROCEDURE` / `REPLACE MACRO` | `REPLACE_IN_PLACE` | Execute directly |
| `GRANT` / `REVOKE` | `DIRECT_EXECUTE` | Execute as-is |
| Join / Hash / Secondary Indexes | `DROP_AND_CREATE` | Drop unconditionally, recreate |
| Triggers | `DROP_AND_CREATE` | Drop unconditionally, recreate |
| System-scope objects | `SKIP_IF_EXISTS` | No-op if already present |
| `ALTER TABLE ... ADD FOREIGN KEY` (`.fk`) | `DIRECT_EXECUTE` | Execute as-is; deploy order 1 |

Full intent resolution and preflight rules: `references/deploy_intents.md`.

---

## File Extensions and Object Types

| Extension | Type | Deploy strategy | Subdir |
|---|---|---|---|
| `.tbl` | TABLE | IDEMPOTENT_DEPLOY | DDL/tables |
| `.viw` | VIEW | REPLACE_IN_PLACE | DDL/views |
| `.spl` | PROCEDURE | REPLACE_IN_PLACE | DDL/procedures |
| `.mcr` | MACRO | REPLACE_IN_PLACE | DDL/macros |
| `.fnc` | FUNCTION | REPLACE_IN_PLACE | DDL/functions |
| `.trg` | TRIGGER | DROP_AND_CREATE | DDL/triggers |
| `.jix` / `.idx` | JOIN/HASH/SECONDARY INDEX | DROP_AND_CREATE | DDL/join_indexes |
| `.fk` | FOREIGN_KEY | DIRECT_EXECUTE | DDL/alters |
| `.dcl` | GRANT/REVOKE | DIRECT_EXECUTE | DCL/inter_db |
| `.dml` | DML | DIRECT_EXECUTE | DML |
| `.cmt` | COMMENT | DIRECT_EXECUTE | DDL/comments |
| `.stt` | STATISTICS | DIRECT_EXECUTE | DDL/statistics |
| `.db` / `.usr` | DATABASE / USER | DIRECT_EXECUTE | pre-requisites/ |

Kind suffix: `_T` (tables, indexes, FK, DML, triggers), `_V` (views), `_M` (macros), `_P` (procedures), `_F` (functions). Type belongs in the DATABASE name only — never suffix the object name.

---

## Dependency Graph Visualisation

When the user requests a dependency graph, wave visualisation, or object relationship diagram, use the **graph-discipline** skill. Recommended options:

- **Option 6 (Sugiyama/Dagre)** — deployment wave ordering; nodes grouped by wave number
- **Option 1 (Force-directed)** — exploratory dependency analysis with search and hover
- **Option 2 (Columnar)** — top-N root objects or phase-based analysis

```python
from td_release_packager.analyser import analyse_project
result = analyse_project("./projects/MyProject/payload/database")

nodes = [
    {"id": qn, "label": qn.split(".")[-1], "type": obj.object_type,
     "wave": next((i for i, w in enumerate(result.waves) if qn in w), -1),
     "file": obj.file_path}
    for qn, obj in result.objects.items()
]
edges = [
    {"source": dep, "target": tgt}
    for dep, targets in result.dependencies.items()
    for tgt in targets
]
# result.cycles lists dependency cycles — flag in red
```

Pass nodes/edges to graph-discipline. Colour by `type`; group by `wave` for Sugiyama layout.

---

## MCP Server & Tools

### Transports

| Transport | When to use |
|---|---|
| `stdio` (default) | Claude Desktop / Claude Code — client launches server as a subprocess |
| `streamable-http` | Enterprise — server runs as a standalone HTTP service (MCP 2025-03-26) |
| `sse` | Legacy clients not yet on streamable-http (MCP 2024-11-05) |

```bash
# stdio (default — no flags needed)
python -m ships_mcp

# streamable-http — enterprise, all interfaces
python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000

# streamable-http — stateless mode for serverless / load-balanced deployments
python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000 --stateless
```

HTTP-only flags (`--host`, `--port`, `--path`, `--stateless`) are rejected when `transport=stdio`. All HTTP settings may also be set via `FASTMCP_*` env vars. TLS must be terminated at the network layer (reverse proxy / API gateway).

### JWT/Bearer Authentication

Authentication is supported for HTTP transports. SHIPS acts as an OAuth 2.0 Resource Server — it validates JWTs from your IdP via JWKS; it does not issue tokens.

```bash
python -m ships_mcp --transport streamable-http --host 0.0.0.0 --port 8000 \
    --auth-jwks-uri https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys \
    --auth-issuer   https://login.microsoftonline.com/{tenant}/v2.0 \
    --auth-audience api://ships-mcp \
    --auth-required-scopes ships.read,ships.deploy \
    --auth-resource-url http://ships-mcp.internal:8000
```

Auth flags: `--auth-jwks-uri` (required to enable) · `--auth-issuer` · `--auth-audience` · `--auth-required-scopes` · `--auth-resource-url` (required with `--auth-jwks-uri`). JWKS is cached for 1 hour with automatic refresh on unknown `kid`. Requires `PyJWT[crypto]` and `httpx` (both in `requirements.txt`). See `references/mcp_tools.md` for IdP JWKS URI examples.

### MCP Tools

The MCP surface mirrors the CLI so agents can drive the whole lifecycle non-interactively.

- **Pipeline (no DB):** `ships_scaffold`, `ships_harvest`, `ships_generate`, `ships_inspect`, `ships_analyse`, `ships_package`, `ships_process`.
- **Plan / changeset:** `ships_plan` (detect-and-recommend from a source tree → commands + rationale + `plan.json`), `ships_changeset` (preview changed objects + dependants). `ships_package` accepts `since_tag`/`since_commit`/`objects` for changeset-scoped builds, plus `source_github`/`source_ref`/`github_token`, `root_parent`, `change_ref`.
- **Catalogue export:** `ships_metadata_export` (`catalogue="alation"|"collibra"|"datahub"`).
- **Deployment (need `host`/`user`/`password`):** `ships_deploy`, `ships_rollback`, `ships_deploy_explain`.
- **Read-only / authoring:** `ships_decisions`, `ships_verify`, `ships_explain_run`, `ships_status`, `ships_describe_package`, plus the validate/author tools (`ships_validate_*`, `ships_author_*`), `ships_apply_diff`, `ships_explain_violation`, `ships_list_fixable_rules`, `ships_fix`, `ships_clean`.

`ships_process` derives `env`/`env_config`/`name` from the `ships.yaml` `packaging:` profile when omitted (#384), and accepts `source_github` for clone-free GitHub runs. The interactive `ships wizard` is intentionally CLI-only (MCP is non-interactive) — use `ships_plan` for the agent path.

Always check `trust_label` before `ships_deploy`. A `BLOCKED` label must not be deployed. See `references/mcp_tools.md`.

### End-to-end agentic flow

`ships_plan` (recommend) → `ships_process` (argless via `packaging:` profile, or from GitHub) → `ships_changeset` (scope an incremental redeploy) → `ships_package` (changeset-scoped, Trust-stamped) → `ships_verify` (gate on `trust_label`) → `ships_deploy` → `ships_metadata_export` (publish to Alation / Collibra / DataHub). Every step returns structured JSON with a recorded audit trail (`decisions.json`).

---

## Coding Discipline Quick Reference

Enforced by Inspect. Violations block Package.

- **Object names:** Never `_V`, `_T`, `_P`, `VW_`, `SP_`, `TBL_` affixes — type belongs in database name only
- **Tables:** Always `MULTISET`. Always explicit primary index.
- **Files:** Atomic eponymous — one CREATE/REPLACE per file, filename = object name
- **Views:** `REPLACE VIEW`. Never `CREATE VIEW` unless `CREATE_ONLY` intent is deliberate. Never `SELECT *`.
- **Companion files:** Every table file requires a companion `.stt` statistics file
- **GLOBAL TEMPORARY tables:** Require MaxTemp > 0 in DBC.DiskSpaceV — preflight must verify
- **INSERT syntax:** Each row is a separate `INSERT INTO … VALUES (…);` — no multi-row comma-separated VALUES
- **DDL separation:** `ddl/`, `viw/`, `dml/` are distinct directories — no DDL in DML directories
- **SQL style:** UPPERCASE keywords · leading commas · spaces only (no tabs)
- **Python:** Surgical changes only · DRY · fail fast · YAGNI · `<400` lines/block · Australian English
- **Branch workflow:** Feature branch → commit with detailed message → push → PR

Full rule set: `references/inspect_rules.md`.

---

## Common Error Patterns

| Error | Cause | Fix |
|---|---|---|
| `UnresolvedToken` | `{{…}}` remains in payload after Harvest | Add to token map; re-harvest with `--force` |
| `IntentMismatch` | DDL verb doesn't match resolved intent | Check deploy intent matrix; correct source DDL |
| `ObjectTypeSuffix` | Object name has `_V`, `TBL_`, etc. | Rename object; update all references |
| `MissingStatsFile` | Table has no companion `.stt` | Create `<ObjectName>.stt` alongside table file |
| `SelectStarViolation` | View body has `SELECT *` | Expand to explicit column list |
| `MultiRowInsert` | DML uses comma-separated VALUES | Split into individual `INSERT … VALUES` rows |
| `MaxTempNotChecked` | GLOBAL TEMPORARY in payload, no preflight | Add MaxTemp > 0 check to preflight |
| `GrantWaveOrder` | Grants deployed before owning database | Verify wave ordering in manifest |
| `find_legacy_placeholders` TypeError | Called without `file_path` argument | Pass `(content, path)` — two arguments required |

---

## Reference Files

| File | Load when… |
|---|---|
| `references/token_map.md` | Working on Harvest, token substitution, or env-specific config |
| `references/inspect_rules.md` | Diagnosing Inspect violations; writing new Inspect checks |
| `references/deploy_intents.md` | Working on wave ordering, Trust Score, preflight, or Ship logic |
| `references/commands.md` | Full CLI flag reference for all commands (incl. `plan`, `wizard`, `changeset`, `metadata export-*`) |
| `references/mcp_tools.md` | Full MCP tool reference (incl. `ships_plan`, `ships_changeset`, `ships_metadata_export`) |

For the newest capabilities, the canonical in-repo docs are: `docs/references/plan_command.md` (plan + wizard), `docs/references/changeset_detection.md` (changeset detect + package), `docs/references/catalogue_metadata_export.md` (Alation/Collibra/DataHub), `docs/references/tokenisation.md` (canonical tokenisation surfaces), and `tools/navigator/README.md` + `tools/navigator/decision-tree.yaml` (the shared decision model).
