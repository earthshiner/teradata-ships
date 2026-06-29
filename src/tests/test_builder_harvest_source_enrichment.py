"""
Unit tests for ``_enrich_provenance_with_harvest_source`` (#477).

The helper is run by the builder right before writing
``context/ships.provenance.json`` — it loads
``<project>/.ships/harvest/source_map.json`` and stamps the
user-authored source path onto each chain whose post-harvest
``source`` stage is present in the map.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    Stage,
    Status,
)
from td_release_packager.builder import _enrich_provenance_with_harvest_source


def _chain(src_rel: str, final_rel: str) -> ProvenanceChain:
    c = ProvenanceChain()
    c.add(Stage("source", src_rel, Status.APPLIED))
    c.add(Stage("eponymous", src_rel, Status.NO_OP, "n"))
    c.add(Stage("token_resolved", src_rel, Status.NO_OP, "n"))
    c.add(Stage("package", final_rel, Status.APPLIED))
    return c


def _write_source_map(project: Path, entries: dict) -> None:
    sm_dir = project / ".ships" / "harvest"
    sm_dir.mkdir(parents=True, exist_ok=True)
    (sm_dir / "source_map.json").write_text(
        json.dumps({"schema_version": "1.0", "entries": entries}),
        encoding="utf-8",
    )


class TestHarvestSourceEnrichment:
    def test_chain_enriched_when_source_map_matches(self, tmp_path):
        """Source-stage path is ``database/...`` (payload-relative); the
        source_map keys it as ``payload/database/...``. The helper must
        normalise across that prefix gap."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_source_map(
            project,
            {
                "payload/database/system/roles/{{DB_PREFIX}}_ROLE_ADMIN.rol": {
                    "source_relpath": "90_access/CustomerDNA_ACCESS.role_admin.dcl",
                    "source_abspath": "/u/src/90_access/CustomerDNA_ACCESS.role_admin.dcl",
                    "type": "role",
                }
            },
        )

        doc = ProvenanceDocument()
        doc.add_chain(
            _chain(
                src_rel="database/system/roles/{{DB_PREFIX}}_ROLE_ADMIN.rol",
                final_rel="01_prereqs/00_system/roles/CustomerDNA_ROLE_ADMIN.rol",
            )
        )

        _enrich_provenance_with_harvest_source(doc, str(project))

        chain = next(iter(doc.entries.values()))
        assert chain.harvest_source == "90_access/CustomerDNA_ACCESS.role_admin.dcl"

    def test_no_source_map_is_a_noop(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        # No .ships/harvest/source_map.json at all.

        doc = ProvenanceDocument()
        doc.add_chain(
            _chain("database/x.tbl", "03_tables/x.tbl"),
        )
        _enrich_provenance_with_harvest_source(doc, str(project))

        chain = next(iter(doc.entries.values()))
        assert chain.harvest_source is None

    def test_unmapped_source_left_unset(self, tmp_path):
        """A chain whose source-stage path isn't in the map keeps
        ``harvest_source = None`` — silent partial enrichment is fine,
        we only stamp what we know."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_source_map(
            project,
            {
                "payload/database/other.tbl": {
                    "source_relpath": "src/other.sql",
                    "source_abspath": "/u/src/other.sql",
                    "type": "table",
                }
            },
        )

        doc = ProvenanceDocument()
        doc.add_chain(_chain("database/x.tbl", "03_tables/x.tbl"))
        _enrich_provenance_with_harvest_source(doc, str(project))

        chain = next(iter(doc.entries.values()))
        assert chain.harvest_source is None

    def test_malformed_source_map_is_a_noop(self, tmp_path):
        """A corrupt JSON file must not crash the build — provenance
        falls back to the unenriched chain."""
        project = tmp_path / "proj"
        (project / ".ships" / "harvest").mkdir(parents=True)
        (project / ".ships" / "harvest" / "source_map.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )

        doc = ProvenanceDocument()
        doc.add_chain(_chain("database/x.tbl", "03_tables/x.tbl"))
        _enrich_provenance_with_harvest_source(doc, str(project))

        chain = next(iter(doc.entries.values()))
        assert chain.harvest_source is None

    def test_existing_harvest_source_not_overwritten(self, tmp_path):
        """If a chain already carries a harvest_source (e.g. set by an
        earlier hook), enrichment must not overwrite it."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_source_map(
            project,
            {
                "payload/database/x.tbl": {
                    "source_relpath": "src/from_map.sql",
                    "source_abspath": "/u/src/from_map.sql",
                    "type": "table",
                }
            },
        )

        doc = ProvenanceDocument()
        chain = _chain("database/x.tbl", "03_tables/x.tbl")
        chain.harvest_source = "src/pre_set.sql"
        doc.add_chain(chain)

        _enrich_provenance_with_harvest_source(doc, str(project))

        assert doc.entries["03_tables/x.tbl"].harvest_source == "src/pre_set.sql"

    def test_windows_backslash_paths_normalised(self, tmp_path):
        """Source-stage paths recorded with backslashes still match
        forward-slash keys in source_map.json."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_source_map(
            project,
            {
                "payload/database/DDL/tables/foo.tbl": {
                    "source_relpath": "src\\foo.sql",
                    "source_abspath": "/u/src/foo.sql",
                    "type": "table",
                }
            },
        )

        doc = ProvenanceDocument()
        doc.add_chain(
            _chain("database\\DDL\\tables\\foo.tbl", "03_tables/foo.tbl"),
        )
        _enrich_provenance_with_harvest_source(doc, str(project))

        chain = next(iter(doc.entries.values()))
        # Forward-slash normalised on output regardless of how it was
        # stored in the source map.
        assert chain.harvest_source == "src/foo.sql"
