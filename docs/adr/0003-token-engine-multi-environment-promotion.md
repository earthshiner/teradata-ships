# ADR 0003: Token Engine for Multi-Environment Promotion

## Status

Accepted | 2026-01-22

## Context

Enterprise Teradata deployments routinely span multiple environments
— at minimum DEV, SIT, and PRD; often also UAT and hot-fix
sandboxes. Each environment uses different database names. A data
product whose Domain module is named `D01_MP_DOM_T` in DEV will
be named `S01_MP_DOM_T` in SIT and `P01_MP_DOM_T` in PRD. Grants,
cross-database view references, and CREATE statements must all
reflect the correct environment names.

Three approaches to environment promotion were in common use at
project inception:

1. **Find-and-replace scripts.** A shell or Python script performs
   string substitution on raw DDL files before deployment. The
   mapping lives in the script, not in the files. Different
   operators write different scripts; the mapping is not versioned
   alongside the DDL.

2. **Separate DDL sets per environment.** Three copies of every
   file, one per environment. Kept in sync by convention. In
   practice, drift between environments is common and hard to
   detect.

3. **Template engines (Jinja2, etc.).** DDL files contain
   template syntax (`{{ database_name }}`). A templating library
   renders them before deployment. Requires templating library as
   a runtime dependency; DDL files are no longer valid SQL.

All three approaches share a failure mode: the mapping between
logical database roles and physical database names is embedded in
tooling or directory structure rather than in an explicit,
versioned, human-readable configuration file.

SHIPS required a promotion approach that:

- Is version-controlled alongside the DDL it governs.
- Keeps DDL files valid SQL with a minimal, mechanical
  substitution syntax that does not require a templating engine
  to read.
- Is auditable: the substitution performed at Harvest time should
  be deterministic, reproducible, and logged.
- Does not require any code change to promote from one environment
  to another — only a configuration change.
- Supports the detokenised filename convention so that the
  staged payload is browsable and diffable without running the
  substitution mentally.

## Decision

SHIPS uses a **token engine** with the following design:

1. **Token syntax.** Database name placeholders in DDL source
   files use double-brace notation: `{{TOKEN_NAME}}`. Tokens are
   UPPERCASE with underscores. Example:

   ```sql
   CREATE TABLE {{DOM_DATABASE_T}}.Customer_H (
       customer_key BIGINT NOT NULL
   )
   PRIMARY INDEX (customer_key);
   ```

   The double-brace syntax was chosen because it is visually
   distinct from both SQL syntax and Teradata macro syntax,
   and is not a reserved character sequence in any Teradata
   dialect.

2. **Token map configuration.** Each project contains a
   `config/token_map.conf` file in `KEY=VALUE` format — one
   token per line. Comments with `#` are supported. The file
   is committed to the project repository.

   ```
   DOM_DATABASE_T=D01_MP_DOM_T
   DOM_DATABASE_V=D01_MP_DOM_V
   MEM_DATABASE=D01_MP_MEM
   DBC_DATABASE=DBC
   ```

   A separate `token_map.conf` exists per environment under
   `config/environments/{ENV}/token_map.conf`. The Harvest
   phase accepts `--env` and resolves the correct map.

3. **Harvest-time substitution.** The Harvest phase reads every
   source DDL file, performs a single-pass substitution of all
   `{{TOKEN}}` references using the environment's token map, and
   writes the result to `payload/`. The substitution is logged:
   each file's token replacements are recorded in the harvest
   manifest.

4. **Token completeness validation.** Inspect checks every file
   in the payload for unresolved tokens (any remaining `{{...}}`
   string). An unresolved token is an ERROR-severity Discipline
   violation. Harvest fails fast if a token present in the source
   is absent from the token map.

5. **DBC is always tokenised.** The special database `DBC` is
   represented as `{{DBC_DATABASE}}` in source, mapping to `DBC`
   by default (or to a filtered views database on sites that
   restrict direct DBC access). This ensures SHIPS packages are
   portable to sites that proxy DBC without file-level changes.

6. **Detokenised filenames in the packaged output.** The release
   ZIP produced by Package uses filenames derived from the
   tokenised content, not the source filenames. If source
   contains `{{DOM_DATABASE_T}}.Customer_H.tbl`, the packaged
   file is `D01_MP_DOM_T.Customer_H.tbl` (for the DEV
   environment). This makes the archive browsable and diffable
   without reference to the token map.

7. **Token names reflect logical roles, not physical names.**
   Token names encode the module and access layer:
   `DOM_DATABASE_T` (Domain tables), `DOM_DATABASE_V` (Domain
   views), `SEM_DATABASE` (Semantic), `MEM_DATABASE` (Memory),
   etc. The physical name is the token map's concern; the logical
   role is the source file's concern.

## Consequences

**Positive**

- Promoting from DEV to SIT requires only a new
  `config/environments/SIT/token_map.conf`. No DDL file changes.
  No script changes. The promotion is a configuration commit,
  not a code change.
- Source files remain valid SQL if the tokens are treated as
  database name identifiers. They can be syntax-checked by
  standard tools as long as the tool does not resolve the
  database names.
- The token map is the single source of truth for the
  environment → physical name mapping. It lives in version
  control, can be reviewed in a PR, and is auditable.
- Unresolved tokens are caught at Inspect time, before the
  package is built. A mis-spelled token name (`{{DOM_DATABASE_T_}}`)
  is caught before it reaches a target environment.

**Negative**

- Source files are not executable SQL. A developer cannot copy
  a source file to BTEQ and run it without first substituting
  tokens. Mitigation: `ships harvest --env DEV` produces
  executable output in `payload/` that can be copied out for
  ad-hoc use.
- Token names must be agreed across the project before Scaffold.
  Adding a new database module mid-project requires adding a
  new token to the map, updating source files, and re-harvesting.
  This is mechanical but not zero-cost.
- The `DBC_DATABASE` token is easy to forget on new projects.
  Inspect catches it, but the error message must be clear
  enough that developers do not simply hardcode `DBC` to work
  around it.

**Neutral**

- The `{{...}}` syntax is the same convention used by several
  other Teradata field tooling projects. This was not a
  deliberate alignment but is a useful consistency.
- The token map format (`KEY=VALUE`) was chosen over YAML or
  JSON for readability and to avoid a parsing dependency. A
  future SHIPS version may accept YAML if the project's
  `ships.yaml` schema absorbs the token map.

## Alternatives considered

**Jinja2 templating.** Rejected: DDL files containing Jinja2
syntax are not valid SQL and require the Jinja2 library to render.
SHIPS is a deployment tool; its source files should be readable
by any SQL developer without installing the toolchain.

**Environment-specific DDL copies.** Rejected: three copies of
every file is three times the maintenance burden and three times
the drift risk. Observed in the field to cause production
deployments from stale SIT-era DDL copies.

**Configuration inside the DDL comment header.** Considered: a
comment block at the top of each file declaring its token
mappings. Rejected: this distributes the mapping across hundreds
of files and makes it impossible to change an environment's
physical naming without touching every source file.

**Shell `sed` / `envsubst` substitution.** Rejected: relies on
operator-side tooling rather than project-side configuration.
Substitutions are not logged, not auditable, and not
reproducible across operators with different shell environments.

## References

- `td_release_packager/token_engine.py` — token substitution
  implementation.
- `td_release_packager/ingest.py` — Harvest phase; invokes the
  token engine per source file.
- `td_release_packager/validate.py` — `_check_token_completeness`
  rule, enforces zero unresolved tokens in the payload.
- `config/token_map.conf` — project-level token map (flat
  form, pre `_T`/`_V` split).
- ADR 0002: SHIPS pipeline phase structure — Harvest is the
  phase responsible for token substitution.
- ADR 0004: Atomic eponymous DDL files — the file naming
  convention that combines with token substitution to produce
  detokenised filenames in the packaged output.
