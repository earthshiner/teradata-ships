# SHIPS FAQ

Answers to the most common questions. Organised by topic — jump to the section that matches your problem.

---

- [Tokenisation and database names](#tokenisation-and-database-names)
- [Harvest and classification](#harvest-and-classification)
- [Inspect errors and warnings](#inspect-errors-and-warnings)
- [Packaging and trust](#packaging-and-trust)
- [Deployment](#deployment)
- [Dependency ordering and waves](#dependency-ordering-and-waves)
- [Environment promotion](#environment-promotion)
- [Schema drift detection](#schema-drift-detection)
- [Audit trail and decisions.json](#audit-trail-and-decisionsjson)
- [Security](#security)
- [Clearscape demo notebooks](#clearscape-demo-notebooks)
- [General](#general)

---

## Tokenisation and database names

> ⚠️ **Deprecation note (closes [#388](https://github.com/earthshiner/teradata-ships/issues/388)):** `token_map.conf` and the `--token-map` / `--generate-token-map` flags are kept for back-compatibility but should not be used in new projects. **Prefer `config/tokenise.conf`** — regex-based with capture groups, strictly more powerful. Authored via the SHIPS Navigator wizard (`tools/navigator/ships-navigator.html`), the `ships_author_token_map` MCP tool, or by hand. See `examples/callcentre/config/tokenise.conf` for a working example. The legacy guidance below still works.

### My DDL still has hardcoded database names after harvest. Nothing was tokenised.

Two things to check:

**1. Did you pass `--token-map`?**

Harvest does not apply tokens unless you tell it which map to use:

```bash
python -m td_release_packager harvest \
    --source /my/sql/ \
    --project /my/project/ \
    --token-map config/token_map.conf   ← required
```

If `token_map.conf` does not exist yet, generate it first with `--generate-token-map`:

```bash
python -m td_release_packager harvest \
    --source /my/sql/ \
    --project /my/project/ \
    --generate-token-map \
    --env-prefix A_D01
```

**2. Is the database name in `token_map.conf`?**

Open `config/token_map.conf`. If the hardcoded name is not listed there, SHIPS does not know to tokenise it. Add an entry:

```
A_D01_MY_DB={{MY_DB}}
```

Then re-run harvest with `--token-map`.

---

### I ran `--generate-token-map` but my database name is not in the map.

SHIPS only generates entries for names it finds in SQL positions where a database qualifier is expected (FROM clause, table references, etc.). Possible causes:

- The file uses a non-standard DDL verb SHIPS does not recognise — check for `UNCLASSIFIED` warnings in the harvest output.
- The database name contains only two parts (e.g. `MyDB`) and SHIPS could not distinguish it from an object name — add it manually to `token_map.conf`.
- The file had a syntax error and was not parsed — check the harvest output for parse warnings.

---

### Where do I define what a token resolves to for each environment?

In `config/env/<ENV>.conf`. For example, `config/env/DEV.conf`:

```properties
SHIPS_ENV=DEV
ENV_PREFIX=A_D01
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
```

And `config/env/PRD.conf`:

```properties
SHIPS_ENV=PRD
ENV_PREFIX=P
SHIPS_PROJECT=OMR
OMR_STD={{ENV_PREFIX}}_{{SHIPS_PROJECT}}_STD
```

The token `{{OMR_STD}}` resolves to `A_D01_OMR_STD` in DEV and `P_OMR_STD` in PRD. Same source, different values.

---

### `{{TOKEN}}` is appearing in the deployed DDL — it was not replaced.

This means the token was not resolved at package time. Three causes:

**1. The token is not in the env config file.**

Open `config/env/DEV.conf` (or whichever env you packaged for). Check that `MY_TOKEN=<value>` is present. Add it if missing, re-run `ships process`.

**2. The token was mistyped — wrong case, spaces inside the braces.**

`{{MY_TOKEN}}` is valid. `{{ MY_TOKEN }}` (with spaces), `{{my_token}}` (wrong case), and `{MY_TOKEN}` (single braces) are not. SHIPS is case-sensitive and whitespace-sensitive.

Run a scan to find all undefined tokens before packaging:

```bash
# Check one environment
python -m td_release_packager scan \
    --source /my/project/ \
    --env-config config/env/DEV.conf

# Check all environments in one pass (recommended)
python -m td_release_packager scan \
    --source /my/project/ \
    --all-envs
```

Exit 0 = all tokens resolved in all environments. Exit 1 = at least one undefined token listed with its file locations.

**3. You packaged without specifying `--env-config`.**

The package command requires `--env-config` to resolve tokens:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env DEV \
    --env-config config/env/DEV.conf \   ← required
    --name MyProject
```

---

### I need to retokenise DDL that's already deployed on another system. How?

#### Why tokenise at all? Why de-tokenise later?

A SHIPS package is meant to be **one artefact that can deploy anywhere** — DEV, TEST, PROD, sandbox, a fresh customer tenant. To do that, every part of the DDL that varies between targets (project prefix, environment tier, ownership database) is replaced with a `{{TOKEN}}` placeholder during harvest. The packaged payload is then **environment-independent**: it has no idea where it will eventually run.

At deploy time, the deployer reads the target's `config/env/<ENV>.conf` (e.g. `PROD.conf` with `SHIPS_PROJECT=callcentre_prod`) and substitutes each `{{TOKEN}}` with that environment's resolved value. Same payload bytes; concrete DDL on the wire. This is the "tokenise once, deploy many" cycle:

```
   Source DDL on system A              Package payload (env-independent)         Deployed DDL on system B
   ───────────────────────             ───────────────────────────────────       ────────────────────────
   CallCentre_DOM_STD_T   ──tokenise─► {{SHIPS_PROJECT}}_DOM_STD_T  ──deploy─►  callcentre_prod_DOM_STD_T
                          (harvest)                                  (token
                                                                     substitution
                                                                     against
                                                                     PROD.conf)
```

You **never** want a package to carry the source system's literal names baked in — that's the failure mode this avoids. Tokenisation at harvest is the moment the payload becomes portable; token substitution at deploy is the moment it gets re-bound to a specific target.

#### When you need the regex form

The common scenario: you've extracted DDL from a live Teradata system where the project prefix is hardcoded (`CallCentre_DOM_STD_T`, `CallCentre_MEM_BUS_V`, etc.) and you want a portable payload that can later be deployed wherever your env config says. The literal `s/.../.../g` form `import-legacy` generates is enough for `$VAR → {{VAR}}` rewrites, but it can't handle "match anything that looks like `<Project>_<DOMAIN>_<TIER>_<KIND>` and keep groups 2/3/4 while replacing group 1 with `{{SHIPS_PROJECT}}`". That's what the `regex::` form is for.

Use a **regex tokenisation rule** in `config/tokenise.conf`:

```
# Token the project prefix on every <Project>_<DOMAIN>_<TIER>_<KIND> name.
regex::(?i)(\w+)_(\w{3})_(\w{3})_(V|T):={{SHIPS_PROJECT}}_$2_$3_$4
```

What this does at harvest time:

- **Match**: any name shaped `<Project>_<DOMAIN>_<TIER>_<KIND>` where `KIND` is `V` (view) or `T` (table), case-insensitive.
- **Replace**: discard the captured project prefix; substitute `{{SHIPS_PROJECT}}` and keep groups 2/3/4 verbatim.
- **Hit**: `CallCentre_DOM_STD_T` → `{{SHIPS_PROJECT}}_DOM_STD_T` in the packaged payload.

What happens at deploy time, automatically:

- The deployer reads the target env config (e.g. `PROD.conf` defines `SHIPS_PROJECT=callcentre_prod`).
- Every `{{SHIPS_PROJECT}}_DOM_STD_T` in the payload becomes `callcentre_prod_DOM_STD_T` — no further regex work needed.

The file accepts both rule kinds side-by-side:

```
# Literal substitutions (what `import-legacy` generates for $VAR / &&VAR&& migration):
s/$DB_PROD/{{DB_PROD}}/g
s/&&CORE&&/{{CORE}}/g

# Regex substitutions (hand-authored — when you need capture groups):
regex::(?i)(\w+)_(\w{3})_(\w{3})_(V|T):={{SHIPS_PROJECT}}_$2_$3_$4
```

Conventions for the `regex::` form:

- Pattern is a real Python regex. `(?i)`, alternation, character classes, anchors all work as expected.
- Replacement supports `$1..$9` back-references. Use `$$` for a literal `$`.
- Always replaces every match (there is no per-rule flag like `g` — that would be redundant).
- An unparseable PATTERN is skipped with a warning, not silently dropped.

Harvest and process auto-apply `config/tokenise.conf` before classification, so this works in the normal pipeline. You can also preview with:

```bash
python -m td_release_packager migrate-source \
    --tokenise-config config/tokenise.conf \
    --source          ./source \
    --dry-run
```

A couple of things worth being deliberate about:

- **Order matters.** Rules apply in file order. If one rule's output could match a later rule, the later rule will fire too.
- **Idempotence is your responsibility.** A pattern like `(\w+)_DOM_STD_T` will match `{{SHIPS_PROJECT}}_DOM_STD_T` on a second run too. Add an anchor or a negative lookahead if you need a second run to be a no-op.
- **Test on a small slice first.** Use `migrate-source --dry-run` to confirm the hit list before letting harvest rewrite hundreds of files.

---

### I want to skip tokenisation for now. Can I?

Yes. Just omit `--token-map` from harvest. Your DDL will be copied into the payload with its original hardcoded database names. Inspect will flag them as `hardcoded_name` warnings. You can suppress that rule in `config/inspect.conf` while you transition:

```properties
hardcoded_name=OFF
```

The package will still build and deploy — you just lose environment portability.

---

### How do I check tokens across all environments at once?

Use `--all-envs`. It discovers every `*.conf` file in `config/env/` and validates tokens against each in one pass:

```bash
python -m td_release_packager scan \
    --source /my/project/ \
    --all-envs
```

Output shows per-environment status:

```
  ✓ [DEV] All tokens resolved — no undefined or orphan tokens
  ✓ [TST] All tokens resolved — no undefined or orphan tokens
  ✗ [PRD] UNDEFINED tokens (referenced but not defined):
      Token '{{OMR_SEM}}' is referenced but not defined in properties.
        → used in: DDL/views/OMR_STD.MySummary.viw
```

Add `--fail-on-orphan` to also flag tokens defined in a config but never used in the payload — useful for keeping env configs tidy as the codebase evolves:

```bash
python -m td_release_packager scan --source . --all-envs --fail-on-orphan
```

This is the recommended pre-promotion gate: run it before every `ships package` to confirm the package will resolve correctly in every target environment.

---

### How do I see which files use a specific token?

Use `--show-map`. It prints the full token → file reverse index:

```bash
python -m td_release_packager scan --source . --show-map
```

Output:

```
  {{OMR_STD}}  (23 references)
      DDL/tables/OMR_STD.Customer.tbl
      DDL/views/OMR_STD.ActiveCustomers.viw
      DCL/inter_db/OMR_STD.grants.dcl
      … and 20 more
```

Useful before renaming a token or changing its value — shows exactly which files are affected.

---

### How do I use `scan` in a CI pipeline or with an agent?

Use `--format json`. It emits machine-readable JSON suitable for parsing:

```bash
python -m td_release_packager scan \
    --source . \
    --all-envs \
    --format json
```

Output structure:

```json
{
  "unique_tokens": 5,
  "files_with_tokens": 12,
  "token_map": {
    "OMR_STD": { "count": 23, "files": ["DDL/tables/OMR_STD.Customer.tbl", ...] }
  },
  "validation": {
    "DEV": { "undefined": [], "orphans": [], "status": "ok" },
    "PRD": { "undefined": ["Token '{{OMR_SEM}}' is referenced but not defined"], "orphans": [], "status": "error" }
  }
}
```

Exit code: 0 = clean, 1 = at least one environment has undefined tokens (or orphan tokens when `--fail-on-orphan` is set). An agent or CI step can branch on the exit code without parsing the output.

---

### What is the difference between `token_map.conf` and the env config files?

> ⚠️ For new projects, the relevant question is the difference between `config/tokenise.conf` (regex-based rewrite rules — supersedes `token_map.conf`) and `config/env/<ENV>.conf` (token-to-value bindings). The split below still applies; just substitute `tokenise.conf` for `token_map.conf`.

They serve two different purposes:

| File | Purpose | Example |
|---|---|---|
| `config/token_map.conf` | Maps a **literal database name** in source to a **token placeholder** | `A_D01_OMR_STD={{OMR_STD}}` |
| `config/env/DEV.conf` | Maps a **token** to its **resolved value** for a specific environment | `OMR_STD=A_D01_OMR_STD` |

Harvest uses `token_map.conf` to replace literals with `{{...}}`. Package uses the env config to replace `{{...}}` with real values. The two-step split is what allows the same source to target multiple environments.

---

### My `decompose-names` output looks wrong. How do I fix it?

`decompose-names` tries to infer composition roots (`ENV_PREFIX`, `SHIPS_PROJECT`) from database names. If the inference is wrong, edit the generated `.conf` file directly — it is just a starting point. The tool is heuristic; your naming convention may not match its assumptions.

---

## Harvest and classification

### SHIPS says my file is unclassified. Why?

SHIPS classifies files by looking for a `CREATE` or `REPLACE` statement near the top of the file. Common causes of unclassification:

- **No DDL statement**: the file is a BTEQ session script, a comment-only file, or contains only `SELECT` statements. Exclude it from `--source`.
- **Missing database qualifier**: `CREATE TABLE my_table` (no `DB.` prefix) is not classified as a TABLE. Add the qualifier.
- **Encoding issue**: the file is not UTF-8. SHIPS reads files as UTF-8 — convert the encoding.
- **Long comment block before the DDL**: SHIPS scans the first portion of the file. Move the `CREATE` statement before multi-paragraph comment blocks.
- **Non-standard Teradata syntax**: SHIPS recognises standard `CREATE`/`REPLACE` verbs. Vendor-specific DDL extensions may not be recognised.

Run harvest without `--token-map` to see which files are classified and which are not:

```bash
python -m td_release_packager harvest --source /my/sql/ --project /my/project/
```

---

### Harvest split my multi-statement file, but I wanted them to stay together.

Harvest splits files that contain multiple DDL objects — one object per file is a SHIPS requirement (eponymous naming). If you have a single logical operation that touches multiple tables in a specific order (e.g. a seed-data INSERT sequence), use a `.dml` file marked with `-- MULTI_TABLE_DML` at the top. SHIPS will not split it.

---

### Harvest keeps overwriting my manually edited payload files.

By default, harvest wipes and rebuilds the payload from source on every run (clean-payload mode). This is intentional — the payload is not a hand-curated artefact; it is always derived from source.

If you need to overlay new files without wiping, use `--keep-existing`:

```bash
python -m td_release_packager harvest --keep-existing ...
```

Use this sparingly. The recommended pattern is: keep all source in version control and let harvest rebuild the payload cleanly each time.

---

### Package fails with "undefined token" errors after re-harvesting with different tokenisation. How do I get a clean slate?

This used to happen when the harvest payload-clean only diffed the produced file set against what was on disk — differently-tokenised filenames (e.g. `{{DB_PREFIX}}_FOO.dcl` from one run, `{{DB_PREFIX_FOO}}.dcl` from another) could survive a re-harvest and break the Package token-coverage scan.

Two fixes are in place:

1. **Harvest auto-clean uses rmtree.** A full (non-`--keep-existing`) harvest now `shutil.rmtree`s `payload/database/` up front and recreates it empty before placing fresh files. A single re-harvest can never inherit a prior run's output.
2. **Explicit `ships clean` tool.** When you want to reset by hand, use:

```bash
# preview what would be removed
python -m td_release_packager clean --project /my/project/ --scope payload

# apply
python -m td_release_packager clean --project /my/project/ --scope payload --apply
```

Scopes: `runs`, `payload` (default), `releases`, `reports`, `decisions`, `all`. `--scope all` resets to scaffolded state but leaves `.build_counter` intact so build provenance survives. `config/` and `ships.yaml` are never touched. The same tool is available to agents over MCP as `ships_clean` (synchronous — returns directly, no `run_id`).

---

### What's the canonical harvest recipe for a reverse-harvested DBC export?

This is the single supported recipe for reverse-harvesting a deployed data product's DBC export — the first-class input path SHIPS now targets. Use it for any source whose database names follow the `<Project>_<Module>_<Tier>_<Kind>` shape.

```bash
python -m td_release_packager harvest \
    --source /path/to/<Project>/DBC/export \
    --project /path/to/SHIPS/project \
    --auto-tokenise \
    --prefix-token <Project>=DB_PREFIX \
    --remove-view-type-affixes
```

What each flag does:

- `--auto-tokenise` — detect literal database names in the source and apply the derived token map in a single pass.
- `--prefix-token <Project>=DB_PREFIX` — rewrite the leading project segment of every identifier to `{{DB_PREFIX}}_<suffix>`, leaving the structural remainder literal. After this, the package is environment-independent on the project axis.
- `--remove-view-type-affixes` — strip redundant `_v` view affixes during harvest so view names follow the SHIPS Object Placement Standard.

**Do not pass `--env-prefix` on this path.** It engages a whole-name derivation that produces braced compound tokens like `{{DB_PREFIX_DOM_BUS_V}}.dcl` instead of the correct prefix form `{{DB_PREFIX}}_DOM_BUS_V.dcl`. The deterministic-deploy programme (PR1) retires `env_prefix` from the prefix path for exactly this reason.

After harvest, define `DB_PREFIX` in each `config/env/<ENV>.conf`:

```properties
DB_PREFIX=CallCentre        # DEV
DB_PREFIX=callcentre_prod   # PRD
```

Then `inspect` will gate the token coverage against every env (PR2), and `package` cannot fail on undefined tokens.

---

### `--prefix-token` doesn't seem to do anything.

For `--prefix-token` to substitute, pair it with `--auto-tokenise`. The substitution is engaged unconditionally once a token map exists, but on the CLI/MCP surface the candidate map is only built when auto-tokenise is on. Use the recipe above — that's the supported flow.

If you've stripped down to the bare `--prefix-token` call, no token map is assembled and no substitution happens. Pass `--auto-tokenise` alongside.

---

### My package keeps generating a `_00_environment_prereqs` archive that demands DBA review for a parent database that obviously already exists (e.g. `DATAPRODUCTS`).

Reverse-harvested products typically inherit a root `CREATE DATABASE <Project> FROM <ExternalParent>` from the DBC export. By default SHIPS treats any non-DBC external parent as a DBA-review gate and emits the `_00_environment_prereqs` package + `DBA_INSTRUCTIONS.md` step. For products whose parent is a well-known platform database that already exists on every target, that gate is friction.

Declare the parent in your env config so the build knows it's expected to pre-exist:

```properties
# config/env/DEV.conf
EXTERNAL_PARENTS=DATAPRODUCTS

# config/env/PRD.conf
EXTERNAL_PARENTS=DATAPRODUCTS,SYSDBA
```

Comma-separated, case-insensitive. `DBC` is implicit and does not need to be listed. The next build skips the environment-prereqs package for declared parents and proceeds straight to the main + prereqs split. Undeclared external parents still trigger the safety net — the exemption is targeted.

---

## Inspect errors and warnings

### What does `db_qualifier ERROR` mean?

Your DDL creates an object without a database prefix:

```sql
-- Wrong:
CREATE TABLE Customer (Id INTEGER);

-- Correct:
CREATE TABLE {{OMR_STD}}.Customer (Id INTEGER);
```

Every object in the payload must be fully qualified. SHIPS enforces this because an unqualified name would deploy into whatever database the DBA happens to be connected to — unpredictable and ungovernable.

---

### What does `type_suffix WARNING` mean?

Your object name encodes the type in the name:

```
VW_Customer.viw   ← type already in the extension (.viw = VIEW)
SP_ProcessOrder.spl  ← same problem
```

SHIPS encodes the type in the file extension, not the name. Rename the object to `Customer.viw` and `ProcessOrder.spl`. The type is always unambiguous from the extension.

---

### What does `hardcoded_name WARNING` mean?

The DDL body still contains a literal database name that was not tokenised:

```
A_D01_OMR_SEM appears in payload/database/DDL/views/OMR_STD.MySummary.viw
```

Add `A_D01_OMR_SEM={{OMR_SEM}}` to `config/token_map.conf` and re-run harvest with `--token-map`. If this is an intentional constant (e.g. a shared system database that never changes), you can suppress the rule in `config/inspect.conf`:

```properties
hardcoded_name=OFF
```

---

### How do I suppress inspect rules I do not want?

Edit `config/inspect.conf` in your project:

```properties
# Values: ERROR | WARNING | OFF
keyword_case=OFF
leading_commas=OFF
db_qualifier=ERROR        # keep this one — it matters
```

Rules set to `OFF` are not checked. Rules set to `WARNING` are reported but do not block packaging (unless you use `--strict`).

---

### Inspect has too many errors. How do I tackle them?

Start with the `ERROR`-severity rules — these block packaging. Fix them first, then address warnings.

A common pattern for onboarding a legacy codebase:

1. Turn `hardcoded_name` to `WARNING` (not `OFF`) so you see what needs tokenising but it does not block you
2. Fix `db_qualifier` errors (all objects must be qualified)
3. Fix `type_suffix` warnings at your own pace

You do not need to fix everything before you can package.

---

### My DCL files have a `.dcl` extension now — I had `.grt` before. What changed?

Issue #365 normalised the DCL extension across SHIPS. There is now **one canonical extension: `.dcl`**. The Generate step (view-layer generator) emits `.dcl` directly; Harvest already normalised source `.grt` files to `.dcl` via the classifier. No `.grt` file survives anywhere under `payload/`.

`.grt` is still recognised in *source* files — you can keep your existing source on disk — but it is normalised on the way into the payload. Any downstream tooling that discovered grant files by globbing `*.grt` under `payload/` needs to update to `*.dcl`.

---

### My generated DCL filenames changed shape after upgrading. Why?

Issue #365 (PR-4) switched the view-layer generator from grantee-grouping to **ON-object grouping**. Each DCL file is now named after the protected database the grants are `ON`, not the grantee receiving the privilege.

| Before | After |
| --- | --- |
| `{{DOM_DATABASE_V}}.grt` | `{{DOM_DATABASE_T}}.dcl` |
| `{{SEM_DATABASE_V}}.grt` | `{{DOM_DATABASE_V}}.dcl` |

The grants are the same — only the file they live in and the file's name changed. Rationale: grants are an attribute of the protected object and should redeploy in that object's deployment wave. ON-object grouping aligns the on-disk structure with deployment-wave ordering.

If your CI/CD or downstream tooling assumes the old `{{grantee}}.grt` shape, update the glob.

---

### What is `object_level_grant` warning me about?

A new Inspect rule (issue #365) that warns when a `.dcl` file contains a `GRANT`/`REVOKE` whose target is a specific object (`ON db.obj`) or column (`GRANT SELECT (col1) ON db.obj`) rather than the containing database (`ON db`).

Teradata best practice is to grant at the database level — privileges propagate to all objects in the container and the access surface stays auditable. Object- and column-level grants produce sprawling privilege graphs that drift.

If the object-level grant is genuinely required, set `object_level_grant=OFF` in `config/inspect.conf`. The rule defaults to WARNING (advisory) and does not block packaging.

---

## Packaging and trust

### Why is my package inside a release-group directory?

SHIPS treats a build as a release group. Even when there is only one package archive, the output is grouped for consistency:

```text
releases/DEV_OMR_BUILD_0042_20260510/
    DEV_OMR_BUILD_0042_20260510_01_main.zip
    DEV_OMR_BUILD_0042_20260510_01_main.zip.sha256
    release_group.json
    README.txt
```

When the build needs environment prerequisites or application prerequisites, the same folder also contains `_00_environment_prereqs` and `_01_prereqs` archives. Deploy the release-group directory directly with `python -m td_release_packager deploy <release_group> ...`; SHIPS reads `release_group.json`, extracts the required archives into `.ships-work`, and runs them in order.


### My package says `READY-WITH-CAVEATS`. Should I worry?

`READY-WITH-CAVEATS` means at least one warning-level signal fired but no error-level signals. Common causes:

- `source_dirty=true` — you built with `--allow-dirty`. The package was built from an uncommitted working tree.
- `inspect_grants` warning — some grants are missing or mismatched.
- `provenance_complete` — `context/ships.provenance.json` was not produced (unusual).

Review the per-signal breakdown in the trust banner or the `context/ships.build.json` `trust.signals` field. For most warning-level signals, proceed with deployment but investigate the underlying issue before the next release.

---

### My package is `BLOCKED`. What do I fix?

`BLOCKED` means at least one `ERROR`-level signal fired. Most signals require you to fix the underlying problem and rebuild.

**One signal resolves automatically at deploy time:**

| Signal | Behaviour |
|---|---|
| `environment_prereq_requires_dba_review` | Build-time only — at deploy time, `deploy.py` queries the target database. If all listed parent objects exist, the block resolves and deployment proceeds. If any are missing, deploy the `_00_environment_prereqs` package first. |

**All other signals require a rebuild:**

| Signal | Fix |
|---|---|
| `inspect_token_format` | Malformed `{{TOKEN}}` in payload — find it with `grep -r '{{' payload/` |
| `inspect_lint` | ERROR-severity lint rule violation — run `ships inspect` for details |
| `inspect_grants` | Grant drift detected at ERROR level — run `ships inspect --fix-grants` |

`--skip-trust-check` is a development escape hatch for the rebuild-required signals. Do not use it in production, and do not use it as a workaround for `environment_prereq_requires_dba_review` — the auto-resolve path is the correct mechanism.

---

### The build counter keeps incrementing. How do I reuse the same build number for a different environment?

Use `--no-increment`:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env TST \
    --env-config config/env/TST.conf \
    --name OMR \
    --no-increment    ← reuses current build number
```

This is the correct pattern for promoting a DEV package to TST or PRD — same source, same build number, different environment tokens.

---

### The `--allow-dirty` flag — when should I use it?

Only in development. `--allow-dirty` lets you build a package from an uncommitted working tree. It stamps `source_dirty=true` in `context/ships.build.json` and degrades the trust label to `READY-WITH-CAVEATS`. Never use it for a package you intend to promote to production — the package cannot be reproducibly rebuilt from source because the uncommitted changes are not in version control.

---

## Deployment

### The deployer says `Error 3523: No privilege`. What do I grant?

The DBA account running `deploy.py` is missing a privilege on one of the target databases. The deployment report's pre-flight section lists exactly which databases and which privilege types are missing.

SHIPS also generates a prerequisite GRANT script during pre-flight. Look in the HTML report for the "Pre-flight Results" section — click the failing check to see the exact `GRANT` statement needed.

Common grants:

```sql
GRANT CREATE TABLE ON MyDB TO deploy_user;
GRANT DROP TABLE   ON MyDB TO deploy_user;
GRANT CREATE VIEW  ON MyDB TO deploy_user;
```

After granting, re-run with `resume`:

```bash
python -m td_release_packager deploy /path/to/release_group/ resume logs/.deploy_manifest_<id>.json --host srv --user dba
```

---

### The deployment failed halfway. Do I have to start over?

No. Re-run the same release-group deploy command. The manifest records each object's state, and the deployer skips anything already in `COMPLETED` state:

```bash
python -m td_release_packager deploy /path/to/release_group/ --host srv --user dba   ← same command
```

Or explicitly resume from the manifest:

```bash
python -m td_release_packager deploy /path/to/release_group/ resume logs/.deploy_manifest_<id>.json --host srv --user dba
```

---

### A table was dropped and recreated but the data is gone.

SHIPS's table deployment strategy backs up the table (renames it to `<TableName>_bk_<timestamp>`) before creating the new version. If the schema was compatible, data was migrated. If not, the backup table is still there.

To check:

```bash
python -m td_release_packager deploy /path/to/release_group/ status logs/.deploy_manifest_<id>.json
```

Look for `backup_table` in the object record. If it is set, you can query `SELECT * FROM <backup_table>` to recover data.

---

### I need to deploy a package built before schema drift detection was added. Will drift detection break it?

No. If `context/ships.build.json` has no `baseline_dir` field (older package), drift detection is silently disabled. `_load_baseline_dir()` returns an empty string and the deployment proceeds normally.

---

### What does `--on-drift continue / skip / abort` mean?

When schema drift is detected (an object changed out-of-band since the last SHIPS deploy):

| Mode | Behaviour |
|---|---|
| `abort` | FAIL the object — stop deployment (default — safe) |
| `skip` | SKIP the object — leave the out-of-band change in place, deploy everything else |
| `continue` | COMPLETE as normal — SHIPS overwrites the out-of-band change |

For rollbacks (`ships rollback --to-tag`), `continue` is the default because the point of rollback is to restore a known-good state.

---

### How do I roll back a deployment?

**Technical rollback** (undo the current deployment using pre-captured DDL):

```bash
# Roll back everything
python -m td_release_packager deploy /path/to/release_group/ rollback logs/.deploy_manifest_<id>.json --host srv --user dba

# Roll back only wave 3
python -m td_release_packager deploy /path/to/release_group/ rollback logs/.deploy_manifest_<id>.json --wave 3 --host srv --user dba
```

**Feature rollback** (re-deploy from a previous git tag):

```bash
python -m td_release_packager rollback \
    --to-tag v1.2.3 \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR
```

Then deploy the produced package with `--on-drift continue`.

Note: JARs (`.sjr`) and C external routines (`LANGUAGE C`) cannot be restored by technical rollback — the compiled binary is not recoverable from Teradata. Feature rollback handles these correctly because the old binary is in the tagged source.

---

## Dependency ordering and waves

### Objects are deploying in the wrong order. A view fails because its base table does not exist yet.

Run the analyser before packaging. The analyser builds the dependency graph and writes `_waves.txt`, which the deployer uses to enforce ordering:

```bash
python -m td_release_packager analyze --source /my/project/
```

Or use `ships process` which runs all stages including analyse automatically.

If you are deploying a manually assembled package without `_waves.txt`, the deployer falls back to type-based ordering (tables before views before procedures). This is usually correct but may fail if you have cross-database dependencies or unusual object type dependencies.

---

### The analyser says I have a circular dependency. How do I fix it?

Two objects depend on each other — for example, View A references View B, and View B references View A. Teradata cannot create them in any order without the other existing.

Common fixes:

- **Split the view**: extract the common columns into a base view that both reference, breaking the cycle.
- **Use a table instead of a view**: for performance reasons, materialise one of the views as a table.
- **Deploy one with a placeholder**: create View A first with a stub definition that does not reference View B, create View B, then replace View A with the full definition.

The analyser reports which objects form the cycle — use that to identify which is easiest to restructure.

---

### I have objects that must deploy in a specific order not captured by dependency analysis.

You can override the wave ordering by writing a `_waves.txt` file manually in the project root:

```
payload/03_ddl/tables/DB.FirstTable.tbl
payload/03_ddl/tables/DB.SecondTable.tbl
---
payload/03_ddl/views/DB.MyView.viw
```

Objects separated by `---` barriers are in different waves. The deployer honours this ordering exactly.

---

## Environment promotion

### How do I take the DEV package to TST without rebuilding from source?

Build a TST package from the same source using `--no-increment`:

```bash
python -m td_release_packager package \
    --source /my/project/ \
    --env TST \
    --env-config config/env/TST.conf \
    --name OMR \
    --no-increment    ← same build number as DEV
```

The DDL structure is identical. Only the token resolution differs — `{{OMR_STD}}` resolves to `T_OMR_STD` instead of `A_D01_OMR_STD`. Always verify the `environment` field in context/ships.build.json before handing off to the DBA.

---

### The DBA is asking which package to deploy to PRD. How do I verify it is the right one?

```bash
python -c "
import zipfile, json
with zipfile.ZipFile('releases/PRD_OMR_BUILD_0042_20260510/PRD_OMR_BUILD_0042_20260510_01_main.zip') as z:
    name = next(n for n in z.namelist() if n.endswith('context/ships.build.json'))
    b = json.loads(z.read(name))
print('Package: ', b['package_name'])
print('Build:   ', b['build_number'])
print('Env:     ', b['environment'])
print('Commit:  ', b['source_commit'])
print('Trust:   ', b['trust']['label'])
"
```

Confirm `environment = PRD`, the build number matches the approved DEV build, and `trust.label` is `READY`.

---

## Schema drift detection

### Drift detection is disabled. The log says "set deployment.baseline_dir in ships.yaml".

Drift detection requires a shared filesystem path configured in `ships.yaml`:

```yaml
# ships.yaml
deployment:
  baseline_dir: /shared/nfs/ships-baselines/OMR/
```

This path must be accessible from every machine that deploys this package. After adding it, rebuild the package — the path is stamped into `context/ships.build.json` and travels with the package automatically. Operators do not need to set any flags.

If no shared path is available, use a local directory for development:

```bash
python -m td_release_packager deploy /path/to/release_group/ --host srv --user dba --baseline-dir /tmp/ships-baselines/
```

This works per-machine but does not share baselines across operators.

---

### Drift was detected on an object I know is correct. How do I reset the baseline?

The simplest reset is to deploy with `--on-drift continue` — this runs the deployment as normal and writes a new baseline from the post-deploy SHOW output:

```bash
python -m td_release_packager deploy /path/to/release_group/ --host srv --user dba --on-drift continue
```

After this run, the baseline reflects what SHIPS just deployed. Future drift detection will compare against the new baseline.

---

## Audit trail and decisions.json

### What is `decisions.json`?

An append-only audit log written by every SHIPS pipeline run. Every stage records what config it used, what it processed, what it produced, and any issues it found. It is the machine-readable history of every `ships harvest`, `ships inspect`, `ships analyse`, `ships package`, and `ships process` run on a project.

Read it in human form:

```bash
python -m td_release_packager explain --project /my/project/ --command process
```

Or query the last run from Python:

```python
import json, pathlib
decisions = json.loads(pathlib.Path("decisions.json").read_text())
last_run = decisions["runs"][-1]
print(last_run["final_status"])
```

---

### `decisions.json` is getting very large. How do I trim it?

```bash
# Keep the 50 most recent runs
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-runs 50

# Keep runs from the last 90 days
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-days 90

# Preview first (no changes written)
python -m td_release_packager decisions prune \
    --project /my/project/ \
    --keep-runs 50 \
    --dry-run
```

---

### How do I find a deployment in Teradata DBQL?

Every SQL statement SHIPS executes carries a query band with the build number, package name, environment, and package hash. Query it with `GetQueryBandValue`:

```sql
SELECT
    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD') AS DeployDate,
    t1.UserName,
    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,
    GetQueryBandValue(t1.QueryBand, 0, 'ENV')   AS Environment,
    COUNT(*)                                     AS Statements
FROM DBC.DBQLogTbl t1
WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '0042'
  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = 'OMR'
GROUP BY DeployDate, UserName, Build, Environment
ORDER BY DeployDate;
```

The SHIPS Deployment Dashboard also generates this query for you — see the Compliance tab on the package detail page.

---

## General

### I'm an agent — what's the read-first file in a SHIPS project?

Open `<project_dir>/ships.project.json`.

It's the agent-discoverable index for the **project** (pre-package), same role that `context/ships.index.json` plays inside a built package. From one file you get the project name, the current lifecycle state (`scaffolded` → `harvested` → `inspected` → `analysed` → `packaged`), the recommended next CLI invocation, and pointers to every other evidence file in the tree (ships.yaml, ships.decisions.json, config/tokenise.conf, config/env/*.conf, latest release archive).

The file is refreshed after every project-mutating CLI command (`scaffold`, `harvest`, `inspect`, `analyse`, `package`). The lifecycle state is derived from `ships.decisions.json` plus the existence of `releases/*.zip` — never inferred.

`actions_ref` points at `ships.project_actions.json` (#273) — the project-side action vocabulary: which CLI actions are safe to take autonomously, which are blocked, and which need human approval first. `policy_ref` points at `ships.project_policy.json` (#275) — the project-side agent policy: do-not flags, stop conditions, and approval triggers with per-condition `detect_via` / `evidence_ref` / `instruction` metadata so an agent can detect each condition and act on it from the policy doc alone. Read both files after the index. Together they form the project-side agent contract from [#268](https://github.com/earthshiner/teradata-ships/issues/268).

### What's in ships.project_actions.json?

A closed-vocabulary view of the SHIPS pre-package CLI surface: `scaffold`, `harvest`, `inspect`, `analyse`, `scan`, `tokenise`, `import_legacy`, `decompose_names`, `package`. Each one lands in exactly one of three lists:

- **`allowed_actions`** — safe to take autonomously.
- **`blocked_actions`** — must not be taken.
- **`requires_human_approval`** — pause and ask the operator first; entries carry `{action, reason, evidence_ref, instruction}`.

`tokenise` is **always** in `requires_human_approval` because it rewrites source files in place — never autonomous. `package` moves between `allowed_actions` and `requires_human_approval` based on whether the project has been harvested yet (an empty `payload/` is a red flag worth pausing on).

A `discovery_flags` block records what's on disk (`tokenise_config_present`, `env_configs_present`, `source_payload_present`) so downstream evaluators can compose without re-scanning.

### Can I package from a GitHub repository directly?

SHIPS always works on a **local directory** — it has no built-in GitHub client. But in practice this is rarely a constraint because the three common patterns all give you a local directory with minimal setup:

---

**Pattern 1 — CI/CD pipeline (most common)**

In GitHub Actions, GitLab CI, or any other pipeline the repository is already checked out before SHIPS runs. Just call SHIPS on the current directory:

```yaml
# .github/workflows/ships.yml
jobs:
  package:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install SHIPS
        run: pip install uv && uv sync

      - name: Run SHIPS pipeline
        run: |
          uv run python -m td_release_packager process \
            --project . \
            --source src/ddl/ \
            --token-map config/token_map.conf \
            --env DEV \
            --env-config config/env/DEV.conf \
            --name MyProject \
            --commit ${{ github.sha }} \
            --strict
```

The `--commit` flag records the GitHub SHA in `context/ships.build.json` so every deployed object is traceable back to the exact commit.

---

**Pattern 2 — local git archive (any branch, tag, or commit)**

If you have a local clone and want to package from a specific ref without a full checkout:

```bash
# Extract the ref into a temp directory
git archive main | tar -x -C /tmp/ships-source/

# Package from the extracted source
python -m td_release_packager process \
    --project /my/project/ \
    --source /tmp/ships-source/ \
    --token-map config/token_map.conf \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name MyProject \
    --commit $(git rev-parse main)

# Clean up
rm -rf /tmp/ships-source/
```

`ships rollback --to-tag v1.2.3` does exactly this internally — it runs `git archive` on the named tag and packages the result.

---

**Pattern 3 — GitHub API tarball (no local clone needed)**

GitHub's API returns a tarball for any ref at:

```
https://api.github.com/repos/{owner}/{repo}/tarball/{ref}
```

Download, extract, and package:

```bash
curl -sL \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/myorg/myrepo/tarball/main" \
  | tar -xz -C /tmp/ships-source/ --strip-components=1

python -m td_release_packager process \
    --project /my/project/ \
    --source /tmp/ships-source/ \
    --token-map config/token_map.conf \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name MyProject
```

This is useful for one-off packaging from a remote repository without maintaining a local clone — for example, an agent that packages on demand from any repo it has API access to.

---

**Summary**

| Pattern | When to use | Local clone needed? |
|---|---|---|
| CI/CD checkout | Standard pipeline — GitHub Actions, GitLab, Jenkins | No (pipeline does it) |
| `git archive` | Packaging a specific ref locally; also what `ships rollback` uses internally | Yes |
| GitHub API tarball | Manual one-off from a remote repo | No |
| **`--source-github`** (built-in) | Any scenario — SHIPS fetches and packages in one command | No |

Note: `git archive --remote=https://github.com/...` is not supported by GitHub over HTTPS. Use the API tarball (Pattern 3) or the built-in `--source-github` flag (Pattern 4) for remote-only access.

---

**Pattern 4 — Built-in `--source-github` flag (no git required)**

SHIPS has native support for packaging directly from a GitHub repository using `--source-github`:

```bash
# Package from main branch
python -m td_release_packager process \
    --project /my/project/ \
    --source-github myorg/myrepo \
    --source-ref main \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name MyProject

# Package from a specific tag (private repo)
python -m td_release_packager package \
    --source-github myorg/myrepo \
    --source-ref v1.2.3 \
    --github-token $GITHUB_TOKEN \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name MyProject

# GitHub Enterprise Server
export SHIPS_GITHUB_API_URL=https://github.mycompany.com/api/v3
python -m td_release_packager process \
    --source-github myorg/myrepo \
    --source-ref main \
    ...
```

SHIPS downloads the repository tarball via the GitHub REST API, extracts it to a temporary directory, runs the full pipeline, and then cleans up. The resolved commit SHA is automatically stamped into `context/ships.build.json` as `source_commit`. No `git` installation required.

Authentication: `--github-token TOKEN` or `GITHUB_TOKEN` environment variable. Public repositories work without a token (subject to 60 req/hr rate limit). Private repositories require a PAT with `repo` scope.

---

## Security

### How do I prevent someone from editing the embedded deployer code to bypass security checks?

Two layers. First, `context/ships.integrity.json` now covers both `payload/` and `lib/` — any
edit to the deployer files changes the package hash and the deploy fails before any
database connection is made. Second, use Ed25519 asymmetric signing
(`ships package --asymmetric-key private.pem`): only the CI pipeline with the private
key can produce a valid signature; DBAs verify with the public key and cannot forge it
even with full access to the extracted package.

---

### What is the minimum key management infrastructure for asymmetric package signing?

Very little. Run `ships keygen` once to generate an Ed25519 key pair. Store the private
key in your CI/CD platform's secrets (GitHub Actions: `SHIPS_PRIVATE_KEY_PATH`). Commit
`ships_signing_public.pem` to your project repository — it is a public key and safe to
share. No certificate authority, HSM, or PKI required.

---

### Can the DBA deploy directly from GitHub without receiving the ZIP file?

Yes. Once CI publishes the package as a GitHub Release, the DBA runs:

```bash
python -m td_release_packager deploy PRD_Pkg_BUILD_0001.zip \
    --host myhost \
    --user ships_dba
```

SHIPS extracts the ZIP into `.ships-work`, verifies the package during the generated
deploy flow, and proceeds with normal deployment. For private repositories, use your
standard artifact download mechanism with `GITHUB_TOKEN`, then point SHIPS at the
downloaded ZIP or release-group directory.

---

### What is the `ships audit-grants` command?

It compares the GRANT statements declared in a package's DCL files against the current
live grant state in Teradata, and reports:

| Category | Meaning |
|---|---|
| `UNDECLARED` | Grant present in Teradata but not in DCL |
| `MISSING` | Grant in DCL but not present in Teradata |
| `MATCHED` | Grant declared in DCL and confirmed in Teradata |

Exit 0 = no drift. Exit 1 = drift detected. Use it as a post-deployment gate or a
standing compliance check.

---

### How do I add a change ticket reference to a package?

Pass `--change-ref CHG0012345` when packaging:

```bash
ships package \
    --source /projects/OMR \
    --env PRD \
    --env-config config/env/PRD.conf \
    --name OMR \
    --change-ref CHG0012345
```

For environments that require it, add `require_change_ref: true` under the environment
block in `ships.yaml`. The Ship preflight will then fail if the package has no change
reference.

---

### Where do I start with an existing codebase?

Run the onboarding wizard. It scans your source directory and recommends the exact steps based on what it finds:

```bash
python -m td_release_packager onboard --source /path/to/legacy/sql/ --env DEV
```

It will tell you whether you need `import-legacy` (old `$VAR` markers), `bootstrap-env-config` (SHIPS tokens but no config yet), or can go straight to `harvest`.

---

### What is the difference between `ships process` and running each command separately?

`ships process` runs all stages under a single `decisions.json` entry — harvest → generate → inspect → analyse → package — and stops on the first error (with `--strict`) or collects all errors and summarises at the end (default). It is the recommended daily driver.

Running stages individually gives you more control: you can inspect the output of each stage before proceeding, or re-run just one stage without running the whole pipeline.

---

### SHIPS is processing files slowly. How do I speed it up?

- **Harvest**: fast by design. If slow, check for very large files — SHIPS reads each file fully.
- **Inspect**: scales with the number of files. For 1000+ files expect a few seconds.
- **Analyse**: scales with the number of objects and dependencies. Complex dependency graphs with many external references take longer.
- **Deploy**: use `--streams 4` (or higher) for large packages. Wave-parallel deployment significantly reduces total time for packages with 50+ objects.

---

### Can multiple developers work on the same SHIPS project simultaneously?

Yes. Put the project directory (including `payload/`) in version control. Each developer:

1. Pulls the latest project
2. Harvests their source changes
3. Commits the updated payload

Because harvest rebuilds from source cleanly each time, the payload always reflects what is in source control. Merge conflicts in the payload are resolved the same way as any other code conflict — by looking at the source, not the payload.

---

### How do I add a new DDL object type that SHIPS does not recognise?

SHIPS supports 20+ Teradata object types out of the box. If you have a custom extension (e.g. `.tdsql`), add it to `ships.yaml`:

```yaml
discovery:
  extensions:
    - .tdsql
```

This tells harvest and the deployer to include files with that extension. The extension is stamped into `context/ships.build.json` so the deployer honours it automatically. You will still need to ensure your DDL files parse correctly — SHIPS classifies by looking for `CREATE`/`REPLACE` verbs regardless of extension.

---

### Is there a way to see the package contents without extracting it?

Yes — open `package_report.html` from inside the archive, or use the Python zipfile module:

```python
import zipfile, json

with zipfile.ZipFile("releases/DEV_OMR_BUILD_0042_20260510/DEV_OMR_BUILD_0042_20260510_01_main.zip") as z:
    # List all files
    print("\n".join(z.namelist()))

    # Read context/ships.build.json
    name = next(n for n in z.namelist() if n.endswith("context/ships.build.json"))
    build = json.loads(z.read(name))
    print(f"Trust: {build['trust']['label']}")
    print(f"Files: {build['file_count']}")
```

Or use the SHIPS Deployment Dashboard (`ships_dashboard.py`) which reads context/ships.build.json from every archive in your `releases/` directory without extracting.

---

## Clearscape demo notebooks

### How do I produce a Jupyter notebook for a Clearscape Experience demo?

Use `ships notebook`. It renders any SHIPS project into a self-contained `.ipynb` with inline DDL and one code cell per analysed wave:

```bash
python -m td_release_packager notebook \
    --project my-project \
    --env-config my-project/config/env/DEV.conf \
    --name MyDemo
```

Output: `my-project/output/MyDemo.clearscape.ipynb`. Hand it to your Clearscape user — they upload it to Jupyter, enter their host/user/password in the connection cell, and run each wave cell in order. No SHIPS install required on the customer side.

Full reference: [CLEARSCAPE_NOTEBOOK.md](./CLEARSCAPE_NOTEBOOK.md).

---

### Can I use the Clearscape notebook target for production deployment?

No. The renderer is deliberately non-production: no preflight, no rollback, no trust report, no integrity fingerprint. It is for demos on Clearscape Experience sandboxes and similar short-lived show-and-tell environments.

For real deployment use `ships package` + `ships deploy` (or the deploy launcher embedded in the package). Those keep the preflight check, atomic-wave execution, rollback, and trust scoring.

---

### Why is the produced notebook so large?

The notebook inlines every CREATE statement as a triple-quoted Python string. For a full AI-Native Data Product (235 objects across 6 waves) that is around 700 KB. This is intentional — Clearscape sandboxes may have restricted network egress, and inlining the DDL makes it part of the demo narrative. If the cells feel long, collapse them in Jupyter (`View → Collapse All Code`) before presenting.

---

### Can the notebook prompt the user for an environment prefix at runtime?

Not in the current renderer. Tokens are resolved at render time from the supplied `--env-config`, so the customer gets a notebook with concrete database names baked in. If you need per-customer naming, render once per customer:

```bash
python -m td_release_packager notebook \
    --project my-project --env-config config/env/ACME.conf \
    --name AcmeDemo --output renders/acme.ipynb
```

A future enhancement could add a prompt-driven prefix substitution cell — file an issue if you need it.
