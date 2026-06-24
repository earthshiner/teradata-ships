# Run sheet — tokenise non-tokenised source into the payload

Goal: harvest raw DDL/DCL/DML that has **hardcoded** database names
(`CustomerDNA_DOM_STD_T`) and land a **fully tokenised** payload —
both file **content** and file **names** (`{{...}}.booking.tbl`).

Two paths that tokenise filenames correctly. Pick one. Both wipe the
payload first by default, so re-running is safe.

> Avoid `--auto-tokenise` and `--token-map`: they tokenise content but
> leave filenames literal.

---

## Prerequisite

A scaffolded project (contains `ships.yaml`). If not yet scaffolded:

    python -m td_release_packager scaffold --project C:\ships\CustomerDNA

---

## Option A — single product-prefix token  (one flag, no config file)

Produces `{{DB_PREFIX}}_DOM_STD_T.booking.tbl` (one token for the
whole product).

    python -m td_release_packager harvest ^
        --source  C:\temp\CustomerDNA\source ^
        --project C:\ships\CustomerDNA ^
        --prefix-token CustomerDNA=DB_PREFIX

- `--source`        directory of raw DDL/DCL/DML
- `--project`       scaffolded SHIPS project (must contain ships.yaml)
- `--prefix-token`  SOURCE=TOKEN. `CustomerDNA` -> `{{DB_PREFIX}}`.
                    Bare token name is rejected; the `=` is required.

Per-environment binding (one line) in `config/env/<ENV>.conf`:

    DB_PREFIX = CustomerDNA      # DEV
    DB_PREFIX = CDNA_PRD         # PROD

MCP equivalent:

    ships_harvest(
        project="C:\\ships\\CustomerDNA",
        source="C:\\temp\\CustomerDNA\\source",
        prefix_token="CustomerDNA=DB_PREFIX")

---

## Option B — per-database tokens  (uses config/tokenise.conf)

Produces `{{DOM_STD_T}}.booking.tbl`, `{{SEM_STD_T}}.value_domain.dml`
(one token per database — matches the `_STD_T` / `_STD_V` convention).

1. Put `tokenise.conf` at `C:\ships\CustomerDNA\config\tokenise.conf`
   (the file already written for you). It is auto-applied by harvest.

2. Plain harvest — no tokenisation flag needed:

       python -m td_release_packager harvest ^
           --source  C:\temp\CustomerDNA\source ^
           --project C:\ships\CustomerDNA

Per-environment binding in `config/env/<ENV>.conf` (one line per db):

    DOM_STD_T = CustomerDNA_DOM_STD_T      # DEV
    SEM_STD_T = CustomerDNA_SEM_STD_T
    DOM_STD_V = CustomerDNA_DOM_STD_V
    ...

MCP equivalent (tokenise.conf still auto-applied):

    ships_harvest(
        project="C:\\ships\\CustomerDNA",
        source="C:\\temp\\CustomerDNA\\source")

---

## Verify

    python -m td_release_packager inspect --project C:\ships\CustomerDNA

Expected after either option:
- payload filenames carry `{{...}}` (e.g. `{{DOM_STD_T}}.booking.tbl`)
- no `hardcoded_name` warnings
- no `zero_tokens` errors (DML content is tokenised -> passes)

Quick eyeball of what landed:

    dir /s /b C:\ships\CustomerDNA\payload\database

---

## Notes

- Default harvest cleans payload-owned files first (keeps `.gitkeep`
  and `_`-prefixed control files). Pass `--keep-existing` only to
  overlay instead of replace.
- DDL is split to atomic + eponymous; DCL is grouped per granted-ON
  database; DML is kept whole (order preserved).
- One flag set, two shapes: `--prefix-token` -> `{{DB_PREFIX}}_DOM_STD_T`;
  `tokenise.conf` (active rule) -> `{{DOM_STD_T}}`.
