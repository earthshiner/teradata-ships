# ADR 0007: Package-Level Rollback via Pre-Flight Snapshot

## Status

Accepted | 2026-03-19

## Context

Enterprise DDL deployments that fail mid-execution leave the
target environment in a partially-deployed state. The failure
may be due to a syntax error in a DDL file, a privilege
insufficiency, a lock contention event, or a transient network
error. In all cases, the operator faces the same question: "what
was the state of the environment before this deployment began,
and how do I restore it?"

Without a pre-deployment capture mechanism, the answer requires:

1. Querying `DBC.TablesV`, `DBC.ColumnsV`, and related system
   views to reconstruct the pre-deployment object definitions.
2. Running `SHOW TABLE`, `SHOW VIEW`, etc. for each affected
   object to obtain DDL.
3. Assembling a rollback script manually.

This is an error-prone process. At the time of failure, the
environment may be in a state where some objects have been
dropped (and their DDL is therefore no longer accessible via
`SHOW`) and some have been replaced. The window for capturing
a rollback is the narrow period between object drop and object
re-create — which may have already passed.

Three rollback strategies were considered:

**Strategy 1: Source-controlled rollback.** The prior version's
source DDL is available in version control. Rollback means
re-deploying the prior release. This is correct for intentional
rollback (a post-deployment decision to revert), but slow and
operationally complex for emergency rollback during a failed
deployment.

**Strategy 2: Target environment query at rollback time.**
On failure, query the target environment for current DDL of
affected objects. Unreliable: some objects may have been dropped
already; some may be in an inconsistent state.

**Strategy 3: Pre-flight snapshot per object.** Before executing
any destructive or replacement operation against an existing
object, capture the object's current DDL via `SHOW`. Store the
snapshot in the deploy manifest alongside the object's status.
On rollback, replay the captured DDL.

Strategy 3 was chosen. It requires that the snapshot be captured
before any mutation of the object — which in practice means it
must be the first action taken for any object that already exists
on the target.

A secondary question was the scope of snapshot capture. Two
options:

- **Selective capture:** snapshot only objects that will be
  dropped (i.e. `DROP_AND_CREATE` strategy objects — tables,
  join indexes, triggers).
- **Universal capture:** snapshot all objects that already exist
  on the target, regardless of strategy.

Universal capture was chosen. A `REPLACE_IN_PLACE` operation
on a view can produce an invalid view definition if the
replacement DDL has an error. Capturing the prior view DDL
allows that failure to be rolled back with the same mechanism
used for tables.

## Decision

SHIPS implements package-level rollback via pre-flight snapshot:

1. **Snapshot before mutation.** For every object in the deploy
   manifest that the Ship phase will act upon, the deployer
   first checks whether the object exists on the target
   (`SELECT 1 FROM DBC.TablesV WHERE DatabaseName = ?
   AND TableName = ?`). If the object exists, the deployer
   executes `SHOW {type} {database}.{name}` and stores the
   returned DDL in the manifest entry for that object under the
   key `snapshot_ddl`. The object's status advances from
   `PENDING` to `SNAPSHOTTED`.

2. **Snapshot scope.** All object types with a deployer strategy
   are eligible for snapshot: tables (`.tbl`), views (`.viw`),
   procedures (`.spl`), macros (`.mcr`), join indexes (`.jix`),
   triggers (`.trg`), STOs (`.sto`), and UDFs (`.fun`). Database
   creation (Wave 1) and grant execution (Wave 2) are not
   snapshotted — `SHOW DATABASE` is not a supported Teradata
   command, and the rollback for a grant is an explicit `REVOKE`,
   not a DDL replay. Grant rollback is handled separately (see
   item 5).

3. **Snapshot storage.** The snapshot DDL is stored in the
   manifest JSON file (`manifest.json`) within the release
   archive. The manifest is updated in-place during Ship
   execution; it is the live state file for the deployment.
   The manifest is not re-packaged into the archive during
   Ship — it exists as an extracted working file in the
   deployment run directory.

4. **Rollback execution.** On failure, `ships ship --rollback`
   reads the manifest, identifies all objects with status
   `SNAPSHOTTED` or `EXECUTED` (objects that were acted upon
   and may need reverting), and replays their `snapshot_ddl`
   in reverse wave order. Objects with status `PENDING`
   (never acted upon) require no rollback action.

5. **Grant rollback.** Grants do not have DDL snapshots.
   If a Wave 2 failure requires grant rollback, the operator
   must run the corresponding `REVOKE` statements manually.
   The deploy log records every `GRANT` that was executed;
   the operator can derive the corresponding `REVOKE` from
   the log. A future SHIPS version may automate this via
   a `--rollback-dcl` flag.

6. **SHA-256 integrity of the release archive.** The release
   archive produced by Package includes a `.sha256` sidecar.
   Ship verifies the archive's integrity before extraction.
   This ensures that the DDL being deployed is exactly what
   was packaged — no bit-flip, no accidental overwrite during
   transit.

7. **Snapshot is informational, not prescriptive.** The
   presence of a snapshot in the manifest does not initiate
   rollback automatically on failure. Rollback is an explicit
   operator action (`ships ship --rollback`). Automatic rollback
   on failure was considered and rejected (see Alternatives).

## Consequences

**Positive**

- A rollback path exists for every object that was snapshotted,
  regardless of whether the failure occurred before or after
  the object was mutated. The snapshot is captured before any
  mutation; the post-failure state of the target environment
  is irrelevant.
- The manifest is the audit record for the deployment. Every
  object touched by Ship has a status, a strategy, a snapshot
  (if applicable), and a timestamp. Post-deployment review is
  a manifest read, not a target-environment query.
- SHA-256 integrity verification closes the gap between package
  construction and package execution. An archive that has been
  modified since packaging is detected before any DDL executes.

**Negative**

- Snapshot capture adds one `SHOW` query per existing object to
  the Ship phase's execution time. For a module with 200 objects
  that all already exist on the target (a re-deployment
  scenario), this is 200 additional round-trips. On typical
  Teradata latency (~5ms per query), this adds approximately
  one second. Acceptable.
- Grant rollback is manual (item 5). This is a known gap. The
  `--rollback-dcl` automation is deferred work.
- `SHOW` DDL output format varies by Teradata version and
  configuration. On some systems, `SHOW VIEW` includes the
  `REPLACE VIEW` verb; on others it uses `CREATE RECURSIVE VIEW`
  for recursive views. The snapshot DDL must be replayed as
  captured — the deployer does not normalise the verb.

**Neutral**

- The snapshot mechanism makes ADR 0009's argument that "`REPLACE`
  and `DROP+CREATE` provide equivalent recovery guarantees" true
  in practice: both strategies produce a snapshot before
  execution, and both strategies use the snapshot for rollback.
  The recovery path is identical regardless of strategy.
- The `.sha256` sidecar convention is compatible with standard
  tooling (`sha256sum --check`). Operators can verify archive
  integrity independently of SHIPS using standard OS utilities.

## Alternatives considered

**Automatic rollback on failure.** Considered: on any fatal
error during Ship, automatically replay all snapshots and exit
with a "rolled back" status. Rejected: automatic rollback on
failure is dangerous. If the failure occurs because of a
privilege insufficiency, the rollback may also fail for the
same reason. If the failure is transient (network timeout),
the objects may have been successfully deployed, and automatic
rollback would undo a successful deployment. Rollback must be
a conscious operator decision.

**Full environment backup before deployment.** Rejected: backing
up a full Teradata environment before each deployment is not
practical. The snapshot approach captures only the objects that
will be touched by this deployment, which is the minimal set
required for rollback.

**Transaction-based rollback (BT/ET).** Considered: wrapping
each object deployment in a Teradata `BT` / `ET` pair. Rejected:
DDL in Teradata is not fully transactional. `CREATE TABLE`
and `DROP TABLE` cause implicit commits in TERA mode. A `BT` /
`ET` wrapper around DDL provides no rollback guarantee. Only
pure DML operations within a session can be reliably rolled
back via `BT` / `ET`.

**Delta-only deployment (deploy only changed objects).** Partially
considered as a rollback optimisation: if a release only modifies
10 of 200 objects, snapshot only those 10. This reduces snapshot
overhead but requires the Ship phase to know which objects have
changed — which requires either a diff against the target
environment or a manifest diff against a prior release. Deferred:
this optimisation is a future enhancement once the baseline
snapshot mechanism is stable.

## References

- `database_package_deployer/deployer.py` — snapshot capture logic,
  `--rollback` flag implementation, wave-reverse replay.
- `database_package_deployer/models.py` — `DeployManifest` schema including
  `snapshot_ddl` field per object entry.
- `td_release_packager/builder.py` — SHA-256 sidecar generation.
- `database_package_deployer/cli.py` — `--rollback` and `--force` flags.
- ADR 0005: Wave ordering for deployment — rollback replays in
  reverse wave order.
- ADR 0006: Deployer owns idempotency — the pre-flight snapshot
  is the mechanism that makes the deployer's idempotency
  recoverable, not merely re-runnable.
