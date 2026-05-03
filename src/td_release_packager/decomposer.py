"""
Decompose Literal Database Names — SHIPS Cascade Bootstrap.

For codebases that pre-date SHIPS and have NO legacy substitution
script (sed, properties, etc.) — only a body of DDL with hardcoded
database names. This tool reads a list of those literals and
decomposes them against the SHIPS naming grammar:

    {ENV_PREFIX}_{SHIPS_ENV}_{INSTANCE}_{LAYER}_{SECURITY_TIER}_{KIND}
          PDE         DEV          00       MDL           0           T

Produces a SHIPS .properties file with:

    1. Composition roots (ENV_PREFIX, SHIPS_ENV, INSTANCE,
       SECURITY_TIER) inferred from the names.
    2. Derived token definitions in the cascade form, e.g.
       ``MDL_T={{PARENT_NODE}}_MDL_{{SECURITY_TIER}}_T``.
    3. Outlier names emitted as literal-valued fallback tokens for
       you to review.

Plus a ``decomposition_report.md`` showing exactly what was
inferred, what didn't match, and the confidence per inference.

When to use this vs. ``import_legacy_substitutions.py``:

    - YOU HAVE a sed/properties file:    use import_legacy_substitutions
    - YOU DON'T:                          use this tool

Workflow::

    # 1. Run harvest with --generate-token-map to discover literals
    python -m td_release_packager harvest \\
        --source legacy_src --project myproj \\
        --generate-token-map --env-prefix PDE_DEV_00

    # 2. Decompose against the grammar
    python tools/decompose_database_names.py \\
        myproj/config/token_map.conf \\
        --env DEV \\
        --output-dir myproj/config

    # 3. Review the generated .properties + report, refine as needed
    # 4. Re-harvest with the (curated) token_map applied
    # 5. package using the .properties

The tool auto-detects whether the input file is a token_map.conf
(LITERAL={{TOKEN}} format) or a plain names file (one literal per
line). Either works.

Author: Paul / Teradata Field Engineering
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Data model
# ---------------------------------------------------------------


@dataclass(frozen=True)
class CompositionRoots:
    """The four composition roots of a SHIPS naming grammar."""

    env_prefix: str
    ships_env: str
    instance: str          # may be "" if grammar has no instance segment
    security_tier: str     # defaults to "0" if no tier observed


@dataclass(frozen=True)
class DecomposedName:
    """One literal database name, decomposed against the grammar."""

    literal: str
    layer: str
    security_tier: Optional[str]
    kind: Optional[str]
    has_instance: bool
    is_parent_node: bool = False  # true when literal == ENV_PREFIX_ENV_INSTANCE

    @property
    def token_name(self) -> str:
        """Default token name from layer + kind."""
        if self.is_parent_node:
            return "PARENT_NODE"
        if not self.layer:
            # Defensive — shouldn't happen for valid decomposition
            return self.literal
        suffix = self.kind or "NODE"
        return f"{self.layer}_{suffix}"

    def cascade_form(self) -> str:
        """Render the value using {{TOKEN}} cascade."""
        if self.is_parent_node:
            return "{{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}"
        if not self.has_instance:
            # Cross-instance form, e.g. PDE_DEV_MDL
            return f"{{{{ENV_PREFIX}}}}_{{{{SHIPS_ENV}}}}_{self.layer}"
        if self.kind and self.security_tier:
            # Full leaf form
            return (
                "{{PARENT_NODE}}_"
                f"{self.layer}_"
                "{{SECURITY_TIER}}_"
                f"{self.kind}"
            )
        # Node form (no tier, no kind)
        return "{{PARENT_NODE}}_" + self.layer


@dataclass
class DecompositionResult:
    """Aggregated output of running decomposition over a name set."""

    roots: CompositionRoots
    decomposed: List[DecomposedName]
    outliers: List[str]
    confidence: Dict[str, str] = field(default_factory=dict)
    collisions: Dict[str, List[str]] = field(default_factory=dict)


# ---------------------------------------------------------------
# Input reading (auto-detect names file vs token_map.conf)
# ---------------------------------------------------------------


def read_input_file(path: str) -> List[str]:
    """
    Read literal database names from either format:

      - token_map.conf:    each non-comment line is ``LITERAL={{TOKEN}}``
                           — we extract the LHS.
      - names file:        each non-comment line is one literal.

    Auto-detected by checking whether every data line contains ``=``.

    Args:
        path: Path to the input file.

    Returns:
        Ordered list of literal database names. Duplicates removed,
        order preserved (first-occurrence wins).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    raw_lines = p.read_text(encoding="utf-8").splitlines()
    data = [
        ln.strip()
        for ln in raw_lines
        if ln.strip() and not ln.strip().startswith("#")
    ]

    if not data:
        return []

    if all("=" in ln for ln in data):
        logger.info("Detected token_map.conf format; extracting LHS")
        names = [ln.split("=", 1)[0].strip() for ln in data]
    else:
        logger.info("Detected plain names-file format")
        names = data

    # Preserve order, drop duplicates
    seen = set()
    unique: List[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


# ---------------------------------------------------------------
# Composition-root inference
# ---------------------------------------------------------------


_TWO_DIGIT_RE = re.compile(r"^\d{2,}$")
_SINGLE_DIGIT_RE = re.compile(r"^\d$")
_SINGLE_LETTER_RE = re.compile(r"^[A-Z]$")


def _confidence_for_ratio(ratio: float) -> str:
    """Map a coverage ratio to a confidence label."""
    if ratio >= 0.9:
        return "HIGH"
    if ratio >= 0.6:
        return "MEDIUM"
    return "LOW"


def _majority_at_position(
    parts_list: List[List[str]], pos: int
) -> Tuple[Optional[str], float]:
    """
    Return (most-common segment at ``pos``, share-of-total-names).

    Names that have fewer than ``pos+1`` segments are simply not
    counted — their absence doesn't fail the majority but does
    reduce confidence.
    """
    counter: Counter = Counter()
    for parts in parts_list:
        if pos < len(parts):
            counter[parts[pos]] += 1
    if not counter:
        return (None, 0.0)
    value, count = counter.most_common(1)[0]
    return (value, count / len(parts_list))


def infer_composition_roots(
    names: List[str],
) -> Tuple[CompositionRoots, Dict[str, str]]:
    """
    Infer (ENV_PREFIX, SHIPS_ENV, INSTANCE, SECURITY_TIER) from
    a list of literal database names.

    Strategy: majority-vote per segment position. Resilient to
    outliers (a single non-conforming name doesn't blank out the
    inference, it just lowers confidence).

      - ENV_PREFIX:    most common segment at position 0
      - SHIPS_ENV:     most common at position 1
      - INSTANCE:      most common at position 2 IF it looks like
                       a 2+digit number; otherwise empty
      - SECURITY_TIER: most common single-digit segment in the
                       position immediately before a single-letter
                       kind suffix (typical position is second-from-
                       last in leaf-style names)

    Each field gets a confidence label (HIGH/MEDIUM/LOW) based on
    the share of names that conform.

    Args:
        names: Literal database names.

    Returns:
        ``(CompositionRoots, confidence_dict)``.
    """
    if not names:
        return (
            CompositionRoots("", "", "", "0"),
            {"env_prefix": "LOW", "ships_env": "LOW",
             "instance": "LOW", "security_tier": "LOW"},
        )

    parts_list = [n.split("_") for n in names]
    confidence: Dict[str, str] = {}

    # ENV_PREFIX (pos 0)
    env_prefix, env_ratio = _majority_at_position(parts_list, 0)
    env_prefix = env_prefix or ""
    confidence["env_prefix"] = (
        _confidence_for_ratio(env_ratio) if env_prefix else "LOW"
    )

    # SHIPS_ENV (pos 1)
    ships_env, ships_ratio = _majority_at_position(parts_list, 1)
    ships_env = ships_env or ""
    confidence["ships_env"] = (
        _confidence_for_ratio(ships_ratio) if ships_env else "LOW"
    )

    # INSTANCE (pos 2) — only accept if the dominant value looks
    # like a 2+ digit number. Otherwise this codebase has no
    # instance segment in its grammar.
    candidate_inst, inst_ratio = _majority_at_position(parts_list, 2)
    if candidate_inst and _TWO_DIGIT_RE.match(candidate_inst):
        instance = candidate_inst
        confidence["instance"] = _confidence_for_ratio(inst_ratio)
    else:
        instance = ""
        confidence["instance"] = "LOW"

    # SECURITY_TIER — most common single-digit segment in the
    # position before a single-letter kind suffix. Only counted
    # against leaf-style names (those with a kind suffix), since
    # node-style names legitimately have no tier.
    tier_counter: Counter = Counter()
    leaf_total = 0
    for parts in parts_list:
        if (
            len(parts) >= 2
            and _SINGLE_LETTER_RE.match(parts[-1])
            and _SINGLE_DIGIT_RE.match(parts[-2])
        ):
            tier_counter[parts[-2]] += 1
            leaf_total += 1

    if tier_counter and leaf_total:
        security_tier, tier_count = tier_counter.most_common(1)[0]
        confidence["security_tier"] = _confidence_for_ratio(
            tier_count / leaf_total
        )
    else:
        security_tier = "0"
        confidence["security_tier"] = "LOW"

    return (
        CompositionRoots(
            env_prefix=env_prefix,
            ships_env=ships_env,
            instance=instance,
            security_tier=security_tier,
        ),
        confidence,
    )


# ---------------------------------------------------------------
# Per-name decomposition
# ---------------------------------------------------------------


def decompose_name(
    name: str, roots: CompositionRoots
) -> Optional[DecomposedName]:
    """
    Decompose one literal name against the grammar.

    Returns ``None`` if the name does not start with the
    ``{ENV_PREFIX}_{SHIPS_ENV}_`` prefix (it's an outlier).

    Otherwise, classifies into one of:
      - PARENT_NODE: ``literal == ENV_PREFIX_ENV_INSTANCE``
      - Cross-instance node: no INSTANCE segment present
      - Full leaf:    has INSTANCE, has tier, has kind
      - Plain node:   has INSTANCE, no tier, no kind
    """
    expected_prefix = f"{roots.env_prefix}_{roots.ships_env}_"
    if not name.startswith(expected_prefix):
        return None

    rest = name[len(expected_prefix):]
    if not rest:
        return None

    parts = rest.split("_")

    # PARENT_NODE itself — matches {env_prefix}_{ships_env}_{instance} exactly
    if roots.instance and parts == [roots.instance]:
        return DecomposedName(
            literal=name,
            layer="",
            security_tier=None,
            kind=None,
            has_instance=True,
            is_parent_node=True,
        )

    # Detect INSTANCE
    has_instance = bool(roots.instance) and parts[0] == roots.instance
    if has_instance:
        layer_parts = parts[1:]
    else:
        layer_parts = parts[:]

    # Detect kind suffix (single uppercase letter)
    kind: Optional[str] = None
    if layer_parts and _SINGLE_LETTER_RE.match(layer_parts[-1]):
        kind = layer_parts[-1]
        layer_parts = layer_parts[:-1]

    # Detect security tier (single digit before kind)
    security_tier: Optional[str] = None
    if layer_parts and _SINGLE_DIGIT_RE.match(layer_parts[-1]):
        security_tier = layer_parts[-1]
        layer_parts = layer_parts[:-1]

    layer = "_".join(layer_parts)
    if not layer:
        # Edge case: instance-only name without further segments
        # already handled above as PARENT_NODE; here we fall through
        # to outlier territory.
        return None

    return DecomposedName(
        literal=name,
        layer=layer,
        security_tier=security_tier,
        kind=kind,
        has_instance=has_instance,
    )


def decompose_all(names: List[str]) -> DecompositionResult:
    """
    Run inference + decomposition over a list of literals.

    Detects token-name collisions (two distinct literals decomposing
    to the same token name) and records them in ``result.collisions``
    so the caller can surface them as warnings.
    """
    roots, confidence = infer_composition_roots(names)

    decomposed: List[DecomposedName] = []
    outliers: List[str] = []

    for n in names:
        d = decompose_name(n, roots)
        if d is None:
            outliers.append(n)
        else:
            decomposed.append(d)

    # Detect collisions: same token_name produced by different literals
    by_token: Dict[str, List[str]] = defaultdict(list)
    for d in decomposed:
        by_token[d.token_name].append(d.literal)
    collisions = {k: v for k, v in by_token.items() if len(v) > 1}

    return DecompositionResult(
        roots=roots,
        decomposed=decomposed,
        outliers=outliers,
        confidence=confidence,
        collisions=collisions,
    )


# ---------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------


def _safe_token_name_for_outlier(literal: str) -> str:
    """Generate a deterministic token name for an outlier literal.

    Replaces non-word chars with underscores and appends ``_LITERAL``
    so the user can spot tokens that came in as outliers.
    """
    sanitised = re.sub(r"[^A-Za-z0-9_]", "_", literal).upper()
    return f"{sanitised}_LITERAL"


def format_properties_file(env: str, result: DecompositionResult) -> str:
    """Render a SHIPS .properties file from a decomposition result."""
    roots = result.roots

    lines = [
        "# ===================================================================",
        f"# {env}.properties — generated by decompose_database_names.py",
        "#",
        "# Composition roots and derived names were INFERRED from a list",
        "# of literal database names. Confidence per field is recorded in",
        "# the companion decomposition_report.md.",
        "#",
        "# Recommended next steps:",
        "#",
        "#   1. Verify the composition roots in section 1 against your",
        "#      platform topology. Low-confidence inferences are flagged.",
        "#",
        "#   2. Review the derived names in section 2 — each token name",
        "#      was generated from layer + kind segments. Rename to your",
        "#      domain language (e.g. MDL_T → BASE_T) where it improves",
        "#      readability.",
        "#",
        "#   3. Resolve outliers in section 3 — names that didn't match",
        "#      the grammar were emitted as literal-valued fallbacks.",
        "# ===================================================================",
        "",
        "# -------------------------------------------------------------------",
        "# 1. Composition roots",
        "# -------------------------------------------------------------------",
        f"SHIPS_ENV={roots.ships_env}",
        f"ENV_PREFIX={roots.env_prefix}",
    ]
    if roots.instance:
        lines.append(f"INSTANCE={roots.instance}")
    lines.append(f"SECURITY_TIER={roots.security_tier}")
    lines.append("")

    lines.append("# -------------------------------------------------------------------")
    lines.append("# 2. Derived database names")
    lines.append("# -------------------------------------------------------------------")
    if roots.instance:
        lines.append("PARENT_NODE={{ENV_PREFIX}}_{{SHIPS_ENV}}_{{INSTANCE}}")
        lines.append("")

    # Group by token_name to handle collisions deterministically.
    # Skip parent_node entries — PARENT_NODE was already emitted
    # explicitly above; including the decomposed copy would just
    # produce a redundant duplicate line.
    seen_tokens: Dict[str, DecomposedName] = {}
    for d in result.decomposed:
        if d.is_parent_node:
            continue
        if d.token_name in seen_tokens:
            # Collision — emit a warning and skip duplicate
            lines.append(
                f"# WARN collision: '{d.literal}' would shadow earlier "
                f"'{seen_tokens[d.token_name].literal}' on token "
                f"'{d.token_name}'. Disambiguate manually."
            )
            continue
        seen_tokens[d.token_name] = d
        lines.append(f"{d.token_name}={d.cascade_form()}")

    if result.outliers:
        lines.append("")
        lines.append("# -------------------------------------------------------------------")
        lines.append("# 3. Outliers — names that did not match the grammar")
        lines.append("# -------------------------------------------------------------------")
        lines.append("# Review each entry. If the name should fit the grammar, rename it")
        lines.append("# in source. If it's legitimately ad-hoc (e.g. an external user or")
        lines.append("# legacy database), keep the literal value.")
        lines.append("")
        for outlier in result.outliers:
            token = _safe_token_name_for_outlier(outlier)
            lines.append(f"{token}={outlier}")

    return "\n".join(lines) + "\n"


def format_decomposition_report(
    env: str, names: List[str], result: DecompositionResult
) -> str:
    """Render a markdown report of what was inferred and what wasn't."""
    roots = result.roots
    lines = [
        f"# Decomposition Report — {env}",
        "",
        f"Input: {len(names)} unique literal database name(s).",
        f"Decomposed: {len(result.decomposed)}.",
        f"Outliers: {len(result.outliers)}.",
        "",
        "## Composition Roots",
        "",
        "| Field | Value | Confidence |",
        "|---|---|---|",
        f"| ENV_PREFIX | `{roots.env_prefix}` | {result.confidence.get('env_prefix', '?')} |",
        f"| SHIPS_ENV | `{roots.ships_env}` | {result.confidence.get('ships_env', '?')} |",
        f"| INSTANCE | `{roots.instance or '(none)'}` | {result.confidence.get('instance', '?')} |",
        f"| SECURITY_TIER | `{roots.security_tier}` | {result.confidence.get('security_tier', '?')} |",
        "",
    ]

    if result.decomposed:
        lines.append("## Decomposed Names")
        lines.append("")
        lines.append("| Literal | Token | Cascade form |")
        lines.append("|---|---|---|")
        for d in result.decomposed:
            lines.append(f"| `{d.literal}` | `{d.token_name}` | `{d.cascade_form()}` |")
        lines.append("")

    if result.collisions:
        lines.append("## Token-name Collisions")
        lines.append("")
        lines.append(
            "Multiple literals decomposed to the same token name. "
            "Disambiguate by renaming literals in source or by editing "
            "the generated .properties file."
        )
        lines.append("")
        for token, literals in sorted(result.collisions.items()):
            lines.append(f"- `{token}`: {', '.join(f'`{l}`' for l in literals)}")
        lines.append("")

    if result.outliers:
        lines.append("## Outliers")
        lines.append("")
        lines.append(
            "These names did not match the inferred grammar and are "
            "emitted as literal-valued fallback tokens in the "
            ".properties file. Review each one."
        )
        lines.append("")
        for o in result.outliers:
            lines.append(f"- `{o}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="decompose_database_names.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "input",
        help="Path to a token_map.conf or plain names file (one literal "
        "per line). Format auto-detected.",
    )
    p.add_argument(
        "--env",
        required=True,
        help="Target environment name (DEV, TST, PRD).",
    )
    p.add_argument(
        "--output-dir",
        default=".",
        help="Output directory (default: current). Files written under "
        "<output-dir>/properties/<env>.properties and "
        "<output-dir>/decomposition_report.md.",
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

    try:
        names = read_input_file(args.input)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not names:
        print(
            f"ERROR: no literal database names found in {args.input}",
            file=sys.stderr,
        )
        return 1

    result = decompose_all(names)

    output_dir = Path(args.output_dir)
    properties_dir = output_dir / "properties"
    properties_dir.mkdir(parents=True, exist_ok=True)

    properties_path = properties_dir / f"{args.env}.properties"
    report_path = output_dir / "decomposition_report.md"

    properties_path.write_text(
        format_properties_file(args.env, result), encoding="utf-8"
    )
    report_path.write_text(
        format_decomposition_report(args.env, names, result),
        encoding="utf-8",
    )

    print("=" * 64)
    print(f"  Decomposed {len(names)} literal name(s) from {args.input}")
    print("=" * 64)
    print(f"  Composition roots:")
    print(
        f"    ENV_PREFIX={result.roots.env_prefix}  "
        f"({result.confidence.get('env_prefix', '?')})"
    )
    print(
        f"    SHIPS_ENV={result.roots.ships_env}  "
        f"({result.confidence.get('ships_env', '?')})"
    )
    print(
        f"    INSTANCE={result.roots.instance or '(none)'}  "
        f"({result.confidence.get('instance', '?')})"
    )
    print(
        f"    SECURITY_TIER={result.roots.security_tier}  "
        f"({result.confidence.get('security_tier', '?')})"
    )
    print(f"  Decomposed:    {len(result.decomposed)}")
    print(f"  Outliers:      {len(result.outliers)}")
    if result.collisions:
        print(f"  Collisions:    {len(result.collisions)}  (see report)")
    print()
    print(f"  Properties:    {properties_path}")
    print(f"  Report:        {report_path}")
    print("=" * 64)

    return 0


if __name__ == "__main__":
    sys.exit(main())
