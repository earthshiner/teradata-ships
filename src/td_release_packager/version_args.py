"""Shared argparse helpers for SHIPS CLI version flags."""

from __future__ import annotations

import argparse

from td_release_packager._version import __version__ as SHIPS_VERSION


def add_version_argument(
    parser: argparse.ArgumentParser,
    *,
    prog: str | None = None,
    version: str = SHIPS_VERSION,
) -> None:
    """Add a non-conflicting version argument to an argparse parser.

    ``-v`` is already used for verbose logging in SHIPS CLIs, so the short
    version flag is intentionally ``-V``.  ``--version`` is always provided.
    """
    if "--version" in parser._option_string_actions:
        return

    display_name = prog or parser.prog
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{display_name} {version}",
        help="Show version and exit.",
    )
