# Functional and Defensible Reasons for Eponymous, Atomic Database Scripts

> **SHIPS context.** This is one of three SHIPS design-rationale notes — see the [Design Rationale index](./README.md) for navigation. The companion notes are [Object-Type Extensions for Database Scripts](./object-type-extensions.md) (which covers *how* the file extension carries the object class) and [DML Naming in an Atomic Database Script Framework](./dml-naming.md) (which covers the edge case where eponymous naming would claim more certainty than the harvester actually has).

## Executive summary

Database code should be created, maintained, packaged, and deployed as **eponymous, atomic scripts** wherever practical.

In this context:

- **Atomic** means each script contains the definition or deployment logic for a single database object or a tightly bound single-object artefact.
- **Eponymous** means the script is named after the object it creates, replaces, alters, grants access to, comments on, or otherwise manages.

For example:

```text
customer_core.customer.tbl
customer_access.customer.viw
risk_model.calculate_pd.spl
customer_access.customer.grt
```

The guiding principle is:

> One object, one file, one name, one deployment unit.

This approach treats database code as a managed software asset. It improves traceability, auditability, automation, restartability, impact analysis, release management, and operational support.

---

## 1. Clear ownership of each database object

An atomic script gives every database object a clear and obvious source artefact.

If a table, view, macro, procedure, function, grant, or comment has its own file, it becomes much easier to answer basic operational and governance questions:

- Where is this object defined?
- Who changed it?
- What version of it is deployed?
- What release introduced the change?
- What other objects depend on it?
- Can this object be regenerated or redeployed independently?

This is much harder when a single deployment script contains many unrelated objects, mixed object types, or statements from different functional areas.

A single-object script makes the object boundary explicit.

---

## 2. Better source control and change history

Eponymous, atomic scripts work naturally with source control systems such as Git.

When each object has its own file, the change history is clearer and easier to review. A commit shows which specific objects changed rather than merely showing that a large deployment script was modified.

Instead of this:

```text
changed: release_2026_05_ddl.sql
```

You get this:

```text
changed: customer_core.customer.tbl
changed: customer_access.customer.viw
changed: risk_model.calculate_pd.spl
```

That makes code review, peer approval, defect investigation, and audit evidence much stronger.

Smaller diffs are also easier to understand. Reviewers can focus on the object that changed rather than searching through thousands of lines of unrelated DDL, DCL, procedural code, comments, and deployment control logic.

---

## 3. Easier impact analysis

Atomic files are easier for humans and tools to analyse.

A dependency scanner can map each source file to a specific object and then identify references to other objects. For example:

```text
customer_access.customer.viw
    depends on customer_core.customer.tbl
    depends on reference.country_code.tbl
```

When database code is held in large combined scripts, tooling first has to split the file, infer object boundaries, and determine which statements belong together. That adds complexity and increases the risk of incorrect dependency analysis.

Atomic scripts reduce ambiguity and make dependency graphs more reliable.

This is especially important for database migration, platform modernisation, object extraction, lineage analysis, and deployment sequencing.

---

## 4. Cleaner deployment ordering

Database deployment often depends on object order.

Some objects must exist before other objects can be created. A typical sequence may include:

```text
databases
roles and users
tables
views
macros
procedures
functions
indexes
statistics
grants
comments
```

Atomic scripts make it possible to build deployment order from metadata, file type, object type, and dependency analysis.

For example:

```text
wave 1: independent base tables
wave 2: views over base tables
wave 3: views over views
wave 4: procedures, macros, and functions
wave 5: grants, comments, and post-deployment metadata
```

This makes deployment more controlled and less dependent on manually maintained monolithic scripts.

It also allows tooling to validate whether the deployment order is correct before execution begins.

---

## 5. Safer partial deployment

In real projects, deployment is not always all-or-nothing.

There are many situations where only part of a change should be deployed:

- one corrected view
- one changed procedure
- one table alteration
- one database package
- one functional area
- one set of objects affected by a defect
- one set of objects approved for a release

Atomic scripts make this possible without manually editing a large combined script.

With monolithic deployment scripts, partial deployment is riskier because unrelated changes may be bundled into the same file. This increases the chance of accidentally deploying objects that were not approved, not tested, or not intended for the current environment.

Atomic scripts make the deployable unit explicit and controllable.

---

## 6. Better restartability after failure

Database deployments can fail partway through.

When a large script fails, it can be difficult to determine:

- which statements succeeded
- which statements failed
- which objects are now partially deployed
- where the deployment should restart
- whether it is safe to rerun the script
- whether manual cleanup is required

Atomic scripts allow deployment tooling to record success or failure at object level:

```text
customer_core.customer.tbl              success
customer_access.customer.viw            success
risk_model.calculate_pd.spl             failed
risk_model.calculate_lgd.spl            not started
```

That gives a much cleaner restart point.

It also supports more accurate reporting to release managers, DBAs, application teams, and support teams.

---

## 7. More reliable automation

Atomic scripts are much easier for tools to process.

A deployment or packaging tool can use the file name, folder structure, extension, and file content to infer or validate:

- database name
- object name
- object type
- deployment category
- dependency level
- script path
- expected target platform
- whether the file conforms to standards

This enables automated behaviours such as:

- manifest generation
- dependency sorting
- parallel deployment of independent objects
- object-level logging
- environment comparison
- code scanning
- naming convention validation
- release packaging
- deployment reporting

These capabilities are far harder to implement reliably when objects are embedded inside large multi-object scripts.

---

## 8. Improved auditability

Auditability depends on traceability.

Atomic scripts make it easier to demonstrate a clear relationship between source code, approval, deployment, and the deployed database object.

A defensible audit trail can show:

```text
this source file defines this object
this commit changed this object
this pull request approved this object change
this deployment installed this object version
this deployment log recorded success or failure for this object
```

This is valuable for internal governance, regulatory control, release assurance, and production support.

The naming convention itself becomes part of the control framework.

---

## 9. Easier environment comparison and drift detection

Atomic scripts make it easier to compare database environments.

For example:

```text
dev/customer_access.customer.viw
test/customer_access.customer.viw
prod/customer_access.customer.viw
```

The object in source control can also be compared against the object definition extracted from a live environment.

This helps detect:

- missing objects
- inconsistent definitions
- emergency hot fixes
- unauthorised production changes
- failed deployments
- lower-environment drift
- differences between source control and the database

Drift detection is much harder when the source of truth is a large release script rather than a set of object-level artefacts.

---

## 10. Better packaging for migration projects

Migration projects benefit strongly from object-level packaging.

Objects often need to be classified, filtered, transformed, excluded, or deployed differently depending on their role in the migration.

For example:

```text
objects to migrate
objects to leave on-premises
objects requiring virtualisation
objects requiring QueryGrid access
objects with cross-platform dependencies
objects blocked by unresolved references
objects requiring manual remediation
objects safe for automated deployment
```

Atomic scripts allow each object to be handled individually.

This is particularly useful in hybrid architectures where some data remains on one platform and some data moves to another platform. Views, veneer views, foreign table wrappers, grants, and compatibility layers can be generated and managed as separate object-level artefacts.

A monolithic script does not provide the same degree of classification or control.

---

## 11. Support for parallel deployment

Once dependencies are known, independent objects can often be deployed in parallel.

Atomic scripts make this practical because each script has a clear deployment boundary.

For example:

```text
thread 1: finance_core.account.tbl
thread 2: customer_core.customer.tbl
thread 3: risk_core.exposure.tbl
thread 4: reference.country_code.tbl
```

Parallel deployment can reduce release duration, especially in large environments containing thousands of database objects.

The key is that parallelism must be dependency-aware. Atomic scripts make it possible to schedule independent work safely while still enforcing order where dependencies exist.

---

## 12. Easier error isolation

When a deployment error occurs, atomic scripts make the failing unit obvious.

Instead of searching through a 20,000-line script, the deployment log can identify the specific object and file that failed:

```text
failed: risk_model.calculate_pd.spl
reason: referenced table does not exist
```

This improves:

- triage
- defect assignment
- rerun logic
- support handover
- root cause analysis
- production incident response

The failed object becomes the natural unit of investigation.

---

## 13. Better review by subject-matter experts

Different database artefacts often require different reviewers.

For example:

```text
security team reviews grants and roles
DBAs review physical design, indexes, and statistics
data modellers review tables and relationships
BI teams review reporting views
application teams review procedures and macros
platform teams review cross-platform access objects
```

Atomic scripts allow review to be routed to the right people.

Large multi-object scripts make this harder because reviewers are forced to inspect unrelated changes that may sit outside their area of responsibility.

Object-level scripts support cleaner ownership and more meaningful approval.

---

## 14. Encourages consistent standards

Atomic scripts make standards easier to enforce.

Each script can be checked for:

- correct file name
- correct file extension
- correct folder location
- correct object type
- correct database qualification
- required header block
- statement termination
- naming convention compliance
- absence of unrelated object definitions
- absence of mixed deployment concerns

For example, a view file should not unexpectedly contain a table definition, grant statement, unrelated macro, or ad hoc data correction.

This makes the codebase more predictable and maintainable.

---

## 15. Reduced merge conflicts

Large shared deployment scripts are prone to source-control conflicts.

If several developers edit the same release file, they are likely to collide:

```text
release_2026_05_ddl.sql
```

With atomic scripts, developers usually modify different files:

```text
customer_access.customer.viw
finance_access.account_balance.viw
risk_model.calculate_pd.spl
```

This reduces merge conflicts and improves development velocity.

It also makes branch comparison and release cherry-picking easier.

---

## 16. Better support for generated code

Many database artefacts are generated from metadata.

Examples include:

- veneer views
- compatibility views
- QueryGrid views
- foreign table wrappers
- grants
- comments
- object extraction scripts
- migration scripts
- deployment manifests

Atomic output allows a generator to create, refresh, compare, and deploy each generated object independently.

For example:

```text
customer_veneer.customer.viw
customer_veneer.account.viw
customer_veneer.transaction.viw
```

If only one source object changes, only one generated file needs to change.

This makes generated code easier to review and less likely to introduce unnecessary noise into source control.

---

## 17. Support for manifest-driven deployment

Atomic scripts work naturally with deployment manifests.

A manifest can record each object as a deployment item:

```text
sequence, object_type, database_name, object_name, script_path, dependency_level
1, table, customer_core, customer, payload/ddl/tables/customer_core.customer.tbl, 1
2, view, customer_access, customer, payload/ddl/views/customer_access.customer.viw, 2
3, grant, customer_access, customer, payload/dcl/grants/customer_access.customer.grt, 3
```

The manifest becomes the contract between source control, packaging, deployment, and audit.

It can also be reviewed before deployment to confirm exactly what will be executed.

---

## 18. Better rollback and recovery planning

Database rollback is often complex.

It may involve restoring a previous object definition, dropping a newly created object, recreating a previous grant set, or applying a compensating change.

Atomic scripts make rollback planning clearer because the recovery boundary is object-level.

For example:

```text
restore previous definition of customer_access.customer.viw
drop newly created risk_model.experimental_score.tbl
reapply previous grants for finance_access.account_balance.viw
```

Even where rollback remains manual, atomic packaging makes it easier to identify the affected objects and prepare targeted recovery actions.

---

## 19. Cleaner release notes and change reporting

Atomic scripts make release documentation easier to generate.

A release can clearly list what was added, changed, removed, or skipped:

```text
added:
  customer_core.customer_status.tbl
  customer_access.customer_status.viw

changed:
  customer_access.customer.viw
  risk_model.calculate_pd.spl

removed:
  legacy_reporting.old_customer_summary.viw
```

This is useful for:

- release notes
- change advisory board packs
- business stakeholder summaries
- production support handover
- implementation plans
- post-implementation review

Object-level release reporting is much more meaningful than simply saying a deployment script was run.

---

## 20. Alignment with modern software engineering practice

Application code is not usually maintained as one giant source file.

It is modularised into files, classes, functions, packages, and deployable components. This improves maintainability, testing, ownership, review, and release management.

Database code should be treated with the same discipline.

Eponymous, atomic scripts make database code behave like a managed software asset rather than an unmanaged deployment artefact.

They support the same engineering principles used elsewhere:

- modularity
- traceability
- version control
- automated validation
- repeatable deployment
- separation of concerns
- controlled release packaging
- object-level ownership

---

## Practical naming convention

A practical naming convention should make the object identity obvious from the file name.

For example:

```text
<database_name>.<object_name>.<extension>
```

Examples:

```text
customer_core.customer.tbl
customer_access.customer.viw
risk_model.calculate_pd.spl
security_app.customer_access.grt
```

The extension should identify the object or artefact type. The case for object-type extensions over generic `.sql` is made in detail in the companion note [Object-Type Extensions for Database Scripts](./object-type-extensions.md).

Example extensions could include:

```text
.tbl   table
.viw   view
.mcr   macro
.spl   stored procedure
.fnc   function
.trg   trigger
.jix   join index
.grt   grant
.cmt   comment
.stt   statistics
```

The exact extensions are less important than consistency, predictability, and tooling support.

---

## Recommended packaging structure

A simple packaging structure may look like this:

```text
payload/
  ddl/
    databases/
    tables/
    views/
    macros/
    procedures/
    functions/
    triggers/
    indexes/
  dcl/
    roles/
    users/
    grants/
  metadata/
    comments/
    statistics/
  manifests/
    deployment_manifest.csv
  logs/
```

This structure separates object definitions, security artefacts, metadata operations, manifests, and deployment logs.

It also helps deployment tooling apply the correct ordering and handling rules for each category.

---

## Important caveats

Eponymous, atomic scripts are a strong default pattern, but there are cases where a script may need to contain more than one statement.

For example, a single-object script may reasonably include:

- the main object definition
- comments for that object
- grants for that object
- statistics for that object
- cleanup statements directly related to that object
- deployment guards or validation checks

The key principle is that the file should still have a single primary object or a single clear deployment purpose.

The pattern should avoid unrelated objects being bundled together simply because they happen to be part of the same release.

The most important real-world deviation is **DML**, where a single script may legitimately touch many tables and where the harvester often cannot identify a single primary target with high confidence. The companion note [DML Naming in an Atomic Database Script Framework](./dml-naming.md) extends the eponymous principle to handle that case without overclaiming certainty in the filename.

---

## Defensible summary

Eponymous, atomic database scripts improve the engineering discipline around database code.

They provide clear object ownership, cleaner source control, stronger auditability, better dependency analysis, safer deployment, simpler restartability, more reliable automation, easier environment comparison, and better migration support.

They also reduce merge conflicts, improve review quality, support manifest-driven deployment, enable parallel execution where safe, and align database development with modern software engineering practices.

The core argument is:

> Eponymous, atomic database scripts improve traceability, reviewability, deployability, restartability, automation, impact analysis, and audit control by ensuring each database object has a single, named, version-controlled source artefact.

A shorter version is:

> One object, one file, one name, one deployment unit.

Or, more simply:

> Object-level source control for database code.
---

## Citations and supporting references

The practice of storing database code as eponymous, atomic scripts is consistent with established database source-control guidance. Redgate describes database source-control strategies in which database objects are scripted out as a file per object, files take their names from the objects they represent, and the folder structure mirrors the structure of the database. Steven Feuerstein's *Oracle PL/SQL Best Practices* explicitly treats meaningful naming conventions for source files as a PL/SQL best practice. Oracle SQL Developer/Data Modeler guidance, Flyway repeatable migrations, Liquibase/Datical SCM packaging, Microsoft SQL Database Projects, and Ora2Pg all provide or document workflows based on object-level source files. Together, these references support treating each database object as a discrete, named, version-controlled software artefact rather than burying database definitions in anonymous monolithic scripts.

The exact phrase **"eponymous, atomic scripts"** is not commonly used in the literature. The same underlying practice is usually discussed using terms such as **"one file per object"**, **"file per object"**, **"single file in version control"**, **"meaningful source-file naming"**, **"object-level source scripts"**, and **"database source control"**.

### Directly relevant database-source-control references

1. Steven Feuerstein, *Oracle PL/SQL Best Practices*, O'Reilly, 2001. See STYL-10, "Adopt meaningful naming conventions for source files." This is a direct PL/SQL-specific reference supporting deliberate source-file naming for database code.

2. Redgate, *A strategy for implementing database source control*. This article describes a repository structure where database objects are scripted as a file per object, files take their names from the objects they represent, and folder structure mirrors database object structure.  
   URL: https://www.red-gate.com/hub/product-learning/sql-source-control/strategy-implementing-database-source-control

3. Redgate, *SQL Source Control*. Redgate describes SQL Source Control as scripting each database object into a file in version control, allowing teams to share work and access the history of changes to each object.  
   URL: https://www.red-gate.com/products/sql-source-control/

4. Redgate, *SQL Server Source Control Basics*. This source-control guide explains the value of maintaining change history for files in a project and is commonly cited in the context of object-level database source control.  
   URL: https://www.red-gate.com/simple-talk/resources/books/simple-talk-published-books/sql-server-source-control-basics/

5. Redgate, *Building a Database Directly from Object-level Source Scripts*. This article discusses building a database from object-level source scripts and using table/object manifests as ordered lists for build and deployment processes.  
   URL: https://www.red-gate.com/hub/product-learning/sql-compare/building-a-database-directly-from-object-level-source-scripts

6. Redgate Flyway, *Tutorial: Repeatable Migrations*. Flyway describes repeatable migrations as useful for database objects whose definitions can be maintained in a single file in version control, including views, procedures, functions, and packages.  
   URL: https://documentation.red-gate.com/flyway/reference/tutorials/tutorial-repeatable-migrations

7. Microsoft, *What are SQL Database Projects?* Microsoft describes a SQL database project as a local representation of SQL objects that comprise a database schema, such as tables, stored procedures, and functions, and connects the model to CI/CD workflows.  
   URL: https://learn.microsoft.com/en-us/sql/tools/sql-database-projects/sql-database-projects

8. Jeff Smith, *Data Modeler Tip: Assigning a SCHEMA to Your Objects*. This Oracle SQL Developer/Data Modeler article advises source-controlling generated DDL, preferably one file per object.  
   URL: https://www.thatjeffsmith.com/archive/2018/10/data-modeler-tip-assigning-a-schema-to-your-objects/

9. Jeff Smith, *How to Generate Schema DDL Scripts with One File Per Table Using SQL Developer*. This article describes Oracle SQL Developer Data Modeler support for generating one file per table with dependent objects.  
   URL: https://www.thatjeffsmith.com/archive/2014/06/how-to-generate-schema-ddl-scripts-with-one-file-per-table-using-sql-developer/

10. Liquibase/Datical, *Developer SCM Packaging*. This guidance states that developers can maintain procedures, functions, views, and triggers in their respective folders with one object per file, and also separates DML into a data-specific folder.  
    URL: https://datical-cs.atlassian.net/wiki/spaces/DDKB/pages/1069365/User%2BGuide%3A%2BDeveloper%2BSCM%2BPackaging

11. Ora2Pg documentation. Ora2Pg provides a `FILE_PER_FUNCTION` option allowing functions, procedures, and triggers to be saved as one file per object, with filenames derived from object names. This is especially relevant to migration and harvesting use cases.  
    URL: https://ora2pg.darold.net/documentation.html

12. Oracle SQLcl documentation, *Using Liquibase*. Oracle SQLcl's Liquibase support can generate changelogs for a single object or a full schema, supporting object-level treatment of database changes.  
    URL: https://docs.oracle.com/en/database/oracle/sql-developer-command-line/23.3/sqcug/using-liquibase.html

### Supporting software-engineering references

13. Brian W. Kernighan and Rob Pike, *The Practice of Programming*, Addison-Wesley, 1999. Their guidance on naming conventions emphasises informative, consistent, descriptive, and systematic names, which supports the general engineering case for meaningful source-file names.

14. Scott W. Ambler and Pramod J. Sadalage, *Refactoring Databases: Evolutionary Database Design*, Addison-Wesley, 2006. This book is not specifically about object-per-file naming, but it supports the broader principle that database schemas should evolve through disciplined, versioned, controlled change.

### How these references support this standard

These references do not all prescribe the same naming convention or file extension taxonomy. However, they consistently support the underlying principles used in this standard:

- database objects should be treated as source-controlled software artefacts
- object definitions benefit from being represented as discrete files
- meaningful file names improve traceability and maintainability
- object-level files support review, comparison, version history, deployment automation, and auditability
- object-type folders or classifications help organise deployment logic
- repeatable object definitions are well suited to single-file version-control patterns

The defensible conclusion is that eponymous, atomic database scripts are not merely a local stylistic preference. They are a practical expression of established source-control, naming, database DevOps, and migration-tooling practices.

