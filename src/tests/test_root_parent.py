from pathlib import Path

import pytest

from td_release_packager.root_parent import (
    inject_root_parent,
    inject_root_parent_in_content,
    normalise_root_parent,
)


def test_inject_root_parent_in_content_adds_from_to_parentless_database():
    content = "CREATE DATABASE Demo_DB AS PERMANENT = 1000000;"

    updated = inject_root_parent_in_content(content, "DEMO_ROOT")

    assert updated == "CREATE DATABASE Demo_DB FROM DEMO_ROOT AS PERMANENT = 1000000;"


def test_inject_root_parent_in_content_preserves_existing_parent():
    content = "CREATE USER Demo_User FROM Existing_Parent AS PERMANENT = 0;"

    updated = inject_root_parent_in_content(content, "DEMO_ROOT")

    assert updated == content


def test_inject_root_parent_updates_project_prereq_files(tmp_path):
    prereqs = tmp_path / "payload" / "database" / "pre-requisites"
    databases = prereqs / "databases"
    users = prereqs / "users"
    databases.mkdir(parents=True)
    users.mkdir(parents=True)
    db_file = databases / "Demo_DB.db"
    user_file = users / "Demo_User.usr"
    db_file.write_text(
        "CREATE DATABASE Demo_DB AS PERMANENT = 1000000;",
        encoding="utf-8",
    )
    user_file.write_text(
        "CREATE USER Demo_User FROM Existing_Parent AS PERMANENT = 0;",
        encoding="utf-8",
    )

    injections = inject_root_parent(Path(tmp_path), "DEMO_ROOT")

    assert injections == 1
    assert "FROM DEMO_ROOT" in db_file.read_text(encoding="utf-8")
    assert "FROM Existing_Parent" in user_file.read_text(encoding="utf-8")


def test_normalise_root_parent_rejects_blank_value():
    with pytest.raises(ValueError, match="RootParentEmpty"):
        normalise_root_parent("  ")


def test_package_cli_root_parent_helper_uses_literal_parent(tmp_path):
    from td_release_packager.cli import _apply_root_parent_option

    prereqs = tmp_path / "payload" / "database" / "pre-requisites" / "databases"
    prereqs.mkdir(parents=True)
    db_file = prereqs / "Demo_DB.db"
    db_file.write_text(
        "CREATE DATABASE Demo_DB AS PERMANENT = 1000000;",
        encoding="utf-8",
    )

    injections = _apply_root_parent_option(str(tmp_path), "DEMO_ROOT")

    assert injections == 1
    assert "FROM DEMO_ROOT" in db_file.read_text(encoding="utf-8")
    assert "{{ROOT_PARENT}}" not in db_file.read_text(encoding="utf-8")
