# ADR 0006: Deployer Owns Idempotency

## Status

Accepted | 2026-03-05

## Context

Idempotent deployment — the ability to re-run a deployment safely
against a target that already has some or all of the objects
present — is a fundamental requirement for enterprise DDL
automation. Deployments fail. Networks time out. Operators
re-run pipelines. A deployment tool that cannot recover from
partial failure without manual state inspection is not production-
grade.

Two architectural positions on idempotency are available:

**Position A: Source is idempotent.**
Each DDL file uses a verb that is safe regardless of whether
the object already exists. For Teradata views, procedures,
macros, and functions, this is `REPLACE`, which creates the
object if absent or replaces it if present. For tables, join
indexes, and triggers — where Teradata has no `REPLACE`
equivalent — the DDL file must include an existence check
(e.g. a conditional macro or a `DROP TABLE IF EXISTS` pattern)
before the `CREATE`.

**Position B: The deployer is idempotent.**
DDL files use the simplest declarative verb (`CREATE`). The
deployer is responsible for making each execution idempotent:
it checks whether the object exists on the target before
executing, captures a pre-flight snapshot of the existing
object's DDL, and selects an appropriate execution strategy
(`DROP_AND_CREATE` or `REPLACE_IN_PLACE`) per object type.

Position A was in common use in the project's predecessor
scripts. It was rejected for the following reasons:

1. **Source complexity.** Existence-check patterns in DDL source
   are verbose and fragile. A table existence check requires
   querying `DBC.TablesV`, capturing the result, and
   conditionally executing a `DROP`. This logic belongs in the
   deployment tool, not in the DDL file. A DDL file should be a
   declaration of what the object is, not a script for managing
   its lifecycle.

2. **Non-uniform verb usage.** With Position A, some files use
   `REPLACE` (views, procedures) and some require custom
   existence-check wrappers (tables, join indexes). This is
   inconsistent. A developer reading two files of different
   object types sees different patterns for the same problem.

3. **Source ≠ definition.** A `REPLACE VIEW` statement says
   "modify this existing view to match this definition." In
   source, the intended statement is "this view's definition is
   X." The deployment action (create if absent, replace if
   present) is the deployer's concern, not the file's concern.

4. **Snapshot timing.** When the deployer executes a `REPLACE`
   statement, it still captures the pre-execution state of the
   object via `SHOW` before issuing the `REPLACE`. The
   `REPLACE_IN_PLACE` strategy (used for views, procedures, macros,
   functions) follows the same snapshot-first sequence as
   `DROP_AND_CREATE`: read the existing DDL, store it in
   `_rollback/`, then execute. Rollback coverage is therefore
   equivalent for both verbs. The advantage of `CREATE`-in-source
   is uniformity and auditability of intent — not a superior
   rollback path.

## Decision

**Position B: The deployer owns idempotency.** DDL source files
prefer `CREATE`. The deployer is responsible for making
execution idempotent on the target environment. `REPLACE` is
also permitted (the deployer handles it safely via the
`REPLACE_IN_PLACE` strategy with pre-flight snapshot), but
`CREATE` is the opinionated convention and the Inspect
`deploy_intent` rule surfaces `REPLACE` usage as an advisory
WARNING. Projects may raise this to ERROR via `inspect.conf` if
they wish to enforce strict `CREATE`-only. This is implemented
as follows:

1. **Source prefers `CREATE`.** `REPLACE` in a DDL source file
   triggers an advisory WARNING at Inspect time (rule
   `deploy_intent` — see ADR 0009 for the full rule history).
   `REPLACE` is not blocked: the deployer handles it safely.
   The WARNING nudges new development toward `CREATE` without
   forcing mass remediation of existing codebases. Projects that
   wish to enforce strict `CREATE`-only may set
   `deploy_intent=ERROR` in `inspect.conf`.

2. **Strategy map by object type.** The deployer maintains a
   `STRATEGY_MAP` that assigns an execution strategy to each
   object type:

   | Strategy | Object types |
   |----------|-------------|
   | `REPLACE_IN_PLACE` | View, macro, procedure, function, STO |
   | `DROP_AND_CREATE`  | Table, join index, trigger |

   `REPLACE_IN_PLACE` executes `REPLACE {type} {name} ...`
   against the target (the Harvest-time rewrite of `CREATE` →
   `REPLACE` in the staged payload ensures the correct verb is
   present — see ADR 0009, decision 5).

   `DROP_AND_CREATE` drops the object if it exists, then
   executes the `CREATE` statement from the staged payload.
   The drop is preceded by a snapshot (see item 3).

3. **Pre-flight snapshot.** Before executing any strategy
   against an existing object, the deployer captures the
   existing object's DDL via `SHOW {type} {name}` and stores it
   in the deploy manifest. If the deployment fails after
   execution has begun, the snapshot provides a mechanically
   derived rollback path.

4. **Error 3598 / 2631 retry logic.** Teradata may return
   Error 3598 (deadlock) or Error 2631 (object locked) during
   DDL execution, particularly for objects with cross-database
   dependents. The deployer retries these errors with
   exponential backoff (3 retries, 2 / 4 / 8 seconds) before
   treating them as fatal. This retry is part of the deployer's
   idempotency contract: a transient lock does not leave the
   deployment in a failed state if a brief wait resolves it.

5. **Checkpoint-based resume.** The deploy manifest tracks
   execution status per object: `PENDING`, `SNAPSHOTTED`,
   `EXECUTED`, `FAILED`. A re-run of `ships ship` against a
   partially-executed manifest skips objects with status
   `EXECUTED` and retries from the first `FAILED` or `PENDING`
   object in wave order. Re-running from scratch is also
   supported via `--force` (resets all statuses to `PENDING`).

6. **`tmode=TERA` for all connections.** The deployer connects
   in Teradata (TERA) mode, not ANSI mode. ANSI mode causes
   `GRANT` statements to generate Error 3932 ("GRANT cannot be
   used in a multi-statement transaction") because ANSI mode
   wraps every statement in an implicit transaction. In TERA
   mode, `GRANT` executes with its natural commit semantics.
   See ADR 0009 for additional context on session mode.

## Consequences

**Positive**

- Source files are uniformly simple: one `CREATE` per file,
  no existence-check boilerplate. A developer writing a new
  view types `CREATE VIEW {db}.{name} AS ...` — nothing more.
- The deployer's strategy choice (REPLACE vs DROP+CREATE) is
  uniform per object type across the entire project. No per-file
  judgement required.
- Pre-flight snapshots provide a rollback path that is
  mechanically derived rather than manually maintained. No
  separate "backup before deploy" step is required.
- Checkpoint-based resume means a deployment failure due to
  a transient database issue is recoverable without full
  re-execution.

**Negative**

- The deployer must connect to the target environment at
  Package time to capture `STRATEGY_MAP` type information in
  the manifest. In practice the manifest is computed from
  file extensions (ADR 0004), which are known without a live
  connection — but strategy assignment must be verified at
  Ship time.
- `DROP_AND_CREATE` for tables is destructive if the table
  contains data and the drop-before-create is unintended.
  For table DDL in a data product context, this is correct
  (re-deploying a table definition should not imply silently
  preserving stale data), but the operator must be aware.
  The pre-flight snapshot captures the table's DDL (not its
  data) for structure rollback only.
- The three-retry / exponential-backoff logic adds complexity
  to the deployer. This is bounded complexity (a single retry
  helper function) and is justified by the production failure
  mode it addresses.

**Neutral**

- The decision to use `CREATE` in source and rewrite to
  `REPLACE` in the staged payload for `REPLACE_IN_PLACE` types
  (ADR 0009, decision 5) is a consequence of this ADR. If the
  deployer owns idempotency, the deployer (via Harvest) is the
  correct place for the `CREATE` → `REPLACE` transformation —
  not the source file.
- This ADR establishes the principle. ADR 0009 operationalises
  the `deploy_intent` Discipline rule that enforces the
  `CREATE`-in-source convention and provides the configurable
  relaxation path for projects with `REPLACE` muscle memory.

## Alternatives considered

**Position A: source is idempotent.** Rejected as described in
Context. The core objection is architectural: source files are
declarations of object definitions, not deployment scripts.
Embedding deployment logic (existence checks, conditional drops)
in source conflates the two concerns.

**Hybrid: `REPLACE` for replaceable types in source, deployer
handles tables.** Rejected: this is an inconsistent middle
ground. Developers must remember which types use which verb,
defeating the "always write `CREATE`" mental model. The Harvest-
time rewrite (ADR 0009) achieves the same technical outcome with
a uniform source convention.

**`CREATE OR REPLACE` syntax.** Rejected: Teradata does not
support `CREATE OR REPLACE` for any DDL type in TERA mode. The
equivalent for replaceable types is `REPLACE`; for non-replaceable
types there is no equivalent. The deployer must handle both cases
anyway.

**Optimistic execution without snapshot.** Considered for
performance: skip the `SHOW` pre-flight and capture only on
failure. Rejected: on failure, the pre-execution state may be
inaccessible (object dropped, session closed). A snapshot on
failure is too late. Pre-flight snapshot is mandatory.

## References

- `database_package_deployer/deployer.py` — `_deploy_replace_in_place`
  (line 1910), `_deploy_drop_and_create` (line 1821), retry
  logic, and checkpoint-based resume.
- `database_package_deployer/models.py` — `STRATEGY_MAP` (lines 131–144).
- `database_package_deployer/cli.py` — `tmode='TERA'` connection parameter.
- ADR 0002: SHIPS pipeline phase structure — Ship phase owns
  the deployer execution.
- ADR 0004: Atomic eponymous DDL files — one object per file
  is a prerequisite for per-object strategy dispatch.
- ADR 0007: Package-level rollback via pre-flight snapshot —
  the snapshot mechanism referenced in item 3 of this ADR.
- ADR 0009: Configurable `deploy_intent` rule with audit waiver
  — operationalises the `CREATE`-in-source principle and adds
  the harvest-time rewrite for `REPLACE_IN_PLACE` types.
