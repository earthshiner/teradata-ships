"""
test_discovery_extensions_stamp.py — Tests for issue #50.

Verifies that the resolved discovery extension set is stamped into
ships.build.json at build time, and that the embedded deployer reads it
back at startup instead of using a hard-coded list.

Covers:
    - _load_build_extensions: absent ships.build.json → None
    - _load_build_extensions: missing discovery block → None
    - _load_build_extensions: non-list extensions → None
    - _load_build_extensions: malformed JSON → None
    - _load_build_extensions: valid extensions → list returned
    - build_package: ships.build.json contains discovery.extensions (defaults)
    - build_package: custom ships.yaml extensions appear in ships.build.json
    - auto-split: both archives preserve discovery.extensions
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path


from database_package_deployer.deployer import _load_build_extensions
from td_release_packager.builder import build_package
from td_release_packager.discovery import DEFAULT_HARVEST_EXTENSIONS
from td_release_packager.models import BuildConfig


# ---------------------------------------------------------------
# Helpers shared with test_builder_auto_split
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _properties_for(env: str, tmp_path: Path) -> Path:
    props_path = tmp_path / f"{env}.conf"
    props_path.write_text(f"SHIPS_ENV={env}\n", encoding="utf-8")
    return props_path


def _read_zip_build_json(archive_path: str) -> dict:
    """Extract and parse the ships.build.json from an archive."""
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if name.endswith("ships.build.json"):
                return json.loads(zf.read(name).decode("utf-8"))
    raise FileNotFoundError(f"No ships.build.json found in {archive_path}")


# ---------------------------------------------------------------
# _load_build_extensions — unit tests
# ---------------------------------------------------------------


class TestLoadBuildExtensions:
    """Unit tests for the ships.build.json extension reader in the deployer."""

    def test_returns_none_when_no_build_json(self, tmp_path):
        result = _load_build_extensions(str(tmp_path))
        assert result is None

    def test_returns_none_when_discovery_block_absent(self, tmp_path):
        (tmp_path / "ships.build.json").write_text(
            json.dumps({"build_number": "0001"}), encoding="utf-8"
        )
        assert _load_build_extensions(str(tmp_path)) is None

    def test_returns_none_when_extensions_not_a_list(self, tmp_path):
        (tmp_path / "ships.build.json").write_text(
            json.dumps({"discovery": {"extensions": ".sql"}}), encoding="utf-8"
        )
        assert _load_build_extensions(str(tmp_path)) is None

    def test_returns_none_when_extensions_contains_non_string(self, tmp_path):
        (tmp_path / "ships.build.json").write_text(
            json.dumps({"discovery": {"extensions": [".sql", 42]}}), encoding="utf-8"
        )
        assert _load_build_extensions(str(tmp_path)) is None

    def test_returns_none_on_malformed_json(self, tmp_path):
        (tmp_path / "ships.build.json").write_text("{not valid json}", encoding="utf-8")
        assert _load_build_extensions(str(tmp_path)) is None

    def test_returns_extension_list_when_valid(self, tmp_path):
        exts = [".bteq", ".sql", ".tbl"]
        (tmp_path / "ships.build.json").write_text(
            json.dumps({"discovery": {"extensions": exts}}), encoding="utf-8"
        )
        result = _load_build_extensions(str(tmp_path))
        assert result == exts

    def test_returns_empty_list_when_extensions_empty(self, tmp_path):
        (tmp_path / "ships.build.json").write_text(
            json.dumps({"discovery": {"extensions": []}}), encoding="utf-8"
        )
        result = _load_build_extensions(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------
# build_package — ships.build.json discovery.extensions stamping
# ---------------------------------------------------------------


class TestBuildJsonDiscoveryStamp:
    """build_package stamps discovery.extensions into ships.build.json."""

    def _minimal_config(self, tmp_project: Path, tmp_path: Path) -> BuildConfig:
        _write(
            tmp_project / "payload" / "database" / "DDL" / "tables" / "MyDB.T.tbl",
            "CREATE MULTISET TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        props = _properties_for("DEV", tmp_path)
        return BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )

    def test_build_json_contains_discovery_key(self, tmp_project, tmp_path):
        config = self._minimal_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        assert "discovery" in manifest

    def test_build_json_discovery_contains_extensions(self, tmp_project, tmp_path):
        config = self._minimal_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        assert "extensions" in manifest["discovery"]

    def test_default_extensions_are_present(self, tmp_project, tmp_path):
        """All DEFAULT_HARVEST_EXTENSIONS must appear in the stamped set."""
        config = self._minimal_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        stamped = set(manifest["discovery"]["extensions"])
        assert DEFAULT_HARVEST_EXTENSIONS.issubset(stamped)

    def test_extensions_are_sorted(self, tmp_project, tmp_path):
        config = self._minimal_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        exts = manifest["discovery"]["extensions"]
        assert exts == sorted(exts)

    def test_custom_ships_yaml_extension_appears_in_build_json(
        self, tmp_project, tmp_path
    ):
        """A project-level extension added via ships.yaml is stamped into ships.build.json."""
        _write(
            tmp_project / "payload" / "database" / "DDL" / "tables" / "MyDB.T.tbl",
            "CREATE MULTISET TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        (tmp_project / "ships.yaml").write_text(
            "discovery:\n  extensions:\n    - .tdsql\n", encoding="utf-8"
        )
        props = _properties_for("DEV", tmp_path)
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        assert ".tdsql" in manifest["discovery"]["extensions"]

    def test_manifest_object_has_discovery_field(self, tmp_project, tmp_path):
        """The in-memory BuildManifest returned by build_package also carries discovery."""
        config = self._minimal_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        _, manifest = main_pair
        assert "extensions" in manifest.discovery
        assert isinstance(manifest.discovery["extensions"], list)


# ---------------------------------------------------------------
# Auto-split — discovery preserved in both archives
# ---------------------------------------------------------------


class TestAutoSplitDiscoveryStamp:
    """Auto-split preserves discovery.extensions in both archive halves."""

    def _split_config(self, tmp_project: Path, tmp_path: Path) -> BuildConfig:
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "MyDB.db",
            "CREATE DATABASE MyDB AS PERMANENT = 1e9;\n",
        )
        _write(
            payload / "DDL" / "tables" / "MyDB.T.tbl",
            "CREATE MULTISET TABLE MyDB.T (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        props = _properties_for("DEV", tmp_path)
        return BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )

    def test_main_archive_has_discovery_extensions(self, tmp_project, tmp_path):
        config = self._split_config(tmp_project, tmp_path)
        (main_pair, _) = build_package(config)
        archive_path, _ = main_pair
        manifest = _read_zip_build_json(archive_path)
        stamped = set(manifest["discovery"]["extensions"])
        assert DEFAULT_HARVEST_EXTENSIONS.issubset(stamped)

    def test_prereqs_archive_has_discovery_extensions(self, tmp_project, tmp_path):
        config = self._split_config(tmp_project, tmp_path)
        (_, prereqs_pair) = build_package(config)
        assert prereqs_pair is not None
        archive_path, _ = prereqs_pair
        manifest = _read_zip_build_json(archive_path)
        stamped = set(manifest["discovery"]["extensions"])
        assert DEFAULT_HARVEST_EXTENSIONS.issubset(stamped)

    def test_both_archives_have_same_extensions(self, tmp_project, tmp_path):
        config = self._split_config(tmp_project, tmp_path)
        (main_pair, prereqs_pair) = build_package(config)
        assert prereqs_pair is not None
        main_exts = _read_zip_build_json(main_pair[0])["discovery"]["extensions"]
        prereqs_exts = _read_zip_build_json(prereqs_pair[0])["discovery"]["extensions"]
        assert main_exts == prereqs_exts
