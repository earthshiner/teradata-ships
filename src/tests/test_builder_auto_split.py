"""
test_builder_auto_split.py — Phase 2 of the intra_package_dependency
work: package stage emits a paired prereqs + main bundle when the
source contains both CREATE DATABASE / USER statements and dependent
objects.

Covers:
    - No-split passthrough (only DDL, only prereqs, empty source)
    - Split detection with prereqs + dependants
    - Tokenised prereq + tokenised dependant pair through the build
    - Manifest tying: shared release_group, role, requires linkage
    - Filename convention (shared release group plus ``_01_prereqs`` / ``_02_main`` suffixes)
    - Per-archive phase_inventory recount post-split
    - Both archives contain full infrastructure (config/, lib/,
      deploy.py, ships.build.json, etc.)
    - Direct unit tests for the helpers (_phase_has_files,
      _is_auto_split_needed, _compute_phase_inventory)
"""

import json
import os
import zipfile
from pathlib import Path

import pytest

from td_release_packager.builder import (
    _compute_phase_inventory,
    _is_auto_split_needed,
    _phase_has_files,
    build_package,
)
from td_release_packager.models import BuildConfig


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write a UTF-8 text file, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _properties_for(env: str, tmp_path: Path, **extra) -> Path:
    """Write a minimal .conf file declaring SHIPS_ENV=<env>.

    Extra keys go in verbatim. The build cross-checks SHIPS_ENV
    against the --env argument, so the two must match.
    """
    lines = [f"SHIPS_ENV={env}"]
    for key, value in extra.items():
        lines.append(f"{key}={value}")
    props_path = tmp_path / f"{env}.conf"
    props_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return props_path


def _zip_contains(archive_path: str, member: str) -> bool:
    """True when ``member`` (anywhere in the zip) ends with the given
    suffix. Used to assert presence of files like ``ships.build.json`` or
    ``deploy.py`` regardless of the top-level archive directory."""
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if name.endswith(member):
                return True
    return False


def _read_zip_member(archive_path: str, suffix: str) -> str:
    """Read the first archive member whose name ends with ``suffix``."""
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if name.endswith(suffix):
                return zf.read(name).decode("utf-8")
    raise FileNotFoundError(f"No member ending with {suffix} in {archive_path}")


def _list_zip_phase_files(archive_path: str, phase: str) -> list:
    """List archive members under ``payload/<phase>/`` excluding empty
    directory entries."""
    prefix = f"payload/{phase}/"
    out = []
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            # Locate the prefix anywhere — the top-level archive
            # directory varies with package name.
            idx = name.find(prefix)
            if idx == -1:
                continue
            tail = name[idx + len(prefix) :]
            if not tail:
                continue  # the directory entry itself
            out.append(tail)
    return out


# ---------------------------------------------------------------
# Unit tests for the new helpers
# ---------------------------------------------------------------


class TestPhaseHasFiles:
    """Unit tests for the per-phase emptiness check."""

    def test_missing_directory_returns_false(self, tmp_path):
        payload = tmp_path / "payload"
        payload.mkdir()
        assert _phase_has_files(str(payload), "01_pre_requisites") is False

    def test_empty_directory_returns_false(self, tmp_path):
        payload = tmp_path / "payload"
        (payload / "01_pre_requisites").mkdir(parents=True)
        assert _phase_has_files(str(payload), "01_pre_requisites") is False

    def test_directory_with_gitkeep_only_returns_false(self, tmp_path):
        payload = tmp_path / "payload"
        prereq_dir = payload / "01_pre_requisites"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / ".gitkeep").write_text("", encoding="utf-8")
        assert _phase_has_files(str(payload), "01_pre_requisites") is False

    def test_directory_with_real_file_returns_true(self, tmp_path):
        payload = tmp_path / "payload"
        prereq_dir = payload / "01_pre_requisites" / "databases"
        prereq_dir.mkdir(parents=True)
        (prereq_dir / "MyDB.db").write_text("CREATE DATABASE MyDB;", encoding="utf-8")
        assert _phase_has_files(str(payload), "01_pre_requisites") is True


class TestIsAutoSplitNeeded:
    """Unit tests for the split-decision predicate."""

    def _empty_payload(self, tmp_path):
        payload = tmp_path / "pkg" / "payload"
        for phase in (
            "00_system",
            "01_pre_requisites",
            "02_dcl",
            "03_ddl",
            "04_dml",
            "05_post_install",
        ):
            (payload / phase).mkdir(parents=True)
        return tmp_path / "pkg"

    def test_no_files_anywhere_no_split(self, tmp_path):
        pkg_dir = self._empty_payload(tmp_path)
        assert _is_auto_split_needed(str(pkg_dir)) is False

    def test_only_prereqs_no_split(self, tmp_path):
        pkg_dir = self._empty_payload(tmp_path)
        (pkg_dir / "payload" / "01_pre_requisites" / "MyDB.db").write_text(
            "CREATE DATABASE MyDB;", encoding="utf-8"
        )
        assert _is_auto_split_needed(str(pkg_dir)) is False

    def test_only_dependants_no_split(self, tmp_path):
        pkg_dir = self._empty_payload(tmp_path)
        (pkg_dir / "payload" / "03_ddl" / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INT);", encoding="utf-8"
        )
        assert _is_auto_split_needed(str(pkg_dir)) is False

    def test_prereqs_plus_dependants_splits(self, tmp_path):
        pkg_dir = self._empty_payload(tmp_path)
        (pkg_dir / "payload" / "01_pre_requisites" / "MyDB.db").write_text(
            "CREATE DATABASE MyDB;", encoding="utf-8"
        )
        (pkg_dir / "payload" / "03_ddl" / "MyDB.T.tbl").write_text(
            "CREATE MULTISET TABLE MyDB.T (Id INT);", encoding="utf-8"
        )
        assert _is_auto_split_needed(str(pkg_dir)) is True

    def test_prereqs_plus_system_splits(self, tmp_path):
        """SYSTEM (00) counts as a dependant for split purposes — it
        deploys after PRE_REQUISITES under the standard phase order."""
        pkg_dir = self._empty_payload(tmp_path)
        (pkg_dir / "payload" / "01_pre_requisites" / "MyDB.db").write_text(
            "CREATE DATABASE MyDB;", encoding="utf-8"
        )
        (pkg_dir / "payload" / "00_system" / "MyRole.rol").write_text(
            "CREATE ROLE MyRole;", encoding="utf-8"
        )
        assert _is_auto_split_needed(str(pkg_dir)) is True


class TestComputePhaseInventory:
    """Unit tests for the post-split inventory recount."""

    def test_counts_files_per_phase(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        payload = pkg_dir / "payload"
        (payload / "01_pre_requisites" / "MyDB.db").parent.mkdir(parents=True)
        (payload / "01_pre_requisites" / "MyDB.db").write_text("x", encoding="utf-8")
        (payload / "03_ddl" / "tables" / "T1.tbl").parent.mkdir(parents=True)
        (payload / "03_ddl" / "tables" / "T1.tbl").write_text("x", encoding="utf-8")
        (payload / "03_ddl" / "tables" / "T2.tbl").write_text("x", encoding="utf-8")

        inventory = _compute_phase_inventory(str(pkg_dir))

        assert inventory == {"01_pre_requisites": 1, "03_ddl": 2}

    def test_skips_empty_phases(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        payload = pkg_dir / "payload"
        (payload / "01_pre_requisites").mkdir(parents=True)
        (payload / "03_ddl").mkdir(parents=True)
        # No files anywhere.
        assert _compute_phase_inventory(str(pkg_dir)) == {}


# ---------------------------------------------------------------
# Integration tests through build_package
# ---------------------------------------------------------------


class TestBuildPackageNoSplit:
    """The single-zip path is preserved when there is nothing to split."""

    def test_only_dependants_returns_single_archive(self, tmp_project, tmp_path):
        """Only DDL, no CREATE DATABASE/USER → single zip, no companion."""
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "DDL" / "tables" / "MyDB.Customer.tbl",
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        properties = _properties_for("DEV", tmp_path)
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(properties),
            build_number=1,
            output_dir=str(tmp_path),
        )

        (main_pair, companion_pair) = build_package(config)

        assert companion_pair is None
        archive_path, manifest = main_pair
        assert os.path.isfile(archive_path)
        assert manifest.role == "main"
        assert manifest.release_group != ""
        assert manifest.requires == []
        assert os.path.basename(archive_path).endswith("_01_main.zip")
        assert Path(archive_path).parent == tmp_path / manifest.release_group
        assert (tmp_path / manifest.release_group / "release_group.json").is_file()

    def test_only_prereqs_returns_single_archive(self, tmp_project, tmp_path):
        """Only CREATE DATABASE, no dependants → single zip (nothing
        to split off — splitting would leave an empty main zip)."""
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "MyDB.db",
            "CREATE DATABASE MyDB AS PERMANENT = 1024;\n",
        )

        properties = _properties_for("DEV", tmp_path)
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(properties),
            build_number=1,
            output_dir=str(tmp_path),
        )

        (main_pair, companion_pair) = build_package(config)

        assert companion_pair is None


class TestBuildPackageAutoSplit:
    """Phase 2 headline behaviour: prereqs + dependants → paired bundle."""

    @pytest.fixture
    def split_config(self, tmp_project, tmp_path):
        """Build a project that must be auto-split: a CREATE DATABASE
        plus a table that lives in that database."""
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "MyDB.db",
            "CREATE DATABASE MyDB AS PERMANENT = 1024 SPOOL = 1024;\n",
        )
        _write(
            payload / "DDL" / "tables" / "MyDB.Customer.tbl",
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        properties = _properties_for("DEV", tmp_path)
        return BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(properties),
            build_number=1,
            output_dir=str(tmp_path),
        )

    def test_two_archives_returned(self, split_config):
        """build_package returns (main_pair, prereqs_pair)."""
        (main_pair, companion_pair) = build_package(split_config)

        assert companion_pair is not None
        main_archive, _ = main_pair
        prereqs_archive, _ = companion_pair
        assert os.path.isfile(main_archive)
        assert os.path.isfile(prereqs_archive)
        assert main_archive != prereqs_archive

    def test_split_archive_names_sort_as_deploy_pair(self, split_config):
        """Split package filenames keep the release identity first and
        append deploy-order role suffixes so the pair sorts together."""
        (_main_pair, companion_pair) = build_package(split_config)
        main_archive = _main_pair[0]
        prereqs_archive = companion_pair[0]

        main_name = os.path.basename(main_archive)
        prereqs_name = os.path.basename(prereqs_archive)

        assert main_name.endswith("_02_main.zip")
        assert prereqs_name.endswith("_01_prereqs.zip")
        assert main_name.replace("_02_main.zip", "") == prereqs_name.replace(
            "_01_prereqs.zip", ""
        )
        assert sorted([main_name, prereqs_name]) == [prereqs_name, main_name]

    def test_release_group_directory_manifest_for_split(self, split_config):
        """A multi-package release is grouped under releases/<release_group>/
        with a group manifest listing deploy order."""
        ((main_archive, main_manifest), (prereqs_archive, prereqs_manifest)) = (
            build_package(split_config)
        )
        group_dir = Path(main_archive).parent
        group_manifest_path = group_dir / "release_group.json"
        group_launcher_path = group_dir / "deploy_release.py"

        assert group_dir.name == main_manifest.release_group
        assert Path(prereqs_archive).parent == group_dir
        assert group_manifest_path.is_file()
        assert group_launcher_path.is_file()
        assert "td_release_packager" in group_launcher_path.read_text(encoding="utf-8")

        group_manifest = json.loads(group_manifest_path.read_text(encoding="utf-8"))
        assert group_manifest["release_group"] == main_manifest.release_group
        assert group_manifest["deploy_order"] == [
            os.path.basename(prereqs_archive),
            os.path.basename(main_archive),
        ]
        assert [pkg["role"] for pkg in group_manifest["packages"]] == [
            "prereqs",
            "main",
        ]
        assert all(
            "context/ships.index.json" in pkg["context_entrypoint"]
            for pkg in group_manifest["packages"]
        )

    def test_manifest_role_and_requires_linkage(self, split_config):
        """Main manifest has role='main' and requires=[prereqs zip].
        Prereqs manifest has role='prereqs' and requires=[]."""
        ((main_archive, main_manifest), (prereqs_archive, prereqs_manifest)) = (
            build_package(split_config)
        )

        assert main_manifest.role == "main"
        assert prereqs_manifest.role == "prereqs"
        assert prereqs_manifest.requires == []
        assert main_manifest.requires == [os.path.basename(prereqs_archive)]

    def test_shared_release_group(self, split_config):
        """Both manifests carry the same release_group ID."""
        ((main_archive, main_manifest), (_prereqs_archive, prereqs_manifest)) = (
            build_package(split_config)
        )

        assert main_manifest.release_group == prereqs_manifest.release_group
        assert main_manifest.release_group != ""
        # The release_group is the shared, unsuffixed basename before role suffixes.
        main_basename = os.path.splitext(os.path.basename(main_archive))[0]
        assert main_basename == f"{main_manifest.release_group}_02_main"

    def test_phase_inventory_per_archive(self, split_config):
        """Each manifest's phase_inventory reflects only what its own
        archive ships — the union has been partitioned, not duplicated."""
        ((_main_archive, main_manifest), (_prereqs_archive, prereqs_manifest)) = (
            build_package(split_config)
        )

        # Main: dependants only.
        assert "01_pre_requisites" not in main_manifest.phase_inventory
        assert main_manifest.phase_inventory.get("03_ddl", 0) >= 1

        # Prereqs: prereq phase only.
        assert "03_ddl" not in prereqs_manifest.phase_inventory
        assert prereqs_manifest.phase_inventory.get("01_pre_requisites", 0) >= 1

    def test_archive_contents_partitioned(self, split_config):
        """The actual zip contents reflect the partition: prereq files
        live in the prereqs zip, dependant files in the main zip."""
        ((main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        prereqs_files = _list_zip_phase_files(prereqs_archive, "01_pre_requisites")
        main_files = _list_zip_phase_files(main_archive, "03_ddl")
        # Each archive's "wrong half" is empty.
        assert _list_zip_phase_files(main_archive, "01_pre_requisites") == []
        assert _list_zip_phase_files(prereqs_archive, "03_ddl") == []
        # And the right half has the expected files.
        assert any("MyDB.db" in name for name in prereqs_files)
        assert any("MyDB.Customer.tbl" in name for name in main_files)

    def test_both_archives_have_full_infrastructure(self, split_config):
        """Each archive must be independently deployable: ships.build.json,
        deploy.py, README, embedded deployer all present in both."""
        ((main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        for archive in (main_archive, prereqs_archive):
            assert _zip_contains(archive, "ships.build.json"), (
                f"ships.build.json missing from {archive}"
            )
            assert _zip_contains(archive, "deploy.py"), (
                f"deploy.py missing from {archive}"
            )
            assert _zip_contains(archive, "README.txt"), (
                f"README.txt missing from {archive}"
            )

    def test_build_json_round_trip(self, split_config):
        """The on-disk ships.build.json in each zip parses and carries the
        expected role / release_group / requires fields — proves the
        manifest changes survive the JSON round-trip."""
        ((main_archive, main_manifest), (prereqs_archive, prereqs_manifest)) = (
            build_package(split_config)
        )

        main_json = json.loads(_read_zip_member(main_archive, "ships.build.json"))
        prereqs_json = json.loads(_read_zip_member(prereqs_archive, "ships.build.json"))

        assert main_json["role"] == "main"
        assert main_json["release_group"] == main_manifest.release_group
        assert main_json["requires"] == [os.path.basename(prereqs_archive)]

        assert prereqs_json["role"] == "prereqs"
        assert prereqs_json["release_group"] == main_manifest.release_group
        assert prereqs_json["requires"] == []

    def test_split_package_reports_are_package_local(self, split_config):
        """Each split package report must only index files that exist in that archive."""
        ((main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        main_report = _read_zip_member(main_archive, "package_report.html")
        prereqs_report = _read_zip_member(prereqs_archive, "package_report.html")

        assert "MyDB.Customer.tbl" in main_report
        assert "MyDB.db" not in main_report

        assert "MyDB.db" in prereqs_report
        assert "MyDB.Customer.tbl" not in prereqs_report

    def test_prereqs_report_title_mentions_pre_requisites(self, split_config):
        """The prereqs report title should clearly identify the companion archive."""
        ((_main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        prereqs_report = _read_zip_member(prereqs_archive, "package_report.html")

        assert "Pre-requisites Package Report" in prereqs_report

    def test_split_provenance_is_package_local(self, split_config):
        """Per-archive provenance should not reference files removed by the split."""
        ((main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        main_provenance = json.loads(
            _read_zip_member(main_archive, "context/ships.provenance.json")
        )
        prereqs_provenance = json.loads(
            _read_zip_member(prereqs_archive, "context/ships.provenance.json")
        )

        main_entries = main_provenance.get("entries", {})
        prereqs_entries = prereqs_provenance.get("entries", {})

        assert any("MyDB.Customer.tbl" in path for path in main_entries)
        assert not any("MyDB.db" in path for path in main_entries)

        assert any("MyDB.db" in path for path in prereqs_entries)
        assert not any("MyDB.Customer.tbl" in path for path in prereqs_entries)

    def test_checksum_sidecar_for_both(self, split_config):
        """Both archives get a .sha256 sidecar so the DBA can verify
        each one independently."""
        ((main_archive, _), (prereqs_archive, _)) = build_package(split_config)

        assert os.path.isfile(main_archive + ".sha256")
        assert os.path.isfile(prereqs_archive + ".sha256")


class TestDeployChaining:
    """Phase 3 — deploy chaining via ships.build.json requires field.

    Verifies that:
    - A split package's main deploy.py contains the chaining code that
      reads ships.build.json, locates the companion prereqs directory, and
      deploys it before the main package.
    - The ships.build.json requires field is populated on the main archive and
      empty on the prereqs archive.
    - The generated deploy.py explicitly references 'requires' and
      'deploy_package' so agents can rely on the chaining behaviour.
    """

    def _make_split_package(self, tmp_path, tmp_project):
        """Build a minimal auto-split package and return (main_zip, prereqs_zip)."""
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "GCFR_STD.db",
            "CREATE DATABASE GCFR_STD FROM DBC AS PERMANENT = 1024;\n",
        )
        _write(
            payload / "DDL" / "tables" / "GCFR_STD.Customer.tbl",
            "CREATE MULTISET TABLE GCFR_STD.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        )
        props = _properties_for("DEV", tmp_path)
        cfg = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="GCFR",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )
        (main_arc, main_mf), (prereqs_arc, prereqs_mf) = build_package(cfg)
        return main_arc, prereqs_arc

    def test_main_deploy_py_contains_chaining_code(self, tmp_path, tmp_project):
        """The main deploy.py must contain chaining logic that reads ships.build.json
        and deploys the companion prereqs before the main package."""
        main_arc, _ = self._make_split_package(tmp_path, tmp_project)
        deploy_py = _read_zip_member(main_arc, "deploy.py")

        # Core chaining keywords must all be present
        assert "requires" in deploy_py, (
            "deploy.py must read 'requires' from ships.build.json"
        )
        assert "prereqs" in deploy_py.lower(), (
            "deploy.py must reference companion prereqs"
        )
        assert "deploy_package" in deploy_py, (
            "deploy.py must call deploy_package for prereqs"
        )
        assert "Deploy chaining" in deploy_py, "deploy.py must banner the chaining step"

    def test_main_build_json_requires_populated(self, tmp_path, tmp_project):
        """Main ships.build.json must have a non-empty requires list."""
        main_arc, _ = self._make_split_package(tmp_path, tmp_project)
        build = json.loads(_read_zip_member(main_arc, "ships.build.json"))

        assert build.get("role") == "main"
        assert build.get("requires"), (
            "main ships.build.json must have non-empty requires"
        )
        assert build["requires"][0].endswith(".zip")

    def test_prereqs_build_json_requires_empty(self, tmp_path, tmp_project):
        """Prereqs ships.build.json must have an empty requires list."""
        _, prereqs_arc = self._make_split_package(tmp_path, tmp_project)
        build = json.loads(_read_zip_member(prereqs_arc, "ships.build.json"))

        assert build.get("role") == "prereqs"
        assert build.get("requires") == [], (
            "prereqs ships.build.json must have empty requires"
        )

    def test_chaining_skipped_message_in_dry_run_path(self, tmp_path, tmp_project):
        """The dry-run code path must log that chaining is skipped."""
        main_arc, _ = self._make_split_package(tmp_path, tmp_project)
        deploy_py = _read_zip_member(main_arc, "deploy.py")

        assert "dry_run" in deploy_py
        assert "skipped in dry-run" in deploy_py.lower() or "skip" in deploy_py.lower()


class TestBuildPackageAutoSplitTokenised:
    """Tokenised CREATE DATABASE + tokenised dependants survive split."""

    def test_tokenised_pair_splits_cleanly(self, tmp_project, tmp_path):
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "{{T_DB}}.db",
            "CREATE DATABASE {{T_DB}} AS PERMANENT = 1024;\n",
        )
        _write(
            payload / "DDL" / "tables" / "{{T_DB}}.Customer.tbl",
            "CREATE MULTISET TABLE {{T_DB}}.Customer (Id INTEGER) "
            "PRIMARY INDEX (Id);\n",
        )

        properties = _properties_for("DEV", tmp_path, T_DB="A_D01_OMR_T")
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(properties),
            build_number=1,
            output_dir=str(tmp_path),
        )

        (main_pair, companion_pair) = build_package(config)

        assert companion_pair is not None
        main_archive, main_manifest = main_pair
        prereqs_archive, prereqs_manifest = companion_pair

        # Token-resolved files end up in the right zips.
        prereqs_files = _list_zip_phase_files(prereqs_archive, "01_pre_requisites")
        main_files = _list_zip_phase_files(main_archive, "03_ddl")
        assert any("A_D01_OMR_T" in name for name in prereqs_files)
        assert any("A_D01_OMR_T.Customer" in name for name in main_files)

        # Manifests still tied via release_group + requires.
        assert main_manifest.release_group == prereqs_manifest.release_group
        assert main_manifest.requires == [os.path.basename(prereqs_archive)]


# ---------------------------------------------------------------
# Prereq _order.txt uses resolved filenames (not tokens)
# ---------------------------------------------------------------


class TestPrereqOrderResolvedNames:
    """After build_package runs, the _order.txt inside the prereqs zip
    must reference the RESOLVED filenames (e.g. PDE_D01_00.db), not
    the token-form filenames ({{BASE_NODE}}.db) that the harvest wrote.
    The deployer reads _order.txt to find files; tokenised names would
    make all entries appear missing."""

    def test_order_txt_uses_resolved_names(self, tmp_project, tmp_path):
        payload = tmp_project / "payload" / "database"
        # Two databases where CHILD depends on PARENT.
        # Alphabetically CHILD sorts before PARENT (B < P) -- so without
        # dependency ordering, the wrong file would deploy first.
        (payload / "pre-requisites" / "databases" / "{{CHILD_DB}}.db").write_text(
            "CREATE DATABASE {{CHILD_DB}} FROM {{PARENT_DB}} AS PERM=0;\n",
            encoding="utf-8",
        )
        (payload / "pre-requisites" / "databases" / "{{PARENT_DB}}.db").write_text(
            "CREATE DATABASE {{PARENT_DB}} FROM DBC AS PERM=0;\n",
            encoding="utf-8",
        )

        # Resolve {{CHILD_DB}} → B_CHILD, {{PARENT_DB}} → P_PARENT
        # so B_CHILD sorts before P_PARENT alphabetically but must deploy after.
        props = _properties_for(
            "DEV",
            tmp_path,
            CHILD_DB="B_CHILD",
            PARENT_DB="P_PARENT",
        )
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(props),
            build_number=1,
            output_dir=str(tmp_path),
        )

        (main_pair, _companion) = build_package(config)
        archive = main_pair[0]

        # _order.txt must be present in the prereqs phase of the archive.
        order_txt = _read_zip_member(archive, "01_pre_requisites/_order.txt")

        # Must use RESOLVED names, not token names.
        assert "{{PARENT_DB}}" not in order_txt
        assert "{{CHILD_DB}}" not in order_txt
        assert "P_PARENT" in order_txt
        assert "B_CHILD" in order_txt

        # Parent must appear BEFORE child — the whole point of ordering.
        parent_pos = order_txt.find("P_PARENT")
        child_pos = order_txt.find("B_CHILD")
        assert parent_pos < child_pos, (
            "Parent database must appear before child in _order.txt"
        )


class TestDeployChainingResolutionScript:
    """Regression tests for generated deploy.py companion resolution."""

    def test_generated_deploy_script_resolves_release_group_siblings(
        self, tmp_project, tmp_path
    ):
        """Main deploy.py must search release-group siblings, including
        double-nested extraction layouts, not inside the main package only."""
        payload = tmp_project / "payload" / "database"
        _write(
            payload / "pre-requisites" / "databases" / "MyDB.db",
            "CREATE DATABASE MyDB AS PERM = 1000000;\n",
        )
        _write(
            payload / "DDL" / "tables" / "MyDB.Customer.tbl",
            "CREATE MULTISET TABLE MyDB.Customer (Id INTEGER) PRIMARY INDEX (Id);\n",
        )

        properties = _properties_for("DEV", tmp_path)
        config = BuildConfig(
            source_dir=str(tmp_project),
            environment="DEV",
            package_name="ships_test",
            env_config_file=str(properties),
            build_number=1,
            output_dir=str(tmp_path),
        )

        (main_pair, _companion_pair) = build_package(config)
        deploy_py = _read_zip_member(main_pair[0], "deploy.py")

        assert "def _find_companion_package" in deploy_py
        assert "def _normalise_package_dir" in deploy_py
        assert "required_base, required_base" in deploy_py
        assert "companion prereqs package not found" in deploy_py
        assert "os.path.dirname(SCRIPT_DIR), _prereqs_basename" not in deploy_py

    def test_package_copy_ignore_excludes_runtime_and_backup_artifacts(self):
        """Embedded runtime packages must not ship bytecode or backup files."""
        from td_release_packager.builder import _package_copy_ignore

        ignored = _package_copy_ignore(
            "ignored",
            [
                "__pycache__",
                "preflight.py.bak",
                "module.pyc",
                "module.pyo",
                "scratch.tmp",
                "old_file.old",
                "patch.rej",
                "swap.swp",
                "~temp",
                "deployer.py",
            ],
        )

        assert "__pycache__" in ignored
        assert "preflight.py.bak" in ignored
        assert "module.pyc" in ignored
        assert "module.pyo" in ignored
        assert "scratch.tmp" in ignored
        assert "old_file.old" in ignored
        assert "patch.rej" in ignored
        assert "swap.swp" in ignored
        assert "~temp" in ignored
        assert "deployer.py" not in ignored
