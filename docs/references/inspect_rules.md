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
| `eponymous`                | WARNING          | Filename must match the DDL object name. Works on tokenised payloads via the canonical `derive_filename` — a body declaring `CREATE TABLE {{DB_PREFIX}}_T.Customer` must live in `{{DB_PREFIX}}_T.Customer.tbl`. (#365) |
| `filename_token_format`    | ERROR            | Every `{{…}}` marker in a filename must be a well-formed token. Orphan `{{` or `}}` pairs in the name are package-blocking: the build cannot substitute a malformed token and the file would land on an unintended path. One finding per malformed marker. (#365) |
| `extension`                | ERROR            | File extension must match the object type.                                   |
| `type_suffix`              | ERROR            | Object names must not carry type suffixes (`_V`, `_T`, `VW_`, `SP_`, etc.). |
| `ddl_terminator`           | ERROR            | Every DDL statement must terminate with a semi-colon (`;`). Missing terminators make statement boundaries ambiguous for deployment scripting and downstream agents. |
| `zero_tokens`              | ERROR            | Every deployable DDL/DML object must have a database qualifier (`Database.Object` or `{{TOKEN}}.Object`). Files with no qualifier cannot be tokenised by SHIPS and cannot be safely promoted across environments. |
| `comment_length`           | ERROR            | The text body of a `COMMENT ON … IS '…'` statement must not exceed 254 characters. Teradata raises Error 5550 ("Comment string is longer than permitted") at deploy time when this limit is breached. Applies to `.cmt` files. |

---

## Style Rules

| Code                       | Default Severity | Description                                                                  |
|----------------------------|------------------|------------------------------------------------------------------------------|
| `hardcoded_name`           | WARNING          | Database names should use `{{TOKENS}}` for environment portability.          |
| `keyword_case`             | OFF              | SQL keywords prefer UPPERCASE. Off by default — most sites don't enforce the convention strictly. Opt in by setting `keyword_case=WARNING` (or ERROR / INFO) in `inspect.conf`. |
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
| `object_level_grant`       | WARNING          | `GRANT`/`REVOKE` targets a specific object (`ON db.obj`) or column (`GRANT SELECT (col) ON …`) rather than the containing database. Teradata best practice is to grant at the database level — privileges propagate to all objects in the container and the access surface stays auditable. Scoped to `.dcl`/`.grt` files only; embedded `GRANT` statements inside procedure bodies are not inspected. (#365) |

### Cross-file grant validation (Step 2 of Inspect)

Step 2 compares the grants *implied* by the package's DDL against the `.dcl` files persisted under `payload/database/DCL/inter_db/`. That directory is reserved for database-to-database grants. Role grants belong under `payload/database/DCL/roles/` and must not use `WITH GRANT OPTION` because Teradata does not allow grant option for roles. Three outcomes are possible per grantee:

| Outcome   | Severity | Meaning                                                              |
|-----------|----------|----------------------------------------------------------------------|
| Consistent | —       | Persisted `.dcl` matches what the DDL implies. No action needed.     |
| Drifted   | ERROR    | `.dcl` exists but its privilege set differs from the DDL implication. Run `--fix-grants` to append missing inferred grants. Extra grants are not removed automatically. |
| Missing   | ERROR    | DDL implies a grant but no `.dcl` file exists. Run `--fix-grants` to create it. |
| External  | INFO *   | A `.dcl` file exists for a grantee — role, database, or user — that no DDL in the package implies. The grantee is *external* to the package's intent. |

\* **External grants are reported at INFO by default** — they are commonly legitimate (e.g. a role granted access in this package whose `GRANT ROLE … TO USER` lives outside). See `inspect.warn_external_grants` below to change the severity.

#### `warn_extra_grants` — extra manual privileges treated as warnings

Set in `config/inspect.conf`:

```
warn_extra_grants=ERROR   # default: ERROR
# also supports WARNING, WARN, and OFF
```

When `ERROR` (the default), any `.dcl` file whose privilege set does not exactly match what SHIPS inferred from the DDL is treated as drift — a hard error that blocks the package.

When `WARNING` or `WARN`, drifted grantees whose `.dcl` files contain only *extra* privileges (grants you added manually beyond what SHIPS infers) are reported as warnings only and do not block packaging. When `OFF`, extra-only grant drift is suppressed. Role grants should normally live in `DCL/roles`, not in `DCL/inter_db`.

**Important:** this flag only applies to *extra* privileges. If a `.dcl` file is *missing* a privilege that SHIPS inferred from the DDL (i.e. the DDL is referencing access that has not been granted), that remains a hard error regardless of this setting.

#### `warn_external_grants` — configurable external-grant severity

Set in `config/inspect.conf`:

```
warn_external_grants=INFO   # default: INFO
# also supports WARNING, WARN, ERROR, and OFF
```

**Note on naming.** This rule was named `warn_orphan_grants` before 2026-06. The name was changed because "orphaned" implied operator action; the new term *external* reflects that the grantee — typically a role, database, or user — lives outside the package's DDL intent and the grant is commonly legitimate.

The old key is **not silently accepted** under the new release. An `inspect.conf` that still carries `warn_orphan_grants=…` raises a clear error at config-read time pointing to the new key and the line number — no silent inheritance of the new INFO default. Rename the line to `warn_external_grants=…` and re-run.

When `INFO` (the default), external-grant `.dcl` files are surfaced in the report and audit trail without blocking the build. This is the correct default when:

- A role is granted database access *within* the package (e.g. `GRANT SELECT ON {{DB_T}} TO {{READ_ROLE}}`), but the corresponding `GRANT ROLE … TO USER` statement is managed *outside* the package — for example, by a DBA, an IGA system, or an autonomous agent.
- The package deliberately pre-provisions access rights that a downstream process will activate.

When `WARNING` or `WARN`, external grants are reported as warnings. When `ERROR`, external grants cause the package to be **BLOCKED** — use this strict posture for fully self-contained packages where every grant must be traceable to in-package DDL. When `OFF`, external grants are suppressed entirely from the report.

Both settings can be set independently in `inspect.conf`. Missing grants (inferred by SHIPS but absent from the `.dcl` file) and missing `.dcl` files entirely remain hard errors regardless of either setting. Use `--fix-grants` to repair missing inferred grants additively: SHIPS appends the required `GRANT` statements to the correct `.dcl` file and leaves extra/external grants untouched for review.

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
