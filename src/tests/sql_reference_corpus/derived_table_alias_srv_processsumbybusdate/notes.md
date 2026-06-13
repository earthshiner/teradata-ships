# derived_table_alias_srv_processsumbybusdate

The canonical regression case from [ADR 0015](../../../../docs/adr/0015-ast-based-sql-reference-inference.md).

A nested derived table is aliased `sRV_ProcessSumByBusDate`. Inside
the outer query it is referenced via `Min(sRV_ProcessSumByBusDate.Process_State)`.
The historical regex scanner mistook the alias for a database name
and emitted:

```sql
GRANT SELECT ON sRV_ProcessSumByBusDate TO GDEV1V_OPR WITH GRANT OPTION;
```

The fix added a balanced-paren derived-alias collector
(`_collect_balanced_derived_aliases` in `infer_grants.py`). Any
implementation of `SqlReferenceExtractor` MUST keep that exclusion —
losing it re-introduces a trust-sensitive defect that blocks
production deploys.
