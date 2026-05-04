"""
Import Legacy Substitution Scripts — SHIPS Bootstrap.

Reads a sed substitution script (the kind commonly used by pre-SHIPS
deployment frameworks) and produces two artefacts that bootstrap a
SHIPS project from chaos:

    1. <env>.properties — token name → value, ready for use at
       ``package`` time. Drop this in ``config/properties/`` next to
       your DEV/TST/PRD files.

    2. legacy_migration.sed — a sed script that converts legacy
       markers in your source tree to the SHIPS ``{{TOKEN}}``
       convention. Run this once against your source files BEFORE
       invoking ``harvest``.

Together these turn a "we have a sed file and a pile of source DDL"
situation into a fully tokenised SHIPS project in two commands plus
the normal harvest/package flow.

Supported legacy marker syntaxes (any may appear in the same file):

    s/$VAR/value/g       bash-style single-dollar
    s/${VAR}/value/g     bash-style braced
    s/\\$VAR/value/g     escaped dollar (treated as $VAR)
    s/&&VAR&&/value/g    double-amp wrapped (Teradata GCFR style)

Each rule is translated to:
    properties:     VAR=value          (sed-escaped slashes unescaped)
    migration sed:  s/<marker>/{{VAR}}/g

Usage::

    python tools/import_legacy_substitutions.py legacy.sh \\
        --env DEV \\
        --output-dir ./MyProject/config

    # Files written:
    #   ./MyProject/config/properties/DEV.properties
    #   ./MyProject/config/legacy_migration.sed

Author: Paul / Teradata Field Engineering
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------


@dataclass(frozen=True)
class Substitution:
    """One legacy substitution rule, after parsing.

    Used by both modes: in ``--script`` mode the value is taken
    from the sed replacement; in ``--scan-source`` mode the value
    is empty (the user populates it after import) and the
    ``line_number`` records the first occurrence in the source
    tree so the .properties comment can show a sample location.
    """

    original_marker: str  # e.g. "$ADMIN_USER" or "&&DATE_FORMAT&&"
    var_name: str         # e.g. "ADMIN_USER"
    value: str            # e.g. "GCFR_APPL_ADMIN_USER"
    line_number: int      # 1-based, for diagnostics


@dataclass
class ScanResult:
    """Aggregated output of ``scan_source_directory``.

    Attributes:
        substitutions:           One ``Substitution`` per unique
                                 (marker, var_name) pair seen in
                                 source. Multiple syntaxes for the
                                 same logical token (e.g. ``$UTL_T``
                                 AND ``${UTL_T}`` AND ``&&UTL_T&&``)
                                 produce three Substitution entries
                                 with the same ``var_name``.
        var_counts:              Per-var_name total occurrence count
                                 across the whole source tree. Used
                                 to order the .properties output by
                                 frequency (most-impactful first).
        var_to_files:            Per-var_name: list of (file_path,
                                 line) tuples — first ``_SAMPLE_LIMIT``
                                 occurrences. Drives the audit
                                 report and the per-token .properties
                                 comments.
        var_to_syntaxes:         Per-var_name: set of syntaxes seen
                                 (``"dollar"`` / ``"dollar-braced"`` /
                                 ``"amp-amp"``). A var with multiple
                                 syntaxes flags a multi-form token.
        files_scanned:           Total source files walked.
        files_with_placeholders: Files where at least one finding
                                 was recorded.
        total_occurrences:       Sum of ``var_counts.values()``.
    """

    substitutions: List[Substitution] = field(default_factory=list)
    var_counts: Dict[str, int] = field(default_factory=dict)
    var_to_files: Dict[str, List[Tuple[str, int]]] = field(
        default_factory=dict
    )
    var_to_syntaxes: Dict[str, set] = field(default_factory=dict)
    files_scanned: int = 0
    files_with_placeholders: int = 0
    total_occurrences: int = 0


# Sed substitution rule: s/<pattern>/<replacement>/<flags>
# Pattern and replacement may contain escaped slashes (\/).
# Captures: 1 = pattern (legacy marker), 2 = replacement (value).
# Trailing flags ([gpiI...]) are accepted but ignored — we do not
# preserve sed semantics, only mine the (marker, value) pair.
_SED_RULE_RE = re.compile(
    r"^s/((?:[^/\\]|\\.)*?)/((?:[^/\\]|\\.)*)/[a-zA-Z]*\s*$"
)


# Legacy marker patterns. Each extracts VAR_NAME from the sed pattern.
# Order matters: ${VAR} must be tried before $VAR so the braces don't
# get parsed as part of the variable name's surrounding text.
_MARKER_PATTERNS = [
    # ${VAR} or \${VAR}
    re.compile(r"^\\?\$\{([A-Za-z_][A-Za-z0-9_]*)\}$"),
    # $VAR or \$VAR
    re.compile(r"^\\?\$([A-Za-z_][A-Za-z0-9_]*)$"),
    # &&VAR&&
    re.compile(r"^&&([A-Za-z_][A-Za-z0-9_]*)&&$"),
]


def parse_sed_substitutions(content: str) -> List[Substitution]:
    """
    Parse sed substitution rules from script content.

    Lines starting with ``#`` and blank lines are ignored. Lines that
    do not match the sed substitution pattern, or that match but use
    an unrecognised marker syntax, are skipped with a warning.

    Args:
        content: Full text of the sed script.

    Returns:
        Ordered list of ``Substitution`` records, in input order.
        Duplicate ``var_name`` entries are preserved (caller decides
        last-wins semantics for the properties output).
    """
    subs: List[Substitution] = []
    for lineno, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = _SED_RULE_RE.match(line)
        if not m:
            logger.warning(
                "Line %d: not a sed substitution rule, skipping: %s",
                lineno,
                line,
            )
            continue

        pattern, replacement = m.group(1), m.group(2)
        var_name = _extract_var_name(pattern)
        if var_name is None:
            logger.warning(
                "Line %d: marker '%s' is not a recognised legacy "
                "syntax ($VAR, ${VAR}, or &&VAR&&), skipping",
                lineno,
                pattern,
            )
            continue

        value = _unescape_sed_value(replacement)
        subs.append(
            Substitution(
                original_marker=pattern,
                var_name=var_name,
                value=value,
                line_number=lineno,
            )
        )

    return subs


def _extract_var_name(marker: str) -> Optional[str]:
    """Extract VAR from $VAR / ${VAR} / &&VAR&&. None if no match."""
    for pattern in _MARKER_PATTERNS:
        m = pattern.match(marker)
        if m:
            return m.group(1)
    return None


def _unescape_sed_value(raw: str) -> str:
    """
    Unescape sed-specific sequences in a replacement string.

    Sed treats ``\\/`` as a literal forward slash inside the
    replacement (because ``/`` is the delimiter). We honour that.
    Other backslash escapes are passed through unchanged — sed
    behaviour for them varies by implementation and is rarely
    significant for token values.
    """
    return raw.replace("\\/", "/")


# ---------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------


def format_properties_file(env: str, subs: List[Substitution]) -> str:
    """
    Render a SHIPS ``.properties`` file from substitutions.

    Renders the canonical 7-section scaffold (composition roots,
    derived names, users & roles, SQL constants, engine flags,
    field-length policy, diagnostic stanzas) with all sections
    EMPTY, plus a final ``Imported (UNCATEGORISED)`` section
    containing every imported (key, value) pair. The user's job is
    to move tokens up out of the Imported section into the
    appropriate categorised section above and delete the Imported
    section when empty.

    Why this shape rather than a flat dump: the user gets the
    skeleton + content in one file, so re-sectioning is cut-and-
    paste rather than open-the-template-and-compare.

    Duplicate keys keep the last-defined value (consistent with
    ``read_properties``). A ``# WARN`` comment is emitted on the
    line where the override happens so the conflict is obvious.
    """
    from td_release_packager.properties_scaffold import render_scaffold

    # Render the imported (key, value) pairs as the body of section 8.
    body_lines: List[str] = []
    seen: Dict[str, int] = {}
    for sub in subs:
        if sub.var_name in seen:
            body_lines.append(
                f"# WARN duplicate '{sub.var_name}' on line "
                f"{sub.line_number}; previous value on line "
                f"{seen[sub.var_name]} overridden"
            )
        seen[sub.var_name] = sub.line_number
        body_lines.append(f"{sub.var_name}={sub.value}")

    return render_scaffold(
        env=env,
        generator_label="import_legacy_substitutions.py",
        source_label="legacy sed substitution script",
        next_steps=[
            "1. Identify your composition roots (ENV_PREFIX, SHIPS_ENV,",
            "   SHIPS_PROJECT, INSTANCE, SECURITY_TIER) from the imported",
            "   values and move them into section 1.",
            "",
            "2. Move database-name tokens to section 2, converting",
            "   literals to cascade form, e.g.",
            "     PARENT_NODE=PDE_DEV_00",
            "       → PARENT_NODE={{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}",
            "",
            "3. Move users/roles to section 3, SQL constants to section 4,",
            "   engine flags to section 5, length-policy to section 6,",
            "   diagnostic stanzas to section 7.",
            "",
            "4. Delete the Imported section once empty.",
        ],
        sections_content={},  # all empty — user populates by moving from sec 8
        final_section_title="Imported (UNCATEGORISED)",
        final_section_purpose=[
            "All entries imported from the legacy substitution script,",
            "verbatim. Move each entry into the appropriate section",
            "above (1-7) and delete this section when empty.",
        ],
        final_section_content="\n".join(body_lines),
    )


def format_migration_sed(subs: List[Substitution]) -> str:
    """
    Render a sed migration script.

    The output is itself a sed script: each input ``s/<marker>/<value>/g``
    becomes ``s/<marker>/{{VAR}}/g``. Run it against the source tree to
    convert legacy syntax markers into SHIPS ``{{TOKEN}}`` references,
    after which the normal ``harvest`` flow handles the rest.

    Deduplication is by ``(marker, var_name)`` -- so a project that
    uses BOTH ``$UTL_T`` AND ``${UTL_T}`` AND ``&&UTL_T&&`` for the
    same logical token gets THREE sed rules, all converging on the
    same ``{{UTL_T}}`` replacement. Pre-Phase-B this deduped on
    ``var_name`` alone, which silently dropped all but the first
    syntax -- the surviving forms then leaked into deployed SQL.
    """
    lines = [
        "# ===================================================================",
        "# legacy_migration.sed — generated by import_legacy_substitutions.py",
        "#",
        "# Converts legacy substitution markers in source files to the",
        "# SHIPS {{TOKEN}} convention. Run ONCE against your source tree",
        "# before invoking 'harvest'.",
        "#",
        "# Usage (GNU sed; in-place edit):",
        "#   find <src> -type f \\( -name '*.sql' -o -name '*.ddl' \\) \\",
        "#       -exec sed -i -f legacy_migration.sed {} +",
        "#",
        "# Usage (BSD sed; macOS):",
        "#   find <src> -type f -name '*.sql' \\",
        "#       -exec sed -i '' -f legacy_migration.sed {} +",
        "# ===================================================================",
        "",
    ]

    seen: set = set()
    for sub in subs:
        key = (sub.original_marker, sub.var_name)
        if key in seen:
            continue
        seen.add(key)
        # Re-escape any forward slashes in the marker. Standard
        # legacy syntaxes don't contain '/', but defence in depth.
        escaped = sub.original_marker.replace("/", r"\/")
        replacement = "{{" + sub.var_name + "}}"
        lines.append(f"s/{escaped}/{replacement}/g")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------
# Source scanning (--scan-source mode)
# ---------------------------------------------------------------
#
# When a project has placeholders in its source DDL but NO sed
# script to point at, ``--scan-source`` walks the source tree and
# auto-discovers the substitutions. Output is the same shape as
# ``--script`` mode (``.properties`` + ``legacy_migration.sed``)
# plus an additional audit file ``scan_report.md`` listing every
# placeholder, syntax, and occurrence. The user fills in the
# .properties values and runs the sed script as before.


#: How many sample (file, line) pairs to record per token.
_SAMPLE_LIMIT = 25


def scan_source_directory(
    source_dir: str,
    project_dir: Optional[str] = None,
) -> ScanResult:
    """Walk a source tree and aggregate non-SHIPS placeholders.

    Uses the harvest-discovery resolver to determine which file
    extensions to scan -- so any project-specific extensions
    declared in ``ships.yaml``'s ``discovery.extensions`` block
    are honoured here without duplication.

    For each file, runs the Phase A
    ``find_legacy_placeholders`` detector. Aggregates findings
    into ``ScanResult`` with one ``Substitution`` per unique
    ``(marker, var_name)`` pair (so a project using both
    ``$UTL_T`` and ``${UTL_T}`` produces two Substitutions, both
    tagged ``UTL_T``).

    Args:
        source_dir:  Root of the source tree to scan.
        project_dir: Optional SHIPS project root, consulted by the
                     discovery resolver for ``ships.yaml`` overrides.
                     Pass ``None`` to use baked-in defaults.

    Returns:
        ``ScanResult`` ready for the formatters below.

    Raises:
        FileNotFoundError: If ``source_dir`` does not exist.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    # Local imports to avoid circular dependency at module load.
    from td_release_packager.discovery import resolve_harvest_extensions
    from td_release_packager.legacy_placeholders import (
        find_legacy_placeholders,
    )

    extensions = resolve_harvest_extensions(project_dir=project_dir)
    result = ScanResult()

    # Track unique (marker, var_name) pairs and their first occurrence.
    seen: Dict[Tuple[str, str], Tuple[str, int]] = {}

    for root, dirs, filenames in os.walk(source_dir):
        dirs.sort()
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.startswith("_"):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in extensions:
                continue

            file_path = os.path.join(root, filename)
            result.files_scanned += 1

            try:
                content = Path(file_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            findings = find_legacy_placeholders(content, file_path)
            if not findings:
                continue
            result.files_with_placeholders += 1

            for finding in findings:
                key = (finding.placeholder, finding.var_name)
                if key not in seen:
                    seen[key] = (file_path, finding.line)

                # Per-var bookkeeping.
                result.var_counts[finding.var_name] = (
                    result.var_counts.get(finding.var_name, 0) + 1
                )
                result.var_to_syntaxes.setdefault(
                    finding.var_name, set()
                ).add(finding.syntax)
                samples = result.var_to_files.setdefault(finding.var_name, [])
                if len(samples) < _SAMPLE_LIMIT:
                    samples.append((file_path, finding.line))

    result.total_occurrences = sum(result.var_counts.values())

    # Build Substitutions ordered by var-frequency desc, then by
    # var_name asc, then by marker (so script output is
    # deterministic regardless of os.walk ordering quirks).
    def _sort_key(item):
        (marker, var_name), _ = item
        return (-result.var_counts[var_name], var_name, marker)

    for (marker, var_name), (first_file, first_line) in sorted(
        seen.items(), key=_sort_key
    ):
        result.substitutions.append(
            Substitution(
                original_marker=marker,
                var_name=var_name,
                value="",  # user fills after import
                line_number=first_line,
            )
        )

    return result


def scan_format_properties_file(env: str, scan: ScanResult) -> str:
    """Render the .properties file from a scan-source result.

    Differs from ``format_properties_file`` (sed-script mode):

      - One entry per ``var_name``, NOT one per Substitution.
        Multiple syntaxes for the same logical token (``$UTL_T``,
        ``${UTL_T}``, ``&&UTL_T&&``) collapse to a single
        ``UTL_T=`` line.
      - Entries ordered by occurrence count (most-impactful first).
      - Each entry preceded by a comment showing occurrence count
        and a sample file:line, so the user reviewing the file
        knows what they're filling in and where it came from.

    Values are empty -- the user populates them after import.
    """
    from td_release_packager.properties_scaffold import render_scaffold

    body_lines: List[str] = []
    seen_vars: set = set()
    for sub in scan.substitutions:
        if sub.var_name in seen_vars:
            continue
        seen_vars.add(sub.var_name)

        count = scan.var_counts.get(sub.var_name, 0)
        syntaxes = sorted(scan.var_to_syntaxes.get(sub.var_name, set()))
        samples = scan.var_to_files.get(sub.var_name, [])
        sample_str = ""
        if samples:
            sample_path, sample_line = samples[0]
            sample_str = f", sample: {os.path.basename(sample_path)}:{sample_line}"

        body_lines.append(
            f"# {sub.var_name}: {count} occurrences "
            f"({'/'.join(syntaxes)}{sample_str})"
        )
        body_lines.append(f"{sub.var_name}=")

    return render_scaffold(
        env=env,
        generator_label="import_legacy_substitutions.py --scan-source",
        source_label="auto-discovered placeholders in source DDL",
        next_steps=[
            "1. Fill in values for each token below. The comment above each",
            "   entry shows where the placeholder appears in source.",
            "",
            "2. Move tokens into the appropriate categorised section above",
            "   (composition roots, derived names, users & roles, etc.) as",
            "   their meaning becomes clear.",
            "",
            "3. Run the migration sed script against your source tree:",
            "     find <src> -type f \\( -name '*.sql' -o -name '*.tbl' \\) \\",
            "         -exec sed -i -f legacy_migration.sed {} +",
            "",
            "4. Re-harvest the migrated source. The placeholders will now",
            "   be {{TOKEN}} references and qualifier-bearing object types",
            "   (TABLE, VIEW, MACRO ...) will land with their full",
            "   Database.Object filenames.",
            "",
            "5. Delete the Imported section once empty.",
        ],
        sections_content={},  # all empty — user populates
        final_section_title="Imported (UNCATEGORISED)",
        final_section_purpose=[
            "Tokens auto-discovered by --scan-source from the source DDL.",
            "Values are empty -- fill them in, then move each entry into",
            "the appropriate section above (1-7) and delete this section",
            "when empty.",
        ],
        final_section_content="\n".join(body_lines),
    )


def scan_format_report(scan: ScanResult, source_dir: str) -> str:
    """Render the audit report ``scan_report.md``.

    Companion artefact to the .properties + .sed files. Lists every
    discovered token, its syntaxes, total occurrence count, and
    every (file, line) where it was seen. Useful when reviewing
    the import for surprises (a token that appears in 200 files vs
    one that appears once is a meaningful distinction the
    .properties file doesn't preserve).
    """
    lines: List[str] = []
    lines.append("# Legacy Placeholder Scan Report")
    lines.append("")
    lines.append(f"- Source directory: `{source_dir}`")
    lines.append(f"- Files scanned: {scan.files_scanned}")
    lines.append(f"- Files with placeholders: {scan.files_with_placeholders}")
    lines.append(f"- Total occurrences: {scan.total_occurrences}")
    lines.append(f"- Distinct tokens: {len(scan.var_counts)}")
    lines.append("")

    if not scan.var_counts:
        lines.append("No placeholders found.")
        return "\n".join(lines) + "\n"

    # Order: most frequent first, then alphabetical.
    sorted_vars = sorted(
        scan.var_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )

    lines.append("## Tokens by frequency")
    lines.append("")
    lines.append("| Token | Occurrences | Syntaxes | Sample location |")
    lines.append("|---|---|---|---|")
    for var_name, count in sorted_vars:
        syntaxes = "/".join(sorted(scan.var_to_syntaxes.get(var_name, set())))
        samples = scan.var_to_files.get(var_name, [])
        sample = ""
        if samples:
            spath, sline = samples[0]
            try:
                rel = os.path.relpath(spath, source_dir)
            except ValueError:
                rel = spath
            sample = f"`{rel}:{sline}`"
        lines.append(f"| `{var_name}` | {count} | {syntaxes} | {sample} |")
    lines.append("")

    # Detail: per-token, list each (file, line). Truncated by
    # _SAMPLE_LIMIT, which the count makes obvious.
    lines.append("## Per-token detail")
    lines.append("")
    for var_name, count in sorted_vars:
        samples = scan.var_to_files.get(var_name, [])
        truncated = count > len(samples)
        lines.append(f"### `{var_name}` ({count} occurrences)")
        lines.append("")
        syntaxes = sorted(scan.var_to_syntaxes.get(var_name, set()))
        lines.append(f"Syntaxes: {', '.join(syntaxes)}")
        lines.append("")
        lines.append("| File | Line |")
        lines.append("|---|---|")
        for spath, sline in samples:
            try:
                rel = os.path.relpath(spath, source_dir)
            except ValueError:
                rel = spath
            lines.append(f"| `{rel}` | {sline} |")
        if truncated:
            lines.append(
                f"| _... +{count - len(samples)} more occurrences "
                f"(truncated at {_SAMPLE_LIMIT})_ | |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="import_legacy_substitutions.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Two input modes, mutually exclusive:
    #   --script <sed_file>   (Phase A original)
    #   --scan-source <dir>   (Phase B addition)
    # Either consumes the SAME output writer, so downstream
    # behaviour is identical regardless of source.
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--script",
        metavar="SED_FILE",
        help="Path to a legacy sed substitution script. Use this when "
        "your project's pre-SHIPS build harness already has a sed "
        "file defining (marker, value) pairs.",
    )
    mode.add_argument(
        "--scan-source",
        metavar="SOURCE_DIR",
        help="Walk a source DDL tree and auto-discover non-SHIPS "
        "placeholders ($VAR, ${VAR}, &&VAR&&). Use this when the "
        "project has placeholders embedded in source but no sed "
        "file to point at -- the .properties values come out empty "
        "for you to fill in, the migration sed converts every "
        "discovered marker to its {{TOKEN}} form.",
    )
    p.add_argument(
        "--project",
        help="Optional SHIPS project root. When supplied, the discovery "
        "resolver consults the project's ships.yaml for any "
        "extra extensions to scan. Only meaningful with --scan-source.",
    )
    p.add_argument(
        "--env",
        required=True,
        help="Target environment name (e.g. DEV, TST, PRD). Used as "
        "the .properties filename and noted in the file header.",
    )
    p.add_argument(
        "--output-dir",
        default=".",
        help="Output directory (default: current). Files are written "
        "under <output-dir>/properties/<env>.properties and "
        "<output-dir>/legacy_migration.sed. In --scan-source mode an "
        "additional <output-dir>/scan_report.md is also written.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (INFO level).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    # Dispatch on mode -- the rest of main is shared.
    if args.script is not None:
        outcome = _run_script_mode(args)
    else:
        outcome = _run_scan_mode(args)

    return outcome


def _run_script_mode(args) -> int:
    """Existing --script flow: parse a sed file, emit .properties + .sed."""
    input_path = Path(args.script)
    if not input_path.is_file():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 1

    content = input_path.read_text(encoding="utf-8")
    subs = parse_sed_substitutions(content)

    if not subs:
        print(
            f"ERROR: no recognisable substitutions found in {input_path}",
            file=sys.stderr,
        )
        return 1

    output_dir = Path(args.output_dir)
    properties_dir = output_dir / "properties"
    properties_dir.mkdir(parents=True, exist_ok=True)

    properties_path = properties_dir / f"{args.env}.properties"
    migration_path = output_dir / "legacy_migration.sed"

    properties_path.write_text(
        format_properties_file(args.env, subs), encoding="utf-8"
    )
    migration_path.write_text(format_migration_sed(subs), encoding="utf-8")

    unique_tokens = len({s.var_name for s in subs})

    print("=" * 64)
    print(f"  Imported {len(subs)} substitution(s) from {input_path}")
    print("=" * 64)
    print(f"  Tokens (unique):  {unique_tokens}")
    print(f"  Properties file:  {properties_path}")
    print(f"  Migration sed:    {migration_path}")
    print()
    print("  Next steps:")
    print()
    print("  1. Apply the migration sed against your source tree:")
    print("       find <src> -type f \\( -name '*.sql' -o -name '*.ddl' \\) \\")
    print(f"           -exec sed -i -f {migration_path} {{}} +")
    print()
    print("  2. Harvest the migrated source:")
    print(
        "       python -m td_release_packager harvest "
        "--source <migrated_src> --project <new_proj>"
    )
    print()
    print(f"  3. Review and section {properties_path}")
    print("     against config/properties/DEV.properties as a structural template.")
    print("=" * 64)

    return 0


def _run_scan_mode(args) -> int:
    """New --scan-source flow: walk source, emit .properties + .sed +
    scan_report.md."""
    source_dir = Path(args.scan_source)
    if not source_dir.is_dir():
        print(
            f"ERROR: source directory not found: {source_dir}",
            file=sys.stderr,
        )
        return 1

    project_dir = args.project if args.project else None
    scan = scan_source_directory(str(source_dir), project_dir=project_dir)

    if not scan.substitutions:
        print(
            f"  Scanned {scan.files_scanned} file(s) under {source_dir}.",
            f"  No legacy placeholders found -- the source is already in",
            f"  SHIPS-canonical form (or contains no placeholders at all).",
            sep="\n",
        )
        return 0

    output_dir = Path(args.output_dir)
    properties_dir = output_dir / "properties"
    properties_dir.mkdir(parents=True, exist_ok=True)

    properties_path = properties_dir / f"{args.env}.properties"
    migration_path = output_dir / "legacy_migration.sed"
    report_path = output_dir / "scan_report.md"

    properties_path.write_text(
        scan_format_properties_file(args.env, scan), encoding="utf-8"
    )
    migration_path.write_text(
        format_migration_sed(scan.substitutions), encoding="utf-8"
    )
    report_path.write_text(
        scan_format_report(scan, str(source_dir)), encoding="utf-8"
    )

    print("=" * 64)
    print(f"  Scanned {scan.files_scanned} file(s) under {source_dir}")
    print("=" * 64)
    print(f"  Files with placeholders: {scan.files_with_placeholders}")
    print(f"  Total occurrences:       {scan.total_occurrences}")
    print(f"  Distinct tokens:         {len(scan.var_counts)}")
    print(f"  Sed rules to be emitted: {len(scan.substitutions)}")
    print(f"  Properties file:         {properties_path}")
    print(f"  Migration sed:           {migration_path}")
    print(f"  Audit report:            {report_path}")
    print()
    print("  Next steps:")
    print()
    print(f"  1. Open {properties_path} and fill in values. The")
    print("     comment above each entry shows where the placeholder")
    print("     appears in source.")
    print()
    print(f"  2. Review {report_path} for any surprises (high-")
    print("     frequency tokens worth promoting to a derived definition,")
    print("     or single-occurrence outliers worth investigating).")
    print()
    print("  3. Apply the migration sed against your source tree:")
    print("       find <src> -type f \\( -name '*.sql' -o -name '*.tbl' \\) \\")
    print(f"           -exec sed -i -f {migration_path} {{}} +")
    print()
    print("  4. Re-harvest the migrated source. The placeholders will")
    print("     now be {{TOKEN}} references and qualifier-bearing object")
    print("     types (TABLE, VIEW, MACRO ...) will land with their")
    print("     full Database.Object filenames.")
    print("=" * 64)

    return 0


if __name__ == "__main__":
    sys.exit(main())
