# Token Map Reference

> **Canonical surface (#383):** for *tokenising* a source tree, `config/tokenise.conf`
> (regex rules, applied to file contents **and** filenames) or `--prefix-token
> <Prefix>=DB_PREFIX` is the canonical path — see `docs/references/tokenisation.md`.
> The `--token-map` / `config/token_map.conf` mechanism described below still works
> but is the deprecated legacy path; prefer it only for existing projects already
> built around it. Environment value resolution at Package time still uses
> `config/env/<ENV>.conf` regardless of tokenisation mode.

The token map is the mechanism by which a single set of source DDL is made environment-specific
without duplicating files. Harvest reads the map and substitutes every `{{TOKEN_NAME}}` in the
source tree with its resolved value, writing the result to `payload/`.

---

## Token Map File Format

`config/token_map.conf` is a plain key-value file:

```conf
# Lines beginning with # are comments — ignored by Harvest
# Keys MUST be UPPERCASE. Values may be any string (no quoting needed).

ENV_PREFIX=DEV01
DB_DOMAIN=MortgagePlatformDomain
DB_SEMANTIC=MortgagePlatformSemantic
DB_SEARCH=MortgagePlatformSearch
DB_PREDICTION=MortgagePlatformPrediction
DB_OBSERVABILITY=MortgagePlatformObservability
DB_MEMORY=MortgagePlatformMemory
DB_CONTRACT=MortgagePlatformContract
PERM_SIZE=1e9
SPOOL_SIZE=5e8
FALLBACK=NO FALLBACK
```

---

## Token Format in Source DDL

In source DDL files, tokens use double-brace format:

```sql
CREATE DATABASE {{DB_DOMAIN}}
    FROM DBC
    AS PERM = {{PERM_SIZE}},
       SPOOL = {{SPOOL_SIZE}};
```

After Harvest with the map above, the payload file contains:

```sql
CREATE DATABASE MortgagePlatformDomain
    FROM DBC
    AS PERM = 1e9,
       SPOOL = 5e8;
```

---

## Token Map Rules

| Rule                    | Detail                                                                |
|-------------------------|-----------------------------------------------------------------------|
| Keys MUST be UPPERCASE  | `DB_DOMAIN` ✓ · `db_domain` ✗                                        |
| No spaces in keys       | `DB DOMAIN` is invalid — use underscores                             |
| No quoting of values    | Values are used verbatim — do not wrap in quotes unless the SQL needs them |
| One token per line      | Multi-value tokens are not supported                                  |
| Comments with `#`       | Only at the start of a line — inline `#` is treated as part of the value |

---

## Generating a Token Map Scaffold

On the first Harvest pass, use `--generate-token-map` to scan the source DDL and emit a scaffold:

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project ./projects/MyProject \
    --generate-token-map \
    --env-prefix DEV01
```

This writes `config/token_map.conf` with every unique `{{TOKEN_NAME}}` found in the source tree,
with placeholder values. Edit the file to supply real values before running Harvest again.

---

## Re-Harvest Rules

- Always use `--force` when re-harvesting — without it, Harvest refuses to overwrite existing payload files.
- After re-harvest, always re-run `ships inspect` before `ships package`. Never package a stale payload.
- The `_T` / `_V` token map convention (for tables vs. views in the same database family) must be resolved in the token map before the first Inspect pass. If `_T` and `_V` variants are needed, add explicit token entries for each:

```conf
DB_DOMAIN_T=MortgagePlatformDomain_T
DB_DOMAIN_V=MortgagePlatformDomain_V
```

---

## Multi-Environment Setup

Maintain one `token_map.conf` per environment under `config/`:

```
config/
├── token_map_DEV.conf
├── token_map_SIT.conf
└── token_map_PROD.conf
```

Pass the appropriate map to Harvest:

```bash
python -m td_release_packager harvest \
    --source /raw/ddl/ \
    --project ./projects/MyProject \
    --token-map config/token_map_SIT.conf \
    --force
```

Environment-specific connection config files live under `config/env/`:

```
config/
└── env/
    ├── DEV.conf
    ├── SIT.conf
    └── PRD.conf
```

The environment label in the env config file (`config/env/{ENV}.conf`) MUST match
the `--env` flag passed to Package. Inspect enforces this via the `PROPS_ENV_MATCH` rule.

---

## Common Token Map Errors

| Error                  | Cause                                         | Fix                                           |
|------------------------|-----------------------------------------------|-----------------------------------------------|
| `UNRESOLVED_TOKEN`     | Source DDL has `{{TOKEN}}` with no map entry  | Add the missing key to `token_map.conf`       |
| `TOKEN_KEY_UPPER`      | Key in map is not UPPERCASE                   | Rename the key to UPPERCASE                   |
| `TOKEN_BRACE_FORMAT`   | Source DDL uses `{TOKEN}` (single brace)      | Change to `{{TOKEN}}` double-brace format     |
| `TOKEN_NO_SPACE`       | Key contains a space                          | Replace spaces with underscores               |
