"""Launch SHIPS package deployments from archives or release groups.

This module removes the operator-facing need to manually extract package
archives or navigate into long generated directory names. It prepares a short
working directory, extracts the required package archives, and invokes each
package's generated ``deploy.py`` entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass
class PackageLaunch:
    """One generated deploy.py invocation."""

    role: str
    package_dir: Path
    deploy_py: Path
    archive: str = ""


@dataclass
class LaunchPlan:
    """Resolved deployment launch plan."""

    target: Path
    work_dir: Path
    invocations: list[PackageLaunch] = field(default_factory=list)


def launch_deploy(
    target: str,
    deploy_args: Sequence[str],
    *,
    role: str = "main",
    work_dir: str | None = None,
    python_executable: str | None = None,
) -> int:
    """Prepare and run a SHIPS deployment from *target*.

    Args:
        target: Zip archive, extracted package directory, or release-group
            directory containing ``release_group.json``.
        deploy_args: Arguments forwarded unchanged to generated ``deploy.py``.
        role: Package role to deploy from a release group. ``main`` is the
            normal DBA workflow; any required ``environment_prereqs`` package is
            run first.
        work_dir: Optional extraction directory. Defaults to ``.ships-work``
            beside the target.
        python_executable: Python interpreter used to launch generated scripts.

    Returns:
        The final subprocess return code.
    """
    plan = build_launch_plan(target, deploy_args, role=role, work_dir=work_dir)
    exe = python_executable or sys.executable

    for invocation in plan.invocations:
        cmd = [exe, str(invocation.deploy_py), *deploy_args]
        print(
            f"SHIPS deploy: {invocation.role} package at {invocation.package_dir}",
            flush=True,
        )
        result = subprocess.run(cmd, cwd=str(invocation.package_dir), check=False)
        if result.returncode != 0:
            return result.returncode

    return 0


def build_launch_plan(
    target: str,
    deploy_args: Sequence[str] = (),
    *,
    role: str = "main",
    work_dir: str | None = None,
) -> LaunchPlan:
    """Resolve extraction and generated deploy.py invocations for *target*."""
    target_path = Path(target).expanduser().resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"SHIPS deploy target does not exist: {target_path}")

    if target_path.is_dir() and (target_path / "release_group.json").is_file():
        return _plan_release_group(
            target_path, deploy_args, role=role, work_dir=work_dir
        )

    if target_path.is_file() and target_path.suffix.lower() == ".zip":
        return _plan_single_zip(target_path, work_dir=work_dir)

    if target_path.is_dir():
        package_dir = _normalise_package_dir(target_path)
        deploy_py = package_dir / "deploy.py"
        return LaunchPlan(
            target=target_path,
            work_dir=package_dir,
            invocations=[
                PackageLaunch(
                    role=_read_package_role(package_dir) or "package",
                    package_dir=package_dir,
                    deploy_py=deploy_py,
                )
            ],
        )

    raise ValueError(
        "SHIPS deploy target must be a .zip archive, extracted package directory, "
        f"or release-group directory: {target_path}"
    )


def _plan_single_zip(zip_path: Path, *, work_dir: str | None) -> LaunchPlan:
    root = (
        Path(work_dir).expanduser().resolve()
        if work_dir
        else _default_work_dir(zip_path)
    )
    package_dir = _extract_package_zip(zip_path, root)
    invocations = []

    pending = list(_read_package_requires(package_dir))
    seen: set[str] = set()
    while pending:
        required = pending.pop(0)
        if required in seen:
            continue
        seen.add(required)
        sibling = zip_path.parent / required
        if sibling.is_file() and sibling.suffix.lower() == ".zip":
            req_dir = _extract_package_zip(sibling, root)
            if _read_package_role(req_dir) == "environment_prereqs":
                invocations.append(
                    PackageLaunch(
                        role="environment_prereqs",
                        archive=sibling.name,
                        package_dir=req_dir,
                        deploy_py=req_dir / "deploy.py",
                    )
                )
            pending.extend(_read_package_requires(req_dir))

    invocations.append(
        PackageLaunch(
            role=_read_package_role(package_dir) or "package",
            archive=zip_path.name,
            package_dir=package_dir,
            deploy_py=package_dir / "deploy.py",
        )
    )
    return LaunchPlan(target=zip_path, work_dir=root, invocations=invocations)


def _plan_release_group(
    group_dir: Path,
    deploy_args: Sequence[str],
    *,
    role: str,
    work_dir: str | None,
) -> LaunchPlan:
    root = (
        Path(work_dir).expanduser().resolve()
        if work_dir
        else _default_work_dir(group_dir)
    )
    manifest = json.loads(
        (group_dir / "release_group.json").read_text(encoding="utf-8")
    )
    packages = manifest.get("packages", [])
    if not packages:
        raise ValueError(f"release_group.json has no packages: {group_dir}")

    extracted_by_archive: dict[str, Path] = {}
    for archive_name in manifest.get("deploy_order", []):
        archive_path = group_dir / archive_name
        if archive_path.is_file() and archive_path.suffix.lower() == ".zip":
            extracted_by_archive[archive_name] = _extract_package_zip(
                archive_path, root
            )

    selected = _select_package(packages, role)
    selected_archive = selected["archive"]
    selected_dir = extracted_by_archive.get(selected_archive)
    if selected_dir is None:
        raise FileNotFoundError(
            f"Selected archive is missing: {group_dir / selected_archive}"
        )

    invocations: list[PackageLaunch] = []
    if not _is_dry_run(deploy_args):
        for pkg in packages:
            if pkg.get("role") == "environment_prereqs":
                env_dir = extracted_by_archive.get(pkg["archive"])
                if env_dir is not None:
                    invocations.append(
                        PackageLaunch(
                            role="environment_prereqs",
                            archive=pkg["archive"],
                            package_dir=env_dir,
                            deploy_py=env_dir / "deploy.py",
                        )
                    )

    invocations.append(
        PackageLaunch(
            role=selected.get("role") or role,
            archive=selected_archive,
            package_dir=selected_dir,
            deploy_py=selected_dir / "deploy.py",
        )
    )
    return LaunchPlan(target=group_dir, work_dir=root, invocations=invocations)


def _select_package(packages: list[dict], role: str) -> dict:
    for pkg in packages:
        if pkg.get("role") == role:
            return pkg
    roles = ", ".join(sorted({str(pkg.get("role")) for pkg in packages}))
    raise ValueError(
        f"No package with role '{role}' in release group. Available: {roles}"
    )


def _extract_package_zip(zip_path: Path, dest_root: Path) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        top_level_dirs = _top_level_dirs(archive)
        _safe_extractall(archive, dest_root)

    candidates = [dest_root / name for name in top_level_dirs]
    candidates.append(dest_root / zip_path.stem)
    for candidate in candidates:
        try:
            package_dir = _normalise_package_dir(candidate)
        except FileNotFoundError:
            continue
        if (package_dir / "deploy.py").is_file():
            return package_dir

    discovered = _find_deploy_package_dir(dest_root)
    if discovered is not None:
        return discovered

    raise FileNotFoundError(f"No generated deploy.py found after extracting {zip_path}")


def _top_level_dirs(archive: zipfile.ZipFile) -> list[str]:
    dirs: set[str] = set()
    for name in archive.namelist():
        first = name.replace("\\", "/").split("/", 1)[0]
        if first:
            dirs.add(first)
    return sorted(dirs)


def _safe_extractall(archive: zipfile.ZipFile, dest_root: Path) -> None:
    dest = dest_root.resolve()
    for member in archive.infolist():
        member_target = (dest / member.filename).resolve()
        if dest != member_target and dest not in member_target.parents:
            raise ValueError(f"Unsafe ZIP member path: {member.filename}")
    archive.extractall(dest)


def _normalise_package_dir(path: Path) -> Path:
    path = path.resolve()
    if (path / "deploy.py").is_file():
        return path

    nested = path / path.name
    if (nested / "deploy.py").is_file():
        return nested.resolve()

    discovered = _find_deploy_package_dir(path)
    if discovered is not None:
        return discovered

    raise FileNotFoundError(f"No generated deploy.py found under {path}")


def _find_deploy_package_dir(root: Path) -> Path | None:
    for deploy_py in root.glob("*/deploy.py"):
        candidate = deploy_py.parent
        if (candidate / "context" / "ships.build.json").is_file():
            return candidate.resolve()
    return None


def _read_build_json(package_dir: Path) -> dict:
    build_json = package_dir / "context" / "ships.build.json"
    if not build_json.is_file():
        return {}
    return json.loads(build_json.read_text(encoding="utf-8"))


def _read_package_role(package_dir: Path) -> str:
    return str(_read_build_json(package_dir).get("role") or "")


def _read_package_requires(package_dir: Path) -> list[str]:
    requires = _read_build_json(package_dir).get("requires", [])
    return [str(item) for item in requires if item]


def _default_work_dir(target: Path) -> Path:
    if target.is_file():
        return target.parent / ".ships-work" / target.stem
    return target / ".ships-work"


def _is_dry_run(args: Sequence[str]) -> bool:
    return "--dry-run" in args
