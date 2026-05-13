"""
graph_export.py — Dependency graph export for SHIPS.

Exports the dependency graph produced by the analyser in five
portable formats for consumption by external tools:

    DOT          Graphviz (.gv)   — dot, fdp, neato, sfdp, etc.
    Mermaid      (.mmd)           — GitHub, Confluence, docs
    JSON         (.json)          — D3, vis.js, cytoscape.js, Graph Discipline
    CSV          (.csv)           — Excel, Neo4j, Gephi, pandas
    OpenLineage  (.openlineage.json) — Marquez, DataHub, Atlan, GCP Lineage

All export functions accept an AnalysisResult and return a string.
The caller writes to disk and (optionally) wires into the CLI.

Usage:
    from td_release_packager.graph_export import (
        export_dot,
        export_mermaid,
        export_json,
        export_csv,
        export_openlineage,
        export_all,
    )

    result = analyse_project('/path/to/project')
    export_all(result, output_dir='/path/to/output')
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict

from td_release_packager.analyser import AnalysisResult

logger = logging.getLogger(__name__)

# -- Producer URI for OpenLineage events -------------------------
_PRODUCER = "https://github.com/earthshiner/teradata-ships"

# -- OpenLineage schema URLs ------------------------------------
_OL_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_OL_JOB_TYPE_SCHEMA = (
    "https://openlineage.io/spec/facets/2-0-2/"
    "JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
)
_OL_SQL_JOB_SCHEMA = (
    "https://openlineage.io/spec/facets/1-1-1/SQLJobFacet.json#/$defs/SQLJobFacet"
)

# -- Object type → Graphviz shape mapping -----------------------
_DOT_SHAPES = {
    "TABLE": "box",
    "VIEW": "parallelogram",
    "MACRO": "hexagon",
    "PROCEDURE": "octagon",
    "FUNCTION": "ellipse",
    "TRIGGER": "diamond",
    "JOIN_INDEX": "trapezium",
    "HASH_INDEX": "trapezium",
    "INDEX": "trapezium",
    "DATABASE": "folder",
    "SCRIPT_TABLE_OPERATOR": "component",
}

# -- Object type → Mermaid shape brackets -----------------------
#    Mermaid shapes: [rectangle], ([stadium]), {rhombus},
#    ((circle)), {{hexagon}}, [/parallelogram/], [\trapezoid\]
_MERMAID_SHAPES = {
    "TABLE": ("[", "]"),
    "VIEW": ("[/", "/]"),
    "MACRO": ("{{", "}}"),
    "PROCEDURE": ("([", "])"),
    "FUNCTION": ("((", "))"),
    "TRIGGER": ("{", "}"),
}


# ---------------------------------------------------------------
# DOT (Graphviz)
# ---------------------------------------------------------------


def export_dot(result: AnalysisResult) -> str:
    """
    Export the dependency graph in Graphviz DOT format.

    Produces a maximally compatible directed graph using only
    standard DOT attributes that all importers can parse
    (Gephi, yEd, Graphviz, vis.js).  Avoids global attribute
    blocks (graph/node/edge [...]) and custom attributes which
    some parsers silently reject.

    Edge direction is deployment flow: A -> B means A must
    exist before B can be created.

    Render with:  dot -Tsvg graph.gv -o graph.svg
                  dot -Tpng graph.gv -o graph.png

    Args:
        result: The AnalysisResult from analyse_project.

    Returns:
        DOT format string.
    """
    lines = [
        "digraph ships_dependencies {",
    ]

    # -- Group nodes by database for readability ------------------
    db_groups: Dict[str, list] = {}
    for qn, obj in sorted(result.objects.items()):
        parts = qn.split(".", 1)
        db = parts[0] if len(parts) == 2 else "_unqualified"
        db_groups.setdefault(db, []).append((qn, obj))

    # -- Emit all nodes at the top level --------------------------
    # Only standard attributes: label.  No global blocks, no
    # custom attributes, no shapes — Gephi ignores or chokes
    # on anything non-standard.
    for db, objects in sorted(db_groups.items()):
        for qn, obj in objects:
            node_id = _dot_id(qn)
            label = f"{obj.object_type}: {qn}"
            lines.append(f'    {node_id} [label="{label}"];')

    # -- Emit external dependency nodes ---------------------------
    ext_nodes = set()
    for ext_refs in result.external_deps.values():
        ext_nodes.update(ext_refs)

    if ext_nodes:
        for ext in sorted(ext_nodes):
            node_id = _dot_id(ext)
            label = f"EXTERNAL: {ext}"
            lines.append(f'    {node_id} [label="{label}"];')

    # -- Emit edges (deployment flow: dependency -> dependent) ----
    for qn, deps in sorted(result.dependencies.items()):
        tgt_id = _dot_id(qn)
        for dep in sorted(deps):
            src_id = _dot_id(dep)
            lines.append(f"    {src_id} -> {tgt_id};")

    # Emit external dependency edges
    for qn, ext_refs in sorted(result.external_deps.items()):
        tgt_id = _dot_id(qn)
        for ext in sorted(ext_refs):
            src_id = _dot_id(ext)
            lines.append(f"    {src_id} -> {tgt_id};")

    lines.append("}")
    return "\n".join(lines)


def _dot_id(qualified_name: str) -> str:
    """
    Convert a qualified name to a valid DOT node identifier.

    Replaces dots, braces, and spaces with underscores.
    Wraps in double quotes to handle any remaining special chars.
    """
    safe = (
        qualified_name.replace("{{", "")
        .replace("}}", "")
        .replace(".", "_")
        .replace(" ", "_")
    )
    return f'"{safe}"'


# ---------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------


def export_mermaid(result: AnalysisResult) -> str:
    """
    Export the dependency graph in Mermaid format.

    Produces a top-to-bottom flowchart with nodes shaped by
    object type.  Edge direction is deployment flow: source must
    exist before target.  Renders natively in GitHub markdown,
    Confluence, and documentation tools.

    Args:
        result: The AnalysisResult from analyse_project.

    Returns:
        Mermaid format string.
    """
    lines = [
        "%% SHIPS Dependency Graph",
        "%% Auto-generated by td_release_packager.graph_export",
        "",
        "flowchart TB",
    ]

    # Emit nodes with type-specific shapes
    for qn, obj in sorted(result.objects.items()):
        node_id = _mermaid_id(qn)
        label = qn
        open_b, close_b = _MERMAID_SHAPES.get(
            obj.object_type,
            ("[", "]"),
        )
        lines.append(f'    {node_id}{open_b}"{label}"{close_b}')

    lines.append("")

    # Emit edges (deployment flow: dependency → dependent)
    for qn, deps in sorted(result.dependencies.items()):
        tgt_id = _mermaid_id(qn)
        for dep in sorted(deps):
            src_id = _mermaid_id(dep)
            lines.append(f"    {src_id} --> {tgt_id}")

    # Emit external edges (dashed)
    for qn, ext_refs in sorted(result.external_deps.items()):
        tgt_id = _mermaid_id(qn)
        for ext in sorted(ext_refs):
            ext_id = _mermaid_id(ext)
            # Declare external node if not already
            lines.append(f'    {ext_id}[/"{ext}"\\]')
            lines.append(f"    {ext_id} -.-> {tgt_id}")

    # Style classes
    lines.append("")
    lines.append("    classDef table fill:#E8F4FD,stroke:#0076CE")
    lines.append("    classDef view fill:#E8F0E8,stroke:#4CAF50")
    lines.append("    classDef external fill:#FFF3E0,stroke:#FF5F02")

    # Apply classes
    for qn, obj in result.objects.items():
        node_id = _mermaid_id(qn)
        if obj.object_type == "TABLE":
            lines.append(f"    class {node_id} table")
        elif obj.object_type == "VIEW":
            lines.append(f"    class {node_id} view")

    return "\n".join(lines)


def _mermaid_id(qualified_name: str) -> str:
    """
    Convert a qualified name to a valid Mermaid node identifier.

    Mermaid IDs cannot contain dots or braces.
    """
    return (
        qualified_name.replace("{{", "")
        .replace("}}", "")
        .replace(".", "_")
        .replace(" ", "_")
    )


# ---------------------------------------------------------------
# JSON (adjacency list + metadata)
# ---------------------------------------------------------------


def export_json(result: AnalysisResult) -> str:
    """
    Export the dependency graph as a JSON document.

    Produces a structure consumable by D3, vis.js, cytoscape.js,
    and the Teradata Graph Discipline skill:

        {
            "metadata": { ... },
            "nodes": [ { "id", "type", "database", "file", "wave" } ],
            "edges": [ { "source", "target", "type" } ],
            "waves": [ ["obj1", "obj2"], ["obj3"] ],
            "cycles": [ ["A", "B", "A"] ],
            "external_dependencies": { "obj": ["ext1"] }
        }

    Args:
        result: The AnalysisResult from analyse_project.

    Returns:
        Pretty-printed JSON string.
    """
    # Build wave lookup: object → wave number
    wave_lookup = {}
    for i, wave in enumerate(result.waves):
        for qn in wave:
            wave_lookup[qn] = i + 1

    # Build nodes
    nodes = []
    for qn, obj in sorted(result.objects.items()):
        parts = qn.split(".", 1)
        node = {
            "id": qn,
            "type": obj.object_type,
            "database": parts[0] if len(parts) == 2 else "",
            "object_name": parts[1] if len(parts) == 2 else parts[0],
            "file": obj.file_path,
            "wave": wave_lookup.get(qn, 0),
        }
        if obj.base_function:
            node["base_function"] = obj.base_function
        nodes.append(node)

    # Build edges (deployment flow: dependency → dependent)
    edges = []
    for qn, deps in sorted(result.dependencies.items()):
        for dep in sorted(deps):
            edges.append(
                {
                    "source": dep,
                    "target": qn,
                    "type": "internal",
                }
            )

    # External edges
    for qn, ext_refs in sorted(result.external_deps.items()):
        for ext in sorted(ext_refs):
            edges.append(
                {
                    "source": ext,
                    "target": qn,
                    "type": "external",
                }
            )

    # Serialise external deps (sets → lists for JSON)
    ext_deps_json = {
        qn: sorted(refs) for qn, refs in sorted(result.external_deps.items())
    }

    doc = {
        "metadata": {
            "generator": "td_release_packager.graph_export",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "object_count": len(result.objects),
            "edge_count": len(edges),
            "wave_count": len(result.waves),
            "cycle_count": len(result.cycles),
        },
        "nodes": nodes,
        "edges": edges,
        "waves": result.waves,
        "cycles": result.cycles,
        "external_dependencies": ext_deps_json,
    }

    return json.dumps(doc, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------
# CSV (edge list)
# ---------------------------------------------------------------


def export_csv(result: AnalysisResult) -> str:
    """
    Export the dependency graph as a CSV edge list.

    Produces a simple CSV with columns:
        source, target, edge_type, source_type, target_type

    Edge direction is deployment flow: source must exist before
    target can be created (e.g. TABLE → VIEW, not VIEW → TABLE).

    Importable into Excel, Neo4j, Gephi, pandas, or any tool
    that consumes edge lists.

    Args:
        result: The AnalysisResult from analyse_project.

    Returns:
        CSV string with header row and data rows only.
    """
    lines = [
        "source,target,edge_type,source_type,target_type",
    ]

    # Edge direction: dependency → dependent (deployment flow).
    # dependencies[qn] = {dep1, dep2} means qn depends on dep1.
    # So dep is the source (exists first), qn is the target.
    for qn, deps in sorted(result.dependencies.items()):
        tgt_type = result.objects[qn].object_type if qn in result.objects else ""
        for dep in sorted(deps):
            src_type = result.objects[dep].object_type if dep in result.objects else ""
            lines.append(f"{dep},{qn},internal,{src_type},{tgt_type}")

    for qn, ext_refs in sorted(result.external_deps.items()):
        tgt_type = result.objects[qn].object_type if qn in result.objects else ""
        for ext in sorted(ext_refs):
            lines.append(f"{ext},{qn},external,,{tgt_type}")

    return "\n".join(lines)


# ---------------------------------------------------------------
# OpenLineage (spec 2-0-2)
# ---------------------------------------------------------------


def export_openlineage(
    result: AnalysisResult,
    namespace: str = "teradata://ships-analysis",
    project_name: str = "ships-project",
) -> str:
    """
    Export the dependency graph as OpenLineage RunEvents.

    Produces one COMPLETE RunEvent per object in the package.
    Each event models the DDL deployment as a "job" with:
    - Input datasets  = objects it depends on (internal deps)
    - Output dataset  = the object being created/replaced
    - Job facets      = JobType (BATCH/SHIPS), SqlJob (DDL text)

    The output is NDJSON (newline-delimited JSON) — one complete
    RunEvent JSON object per line.  This is the standard format
    for OpenLineage event files and is accepted by all compatible
    backends:
    - Marquez (reference implementation)
    - DataHub
    - Atlan
    - GCP Data Lineage API
    - OpenMetadata

    For runtime lineage (during actual deployment), the deployer
    should emit START and COMPLETE/FAIL events with the same
    run ID.  This static export provides the relationship
    structure for cataloguing and visualisation.

    Args:
        result:       The AnalysisResult from analyse_project.
        namespace:    The dataset namespace URI.  For a real
                      Teradata system, use:
                          teradata://hostname:1025
                      Defaults to 'teradata://ships-analysis'
                      for static analysis output.
        project_name: The job namespace (project/scheduler name).

    Returns:
        NDJSON string — one RunEvent JSON object per line.
    """
    # Build wave lookup for wave facet
    wave_lookup = {}
    for i, wave in enumerate(result.waves):
        for qn in wave:
            wave_lookup[qn] = i + 1

    # Generate a parent run ID for the overall analysis
    analysis_run_id = str(uuid.uuid4())
    event_time = datetime.now(timezone.utc).isoformat()

    events = []

    for qn, obj in sorted(result.objects.items()):
        # Split qualified name into database and object
        parts = qn.split(".", 1)
        db_name = parts[0] if len(parts) == 2 else ""
        obj_name = parts[1] if len(parts) == 2 else parts[0]

        # Dataset namespace includes the database
        ds_namespace = f"{namespace}/{db_name}" if db_name else namespace

        # Build input datasets from internal dependencies
        inputs = []
        for dep in sorted(result.dependencies.get(qn, set())):
            dep_parts = dep.split(".", 1)
            dep_db = dep_parts[0] if len(dep_parts) == 2 else ""
            dep_obj = dep_parts[1] if len(dep_parts) == 2 else dep_parts[0]
            dep_ns = f"{namespace}/{dep_db}" if dep_db else namespace

            inputs.append(
                {
                    "namespace": dep_ns,
                    "name": dep_obj,
                }
            )

        # Build output dataset (the object itself)
        outputs = [
            {
                "namespace": ds_namespace,
                "name": obj_name,
            }
        ]

        # Build the RunEvent
        event = {
            "eventTime": event_time,
            "eventType": "COMPLETE",
            "producer": _PRODUCER,
            "schemaURL": _OL_SCHEMA_URL,
            "job": {
                "namespace": project_name,
                "name": f"deploy.{qn}",
                "facets": {
                    "jobType": {
                        "_producer": _PRODUCER,
                        "_schemaURL": _OL_JOB_TYPE_SCHEMA,
                        "processingType": "BATCH",
                        "integration": "SHIPS",
                        "jobType": "DDL_DEPLOYMENT",
                    },
                    "sql": {
                        "_producer": _PRODUCER,
                        "_schemaURL": _OL_SQL_JOB_SCHEMA,
                        "query": obj.ddl_text,
                    },
                },
            },
            "run": {
                "runId": str(uuid.uuid4()),
                "facets": {
                    "parent": {
                        "_producer": _PRODUCER,
                        "_schemaURL": (
                            "https://openlineage.io/spec/facets/"
                            "1-0-0/ParentRunFacet.json"
                        ),
                        "job": {
                            "namespace": project_name,
                            "name": "ships-analysis",
                        },
                        "run": {
                            "runId": analysis_run_id,
                        },
                    },
                    # Custom facet: SHIPS-specific metadata
                    "ships": {
                        "_producer": _PRODUCER,
                        "_schemaURL": (
                            f"{_PRODUCER}/blob/main/spec/ShipsRunFacet.json"
                        ),
                        "objectType": obj.object_type,
                        "qualifiedName": qn,
                        "wave": wave_lookup.get(qn, 0),
                        "filePath": obj.file_path,
                    },
                },
            },
            "inputs": inputs,
            "outputs": outputs,
        }

        events.append(event)

    # NDJSON: one complete JSON object per line, no wrapping array.
    # This is the standard format for OpenLineage event files.
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


# ---------------------------------------------------------------
# Batch export — all formats at once
# ---------------------------------------------------------------


def export_all(
    result: AnalysisResult,
    output_dir: str,
    base_name: str = "ships_dependencies",
    namespace: str = "teradata://ships-analysis",
    project_name: str = "ships-project",
) -> Dict[str, str]:
    """
    Export the dependency graph in all supported formats.

    Writes five files to output_dir:
        {base_name}.gv                  — Graphviz DOT
        {base_name}.mmd                 — Mermaid
        {base_name}.json                — JSON adjacency list
        {base_name}.csv                 — CSV edge list
        {base_name}.openlineage.json    — OpenLineage events

    Args:
        result:       The AnalysisResult from analyse_project.
        output_dir:   Directory to write files to (created if needed).
        base_name:    Base filename (without extension).
        namespace:    OpenLineage dataset namespace URI.
        project_name: OpenLineage job namespace.

    Returns:
        Dict mapping format name → file path written.
    """
    os.makedirs(output_dir, exist_ok=True)

    exports = {
        "dot": (f"{base_name}.gv", export_dot(result)),
        "mermaid": (f"{base_name}.mmd", export_mermaid(result)),
        "json": (f"{base_name}.json", export_json(result)),
        "csv": (f"{base_name}.csv", export_csv(result)),
        "openlineage": (
            f"{base_name}.openlineage.json",
            export_openlineage(result, namespace, project_name),
        ),
    }

    paths = {}
    for fmt, (filename, content) in exports.items():
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        paths[fmt] = filepath
        logger.info("Exported %s → %s", fmt, filepath)

    return paths
