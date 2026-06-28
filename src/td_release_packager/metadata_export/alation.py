"""
alation.py — render the neutral ProductMetadata into an Alation bundle (#244).

Projects the shared model into the modular JSON file set Alation (or an Alation
integration process) can ingest. Pure projection: no Alation connectivity, no
fabrication — only what the extractor found.
"""

from __future__ import annotations

from typing import Any, Dict

from td_release_packager.metadata_export.model import ProductMetadata

BUNDLE_TYPE = "alation_metadata_export"
BUNDLE_VERSION = "0.1.0"


def render(meta: ProductMetadata, generated_at: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{filename: json_obj}`` for the Alation bundle (incl. manifest)."""
    ident = meta.identity
    files: Dict[str, Dict[str, Any]] = {
        "data_product.json": {
            "product_id": ident.product_id,
            "product_name": ident.product_name,
            "domain": ident.domain,
            "version": ident.version,
            "environment": ident.environment,
            "description": ident.description,
            "status": ident.status,
            "business_owner": ident.business_owner,
            "technical_owner": ident.technical_owner,
            "steward": ident.steward,
            "last_built_at": ident.last_built_at,
        },
        "logical_interfaces.json": {
            "interfaces": [
                {
                    "interface_name": i.interface_name,
                    "interface_type": i.interface_type,
                    "platform": i.platform,
                    "database": i.database,
                    "object_name": i.object_name,
                    "consumer_facing": i.consumer_facing,
                    "purpose": i.purpose,
                }
                for i in meta.interfaces
            ]
        },
        "physical_mappings.json": {
            "physical_assets": [
                {
                    "platform": a.platform,
                    "database": a.database,
                    "object": a.object_name,
                    "object_type": a.object_type,
                }
                for a in meta.physical_assets
            ],
            "asset_mappings": [
                {"logical_asset": m.logical_asset, "physical_assets": m.physical_assets}
                for m in meta.asset_mappings
            ],
        },
        "column_metadata.json": {
            "columns": [
                {
                    "asset": c.asset,
                    "column_name": c.column_name,
                    "data_type": c.data_type,
                    "nullable": c.nullable,
                    "is_key": c.is_key,
                }
                for c in meta.columns
            ]
        },
        "glossary_terms.json": {
            "terms": [
                {
                    "term": t.term,
                    "definition": t.definition,
                    "domain": t.domain,
                    "related_assets": t.related_assets,
                }
                for t in meta.glossary_terms
            ]
        },
        "lineage.json": {
            "lineage": [
                {
                    "source": ln.source,
                    "target": ln.target,
                    "lineage_type": ln.lineage_type,
                }
                for ln in meta.lineage
            ]
        },
        "quality_and_trust.json": {
            "trust": {
                "state": meta.trust.state,
                "integrity_passed": meta.trust.integrity_passed,
                "package_hash": meta.trust.package_hash,
                "blocking_signals": meta.trust.blocking_signals,
                "warning_signals": meta.trust.warning_signals,
                "caveats": meta.trust.caveats,
            }
        },
        "access_model.json": {
            "access": [
                {
                    "interface": a.interface,
                    "access_role": a.access_role,
                    "grant_type": a.grant_type,
                }
                for a in meta.access
            ]
        },
        "provenance.json": {
            "deployment": {
                "package_name": meta.provenance.package_name,
                "build_number": meta.provenance.build_number,
                "environment": meta.provenance.environment,
                "source_commit": meta.provenance.source_commit,
                "built_by": meta.provenance.built_by,
                "built_at": meta.provenance.built_at,
                "package_hash": meta.provenance.package_hash,
            }
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
        "environment": meta.identity.environment,
        "warnings": meta.warnings,
        "files": [k for k in files if k != "manifest.json"],
    }
    return files
