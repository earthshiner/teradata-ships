# ADR 0002: SHIPS Pipeline Phase Structure

## Status

Accepted | 2026-01-15

## Context

The SHIPS project needed a top-level mental model for how raw DDL
source becomes a deployed, auditable, environment-promoted artefact
on a Teradata Vantage system. Early prototyping revealed a set of
recurring failure modes:

- DDL checked into source contained hard-coded database names that
  could not be promoted across DEV → SIT → UAT → PROD without
  manual find-and-replace operations that introduced errors.
- There was no clear boundary between "what the developer wrote"
  and "what the deployer executes." This made debugging ambiguous:
  a runtime failure could be traced to the source file, the
  tokenisation substitution, or the deployment execution.
- Re-running a deployment after a partial failure was unsafe:
  no checkpoint mechanism existed, and re-executing everything
  from scratch risked double-applying grants or dropping objects
  that had been successfully deployed in the failed run.
- Validation was done at deployment time, meaning errors surfaced
  only after the package was in motion. Pre-flight checks needed
  to be separated from deployment execution.
- There was no consistent packaging format. Different operators
  produced different directory layouts, making automation difficult
  and reviews inconsistent.

A pipeline with clearly bounded, independently re-runnable phases
was identified as the solution. The phase structure needed to
satisfy three properties:

1. **Separation of concerns.** Each phase has one responsibility.
   A failure in any phase is diagnosable without understanding
   the others.
2. **Re-entrancy.** Every phase must be safe to re-run against
   the same inputs without producing different outputs or causing
   unintended side effects.
3. **Auditability.** Each phase produces a visible artefact (a
   directory tree, a manifest, a report) that can be reviewed
   and committed independently.

Several phase-count options were considered: three phases (build,
validate, deploy), four phases, and the selected five. A three-phase
model collapsed concerns — validation of source content and
validation of package integrity are distinct problems and should
be separable. A four-phase model that combined Harvest and Inspect
lost the property that inspected content is always derived from
an already-tokenised payload, not from raw source.

The name SHIPS was chosen after the phases were finalised. It is
a mnemonic, not a backronym: the phases were named for their
function first, and the acronym emerged.

## Decision

The SHIPS pipeline consists of exactly five sequential phases,
each with a single responsibility and a defined input/output
contract:

1. **Scaffold.** Creates the project directory structure for a
   named data product and a set of target environments. Scaffold
   is idempotent: re-running it against an existing project
   directory is safe. Output: `{project}/config/`,
   `{project}/source/`, `{project}/payload/`, `{project}/releases/`
   skeleton with `.gitkeep` files and a starter `ships.yaml`.

2. **Harvest (Ingest).** Reads DDL source files from a nominated
   source directory, applies the token map to substitute
   `{{TOKEN}}` placeholders with environment-specific values,
   and writes the tokenised payload to `{project}/payload/`. All
   content visible at this stage is what will actually execute
   at Ship time. Output: fully tokenised file tree under
   `payload/`.

3. **Inspect (Validate).** Reads the tokenised payload — never
   the raw source — and applies all Discipline rules: token
   completeness, deploy-intent verb checking, grant correctness,
   object naming, properties file conformance, and structural
   integrity. Emits a structured report and, on violation, exits
   non-zero. Output: `inspect_report.json` and a human-readable
   HTML summary.

4. **Package.** Assembles the inspected payload into a
   self-contained, versioned release artefact: a ZIP archive
   containing the tokenised DDL files, the deploy manifest
   (with wave ordering and per-object intent), a SHA-256 sidecar
   for integrity verification, and the Package Trust Score.
   Output: `{project}/releases/{name}-{env}-{version}.zip` plus
   `{name}-{env}-{version}.sha256`.

5. **Ship (Deploy).** Reads the release artefact produced by
   Package and executes it against a live Teradata system in
   wave order: databases first, DCL grants second, DDL objects
   third. Per-object snapshots are captured before execution for
   rollback. Output: a deploy log and updated manifest with
   execution status per object.

Phases are invoked via the CLI as subcommands: `ships scaffold`,
`ships harvest`, `ships inspect`, `ships package`, `ships ship`
(or `td_release_packager <subcommand>` / `ddl_deployer deploy`
for their respective packages).

No phase is permitted to write to the artefact produced by a
prior phase, except by re-running that phase. Harvest does not
modify source; Inspect does not modify the payload; Package does
not modify the payload or the inspect report; Ship does not modify
the package archive.

## Consequences

**Positive**

- The five-phase boundary provides a natural checkpoint after each
  phase. A developer can Harvest, review the tokenised payload,
  and only proceed to Inspect once satisfied. This dramatically
  narrows the surface area of each debugging session.
- Because each phase reads from the prior phase's output (not
  from source), a failed Ship does not require re-running Harvest
  or Inspect. The package is re-deployable as-is.
- The phase names are comprehensible to non-developers. A
  stakeholder can understand "we harvested the source, inspected
  it, packaged it, and shipped it" without understanding the
  internals of any phase.
- CI/CD integration maps naturally: Harvest + Inspect + Package
  run in the pipeline on every commit; Ship is gated to an
  approved merge.

**Negative**

- Five distinct CLI invocations is more ceremony than a single
  `deploy --source . --target dev` command. Operators familiar
  with simpler tools may find the pipeline verbose for small
  changes. Mitigation: a `ships run-all` convenience command
  can chain all five phases.
- The strict no-cross-phase-write rule means that fixing a token
  value after Harvest requires re-running Harvest and all
  subsequent phases. This is correct behaviour, but it surprises
  operators who expect to edit a payload file and re-run only
  Inspect.
- Five phase boundaries create five points at which a pipeline
  can fail. This is intentional — fail fast at the earliest
  possible phase — but increases the number of failure messages
  a new operator must learn to interpret.

**Neutral**

- The phase count is fixed. Future enhancements (e.g. a
  Plan phase that produces a human-readable diff of what Ship
  will do without executing it) can be added as additional
  subcommands without reordering or renaming existing phases.
- Harvest is named "Ingest" in some internal documents from the
  early design period. "Harvest" was chosen as the canonical
  name because it better captures directionality: the phase
  gathers from an external source and brings content into the
  project. "Ingest" is acceptable as a synonym in documentation
  but not in CLI surface or code.

## Alternatives considered

**Three phases: Build, Validate, Deploy.** Rejected: "Build"
combined Scaffold, Harvest, and aspects of Inspect in a way that
made re-entrancy impossible. A failed Build left the project in
an indeterminate state. Splitting Validate from Build also proved
insufficient because source validation (is the DDL well-formed?)
and payload validation (is the tokenised output consistent?) are
distinct concerns requiring separate checkpoints.

**Continuous deployment: a single `deploy` command that does
everything.** Rejected: this is the prior state that motivated
the project. Without visible intermediate artefacts, debugging a
production deployment failure is a forensic exercise. The pipeline
exists precisely to prevent this.

**Six phases: adding an explicit Rollback phase.** Considered.
Rejected: rollback is a capability of Ship, not a separate phase.
Making it a phase would imply that every successful Ship must be
followed by a Rollback decision, which is operationally incorrect.
Rollback remains a Ship subcommand (`ships ship --rollback`).

## References

- `td_release_packager/cli.py` — top-level CLI dispatcher for
  phases 1–4.
- `ddl_deployer/cli.py` — CLI dispatcher for phase 5 (Ship).
- `td_release_packager/scaffolder.py` — Scaffold phase
  implementation.
- `td_release_packager/ingest.py` — Harvest phase implementation.
- `td_release_packager/validate.py` — Inspect phase implementation.
- `td_release_packager/builder.py` — Package phase implementation.
- `ddl_deployer/deployer.py` — Ship phase implementation.
- ADR 0005: Wave ordering for deployment — governs the execution
  sequence within the Ship phase.
- ADR 0006: Deployer owns idempotency — governs the per-object
  execution strategy within the Ship phase.
