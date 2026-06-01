# AI-Native Data Product Discovery

This reference defines the product-level discovery contract for
AI-native data products exposed to MCP-compatible clients.

`data_product_map` remains the module inventory inside the Semantic
module. It is not the first table a client should query. A client first
discovers the current product and its declared metadata surfaces from
`data_product_registry`, then navigates to Semantic, Memory,
Observability, policy, quality, and approved data entrypoints.

## Product Registry

Use the contract/governance database token for SHIPS portability. In an
environment where the physical governance database is named
`governance`, map `CTR_DATABASE=governance`.

```sql
CREATE MULTISET TABLE {{CTR_DATABASE}}.data_product_registry
(
    product_id             VARCHAR(128) NOT NULL
   ,product_name           VARCHAR(256) NOT NULL
   ,product_version        VARCHAR(32) NOT NULL
   ,product_description    VARCHAR(1000)
   ,product_status         VARCHAR(32) NOT NULL
   ,owner_team             VARCHAR(256)
   ,semantic_database      VARCHAR(128)
   ,memory_database        VARCHAR(128)
   ,observability_database VARCHAR(128)
   ,manifest_json          CLOB
   ,contract_uri           VARCHAR(1000)
   ,semantic_uri           VARCHAR(1000)
   ,quality_uri            VARCHAR(1000)
   ,lineage_uri            VARCHAR(1000)
   ,policy_uri             VARCHAR(1000)
   ,glossary_uri           VARCHAR(1000)
   ,query_cookbook_uri     VARCHAR(1000)
   ,approved_entrypoint    VARCHAR(1000)
   ,approved_access_mode   VARCHAR(32)
   ,is_active              BYTEINT NOT NULL
   ,is_deleted             BYTEINT NOT NULL
   ,created_at             TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP(6)
   ,updated_at             TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP(6)
)
PRIMARY INDEX (product_id);
```

Suggested status values:

- `DRAFT`
- `ACTIVE`
- `DEPRECATED`
- `RETIRED`

Suggested access mode values:

- `VIEW`
- `MCP_TOOL`
- `SEMANTIC_QUERY`

## Client Discovery Order

An MCP client should discover and navigate a data product in this order:

1. Query `{{CTR_DATABASE}}.data_product_registry` for active,
   non-deleted products.
2. Read `manifest_json` or `contract_uri` for the machine-readable
   navigation contract.
3. Use `semantic_database`, `memory_database`, and
   `observability_database` to locate metadata modules.
4. Query Semantic metadata:
   `data_product_map`, `naming_standard`, `entity_metadata`,
   `column_metadata`, `table_relationship`, then the relationship paths
   view.
5. Query Memory metadata before generating SQL:
   `Business_Glossary`, `Query_Cookbook`, and `Design_Decision`.
6. Query Observability and governance surfaces through `lineage_uri`,
   `quality_uri`, and `policy_uri` where present.
7. Access data only through `approved_entrypoint` using the declared
   `approved_access_mode`.

## MCP Resource Shape

MCP servers should expose registry-backed resources so clients do not
need to guess database names or physical tables before reading the
contract:

```text
data-products://catalog
data-products://{product_id}/manifest
data-products://{product_id}/semantic-map
data-products://{product_id}/query-cookbook
data-products://{product_id}/approved-entrypoints
```

`data-products://catalog` should be backed by:

```sql
SELECT product_id
      ,product_name
      ,product_version
      ,product_description
      ,product_status
      ,owner_team
      ,semantic_database
      ,memory_database
      ,observability_database
      ,contract_uri
      ,semantic_uri
      ,quality_uri
      ,lineage_uri
      ,policy_uri
      ,glossary_uri
      ,query_cookbook_uri
      ,approved_entrypoint
      ,approved_access_mode
FROM {{CTR_DATABASE}}.data_product_registry
WHERE is_active = 1
  AND is_deleted = 0;
```

