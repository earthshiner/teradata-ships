import json
import tarfile
import zipfile
from types import SimpleNamespace

from td_release_packager.cli import _write_package_run_context_to_archives


def _fake_run():
    return SimpleNamespace(
        _run_entry={
            "run_id": "run-1234",
            "command": "process",
            "started_at": "2026-05-14T00:00:00+00:00",
            "finished_at": "2026-05-14T00:00:05+00:00",
            "duration_ms": 5000,
            "final_status": "failed",
            "stages": [
                {
                    "stage": "inspect",
                    "started_at": "2026-05-14T00:00:01+00:00",
                    "finished_at": "2026-05-14T00:00:02+00:00",
                    "duration_ms": 1000,
                    "status": "error",
                    "inputs": {"source_dir": "project"},
                    "outputs": {"error_count": 1},
                    "decisions": {},
                    "issues": [
                        {
                            "severity": "error",
                            "code": "INSPECT-LINT",
                            "message": "lint failed",
                        }
                    ],
                },
                {
                    "stage": "package",
                    "started_at": "2026-05-14T00:00:03+00:00",
                    "finished_at": "2026-05-14T00:00:04+00:00",
                    "duration_ms": 1000,
                    "status": "success",
                    "inputs": {},
                    "outputs": {"archive_path": "pkg.zip"},
                    "decisions": {},
                    "issues": [],
                },
            ],
        }
    )


def test_process_run_context_is_written_to_zip_archive(tmp_path):
    archive_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("pkg/README.txt", "hello")
        archive.writestr("pkg/context/ships.index.json", "{}")

    written = _write_package_run_context_to_archives(
        str(tmp_path),
        [str(archive_path)],
        _fake_run(),
    )

    assert written == [str(archive_path)]
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "pkg/context/stages/process.result.json" in names
        assert "pkg/context/stages/inspect.result.json" in names
        process = json.loads(archive.read("pkg/context/stages/process.result.json"))
        inspect = json.loads(archive.read("pkg/context/stages/inspect.result.json"))

    assert process["package_local"] is True
    assert process["project_decisions_path"].endswith("ships.decisions.json")
    assert process["final_status"] == "failed"
    assert inspect["issue_counts"]["error"] == 1


def test_process_run_context_is_written_to_tar_gz_archive(tmp_path):
    package_dir = tmp_path / "pkg"
    (package_dir / "context").mkdir(parents=True)
    (package_dir / "README.txt").write_text("hello")
    (package_dir / "context" / "ships.index.json").write_text("{}")
    archive_path = tmp_path / "pkg.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(package_dir, arcname="pkg")

    written = _write_package_run_context_to_archives(
        str(tmp_path),
        [str(archive_path)],
        _fake_run(),
    )

    assert written == [str(archive_path)]
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        assert "pkg/context/stages/process.result.json" in names
        member = archive.extractfile("pkg/context/stages/package.result.json")
        assert member is not None
        package = json.loads(member.read().decode("utf-8"))

    assert package["status"] == "success"
