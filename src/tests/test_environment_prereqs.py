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
    env_archive = tmp_path / f"{release_group}_00_environment_prereqs.zip"

    assert env_archive.is_file()
    assert os.path.basename(prereqs_archive).endswith("_01_prereqs.zip")
    assert os.path.basename(main_archive).endswith("_02_main.zip")
    assert prereqs_manifest.requires == [env_archive.name]
    assert main_manifest.requires == [os.path.basename(prereqs_archive)]

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
