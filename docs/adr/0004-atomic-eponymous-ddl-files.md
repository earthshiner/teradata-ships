# ADR 0004: Atomic Eponymous DDL Files

## Status

Accepted | 2026-02-05

## Context

DDL for enterprise Teradata systems has historically been organised
in a variety of ways:

- **Monolithic scripts.** A single `.bteq` or `.sql` file
  containing hundreds of `CREATE` statements for an entire
  database or module. Common in migration projects; common in
  legacy BTEQ tooling.
- **Functional groupings.** Files grouped by operation type:
  `create_tables.sql`, `create_views.sql`, `grant_all.sql`.
  Each file contains many objects.
- **Ad-hoc naming.** Files named for the developer, the sprint,
  or the ticket: `PD-1234-customer-tables.sql`.

All three patterns have the same failure mode when used with an
automated deployment pipeline: the pipeline cannot reason about
individual objects. If a monolithic script fails on object 47 of
200, the pipeline cannot selectively retry from that point. It
cannot determine which objects have already been deployed and
which have not. It cannot produce a per-object audit trail.

SHIPS requires the pipeline to track deployment status, rollback
capability, and Discipline compliance at the level of individual
objects, not files. This is only possible if each file contains
exactly one object definition.

Additionally, the Harvest phase (ADR 0003) produces a tokenised
payload where file paths encode the physical database name and
object name. If a file contains multiple objects from multiple
databases, the path cannot encode that information without
inventing an arbitrary key.

A file naming convention was required that:

- Makes the object's identity (database + name) recoverable from
  the filename alone, without parsing the file content.
- Allows the pipeline to detect duplicate objects (same database
  and name appearing in two files) at Inspect time rather than
  at Ship time.
- Is consistent across all Teradata DDL object types.
- Makes file diffs in version control meaningful: a diff on
  `D01_MP_DOM_T.Customer_H.tbl` is obviously about the
  `Customer_H` table in the `D01_MP_DOM_T` database.
- Carries object type information without requiring the pipeline
  to parse the DDL content to determine type.

## Decision

All DDL source files in a SHIPS project conform to the
**atomic eponymous** convention:

1. **One object per file.** Each file contains exactly one
   `CREATE` statement and nothing else (no DML, no grants, no
   comments that span multiple objects). A companion `.stt`
   statistics file is the only permitted sibling artefact for
   table files (see item 5).

2. **Filename encodes identity.** The file is named
   `{DatabaseToken}.{ObjectName}.{extension}` where:
   - `{DatabaseToken}` is the token name from the token map
     (pre-harvest) or the physical database name (post-harvest
     in the payload and release archive).
   - `{ObjectName}` is the exact name of the object as it
     appears in the `CREATE` statement.
   - `{extension}` identifies the object type (see item 3).

   Example: `{{DOM_DATABASE_T}}.Customer_H.tbl` in source;
   `D01_MP_DOM_T.Customer_H.tbl` in the packaged release for DEV.

3. **Standard extensions by object type.** Extensions are fixed
   and mandatory:

   | Extension | Object type |
   |-----------|-------------|
   | `.tbl`    | Table (permanent, including GTT) |
   | `.viw`    | View |
   | `.spl`    | Stored procedure |
   | `.mcr`    | Macro |
   | `.jix`    | Join index |
   | `.trg`    | Trigger |
   | `.sto`    | Script table operator (STO) |
   | `.fun`    | User-defined function (UDF) |

   Extensions are lowercase. Any file with an unrecognised
   extension is flagged by Inspect at ERROR severity.

4. **Object type prefix or suffix is prohibited in the object
   name.** The extension carries the type signal. `Customer_H_T`
   (trailing `_T` for table) or `VW_Customer_H` (leading `VW_`
   for view) are naming convention violations. Object type belongs
   in the extension, not the name.

5. **Companion statistics files.** Each `.tbl` file may have a
   companion `{DatabaseToken}.{ObjectName}.stt` file containing
   the `COLLECT STATISTICS` statement for that table. The `.stt`
   file is an optional peer; its absence is not an Inspect
   violation. Its presence without a matching `.tbl` is.

6. **Directory structure within the payload.** Files are organised
   under `payload/{DatabaseToken}/` (pre-harvest) or
   `payload/{PhysicalDatabase}/` (post-harvest). Within that
   directory, DDL files are at the top level; no subdirectory
   organisation by object type is used — the extension encodes
   the type, making subdirectories redundant.

7. **Duplicate detection.** Inspect checks for duplicate
   `{Database}.{ObjectName}` across the payload. A duplicate
   (the same object appearing in two files) is an ERROR-severity
   violation. The pipeline cannot determine which file is
   canonical at Ship time; the developer must resolve the
   ambiguity at Inspect time.

## Consequences

**Positive**

- The pipeline can map each file to exactly one database object.
  Per-object deployment status, rollback snapshot, and Discipline
  compliance reporting become straightforward.
- Duplicate object detection is O(n) over filenames rather than
  O(n) over parsed DDL content. Fast and reliable.
- File diffs in version control are semantically meaningful.
  A PR that touches `D01_MP_SEM_V.Borrower_Summary.viw` is
  obviously about the `Borrower_Summary` view.
- Onboarding new DDL from a legacy monolithic script is a
  mechanical split operation: one file per `CREATE` statement,
  named for the object. No design decisions required.
- The Discipline check "does this file contain exactly one
  CREATE?" is a simple parse, not a semantic analysis.

**Negative**

- A module with 200 tables produces 200 files plus up to 200
  `.stt` companions — up to 400 files per database. File system
  overhead is trivial on modern systems, but directory listings
  require tooling that understands the convention (or filtering)
  to be navigable.
- Developers accustomed to monolithic scripts must adopt a new
  workflow for authoring DDL. The `ships normalise` command
  (ADR 0009) can split a monolithic script into atomic files,
  but the split must be reviewed.
- Some editors and diff tools behave poorly with large numbers
  of small files. This is a tooling choice concern, not a
  correctness concern.

**Neutral**

- The `.stt` companion convention is specific to Teradata.
  Other platforms do not have an equivalent; the convention
  would need to be reconsidered if SHIPS were extended to
  other database engines.
- The prohibition on type suffixes/prefixes (`_V`, `_T`, `VW_`)
  in object names is a naming standard, not enforced by the
  extension-based typing. Inspect validates extensions; name
  pattern validation is a separate Discipline rule.

## Alternatives considered

**Monolithic scripts per database.** Rejected: the pipeline
cannot track per-object status. A failed deployment leaves the
database in an indeterminate state. Retry requires re-running
the full script with no way to skip already-applied objects.

**Subdirectory grouping by type** (e.g. `tables/`, `views/`).
Rejected: the extension already encodes the type. Subdirectories
add path length without adding information, and complicate the
Harvest-time path-to-database-name derivation.

**Extension-less naming with type in a manifest.** Considered.
Rejected: requires the manifest to be the source of truth for
type, making the file system non-self-describing. A developer
viewing the payload directory cannot determine object types
without consulting the manifest.

**Object name as full path** (e.g.
`D01_MP_DOM_T/tables/Customer_H.sql`). Rejected: splits the
database-name and object-name information across the path
hierarchy, complicating filename-to-identity derivation in
both the token engine and the deployer.

## References

- `td_release_packager/ingest.py` — derives database and object
  name from filenames during Harvest; enforces the naming
  convention.
- `td_release_packager/validate.py` — `_check_file_naming` and
  `_check_duplicate_objects` Discipline rules.
- `td_release_packager/builder.py` — uses detokenised filenames
  when assembling the release archive.
- ADR 0003: Token engine — provides the token substitution that
  transforms `{{DatabaseToken}}.ObjectName.ext` filenames into
  `PhysicalDatabase.ObjectName.ext` in the packaged output.
- ADR 0006: Deployer owns idempotency — per-object intent is
  derivable from the file extension via `STRATEGY_MAP`, which
  depends on the atomic-one-object-per-file property.
