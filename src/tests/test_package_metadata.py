import json

from database_package_deployer.package_metadata import (
    package_file,
    package_file_candidates,
    package_index_file,
    read_package_index,
    read_package_json,
)


def test_ships_metadata_paths_are_canonical_context_paths(tmp_path):
    assert package_file(str(tmp_path), "ships.build.json") == str(
        tmp_path / "context" / "ships.build.json"
    )
    assert package_index_file(str(tmp_path)) == str(
        tmp_path / "context" / "ships.index.json"
    )
    assert list(package_file_candidates(str(tmp_path), "ships.build.json")) == [
        str(tmp_path / "context" / "ships.build.json")
    ]


def test_read_package_json_does_not_fall_back_to_root_level_metadata(tmp_path):
    (tmp_path / "ships.build.json").write_text(
        json.dumps({"package_name": "legacy-root"}), encoding="utf-8"
    )

    assert read_package_json(str(tmp_path), "ships.build.json") == {}


def test_read_package_json_accepts_logs_directory_and_context_metadata(tmp_path):
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "ships.index.json").write_text(
        json.dumps({"read_first": "context/ships.index.json"}), encoding="utf-8"
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    assert read_package_index(str(logs_dir)) == {
        "read_first": "context/ships.index.json"
    }
