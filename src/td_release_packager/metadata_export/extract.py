"""
extract.py — build the neutral ``ProductMetadata`` from a SHIPS package (#244).

Reads the context evidence a SHIPS package already carries (``context/*.json``)
plus its payload DDL/DCL, and populates the neutral model. The extractor is
deliberately conservative: it reports what the package contains and records a
warning for anything absent, never inventing business meaning (owners, glossary
terms, AI-approval, classifications) that wasn't in the source. SQL is parsed as
text, never executed.

Sources used:
    * ``context/ships.build.json``        — identity + provenance
    * ``context/ships.dependencies.json`` — physical assets, interfaces, lineage
    * ``context/ships.trust.json``        — trust state + signals
    * ``context/ships.integrity.json``    — integrity hash / pass
    * ``payload/**``                      — column metadata (DDL) + access (DCL)
    * ``ships.decisions.json`` (if present)— design decisions
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from td_release_packager.contract import extract_contract
from td_release_packager.metadata_export.model import (
    AccessRule,
    ColumnMetadata,
    Decision,
    LineageHint,
    LogicalInterface,
    PhysicalAsset,
    ProductIdentity,
    ProductMetadata,
    Provenance,
    TrustState,
)

#: Object types treated as approved consumer-facing interfaces. Tables and
#: other physical objects are internal implementation unless --include-internal.
_CONSUMER_FACING = {"VIEW", "MACRO"}

_GRANT_RE = re.compile(
    r"(?is)\bGRANT\s+(?P<priv>[A-Z, ]+?)\s+ON\s+(?P<obj>[A-Za-z0-9_.\"]+)"
    r"\s+TO\s+(?P<grantee>[A-Za-z0-9_\"]+)"
)


class MetadataExtractError(Exception):
    """Raised when the package directory is missing or unreadable."""


def find_package_root(package_dir: str) -> str:
    """Resolve the directory that holds ``context/ships.build.json``.

    Accepts a directly-unpacked package, or a release-group directory with the
    main package in an immediate subdirectory (e.g. ``01_main/``).
    """
    if not os.path.isdir(package_dir):
        raise MetadataExtractError(f"package directory not found: {package_dir}")
    direct = os.path.join(package_dir, "context", "ships.build.json")
    if os.path.isfile(direct):
        return package_dir
    for entry in sorted(os.listdir(package_dir)):
        cand = os.path.join(package_dir, entry)
        if os.path.isdir(cand) and os.path.isfile(
            os.path.join(cand, "context", "ships.build.json")
        ):
            return cand
    raise MetadataExtractError(
        f"no SHIPS package (context/ships.build.json) found under: {package_dir}"
    )


def _load(root: str, relpath: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(root, relpath)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _identity(build: Dict[str, Any]) -> ProductIdentity:
    name = build.get("package_name") or "unknown"
    return ProductIdentity(
        product_id=str(name).lower(),
        product_name=str(name),
        version=str(build.get("build_number") or ""),
        environment=str(build.get("environment") or ""),
        description=build.get("description") or None,
        last_built_at=build.get("package_built_at") or build.get("timestamp"),
    )


def _interfaces_assets_lineage(
    deps: Optional[Dict[str, Any]], include_internal: bool
) -> Tuple[List[LogicalInterface], List[PhysicalAsset], List[LineageHint]]:
    interfaces: List[LogicalInterface] = []
    assets: List[PhysicalAsset] = []
    lineage: List[LineageHint] = []
    if not deps:
        return interfaces, assets, lineage

    for node in deps.get("nodes", []):
        obj_type = str(node.get("type", "")).upper()
        database = node.get("database", "")
        object_name = node.get("object_name", "")
        assets.append(
            PhysicalAsset(
                platform="teradata",
                database=database,
                object_name=object_name,
                object_type=obj_type or "UNKNOWN",
            )
        )
        consumer = obj_type in _CONSUMER_FACING
        if consumer or include_internal:
            interfaces.append(
                LogicalInterface(
                    interface_name=object_name,
                    interface_type=obj_type.lower() or "unknown",
                    platform="teradata",
                    database=database,
                    object_name=object_name,
                    consumer_facing=consumer,
                )
            )

    for edge in deps.get("edges", []):
        lineage.append(
            LineageHint(
                source=edge.get("source", ""),
                target=edge.get("target", ""),
                lineage_type=edge.get("type", "internal"),
            )
        )
    return interfaces, assets, lineage


def _columns_and_access(
    root: str,
) -> Tuple[List[ColumnMetadata], List[AccessRule]]:
    columns: List[ColumnMetadata] = []
    access: List[AccessRule] = []
    payload = os.path.join(root, "payload")
    if not os.path.isdir(payload):
        return columns, access

    for cur, _dirs, files in os.walk(payload):
        for f in sorted(files):
            path = os.path.join(cur, f)
            ext = os.path.splitext(f)[1].lower()
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue

            if ext in (".dcl", ".grt"):
                for m in _GRANT_RE.finditer(content):
                    for priv in m.group("priv").split(","):
                        priv = priv.strip().lower()
                        if priv:
                            access.append(
                                AccessRule(
                                    interface=m.group("obj").strip('"'),
                                    access_role=m.group("grantee").strip('"'),
                                    grant_type=priv,
                                )
                            )
                continue

            contract = extract_contract(content)
            if not contract or not contract.get("items"):
                continue
            asset = os.path.splitext(f)[0]  # eponymous DB.Object
            for item in contract["items"]:
                # TABLE items are {name,type}; VIEW/param items may be bare
                # column-name strings.
                if isinstance(item, dict):
                    name = item.get("name", "")
                    dtype = item.get("type")
                else:
                    name = str(item)
                    dtype = None
                if name:
                    columns.append(
                        ColumnMetadata(asset=asset, column_name=name, data_type=dtype)
                    )
    return columns, access


def _trust(
    trust: Optional[Dict[str, Any]], integrity: Optional[Dict[str, Any]]
) -> TrustState:
    state = TrustState()
    if trust:
        state.state = trust.get("status")
        state.blocking_signals = list(trust.get("blocking_signals", []))
        state.warning_signals = list(trust.get("warning_signals", []))
    if integrity:
        state.package_hash = integrity.get("package_hash")
        state.integrity_passed = bool(integrity.get("package_hash"))
    return state


def _provenance(
    build: Dict[str, Any], integrity: Optional[Dict[str, Any]]
) -> Provenance:
    return Provenance(
        package_name=build.get("package_name"),
        build_number=build.get("build_number"),
        environment=build.get("environment"),
        source_commit=build.get("source_commit") or None,
        built_by=build.get("author") or None,
        built_at=build.get("package_built_at") or build.get("timestamp"),
        package_hash=(integrity or {}).get("package_hash"),
    )


def _decisions(root: str) -> List[Decision]:
    doc = _load(root, "ships.decisions.json") or _load(
        root, os.path.join("context", "ships.decisions.json")
    )
    out: List[Decision] = []
    if not doc:
        return out
    raw = doc.get("decisions") if isinstance(doc, dict) else None
    for d in raw or []:
        if isinstance(d, dict):
            out.append(
                Decision(
                    decision_id=d.get("decision_id") or d.get("id"),
                    title=d.get("title"),
                    reason=d.get("reason"),
                    impact=d.get("impact"),
                    status=d.get("status"),
                )
            )
    return out


def extract_product_metadata(
    package_dir: str, include_internal: bool = False
) -> ProductMetadata:
    """Extract the neutral product metadata model from a SHIPS package."""
    root = find_package_root(package_dir)

    build = _load(root, os.path.join("context", "ships.build.json"))
    if build is None:
        raise MetadataExtractError("context/ships.build.json missing or unreadable")
    deps = _load(root, os.path.join("context", "ships.dependencies.json"))
    trust = _load(root, os.path.join("context", "ships.trust.json"))
    integrity = _load(root, os.path.join("context", "ships.integrity.json"))

    interfaces, assets, lineage = _interfaces_assets_lineage(deps, include_internal)
    columns, access = _columns_and_access(root)
    decisions = _decisions(root)

    meta = ProductMetadata(
        identity=_identity(build),
        interfaces=interfaces,
        physical_assets=assets,
        columns=columns,
        lineage=lineage,
        trust=_trust(trust, integrity),
        access=access,
        provenance=_provenance(build, integrity),
        decisions=decisions,
    )

    # -- conservative warnings for absent / unfabricated sections --
    if deps is None:
        meta.warnings.append(
            "ships.dependencies.json absent — no physical assets, interfaces, "
            "or lineage extracted."
        )
    if not meta.glossary_terms:
        meta.warnings.append(
            "No glossary terms in the package — glossary section left empty "
            "(not fabricated)."
        )
    if not meta.asset_mappings:
        meta.warnings.append(
            "No external (non-Teradata) asset mappings declared — section left empty."
        )
    if not decisions:
        meta.warnings.append(
            "No design decisions found in the package — decisions section left empty."
        )
    if not meta.identity.business_owner:
        meta.warnings.append(
            "No ownership/stewardship metadata in the package — owner fields "
            "omitted (not fabricated)."
        )
    return meta
