#!/usr/bin/env python3
"""
generate_view_layer.py — Thin CLI shim for the view-layer generator.

The engine lives at ``td_release_packager.view_layer_generator`` so
that the future Generate stage of the orchestrator (build-order
item 7) can import and drive it directly. This file exists so that
the standalone, ad-hoc command continues to work for users running
the tool from a clone of the repo without installing the package:

    python tools/generate_view_layer.py --project ./X --modules ALL

If you have ``td_release_packager`` installed (``pip install -e .`` or
``uv sync``), the equivalent invocation is:

    python -m td_release_packager.view_layer_generator --project ./X --modules ALL

Both paths run the same code.
"""

import os
import sys

# Make ``td_release_packager`` importable when running from a fresh
# clone without ``pip install -e .``. This is the only thing that
# keeps the standalone ``python tools/generate_view_layer.py ...``
# invocation working in the no-install case; everything else lives
# in the package.
_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from td_release_packager.view_layer_generator import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
