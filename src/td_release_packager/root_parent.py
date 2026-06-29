"""Root parent injection for parentless database/user prerequisites."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


# ``{{TOKEN}}\w*`` allows tokenised database names with a literal suffix
# (e.g. ``{{DB_PREFIX}}_Domain``) to match in full instead of truncating
# after the closing braces (#454).
CREATE_PREREQ_HEADER_RE = re.compile(
    r"\bCREATE\s+(?:DATABASE|USER)\s+"
    r"(?:\"[^\"]+\"|\{\{[A-Za-z_][A-Za-z0-9_]*\}\}\w*|[A-Za-z_][A-Za-z0-9_$#]*)",
    re.IGNORECASE,
)
PREREQ_FROM_RE = re.compile(r"\bFROM\b", re.IGNORECASE)


def normalise_root_parent(root_parent: str | None) -> str | None:
    """Return a stripped root parent value or raise for blank input."""
    if root_parent is None:
        return None
    value = root_parent.strip()
    if not value:
        raise ValueError("[RootParentEmpty] --root-parent cannot be blank.")
    return value


def inject_root_parent(
    project_dir: Path,
    root_parent: str | None,
    *,
    parent_expression: str | None = None,
) -> int:
    """Inject an explicit parent into parentless prereq DDL files.

    Args:
        project_dir: SHIPS project root containing ``payload/database``.
        root_parent: CLI/configured parent value. A blank value is invalid.
        parent_expression: Optional SQL expression to inject instead of the
            literal root parent. Demo mode uses this to inject ``{{ROOT_PARENT}}``
            while resolving the token in its generated env config.

    Returns:
        Number of files changed.
    """
    root_parent_value = normalise_root_parent(root_parent)
    if root_parent_value is None:
        return 0

    injected_parent = parent_expression or root_parent_value
    prereq_dir = project_dir / "payload" / "database" / "pre-requisites"
    if not prereq_dir.is_dir():
        return 0

    injections = 0
    for path in _iter_prereq_files(prereq_dir):
        content = path.read_text(encoding="utf-8")
        updated = inject_root_parent_in_content(content, injected_parent)
        if updated != content:
            path.write_text(updated, encoding="utf-8")
            injections += 1
    return injections


def inject_root_parent_in_content(content: str, parent_expression: str) -> str:
    """Inject ``FROM parent_expression`` into one parentless prereq statement."""
    match = CREATE_PREREQ_HEADER_RE.search(content)
    if not match:
        return content

    statement_end = content.find(";", match.end())
    if statement_end == -1:
        statement_end = len(content)
    statement_tail = content[match.end() : statement_end]
    if PREREQ_FROM_RE.search(statement_tail):
        return content

    return (
        content[: match.end()] + f" FROM {parent_expression}" + content[match.end() :]
    )


def _iter_prereq_files(prereq_dir: Path) -> Iterable[Path]:
    for path in sorted(prereq_dir.rglob("*")):
        if path.suffix.lower() in {".db", ".usr"}:
            yield path
