# comma_join_with_correlation_aliases

```sql
FROM {{DOM_T}}.Customer c,
     {{REF_T}}.Address a,
     {{REF_T}}.Phone p
```

The classic comma-style join. Teradata accepts this; many real
customer packages still use it.

## Known regex gap (opted out via `skip_extractors`)

`appears_as_read_source()` in `infer_grants.py` only matches when
`FROM`, `JOIN`, or `USING` directly precedes the database-qualified
reference. The second and third entries in a comma list are preceded
by `,` rather than `FROM`, so the regex extractor misses them.

```python
pattern = re.compile(
    r"\b(?P<keyword>FROM|JOIN|USING)\s+" + re.escape(db_ref) + r"\s*\.",
    ...
)
```

This is one of the trust-sensitive scenarios ADR 0015 cites — the
regex misses real read sources, so inferred DCL omits required
SELECT grants. The AST implementation parses the comma list as a
sequence of Table nodes parented to `From` / `Join` and gets all
three correctly.

The case is opted out for `regex` until either:
1. Phase 3+ fixes `appears_as_read_source` to walk forwards through
   commas while still in FROM scope, or
2. Phase 5 retires the regex extractor entirely.
