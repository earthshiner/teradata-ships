# Why Use Object-Type Extensions for Database Scripts?

> **SHIPS context.** This is one of three SHIPS design-rationale notes — see the [Design Rationale index](./README.md) for navigation. The companion notes are [Eponymous, Atomic Database Scripts](./eponymous-atomic-scripts.md) (which covers the broader "one object, one file" principle this note builds on) and [DML Naming in an Atomic Database Script Framework](./dml-naming.md) (which covers when an eponymous filename would claim more certainty than the harvester actually has).

## Purpose

This note explains the functional reasons for using object-type file extensions for governed database source code, especially when database objects are maintained as [eponymous, atomic scripts](./eponymous-atomic-scripts.md).

It also plays devil's advocate, because using `.sql` for everything is common, familiar, and often perfectly reasonable.

---

## Devil's advocate: why `.sql` is reasonable

The strongest argument for `.sql` is simplicity.

Most database code is SQL or SQL-adjacent, so teams often use names like:

```text
customer_view.sql
account_table.sql
load_customer.sql
grant_access.sql
collect_statistics.sql
```

That convention has real advantages:

- every editor recognises `.sql`
- syntax highlighting usually works automatically
- DBAs and developers immediately understand the file type
- CI/CD tools often expect `.sql`
- it avoids inventing a project-specific extension taxonomy
- it reduces onboarding friction
- it does not require people to memorise extensions such as `.viw`, `.tbl`, `.mcr`, `.grt`, and `.stt`

The pragmatic argument is:

> The object type should be visible from the folder path, file name, or file contents. The extension does not need to carry that meaning.

For example:

```text
ddl/views/customer_access.customer.sql
ddl/tables/customer_core.customer.sql
dcl/grants/customer_access.customer.sql
statistics/customer_core.customer.sql
```

That is a reasonable convention. It is not wrong.

The other good argument is portability. A `.sql` file is a neutral database artefact. A custom extension may be meaningful inside one toolchain, but less obvious to a customer, DBA, GitHub viewer, code editor, or deployment platform.

So the devil's advocate position is:

> Use `.sql` everywhere unless the toolchain has a concrete reason to distinguish object types from the file name alone.

That is a fair challenge.

---

# Why use object-type extensions?

The defensible reason is not aesthetics. It is that the extension becomes **machine-readable metadata**.

An object-type extension lets the deployment framework classify the artefact without opening and parsing the file.

For example:

```text
customer_core.customer.tbl
customer_access.customer.viw
risk_model.calculate_pd.spl
customer_access.customer.grt
customer_core.customer.stt
```

The extension tells the tool what kind of thing the file represents before reading the content.

That helps with packaging, validation, sorting, dependency handling, reporting, review, and safety.

---

## 1. It gives immediate object classification

With `.sql`, the tool has to infer intent from somewhere else:

```text
customer.sql
customer_view.sql
customer_table.sql
customer_ddl.sql
```

That may be obvious to a person, but less reliable for automation.

With object-type extensions:

```text
customer.tbl
customer.viw
customer.stt
customer.grt
```

The type is explicit and consistently located.

The tool can apply a simple mapping:

```text
.tbl = table
.viw = view
.stt = statistics
.grt = grant
.spl = stored procedure
.mcr = macro
```

That is easier to enforce than relying on variable naming suffixes or parsing logic.

---

## 2. It separates SQL language from deployment intent

The extension `.sql` tells you the language.

It does **not** tell you the deployment intent.

A file containing any of these could be called `.sql`:

```sql
replace view ...;
create table ...;
grant select ...;
collect statistics ...;
insert into ...;
delete from ...;
call ...;
```

Those statements have very different risk profiles.

Object-type extensions encode the intended deployment class:

```text
.tbl    structural DDL
.viw    replaceable view DDL
.grt    privilege DCL
.stt    statistics operation
.dml    data change
.bteq   executable script wrapper
```

That distinction matters because DDL, DCL, DML, and operational SQL should often be reviewed, sequenced, tested, approved, and executed differently. The `.dml` case in particular has subtleties — see [DML Naming in an Atomic Database Script Framework](./dml-naming.md) for when `.dml` should be paired with eponymous `<db>.<table>` naming and when it should fall back to source-file-based naming.

---

## 3. It prevents dangerous mixing

Using `.sql` for everything can quietly encourage mixed scripts:

```sql
create table ...;
insert into ...;
grant select ...;
collect statistics ...;
replace view ...;
delete from ...;
```

That may be convenient, but it weakens control.

Object-type extensions make mixed intent stand out. If a `.tbl` file contains grants, inserts, deletes, statistics collection, and view definitions, that is a standards violation.

That gives the deployment framework something concrete to validate:

```text
.tbl should contain table DDL
.viw should contain view DDL
.grt should contain grants
.stt should contain statistics
```

Exceptions can still be allowed, but they become explicit exceptions rather than accidental habits.

---

## 4. It improves deployment ordering

Object type is one of the main inputs to deployment order.

A simple deployment order might be:

```text
.db
.user
.rol
.prf
.auth
.fsvr
.tbl
.viw
.mcr
.spl
.grt
.stt
.cmt
```

If everything is `.sql`, the tool has to determine type by:

- parsing file contents
- trusting folder names
- trusting naming suffixes
- reading a manifest
- using fragile pattern matching

Object-type extensions allow simple, fast ordering before deeper parsing.

That does not replace dependency analysis, but it gives the deployment engine a strong first pass.

---

## 5. It makes manifests cleaner

A manifest generated from object-type extensions can be clean and deterministic:

```text
script_path,object_type,database_name,object_name
payload/ddl/tables/customer_core.customer.tbl,table,customer_core,customer
payload/ddl/views/customer_access.customer.viw,view,customer_access,customer
payload/dcl/grants/customer_access.customer.grt,grant,customer_access,customer
payload/ddl/statistics/customer_core.customer.stt,statistics,customer_core,customer
```

With `.sql`, the manifest may still be possible, but the object type has to come from somewhere else.

That "somewhere else" becomes another possible failure point.

---

## 6. It supports object-specific validation

Different object types have different validation rules.

A `.viw` file can be checked for:

- one `replace view` or `create view`
- expected qualified view name
- no DML
- no unrelated grants
- no statistics collection

A `.tbl` file can be checked for:

- table DDL
- correct database qualification
- no procedural logic
- expected table kind
- optional primary index or partitioning standards

A `.grt` file can be checked for:

- only grant or revoke-style statements
- no object-creation statements
- no destructive DDL

A `.stt` file can be checked for:

- only statistics-related statements
- correct target table or index
- no object definition changes

This makes standards enforcement much more practical.

---

## 7. It helps packaging and selective deployment

Object-type extensions make it easy to package or deploy subsets.

Examples:

```text
deploy only .viw files
deploy all .grt files after object creation
exclude .stt files from emergency deployment
package .tbl and .viw but not .dml
run .stt files in a post-deployment wave
send .grt files to security review
```

This can also be done with folder names, but extensions make the rule portable even if files are moved, copied, zipped, or flattened.

---

## 8. It makes code review more targeted

Reviewers can filter by extension.

A DBA may care about:

```text
.tbl
.jix
.hix
.stt
```

A security reviewer may care about:

```text
.grt
.rol
.user
.prf
```

A data modeller may care about:

```text
.tbl
.viw
.fk
.cmt
```

A migration engineer may care about:

```text
.fsvr
.auth
.viw
.spl
```

With `.sql`, the team relies more heavily on folder discipline or naming suffixes.

---

## 9. It improves reporting

A deployment summary can group naturally by extension-derived type:

```text
tables: 421
views: 1,248
macros: 37
procedures: 18
grants: 612
statistics: 403
comments: 1,990
```

That sounds minor, but it is useful in migration, governance, and operational reporting.

It also helps answer questions like:

> How many view definitions changed in this release?

or:

> Are there any DCL changes in this package?

---

## 10. It reduces reliance on parsing

Parsing SQL is harder than people think.

Even detecting object type reliably can be awkward because of:

- comments
- conditional execution
- BTEQ commands
- database-specific syntax
- `replace` versus `create`
- stored procedure bodies
- nested SQL
- dynamic SQL
- quoted identifiers
- multiple statements per file
- vendor-specific grammar quirks

If the extension already tells you the expected object class, parsing becomes validation, not discovery.

That is a big difference.

---

# Why not `_view.sql`, `_table.sql`, `_statistics.sql`?

This is a reasonable compromise.

For example:

```text
customer_core.customer_view.sql
customer_core.customer_table.sql
customer_core.customer_statistics.sql
```

The benefit is that editors still recognise `.sql`.

The downside is that the type marker is now embedded in the filename rather than the extension.

---

## 1. The object name becomes polluted

If the file is supposed to be eponymous, this is less clean:

```text
customer_core.customer_view.sql
```

Is the object called:

```text
customer
```

or:

```text
customer_view
```

or:

```text
customer_core.customer_view
```

You can define rules, but now the tool has to strip suffixes.

With this:

```text
customer_core.customer.viw
```

The base name is the object name, and the extension is the type.

That is cleaner.

---

## 2. Suffixes are less standard than extensions

People may write:

```text
customer_view.sql
customer.views.sql
customer.vw.sql
customer.viw.sql
customer__view.sql
customer_view_ddl.sql
```

Extensions are easier to constrain.

A file either ends in `.viw` or it does not.

---

## 3. It gets clumsy for object names that already contain type-like words

This can become awkward:

```text
customer_view_view.sql
table_usage_table.sql
statistics_summary_statistics.sql
grant_history_grant.sql
```

With object-type extensions:

```text
customer_view.viw
table_usage.tbl
statistics_summary.viw
grant_history.tbl
```

The object name remains natural.

---

# Why not `.ddl` for tables and views?

`.ddl` is better than `.sql` if the goal is to separate schema definition from general SQL.

For example:

```text
customer_core.customer.ddl
customer_access.customer.ddl
```

But `.ddl` still does not distinguish between:

```text
table
view
macro
procedure
function
trigger
join index
hash index
foreign server
authorization
comment
statistics
```

It says "definition language", but not "which deployable object type".

So `.ddl` is useful at the broad category level, but less useful for object-level automation.

---

# The strongest argument against object-type extensions

The strongest argument against them is that they are non-standard.

A new team member may not immediately know what these mean:

```text
.viw
.mcr
.stt
.cmt
.fsvr
.auth
.spl
```

Some tools may not automatically syntax-highlight them as SQL. Some deployment systems may need configuration. Some people may consider the convention too bespoke.

That is a real cost.

So the standard needs to be documented and enforced. Otherwise, custom extensions become another source of confusion.

---

# The defensible position

The best position is not:

> `.sql` is wrong.

The defensible position is:

> `.sql` is fine for general SQL scripts, ad hoc SQL, mixed operational scripts, or simple projects. But for governed database source code, especially in migration and automated deployment frameworks, object-type extensions provide useful metadata that improves classification, validation, sequencing, packaging, review, and reporting.

So the distinction is:

```text
.sql    general-purpose SQL script
.tbl    table definition artefact
.viw    view definition artefact
.grt    grant/security artefact
.stt    statistics artefact
.spl    stored procedure artefact
.mcr    macro artefact
.cmt    comment artefact
```

In other words:

> `.sql` describes the language.  
> `.viw`, `.tbl`, `.grt`, and `.stt` describe the deployable artefact.

That is the key argument.

---

# Practical policy

A balanced standard could be:

> Use object-type extensions for governed, object-level deployable artefacts.

Use `.sql` for:

- ad hoc SQL
- one-off support scripts
- investigation queries
- data repair scripts
- release orchestration scripts
- mixed-purpose scripts where object-level classification is not appropriate

Use `.bteq`, `.sh`, `.ps1`, or equivalent for executable wrappers.

This avoids being dogmatic while still giving the deployment framework useful metadata.

---

# Governance statement

Object-level database source files should use object-type extensions rather than the generic `.sql` extension. This allows deployment tooling to classify, validate, sequence, package, and report database artefacts without relying solely on SQL parsing, folder location, or naming suffixes. The `.sql` extension should be reserved for general-purpose SQL, ad hoc queries, mixed operational scripts, or release orchestration where the file is not intended to represent a single deployable database object.

---

# Short version

> `.sql` tells you it contains SQL.  
> An object-type extension tells you what is safe to do with it.
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

