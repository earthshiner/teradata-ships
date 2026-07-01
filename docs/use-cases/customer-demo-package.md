# Customer-demo package (demo mode)

The second primary SHIPS use case. The field team needs **one
self-contained, trustworthy package** that someone *other than the
author* can deploy into a demo or trial Teradata environment to stand a
working data product up in front of a customer. The emphasis shifts from
promotion (one payload, many environments) to **hand-off**: the package
must carry its own provenance, pass its trust checks, and deploy cleanly
into a clean demo database — with no SHIPS install on the deploying
machine, because `database_package_deployer` is standalone.

The source is usually a **DBC export of a known-good reference product**
(e.g. the CallCentre AI-Native Data Product pulled from a trial system),
or the live `payload/` of a product you already maintain. Unlike the
multi-environment case there is normally a **single target** — the demo
system — and the recommended tokenisation is an **explicit
one-token-per-database map**, the most reliable approach when the package
will be deployed by hand at a customer site.

```
   DBC export ──▶ ┌─────────────┐
   (reference     │   HARVEST   │ ─▶ tokenised payload (explicit map)
    product)      └─────────────┘            │
                                             ├─▶ INSPECT / ANALYSE   (trust ⇒ READY)
                                             │
                                             └─▶ PACKAGE --env DEMO ─▶ DEMO.zip + report
                                                                          │
                                       hand to field team / SE  ──────────┘
                                                                          │
                                       deploy into fresh demo DB ─────────▶ live demo
```

In the rest of this document `$PROJECT` is the SHIPS project root and
`$SOURCE` is the directory holding the raw export (or DDL) you want to
package. `DEMO` is the single target environment.

## When is it demo mode (vs the multi-environment package)?

| | Multi-environment package | Customer-demo package (demo mode) |
|---|---|---|
| Goal | Promote identical code across DEV/TST/PRD | One package the field team deploys at a customer demo |
| Targets | Many (`package --env` looped) | One (the demo / trial system) |
| Source | Messy legacy DDL | DBC export of a reference product, or a maintained `payload/` |
| Tokenisation | prefix or whole-name; `bootstrap-env-config` aligns env configs | explicit one-token-per-database map (most reliable for hand-off) |
| Naming | environment-specific (`DEV_…`, `PRD_…`) | environment-agnostic product names, or a single `DEMO_…` |
| Emphasis | repeatable promotion, CI gates | provenance, trust **READY**, deployable by a non-author |
| Target state | may already hold objects | **clean** demo DB — fresh, so seeded demo data isn't displaced |

> **Clean-target note.** SHIPS cannot evolve a populated schema in place:
> `ALTER TABLE` is silently dropped by the deployer, and `DROP`/`RENAME
> TABLE` are unsupported. A redeployed `CREATE TABLE` resolves to
> `IDEMPOTENT_DEPLOY` — the deployer renames any existing table to a
> backup and creates the new (empty) definition, so a redeploy is safe to
> rerun but lands an **empty** table (the old rows stay in the backup). For
> a customer demo that means deploying into a **fresh** demo database so
> the demo data you seed afterwards isn't pushed aside by a re-run.
> Confirm the target is clean before deploying.

## 0. Scaffold — a single DEMO environment

```bash
python -m td_release_packager scaffold \
  --name <ProjectName> \
  --output <parent-dir> \
  --environments DEMO
```

One environment is enough for demo mode — you are not promoting across a
DEV/TST/PRD ladder. Scaffold lays down `config/env/DEMO.conf`,
`payload/database/`, `ships.yaml`, `config/token_map.conf` (legacy — see
deprecation note below), and `config/inspect.conf`.

> ⚠️ **Deprecation note (closes [#388](https://github.com/earthshiner/teradata-ships/issues/388)):** `token_map.conf` and the `--token-map` flag used throughout this walkthrough are kept for back-compatibility. **Prefer `config/tokenise.conf`** for new projects — regex-based, strictly more powerful. Authored via the SHIPS Navigator wizard (`tools/navigator/ships-navigator.html`) or by hand. The walkthrough below still works as written.

## 1. Author the explicit token map (one token per database)

For a hand-off package the **explicit whole-name map** is the agreed
reliable approach: one `LITERAL = {{TOKEN}}` line per database, so every
substitution is deterministic and reviewable rather than inferred by a
prefix heuristic. (For CallCentre this is the 19-entry map — one token
per database across the six modules and their three-database placement
split.)

Author it through the propose → `apply_diff` flow, never by hand:

```
# config/token_map.conf  (illustrative — one token per database)
CallCentre_DOM_STD_T = {{CallCentre_DOM_STD_T}}
CallCentre_DOM_STD_V = {{CallCentre_DOM_STD_V}}
CallCentre_DOM_BUS_V = {{CallCentre_DOM_BUS_V}}
CallCentre_SEM_STD_T = {{CallCentre_SEM_STD_T}}
…one line per database…
```

| Tool / flag | Why for this scenario |
|---|---|
| `ships_author_token_map` (MCP) | Returns a proposal envelope (`proposed_content`, `expected_hash`); commit with `ships_apply_diff`. Keeps comments and reference-count annotations intact. |
| explicit map | Every database resolves through a named token you can read in the package report — no surprises when the SE deploys it. |
| `ships_validate_token_map(project)` | Confirm the map parses before harvesting. |

> A `token_map.conf` **value must be a single whole `{{TOKEN}}`** — it
> cannot express a partial `{{DB_PREFIX}}_DOM_STD_T`. Whole-name tokens
> are exactly what demo mode wants.

## 2. Harvest — split and tokenise against the explicit map

```bash
python -m td_release_packager harvest \
  --project $PROJECT \
  --source  $SOURCE \
  --token-map config/token_map.conf \
  --force
```

| Flag | Why for this scenario |
|---|---|
| `--token-map` | **The key flag.** Drives substitution from the explicit map rather than the `--auto-tokenise` heuristic. Deterministic and reviewable — the right trade-off for a package a non-author will deploy. |
| `--source` | Read-only — harvest never modifies the export. Point it at the DBC export directory. |
| `--force` | Wipe `payload/database/` first so stale split-files from a previous harvest don't linger. Keep on every re-harvest; drop on the very first run. |

Do **not** combine `--token-map` with `--auto-tokenise` for a hand-off
build — the explicit map is the single source of truth.

Optional but commonly useful:

- `--remove-view-type-affixes` — strip legacy `_V` / `_T` suffixes from
  view-layer object names if the export carries them and your house style
  doesn't (object type belongs in the database name only).
- `--reconcile` — interactive prompt for ambiguous classifications the
  first time you onboard an unfamiliar export.

**Result:** `payload/database/{DDL,DCL,DML,…}` populated with eponymous
atomic files, each database literal replaced by its `{{TOKEN}}`. Grant
(DCL) files are harvested alongside the DDL.

## 3. Generate — view layer (only for topology products)

If the reference product follows the three-database placement standard
and the export did not include the locking / business views, regenerate
them so the demo deploys a complete object set:

```bash
python -m td_release_packager generate --project $PROJECT
```

Skip this when the export already contains every view (a full DBC export
usually does).

## 4. Scan — confirm DEMO can supply every token

```bash
python -m td_release_packager scan \
  --project $PROJECT \
  --all-envs \
  --fail-on-orphan
```

With only `DEMO` present, `--all-envs` resolves the payload against
`config/env/DEMO.conf` and flags any token the demo environment cannot
supply — the show-stopper for the build. `--fail-on-orphan` also catches
any token the env config defines but the payload never references.

If tokens come back undefined, fill in the real demo database names in
`config/env/DEMO.conf` (via `ships_author_env_config`, or
`bootstrap-env-config --env DEMO --force` to park every referenced token
for you to complete).

## 5. Inspect — lint clean for a trustworthy hand-off

```bash
python -m td_release_packager inspect --project $PROJECT --strict
```

A demo package should be not just `ERROR`-free but ideally
**warning-free** — the SE who deploys it cannot easily triage findings in
the field. `--strict` promotes warnings to errors so nothing soft slips
through. Run `ships fix --project $PROJECT` (default-on — clears
DDL terminators and grants derivation) and
`ships fix --project $PROJECT --rules non_ascii` to clear the mechanical
categories before the final build. `comment_length` is a guided fix
(truncation is a judgement call) and `set_multiset` is manual.

## 6. Analyse — deploy order

```bash
python -m td_release_packager analyse --project $PROJECT --overwrite
```

Writes `_waves.txt`, the deterministic deploy order the deployer consumes
in the field. `--graph $PROJECT/output/graphs --formats svg,dot` is worth
adding here — a dependency graph is a useful artefact to hand the SE
alongside the package.

## 7. Package — single DEMO build, full provenance, clean tree

```bash
python -m td_release_packager package \
  --project $PROJECT \
  --env DEMO \
  --name <release-name> \
  --author "$USER" \
  --description "CallCentre demo package for <customer>" \
  --change-ref <TICKET-ID>
```

| Flag | Why for this scenario |
|---|---|
| `--env DEMO` | Resolves every `{{TOKEN}}` against `config/env/DEMO.conf` and writes the substituted DDL into the release zip. |
| `--author` / `--description` / `--change-ref` | Provenance for the hand-off — recorded in `package_report.html` so the deploying team knows what they are running and why. |
| `--asymmetric-key path/to/key.pem` (optional) | Sign the package; the deployer can verify integrity at the customer site. Recommended when the package leaves your network. |

**Build from a clean working tree.** A clean tree keeps `source_commit`
authoritative in the provenance block, so the package's origin is
traceable. **Avoid `--allow-dirty`** for a hand-off build — a dirty tree
makes provenance unreliable, which is exactly what a deploying non-author
cannot afford. `ships_verify` reports **READY** once the archive exists,
the package stage recorded no warnings, and that stage succeeded.

**Auto-split:** when the export contains `CREATE DATABASE`/`USER`
statements as well as the objects that depend on them, Package emits two
archives — `<release>_01_prereqs.zip` (deploy first) and
`<release>_02_main.zip`. Hand both to the field team in that order.

**Output:** `<release-name>_BUILD_NNNN_<timestamp>.zip` plus
`package_report.html` showing the trust status and every substitution
applied.

## 8. Verify and hand off

```bash
# MCP, read-only
ships_verify(project=$PROJECT)            # ⇒ READY / NOT READY
ships_describe_package(project=$PROJECT)  # what's inside the newest archive
```

The deliverable is the package zip(s) **plus** `package_report.html`. The
field team does **not** need SHIPS installed — `database_package_deployer`
is a standalone tool that runs the archive directly. Confirm `ships_verify`
reports **READY** before the package leaves your hands. If it returns
**NOT READY**, read the per-check breakdown — archive present, no package
warnings, package stage succeeded — resolve the failing check, and
rebuild. Running inspect and package in the same pipeline keeps
`ships.decisions.json` populated so the package-stage status resolves cleanly.

## 9. Deploy at the demo (the receiving team)

Runtime is on-demand from a terminal — there is no SHIPS service to
install on the demo box.

```bash
# 1. Dry run first — pre-flight (permissions, space, object existence) still runs
python -m database_package_deployer deploy --dry-run <package_dir>

# 2. (Optional) EXPLAIN-only validation on the live server, no execution
#    via MCP: ships_deploy_explain(package_dir, host, user, password, logmech)

# 3. Live deploy — prereqs archive first if the build auto-split
python -m database_package_deployer deploy \
  --host <demo-host> --user <demo-user> <package_dir>
```

`logmech` is `TD2` by default (`LDAP` / `TDNEGO` as the demo system
requires) and `tmode=TERA` is honoured on every connection. Per-object
backups are captured before execution, so `rollback` against the
`.deploy_manifest.json` in the package `logs/` can unwind a bad demo
deploy.

## One-shot equivalent

`process` runs harvest → generate → inspect → analyse → package in order:

```bash
python -m td_release_packager process \
  --project $PROJECT \
  --source  $SOURCE \
  --token-map config/token_map.conf \
  --env DEMO \
  --name <release-name> \
  --strict
```

Good for rebuilding the demo payload quickly during development. Note,
though, that `process` exposes a curated subset of flags and **omits
`--change-ref`, `--asymmetric-key`, and `--build-number`**. For the final
package you actually hand to a customer team, run the steps individually
(or at least the final `package`) so the provenance and signing flags are
present. `process` always harvests in clean mode.
