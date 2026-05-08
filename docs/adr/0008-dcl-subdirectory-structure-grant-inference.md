# ADR 0008: DCL Subdirectory Structure and OPS-Aware Grant Inference

## Status

Revised | 2026-05-08 (supersedes Accepted | 2026-04-02)

### Revision summary

The original inference model (v1) applied `GRANT SELECT WITH GRANT OPTION`
uniformly to all inter-database grants, regardless of whether the source
database held tables or views. This was incorrect: it produced grants that
exposed table databases directly to consumer roles, bypassing the view layer
that the Object Placement Standard (OPS) exists to enforce. The revised model
(v2) encodes the two-tier grant chain that OPS requires and adds an Inspect
rule to flag direct table access in source DDL as an ERROR-severity violation.

---

## Context

Teradata access control for a multi-database data product requires
several categories of `GRANT` statement:

1. **Role grants.** `GRANT {privilege} ON {database} TO {role}`.
   Grants a named role access to a database. Roles aggregate
   privileges; users are then granted roles rather than direct
   privileges.

2. **User grants.** `GRANT {role} TO {user}`. Assigns a role to
   a user or service account. May also include direct database
   grants to functional accounts (ETL users, reporting users).

3. **Inter-database grants.** Structural plumbing grants that
   allow one database to read objects in another. In Teradata,
   a view database requires `SELECT WITH GRANT OPTION` on its
   backing table database so that the view can be queried by
   downstream consumers. These grants are the connective tissue
   of a multi-database data product.

In early SHIPS prototypes, all `GRANT` statements were placed in
a flat `DCL/` directory alongside the DDL files. This created
several problems:

- A reviewer could not determine at a glance whether a file
  contained a role grant, a user grant, or an inter-database
  grant. All three require different review contexts.
- Wave 2 ordering within DCL was underdetermined. Roles must
  exist before they are granted to users. Inter-database grants
  must come after both sides of the grant exist. A flat directory
  provides no mechanism for the deployer to derive this ordering.
- Cross-database grants were being written manually, producing
  inconsistencies: some developers granted `SELECT`, some granted
  `SELECT, INSERT` (incorrect for view databases), some omitted
  `WITH GRANT OPTION` where it was required.

The v1 inference engine addressed these problems but introduced
a new one: it inferred `GRANT SELECT WITH GRANT OPTION` for both
table databases and view databases, granting consumer roles direct
access to table databases. This violates the Object Placement
Standard, which interposes a 1:1 locking view layer between raw
table stores and all downstream consumers. A package that
correctly separates tables and views should never grant a consumer
role direct SELECT on the table database — that access belongs to
the view database only.

The v1 engine could not distinguish a table database from a view
database, so it applied the same grant pattern to both. The
correct two-tier chain is:

```
Table database  ──GRANT SELECT WITH GRANT OPTION──►  View database
View database   ──GRANT SELECT (no GRANT OPTION)──►  Consumer roles
```

The v1 engine produced the wrong chain:

```
Table database  ──GRANT SELECT WITH GRANT OPTION──►  Consumer roles  ✗
View database   ──GRANT SELECT WITH GRANT OPTION──►  Consumer roles  ✗
```

`WITH GRANT OPTION` on a view database to consumer roles is also
incorrect: consumers should not be able to re-grant their access
to other parties. Only the view database's grant to its backing
table database needs `WITH GRANT OPTION`, so the privilege
propagates through the view layer when consumers query the view.

---

## Decision

### DCL Subdirectory Structure

Unchanged from v1. The `DCL/` subdirectory within each database's
payload directory is organised into three mandatory subdirectories:

```
payload/{database}/DCL/
    roles/      # GRANT {privilege} ON {database} TO {role}
    users/      # GRANT {role} TO {user}; direct user grants
    inter_db/   # GRANT {privilege} ON {source_db} TO {target_db}
```

Each subdirectory contains `.dcl` files following the atomic
eponymous convention (ADR 0004 adapted for DCL):
`{source}.{target}.dcl` for inter-database grants;
`{database}.{role}.dcl` for role grants;
`{database}.{user}.dcl` for user grants.

The deployer executes Wave 2 in the order: `roles/` first,
`users/` second, `inter_db/` third. This ordering is structural —
derived from subdirectory name, not from file content.

### Database Classification

Before inferring any grant, `infer_grants.py` classifies every
database in the package into one of three tiers:

| Tier | Classification rule | OPS role |
|------|--------------------|----|
| **Table database** | Contains `.tbl` files; no `.viw` files | Raw data store; not consumer-facing |
| **View database** | Contains `.viw` files | Consumer-facing interface layer |
| **Execution database** | Contains `.spl` or `.mcr` files; no `.tbl` or `.viw` files | Procedure/macro host |

A database directory that contains both `.tbl` and `.viw` files is
classified as **ambiguous**. The inference engine does not generate
grants for an ambiguous database and raises an Inspect WARNING
(`DCL_AMBIGUOUS_DB_TYPE`), because the correct grant tier cannot
be determined without knowing which objects are tables and which
are views.

**Corroborating signal — naming convention.** Where a database
name ends with a recognised OPS suffix (`_T`, `_H`, `_V`, `_VW`,
`_VIEWS`), the inference engine cross-checks the suffix against
the content classification. A mismatch (e.g., a `_T`-suffixed
database that contains `.viw` files) raises an Inspect WARNING
(`DCL_NAME_CONTENT_MISMATCH`) and halts inference for that
database. Content classification is always the primary signal;
naming is corroborating only.

### OPS-Aware Two-Tier Grant Inference

`infer_grants.py` applies the following rules after classification:

#### Tier 1 — Structural plumbing (table database → view database)

For every table database that is referenced by a view in another
database within the same package:

```sql
GRANT SELECT ON {{TABLE_DB}} TO {{VIEW_DB}} WITH GRANT OPTION;
```

`WITH GRANT OPTION` is mandatory here. It allows the view
database to propagate `SELECT` to its consumers — without it,
a consumer granted `SELECT ON {{VIEW_DB}}` cannot execute views
that reference `{{TABLE_DB}}`.

The grant target is always the view database, never a consumer
role. Granting consumer roles direct access to a table database
is never inferred.

#### Tier 2 — Consumer access (view database → roles)

For every view database in the package, a role grant is generated
in `roles/`:

```sql
GRANT SELECT ON {{VIEW_DB}} TO {{ROLE_NAME}};
```

No `WITH GRANT OPTION`. Consumers can query the view database but
cannot re-grant that access to other parties.

#### Execution databases (procedures / macros)

```sql
GRANT EXECUTE PROCEDURE ON {{EXEC_DB}} TO {{ROLE_NAME}};
GRANT EXECUTE MACRO ON {{EXEC_DB}} TO {{ROLE_NAME}};
```

These are always consumer-facing; no two-tier chain applies.
No `WITH GRANT OPTION`.

#### Single-database deployments (no OPS separation)

When the package contains a single database holding both tables
and views (i.e., OPS separation has not been applied), the
database is classified as ambiguous. The inference engine does
not produce inter-database grants. It raises an Inspect WARNING
(`DCL_NO_OPS_SEPARATION`) and generates a role grant:

```sql
GRANT SELECT ON {{DB}} TO {{ROLE_NAME}};
```

This grant is marked in the generated file with a comment:

```sql
-- WARNING: No OPS view layer detected. This grant gives consumer
-- roles direct SELECT on the database, including any base tables.
-- Separate tables and views into distinct databases (OPS) to
-- enforce a view layer and restrict this grant to views only.
```

The package is deployable, but the WARNING contributes to a
reduced Trust Score in the Completeness and Isolation dimensions.

#### Grant matrix summary

| Source tier | Grant target | Privilege | WITH GRANT OPTION |
|---|---|---|---|
| Table database | View database (same package) | `SELECT` | Yes |
| View database | Consumer roles | `SELECT` | No |
| Execution database | Consumer roles | `EXECUTE PROCEDURE` / `EXECUTE MACRO` | No |
| Table database | Consumer roles | — | Never inferred |

### Direct Table Access — Inspect Rule

The dependency analyser (ADR 0005) tracks which database each
`FROM` clause resolves to. If any view resolves a `FROM` clause
directly to a table database (i.e., the view queries tables
without an intervening view layer), Inspect raises:

```
DCL_DIRECT_TABLE_ACCESS  ERROR
  View {db}.{view_name} references table database {table_db}
  directly. Consumer access to {table_db} will not be inferred.
  Introduce a 1:1 locking view in a separate view database, or
  provide an explicit inter_db grant if direct access is
  intentional.
```

This is an ERROR-severity finding. It blocks the Trust Score
Safety dimension and will prevent packaging at `--min-trust-score`
thresholds above the Safety dimension floor.

Direct table access is not automatically granted to consumer roles
under any circumstance. If a project genuinely requires it, the
developer must provide an explicit hand-authored grant in
`inter_db/` — making the intent deliberate and visible in code
review rather than silently generated.

### Inference Engine Behaviour

`infer_grants.py` is invoked during the Inspect phase
(`ships inspect --infer-grants`). It writes generated files to
`payload/{database}/DCL/inter_db/` and `payload/{database}/DCL/roles/`.

Generated files carry a header comment identifying them as
inferred:

```sql
-- GENERATED by infer_grants.py — do not edit directly.
-- Source: {database} classified as {tier} (content: {file_types}).
-- To override, create a hand-authored file with the same name;
-- the hand-authored file takes precedence.
```

Manual override is always permitted. A hand-authored `.dcl` file
with the same `{source}.{target}.dcl` name as a generated file
takes precedence. When the inference engine detects an override,
it logs the override at INFO level and skips generation for that
source-target pair. It does not warn or error on an override —
the presence of a hand-authored file is an explicit developer
decision, not an anomaly.

### DBC Tokenisation

Unchanged from v1. The system catalog database is referenced as
`{{DBC_DATABASE}}` in all grant files, mapping to `DBC` by
default. Sites that proxy DBC through a filtered views database
can override this token without touching any grant file.
`validate_grants.py` raises an Inspect ERROR for any grant file
containing a literal `DBC` reference.

---

## Consequences

**Positive**

- The inference engine now correctly models the OPS grant chain.
  Consumer roles never receive direct SELECT on a table database
  through an inferred grant. This closes the privilege escalation
  gap identified in the v1 design.
- `WITH GRANT OPTION` is applied precisely: to structural
  table→view plumbing only, not to consumer role grants. This
  is the minimum privilege required for the view layer to function.
- Direct table access in source DDL is surfaced as an ERROR at
  Inspect time, before packaging. Developers are guided toward
  OPS compliance rather than having the wrong access pattern
  silently normalised.
- Single-database packages remain deployable with a clear WARNING
  explaining what was not inferred and why. The package is not
  blocked; the risk is visible.
- Generated files are clearly stamped and always overridable.
  Developer intent expressed through a hand-authored file always
  wins.

**Negative**

- The classification step requires the inference engine to read
  every database directory before generating any grant. For large
  packages (hundreds of databases) this is a sequential scan.
  In practice, Teradata packages rarely exceed tens of databases;
  this is not expected to be a performance concern.
- Ambiguous databases (both `.tbl` and `.viw` files in the same
  directory) produce no inferred grants and a WARNING, which may
  surprise developers who have not yet separated their schemas
  by OPS. Clear diagnostic messages mitigate this.
- The corroborating naming-convention check adds a dependency on
  OPS suffix conventions. Projects that do not use OPS suffixes
  (`_T`, `_V`, etc.) will not trigger the mismatch check, so the
  cross-validation only fires when suffixes are present. Content
  classification still works without suffixes.

**Neutral**

- `roles/` and `users/` subdirectories continue to accept
  manually authored files only for role-to-user assignments and
  environment-specific role membership. The inference engine does
  not generate these.
- The `.dcl` extension is unchanged.
- DBC tokenisation is unchanged.

---

## Alternatives Considered

**Infer consumer role grants from dependency graph only, ignore
database tier.** Rejected: this is the v1 approach. It produces
grants to the wrong target (table databases exposed to consumer
roles) and does not encode OPS intent.

**Require all inter-database grants to be hand-authored; drop
inference entirely.** Considered seriously. Rejected because: the
OPS grant chain is mechanical and highly consistent across
packages; inference eliminates a class of `WITH GRANT OPTION`
omission bugs that are difficult to detect in review; and the
inference engine now refuses to infer in ambiguous cases, so it
cannot silently produce a wrong grant — it either produces the
correct grant or produces nothing and raises a finding. The
risk of silent misinterpretation that motivated this alternative
is addressed by the classification rules.

**Privilege matrix in `ships.yaml`.** Considered in v1, rejected
again. The two-tier grant chain is derivable from package content;
a configuration artefact would duplicate information already
expressed in the DDL structure and create a consistency surface
to maintain.

**Object-level grants.** Rejected at project inception and
unchanged. Table- and column-level grants are a governance and
security engineering concern outside SHIPS scope.

**Flat DCL directory with content-based ordering.** Rejected in
v1 and unchanged. Structural ordering via subdirectory names is
simpler and more reliable than parsing grant statements.

---

## References

- `td_release_packager/infer_grants.py` — OPS-aware grant
  inference engine (v2). Implements database classification,
  two-tier grant chain, and single-database fallback.
- `td_release_packager/validate_grants.py` — Inspect rules:
  `DCL_AMBIGUOUS_DB_TYPE`, `DCL_NAME_CONTENT_MISMATCH`,
  `DCL_NO_OPS_SEPARATION`, `DCL_DIRECT_TABLE_ACCESS`,
  DBC tokenisation check.
- `td_release_packager/models.py` — `DCLManifest` and grant
  file schema.
- ADR 0003: Token engine — `{{DBC_DATABASE}}` token.
- ADR 0004: Atomic eponymous DDL files — naming convention that
  DCL files follow.
- ADR 0005: Wave ordering for deployment — Wave 2 execution
  sequence (`roles/` → `users/` → `inter_db/`).
- ADR 0009: Configurable deploy intent — Trust Score dimensions
  (Isolation, Safety) affected by `DCL_DIRECT_TABLE_ACCESS` and
  `DCL_NO_OPS_SEPARATION` findings.
