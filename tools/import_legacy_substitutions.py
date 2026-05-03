#!/usr/bin/env python3
"""
import_legacy_substitutions.py — Thin CLI shim for the legacy
substitution-script importer.

The engine lives at ``td_release_packager.legacy_importer`` so the
main CLI (``python -m td_release_packager import-legacy ...``) and
this standalone shim share a single implementation. This file exists
so the ad-hoc command keeps working from a fresh clone without
``pip install -e .``::

    python tools/import_legacy_substitutions.py legacy.sh \\
        --env DEV --output-dir ./MyProject/config

If you have the package installed, the equivalent invocation is::

    python -m td_release_packager import-legacy legacy.sh \\
        --env DEV --output-dir ./MyProject/config
"""

import os
import sys

# Make td_release_packager importable when running from a fresh
# clone without `pip install -e .`. Mirrors the pattern used by
# tools/generate_view_layer.py.
_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from td_release_packager.legacy_importer import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
