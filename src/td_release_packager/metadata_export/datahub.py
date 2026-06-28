"""
datahub.py — render the neutral ProductMetadata into a DataHub bundle (#244).

Projects the shared model into DataHub's ingestion format: a list of
MetadataChangeProposals (MCPs) in the ``aspect.json`` envelope produced by the
DataHub file emitter, consumable by DataHub's file source. Emits a ``dataset``
entity per physical asset (with ``datasetProperties``, ``subTypes``, and
``schemaMetadata``), ``upstreamLineage`` for each lineage edge, and a
``dataProduct`` entity grouping the assets.

Pure projection from the same ``ProductMetadata`` the Alation/Collibra renderers
consume: no DataHub connectivity, no fabrication.
"""

from __future__ import annotations

from typing import Any, Dict, List

from td_release_packager.metadata_export.model import ProductMetadata

BUNDLE_TYPE = "datahub_metadata_export"
BUNDLE_VERSION = "0.1.0"

#: Teradata's DataHub data-platform identifier.
_PLATFORM = "urn:li:dataPlatform:teradata"


def _fabric(environment: str) -> str:
    """Map an environment to a DataHub fabric (defaults to DEV)."""
    return (environment or "DEV").upper()


def _dataset_urn(qualified: str, environment: str) -> str:
    return f"urn:li:dataset:({_PLATFORM},{qualified},{_fabric(environment)})"


def _mcp(
    entity_type: str, urn: str, aspect_name: str, aspect: Dict[str, Any]
) -> Dict[str, Any]:
    """Build one MetadataChangeProposal in the file-emitter envelope."""
    return {
        "entityType": entity_type,
        "entityUrn": urn,
        "changeType": "UPSERT",
        "aspectName": aspect_name,
        "aspect": {"json": aspect},
    }


#: SQL native type → DataHub schema field type class (best-effort, conservative).
def _field_type(native: str) -> Dict[str, Any]:
    n = (native or "").upper()
    if any(t in n for t in ("CHAR", "VARCHAR", "CLOB", "STRING")):
        cls = "StringType"
    elif any(
        t in n
        for t in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "REAL", "BYTEINT", "NUMBER")
    ):
        cls = "NumberType"
    elif any(t in n for t in ("DATE", "TIMESTAMP", "TIME")):
        cls = "DateType"
    else:
        cls = "NullType"
    return {"type": {f"com.linkedin.schema.{cls}": {}}}


def render(meta: ProductMetadata, generated_at: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{filename: json_obj}`` for the DataHub bundle (incl. manifest)."""
    env = meta.identity.environment
    mcps: List[Dict[str, Any]] = []

    # -- group columns by their owning asset (qualified DB.Object) --
    cols_by_asset: Dict[str, List[Any]] = {}
    for c in meta.columns:
        cols_by_asset.setdefault(c.asset, []).append(c)

    # -- a dataset per physical asset --
    asset_urns: List[str] = []
    for a in meta.physical_assets:
        qualified = f"{a.database}.{a.object_name}" if a.database else a.object_name
        urn = _dataset_urn(qualified, env)
        asset_urns.append(urn)

        mcps.append(
            _mcp(
                "dataset",
                urn,
                "datasetProperties",
                {
                    "name": a.object_name,
                    "qualifiedName": qualified,
                    "description": meta.identity.description,
                    "customProperties": {
                        "database": a.database,
                        "object_type": a.object_type,
                        "environment": env,
                        "trust_state": meta.trust.state or "",
                        "package_hash": meta.trust.package_hash or "",
                    },
                },
            )
        )
        mcps.append(
            _mcp("dataset", urn, "subTypes", {"typeNames": [a.object_type.title()]})
        )

        fields = cols_by_asset.get(qualified, [])
        if fields:
            mcps.append(
                _mcp(
                    "dataset",
                    urn,
                    "schemaMetadata",
                    {
                        "schemaName": qualified,
                        "platform": _PLATFORM,
                        "version": 0,
                        "hash": "",
                        "platformSchema": {
                            "com.linkedin.schema.OtherSchema": {"rawSchema": ""}
                        },
                        "fields": [
                            {
                                "fieldPath": col.column_name,
                                "nativeDataType": col.data_type or "",
                                "type": _field_type(col.data_type or ""),
                                "nullable": True
                                if col.nullable is None
                                else col.nullable,
                            }
                            for col in fields
                        ],
                    },
                )
            )

    # -- lineage edges → upstreamLineage on the target dataset --
    upstreams_by_target: Dict[str, List[str]] = {}
    for ln in meta.lineage:
        tgt = _dataset_urn(ln.target, env)
        upstreams_by_target.setdefault(tgt, []).append(_dataset_urn(ln.source, env))
    for tgt, sources in upstreams_by_target.items():
        mcps.append(
            _mcp(
                "dataset",
                tgt,
                "upstreamLineage",
                {"upstreams": [{"dataset": s, "type": "TRANSFORMED"} for s in sources]},
            )
        )

    # -- the data product entity grouping the assets --
    product_urn = f"urn:li:dataProduct:{meta.identity.product_id}"
    mcps.append(
        _mcp(
            "dataProduct",
            product_urn,
            "dataProductProperties",
            {
                "name": meta.identity.product_name,
                "description": meta.identity.description,
                "customProperties": {
                    "version": meta.identity.version,
                    "environment": env,
                    "trust_state": meta.trust.state or "",
                },
                "assets": [{"destinationUrn": u} for u in asset_urns],
            },
        )
    )

    files: Dict[str, Dict[str, Any]] = {
        "datahub_mcps.json": {"proposals": mcps},
        "manifest.json": {
            "bundle_type": BUNDLE_TYPE,
            "bundle_version": BUNDLE_VERSION,
            "generated_by": "ships",
            "generated_at": generated_at,
            "source_package": meta.provenance.package_name,
            "environment": env,
            "platform": _PLATFORM,
            "mcp_count": len(mcps),
            "warnings": meta.warnings,
            "files": ["datahub_mcps.json"],
        },
    }
    return files
