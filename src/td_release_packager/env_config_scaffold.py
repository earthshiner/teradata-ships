"""
env_config_scaffold.py — Renders the canonical 7-section
``.conf`` template used by SHIPS env files.

The bootstrap tools (``import_legacy``, ``decomposer``) emit
``.conf`` files that downstream stages consume. Both tools
now write the full canonical structure:

    1. Composition roots
    2. Derived database names
    3. Users & roles
    4. SQL constants
    5. Engine / runtime flags
    6. Field-length policy
    7. Diagnostic / DI-tool stanzas

Sections each tool can't pre-populate are emitted as empty
placeholders with guidance comments. A final section 8
(``Imported`` for ``import_legacy``, ``Outliers`` for
``decomposer``) carries any content that hasn't been categorised
yet — the user moves entries from section 8 into the appropriate
sections above and deletes section 8 when empty.

Why this lives in its own module:
  - Both bootstrap tools render the same structure; centralising
    the section metadata avoids drift.
  - Future tools (e.g. interactive editor, web UI) will consume
    the same ``SECTIONS`` definition.
  - Tests pin the canonical structure here, so renaming a section
    is a deliberate change with one place to update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------
# Section metadata
# ---------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One section of the canonical .conf scaffold.

    Attributes:
        number:      1-based section index.
        title:       Short title (e.g. "Composition roots").
        description: Optional parenthetical (e.g. "(env-specific)").
        purpose:     Comment lines describing what belongs here.
        empty_hint:  Comment shown when the section has no entries.
    """

    number: int
    title: str
    description: str = ""
    purpose: List[str] = field(default_factory=list)
    empty_hint: str = (
        "(no entries — populate by moving relevant tokens from "
        "the Imported section below.)"
    )


#: The seven canonical sections, in render order.
#: Mirrors the structure of ``config/env/DEV.conf``.
SECTIONS: List[Section] = [
    Section(
        number=1,
        title="Composition roots",
        description="(the only env-specific section)",
        purpose=[
            "The roots that DEFINE the environment. Edit these when",
            "promoting between DEV / TST / PRD. SHIPS_ENV is",
            "cross-checked against --env at package time —",
            "mismatch = build fail.",
            "",
            "Required tokens:",
            "  SHIPS_ENV       Logical environment (DEV/TST/PRD)",
            "  ENV_PREFIX      Physical Teradata prefix (per topology)",
            "  SHIPS_PROJECT   Project identifier",
            "  INSTANCE        Parallel deployment instance (00 = primary)",
            "  SECURITY_TIER   Data classification (0 = generally accessible)",
        ],
    ),
    Section(
        number=2,
        title="Derived database names",
        description="(do NOT edit per-env)",
        purpose=[
            "Database names composed from section 1 via {{TOKEN}}",
            "cascade. Promotion changes section 1; this section",
            "follows automatically.",
            "",
            "PARENT_NODE={{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}} is",
            "the foundation; most leaf tokens reference it.",
        ],
    ),
    Section(
        number=3,
        title="Users & roles",
        purpose=[
            "Service users, role grants, and similar identifiers.",
            "Some derive from {{PARENT_NODE}}; admin / external",
            "users typically don't.",
        ],
    ),
    Section(
        number=4,
        title="SQL constants",
        description="(env-agnostic; override per env only if required)",
        purpose=[
            "Date formats, type literals, character constants. These",
            "are the same across DEV/TST/PRD by default — only",
            "override per env if a specific environment needs it.",
        ],
    ),
    Section(
        number=5,
        title="Engine / runtime flags",
        description="(legitimately differ per env)",
        purpose=[
            "Boolean-ish toggles for engine-level features",
            "(e.g. JAVA_XSP_SUPPORTED, security privilege options).",
            "Often YES/NO or empty.",
        ],
    ),
    Section(
        number=6,
        title="Field-length policy",
        description="(governance — change with care)",
        purpose=[
            "Maximum lengths for filename and identifier conventions.",
            "Changing these can cascade into existing DDL — coordinate",
            "with downstream consumers before promoting.",
        ],
    ),
    Section(
        number=7,
        title="Diagnostic / DI-tool stanzas",
        purpose=[
            "Workarounds and diagnostic snippets injected by ETL",
            "tools. Often comments-as-tokens for DataStage transaction",
            "control or similar tool-specific bookkeeping.",
        ],
    ),
]


# ---------------------------------------------------------------
# Grammar header (shared)
# ---------------------------------------------------------------


GRAMMAR_HEADER_LINES: List[str] = [
    "Naming grammar:",
    "",
    "  {{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}_{{LAYER}}_{{SECURITY_TIER}}_{{KIND}}",
    "        PDE          DEV           00           MDL           0             T",
    "  | environment composite |  | classified leaf                              |",
    "",
    "  ENV_PREFIX     Physical Teradata prefix (per topology)",
    "  SHIPS_ENV      Logical environment (DEV/TST/PRD; matches --env)",
    "  INSTANCE       Parallel deployment instance (00 = primary)",
    "  LAYER          Architectural layer (MDL/STG/SEM/OPR/...)",
    "  SECURITY_TIER  Data classification (0 = generally accessible)",
    "  KIND           Object kind (T=table, V=view, M=macro, P=procedure)",
]


# ---------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------


_SECTION_BAR = "# " + "-" * 67


def _comment_block(lines: List[str]) -> List[str]:
    """Convert a list of plain text lines into '# '-prefixed comments."""
    return [f"# {ln}" if ln else "#" for ln in lines]


def _render_section_header(section: Section) -> List[str]:
    """Render the divider + title + purpose block for one section."""
    title_line = f"# {section.number}. {section.title}"
    if section.description:
        title_line = f"{title_line}  {section.description}"
    out = [_SECTION_BAR, title_line, _SECTION_BAR]
    if section.purpose:
        out.extend(_comment_block(section.purpose))
    return out


def render_scaffold(
    *,
    env: str,
    generator_label: str,
    source_label: str,
    next_steps: List[str],
    sections_content: Dict[int, str],
    final_section_title: Optional[str] = None,
    final_section_purpose: Optional[List[str]] = None,
    final_section_content: Optional[str] = None,
) -> str:
    """
    Render the canonical 7-section ``.conf`` scaffold.

    Args:
        env:                   Target env name (DEV / TST / PRD).
        generator_label:       Tool name that produced the file
                               (e.g. ``"import_legacy_substitutions.py"``).
        source_label:          Short description of the input, e.g.
                               ``"sed substitution script legacy.sh"``.
        next_steps:            Bullet text for the file header. Each
                               string is one line; empty strings render
                               as blank comment lines.
        sections_content:      Map of section number → already-rendered
                               content lines (with no trailing newline).
                               Sections not in the map are emitted as
                               empty placeholders.
        final_section_title:   Title for an optional 8th section
                               (e.g. ``"Imported (UNCATEGORISED)"`` or
                               ``"Outliers"``). Omit to skip.
        final_section_purpose: Comment lines describing why this section
                               exists and what to do with its contents.
        final_section_content: Body of the final section.

    Returns:
        Full ``.conf`` text, terminated with a newline.
    """
    lines: List[str] = []

    # --- Top banner ---
    lines.append("# " + "=" * 67)
    lines.append(f"# {env}.conf — generated by {generator_label}")
    lines.append("#")
    lines.append(f"# Source: {source_label}")
    lines.append("#")
    if next_steps:
        lines.append("# Next steps:")
        lines.append("#")
        for step in next_steps:
            lines.append(f"# {step}" if step else "#")
        lines.append("#")
    lines.extend(_comment_block(GRAMMAR_HEADER_LINES))
    lines.append("# " + "=" * 67)
    lines.append("")

    # --- Sections 1-7 ---
    for section in SECTIONS:
        lines.append("")
        lines.extend(_render_section_header(section))
        body = sections_content.get(section.number, "").strip()
        if body:
            lines.append(body)
        else:
            lines.append(f"# {section.empty_hint}")

    # --- Optional final section ---
    if final_section_title is not None:
        final = Section(
            number=8,
            title=final_section_title,
            purpose=final_section_purpose or [],
        )
        lines.append("")
        lines.extend(_render_section_header(final))
        if final_section_content:
            lines.append(final_section_content.strip())

    return "\n".join(lines) + "\n"
