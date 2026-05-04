#!/usr/bin/env python3
"""
bootstrap_properties.py — Thin CLI shim for the
already-tokenised-source bootstrapper.

The engine lives at ``td_release_packager.properties_bootstrapper``
so the main CLI (``python -m td_release_packager bootstrap-properties``)
and this standalone shim share a single implementation.

Use this when your DDL is already tokenised (uses ``{{TOKEN}}``
references) but you don't yet have a ``.properties`` file with
values. It scans the project for referenced tokens and writes a
7-section ``.properties`` scaffold with all of them parked in
section 8 for you to re-section by cut-and-paste.

When to use this vs the other two bootstrappers:

    Source state                                 Use
    ──────────────────────────────────────────   ──────────────────────
    Have a sed substitution script               import-legacy
    Have only literal hardcoded database names   decompose-names
    Source already uses {{TOKEN}} references     bootstrap-properties

Usage::

    python tools/bootstrap_properties.py \\
        --source ./MyProject \\
        --env DEV \\
        --output-dir ./MyProject/config

If you have the package installed, the equivalent invocation is::

    python -m td_release_packager bootstrap-properties \\
        --source ./MyProject --env DEV
"""

import os
import sys

_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from td_release_packager.properties_bootstrapper import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
