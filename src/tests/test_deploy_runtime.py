"""
test_deploy_runtime.py — Unit tests for database_package_deployer.deploy_runtime.

These tests validate the ordering logic and file-discovery logic directly,
without building a package. This is the point of the refactor: logic bugs
in PHASE_SUBDIR_ORDERS or discover_files previously only surfaced at
deploy time, not in CI.

Covers:
    - PHASE_SUBDIR_ORDERS: jar_install before procedures (historical bug)
    - PHASE_SUBDIR_ORDERS: tables before views before triggers
    - discover_files: correct phase ordering for 03_ddl
    - discover_files: unknown sub-dirs sort to the end
    - discover_files: control files (_order.txt, .gitkeep) are skipped
    - discover_files: alphabetical sort within each sub-directory
    - read_order_file: reads filenames in listed order
    - read_order_file: skips blank lines and # comments
    - read_order_file: missing files are skipped with a warning
"""

from __future__ import annotations

import os
from pathlib import Path


from database_package_deployer.deploy_runtime import (
    PHASE_SUBDIR_ORDERS,
    discover_files,
    read_order_file,
)


# ---------------------------------------------------------------
# PHASE_SUBDIR_ORDERS — ordering map correctness
# ---------------------------------------------------------------


class TestPhaseSubdirOrders:
    def test_jar_install_before_procedures(self):
        """JAR install scripts must deploy before Java procedures that use them."""
        ddl = PHASE_SUBDIR_ORDERS["03_ddl"]
        assert ddl["jar_install"] < ddl["procedures"], (
            "jar_install must sort before procedures in 03_ddl"
        )

    def test_tables_before_views(self):
        """Views depend on tables — tables must deploy first."""
        ddl = PHASE_SUBDIR_ORDERS["03_ddl"]
        assert ddl["tables"] < ddl["views"]

    def test_views_before_procedures(self):
        """Procedures may call views — views should deploy first."""
        ddl = PHASE_SUBDIR_ORDERS["03_ddl"]
        assert ddl["views"] < ddl["procedures"]

    def test_views_before_triggers(self):
        """Triggers fire on tables (same layer), after views."""
        ddl = PHASE_SUBDIR_ORDERS["03_ddl"]
        assert ddl["views"] < ddl["triggers"]

    def test_tables_before_indexes(self):
        """Indexes must be created after their base table."""
        ddl = PHASE_SUBDIR_ORDERS["03_ddl"]
        assert ddl["tables"] < ddl["join_indexes"]
        assert ddl["tables"] < ddl["hash_indexes"]
        assert ddl["tables"] < ddl["secondary_indexes"]

    def test_dcl_roles_before_users(self):
        """Roles must exist before they can be granted to users."""
        dcl = PHASE_SUBDIR_ORDERS["02_dcl"]
        assert dcl["roles"] < dcl["users"]

    def test_all_three_phases_present(self):
        assert "00_system" in PHASE_SUBDIR_ORDERS
        assert "02_dcl" in PHASE_SUBDIR_ORDERS
        assert "03_ddl" in PHASE_SUBDIR_ORDERS


# ---------------------------------------------------------------
# discover_files — ordering and filtering
# ---------------------------------------------------------------


def _make_phase(tmp_path: Path, phase_name: str, structure: dict) -> Path:
    """
    Create a phase directory tree.

    structure: {subdir_name: [filename, ...]}
    Returns the phase directory path.
    """
    phase = tmp_path / phase_name
    for subdir, files in structure.items():
        (phase / subdir).mkdir(parents=True, exist_ok=True)
        for fname in files:
            (phase / subdir / fname).write_text("-- ddl\n", encoding="utf-8")
    return phase


class TestDiscoverFiles:
    def test_03_ddl_tables_before_views(self, tmp_path):
        """Tables sub-dir must come before views in 03_ddl phase."""
        phase = _make_phase(
            tmp_path,
            "03_ddl",
            {
                "views": ["V.viw"],
                "tables": ["T.tbl"],
            },
        )
        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        assert names.index("T.tbl") < names.index("V.viw")

    def test_03_ddl_jar_install_before_procedures(self, tmp_path):
        """jar_install must come before procedures in 03_ddl phase."""
        phase = _make_phase(
            tmp_path,
            "03_ddl",
            {
                "procedures": ["P.spl"],
                "jar_install": ["J.sjr"],
            },
        )
        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        assert names.index("J.sjr") < names.index("P.spl")

    def test_unknown_subdir_sorts_to_end(self, tmp_path):
        """Sub-directories not in the map appear after known ones."""
        phase = _make_phase(
            tmp_path,
            "03_ddl",
            {
                "tables": ["T.tbl"],
                "zzz_custom": ["custom.sql"],
            },
        )
        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        assert names.index("T.tbl") < names.index("custom.sql")

    def test_control_files_skipped(self, tmp_path):
        """Files starting with _ or . must be excluded."""
        phase = tmp_path / "03_ddl"
        subdir = phase / "tables"
        subdir.mkdir(parents=True)
        (subdir / "_order.txt").write_text("T.tbl\n", encoding="utf-8")
        (subdir / ".gitkeep").write_text("", encoding="utf-8")
        (subdir / "T.tbl").write_text("-- ddl\n", encoding="utf-8")

        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        assert "_order.txt" not in names
        assert ".gitkeep" not in names
        assert "T.tbl" in names

    def test_alphabetical_within_subdir(self, tmp_path):
        """Within a sub-directory, files sort alphabetically."""
        phase = _make_phase(
            tmp_path,
            "03_ddl",
            {
                "tables": ["Z.tbl", "A.tbl", "M.tbl"],
            },
        )
        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        assert names == ["A.tbl", "M.tbl", "Z.tbl"]

    def test_empty_phase_returns_empty_list(self, tmp_path):
        """An empty phase directory returns an empty list."""
        phase = tmp_path / "03_ddl"
        phase.mkdir()
        assert discover_files(str(phase)) == []

    def test_unknown_phase_name_uses_alphabetical(self, tmp_path):
        """A phase not in PHASE_SUBDIR_ORDERS uses alphabetical sub-dir ordering."""
        phase = _make_phase(
            tmp_path,
            "99_custom",
            {
                "zzz": ["Z.sql"],
                "aaa": ["A.sql"],
            },
        )
        files = discover_files(str(phase))
        names = [os.path.basename(f) for f in files]
        # aaa < zzz alphabetically → A.sql first
        assert names.index("A.sql") < names.index("Z.sql")


# ---------------------------------------------------------------
# read_order_file
# ---------------------------------------------------------------


class TestReadOrderFile:
    def test_reads_files_in_listed_order(self, tmp_path):
        """Files are returned in the order listed in the control file."""
        (tmp_path / "B.tbl").write_text("", encoding="utf-8")
        (tmp_path / "A.tbl").write_text("", encoding="utf-8")
        order = tmp_path / "_order.txt"
        order.write_text("B.tbl\nA.tbl\n", encoding="utf-8")

        result = read_order_file(str(order), str(tmp_path))
        names = [os.path.basename(f) for f in result]
        assert names == ["B.tbl", "A.tbl"]

    def test_skips_blank_lines_and_comments(self, tmp_path):
        """Blank lines and # comments are ignored."""
        (tmp_path / "T.tbl").write_text("", encoding="utf-8")
        order = tmp_path / "_order.txt"
        order.write_text(
            "# This is a comment\n\nT.tbl\n\n# Another comment\n",
            encoding="utf-8",
        )
        result = read_order_file(str(order), str(tmp_path))
        assert len(result) == 1
        assert os.path.basename(result[0]) == "T.tbl"

    def test_missing_file_skipped_with_warning(self, tmp_path, caplog):
        """Files listed in _order.txt but absent from disk are skipped."""
        (tmp_path / "exists.tbl").write_text("", encoding="utf-8")
        order = tmp_path / "_order.txt"
        order.write_text("exists.tbl\nmissing.tbl\n", encoding="utf-8")

        import logging

        with caplog.at_level(
            logging.WARNING, logger="database_package_deployer.deploy_runtime"
        ):
            result = read_order_file(str(order), str(tmp_path))

        names = [os.path.basename(f) for f in result]
        assert names == ["exists.tbl"]
        assert any("missing.tbl" in r.message for r in caplog.records)

    def test_empty_order_file_returns_empty_list(self, tmp_path):
        order = tmp_path / "_order.txt"
        order.write_text("", encoding="utf-8")
        assert read_order_file(str(order), str(tmp_path)) == []
