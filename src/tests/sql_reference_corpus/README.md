# SQL Reference Extractor — Regression Corpus

Customer-style SQL fragments and their expected
`SqlReferenceExtractor` outputs. The corpus is the source of truth
for #234 / ADR 0015: every implementation
(`RegexSqlReferenceExtractor`, future `SqlGlotSqlReferenceExtractor`)
must produce the documented results on every entry.

## Layout

```
src/tests/sql_reference_corpus/
    README.md                              — this file
    <case_name>/
        sql.sql                            — the SQL fragment
        expected.json                      — expected extractor outputs
        notes.md                           — optional context
```

## `expected.json` shape

```json
{
  "summary": "short description shown in test names",
  "owner": {
    "database": "{{DOM_V}}",
    "object_name": "Customer_V",
    "object_type": "VIEW"
  },
  "read_sources": [
    {"database": "{{REF_T}}", "object_name": "Customer"}
  ],
  "write_targets": [
    {"database": "{{TGT}}", "object_name": "Log", "privileges": ["INSERT"]}
  ],
  "call_targets": [
    {"database": "{{PROC_DB}}", "object_name": "Refresh", "privileges": ["EXECUTE PROCEDURE"]}
  ]
}
```

Set `owner` to `null` if the fragment is not a CREATE/REPLACE
statement. Omit keys whose expected list is empty if you'd rather
the test infer "extractor should return nothing."

## Adding a case

1. Create a directory named after the scenario
   (`derived_table_alias_srv_processsumbybusdate/`,
   `cte_then_join/`, `merge_with_using_subquery/`).
2. Drop the SQL into `sql.sql` — pre-strip comments (the test
   passes the file content through `strip_sql_comments`).
3. Write `expected.json` with the four fields above.
4. (Optional) `notes.md` with context: why this case matters,
   which customer/package it came from (sanitised), which historical
   bug it guards.

The test harness (`test_sql_reference_corpus.py`) discovers every
subdirectory automatically — no registration needed.

## Phase coverage

| Phase | Implementation under test |
|-------|---------------------------|
| 1 (now) | `RegexSqlReferenceExtractor` |
| 2 | regex + AST in compare mode |
| 3+ | AST authoritative |
