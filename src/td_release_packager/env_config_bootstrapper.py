"""
env_config_bootstrapper.py — Bootstrap a SHIPS ``.conf`` file
from an already-tokenised project tree.

Closes the third bootstrap path. The other two assume some form of
literal substitution to start from:

    - import_legacy:    legacy sed script              → .conf
    - decomposer:       literal database names         → .conf
    - bootstrap (here): already-tokenised source tree  → .conf

When the harvested DDL already uses ``{{TOKEN}}`` references — the
end-state both other bootstrappers work towards — there are no
literals to convert and no substitution map to derive from. What's
missing is a ``.conf`` file with values for the tokens the
source actually references.

This module:
    1. Scans the project payload for ``{{TOKEN}}`` references.
    2. Optionally reads an existing ``.conf`` file at the
       target path and preserves any values already set.
    3. Renders a 7-section ``.conf`` scaffold via
       ``env_config_scaffold.render_scaffold`` with every referenced
       token parked in section 8 ("Imported (UNCATEGORISED)") for
       the user to re-section by cut-and-paste.

Re-running the tool against the same project after the user has
edited the file will OVERWRITE — the merge logic is intentionally
naive: tokens in source land in section 8, regardless of where
they were before. This is opt-in via ``--force``; without it, the
tool refuses to clobber an existing file. The intent is bootstrap-
once, then hand-edit; not a continuous regeneration cycle.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------


def discover_referenced_tokens(project_dir: str) -> Set[str]:
    """
    Scan ``<project_dir>`` for ``{{TOKEN}}`` references and return
    the deduped set of token names found.

    Prefers ``payload/`` if present (skips ``config/``, ``releases/``,
    ``logs/``, etc.) but falls back to the entire project root if
    no payload directory exists.
    """
    from td_release_packager.token_engine import scan_tokens_in_directory

    scan_root = project_dir
    for candidate in ("payload/database", "payload"):
        path = os.path.join(project_dir, candidate)
        if os.path.isdir(path):
            scan_root = path
            break

    usage = scan_tokens_in_directory(scan_root)
    referenced: Set[str] = set()
    for tokens in usage.values():
        referenced.update(tokens)
    return referenced


def read_existing_values(env_config_path: str) -> Dict[str, str]:
    """
    Read an existing ``.conf`` file at ``env_config_path`` and
    return ``{name: value}``. Returns an empty dict if the file does
    not exist. Never raises on a missing file — it's the expected
    case for the first bootstrap run.

    Uses the project's own properties parser so comments, blank
    lines, and the ``=``-after-first-occurrence rule are all
    handled consistently.
    """
    if not os.path.isfile(env_config_path):
        return {}
    from td_release_packager.token_engine import read_env_config

    try:
        return dict(read_env_config(env_config_path))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not parse existing properties at %s (%s) — treating as empty.",
            env_config_path,
            e,
        )
        return {}


# ---------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------


def format_section_8_body(referenced: Set[str], existing: Dict[str, str]) -> str:
    """
    Render the ``KEY=value`` lines that go in section 8 of the
    bootstrap output.

    Behaviour:
      - Every referenced token gets a line. Value is the existing
        value if known; otherwise empty.
      - Tokens defined in ``existing`` but NOT referenced are
        emitted with a ``# WARN unused`` comment so the user can
        decide whether to delete or whether a referencing file is
        missing.
      - Lines are sorted alphabetically by token name for stable
        output across runs.
    """
    lines: List[str] = []

    # Referenced tokens — with values where known
    for name in sorted(referenced):
        value = existing.get(name, "")
        lines.append(f"{name}={value}")

    # Unreferenced tokens that exist in the file — flag and keep
    unused = sorted(set(existing) - referenced)
    if unused:
        lines.append("")
        lines.append(
            "# --- UNUSED tokens — defined here but not referenced in source. ---"
        )
        lines.append(
            "# Review whether to delete or whether a referencing file is missing."
        )
        for name in unused:
            lines.append(f"# WARN unused: {name}={existing[name]}")

    return "\n".join(lines)


def render_bootstrap_env_config(
    env: str,
    referenced: Set[str],
    existing: Dict[str, str],
) -> str:
    """Render the full 7-section .conf scaffold for the
    bootstrap output."""
    from td_release_packager.env_config_scaffold import render_scaffold

    return render_scaffold(
        env=env,
        generator_label="bootstrap_env_config",
        source_label=("tokens scanned from already-tokenised project source"),
        next_steps=[
            "1. Identify your composition roots from the imported tokens",
            "   below and move them into section 1 with values that match",
            "   your target environment topology:",
            "",
            "     SHIPS_ENV       Logical env (DEV/TST/PRD)",
            "     ENV_PREFIX      Physical Teradata prefix",
            "     SHIPS_PROJECT   Project identifier",
            "     INSTANCE        00 (primary) or 01+ for parallel",
            "     SECURITY_TIER   0 (generally accessible) or 1+",
            "",
            "2. Move database-name tokens to section 2, ideally converting",
            "   to cascade form referencing PARENT_NODE.",
            "",
            "3. Move users/roles to section 3, SQL constants to section 4,",
            "   engine flags to section 5, length-policy to section 6,",
            "   diagnostic stanzas to section 7.",
            "",
            "4. Validate the result:",
            "     python -m td_release_packager scan --source <project> \\",
            "         --env-config config/env/{env}.conf".format(env=env),
            "",
            "5. Delete the Imported section once empty.",
        ],
        sections_content={},  # all empty — user re-sections from sec 8
        final_section_title="Imported (UNCATEGORISED)",
        final_section_purpose=[
            "Every {{TOKEN}} discovered in the project's tokenised",
            "DDL, with its current value (if a previous .conf",
            "file was found) or empty if none. Move each entry into",
            "the appropriate section above (1-7) and delete this",
            "section when empty.",
        ],
        final_section_content=format_section_8_body(referenced, existing),
    )


# ---------------------------------------------------------------
# Driver
# ---------------------------------------------------------------


def bootstrap_env_config_file(
    *,
    project_dir: str,
    env: str,
    output_dir: Optional[str] = None,
    force: bool = False,
) -> Dict[str, object]:
    """
    Run the full bootstrap pipeline and write the resulting
    ``.conf`` file.

    Args:
        project_dir: Path to the SHIPS project (with payload/).
        env:         Target environment name (DEV / TST / PRD).
        output_dir:  Directory under which to write
                     ``env/<env>.conf``. Defaults to
                     ``<project_dir>/config``.
        force:       Overwrite an existing properties file at the
                     target path. Without this flag, the tool
                     refuses to clobber.

    Returns:
        Diagnostic dict with the scan/merge results — keys:
            ``env_config_path``, ``referenced``, ``new``, ``unused``,
            ``preserved``, ``overwrote``.

    Raises:
        FileExistsError: if the target file exists and ``force`` is
                         False.
    """
    if not os.path.isdir(project_dir):
        raise NotADirectoryError(f"Project not found: {project_dir}")

    referenced = discover_referenced_tokens(project_dir)

    output_root = Path(output_dir) if output_dir else Path(project_dir) / "config"
    env_config_dir = output_root / "env"
    env_config_path = env_config_dir / f"{env}.conf"

    existing = read_existing_values(str(env_config_path))
    overwrote = bool(existing)

    if overwrote and not force:
        raise FileExistsError(
            f"Config file already exists: {env_config_path}\n"
            "  Re-run with --force to overwrite (existing values for "
            "still-referenced tokens will be preserved)."
        )

    new_tokens = sorted(referenced - set(existing))
    unused_tokens = sorted(set(existing) - referenced)
    preserved = sorted(referenced & set(existing))

    env_config_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_bootstrap_env_config(env, referenced, existing)
    env_config_path.write_text(rendered, encoding="utf-8")

    return {
        "env_config_path": str(env_config_path),
        "referenced": sorted(referenced),
        "new": new_tokens,
        "unused": unused_tokens,
        "preserved": preserved,
        "overwrote": overwrote,
    }


# ---------------------------------------------------------------
# CLI
# ---------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootstrap_env_config",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--source",
        required=True,
        help="SHIPS project directory (with payload/ already harvested).",
    )
    p.add_argument(
        "--env",
        required=True,
        help="Target environment name (DEV / TST / PRD).",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help="Output directory; the .conf file is written under "
        "<output-dir>/env/<env>.conf. Defaults to "
        "<source>/config.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .conf file at the target "
        "path. Without this, the tool refuses to clobber.",
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
        result = bootstrap_env_config_file(
            project_dir=args.source,
            env=args.env,
            output_dir=args.output_dir,
            force=args.force,
        )
    except (NotADirectoryError, FileExistsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Stage banner — "what just happened"
    print("=" * 64)
    print(f"  [bootstrap-env-config] — {args.env}.conf scaffold written")
    print("=" * 64)
    print(f"  Config file: {result['env_config_path']}")
    print(f"  Tokens referenced in source: {len(result['referenced'])}")
    if result["overwrote"]:
        print(
            f"  Existing file overwritten — {len(result['preserved'])} "
            f"value(s) preserved"
        )
        if result["unused"]:
            print(
                f"  Unused tokens flagged (defined but unreferenced): "
                f"{len(result['unused'])}"
            )
    else:
        print(
            f"  Empty entries created for "
            f"{len(result['new'])} token(s) — fill in values per env."
        )

    # Next-steps banner — "where am I, what's next"
    print()
    print("=" * 64)
    print("  Next Steps")
    print("=" * 64)
    print()
    print("  You are here: bootstrap-env-config complete")
    print("  Project state: tokens scanned, scaffold written, values empty")
    print()
    print("  → Next: edit the .conf file to populate values:")
    print(f"          {result['env_config_path']}")
    print()
    print("    Open it. Section 8 (Imported) lists every token your source")
    print("    references. Move each one into sections 1-7 by cut-and-paste:")
    print()
    print("      Section 1: SHIPS_ENV / ENV_PREFIX / SHIPS_PROJECT / etc.")
    print("      Section 2: derived database names (cascade form)")
    print("      Section 3: users + roles")
    print("      Section 4: SQL constants (date formats, type literals)")
    print("      Section 5: engine flags (YES/NO toggles)")
    print("      Section 6: field-length policy")
    print("      Section 7: diagnostic stanzas")
    print()
    print("    Delete section 8 when empty.")
    print()
    print("  → Then validate: ")
    print("      python -m td_release_packager scan \\")
    print(f"          --source {args.source} \\")
    print(f"          --env-config {result['env_config_path']}")
    print()
    print(f"{'=' * 64}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
