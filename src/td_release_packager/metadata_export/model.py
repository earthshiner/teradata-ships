"""
model.py — neutral, catalogue-agnostic data-product metadata model (#244).

SHIPS extracts one neutral ``ProductMetadata`` from a built package, then each
catalogue renderer (Alation, Collibra, …) projects that single model into its
own JSON shape. Defining the model once — rather than coupling the extractor to
any one catalogue — is what lets the same evidence feed multiple enterprise
catalogues without re-reading the package per target.

Every field is optional-friendly: the extractor populates what the package
actually contains and records a warning for what it can't, so a renderer never
fabricates business meaning that wasn't in the source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def _clean(d: Any) -> Any:
    """Recursively drop ``None`` values so emitted JSON stays tidy."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_clean(v) for v in d]
    return d


@dataclass
class ProductIdentity:
    product_id: str
    product_name: str
    version: str
    environment: str
    description: Optional[str] = None
    domain: Optional[str] = None
    status: Optional[str] = None
    business_owner: Optional[str] = None
    technical_owner: Optional[str] = None
    steward: Optional[str] = None
    last_built_at: Optional[str] = None


@dataclass
class LogicalInterface:
    interface_name: str
    interface_type: str  # view | table | macro | …
    platform: str
    database: str
    object_name: str
    consumer_facing: bool
    purpose: Optional[str] = None


@dataclass
class PhysicalAsset:
    platform: str
    database: str
    object_name: str
    object_type: str


@dataclass
class AssetMapping:
    logical_asset: str
    physical_assets: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ColumnMetadata:
    asset: str
    column_name: str
    data_type: Optional[str] = None
    nullable: Optional[bool] = None
    is_key: Optional[bool] = None


@dataclass
class GlossaryTerm:
    term: str
    definition: str
    domain: Optional[str] = None
    related_assets: List[str] = field(default_factory=list)


@dataclass
class LineageHint:
    source: str
    target: str
    lineage_type: str


@dataclass
class TrustState:
    state: Optional[str] = None
    integrity_passed: Optional[bool] = None
    package_hash: Optional[str] = None
    blocking_signals: List[str] = field(default_factory=list)
    warning_signals: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)


@dataclass
class AccessRule:
    interface: str
    access_role: str
    grant_type: str


@dataclass
class Provenance:
    package_name: Optional[str] = None
    build_number: Optional[str] = None
    environment: Optional[str] = None
    source_commit: Optional[str] = None
    built_by: Optional[str] = None
    built_at: Optional[str] = None
    package_hash: Optional[str] = None


@dataclass
class Decision:
    decision_id: Optional[str] = None
    title: Optional[str] = None
    reason: Optional[str] = None
    impact: Optional[str] = None
    status: Optional[str] = None


@dataclass
class ProductMetadata:
    """The single neutral model every catalogue renderer consumes."""

    identity: ProductIdentity
    interfaces: List[LogicalInterface] = field(default_factory=list)
    physical_assets: List[PhysicalAsset] = field(default_factory=list)
    asset_mappings: List[AssetMapping] = field(default_factory=list)
    columns: List[ColumnMetadata] = field(default_factory=list)
    glossary_terms: List[GlossaryTerm] = field(default_factory=list)
    lineage: List[LineageHint] = field(default_factory=list)
    trust: TrustState = field(default_factory=TrustState)
    access: List[AccessRule] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    decisions: List[Decision] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))
