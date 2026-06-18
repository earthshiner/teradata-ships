"""
pipeline_report.py — Pre-package pipeline HTML report (#324).

Renders a single, self-contained ``pipeline_report.html`` that shows a
human what happened at each pre-package step — harvest, inspect, scan,
analyse — *before* a package archive is sealed.  The existing
``package_report.html`` only exists after a build; this report is the
earlier lens, regenerated automatically after every pipeline step.

The report draws its step data from ``ships.decisions.json`` (a read-only
projection that changes no pipeline behaviour) and the live payload tree.
All page chrome comes from ``reporting.common`` so the visual identity
matches the package report.

Tabs: Run timeline, Harvest, Inspect, Scan, Analyse (each a projection of
its decisions.json stage), Payload (the pre-package object/wave view,
rendered with the shared wave SVG from ``reporting.waves``), and
Tokenisation (a before/after substitution preview from
``reporting.tokenisation``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from td_release_packager.reporting import common, tokenisation, waves
from td_release_packager.reporting.common import Tab, h

logger = logging.getLogger(__name__)

DECISIONS_FILENAME = "ships.decisions.json"
REPORT_DIRNAME = os.path.join("output", "reports")
REPORT_FILENAME = "pipeline_report.html"

# Canonical SHIPS pipeline order. The timeline displays stages in this
# order regardless of when each was last invoked — re-running ``harvest``
# after ``package`` should not push it to the bottom of the visual
# pipeline. Unknown stage names fall through to the end, ordered by their
# ``started_at`` timestamp.
_PIPELINE_ORDER = (
    "scaffold",
    "harvest",
    "inspect",
    "scan",
    "analyse",
    "package",
)

# Per-step metrics surfaced in the report.  Each entry is a
# (display_label, decisions key) pair; the key is looked up across a
# stage's merged inputs+outputs, and absent or zero-noise values are
# skipped so the panels stay readable.  Mirrors the package report's
# Build Provenance metric map for the steps this report covers.
_STEP_METRICS: Dict[str, List[Tuple[str, str]]] = {
    "harvest": [
        ("classified", "classified"),
        ("unclassified", "unclassified"),
        ("files placed", "files_placed"),
        ("MULTISET injected", "multiset_injected"),
        ("cleaned", "cleaned"),
    ],
    "inspect": [
        ("files scanned", "files_scanned"),
        ("lint errors", "lint_errors"),
        ("lint warnings", "lint_warnings"),
        ("files with issues", "files_with_issues"),
    ],
    "scan": [
        ("unique tokens", "unique_tokens"),
        ("files with tokens", "files_with_tokens"),
    ],
    "analyse": [
        ("objects", "object_count"),
        ("waves", "wave_count"),
        ("dependencies", "dependency_count"),
        ("cycles", "cycle_count"),
    ],
}

# Metrics that are only worth showing when non-zero (a "0 unclassified" or
# "0 lint errors" is reassuring noise that clutters the panel).
_ZERO_NOISE_KEYS = frozenset(
    {
        "unclassified",
        "lint_errors",
        "lint_warnings",
        "files_with_issues",
        "cycle_count",
        "cleaned",
        "multiset_injected",
    }
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_latest_run(project_dir: str) -> Optional[dict]:
    """Return a synthetic "latest pipeline" run dict from ``ships.decisions.json``.

    Each SHIPS CLI invocation (``ships harvest``, ``ships inspect``, ...) is
    recorded as its own run, so the literal newest run typically carries only
    one stage. To give the report a complete view of the most recent
    pipeline state, this function merges stages across runs: for each stage
    name it keeps the most recently recorded stage entry. Run-level fields
    (``command``, ``run_id``, ``final_status``, ``duration_ms``) come from
    the newest run so the timeline header still identifies the last action.

    Args:
        project_dir: SHIPS project root that should contain the decisions
                     file. The file is read directly (not searched up the
                     tree) because the pipeline always records against the
                     project root.

    Returns:
        A synthetic run object (``{"run_id", "command", "stages", ...}``)
        with merged ``stages``, or ``None`` when the file is absent,
        unreadable, malformed, or empty.
    """
    path = os.path.join(project_dir, DECISIONS_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("pipeline_report: could not read %s: %s", path, exc)
        return None
    runs = data.get("runs") if isinstance(data, dict) else None
    if not runs:
        return None

    # Walk forward so later occurrences overwrite earlier ones; each stage
    # name ends up holding its most recent entry.
    latest_stage: Dict[str, dict] = {}
    for run in runs:
        for stage in run.get("stages", []) or []:
            name = str(stage.get("stage", "")).lower()
            if name:
                latest_stage[name] = stage

    def _sort_key(stage: dict) -> Tuple[int, str]:
        name = str(stage.get("stage", "")).lower()
        try:
            return (_PIPELINE_ORDER.index(name), "")
        except ValueError:
            # Unknown stage names sort after the canonical pipeline,
            # ordered by their started_at timestamp for stability.
            return (len(_PIPELINE_ORDER), str(stage.get("started_at") or ""))

    merged_stages = sorted(latest_stage.values(), key=_sort_key)

    newest = runs[-1]
    return {
        "run_id": newest.get("run_id"),
        "command": newest.get("command"),
        "final_status": newest.get("final_status", "success"),
        "duration_ms": newest.get("duration_ms"),
        "stages": merged_stages,
    }


def _find_stage(stages: List[dict], name: str) -> Optional[dict]:
    """Return the last stage entry matching ``name`` (case-insensitive)."""
    match = None
    for stage in stages:
        if str(stage.get("stage", "")).lower() == name:
            match = stage
    return match


def _stage_metric_pairs(stage: dict, name: str) -> List[Tuple[str, str]]:
    """Return (label, value) metric pairs to display for one stage."""
    combined = {**stage.get("inputs", {}), **stage.get("outputs", {})}
    pairs: List[Tuple[str, str]] = []
    for label, key in _STEP_METRICS.get(name, []):
        val = combined.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)) and val == 0 and key in _ZERO_NOISE_KEYS:
            continue
        pairs.append((label, str(val)))
    return pairs


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------


def _timeline_tab(run: dict) -> str:
    """Render the Run-timeline tab: one expandable row per recorded stage."""
    stages = run.get("stages", []) or []
    if not stages:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "No stages recorded in the latest run yet.</p>"
        )

    rows: List[str] = []
    for stage in stages:
        name = stage.get("stage", "unknown")
        status = str(stage.get("status", "success")).lower()
        issues = stage.get("issues", []) or []
        n_errors = sum(1 for i in issues if str(i.get("severity")).lower() == "error")
        n_warnings = sum(
            1 for i in issues if str(i.get("severity")).lower() == "warning"
        )
        started = str(stage.get("started_at") or "")
        time_label = started[11:19] if len(started) >= 19 else started

        metric_pairs = _stage_metric_pairs(stage, str(name).lower())
        metrics = "  ·  ".join(
            f"<strong>{h(v)}</strong> {h(lbl)}" for lbl, v in metric_pairs
        )
        metrics_html = (
            f'<div style="padding:0 14px 8px 14px;font-size:12px;color:#666">{metrics}</div>'
            if metrics
            else ""
        )

        open_attr = " open" if n_errors else ""
        row_bg = "#FFF8F8" if n_errors else ("#FFFDF0" if n_warnings else "#FFF")
        if n_errors:
            border_left = "4px solid #DC3545"
        elif n_warnings:
            border_left = "4px solid #FFC107"
        else:
            border_left = "4px solid #28A745"

        rows.append(
            f'<details{open_attr} style="border-left:{border_left};background:{row_bg};'
            f'margin-bottom:4px;border-radius:0 4px 4px 0">'
            f'<summary style="display:flex;align-items:center;gap:10px;'
            f'padding:10px 14px;cursor:pointer;user-select:none">'
            f"{common.stage_status_badge(status, name)}"
            f'<span style="font-size:12px;color:#555">'
            f"{common.fmt_duration(stage.get('duration_ms'))}</span>"
            + (
                f'<span style="font-size:11px;color:#888">{h(time_label)}</span>'
                if time_label
                else ""
            )
            + f"{common.issue_count_badges(issues)}"
            f"</summary>"
            f"{metrics_html}"
            f'<div style="background:#F8F9FA;border-top:1px solid #DEE2E6;'
            f'padding:12px 16px;font-size:13px">{common.render_issue_list(issues)}</div>'
            f"</details>"
        )

    command = h(str(run.get("command", "—")))
    run_id = h(str(run.get("run_id", "—")))
    final_status = str(run.get("final_status", "success"))
    bg, fg = common.run_status_style(final_status)
    total = common.fmt_duration(run.get("duration_ms"))

    header = (
        f'<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;'
        f'margin-bottom:16px">'
        f'<span style="background:{bg};color:{fg};font-weight:700;font-size:13px;'
        f'padding:3px 12px;border-radius:12px">{h(final_status.upper())}</span>'
        f'<span style="font-size:13px;color:#555">command '
        f"<code>{command}</code></span>"
        f'<span style="font-size:13px;color:#555">total {h(total)}</span>'
        f'<span style="font-size:11px;color:#999;margin-left:auto">run {run_id}</span>'
        f"</div>"
    )
    intro = (
        '<p style="font-size:13px;color:#555;margin-bottom:14px">'
        "Steps recorded in <code>ships.decisions.json</code> for the most recent "
        "run. Steps with errors open automatically — click any row to expand "
        "or collapse its issue detail.</p>"
    )
    return header + intro + "".join(rows)


# Step keys that get a metric-card + issues detail tab, with the display
# title and the command a reviewer runs to populate the step.
_STEP_TABS: Dict[str, Tuple[str, str]] = {
    "harvest": ("Harvest", "ships harvest"),
    "inspect": ("Inspect", "ships inspect"),
    "scan": ("Scan", "ships scan"),
    "analyse": ("Analyse", "ships analyse"),
}


def _step_detail_tab(stages: List[dict], step_key: str) -> str:
    """Render a detail tab for one recorded step: metric cards + issues.

    Shared by Harvest / Inspect / Scan / Analyse — each is structurally the
    same projection of its decisions.json stage entry, so one builder keeps
    them consistent (DRY).

    Args:
        stages:   The latest run's stage list.
        step_key: Canonical step name (key of ``_STEP_TABS``).
    """
    title, command = _STEP_TABS.get(step_key, (step_key.title(), f"ships {step_key}"))
    stage = _find_stage(stages, step_key)
    if stage is None:
        return (
            f'<p style="color:#6C757D;padding:24px;text-align:center">'
            f"{h(title)} has not run in the latest recorded run. Run "
            f"<code>{h(command)}</code> to populate this report.</p>"
        )

    status = str(stage.get("status", "success")).lower()
    issues = stage.get("issues", []) or []
    metric_pairs = _stage_metric_pairs(stage, step_key)

    # Metric cards — the headline counts a reviewer scans first.
    if metric_pairs:
        cards = "".join(
            f'<div style="border:1px solid {common.BORDER};border-radius:8px;'
            f'padding:14px 18px;min-width:120px">'
            f'<div style="font-size:24px;font-weight:700;color:{common.NAVY}">{h(v)}</div>'
            f'<div style="font-size:12px;color:#6C757D;margin-top:2px">{h(lbl)}</div>'
            f"</div>"
            for lbl, v in metric_pairs
        )
        metrics_html = (
            f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">'
            f"{cards}</div>"
        )
    else:
        metrics_html = (
            f'<p style="color:#6C757D;margin-bottom:16px">'
            f"No {h(title.lower())} metrics were recorded.</p>"
        )

    header = (
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
        f"{common.stage_status_badge(status, step_key)}"
        f'<span style="font-size:12px;color:#555">'
        f"{common.fmt_duration(stage.get('duration_ms'))}</span>"
        f"{common.issue_count_badges(issues)}</div>"
    )
    issues_block = (
        f'<h3 style="font-size:14px;color:{common.NAVY};margin:8px 0 10px">Issues</h3>'
        f"{common.render_issue_list(issues)}"
    )
    return header + metrics_html + issues_block


# Pre-package project payload lives under payload/database/<source-dir>/…,
# using source-level directory names (NOT the package's numbered phases).
_PROJECT_PAYLOAD_SUBPATH = os.path.join("payload", "database")
_PROJECT_PHASE_LABELS = {
    "system": "System",
    "pre-requisites": "Pre-requisites",
    "dcl": "DCL",
    "ddl": "DDL",
    "dml": "DML",
    "post-install": "Post-install",
}
_PAYLOAD_SKIP_NAMES = frozenset({"_waves.txt", "_order.txt", ".gitkeep", ".gitignore"})


def scan_project_payload(project_dir: str) -> List[dict]:
    """Scan a project's pre-package payload tree into object records.

    Walks ``payload/database/`` and reads the project-root ``_waves.txt`` to
    assign deployment waves, producing the same record shape the package
    report uses so the shared wave SVG renders identically.

    Args:
        project_dir: SHIPS project root.

    Returns:
        One record per DDL/DCL/DML object, sorted by phase, wave, name.
        Empty when the project has not been harvested.
    """
    payload_root = os.path.join(project_dir, _PROJECT_PAYLOAD_SUBPATH)
    if not os.path.isdir(payload_root):
        return []

    wave_map = waves.parse_waves_txt(os.path.join(project_dir, "_waves.txt"))
    records: List[dict] = []
    for root, dirs, files in os.walk(payload_root):
        dirs.sort()
        rel_payload = os.path.relpath(root, payload_root).replace("\\", "/")
        phase_seg = rel_payload.split("/")[0] if rel_payload != "." else ""
        phase_label = _PROJECT_PHASE_LABELS.get(phase_seg.lower(), phase_seg or "Other")
        for fname in sorted(files):
            if fname in _PAYLOAD_SKIP_NAMES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in waves.EXT_TYPE:
                continue

            rel_proj = os.path.relpath(os.path.join(root, fname), project_dir).replace(
                "\\", "/"
            )
            rel_payload_file = os.path.relpath(
                os.path.join(root, fname), payload_root
            ).replace("\\", "/")
            stem = os.path.splitext(fname)[0]

            wave = (
                wave_map.get(rel_proj)
                or wave_map.get(rel_payload_file)
                or wave_map.get(fname)
            )
            # System-scope objects deploy serially before any numbered wave.
            if phase_seg.lower() == "system":
                wave = None

            records.append(
                {
                    "name": stem,
                    "type": waves.EXT_TYPE.get(ext, "UNKNOWN"),
                    "phase": phase_label,
                    "wave": wave,
                    "file": fname,
                    "path": rel_proj,
                    "ext": ext,
                }
            )

    records.sort(key=lambda r: (r["phase"], r["wave"] or 999, r["name"]))
    return records


def _payload_object_list(records: List[dict]) -> str:
    """Render a compact per-phase object list (used when waves are absent)."""
    rows = "".join(
        f'<tr><td style="font-family:monospace;padding:6px 12px;'
        f'border-bottom:1px solid #f0f0f0">{h(r["name"])}</td>'
        f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0">'
        f"{waves.type_badge(r['type'])}</td>"
        f'<td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;color:#6C757D">'
        f"{h(r['phase'])}</td></tr>"
        for r in records
    )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<thead><tr style="background:{common.NAVY};color:{common.WHITE}">'
        '<th style="padding:8px 12px;text-align:left">Object</th>'
        '<th style="padding:8px 12px;text-align:left">Type</th>'
        '<th style="padding:8px 12px;text-align:left">Phase</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _payload_tab(project_dir: str) -> str:
    """Render the Payload tab: what is about to be packaged, grouped by wave."""
    records = scan_project_payload(project_dir)
    if not records:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "No payload found yet. Run <code>ships harvest</code> to classify "
            "source DDL into the payload tree.</p>"
        )

    type_counts: Dict[str, int] = {}
    for r in records:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
    summary = ",  ".join(
        f"{v} {k.lower()}{'s' if v != 1 else ''}"
        for k, v in sorted(type_counts.items())
    )
    intro = (
        f'<p style="font-size:13px;color:#555;margin-bottom:6px">'
        f"<strong>{len(records)}</strong> object{'s' if len(records) != 1 else ''} "
        f"about to be packaged — {h(summary)}. This is the pre-package view; the "
        f"sealed package report carries the same objects with trust flags.</p>"
    )

    has_waves = any(r["wave"] is not None for r in records)
    if has_waves:
        body = waves.render_wave_svg(records)
    else:
        body = (
            '<div style="background:#fff3cd;border:1px solid #ffca2c;'
            "border-left:6px solid #FF5F02;padding:12px 16px;border-radius:6px;"
            'margin:12px 0;font-size:13px;color:#7a3b00">'
            "Deployment waves not computed yet — run <code>ships analyse</code> "
            "to see parallel wave ordering. Objects are listed below.</div>"
            + _payload_object_list(records)
        )
    return intro + body


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_pipeline_report(project_dir: str) -> Optional[str]:
    """Generate ``output/reports/pipeline_report.html`` for a project.

    Reads the latest run from ``ships.decisions.json`` and writes a
    self-contained HTML report. Returns ``None`` (writing nothing) when
    there is no run to report — e.g. the project has not been harvested.

    Args:
        project_dir: SHIPS project root.

    Returns:
        Absolute path to the written report, or ``None`` when skipped.
    """
    run = load_latest_run(project_dir)
    if run is None:
        return None

    stages = run.get("stages", []) or []
    final_status = str(run.get("final_status", "success"))
    bg, fg = common.run_status_style(final_status)

    n_steps = len(stages)
    n_errors = sum(
        1
        for s in stages
        for i in (s.get("issues") or [])
        if str(i.get("severity")).lower() == "error"
    )
    meta_html = (
        f"<span><strong>{n_steps}</strong> step{'s' if n_steps != 1 else ''}</span>"
        f'<span style="color:{common.BORDER}">|</span>'
        f"<span><strong>{n_errors}</strong> error{'s' if n_errors != 1 else ''}</span>"
        f'<span style="color:{common.BORDER}">|</span>'
        f'<span style="color:#777">pre-package pipeline — regenerated after every step</span>'
    )

    tabs = [
        Tab("tab-timeline", "Run timeline", _timeline_tab(run), active=True),
        Tab("tab-harvest", "Harvest", _step_detail_tab(stages, "harvest")),
        Tab("tab-inspect", "Inspect", _step_detail_tab(stages, "inspect")),
        Tab("tab-scan", "Scan", _step_detail_tab(stages, "scan")),
        Tab("tab-analyse", "Analyse", _step_detail_tab(stages, "analyse")),
        Tab("tab-payload", "Payload", _payload_tab(project_dir)),
        Tab(
            "tab-tokens",
            "Tokenisation",
            tokenisation.tokenisation_tab(project_dir),
        ),
    ]

    doc = common.render_page(
        doc_title=f"SHIPS Pipeline Report — {run.get('command', '')}",
        header_title="Pipeline Report",
        header_sub="Pre-package step-by-step view",
        header_pill=common.status_pill(final_status.upper(), bg, fg),
        meta_html=meta_html,
        tabs=tabs,
    )

    report_dir = os.path.join(project_dir, REPORT_DIRNAME)
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, REPORT_FILENAME)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    logger.info("Pipeline report: %s", report_path)
    return report_path
