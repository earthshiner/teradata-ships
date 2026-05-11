# Inspect Rules Reference

Authoritative reference for all SHIPS Inspect (Coding Discipline) rules.
Rules are configured via `inspect.conf` in the project root.

---

## Structural Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `db_qualifier`             | ERROR            | Object must use `Database.ObjectName` syntax.                                |
| `set_multiset`             | WARNING          | `CREATE TABLE` must specify `SET` or `MULTISET`.                             |
| `deploy_intent`            | ERROR            | `REPLACE` is prohibited — use `CREATE`. The deployer owns idempotency.       |
| `one_object`               | WARNING          | Each file must contain exactly one DDL statement.                            |
| `eponymous`                | WARNING          | Filename must match the DDL object name.                                     |
| `extension`                | ERROR            | File extension must match the object type.                                   |
| `type_suffix`              | ERROR            | Object names must not carry type suffixes (`_V`, `_T`, `VW_`, `SP_`, etc.). |

---

## Style Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `hardcoded_name`           | WARNING          | Database names should use `{{TOKENS}}` for environment portability.          |
| `keyword_case`             | WARNING          | SQL keywords must be UPPERCASE.                                              |
| `comma_style`              | WARNING          | Comma placement must follow the configured style (default: leading).         |

---

## Object Placement Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `object_placement`         | ERROR            | Views must not reference tables databases directly.                           |
| `view_macro_self_reference`| ERROR            | A view or macro must not reference its own fully qualified name.             |

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
