"""Tests for SHIPS environment prerequisite package generation."""

import json
import os
import zipfile
from pathlib import Path

from td_release_packager.builder import build_package
from td_release_packager.environment_prereqs import (
    analyse_environment_parent_requirements,
)
from td_release_packager.models import BuildConfig


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _properties_for(env: str, tmp_path: Path) -> Path:
    props = tmp_path / f"{env}.conf"
    props.write_text(f"SHIPS_ENV={env}\n", encoding="utf-8")
    return props


def _read_zip_member(archive_path: str, suffix: str) -> str:
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if name.endswith(suffix):
                return zf.read(name).decode("utf-8")
    raise FileNotFoundError(suffix)


def test_analyse_environment_parent_requirements_detects_external_parent(tmp_path):
    """A parent not created in-package and not DBC becomes a requirement."""
    pkg = tmp_path / "pkg"
    _write(
        pkg / "payload" / "01_pre_requisites" / "databases" / "GDEV1_BASE.db",
        "create database GDEV1_BASE from GCFR_MAIN as perm = 100M;\n",
    )
    _write(
        pkg / "payload" / "01_pre_requisites" / "databases" / "GDEV1_GCFR.db",
        "create database GDEV1_GCFR from GCFR_MAIN as perm = 200M;\n",
    )
    _write(
        pkg / "payload" / "01_pre_requisites" / "databases" / "GDEV1T_GCFR.db",
        "create database GDEV1T_GCFR from GDEV1_GCFR as perm = 50M;\n",
    )

    requirements = analyse_environment_parent_requirements(str(pkg))

    assert [req.parent_name for req in requirements] == ["GCFR_MAIN"]
    assert len(requirements[0].required_by) == 2
    assert requirements[0].minimum_required_perm_bytes == 300 * 1024 * 1024


def test_build_package_emits_environment_prereqs_zip_and_chains_requires(
    tmp_project, tmp_path
):
    """External parents produce a _00 package and prereqs requires it."""
    payload = tmp_project / "payload" / "database"
    _write(
        payload / "pre-requisites" / "databases" / "GDEV1_BASE.db",
        "create database GDEV1_BASE from GCFR_MAIN as perm = 100M;\n",
    )
    _write(
        payload / "DDL" / "tables" / "GDEV1_BASE.Customer.tbl",
        "create multiset table GDEV1_BASE.Customer (Id integer) primary index (Id);\n",
    )

    cfg = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="GCFR",
        env_config_file=str(_properties_for("DEV", tmp_path)),
        build_number=1,
        output_dir=str(tmp_path),
    )

    (main_pair, prereqs_pair) = build_package(cfg)
    main_archive, main_manifest = main_pair
    prereqs_archive, prereqs_manifest = prereqs_pair
    release_group = main_manifest.release_group
    group_dir = tmp_path / release_group
    env_archive = group_dir / f"{release_group}_00_environment_prereqs.zip"

    assert group_dir.is_dir()
    assert env_archive.is_file()
    assert Path(prereqs_archive).parent == group_dir
    assert Path(main_archive).parent == group_dir
    assert os.path.basename(prereqs_archive).endswith("_01_prereqs.zip")
    assert os.path.basename(main_archive).endswith("_02_main.zip")
    assert prereqs_manifest.requires == [env_archive.name]
    assert main_manifest.requires == [os.path.basename(prereqs_archive)]

    group_manifest = json.loads(
        (group_dir / "release_group.json").read_text(encoding="utf-8")
    )
    assert group_manifest["deploy_order"] == [
        env_archive.name,
        os.path.basename(prereqs_archive),
        os.path.basename(main_archive),
    ]
    assert [pkg["role"] for pkg in group_manifest["packages"]] == [
        "environment_prereqs",
        "prereqs",
        "main",
    ]

    env_build = json.loads(
        _read_zip_member(str(env_archive), "context/ships.build.json")
    )
    assert env_build["role"] == "environment_prereqs"
    assert env_build["release_group"] == release_group
    assert env_build["trust"]["label"] == "BLOCKED"

    requirements = json.loads(
        _read_zip_member(
            str(env_archive), "context/prerequisites/database_parent_requirements.json"
        )
    )
    assert requirements["missing_parents"][0]["name"] == "GCFR_MAIN"
    assert requirements["execution_policy"]["requires_execution_evidence"] is True

    review_sql = _read_zip_member(
        str(env_archive), "context/prerequisites/create_missing_parents.review.sql"
    )
    assert "create database GCFR_MAIN" in review_sql
    assert "<DBA_SELECTED_PARENT>" in review_sql


def test_environment_prereqs_zip_contains_deployable_payload(tmp_project, tmp_path):
    """The generated _00 package carries .db payload, not only review SQL."""
    payload = tmp_project / "payload" / "database"
    _write(
        payload / "pre-requisites" / "databases" / "GDEV1_BASE.db",
        "create database GDEV1_BASE from GCFR_MAIN as perm = 0;\n",
    )
    _write(
        payload / "DDL" / "tables" / "GDEV1_BASE.Customer.tbl",
        "create multiset table GDEV1_BASE.Customer (Id integer) primary index (Id);\n",
    )

    cfg = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="GCFR",
        env_config_file=str(_properties_for("DEV", tmp_path)),
        build_number=1,
        output_dir=str(tmp_path),
    )

    (main_pair, _prereqs_pair) = build_package(cfg)
    release_group = main_pair[1].release_group
    env_archive = (
        tmp_path / release_group / f"{release_group}_00_environment_prereqs.zip"
    )

    payload_ddl = _read_zip_member(
        str(env_archive), "payload/01_pre_requisites/databases/GCFR_MAIN.db"
    )
    assert "create database GCFR_MAIN" in payload_ddl
    assert "from <DBA_SELECTED_PARENT>" in payload_ddl
    assert "as perm = <DBA_REVIEWED_PERM>" in payload_ddl

    requirements = json.loads(
        _read_zip_member(
            str(env_archive), "context/prerequisites/database_parent_requirements.json"
        )
    )
    assert (
        "payload/01_pre_requisites/databases/GCFR_MAIN.db"
        in requirements["deployable_payload"]
    )


def test_repackage_unblocks_reviewed_environment_payload(tmp_project, tmp_path):
    """DBA edits the generated .db payload, then repackage refreshes trust."""
    import shutil

    from td_release_packager.builder import repackage_package_dir

    payload = tmp_project / "payload" / "database"
    _write(
        payload / "pre-requisites" / "databases" / "GDEV1_BASE.db",
        "create database GDEV1_BASE from GCFR_MAIN as perm = 0;\n",
    )
    _write(
        payload / "DDL" / "tables" / "GDEV1_BASE.Customer.tbl",
        "create multiset table GDEV1_BASE.Customer (Id integer) primary index (Id);\n",
    )

    cfg = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="GCFR",
        env_config_file=str(_properties_for("DEV", tmp_path)),
        build_number=1,
        output_dir=str(tmp_path),
    )

    (main_pair, _prereqs_pair) = build_package(cfg)
    release_group = main_pair[1].release_group
    group_dir = tmp_path / release_group
    env_archive = group_dir / f"{release_group}_00_environment_prereqs.zip"
    shutil.unpack_archive(str(env_archive), str(group_dir))

    env_pkg_dir = group_dir / f"{release_group}_00_environment_prereqs"
    dba_payload = env_pkg_dir / "payload/01_pre_requisites/databases/GCFR_MAIN.db"
    dba_payload.write_text(
        dba_payload.read_text(encoding="utf-8")
        .replace("<DBA_SELECTED_PARENT>", "DBC")
        .replace("<DBA_REVIEWED_PERM>", "50G"),
        encoding="utf-8",
    )

    archive_path, manifest = repackage_package_dir(str(env_pkg_dir), strict=True)

    assert Path(archive_path).is_file()
    assert manifest.trust["label"] == "READY_WITH_CAVEATS"
    rebuilt = _read_zip_member(
        archive_path, "payload/01_pre_requisites/databases/GCFR_MAIN.db"
    )
    assert "from DBC" in rebuilt
    assert "as perm = 50G" in rebuilt
    assert "<DBA_SELECTED_PARENT>" not in rebuilt
    assert "<DBA_REVIEWED_PERM>" not in rebuilt


def test_environment_prereqs_zip_contains_dba_instructions(tmp_project, tmp_path):
    """DBA remediation instructions are package-local and include shell commands."""
    payload = tmp_project / "payload" / "database"
    _write(
        payload / "pre-requisites" / "databases" / "GDEV1_BASE.db",
        "create database GDEV1_BASE from GCFR_MAIN as perm = 0;\n",
    )
    _write(
        payload / "DDL" / "tables" / "GDEV1_BASE.Customer.tbl",
        "create multiset table GDEV1_BASE.Customer (Id integer) primary index (Id);\n",
    )

    cfg = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="GCFR",
        env_config_file=str(_properties_for("DEV", tmp_path)),
        build_number=1,
        output_dir=str(tmp_path),
    )

    (main_pair, _prereqs_pair) = build_package(cfg)
    release_group = main_pair[1].release_group
    env_archive = (
        tmp_path / release_group / f"{release_group}_00_environment_prereqs.zip"
    )

    instructions = _read_zip_member(
        str(env_archive), "context/prerequisites/DBA_INSTRUCTIONS.md"
    )
    assert "PowerShell" in instructions
    assert "Bash / Git Bash / Linux shell" in instructions
    assert "python -m td_release_packager repackage" in instructions
    assert ".ships-work" in instructions
    assert "extracted package" in instructions
    assert "payload/01_pre_requisites/databases/GCFR_MAIN.db" in instructions
    assert "<DBA_SELECTED_PARENT>" in instructions
    assert "<DBA_REVIEWED_PERM>" in instructions

    root_readme = _read_zip_member(str(env_archive), "README.txt")
    assert "context/prerequisites/DBA_INSTRUCTIONS.md" in root_readme
    assert "python -m td_release_packager repackage" in root_readme

    ships_index = json.loads(
        _read_zip_member(str(env_archive), "context/ships.index.json")
    )
    prereq_entry = ships_index["entrypoints"]["prerequisites"]
    assert "context/prerequisites/DBA_INSTRUCTIONS.md" in prereq_entry["contains"]
