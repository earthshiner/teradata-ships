# SHIPS Glossary

SHIPS is a structured packaging, validation, deployment, evidence, and context-exchange framework for Teradata assets. It is not an agent by itself; it is the durable operational substrate that humans, CI/CD pipelines, MCP tools, and autonomous agents can use safely.

## Core terms

| Term | Meaning |
|---|---|
| SHIPS | The framework and methodology for turning database assets into trusted, deployable packages with validation, provenance, governance, and evidence. |
| `teradata-ships` | The repository/project name for the SHIPS implementation focused on Teradata packaging and deployment. |
| Agent | An autonomous or semi-autonomous actor that can read SHIPS artefacts, make bounded decisions, and invoke SHIPS workflows. SHIPS enables agents but is not itself an agent. |
| Agentic context | A workflow where one or more agents participate in build, validation, review, deployment, or evidence collection while using durable package context instead of transient chat memory. |
| Human-in-the-loop | A workflow where a human developer, DBA, approver, or operator reviews and authorises specific stages rather than delegating the whole process to automation. |
| Package | A self-contained deployable artefact containing resolved payload files, deployment engine, metadata, validation context, evidence references, and operator instructions. |
| Payload | The deployable database content inside the package, organised into numbered phases such as system objects, pre-requisites, DCL, DDL, DML, and post-install scripts. |
| Package builder | The SHIPS component that resolves tokens, validates package inputs, copies payload, embeds the deployer, emits manifests/reports/context, and archives the result. |
| Package deployer | The embedded or standalone deployment runtime that reads package metadata and executes package content against a Teradata target. |
| Deployment contract | The machine-readable promises a package makes about what it contains, what it depends on, how it should be deployed, and what controls must be satisfied. |
| Context contract | The durable context exchanged between actors and agents, represented by files such as `context/ships.context.json`, `context/ships.manifest.json`, and `context/ships.handoff.json`. |

## Pipeline terms

| Term | Meaning |
|---|---|
| Scaffold | The stage that creates a SHIPS project structure, configuration skeleton, payload directories, and control files. |
| Harvest | The stage that imports raw DDL/DCL/DML files into the canonical SHIPS payload structure. |
| Generate | The stage that derives additional artefacts, such as generated view-layer DDL, from harvested inputs. |
| Inspect | The stage that validates DDL quality, token format, coding discipline, and grant consistency. |
| Analyse / Analyze | The stage that examines dependency relationships, produces deployment waves, and can export graph formats. |
| Package | The stage that resolves tokens for a target environment and creates a self-contained release package. |
| Ship | The deployment stage where the package is executed against a target environment by a human, tool, CI/CD job, or deployment agent. |
| Process | The meta-command that runs the SHIPS stages in sequence and records one coherent run in `ships.decisions.json`. |
| Strict mode | A mode where errors stop the pipeline immediately, normally used for platform or controlled promotion workflows. |
| Developer mode | A more permissive mode designed for fast iteration, where warnings are surfaced but do not necessarily stop the workflow. |

## Artefacts

| Artefact | Meaning |
|---|---|
| `ships.yaml` | Project-level SHIPS configuration, including environments, paths, stage policy, and deployment controls. |
| `ships.decisions.json` | Append-only stage/run audit trail. It records what happened, which options were resolved, what decisions were made, and what issues were encountered. |
| `context/ships.build.json` | Authoritative package build manifest embedded in the package. It contains technical package metadata, resolved token values, inventory, trust flags, and deployment controls. |
| `context/ships.context.json` | Agent-facing durable workflow context. It explains current state, objective, constraints, source-of-truth pointers, trust state, governance controls, and evidence references. |
| `context/ships.manifest.json` | Agent-safe package inventory and dependency contract. It summarises the package without duplicating sensitive or high-volume details. Token values are deliberately redacted here and referenced through `context/ships.build.json`. |
| `context/ships.handoff.json` | Next-actor handoff instructions for a human, deployment agent, CI/CD job, or MCP tool. It lists required actions, preconditions, blocking conditions, and evidence to return. |
| `context/ships.provenance.json` | File-level traceability from source to eponymous name, token-resolved name, and packaged file. |
| `context/ships.integrity.json` | Package fingerprint manifest used to verify package contents have not changed unexpectedly. |
| `package_report.html` | Human-readable package report showing inventory, trust information, and deployment guidance. |
| `README.txt` | Operator-oriented package instructions embedded in the package. |
| `.build_counter` | Project-local build counter used to allocate incrementing build numbers. |

## Configuration and token terms

| Term | Meaning |
|---|---|
| Token | A placeholder such as `{{CORE_T}}` that is resolved at package time from an environment configuration file. |
| Environment configuration | A `.conf` file under `config/env/` containing environment-specific token values and controls. |
| `SHIPS_ENV` | The environment marker inside an environment config file. SHIPS checks it against `--env` to avoid building a DEV-labelled package with PROD values. |
| Token map | A mapping from literal database names to SHIPS token placeholders, normally used during harvest or migration from legacy source. |
| Malformed token | A broken token marker, such as one with stray whitespace or unmatched braces, that must be fixed before packaging. |
| Five-layer cascade | Configuration resolution model: Layer 5 CLI, Layer 4 environment properties, Layer 3 project config, Layer 2 platform template, Layer 1 defaults. |
| Layer 1 defaults | Built-in developer-friendly defaults. |
| Layer 2 template | Optional cross-project platform standard. |
| Layer 3 project config | Project-level committed configuration such as `ships.yaml`. |
| Layer 4 environment properties | Per-environment values, especially token values. |
| Layer 5 CLI | Per-invocation command-line overrides. |

## Deployment terms

| Term | Meaning |
|---|---|
| Phase | A numbered payload directory that controls deployment order. |
| System phase | Phase for system-scope objects such as maps, roles, profiles, authorizations, and foreign servers. |
| Pre-requisites phase | Phase for foundation objects such as databases and users. |
| DCL phase | Phase for grants and access control scripts. |
| DDL phase | Phase for structural database objects such as tables, views, macros, procedures, functions, indexes, and triggers. |
| DML phase | Phase for reference data, seed data, or controlled data changes. |
| Post-install phase | Phase for validation, statistics, cleanup, and smoke-test scripts. |
| Wave | A dependency-safe group of objects that can be deployed together or in a calculated order. |
| Auto-split | Build behaviour where SHIPS emits paired pre-requisite and main packages when a package contains both container objects and dependants. |
| `release_group` | Shared identifier that ties auto-split packages together. |
| `requires` | Package dependency list. A main package can require a pre-requisite package to be deployed first. |
| Target environment lock | Deployment control that requires the deployment target to match the environment stamped into the package. |
| Change reference | Change-ticket or approval reference stamped into the package and enforced when required. |
| Four-eyes approval | A control requiring a second operator/approver before deployment. |
| TLS requirement | A control requiring deployment over a TLS/SSL-protected Teradata connection. |
| Package age TTL | A control that warns or blocks if a package is older than the configured maximum age. |

## Trust, evidence, and governance terms

| Term | Meaning |
|---|---|
| Trust report | A package readiness assessment stamped into the package, usually expressed as labels such as READY, READY-WITH-CAVEATS, or BLOCKED. |
| READY | The package has passed the relevant checks for deployment. |
| READY-WITH-CAVEATS | The package can proceed only with understood caveats, such as a dirty source tree or warnings. |
| BLOCKED | The package should not be deployed until blocking issues are resolved. |
| Evidence | Files and records that prove what was built, validated, approved, deployed, skipped, failed, or waived. |
| Provenance | Traceability from packaged artefacts back to source files and build-time transformations. |
| Integrity fingerprint | Hash-based proof of package contents. |
| Deployment evidence | Logs, manifests, audit rows, post-install outputs, and validation results captured after deployment. |
| Context budget | The design rule that agents receive compact structured context and open detailed evidence only when needed. |

## Naming and source terms

| Term | Meaning |
|---|---|
| Atomic script | A script containing one primary database object or deployment concern. |
| Eponymous script | A script whose filename matches the object it defines or deploys. |
| E&A | Short form for atomic and eponymous. |
| Source dirty | A build stamped as coming from a Git working tree with uncommitted tracked changes. |
| Source commit | The Git commit hash recorded for traceability when supplied. |
| Legacy substitutions | Older variable formats such as `$VAR`, `${VAR}`, or `&&VAR&&` that SHIPS can migrate to `{{TOKEN}}` form. |
| Object placement | Rules that determine where harvested objects land in the SHIPS payload structure. |
| Grant inference | SHIPS analysis that derives required inter-database grants from DDL intent. |

## Agent interoperability terms

| Term | Meaning |
|---|---|
| MCP tool | A Model Context Protocol tool that exposes SHIPS capabilities to agent hosts. |
| Agent handoff | Passing a package and its durable context to another actor without assuming shared memory. |
| Next actor | The human, CI/CD job, MCP tool, or autonomous agent expected to continue the workflow. |
| Handoff evidence | The result data a receiving actor should return after completing its stage. |
| Source of truth | The canonical reference for package content and decisions, typically the Git commit, package metadata, and embedded evidence files. |
| Context engineering | Designing durable, compact, machine-readable context so agents remain efficient, safe, and effective across handoffs. |

## New/updated package metadata artefacts

| Term | Meaning |
|---|---|
| `context/ships.index.json` | Canonical read-first SHIPS package index. Describes each package metadata file, marks required artefacts, provides the recommended read order, and carries standing agent instructions. |
| `ships.decisions.json` | Canonical project-level decision/audit trail. Replaces the older `decisions.json` name and records pipeline stage outcomes, decisions, issue codes, config provenance, and output references. |
| Context entrypoint | The machine-readable pointer returned by MCP/package tooling that tells a downstream agent to read `context/ships.index.json` before taking action. |
| Recommended read order | The ordered list in `context/ships.index.json` that tells agents and operators how to inspect SHIPS metadata before deployment, approval, modification, or summary. |
