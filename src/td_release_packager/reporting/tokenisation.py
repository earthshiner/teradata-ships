"""
tokenisation.py — Pre-package tokenisation preview (#325).

Renders the pipeline report's Tokenisation tab: a per-environment resolution
matrix plus, for one focused environment, before/after rendered examples of
``{{TOKEN}}`` substitution.  The point is to catch incomplete or wrong
tokenisation *before* a useless package is built.

Detection of undefined / unused tokens already lives in the SHIPS token
engine — this module surfaces it (it does not re-implement it) and adds two
checks the engine lacks: value **collisions** and **empty** resolutions.

Crucially, the preview is **secret-free**: it never resolves ``$env:`` /
``vault:`` references (which would require the real secret to be present and
would risk leaking it into the bundled HTML).  Secret references and
sensitive token names are replaced with placeholders, and only internal
``{{TOKEN}}`` references among plain values are resolved.  Substitution uses
the real ``token_engine.substitute_tokens`` so plain-value rendering is
faithful.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from td_release_packager.reporting import common, redaction
from td_release_packager.reporting.common import h

logger = logging.getLogger(__name__)

# Matches a well-formed token, mirroring token_engine._TOKEN_RE.
_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_-]*)\}\}")

# Project payload tree scanned for token references (matches pipeline_report).
_PAYLOAD_SUBPATH = os.path.join("payload", "database")
_ENV_GLOB_SUBPATH = os.path.join("config", "env")

# Reserved metadata keys — not deployment tokens; excluded from "unused".
_RESERVED = {"SHIPS_ENV", "SHIPS_PROJECT", "ENV_PREFIX"}

# Internal-reference resolution passes (circular-reference guard).
_MAX_RESOLVE_PASSES = 10


# ---------------------------------------------------------------------------
# Env config reading + secret-free resolution
# ---------------------------------------------------------------------------


def parse_raw_conf(path: str) -> Dict[str, str]:
    """Parse a ``.conf`` into raw token→value pairs WITHOUT resolution.

    Performs no secret or internal-reference resolution and never raises —
    raw values are needed to detect ``$env:`` / ``vault:`` secret references
    for redaction. Returns an empty dict on any read error.
    """
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, value = stripped.split("=", 1)
                name = name.strip()
                if name:
                    values[name] = value.strip()
    except OSError as exc:
        logger.debug("tokenisation: could not read %s: %s", path, exc)
    return values


def preview_resolve(raw: Dict[str, str]) -> Dict[str, str]:
    """Resolve internal ``{{TOKEN}}`` references without touching secrets.

    Secret-reference and sensitive tokens are seeded with a redacted
    placeholder before resolution, so a secret value is never produced even
    transitively (e.g. ``CONN={{DB_PASSWORD}}@host`` resolves to
    ``«masked»@host``). Unresolvable references are left literal.
    """
    work: Dict[str, str] = {}
    for name, value in raw.items():
        if redaction.is_redacted(name, value):
            work[name] = redaction.masked_display(name, value, None)
        else:
            work[name] = value

    for _ in range(_MAX_RESOLVE_PASSES):
        changed = False
        for name, value in list(work.items()):
            if "{{" not in value:
                continue

            def _repl(match: re.Match) -> str:
                nonlocal changed
                token = match.group(1)
                if token in work:
                    changed = True
                    return work[token]
                return match.group(0)  # leave unresolved references literal

            work[name] = _TOKEN_RE.sub(_repl, value)
        if not changed:
            break
    return work


def list_env_configs(project_dir: str) -> List[Tuple[str, str]]:
    """Return ``(env_name, path)`` for every ``config/env/*.conf``, sorted."""
    env_dir = os.path.join(project_dir, _ENV_GLOB_SUBPATH)
    if not os.path.isdir(env_dir):
        return []
    out: List[Tuple[str, str]] = []
    for fname in sorted(os.listdir(env_dir)):
        if fname.endswith(".conf"):
            out.append((os.path.splitext(fname)[0], os.path.join(env_dir, fname)))
    return out


def _token_usage(project_dir: str) -> Dict[str, Set[str]]:
    """Return ``{file_path: {token_names}}`` for the project payload."""
    payload_dir = os.path.join(project_dir, _PAYLOAD_SUBPATH)
    if not os.path.isdir(payload_dir):
        return {}
    try:
        from td_release_packager.token_engine import scan_tokens_in_directory

        return scan_tokens_in_directory(payload_dir, project_dir=project_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("tokenisation: token scan failed: %s", exc)
        return {}


def _referenced_tokens(usage: Dict[str, Set[str]]) -> Set[str]:
    referenced: Set[str] = set()
    for tokens in usage.values():
        referenced.update(tokens)
    return referenced


def _collision_groups(
    resolved: Dict[str, str], raw: Dict[str, str]
) -> List[Tuple[str, List[str]]]:
    """Return ``(value, [token_names])`` for plain values shared by >1 token.

    Redacted tokens (which all share a placeholder) and empty values are
    excluded so the check only flags genuine plain-value collisions.
    """
    by_value: Dict[str, List[str]] = {}
    for name, value in resolved.items():
        if value == "" or redaction.is_redacted(name, raw.get(name)):
            continue
        by_value.setdefault(value, []).append(name)
    groups = [
        (value, sorted(names)) for value, names in by_value.items() if len(names) > 1
    ]
    return sorted(groups, key=lambda g: g[1])


def _env_summary(env_name: str, path: str, referenced: Set[str]) -> dict:
    """Compute the resolution summary for one environment (never raises)."""
    raw = parse_raw_conf(path)
    resolved = preview_resolve(raw)
    defined = set(raw.keys())

    undefined = sorted(referenced - defined)
    unused = sorted(defined - referenced - _RESERVED)
    empty = sorted(
        t
        for t in defined
        if resolved.get(t, "") == "" and not redaction.is_redacted(t, raw.get(t))
    )
    collisions = _collision_groups(resolved, raw)

    if undefined:
        status = "error"
    elif unused or empty or collisions:
        status = "warning"
    else:
        status = "success"
    return {
        "env": env_name,
        "status": status,
        "defined": len(defined),
        "undefined": undefined,
        "unused": unused,
        "empty": empty,
        "collisions": collisions,
    }


def _display_sub_map(referenced: Set[str], resolved: Dict[str, str]) -> Dict[str, str]:
    """Build a render-safe substitution map covering every referenced token.

    Undefined tokens stay literal (so they visibly survive in the "after"
    column); defined tokens use their secret-free resolved value (already
    masked for secret/sensitive tokens). Covers every referenced token so
    ``substitute_tokens`` never raises ``KeyError``.
    """
    sub: Dict[str, str] = {}
    for token in referenced:
        sub[token] = resolved.get(token, f"{{{{{token}}}}}")
    return sub


def _token_examples(
    usage: Dict[str, Set[str]],
    referenced: Set[str],
    sub_map: Dict[str, str],
) -> List[dict]:
    """Return one before/after example line per referenced token."""
    from td_release_packager.token_engine import substitute_tokens

    seen: Dict[str, dict] = {}
    for file_path in sorted(usage):
        if len(seen) == len(referenced):
            break
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            for match in _TOKEN_RE.finditer(line):
                token = match.group(1)
                if token in referenced and token not in seen:
                    try:
                        after, _ = substitute_tokens(line, sub_map)
                    except KeyError:
                        after = line
                    seen[token] = {
                        "token": token,
                        "before": line.strip(),
                        "after": after.strip(),
                    }
    return [seen[t] for t in sorted(seen)]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _matrix_html(summaries: List[dict]) -> str:
    """Render the per-environment resolution matrix table."""

    def cell(value: int, danger: bool = False) -> str:
        colour = "#DC3545" if danger and value else "#333"
        return f'<td style="padding:7px 12px;color:{colour}">{h(value)}</td>'

    rows = "".join(
        f'<tr><td style="padding:7px 12px">'
        f"{common.stage_status_badge(s['status'], s['env'])}</td>"
        f'<td style="padding:7px 12px">{s["defined"]}</td>'
        f"{cell(len(s['undefined']), danger=True)}"
        f"{cell(len(s['unused']))}"
        f"{cell(len(s['empty']), danger=True)}"
        f"{cell(len(s['collisions']), danger=True)}</tr>"
        for s in summaries
    )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px">'
        f'<thead><tr style="background:{common.NAVY};color:{common.WHITE}">'
        '<th style="padding:8px 12px;text-align:left">Environment</th>'
        '<th style="padding:8px 12px;text-align:left">Defined</th>'
        '<th style="padding:8px 12px;text-align:left">Undefined</th>'
        '<th style="padding:8px 12px;text-align:left">Unused</th>'
        '<th style="padding:8px 12px;text-align:left">Empty</th>'
        '<th style="padding:8px 12px;text-align:left">Collisions</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _flags_html(summary: dict) -> str:
    """Render the undefined/empty/collision detail for the focused env."""
    parts: List[str] = []
    if summary["undefined"]:
        items = ", ".join(f"<code>{{{{{h(t)}}}}}</code>" for t in summary["undefined"])
        parts.append(
            f'<div style="background:#F8D7DA;color:#721C24;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:8px;font-size:13px">'
            f"<strong>Undefined</strong> — referenced but not in this env config; "
            f"these will ship unresolved: {items}</div>"
        )
    if summary["collisions"]:
        rows = "".join(
            f"<li><code>{h(', '.join(names))}</code> → <code>{h(value)}</code></li>"
            for value, names in summary["collisions"]
        )
        parts.append(
            f'<div style="background:#FFF3CD;color:#856404;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:8px;font-size:13px">'
            f"<strong>Collisions</strong> — multiple tokens resolve to the same "
            f"value:<ul style='margin:6px 0 0 18px'>{rows}</ul></div>"
        )
    if summary["empty"]:
        items = ", ".join(f"<code>{h(t)}</code>" for t in summary["empty"])
        parts.append(
            f'<div style="background:#FFF3CD;color:#856404;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:8px;font-size:13px">'
            f"<strong>Empty</strong> — resolve to an empty string (may produce "
            f"malformed identifiers): {items}</div>"
        )
    return "".join(parts)


def _examples_html(examples: List[dict], sub_map: Dict[str, str]) -> str:
    """Render the token-by-token before/after table."""
    if not examples:
        return (
            '<p style="color:#6C757D;padding:12px">No token references found in '
            "the payload.</p>"
        )
    rows = []
    for ex in examples:
        token = ex["token"]
        resolved_to = sub_map.get(token, "")
        unresolved = resolved_to == f"{{{{{token}}}}}"
        value_cell = (
            '<span style="color:#DC3545">unresolved</span>'
            if unresolved
            else f"<code>{h(resolved_to)}</code>"
        )
        rows.append(
            f'<tr style="border-bottom:1px solid #f0f0f0">'
            f'<td style="padding:7px 12px;font-family:monospace;white-space:nowrap">'
            f"{{{{{h(token)}}}}}</td>"
            f'<td style="padding:7px 12px">{value_cell}</td>'
            f'<td style="padding:7px 12px;font-family:monospace;font-size:12px;color:#777">'
            f"{h(ex['before'])}</td>"
            f'<td style="padding:7px 12px;font-family:monospace;font-size:12px;color:#0D6EFD">'
            f"{h(ex['after'])}</td></tr>"
        )
    return (
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<thead><tr style="background:{common.NAVY};color:{common.WHITE}">'
        '<th style="padding:8px 12px;text-align:left">Token</th>'
        '<th style="padding:8px 12px;text-align:left">Resolves to</th>'
        '<th style="padding:8px 12px;text-align:left">Before</th>'
        '<th style="padding:8px 12px;text-align:left">After</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _focused_env(configs: List[Tuple[str, str]]) -> Optional[str]:
    """Pick the focused environment for examples (first, alphabetically)."""
    return configs[0][0] if configs else None


def tokenisation_tab(project_dir: str) -> str:
    """Render the Tokenisation tab for a project. Always returns HTML."""
    configs = list_env_configs(project_dir)
    if not configs:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "No environment configs found under <code>config/env/*.conf</code>. "
            "Add one to preview tokenisation.</p>"
        )

    usage = _token_usage(project_dir)
    referenced = _referenced_tokens(usage)
    if not referenced:
        return (
            '<p style="color:#6C757D;padding:24px;text-align:center">'
            "No <code>{{TOKEN}}</code> references found in the payload — nothing "
            "to substitute.</p>"
        )

    summaries = [_env_summary(name, path, referenced) for name, path in configs]
    focused = _focused_env(configs)

    note = (
        '<p style="font-size:13px;color:#555;margin-bottom:12px">'
        "What packaging would produce per environment. Secret references "
        "(<code>$env:</code> / <code>vault:</code>) and sensitive token names "
        "are <strong>redacted</strong> — this report ships inside the package, "
        "so values are never resolved against real secrets.</p>"
    )
    matrix = (
        f'<h3 style="font-size:14px;color:{common.NAVY};margin:4px 0 10px">'
        f"Resolution by environment</h3>{_matrix_html(summaries)}"
    )
    body = note + matrix

    if focused is None:
        return body

    focused_path = dict(configs)[focused]
    resolved = preview_resolve(parse_raw_conf(focused_path))
    focused_summary = next(s for s in summaries if s["env"] == focused)

    body += (
        f'<h3 style="font-size:14px;color:{common.NAVY};margin:20px 0 10px">'
        f"Rendered examples — environment <code>{h(focused)}</code></h3>"
    )
    body += _flags_html(focused_summary)

    sub_map = _display_sub_map(referenced, resolved)
    examples = _token_examples(usage, referenced, sub_map)
    body += _examples_html(examples, sub_map)
    return body
