"""
test_builder.py — Tests for the SHIPS builder module.

Covers:
    - Filename resolution (_resolve_filename) from resolved DDL content
    - Environment-specific prefix replacement in filenames
    - Binary/non-DDL file preservation
    - Hidden/underscore file preservation
    - Dirty working tree gate (_check_working_tree)
    - Integrity file generation covers payload/ and lib/ directories
"""

import json
import os
import tempfile
import zipfile
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from td_release_packager.builder import (
    _check_working_tree,
    _generate_integrity_file,
    _resolve_filename,
    build_package,
)
from td_release_packager.models import BuildConfig


# ---------------------------------------------------------------
# _generate_integrity_file — lib/ coverage
# ---------------------------------------------------------------


class TestGenerateIntegrityFile:
    """Verify that _generate_integrity_file covers both payload/ and lib/."""

    def _make_package_dir(self, tmp_dir: str) -> str:
        """Create a minimal package directory structure."""
        payload_dir = os.path.join(tmp_dir, "payload")
        lib_dir = os.path.join(tmp_dir, "lib", "database_package_deployer")
        os.makedirs(payload_dir, exist_ok=True)
        os.makedirs(lib_dir, exist_ok=True)

        payload_file = os.path.join(payload_dir, "create_table.tbl")
        lib_file = os.path.join(lib_dir, "preflight.py")
        with open(payload_file, "w", encoding="utf-8") as fh:
            fh.write("CREATE TABLE Db.T1 (Id INT);\n")
        with open(lib_file, "w", encoding="utf-8") as fh:
            fh.write("# fake deployer module\n")

        return tmp_dir

    def test_lib_files_included_in_integrity_json(self):
        """lib/ files appear in ships.integrity.json file_hashes."""
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = self._make_package_dir(tmp)
            _generate_integrity_file(pkg_dir)

            integrity_path = os.path.join(pkg_dir, "context", "ships.integrity.json")
            assert os.path.isfile(integrity_path)

            with open(integrity_path, encoding="utf-8") as fh:
                data = json.load(fh)

            file_keys = data["files"]
            payload_keys = [k for k in file_keys if k.startswith("payload/")]
            lib_keys = [k for k in file_keys if k.startswith("lib/")]

            assert len(payload_keys) >= 1, "payload/ files must be in integrity JSON"
            assert len(lib_keys) >= 1, "lib/ files must be in integrity JSON"

    def test_package_hash_changes_when_lib_modified(self):
        """Modifying a lib/ file changes the package_hash."""
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = self._make_package_dir(tmp)
            hash_before = _generate_integrity_file(pkg_dir)

            lib_file = os.path.join(
                tmp, "lib", "database_package_deployer", "preflight.py"
            )
            with open(lib_file, "a", encoding="utf-8") as fh:
                fh.write("# modified\n")

            hash_after = _generate_integrity_file(pkg_dir)

            assert hash_before != hash_after, (
                "package_hash must change when a lib/ file is modified"
            )


def test_single_package_archive_uses_context_metadata_only(
    tmp_path, tmp_project, sample_env_config_file
):
    """A normal package archive has context/ships.*.json and no root metadata."""
    table = (
        tmp_project / "payload" / "database" / "DDL" / "tables" / "MyDB.Customer.tbl"
    )
    table.write_text(
        "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(sample_env_config_file),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    ((archive_path, _manifest), companion) = build_package(config)
    assert companion is None

    with zipfile.ZipFile(archive_path) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
        docs = {
            name.split("/", 1)[1]: json.loads(archive.read(name).decode("utf-8"))
            for name in names
            if name.endswith(".json")
            and "/context/" in name
            and "/context/schemas/" not in name
        }
        schemas = {
            name.rsplit("/", 1)[1]: json.loads(archive.read(name).decode("utf-8"))
            for name in names
            if "/context/schemas/" in name and name.endswith(".schema.json")
        }

    assert any(name.endswith("/context/ships.index.json") for name in names)
    assert any(name.endswith("/context/ships.build.json") for name in names)
    assert any(
        name.endswith("/context/schemas/ships.index.schema.json") for name in names
    )
    assert not any(
        len(name.split("/")) == 2
        and name.split("/")[1].startswith("ships.")
        and name.split("/")[1].endswith(".json")
        for name in names
    )

    for document_path, schema_name in {
        "context/ships.index.json": "ships.index.schema.json",
        "context/ships.context.json": "ships.context.schema.json",
        "context/ships.manifest.json": "ships.manifest.schema.json",
        "context/ships.handoff.json": "ships.handoff.schema.json",
        "context/ships.build.json": "ships.build.schema.json",
        "context/ships.provenance.json": "ships.provenance.schema.json",
        "context/ships.integrity.json": "ships.integrity.schema.json",
    }.items():
        assert document_path in docs
        schema = schemas[schema_name]
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(docs[document_path])
        assert schema["description"]
        assert schema["examples"]


def test_inferred_grants_are_packaged_as_dcl(
    tmp_path, tmp_project, sample_env_config_file
):
    """Package build backfills visible deployable .dcl grant scripts."""
    view = tmp_project / "payload" / "database" / "DDL" / "views" / "APP_V.MV.viw"
    view.write_text(
        "REPLACE VIEW APP_V.MV AS SELECT Id FROM DATA_DB.T;\n",
        encoding="utf-8",
    )
    macro = tmp_project / "payload" / "database" / "DDL" / "macros" / "APP_DB.M.mcr"
    macro.write_text(
        "REPLACE MACRO APP_DB.M AS (DELETE FROM APP_V.MV WHERE Id = :Id;);\n",
        encoding="utf-8",
    )
    source_dcl = (
        tmp_project / "payload" / "database" / "DCL" / "inter_db" / "APP_DB.dcl"
    )
    assert not source_dcl.exists()

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(sample_env_config_file),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    ((archive_path, _manifest), companion) = build_package(config)
    assert companion is None

    with zipfile.ZipFile(archive_path) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
        assert any(
            name.endswith("payload/02_dcl/inter_db/APP_DB.dcl") for name in names
        )
        report_name = next(
            name for name in names if name.endswith("package_report.html")
        )
        report = archive.read(report_name).decode("utf-8")
        assert "APP_DB.dcl" in report
        assert "APP_V.dcl" in report
        macro_dcl_name = next(name for name in names if name.endswith("APP_DB.dcl"))
        macro_dcl = archive.read(macro_dcl_name).decode("utf-8")
        assert "GRANT DELETE ON APP_V TO APP_DB WITH GRANT OPTION;" in macro_dcl
        assert "GRANT DELETE ON DATA_DB TO APP_DB WITH GRANT OPTION;" not in macro_dcl
        view_dcl_name = next(name for name in names if name.endswith("APP_V.dcl"))
        view_dcl = archive.read(view_dcl_name).decode("utf-8")
        assert "GRANT SELECT, DELETE ON DATA_DB TO APP_V WITH GRANT OPTION;" in view_dcl

    assert not source_dcl.exists()


def test_role_grantees_are_packaged_as_role_ddl(
    tmp_path, tmp_project, sample_env_config_file
):
    """Package build materialises missing CREATE ROLE scripts for role grants."""
    dcl = tmp_project / "payload" / "database" / "DCL" / "roles" / "APP_DB.dcl"
    dcl.write_text(
        "GRANT SELECT ON APP_DB TO APP_DB_READ_ROLE;\n",
        encoding="utf-8",
    )

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(sample_env_config_file),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    ((_main_archive, _manifest), companion) = build_package(config)
    assert companion is not None
    archive_path, _prereqs_manifest = companion

    with zipfile.ZipFile(archive_path) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
        role_name = next(
            name
            for name in names
            if name.endswith("payload/00_system/roles/APP_DB_READ_ROLE.rol")
        )
        assert archive.read(role_name).decode("utf-8").splitlines() == [
            "CREATE ROLE APP_DB_READ_ROLE;"
        ]

    assert not (
        tmp_project
        / "payload"
        / "database"
        / "system"
        / "roles"
        / "APP_DB_READ_ROLE.rol"
    ).exists()


def test_generated_write_role_is_inserted_into_system_waves(
    tmp_path, tmp_project, sample_env_config_file
):
    """Generated role DDL must not be invisible to wave-based deploy."""
    existing_role = (
        tmp_project
        / "payload"
        / "database"
        / "system"
        / "roles"
        / "APP_DB_READ_ROLE.rol"
    )
    existing_role.write_text("CREATE ROLE APP_DB_READ_ROLE;\n", encoding="utf-8")
    (tmp_project / "_waves.txt").write_text(
        "\n".join(
            [
                "# _waves.txt",
                "payload/database/system/roles/APP_DB_READ_ROLE.rol",
                "---",
                "payload/database/DCL/roles/APP_DB.dcl",
                "",
            ]
        ),
        encoding="utf-8",
    )
    dcl = tmp_project / "payload" / "database" / "DCL" / "roles" / "APP_DB.dcl"
    dcl.write_text(
        "GRANT SELECT ON APP_DB TO APP_DB_WRITE_ROLE;\n",
        encoding="utf-8",
    )

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(sample_env_config_file),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    ((_main_archive, _manifest), companion) = build_package(config)
    assert companion is not None
    archive_path, _prereqs_manifest = companion

    with zipfile.ZipFile(archive_path) as archive:
        waves_name = next(
            name for name in archive.namelist() if name.endswith("00_system/_waves.txt")
        )
        waves = archive.read(waves_name).decode("utf-8")
        first_wave = waves.split("---", 1)[0]

    assert "roles/APP_DB_READ_ROLE.rol" in first_wave
    assert "roles/APP_DB_WRITE_ROLE.rol" in first_wave


def test_existing_write_role_is_inserted_into_system_waves(
    tmp_path, tmp_project, sample_env_config_file
):
    """Existing role files must also be reconciled into package waves."""
    role_dir = tmp_project / "payload" / "database" / "system" / "roles"
    (role_dir / "APP_DB_READ_ROLE.rol").write_text(
        "CREATE ROLE APP_DB_READ_ROLE;\n",
        encoding="utf-8",
    )
    (role_dir / "APP_DB_WRITE_ROLE.rol").write_text(
        "CREATE ROLE APP_DB_WRITE_ROLE;\n",
        encoding="utf-8",
    )
    (tmp_project / "_waves.txt").write_text(
        "\n".join(
            [
                "# _waves.txt",
                "payload/database/system/roles/APP_DB_READ_ROLE.rol",
                "---",
                "payload/database/DCL/roles/APP_DB.dcl",
                "",
            ]
        ),
        encoding="utf-8",
    )
    dcl = tmp_project / "payload" / "database" / "DCL" / "roles" / "APP_DB.dcl"
    dcl.write_text(
        "GRANT SELECT ON APP_DB TO APP_DB_WRITE_ROLE;\n",
        encoding="utf-8",
    )

    config = BuildConfig(
        source_dir=str(tmp_project),
        environment="DEV",
        package_name="Pkg",
        env_config_file=str(sample_env_config_file),
        build_number=1,
        output_dir=str(tmp_path),
        allow_dirty=True,
    )

    ((_main_archive, _manifest), companion) = build_package(config)
    assert companion is not None
    archive_path, _prereqs_manifest = companion

    with zipfile.ZipFile(archive_path) as archive:
        waves_name = next(
            name for name in archive.namelist() if name.endswith("00_system/_waves.txt")
        )
        first_wave = archive.read(waves_name).decode("utf-8").split("---", 1)[0]

    assert "roles/APP_DB_READ_ROLE.rol" in first_wave
    assert "roles/APP_DB_WRITE_ROLE.rol" in first_wave


# ---------------------------------------------------------------
# _check_working_tree
# ---------------------------------------------------------------


class TestCheckWorkingTree:
    """Tests for the dirty working tree gate."""

    def _run(self, stdout="", returncode=0, side_effect=None):
        """Helper: mock subprocess.run and call _check_working_tree."""

        mock_result = type("R", (), {"returncode": returncode, "stdout": stdout})()
        with patch("td_release_packager.builder.subprocess.run") as mock_run:
            if side_effect:
                mock_run.side_effect = side_effect
            else:
                mock_run.return_value = mock_result
            return _check_working_tree("/fake/dir", allow_dirty=False), mock_run

    def test_clean_tree_returns_false(self):
        dirty, _ = self._run(stdout="")
        assert dirty is False

    def test_dirty_tree_raises_without_flag(self):

        mock_result = type("R", (), {"returncode": 0, "stdout": " M some_file.tbl\n"})()
        with patch(
            "td_release_packager.builder.subprocess.run", return_value=mock_result
        ):
            with pytest.raises(ValueError, match="uncommitted changes"):
                _check_working_tree("/fake/dir", allow_dirty=False)

    def test_dirty_tree_returns_true_with_allow_dirty(self):

        mock_result = type("R", (), {"returncode": 0, "stdout": " M some_file.tbl\n"})()
        with patch(
            "td_release_packager.builder.subprocess.run", return_value=mock_result
        ):
            dirty = _check_working_tree("/fake/dir", allow_dirty=True)
        assert dirty is True

    def test_git_not_found_returns_false(self):

        dirty, _ = self._run(side_effect=FileNotFoundError("git not found"))
        assert dirty is False

    def test_nonzero_returncode_returns_false(self):
        dirty, _ = self._run(returncode=128, stdout="fatal: not a git repository")
        assert dirty is False

    def test_error_message_lists_changed_files(self):

        mock_result = type(
            "R", (), {"returncode": 0, "stdout": " M file_a.tbl\n M file_b.viw\n"}
        )()
        with patch(
            "td_release_packager.builder.subprocess.run", return_value=mock_result
        ):
            with pytest.raises(ValueError) as exc_info:
                _check_working_tree("/fake/dir", allow_dirty=False)
        assert "file_a.tbl" in str(exc_info.value)
        assert "--allow-dirty" in str(exc_info.value)


# ---------------------------------------------------------------
# _resolve_filename — Eponymous filename from resolved content
# ---------------------------------------------------------------


class TestResolveFilename:
    """Tests for deriving filenames from resolved DDL content."""

    def test_table_filename_resolved(self):
        """Table filename is derived from resolved DDL content."""
        content = "CREATE MULTISET TABLE P_CORE.Customer (Id INT);"
        result = _resolve_filename("DEV01_CORE.Customer.tbl", content)
        assert result == "P_CORE.Customer.tbl"

    def test_view_filename_resolved(self):
        """View filename is derived from resolved DDL content."""
        content = "REPLACE VIEW P_CORE.ActiveCustomers AS SELECT 1;"
        result = _resolve_filename("DEV01_CORE.ActiveCustomers.viw", content)
        assert result == "P_CORE.ActiveCustomers.viw"

    def test_procedure_filename_resolved(self):
        """Procedure filename is derived from resolved DDL content."""
        content = "REPLACE PROCEDURE P_CORE.sp_Refresh() BEGIN END;"
        result = _resolve_filename("DEV01_CORE.sp_Refresh.spl", content)
        assert result == "P_CORE.sp_Refresh.spl"

    def test_join_index_filename_resolved(self):
        """Join index filename is derived from resolved DDL content."""
        content = "CREATE JOIN INDEX P_CORE.JI_Cust AS SELECT * FROM P_CORE.Customer;"
        result = _resolve_filename("DEV01_CORE.JI_Cust.jix", content)
        assert result == "P_CORE.JI_Cust.jix"

    def test_function_filename_resolved(self):
        """Function filename is derived from resolved DDL content."""
        content = "REPLACE FUNCTION P_CORE.fn_Calc(x INT) RETURNS INT RETURN x;"
        result = _resolve_filename("DEV01_CORE.fn_Calc.fnc", content)
        assert result == "P_CORE.fn_Calc.fnc"

    def test_trigger_filename_resolved(self):
        """Trigger filename is derived from resolved DDL content."""
        content = (
            "REPLACE TRIGGER P_CORE.trg_Audit "
            "AFTER INSERT ON P_CORE.Customer (SELECT 1;);"
        )
        result = _resolve_filename("DEV01_CORE.trg_Audit.trg", content)
        assert result == "P_CORE.trg_Audit.trg"

    def test_same_env_no_change(self):
        """Filename unchanged when source env matches target env."""
        content = "CREATE MULTISET TABLE P_CORE.Customer (Id INT);"
        result = _resolve_filename("P_CORE.Customer.tbl", content)
        assert result == "P_CORE.Customer.tbl"

    def test_extension_preserved(self):
        """Original file extension is preserved regardless of content."""
        content = "CREATE MULTISET TABLE P_CORE.Customer (Id INT);"
        result = _resolve_filename("DEV01_CORE.Customer.tbl", content)
        assert result.endswith(".tbl")

    def test_no_qualified_name_unchanged(self):
        """Files without extractable qualified names keep original filename."""
        content = "GRANT SELECT ON P_CORE TO SomeRole;"
        result = _resolve_filename("some_grant.dcl", content)
        assert result == "some_grant.dcl"

    def test_dcl_grant_create_procedure_does_not_rename_to_on(self):
        """DCL files keep their filename even when privileges contain DDL words."""
        content = "GRANT CREATE PROCEDURE ON GDEV1P_BB TO DBC;"
        result = _resolve_filename("GDEV1P_BB.dcl", content)
        assert result == "GDEV1P_BB.dcl"

    def test_ordered_sql_filename_is_not_derived_from_inner_ddl(self):
        """Ordered SQL keeps source choreography identity."""
        content = (
            "GRANT SELECT ON MyDB TO WorkerRole;\n"
            "CREATE TABLE MyDB.T (id INT);\n"
            "REVOKE SELECT ON MyDB FROM WorkerRole;"
        )
        result = _resolve_filename("temporary_access.ordered.osql", content)
        assert result == "temporary_access.ordered.osql"

    def test_c_source_unchanged(self):
        """C source files (.c) are never renamed."""
        content = "#include <stdio.h>\nvoid fn() {}"
        result = _resolve_filename("OMR_Put_Trace.c", content)
        assert result == "OMR_Put_Trace.c"

    def test_c_header_unchanged(self):
        """C header files (.h) are never renamed."""
        content = "#ifndef GUARD_H\n#define GUARD_H\n#endif"
        result = _resolve_filename("sqltypes_td.h", content)
        assert result == "sqltypes_td.h"

    def test_jar_binary_unchanged(self):
        """JAR binary files (.jar) are never renamed."""
        content = "binary content"
        result = _resolve_filename("DataFlowJane.jar", content)
        assert result == "DataFlowJane.jar"

    def test_hidden_file_unchanged(self):
        """Hidden files (dot-prefixed) are never renamed."""
        content = "CREATE TABLE P_CORE.T (Id INT);"
        result = _resolve_filename(".gitkeep", content)
        assert result == ".gitkeep"

    def test_underscore_file_unchanged(self):
        """Underscore-prefixed files (e.g. _waves.txt) are never renamed."""
        content = "some content"
        result = _resolve_filename("_waves.txt", content)
        assert result == "_waves.txt"

    def test_quoted_identifiers(self):
        """Quoted identifiers have quotes stripped in the filename."""
        content = 'CREATE MULTISET TABLE "P_CORE"."Customer" (Id INT);'
        result = _resolve_filename("DEV01_CORE.Customer.tbl", content)
        assert result == "P_CORE.Customer.tbl"

    def test_volatile_table(self):
        """VOLATILE TABLE qualified name is extracted correctly."""
        content = "CREATE MULTISET VOLATILE TABLE P_CORE.TmpWork (Id INT);"
        result = _resolve_filename("DEV01_CORE.TmpWork.tbl", content)
        assert result == "P_CORE.TmpWork.tbl"

    def test_global_temporary_trace_table(self):
        """GLOBAL TEMPORARY TRACE TABLE qualified name is extracted."""
        content = "CREATE MULTISET GLOBAL TEMPORARY TRACE TABLE P_CORE.Trace (Id INT);"
        result = _resolve_filename("DEV01_CORE.Trace.tbl", content)
        assert result == "P_CORE.Trace.tbl"

    def test_macro_filename_resolved(self):
        """Macro filename is derived from resolved DDL content."""
        content = "REPLACE MACRO P_CORE.mc_Report AS (SELECT 1;);"
        result = _resolve_filename("DEV01_CORE.mc_Report.mcr", content)
        assert result == "P_CORE.mc_Report.mcr"

    def test_single_part_name_unchanged(self):
        """DDL with single-part name (no db prefix) keeps original filename."""
        content = "CREATE ROLE analyst_role;"
        result = _resolve_filename("analyst_role.rol", content)
        # No qualified DB.Object name → original preserved
        assert result == "analyst_role.rol"
