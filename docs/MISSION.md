# SHIPS Mission Statement

## Why SHIPS Exists

The database deployment space was shaped by a world of human operators: DBAs who understood context, asked clarifying questions, recovered from ambiguity, and carried institutional knowledge in their heads. The tooling that emerged from that world — release scripts, change logs, manual checklists — reflects that assumption. It was designed to be *read* by people, not *consumed* by machines.

That world is changing.

AI agents are becoming active participants in software delivery. They generate code, analyse schemas, detect regressions, and increasingly orchestrate their own deployments. But when an agent reaches the database layer, it hits a wall: deployment artefacts that were never designed for machine interpretation, dependency ordering baked into tribal knowledge, and packaging workflows that assume a human is watching.

SHIPS is the bridge.

---

## Mission

> **Provide a Teradata deployment framework that is equally capable of serving human operators and autonomous AI agents — today and as the agentic software delivery ecosystem matures.**

SHIPS does this by treating **metadata quality, object granularity, deterministic structure, and machine interpretability as first-class architectural concerns**, not secondary implementation details.

---

## The SHIPS Spectrum

SHIPS is intentionally designed to serve users at every point on the deployment formality spectrum — from a developer deploying a personal proof-of-concept to an autonomous agent deploying to a regulated production environment. The workflow is the same at every point; only the ceremony level changes.

| Mode | User | Ceremony | What they get |
|---|---|---|---|
| **Personal / PoC** | Developer | Zero — `ships process` and done | A build-numbered, rollback-capable, audit-trailed package without any packaging expertise |
| **Team / Project** | Developer + DBA | Low — review token map, hand off package | Formal DBA handoff with trust report, pre-flight validation, and HTML deployment report |
| **Enterprise** | Release manager + CI | Medium — governed pipeline | Schema drift detection, compliance audit trail, OpenLineage catalog integration |
| **Agentic** | AI agent | None — fully autonomous | All of the above, consumed via MCP tools or deterministic CLI exit codes |

A developer doing a Friday-afternoon PoC gets a SHA-256 tamper-proof package with a deployment audit trail in Teradata DBQL — whether they care about it or not. When that PoC becomes a production deployment, the artefact quality was already there. Nothing needs to be re-built.

---

## What SHIPS Enables

### 1. Human-Managed Deployment Packaging
Developers, release managers, and DBAs can create, review, validate, and deploy database packages through controlled, auditable, repeatable processes. The workflow is structured enough to be governed, and simple enough that a developer only needs to drop files in a directory.

### 2. Autonomous / Agentic Packaging and Deployment
AI agents can independently analyse source assets, construct deployment packages, validate dependencies, sequence deployment order, generate manifests, and prepare deployment instructions — all without human intervention. Under governed controls, agents can also execute deployments.

### 3. Git-Based Packaging

Packages can be generated directly from source control without requiring a local git clone. SHIPS supports:

- **GitHub repository** — `--source-github owner/repo --source-ref main` downloads the repository tarball via the GitHub REST API, extracts it, and runs the full pipeline. No `git` installation required. The resolved commit SHA is automatically stamped into `context/ships.build.json`.
- **Local git archive** — `git archive <ref>` piped into a temp directory, then `ships process --source /tmp/extracted/`. The `ships rollback --to-tag` command uses this internally.
- **CI/CD checkout** — the repository is already present in the workspace; SHIPS runs on the current directory.
- **GitHub Enterprise Server** — set `SHIPS_GITHUB_API_URL` to route requests to an enterprise endpoint.

This supports both traditional CI/CD pipelines (GitHub Actions, GitLab CI) and agent-driven release workflows where an autonomous agent packages on demand from any ref in any accessible repository.

### 3b. Frictionless PoC and Demo Packaging

Developers building proofs-of-concept, demos, or personal projects can get a deployment-ready, auditable package with a single command and no packaging expertise:

```bash
python -m td_release_packager process \
    --project my_poc \
    --source my_sql/ \
    --auto-tokenise \
    --env DEV \
    --env-config config/env/DEV.conf \
    --name my_poc
```

`--auto-tokenise` detects and tokenises all hardcoded database names in one pass. No token map review, no manual file editing. The output is a self-contained `.zip` with an embedded deployment engine — the developer can hand it to a DBA or deploy it themselves. The enterprise-grade artefact quality (build number, integrity fingerprint, rollback capability, DBQL audit trail) is present whether the developer thinks about it or not.

When the PoC graduates to a production project, no re-packaging is needed. The workflow is identical; only the governance around it changes.

### 4. Frictionless Developer Ingestion
Developers drop Teradata assets into a directory. SHIPS does the rest: classifies object types, validates syntax, identifies dependencies, generates manifests, determines deployment ordering, and produces a deployment-ready package. No packaging expertise required.

### 5. Legacy Codebase Packaging
Existing "cold" or unmanaged database codebases — environments never designed for CI/CD — can be ingested, analysed, and converted into trusted, self-contained deployment packages. SHIPS meets the codebase where it is.

### 6. Trusted, Self-Contained Packages
Every package SHIPS produces is:

| Property | Meaning |
|---|---|
| **Portable** | Moves between teams, pipelines, and environments unchanged |
| **Deterministic** | Same inputs always produce the same package |
| **Auditable** | Full provenance: who built it, from what, when |
| **Reproducible** | Can be rebuilt from source at any point in time |
| **Self-describing** | Carries its own manifest, metadata, and deployment instructions |

No external knowledge is required to deploy a SHIPS package. Everything the deployer needs is inside the package.

Trust is not assumed — it is **built in and verified at deployment time**. See [Governance and Auditability](#governance-and-auditability) below.

### 7. Multi-Consumer Deployment Targets
A single package can be consumed by customer DBAs, internal DBAs, CI/CD pipelines, orchestration frameworks, or autonomous deployment agents — without modification.

### 8. Agent-Ready Metadata and Structure
SHIPS packages are explicitly designed for machine consumption. This includes:

- **Atomic, eponymous object files** — one object per file, named for the object
- **Rich manifest metadata** — type, intent, wave, dependencies, idempotency class
- **Explicit dependency graphs** — in dot, Mermaid, JSON, CSV, and OpenLineage formats
- **Deployment intent** — what the DDL verb means in execution terms
- **Idempotency characteristics** — what happens on re-run
- **Environment targeting** — token-resolved per environment
- **Rollback metadata** — pre-deployment SHOW captures for recovery; wave, full-build, and git-tag rollback paths
- **Validation status** — inspection results and Trust Report embedded in the package
- **Object lineage** — where each object came from, what it depends on
- **Semantic object classification** — database, table, view, macro, procedure, etc.
- **Schema drift signals** — baseline captures enable automatic out-of-band change detection between deployments
- **OpenTelemetry spans** — per-stage tracing for operations dashboards
- **OpenLineage events** — START/COMPLETE/FAIL events from deploy\_package with output datasets for data catalog integration
- **decisions.json audit trail** — append-only record of every pipeline run; queryable by agents for outcome and issue codes

---

## Governance and Auditability

One of the most critical — and most overlooked — requirements in enterprise database deployment is the ability to prove, after the fact, exactly what was deployed, from what source, at what time, and by whom. This is not a reporting convenience: in regulated industries it is a legal obligation.

SHIPS is designed from the ground up to satisfy that obligation, and to do so in a way that cannot be circumvented or obscured — whether the deployer is a human DBA, a CI pipeline, or an autonomous agent.

### Tamper-Evident Packages

Every SHIPS package carries a SHA-256 fingerprint computed over every file in the deployment payload before the package is archived. This fingerprint is stored in `context/ships.integrity.json` inside the package itself.

When the package is deployed — regardless of how long after it was built, and regardless of what transport mechanism carried it — the deployer recomputes the fingerprint and compares it to the stored value. **If any file has been added, removed, or modified, deployment is aborted before any database connection is opened.** No SQL reaches Teradata from a tampered package.

This means:
- A package cannot be silently modified in transit (across SFTP, shared drives, email, or manual extraction steps)
- A package cannot be partially applied or selectively tampered with
- An operator cannot alter a payload file after it has been packaged without breaking the fingerprint
- Individual file hashes in `context/ships.integrity.json` enable precise forensic identification of which file was changed, not just that *something* changed

If the integrity check is bypassed via the `--skip-integrity-check` flag, this is recorded at WARNING level in the deployment log **and** written into the Teradata DBQL query band — it cannot be used silently.

### Asymmetric Signing (Private / Public Key)

SHA-256 fingerprinting addresses chain-of-custody. The next layer — adversarial tamper resistance — is **asymmetric signing**.

Under the designed model, the packager signs the `package_hash` using a private key held outside the package directory (in a CI/CD secrets store or an HSM). The corresponding public key is distributed to deployment hosts. The deployer verifies the signature before executing anything. An attacker with full write access to the extracted package directory cannot forge a valid signature without the private key.

This is the correct control for environments where insider threat or supply-chain interference is part of the threat model, and where SOX or APRA controls require proof that the package executed is byte-for-byte identical to what the authorised build system produced.

The signing infrastructure is designed (see [ADR 0011](adr/0011-sha256-package-integrity-fingerprinting.md)) and will be activated when key management infrastructure is available. The SHA-256 fingerprint layer is the foundation it builds on.

### Permanent DBQL Audit Trail

Every SQL statement executed by SHIPS carries the package fingerprint in the Teradata session query band:

```
BUILD=0002;PKG=MortgagePlatform_SHIPS;ENV=PROD;PKG_HASH=a3f8c2d1e9b74056;DEPLOYER=database_package_deployer_v2;
```

Because DBQL records every executed statement against its session's query band, this creates a **permanent, queryable link between every DDL statement executed in the database and the exact package version that produced it**. An auditor can run a DBQL query and confirm: "all DDL executed in this deployment window came from build 0002 of package MortgagePlatform_SHIPS with fingerprint a3f8c2d1e9b74056." The log cannot be retroactively altered.

This satisfies the audit trail requirement at the database layer — not just in the application log.

### Package Trust Score

A SHIPS package is not simply pass/fail. It carries a **Trust Score** — a composite metric in the range 0–97% — computed at build time and embedded in the package manifest. The score aggregates six quality dimensions:

| Dimension | Weight | What it measures |
|---|---|---|
| Quality | 20% | DDL coding discipline compliance; naming conventions; token completeness |
| Safety | 20% | No destructive operations on unowned objects; no force-flag overrides |
| Completeness | 15% | All expected object types present; companion sidecars present; properties conformance |
| Isolation | 15% | Cross-database grant coverage; no unresolved tokens; no hardcoded database names |
| Verifiability | 15% | SHA-256 fingerprint present and valid; manifest internally consistent |
| Provenance | 15% | Build metadata complete: project, version, author, harvest timestamp, token map version |

The Trust Score is:
- Printed to the CLI at package build time with a per-dimension breakdown
- Embedded in `manifest.json` with contributing violations per dimension
- Displayed as a colour-coded gauge in the HTML deployment report (green ≥ 90%, amber 75–89%, red < 75%)
- Enforceable in CI/CD via `--min-trust-score N` — the pipeline fails if the package does not meet the threshold

The 97% ceiling is deliberate: SHIPS never claims a package is guaranteed to deploy successfully. Runtime conditions (privileges, space, lock availability) are outside static analysis scope. The Trust Score characterises the **static risk profile** of the package, not the outcome of deployment.

### Regulatory Compliance

SHIPS's governance model is designed to address the specific evidence requirements of the two regulatory regimes most commonly encountered in Teradata customer environments:

**Sarbanes-Oxley (SOX)**

SOX IT General Controls require that changes to systems that process financial data are authorised, tested, and implemented as approved. The evidence burden falls on three controls:

| SOX Control | SHIPS evidence |
|---|---|
| Change is authorised | Package provenance metadata (author, build timestamp, source ref) in `context/ships.build.json` |
| Change is what was approved | SHA-256 fingerprint / asymmetric signature proving the deployed payload matches the approved package |
| Change is traceable to a specific execution | `PKG_HASH` in every DBQL record permanently associates the executed DDL with the package version |

**APRA (Australian Prudential Regulation Authority)**

APRA's operational risk framework (CPS 230) and information security standard (CPS 234) require that regulated entities maintain change management controls, audit logs, and the ability to reconstruct the state of material systems. SHIPS addresses these through:

| APRA requirement | SHIPS evidence |
|---|---|
| Change management — authorised change only | Trust Score and `--min-trust-score` CI gate enforce quality threshold before any deployment |
| Audit log — who changed what and when | `context/ships.build.json` provenance + DBQL query band record |
| Integrity — deployed asset matches approved asset | SHA-256 fingerprint blocks deployment of any tampered package |
| Recovery — ability to restore prior state | Pre-deployment SHOW captures in the rollback directory enable point-in-time reversion |

### What This Means for Agentic Deployments

Governance controls are not optional extras that get relaxed for autonomous agents — they become *more* important. A human DBA can exercise judgement when something looks wrong. An agent cannot. The package itself must carry enough integrity assurance that the agent can trust its inputs without human oversight.

SHIPS's governance layer means that an autonomous agent receiving a SHIPS package can:
- Verify the package has not been tampered with (fingerprint)
- Confirm the package meets organisational quality standards (Trust Score ≥ threshold)
- Trace every executed statement back to the approved build (DBQL query band)
- Present a complete audit record to a human reviewer after the fact

This is what "governed autonomous deployment" looks like in practice.

---

## The Bigger Picture

Traditional deployment packaging standards were designed for human-operated pipelines. Metadata was a courtesy. Dependency ordering was implicit. Package structure was a convention, not a contract.

SHIPS takes a different position: **packaging artefacts are the contract between the builder and the deployer** — whether that deployer is a DBA, a CI runner, or an autonomous agent.

As Teradata customers and Teradata associates alike move toward agent-assisted and fully autonomous delivery models, the quality of that contract becomes the foundation everything else rests on. An agent that cannot trust its inputs cannot make safe decisions. A package that omits dependencies cannot be safely ordered. A manifest that obscures intent cannot be acted on without human review.

SHIPS is built to be that foundation.

By getting the package model right — atomic, self-describing, machine-interpretable, and deterministic — SHIPS reduces the friction of adopting autonomous agents for Teradata environments. It lowers the bar for customers exploring agentic delivery, gives Teradata associates a production-grade reference implementation to build on, and establishes a packaging standard that can evolve with the agentic ecosystem rather than against it.

---

## Design Principles

| Principle | What it means in practice |
|---|---|
| **Metadata first** | Every object carries enough context to be acted on without external knowledge |
| **Agent-native** | Machine interpretability is a requirement, not an afterthought |
| **Human-friendly** | The CLI, reports, and package layout are readable by people too |
| **Trust by construction** | Packages are validated, fingerprinted, and scored before they leave the build step — tamper detection is automatic, not optional |
| **Governed by default** | Every deployment produces a permanent, unforgeable audit trail in Teradata DBQL; regulatory evidence is a by-product of normal operation, not a separate reporting step |
| **Open at the seams** | Outputs (graphs, manifests, reports) use open formats for downstream integration |
| **No hidden knowledge** | Dependencies, ordering, intent, and constraints are explicit, not implied |

---

## Audience

SHIPS is designed for:

- **Teradata customers** adopting modern DevOps or agentic delivery for their data platforms
- **Teradata associates** building, advising on, or demonstrating agentic Teradata workflows
- **AI agents** operating autonomously within governed database delivery pipelines
- **DBAs** who need trustworthy, self-describing packages they can deploy with confidence
- **Release engineers** who need deterministic, auditable artefacts for compliance and change management
