"""
Unit tests for td_release_packager.provenance.

Exercises every invariant the schema enforces and verifies JSON
roundtrip integrity. These tests are the safety net for the
provenance contract — if they pass, the v2 JSON shape is sound and
both the builder and the report can rely on it.
"""

import json
import os
import tempfile

import pytest

from database_package_deployer.provenance import (
    ProvenanceChain,
    ProvenanceDocument,
    SCHEMA_VERSION,
    STAGE_ORDER,
    Stage,
    Status,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


def _complete_chain(
    src: str = "src/x.tbl",
    final: str = "03_tables/x.tbl",
) -> ProvenanceChain:
    """Build a minimal valid four-stage chain for tests."""
    c = ProvenanceChain()
    c.add(Stage("source", src, Status.APPLIED))
    c.add(Stage("eponymous", src, Status.NO_OP, "filename already eponymous"))
    c.add(Stage("token_resolved", src, Status.NO_OP, "no tokens"))
    c.add(Stage("package", final, Status.APPLIED))
    return c


# -------------------------------------------------------------------
# Stage construction
# -------------------------------------------------------------------


class TestStage:
    """Stage-level invariants."""

    def test_applied_without_note_allowed(self):
        """Applied stages don't require a note."""
        s = Stage("source", "x.tbl", Status.APPLIED)
        assert s.note is None

    def test_no_op_without_note_rejected(self):
        """no_op without a note is a silent skip — must reject."""
        with pytest.raises(ValueError, match="no_op"):
            Stage("eponymous", "x.tbl", Status.NO_OP)

    def test_skipped_without_note_rejected(self):
        """skipped without a note is a silent skip — must reject."""
        with pytest.raises(ValueError, match="skipped"):
            Stage("eponymous", "x.tbl", Status.SKIPPED)

    def test_failed_without_note_rejected(self):
        """failed without a note hides debugging info — must reject."""
        with pytest.raises(ValueError, match="failed"):
            Stage("eponymous", "x.tbl", Status.FAILED)

    def test_unknown_stage_name_rejected(self):
        """Stages outside STAGE_ORDER must be rejected."""
        with pytest.raises(ValueError, match="Unknown stage"):
            Stage("not_a_real_stage", "x.tbl", Status.APPLIED)

    def test_to_dict_omits_none_note(self):
        """Applied stages serialise without a 'note' key when note is None."""
        s = Stage("source", "x.tbl", Status.APPLIED)
        d = s.to_dict()
        assert "note" not in d

    def test_to_dict_includes_note_when_set(self):
        """Stages with notes include them in the dict."""
        s = Stage("eponymous", "x.tbl", Status.NO_OP, "no qualified name")
        d = s.to_dict()
        assert d["note"] == "no qualified name"


# -------------------------------------------------------------------
# ProvenanceChain
# -------------------------------------------------------------------


class TestProvenanceChain:
    """Chain-level invariants."""

    def test_complete_chain_is_complete(self):
        c = _complete_chain()
        assert c.is_complete()

    def test_partial_chain_not_complete(self):
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        assert not c.is_complete()

    def test_out_of_order_stage_rejected(self):
        """Adding 'package' before 'eponymous' must fail."""
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        with pytest.raises(ValueError, match="out of order"):
            c.add(Stage("package", "x.tbl", Status.APPLIED))

    def test_duplicate_stage_rejected(self):
        """Adding 'source' twice must fail."""
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        with pytest.raises(ValueError, match="out of order"):
            c.add(Stage("source", "y.tbl", Status.APPLIED))

    def test_too_many_stages_rejected(self):
        """A fifth stage must be rejected — chain has fixed length."""
        c = _complete_chain()
        with pytest.raises(ValueError, match="maximum"):
            c.add(Stage("source", "x.tbl", Status.APPLIED))

    def test_final_path_on_incomplete_chain_raises(self):
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        with pytest.raises(ValueError, match="final_path"):
            c.final_path()

    def test_source_path_on_empty_chain_raises(self):
        c = ProvenanceChain()
        with pytest.raises(ValueError, match="source_path"):
            c.source_path()

    def test_final_path_returns_last_stage(self):
        c = _complete_chain(final="03_tables/x.tbl")
        assert c.final_path() == "03_tables/x.tbl"

    def test_source_path_returns_first_stage(self):
        c = _complete_chain(src="src/x.tbl")
        assert c.source_path() == "src/x.tbl"


# -------------------------------------------------------------------
# ProvenanceDocument
# -------------------------------------------------------------------


class TestProvenanceDocument:
    """Document-level invariants and serialisation."""

    def test_default_version_is_schema_version(self):
        d = ProvenanceDocument()
        assert d.version == SCHEMA_VERSION

    def test_add_incomplete_chain_rejected(self):
        c = ProvenanceChain()
        c.add(Stage("source", "x.tbl", Status.APPLIED))
        d = ProvenanceDocument()
        with pytest.raises(ValueError, match="incomplete"):
            d.add_chain(c)

    def test_duplicate_package_path_rejected(self):
        """Two chains with the same final path indicate a builder
        collision and must fail loudly per discipline rule 9."""
        d = ProvenanceDocument()
        d.add_chain(_complete_chain(final="03_tables/x.tbl"))
        with pytest.raises(ValueError, match="Duplicate package path"):
            d.add_chain(_complete_chain(final="03_tables/x.tbl"))

    def test_duplicate_error_names_both_sources(self):
        """The collision error must name BOTH source files so the
        DBA can immediately see which two files need disambiguating
        — names alone aren't enough, so the source-side paths from
        each chain's first stage have to appear in the message."""
        d = ProvenanceDocument()
        d.add_chain(
            _complete_chain(
                src="domain/grants/D01_MP_SEM_V.grt",
                final="02_dcl/inter_db/D01_MP_SEM_V.grt",
            )
        )

        with pytest.raises(ValueError) as exc_info:
            d.add_chain(
                _complete_chain(
                    src="semantic/grants/MortgagePlatform_Semantic.grt",
                    final="02_dcl/inter_db/D01_MP_SEM_V.grt",
                )
            )

        msg = str(exc_info.value)
        assert "domain/grants/D01_MP_SEM_V.grt" in msg, (
            "First source path missing from collision error"
        )
        assert "semantic/grants/MortgagePlatform_Semantic.grt" in msg, (
            "Second source path missing from collision error"
        )
        # Sanity: the destination should also still be there
        assert "02_dcl/inter_db/D01_MP_SEM_V.grt" in msg

    def test_distinct_paths_accepted(self):
        d = ProvenanceDocument()
        d.add_chain(_complete_chain(final="03_tables/x.tbl"))
        d.add_chain(_complete_chain(final="03_tables/y.tbl"))
        assert len(d.entries) == 2

    def test_to_dict_shape(self):
        """Serialised document has the canonical shape."""
        d = ProvenanceDocument()
        d.add_chain(_complete_chain(final="03_tables/x.tbl"))
        out = d.to_dict()

        assert out["version"] == SCHEMA_VERSION
        assert "generated_at" in out
        assert "entries" in out
        assert "03_tables/x.tbl" in out["entries"]
        assert len(out["entries"]["03_tables/x.tbl"]["stages"]) == len(STAGE_ORDER)


class TestRoundtrip:
    """JSON roundtrip integrity — write then read back."""

    def test_roundtrip_preserves_content(self):
        d = ProvenanceDocument()
        d.add_chain(_complete_chain(src="src/a.tbl", final="03_tables/A.tbl"))
        d.add_chain(_complete_chain(src="src/b.tbl", final="03_tables/B.tbl"))

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmppath = f.name
        try:
            d.write(tmppath)
            loaded = ProvenanceDocument.load(tmppath)

            assert loaded.version == d.version
            assert set(loaded.entries.keys()) == set(d.entries.keys())
            for key in d.entries:
                assert loaded.entries[key].source_path() == d.entries[key].source_path()
                assert loaded.entries[key].final_path() == d.entries[key].final_path()
        finally:
            os.unlink(tmppath)

    def test_load_rejects_wrong_version(self):
        """Loading v1 (or future v3) JSON must fail loudly."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"version": 1, "entries": {}}, f)
            tmppath = f.name
        try:
            with pytest.raises(ValueError, match="version mismatch"):
                ProvenanceDocument.load(tmppath)
        finally:
            os.unlink(tmppath)

    def test_load_rejects_missing_file(self):
        with pytest.raises(ValueError, match="not found"):
            ProvenanceDocument.load("/nonexistent/path.json")

    def test_load_handles_realistic_document(self):
        """End-to-end: write a multi-entry document, read it back,
        verify all stage details survive the roundtrip."""
        d = ProvenanceDocument()

        # Mix of statuses to exercise the serialisation paths
        c1 = ProvenanceChain()
        c1.add(Stage("source", "src/a.tbl", Status.APPLIED))
        c1.add(
            Stage(
                "eponymous",
                "src/A.tbl",
                Status.APPLIED,
                "Renamed from DDL: A",
            )
        )
        c1.add(
            Stage(
                "token_resolved",
                "src/A.tbl",
                Status.NO_OP,
                "no tokens",
            )
        )
        c1.add(Stage("package", "03_tables/A.tbl", Status.APPLIED))
        d.add_chain(c1)

        c2 = ProvenanceChain()
        c2.add(Stage("source", "src/b.viw", Status.APPLIED))
        c2.add(
            Stage(
                "eponymous",
                "src/b.viw",
                Status.FAILED,
                "Could not parse DDL header",
            )
        )
        c2.add(
            Stage(
                "token_resolved",
                "src/b.viw",
                Status.SKIPPED,
                "Upstream stage failed",
            )
        )
        c2.add(Stage("package", "04_views/b.viw", Status.APPLIED))
        d.add_chain(c2)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            tmppath = f.name
        try:
            d.write(tmppath)
            loaded = ProvenanceDocument.load(tmppath)

            # Verify the failed chain is preserved with its note intact
            failed = loaded.entries["04_views/b.viw"]
            eponymous = failed.stages[1]
            assert eponymous.status == Status.FAILED
            assert "parse" in eponymous.note.lower()

            # Verify status enum survives roundtrip
            for key, chain in loaded.entries.items():
                for s in chain.stages:
                    assert isinstance(s.status, Status)
        finally:
            os.unlink(tmppath)
