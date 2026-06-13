# ADR 0015: AST-based SQL Reference Inference

## Status

Accepted | 2026-06-14 (Phase 1 landed; see issue #234 and PR introducing
`src/td_release_packager/sql_reference_extractor.py`)
Proposed | 2026-05-21

## Context

SHIPS infers inter-database grant DCL and dependency ordering by scanning
Teradata SQL for qualified references. The current implementation uses
regular expressions with targeted alias filtering. This was sufficient for
simple atomic DDL, but real customer code now includes nested derived tables,
CTEs, comma joins, macro DML, view pass-through grants, and Teradata-specific
expressions.

The failure mode is no longer cosmetic. If the scanner mistakes a runtime
alias for a database, SHIPS emits invalid DCL such as:

```sql
GRANT SELECT ON sRV_ProcessSumByBusDate TO GDEV1V_OPR WITH GRANT OPTION;
```

In that example, `sRV_ProcessSumByBusDate` is a derived-table alias created by
`FROM (...) sRV_ProcessSumByBusDate`. It is not a persisted Teradata database
or object and cannot be granted against. Similar issues have appeared for
short table aliases, CTE names, derived-table aliases, and system database
references.

Regex improvements can keep production moving for known cases, but the pattern
is becoming a parser by accumulation. Grant inference is a trust-sensitive
feature: false positives can block deployment, and false negatives can miss
permissions needed to create production objects.

## Decision

SHIPS should migrate SQL reference inference to an AST-backed parser behind a
small internal abstraction:

```text
SqlReferenceExtractor
  - extract_read_sources(sql)
  - extract_write_targets(sql)
  - extract_call_targets(sql)
  - extract_statement_owner(sql)
```

The first candidate parser is SQLGlot because it has a Teradata dialect and a
Python API that exposes table, column, CTE, subquery, and DML target nodes.
Teradata support is community-maintained, so the migration must be incremental
and guarded by tests rather than a big-bang replacement.

The existing regex scanner remains as a compatibility fallback while the AST
extractor is introduced and verified.

## Consequences

**Positive**

- Distinguishes persisted table/view references from aliases, CTEs, and
  derived-table scopes using parse-tree semantics rather than string shape.
- Reduces the risk of invalid inferred DCL blocking deployments.
- Creates one parser boundary that can be reused by grant inference,
  dependency analysis, linting, and package reports.
- Makes complex customer SQL a test corpus rather than a stream of one-off
  regex patches.

**Negative**

- Adds a parser dependency and its own compatibility surface.
- SQLGlot's Teradata dialect is community-supported, so SHIPS may need local
  pre-processing or dialect patches for Teradata-only syntax.
- AST parsing may fail on legacy SQL that Teradata accepts. The regex fallback
  must remain until enough coverage proves the AST path is stable.

**Neutral**

- DDL envelope parsing can remain in SHIPS. For example, SHIPS can still
  identify `REPLACE VIEW db.obj AS ...`, then pass only the query body to the
  AST parser.
- The grant model does not change: inferred grants remain database-level,
  never object-level.

## Migration plan

1. Add a `SqlReferenceExtractor` module with the current regex implementation
   behind the abstraction.
2. Add SQLGlot as an optional implementation and run it in compare mode in
   tests for representative views, macros, procedures, DML, CTEs, and derived
   tables.
3. Capture parser mismatches in a structured diagnostic that names the file
   and references discovered by each extractor.
4. Make AST extraction authoritative for read-source discovery once the
   regression corpus is green.
5. Extend AST extraction to DML targets, `CALL`, `EXEC`, and dependency
   ordering.
6. Retire regex fallback only after a release cycle with no parser mismatches
   on customer-style packages.

## Alternatives considered

**Continue extending regexes.** Acceptable for urgent defects, but rejected as
the long-term strategy. Regex cannot reliably model SQL scope, especially with
nested subqueries and aliases.

**Write a custom Teradata parser.** Rejected. SHIPS should not own a full SQL
grammar unless no maintained parser can be made fit for purpose.

**Make inferred grants manual only.** Rejected. Automatic inference is central
to making SHIPS practical and agent-friendly, especially for large legacy
codebases.

## References

- GitHub issue #234: Migrate SQL reference inference to AST parser.
- `td_release_packager/infer_grants.py` — current regex implementation.
- ADR 0008: DCL Subdirectory Structure and Grant Inference.
- ADR 0012: Package Trust Score Design.
- SQLGlot Teradata dialect documentation: https://sqlglot.com/sqlglot/dialects/teradata.html
