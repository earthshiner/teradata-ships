# ADR 0014: Deploy-time Trust Resolution for Environment Prerequisite Signal

## Status

Accepted | 2026-05-18

## Context

SHIPS auto-generates a `_00_environment_prereqs` package whenever it detects
that a prerequisite package's `CREATE DATABASE` / `CREATE USER` statements
depend on a parent object that is not itself created by the package. Because
SHIPS cannot know whether that parent already exists in the target environment,
it stamps the environment prereqs package with:

```
Trust label:  BLOCKED
Signal:       environment_prereq_requires_dba_review  (fail)
Issues:       [list of parent object names]
```

This is correct behaviour at build time: the packager has no database
connection and therefore no way to verify existence.

The problem arises on subsequent deployments to an environment where those
parent databases were created during an earlier release cycle. The objects
exist, the `_00_environment_prereqs` package would be a no-op (all its DDL
uses `SKIP_IF_EXISTS` semantics), but the deploy-time BLOCKED check still
hard-exits before making a connection. The DBA must either:

- use `--skip-trust-check` (a development-only escape hatch that carries no
  audit trail), or
- manually repackage the prereqs package after removing the now-unnecessary
  DBA placeholders (process friction with no safety benefit).

Neither option is acceptable for a production deployment workflow. The first
bypasses the trust gate entirely; the second adds manual steps to a path that
should be automatic.

The root cause is that `environment_prereq_requires_dba_review` is a
*conditional* signal: it is `fail` when the objects are absent and implicitly
`pass` when the objects are present. All other BLOCKED signals are
*unconditional*: the failure is a property of the package itself (bad DDL,
missing provenance, inspect violations) and cannot be resolved by querying a
database.

## Decision

`deploy.py` will handle `environment_prereq_requires_dba_review` differently
from all other BLOCKED signals:

1. **Detect the deferrable case.** At the trust banner check (before the
   connection is opened), if the package is BLOCKED and the *only* failing
   signal is `environment_prereq_requires_dba_review`, the hard exit is
   deferred. The object names listed under `issues` are captured for
   verification.

2. **Establish the connection.** The deployer connects to the database as
   normal. If the connection fails, deployment cannot proceed regardless.

3. **Verify existence immediately after connect.** For each object name in
   the captured list, query `DBC.DatabasesV` and `DBC.UsersV`. Log the
   outcome for every object individually (`✓ VERIFIED` or `✗ MISSING`).

4. **Branch on outcome:**
   - **All objects verified present** → log a prominent `TRUST RESOLVED`
     banner at INFO level explaining that the build-time BLOCKED signal has
     been satisfied by live verification. Deployment proceeds normally.
   - **Any object missing** → log a hard error listing the missing objects
     and exit non-zero. The DBA must deploy the `_00_environment_prereqs`
     package first.

5. **Mixed BLOCKED signals.** When `environment_prereq_requires_dba_review`
   is failing alongside other signals, the existing hard exit fires
   immediately. The deferral path is only taken when this is the *sole*
   failing signal — any other co-blocking signal implies the package has
   additional problems that cannot be resolved by a live check.

6. **Dry-run behaviour.** In `--dry-run` mode there is no connection, so
   the deferral path is not taken. The deployer logs the BLOCKED label and
   exits as before.

## Consequences

**Positive**

- Eliminates friction for the common production deployment pattern: a
  long-lived environment where parent databases were created once and remain
  in place across many release cycles.
- The trust gate is not weakened: deployment is authorised only when every
  listed object is positively confirmed to exist in the target.
- The resolution event is logged prominently and explicitly, providing a
  full audit trail of why a BLOCKED package was allowed to proceed.
- `--skip-trust-check` is no longer needed (or appropriate) for this
  scenario.

**Negative**

- The deployer now performs a database query as part of its trust check,
  coupling the trust gate to the live database. This is unavoidable given
  that the signal itself is about a live environment state.
- If `DBC.DatabasesV` or `DBC.UsersV` are inaccessible to the deploying
  user, the verification query will fail and the deployment will be blocked.
  This is the correct behaviour: if the deployer cannot verify, it must not
  proceed.

**Neutral**

- The `ships.build.json` trust block is not modified. The build-time record
  remains as stamped; resolution is a deploy-time event recorded in the
  deploy log only.
- The `--skip-trust-check` flag continues to exist as a development escape
  hatch for other BLOCKED signals. It is not the appropriate mechanism for
  this scenario and the documentation is updated to reflect that distinction.

## Alternatives considered

**Offline evidence file.** A DBA drops a `context/prereqs_satisfied.json`
file asserting that the listed objects exist, and `deploy.py` checks for that
file instead of querying the database. Rejected: this adds a manual step with
no safety advantage over a live query. The file could be stale or fabricated.
A live query is cheaper, faster, and more reliable.

**Repackage after DBA confirmation.** The existing workflow: DBA edits the
generated `.db` files, removes placeholders, and runs `ships repackage
--strict`. Continues to be valid when objects genuinely do not exist and must
be created. Not valid when objects already exist — the repackage produces DDL
that will be silently skipped at deploy time anyway.

**Downgrade the signal to warn at build time.** Rejected: at build time SHIPS
has no connection. Emitting a warning rather than a fail for a missing parent
would allow packages to deploy against environments where the parent truly
does not exist. The build-time fail is correct; the deploy-time resolution is
the appropriate mitigation.

## References

- `td_release_packager/builder.py` — trust banner and deferred verification
  logic in `_generate_deploy_script()`.
- `td_release_packager/environment_prereqs.py` — signal and issues list
  construction.
- ADR 0012: Package Trust Score Design — trust label semantics.
- ADR 0002: SHIPS pipeline phase structure — environment prereqs as Phase 2
  of the intra-package dependency trilogy.
