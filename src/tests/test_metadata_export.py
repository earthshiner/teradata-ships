"""
test_metadata_export.py — catalogue metadata export (#244).

Covers:
    - extract_product_metadata over a fabricated package (identity, interfaces
      vs internal assets, columns, lineage, trust, provenance, access, warnings)
    - the Alation renderer file set + content
    - the Collibra renderer resource graph
    - missing-metadata (warnings, non-strict) and blocked-trust scenarios
"""

import json
import os
from pathlib import Path

import pytest

from td_release_packager.metadata_export import (
    MetadataExtractError,
    extract_product_metadata,
)
from td_release_packager.metadata_export.alation import render as render_alation
from td_release_packager.metadata_export.collibra import render as render_collibra
from td_release_packager.metadata_export.datahub import render as render_datahub


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_package(
    tmp_path: Path,
    *,
    trust_status="READY_WITH_CAVEATS",
    with_deps=True,
) -> Path:
    root = tmp_path / "pkg" / "01_main"
    ctx = root / "context"
    _write(
        ctx / "ships.build.json",
        {
            "package_name": "CallCentre",
            "build_number": "0042",
            "environment": "DEV",
            "description": "Call centre data product.",
            "author": "ci",
            "source_commit": "abc1234",
            "package_built_at": "2026-05-28T00:00:00Z",
        },
    )
    if with_deps:
        _write(
            ctx / "ships.dependencies.json",
            {
                "nodes": [
                    {
                        "id": "CC_DOM_T.call_summary",
                        "type": "TABLE",
                        "database": "CC_DOM_T",
                        "object_name": "call_summary",
                    },
                    {
                        "id": "CC_DOM_BUS_V.call_summary_current",
                        "type": "VIEW",
                        "database": "CC_DOM_BUS_V",
                        "object_name": "call_summary_current",
                    },
                ],
                "edges": [
                    {
                        "source": "CC_DOM_T.call_summary",
                        "target": "CC_DOM_BUS_V.call_summary_current",
                        "type": "internal",
                    }
                ],
            },
        )
    _write(
        ctx / "ships.trust.json",
        {
            "status": trust_status,
            "blocking_signals": [],
            "warning_signals": ["inspect_lint"],
        },
    )
    _write(
        ctx / "ships.integrity.json",
        {"package_hash": "deadbeef", "algorithm": "SHA-256"},
    )

    # payload: one table (internal) + one view (interface) + a DCL grant
    pay = root / "payload"
    (pay / "03_ddl" / "tables").mkdir(parents=True, exist_ok=True)
    (pay / "03_ddl" / "views").mkdir(parents=True, exist_ok=True)
    (pay / "02_dcl").mkdir(parents=True, exist_ok=True)
    (pay / "03_ddl" / "tables" / "CC_DOM_T.call_summary.tbl").write_text(
        "CREATE MULTISET TABLE CC_DOM_T.call_summary "
        "(customer_id VARCHAR(50), calls INTEGER) PRIMARY INDEX (customer_id);\n",
        encoding="utf-8",
    )
    (pay / "03_ddl" / "views" / "CC_DOM_BUS_V.call_summary_current.viw").write_text(
        "REPLACE VIEW CC_DOM_BUS_V.call_summary_current "
        "(customer_id, calls) AS SELECT customer_id, calls FROM CC_DOM_T.call_summary;\n",
        encoding="utf-8",
    )
    (pay / "02_dcl" / "reader.dcl").write_text(
        "GRANT SELECT ON CC_DOM_BUS_V.call_summary_current TO CALLCENTRE_READER;\n",
        encoding="utf-8",
    )
    return tmp_path / "pkg"


# ---------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------


class TestExtract:
    def test_identity(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        assert meta.identity.product_id == "callcentre"
        assert meta.identity.version == "0042"
        assert meta.identity.environment == "DEV"

    def test_interfaces_vs_internal(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        names = {i.object_name: i.consumer_facing for i in meta.interfaces}
        # View is a consumer-facing interface; table is internal (excluded).
        assert names.get("call_summary_current") is True
        assert "call_summary" not in names
        assert len(meta.physical_assets) == 2  # both assets still listed

    def test_include_internal(self, tmp_path):
        meta = extract_product_metadata(
            str(_make_package(tmp_path)), include_internal=True
        )
        names = {i.object_name for i in meta.interfaces}
        assert "call_summary" in names and "call_summary_current" in names

    def test_columns_extracted(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        cols = {(c.asset, c.column_name) for c in meta.columns}
        assert ("CC_DOM_T.call_summary", "customer_id") in cols

    def test_lineage(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        assert any(
            ln.source == "CC_DOM_T.call_summary"
            and ln.target == "CC_DOM_BUS_V.call_summary_current"
            for ln in meta.lineage
        )

    def test_trust_and_provenance(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        assert meta.trust.state == "READY_WITH_CAVEATS"
        assert meta.trust.package_hash == "deadbeef"
        assert meta.trust.integrity_passed is True
        assert meta.provenance.source_commit == "abc1234"

    def test_access_from_dcl(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        assert any(
            a.access_role == "CALLCENTRE_READER" and a.grant_type == "select"
            for a in meta.access
        )

    def test_warnings_for_missing(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        joined = " ".join(meta.warnings).lower()
        assert "glossary" in joined and "owner" in joined

    def test_missing_deps_warns(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path, with_deps=False)))
        assert not meta.interfaces
        assert any("dependencies" in w for w in meta.warnings)

    def test_missing_package_raises(self, tmp_path):
        with pytest.raises(MetadataExtractError):
            extract_product_metadata(str(tmp_path / "nope"))


# ---------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------


class TestAlationRenderer:
    def test_file_set_and_manifest(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        bundle = render_alation(meta, "2026-05-28T00:00:00Z")
        for f in (
            "data_product.json",
            "logical_interfaces.json",
            "physical_mappings.json",
            "column_metadata.json",
            "glossary_terms.json",
            "lineage.json",
            "quality_and_trust.json",
            "access_model.json",
            "provenance.json",
            "decisions.json",
            "manifest.json",
        ):
            assert f in bundle
        assert bundle["manifest.json"]["bundle_type"] == "alation_metadata_export"
        assert bundle["data_product.json"]["product_id"] == "callcentre"
        assert (
            bundle["quality_and_trust.json"]["trust"]["state"] == "READY_WITH_CAVEATS"
        )


class TestCollibraRenderer:
    def test_resource_graph(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        bundle = render_collibra(meta, "2026-05-28T00:00:00Z")
        assert bundle["manifest.json"]["bundle_type"] == "collibra_metadata_export"
        resources = bundle["collibra_import.json"]["resources"]
        kinds = [r["resourceType"] for r in resources]
        assert "Community" in kinds and "Domain" in kinds and "Asset" in kinds
        # A product asset and the view interface asset are present.
        names = [r.get("identifier", {}).get("name") for r in resources]
        assert "CallCentre" in names
        assert "CC_DOM_BUS_V.call_summary_current" in names
        # Lineage rendered as a Relation resource.
        assert any(r["resourceType"] == "Relation" for r in resources)


class TestDataHubRenderer:
    def test_mcp_envelope_and_entities(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        bundle = render_datahub(meta, "2026-05-28T00:00:00Z")
        assert bundle["manifest.json"]["bundle_type"] == "datahub_metadata_export"
        mcps = bundle["datahub_mcps.json"]["proposals"]
        # Every MCP uses the file-emitter envelope.
        assert all(m["changeType"] == "UPSERT" and "json" in m["aspect"] for m in mcps)
        aspects = {m["aspectName"] for m in mcps}
        assert {"datasetProperties", "subTypes", "schemaMetadata"} <= aspects
        # A dataProduct entity groups the assets.
        dp = [m for m in mcps if m["entityType"] == "dataProduct"]
        assert dp and dp[0]["aspect"]["json"]["assets"]

    def test_dataset_urn_and_lineage(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        mcps = render_datahub(meta, "t")["datahub_mcps.json"]["proposals"]
        urns = {m["entityUrn"] for m in mcps}
        assert (
            "urn:li:dataset:(urn:li:dataPlatform:teradata,"
            "CC_DOM_BUS_V.call_summary_current,DEV)"
        ) in urns
        lineage = [m for m in mcps if m["aspectName"] == "upstreamLineage"]
        assert lineage
        assert lineage[0]["aspect"]["json"]["upstreams"][0]["type"] == "TRANSFORMED"


# ---------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------


class TestScenarios:
    def test_blocked_trust(self, tmp_path):
        meta = extract_product_metadata(
            str(_make_package(tmp_path, trust_status="BLOCKED"))
        )
        bundle = render_alation(meta, "t")
        assert bundle["quality_and_trust.json"]["trust"]["state"] == "BLOCKED"

    def test_both_renderers_share_one_extraction(self, tmp_path):
        meta = extract_product_metadata(str(_make_package(tmp_path)))
        a = render_alation(meta, "t")
        c = render_collibra(meta, "t")
        # Same provenance hash flows into both bundles from one extraction.
        assert a["provenance.json"]["deployment"]["package_hash"] == "deadbeef"
        assert c["provenance.json"]["package_hash"] == "deadbeef"
