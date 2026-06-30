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

import html
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from td_release_packager.trust import TRUST_PASS, TRUST_WARN
from td_release_packager.report_viewer import (
    safe_viewer_filename as _safe_viewer_filename,
    source_viewer_html as _source_viewer_html,
    SIGNAL_EXPLANATIONS as _SIGNAL_EXPLANATIONS,
    signal_name_cell as _signal_name_cell_shared,
    VIEWER_INDEX_FILENAME as _VIEWER_INDEX_FILENAME,
)
from td_release_packager.reporting import common as _common, waves as _waves

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension → object type mapping
# ---------------------------------------------------------------------------

# Object-type colour system and extension mapping are shared with the
# pipeline report via reporting.waves so both reports render objects
# identically.  These module-level aliases preserve the existing call
# sites (and the package-report test imports) unchanged.
_EXT_TYPE: Dict[str, str] = _waves.EXT_TYPE
_TYPE_COLOURS: Dict[str, Tuple[str, str]] = _waves.TYPE_COLOURS
_TYPE_COLOUR_DEFAULT = _waves.TYPE_COLOUR_DEFAULT

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
        "context",
    }
)
_SKIP_EXTS = frozenset({".json", ".py", ".sh", ".bat", ".txt", ".html", ".jar"})

_SCRIPT_VERB_RE = re.compile(
    r"""
    ^\s*
    (?P<verb>CREATE|REPLACE|DROP|ALTER|GRANT|REVOKE|INSERT|UPDATE|DELETE|MERGE|CALL|COMMENT|COLLECT)
    (?:\s+(?P<qualifier>MULTISET|SET|GLOBAL\s+TEMPORARY|VOLATILE|JOIN|HASH|UNIQUE|SUMMARY|ON))?
    (?:\s+(?P<object>TABLE|VIEW|MACRO|PROCEDURE|FUNCTION|TRIGGER|INDEX|DATABASE|USER|ROLE|PROFILE|MAP|AUTHORIZATION|SERVER|STATISTICS|SQLJ))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SUMMARY_BASELINES = {
    "DCL": ["GRANT", "REVOKE", "MIXED DCL"],
    "DDL": [
        "CREATE/TABLE",
        "CREATE/VIEW",
        "CREATE/MACRO",
        "CREATE/PROCEDURE",
        "CREATE/FUNCTION",
        "REPLACE/VIEW",
        "REPLACE/MACRO",
        "REPLACE/PROCEDURE",
        "REPLACE/FUNCTION",
        "DROP/TABLE",
        "DROP/VIEW",
    ],
    "DML": ["INSERT", "UPDATE", "DELETE", "MERGE", "MIXED DML", "ORDERED SQL"],
}


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


# Wave-file parsing is shared with the pipeline report (reporting.waves).
_parse_waves_txt = _waves.parse_waves_txt


def _strip_report_comments_and_strings(sql: str) -> str:
    """Blank comments and string literals for lightweight script classification."""
    chars = list(sql)
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch == "-" and nxt == "-":
            start = i
            i += 2
            while i < len(chars) and chars[i] != "\n":
                i += 1
            for j in range(start, i):
                chars[j] = " "
            continue
        if ch == "/" and nxt == "*":
            start = i
            i += 2
            while i + 1 < len(chars) and not (chars[i] == "*" and chars[i + 1] == "/"):
                i += 1
            i = min(i + 2, len(chars))
            for j in range(start, i):
                chars[j] = " "
            continue
        if ch == "'":
            start = i
            i += 1
            while i < len(chars):
                if chars[i] == "'":
                    if i + 1 < len(chars) and chars[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            for j in range(start, i):
                chars[j] = " "
            continue
        i += 1
    return "".join(chars)


def _normalise_script_head(
    verb: str, qualifier: str, obj: str, fallback_type: str
) -> str:
    """Return a compact report label such as ``CREATE/PROCEDURE``."""
    verb = verb.upper()
    qualifier = " ".join((qualifier or "").upper().split())
    obj = (obj or "").upper()
    if qualifier == "JOIN" and obj == "INDEX":
        obj = "JOIN INDEX"
    elif qualifier == "HASH" and obj == "INDEX":
        obj = "HASH INDEX"
    elif obj in {"", "SQLJ"}:
        obj = fallback_type.upper()
    if verb == "DELETE":
        return "DELETE"
    if verb == "INSERT":
        return "INSERT"
    if verb == "UPDATE":
        return "UPDATE"
    if verb == "MERGE":
        return "MERGE"
    if verb in {"GRANT", "REVOKE"}:
        return verb
    if verb == "CALL" and fallback_type == "JAR":
        return "CALL/JAR"
    if verb in {"COMMENT", "COLLECT"}:
        return fallback_type.upper()
    return f"{verb}/{obj}"


def _script_intent(content: str, obj_type: str, phase: str, ext: str) -> str:
    """Classify the script's high-level intent for the Summary tab."""
    if ext == ".osql":
        return "ORDERED SQL"

    clean = _strip_report_comments_and_strings(content)
    heads: List[str] = []
    for chunk in re.split(r";", clean):
        match = _SCRIPT_VERB_RE.search(chunk)
        if not match:
            continue
        heads.append(
            _normalise_script_head(
                match.group("verb") or "",
                match.group("qualifier") or "",
                match.group("object") or "",
                obj_type,
            )
        )

    if not heads:
        return obj_type

    phase_upper = phase.upper()
    if phase_upper == "DCL":
        dcl_heads = [h for h in heads if h in {"GRANT", "REVOKE"}]
        if dcl_heads and len(set(dcl_heads)) == 1:
            return dcl_heads[0]
        if dcl_heads:
            return "MIXED DCL"
        return obj_type

    if phase_upper == "DML":
        dml_heads = [h for h in heads if h in {"INSERT", "UPDATE", "DELETE", "MERGE"}]
        if dml_heads and len(set(dml_heads)) == 1:
            return dml_heads[0]
        if dml_heads:
            return "MIXED DML"
        return heads[0]

    return heads[0]


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
            rel_file = os.path.relpath(os.path.join(root, fname), pkg_dir).replace(
                "\\", "/"
            )
            phase_root = os.path.join(payload_dir, phase_dir) if phase_dir else root
            phase_rel_file = os.path.relpath(
                os.path.join(root, fname), phase_root
            ).replace("\\", "/")

            stem = os.path.splitext(fname)[0]  # e.g. "OMR_STD.Customer"
            wave = (
                wave_map.get(phase_rel_file)
                or wave_map.get(rel_file)
                or wave_map.get(fname)
            )
            # System-scope artefacts are executed serially before any wave
            # deployment starts.  Even if a package-local _waves.txt exists
            # to help older runtimes, the package report must not show
            # CREATE ROLE/PROFILE/MAP/AUTHORIZATION/FOREIGN SERVER scripts
            # in the same numbered wave as later DCL/DDL work.
            if phase_dir == "00_system":
                wave = None
            phase_label = _phase_label(os.path.join(payload_dir, phase_dir))
            file_path = os.path.join(root, fname)
            try:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                content = ""

            records.append(
                {
                    "name": stem,
                    "type": obj_type,
                    "phase": phase_label,
                    "intent": _script_intent(content, obj_type, phase_label, ext),
                    "wave": wave,
                    "file": fname,
                    "path": rel_file,
                    "ext": ext,
                }
            )

    # Sort: phase, then wave (None last), then name
    records.sort(key=lambda r: (r["phase"], r["wave"] or 999, r["name"]))
    return records


# Wave grouping is shared with the pipeline report (reporting.waves).
_group_by_wave = _waves.group_by_wave


# ---------------------------------------------------------------------------
# HTML generation helpers
# ---------------------------------------------------------------------------


def _type_badge(obj_type: str) -> str:
    return _waves.type_badge(obj_type)


def _trust_icon(status: str) -> str:
    if status == "pass":
        return '<span style="color:#198754;font-size:16px">✓</span>'
    if status == "fail":
        return '<span style="color:#DC3545;font-size:16px">✗</span>'
    return '<span style="color:#FFC107;font-size:16px">⚠</span>'


def _trust_label_style(label: str) -> Tuple[str, str]:
    """Return (background, text) colours for a trust status."""
    if label == "READY":
        return "#198754", _WHITE
    if label == "BLOCKED":
        return "#DC3545", _WHITE
    return "#FFC107", _NAVY  # READY_WITH_CAVEATS


def _h(value: object) -> str:
    """HTML-escape text content."""
    return html.escape(str(value), quote=False)


def _a(value: object) -> str:
    """HTML-escape attribute values."""
    return html.escape(str(value), quote=True)


def _file_link(record: Dict, viewer_links: Optional[Dict[str, str]] = None) -> str:
    """Return a hyperlink for a payload file.

    When ``viewer_links`` contains a pre-generated syntax-highlighted
    viewer page for this record's path, the link points at that page
    instead of the raw payload file.  This gives the same click-to-view
    experience as the deploy report.

    Args:
        record:       A record dict from ``_scan_payload``.
        viewer_links: Optional mapping of package-relative payload path
                      to viewer-page href, as returned by
                      ``_write_package_viewers``.  When ``None`` (or
                      when the path is not in the map) the link falls
                      back to the raw payload file.

    Returns:
        An HTML ``<a>`` element string.
    """
    path = record.get("path") or record.get("file", "")
    label = record.get("file", path)
    norm_path = path.replace("\\", "/").lstrip("./")
    href = (viewer_links or {}).get(norm_path) or (viewer_links or {}).get(path)
    if href is None:
        href = path
    return (
        f'<a href="{_a(href)}" title="{_a(path)}" '
        f'style="color:#0D6EFD;text-decoration:none">{_h(label)}</a>'
    )


def _write_package_viewers(
    pkg_dir: str,
    records: List[Dict],
) -> Dict[str, str]:
    """Write syntax-highlighted viewer pages for every payload script.

    The viewer pages are standalone HTML files written into a hidden
    sub-directory beside the package report so the package remains
    self-contained.  The directory name mirrors the convention used by
    the deploy report so the two report types look and behave
    consistently.

    Args:
        pkg_dir: Package root directory (the directory that contains the
                 ``payload/`` tree and ``package_report.html``).
        records: Payload records from ``_scan_payload``.  Each record
                 must contain ``path`` (package-relative payload path)
                 and the file content is re-read from disk via that path.

    Returns:
        Mapping of normalised package-relative payload path to the
        report-relative href of the corresponding viewer page.  Keys
        use forward slashes and have any leading ``./`` stripped so they
        match the form used in ``_file_link``.  Returns an empty dict
        when no viewers could be written.
    """
    viewer_dir_name = ".package_report_code"
    viewer_dir = os.path.join(pkg_dir, viewer_dir_name)
    links: Dict[str, str] = {}

    sidecar_index: Dict[str, str] = {}
    for index, record in enumerate(records, 1):
        raw_path = record.get("path", "")
        if not raw_path:
            continue
        norm_path = raw_path.replace("\\", "/").lstrip("./")
        abs_path = os.path.join(pkg_dir, norm_path)
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            # File unreadable — skip; the raw payload link is the fallback.
            continue

        os.makedirs(viewer_dir, exist_ok=True)
        viewer_name = _safe_viewer_filename(norm_path, index)
        viewer_path = os.path.join(viewer_dir, viewer_name)
        html = _source_viewer_html(
            title=f"Source: {norm_path}",
            packaged_path=norm_path,
            source_path=record.get("file", norm_path),
            content=content,
        )
        try:
            with open(viewer_path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except OSError as exc:
            logger.warning("Could not write viewer page for %s: %s", norm_path, exc)
            continue

        links[norm_path] = f"{viewer_dir_name}/{viewer_name}"
        sidecar_index[viewer_name] = norm_path

    # Sidecar: viewer_filename -> payload_path. Lets humans (and any
    # external tool) map the hashed filenames back to source paths
    # without scanning the HTML report. See #392.
    if sidecar_index:
        try:
            sidecar_path = os.path.join(viewer_dir, _VIEWER_INDEX_FILENAME)
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                json.dump(sidecar_index, fh, indent=2, sort_keys=True)
                fh.write("\n")
        except OSError as exc:
            logger.warning("Could not write viewer index sidecar: %s", exc)

    return links


def _normalise_issue_path(value: object) -> str:
    """Normalise a trust issue location or package path for loose matching."""
    text = str(value or "").replace("\\", "/")
    if ": [" in text:
        text = text.split(": [", 1)[0]
    if ":" in text:
        head, tail = text.rsplit(":", 1)
        if tail.isdigit():
            text = head
    if "/payload/" in text:
        text = "payload/" + text.split("/payload/", 1)[1]
    return text.strip().lstrip("./").lower()


def _trust_issue_map(trust: dict) -> Dict[str, List[str]]:
    """Return package/script path keys that have failing trust issues."""
    issue_map: Dict[str, List[str]] = {}
    for signal_name, signal in (trust or {}).get("signals", {}).items():
        if not isinstance(signal, dict):
            continue
        status = signal.get("status")
        if status in (TRUST_PASS, TRUST_WARN):
            continue
        for issue in signal.get("issues", []) or []:
            path = _normalise_issue_path(issue)
            if not path:
                continue
            issue_map.setdefault(path, []).append(f"{signal_name}: {issue}")
    return issue_map


def _record_trust_issues(record: Dict, issue_map: Dict[str, List[str]]) -> List[str]:
    """Return failing trust issues that point at a payload record."""
    if not issue_map:
        return []
    candidates = {
        _normalise_issue_path(record.get("path")),
        _normalise_issue_path(record.get("file")),
    }
    path = _normalise_issue_path(record.get("path"))
    if path.startswith("payload/"):
        candidates.add(path.removeprefix("payload/"))

    matches: List[str] = []
    for issue_path, issues in issue_map.items():
        issue_name = os.path.basename(issue_path)
        if issue_path in candidates or issue_name in candidates:
            matches.extend(issues)
    return matches


def _trust_blocker_badge(issues: List[str]) -> str:
    """Render a compact blocker badge for an object-table row."""
    if not issues:
        return ""
    title = "\n".join(issues[:5])
    return (
        f'<span title="{_a(title)}" '
        f'style="display:inline-block;background:#DC3545;color:#fff;'
        f"padding:2px 7px;border-radius:3px;font-size:11px;"
        f'font-weight:700;margin-left:6px">BLOCKS TRUST</span>'
    )


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------


def _package_report_label(manifest_dict: dict) -> str:
    """Return the report label, including split role when applicable."""
    role = str(manifest_dict.get("role") or "").lower()
    if role == "prereqs":
        return "Pre-requisites Package Report"
    if role == "main":
        return "Main Package Report"
    return "Package Report"


def _objects_tab(
    records: List[Dict],
    trust: Optional[dict] = None,
    viewer_links: Optional[Dict[str, str]] = None,
) -> str:
    """Filterable object inventory table.

    Args:
        records:      Payload records from ``_scan_payload``.
        trust:        Trust report dict from the build manifest, used to
                      flag objects with blocking trust issues.
        viewer_links: Optional mapping returned by ``_write_package_viewers``.
                      When supplied, each file link opens a syntax-highlighted
                      viewer page rather than the raw payload file.
    """
    type_set = sorted({r["type"] for r in records})
    issue_map = _trust_issue_map(trust or {})

    filter_btns = "\n".join(
        f'<button class="flt-btn" onclick="filterType(\'{t}\')">{t}</button>'
        for t in type_set
    )

    rows = "\n".join(
        f'<tr data-type="{_a(r["type"])}">'
        f"<td style='font-family:monospace' title='{_a(r['name'])}'>"
        f"{_h(r['name'])}{_trust_blocker_badge(_record_trust_issues(r, issue_map))}</td>"
        f"<td>{_type_badge(r['type'])}</td>"
        f"<td>{_h(r['phase'])}</td>"
        f"<td>{'Wave ' + str(r['wave']) if r['wave'] else '—'}</td>"
        f"<td style='color:#6C757D;font-size:12px'>{_file_link(r, viewer_links)}</td>"
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


def _summary_category(phase: str) -> str:
    phase_upper = phase.upper()
    if phase_upper == "DCL":
        return "DCL"
    if phase_upper == "DML":
        return "DML"
    if phase_upper in {"DDL", "PRE-REQUISITES", "SYSTEM", "POST-INSTALL"}:
        return "DDL" if phase_upper == "DDL" else phase
    return "Other"


def _script_summary(records: List[Dict]) -> Dict[str, Dict[str, int]]:
    """Aggregate records by report category and intent label."""
    summary: Dict[str, Dict[str, int]] = {}
    for category, labels in _SUMMARY_BASELINES.items():
        summary[category] = {label: 0 for label in labels}

    for record in records:
        category = _summary_category(record.get("phase", "Other"))
        intent = record.get("intent") or record.get("type") or "UNKNOWN"
        summary.setdefault(category, {})
        summary[category][intent] = summary[category].get(intent, 0) + 1

    return summary


def _summary_flags(summary: Dict[str, Dict[str, int]]) -> List[str]:
    """Return human-readable high-level signals from the summary counts."""
    flags: List[str] = []
    dcl = summary.get("DCL", {})
    dml = summary.get("DML", {})
    if dcl.get("REVOKE", 0) and dcl.get("GRANT", 0) == 0:
        flags.append("DCL contains REVOKE scripts but no GRANT scripts.")
    if dcl.get("MIXED DCL", 0):
        flags.append("DCL contains mixed GRANT/REVOKE scripts.")
    if dml.get("DELETE", 0) and dml.get("MERGE", 0) == 0:
        flags.append("DML contains DELETE scripts and no MERGE scripts.")
    if dml.get("ORDERED SQL", 0):
        flags.append("Ordered SQL scripts preserve source choreography.")
    return flags


def _summary_tab(records: List[Dict]) -> str:
    """Top-down script type summary."""
    summary = _script_summary(records)
    flags = _summary_flags(summary)

    def render_category(name: str, counts: Dict[str, int]) -> str:
        # Issue #277 — highlight rows where count > 0 so a DBA can
        # skim the Summary tab for what's actually in the package.
        # The .has-count / .zero-count class drives the styling; the
        # row markup is otherwise identical (no a11y regressions).
        rows = "\n".join(
            f'<tr class="{"has-count" if count > 0 else "zero-count"}">'
            f"<td>{_h(label)}</td><td>{count}</td></tr>"
            for label, count in sorted(counts.items(), key=lambda item: item[0])
        )
        return f"""
<section class="summary-section">
  <h3>{_h(name)}</h3>
  <table class="summary-table">
    <tbody>{rows}</tbody>
  </table>
</section>
"""

    # System and Pre-requisites are deployment prerequisites, so they are
    # placed first (left-to-right reading order mirrors execution order).
    priority_categories = ["System", "Pre-requisites"]
    ordered_categories = ["DCL", "DDL", "DML"]
    known_categories = set(priority_categories + ordered_categories)
    extra_categories = sorted(k for k in summary if k not in known_categories)
    display_order = priority_categories + ordered_categories + extra_categories
    rendered = [
        render_category(category, summary[category])
        for category in display_order
        if summary.get(category)
    ]
    sections = "\n".join(rendered)

    # Fix the grid column count explicitly to the number of visible panels so
    # that CSS auto-fit cannot wrap a panel (e.g. System) onto a second row
    # when the viewport happens to be narrower than 5 × 260 px.  Panels are
    # still responsive: the grid collapses gracefully on narrow screens because
    # minmax(0,1fr) allows each column to shrink freely.
    col_count = max(len(rendered), 1)

    flag_html = ""
    if flags:
        flag_items = "".join(f"<li>{_h(flag)}</li>" for flag in flags)
        flag_html = f"""
<div class="summary-flags">
  <strong>Signals worth checking</strong>
  <ul>{flag_items}</ul>
</div>
"""

    return f"""
<p style="color:#555;margin-bottom:16px">
  Script intent is inferred from the top-level statement verbs in each packaged file.
</p>
{flag_html}
<div class="summary-grid" style="grid-template-columns:repeat({col_count},minmax(0,1fr))">
{sections}
</div>
"""


def _waves_tab(records: List[Dict]) -> str:
    """Tiered SVG wave plan visualisation (shared with the pipeline report)."""
    return _waves.render_wave_svg(records)


def _load_content_provenance(pkg_dir: str) -> Optional[dict]:
    """Load the v2 content-provenance document from ``context/ships.provenance.json``.

    Unlike :func:`_load_build_provenance` (which reads project-side
    ``ships.decisions.json``), this artefact lives **inside** the package
    and travels with it — so the Content Provenance tab keeps working
    after the package is handed off or extracted on another machine.

    Schema (v2): top-level ``entries`` maps each packaged path to a chain
    of four stages (source -> eponymous -> token_resolved -> package),
    each stage carrying status (applied / no_op / skipped / failed) and
    an optional note. See ``database_package_deployer.provenance`` for the
    canonical schema definition.

    Args:
        pkg_dir: Package root directory passed to ``generate_package_report``.

    Returns:
        The parsed document, or ``None`` when ``ships.provenance.json`` is
        missing, unreadable, or carries an unrecognised schema version.
    """
    provenance_path = os.path.join(pkg_dir, "context", "ships.provenance.json")
    if not os.path.isfile(provenance_path):
        return None
    try:
        with open(provenance_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    # Only v2 is supported; v1 was a flat {package_path: source_path} dict
    # and is not produced by any shipped version.
    if not isinstance(data, dict) or data.get("version") != 2:
        return None
    if not isinstance(data.get("entries"), dict):
        return None
    return data


def _content_provenance_tab(
    doc: Optional[dict], viewer_links: Optional[Dict[str, str]] = None
) -> str:
    """Render the Content Provenance tab — one row per packaged file.

    Shows where each packaged artefact came from (source path) and the
    full transformation chain (source -> eponymous -> token_resolved ->
    package) including the status badge and note for each stage.

    Each row is clickable to reveal the chain detail; the packaged path
    links to its source viewer page (``.package_report_code/...html``)
    when available, so a click goes from "where did this come from?"
    straight to the highlighted file content.

    Args:
        doc:          Parsed provenance document from
                      :func:`_load_content_provenance`. ``None`` renders
                      the "not available" placeholder.
        viewer_links: Map of packaged path -> viewer URL. Provided by
                      :func:`_write_package_viewers`; pass ``None`` or
                      ``{}`` to render the packaged path as plain text.

    Returns:
        HTML string ready to embed in the report's ``<div class="card">``.
    """
    if doc is None:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "Content provenance not available &mdash; "
            "<code>context/ships.provenance.json</code> was not found in this "
            "package, or could not be parsed. Re-build the package with a "
            "current SHIPS version to populate it."
            "</p>"
        )

    entries = doc.get("entries", {})
    if not entries:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "Content provenance document is empty (zero entries)."
            "</p>"
        )

    # Status -> badge style. Map provenance Status values to the same
    # colour vocabulary used by _build_provenance_tab so the report's
    # two provenance tabs feel consistent.
    _STATUS_ICON = {
        "applied": "✔",
        "no_op": "—",
        "skipped": "○",
        "failed": "✗",
    }
    _STATUS_BG = {
        "applied": "#D4EDDA",
        "no_op": "#E9ECEF",
        "skipped": "#E9ECEF",
        "failed": "#F8D7DA",
    }
    _STATUS_FG = {
        "applied": "#155724",
        "no_op": "#495057",
        "skipped": "#495057",
        "failed": "#721C24",
    }

    def _status_badge(status: str) -> str:
        icon = _STATUS_ICON.get(status, "?")
        bg = _STATUS_BG.get(status, "#E9ECEF")
        fg = _STATUS_FG.get(status, "#495057")
        return (
            f'<span style="display:inline-block;padding:2px 8px;'
            f"background:{bg};color:{fg};border-radius:10px;"
            f'font-size:11px;font-weight:600">{icon} {html.escape(status)}</span>'
        )

    def _esc(s: object) -> str:
        return html.escape(str(s or ""))

    viewer_links = viewer_links or {}

    rows: List[str] = []
    stage_counts: Dict[str, int] = {
        "applied": 0,
        "no_op": 0,
        "skipped": 0,
        "failed": 0,
    }

    # Sort by packaged path so the table reads in the same order as the
    # Objects tab — keeps the two views correlatable at a glance.
    for packaged_path in sorted(entries.keys()):
        chain = entries[packaged_path] or {}
        stages = chain.get("stages") or []
        if not stages:
            continue

        # ``harvest_source`` (#477) is the *user-authored* source file
        # the chain ultimately traces back to — the path the operator
        # actually edits to change the deployed object. The post-harvest
        # ``source`` stage's path is downstream of this: harvest splits
        # multi-statement source files into one destination file per
        # statement and renames them to the eponymous-form layout.
        # When the field is present, surface it in the outer row's
        # "Source path" column so the operator sees the path they
        # actually need to open; otherwise fall back to the post-harvest
        # source-stage path (the prior behaviour, kept for chains built
        # outside the harvest-aware build flow).
        harvest_source = chain.get("harvest_source") or ""
        source_path = harvest_source or stages[0].get("path", "")

        # Packaged path links to the existing viewer page when present.
        # Don't synthesize a link to a file that wasn't written.
        viewer_url = viewer_links.get(packaged_path)
        if viewer_url:
            packaged_cell = (
                f'<a href="{_esc(viewer_url)}" '
                f'title="{_esc(packaged_path)}" '
                f'style="font-family:ui-monospace,monospace;font-size:12px;'
                f'color:#0E4D8C;text-decoration:none">{_esc(packaged_path)}</a>'
            )
        else:
            packaged_cell = (
                f'<span style="font-family:ui-monospace,monospace;'
                f'font-size:12px">{_esc(packaged_path)}</span>'
            )

        # Stage-chain detail rendered as a small inner table inside the
        # row's <details> body. Each stage row: badge + name + path + note.
        chain_rows: List[str] = []
        if harvest_source:
            # Render harvest_source as a synthetic leading row so the
            # full provenance is visible end-to-end. It sits *before*
            # the v2 ``source`` stage because it is upstream of harvest.
            # No status badge — this is a recorded fact about the user
            # workspace, not a pipeline-stage outcome.
            chain_rows.append(
                f"<tr>"
                f'<td style="padding:4px 8px;white-space:nowrap">'
                f'<span style="display:inline-block;padding:2px 8px;'
                f"border-radius:3px;background:#E0E7FF;color:#3730A3;"
                f'font-size:10px;font-weight:600;text-transform:uppercase">'
                f"authored</span></td>"
                f'<td style="padding:4px 8px;font-weight:600;'
                f'color:#0E4D8C;white-space:nowrap">harvest_source</td>'
                f'<td style="padding:4px 8px;font-family:ui-monospace,monospace;'
                f'font-size:11px;color:#333;word-break:break-all">'
                f"{_esc(harvest_source)}</td>"
                f'<td style="padding:4px 8px;font-size:12px;'
                f'color:#666;font-style:italic">'
                f"User-authored source file (pre-harvest)</td>"
                f"</tr>"
            )
        for stage in stages:
            stage_name = stage.get("stage", "")
            stage_path = stage.get("path", "")
            stage_status = stage.get("status", "")
            stage_note = stage.get("note") or ""
            stage_counts[stage_status] = stage_counts.get(stage_status, 0) + 1
            chain_rows.append(
                f"<tr>"
                f'<td style="padding:4px 8px;white-space:nowrap">{_status_badge(stage_status)}</td>'
                f'<td style="padding:4px 8px;font-weight:600;'
                f'color:#0E4D8C;white-space:nowrap">{_esc(stage_name)}</td>'
                f'<td style="padding:4px 8px;font-family:ui-monospace,monospace;'
                f'font-size:11px;color:#333;word-break:break-all">{_esc(stage_path)}</td>'
                f'<td style="padding:4px 8px;font-size:12px;'
                f'color:#666;font-style:italic">{_esc(stage_note)}</td>'
                f"</tr>"
            )

        rows.append(
            f"<tr>"
            f'<td style="padding:8px 10px">'
            f"<details>"
            f'<summary style="cursor:pointer;list-style:none">'
            f"{packaged_cell}"
            f"</summary>"
            f'<table style="width:100%;margin-top:8px;border-collapse:collapse;'
            f'background:#FAFBFC;border:1px solid #E5E7EB;border-radius:4px">'
            f'<thead><tr style="background:#F0F2F5">'
            f'<th style="padding:6px 8px;text-align:left;font-size:11px;'
            f'color:#666">Status</th>'
            f'<th style="padding:6px 8px;text-align:left;font-size:11px;'
            f'color:#666">Stage</th>'
            f'<th style="padding:6px 8px;text-align:left;font-size:11px;'
            f'color:#666">Path after stage</th>'
            f'<th style="padding:6px 8px;text-align:left;font-size:11px;'
            f'color:#666">Note</th>'
            f"</tr></thead><tbody>" + "".join(chain_rows) + f"</tbody></table>"
            f"</details>"
            f"</td>"
            f'<td style="padding:8px 10px;font-family:ui-monospace,monospace;'
            f'font-size:12px;color:#444;word-break:break-all">'
            f"{_esc(source_path)}"
            f"</td>"
            f"</tr>"
        )

    generated_at = doc.get("generated_at", "")
    schema_version = doc.get("version", "?")
    summary_parts = [
        f"<strong>{len(entries)}</strong> files",
    ]
    for status_key in ("applied", "no_op", "skipped", "failed"):
        n = stage_counts.get(status_key, 0)
        if n:
            summary_parts.append(
                f"{_status_badge(status_key)} <strong>{n}</strong> stage steps"
            )
    summary = " &middot; ".join(summary_parts)

    return (
        '<div style="margin-bottom:12px;padding:10px 14px;background:#F0F4F8;'
        'border-left:3px solid #0E4D8C;border-radius:3px;font-size:13px">'
        f"{summary}"
        f'<div style="margin-top:4px;color:#666;font-size:11px">'
        f"Generated {_esc(generated_at)} &middot; schema v{_esc(schema_version)} &middot; "
        f"source: <code>context/ships.provenance.json</code>"
        f"</div>"
        f"</div>"
        '<p style="color:#666;font-size:12px;margin:0 0 10px 0">'
        "Click any packaged path to expand its full transformation chain "
        "(harvest_source &rarr; source &rarr; eponymous &rarr; "
        "token_resolved &rarr; package). The harvest_source row is only "
        "shown when the package was built from a harvested source tree."
        "</p>"
        '<table style="width:100%;border-collapse:collapse;'
        'background:white;border:1px solid #E5E7EB">'
        '<thead><tr style="background:#F8F9FA">'
        '<th style="padding:8px 10px;text-align:left;font-size:12px;'
        'color:#444;border-bottom:1px solid #E5E7EB">Packaged path '
        '<span style="color:#999;font-weight:400;font-size:11px">'
        "(click to expand chain)</span></th>"
        '<th style="padding:8px 10px;text-align:left;font-size:12px;'
        'color:#444;border-bottom:1px solid #E5E7EB">Source path</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _load_build_provenance(pkg_dir: str) -> List[dict]:
    """Load stage results from ships.decisions.json for the Build Provenance tab.

    Walks up from ``pkg_dir`` looking for ``ships.decisions.json`` using the
    same ancestor-search strategy as trust loading, so the function works
    whether the package directory is the project root or a subdirectory.

    Only the **most recent run** is used — older runs are historical noise
    that would clutter the tab.  Within that run, stages are returned in
    chronological order so the timeline reads top-to-bottom in the order
    the developer executed them.

    Args:
        pkg_dir: Package root directory passed to ``generate_package_report``.

    Returns:
        List of stage dicts from the latest run, or an empty list when
        ships.decisions.json is absent, unreadable, or contains no runs.
    """
    import json as _json

    from td_release_packager.project_paths import decisions_json_path

    candidate = os.path.abspath(pkg_dir)
    for _ in range(6):  # current dir + up to 5 ancestors
        decisions_path = decisions_json_path(candidate)
        if os.path.isfile(decisions_path):
            try:
                with open(decisions_path, encoding="utf-8") as fh:
                    data = _json.load(fh)
                runs = data.get("runs", [])
                if runs:
                    return runs[-1].get("stages", [])
            except Exception:
                pass
            return []
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return []


def _build_invocation_fallback(inv: dict) -> str:
    """Render the build-invocation snapshot (#397) as the Build Provenance
    fallback shown when the project-side ``ships.decisions.json`` is not
    reachable (e.g. a distributed/extracted package).

    Args:
        inv: The ``build_invocation`` block from ``ships.build.json``
             (command, args, cwd, env_config, timestamp, ships_version,
             python_version). Args are already redacted at capture time.

    Returns:
        HTML string for use inside the Build Provenance card.
    """
    command = inv.get("command", "")
    args = inv.get("args", []) or []
    # Reconstruct the command line for display; args are pre-redacted.
    cmd_line = " ".join([str(command)] + [str(a) for a in args]).strip()

    def _row(label: str, value: object) -> str:
        if value in (None, ""):
            return ""
        return (
            '<tr><td style="padding:6px 16px 6px 0;color:#6C757D;'
            'white-space:nowrap;vertical-align:top">'
            f"{_h(label)}</td>"
            f'<td style="padding:6px 0"><code>{_h(value)}</code></td></tr>'
        )

    rows = "".join(
        [
            _row("Working directory", inv.get("cwd")),
            _row("Env config", inv.get("env_config")),
            _row("Timestamp", inv.get("timestamp")),
            _row("SHIPS version", inv.get("ships_version")),
            _row("Python version", inv.get("python_version")),
        ]
    )

    return (
        '<div style="padding:16px">'
        '<p style="color:#6C757D;margin:0 0 12px">'
        "Pipeline stage history (<code>ships.decisions.json</code>) is not "
        "available in this package — showing the recorded build invocation "
        "from <code>ships.build.json</code> instead. Secret values are redacted."
        "</p>"
        '<pre style="background:#F8F9FA;border:1px solid #E9ECEF;border-radius:6px;'
        'padding:12px;overflow-x:auto;font-size:13px"><code>'
        f"{_h(cmd_line)}</code></pre>"
        '<table style="border-collapse:collapse;font-size:13px;margin-top:8px">'
        f"{rows}</table>"
        "</div>"
    )


def _build_provenance_tab(
    stages: List[dict], build_invocation: Optional[dict] = None
) -> str:
    """Render the Build Provenance tab from a list of pipeline stage dicts.

    Produces a compact timeline — one row per stage — showing the stage name,
    pass/fail status badge, duration, and key output metrics extracted from
    the stage's ``outputs`` dict.  Each row is clickable to expand a detail
    panel showing any issues recorded during that stage.

    When ``stages`` is empty (the project-side ``ships.decisions.json`` is not
    reachable — the normal case for a distributed package), falls back to the
    ``build_invocation`` snapshot carried inside ``ships.build.json`` (#397),
    so "what command + args built this?" stays answerable. Only when neither
    source is available does the "not available" placeholder render.

    Args:
        stages:           Stage dicts from ``_load_build_provenance``.
        build_invocation: The ``build_invocation`` block from the package
                          manifest, used as the post-distribution fallback.

    Returns:
        HTML string for use inside the Build Provenance ``<div class="card">``.
    """
    if not stages:
        if build_invocation:
            return _build_invocation_fallback(build_invocation)
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "Build provenance not available — "
            "<code>ships.decisions.json</code> was not found in the project "
            "directory tree, and no build invocation was recorded in "
            "<code>ships.build.json</code>. Run the full pipeline (harvest → "
            "inspect → scan → analyse → package) from the project root to "
            "populate it."
            "</p>"
        )

    # -- Status styling --
    _STATUS_ICON = {
        "success": "✔",
        "warning": "⚠",
        "error": "✗",
        "skipped": "○",
        "no-op": "—",
    }
    _STATUS_BG = {
        "success": "#D4EDDA",
        "warning": "#FFF3CD",
        "error": "#F8D7DA",
        "skipped": "#E9ECEF",
        "no-op": "#E9ECEF",
    }
    _STATUS_FG = {
        "success": "#155724",
        "warning": "#856404",
        "error": "#721C24",
        "skipped": "#495057",
        "no-op": "#495057",
    }

    # -- Key metrics to surface per stage (label, outputs key) --
    # Each entry is a (display_label, output_key) pair.  The first key that
    # exists in the stage's outputs dict is rendered; the rest are skipped.
    # This keeps the row compact and avoids showing zeros for metrics that
    # were never set by that stage.
    _STAGE_METRICS: dict[str, list[tuple[str, str]]] = {
        "harvest": [
            ("classified", "classified"),
            ("unclassified", "unclassified"),
            ("files placed", "files_placed"),
            ("MULTISET injected", "multiset_injected"),
            ("cleaned", "cleaned"),
        ],
        "inspect": [
            ("files scanned", "files_scanned"),  # from inputs
            ("lint errors", "lint_errors"),
            ("lint warnings", "lint_warnings"),
            ("files with issues", "files_with_issues"),
        ],
        "scan": [
            ("unique tokens", "unique_tokens"),
            ("files with tokens", "files_with_tokens"),  # from inputs
        ],
        "analyse": [
            ("objects", "object_count"),
            ("waves", "wave_count"),
            ("dependencies", "dependency_count"),
            ("cycles", "cycle_count"),
        ],
        "package": [
            ("files", "file_count"),
            ("tokens substituted", "token_count"),
        ],
    }

    def _fmt_duration(ms: int) -> str:
        """Format a millisecond duration as a human-readable string."""
        if ms < 1000:
            return f"{ms} ms"
        if ms < 60_000:
            return f"{ms / 1000:.1f} s"
        return f"{ms / 60_000:.1f} min"

    def _stage_metrics_html(stage: dict) -> str:
        """Return a short comma-separated metric string for one stage."""
        name = (stage.get("stage") or "").lower()
        metric_defs = _STAGE_METRICS.get(name, [])
        outputs = stage.get("outputs", {})
        inputs = stage.get("inputs", {})
        combined = {**inputs, **outputs}  # outputs win on collision

        parts = []
        for label, key in metric_defs:
            val = combined.get(key)
            if val is None:
                continue
            # Don't show zero-value noise metrics
            if (
                isinstance(val, (int, float))
                and val == 0
                and key
                in (
                    "unclassified",
                    "lint_errors",
                    "lint_warnings",
                    "files_with_issues",
                    "cycle_count",
                    "cleaned",
                    "multiset_injected",
                )
            ):
                continue
            parts.append(f"<strong>{_h(str(val))}</strong> {_h(label)}")
        return "  ·  ".join(parts) if parts else ""

    def _issues_html(stage: dict) -> str:
        """Render the issues list for one stage's detail panel.

        Each code span carries a ``title`` attribute with the human
        description from :data:`issue_codes.ISSUE_CODES`, so a reader
        can hover any code to see what it means.
        """
        from td_release_packager.orchestrator.issue_codes import describe

        issues = stage.get("issues", []) or []
        if not issues:
            return (
                '<p style="color:#28A745;font-size:13px;margin:0">'
                "No issues recorded.</p>"
            )
        _SEV_COLOUR = {"error": "#DC3545", "warning": "#856404", "info": "#0D6EFD"}
        _SEV_ICON = {"error": "✗", "warning": "⚠", "info": "ℹ"}
        rows = []
        for issue in issues:
            sev = str(issue.get("severity", "info")).lower()
            colour = _SEV_COLOUR.get(sev, "#555")
            icon = _SEV_ICON.get(sev, "·")
            raw_code = str(issue.get("code", ""))
            code = _h(raw_code)
            tooltip = describe(raw_code) if raw_code else ""
            title_attr = (
                f' title="{_h(tooltip)}"'
                if tooltip and tooltip != "(unregistered code)"
                else ""
            )
            msg = _h(str(issue.get("message", "")))
            loc = issue.get("location", "")
            loc_html = (
                f'<div style="font-size:11px;color:#6C757D;margin-top:2px">'
                f"{_h(str(loc))}</div>"
                if loc
                else ""
            )
            rows.append(
                f'<div style="padding:5px 0;border-bottom:1px solid #F0F0F0">'
                f'<span style="color:{colour};font-weight:700;margin-right:6px">'
                f"{icon}</span>"
                f'<span style="font-family:monospace;font-size:12px;'
                f"color:{colour};margin-right:8px;cursor:help;"
                f'border-bottom:1px dotted {colour}"{title_attr}>{code}</span>'
                f'<span style="font-size:12px;color:#333">{msg}</span>'
                f"{loc_html}"
                f"</div>"
            )
        return "".join(rows)

    # -- Build the rows --
    rows_html = ""
    for idx, stage in enumerate(stages):
        name = stage.get("stage", "unknown")
        status = str(stage.get("status", "success")).lower()
        duration_ms = int(stage.get("duration_ms") or 0)
        started = str(stage.get("started_at") or "")
        # Trim to HH:MM:SS if it's a full ISO timestamp
        time_label = started[11:19] if len(started) >= 19 else started

        icon = _STATUS_ICON.get(status, "?")
        badge_bg = _STATUS_BG.get(status, "#E9ECEF")
        badge_fg = _STATUS_FG.get(status, "#333")
        issue_counts = stage.get(
            "issue_counts",
            {
                "error": sum(
                    1
                    for i in (stage.get("issues") or [])
                    if str(i.get("severity", "")).lower() == "error"
                ),
                "warning": sum(
                    1
                    for i in (stage.get("issues") or [])
                    if str(i.get("severity", "")).lower() == "warning"
                ),
            },
        )
        n_errors = issue_counts.get("error", 0)
        n_warnings = issue_counts.get("warning", 0)
        n_issues = n_errors + n_warnings

        # Issue count badge shown in the row when non-zero
        issue_badge = ""
        if n_errors:
            issue_badge += (
                f'<span style="background:#F8D7DA;color:#721C24;'
                f"font-size:11px;font-weight:700;padding:1px 7px;"
                f'border-radius:10px;margin-left:8px">'
                f"{n_errors} error{'s' if n_errors != 1 else ''}</span>"
            )
        if n_warnings:
            issue_badge += (
                f'<span style="background:#FFF3CD;color:#856404;'
                f"font-size:11px;font-weight:700;padding:1px 7px;"
                f'border-radius:10px;margin-left:4px">'
                f"{n_warnings} warning{'s' if n_warnings != 1 else ''}</span>"
            )

        metrics = _stage_metrics_html(stage)
        metrics_html = (
            f'<div style="font-size:12px;color:#666;margin-top:3px">{metrics}</div>'
            if metrics
            else ""
        )

        detail_panel = (
            f'<div style="background:#F8F9FA;border-top:1px solid #DEE2E6;'
            f'padding:12px 16px 10px 16px;font-size:13px">'
            f"{_issues_html(stage)}"
            f"</div>"
        )

        # Expand the detail panel automatically when the stage has errors
        open_attr = " open" if n_errors else ""
        row_bg = "#FFF8F8" if n_errors else ("#FFFDF0" if n_warnings else "#FFF")
        border_left = (
            f"4px solid #DC3545"
            if n_errors
            else (f"4px solid #FFC107" if n_warnings else f"4px solid #28A745")
        )

        rows_html += f"""
<details{open_attr} style="border-left:{border_left};background:{row_bg};
  margin-bottom:4px;border-radius:0 4px 4px 0">
<summary style="list-style:none;display:flex;align-items:center;
  gap:10px;padding:10px 14px;cursor:pointer;user-select:none">
  <span style="background:{badge_bg};color:{badge_fg};font-weight:700;
    font-size:13px;padding:2px 10px;border-radius:10px;min-width:80px;
    text-align:center">{icon} {_h(name)}</span>
  <span style="font-size:12px;color:#555">{_fmt_duration(duration_ms)}</span>
  {f'<span style="font-size:11px;color:#888">{_h(time_label)}</span>' if time_label else ""}
  {issue_badge}
  <span style="margin-left:auto;font-size:11px;color:#AAA">
    {"click to collapse" if n_errors else "click to expand"}
  </span>
</summary>
{metrics_html and f'<div style="padding:0 14px 8px 14px">{metrics_html}</div>' or ""}
{detail_panel}
</details>"""

    return f"""
<style>
  #build-provenance details > summary {{ list-style:none; }}
  #build-provenance details > summary::-webkit-details-marker {{ display:none; }}
</style>
<div id="build-provenance">
  <p style="font-size:13px;color:#555;margin-bottom:16px">
    Pipeline steps recorded in <code>ships.decisions.json</code> for the most
    recent run. Steps with errors open automatically.
    Click any row to expand or collapse its issue detail.
  </p>
  {rows_html}
</div>"""


def _guide_tab(manifest_dict: dict, records: List[Dict]) -> str:
    """Reader's Guide tab — oriented towards first-time DBAs and reviewers.

    Explains what a SHIPS package is, how it was built, what the deployment
    phases mean, what to do with it, and provides a glossary of every term
    used elsewhere in the report.  All content is static prose plus a few
    dynamic values (build number, environment, phase list, wave count) drawn
    from the manifest and scanned records.

    Args:
        manifest_dict: The ``BuildManifest.__dict__`` for this package.
        records:       Payload records from ``_scan_payload``.

    Returns:
        HTML string for use inside the Guide ``<div class="card">``.
    """
    build_no = _h(str(manifest_dict.get("build_number", "?")))
    env = _h(str(manifest_dict.get("environment", "?")))
    role = str(manifest_dict.get("role", "")).lower()
    file_count = manifest_dict.get("file_count", len(records))
    report_label = _package_report_label(manifest_dict)

    # Describe the package role in plain English
    if role == "prereqs" or role == "environment_prereqs":
        role_description = (
            "pre-requisites package — it creates the database containers and "
            "roles that the main package depends on. Deploy this package "
            "<strong>before</strong> the main package."
        )
    else:
        role_description = (
            "main package — it contains the data model objects and grants "
            "that make up the deployed solution."
        )

    # Identify which phases are present and how many DDL waves there are
    phase_set: Dict[str, bool] = {}
    for r in records:
        phase_set[r["phase"]] = True

    # Canonical phase display order
    _PHASE_ORDER = ["System", "Pre-requisites", "DCL", "DDL", "DML", "Post-install"]
    present_phases = [p for p in _PHASE_ORDER if p in phase_set]
    # Any phases not in the canonical order go at the end
    for p in phase_set:
        if p not in present_phases:
            present_phases.append(p)

    phase_count = len(present_phases)
    phases_str = ", ".join(f"<strong>{_h(p)}</strong>" for p in present_phases)

    wave_nums = sorted({r["wave"] for r in records if r.get("wave") is not None})
    wave_count = len(wave_nums)

    # ── Phase cards (only for phases actually present) ──────────────────
    _PHASE_META = {
        "System": (
            "🔧",
            "System — System-scope objects",
            "Creates system-level objects (Roles, Profiles, Maps, Authorisations, "
            "Foreign Servers) that are shared across all environments on this "
            "Teradata system. These always deploy first and use SKIP IF EXISTS "
            "semantics — they are never dropped or re-created.",
        ),
        "Pre-requisites": (
            "🏛",
            "Pre-requisites — Database containers",
            "Creates the database and user containers that all subsequent objects "
            "live inside. These must exist before any table, view, or grant can be "
            "deployed. Contained in the companion pre-requisites package when "
            "auto-split is used.",
        ),
        "DCL": (
            "🔑",
            "DCL — Data Control Language",
            "Grants and revokes privileges so that roles and users can access the "
            "objects being deployed.",
        ),
        "DDL": (
            "🏗️",
            "DDL — Data Definition Language",
            "Creates or replaces tables, views, macros, procedures, and functions — "
            "the structural objects of the data model.",
        ),
        "DML": (
            "📥",
            "DML — Data Manipulation Language",
            "Runs INSERT, MERGE, or DELETE statements to seed or transform data "
            "after the structure is in place.",
        ),
        "Post-install": (
            "✅",
            "Post-install — Post-deployment steps",
            "Runs after all DDL and DML. Typically collects statistics, validates "
            "data counts, or executes smoke-test macros.",
        ),
    }

    def _phase_card(phase: str, is_last: bool) -> str:
        meta = _PHASE_META.get(phase)
        if not meta:
            icon, title, desc = "📄", _h(phase), ""
        else:
            icon, title, desc = meta[0], meta[1], meta[2]
        arrow = (
            ""
            if is_last
            else '<div style="display:flex;align-items:center;color:#aaa;'
            'font-size:18px;padding:0 4px;align-self:center">›</div>'
        )
        return (
            f'<div style="display:flex;align-items:stretch;gap:0">'
            f'<div style="background:#f0f4f8;border:1px solid {_BORDER};border-radius:8px;'
            f'padding:14px 16px;min-width:130px;max-width:170px;flex:1">'
            f'<div style="font-size:22px;margin-bottom:6px">{icon}</div>'
            f'<div style="font-size:12px;font-weight:700;color:{_NAVY};margin-bottom:4px">'
            f"{title}</div>"
            f'<div style="font-size:11px;color:#555;line-height:1.4">{desc}</div>'
            f"</div>"
            f"{arrow}"
            f"</div>"
        )

    phase_cards = "".join(
        _phase_card(p, i == len(present_phases) - 1)
        for i, p in enumerate(present_phases)
    )

    wave_sentence = (
        f" The DDL phase is further divided into "
        f"<strong>{wave_count} "
        f'<span data-tip="Objects in the same wave have no dependencies on each '
        f'other and can be deployed in parallel, reducing total deployment time.">'
        f"waves</span></strong>. Each wave deploys in parallel; waves are sequenced "
        f"so that no object is created before the objects it depends on."
        if wave_count > 0
        else ""
    )

    from td_release_packager.reporting.common import render_issue_code_glossary

    issue_code_glossary = render_issue_code_glossary()

    return f"""
<div class="guide-hero">
  <div class="guide-hero-text">
    <h2>SHIPS Package Report — Reader's Guide</h2>
    <p>This is a
      <span data-tip="{"The main package contains the core data objects: tables, views, procedures, grants, and DML. It is deployed after the pre-requisites package." if "main" in role_description else "The pre-requisites package creates the database containers and roles that the main package depends on. Deploy this first."}">{role_description}</span>
      &nbsp; This report was generated automatically at build time
      to give you full visibility of what is inside the package before you
      deploy it. Build <strong>{build_no}</strong> &nbsp;·&nbsp;
      Target environment: <strong>{env}</strong> &nbsp;·&nbsp;
      <strong>{file_count}</strong> objects across
      <strong>{phase_count}</strong> {"phase" if phase_count == 1 else "phases"}.</p>
  </div>
</div>

<p class="guide-section-title">How this package was built</p>
<p style="font-size:13px;color:#555;margin-bottom:16px;line-height:1.6">
  SHIPS (<span data-tip="Scaffold → Harvest → Inspect → Package → Ship. The full pipeline
  that turns source SQL files into a validated, versioned deployment archive.">Scaffold →
  Harvest → Inspect → Package → Ship</span>) assembled this package from your project's
  source SQL files. Each file was classified by its
  <span data-tip="The top-level SQL verb in the script: CREATE, GRANT, INSERT, etc.
  SHIPS uses this to place the file in the correct phase and validate it.">script intent</span>,
  assigned to the correct
  <span data-tip="A logical group of scripts by purpose. Phases always execute in order:
  System → Pre-requisites → DCL → DDL → DML → Post-install.">deployment phase</span>,
  and — within DDL — placed into a
  <span data-tip="A wave is a group of objects with no mutual dependencies. Objects in
  the same wave can be deployed in parallel. Waves are computed automatically by analysing
  foreign-key and view dependencies.">wave</span>
  based on its dependencies. The
  <span data-tip="A set of automated checks that verify the package is complete, correctly
  structured, and safe to deploy before it leaves the build pipeline.">Trust Report</span>
  then validated the result before the package was sealed.
</p>

<div style="overflow-x:auto;padding-bottom:8px">
  <div style="display:inline-flex;gap:8px;align-items:stretch;min-width:max-content;padding:4px 0">
    <div style="background:{_NAVY};color:{_WHITE};border-radius:8px;padding:14px 16px;
                min-width:110px;display:flex;flex-direction:column;justify-content:center;
                text-align:center">
      <div style="font-size:20px;margin-bottom:4px">📁</div>
      <div style="font-size:11px;font-weight:700">Source SQL<br>files</div>
    </div>
    <div style="display:flex;align-items:center;color:{_ORANGE};font-size:22px;font-weight:700">›</div>
    <div style="background:{_ORANGE};color:{_WHITE};border-radius:8px;padding:14px 16px;
                min-width:110px;display:flex;flex-direction:column;justify-content:center;
                text-align:center">
      <div style="font-size:20px;margin-bottom:4px">⚙️</div>
      <div style="font-size:11px;font-weight:700">SHIPS<br>pipeline</div>
    </div>
    <div style="display:flex;align-items:center;color:{_ORANGE};font-size:22px;font-weight:700">›</div>
    <div style="background:#f0f4f8;border:1px solid {_BORDER};border-radius:8px;
                padding:14px 16px;min-width:110px;display:flex;flex-direction:column;
                justify-content:center;text-align:center">
      <div style="font-size:20px;margin-bottom:4px">✅</div>
      <div style="font-size:11px;font-weight:700;color:{_NAVY}">Trust<br>check</div>
    </div>
    <div style="display:flex;align-items:center;color:{_ORANGE};font-size:22px;font-weight:700">›</div>
    <div style="background:#f0f4f8;border:2px solid {_ORANGE};border-radius:8px;
                padding:14px 16px;min-width:110px;display:flex;flex-direction:column;
                justify-content:center;text-align:center">
      <div style="font-size:20px;margin-bottom:4px">📦</div>
      <div style="font-size:11px;font-weight:700;color:{_NAVY}">This<br>package</div>
    </div>
    <div style="display:flex;align-items:center;color:{_ORANGE};font-size:22px;font-weight:700">›</div>
    <div style="background:{_NAVY};color:{_WHITE};border-radius:8px;padding:14px 16px;
                min-width:110px;display:flex;flex-direction:column;justify-content:center;
                text-align:center">
      <div style="font-size:20px;margin-bottom:4px">🚀</div>
      <div style="font-size:11px;font-weight:700">Teradata<br>{env}</div>
    </div>
  </div>
</div>

<p class="guide-section-title">Deployment phase order</p>
<p style="font-size:13px;color:#555;margin-bottom:12px;line-height:1.6">
  Scripts always execute left-to-right through these phases. Each phase must
  complete successfully before the next begins.
  This package contains: {phases_str}.{wave_sentence}
</p>
<div style="overflow-x:auto;padding-bottom:8px">
  <div style="display:inline-flex;gap:8px;align-items:stretch;min-width:max-content;padding:4px 0">
    {phase_cards}
  </div>
</div>

<p class="guide-section-title">What do you do with this package?</p>

<div class="guide-steps">
  <div class="guide-step">
    <div class="guide-step-num">1</div>
    <h4>Review this report</h4>
    <p>Check the <strong>Summary</strong> tab to understand what types of objects
       are included. Check the <strong>Trust Report</strong> tab — the package
       should show <em>READY</em> before deployment.</p>
  </div>
  <div class="guide-step">
    <div class="guide-step-num">2</div>
    <h4>Run a dry run</h4>
    <p>Go to the <strong>Deploy</strong> tab and copy the
       <em>Dry run</em> command. This validates the pipeline and runs pre-flight
       checks without executing any DDL on the target system.</p>
  </div>
  <div class="guide-step">
    <div class="guide-step-num">3</div>
    <h4>Explain the plan</h4>
    <p>Run the <em>Explain deployment plan</em> command to print the full
       wave-by-wave execution order. Verify the object sequence looks correct
       before committing to a real deployment.</p>
  </div>
  <div class="guide-step">
    <div class="guide-step-num">4</div>
    <h4>Deploy</h4>
    <p>For production use, run <code>deploy_release.py</code> from the
       release group directory — it sequences all packages automatically.
       For single-package or troubleshooting use, run <code>deploy.py</code>
       from inside the extracted package directory.</p>
  </div>
  <div class="guide-step">
    <div class="guide-step-num">5</div>
    <h4>Verify</h4>
    <p>After deployment, confirm objects exist in the target Teradata system
       and that any expected data has been loaded. Check application connectivity
       if this package updates existing objects.</p>
  </div>
</div>

<p class="guide-section-title">Glossary — terms used in this report</p>
<p style="font-size:13px;color:#555;margin-bottom:12px">
  Hover over any <span data-tip="Like this — hover text gives you the definition
  inline without leaving the page.">underlined term</span> anywhere in this
  report for a quick definition. The full glossary is below.
</p>
<div class="guide-glossary">
<div class="guide-glossary-item">
  <dt>SHIPS</dt>
  <dd>Scaffold → Harvest → Inspect → Package → Ship. The Teradata structured deployment pipeline that builds, validates, and packages DDL/DCL/DML for repeatable deployment across environments.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Package</dt>
  <dd>A versioned, self-contained zip archive produced by SHIPS containing all scripts needed to deploy or update a Teradata data product. Named as &lt;product&gt;_&lt;build&gt;_&lt;env&gt;.zip.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Build number</dt>
  <dd>A monotonically increasing integer that uniquely identifies this run of the packaging pipeline. Higher = newer.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Environment</dt>
  <dd>The target Teradata system this package was built for (e.g. DEV, TEST, PROD). Packages are environment-specific — do not deploy a DEV package to PROD.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Payload</dt>
  <dd>The folder inside the package containing all the SQL scripts, organised into phase subdirectories.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Phase</dt>
  <dd>A logical grouping of scripts by purpose: System, Pre-requisites, DCL, DDL, DML, and Post-install. Phases always execute in this order.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Wave</dt>
  <dd>A group of DDL objects within a phase that have no dependencies on each other and can be deployed in parallel. Computed automatically by <code>ships analyse</code>.</dd>
</div>
<div class="guide-glossary-item">
  <dt>DDL — Data Definition Language</dt>
  <dd>SQL statements that define or alter the structure of database objects: CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, DROP TABLE, etc.</dd>
</div>
<div class="guide-glossary-item">
  <dt>DCL — Data Control Language</dt>
  <dd>SQL statements that control access to objects: GRANT and REVOKE.</dd>
</div>
<div class="guide-glossary-item">
  <dt>DML — Data Manipulation Language</dt>
  <dd>SQL statements that manipulate data within objects: INSERT, UPDATE, DELETE, MERGE.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Trust Report</dt>
  <dd>A set of automated checks (signals) that assess whether the package is safe to deploy. A package must be READY or READY_WITH_CAVEATS before deployment.</dd>
</div>
<div class="guide-glossary-item">
  <dt>READY</dt>
  <dd>All trust signals passed. The package is safe to deploy as-is.</dd>
</div>
<div class="guide-glossary-item">
  <dt>READY_WITH_CAVEATS</dt>
  <dd>The package can be deployed but one or more non-blocking signals raised warnings. Review the Trust Report tab before proceeding.</dd>
</div>
<div class="guide-glossary-item">
  <dt>BLOCKED</dt>
  <dd>One or more trust signals failed. The package must not be deployed until the issues listed in the Trust Report are resolved.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Dry run</dt>
  <dd>Executing the deploy script with <code>--dry-run</code>: validates the pipeline and runs pre-flight checks without connecting to Teradata or executing any DDL.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Explain</dt>
  <dd>Executing the deploy script with <code>--explain</code>: prints the full wave-by-wave execution plan so you can verify object order before committing to a real deployment.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Wave-parallel deployment</dt>
  <dd>Using <code>--streams N</code> to deploy objects within a wave concurrently, reducing total deployment time for large packages.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Pre-requisites package</dt>
  <dd>A package whose role is "prereqs": it creates the database containers and roles that must exist before any main package can be deployed. Always deploy this first.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Serial column</dt>
  <dd>Objects in the Waves diagram labelled "Serial" run before any wave and cannot be parallelised — typically system-level objects like roles and profiles.</dd>
</div>
<div class="guide-glossary-item">
  <dt>deploy_release.py</dt>
  <dd>A thin launcher script in the release group directory. It invokes <code>td_release_packager deploy</code> on the whole group, deploying every package in the correct order automatically. Preferred for production deployments.</dd>
</div>
<div class="guide-glossary-item">
  <dt>deploy.py</dt>
  <dd>A single-package deployer script found inside each extracted package directory. Use it for dry runs, explain plans, troubleshooting, or deploying one package in isolation.</dd>
</div>
<div class="guide-glossary-item">
  <dt>Release group</dt>
  <dd>A directory containing all packages that make up a release — typically a pre-requisites package and one or more main packages. <code>deploy_release.py</code> lives here and orchestrates the whole group.</dd>
</div>
{issue_code_glossary}
</div>
"""


def _signal_name_cell(name: str) -> str:
    """Render a trust signal name as a collapsible explanation block.

    Delegates to :func:`report_viewer.signal_name_cell` using this
    module's brand colour constants so the package report and the deploy
    report share identical signal explanations from a single source of truth.

    Args:
        name: The signal key, e.g. ``"inspect_lint"``.

    Returns:
        An HTML string for use inside a ``<td>`` element.
    """
    return _signal_name_cell_shared(name, navy=_NAVY, orange=_ORANGE)


def _trust_tab(trust: dict) -> str:
    """Trust Report signals table with expandable signal explanations."""
    label = trust.get("status", "UNKNOWN")
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
        icon = _trust_icon(status)

        # Build detail cell — always show message; for non-pass signals
        # also list any specific issues so the operator knows what to fix.
        if isinstance(sig, dict):
            # Trust signals serialize with 'message'; older packages may use 'detail'
            message = sig.get("message") or sig.get("detail", "")
            issues = sig.get("issues", [])
            if status == TRUST_PASS:
                detail = f"<span style='color:#555'>{message}</span>"
            else:
                # Non-pass: highlight the message and list each issue
                colour = "#B45309" if status == TRUST_WARN else "#991B1B"
                detail = (
                    f"<span style='color:{colour};font-weight:600'>{message}</span>"
                )
                if issues:
                    issue_items = "".join(
                        f"<li style='margin-top:4px'>{i}</li>" for i in issues
                    )
                    detail += (
                        f"<ul style='margin:6px 0 0 0;padding-left:18px;"
                        f"color:{colour};font-size:12px'>{issue_items}</ul>"
                    )
        else:
            detail = ""

        rows += (
            f"<tr>"
            f"<td style='padding:10px 12px;vertical-align:top'>{_signal_name_cell(name)}</td>"
            f"<td style='padding:10px 12px'>{icon} {status}</td>"
            f"<td style='padding:10px 12px;font-size:13px'>{detail}</td>"
            "</tr>"
        )

    if not rows:
        rows = '<tr><td colspan="3" style="padding:16px;color:#6C757D">No signals recorded.</td></tr>'

    return f"""
<style>
/* Hide the native details marker — we supply our own triangle span. */
#trust-signals-table details > summary {{ list-style: none; }}
#trust-signals-table details > summary::-webkit-details-marker {{ display: none; }}
/* Rotate our triangle when the details element is open. */
#trust-signals-table details[open] > summary > span:first-child {{ transform: rotate(90deg); display: inline-block; }}
</style>
{label_html}
<table id="trust-signals-table" style="width:100%;border-collapse:collapse;font-size:14px">
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
    release_group = str(manifest_dict.get("release_group") or "").strip()
    package_filename = str(manifest_dict.get("package_filename") or "").strip()
    package_dir = package_filename.rsplit(".", 1)[0] if package_filename else pkg_name

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

    def release_group_panel() -> str:
        """Render release-group deployment guidance for paired packages."""
        if not requires and not release_group:
            return ""

        package_list = ""
        if requires:
            items = "\n".join(f"<li><code>{_h(name)}</code></li>" for name in requires)
            package_list = f"""
<div style="background:#E7F1FF;border:1px solid #9EC5FE;border-radius:6px;
  padding:14px 18px;margin-top:14px;color:{_NAVY}">
  <div style="font-size:16px;font-weight:700;margin-bottom:8px">
    Associated packages — deploy these together
  </div>
  <p style="font-size:13px;line-height:1.5;margin-bottom:8px">
    This package is part of a multi-package release. The following package(s)
    must be deployed to the same environment before this package will function correctly:
  </p>
  <ul style="margin-left:20px;font-size:13px;line-height:1.7">{items}</ul>
  <p style="font-size:13px;color:#555;margin-top:8px">
    Use <code>deploy_release.py</code> from the release group directory to deploy
    all packages in the correct order automatically.
  </p>
</div>"""

        return f"""
<div style="background:{_NAVY};color:{_WHITE};border-radius:6px;
  padding:22px 24px;margin-bottom:22px">
  <div style="font-size:12px;font-weight:700;letter-spacing:.08em;
    color:#8ba4be;text-transform:uppercase;margin-bottom:6px">
    Recommended — release group deployment
  </div>
  <div style="font-size:17px;font-weight:700;margin-bottom:10px">
    Use <code style="background:rgba(255,255,255,.12);color:{_WHITE};
      padding:2px 6px;border-radius:4px">deploy_release.py</code>
    for production deployments
  </div>
  <p style="font-size:14px;line-height:1.6;margin-bottom:12px;color:#D7E8F7">
    This package is part of a release group. <code style="background:rgba(255,255,255,.12);
    color:{_WHITE};padding:2px 5px;border-radius:4px">deploy_release.py</code>
    sits alongside all packages in the release group directory and deploys them
    in the correct sequence automatically: pre-requisites first, then main packages.
    You do not need to manage package order manually.
  </p>
  <p style="font-size:13px;color:#8ba4be;margin-bottom:8px">
    Run from the release group directory{
            f" ({_h(release_group)})" if release_group else ""
        }:
  </p>
  {
            cmd_block(
                "Release group deployment (recommended)",
                "python deploy_release.py --host &lt;host&gt; --user &lt;user&gt;",
                "Deploys all packages in this release group in dependency order.",
            )
        }
</div>
<div style="font-weight:700;margin-bottom:4px">Single-package commands</div>
<p style="font-size:13px;color:#666;margin-bottom:14px">
  Run these from inside the extracted package directory
  (<code>{_h(package_dir)}</code>). Use them for inspection, troubleshooting,
  or when deploying a single package in isolation.
</p>
{package_list}
"""

    blocks = ""
    blocks += release_group_panel()

    blocks += cmd_block(
        "Dry run (recommended first)",
        "python deploy.py --host &lt;host&gt; --user &lt;user&gt; --dry-run",
        "Validates the pipeline and runs pre-flight checks. No DDL is executed.",
    )
    blocks += cmd_block(
        "Explain deployment plan",
        "python deploy.py --host &lt;host&gt; --user &lt;user&gt; --explain",
        "Prints the full wave execution plan — objects, phases, and ordering — without connecting to the database.",
    )
    blocks += cmd_block(
        "Standard deployment",
        "python deploy.py --host &lt;host&gt; --user &lt;user&gt;",
        "Serial deployment. Safe for small packages.",
    )
    blocks += cmd_block(
        "Wave-parallel deployment",
        "python deploy.py --host &lt;host&gt; --user &lt;user&gt; --streams 4",
        "Deploys independent objects in parallel. Faster for large packages (50+ objects).",
    )
    blocks += cmd_block(
        "Continue on error (collect all failures in one pass)",
        "python deploy.py --host &lt;host&gt; --user &lt;user&gt; --continue-on-error",
    )

    blocks += _common.render_dbql_lookup_card(manifest_dict)

    return f"""
<div style="margin-bottom:16px;padding:12px 16px;background:{_LIGHT};
  border-radius:6px;font-size:14px">
  <strong>{pkg_name}</strong> &nbsp;|&nbsp; Build {build_no} &nbsp;|&nbsp; {env}
  &nbsp;&nbsp; — run the commands below from inside the extracted package directory
</div>
{blocks}
"""


def _environment_prereq_banner(manifest_dict: dict) -> str:
    """Return a prominent DBA action banner for environment prereq packages."""
    if str(manifest_dict.get("role") or "") != "environment_prereqs":
        return ""
    package_filename = str(manifest_dict.get("package_filename") or "<package>.zip")
    # The archive's internal root drops the build-id so extraction into
    # a nested ``.ships-work/`` folder stays under Windows MAX_PATH
    # (#395).  The extracted directory is named for the role only.
    extracted_dir = "00_environment_prereqs"
    return f"""
<div class="action-banner">
  <h2>ACTION REQUIRED — DBA REVIEW NEEDED</h2>
  <p>This <strong>_00_environment_prereqs</strong> package is blocked until DBA-approved parent and PERM values are supplied.</p>
  <p><strong>Do not edit:</strong> the source project <code>payload/database/pre-requisites</code>, the zip file directly, or the <strong>_01_prereqs</strong> package.</p>
  <p><strong>1. Extract this package zip:</strong> <code>{package_filename}</code> to a working folder such as <code>.ships-work/</code>.</p>
  <p><strong>2. Amend generated payload inside the extracted package:</strong> <code>.ships-work/{extracted_dir}/payload/01_pre_requisites/</code></p>
  <p><strong>3. Repackage the extracted package root:</strong> <code>python -m td_release_packager repackage --package-dir ".ships-work/{extracted_dir}" --strict</code></p>
  <p><strong>Full instructions:</strong> <code>.ships-work/{extracted_dir}/context/prerequisites/DBA_INSTRUCTIONS.md</code></p>
</div>
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Package-report-specific CSS appended after the shared chrome in
# reporting.common.BASE_CSS.  Only classes unique to this report live here;
# the page shell (header, meta bar, tabs, cards) comes from the shared
# renderer so the package, deploy, and pipeline reports stay visually aligned.
_PACKAGE_EXTRA_CSS = (
    f"""
.flt-btn {{ background: {_LIGHT}; border: 1px solid {_BORDER}; border-radius: 4px;
            padding: 4px 10px; font-size: 12px; cursor: pointer; }}
.flt-btn.active {{ background: {_NAVY}; color: {_WHITE}; border-color: {_NAVY}; }}
.flt-btn:hover {{ border-color: {_ORANGE}; }}
#obj-tbody tr {{ cursor: default; }}
#obj-tbody tr:hover {{ background: #e8f0fe !important; }}
#obj-tbody td {{ padding: 7px 12px; border-bottom: 1px solid #f0f0f0; }}
pre {{ white-space: pre-wrap; word-break: break-all; }}
.action-banner {{ background: #fff3cd; border: 2px solid #ffca2c; border-left: 8px solid #FF5F02;
                  border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }}
.action-banner h2 {{ color: #7a3b00; font-size: 18px; margin-bottom: 8px; }}
.action-banner p {{ margin: 6px 0; color: #3b2a00; }}
.action-banner code {{ background: rgba(255,255,255,.8); padding: 2px 5px; border-radius: 4px; }}
.summary-grid {{ display: grid; gap: 16px; align-items: start; }}
.summary-section {{ border: 1px solid {_BORDER}; border-radius: 6px; overflow: hidden; background: #fff; }}
.summary-section h3 {{ background: {_NAVY}; color: {_WHITE}; padding: 9px 12px; font-size: 14px; }}
.summary-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.summary-table td {{ padding: 8px 12px; border-bottom: 1px solid #f0f0f0; }}
.summary-table td:last-child {{ text-align: right; font-weight: 700; font-family: monospace; }}
/* Issue #277 — emphasise rows with count > 0, fade rows that are zero. */
.summary-table tr.has-count td {{ background: #F5F8FA; color: {_NAVY}; }}
.summary-table tr.has-count td:last-child {{ color: {_ORANGE}; }}
.summary-table tr.zero-count td {{ color: #ADB5BD; }}
.summary-flags {{ background: #fff3cd; border: 1px solid #ffca2c; border-left: 6px solid {_ORANGE};
                  padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }}
.summary-flags ul {{ margin: 8px 0 0 18px; color: #7a3b00; }}
"""
    + _common.GUIDE_CSS
)


def generate_package_report(pkg_dir: str, manifest_dict: dict) -> str:
    """Generate the interactive HTML package report and write it to ``pkg_dir``.

    Args:
        pkg_dir:       The package directory (not yet archived).
        manifest_dict: The ``BuildManifest.__dict__`` already written to
                       ships.build.json.

    Returns:
        Absolute path to the written report file.
    """
    records = _scan_payload(pkg_dir)
    from td_release_packager.trust import load_trust_result

    trust = load_trust_result(pkg_dir) or {}
    viewer_links = _write_package_viewers(pkg_dir, records)
    stages = _load_build_provenance(pkg_dir)
    content_provenance = _load_content_provenance(pkg_dir)

    pkg_name = manifest_dict.get("package_name", "Package")
    report_label = _package_report_label(manifest_dict)
    build_no = manifest_dict.get("build_number", "?")
    env = manifest_dict.get("environment", "?")
    file_count = manifest_dict.get("file_count", len(records))
    trust_label = trust.get("status", "")
    trust_bg, trust_fg = _trust_label_style(trust_label)

    # Meta-bar summary line
    type_counts: Dict[str, int] = {}
    for r in records:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
    summary_parts = [
        f"{v} {_common.pluralise(k.lower(), v)}" for k, v in sorted(type_counts.items())
    ]
    summary = ",  ".join(summary_parts[:6])
    if len(summary_parts) > 6:
        summary += f",  …and {len(summary_parts) - 6} more types"

    meta_html = (
        f"<span><strong>{file_count}</strong> objects</span>"
        f'<span style="color:{_BORDER}">|</span>'
        f'<span style="color:#777">{summary}</span>'
    )

    tabs = [
        _common.Tab("tab-guide", "📖 Guide", _guide_tab(manifest_dict, records), True),
        _common.Tab("tab-summary", "Summary", _summary_tab(records)),
        _common.Tab("tab-waves", "Waves", _waves_tab(records)),
        _common.Tab(
            "tab-objects", "Objects", _objects_tab(records, trust, viewer_links)
        ),
        _common.Tab(
            "tab-provenance",
            "Build Provenance",
            _build_provenance_tab(stages, manifest_dict.get("build_invocation")),
        ),
        _common.Tab(
            "tab-content-provenance",
            "Content Provenance",
            _content_provenance_tab(content_provenance, viewer_links),
        ),
        _common.Tab("tab-trust", "Trust Report", _trust_tab(trust)),
        _common.Tab("tab-deploy", "Deploy", _deploy_tab(manifest_dict)),
    ]

    html = _common.render_page(
        doc_title=f"SHIPS {report_label} — {pkg_name} {build_no}",
        header_title=f"{report_label} · {pkg_name}",
        header_sub=f"Build {build_no} · {env}",
        header_pill=_common.status_pill(trust_label or "—", trust_bg, trust_fg),
        meta_html=meta_html,
        tabs=tabs,
        content_prefix=_environment_prereq_banner(manifest_dict),
        extra_css=_PACKAGE_EXTRA_CSS,
        project_name=manifest_dict.get("project_name") or None,
        ships_version=manifest_dict.get("ships_version") or None,
        favicon_kind="package",
    )

    report_path = os.path.join(pkg_dir, "package_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("Package report: %s", report_path)
    return report_path
