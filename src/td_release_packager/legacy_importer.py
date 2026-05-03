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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------


@dataclass(frozen=True)
class Substitution:
    """One legacy substitution rule, after parsing."""

    original_marker: str  # e.g. "$ADMIN_USER" or "&&DATE_FORMAT&&"
    var_name: str         # e.g. "ADMIN_USER"
    value: str            # e.g. "GCFR_APPL_ADMIN_USER"
    line_number: int      # 1-based, for diagnostics


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

    Duplicate ``var_name`` entries produce only one rule (the first
    occurrence wins for source migration — every legacy marker for
    the same name maps to the same ``{{VAR}}`` regardless).
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

    seen = set()
    for sub in subs:
        if sub.var_name in seen:
            continue
        seen.add(sub.var_name)
        # Re-escape any forward slashes in the marker. Standard
        # legacy syntaxes don't contain '/', but defence in depth.
        escaped = sub.original_marker.replace("/", r"\/")
        replacement = "{{" + sub.var_name + "}}"
        lines.append(f"s/{escaped}/{replacement}/g")

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
    p.add_argument(
        "input",
        help="Path to the legacy sed substitution script.",
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
        "<output-dir>/legacy_migration.sed.",
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

    input_path = Path(args.input)
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


if __name__ == "__main__":
    sys.exit(main())
