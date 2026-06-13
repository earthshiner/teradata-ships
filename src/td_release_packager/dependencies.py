"""
dependencies.py — Canonical dependency graph artefact for packages (#150).

Wraps the existing analyser + JSON exporter into the now-familiar
canonical-file pattern used by trust (#146), actions (#143),
capabilities (#149), policy (#151), and required-evidence (#148).

``context/ships.dependencies.json`` lets an agent read the package's
dependency graph — objects, edges, deploy waves, cycles, and
unresolved/external references — without parsing ``_waves.txt`` or
the per-phase order files. Same shape produced by
``td_release_packager analyze --formats json`` so existing consumers
(D3, vis.js, cytoscape.js, Teradata Graph Discipline skill) keep
working.
"""

from __future__ import annotations

import json
import os
from typing import Optional


# ---------------------------------------------------------------
# Schema + filename
# ---------------------------------------------------------------

DEPENDENCIES_SCHEMA_VERSION = "1.0"

DEPENDENCIES_RESULT_FILENAME = "ships.dependencies.json"
DEPENDENCIES_RESULT_REF = f"context/{DEPENDENCIES_RESULT_FILENAME}"


# ---------------------------------------------------------------
# Computation
# ---------------------------------------------------------------


def compute_dependencies_document(source_dir: str) -> dict:
    """Run dependency analysis on ``source_dir`` and return the JSON
    document an agent will read.

    Wraps ``analyse_project`` + ``export_json`` so the canonical file
    is exactly the same shape as ``analyze --formats json`` produces,
    plus a top-level ``schema_version`` field so consumers can pin.
    """
    from td_release_packager.analyser import analyse_project
    from td_release_packager.graph_export import export_json

    result = analyse_project(source_dir)
    doc = json.loads(export_json(result))
    # Tag the top-level document so an agent can pin against the
    # schema. Keep the existing ``metadata`` block alongside so
    # downstream consumers (D3 viewers, the Graph Discipline skill)
    # see no change.
    doc["schema_version"] = DEPENDENCIES_SCHEMA_VERSION
    return doc


# ---------------------------------------------------------------
# I/O
# ---------------------------------------------------------------


def write_dependencies_result(pkg_dir: str, source_dir: str) -> str:
    """Run the analyser against ``source_dir`` and write the result to
    ``<pkg_dir>/context/ships.dependencies.json``. Returns the path.
    """
    doc = compute_dependencies_document(source_dir)
    path = os.path.join(pkg_dir, "context", DEPENDENCIES_RESULT_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_dependencies_result(pkg_dir: str) -> Optional[dict]:
    """Load ``context/ships.dependencies.json`` from ``pkg_dir`` or
    return None when absent / unreadable."""
    path = os.path.join(pkg_dir, "context", DEPENDENCIES_RESULT_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
