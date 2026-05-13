"""
ships_dashboard.py — SHIPS Deployment Dashboard.

A lightweight web application for release managers that provides
cross-project visibility into SHIPS packages: trust status, approval
workflow, compliance evidence, and DBQL audit queries.

Usage
-----
Install the optional extra first:

    uv pip install -e ".[dashboard]"

Then start the server, pointing it at one or more project directories:

    python -m ships_dashboard --projects /path/to/OMR,/path/to/GCFR

Open http://localhost:8000 in a browser.

Options:
    --projects   Comma-separated list of project directories to scan
    --port       Port to listen on (default: 8000)
    --host       Host to bind (default: 127.0.0.1)

Design
------
The dashboard is read-mostly: it reads artefacts SHIPS already produces
(ships.decisions.json, ships.build.json inside archives, deployment manifests) and
owns no persistent data beyond the approval sidecar files it writes.

Approval sidecar files sit alongside the package archive:
    <archive>.approved  — empty file; written when release manager approves
    <archive>.rejected  — contains rejection reason

The dashboard NEVER executes DDL. Rollback and deployment commands are
surfaced as copyable text for a human or agent to run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_NAVY = "#00233C"
_ORANGE = "#FF5F02"
_WHITE = "#FFFFFF"
_LIGHT = "#F8F9FA"
_BORDER = "#DEE2E6"


@dataclass
class PackageInfo:
    """Metadata for one release archive discovered in a project."""

    project_name: str
    project_dir: str
    archive_path: str
    archive_filename: str
    package_name: str
    build_number: str
    environment: str
    timestamp: str
    author: str
    description: str
    trust_label: str
    trust_signals: Dict = field(default_factory=dict)
    file_count: int = 0
    requires: List[str] = field(default_factory=list)
    source_commit: str = ""
    source_dirty: bool = False
    approved: bool = False
    rejected: bool = False
    rejection_reason: str = ""
    has_report: bool = False

    @property
    def archive_stem(self) -> str:
        """Stable ID derived from the archive filename (no extension)."""
        name = self.archive_filename
        for suffix in (".zip", ".tar.gz"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name

    @property
    def approval_status(self) -> str:
        if self.approved:
            return "Approved"
        if self.rejected:
            return "Rejected"
        return "Awaiting"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def read_build_json_from_zip(archive_path: str) -> Optional[dict]:
    """Extract and parse ships.build.json from inside a package archive."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if name.endswith("ships.build.json"):
                    return json.loads(zf.read(name).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "dashboard: could not read ships.build.json from %s: %s", archive_path, exc
        )
    return None


def archive_has_report(archive_path: str) -> bool:
    """Return True when the archive contains a package_report.html."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return any(n.endswith("package_report.html") for n in zf.namelist())
    except Exception:  # noqa: BLE001
        return False


def check_approval(archive_path: str):
    """Return (approved, rejected, reason) based on sidecar files."""
    approved_path = archive_path + ".approved"
    rejected_path = archive_path + ".rejected"
    if os.path.isfile(approved_path):
        return True, False, ""
    if os.path.isfile(rejected_path):
        try:
            reason = open(rejected_path, encoding="utf-8").read().strip()
        except OSError:
            reason = "(reason unreadable)"
        return False, True, reason
    return False, False, ""


def scan_project(project_dir: str) -> List[PackageInfo]:
    """Scan a project's releases/ directory and return PackageInfo for each archive."""
    releases_dir = os.path.join(project_dir, "releases")
    if not os.path.isdir(releases_dir):
        return []

    packages: List[PackageInfo] = []
    for fname in sorted(os.listdir(releases_dir), reverse=True):
        if not (fname.endswith(".zip") or fname.endswith(".tar.gz")):
            continue
        archive_path = os.path.join(releases_dir, fname)
        build = read_build_json_from_zip(archive_path)
        if build is None:
            continue

        trust = build.get("trust", {})
        approved, rejected, reason = check_approval(archive_path)

        packages.append(
            PackageInfo(
                project_name=os.path.basename(project_dir),
                project_dir=project_dir,
                archive_path=archive_path,
                archive_filename=fname,
                package_name=build.get("package_name", ""),
                build_number=build.get("build_number", "?"),
                environment=build.get("environment", "?"),
                timestamp=build.get("timestamp", ""),
                author=build.get("author", ""),
                description=build.get("description", ""),
                trust_label=trust.get("label", "UNKNOWN"),
                trust_signals=trust.get("signals", {}),
                file_count=build.get("file_count", 0),
                requires=build.get("requires", []),
                source_commit=build.get("source_commit", ""),
                source_dirty=build.get("source_dirty", False),
                approved=approved,
                rejected=rejected,
                rejection_reason=reason,
                has_report=archive_has_report(archive_path),
            )
        )

    return packages


def scan_all_projects(project_dirs: List[str]) -> List[PackageInfo]:
    """Scan all configured project directories."""
    all_packages: List[PackageInfo] = []
    for project_dir in project_dirs:
        project_dir = os.path.abspath(project_dir)
        if not os.path.isdir(project_dir):
            logger.warning("dashboard: project dir not found: %s", project_dir)
            continue
        all_packages.extend(scan_project(project_dir))
    return all_packages


def write_approval(archive_path: str, approved: bool, reason: str = "") -> None:
    """Write or remove approval sidecar files."""
    approved_path = archive_path + ".approved"
    rejected_path = archive_path + ".rejected"
    # Clear both first
    for p in (approved_path, rejected_path):
        if os.path.isfile(p):
            os.remove(p)
    if approved:
        open(approved_path, "w").close()
    else:
        with open(rejected_path, "w", encoding="utf-8") as f:
            f.write(reason or "Rejected by release manager")


def generate_dbql_query(package_name: str, build_number: str) -> str:
    """Generate a DBQL audit query for a specific package build."""
    return (
        f"SELECT\n"
        f"    CAST(t1.CollectTimeStamp AS DATE FORMAT 'YYYY-MM-DD') AS DeployDate,\n"
        f"    t1.UserName,\n"
        f"    GetQueryBandValue(t1.QueryBand, 0, 'BUILD') AS Build,\n"
        f"    GetQueryBandValue(t1.QueryBand, 0, 'ENV')   AS Environment,\n"
        f"    GetQueryBandValue(t1.QueryBand, 0, 'WAVE')  AS Wave,\n"
        f"    COUNT(*)                                    AS Statements\n"
        f"FROM DBC.DBQLogTbl t1\n"
        f"WHERE GetQueryBandValue(t1.QueryBand, 0, 'BUILD') = '{build_number}'\n"
        f"  AND GetQueryBandValue(t1.QueryBand, 0, 'PKG')   = '{package_name}'\n"
        f"GROUP BY DeployDate, UserName, Build, Environment, Wave\n"
        f"ORDER BY DeployDate, Wave;"
    )


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _trust_badge(label: str) -> str:
    colours = {
        "READY": ("#198754", _WHITE, "✓"),
        "READY-WITH-CAVEATS": ("#FFC107", _NAVY, "⚠"),
        "BLOCKED": ("#DC3545", _WHITE, "✗"),
    }
    bg, fg, icon = colours.get(label, ("#6C757D", _WHITE, "?"))
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:3px;font-size:12px;font-weight:700;white-space:nowrap">'
        f"{icon} {label}</span>"
    )


def _approval_badge(pkg: PackageInfo) -> str:
    if pkg.approved:
        return '<span style="color:#198754;font-weight:600">✓ Approved</span>'
    if pkg.rejected:
        return '<span style="color:#DC3545;font-weight:600">✗ Rejected</span>'
    return '<span style="color:#FFC107;font-weight:600">● Awaiting</span>'


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f4f8;color:#212529;min-height:100vh}}
.hdr{{background:{_NAVY};color:{_WHITE};padding:0 24px;
     display:flex;align-items:center;gap:16px;height:52px}}
.hdr a{{color:{_WHITE};text-decoration:none;font-size:13px;opacity:.7}}
.hdr a:hover{{opacity:1}}
.hdr-title{{font-size:16px;font-weight:700;letter-spacing:-.2px}}
.content{{max-width:1280px;margin:0 auto;padding:20px 24px}}
.card{{background:{_WHITE};border:1px solid {_BORDER};border-radius:8px;
       padding:20px 24px;margin-bottom:16px}}
.sum-bar{{background:{_NAVY};color:{_WHITE};padding:12px 24px;
          display:flex;gap:24px;font-size:13px;flex-wrap:wrap}}
.sum-val{{font-size:20px;font-weight:700;margin-right:4px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:{_NAVY};color:{_WHITE};padding:8px 12px;text-align:left;font-weight:600}}
td{{padding:8px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
tr:hover td{{background:#f0f4ff}}
.btn{{display:inline-block;padding:5px 12px;border-radius:4px;font-size:12px;
      font-weight:600;text-decoration:none;border:none;cursor:pointer}}
.btn-primary{{background:{_ORANGE};color:{_WHITE}}}
.btn-success{{background:#198754;color:{_WHITE}}}
.btn-danger{{background:#DC3545;color:{_WHITE}}}
.btn-secondary{{background:#6C757D;color:{_WHITE}}}
.btn:hover{{opacity:.85}}
.flt-btn{{background:{_LIGHT};border:1px solid {_BORDER};border-radius:4px;
          padding:4px 10px;font-size:12px;cursor:pointer;margin-right:4px}}
.flt-btn.active,.flt-btn:hover{{background:{_NAVY};color:{_WHITE};border-color:{_NAVY}}}
pre{{background:#1E2761;color:#E8F0FE;padding:14px;border-radius:6px;
     font-size:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
.tab-btn{{background:none;border:none;border-bottom:3px solid transparent;
          padding:12px 18px;font-size:14px;font-weight:500;cursor:pointer;color:#555;
          margin-bottom:-2px}}
.tab-btn.active{{color:{_NAVY};border-bottom-color:{_ORANGE};font-weight:700}}
.tab-pane{{display:none}}.tab-pane.active{{display:block}}
.signal-pass{{color:#198754}}.signal-fail{{color:#DC3545}}.signal-warn{{color:#FFC107}}
input[type=text],textarea{{width:100%;padding:8px 10px;border:1px solid {_BORDER};
  border-radius:4px;font-size:13px;font-family:inherit}}
textarea{{height:80px;resize:vertical}}
label{{font-size:13px;font-weight:600;display:block;margin-bottom:4px}}
.form-row{{margin-bottom:12px}}
.alert-warn{{background:#FFF3CD;border:1px solid #FFC107;border-radius:4px;
             padding:10px 14px;font-size:13px;margin-bottom:12px}}
</style>
</head>
<body>
<div class="hdr">
  <svg width="88" height="22" viewBox="0 0 88 22" xmlns="http://www.w3.org/2000/svg">
    <text x="0" y="18" font-family="Inter,sans-serif" font-size="17" font-weight="700"
          letter-spacing="-.3" fill="#fff">Teradata</text>
  </svg>
  <div class="hdr-title">SHIPS Deployment Dashboard</div>
  <div style="margin-left:auto">
    <a href="/">← All Packages</a>
  </div>
</div>
{body}
<script>
function switchTab(btn,pane){{
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(pane).classList.add('active');
}}
function copyText(id){{
  var el=document.getElementById(id);
  navigator.clipboard.writeText(el.innerText).then(()=>{{
    var btn=el.nextElementSibling;
    var orig=btn.textContent;
    btn.textContent='Copied!';btn.style.background='#198754';
    setTimeout(()=>{{btn.textContent=orig;btn.style.background='{_ORANGE}';}},1500);
  }});
}}
function filterStatus(val){{
  document.querySelectorAll('.flt-btn').forEach(b=>{{
    b.classList.toggle('active',
      val==='all'?b.dataset.filter==='all':b.dataset.filter===val);
  }});
  document.querySelectorAll('tr[data-trust]').forEach(row=>{{
    if(val==='all'){{row.style.display='';return;}}
    if(val==='awaiting'){{
      row.style.display=row.dataset.approval==='awaiting'?'':'none';
    }}else{{
      row.style.display=row.dataset.trust===val?'':'none';
    }}
  }});
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


def create_app(project_dirs: List[str]) -> "FastAPI":
    """Create and return the FastAPI application."""
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError(
            "FastAPI is required for the dashboard. "
            "Install it with: uv pip install -e '.[dashboard]'"
        )

    app = FastAPI(title="SHIPS Deployment Dashboard", docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------
    # Home — package registry
    # ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        packages = scan_all_projects(project_dirs)

        ready = sum(1 for p in packages if p.trust_label == "READY")
        caveats = sum(1 for p in packages if p.trust_label == "READY-WITH-CAVEATS")
        blocked = sum(1 for p in packages if p.trust_label == "BLOCKED")
        awaiting = sum(1 for p in packages if p.approval_status == "Awaiting")

        summary = f"""
<div class="sum-bar">
  <span><span class="sum-val">{len(packages)}</span> packages</span>
  <span><span class="sum-val" style="color:#6dcc85">{ready}</span> READY</span>
  <span><span class="sum-val" style="color:#ffc107">{caveats}</span> CAVEATS</span>
  <span><span class="sum-val" style="color:#f77">{blocked}</span> BLOCKED</span>
  <span><span class="sum-val" style="color:#ffc107">{awaiting}</span> awaiting approval</span>
</div>"""

        filters = """
<div style="padding:12px 24px;background:#fff;border-bottom:1px solid #dee2e6;
     display:flex;gap:6px;align-items:center;flex-wrap:wrap">
  <span style="font-size:13px;font-weight:600;margin-right:4px">Filter:</span>
  <button class="flt-btn active" data-filter="all" onclick="filterStatus('all')">All</button>
  <button class="flt-btn" data-filter="ready" onclick="filterStatus('ready')">READY</button>
  <button class="flt-btn" data-filter="ready-with-caveats"
    onclick="filterStatus('ready-with-caveats')">CAVEATS</button>
  <button class="flt-btn" data-filter="blocked"
    onclick="filterStatus('blocked')">BLOCKED</button>
  <button class="flt-btn" data-filter="awaiting"
    onclick="filterStatus('awaiting')">Awaiting approval</button>
</div>"""

        rows = ""
        for pkg in packages:
            trust_data = pkg.trust_label.lower()
            approval_data = pkg.approval_status.lower()
            short_commit = pkg.source_commit[:8] if pkg.source_commit else "—"
            dirty_note = " ⚠ dirty" if pkg.source_dirty else ""
            actions = f'<a class="btn btn-primary" href="/package/{pkg.archive_stem}">View</a>'
            if pkg.approval_status == "Awaiting":
                actions += (
                    f' &nbsp;<a class="btn btn-success" href="/approve/{pkg.archive_stem}"'
                    f" onclick=\"return confirm('Approve {pkg.archive_filename}?')\">✓ Approve</a>"
                    f' <a class="btn btn-danger" href="/reject-form/{pkg.archive_stem}">✗ Reject</a>'
                )
            rows += (
                f'<tr data-trust="{trust_data}" data-approval="{approval_data}">'
                f"<td style='color:#555'>{pkg.project_name}</td>"
                f"<td><strong>{pkg.package_name}</strong></td>"
                f"<td style='font-family:monospace'>{pkg.build_number}</td>"
                f"<td>{pkg.environment}</td>"
                f"<td>{_trust_badge(pkg.trust_label)}</td>"
                f"<td>{_approval_badge(pkg)}</td>"
                f"<td style='font-family:monospace;font-size:11px;color:#555'>"
                f"{short_commit}{dirty_note}</td>"
                f"<td>{actions}</td>"
                "</tr>"
            )

        table = f"""
<div class="content" style="padding-top:12px">
<div class="card" style="padding:0;overflow:hidden">
<table>
<thead>
<tr>
  <th>Project</th><th>Package</th><th>Build</th><th>Env</th>
  <th>Trust</th><th>Approval</th><th>Commit</th><th>Actions</th>
</tr>
</thead>
<tbody>{rows or '<tr><td colspan="8" style="padding:24px;color:#6C757D;text-align:center">No packages found. Check --projects configuration.</td></tr>'}</tbody>
</table>
</div>
</div>"""

        return _page("SHIPS Dashboard", summary + filters + table)

    # ---------------------------------------------------------------
    # Package detail
    # ---------------------------------------------------------------

    def _find_pkg(archive_stem: str) -> Optional[PackageInfo]:
        for pkg in scan_all_projects(project_dirs):
            if pkg.archive_stem == archive_stem:
                return pkg
        return None

    @app.get("/package/{archive_stem}", response_class=HTMLResponse)
    async def package_detail(archive_stem: str):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return HTMLResponse("<h2>Package not found</h2>", status_code=404)

        # -- Trust tab --
        signal_rows = ""
        for name, sig in pkg.trust_signals.items():
            status = sig.get("status", "?") if isinstance(sig, dict) else str(sig)
            detail = sig.get("detail", "") if isinstance(sig, dict) else ""
            icon_cls = {
                "pass": "signal-pass",
                "fail": "signal-fail",
                "warn": "signal-warn",
            }.get(status, "")
            icon = {"pass": "✓", "fail": "✗", "warn": "⚠"}.get(status, "?")
            signal_rows += (
                f"<tr><td style='font-family:monospace'>{name}</td>"
                f"<td class='{icon_cls}' style='font-weight:600'>{icon} {status}</td>"
                f"<td style='color:#555;font-size:12px'>{detail}</td></tr>"
            )
        if not signal_rows:
            signal_rows = '<tr><td colspan="3" style="color:#6C757D;padding:16px">No signals.</td></tr>'

        trust_tab = f"""
{_trust_badge(pkg.trust_label)}
<div style="margin-top:16px">
<table>
<thead><tr>
  <th>Signal</th><th>Status</th><th>Detail</th>
</tr></thead>
<tbody>{signal_rows}</tbody>
</table>
</div>"""

        # -- Compliance tab --
        dbql = generate_dbql_query(pkg.package_name, pkg.build_number)
        requires_note = ""
        if pkg.requires:
            requires_note = (
                f'<div class="alert-warn">⚠ This package requires the following '
                f"companion archive(s) to be deployed first: "
                f"<strong>{'</strong>, <strong>'.join(pkg.requires)}</strong></div>"
            )
        report_link = ""
        if pkg.has_report:
            report_link = (
                '<div style="margin-top:16px">'
                "<strong>Interactive Package Report</strong><br>"
                '<span style="font-size:13px;color:#555">Extract the archive and open '
                "<code>package_report.html</code> in a browser for the full object "
                "inventory, wave plan, and deploy commands.</span>"
                "</div>"
            )

        compliance_tab = f"""
{requires_note}
<div class="form-row">
  <label>DBQL Audit Query</label>
  <span style="font-size:12px;color:#555">
    Run in Teradata to find all statements from this build in DBQL.
  </span>
  <div style="position:relative;margin-top:6px">
    <pre id="dbql-q">{dbql}</pre>
    <button class="btn btn-primary"
      onclick="copyText('dbql-q')"
      style="position:absolute;top:8px;right:8px">Copy</button>
  </div>
</div>
<div class="form-row" style="margin-top:16px">
  <label>Package Metadata</label>
  <table style="font-size:13px;margin-top:6px">
    <tr><td style="color:#555;padding:4px 8px 4px 0;width:140px">Package name</td>
        <td style="font-family:monospace">{pkg.package_name}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Build number</td>
        <td style="font-family:monospace">{pkg.build_number}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Environment</td>
        <td>{pkg.environment}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Timestamp</td>
        <td>{pkg.timestamp}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Author</td>
        <td>{pkg.author or "—"}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Source commit</td>
        <td style="font-family:monospace">{pkg.source_commit[:16] if pkg.source_commit else "—"}
        {"<span style='color:#FFC107'> ⚠ dirty build</span>" if pkg.source_dirty else ""}
        </td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Files</td>
        <td>{pkg.file_count}</td></tr>
    <tr><td style="color:#555;padding:4px 8px 4px 0">Archive</td>
        <td style="font-family:monospace;font-size:11px">{pkg.archive_filename}</td></tr>
  </table>
</div>
{report_link}"""

        # -- Actions tab --
        if pkg.approved:
            approval_section = (
                f'<div style="color:#198754;font-weight:600;margin-bottom:12px">'
                f"✓ This package has been approved.</div>"
                f'<form method="post" action="/reject-submit/{pkg.archive_stem}">'
                f'<div class="form-row"><label>Revoke approval / reject</label>'
                f'<textarea name="reason" placeholder="Reason for rejection..."></textarea></div>'
                f'<button class="btn btn-danger" type="submit">Revoke &amp; Reject</button>'
                f"</form>"
            )
        elif pkg.rejected:
            approval_section = (
                f'<div style="color:#DC3545;font-weight:600;margin-bottom:6px">'
                f"✗ Rejected</div>"
                f'<div style="font-size:13px;color:#555;margin-bottom:12px">'
                f"Reason: {pkg.rejection_reason}</div>"
                f'<a class="btn btn-success" href="/approve/{pkg.archive_stem}"'
                f" onclick=\"return confirm('Approve this package?')\">✓ Approve</a>"
            )
        else:
            approval_section = (
                f'<div style="margin-bottom:20px">'
                f'<a class="btn btn-success" href="/approve/{pkg.archive_stem}"'
                f" onclick=\"return confirm('Approve {pkg.archive_filename}?')\" "
                f'style="margin-right:8px">✓ Approve</a>'
                f'<a class="btn btn-danger" href="/reject-form/{pkg.archive_stem}">✗ Reject</a>'
                f"</div>"
            )

        actions_tab = f"""
<div style="margin-bottom:24px">
  <strong>Approval</strong>
  <div style="margin-top:10px">{approval_section}</div>
</div>
<div>
  <strong>Deploy commands</strong>
  <div style="margin-top:10px;font-size:13px;color:#555;margin-bottom:8px">
    Extract the package archive, then run from the extracted directory:
  </div>
  <div style="margin-bottom:10px;position:relative">
    <pre id="cmd-dry">python deploy.py --host &lt;host&gt; --user &lt;user&gt; --dry-run</pre>
    <button class="btn btn-primary" onclick="copyText('cmd-dry')"
      style="position:absolute;top:8px;right:8px">Copy</button>
  </div>
  <div style="position:relative">
    <pre id="cmd-live">python deploy.py --host &lt;host&gt; --user &lt;user&gt; --streams 4</pre>
    <button class="btn btn-primary" onclick="copyText('cmd-live')"
      style="position:absolute;top:8px;right:8px">Copy</button>
  </div>
</div>"""

        body = f"""
<div class="sum-bar" style="justify-content:space-between">
  <span style="font-size:15px;font-weight:700">
    {pkg.package_name} &nbsp;·&nbsp; Build {pkg.build_number} &nbsp;·&nbsp; {pkg.environment}
  </span>
  <span>{_trust_badge(pkg.trust_label)} &nbsp; {_approval_badge(pkg)}</span>
</div>
<div class="content">
  <div class="card" style="padding:0">
    <div style="border-bottom:2px solid {_BORDER};padding:0 24px;display:flex">
      <button class="tab-btn active" onclick="switchTab(this,'tab-trust')">Trust Report</button>
      <button class="tab-btn" onclick="switchTab(this,'tab-compliance')">Compliance</button>
      <button class="tab-btn" onclick="switchTab(this,'tab-actions')">Actions</button>
    </div>
    <div style="padding:20px 24px">
      <div id="tab-trust" class="tab-pane active">{trust_tab}</div>
      <div id="tab-compliance" class="tab-pane">{compliance_tab}</div>
      <div id="tab-actions" class="tab-pane">{actions_tab}</div>
    </div>
  </div>
</div>"""

        return _page(f"SHIPS — {pkg.package_name} {pkg.build_number}", body)

    # ---------------------------------------------------------------
    # Approval actions
    # ---------------------------------------------------------------

    @app.get("/approve/{archive_stem}")
    async def approve(archive_stem: str):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return HTMLResponse("<h2>Package not found</h2>", status_code=404)
        write_approval(pkg.archive_path, approved=True)
        return RedirectResponse(f"/package/{archive_stem}", status_code=303)

    @app.get("/reject-form/{archive_stem}", response_class=HTMLResponse)
    async def reject_form(archive_stem: str):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return HTMLResponse("<h2>Package not found</h2>", status_code=404)
        body = f"""
<div class="content">
<div class="card" style="max-width:480px">
  <h2 style="margin-bottom:16px;font-size:16px">Reject package</h2>
  <p style="font-size:13px;color:#555;margin-bottom:16px">
    <strong>{pkg.package_name}</strong> Build {pkg.build_number} ({pkg.environment})
  </p>
  <form method="post" action="/reject-submit/{archive_stem}">
    <div class="form-row">
      <label>Reason for rejection</label>
      <textarea name="reason" placeholder="Describe why this package is being rejected..."
        required></textarea>
    </div>
    <button class="btn btn-danger" type="submit">✗ Reject package</button>
    &nbsp;
    <a class="btn btn-secondary" href="/package/{archive_stem}">Cancel</a>
  </form>
</div>
</div>"""
        return _page(f"Reject — {pkg.package_name}", body)

    @app.post("/reject-submit/{archive_stem}")
    async def reject_submit(archive_stem: str, reason: str = Form("")):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return HTMLResponse("<h2>Package not found</h2>", status_code=404)
        write_approval(pkg.archive_path, approved=False, reason=reason)
        return RedirectResponse(f"/package/{archive_stem}", status_code=303)

    # ---------------------------------------------------------------
    # JSON API (for agent consumption)
    # ---------------------------------------------------------------

    @app.get("/api/packages")
    async def api_packages():
        packages = scan_all_projects(project_dirs)
        return [
            {
                "archive_stem": p.archive_stem,
                "project": p.project_name,
                "package_name": p.package_name,
                "build_number": p.build_number,
                "environment": p.environment,
                "trust_label": p.trust_label,
                "approval_status": p.approval_status,
                "file_count": p.file_count,
                "timestamp": p.timestamp,
            }
            for p in packages
        ]

    @app.get("/api/package/{archive_stem}/build_json")
    async def api_build_json(archive_stem: str):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        build = read_build_json_from_zip(pkg.archive_path)
        return build or {}

    @app.get("/api/package/{archive_stem}/dbql_query")
    async def api_dbql_query(archive_stem: str):
        pkg = _find_pkg(archive_stem)
        if pkg is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"query": generate_dbql_query(pkg.package_name, pkg.build_number)}

    return app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser(
        description="SHIPS Deployment Dashboard — cross-project package visibility"
    )
    p.add_argument(
        "--projects",
        required=True,
        help="Comma-separated list of project directories to scan "
        "(e.g. /projects/OMR,/projects/GCFR)",
    )
    p.add_argument(
        "--port", type=int, default=8000, help="Port to listen on (default: 8000)"
    )
    p.add_argument(
        "--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)"
    )
    return p.parse_args()


def main():
    args = _parse_args()
    project_dirs = [d.strip() for d in args.projects.split(",") if d.strip()]

    if not _FASTAPI_AVAILABLE:
        print(
            "ERROR: FastAPI is required. Install with:\n"
            "  uv pip install -e '.[dashboard]'"
        )
        raise SystemExit(1)

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is required. Install with:\n  uv pip install uvicorn")
        raise SystemExit(1)

    app = create_app(project_dirs)
    print("\n  SHIPS Deployment Dashboard")
    print(f"  Scanning: {', '.join(project_dirs)}")
    print(f"  Open:     http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
