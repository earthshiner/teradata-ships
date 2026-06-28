"""
test_changeset_packaging.py — changeset-scoped packaging (#115).

Covers:
    - changeset_from_objects: explicit list expanded by dependants
    - stage_changeset_payload: filtered tree contains only selected objects
    - end-to-end build of a staged changeset → manifest.changeset stamped,
      archive payload scoped to the selected objects
"""

import json
import os
import zipfile
from pathlib import Path

from td_release_packager.changeset import (
    changeset_from_objects,
    stage_changeset_payload,
)


def _mk_project(tmp_path: Path) -> Path:
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


def _seed(project: Path) -> None:
    (project / "payload/database/DDL/tables/DB.Customer.tbl").write_text(
        "CREATE MULTISET TABLE DB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )
    (project / "payload/database/DDL/views/DB.ActiveCust.viw").write_text(
        "REPLACE VIEW DB.ActiveCust AS SELECT Id FROM DB.Customer;\n",
        encoding="utf-8",
    )
    # An unrelated object that must NOT be packaged.
    (project / "payload/database/DDL/tables/DB.Unrelated.tbl").write_text(
        "CREATE MULTISET TABLE DB.Unrelated (Id INTEGER) PRIMARY INDEX (Id);\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------
# changeset_from_objects
# ---------------------------------------------------------------


class TestChangesetFromObjects:
    def test_explicit_object_pulls_dependants(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed(project)
        result = changeset_from_objects(str(project), {"DB.Customer"})
        assert result.mode == "objects"
        assert result.changed == {"DB.Customer"}
        assert result.dependants == {"DB.ActiveCust"}
        assert "DB.Unrelated" not in result.selected

    def test_unknown_object_noted_and_ignored(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed(project)
        result = changeset_from_objects(str(project), {"DB.Ghost"})
        assert result.changed == set()
        assert "DB.Ghost" in result.note


# ---------------------------------------------------------------
# stage_changeset_payload
# ---------------------------------------------------------------


class TestStagePayload:
    def test_stages_only_selected(self, tmp_path):
        project = _mk_project(tmp_path)
        _seed(project)
        dest = stage_changeset_payload(str(project), {"DB.Customer", "DB.ActiveCust"})
        try:
            assert os.path.isfile(
                os.path.join(dest, "payload/database/DDL/tables/DB.Customer.tbl")
            )
            assert os.path.isfile(
                os.path.join(dest, "payload/database/DDL/views/DB.ActiveCust.viw")
            )
            assert not os.path.exists(
                os.path.join(dest, "payload/database/DDL/tables/DB.Unrelated.tbl")
            )
            # config/ and .ships/ copied verbatim.
            assert os.path.isdir(os.path.join(dest, "config"))
            assert os.path.isfile(os.path.join(dest, ".ships/.build_counter"))
        finally:
            import shutil

            shutil.rmtree(dest, ignore_errors=True)


# ---------------------------------------------------------------
# End-to-end build of a staged changeset
# ---------------------------------------------------------------


class TestChangesetBuild:
    def test_build_scoped_and_stamped(self, tmp_path):
        from td_release_packager.builder import build_package
        from td_release_packager.models import BuildConfig

        project = _mk_project(tmp_path)
        _seed(project)
        result = changeset_from_objects(str(project), {"DB.Customer"})
        staged = stage_changeset_payload(str(project), result.selected)

        props = tmp_path / "DEV.conf"
        props.write_text("SHIPS_ENV=DEV\n", encoding="utf-8")
        meta = {
            "mode": result.mode,
            "base": "objects",
            "objects": sorted(result.selected),
        }
        cfg = BuildConfig(
            source_dir=staged,
            environment="DEV",
            package_name="Chg",
            env_config_file=str(props),
            build_number=7,
            output_dir=str(tmp_path / "out"),
            changeset=meta,
        )
        try:
            (main_arc, manifest), _companion = build_package(cfg)
        finally:
            import shutil

            shutil.rmtree(staged, ignore_errors=True)

        assert manifest.changeset == meta

        with zipfile.ZipFile(main_arc) as zf:
            names = zf.namelist()
            build_name = next(n for n in names if n.endswith("ships.build.json"))
            build_data = json.loads(zf.read(build_name))
            payload_ddl = [n for n in names if "/payload/" in n and n.endswith(".tbl")]

        assert build_data.get("changeset", {}).get("mode") == "objects"
        # Only the changed table travels — the unrelated table is excluded.
        assert any("DB.Customer" in n for n in payload_ddl)
        assert not any("DB.Unrelated" in n for n in payload_ddl)
