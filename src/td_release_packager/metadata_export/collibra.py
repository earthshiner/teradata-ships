"""
collibra.py — render the neutral ProductMetadata into a Collibra bundle (#244).

Projects the same shared model into a Collibra-oriented import payload. The
primary artefact is ``collibra_import.json`` — a resource list (Community →
Domain → Assets with attributes + relations) following Collibra's Import API
conventions — accompanied by governance / provenance side files and a manifest.

Pure projection: no Collibra connectivity, no fabrication. Because it consumes
the same ``ProductMetadata`` as the Alation renderer, the two catalogues stay in
sync from one extraction.
"""

from __future__ import annotations

from typing import Any, Dict, List

from td_release_packager.metadata_export.model import ProductMetadata

BUNDLE_TYPE = "collibra_metadata_export"
BUNDLE_VERSION = "0.1.0"

#: The community every exported data product is filed under in Collibra.
_COMMUNITY = "Data Products"


def _domain_ref(product_id: str) -> Dict[str, Any]:
    return {"name": product_id, "community": {"name": _COMMUNITY}}


def _asset_id(qualified: str, product_id: str) -> Dict[str, Any]:
    return {"name": qualified, "domain": _domain_ref(product_id)}


def render(meta: ProductMetadata, generated_at: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{filename: json_obj}`` for the Collibra bundle (incl. manifest)."""
    ident = meta.identity
    pid = ident.product_id

    resources: List[Dict[str, Any]] = [
        {"resourceType": "Community", "identifier": {"name": _COMMUNITY}},
        {
            "resourceType": "Domain",
            "identifier": _domain_ref(pid),
            "type": {"name": "Data Asset Domain"},
        },
    ]

    # The product itself as a Data Product asset.
    product_attrs = {
        "Description": [ident.description] if ident.description else [],
        "Version": [ident.version] if ident.version else [],
        "Environment": [ident.environment] if ident.environment else [],
        "Status": [ident.status] if ident.status else [],
    }
    resources.append(
        {
            "resourceType": "Asset",
            "identifier": _asset_id(ident.product_name, pid),
            "type": {"name": "Data Product"},
            "attributes": {k: v for k, v in product_attrs.items() if v},
        }
    )

    # Interfaces / physical assets → Table/View assets, related to the product.
    type_map = {"view": "View", "table": "Table", "macro": "Function"}
    for i in meta.interfaces:
        qualified = f"{i.database}.{i.object_name}" if i.database else i.object_name
        resources.append(
            {
                "resourceType": "Asset",
                "identifier": _asset_id(qualified, pid),
                "type": {"name": type_map.get(i.interface_type, "Table")},
                "attributes": {
                    "Consumer Facing": [str(i.consumer_facing).lower()],
                },
                "relations": {
                    "Data Product contains Asset": [
                        {"target": _asset_id(ident.product_name, pid)}
                    ]
                },
            }
        )

    # Columns → Column assets related to their asset.
    for c in meta.columns:
        resources.append(
            {
                "resourceType": "Asset",
                "identifier": _asset_id(f"{c.asset}.{c.column_name}", pid),
                "type": {"name": "Column"},
                "attributes": {
                    "Data Type": [c.data_type] if c.data_type else [],
                },
                "relations": {
                    "Column is part of Table": [{"target": _asset_id(c.asset, pid)}]
                },
            }
        )

    # Lineage hints → source/target relations.
    for ln in meta.lineage:
        resources.append(
            {
                "resourceType": "Relation",
                "type": {"name": "Asset sources Asset"},
                "source": _asset_id(ln.source, pid),
                "target": _asset_id(ln.target, pid),
                "attributes": {"Lineage Type": [ln.lineage_type]},
            }
        )

    files: Dict[str, Dict[str, Any]] = {
        "collibra_import.json": {"resources": resources},
        "governance.json": {
            "trust": {
                "state": meta.trust.state,
                "integrity_passed": meta.trust.integrity_passed,
                "package_hash": meta.trust.package_hash,
                "blocking_signals": meta.trust.blocking_signals,
                "warning_signals": meta.trust.warning_signals,
                "caveats": meta.trust.caveats,
            },
            "access": [
                {
                    "interface": a.interface,
                    "access_role": a.access_role,
                    "grant_type": a.grant_type,
                }
                for a in meta.access
            ],
        },
        "provenance.json": {
            "package_name": meta.provenance.package_name,
            "build_number": meta.provenance.build_number,
            "environment": meta.provenance.environment,
            "source_commit": meta.provenance.source_commit,
            "built_by": meta.provenance.built_by,
            "built_at": meta.provenance.built_at,
            "package_hash": meta.provenance.package_hash,
        },
        "decisions.json": {
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "title": d.title,
                    "reason": d.reason,
                    "impact": d.impact,
                    "status": d.status,
                }
                for d in meta.decisions
            ]
        },
    }

    files["manifest.json"] = {
        "bundle_type": BUNDLE_TYPE,
        "bundle_version": BUNDLE_VERSION,
        "generated_by": "ships",
        "generated_at": generated_at,
        "source_package": meta.provenance.package_name,
        "environment": ident.environment,
        "community": _COMMUNITY,
        "warnings": meta.warnings,
        "files": [k for k in files if k != "manifest.json"],
    }
    return files
