# SHIPS Navigator

Offline, single-file HTML wizard for guided SHIPS packaging.

Tracking issue: [#380 — SHIPS Navigator (offline HTML wizard)](https://github.com/earthshiner/teradata-ships/issues/380)
Milestone: [Guided Packaging (SHIPS Navigator)](https://github.com/earthshiner/teradata-ships/milestone/4)

## What it does

Walks a user through the SHIPS packaging decisions without an AI present and emits:

1. An ordered command sequence in **three shells** — PowerShell (`.ps1`), bash (`.sh`), and Windows cmd (`.bat`). Long paths bind to shell variables (`$SOURCE`, `$OUTPUT_ROOT`, `$NAME`, `$PROJECT`, `$PKG_NAME`, `$GRAPHS_DIR`, plus `$ENV` / `$ENV_CONFIG` reset per environment block) so retargeting is a one-line edit at the top of the script. Two emission modes:
   - **Quick** — one `ships process` call per environment (the full SHIPS pipeline behind one verb).
   - **Detailed** — each pipeline step (`scaffold` / `harvest` / `generate` / `inspect` / `scan` / `analyse` / `package`) shown separately for transparency, CI scripting, or selective re-runs. (`ships stage` is a separate post-package git-staging gate, not a pipeline step — see the [user guide](../../docs/USER_GUIDE.md#ships-stage).)
2. A `config/tokenise.conf` matching the chosen token model (prefix or per-database).
3. One `config/env/<ENV>.conf` skeleton per target environment. If the source is already tokenised, the wizard instead emits `ships bootstrap-env-config` calls so SHIPS scaffolds the real env files from the payload's actual token usage.
4. A `ships.yaml` with a `packaging:` profile (source, package name, default env, env-config) capturing the wizard's answers so `ships process --project .` re-runs argless (#382, consumed by the single front door #384).
5. A "Why each step" rationale + a glossary of the vocabulary.

The wizard is deterministic — the same answers always produce the same output.

## How to use

Double-click `ships-navigator.html` from any file share, or open it from your browser's `File → Open` menu. No server, no network, no install.

1. Answer the questions in the left pane. Hidden questions auto-clear when their parent answer rules them out. Heads-up messages surface above the output for things like a trailing-underscore prefix or a non-absolute project path.
2. Read the tabs in the right pane (Commands, tokenise.conf, env files, ships.yaml, inspect.conf, Why each step, Glossary, FAQ). Each artefact has Copy + Download buttons.
3. Click **Download all (.zip)** at the top of the outputs to get every generated file plus a `plan.json` and a small `README.md` in one bundle. The ZIP encoder is vanilla JS — no CDN.
4. Answers persist to `localStorage`, so refreshing the page doesn't wipe a 13-question fill. Use **Reset** in the questions header to clear.
5. (Optional) Export `plan.json` from the rationale tab so the next run can paste it in and skip the questionnaire.

## Decision model

The authoritative elicitation model lives in
[`decision-tree.yaml`](decision-tree.yaml) (issue #378) — the single source of
truth shared by the HTML wizard, the CLI wizard, and the AI skill. The model is
data: each question carries its `id`, `label`, `hint`, `kind`, `options`,
optional `default`, and a `show` / `warn` condition expressed in a small DSL
(`eq` / `ne` / `truthy` / `all` / `any` / `derived`) so every front end
evaluates visibility identically.

The Python side loads and evaluates it via
`td_release_packager.decision_tree` (`load_decision_tree`, `is_visible`,
`active_warnings`). The offline single-file HTML wizard embeds the same model as
its inline `QUESTIONS` array — it can't read an external file at `file://` —
and a lockstep test (`src/tests/test_decision_tree.py::TestLockstep`) fails the
build if the YAML and the inline copy drift. Edit the YAML and mirror the change
in the HTML in the same commit.

The questions encoded here, in order:

| # | Question | Notes |
|---|----------|-------|
| Mode | Quick vs Detailed | One `ships process` call per env, or each pipeline step explicit |
| Q1 | Source location | `github` (owner/repo + ref) or `filesystem` (path) |
| Q2 | Already tokenised? | `yes` skips Q3/Q4 and triggers `bootstrap-env-config` for each env |
| Q3 | Token shape | `prefix` (one `DB_PREFIX`) or `per_database` (one binding per db) |
| Q4 | Product prefix | e.g. `CustomerDNA` |
| Q5 | Atomic & eponymous? | Reassures on `no` / `unsure` that SHIPS auto-splits multi-object files (incl. procedures/macros/triggers/functions) into atomic, eponymous files |
| Q6 | Generate view layer? | Maps to `ships generate` (or `--skip-generate` on Quick) — locking/access/business views |
| Q7 | Dependency analysis (waves)? | Maps to `ships analyse` — emits `_waves.txt` |
| Q8 | Export dependency graph? | Adds `--graph` to `analyse` (incl. OpenLineage JSON) + namespace / project name |
| Q9 | Orphan-token scan? | Adds `ships scan --all-envs --fail-on-orphan` as a CI gate |
| Q10 | Target environments | Free list — e.g. `DEV, TST, PRD` |
| Q11 | Project path | Drive-letter selects Windows `.bat` output; checkbox controls whether `scaffold` runs |
| Q12 | Package name | Build artefact name — e.g. `create_objects` |
| Strict | Abort on first stage error | Maps to `--strict` on `process` |

## SHIPS features the wizard surfaces

Beyond the four-step harvest/inspect/package path, this wizard makes the rest of the pipeline visible to new users:

- **`ships scaffold`** — runs automatically when "project already scaffolded?" is unticked.
- **`ships generate`** — view-layer DDL generation per the object-placement standard.
- **`ships scan --all-envs --fail-on-orphan`** — orphan-token CI gate.
- **`ships analyse`** — wave ordering, with optional dependency graph export (`--graph`, `--namespace`, `--project-name`) for OpenLineage / impact analysis.
- **`ships process`** — the single front-door verb that orchestrates the pipeline and records every stage decision into `ships.decisions.json`.
- **`ships bootstrap-env-config`** — scaffolds env files from an already-tokenised source so the user never has to guess the token list.
- **`ships deploy`** is mentioned in the rationale panel as the next step after `package`; the wizard deliberately stops at `package` because deployment needs a live Teradata connection.

## Guardrails encoded

These are the empirically-verified rules the wizard refuses to violate (handover Part B §6):

- Only `--prefix-token` and `config/tokenise.conf` are offered for tokenisation — `--auto-tokenise` / `--token-map` are kept out of the wizard until [#383 — consolidate tokenisation paths](https://github.com/earthshiner/teradata-ships/issues/383) lands, because those modes tokenise content but leave filenames literal.
- `config/inspect.conf` (`rule=SEVERITY`) and `config/env/<ENV>.conf` (`TOKEN=value`) are clearly distinguished. The generated `inspect` command never carries `--config`, so an env file can never accidentally end up there.
- `inspect` has no `--env` flag — coverage auto-discovers `config/env/*.conf`. The rationale panel states the "bind every token in every env file" requirement.
- `harvest` cleans the payload by default. `--keep-existing` is only mentioned as the overlay opt-out.
- Atomic + eponymous applies to DDL only; DCL is grouped per granted-ON database, DML is kept whole.
- Detailed mode + GitHub source raises a warning, because `ships harvest` does NOT accept `--source-github` (only `process` and `package` do).

## Inline validation

The wizard flags common-mistake patterns above the output panel without blocking generation:

- Prefix with a trailing underscore (the tokenise rule already adds it).
- Prefix with illegal characters.
- Non-absolute project path.
- Package name with spaces.
- Empty / unparseable env list.
- Detailed mode + GitHub source (suggests switching to Quick mode).

## Branding

- Colours: Orange `#FF5F02`, Navy `#00233C`, White `#FFFFFF` (exact hex per the brand guidelines).
- Logo: the official `teradata_sym_rgb_pos.png` is embedded as base64 from the `teradata-brand` skill's `assets/logo-base64-reference.md`. Per brand guidelines we use the provided asset rather than recreating the mark.
- Font: system stack with Inter as the preferred family (`'Inter', -apple-system, 'Segoe UI', Roboto, sans-serif`). Full offline brand compliance wants Inter embedded as base64 woff2 — flagged as a follow-up; the handover marks the system stack acceptable for v1.

## Renaming the app

The display name is a one-line change at the top of the script block:

```js
const APP_NAME = "SHIPS Navigator";
```

## File layout

```
tools/navigator/
  ships-navigator.html   # the wizard
  decision-tree.yaml     # authoritative elicitation model (#378)
  README.md              # this file
```

## Troubleshooting

### Windows: "These files can't be opened"

Windows tags browser-downloaded files with Mark-of-the-Web (a hidden `Zone.Identifier` alternate data stream), and SmartScreen blocks `.ps1` / `.bat` from running. The wizard's outputs are subject to this. Two ways round it:

1. **Copy, don't download.** Use the Copy button on the script block and paste straight into your terminal. No file is created, so MOTW never applies.
2. **`Unblock-File`.** After downloading, run in PowerShell:
   ```powershell
   Unblock-File -Path .\ships-run.ps1
   # or .\ships-run.bat
   ```
   To unblock everything in a downloaded bundle in one go:
   ```powershell
   Get-ChildItem -Recurse -Include *.ps1,*.bat,*.sh | Unblock-File
   ```

The wizard already includes this hint as a comment at the top of every `.ps1` and `.bat` it emits.

## Verifying offline-ness

Open the file in a browser, open DevTools → Network, then reload. There should be zero network requests (the logo loads via a `data:` URI, not over the wire; the ZIP encoder is inline; no fonts are fetched).
