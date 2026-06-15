"""
test_token_map_identifier_aware.py — Regression tests for #311.

Real-data verification of Model B failed when `--token-map` was used
instead of the new `--prefix-token` flag — qualified database
identifiers were left literal across tables, views and database DDL,
and the one standalone reference was mangled into ``{{PREFIX_T}}``.

These tests verify the fix for the ``--token-map`` path:

* ``_detect_prefix_mode_literals`` classifies a literal as prefix mode
  when it appears as a leading identifier segment in any source file.
* ``_apply_kind_aware_tokens`` routes prefix-mode literals through the
  identifier-aware path with no kind suffix.
* The three real-data fixture lines from the defect handoff §6
  produce the required output.
* The ``{{PREFIX_`` regression guard: a payload-wide scan after
  substitution returns 0 hits.
* ``scan_payload_databases`` now surfaces view-target databases
  (extension list under-reported them previously).
"""

from __future__ import annotations

import os
from pathlib import Path

from td_release_packager.ingest import (
    _apply_kind_aware_tokens,
    _detect_prefix_mode_literals,
)


# ---------------------------------------------------------------------------
# _detect_prefix_mode_literals
# ---------------------------------------------------------------------------


class TestDetectPrefixMode:
    def test_literal_with_leading_segment_is_classified_prefix(self, tmp_path: Path):
        src = tmp_path / "CallCentre_DOM_STD_T.Call_H.tbl"
        src.write_text(
            "CREATE MULTISET TABLE CallCentre_DOM_STD_T.Call_H ,FALLBACK;\n",
            encoding="utf-8",
        )
        found = _detect_prefix_mode_literals([str(src)], {"CallCentre": "{{PREFIX}}"})
        assert found == {"CallCentre"}

    def test_literal_only_standalone_is_not_prefix(self, tmp_path: Path):
        # The literal appears only as a whole-name reference, never as a
        # leading segment — classic full-DB token-map shape.
        src = tmp_path / "stmt.sql"
        src.write_text("CREATE TABLE A_D01_STD.T (id INTEGER);\n", encoding="utf-8")
        found = _detect_prefix_mode_literals(
            [str(src)], {"A_D01_STD": "{{STD_DATABASE}}"}
        )
        assert found == set()

    def test_negative_boundaries_not_matched(self, tmp_path: Path):
        # ``XCallCentre_X`` must not promote ``CallCentre`` to prefix mode.
        src = tmp_path / "stmt.sql"
        src.write_text(
            "CREATE TABLE XCallCentre_DOM.Q (id INTEGER);\n", encoding="utf-8"
        )
        found = _detect_prefix_mode_literals([str(src)], {"CallCentre": "{{PREFIX}}"})
        assert found == set()

    def test_empty_apply_tokens_returns_empty(self):
        assert _detect_prefix_mode_literals([], {}) == set()

    def test_short_literal_extended_by_longer_in_same_map_excluded(
        self, tmp_path: Path
    ):
        """When the user explicitly maps both ``A_B`` and ``A_B_V``,
        ``A_B`` is a full-DB entry, not a prefix shape — the longer
        literal extends it.  Classifying the short one as prefix-mode
        would let it eat ``A_B_V`` matches (issue #311 regression seen
        in TestHarvesterWordBoundarySubstitution)."""
        src = tmp_path / "stmt.sql"
        src.write_text(
            "CREATE VIEW MortgagePlatform_Domain_V.v AS "
            "SELECT 1 FROM MortgagePlatform_Domain.t;\n",
            encoding="utf-8",
        )
        found = _detect_prefix_mode_literals(
            [str(src)],
            {
                "MortgagePlatform_Domain": "{{DOM_DATABASE_T}}",
                "MortgagePlatform_Domain_V": "{{DOM_DATABASE_V}}",
            },
        )
        # The short one is explicitly extended by the longer one in
        # the same map — both stay in full-DB mode.
        assert found == set()


# ---------------------------------------------------------------------------
# _apply_kind_aware_tokens — prefix mode emits token verbatim
# ---------------------------------------------------------------------------


class TestApplyKindAwarePrefixMode:
    APPLY = {"CallCentre": "{{PREFIX}}"}
    PREFIX_MODE = {"CallCentre"}

    def test_create_database_line(self):
        # Real-data fixture from defect handoff §6.
        src = (
            "create database CallCentre_DOM_STD_T from CallCentre "
            "as perm = 0.0 spool = 1.4E9 fallback ;"
        )
        out = _apply_kind_aware_tokens(
            src, "T", self.APPLY, {}, prefix_mode_literals=self.PREFIX_MODE
        )
        assert out == (
            "create database {{PREFIX}}_DOM_STD_T from {{PREFIX}} "
            "as perm = 0.0 spool = 1.4E9 fallback ;"
        )
        assert "{{PREFIX_" not in out

    def test_create_table_line(self):
        src = "CREATE MULTISET TABLE CallCentre_DOM_STD_T.Call_H ,FALLBACK ,"
        out = _apply_kind_aware_tokens(
            src, "T", self.APPLY, {}, prefix_mode_literals=self.PREFIX_MODE
        )
        assert out == "CREATE MULTISET TABLE {{PREFIX}}_DOM_STD_T.Call_H ,FALLBACK ,"
        assert "{{PREFIX_" not in out

    def test_replace_view_line(self):
        src = "REPLACE VIEW CallCentre_DOM_STD_V.Call_H"
        out = _apply_kind_aware_tokens(
            src, "V", self.APPLY, {}, prefix_mode_literals=self.PREFIX_MODE
        )
        assert out == "REPLACE VIEW {{PREFIX}}_DOM_STD_V.Call_H"
        assert "{{PREFIX_" not in out

    def test_no_kind_suffix_even_when_file_kind_is_table(self):
        # Standalone reference in a .tbl context — historically the
        # kind-aware path appended ``_T`` inside the braces, producing
        # the ``{{PREFIX_T}}`` malformation.  Prefix mode must NOT do
        # that.
        src = "GRANT SELECT ON CallCentre TO bob;"
        out = _apply_kind_aware_tokens(
            src, "T", self.APPLY, {}, prefix_mode_literals=self.PREFIX_MODE
        )
        assert "{{PREFIX_" not in out
        assert "{{PREFIX}}" in out

    def test_full_db_mode_keeps_kind_suffix(self):
        # When the literal is NOT in prefix_mode_literals it falls
        # through to the existing kind-aware behaviour — token gets
        # the ``_T`` suffix in a table context.
        src = "CREATE TABLE A_D01_STD.T (id INTEGER);"
        out = _apply_kind_aware_tokens(
            src,
            "T",
            {"A_D01_STD": "{{STD_DATABASE}}"},
            {"a_d01_std.t": "T"},
            prefix_mode_literals=set(),
        )
        assert "{{STD_DATABASE_T}}" in out

    def test_negative_boundary_not_substituted(self):
        # ``XCallCentre_DOM`` must not become ``X{{PREFIX}}_DOM``.
        src = "CREATE TABLE XCallCentre_DOM.Q (id INTEGER);"
        out = _apply_kind_aware_tokens(
            src, "T", self.APPLY, {}, prefix_mode_literals=self.PREFIX_MODE
        )
        assert out == src


# ---------------------------------------------------------------------------
# Payload-wide regression guard: no {{PREFIX_ after substitution
# ---------------------------------------------------------------------------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _read_all(p: Path) -> str:
    blobs = []
    for root, _dirs, files in os.walk(p):
        for name in files:
            blobs.append((Path(root) / name).read_text(encoding="utf-8"))
    return "\n".join(blobs)


class TestPayloadRegressionGuard:
    def test_end_to_end_no_malformed_token(self, tmp_path: Path):
        """End-to-end ``ingest_directory`` regression: scan every
        placed file under ``payload/`` after harvest and verify
        ``{{PREFIX_`` never appears."""
        from td_release_packager.ingest import ingest_directory

        # Scaffold a minimal SHIPS project tree.
        project = tmp_path / "proj"
        (project / "payload" / "database" / "DDL" / "tables").mkdir(
            parents=True, exist_ok=True
        )
        (project / "payload" / "database" / "DDL" / "views").mkdir(
            parents=True, exist_ok=True
        )
        (project / "payload" / "database" / "pre-requisites" / "databases").mkdir(
            parents=True, exist_ok=True
        )

        source = tmp_path / "src"
        _write(
            source / "CallCentre_DOM_STD_T.Call_H.tbl",
            "CREATE MULTISET TABLE CallCentre_DOM_STD_T.Call_H "
            "(Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        _write(
            source / "CallCentre_DOM_STD_V.Call_H.viw",
            "REPLACE VIEW CallCentre_DOM_STD_V.Call_H AS SELECT 1;\n",
        )
        _write(
            source / "CallCentre_DOM_STD_T.db",
            "create database CallCentre_DOM_STD_T from CallCentre "
            "as perm = 0.0 spool = 1.4E9 fallback ;\n",
        )

        # token_map.conf shape: CallCentre = {{PREFIX}}
        ingest_directory(
            source_dir=str(source),
            project_dir=str(project),
            detect_tokens=False,
            apply_tokens={"CallCentre": "{{PREFIX}}"},
        )

        payload_text = _read_all(project / "payload")
        assert "{{PREFIX_" not in payload_text, (
            "Malformed {{PREFIX_*}} token in payload: "
            + payload_text[: payload_text.find("{{PREFIX_") + 200]
        )
        # And the prefix-mode substitution actually fired.
        assert "{{PREFIX}}_DOM_STD_T" in payload_text
        assert "{{PREFIX}}_DOM_STD_V" in payload_text


# ---------------------------------------------------------------------------
# Candidate analyser — extension list now includes .viw
# ---------------------------------------------------------------------------


class TestScanPayloadDatabases:
    def test_view_file_extension_is_scanned(self, tmp_path: Path):
        """``REPLACE VIEW <db>.<obj>`` files use the ``.viw`` extension;
        the candidate analyser must list them so view-target databases
        appear among the un-tokenised literals."""
        from td_release_packager.mcp_authoring import scan_payload_databases

        project = tmp_path / "p"
        views_dir = project / "payload" / "database" / "DDL" / "views"
        views_dir.mkdir(parents=True, exist_ok=True)
        _write(
            views_dir / "CallCentre_DOM_STD_V.Call_H.viw",
            "REPLACE VIEW CallCentre_DOM_STD_V.Call_H AS SELECT 1;\n",
        )

        out = scan_payload_databases(str(project))
        assert "CallCentre_DOM_STD_V" in out, (
            "view-target database missed by scan_payload_databases (#311)"
        )
