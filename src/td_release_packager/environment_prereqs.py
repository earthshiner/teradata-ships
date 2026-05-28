"""Environment prerequisite analysis for SHIPS packages.

Detects CREATE DATABASE / CREATE USER parent containers that are required by
package prerequisites but are not themselves created by the package.  These
external parents are platform/environment responsibilities, so SHIPS emits a
reviewable _00_environment_prereqs package instead of silently creating them
inside the application package.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Teradata database/user creation syntax used by SHIPS prerequisite payloads.
_CREATE_PARENT_RE = re.compile(
    r"^\s*CREATE\s+(DATABASE|USER)\s+"
    r"(\{\{[A-Za-z_]\w*\}\}|[\"']?[A-Za-z_]\w*[\"']?)"
    r"\s+FROM\s+"
    r"(\{\{[A-Za-z_]\w*\}\}|[\"']?[A-Za-z_]\w*[\"']?)",
    re.IGNORECASE | re.MULTILINE,
)
_PERM_RE = re.compile(
    r"\bPERM\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)\b",
    re.IGNORECASE,
)

# Parents that are normally platform roots and should not trigger a generated
# SHIPS environment-prerequisite package.
_DEFAULT_KNOWN_EXTERNAL_PARENTS = frozenset({"DBC"})

_DBA_PARENT_PLACEHOLDER = "<DBA_SELECTED_PARENT>"
_DBA_PERM_PLACEHOLDER = "<DBA_REVIEWED_PERM>"
_DBA_PLACEHOLDERS = frozenset({_DBA_PARENT_PLACEHOLDER, _DBA_PERM_PLACEHOLDER})
_SQL_LINE_COMMENT_RE = re.compile(r"--.*?$", re.MULTILINE)
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_PLACEHOLDER_SCAN_SUFFIXES = frozenset({".db", ".usr"})


def _normalise_identifier(value: str) -> str:
    """Normalise a Teradata identifier for case-insensitive comparison."""
    return value.strip().strip("'\"").upper()


def _strip_sql_comments(text: str) -> str:
    """Remove SQL comments before checking deployable placeholder state."""
    without_blocks = _SQL_BLOCK_COMMENT_RE.sub(
        lambda match: "\n" * match.group(0).count("\n"),
        text,
    )
    return _SQL_LINE_COMMENT_RE.sub("", without_blocks)


def _placeholder_scan_files(base: Path) -> Iterable[Path]:
    """Yield deployable payload files that can contain DBA placeholders."""
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith((".", "_")):
            continue
        if path.suffix.lower() not in _PLACEHOLDER_SCAN_SUFFIXES:
            continue
        yield path


def _parse_perm_bytes(value_str: str, suffix: str) -> int:
    """Convert a Teradata PERM literal with optional K/M/G/T suffix to bytes."""
    value = float(value_str)
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }.get(suffix.upper(), 1)
    return int(value * multiplier)


def _format_bytes(num_bytes: int) -> str:
    """Format a byte count as a compact human-readable string."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} EB"


@dataclass
class ParentDependency:
    """One CREATE DATABASE/USER FROM dependency parsed from prereq DDL."""

    child_name: str
    child_type: str
    parent_name: str
    source_file: str
    declared_perm_bytes: int = 0

    def to_dict(self) -> dict:
        """Serialise the dependency for context JSON."""
        return {
            "child_name": self.child_name,
            "child_type": self.child_type,
            "parent_name": self.parent_name,
            "source_file": self.source_file,
            "declared_perm_bytes": self.declared_perm_bytes,
            "declared_perm": _format_bytes(self.declared_perm_bytes)
            if self.declared_perm_bytes
            else "UNKNOWN",
        }


@dataclass
class EnvironmentParentRequirement:
    """An external parent database/user required before app prereqs deploy."""

    parent_name: str
    required_by: list[ParentDependency] = field(default_factory=list)
    minimum_required_perm_bytes: int = 0
    recommended_buffer_percent: int = 20

    @property
    def recommended_perm_bytes(self) -> int:
        """Return minimum PERM plus the configured review buffer."""
        return int(
            self.minimum_required_perm_bytes
            * (1 + (self.recommended_buffer_percent / 100.0))
        )

    def to_dict(self) -> dict:
        """Serialise the requirement for context JSON."""
        return {
            "name": self.parent_name,
            "required_by": [dep.to_dict() for dep in self.required_by],
            "minimum_required_perm_bytes": self.minimum_required_perm_bytes,
            "minimum_required_perm": _format_bytes(self.minimum_required_perm_bytes),
            "recommended_buffer_percent": self.recommended_buffer_percent,
            "recommended_perm_bytes": self.recommended_perm_bytes,
            "recommended_perm": _format_bytes(self.recommended_perm_bytes),
            "status": "requires_dba_review",
            "action": "create_parent_database_or_confirm_existing",
        }


def _iter_prereq_files(package_dir: str) -> Iterable[Path]:
    """Yield CREATE DATABASE/USER candidate files from a package directory."""
    root = Path(package_dir) / "payload" / "01_pre_requisites"
    for subdir in ("databases", "users"):
        folder = root / subdir
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.name.startswith(".") or path.name.startswith("_"):
                continue
            if path.suffix.lower() in {".db", ".usr"}:
                yield path


def analyse_environment_parent_requirements(
    package_dir: str,
    known_external_parents: Iterable[str] | None = None,
) -> list[EnvironmentParentRequirement]:
    """Find external parent containers required by prereq DDL in a package.

    Args:
        package_dir: Package root containing payload/01_pre_requisites.
        known_external_parents: Parent names that are assumed to exist and should
            not produce an environment-prerequisite package. Defaults to DBC.

    Returns:
        Sorted list of environment parent requirements.
    """
    known = {
        _normalise_identifier(parent)
        for parent in (known_external_parents or _DEFAULT_KNOWN_EXTERNAL_PARENTS)
    }
    dependencies: list[ParentDependency] = []
    created_names: set[str] = set()

    for path in _iter_prereq_files(package_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        match = _CREATE_PARENT_RE.search(text)
        if not match:
            continue

        child_type = match.group(1).upper()
        child_name = _normalise_identifier(match.group(2))
        parent_name = _normalise_identifier(match.group(3))
        created_names.add(child_name)

        perm_bytes = 0
        perm_match = _PERM_RE.search(text)
        if perm_match:
            perm_bytes = _parse_perm_bytes(perm_match.group(1), perm_match.group(2))

        dependencies.append(
            ParentDependency(
                child_name=child_name,
                child_type=child_type,
                parent_name=parent_name,
                source_file=os.path.relpath(path, package_dir).replace(os.sep, "/"),
                declared_perm_bytes=perm_bytes,
            )
        )

    by_parent: dict[str, EnvironmentParentRequirement] = {}
    for dep in dependencies:
        if dep.parent_name in created_names or dep.parent_name in known:
            continue
        requirement = by_parent.setdefault(
            dep.parent_name,
            EnvironmentParentRequirement(parent_name=dep.parent_name),
        )
        requirement.required_by.append(dep)
        requirement.minimum_required_perm_bytes += dep.declared_perm_bytes

    return [by_parent[name] for name in sorted(by_parent)]


def _perm_literal_for_requirement(req: EnvironmentParentRequirement) -> str:
    """Return a PERM literal for generated DBA-review DDL.

    When SHIPS cannot infer child PERM declarations, do not emit ``perm = 0``
    because that can be mistaken for an approved allocation. Use a clear DBA
    placeholder instead.
    """
    if req.recommended_perm_bytes <= 0:
        return _DBA_PERM_PLACEHOLDER
    return str(req.recommended_perm_bytes)


def _render_parent_database_ddl(req: EnvironmentParentRequirement) -> str:
    """Render the deployable payload DDL for one missing parent database."""
    required_lines = []
    for dep in req.required_by:
        required_lines.append(
            f"--   {dep.child_type} {dep.child_name} "
            f"({dep.source_file}, PERM={_format_bytes(dep.declared_perm_bytes)})"
        )

    lines = [
        "-- SHIPS generated environment prerequisite payload.",
        "-- DBA review required before deployment.",
        "--",
        f"-- Missing parent database: {req.parent_name}",
        "-- Required by:",
        *required_lines,
        f"-- Minimum required PERM: {_format_bytes(req.minimum_required_perm_bytes)}",
        f"-- Recommended PERM (+{req.recommended_buffer_percent}% buffer): "
        f"{_format_bytes(req.recommended_perm_bytes)}",
        "--",
        "-- Replace placeholders before approving/repackaging this package:",
        f"--   {_DBA_PARENT_PLACEHOLDER}",
        f"--   {_DBA_PERM_PLACEHOLDER}",
        "",
        f"create database {req.parent_name}",
        f"from {_DBA_PARENT_PLACEHOLDER}",
        f"as perm = {_perm_literal_for_requirement(req)}",
        ";",
        "",
    ]
    return "\n".join(lines)


def write_environment_prereq_payload(
    package_dir: str,
    requirements: list[EnvironmentParentRequirement],
) -> list[str]:
    """Write deployable DBA-review payload files for missing parents.

    The context review script is explanatory only. The SHIPS-managed audit path
    requires deployable payload, so each missing parent database is emitted as a
    ``.db`` file under ``payload/01_pre_requisites/databases``. Files contain
    explicit DBA placeholders when platform parent/PERM values are not known.

    Args:
        package_dir: Root of the generated _00_environment_prereqs package.
        requirements: Missing external parent requirements.

    Returns:
        Package-relative payload paths written.
    """
    payload_dir = Path(package_dir) / "payload" / "01_pre_requisites" / "databases"
    payload_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for req in requirements:
        filename = f"{req.parent_name}.db"
        target = payload_dir / filename
        target.write_text(_render_parent_database_ddl(req), encoding="utf-8")
        written.append(str(target.relative_to(package_dir)).replace("\\", "/"))
    return written


def has_dba_placeholders(package_dir: str) -> bool:
    """Return True when generated DBA placeholders remain in deployable SQL."""
    return bool(find_dba_placeholders(package_dir))


def find_dba_placeholders(package_dir: str) -> list[tuple[str, int, str]]:
    """Return executable DBA placeholder locations as path, line and marker."""
    root = Path(package_dir)
    # Only deployable payload controls the blocked/unblocked package state.
    # The review script under context/prerequisites may intentionally retain
    # explanatory placeholder examples.
    base = root / "payload"
    if not base.exists():
        return []

    findings: list[tuple[str, int, str]] = []
    for path in _placeholder_scan_files(base):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        executable_sql = _strip_sql_comments(text)
        relative_path = str(path.relative_to(root)).replace("\\", "/")
        for line_no, line in enumerate(executable_sql.splitlines(), start=1):
            for marker in _DBA_PLACEHOLDERS:
                if marker in line:
                    findings.append((relative_path, line_no, marker))
    return findings


def write_environment_prereq_context(
    package_dir: str,
    requirements: list[EnvironmentParentRequirement],
    *,
    release_group: str,
    package_filename: str,
    payload_paths: list[str] | None = None,
) -> None:
    """Write review script, manifest and requirements JSON into context/."""
    prereq_dir = Path(package_dir) / "context" / "prerequisites"
    prereq_dir.mkdir(parents=True, exist_ok=True)

    requirements_payload = {
        "requirement_type": "database_parent_dependency",
        "status": "requires_dba_review" if requirements else "not_required",
        "release_group": release_group,
        "package_filename": package_filename,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_script": "context/prerequisites/create_missing_parents.review.sql",
        "manifest": "context/prerequisites/parents.manifest.json",
        "dba_instructions": "context/prerequisites/DBA_INSTRUCTIONS.md",
        "deployable_payload": payload_paths or [],
        "dba_action_required": [
            "Review context/prerequisites/create_missing_parents.review.sql",
            "Extract the _00_environment_prereqs zip before editing",
            "Edit the generated payload .db/.usr files under the extracted package's payload/01_pre_requisites",
            "Replace <DBA_SELECTED_PARENT> and <DBA_REVIEWED_PERM>",
            "Read context/prerequisites/DBA_INSTRUCTIONS.md",
            "Run: python -m td_release_packager repackage --package-dir <path-to-extracted-_00_environment_prereqs-package-root> --strict",
        ],
        "missing_parents": [req.to_dict() for req in requirements],
        "execution_policy": {
            "auto_execute_allowed": False,
            "requires_human_approval": True,
            "requires_execution_evidence": True,
            "evidence_expected_at": "logs/prerequisite_execution_evidence.json",
        },
    }

    (prereq_dir / "database_parent_requirements.json").write_text(
        json.dumps(requirements_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    manifest_payload = {
        "requirement_type": "database_parent_dependency",
        "script": "context/prerequisites/create_missing_parents.review.sql",
        "script_is_review_only": True,
        "auto_execute_allowed": False,
        "requires_human_approval": True,
        "requires_execution_evidence": True,
        "script_sha256": None,
        "missing_parent_count": len(requirements),
        "missing_parents": [req.parent_name for req in requirements],
        "deployable_payload": payload_paths or [],
    }
    (prereq_dir / "parents.manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    script = _render_review_script(requirements, release_group=release_group)
    script_path = prereq_dir / "create_missing_parents.review.sql"
    script_path.write_text(script, encoding="utf-8")

    import hashlib

    manifest_payload["script_sha256"] = hashlib.sha256(
        script_path.read_bytes()
    ).hexdigest()
    (prereq_dir / "parents.manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    instructions = _render_dba_instructions(
        requirements,
        release_group=release_group,
        package_filename=package_filename,
        payload_paths=payload_paths or [],
    )
    (prereq_dir / "DBA_INSTRUCTIONS.md").write_text(
        instructions,
        encoding="utf-8",
    )


def _render_dba_instructions(
    requirements: list[EnvironmentParentRequirement],
    *,
    release_group: str,
    package_filename: str,
    payload_paths: list[str],
) -> str:
    """Render package-local DBA instructions for environment prereqs."""
    package_name = package_filename.rsplit(".", 1)[0]
    first_payload = (
        payload_paths[0]
        if payload_paths
        else "payload/01_pre_requisites/databases/<missing_parent>.db"
    )
    parent_names = ", ".join(req.parent_name for req in requirements) or "none"
    return f"""# DBA Instructions — Environment Prerequisite Package

This package is blocked because SHIPS detected missing environment parent databases/users required before the application prerequisite package can deploy.

Release group: `{release_group}`

Environment prerequisite package: `{package_name}`

Missing parent object(s): `{parent_names}`

Do **not** edit the project payload and do **not** edit the `_01_prereqs` package. The DBA-reviewed file to amend is inside the extracted `_00_environment_prereqs` package.

Primary file to amend:

`{first_payload}`

## Required DBA action

1. Extract the `_00_environment_prereqs` zip to a working directory.
2. Edit the generated `.db` / `.usr` payload file(s) inside the extracted package.
3. Replace `<DBA_SELECTED_PARENT>` and `<DBA_REVIEWED_PERM>` with approved values.
4. Repackage the edited package root using SHIPS.
5. Deploy the release group in order: `_00_environment_prereqs`, `_01_prereqs`, `_02_main`.

## Windows path-length note

On Windows, extraction may fail with a misleading `FileNotFoundError`, `The system cannot find the path specified`, or similar error if the release directory is too deeply nested. This is usually caused by the legacy Windows path-length limit rather than by a missing file in the zip.

If that happens, copy the whole release group folder to a short path such as `C:\\ships\\{release_group}` and use that shorter directory for the extract, edit, and repackage steps. Do not edit the zip directly.

## PowerShell

```powershell
# Prefer a short path on Windows to avoid legacy MAX_PATH failures during extraction.
# Example: copy the whole release group folder to C:\\ships\\{release_group} first.
$ReleaseGroup = "C:\\ships\\{release_group}"
$PackageName = "{package_name}"
$EnvZip = "$ReleaseGroup\\$PackageName.zip"
$PackageDir = "$ReleaseGroup\\$PackageName"

Expand-Archive -Path $EnvZip -DestinationPath "$ReleaseGroup\\.ships-work" -Force
$PackageDir = "$ReleaseGroup\\.ships-work\\$PackageName"

notepad "$PackageDir\\{first_payload.replace("/", "\\")}"

python -m td_release_packager repackage `
    --package-dir "$PackageDir" `
    --strict
```

## Bash / Git Bash / Linux shell

```bash
ReleaseGroup="/path/to/releases/{release_group}"
PackageName="{package_name}"
EnvZip="$ReleaseGroup/$PackageName.zip"
PackageDir="$ReleaseGroup/$PackageName"

unzip -o "$EnvZip" -d "$ReleaseGroup/.ships-work"
PackageDir="$ReleaseGroup/.ships-work/$PackageName"

${{EDITOR:-vi}} "$PackageDir/{first_payload}"

python -m td_release_packager repackage \
    --package-dir "$PackageDir" \
    --strict
```

## Example reviewed payload

Replace generated placeholder DDL like this:

```sql
create database GCFR_MAIN
from <DBA_SELECTED_PARENT>
as perm = <DBA_REVIEWED_PERM>
;
```

with DBA-approved values, for example:

```sql
create database GCFR_MAIN
from DBC
as perm = 50G
;
```

The parent database and PERM value above are examples only. The DBA must choose values appropriate for the target environment.

## Unblock rule

The package remains blocked while DBA placeholders remain in deployable payload.

The package can be unblocked only after:

- the generated payload file exists inside the extracted `_00_environment_prereqs` package;
- `<DBA_SELECTED_PARENT>` has been replaced;
- `<DBA_REVIEWED_PERM>` has been replaced;
- `python -m td_release_packager repackage --package-dir "<path-to-extracted-_00_environment_prereqs-package-root>" --strict` completes successfully;
- the regenerated `.zip` and `.sha256` sidecar are used for deployment.
"""


def _render_review_script(
    requirements: list[EnvironmentParentRequirement],
    *,
    release_group: str,
) -> str:
    """Render DBA-review SQL for missing external parent containers."""
    lines = [
        "-- SHIPS generated environment prerequisite review script",
        f"-- Release group: {release_group}",
        f"-- Generated at: {datetime.now(timezone.utc).isoformat()}",
        "--",
        "-- Purpose:",
        "--   Create or confirm parent databases/users required before the",
        "--   application prerequisite package can deploy.",
        "--",
        "-- Review required:",
        "--   YES. SHIPS cannot infer the platform parent or final PERM.",
        "--   Replace <DBA_SELECTED_PARENT> and <DBA_REVIEWED_PERM> before packaging.",
        "--",
        "-- Execution policy:",
        "--   Review-only. Do not auto-execute without DBA approval and evidence.",
        "",
    ]
    for req in requirements:
        lines.extend(
            [
                f"-- Missing parent: {req.parent_name}",
                "-- Required by:",
            ]
        )
        for dep in req.required_by:
            lines.append(
                f"--   {dep.child_type} {dep.child_name} "
                f"({dep.source_file}, PERM={_format_bytes(dep.declared_perm_bytes)})"
            )
        lines.extend(
            [
                f"-- Minimum required PERM: {_format_bytes(req.minimum_required_perm_bytes)}",
                f"-- Recommended PERM (+{req.recommended_buffer_percent}% buffer): "
                f"{_format_bytes(req.recommended_perm_bytes)}",
                f"create database {req.parent_name}",
                "from <DBA_SELECTED_PARENT>",
                f"as perm = {_perm_literal_for_requirement(req)}",
                ";",
                "",
            ]
        )
    return "\n".join(lines)
