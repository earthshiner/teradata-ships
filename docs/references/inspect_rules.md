# Inspect Rules Reference

Authoritative reference for all SHIPS Inspect (Coding Discipline) rules.
Rules are configured via `inspect.conf` in the project root.

---

## Structural Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `db_qualifier`             | ERROR            | Object must use `Database.ObjectName` syntax.                                |
| `set_multiset`             | WARNING          | `CREATE TABLE` must specify `SET` or `MULTISET`.                             |
| `deploy_intent`            | OFF              | Retired compatibility rule. `REPLACE` and `CREATE` are both supported for replaceable Teradata objects; the deployer records the source verb and captures rollback snapshots before executing either path. |
| `one_object`               | WARNING          | Each file must contain exactly one DDL statement.                            |
| `eponymous`                | WARNING          | Filename must match the DDL object name.                                     |
| `extension`                | ERROR            | File extension must match the object type.                                   |
| `type_suffix`              | ERROR            | Object names must not carry type suffixes (`_V`, `_T`, `VW_`, `SP_`, etc.). |
| `ddl_terminator`           | ERROR            | Every DDL statement must terminate with a semi-colon (`;`). Missing terminators make statement boundaries ambiguous for deployment scripting and downstream agents. |
| `zero_tokens`              | ERROR            | Every deployable DDL/DML object must have a database qualifier (`Database.Object` or `{{TOKEN}}.Object`). Files with no qualifier cannot be tokenised by SHIPS and cannot be safely promoted across environments. |

---

## Style Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `hardcoded_name`           | WARNING          | Database names should use `{{TOKENS}}` for environment portability.          |
| `keyword_case`             | WARNING          | SQL keywords must be UPPERCASE.                                              |
| `comma_style`              | WARNING          | Comma placement must follow the configured style (default: `leading`). Set `comma_style=trailing` or `comma_style=as-per-source` in `inspect.conf` to change. |

---

## Object Placement Rules

| Code                        | Default Severity | Description                                                                 |
|-----------------------------|------------------|-----------------------------------------------------------------------------|
| `object_placement`          | ERROR            | Views must not reference tables databases directly.                          |
| `view_macro_self_reference` | ERROR            | A view or macro must not reference its own fully qualified name.             |

---

## Agent-Friendliness Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `view_column_list`         | WARNING          | `CREATE VIEW` must declare an explicit column list before `AS` — e.g. `CREATE VIEW db.MyView (Col1, Col2) AS ...`. Without an explicit column list the view's schema contract is implicit; agents and tooling must query the live database (`HELP VIEW` / `DBC.ColumnsV`) to discover column names. Promote to `ERROR` in agent-heavy environments. |

---

## Grant Architecture Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `public_grant_on_tables`   | WARNING          | `GRANT … TO PUBLIC` on a tables database bypasses the placement architecture.|
| `review_unmapped_grants`   | WARNING          | GRANT targets a database not in the placement map.                           |

---

## Cross-File Structural Rules

| Code                        | Default Severity | Description                                                                 |
|-----------------------------|------------------|-----------------------------------------------------------------------------|
| `intra_package_dependency`  | OFF              | Object lives in a database CREATEd elsewhere in the same package.           |

---

## Perm Space Rules

These rules are produced by the static perm-space analyser (`perm_analyser.py`) which runs
at Inspect time. No live Teradata connection is required — the analysis is based entirely on
PERM declarations in `.db` and `.usr` files and object counts in the payload.

The estimated footprint is a **conservative single-AMP floor** — actual consumption depends
on data volume and AMP count. The live preflight (`ships deploy`) performs a real-time check
via `DBC.DiskSpaceV` with skew correction.

**Object type floors used in the estimate:**

| Type | Floor |
|---|---|
| `TABLE`, `JOIN_INDEX`, `HASH_INDEX` | 512 KB per object |
| `PROCEDURE`, `FUNCTION`, `TRIGGER` | 128 KB per object |
| `VIEW`, `MACRO`, `STATISTICS`, `COMMENT` | 0 bytes (no perm consumed) |

| Code                        | Default Severity | Description                                                                 |
|-----------------------------|------------------|-----------------------------------------------------------------------------|
| `PERM_SPACE_INSUFFICIENT`   | ERROR            | Estimated object footprint exceeds the PERM declared for the target database in the package's `.db` or `.usr` file. Increase the PERM allocation or reduce the number of space-consuming objects before deploying. |
| `PERM_SPACE_LOW`            | WARNING          | Estimated headroom is below 20% of declared PERM. Deployment will likely succeed but leaves little room for data growth. Consider increasing the PERM allocation. |

---

## Security Rules

| Code                        | Severity | Description                                                                 | Gap     |
|-----------------------------|----------|-----------------------------------------------------------------------------|---------|
| `SECRET_PATTERN_DETECTED`   | ERROR    | Embedded credentials or secret patterns found in DDL/DML file bodies.       | GAP-003 |
| `DYNAMIC_SQL_DETECTED`      | WARNING  | Dynamic SQL constructs (`EXECUTE IMMEDIATE`, `DBC.SYSEXECSQL`) detected.    | GAP-008 |
| `VAULT_REF_UNRESOLVED`      | ERROR    | Unresolved `$env:` or `vault:` prefix found in payload after Harvest.       | GAP-011 |

---

## Data Governance Rules

| Code                        | Severity         | Description                                                                 | Gap     |
|-----------------------------|------------------|-----------------------------------------------------------------------------|---------|
| `MISSING_SENSITIVITY_CLASS` | WARNING / ERROR  | DDL/view object lacks a companion `.cls` sensitivity classification file.   | GAP-009 |
| `INVALID_SENSITIVITY_CLASS` | ERROR            | `.cls` file contains an unrecognised sensitivity class token.               | GAP-009 |
