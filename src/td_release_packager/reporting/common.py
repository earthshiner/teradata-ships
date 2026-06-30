"""
common.py — Shared HTML rendering chrome for SHIPS reports.

Every SHIPS report (the post-build package report, the post-deploy report,
and the pre-package pipeline report introduced in #324) needs the same
page shell: Teradata-branded header, meta bar, tabbed navigation, and a
consistent set of status helpers for the data recorded in
``ships.decisions.json``.

This module is the single home for that chrome so the visual identity is
defined once and no CSS/JavaScript is duplicated across report generators.
It is deliberately dependency-free — pure standard library — so any report
module can import it without pulling in the wider package.

Public API
----------
    Brand palette constants (NAVY, ORANGE, ...).
    h(value) / a(value)           HTML-escape text / attribute values.
    fmt_duration(ms)              Human-readable millisecond duration.
    stage_status_badge(status, …) Coloured status pill for a stage.
    render_issue_list(issues)     Issue detail block for a stage.
    render_issue_code_glossary()  Glossary block listing every registered
                                  ships.decisions.json issue code with its
                                  description, grouped by domain.
    Tab(id, label, body, active)  One tab definition.
    render_page(...)              Assemble a complete self-contained HTML doc.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Teradata brand palette
# ---------------------------------------------------------------------------

NAVY = "#00233C"
ORANGE = "#FF5F02"
WHITE = "#FFFFFF"
LIGHT = "#F8F9FA"
BORDER = "#DEE2E6"

# ---------------------------------------------------------------------------
# Stage / issue status styling
# ---------------------------------------------------------------------------
#
# Shared by the package report's Build Provenance tab and the pipeline
# report's Run timeline so a "warning" stage looks identical wherever it
# is shown.  Keys match the StageRecorder status vocabulary
# (success / warning / error / skipped / no-op).

STATUS_ICON: Dict[str, str] = {
    "success": "✔",
    "warning": "⚠",
    "error": "✗",
    "skipped": "○",
    "no-op": "—",
}
STATUS_BG: Dict[str, str] = {
    "success": "#D4EDDA",
    "warning": "#FFF3CD",
    "error": "#F8D7DA",
    "skipped": "#E9ECEF",
    "no-op": "#E9ECEF",
}
STATUS_FG: Dict[str, str] = {
    "success": "#155724",
    "warning": "#856404",
    "error": "#721C24",
    "skipped": "#495057",
    "no-op": "#495057",
}

_SEV_COLOUR = {"error": "#DC3545", "warning": "#856404", "info": "#0D6EFD"}
_SEV_ICON = {"error": "✗", "warning": "⚠", "info": "ℹ"}


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def h(value: object) -> str:
    """HTML-escape text content (leaves quotes intact for readability)."""
    return html.escape(str(value), quote=False)


def a(value: object) -> str:
    """HTML-escape an attribute value (escapes quotes as well)."""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


#: Irregular English plurals SHIPS report copy actually uses. Kept
#: small on purpose — extend only when a real report string surfaces a
#: noun that the naive ``+s`` rule botches (e.g. ``childs``).
_IRREGULAR_PLURALS: Dict[str, str] = {
    "child": "children",
    "person": "people",
    "index": "indices",
    "matrix": "matrices",
}


def pluralise(noun: str, count: int) -> str:
    """Return ``noun`` followed by a numerically-appropriate plural form.

    Handles three cases:

    * **Already-plural nouns** (``statistics``, ``analysis``-like
      collective forms ending in ``s``) are returned unchanged so a
      caller writing ``"3 statistics"`` doesn't get ``"3 statisticss"``.
    * **Irregular plurals** are looked up in :data:`_IRREGULAR_PLURALS`
      so ``child`` becomes ``children``, not ``childs``. Case of the
      caller's input is preserved on the suffix lookup; the table is
      keyed lower-case.
    * **Regular nouns** get an ``s`` when ``count != 1``.
    """
    if count == 1:
        return noun
    lower = noun.lower()
    if lower.endswith("s"):
        return noun
    if lower in _IRREGULAR_PLURALS:
        plural = _IRREGULAR_PLURALS[lower]
        # Preserve a leading capital so ``Child`` → ``Children``.
        return plural.capitalize() if noun[:1].isupper() else plural
    return f"{noun}s"


def fmt_duration(ms: object) -> str:
    """Format a millisecond duration as a compact human-readable string."""
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return "—"
    if value < 1000:
        return f"{value} ms"
    if value < 60_000:
        return f"{value / 1000:.1f} s"
    return f"{value / 60_000:.1f} min"


def stage_status_badge(status: str, label: Optional[str] = None) -> str:
    """Return a coloured status pill for a stage row.

    Args:
        status: One of the StageRecorder statuses; unknown values fall
                back to neutral styling.
        label:  Optional text shown after the icon. Defaults to the
                status name so the badge is meaningful on its own.

    Returns:
        An HTML ``<span>`` element string.
    """
    key = str(status).lower()
    icon = STATUS_ICON.get(key, "?")
    bg = STATUS_BG.get(key, "#E9ECEF")
    fg = STATUS_FG.get(key, "#333")
    text = label if label is not None else key
    return (
        f'<span style="background:{bg};color:{fg};font-weight:700;'
        f"font-size:13px;padding:2px 10px;border-radius:10px;"
        f'min-width:80px;text-align:center;display:inline-block">'
        f"{icon} {h(text)}</span>"
    )


def issue_count_badges(issues: Sequence[dict]) -> str:
    """Return small error/warning count pills for a stage summary line."""
    n_errors = sum(1 for i in issues if str(i.get("severity", "")).lower() == "error")
    n_warnings = sum(
        1 for i in issues if str(i.get("severity", "")).lower() == "warning"
    )
    out = ""
    if n_errors:
        out += (
            f'<span style="background:#F8D7DA;color:#721C24;font-size:11px;'
            f'font-weight:700;padding:1px 7px;border-radius:10px;margin-left:8px">'
            f"{n_errors} error{'s' if n_errors != 1 else ''}</span>"
        )
    if n_warnings:
        out += (
            f'<span style="background:#FFF3CD;color:#856404;font-size:11px;'
            f'font-weight:700;padding:1px 7px;border-radius:10px;margin-left:4px">'
            f"{n_warnings} warning{'s' if n_warnings != 1 else ''}</span>"
        )
    return out


def lookup_source_provenance(
    location: str, source_map: Optional[dict]
) -> Optional[dict]:
    """Return the source-file entry for an inspect-finding location.

    Inspect stores locations like ``DDL\\views\\foo.viw:37`` (relative
    to ``payload/database/``, Windows backslashes, with a trailing
    ``:line`` suffix). The harvest source map keys by full
    ``payload/database/DDL/views/foo.viw`` paths with forward slashes.
    This helper handles the common normalisations so callers don't
    have to reinvent the mapping.

    Args:
        location: Issue location string from ``ships.decisions.json``.
        source_map: Loaded ``.ships/harvest/source_map.json`` dict or
            ``None``. When ``None`` or empty, the lookup short-circuits.

    Returns:
        The ``entries[<key>]`` dict (with ``source_relpath`` /
        ``source_abspath`` / ``type``), or ``None`` if no match.
    """
    if not source_map or not location:
        return None
    entries = source_map.get("entries") or {}
    if not entries:
        return None

    # Strip trailing ":<line>" suffix and normalise slashes.
    raw = str(location)
    if ":" in raw:
        head, tail = raw.rsplit(":", 1)
        if tail.isdigit():
            raw = head
    raw = raw.replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]

    if raw in entries:
        return entries[raw]
    # Try common prefix expansions — inspect typically reports paths
    # relative to ``payload/database/`` so prepend that and try.
    for prefix in ("payload/database/", "payload/"):
        candidate = prefix + raw
        if candidate in entries:
            return entries[candidate]
    return None


def render_issue_list(
    issues: Sequence[dict],
    source_map: Optional[dict] = None,
) -> str:
    """Render a stage's issue list as an HTML detail block.

    Args:
        issues: The ``issues`` array from a decisions.json stage entry.
                Each issue has ``severity`` / ``code`` / ``message`` and
                an optional ``location``.
        source_map: Optional loaded harvest source map (#466). When
            provided, each issue's location is resolved to its source
            file via :func:`lookup_source_provenance` and the source
            path is rendered as a faint subline so the reader knows
            which source file to edit.

    Returns:
        HTML string. A green "no issues" note when the list is empty.

    Each ``code`` span carries a ``title`` attribute with the human
    description from :data:`issue_codes.ISSUE_CODES`, so a reader can
    hover any code to see what it means without leaving the page.
    Unregistered codes (e.g. ad-hoc test fixtures) just get no tooltip.
    """
    from td_release_packager.orchestrator.issue_codes import describe

    if not issues:
        return (
            '<p style="color:#28A745;font-size:13px;margin:0">No issues recorded.</p>'
        )
    rows: List[str] = []
    for issue in issues:
        sev = str(issue.get("severity", "info")).lower()
        colour = _SEV_COLOUR.get(sev, "#555")
        icon = _SEV_ICON.get(sev, "·")
        raw_code = str(issue.get("code", ""))
        code = h(raw_code)
        tooltip = describe(raw_code) if raw_code else ""
        title_attr = (
            f' title="{h(tooltip)}"'
            if tooltip and tooltip != "(unregistered code)"
            else ""
        )
        msg = h(str(issue.get("message", "")))
        loc = issue.get("location", "")
        source_entry = lookup_source_provenance(str(loc), source_map) if loc else None
        source_subline = ""
        if source_entry:
            source_rel = source_entry.get("source_relpath", "")
            source_abs = source_entry.get("source_abspath", "")
            if source_rel:
                source_subline = (
                    f'<div style="font-size:11px;color:#4A6FA5;'
                    f'margin-top:2px" title="{a(source_abs)}">'
                    f"↳ source: <code>{h(source_rel)}</code></div>"
                )
        loc_html = (
            f'<div style="font-size:11px;color:#6C757D;margin-top:2px">{h(str(loc))}</div>'
            if loc
            else ""
        )
        rows.append(
            f'<div style="padding:5px 0;border-bottom:1px solid #F0F0F0">'
            f'<span style="color:{colour};font-weight:700;margin-right:6px">{icon}</span>'
            f'<span style="font-family:monospace;font-size:12px;color:{colour};'
            f'margin-right:8px;cursor:help;border-bottom:1px dotted {colour}"{title_attr}>'
            f"{code}</span>"
            f'<span style="font-size:12px;color:#333">{msg}</span>'
            f"{loc_html}"
            f"{source_subline}</div>"
        )
    return "".join(rows)


def render_issue_code_glossary() -> str:
    """Render every registered issue code as a glossary block.

    Drives off :data:`issue_codes.ISSUE_CODES` so the catalogue stays
    in sync with the source of truth — when a new code lands, it
    appears in this glossary automatically. Codes are grouped by
    their domain prefix (HARVEST_, INSPECT_, ANALYSE_, GENERATE_,
    PACKAGE_, TOKEN_, PROPERTIES_) so a reader scanning for a stage's
    findings finds them together. Within a domain, codes are listed
    alphabetically.

    Returns:
        HTML block of ``<div class="guide-glossary-item">`` entries
        ready to drop inside an existing ``guide-glossary`` container.
    """
    from td_release_packager.orchestrator.issue_codes import ISSUE_CODES

    _DOMAINS: List[Tuple[str, str]] = [
        ("HARVEST_", "Harvest"),
        ("INSPECT_", "Inspect"),
        ("ANALYSE_", "Analyse"),
        ("GENERATE_", "Generate"),
        ("PACKAGE_", "Package"),
        ("TOKEN_", "Token"),
        ("PROPERTIES_", "Properties"),
    ]
    by_domain: Dict[str, List[str]] = {label: [] for _prefix, label in _DOMAINS}
    misc: List[str] = []
    for code in sorted(ISSUE_CODES):
        placed = False
        for prefix, label in _DOMAINS:
            if code.startswith(prefix):
                by_domain[label].append(code)
                placed = True
                break
        if not placed:
            misc.append(code)
    parts: List[str] = []
    for _prefix, label in _DOMAINS:
        codes = by_domain[label]
        if not codes:
            continue
        parts.append(f'<div class="guide-glossary-item"><dt>{h(label)} codes</dt><dd>')
        items = "".join(
            (
                f'<div style="margin-bottom:6px"><code style="font-weight:600">'
                f"{h(code)}</code> — {h(ISSUE_CODES[code])}</div>"
            )
            for code in codes
        )
        parts.append(items + "</dd></div>")
    if misc:
        parts.append('<div class="guide-glossary-item"><dt>Other codes</dt><dd>')
        items = "".join(
            (
                f'<div style="margin-bottom:6px"><code style="font-weight:600">'
                f"{h(code)}</code> — {h(ISSUE_CODES[code])}</div>"
            )
            for code in misc
        )
        parts.append(items + "</dd></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------


@dataclass
class Tab:
    """One tab in a report.

    Attributes:
        id:     Unique DOM id for the pane (e.g. ``"tab-timeline"``).
        label:  Button text shown in the tab bar.
        body:   Pre-rendered HTML for the pane's inner content.
        active: Whether this tab is shown first.
    """

    id: str
    label: str
    body: str
    active: bool = False


# Shared chrome — the subset of CSS common to every SHIPS report.  Report
# modules pass their own ``extra_css`` for anything tab-specific.
BASE_CSS = f"""
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f0f4f8; color: #212529; min-height: 100vh; }}
.project-ribbon {{ background: {ORANGE}; color: {WHITE}; padding: 6px 24px;
                   font-size: 13px; font-weight: 700; letter-spacing: .3px;
                   display: flex; align-items: center; gap: 12px;
                   text-transform: uppercase; }}
.project-ribbon .ribbon-sep {{ color: rgba(255,255,255,.6); font-weight: 400; }}
.project-ribbon .ribbon-version {{ font-weight: 400; letter-spacing: .2px;
                                   text-transform: none; }}
.hdr {{ background: {NAVY}; color: {WHITE}; padding: 0 24px;
        display: flex; align-items: center; gap: 16px; height: 56px; }}
.hdr-title {{ font-size: 17px; font-weight: 700; letter-spacing: -.2px; }}
.hdr-sub {{ font-size: 13px; color: #8ba4be; }}
.status-pill {{ margin-left: auto; padding: 4px 14px; border-radius: 20px;
                font-size: 13px; font-weight: 700; letter-spacing: .3px;
                white-space: nowrap; }}
.meta-bar {{ background: {WHITE}; border-bottom: 1px solid {BORDER};
             padding: 10px 24px; font-size: 13px; color: #555;
             display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
.meta-bar strong {{ color: {NAVY}; }}
.tabs {{ background: {WHITE}; border-bottom: 2px solid {BORDER}; padding: 0 24px;
         display: flex; gap: 0; flex-wrap: wrap; }}
.tab-btn {{ background: none; border: none; border-bottom: 3px solid transparent;
            padding: 14px 20px; font-size: 14px; font-weight: 500; cursor: pointer;
            color: #555; margin-bottom: -2px; }}
.tab-btn.active {{ color: {NAVY}; border-bottom-color: {ORANGE}; font-weight: 700; }}
.tab-btn:hover {{ color: {NAVY}; }}
.tab-pane {{ display: none; }}
.tab-pane.active {{ display: block; }}
.content {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
.card {{ background: {WHITE}; border-radius: 8px; border: 1px solid {BORDER};
         padding: 20px 24px; margin-bottom: 16px; }}
details > summary {{ list-style: none; }}
details > summary::-webkit-details-marker {{ display: none; }}
"""


# Shared "Reader's Guide" tab styling. Used by both the package report and
# the pre-package pipeline report so the visual language stays consistent.
GUIDE_CSS = f"""
/* ── Guide tab ── */
.guide-hero {{ background: {NAVY}; color: {WHITE}; border-radius: 8px;
               padding: 28px 32px; margin-bottom: 24px;
               display: flex; align-items: center; gap: 24px; }}
.guide-hero-text h2 {{ font-size: 20px; font-weight: 700; margin-bottom: 6px; }}
.guide-hero-text p {{ font-size: 14px; color: #8ba4be; max-width: 680px; line-height: 1.6; }}
.guide-steps {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr));
                gap: 16px; margin-bottom: 24px; }}
.guide-step {{ border: 1px solid {BORDER}; border-radius: 8px; padding: 18px 20px;
               background: {WHITE}; }}
.guide-step-num {{ display: inline-flex; align-items: center; justify-content: center;
                   width: 28px; height: 28px; border-radius: 50%; background: {ORANGE};
                   color: {WHITE}; font-size: 13px; font-weight: 700; margin-bottom: 10px; }}
.guide-step h4 {{ font-size: 14px; font-weight: 700; color: {NAVY}; margin-bottom: 6px; }}
.guide-step p {{ font-size: 13px; color: #555; line-height: 1.5; }}
.guide-glossary {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr));
                   gap: 12px; margin-bottom: 8px; }}
.guide-glossary-item {{ border-left: 4px solid {ORANGE}; background: #f8f9fa;
                         padding: 10px 14px; border-radius: 0 4px 4px 0; }}
.guide-glossary-item dt {{ font-weight: 700; font-size: 13px; color: {NAVY}; }}
.guide-glossary-item dd {{ font-size: 13px; color: #555; margin: 4px 0 0; line-height: 1.5; }}
.guide-section-title {{ font-size: 15px; font-weight: 700; color: {NAVY};
                         margin: 24px 0 12px; padding-bottom: 8px;
                         border-bottom: 2px solid {ORANGE}; }}
/* Tooltip */
[data-tip] {{ border-bottom: 1px dashed {ORANGE}; cursor: help; position: relative; }}
[data-tip]:hover::after {{ content: attr(data-tip); position: absolute; bottom: 125%;
  left: 50%; transform: translateX(-50%); background: {NAVY}; color: {WHITE};
  font-size: 12px; padding: 6px 10px; border-radius: 5px; white-space: normal;
  width: 260px; line-height: 1.5; z-index: 100; pointer-events: none;
  box-shadow: 0 4px 12px rgba(0,0,0,.25); }}
[data-tip]:hover::before {{ content: ""; position: absolute; bottom: 115%;
  left: 50%; transform: translateX(-50%); border: 6px solid transparent;
  border-top-color: {NAVY}; z-index: 100; }}
"""

# Teradata wordmark used in the header bar across all reports.
_WORDMARK = (
    '<svg width="90" height="24" viewBox="0 0 90 24" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<text x="0" y="19" font-family="Inter,sans-serif" font-size="18" '
    'font-weight="700" letter-spacing="-.3" fill="#fff">Teradata</text></svg>'
)


def status_pill(label: str, bg: str, fg: str) -> str:
    """Return the header status pill markup (right-aligned in the header)."""
    return (
        f'<div class="status-pill" style="background:{bg};color:{fg}">{h(label)}</div>'
    )


def render_page(
    *,
    doc_title: str,
    header_title: str,
    header_sub: Optional[str] = None,
    header_pill: Optional[str] = None,
    meta_html: Optional[str] = None,
    tabs: Sequence[Tab],
    content_prefix: str = "",
    extra_css: str = "",
    project_name: Optional[str] = None,
    ships_version: Optional[str] = None,
) -> str:
    """Assemble a complete, self-contained SHIPS report HTML document.

    The output is a single HTML string with embedded CSS and a tiny
    tab-switching script — no external requests, opens directly from a
    ``file:`` URL.

    Args:
        doc_title:      The ``<title>`` text.
        header_title:   Bold title shown in the navy header bar.
        header_sub:     Optional sub-line under the header title.
        header_pill:    Optional pre-rendered status pill (see ``status_pill``).
        meta_html:      Optional pre-rendered inner HTML for the meta bar.
                        When ``None`` the meta bar is omitted entirely.
        tabs:           Tab definitions, rendered left-to-right. Exactly one
                        is shown first: the first marked ``active``, or the
                        first tab when none is marked.
        content_prefix: Optional raw HTML rendered inside the content area
                        before the tab panes (e.g. an action banner).
        extra_css:      Report-specific CSS appended after ``BASE_CSS``.
        project_name:   Project this report belongs to (e.g. ``CustomerDNA``).
                        When set, an orange ribbon is rendered above the
                        navy header bar showing ``<project> · SHIPS v<ver>``
                        (issue #481). Omitted when ``None``.
        ships_version:  SHIPS version string for the ribbon. Defaults to
                        ``td_release_packager.__version__`` when ``None``
                        and ``project_name`` is set.

    Returns:
        The full HTML document as a string.
    """
    tab_list = list(tabs)
    if tab_list and not any(t.active for t in tab_list):
        tab_list[0].active = True

    buttons = "\n".join(
        f'<button class="tab-btn{" active" if t.active else ""}" '
        f"onclick=\"switchTab(this,'{a(t.id)}')\">{h(t.label)}</button>"
        for t in tab_list
    )
    panes = "\n".join(
        f'<div id="{a(t.id)}" class="tab-pane{" active" if t.active else ""} card">'
        f"\n{t.body}\n</div>"
        for t in tab_list
    )

    sub_html = f'<div class="hdr-sub">{h(header_sub)}</div>' if header_sub else ""
    meta_bar = f'<div class="meta-bar">{meta_html}</div>' if meta_html else ""

    # Project / version ribbon (issue #481) — rendered above the navy
    # header bar so the reader sees the report's provenance immediately.
    # Omitted when the caller hasn't supplied a project name so the
    # legacy chrome (older tests, ad-hoc previews) keeps rendering
    # identically.
    ribbon_html = ""
    if project_name:
        from td_release_packager._version import __version__ as _DEFAULT_VERSION

        version_text = ships_version or _DEFAULT_VERSION
        ribbon_html = (
            '<div class="project-ribbon">'
            f"<span>{h(project_name)}</span>"
            '<span class="ribbon-sep">·</span>'
            f'<span class="ribbon-version">SHIPS v{h(version_text)}</span>'
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(doc_title)}</title>
<style>
{BASE_CSS}
{extra_css}
</style>
</head>
<body>

{ribbon_html}
<div class="hdr">
  {_WORDMARK}
  <div>
    <div class="hdr-title">{h(header_title)}</div>
    {sub_html}
  </div>
  {header_pill or ""}
</div>

{meta_bar}

<div class="tabs">
  {buttons}
</div>

<div class="content">
{content_prefix}
{panes}
</div>

<script>
function switchTab(btn, pane) {{
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.querySelectorAll('.tab-pane').forEach(function(p) {{ p.classList.remove('active'); }});
  btn.classList.add('active');
  document.getElementById(pane).classList.add('active');
}}
// Shared copy-to-clipboard helper used by the Deploy tab's command
// blocks and the DBQL Lookup card (issue #483). Defined here in the
// shared chrome so every SHIPS report supports the Copy button.
function copyCmd(id) {{
  var el = document.getElementById(id);
  if (!el) {{ return; }}
  var text = el.innerText;
  navigator.clipboard.writeText(text).then(function() {{
    var btn = el.nextElementSibling;
    if (!btn) {{ return; }}
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.style.background = '#198754';
    setTimeout(function() {{
      btn.textContent = orig;
      btn.style.background = '{ORANGE}';
    }}, 1500);
  }});
}}
</script>
</body>
</html>"""


def render_dbql_lookup_card(
    manifest_dict: Dict[str, object],
    *,
    operator_extras: Optional[Dict[str, str]] = None,
    title: str = "Find this in Teradata DBQL",
    intro: Optional[str] = None,
) -> str:
    """Render the DBQL Lookup card (issue #483).

    Shared by the package report's Deploy tab and the deploy report's
    Deployment pane. Shows the QueryBand keys SHIPS sets on every
    statement plus a copy-paste-ready ``DBC.DBQLogTbl`` filter so a DBA
    (or agent) can find the trace without parsing the band format.

    Args:
        manifest_dict:   ``BuildManifest.__dict__`` for the package —
                         supplies ``build_number`` / ``package_name`` /
                         ``environment`` for the static keys.
        operator_extras: Extra ``{key: value}`` pairs the deployer ran
                         with (``DeployConfig.query_band``). Empty on
                         the package report; populated on the deploy
                         report when the operator passed ``--query-band``.
        title:           Card heading. Defaults to "Find this in
                         Teradata DBQL"; the deploy report typically
                         overrides this to "QueryBand used by this
                         deployment".
        intro:           Optional override for the intro paragraph.

    Returns:
        Self-contained HTML snippet (no external CSS / JS required —
        the surrounding report supplies ``copyCmd`` for the Copy SQL
        button when present, otherwise the button is a no-op).
    """
    from td_release_packager.query_band import describe_query_band

    qb = describe_query_band(
        build_number=str(manifest_dict.get("build_number") or "?"),
        package_name=str(manifest_dict.get("package_name") or "?"),
        environment=str(manifest_dict.get("environment") or "?"),
        operator_extras=operator_extras,
    )

    default_intro = (
        "Every SQL statement SHIPS runs carries a QueryBand. After "
        "deployment, use these keys to retrieve the trace from "
        "<code>DBC.DBQLogTbl</code>. The values below are pre-filled "
        "for this package; <code>PHASE</code> / <code>FILE</code> / "
        "<code>STREAM</code> / <code>WAVE</code> are added dynamically "
        "by the deployer and can be filtered the same way once the "
        "deploy has run."
    )
    intro_text = intro if intro is not None else default_intro

    def row(key: str, value: str, source: str) -> str:
        return (
            f'<tr><td style="padding:6px 12px;border-bottom:1px solid #f0f0f0">'
            f"<code>{h(key)}</code></td>"
            f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0">'
            f"<code>{h(value)}</code></td>"
            f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;color:#666">'
            f"{h(source)}</td></tr>"
        )

    rows = [
        row("BUILD", qb["static"]["BUILD"], "manifest.build_number"),
        row("PKG", qb["static"]["PKG"], "manifest.package_name"),
        row("ENV", qb["static"]["ENV"], "manifest.environment"),
    ]
    for key in qb["dynamic_keys"]:
        rows.append(
            row(
                key,
                "(set at runtime)",
                "deployer — per phase / file / wave-parallel stream",
            )
        )
    for key, value in qb["operator_extras"].items():
        rows.append(row(key, value, "operator extras (--query-band)"))

    sql = (
        "SELECT\n"
        "    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD') AS DeployDate,\n"
        "    t1.UserName,\n"
        "    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,\n"
        "    GetQueryBandValue(t1.QueryBand, 0, 'PHASE') AS Phase,\n"
        "    GetQueryBandValue(t1.QueryBand, 0, 'FILE')  AS File,\n"
        "    t1.StatementType,\n"
        "    t1.NumResultRows\n"
        "FROM DBC.DBQLogTbl t1\n"
        "WHERE " + qb["dbql_filter_template"] + "\n"
        "ORDER BY t1.CollectTimeStamp;"
    )

    return f"""
<div style="margin-top:24px;border:1px solid {BORDER};border-radius:8px;
  background:{WHITE};padding:18px 22px">
  <div style="font-size:15px;font-weight:700;color:{NAVY};margin-bottom:6px">
    {h(title)}
  </div>
  <p style="font-size:13px;color:#555;line-height:1.5;margin-bottom:14px">
    {intro_text}
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px">
    <thead>
      <tr style="background:{LIGHT};color:{NAVY}">
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid {BORDER}">Key</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid {BORDER}">Value</th>
        <th style="text-align:left;padding:8px 12px;border-bottom:2px solid {BORDER}">Source</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  <div style="position:relative">
    <pre id="dbql_filter_sql" style="background:#1E2761;color:#E8F0FE;padding:14px 48px 14px 16px;
      border-radius:6px;font-size:13px;overflow-x:auto;margin:0;white-space:pre">{h(sql)}</pre>
    <button onclick="copyCmd('dbql_filter_sql')"
      style="position:absolute;top:8px;right:8px;background:{ORANGE};color:#fff;
      border:none;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px">
      Copy SQL
    </button>
  </div>
</div>"""


def run_status_style(final_status: str) -> Tuple[str, str]:
    """Return (background, text) colours for a run-level final status."""
    key = str(final_status).lower()
    if key == "success":
        return "#198754", WHITE
    if key in ("failed", "partial"):
        return "#DC3545", WHITE
    if key == "warning":
        return "#FFC107", NAVY
    return "#6C757D", WHITE
