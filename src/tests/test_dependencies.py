"""
test_dependencies.py — Tests for ships.dependencies.json (#150).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from td_release_packager.dependencies import (
    DEPENDENCIES_RESULT_FILENAME,
    DEPENDENCIES_RESULT_REF,
    DEPENDENCIES_SCHEMA_VERSION,
    compute_dependencies_document,
    load_dependencies_result,
    write_dependencies_result,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_project(tmp_path):
    project = tmp_path / "project"
    for sub in (
        "payload/database/DDL/tables",
        "payload/database/DDL/views",
        "config/env",
    ):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / ".ships").mkdir(parents=True, exist_ok=True)
    (project / ".ships" / ".build_counter").write_text("0\n", encoding="utf-8")
    return project


def _seed_table_and_view(project: Path) -> None:
    _write(
        project / "payload/database/DDL/tables/DB.T.tbl",
        "CREATE MULTISET TABLE DB.T (Id INTEGER) PRIMARY INDEX (Id);\n",
    )
    _write(
        project / "payload/database/DDL/views/DB.V.viw",
        "REPLACE VIEW DB.V AS SELECT * FROM DB.T;\n",
    )


# ---------------------------------------------------------------
# Document shape
# ---------------------------------------------------------------


class TestDocumentShape:
    def test_schema_version_emitted(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        assert doc["schema_version"] == DEPENDENCIES_SCHEMA_VERSION

    def test_top_level_keys(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        for key in (
            "schema_version",
            "metadata",
            "nodes",
            "edges",
            "waves",
            "cycles",
            "external_dependencies",
        ):
            assert key in doc

    def test_metadata_counts_present(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        meta = doc["metadata"]
        for key in (
            "generator",
            "generated_at",
            "object_count",
            "edge_count",
            "wave_count",
            "cycle_count",
        ):
            assert key in meta

    def test_nodes_carry_object_metadata(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        ids = {n["id"] for n in doc["nodes"]}
        assert "DB.T" in ids
        assert "DB.V" in ids

    def test_view_depends_on_table(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        edge = next(
            (
                e
                for e in doc["edges"]
                if e["source"] == "DB.T" and e["target"] == "DB.V"
            ),
            None,
        )
        assert edge is not None, "expected DB.T -> DB.V internal edge"
        assert edge["type"] == "internal"

    def test_waves_place_table_before_view(self, tmp_project):
        _seed_table_and_view(tmp_project)
        doc = compute_dependencies_document(str(tmp_project))
        table_wave = next(n["wave"] for n in doc["nodes"] if n["id"] == "DB.T")
        view_wave = next(n["wave"] for n in doc["nodes"] if n["id"] == "DB.V")
        assert table_wave < view_wave


# ---------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------


class TestRoundTrip:
    def test_write_then_load(self, tmp_path, tmp_project):
        _seed_table_and_view(tmp_project)
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        path = write_dependencies_result(str(pkg_dir), str(tmp_project))
        assert path.endswith(DEPENDENCIES_RESULT_FILENAME)
        loaded = load_dependencies_result(str(pkg_dir))
        assert loaded["schema_version"] == DEPENDENCIES_SCHEMA_VERSION

    def test_load_returns_none_when_absent(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        assert load_dependencies_result(str(pkg_dir)) is None


# ---------------------------------------------------------------
# Integration: build_package emits the canonical file
# ---------------------------------------------------------------


class TestBuildPackageEmitsDependencies:
    def test_dependencies_json_in_archive_and_pointer_in_build(
        self, tmp_path, tmp_project
    ):
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        _seed_table_and_view(tmp_project)
        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")

        cfg = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="TestPkg",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_arc, _manifest), _companion = build_package(cfg)

        with zipfile.ZipFile(main_arc) as zf:
            dep_name = next(
                n for n in zf.namelist() if n.endswith("ships.dependencies.json")
            )
            doc = json.loads(zf.read(dep_name))
            build_name = next(
                n for n in zf.namelist() if n.endswith("ships.build.json")
            )
            build_data = json.loads(zf.read(build_name))
            schema_name = next(
                n for n in zf.namelist() if n.endswith("ships.dependencies.schema.json")
            )
            schema = json.loads(zf.read(schema_name))
            index_name = next(
                n for n in zf.namelist() if n.endswith("ships.index.json")
            )
            index = json.loads(zf.read(index_name))

        assert build_data.get("dependencies_ref") == DEPENDENCIES_RESULT_REF
        assert doc["schema_version"] == DEPENDENCIES_SCHEMA_VERSION
        # Same JSON shape as `analyze --formats json`.
        assert "nodes" in doc and "edges" in doc and "waves" in doc
        # Schema was published alongside the doc.
        assert schema["$id"].endswith("ships.dependencies.schema.json")
        # Index references the dependencies entrypoint.
        assert "dependencies" in index["entrypoints"]
        assert index["entrypoints"]["dependencies"]["path"].endswith(
            "ships.dependencies.json"
        )
