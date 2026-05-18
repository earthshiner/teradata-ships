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
