# DML Naming in an Atomic Database Script Framework

> **SHIPS context.** This is one of three SHIPS design-rationale notes — see the [Design Rationale index](./README.md) for navigation. The companion notes are [Eponymous, Atomic Database Scripts](./eponymous-atomic-scripts.md) (the principle this note extends to a harder case) and [Object-Type Extensions for Database Scripts](./object-type-extensions.md) (which covers the value of `.dml` as a deployment-intent classifier).

## Purpose

This note defines a practical naming approach for DML scripts in a database deployment framework that otherwise uses [eponymous, atomic scripts](./eponymous-atomic-scripts.md) for database object definitions.

The key issue is that DML is different from DDL. DDL normally creates, replaces, or alters a known database object. DML changes data, and the target of that change may not always be clear from the script or from a harvesting process.

The recommended position is:

> Use `.dml` to classify governed data-changing scripts, but only use `database.table.dml` naming when the harvesting process can identify a single primary target table with high confidence.

Do not force DML into an object-eponymous naming model when the framework cannot reliably determine the object scope.

---

## Background

For object-defining database artefacts, eponymous naming is usually reliable.

Examples:

```text
customer_core.customer.tbl
customer_access.customer.viw
risk_model.calculate_pd.spl
customer_access.get_customer.mcr
```

In these cases, the database catalogue or parser can usually determine the object name and object type.

For DML, this is less reliable. A DML script may:

- insert into one table
- update one table
- merge into one table
- delete from one table
- read from many tables
- change multiple tables
- call a procedure that changes data indirectly
- use dynamic SQL
- use temporary or volatile tables
- represent a migration operation rather than a table-specific artefact
- contain several unrelated data changes in one script

Because of that, DML should be classified as data-changing SQL, but it should not always be treated as object-eponymous.

---

## Core Principle

The filename should not claim more certainty than the harvesting process can prove.

For DDL:

```text
one object = one eponymous file
```

For DML:

```text
one data-changing artefact = one classified file, with target confidence recorded separately
```

The extension `.dml` is valuable because it tells the deployment framework that the script changes data. The base filename should only name a table when the script is genuinely table-scoped.

---

## Why Use `.dml` Instead of `.sql`?

The `.sql` extension describes the language. It does not describe the deployment intent.

A `.sql` file could contain:

```sql
select * from customer;
insert into customer values (...);
grant select on customer to reporting_role;
replace view customer_v as ...;
collect statistics on customer;
```

Those are very different deployment actions.

A `.dml` file declares that the script intentionally changes table data through operations such as:

- `insert`
- `update`
- `delete`
- `merge`
- procedure calls or scripts whose purpose is to mutate data

That distinction allows the deployment framework to apply stronger controls.

For example, `.dml` files may require:

- explicit production approval
- row-count logging
- pre-run and post-run validation
- backup or rollback planning
- idempotency checks
- exclusion from structural-only deployments
- manual review when targets are ambiguous

The key argument is:

> `.sql` tells you the script contains SQL. `.dml` tells you the script changes data.

---

## When `database.table.dml` Is Appropriate

Use `database.table.dml` only when the script is clearly scoped to one primary target table.

Examples:

```text
reference_data.country_code.dml
reference_data.currency_code.dml
app_config.feature_flags.dml
control.batch_calendar.dml
migration_control.object_status.dml
```

This naming pattern is appropriate for:

- reference data loads
- seed data
- configuration data
- lookup tables
- control tables
- small governed data sets
- single-target `insert`, `update`, `delete`, or `merge` scripts

Example:

```sql
merge into reference_data.country_code as tgt
using reference_data.country_code_seed as src
on tgt.country_code = src.country_code
when matched then update
set country_name = src.country_name
when not matched then insert
(
    country_code
  , country_name
)
values
(
    src.country_code
  , src.country_name
);
```

A file like this could reasonably be named:

```text
reference_data.country_code.dml
```

The table is the subject of the artefact.

---

## When `database.table.dml` Is Not Appropriate

Do not use `database.table.dml` when the DML is not clearly scoped to one table.

This includes scripts that:

- modify multiple tables
- perform a business operation
- perform a migration step
- call procedures that hide the actual data changes
- use dynamic SQL
- use ambiguous aliases or generated table names
- contain several unrelated statements
- cannot be confidently parsed by the harvester

For example:

```sql
insert into customer_domain.customer_h
select *
from staging.customer_extract;

update migration_control.batch_status
set status_code = 'complete'
where batch_id = 1001;

delete from staging.customer_extract
where batch_id = 1001;
```

This should not be named after only one table, because doing so would be misleading.

A better name would be operation-based or source-based, such as:

```text
load_customer.statement_000017.multi_table.dml
```

or:

```text
migration_control.load_customer_batch.dml
```

---

## Recommended DML Categories

A harvesting framework should classify DML scripts into practical categories.

### 1. Table-scoped DML

Use this category when the harvester identifies one clear primary target table with high confidence.

Recommended naming:

```text
database.table.dml
```

Example:

```text
reference_data.country_code.dml
```

### 2. Multi-table DML

Use this category when the script modifies more than one target table.

Recommended naming:

```text
sourcefile.statement_000123.multi_table.dml
```

Example:

```text
load_customer.statement_000017.multi_table.dml
```

### 3. Unknown-target DML

Use this category when the harvester can identify that the script changes data, but cannot determine the target table reliably.

Recommended naming:

```text
sourcefile.statement_000124.unknown_target.dml
```

Example:

```text
month_end.statement_000044.unknown_target.dml
```

### 4. Review-required DML

Use this category when the script may be data-changing, but the harvester cannot safely classify it.

Recommended naming:

```text
sourcefile.statement_000125.review_required.dml
```

Example:

```text
legacy_load.statement_000088.review_required.dml
```

### 5. Operation-scoped DML

Use this category when the DML represents a named operation rather than a table-level artefact.

Recommended naming:

```text
operation_name.dml
```

Examples:

```text
initialise_release_state.dml
backfill_customer_segment.dml
load_month_end_balances.dml
```

A harvesting process may not always be able to infer this automatically. Operation-scoped naming is more suitable for curated scripts than raw harvested scripts.

---

## Folder-Based Layout Option

A folder-based structure is usually clearer than very long filenames.

Example:

```text
payload/
  dml/
    table_scoped/
      reference_data.country_code.dml
      app_config.feature_flags.dml

    multi_table/
      load_customer.statement_000017.dml
      month_end.statement_000031.dml

    unknown_target/
      legacy_load.statement_000044.dml

    review_required/
      adhoc_fix.statement_000009.dml
```

This keeps filenames readable while preserving classification.

---

## Flat Layout Option

A flat layout can also work, especially where packaging tools expect all files in one directory.

Example:

```text
payload/dml/
  reference_data.country_code.dml
  app_config.feature_flags.dml
  load_customer.statement_000017.multi_table.dml
  month_end.statement_000044.unknown_target.dml
  adhoc_fix.statement_000009.review_required.dml
```

This is simpler for some tools, but filenames can become longer.

---

## The Manifest Should Be the Source of Truth

For DML, the filename should be helpful, but the manifest should carry the real metadata.

Example manifest columns:

```text
artefact_path
artefact_type
operation
target_count
primary_target
target_candidates
source_candidates
confidence
source_file
statement_number
review_status
```

Example manifest entries:

```text
artefact_path,artefact_type,operation,target_count,primary_target,confidence,source_file,statement_number
payload/dml/table_scoped/reference_data.country_code.dml,dml,merge,1,reference_data.country_code,high,seed_reference.btq,12
payload/dml/multi_table/load_customer.statement_000017.dml,dml,mixed,3,,medium,load_customer.btq,17
payload/dml/unknown_target/month_end.statement_000044.dml,dml,unknown,,,low,month_end.btq,44
```

This allows the deployment process to make better decisions than it could from the filename alone.

---

## Optional Sidecar Metadata

For harvested DML, a sidecar metadata file can be useful.

Example:

```text
payload/dml/table_scoped/reference_data.country_code.dml
payload/dml/table_scoped/reference_data.country_code.json
```

Example metadata:

```json
{
  "artefact_type": "dml",
  "source_file": "seed_reference.btq",
  "statement_number": 12,
  "operation": "merge",
  "target_candidates": [
    "reference_data.country_code"
  ],
  "source_candidates": [
    "reference_data.country_code_seed"
  ],
  "confidence": "high",
  "table_scoped": true,
  "review_required": false
}
```

This is especially useful where the DML was harvested from larger scripts and needs traceability back to the original source.

---

## Deployment Controls for DML

Because DML changes data, it should usually be controlled separately from DDL and DCL.

Possible controls include:

- exclude `.dml` from default structural deployments
- require an explicit `--include-dml` option
- require approval metadata before production deployment
- require high confidence for automatic deployment
- quarantine unknown-target DML
- route multi-table DML for manual review
- log affected row counts
- require validation SQL
- require rollback or compensating scripts where practical
- require idempotent `merge` patterns for reference data

Example deployment behaviour:

```text
high-confidence table-scoped DML: eligible for controlled automated deployment
multi-table DML: manual review required
unknown-target DML: quarantined by default
review-required DML: blocked until classified
```

---

## Recommended Policy Wording

The following wording can be used in a database source-code standard.

> Governed data-changing scripts should use the `.dml` extension rather than the generic `.sql` extension. The `.dml` extension identifies scripts that intentionally mutate table data and allows deployment tooling to apply appropriate controls, review rules, sequencing, and reporting.
>
> DML files should only use `database.table.dml` naming when the script is clearly scoped to a single primary target table and the harvesting process can identify that table with high confidence. Where the target is ambiguous, multi-table, dynamic, or unknown, the file should be named using source-file and statement identifiers, and the detected targets, operation type, and confidence level should be recorded in the deployment manifest.
>
> The filename should not encode certainty that the harvesting process does not actually have.

---

## Recommended Rule Set

Use the following rules:

```text
Use .dml for governed data-changing scripts.

Use database.table.dml only for high-confidence single-target DML.

Use sourcefile.statement_number.multi_table.dml for scripts that modify multiple tables.

Use sourcefile.statement_number.unknown_target.dml when the target cannot be reliably determined.

Use sourcefile.statement_number.review_required.dml when the script cannot be safely classified.

Use operation_name.dml only for curated operation-scoped scripts, not for raw harvested scripts unless the operation can be reliably identified.

Keep DML metadata in the manifest or sidecar JSON, not only in the filename.
```

---

## Bottom Line

DML should be classified, but not always object-eponymous.

The most defensible principle is:

> Do not encode certainty in the filename that the harvesting process does not actually have.

A good framework should therefore use `.dml` as the important classification, use `database.table.dml` only when a single target table is known with high confidence, and rely on a manifest or sidecar metadata to capture operation type, target candidates, source candidates, and confidence.

---

## SHIPS-specific implementation notes

The SHIPS harvester implements this standard with the following conventions:

* **Per-statement splitting.** Multi-statement DML files are split into individual `INSERT` / `UPDATE` / `DELETE` / `MERGE` chunks so each chunk has a single primary target. Where each chunk has high-confidence single-target naming, it is placed as `<db>.<table>.dml` and aggregating-type append behaviour folds repeated targets into one file.

* **`-- MULTI_TABLE_DML` opt-out marker.** A source author can disable per-statement splitting by placing a `-- MULTI_TABLE_DML` header marker at the top of a SQL file. The harvester then treats the entire script as one DML artefact named after the source file rather than one of its targets — preserving the script's sequencing and transactional intent. This is the right choice for migration scripts, sequenced operations like `INSERT staging; UPDATE control; DELETE staging;`, or any case where the order of statements is the meaning of the script. The marker mirrors the existing `-- LOCKING VIEW` convention used by the SHIPS view-layer generator.

* **Foreign-key ordering across split DML files.** Once a multi-INSERT script has been split into per-target eponymous files, the deployer needs to order them so parent inserts run before child inserts. SHIPS reads `REFERENCES` clauses from the package's `CREATE TABLE` statements (the same dependency graph the `analyze` stage already builds for prereqs) and emits a DML wave ordering. Naming carries the *what*; the manifest and `_order.txt` carry the *when*.

* **The manifest is the source of truth, not the filename.** SHIPS' `BUILD.json` records `primary_target`, `target_count`, and (where applicable) `target_candidates` and `confidence` for each placed file. The deployer uses these for ordering and gating decisions. The filename is a useful summary; the manifest is the contract.
