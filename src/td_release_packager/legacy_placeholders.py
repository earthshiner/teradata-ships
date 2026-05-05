"""
legacy_placeholders.py — detect non-SHIPS substitution placeholders
in source DDL.

Phase A of the placeholder-visibility work. SHIPS substitutes
``{{TOKEN}}`` markers at build time but does nothing with other
substitution syntaxes that pre-date SHIPS in many codebases:

    $VAR             bash-style single-dollar    (build harness)
    ${VAR}           bash-style braced           (build harness)
    \\$VAR           escaped dollar              (build harness)
    &&VAR&&          Teradata BTEQ variables     (BTEQ runtime)

When source uses one of these forms, SHIPS' eponymous-rename
extractor cannot recognise it as a database qualifier (the regex
expects ``{{TOKEN}}``, ``"QuotedIdent"`` or a bare identifier), so
``$UTL_T.MyTable`` lands harvested as ``MyTable.tbl`` -- a silent
loss of the qualifier portion. The build then fails downstream when
the unsubstituted ``$UTL_T`` survives into the deployed SQL.

This module surfaces those placeholders during harvest:

    1. ``find_legacy_placeholders`` scans cleaned content for each
       supported syntax and returns one ``LegacyPlaceholderFinding``
       per occurrence.
    2. ``format_legacy_placeholders_report`` renders a human-readable
       banner suitable for the harvest CLI -- grouped by syntax,
       sorted by frequency, with a specific call-to-action.

The detection is intentionally NOT a build-blocker. Harvest still
completes; the user sees a prominent warning that names the syntax,
counts the occurrences, lists the affected files, and points at the
right tool. Phase B will land that tool (``import-legacy
--scan-source``); until then the message points at the existing
``import-legacy`` documentation.

Note: this module deliberately duplicates the comment+literal
stripping helper rather than relying on the validate-side import.
The harvest pipeline runs before any validation; coupling them
would create a circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from td_release_packager.sql_text import strip_comments_and_string_literals


# ---------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------


@dataclass(frozen=True)
class LegacyPlaceholderFinding:
    """One occurrence of a non-SHIPS placeholder in source DDL.

    Attributes:
        syntax:        Friendly tag for the placeholder family
                       (e.g. ``"dollar"``, ``"dollar-braced"``,
                       ``"amp-amp"``). Used to group findings in
                       the report.
        placeholder:   The full literal placeholder as it appears
                       in source (``"$UTL_T"``, ``"${UTL_T}"``,
                       ``"&&DATE_FORMAT&&"``).
        var_name:      The variable name with all framing stripped
                       (``"UTL_T"``, ``"DATE_FORMAT"``). Equal across
                       different syntaxes for the same logical token.
        file_path:     Absolute path to the file the placeholder
                       was found in. Kept absolute so callers can
                       relativise against whichever root they choose.
        line:          1-based line number in the original content.
        column:        1-based column number in the original content.
    """

    syntax: str
    placeholder: str
    var_name: str
    file_path: str
    line: int
    column: int


# ---------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------
#
# Each pattern targets one substitution syntax. Order in the
# ``_PATTERNS`` list matters only for tie-breaking when two
# patterns could match the same span; the patterns below are
# disjoint by construction (different framing characters), so the
# order is purely cosmetic for the banner grouping.

# $VAR  — single dollar followed by an identifier. The lookbehind
# excludes mid-identifier dollars (we don't want to flag
# ``foo$bar`` even though that's an unusual identifier in Teradata).
# Word-character lookbehind is fixed-width so re engines accept it.
_DOLLAR_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z_]\w*)\b")

# ${VAR}  — bash-style braced. Independent regex so the report
# can distinguish ``$VAR`` from ``${VAR}`` (some sites use both
# in the same file with different semantics).
_DOLLAR_BRACED_RE = re.compile(r"\$\{([A-Za-z_]\w*)\}")

# &&VAR&&  — Teradata BTEQ-style variable. The framing is
# distinctive enough that we don't need a lookbehind/lookahead
# guard.
_AMP_AMP_RE = re.compile(r"&&([A-Za-z_]\w*)&&")


_PATTERNS = (
    ("dollar-braced", _DOLLAR_BRACED_RE),
    ("dollar", _DOLLAR_RE),
    ("amp-amp", _AMP_AMP_RE),
)


# ---------------------------------------------------------------
# Detection
# ---------------------------------------------------------------


def find_legacy_placeholders(
    content: str,
    file_path: str,
) -> List[LegacyPlaceholderFinding]:
    """Find every non-SHIPS placeholder in a single source file.

    Comments and string literals are stripped before matching, so
    a placeholder that appears in a header comment or a quoted
    string is not flagged. The position-preserving stripper means
    line/column numbers in the returned findings match the
    original content.

    Args:
        content:   Raw file content.
        file_path: Path to the source file -- carried into each
                   finding so the caller can group/report by file
                   without re-reading.

    Returns:
        Flat list of findings, one per occurrence. Order matches
        document order. Empty list when the file contains no
        legacy placeholders.
    """
    cleaned = strip_comments_and_string_literals(content)
    findings: List[LegacyPlaceholderFinding] = []

    # Track ${VAR} spans so the broader $VAR pattern doesn't
    # double-count the inner ``$VAR`` that the braced form already
    # claimed.
    braced_spans: List[range] = []
    for match in _DOLLAR_BRACED_RE.finditer(cleaned):
        braced_spans.append(range(match.start(), match.end()))

    def _spans_contain(start: int) -> bool:
        # Linear scan is fine: braced_spans is short in practice
        # (a handful per file) and findings rarely cross thousands.
        for span in braced_spans:
            if start in span:
                return True
        return False

    for syntax, pattern in _PATTERNS:
        for match in pattern.finditer(cleaned):
            # Skip $VAR matches that fall inside a ${VAR} span.
            if syntax == "dollar" and _spans_contain(match.start()):
                continue

            placeholder = match.group(0)
            var_name = match.group(1)
            line, column = _line_col(content, match.start())
            findings.append(
                LegacyPlaceholderFinding(
                    syntax=syntax,
                    placeholder=placeholder,
                    var_name=var_name,
                    file_path=file_path,
                    line=line,
                    column=column,
                )
            )

    # Sort by line then column so the order is stable per file
    # regardless of which pattern matched first.
    findings.sort(key=lambda f: (f.line, f.column, f.syntax))
    return findings


def _line_col(content: str, position: int) -> tuple:
    """1-based line and column for a zero-based byte position."""
    line = content.count("\n", 0, position) + 1
    line_start = content.rfind("\n", 0, position) + 1
    column = position - line_start + 1
    return (line, column)


# ---------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------


# Friendly labels used in the banner. The internal syntax tag is
# kept terse for code-side filtering; the banner shows the full
# example so a reader who's never seen the term knows what's meant.
_SYNTAX_LABELS = {
    "dollar": "$VAR style",
    "dollar-braced": "${VAR} style",
    "amp-amp": "&&VAR&& style",
}

#: How many sample files to list in the banner before truncating.
_SAMPLE_FILES_LIMIT = 5

#: How many sample placeholder names to list per syntax.
_SAMPLE_NAMES_LIMIT = 6


def format_legacy_placeholders_report(
    findings: List[LegacyPlaceholderFinding],
    source_dir: str = "",
    project_dir_hint: str = "<project>",
) -> str:
    """Render the harvest banner for legacy-placeholder findings.

    Banner shape (intentional):

      - One headline counting total occurrences.
      - Per-syntax breakdown so the user sees which framing(s)
        their codebase uses.
      - Sample placeholder names per syntax (top N by frequency).
      - Sample affected files (top N by occurrence count) with
        relative paths against ``source_dir`` when supplied.
      - A specific call-to-action naming ``import-legacy`` and
        showing the exact command, including a placeholder for
        the user's ``--env``.

    Args:
        findings:         All findings collected across the harvest.
                          Empty list -> empty string (caller suppresses
                          the banner).
        source_dir:       Optional source directory the harvest scanned.
                          When supplied, file paths in the banner are
                          relativised against this root.
        project_dir_hint: Placeholder shown in the suggested
                          ``--output-dir`` flag. Defaults to
                          ``<project>`` so the user knows to substitute.

    Returns:
        Multi-line string ready to print to stdout. Empty string
        when ``findings`` is empty.
    """
    if not findings:
        return ""

    total = len(findings)
    files = {f.file_path for f in findings}
    n_files = len(files)

    # Group by syntax -> { var_name -> count }
    by_syntax: Dict[str, Dict[str, int]] = {}
    # Per-file occurrence count for the top-files section.
    per_file: Dict[str, int] = {}
    for finding in findings:
        by_syntax.setdefault(finding.syntax, {}).setdefault(finding.var_name, 0)
        by_syntax[finding.syntax][finding.var_name] += 1
        per_file[finding.file_path] = per_file.get(finding.file_path, 0) + 1

    bar = "=" * 64
    lines = []
    lines.append("")
    lines.append(bar)
    lines.append(
        f"  WARN HARVEST -- non-SHIPS placeholders detected "
        f"({total} occurrences in {n_files} files)"
    )
    lines.append(bar)
    lines.append("")
    lines.append("  These references use legacy substitution syntax that")
    lines.append("  SHIPS does not substitute at build time. Their qualifier")
    lines.append("  could not be extracted; staged filenames are missing the")
    lines.append("  ``Database.`` prefix and the placeholders will survive")
    lines.append("  into deployed SQL unless converted to ``{{TOKEN}}`` form.")
    lines.append("")

    # By syntax
    lines.append("  By syntax:")
    for syntax in ("dollar", "dollar-braced", "amp-amp"):
        if syntax not in by_syntax:
            continue
        names = by_syntax[syntax]
        total_for_syntax = sum(names.values())
        label = _SYNTAX_LABELS[syntax]
        sample_names = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[
            :_SAMPLE_NAMES_LIMIT
        ]
        sample_str = ", ".join(name for name, _ in sample_names)
        suffix = (
            ""
            if len(names) <= _SAMPLE_NAMES_LIMIT
            else (f" ... +{len(names) - _SAMPLE_NAMES_LIMIT} more")
        )
        lines.append(
            f"    {label:<16} {total_for_syntax:>4} occurrences  ({sample_str}{suffix})"
        )
    lines.append("")

    # By file (top N)
    sorted_files = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    sample_files = sorted_files[:_SAMPLE_FILES_LIMIT]
    extra_files = len(sorted_files) - _SAMPLE_FILES_LIMIT
    lines.append(f"  Affected files (showing {len(sample_files)} of {n_files}):")
    for path, count in sample_files:
        rel = _relativise(path, source_dir)
        lines.append(f"    {rel}  ({count} occurrences)")
    if extra_files > 0:
        lines.append(f"    ... +{extra_files} more files")
    lines.append("")

    # Call to action
    lines.append("  Convert these to SHIPS {{TOKEN}} form:")
    lines.append("")
    lines.append("    python -m td_release_packager import-legacy \\")
    lines.append("        --scan-source <source_dir> \\")
    lines.append("        --env <ENV> \\")
    lines.append(f"        --output-dir {project_dir_hint}/config")
    lines.append("")
    lines.append("  Then re-harvest. The placeholders will be replaced with")
    lines.append("  {{TOKEN}} references and qualifier-bearing object types")
    lines.append("  (TABLE, VIEW, MACRO ...) will land with full")
    lines.append("  Database.Object filenames.")
    lines.append("")
    lines.append("  ``import-legacy --scan-source`` is Phase B of this work")
    lines.append("  and ships separately. Until it lands, the existing")
    lines.append("  ``--script <sed_file>`` mode handles the same conversion")
    lines.append("  if your project has a sed-style substitution script.")
    lines.append(bar)
    lines.append("")

    return "\n".join(lines)


def _relativise(path: str, source_dir: str) -> str:
    """Best-effort relative path from ``source_dir``.

    Falls back to the absolute path if ``source_dir`` isn't a
    prefix of ``path`` or if a normpath comparison fails.
    """
    if not source_dir:
        return path
    try:
        import os

        rel = os.path.relpath(path, source_dir)
        # If relpath has to walk up out of source_dir (..), the
        # absolute form is more useful for the reader.
        if rel.startswith(".."):
            return path
        return rel
    except (ValueError, OSError):
        return path
