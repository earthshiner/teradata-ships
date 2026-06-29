"""
waves.py — Shared object/wave visualisation for SHIPS reports.

The deployment-wave SVG and the object-type colour system are needed by
both the package report (post-build) and the pipeline report's Payload tab
(pre-build). This module is the single home for that visualisation so the
two reports render objects and waves identically.

All functions operate on the neutral "record" shape produced by a payload
scan — a dict with at least ``name`` / ``type`` / ``phase`` / ``wave`` —
so the module is agnostic to whether the records came from a built package
or a pre-package project tree.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

from td_release_packager.reporting.common import (
    BORDER,
    NAVY,
    ORANGE,
    WHITE,
    h,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension → object type mapping
# ---------------------------------------------------------------------------

EXT_TYPE: Dict[str, str] = {
    ".tbl": "TABLE",
    ".viw": "VIEW",
    ".mcr": "MACRO",
    ".spl": "PROCEDURE",
    ".fnc": "FUNCTION",
    ".trg": "TRIGGER",
    ".jix": "JOIN INDEX",
    ".idx": "INDEX",
    ".sjr": "JAR",
    ".sto": "STO",
    ".dcl": "GRANT",
    ".grt": "GRANT",
    ".db": "DATABASE",
    ".usr": "USER",
    ".rol": "ROLE",
    ".prf": "PROFILE",
    ".auth": "AUTHORISATION",
    ".fsvr": "FOREIGN SERVER",
    ".map": "MAP",
    ".dml": "DML",
    ".osql": "ORDERED SQL",
    ".sql": "SQL",
    ".ddl": "DDL",
    ".c": "C SOURCE",
    ".cpp": "CPP SOURCE",
    ".cc": "CPP SOURCE",
    ".cxx": "CPP SOURCE",
    ".h": "C HEADER",
    ".hpp": "C HEADER",
    ".hh": "C HEADER",
    ".bteq": "BTEQ",
    ".btq": "BTQ",
    ".cmt": "COMMENT",
    ".stt": "STATISTICS",
    ".fk": "FOREIGN KEY",
}

# Type → badge colour (background, text)
TYPE_COLOURS: Dict[str, Tuple[str, str]] = {
    "TABLE": ("#0D6EFD", "#fff"),
    "VIEW": ("#6610F2", "#fff"),
    "PROCEDURE": ("#198754", "#fff"),
    "FUNCTION": ("#20C997", "#fff"),
    "MACRO": ("#0DCAF0", "#000"),
    "TRIGGER": ("#FFC107", "#000"),
    "JOIN INDEX": ("#6C757D", "#fff"),
    "INDEX": ("#ADB5BD", "#000"),
    "JAR": ("#D63384", "#fff"),
    "DATABASE": ("#FF5F02", "#fff"),
    "USER": ("#FF5F02", "#fff"),
    "GRANT": ("#FD7E14", "#000"),
    "DML": ("#6F42C1", "#fff"),
    "C SOURCE": ("#6C757D", "#fff"),
    "CPP SOURCE": ("#6C757D", "#fff"),
    "C HEADER": ("#6C757D", "#fff"),
}
TYPE_COLOUR_DEFAULT = ("#6C757D", "#fff")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_waves_txt(waves_path: str) -> Dict[str, int]:
    """Parse a ``_waves.txt`` file into a path-aware wave mapping (1-based).

    Lines are object paths; a ``---`` line separates one wave from the next.
    Both the full normalised path and the bare basename are recorded as keys
    so a record can be matched by either — except when a basename is
    ambiguous across waves, in which case the basename alias is dropped.

    Args:
        waves_path: Path to a ``_waves.txt`` file.

    Returns:
        Mapping of object key → wave number. Empty when the file is absent
        or unreadable.
    """
    result: Dict[str, int] = {}
    if not os.path.isfile(waves_path):
        return result
    wave_num = 1
    basenames: Dict[str, Optional[int]] = {}
    try:
        with open(waves_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "---":
                    wave_num += 1
                else:
                    # Keep the full relative path so duplicate filenames in
                    # different folders cannot borrow each other's wave labels.
                    norm = line.replace("\\", "/").lstrip("./")
                    result[norm] = wave_num

                    # Backward compatibility: also index by basename, but drop
                    # the alias if the same basename appears in two waves.
                    basename = os.path.basename(norm)
                    previous = basenames.get(basename)
                    if previous is None and basename not in basenames:
                        basenames[basename] = wave_num
                        result[basename] = wave_num
                    elif previous != wave_num:
                        basenames[basename] = None
                        result.pop(basename, None)
    except OSError as exc:
        logger.debug("waves: could not read %s: %s", waves_path, exc)
    return result


def type_badge(obj_type: str) -> str:
    """Return a coloured object-type badge ``<span>``."""
    bg, fg = TYPE_COLOURS.get(obj_type, TYPE_COLOUR_DEFAULT)
    return (
        f'<span style="background:{bg};color:{fg};'
        f"padding:2px 7px;border-radius:3px;font-size:11px;"
        f'font-weight:600;letter-spacing:.3px">{obj_type}</span>'
    )


def truncate(value: object, max_len: int = 28) -> str:
    """Return a display-safe truncated value with an ellipsis."""
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def group_by_wave(records: List[Dict]) -> Dict[Optional[int], List[Dict]]:
    """Group records by wave number. ``None`` = serial pre-wave work."""
    groups: Dict[Optional[int], List[Dict]] = {}
    for rec in records:
        key = rec["wave"]
        groups.setdefault(key, []).append(rec)
    return groups


def render_wave_svg(records: List[Dict]) -> str:
    """Render the tiered SVG wave-plan visualisation for a set of records.

    Serial (no-wave) objects are shown first as a "Serial" column, followed
    by one column per numbered wave. Objects in the same wave have no mutual
    dependencies and deploy in parallel.

    Args:
        records: Payload records with ``name`` / ``type`` / ``wave`` keys.

    Returns:
        HTML string — the SVG plus a type legend, or a friendly note when
        no wave data is available.
    """
    wave_groups = group_by_wave(records)
    wave_nums = sorted(k for k in wave_groups if k is not None)
    has_serial = None in wave_groups

    if not wave_nums and not has_serial:
        return (
            '<p style="color:#6C757D;padding:32px;text-align:center">No wave data '
            "available — run <code>ships analyse</code> before packaging.</p>"
        )

    # Build columns: serial (prereqs) first if present, then waves
    columns = []
    if has_serial:
        columns.append(("Serial", wave_groups[None]))
    for wn in wave_nums:
        columns.append((f"Wave {wn}", wave_groups[wn]))

    # Layout constants. Cell width widened (was 160) so tokenised
    # qualified names like ``{{DB_PREFIX}}_DOM_STD_T.customer_keymap``
    # display intact instead of truncating to ``{{DB_PREFIX}}_DOM_S…``.
    cell_h = 22
    cell_w = 260
    gap = 28  # arrow gap between columns
    col_pad = 8  # padding inside column header
    header_h = 30
    margin = 16
    arrow_w = gap
    item_cap = 40  # max items per column before "... N more" footer

    # Each column sizes to its own content rather than the tallest
    # column — the prior "max_items everywhere" layout left lots of
    # whitespace under shorter waves.
    def _column_height(items: List[Dict]) -> int:
        n = min(len(items), item_cap)
        if len(items) > item_cap:
            n += 1  # "... N more" footer takes one cell row
        return n * cell_h + header_h + col_pad * 2

    col_heights = [_column_height(items) for _, items in columns]
    svg_w = len(columns) * (cell_w + gap) - gap + margin * 2
    svg_h = max(col_heights) + margin * 2

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'style="font-family:Inter,-apple-system,sans-serif;display:block;margin:0 auto">'
    ]

    for ci, (label, items) in enumerate(columns):
        x = margin + ci * (cell_w + gap)
        y = margin
        col_h = col_heights[ci]

        # Column background
        svg_parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_w}" height="{col_h}" '
            f'rx="6" fill="#f0f4f8" stroke="{BORDER}" stroke-width="1"/>'
        )
        # Column header
        svg_parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_w}" height="{header_h}" '
            f'rx="6" fill="{NAVY}"/>'
        )
        svg_parts.append(
            f'<rect x="{x}" y="{y + header_h - 6}" width="{cell_w}" height="6" fill="{NAVY}"/>'
        )
        svg_parts.append(
            f'<text x="{x + cell_w // 2}" y="{y + 19}" text-anchor="middle" '
            f'font-size="12" font-weight="600" fill="{WHITE}">{label}</text>'
        )

        # Items — cap at item_cap per wave for readability; full names
        # remain on the SVG <title> tooltip for the truncated ones.
        for ii, item in enumerate(items[:item_cap]):
            iy = y + header_h + col_pad + ii * cell_h
            bg, fg = TYPE_COLOURS.get(item["type"], TYPE_COLOUR_DEFAULT)
            # type dot
            svg_parts.append(
                f'<circle cx="{x + 12}" cy="{iy + 11}" r="4" fill="{bg}"/>'
            )
            # object name. Truncate to roughly fit the wider cell; the
            # SVG <title> still exposes the full name on hover.
            full_name = item["name"]
            display_name = truncate(full_name, 36)
            svg_parts.append(
                f'<text x="{x + 22}" y="{iy + 15}" font-size="11" fill="#333">'
                f"<title>{h(full_name)}</title>{h(display_name)}</text>"
            )

        if len(items) > item_cap:
            iy = y + header_h + col_pad + item_cap * cell_h
            svg_parts.append(
                f'<text x="{x + cell_w // 2}" y="{iy + 11}" text-anchor="middle" '
                f'font-size="11" fill="#6C757D">… {len(items) - item_cap} more</text>'
            )

        # Arrow to next column — anchored at the column-header midline
        # so arrows still align across columns of different heights.
        if ci < len(columns) - 1:
            ax_start = x + cell_w + 5
            ax_end = x + cell_w + arrow_w - 5
            ay = margin + header_h // 2
            svg_parts.append(
                f'<line x1="{ax_start}" y1="{ay}" x2="{ax_end}" y2="{ay}" '
                f'stroke="{ORANGE}" stroke-width="1.5" stroke-linecap="round" '
                f'marker-end="url(#arr)"/>'
            )

    # Open-chevron arrowhead — matches the SHIPS report design language.
    svg_parts.insert(
        1,
        "<defs>"
        '<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="10" markerHeight="10" orient="auto-start-reverse" '
        'markerUnits="userSpaceOnUse">'
        '<path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        "</marker>"
        "</defs>",
    )

    svg_parts.append("</svg>")

    # Type legend — pinned at the top of the visualisation so the
    # reader knows what each colour dot means before scanning the
    # waves, rather than scrolling past the SVG to find it.
    legend_items = sorted({r["type"] for r in records})
    legend_parts = []
    for t in legend_items[:12]:
        bg, fg = TYPE_COLOURS.get(t, TYPE_COLOUR_DEFAULT)
        legend_parts.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">'
            f'<span style="width:10px;height:10px;border-radius:50%;background:{bg};display:inline-block"></span>'
            f'<span style="font-size:12px;color:#555">{t}</span></span>'
        )

    legend_html = (
        '<div style="display:flex;flex-wrap:wrap;align-items:center;'
        "padding:8px 12px;margin-bottom:12px;background:#F8F9FA;"
        'border:1px solid #DEE2E6;border-radius:6px">'
        + "".join(legend_parts)
        + "</div>"
    )

    return (
        legend_html
        + '<div style="overflow-x:auto;padding:8px 0">\n'
        + "\n".join(svg_parts)
        + "\n</div>"
    )
