"""
Provenance Renderer — HTML drill-down for the SHIPS deployment report.

Renders a ProvenanceChain as a collapsible ``<details>`` block showing
the four-stage filename transformation chain. Designed to be embedded
inside an existing object-results table row so DBAs can drill into a
failed or skipped object and see exactly where in the build pipeline
the path was rewritten.

Design constraints (locked in this build):

    Portability:    No CDN dependencies. No JS framework. The report
                    must open correctly years from now, online or off,
                    on any machine. Drill-down uses native HTML
                    ``<details>``/``<summary>`` — no JavaScript needed.

    Branding:       Teradata Orange (#FF5F02) for emphasis, Navy
                    (#00233C) for structure, white space for breathing
                    room. Inter font with system fallbacks so the
                    report degrades gracefully if Inter isn't
                    installed. WCAG AA contrast targets.

    Status colours: Failed = red, Skipped = amber, No-op = grey,
                    Applied = navy. Status colour is applied to the
                    status pill only, not whole rows — keeps the
                    drill-down readable without alarm fatigue.

Public API:

    render_chain(chain)         -> HTML fragment for one chain
    PROVENANCE_CSS              -> the CSS block (include once per
                                   report in the page <style>)

Author: SHIPS / Teradata Field Engineering
"""

from __future__ import annotations

import html
from typing import Optional

from ddl_deployer.provenance import (
    ProvenanceChain,
    STAGE_ORDER,
    Stage,
    Status,
)


# ---------------------------------------------------------------------------
# CSS — emitted once per report. Scoped under .ships-prov to avoid
# collision with the host page's existing styles. All colour and font
# values follow the Teradata brand palette.
# ---------------------------------------------------------------------------

PROVENANCE_CSS = """
/* === SHIPS provenance drill-down ====================================== */

.ships-prov {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont,
                 'Segoe UI', Roboto, sans-serif;
    font-size: 0.875rem;
    color: #00233C;
    margin-top: 0.5rem;
}

.ships-prov-summary {
    cursor: pointer;
    color: #00233C;
    font-weight: 500;
    padding: 0.375rem 0.5rem;
    border-radius: 3px;
    user-select: none;
    list-style: none;
}

.ships-prov-summary::-webkit-details-marker { display: none; }

.ships-prov-summary::before {
    content: "\\25B8";  /* right-pointing triangle */
    display: inline-block;
    margin-right: 0.5rem;
    transition: transform 0.15s ease;
    color: #FF5F02;
}

.ships-prov[open] > .ships-prov-summary::before {
    transform: rotate(90deg);
}

.ships-prov-summary:hover { background: #F5F7F9; }

.ships-prov-table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.5rem 0 0.75rem 1.5rem;
    font-variant-numeric: tabular-nums;
}

.ships-prov-table th,
.ships-prov-table td {
    padding: 0.5rem 0.75rem;
    text-align: left;
    border-bottom: 1px solid #E5E9EC;
    vertical-align: top;
}

.ships-prov-table th {
    font-weight: 600;
    color: #00233C;
    background: #F5F7F9;
    border-bottom: 2px solid #00233C;
    font-size: 0.8125rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
}

.ships-prov-table td.stage-name {
    font-weight: 500;
    white-space: nowrap;
    color: #00233C;
}

.ships-prov-table td.stage-path {
    font-family: 'Menlo', 'Consolas', 'Courier New', monospace;
    font-size: 0.8125rem;
    word-break: break-all;
    color: #00233C;
}

.ships-prov-table td.stage-note {
    color: #5A6B7A;
    font-size: 0.8125rem;
}

/* Status pills */
.ships-prov-status {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 3px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    white-space: nowrap;
}

.ships-prov-status.applied        { background: #E5EDF2; color: #00233C; }
.ships-prov-status.no_op          { background: #ECEEF0; color: #5A6B7A; }
.ships-prov-status.skipped        { background: #FFF4E5; color: #8B5A00; }
.ships-prov-status.failed         { background: #FCE8E8; color: #B71C1C; }

/* Highlight the failed stage row (when the build failed at a stage) */
.ships-prov-table tr.stage-failed {
    background: #FFF8F8;
}
.ships-prov-table tr.stage-failed td.stage-path {
    color: #B71C1C;
}

/* Neutralise host-page tr:hover cascade. The host report's
   'tr:hover td { background: ... }' rule would otherwise apply to
   our nested table rows. Keep the failed-row highlight intact. */
.ships-prov-table tr:hover td {
    background: inherit;
}
.ships-prov-table tr.stage-failed:hover td {
    background: #FFF8F8;
}
"""


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

# Human-readable labels for each stage, shown in the drill-down table.
# Keeping these here rather than in provenance.py because they are a
# rendering concern, not a schema concern.
_STAGE_LABELS = {
    "source": "Source",
    "eponymous": "Eponymous rename",
    "token_resolved": "Token substitution",
    "package": "Package path",
}


def _render_stage_row(stage: Stage, prev_path: Optional[str]) -> str:
    """
    Render a single stage as one ``<tr>`` element.

    Args:
        stage:     The stage being rendered.
        prev_path: The path from the previous stage (None for the
                   first stage). Used to decide whether to display the
                   path as "unchanged" — keeps the drill-down compact
                   for no_op/skipped stages where the path is the same
                   as the line above.

    Returns:
        HTML string for one ``<tr>``.
    """
    label = _STAGE_LABELS.get(stage.stage, stage.stage)

    # Compact display: when the path didn't change, render an arrow
    # pointing up rather than repeating the same string. Keeps the
    # eye drawn to the stages that DID change something.
    if prev_path is not None and stage.path == prev_path:
        path_cell = '<span style="color:#5A6B7A;">↑ unchanged</span>'
    else:
        path_cell = html.escape(stage.path)

    note_cell = html.escape(stage.note) if stage.note else ""

    row_class = ""
    if stage.status == Status.FAILED:
        row_class = ' class="stage-failed"'

    return (
        f"<tr{row_class}>"
        f'<td class="stage-name">{html.escape(label)}</td>'
        f'<td class="stage-path">{path_cell}</td>'
        f'<td><span class="ships-prov-status {stage.status.value}">'
        f"{html.escape(stage.status.value)}</span></td>"
        f'<td class="stage-note">{note_cell}</td>'
        f"</tr>"
    )


def render_chain(chain: ProvenanceChain) -> str:
    """
    Render a full chain as a collapsible drill-down fragment.

    The fragment is a self-contained ``<details>`` element. Drop it
    into a table cell or list item below the failing/skipped object's
    primary row. Multiple chains can be rendered on the same page —
    they share the CSS in ``PROVENANCE_CSS`` (which should be
    included once in the page's ``<style>`` block).

    Args:
        chain: A complete ProvenanceChain (all four stages recorded).

    Returns:
        HTML string. Safe to embed directly inside the report — all
        user-controlled values (paths, notes) are HTML-escaped.

    Raises:
        ValueError: If the chain is incomplete.
    """
    if not chain.is_complete():
        raise ValueError(
            f"[render_chain] Refusing to render incomplete chain — "
            f"has {len(chain.stages)}/{len(STAGE_ORDER)} stages."
        )

    # Build the table body, tracking the previous stage's path so
    # _render_stage_row can fold no-op rows visually.
    rows = []
    prev_path: Optional[str] = None
    for stage in chain.stages:
        rows.append(_render_stage_row(stage, prev_path))
        prev_path = stage.path

    # Summary line — concise enough to fit on one line in a tight
    # report cell, informative enough to indicate whether the user
    # should expand further. Show source filename → final filename
    # for the typical case.
    src = html.escape(chain.source_path())
    final = html.escape(chain.final_path())
    summary = (
        f"Expand transformation chain "
        f"<span style='color:#5A6B7A;'>"
        f"({src} → {final})</span>"
    )

    return (
        '<details class="ships-prov">'
        f'<summary class="ships-prov-summary">{summary}</summary>'
        '<table class="ships-prov-table">'
        "<thead><tr>"
        "<th>Stage</th>"
        "<th>Path</th>"
        "<th>Status</th>"
        "<th>Note</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
        "</details>"
    )
