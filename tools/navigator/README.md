# SHIPS Navigator

Offline, single-file HTML wizard for guided SHIPS packaging.

Tracking issue: [#380 — SHIPS Navigator (offline HTML wizard)](https://github.com/earthshiner/teradata-ships/issues/380)
Milestone: [Guided Packaging (SHIPS Navigator)](https://github.com/earthshiner/teradata-ships/milestone/4)

## What it does

Walks a user through the SHIPS packaging decisions without an AI present and emits:

1. An ordered command sequence (`.bat` for Windows, `.sh` for POSIX) — `scaffold` (if needed) → `harvest` → `inspect` → `package`.
2. A `config/tokenise.conf` matching the chosen token model (prefix or per-database).
3. One `config/env/<ENV>.conf` skeleton per target environment, with the bindings the chosen token model needs.
4. A "Why each step" panel with the correctness guardrails baked in.

The wizard is deterministic — the same answers always produce the same output.

## How to use

Double-click `ships-navigator.html` from any file share, or open it from your browser's `File → Open` menu. No server, no network, no install.

1. Answer Q1–Q7 in the left pane. Hidden questions auto-clear when their parent answer rules them out.
2. Read the four tabs in the right pane. Each artefact has a **Copy** button and a **Download** button.
3. (Optional) Export a `plan.json` from the rationale tab so the next run can paste it in and skip the questionnaire.

## Decision model

v1 inlines the decision tree directly in the HTML for portability. Once
[#378 — declarative decision model `decision-tree.yaml`](https://github.com/earthshiner/teradata-ships/issues/378)
lands, the wizard will be regenerated from that shared model so the CLI, HTML, and AI front ends stay lock-step.

The question set encoded here is Part B §3 of `HANDOVER-ships-navigator-guided-packaging.md`:

| # | Question | Notes |
|---|----------|-------|
| Q1 | Source location | `github` (owner/repo + ref) or `filesystem` (path) |
| Q2 | Already tokenised? | `yes` skips Q3 and Q4 |
| Q3 | Token shape | `prefix` (one `DB_PREFIX`) or `per_database` (one binding per db) |
| Q4 | Product prefix | e.g. `CustomerDNA` |
| Q5 | Atomic & eponymous? | Surfaces the BEGIN…END / macro caveat on `no` / `unsure` |
| Q6 | Target environments | Free list — e.g. `DEV, TST, PRD` |
| Q7 | Project path | Drive-letter selects Windows `.bat` output |

## Guardrails encoded

These are the empirically-verified rules the wizard refuses to violate (Part B §6):

- Only `--prefix-token` and `config/tokenise.conf` are offered — `--auto-tokenise` / `--token-map` are kept out of the wizard until [#383 — consolidate tokenisation paths](https://github.com/earthshiner/teradata-ships/issues/383) lands, because those modes tokenise content but leave filenames literal.
- `config/inspect.conf` (`rule=SEVERITY`) and `config/env/<ENV>.conf` (`TOKEN=value`) are clearly distinguished. The generated commands never put an env file behind `--config`.
- `inspect` has no `--env` flag — coverage auto-discovers `config/env/*.conf`. The rationale panel states the "bind every token in every env file" requirement.
- `harvest` cleans the payload by default. `--keep-existing` is only mentioned as the overlay opt-out.
- Atomic + eponymous applies to DDL only; DCL is grouped per granted-ON database, DML is kept whole.

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
  README.md              # this file
```

A `decision-tree.yaml` will join this directory under [#378](https://github.com/earthshiner/teradata-ships/issues/378).

## Verifying offline-ness

Open the file in a browser, open DevTools → Network, then reload. There should be zero network requests (the embedded logo loads via a `data:` URI, not over the wire).
