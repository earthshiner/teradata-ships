"""
package_report.py — Interactive HTML package report generator.

Produces a self-contained, interactive HTML file embedded inside every
SHIPS package at build time.  The report gives developers and DBAs a
visual overview of the package contents before deployment:

    Objects tab  — filterable table of every DDL object with type badge,
                   phase, wave assignment, and source filename.

    Waves tab    — tiered SVG visualisation of the deployment wave plan:
                   each column is a wave; each cell is an object.  Objects
                   in the same wave have no mutual dependencies and deploy
                   in parallel.

    Trust tab    — per-signal breakdown of the Package Trust Report
                   (READY / READY-WITH-CAVEATS / BLOCKED).

    Deploy tab   — pre-filled deploy commands with one-click clipboard
                   copy, covering dry-run, standard, and wave-parallel
                   deployment modes.

The report is purely static HTML — no server, no build step, no external
network requests.  It opens directly from the filesystem (file: URL).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension → object type mapping
# ---------------------------------------------------------------------------

_EXT_TYPE: Dict[str, str] = {
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
    ".db": "DATABASE",
    ".usr": "USER",
    ".rol": "ROLE",
    ".prf": "PROFILE",
    ".auth": "AUTHORISATION",
    ".fsvr": "FOREIGN SERVER",
    ".map": "MAP",
    ".dml": "DML",
    ".sql": "SQL",
    ".ddl": "DDL",
    ".bteq": "BTEQ",
    ".btq": "BTQ",
    ".cmt": "COMMENT",
    ".stt": "STATISTICS",
}

# Type → badge colour (bg, text)
_TYPE_COLOURS: Dict[str, Tuple[str, str]] = {
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
}
_TYPE_COLOUR_DEFAULT = ("#6C757D", "#fff")

# Teradata brand
_NAVY = "#00233C"
_ORANGE = "#FF5F02"
_WHITE = "#FFFFFF"
_LIGHT = "#F8F9FA"
_BORDER = "#DEE2E6"

# Control/hidden files to skip when scanning the payload
_SKIP_NAMES = frozenset(
    {
        "_waves.txt",
        "_order.txt",
        ".gitkeep",
        ".gitignore",
        "package_integrity.json",
        "_provenance.json",
        "BUILD.json",
    }
)
_SKIP_EXTS = frozenset({".json", ".py", ".sh", ".bat", ".txt", ".html", ".jar"})


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def _phase_label(phase_dir: str) -> str:
    """Return a human-readable phase label from a payload subdirectory name."""
    labels = {
        "00_system": "System",
        "01_pre_requisites": "Pre-requisites",
        "02_dcl": "DCL",
        "03_ddl": "DDL",
        "04_dml": "DML",
        "05_post_install": "Post-install",
        # Legacy names
        "system": "System",
        "pre-requisites": "Pre-requisites",
        "DCL": "DCL",
        "DDL": "DDL",
        "DML": "DML",
    }
    return labels.get(os.path.basename(phase_dir), os.path.basename(phase_dir))


def _parse_waves_txt(waves_path: str) -> Dict[str, int]:
    """Parse a _waves.txt file into {filename: wave_number} mapping (1-based)."""
    result: Dict[str, int] = {}
    if not os.path.isfile(waves_path):
        return result
    wave_num = 1
    try:
        with open(waves_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "---":
                    wave_num += 1
                else:
                    # Entries are relative paths like "tables/DB.Object.tbl"
                    result[os.path.basename(line)] = wave_num
    except OSError as exc:
        logger.debug("package_report: could not read %s: %s", waves_path, exc)
    return result


def _scan_payload(pkg_dir: str) -> List[Dict]:
    """Walk the payload directory and return one record per DDL object."""
    payload_dir = os.path.join(pkg_dir, "payload")
    if not os.path.isdir(payload_dir):
        return []

    # Collect _waves.txt assignments per phase
    phase_waves: Dict[str, Dict[str, int]] = {}

    records: List[Dict] = []
    for root, dirs, files in os.walk(payload_dir):
        dirs.sort()
        # Determine phase (first level below payload/)
        rel_root = os.path.relpath(root, payload_dir)
        parts = rel_root.replace("\\", "/").split("/")
        phase_dir = parts[0] if parts[0] != "." else ""

        # Load _waves.txt for this phase once
        if phase_dir and phase_dir not in phase_waves:
            waves_path = os.path.join(payload_dir, phase_dir, "_waves.txt")
            phase_waves[phase_dir] = _parse_waves_txt(waves_path)

        wave_map = phase_waves.get(phase_dir, {})

        for fname in sorted(files):
            if fname in _SKIP_NAMES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SKIP_EXTS or not ext:
                continue

            obj_type = _EXT_TYPE.get(ext, "UNKNOWN")
            # Derive DB.Object from filename (strip extension)
            stem = os.path.splitext(fname)[0]  # e.g. "OMR_STD.Customer"
            wave = wave_map.get(fname)

            records.append(
                {
                    "name": stem,
                    "type": obj_type,
                    "phase": _phase_label(os.path.join(payload_dir, phase_dir)),
                    "wave": wave,
                    "file": fname,
                    "ext": ext,
                }
            )

    # Sort: phase, then wave (None last), then name
    records.sort(key=lambda r: (r["phase"], r["wave"] or 999, r["name"]))
    return records


def _group_by_wave(records: List[Dict]) -> Dict[Optional[int], List[Dict]]:
    """Group records by wave number. None = no wave (serial / prereqs)."""
    groups: Dict[Optional[int], List[Dict]] = {}
    for rec in records:
        key = rec["wave"]
        groups.setdefault(key, []).append(rec)
    return groups


# ---------------------------------------------------------------------------
# HTML generation helpers
# ---------------------------------------------------------------------------


def _type_badge(obj_type: str) -> str:
    bg, fg = _TYPE_COLOURS.get(obj_type, _TYPE_COLOUR_DEFAULT)
    return (
        f'<span style="background:{bg};color:{fg};'
        f"padding:2px 7px;border-radius:3px;font-size:11px;"
        f'font-weight:600;letter-spacing:.3px">{obj_type}</span>'
    )


def _trust_icon(status: str) -> str:
    if status == "pass":
        return '<span style="color:#198754;font-size:16px">✓</span>'
    if status == "fail":
        return '<span style="color:#DC3545;font-size:16px">✗</span>'
    return '<span style="color:#FFC107;font-size:16px">⚠</span>'


def _trust_label_style(label: str) -> Tuple[str, str]:
    """Return (background, text) colours for a trust label."""
    if label == "READY":
        return "#198754", _WHITE
    if label == "BLOCKED":
        return "#DC3545", _WHITE
    return "#FFC107", _NAVY  # READY-WITH-CAVEATS


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------


def _objects_tab(records: List[Dict]) -> str:
    """Filterable object inventory table."""
    type_set = sorted({r["type"] for r in records})

    filter_btns = "\n".join(
        f'<button class="flt-btn" onclick="filterType(\'{t}\')">{t}</button>'
        for t in type_set
    )

    rows = "\n".join(
        f'<tr data-type="{r["type"]}">'
        f"<td style='font-family:monospace'>{r['name']}</td>"
        f"<td>{_type_badge(r['type'])}</td>"
        f"<td>{r['phase']}</td>"
        f"<td>{'Wave ' + str(r['wave']) if r['wave'] else '—'}</td>"
        f"<td style='color:#6C757D;font-size:12px'>{r['file']}</td>"
        "</tr>"
        for r in records
    )

    return f"""
<div style="margin-bottom:12px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
  <span style="font-weight:600;margin-right:4px">Filter:</span>
  <button class="flt-btn active" onclick="filterType('ALL')">All ({len(records)})</button>
  {filter_btns}
</div>
<div style="overflow-x:auto">
<table id="obj-table" style="width:100%;border-collapse:collapse;font-size:13px">
  <thead>
    <tr style="background:{_NAVY};color:{_WHITE}">
      <th style="padding:8px 12px;text-align:left">Object</th>
      <th style="padding:8px 12px;text-align:left">Type</th>
      <th style="padding:8px 12px;text-align:left">Phase</th>
      <th style="padding:8px 12px;text-align:left">Wave</th>
      <th style="padding:8px 12px;text-align:left">File</th>
    </tr>
  </thead>
  <tbody id="obj-tbody">
    {rows}
  </tbody>
</table>
</div>
<script>
function filterType(t) {{
  document.querySelectorAll('.flt-btn').forEach(function(b) {{
    b.classList.toggle('active', b.textContent.startsWith(t === 'ALL' ? 'All' : t));
  }});
  document.querySelectorAll('#obj-tbody tr').forEach(function(row) {{
    row.style.display = (t === 'ALL' || row.dataset.type === t) ? '' : 'none';
  }});
}}
// Zebra stripe
document.querySelectorAll('#obj-tbody tr').forEach(function(row, i) {{
  row.style.background = i % 2 === 0 ? '#fff' : '#f8f9fa';
}});
</script>
"""


def _waves_tab(records: List[Dict]) -> str:
    """Tiered SVG wave plan visualisation."""
    wave_groups = _group_by_wave(records)
    wave_nums = sorted(k for k in wave_groups if k is not None)
    has_serial = None in wave_groups

    if not wave_nums and not has_serial:
        return '<p style="color:#6C757D;padding:32px;text-align:center">No wave data available — run <code>ships analyse</code> before packaging.</p>'

    # Build columns: serial (prereqs) first if present, then waves
    columns = []
    if has_serial:
        columns.append(("Serial", wave_groups[None]))
    for wn in wave_nums:
        columns.append((f"Wave {wn}", wave_groups[wn]))

    cell_h = 26
    cell_w = 210
    gap = 40  # arrow gap between columns
    col_pad = 12  # padding inside column header
    header_h = 34
    margin = 20
    arrow_w = gap

    max_items = max(len(items) for _, items in columns)
    col_h = max_items * cell_h + header_h + col_pad * 2
    svg_w = len(columns) * (cell_w + gap) - gap + margin * 2
    svg_h = col_h + margin * 2

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
        f'style="font-family:Inter,-apple-system,sans-serif;display:block;margin:0 auto">'
    ]

    for ci, (label, items) in enumerate(columns):
        x = margin + ci * (cell_w + gap)
        y = margin

        # Column background
        svg_parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_w}" height="{col_h}" '
            f'rx="6" fill="#f0f4f8" stroke="{_BORDER}" stroke-width="1"/>'
        )
        # Column header
        svg_parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_w}" height="{header_h}" '
            f'rx="6" fill="{_NAVY}"/>'
        )
        svg_parts.append(
            f'<rect x="{x}" y="{y + header_h - 6}" width="{cell_w}" height="6" fill="{_NAVY}"/>'
        )
        svg_parts.append(
            f'<text x="{x + cell_w // 2}" y="{y + 22}" text-anchor="middle" '
            f'font-size="13" font-weight="600" fill="{_WHITE}">{label}</text>'
        )

        # Items
        for ii, item in enumerate(items[:40]):  # cap at 40 per wave for readability
            iy = y + header_h + col_pad + ii * cell_h
            bg, fg = _TYPE_COLOURS.get(item["type"], _TYPE_COLOUR_DEFAULT)
            # type dot
            svg_parts.append(
                f'<circle cx="{x + 14}" cy="{iy + 13}" r="5" fill="{bg}"/>'
            )
            # object name (truncate to fit)
            name = item["name"]
            if len(name) > 28:
                name = name[:25] + "…"
            svg_parts.append(
                f'<text x="{x + 26}" y="{iy + 17}" font-size="11" fill="#333">{name}</text>'
            )

        if len(items) > 40:
            iy = y + header_h + col_pad + 40 * cell_h
            svg_parts.append(
                f'<text x="{x + cell_w // 2}" y="{iy + 13}" text-anchor="middle" '
                f'font-size="11" fill="#6C757D">… {len(items) - 40} more</text>'
            )

        # Arrow to next column
        if ci < len(columns) - 1:
            ax = x + cell_w
            ay = margin + col_h // 2
            svg_parts.append(
                f'<line x1="{ax}" y1="{ay}" x2="{ax + arrow_w}" y2="{ay}" '
                f'stroke="{_ORANGE}" stroke-width="2" marker-end="url(#arr)"/>'
            )

    # Arrow marker
    svg_parts.insert(
        1,
        '<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">'
        f'<path d="M0,0 L0,6 L8,3 z" fill="{_ORANGE}"/></marker></defs>',
    )

    svg_parts.append("</svg>")

    # Type legend
    legend_items = sorted({r["type"] for r in records})
    legend_parts = []
    for t in legend_items[:12]:
        bg, fg = _TYPE_COLOURS.get(t, _TYPE_COLOUR_DEFAULT)
        legend_parts.append(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">'
            f'<span style="width:10px;height:10px;border-radius:50%;background:{bg};display:inline-block"></span>'
            f'<span style="font-size:12px;color:#555">{t}</span></span>'
        )

    return (
        '<div style="overflow-x:auto;padding:8px 0">\n'
        + "\n".join(svg_parts)
        + '\n</div>\n<div style="margin-top:16px;padding:0 8px">'
        + "".join(legend_parts)
        + "</div>"
    )


def _trust_tab(trust: dict) -> str:
    """Trust Report signals table."""
    label = trust.get("label", "UNKNOWN")
    signals = trust.get("signals", {})
    bg, fg = _trust_label_style(label)

    label_html = (
        f'<div style="display:inline-block;background:{bg};color:{fg};'
        f"padding:6px 20px;border-radius:4px;font-size:18px;font-weight:700;"
        f'margin-bottom:20px">{label}</div>'
    )

    rows = ""
    for name, sig in signals.items():
        status = sig.get("status", "?") if isinstance(sig, dict) else str(sig)
        detail = sig.get("detail", "") if isinstance(sig, dict) else ""
        icon = _trust_icon(status)
        rows += (
            f"<tr>"
            f"<td style='padding:10px 12px;font-family:monospace;font-size:13px'>{name}</td>"
            f"<td style='padding:10px 12px'>{icon} {status}</td>"
            f"<td style='padding:10px 12px;color:#555;font-size:13px'>{detail}</td>"
            "</tr>"
        )

    if not rows:
        rows = '<tr><td colspan="3" style="padding:16px;color:#6C757D">No signals recorded.</td></tr>'

    return f"""
{label_html}
<table style="width:100%;border-collapse:collapse;font-size:14px">
  <thead>
    <tr style="background:{_NAVY};color:{_WHITE}">
      <th style="padding:8px 12px;text-align:left">Signal</th>
      <th style="padding:8px 12px;text-align:left">Status</th>
      <th style="padding:8px 12px;text-align:left">Detail</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
"""


def _deploy_tab(manifest_dict: dict) -> str:
    """Pre-filled deploy commands with one-click clipboard copy."""
    pkg_name = manifest_dict.get("package_name", "")
    build_no = manifest_dict.get("build_number", "?")
    env = manifest_dict.get("environment", "?")
    requires = manifest_dict.get("requires", [])

    prereqs_note = ""
    if requires:
        prereqs_note = (
            f'<div style="background:#FFF3CD;border:1px solid #FFC107;'
            f'border-radius:4px;padding:12px 16px;margin-bottom:20px">'
            f"<strong>⚠ Deploy the companion archive first</strong><br>"
            f"This package requires: <code>{'</code>, <code>'.join(requires)}</code><br>"
            f"Extract and deploy that archive before deploying this one."
            f"</div>"
        )

    def cmd_block(label: str, cmd: str, note: str = "") -> str:
        cmd_id = label.replace(" ", "_").lower()
        return f"""
<div style="margin-bottom:20px">
  <div style="font-weight:600;margin-bottom:6px">{label}</div>
  {"<div style='font-size:13px;color:#555;margin-bottom:6px'>" + note + "</div>" if note else ""}
  <div style="position:relative">
    <pre id="{cmd_id}" style="background:#1E2761;color:#E8F0FE;padding:14px 48px 14px 16px;
      border-radius:6px;font-size:13px;overflow-x:auto;margin:0">{cmd}</pre>
    <button onclick="copyCmd('{cmd_id}')"
      style="position:absolute;top:8px;right:8px;background:#FF5F02;color:#fff;
      border:none;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px">
      Copy
    </button>
  </div>
</div>"""

    blocks = ""
    if prereqs_note:
        blocks += prereqs_note

    blocks += cmd_block(
        "Dry run (recommended first)",
        f"python deploy.py --host &lt;host&gt; --user &lt;user&gt; --dry-run",
        "Validates the pipeline and runs pre-flight checks. No DDL is executed.",
    )
    blocks += cmd_block(
        "Standard deployment",
        f"python deploy.py --host &lt;host&gt; --user &lt;user&gt;",
        "Serial deployment. Safe for small packages.",
    )
    blocks += cmd_block(
        "Wave-parallel deployment",
        f"python deploy.py --host &lt;host&gt; --user &lt;user&gt; --streams 4",
        "Deploys independent objects in parallel. Faster for large packages (50+ objects).",
    )
    blocks += cmd_block(
        "Continue on error (collect all failures in one pass)",
        f"python deploy.py --host &lt;host&gt; --user &lt;user&gt; --continue-on-error",
    )

    return f"""
<div style="margin-bottom:16px;padding:12px 16px;background:{_LIGHT};
  border-radius:6px;font-size:14px">
  <strong>{pkg_name}</strong> &nbsp;|&nbsp; Build {build_no} &nbsp;|&nbsp; {env}
  &nbsp;&nbsp; — run the commands below from inside the extracted package directory
</div>
{blocks}
<script>
function copyCmd(id) {{
  var el = document.getElementById(id);
  var text = el.innerText.replace(/</g,'<').replace(/>/g,'>');
  navigator.clipboard.writeText(text).then(function() {{
    var btn = el.nextElementSibling;
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.style.background = '#198754';
    setTimeout(function() {{ btn.textContent = orig; btn.style.background = '#FF5F02'; }}, 1500);
  }});
}}
</script>
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_package_report(pkg_dir: str, manifest_dict: dict) -> str:
    """Generate the interactive HTML package report and write it to ``pkg_dir``.

    Args:
        pkg_dir:       The package directory (not yet archived).
        manifest_dict: The ``BuildManifest.__dict__`` already written to
                       BUILD.json.

    Returns:
        Absolute path to the written report file.
    """
    records = _scan_payload(pkg_dir)
    trust = manifest_dict.get("trust", {})

    pkg_name = manifest_dict.get("package_name", "Package")
    build_no = manifest_dict.get("build_number", "?")
    env = manifest_dict.get("environment", "?")
    file_count = manifest_dict.get("file_count", len(records))
    trust_label = trust.get("label", "")
    trust_bg, trust_fg = _trust_label_style(trust_label)

    # Subtitle summary line
    type_counts: Dict[str, int] = {}
    for r in records:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
    summary_parts = [
        f"{v} {k.lower()}{'s' if v != 1 else ''}"
        for k, v in sorted(type_counts.items())
    ]
    summary = ",  ".join(summary_parts[:6])
    if len(summary_parts) > 6:
        summary += f",  …and {len(summary_parts) - 6} more types"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SHIPS Package Report — {pkg_name} {build_no}</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f0f4f8; color: #212529; min-height: 100vh; }}
.hdr {{ background: {_NAVY}; color: {_WHITE}; padding: 0 24px;
        display: flex; align-items: center; gap: 16px; height: 56px; }}
.hdr-title {{ font-size: 17px; font-weight: 700; letter-spacing: -.2px; }}
.hdr-sub {{ font-size: 13px; color: #8ba4be; }}
.trust-pill {{ margin-left: auto; background: {trust_bg}; color: {trust_fg};
               padding: 4px 14px; border-radius: 20px; font-size: 13px;
               font-weight: 700; letter-spacing: .3px; white-space: nowrap; }}
.meta-bar {{ background: {_WHITE}; border-bottom: 1px solid {_BORDER};
             padding: 10px 24px; font-size: 13px; color: #555;
             display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
.meta-bar strong {{ color: {_NAVY}; }}
.tabs {{ background: {_WHITE}; border-bottom: 2px solid {_BORDER}; padding: 0 24px;
         display: flex; gap: 0; }}
.tab-btn {{ background: none; border: none; border-bottom: 3px solid transparent;
            padding: 14px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
            color: #555; margin-bottom: -2px; }}
.tab-btn.active {{ color: {_NAVY}; border-bottom-color: {_ORANGE}; font-weight: 700; }}
.tab-btn:hover {{ color: {_NAVY}; }}
.tab-pane {{ display: none; }}
.tab-pane.active {{ display: block; }}
.content {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
.card {{ background: {_WHITE}; border-radius: 8px; border: 1px solid {_BORDER};
         padding: 20px 24px; margin-bottom: 16px; }}
.flt-btn {{ background: {_LIGHT}; border: 1px solid {_BORDER}; border-radius: 4px;
            padding: 4px 10px; font-size: 12px; cursor: pointer; }}
.flt-btn.active {{ background: {_NAVY}; color: {_WHITE}; border-color: {_NAVY}; }}
.flt-btn:hover {{ border-color: {_ORANGE}; }}
#obj-tbody tr {{ cursor: default; }}
#obj-tbody tr:hover {{ background: #e8f0fe !important; }}
#obj-tbody td {{ padding: 7px 12px; border-bottom: 1px solid #f0f0f0; }}
pre {{ white-space: pre-wrap; word-break: break-all; }}
</style>
</head>
<body>

<div class="hdr">
  <svg width="90" height="24" viewBox="0 0 90 24" xmlns="http://www.w3.org/2000/svg">
    <text x="0" y="19" font-family="Inter,sans-serif" font-size="18" font-weight="700"
          letter-spacing="-.3" fill="#fff">Teradata</text>
  </svg>
  <div>
    <div class="hdr-title">Package Report &nbsp;·&nbsp; {pkg_name}</div>
    <div class="hdr-sub">Build {build_no} &nbsp;·&nbsp; {env}</div>
  </div>
  <div class="trust-pill">{trust_label or "—"}</div>
</div>

<div class="meta-bar">
  <span><strong>{file_count}</strong> objects</span>
  <span style="color:{_BORDER}">|</span>
  <span style="color:#777">{summary}</span>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab(this,'tab-objects')">Objects</button>
  <button class="tab-btn" onclick="switchTab(this,'tab-waves')">Waves</button>
  <button class="tab-btn" onclick="switchTab(this,'tab-trust')">Trust Report</button>
  <button class="tab-btn" onclick="switchTab(this,'tab-deploy')">Deploy</button>
</div>

<div class="content">

<div id="tab-objects" class="tab-pane active card">
{_objects_tab(records)}
</div>

<div id="tab-waves" class="tab-pane card">
{_waves_tab(records)}
</div>

<div id="tab-trust" class="tab-pane card">
{_trust_tab(trust)}
</div>

<div id="tab-deploy" class="tab-pane card">
{_deploy_tab(manifest_dict)}
</div>

</div>

<script>
function switchTab(btn, pane) {{
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.querySelectorAll('.tab-pane').forEach(function(p) {{ p.classList.remove('active'); }});
  btn.classList.add('active');
  document.getElementById(pane).classList.add('active');
}}
</script>
</body>
</html>"""

    report_path = os.path.join(pkg_dir, "package_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("Package report: %s", report_path)
    return report_path
