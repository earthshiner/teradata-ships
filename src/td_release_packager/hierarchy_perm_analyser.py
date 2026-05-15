"""Static database hierarchy PERM capacity analysis for SHIPS.

This module validates Teradata CREATE DATABASE / CREATE USER hierarchy
scripts without requiring a live database connection.  It checks each
package-created parent container against the sum of its immediate child
PERM allocations.  Grandchildren are deliberately not counted against
higher ancestors, because in Teradata space is delegated one parent/child
edge at a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from td_release_packager.sql_text import strip_comments_and_string_literals


_IDENTIFIER = r'(?:"[^"]+"|\{\{[A-Za-z0-9_]+\}\}|[A-Za-z0-9_$]+)'
_CREATE_CONTAINER_RE = re.compile(
    rf"\bcreate\s+(database|user)\s+(?P<child>{_IDENTIFIER})\s+"
    rf"from\s+(?P<parent>{_IDENTIFIER})(?P<body>.*?);",
    re.IGNORECASE | re.DOTALL,
)
_PERM_RE = re.compile(
    r"\bperm(?:anent)?\s*=\s*"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s*"
    r"(?P<suffix>[KMGT]?)\b",
    re.IGNORECASE,
)

CONTAINER_EXTENSIONS = {".db", ".usr"}
KNOWN_ROOTS = {"DBC"}


@dataclass
class ContainerDeclaration:
    """A CREATE DATABASE / CREATE USER declaration found in payload."""

    name: str
    object_type: str
    parent_name: str
    declared_perm_bytes: Optional[int]
    source_file: str

    @property
    def key(self) -> str:
        """Case-insensitive lookup key for the container name."""

        return _normalise_key(self.name)

    @property
    def parent_key(self) -> str:
        """Case-insensitive lookup key for the parent name."""

        return _normalise_key(self.parent_name)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""

        return {
            "name": self.name,
            "object_type": self.object_type,
            "parent_name": self.parent_name,
            "declared_perm_bytes": self.declared_perm_bytes,
            "declared_perm": _format_bytes(self.declared_perm_bytes),
            "source_file": self.source_file,
        }


@dataclass
class HierarchyCapacityFinding:
    """Capacity result for one parent and its immediate children."""

    parent_name: str
    parent_source_file: Optional[str]
    declared_perm_bytes: Optional[int]
    direct_child_perm_bytes: int
    headroom_bytes: Optional[int]
    status: str
    message: str
    children: List[ContainerDeclaration] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True when this finding is not an error."""

        return self.status != "INSUFFICIENT"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""

        return {
            "parent_name": self.parent_name,
            "parent_source_file": self.parent_source_file,
            "declared_perm_bytes": self.declared_perm_bytes,
            "declared_perm": _format_bytes(self.declared_perm_bytes),
            "direct_child_perm_bytes": self.direct_child_perm_bytes,
            "direct_child_perm": _format_bytes(self.direct_child_perm_bytes),
            "headroom_bytes": self.headroom_bytes,
            "headroom": _format_bytes(self.headroom_bytes),
            "status": self.status,
            "message": self.message,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class HierarchyPermAnalysisResult:
    """Result of static hierarchy PERM analysis."""

    declarations: List[ContainerDeclaration] = field(default_factory=list)
    findings: List[HierarchyCapacityFinding] = field(default_factory=list)
    external_parents: List[HierarchyCapacityFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True when no package-created parent is over-allocated."""

        return all(finding.passed for finding in self.findings)

    @property
    def errors(self) -> int:
        """Return number of insufficient parent findings."""

        return sum(1 for finding in self.findings if not finding.passed)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""

        return {
            "passed": self.passed,
            "errors": self.errors,
            "declarations": [decl.to_dict() for decl in self.declarations],
            "findings": [finding.to_dict() for finding in self.findings],
            "external_parents": [
                finding.to_dict() for finding in self.external_parents
            ],
        }


def analyse_hierarchy_perm_capacity(payload_dir: str) -> HierarchyPermAnalysisResult:
    """Analyse database/user hierarchy PERM capacity from SHIPS payload.

    Args:
        payload_dir: Path to a SHIPS payload directory or project root that
            contains a payload directory.

    Returns:
        HierarchyPermAnalysisResult with per-parent capacity findings.
    """

    root = _resolve_payload_dir(Path(payload_dir))
    declarations = list(_collect_container_declarations(root))
    declarations_by_key: Dict[str, ContainerDeclaration] = {
        declaration.key: declaration for declaration in declarations
    }
    children_by_parent: Dict[str, List[ContainerDeclaration]] = {}

    for declaration in declarations:
        children_by_parent.setdefault(declaration.parent_key, []).append(declaration)

    findings: List[HierarchyCapacityFinding] = []
    external_parents: List[HierarchyCapacityFinding] = []

    for parent_key, children in sorted(
        children_by_parent.items(),
        key=lambda item: _display_parent_name(item[0], item[1]),
    ):
        if parent_key in {_normalise_key(root_name) for root_name in KNOWN_ROOTS}:
            continue

        parent_decl = declarations_by_key.get(parent_key)
        direct_child_perm = sum(child.declared_perm_bytes or 0 for child in children)

        if parent_decl is None:
            parent_name = _display_parent_name(parent_key, children)
            external_parents.append(
                HierarchyCapacityFinding(
                    parent_name=parent_name,
                    parent_source_file=None,
                    declared_perm_bytes=None,
                    direct_child_perm_bytes=direct_child_perm,
                    headroom_bytes=None,
                    status="EXTERNAL",
                    message=(
                        f"Parent '{parent_name}' is external to this package; "
                        f"direct package children require {_format_bytes(direct_child_perm)}."
                    ),
                    children=children,
                )
            )
            continue

        if parent_decl.declared_perm_bytes is None:
            findings.append(
                HierarchyCapacityFinding(
                    parent_name=parent_decl.name,
                    parent_source_file=parent_decl.source_file,
                    declared_perm_bytes=None,
                    direct_child_perm_bytes=direct_child_perm,
                    headroom_bytes=None,
                    status="UNKNOWN",
                    message=(
                        f"Parent '{parent_decl.name}' has no declared PERM; "
                        f"direct children require {_format_bytes(direct_child_perm)}."
                    ),
                    children=children,
                )
            )
            continue

        headroom = parent_decl.declared_perm_bytes - direct_child_perm
        status = "OK" if headroom >= 0 else "INSUFFICIENT"
        message = (
            f"Parent '{parent_decl.name}' declares "
            f"{_format_bytes(parent_decl.declared_perm_bytes)}; direct children require "
            f"{_format_bytes(direct_child_perm)}; headroom {_format_bytes(headroom)}."
        )
        findings.append(
            HierarchyCapacityFinding(
                parent_name=parent_decl.name,
                parent_source_file=parent_decl.source_file,
                declared_perm_bytes=parent_decl.declared_perm_bytes,
                direct_child_perm_bytes=direct_child_perm,
                headroom_bytes=headroom,
                status=status,
                message=message,
                children=children,
            )
        )

    return HierarchyPermAnalysisResult(
        declarations=declarations,
        findings=findings,
        external_parents=external_parents,
    )


def format_hierarchy_perm_report(result: HierarchyPermAnalysisResult) -> str:
    """Format a human-readable hierarchy capacity report."""

    lines: List[str] = []
    if not result.findings and not result.external_parents:
        return "  No CREATE DATABASE/USER hierarchy PERM declarations found."

    if result.findings:
        lines.append("  Package-created parent capacity:")
        for finding in result.findings:
            icon = "✓" if finding.passed else "✗"
            lines.append(
                "    "
                f"{icon} {finding.parent_name}: "
                f"declared {_format_bytes(finding.declared_perm_bytes)}, "
                f"direct children {_format_bytes(finding.direct_child_perm_bytes)}, "
                f"headroom {_format_bytes(finding.headroom_bytes)} "
                f"[{finding.status}]"
            )
            if not finding.passed:
                for child in finding.children:
                    lines.append(
                        "        - "
                        f"{child.object_type} {child.name}: "
                        f"{_format_bytes(child.declared_perm_bytes)} "
                        f"({child.source_file})"
                    )

    if result.external_parents:
        if lines:
            lines.append("")
        lines.append("  External parent requirements:")
        for finding in result.external_parents:
            lines.append(
                "    ℹ "
                f"{finding.parent_name}: direct package children require "
                f"{_format_bytes(finding.direct_child_perm_bytes)}"
            )

    return "\n".join(lines)


def _collect_container_declarations(root: Path) -> Iterable[ContainerDeclaration]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CONTAINER_EXTENSIONS:
            continue
        try:
            raw_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stripped_text = strip_comments_and_string_literals(raw_text)
        relative_path = path.relative_to(root).as_posix()
        for match in _CREATE_CONTAINER_RE.finditer(stripped_text):
            object_type = match.group(1).upper()
            name = _normalise_identifier(match.group("child"))
            parent_name = _normalise_identifier(match.group("parent"))
            perm_bytes = _extract_perm_bytes(match.group(0))
            yield ContainerDeclaration(
                name=name,
                object_type=object_type,
                parent_name=parent_name,
                declared_perm_bytes=perm_bytes,
                source_file=relative_path,
            )


def _extract_perm_bytes(statement: str) -> Optional[int]:
    match = _PERM_RE.search(statement)
    if not match:
        return None
    return _parse_perm_bytes(match.group("value"), match.group("suffix") or "")


def _parse_perm_bytes(value: str, suffix: str = "") -> int:
    number = float(value)
    multipliers = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }
    return int(number * multipliers.get(suffix.upper(), 1))


def _normalise_identifier(identifier: str) -> str:
    value = identifier.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _normalise_key(identifier: str) -> str:
    return _normalise_identifier(identifier).upper()


def _display_parent_name(parent_key: str, children: List[ContainerDeclaration]) -> str:
    for child in children:
        if child.parent_key == parent_key:
            return child.parent_name
    return parent_key


def _resolve_payload_dir(path: Path) -> Path:
    candidate = path / "payload"
    if candidate.exists() and candidate.is_dir():
        return candidate
    return path


def _format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "UNKNOWN"
    sign = "-" if value < 0 else ""
    size = abs(float(value))
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{sign}{int(size)} {units[unit]}"
    return f"{sign}{size:.1f} {units[unit]}"
