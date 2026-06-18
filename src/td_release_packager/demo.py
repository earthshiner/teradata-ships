"""Low-friction SHIPS demo packaging workflow.

The demo workflow accepts a plain SQL repository, stages it into a
temporary SHIPS project, applies the minimum useful SHIPS checks, and
optionally produces/deploys a package. It is deliberately thinner than
the production pipeline: the goal is to keep "clone, configure, run"
ergonomics while reusing SHIPS' tokenisation, classification, linting,
analysis, packaging, and deploy launcher.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from td_release_packager.analyser import analyse_project
from td_release_packager.builder import build_package
from td_release_packager.discovery import resolve_harvest_extensions
from td_release_packager.ingest import ingest_directory
from td_release_packager.models import BuildConfig
from td_release_packager.root_parent import inject_root_parent, normalise_root_parent
from td_release_packager.scaffolder import scaffold_project
from td_release_packager.token_engine import generate_token_map, write_token_map
from td_release_packager.validate import (
    read_inspect_config,
    resolve_inspect_root,
    validate_directory,
)


DEMO_INSPECT_OVERRIDES: dict[str, str] = {
    "db_qualifier": "WARNING",
    "one_object": "WARNING",
    "eponymous": "WARNING",
    "set_multiset": "WARNING",
    "zero_tokens": "WARNING",
    "hardcoded_name": "WARNING",
    "object_placement": "WARNING",
    "view_column_list": "WARNING",
    "public_grant_on_tables": "WARNING",
    "review_unmapped_grants": "WARNING",
    "secret_scan": "ERROR",
    "vault_ref": "ERROR",
    "extension": "ERROR",
    "type_suffix": "ERROR",
}


@dataclass
class DemoResult:
    """Outcome of a demo workflow run."""

    project_dir: Path
    source_dir: Path
    env_config: Path
    token_map: Path
    lint_errors: int = 0
    lint_warnings: int = 0
    classified: int = 0
    unclassified: int = 0
    analysis_objects: int = 0
    analysis_waves: int = 0
    archive_path: str = ""
    release_group: str = ""
    companion_archive_path: str = ""
    report_paths: list[str] = field(default_factory=list)
    root_parent_injections: int = 0
    deploy_exit_code: int | None = None
    warnings: list[str] = field(default_factory=list)


def run_demo(
    *,
    source: str,
    name: str | None = None,
    work_dir: str = ".ships-demo",
    output_dir: str | None = None,
    env: str = "DEV",
    env_prefix: str | None = None,
    root_parent: str | None = None,
    package: bool = True,
    deploy: bool = False,
    deploy_args: Sequence[str] = (),
    source_commit: str = "",
    author: str = "",
) -> DemoResult:
    """Run the low-friction demo workflow.

    Args:
        source: Source repository/directory containing SQL.
        name: Demo project/package name. Defaults to the source folder name.
        work_dir: Parent directory for the generated SHIPS demo project.
        output_dir: Optional package output directory. Defaults to
            ``<project>/releases``.
        env: Logical environment name used for packaging.
        env_prefix: Optional prefix stripped when deriving token names.
        root_parent: Optional root database/user parent for parentless
            ``CREATE DATABASE`` and ``CREATE USER`` demo prerequisites.
        package: Build a SHIPS package after staging and analysis.
        deploy: Launch the built package/release group after packaging.
        deploy_args: Arguments forwarded to ``td_release_packager deploy``.
        source_commit: Optional source commit stamped into package metadata.
        author: Optional package author.

    Returns:
        DemoResult describing generated artefacts.
    """
    source_root = Path(source).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(
            f"[DemoSourceMissing] Source directory not found: {source}"
        )

    demo_name = _normalise_demo_name(name or source_root.name)
    work_root = Path(work_dir).expanduser().resolve()
    project_dir = work_root / demo_name

    if project_dir.exists() and not (project_dir / "ships.yaml").is_file():
        raise FileExistsError(
            "[DemoProjectBlocked] Demo work directory exists but is not a SHIPS "
            f"project: {project_dir}. Choose a different --work-dir or --name."
        )

    if project_dir.exists():
        scaffold_project(demo_name, str(work_root), environments=[env], repair=True)
    else:
        scaffold_project(demo_name, str(work_root), environments=[env])

    sql_source = discover_demo_sql_root(source_root)
    _write_demo_inspect_config(project_dir)

    token_candidates = detect_demo_token_candidates(sql_source)
    token_map = generate_token_map(token_candidates, env_prefix)
    token_map_path = project_dir / "config" / "token_map.conf"
    write_token_map(str(token_map_path), token_map, token_candidates, env_prefix or "")

    result = ingest_directory(
        source_dir=str(sql_source),
        project_dir=str(project_dir),
        detect_tokens=True,
        apply_tokens=token_map,
        force=True,
        clean_payload=True,
    )
    root_parent_value = normalise_root_parent(root_parent)
    root_parent_injections = inject_root_parent(
        project_dir,
        root_parent_value,
        parent_expression=_ROOT_PARENT_TOKEN,
    )
    env_config = _write_demo_env_config(project_dir, env, token_map, root_parent_value)

    rules = read_inspect_config(str(project_dir / "config" / "inspect.conf"))
    lint = validate_directory(
        resolve_inspect_root(str(project_dir)), rules_config=rules, strict=False
    )
    analysis = analyse_project(str(project_dir))

    demo_result = DemoResult(
        project_dir=project_dir,
        source_dir=sql_source,
        env_config=env_config,
        token_map=token_map_path,
        lint_errors=lint.errors,
        lint_warnings=lint.warnings,
        classified=result.classified,
        unclassified=result.unclassified,
        analysis_objects=len(analysis.objects),
        analysis_waves=len(analysis.waves),
        root_parent_injections=root_parent_injections,
        warnings=list(result.warnings),
    )

    if package:
        config = BuildConfig(
            source_dir=str(project_dir),
            environment=env.upper(),
            package_name=demo_name,
            env_config_file=str(env_config),
            build_number=1,
            output_dir=str(Path(output_dir).expanduser().resolve())
            if output_dir
            else str(project_dir / "releases"),
            archive_format="zip",
            author=author,
            description="Built with SHIPS demo mode.",
            source_commit=source_commit,
            allow_dirty=True,
        )
        main_pair, companion_pair = build_package(config)
        archive_path, manifest = main_pair
        demo_result.archive_path = archive_path
        demo_result.release_group = str(Path(archive_path).parent)
        archive_paths = [Path(archive_path)]
        if companion_pair is not None:
            demo_result.companion_archive_path = companion_pair[0]
            archive_paths.append(Path(companion_pair[0]))
        demo_result.report_paths = [
            str(path) for path in _copy_package_reports_from_archives(archive_paths)
        ]

    if deploy:
        if not demo_result.archive_path:
            raise ValueError("[DemoDeployNoPackage] --deploy requires package output.")
        from td_release_packager.deploy_launcher import launch_deploy

        target = demo_result.release_group or demo_result.archive_path
        demo_result.deploy_exit_code = launch_deploy(target, list(deploy_args))

    return demo_result


def _copy_package_reports_from_archives(archive_paths: Sequence[Path]) -> list[Path]:
    """Copy package reports beside archives using short, Windows-friendly names."""
    report_paths: list[Path] = []
    used_names: set[str] = set()
    for archive_path in archive_paths:
        if not archive_path.is_file() or archive_path.suffix.lower() != ".zip":
            continue
        try:
            with zipfile.ZipFile(archive_path) as archive:
                report_member = _find_package_report_member(archive.namelist())
                if report_member is None:
                    continue
                role = _report_role_from_archive(archive_path)
                output_name = _unique_report_name(role, used_names)
                report_path = archive_path.parent / output_name
                report_path.write_bytes(archive.read(report_member))
                report_paths.append(report_path)
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
    return report_paths


def _find_package_report_member(members: Sequence[str]) -> str | None:
    for member in members:
        normalised = member.replace("\\", "/")
        if normalised == "package_report.html":
            return member
        if normalised.endswith("/package_report.html"):
            return member
    return None


def _report_role_from_archive(archive_path: Path) -> str:
    name = archive_path.name.lower()
    if "_00_environment_prereqs" in name:
        return "environment_prereqs"
    if "_01_prereqs" in name:
        return "prereqs"
    if "_01_main" in name or "_02_main" in name:
        return "main"
    return "package"


def _unique_report_name(role: str, used_names: set[str]) -> str:
    base_name = f"package_report_{role}.html"
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    index = 2
    while True:
        name = f"package_report_{role}_{index}.html"
        if name not in used_names:
            used_names.add(name)
            return name
        index += 1


def discover_demo_sql_root(source_root: Path) -> Path:
    """Return the most likely SQL source root in a demo repository."""
    workspace_src = source_root / "workspace" / "src"
    if workspace_src.is_dir():
        children = [p for p in workspace_src.iterdir() if p.is_dir()]
        if len(children) == 1:
            return children[0].resolve()
        if children:
            return workspace_src.resolve()

    sql_dirs: list[Path] = []
    for child in source_root.iterdir():
        if child.is_dir() and any(child.rglob("*.sql")):
            sql_dirs.append(child)
    if len(sql_dirs) == 1:
        return sql_dirs[0].resolve()

    return source_root


_ROOT_PARENT_TOKEN = "{{ROOT_PARENT}}"


def detect_demo_token_candidates(source_root: Path) -> dict[str, list[str]]:
    """Detect literal database names without mutating the source or project."""
    candidates: dict[str, list[str]] = {}
    extensions = resolve_harvest_extensions()
    for path in sorted(p for p in source_root.rglob("*") if p.is_file()):
        if path.suffix.lower() not in extensions:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(source_root))
        for db_name in _literal_database_names(content):
            candidates.setdefault(db_name, []).append(rel)
    return candidates


def _normalise_demo_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return cleaned.strip("._-") or "DemoPackage"


_QUALIFIED_OWNER_RE = re.compile(
    r"\b(?:CREATE|REPLACE)\s+"
    r"(?:(?:MULTISET|SET|VOLATILE|GLOBAL\s+TEMPORARY)\s+)*"
    r"(?:TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|JOIN\s+INDEX|HASH\s+INDEX)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)\s*\.",
    re.IGNORECASE,
)
_QUALIFIED_REFERENCE_RE = re.compile(
    r"\b(?:FROM|JOIN|USING|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|ON)"
    r"\s+(?:TABLE\s+|VIEW\s+|MACRO\s+|PROCEDURE\s+|FUNCTION\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\.",
    re.IGNORECASE,
)
_DATABASE_DDL_RE = re.compile(
    r"\b(?:CREATE|DROP|DELETE|COMMENT\s+ON)\s+DATABASE\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_BARE_GRANT_RE = re.compile(
    r"\b(?:GRANT|REVOKE)\b.*?\bON\s+(?:DATABASE\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE | re.DOTALL,
)
_DEMO_TOKEN_STOPWORDS = {
    "ALL",
    "AS",
    "BIGINT",
    "BYTEINT",
    "CHAR",
    "CHARACTER",
    "CREATE",
    "CURRENT_TIMESTAMP",
    "DATE",
    "DECIMAL",
    "DEFAULT",
    "DELETE",
    "DROP",
    "FROM",
    "INDEX",
    "INSERT",
    "INTEGER",
    "JOIN",
    "MULTISET",
    "NOT",
    "NULL",
    "ON",
    "PRIMARY",
    "REPLACE",
    "SELECT",
    "SET",
    "SMALLINT",
    "TABLE",
    "TIME",
    "TIMESTAMP",
    "UPDATE",
    "VALUES",
    "VARCHAR",
    "VIEW",
    "WITH",
    "ZONE",
}


def _literal_database_names(content: str) -> set[str]:
    names = {match.group(1) for match in _QUALIFIED_OWNER_RE.finditer(content)}
    names.update(match.group(1) for match in _QUALIFIED_REFERENCE_RE.finditer(content))
    names.update(match.group(1) for match in _DATABASE_DDL_RE.finditer(content))
    names.update(match.group(1) for match in _BARE_GRANT_RE.finditer(content))
    return {
        name
        for name in names
        if not name.startswith("{{") and name.upper() not in _DEMO_TOKEN_STOPWORDS
    }


def _write_demo_inspect_config(project_dir: Path) -> Path:
    config_path = project_dir / "config" / "inspect.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    marker = "# ---- SHIPS demo mode overrides ----"
    if marker in existing:
        existing = existing.split(marker, 1)[0].rstrip() + "\n"

    lines = [existing.rstrip(), "", marker]
    lines.extend(
        f"{name}={severity}" for name, severity in DEMO_INSPECT_OVERRIDES.items()
    )
    config_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return config_path


def _write_demo_env_config(
    project_dir: Path,
    env: str,
    token_map: dict[str, str],
    root_parent: str | None = None,
) -> Path:
    env_upper = env.upper()
    env_path = project_dir / "config" / "env" / f"{env_upper}.conf"
    env_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# {env_upper} demo environment config",
        "# Generated by SHIPS demo mode from detected database names.",
        f"SHIPS_ENV={env_upper}",
        f"ENV_PREFIX={env_upper}",
        f"SHIPS_PROJECT={project_dir.name}",
        "PERM_SPACE=1e9",
        "SPOOL_SPACE=1e9",
        "",
    ]
    if root_parent:
        lines.append(f"ROOT_PARENT={root_parent}")
        lines.append("")

    for literal, token in sorted(token_map.items()):
        token_name = token.strip("{}")
        if token_name:
            lines.append(f"{token_name}={literal}")
            for suffix in ("T", "V", "M", "P", "F"):
                lines.append(f"{token_name}_{suffix}={literal}")

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return env_path
