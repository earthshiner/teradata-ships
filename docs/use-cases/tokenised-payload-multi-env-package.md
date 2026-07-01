# Tokenised payload → multi-environment packages

The primary SHIPS use case: legacy DDL/DML rarely arrives in a
SHIPS-compliant shape. SHIPS takes that messy source, splits it into
eponymous atomic files, replaces hard-coded database names with
`{{TOKEN}}` placeholders, validates the tokenised payload once, and
then re-tokenises that single payload into one package per target
environment.

```
                ┌─────────────┐
   raw DDL ───▶ │   HARVEST   │ ─▶ tokenised payload (one copy)
                └─────────────┘            │
                                           ├─▶ SCAN / INSPECT / ANALYSE
                                           │      (validate once)
                                           │
                                           ├─▶ PACKAGE --env DEV ─▶ DEV.zip
                                           ├─▶ PACKAGE --env TST ─▶ TST.zip
                                           └─▶ PACKAGE --env PRD ─▶ PRD.zip
```

In the rest of this document, `$PROJECT` is the SHIPS project root and
`$SOURCE` is the directory holding the raw legacy DDL you want to
package.

## 0. Scaffold (once per project)

```bash
python -m td_release_packager scaffold \
  --name <ProjectName> \
  --output <parent-dir> \
  --environments DEV,TST,PRD
```

Creates `$PROJECT/` with `config/env/{DEV,TST,PRD}.conf` skeletons,
`payload/database/`, `ships.yaml`, and `config/inspect.conf`. **Edit
`config/env/*.conf`** with the real database names for each
environment before you harvest — those values are what SHIPS will
substitute back in at packaging time.

## 1. Harvest — split and tokenise

```bash
python -m td_release_packager harvest \
  --project $PROJECT \
  --source  $SOURCE \
  --auto-tokenise \
  --force
```

| Flag | Why for this scenario |
|---|---|
| `--auto-tokenise` | **The key flag.** Scans the source for hard-coded database names, rewrites them to `{{TOKEN}}` placeholders, and writes the mapping into `config/token_map.conf`. This is what makes the payload environment-portable. |
| `--source` | Read-only — harvest never modifies the source tree. |
| `--force` | Wipe `payload/database/` first so stale split-files from a previous harvest don't linger. Drop on the very first run; keep on every re-harvest. |

Optional but commonly useful:

- `--prefix-token PREFIX=TOKEN` — when the source uses a stem like
  `BIONIC_DEV_` that should resolve to a single `{{ENV_PREFIX}}` token
  varying by environment.
- `--reconcile` — interactive prompt for ambiguous classifications and
  rename decisions. Worth turning on the first time you onboard a
  messy legacy tree.
- `--remove-view-type-affixes` — strip legacy `_V` / `_T` suffixes
  from view-layer object names if your house style doesn't keep them.

**Result:** `payload/database/{DDL,DCL,DML,…}` populated with
eponymous atomic files, each containing `{{TOKEN}}` references in
place of database literals. `config/token_map.conf` records every
substitution decision.

## 1b. Align env config to the harvested tokens

`--auto-tokenise` chooses token names based on the literal database
prefixes it finds in your source — e.g. a source DDL using
`CallCentre_DOM_STD_T.Agent_H` becomes
`{{CallCentre_DOM_STD_T}}.Agent_H` in the payload. If the
`config/env/*.conf` files were scaffolded with a different naming
convention (a common case when the scaffold template predates the
data you're harvesting), the payload tokens won't intersect the env
config, and step 2 will report every reference as undefined.

Re-generate each environment's config to match the actual tokens
the harvest produced:

```bash
for env in DEV TST PRD; do
  python -m td_release_packager bootstrap-env-config \
    --source $PROJECT \
    --env $env \
    --force
done
```

| Flag | Why for this scenario |
|---|---|
| `--source` | The project whose tokenised payload should drive the env config. |
| `--env` | Which environment file to (re)generate — `<source>/config/env/<env>.conf`. |
| `--force` | Overwrite any pre-existing template that doesn't match the harvest output. Drop this on the very first project so an editorial draft isn't clobbered; keep it whenever you re-harvest with a different naming convention. |

`bootstrap-env-config` parks every referenced token in section 8 of
the .conf for you to fill in with the real database name per
environment. The composition-roots cascade (sections 1–2) is
untouched on existing files unless `--force` rewrites them.

When you can't or don't want to regenerate the env config (e.g. you
have a hand-authored cascade you want to keep), the alternative is to
re-run harvest with explicit `--prefix-token` mappings so the
payload's token names match the names the env config already
defines:

```bash
python -m td_release_packager harvest \
  --project $PROJECT \
  --source  $SOURCE \
  --prefix-token CallCentre_DOM_STD_T={{DB_DOMAIN_STD_T}} \
  --prefix-token CallCentre_DOM_STD_V={{DB_DOMAIN_STD_V}} \
  --force
```

Use one or the other approach per project, not both.

## 2. Scan — validate token coverage

```bash
python -m td_release_packager scan \
  --project $PROJECT \
  --all-envs \
  --fail-on-orphan
```

| Flag | Why for this scenario |
|---|---|
| `--all-envs` | Resolve the tokenised payload against every `config/env/*.conf`. Flags any token an environment cannot supply — the show-stopper for that environment's package build. |
| `--fail-on-orphan` | Non-zero exit if any env defines a token the payload never references. Catches drift between env config and DDL. Keep off during early development; turn on in CI. |

If many tokens come back undefined, step 1b is the fix — the env
config and payload were generated with mismatching naming. Scan now
points at `bootstrap-env-config` in that case.

## 3. Inspect — lint the tokenised payload

```bash
python -m td_release_packager inspect \
  --project $PROJECT
```

Reads `config/inspect.conf` (auto-generated by scaffold). Checks token
format, keyword case, leading commas, grant consistency, and the
Coding Discipline rules registered there.

Useful add-ons:

- `--strict` — promote warnings to errors. Use in CI gates.
- `--fix-grants` — auto-repair missing `.grt` entries in place.
- `ships fix` (separate verb) — auto-repair DDL terminators, non-ASCII
  characters, and every other rule in the fix registry. Run it before
  `inspect` (or wait for the process pipeline stage in #523).

## 4. Analyse — dependency waves

```bash
python -m td_release_packager analyse \
  --project $PROJECT \
  --overwrite
```

Walks dependencies and writes `_waves.txt` — the deploy order the
packager and deployer both consume. `--overwrite` keeps it deterministic
across re-runs.

Optional: `--graph $PROJECT/output/graphs --formats svg,dot` to export
a visual dependency graph for review.

## 5. Package — re-tokenise to target environment

```bash
python -m td_release_packager package \
  --project $PROJECT \
  --env DEV \
  --name <release-name> \
  --author "$USER" \
  --description "<short change summary>" \
  --change-ref <TICKET-ID>
```

| Flag | Why for this scenario |
|---|---|
| `--env DEV` | **The re-tokenisation step.** The packager resolves every `{{TOKEN}}` in the payload against `config/env/DEV.conf` and writes the substituted DDL into the release zip. Change `--env` and re-run to produce a TST or PRD package from the same payload — no re-harvest needed. |
| `--name` | Logical release name. Appears in the zip filename and the package report. |
| `--author` / `--description` | Recorded in the package report's provenance block. |
| `--change-ref` | Ticket / change ref carried into the deploy log for traceability. |

Optional:

- `--build-number 0042` to override the auto-incremented counter, or
  `--no-increment` to reuse the current one.
- `--asymmetric-key path/to/key.pem` to sign the package; the
  deployer can later verify integrity.
- `--allow-dirty` is intentionally **avoided** for production builds —
  a dirty tree means the `source_commit` in provenance is unreliable.

**Output:** `<release-name>_BUILD_NNNN_<timestamp>.zip` plus
`package_report.html` showing the trust state and every substitution
applied.

## Building multiple environment packages from one payload

The payload only needs to be harvested / scanned / inspected /
analysed **once**. Each environment is just a final `package --env
<ENV>` against the same tokenised payload:

```bash
for env in DEV TST PRD; do
  python -m td_release_packager package \
    --project $PROJECT \
    --env $env \
    --name <release-name>
done
```

This is the round-trip the use case describes: source → tokenise once
→ many environment-specific packages.

## One-shot equivalent

`process` runs harvest → generate → inspect → analyse → package in
order. Add `--strict` to stop on the first stage that finishes with
errors; without it every stage runs and errors are summarised at the
end. Useful in CI:

```bash
python -m td_release_packager process \
  --project $PROJECT \
  --source  $SOURCE \
  --auto-tokenise \
  --env DEV \
  --name <release-name> \
  --strict
```

`process` exposes a curated subset of the per-stage flags. Notable
omissions vs. the individual steps:

- No `--force` / `--keep-existing` — `process` always runs harvest in
  the default (clean) mode.
- No `--reconcile` — interactive reconciliation belongs in standalone
  `harvest` runs, not in a one-shot pipeline.
- No `--build-number`, `--no-increment`, `--change-ref`,
  `--allow-dirty`, or signing flags — when you need any of those,
  run the steps individually and pass the flag to `package`.

For multi-env builds, run `process` once to land the payload, then
loop over `package --env <ENV>`.
