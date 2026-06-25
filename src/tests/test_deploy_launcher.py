import json
import subprocess
import zipfile
from pathlib import Path

from td_release_packager.deploy_launcher import build_launch_plan, launch_deploy


def _write_package_zip(
    root: Path,
    name: str,
    *,
    role: str = "main",
    requires: list[str] | None = None,
) -> Path:
    zip_path = root / f"{name}.zip"
    build_json = {
        "role": role,
        "requires": requires or [],
        "package_name": "Demo",
        "release_group": "DEV_Demo_BUILD_0001_20260518000000",
    }
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{name}/deploy.py", "print('deploy')\n")
        archive.writestr(f"{name}/context/ships.build.json", json.dumps(build_json))
        archive.writestr(f"{name}/payload/.gitkeep", "")
    return zip_path


def test_single_zip_plans_generated_deploy_without_manual_extraction(tmp_path):
    archive = _write_package_zip(tmp_path, "DEV_Demo_BUILD_0001_01_main")

    plan = build_launch_plan(str(archive), ["--dry-run"])

    assert plan.work_dir == tmp_path / ".ships-work" / archive.stem
    assert len(plan.invocations) == 1
    invocation = plan.invocations[0]
    assert invocation.role == "main"
    assert invocation.deploy_py.is_file()
    assert invocation.package_dir.name == archive.stem


def test_single_zip_extracts_transitive_environment_prereq_before_main(tmp_path):
    env = _write_package_zip(
        tmp_path,
        "DEV_Demo_BUILD_0001_00_environment_prereqs",
        role="environment_prereqs",
    )
    prereqs = _write_package_zip(
        tmp_path,
        "DEV_Demo_BUILD_0001_01_prereqs",
        role="prereqs",
        requires=[env.name],
    )
    main = _write_package_zip(
        tmp_path,
        "DEV_Demo_BUILD_0001_02_main",
        role="main",
        requires=[prereqs.name],
    )

    plan = build_launch_plan(str(main), ["--host", "td"])

    assert [item.role for item in plan.invocations] == [
        "environment_prereqs",
        "main",
    ]


def test_release_group_plans_environment_prereq_before_main(tmp_path):
    group = tmp_path / "DEV_Demo_BUILD_0001_20260518000000"
    group.mkdir()
    env = _write_package_zip(
        group, f"{group.name}_00_environment_prereqs", role="environment_prereqs"
    )
    main = _write_package_zip(group, f"{group.name}_01_main", role="main")
    (group / "release_group.json").write_text(
        json.dumps(
            {
                "release_group": group.name,
                "deploy_order": [env.name, main.name],
                "packages": [
                    {"role": "environment_prereqs", "archive": env.name},
                    {"role": "main", "archive": main.name},
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = build_launch_plan(str(group), ["--host", "td"])

    assert [item.role for item in plan.invocations] == [
        "environment_prereqs",
        "main",
    ]


def test_release_group_dry_run_only_runs_selected_role(tmp_path):
    group = tmp_path / "DEV_Demo_BUILD_0001_20260518000000"
    group.mkdir()
    env = _write_package_zip(
        group, f"{group.name}_00_environment_prereqs", role="environment_prereqs"
    )
    main = _write_package_zip(group, f"{group.name}_01_main", role="main")
    (group / "release_group.json").write_text(
        json.dumps(
            {
                "release_group": group.name,
                "deploy_order": [env.name, main.name],
                "packages": [
                    {"role": "environment_prereqs", "archive": env.name},
                    {"role": "main", "archive": main.name},
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = build_launch_plan(str(group), ["--dry-run"])

    assert [item.role for item in plan.invocations] == ["main"]


def test_launch_deploy_forwards_args_to_generated_deploy_py(tmp_path, monkeypatch):
    archive = _write_package_zip(tmp_path, "DEV_Demo_BUILD_0001_01_main")
    calls = []

    def fake_run(cmd, cwd, check):
        calls.append((cmd, cwd, check))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = launch_deploy(
        str(archive),
        ["--host", "td", "--user", "dbc"],
        python_executable="python-test",
    )

    assert rc == 0
    assert calls[0][0][0] == "python-test"
    assert calls[0][0][-4:] == ["--host", "td", "--user", "dbc"]
    assert calls[0][1].endswith("DEV_Demo_BUILD_0001_01_main")


# ---------------------------------------------------------------------------
# Issue #392 — cosmetic-artefact extraction must never abort deploy.
# ---------------------------------------------------------------------------


def test_is_cosmetic_member_recognises_package_and_deploy_report_paths():
    from td_release_packager.deploy_launcher import _is_cosmetic_member

    assert _is_cosmetic_member(
        "DEV_Demo_BUILD_0001_01_main/.package_report_code/0001_a3f8b2c19e4d.html"
    )
    assert _is_cosmetic_member(
        ".deploy_report_2026-01-01T00-00Z_code/0001_a3f8b2c19e4d.html"
    )
    # Backslash separators (Windows ZIP entries) must also match.
    assert _is_cosmetic_member(
        r"DEV_Demo_BUILD_0001_01_main\.package_report_code\0001_a3f8b2c19e4d.html"
    )
    # Real payload files must NOT match — a missing .sql must surface.
    assert not _is_cosmetic_member(
        "DEV_Demo_BUILD_0001_01_main/payload/03_ddl/tables/DB.Customer.tbl"
    )
    assert not _is_cosmetic_member(
        "DEV_Demo_BUILD_0001_01_main/context/ships.build.json"
    )


def test_safe_extractall_skips_cosmetic_failures_only(tmp_path, monkeypatch):
    """Issue #392 regression: an OSError on a viewer page is swallowed with a
    summary warning; an OSError on payload propagates so deploy aborts on
    real failures."""
    import logging
    import zipfile

    from td_release_packager import deploy_launcher
    from td_release_packager.deploy_launcher import _safe_extractall

    # Build an archive with a payload file, two cosmetic viewer pages,
    # and a manifest. All members validate the Zip-Slip guard.
    archive_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive_path, "w") as z:
        z.writestr("pkg/payload/03_ddl/tables/DB.T.tbl", "CREATE TABLE x;")
        z.writestr("pkg/.package_report_code/0001_aaaaaaaaaaaa.html", "viewer 1")
        z.writestr("pkg/.package_report_code/0002_bbbbbbbbbbbb.html", "viewer 2")
        z.writestr("pkg/context/ships.build.json", '{"role":"main"}')

    # Stub archive.extract: raise OSError on cosmetic members, succeed on
    # real payload + manifest. This simulates Windows MAX_PATH failing on
    # the viewer pages without actually nesting paths past 260 chars.
    original_extract = zipfile.ZipFile.extract

    def _flaky_extract(self, member, path=None, pwd=None):
        name = member.filename if hasattr(member, "filename") else member
        if ".package_report_code/" in name.replace("\\", "/"):
            raise OSError(2, "No such file or directory", str(path))
        return original_extract(self, member, path=path, pwd=pwd)

    monkeypatch.setattr(zipfile.ZipFile, "extract", _flaky_extract)

    dest = tmp_path / "out"
    dest.mkdir()

    # Capture warnings emitted by deploy_launcher's module logger.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    deploy_launcher_logger = logging.getLogger("td_release_packager.deploy_launcher")
    deploy_launcher_logger.addHandler(handler)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            _safe_extractall(archive, dest)
    finally:
        deploy_launcher_logger.removeHandler(handler)

    # Payload + manifest extracted successfully despite cosmetic failures.
    assert (dest / "pkg" / "payload" / "03_ddl" / "tables" / "DB.T.tbl").is_file()
    assert (dest / "pkg" / "context" / "ships.build.json").is_file()

    # Exactly one summary WARN line, not one per failed page.
    cosmetic_warnings = [r for r in records if "viewer page" in r.getMessage().lower()]
    assert len(cosmetic_warnings) == 1, (
        f"expected exactly one summary warning; got {[r.getMessage() for r in records]}"
    )
    assert "2" in cosmetic_warnings[0].getMessage(), (
        "summary must count the skipped pages — got "
        f"{cosmetic_warnings[0].getMessage()!r}"
    )


def test_safe_extractall_payload_failure_still_aborts(tmp_path, monkeypatch):
    """Negative guard for #392: an OSError on a real payload file must
    propagate so a broken package can never deploy silently."""
    import zipfile

    from td_release_packager.deploy_launcher import _safe_extractall

    archive_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive_path, "w") as z:
        z.writestr("pkg/payload/03_ddl/tables/DB.T.tbl", "CREATE TABLE x;")

    def _always_fail(self, member, path=None, pwd=None):
        raise OSError(2, "No such file or directory", str(path))

    monkeypatch.setattr(zipfile.ZipFile, "extract", _always_fail)

    dest = tmp_path / "out"
    dest.mkdir()
    import pytest

    with pytest.raises(OSError):
        with zipfile.ZipFile(archive_path, "r") as archive:
            _safe_extractall(archive, dest)
