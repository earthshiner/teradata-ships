# Inspect Rules Reference

Full set of Coding Discipline rules enforced by `ships inspect`. Each rule has a machine code
used in `inspect_report.json`. Rules are grouped by category.

---

## Object Naming Rules

| Code                  | Rule                                                                               |
|-----------------------|------------------------------------------------------------------------------------|
| `OBJECT_TYPE_SUFFIX`  | Object name MUST NOT end with `_V`, `_T`, `_P`, `_M`, `_SP`, `_JI`, `_TR`        |
| `OBJECT_TYPE_PREFIX`  | Object name MUST NOT begin with `VW_`, `TBL_`, `SP_`, `PR_`, `MAC_`               |
| `NAME_UPPER`          | Object names MUST be uppercase or consistently cased per convention                |
| `DB_QUALIFIER`        | Environment-scope objects MUST carry a database qualifier (token-resolved)         |
| `SYSTEM_NO_QUALIFIER` | System-scope objects (`00_system`) MUST NOT carry a database qualifier             |

---

## File Structure Rules

| Code                  | Rule                                                                               |
|-----------------------|------------------------------------------------------------------------------------|
| `ATOMIC_FILE`         | Each file MUST contain exactly one CREATE / REPLACE statement                      |
| `EPONYMOUS_FILE`      | Filename (without extension) MUST match the object name exactly                    |
| `DIR_SEPARATION`      | DDL objects (`ddl/`), views (`viw/`), and DML (`dml/`) MUST be in separate dirs   |
| `MISSING_STATS_FILE`  | Every table file MUST have a companion `.stt` file in the same directory           |
| `NO_SYSTEM_IN_ENV`    | System-scope objects MUST NOT appear under environment wave directories            |

---

## Token Rules

| Code                    | Rule                                                                             |
|-------------------------|----------------------------------------------------------------------------------|
| `UNRESOLVED_TOKEN`      | No `{{…}}` placeholder may remain in the tokenised payload after Harvest         |
| `TOKEN_KEY_UPPER`       | Token keys in `token_map.conf` MUST be UPPERCASE                                 |
| `TOKEN_NO_SPACE`        | Token keys MUST NOT contain spaces                                               |
| `TOKEN_BRACE_FORMAT`    | Tokens in source DDL MUST use double-brace format: `{{TOKEN_NAME}}`              |

---

## DDL Syntax Rules

| Code                    | Rule                                                                             |
|-------------------------|----------------------------------------------------------------------------------|
| `MULTISET_REQUIRED`     | All `CREATE TABLE` statements MUST specify `MULTISET`                            |
| `NO_SELECT_STAR`        | Views MUST NOT use `SELECT *` — all columns must be explicitly named             |
| `REPLACE_VIEW_REQUIRED` | Views targeted for update MUST use `REPLACE VIEW`, not `CREATE VIEW`             |
| `KEYWORD_UPPER`         | All SQL keywords MUST be UPPERCASE                                               |
| `LEADING_COMMA`         | Multi-column lists MUST use leading commas (`, column_name`) not trailing        |
| `SPACES_ONLY`           | DDL files MUST use spaces only — no tab characters                               |

---

## DML Syntax Rules

| Code                   | Rule                                                                              |
|------------------------|-----------------------------------------------------------------------------------|
| `SINGLE_ROW_INSERT`    | Each INSERT MUST be a separate `INSERT INTO … VALUES (…);` statement             |
| `NO_MULTIROW_VALUES`   | Comma-separated multi-row VALUES blocks are FORBIDDEN                            |

---

## Grant / DCL Rules

| Code                   | Rule                                                                              |
|------------------------|-----------------------------------------------------------------------------------|
| `GRANT_DB_EXISTS`      | Grant targets (databases) MUST appear in an earlier wave than their grants       |
| `GRANT_ROLE_EXISTS`    | Roles granted TO MUST exist in `00_system` or a prior wave                       |
| `GRANT_FORMAT`         | GRANT statements MUST follow the canonical form from `infer_grants.py` output    |
| `NO_GRANT_IN_DDL`      | GRANT statements MUST NOT appear inside DDL or DML files — grants live in `dcl/` |

---

## Properties File Rules

| Code                      | Rule                                                                           |
|---------------------------|--------------------------------------------------------------------------------|
| `PROPS_KEY_PRESENT`       | All keys referenced in `ships.yaml` MUST be present in the properties file    |
| `PROPS_NO_PLAINTEXT_CRED` | Passwords and credentials MUST NOT appear in properties files as plaintext     |
| `PROPS_ENV_MATCH`         | Properties file environment label MUST match the `--env` flag passed to Package|

---

## Structural Integrity Rules

| Code                       | Rule                                                                          |
|----------------------------|-------------------------------------------------------------------------------|
| `WAVE_COMPLETENESS`        | Every object referenced in the manifest MUST have a corresponding file        |
| `MANIFEST_OBJECT_MATCH`    | Every file in the payload MUST correspond to exactly one manifest entry       |
| `GLOBAL_TEMP_MAXTEMP`      | Any GLOBAL TEMPORARY table in the payload MUST trigger a MaxTemp preflight check |
| `CIRCULAR_DEPENDENCY`      | No circular DDL dependency may exist within a single wave                     |

---

## How Inspect Invokes Rules

Rules are applied in category order:
1. Token rules (fail-fast if unresolved tokens remain)
2. File structure rules
3. Object naming rules
4. DDL / DML syntax rules
5. Grant / DCL rules
6. Properties file rules
7. Structural integrity rules

A single `ERROR`-level violation causes `inspect` to exit non-zero. `WARNING`-level findings are reported but do not block Package. Each violation entry in `inspect_report.json` carries: `code`, `severity` (`ERROR` | `WARNING`), `file`, `line`, `message`.

---

## Adding a New Inspect Rule

1. Add the rule code and description to this reference file first.
2. Implement the checker in `td_release_packager/inspect/rules/<category>.py`.
3. Register the rule in `td_release_packager/inspect/rule_registry.py`.
4. Add a unit test in `tests/inspect/test_<category>.py` covering pass + fail cases.
5. Re-run the full test suite (`pytest`) — all 524+ tests must remain green.
