import zipfile
from pathlib import Path

from td_release_packager.demo import (
    _copy_package_reports_from_archives,
    detect_demo_token_candidates,
    discover_demo_sql_root,
    run_demo,
)


class _Manifest:
    release_group = "DEV_demo_BUILD_0001"


def test_discover_demo_sql_root_prefers_single_workspace_product(tmp_path):
    product = tmp_path / "workspace" / "src" / "cargointelligence"
    product.mkdir(parents=True)
    (product / "01-tables.sql").write_text(
        "CREATE TABLE Demo_DB.Sample (id INTEGER) PRIMARY INDEX (id);",
        encoding="utf-8",
    )

    assert discover_demo_sql_root(tmp_path) == product.resolve()


def test_run_demo_prepare_only_stages_tokens_and_env_config(tmp_path):
    source = tmp_path / "repo" / "workspace" / "src" / "demo" / "01-core"
    source.mkdir(parents=True)
    (source / "00-setup.sql").write_text(
        "CREATE DATABASE Demo_DB AS PERMANENT = 1000000, SPOOL = 1000000;",
        encoding="utf-8",
    )
    (source / "01-table.sql").write_text(
        "CREATE TABLE Demo_DB.Sample (id INTEGER) PRIMARY INDEX (id);",
        encoding="utf-8",
    )

    result = run_demo(
        source=str(tmp_path / "repo"),
        name="demo",
        work_dir=str(tmp_path / "work"),
        package=False,
    )

    assert result.classified == 2
    assert result.unclassified == 0
    assert result.archive_path == ""
    assert result.env_config.is_file()
    assert "Demo_DB=Demo_DB" in result.env_config.read_text(encoding="utf-8")
    payload_files = list((result.project_dir / "payload" / "database").rglob("*"))
    assert any(path.suffix == ".tbl" for path in payload_files)


def test_run_demo_injects_root_parent_for_parentless_prereqs(tmp_path):
    source = tmp_path / "repo" / "workspace" / "src" / "demo" / "01-core"
    source.mkdir(parents=True)
    (source / "00-setup.sql").write_text(
        """
CREATE DATABASE Demo_DB AS PERMANENT = 1000000, SPOOL = 1000000;
CREATE USER Demo_User AS PERMANENT = 0 PASSWORD = "temporary";
""",
        encoding="utf-8",
    )

    result = run_demo(
        source=str(tmp_path / "repo"),
        name="demo",
        work_dir=str(tmp_path / "work"),
        root_parent="DEMO_ROOT",
        package=False,
    )

    prereq_files = list(
        (result.project_dir / "payload" / "database" / "pre-requisites").rglob("*")
    )
    contents = "\n".join(
        path.read_text(encoding="utf-8")
        for path in prereq_files
        if path.suffix.lower() in {".db", ".usr"}
    )

    assert result.root_parent_injections == 2
    assert "CREATE DATABASE {{Demo_DB_T}} FROM {{ROOT_PARENT}} AS" in contents
    assert "CREATE USER Demo_User FROM {{ROOT_PARENT}} AS" in contents
    assert "ROOT_PARENT=DEMO_ROOT" in result.env_config.read_text(encoding="utf-8")


def test_run_demo_preserves_existing_prereq_parent(tmp_path):
    source = tmp_path / "repo" / "workspace" / "src" / "demo" / "01-core"
    source.mkdir(parents=True)
    (source / "00-setup.sql").write_text(
        "CREATE DATABASE Demo_DB FROM Existing_Parent AS PERMANENT = 1000000;",
        encoding="utf-8",
    )

    result = run_demo(
        source=str(tmp_path / "repo"),
        name="demo",
        work_dir=str(tmp_path / "work"),
        root_parent="DEMO_ROOT",
        package=False,
    )

    prereq_files = list(
        (result.project_dir / "payload" / "database" / "pre-requisites").rglob("*.db")
    )
    assert len(prereq_files) == 1
    content = prereq_files[0].read_text(encoding="utf-8")

    assert result.root_parent_injections == 0
    assert "FROM Existing_Parent" in content
    assert "{{ROOT_PARENT}}" not in content


def test_demo_token_detection_ignores_keywords_datatypes_and_columns(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "table.sql").write_text(
        """
CREATE TABLE CargoIntelligence_Domain.Consignment_H (
    consignment_id   BIGINT NOT NULL,
    declaration_date DATE NOT NULL,
    trade_value_usd  DECIMAL(15,2),
    updated_dt       TIMESTAMP(6) WITH TIME ZONE
)
PRIMARY INDEX (consignment_id);
""",
        encoding="utf-8",
    )

    candidates = detect_demo_token_candidates(source)

    assert set(candidates) == {"CargoIntelligence_Domain"}
    assert "TABLE" not in candidates
    assert "BIGINT" not in candidates
    assert "NULL" not in candidates
    assert "TIME" not in candidates
    assert "ZONE" not in candidates
    assert "consignment_id" not in candidates


def test_run_demo_only_tokenises_database_not_object_or_columns(tmp_path):
    source = tmp_path / "repo" / "workspace" / "src" / "demo" / "01-core"
    source.mkdir(parents=True)
    (source / "01-table.sql").write_text(
        """
CREATE MULTISET TABLE CargoIntelligence_Domain.Consignment_H (
    consignment_id   BIGINT NOT NULL,
    consignment_key  VARCHAR(30) NOT NULL,
    declaration_date DATE NOT NULL,
    created_dt       TIMESTAMP(6) WITH TIME ZONE
)
PRIMARY INDEX (consignment_id);
""",
        encoding="utf-8",
    )

    result = run_demo(
        source=str(tmp_path / "repo"),
        name="demo",
        work_dir=str(tmp_path / "work"),
        package=False,
    )

    tables = list(
        (result.project_dir / "payload" / "database" / "DDL" / "tables").glob("*.tbl")
    )
    assert len(tables) == 1
    content = tables[0].read_text(encoding="utf-8")

    assert "{{CargoIntelligence_Domain_T}}.Consignment_H" in content
    assert "{{Consignment_H_T}}" not in content
    assert "{{consignment_id_T}}" not in content
    assert "{{declaration_date_T}}" not in content
    assert "PRIMARY INDEX (consignment_id)" in content


def test_run_demo_uses_explicit_package_output_dir(tmp_path, monkeypatch):
    source = tmp_path / "repo" / "workspace" / "src" / "demo" / "01-core"
    output = tmp_path / "packages"
    source.mkdir(parents=True)
    (source / "01-table.sql").write_text(
        "CREATE TABLE Demo_DB.Sample (id INTEGER) PRIMARY INDEX (id);",
        encoding="utf-8",
    )

    captured = {}

    def fake_build_package(config):
        captured["output_dir"] = config.output_dir
        archive = output / "DEV_demo_BUILD_0001" / "DEV_demo_BUILD_0001_01_main.zip"
        return (str(archive), _Manifest()), None

    monkeypatch.setattr("td_release_packager.demo.build_package", fake_build_package)

    result = run_demo(
        source=str(tmp_path / "repo"),
        name="demo",
        work_dir=str(tmp_path / "work"),
        output_dir=str(output),
    )

    assert captured["output_dir"] == str(output.resolve())
    assert result.archive_path.endswith("DEV_demo_BUILD_0001_01_main.zip")
    assert result.release_group == str((output / "DEV_demo_BUILD_0001").resolve())


def test_copy_package_reports_from_archives_uses_short_sidecar_names(tmp_path):
    archive = tmp_path / "DEV_demo_BUILD_1_20260602000000_02_main.zip"
    with zipfile.ZipFile(archive, "w") as package_zip:
        package_zip.writestr(
            "DEV_demo_BUILD_1_20260602000000_02_main/package_report.html",
            "<html>Main report</html>",
        )

    report_paths = _copy_package_reports_from_archives([archive])

    assert report_paths == [tmp_path / "package_report_main.html"]
    assert report_paths[0].read_text(encoding="utf-8") == "<html>Main report</html>"
