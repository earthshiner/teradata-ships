"""
report_viewer.py — Shared SQL source viewer helpers.

Used by both the deploy-time report (report.py) and the package-time
report (package_report.py) to produce syntax-highlighted standalone
HTML viewer pages that open when a user clicks a script hyperlink.

The viewer pages are purely static HTML — no server, no CDN, no
external network requests.  They can be opened directly from the
filesystem (file: URL) or from within a package ZIP after extraction.

Public API
----------
highlight_sql(source)
    Return an HTML string with Teradata SQL keywords, string literals,
    and comments wrapped in ``<span>`` elements.

source_viewer_html(*, title, packaged_path, source_path, content)
    Return a complete standalone HTML document for one source file.

safe_viewer_filename(final_path, index)
    Return a filesystem-safe filename for a viewer page derived from
    the payload-relative path of the corresponding source file.
"""

from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# Teradata SQL keyword set — used for syntax highlighting.
# ---------------------------------------------------------------------------

SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "ABORT",
        "ADD",
        "ALTER",
        "AND",
        "AS",
        "BEGIN",
        "BETWEEN",
        "BY",
        "CALL",
        "CASE",
        "COLLECT",
        "COLUMN",
        "COMMENT",
        "CREATE",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "DATABASE",
        "DEFAULT",
        "DELETE",
        "DROP",
        "ELSE",
        "END",
        "EXECUTE",
        "EXTERNAL",
        "FROM",
        "FUNCTION",
        "GRANT",
        "GROUP",
        "IF",
        "IN",
        "INDEX",
        "INNER",
        "INSERT",
        "JOIN",
        "LEFT",
        "MACRO",
        "MERGE",
        "MULTISET",
        "NOT",
        "NULL",
        "ON",
        "OR",
        "ORDER",
        "OUT",
        "PROCEDURE",
        "REPLACE",
        "REVOKE",
        "RIGHT",
        "ROLE",
        "SELECT",
        "SET",
        "TABLE",
        "THEN",
        "TO",
        "TRIGGER",
        "UPDATE",
        "USER",
        "VALUES",
        "VIEW",
        "VOLATILE",
        "WHEN",
        "WHERE",
    }
)

# Matches -- line comments, /* block comments */, single-quoted strings,
# and bare identifiers (keywords and object names).  Order matters: the
# alternation tries comment/string patterns before the identifier fallback
# so embedded SQL-looking text inside strings is not misclassified.
_SQL_TOKEN_RE: re.Pattern[str] = re.compile(
    r"(--[^\r\n]*|/\*.*?\*/|'(?:''|[^'])*'|\b[A-Za-z_][A-Za-z0-9_]*\b)",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Teradata brand colours — duplicated here so this module is self-contained
# and does not create a circular import with report.py.
# ---------------------------------------------------------------------------
_NAVY = "#00233C"
_ORANGE = "#FF5F02"


def _escape(value: str) -> str:
    """Escape a string for safe inclusion in HTML text content."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def highlight_sql(source: str) -> str:
    """Return HTML with Teradata SQL keyword, comment, and string highlighting.

    Processes ``source`` character-by-character using a compiled regex
    that matches comments, string literals, and identifiers in priority
    order so that SQL-looking text embedded inside string literals is
    not inadvertently highlighted as a keyword.

    Args:
        source: Raw SQL source text to highlight.

    Returns:
        HTML string with ``<span class="sql-*">`` wrappers applied to
        keywords, string literals, and comments.  All other characters
        are HTML-escaped but otherwise unchanged.
    """
    pieces: list[str] = []
    pos = 0
    for match in _SQL_TOKEN_RE.finditer(source):
        # Escape and emit any literal text before this token.
        pieces.append(_escape(source[pos : match.start()]))
        token = match.group(0)
        escaped = _escape(token)
        upper = token.upper()
        if token.startswith("--") or token.startswith("/*"):
            pieces.append(f'<span class="sql-comment">{escaped}</span>')
        elif token.startswith("'"):
            pieces.append(f'<span class="sql-string">{escaped}</span>')
        elif upper in SQL_KEYWORDS:
            pieces.append(f'<span class="sql-keyword">{escaped}</span>')
        else:
            pieces.append(escaped)
        pos = match.end()
    # Emit any trailing text after the final token.
    pieces.append(_escape(source[pos:]))
    return "".join(pieces)


def source_viewer_html(
    *,
    title: str,
    packaged_path: str,
    source_path: str,
    content: str,
) -> str:
    """Build a standalone highlighted source viewer page.

    The page is self-contained HTML — no external resources, no
    JavaScript.  It can be opened directly from a filesystem path
    (``file:`` URL) without a web server.

    Args:
        title:         Browser ``<title>`` and ``<h1>`` text.
        packaged_path: Package-relative path shown as metadata, e.g.
                       ``payload/03_ddl/tables/DB.Customer.tbl``.
        source_path:   Original project source path shown as metadata.
        content:       Raw SQL text to display with syntax highlighting.

    Returns:
        Complete HTML document as a string.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_escape(title)}</title>
<style>
body {{
  margin: 0;
  background: #F8F9FA;
  color: {_NAVY};
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
header {{
  background: {_NAVY};
  color: white;
  padding: 16px 22px;
  border-bottom: 4px solid {_ORANGE};
}}
h1 {{
  margin: 0 0 8px 0;
  font-size: 18px;
  font-weight: 700;
}}
.meta {{
  font-family: Consolas, "Courier New", monospace;
  font-size: 12px;
  line-height: 1.5;
  color: #DDEAF3;
}}
main {{
  padding: 18px 22px;
}}
pre {{
  margin: 0;
  padding: 18px;
  overflow: auto;
  background: #FFFFFF;
  border: 1px solid #DEE2E6;
  border-radius: 6px;
  line-height: 1.5;
  font-family: Consolas, "Courier New", monospace;
  font-size: 13px;
  tab-size: 4;
  white-space: pre;
}}
.sql-keyword {{ color: #0D6EFD; font-weight: 700; }}
.sql-string  {{ color: #198754; }}
.sql-comment {{ color: #6C757D; font-style: italic; }}
</style>
</head>
<body>
<header>
<h1>{_escape(packaged_path.rsplit("/", 1)[-1] or title)}</h1>
<div class="meta">Package: {_escape(packaged_path)}</div>
<div class="meta">Source:  {_escape(source_path)}</div>
</header>
<main>
<pre><code>{highlight_sql(content)}</code></pre>
</main>
</body>
</html>"""


def safe_viewer_filename(final_path: str, index: int) -> str:
    """Create a filesystem-safe viewer filename for a payload path.

    Replaces every character that is not alphanumeric, an underscore,
    a hyphen, or a period with an underscore, then prefixes with a
    zero-padded index so files sort in the order they were written.
    Long stems are capped and hash-suffixed to keep extracted SHIPS
    packages below Windows path-length limits.

    Args:
        final_path: Payload-relative path, e.g.
                    ``03_ddl/tables/DB.Customer.tbl``.
        index:      Monotonically increasing integer used as a sort prefix.

    Returns:
        A filename such as ``0001_03_ddl_tables_DB.Customer.tbl.html``.
    """
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", final_path.replace("\\", "/")).strip("_")
    if not stem:
        stem = f"source_{index}"
    if len(stem) > 72:
        digest = hashlib.sha1(final_path.encode("utf-8")).hexdigest()[:10]
        stem = f"{stem[:57].rstrip('_.-')}_{digest}"
    return f"{index:04d}_{stem}.html"


# ---------------------------------------------------------------------------
# Trust signal explanation lookup — shared by package_report and deploy report
# ---------------------------------------------------------------------------

# Each entry is (short_title, what_it_checks, what_to_do_on_fail).
# Both the package-time report and the deploy-time report use this table so
# the plain-English explanations stay in one place.
SIGNAL_EXPLANATIONS: dict[str, tuple[str, str, str]] = {
    "inspect_lint": (
        "Coding Discipline lint",
        "Runs the SHIPS Inspect lint rules against every DDL, DCL, and DML file "
        "in the payload. The rules enforce the Teradata Coding Discipline — "
        "database-qualified object names, MULTISET tables, uppercase SQL keywords, "
        "leading commas, eponymous filenames, correct object-type extensions, and "
        "object placement strategy compliance. A FAIL means one or more files "
        "violated a rule that blocks deployment.",
        "Open the Objects tab in the package report, click the flagged file link "
        "to view the highlighted source, then correct the violation in the source "
        "project and re-run harvest → inspect → package.",
    ),
    "inspect_token_format": (
        "Token marker format",
        "Checks that every {{TOKEN}} placeholder in the payload is correctly "
        "formed — double curly braces, an UPPERCASE_SNAKE_CASE name, and no "
        "stray punctuation inside the braces. Malformed markers (e.g. {TOKEN}, "
        "{{ TOKEN }}, or {{token}}) will not be substituted at deploy time and "
        "will cause the deployed DDL to reference the wrong database names.",
        "Run 'ships scan --show-map' against the source project to identify "
        "malformed markers, correct them in source, then re-harvest and re-inspect.",
    ),
    "inspect_grants": (
        "Grant validation",
        "Compares the grants implied by the DDL across the whole package against "
        "the .dcl files persisted under DCL/inter_db/. Three outcomes are "
        "possible per grantee — Consistent (no action needed), Drifted (the .dcl "
        "exists but its privilege set differs from what the DDL implies), or "
        "External (a .dcl file exists for a grantee — role, database, or user "
        "— that no DDL in the package implies). Missing inferred grants are "
        "always a hard error. Drifted entries with only extra manual "
        "privileges have configurable severity, and external grants default "
        "to INFO because they are commonly legitimate.",
        "Missing inferred grants: run 'ships inspect --fix-grants' to append "
        "the required GRANT statements to the correct .dcl file. "
        "Extra manual privileges (warn_extra_grants): set to WARNING or OFF in "
        "inspect.conf if intentional grants beyond what SHIPS infers are expected. "
        "External grants (warn_external_grants): default INFO — common when a "
        "role is granted access in this package but GRANT ROLE … TO USER is "
        "managed outside it (by a DBA, IGA system, or agent). Promote to "
        "ERROR for fully self-contained packages where every grant must be "
        "traceable to in-package DDL.",
    ),
    "provenance_complete": (
        "Provenance file present",
        "Checks that context/ships.provenance.json was included in the package. "
        "This file records the full file-transformation chain from original source "
        "through harvest, token substitution, and packaging for every payload file. "
        "Without it, the deploy report cannot link failed objects back to their "
        "source files, and the 'Open code' drill-down in the deploy report is "
        "disabled.",
        "Rebuild the package using the current version of SHIPS. Provenance is "
        "generated automatically at package time — a missing file means the "
        "package was built with an older version of the builder.",
    ),
    "build_reproducible": (
        "Built from clean working tree",
        "Checks whether the package was built from a clean Git working tree "
        "(no uncommitted changes). A WARN means the package was built with "
        "--allow-dirty, so the source state that produced this package cannot "
        "be precisely reproduced from version control. This is acceptable for "
        "development builds but should not be promoted to production.",
        "Commit or stash all working-tree changes before building a package "
        "intended for SIT or production promotion. Remove --allow-dirty from "
        "the build command.",
    ),
}


def signal_name_cell(name: str, navy: str = _NAVY, orange: str = _ORANGE) -> str:
    """Render a trust signal name as a collapsible plain-English explanation.

    The ``<summary>`` shows the technical signal key so experienced operators
    can scan the table at a glance.  Clicking it expands a card with:

    - A short human-readable title for the signal.
    - A paragraph explaining what the signal checks and why it matters.
    - An "If this fails:" line with actionable remediation guidance.

    Unknown signal names (not in :data:`SIGNAL_EXPLANATIONS`) degrade
    gracefully to plain monospace text with no expansion, so future
    signals added to SHIPS do not break either report.

    Args:
        name:   The signal key, e.g. ``"inspect_lint"``.
        navy:   Override for the primary dark colour (for theming).
        orange: Override for the accent colour (for theming).

    Returns:
        An HTML string suitable for use inside a ``<td>`` element.
    """
    entry = SIGNAL_EXPLANATIONS.get(name)
    if entry is None:
        return (
            f"<span style='font-family:monospace;font-size:13px'>{_escape(name)}</span>"
        )

    short_title, what_it_checks, what_to_do = entry
    return (
        f"<details style='cursor:pointer'>"
        f"<summary style='"
        f"font-family:monospace;font-size:13px;list-style:none;"
        f"display:flex;align-items:center;gap:6px;user-select:none' "
        f"title='Click to expand explanation'>"
        f"<span style='color:{orange};font-size:10px;flex-shrink:0'>&#9654;</span>"
        f"{_escape(name)}"
        f"</summary>"
        f"<div style='margin-top:8px;padding:10px 12px;"
        f"background:#F8F9FA;border-left:3px solid {orange};"
        f"border-radius:0 4px 4px 0;font-family:sans-serif;font-size:12px;"
        f"line-height:1.55;max-width:360px'>"
        f"<div style='font-weight:600;margin-bottom:4px;color:{navy}'>"
        f"{_escape(short_title)}</div>"
        f"<div style='color:#444;margin-bottom:8px'>{_escape(what_it_checks)}</div>"
        f"<div style='color:#555'>"
        f"<span style='font-weight:600;color:{navy}'>If this fails: </span>"
        f"{_escape(what_to_do)}</div>"
        f"</div>"
        f"</details>"
    )
