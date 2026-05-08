# ADR 0005: Wave Ordering for Deployment

## Status

Accepted | 2026-02-19

## Context

A data product deployment on Teradata involves three distinct
categories of DDL/DCL operation, each with dependency relationships
that are not visible within any single file:

1. **Database creation.** `CREATE DATABASE` / `CREATE USER`
   statements that provision the containers in which all other
   objects will reside. No other object can be created before its
   database exists.

2. **Grant statements.** DCL operations that establish access
   rights between databases. A `GRANT SELECT ON {A} TO {B}` can
   only succeed after both `{A}` and `{B}` exist as databases.
   Cross-database grants — particularly the inter-database grants
   used to give view databases SELECT access to their backing
   table databases — must precede view creation, because Teradata
   validates privilege coverage at `REPLACE VIEW` / `CREATE VIEW`
   execution time in some configurations.

3. **DDL objects.** Tables, views, procedures, macros, join
   indexes, triggers. Views that reference objects in other
   databases require that those objects already exist and that
   the view database has SELECT on the source database.

Without enforced execution ordering, a naive sequential execution
of a tokenised payload can fail in any of the following ways:

- A `CREATE TABLE` executes before its containing `CREATE
  DATABASE`, producing "database does not exist."
- A `REPLACE VIEW` executes before the inter-database `GRANT
  SELECT` that authorises cross-database reference, producing
  a privilege error.
- A join index is created before one of its base tables,
  producing "table does not exist."
- A stored procedure referencing a view is compiled before
  the view exists, producing a compilation-time object
  resolution error.

Prior to SHIPS, these ordering requirements were handled by
operator knowledge: the developer running the deployment knew
to execute database scripts first, then grants, then objects,
in an order they had mentally computed. This is not scalable,
not auditable, and not reproducible across operators.

The wave model needed to:

- Guarantee correct execution order for the three dependency tiers
  (databases, grants, objects) without requiring operator
  knowledge.
- Be computable from the payload structure alone (file extensions,
  directory names) without parsing DDL content or querying the
  target environment.
- Allow parallelism within a wave where dependencies between
  objects in the same wave are absent (e.g., independent tables
  in the same database can be created in parallel).
- Be explicit in the deploy manifest so the operator can review
  the planned execution order before Ship runs.

## Decision

SHIPS deploys in exactly three ordered waves. All objects in
wave N must complete successfully before wave N+1 begins:

**Wave 1 — Databases.**
All `CREATE DATABASE` and `CREATE USER` statements. These are
identified by file extension `.db` and `.usr` (or by the
`DATABASES` section of the manifest). Wave 1 executes
sequentially within itself: if database B is subordinate to
database A (i.e. its `PERM SPACE` is allocated from A), A must
be created before B. The manifest records the parent–child
relationship derived from the `FROM` clause of each `CREATE
DATABASE` statement.

**Wave 2 — Grants (DCL).**
All `GRANT` statements from the `DCL/` subdirectory of the
payload. This includes role-level grants (`DCL/roles/`),
user-level grants (`DCL/users/`), and cross-database grants
(`DCL/inter_db/`). Within Wave 2, the execution order is:
roles first (a role must exist before it is granted), then
user grants (a user must exist before it receives a grant),
then inter-database grants (both source and target databases
must exist, which is guaranteed by Wave 1 completion).

Wave 2 executes with DCL serialisation — a single-threaded
lock on the DCL executor is held for the duration of the wave.
This prevents deadlocks from concurrent `GRANT` operations
targeting overlapping object sets, a failure mode observed in
early parallel DCL testing.

**Wave 3 — DDL Objects.**
All DDL objects in the payload: tables (`.tbl`), views (`.viw`),
stored procedures (`.spl`), macros (`.mcr`), join indexes
(`.jix`), triggers (`.trg`), script table operators (`.sto`),
and user-defined functions (`.fun`). Within Wave 3:

- Tables are deployed before views (a view cannot reference a
  non-existent table).
- Join indexes are deployed after their base tables.
- Triggers are deployed after their base tables.
- Procedures and macros are deployed after any objects they
  reference (if the reference set is known at package time).
- Within the same sub-tier, independent objects may be deployed
  in parallel using a thread pool bounded by `--max-workers`
  (default: 4).

The manifest produced by Package records the wave number and
intra-wave position for every object. The Ship phase reads the
manifest and executes accordingly; it does not re-derive
ordering at runtime.

## Consequences

**Positive**

- Correct deployment order is guaranteed by the manifest, not
  by operator knowledge. A new team member running `ships ship`
  for the first time gets the correct execution order without
  being briefed.
- Wave boundaries are natural retry points. A Wave 2 failure
  can be retried from Wave 2 without re-running Wave 1, provided
  Wave 1 was fully committed.
- The manifest's explicit wave assignments make the planned
  deployment reviewable. A reviewer can scan the manifest and
  verify that, e.g., the view database's inter-database GRANT
  precedes the view creation.
- Intra-wave parallelism in Wave 3 significantly reduces
  deployment time for modules with many independent tables.

**Negative**

- The three-wave model assumes that all cross-wave dependencies
  can be classified into the database / grant / object tiers.
  Edge cases exist: a stored procedure that executes a `GRANT`
  at runtime is not modelled. These are rare in Teradata DDL
  but may surface in legacy schemas.
- DCL serialisation in Wave 2 is a performance constraint.
  A module with many inter-database grants (e.g. a full AI-Native
  Data Product with Domain, Semantic, Memory, and BAL databases)
  executes its grants sequentially. In practice this is
  millisecond-scale per grant and is not a user-visible bottleneck.
- Wave 1's parent–child ordering requires parsing the `FROM`
  clause of `CREATE DATABASE` statements. This is the one case
  where the pipeline must read DDL content rather than deriving
  ordering from filename metadata alone.

**Neutral**

- A future "Wave 0" for pre-deployment environment checks (profile
  validation, space availability, privilege pre-flight) is a
  natural extension of this model. Wave 0 would not execute DDL;
  it would validate that the target environment can accept
  Waves 1–3.
- The three-wave model maps well to the DCL subdirectory
  structure (ADR 0008): `DCL/roles/` → Wave 2 first, `DCL/users/`
  → Wave 2 second, `DCL/inter_db/` → Wave 2 third.

## Alternatives considered

**Topological sort of all objects across all waves.** Considered:
a full dependency graph across database creation, grants, and DDL
objects would allow arbitrary execution ordering. Rejected: the
dependency graph for grants is not statically derivable without
querying the target environment for current privilege state, which
couples the Package phase to a live system. The three-wave model
derives ordering from payload structure alone — no target
environment query required.

**Single sequential wave (manifest defines execution order, no
parallelism).** Rejected: correct but slow for large modules.
A module with 200 independent tables deployed sequentially is
the unnecessary serialisation this pipeline exists to avoid.

**Operator-specified wave assignments in `ships.yaml`.** Rejected:
this re-introduces operator knowledge as a prerequisite and
makes the manifest non-reproducible across operators.

**Two waves (databases+grants combined, then DDL objects).**
Considered. Rejected: combining database creation and grant
execution in one wave does not resolve the ordering problem —
grants targeting databases must still come after those databases
exist. The three-wave separation makes the precedence
constraints explicit and avoids intra-wave ordering complexity
within a combined wave.

## References

- `td_release_packager/builder.py` — computes wave assignments
  and writes the deploy manifest.
- `database_package_deployer/deployer.py` — reads the manifest and executes
  waves 1–3 in order; manages the thread pool for intra-Wave 3
  parallelism.
- `database_package_deployer/models.py` — `STRATEGY_MAP` defines which
  deployer strategy applies per object type within Wave 3.
- ADR 0002: SHIPS pipeline phase structure — the Ship phase
  executes the three waves.
- ADR 0008: DCL subdirectory structure — governs the layout
  of the `DCL/` directory that feeds Wave 2.
