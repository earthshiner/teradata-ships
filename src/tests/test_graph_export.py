"""
test_graph_export.py — Tests for the SHIPS dependency graph exporter.

Covers all five export formats:
    - DOT (Graphviz)
    - Mermaid
    - JSON (adjacency list)
    - CSV (edge list)
    - OpenLineage (spec 2-0-2)

Also covers:
    - export_all batch export
    - Empty graph handling
    - External dependency rendering
    - Cycle metadata
"""

import json
import os
import pytest

from td_release_packager.analyser import AnalysisResult, IndexedObject
from td_release_packager.graph_export import (
    export_dot,
    export_mermaid,
    export_json,
    export_csv,
    export_openlineage,
    export_all,
)


# ---------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------

@pytest.fixture
def simple_result():
    """
    A minimal three-object dependency graph for testing.

    STD.Customer (TABLE, no deps)
        ↑
    VIW.Customer (VIEW, depends on STD.Customer)
        ↑
    SEM.Dashboard (VIEW, depends on VIW.Customer)
    """
    result = AnalysisResult()

    result.objects = {
        "STD.Customer": IndexedObject(
            "STD.Customer", "TABLE",
            "DDL/tables/STD.Customer.tbl",
            "CREATE MULTISET TABLE STD.Customer (Id INTEGER);",
        ),
        "VIW.Customer": IndexedObject(
            "VIW.Customer", "VIEW",
            "DDL/views/VIW.Customer.viw",
            "REPLACE VIEW VIW.Customer AS\n"
            "SELECT * FROM STD.Customer;",
        ),
        "SEM.Dashboard": IndexedObject(
            "SEM.Dashboard", "VIEW",
            "DDL/views/SEM.Dashboard.viw",
            "REPLACE VIEW SEM.Dashboard AS\n"
            "SELECT * FROM VIW.Customer;",
        ),
    }

    result.dependencies = {
        "STD.Customer": set(),
        "VIW.Customer": {"STD.Customer"},
        "SEM.Dashboard": {"VIW.Customer"},
    }

    result.waves = [
        ["STD.Customer"],
        ["VIW.Customer"],
        ["SEM.Dashboard"],
    ]

    result.cycles = []
    result.external_deps = {}
    result.function_groups = {}
    result.waves_file_content = ""

    return result


@pytest.fixture
def result_with_externals(simple_result):
    """
    Extends simple_result with an external dependency.

    SEM.Dashboard also depends on ExternalDB.Lookup (external).
    """
    simple_result.external_deps = {
        "SEM.Dashboard": {"ExternalDB.Lookup"},
    }
    return simple_result


@pytest.fixture
def empty_result():
    """An empty analysis result (no objects)."""
    return AnalysisResult()


# ---------------------------------------------------------------
# DOT (Graphviz) tests
# ---------------------------------------------------------------

class TestExportDot:
    """Tests for Graphviz DOT export."""

    def test_valid_dot_structure(self, simple_result):
        """Output is a valid DOT digraph."""
        dot = export_dot(simple_result)
        assert dot.strip().startswith("digraph ships_dependencies {")
        assert dot.strip().endswith("}")

    def test_nodes_present(self, simple_result):
        """All objects appear as nodes with label attributes."""
        dot = export_dot(simple_result)
        assert "STD_Customer" in dot
        assert "VIW_Customer" in dot
        assert "SEM_Dashboard" in dot

    def test_node_labels_include_type(self, simple_result):
        """Node labels include the object type for identification."""
        dot = export_dot(simple_result)
        assert 'label="TABLE: STD.Customer"' in dot
        assert 'label="VIEW: VIW.Customer"' in dot

    def test_edges_present(self, simple_result):
        """Dependency edges use -> syntax."""
        dot = export_dot(simple_result)
        assert "->" in dot

    def test_edge_direction_deployment_flow(self, simple_result):
        """Edges flow from dependency to dependent (TABLE -> VIEW)."""
        dot = export_dot(simple_result)
        # STD.Customer must exist before VIW.Customer
        assert '"STD_Customer" -> "VIW_Customer"' in dot

    def test_external_deps_present(self, result_with_externals):
        """External dependencies appear as nodes and edges."""
        dot = export_dot(result_with_externals)
        assert "ExternalDB_Lookup" in dot
        assert 'label="EXTERNAL: ExternalDB.Lookup"' in dot

    def test_no_global_attribute_blocks(self, simple_result):
        """No graph/node/edge global blocks (Gephi can't parse them)."""
        dot = export_dot(simple_result)
        assert "graph [" not in dot
        assert "node [" not in dot
        assert "edge [" not in dot

    def test_only_label_attribute(self, simple_result):
        """Nodes use only the label attribute (maximum compatibility)."""
        dot = export_dot(simple_result)
        # No custom attributes that break Gephi
        assert "database=" not in dot
        assert "object_type=" not in dot
        assert "shape=" not in dot

    def test_empty_graph(self, empty_result):
        """Empty graph produces valid DOT with no nodes."""
        dot = export_dot(empty_result)
        assert "digraph ships_dependencies {" in dot
        assert dot.strip().endswith("}")


# ---------------------------------------------------------------
# Mermaid tests
# ---------------------------------------------------------------

class TestExportMermaid:
    """Tests for Mermaid export."""

    def test_valid_mermaid_structure(self, simple_result):
        """Output starts with a flowchart declaration."""
        mmd = export_mermaid(simple_result)
        assert "flowchart TB" in mmd

    def test_nodes_present(self, simple_result):
        """All objects appear as nodes."""
        mmd = export_mermaid(simple_result)
        assert "STD_Customer" in mmd
        assert "VIW_Customer" in mmd
        assert "SEM_Dashboard" in mmd

    def test_edges_present(self, simple_result):
        """Dependency edges use --> syntax."""
        mmd = export_mermaid(simple_result)
        assert "-->" in mmd

    def test_table_shape_brackets(self, simple_result):
        """Tables use rectangle brackets."""
        mmd = export_mermaid(simple_result)
        # TABLE shape is ["label"]
        assert '["STD.Customer"]' in mmd

    def test_view_shape_brackets(self, simple_result):
        """Views use parallelogram brackets."""
        mmd = export_mermaid(simple_result)
        # VIEW shape is [/"label"/]
        assert '[/"VIW.Customer"/]' in mmd

    def test_external_deps_dashed_arrow(self, result_with_externals):
        """External dependencies use dashed arrows."""
        mmd = export_mermaid(result_with_externals)
        assert "-.->" in mmd

    def test_style_classes(self, simple_result):
        """Style class definitions are present."""
        mmd = export_mermaid(simple_result)
        assert "classDef table" in mmd
        assert "classDef view" in mmd

    def test_empty_graph(self, empty_result):
        """Empty graph produces valid Mermaid."""
        mmd = export_mermaid(empty_result)
        assert "flowchart TB" in mmd


# ---------------------------------------------------------------
# JSON tests
# ---------------------------------------------------------------

class TestExportJson:
    """Tests for JSON adjacency list export."""

    def test_valid_json(self, simple_result):
        """Output is valid JSON."""
        raw = export_json(simple_result)
        doc = json.loads(raw)
        assert isinstance(doc, dict)

    def test_metadata_section(self, simple_result):
        """Metadata includes counts."""
        doc = json.loads(export_json(simple_result))
        meta = doc["metadata"]
        assert meta["object_count"] == 3
        assert meta["wave_count"] == 3
        assert meta["cycle_count"] == 0

    def test_nodes_structure(self, simple_result):
        """Each node has required fields."""
        doc = json.loads(export_json(simple_result))
        for node in doc["nodes"]:
            assert "id" in node
            assert "type" in node
            assert "database" in node
            assert "object_name" in node
            assert "file" in node
            assert "wave" in node

    def test_node_wave_numbers(self, simple_result):
        """Nodes have correct wave assignments."""
        doc = json.loads(export_json(simple_result))
        node_map = {n["id"]: n for n in doc["nodes"]}
        assert node_map["STD.Customer"]["wave"] == 1
        assert node_map["VIW.Customer"]["wave"] == 2
        assert node_map["SEM.Dashboard"]["wave"] == 3

    def test_edges_structure(self, simple_result):
        """Each edge has source, target, and type."""
        doc = json.loads(export_json(simple_result))
        for edge in doc["edges"]:
            assert "source" in edge
            assert "target" in edge
            assert "type" in edge

    def test_internal_edges(self, simple_result):
        """Internal dependency edges are typed 'internal'."""
        doc = json.loads(export_json(simple_result))
        internal_edges = [
            e for e in doc["edges"] if e["type"] == "internal"
        ]
        assert len(internal_edges) == 2

    def test_external_edges(self, result_with_externals):
        """External dependency edges are typed 'external'."""
        doc = json.loads(export_json(result_with_externals))
        ext_edges = [
            e for e in doc["edges"] if e["type"] == "external"
        ]
        assert len(ext_edges) == 1
        assert ext_edges[0]["source"] == "ExternalDB.Lookup"

    def test_waves_preserved(self, simple_result):
        """Wave structure is preserved in JSON output."""
        doc = json.loads(export_json(simple_result))
        assert len(doc["waves"]) == 3
        assert doc["waves"][0] == ["STD.Customer"]

    def test_empty_graph(self, empty_result):
        """Empty graph produces valid JSON with empty arrays."""
        doc = json.loads(export_json(empty_result))
        assert doc["nodes"] == []
        assert doc["edges"] == []
        assert doc["waves"] == []


# ---------------------------------------------------------------
# CSV tests
# ---------------------------------------------------------------

class TestExportCsv:
    """Tests for CSV edge list export."""

    def test_header_row(self, simple_result):
        """CSV has a header row with column names."""
        csv = export_csv(simple_result)
        first_line = csv.split('\n')[0]
        assert first_line == "source,target,edge_type,source_type,target_type"

    def test_edge_count(self, simple_result):
        """Two internal edges in the simple graph."""
        csv = export_csv(simple_result)
        data_lines = [
            l for l in csv.split('\n')
            if l.strip() and not l.startswith('source')
        ]
        assert len(data_lines) == 2

    def test_edge_content(self, simple_result):
        """Edge rows contain correct object names."""
        csv = export_csv(simple_result)
        assert "STD.Customer,VIW.Customer,internal,TABLE,VIEW" in csv
        assert "VIW.Customer,SEM.Dashboard,internal,VIEW,VIEW" in csv

    def test_external_edge(self, result_with_externals):
        """External edges are included with type 'external'."""
        csv = export_csv(result_with_externals)
        assert "ExternalDB.Lookup,SEM.Dashboard,external,,VIEW" in csv

    def test_no_comment_lines(self, simple_result):
        """CSV has no comment lines — clean format for all parsers."""
        csv = export_csv(simple_result)
        for line in csv.split('\n'):
            assert not line.startswith('#'), f"Comment found: {line}"

    def test_empty_graph(self, empty_result):
        """Empty graph produces header-only CSV."""
        csv = export_csv(empty_result)
        lines = [l for l in csv.split('\n') if l.strip()]
        assert len(lines) == 1  # Header only


# ---------------------------------------------------------------
# OpenLineage tests
# ---------------------------------------------------------------

class TestExportOpenLineage:
    """Tests for OpenLineage spec 2-0-2 export."""

    @staticmethod
    def _parse_ndjson(raw: str) -> list:
        """Parse NDJSON (newline-delimited JSON) into a list of dicts."""
        return [
            json.loads(line)
            for line in raw.strip().splitlines()
            if line.strip()
        ]

    def test_valid_ndjson(self, simple_result):
        """Output is valid NDJSON — one JSON object per line."""
        raw = export_openlineage(simple_result)
        events = self._parse_ndjson(raw)
        assert len(events) == 3
        # Each line must be independently valid JSON
        for line in raw.strip().splitlines():
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_event_structure(self, simple_result):
        """Each event has all required OpenLineage fields."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        for event in events:
            assert "eventTime" in event
            assert "eventType" in event
            assert event["eventType"] == "COMPLETE"
            assert "producer" in event
            assert "schemaURL" in event
            assert "job" in event
            assert "run" in event
            assert "inputs" in event
            assert "outputs" in event

    def test_schema_url(self, simple_result):
        """Schema URL points to OpenLineage spec 2-0-2."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        for event in events:
            assert "2-0-2" in event["schemaURL"]
            assert "RunEvent" in event["schemaURL"]

    def test_job_namespace_and_name(self, simple_result):
        """Job has a namespace and a qualified name."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        for event in events:
            assert event["job"]["namespace"] == "ships-project"
            assert event["job"]["name"].startswith("deploy.")

    def test_job_type_facet(self, simple_result):
        """JobType facet identifies SHIPS as the integration."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        for event in events:
            jt = event["job"]["facets"]["jobType"]
            assert jt["processingType"] == "BATCH"
            assert jt["integration"] == "SHIPS"
            assert jt["jobType"] == "DDL_DEPLOYMENT"
            assert "_producer" in jt
            assert "_schemaURL" in jt

    def test_sql_facet(self, simple_result):
        """SqlJob facet contains the DDL text."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        # Find the Customer table event
        cust_event = next(
            e for e in events
            if e["job"]["name"] == "deploy.STD.Customer"
        )
        assert "CREATE MULTISET TABLE" in cust_event["job"]["facets"]["sql"]["query"]

    def test_run_id_unique(self, simple_result):
        """Each event has a unique run ID."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        run_ids = [e["run"]["runId"] for e in events]
        assert len(set(run_ids)) == len(run_ids)

    def test_parent_run_facet(self, simple_result):
        """All events share the same parent run ID."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        parent_ids = [
            e["run"]["facets"]["parent"]["run"]["runId"]
            for e in events
        ]
        assert len(set(parent_ids)) == 1  # All same parent

    def test_ships_custom_facet(self, simple_result):
        """Custom SHIPS facet includes object metadata."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        cust_event = next(
            e for e in events
            if e["job"]["name"] == "deploy.STD.Customer"
        )
        ships = cust_event["run"]["facets"]["ships"]
        assert ships["objectType"] == "TABLE"
        assert ships["qualifiedName"] == "STD.Customer"
        assert ships["wave"] == 1

    def test_input_datasets(self, simple_result):
        """Input datasets reflect internal dependencies."""
        events = self._parse_ndjson(export_openlineage(simple_result))

        # STD.Customer has no inputs
        cust_event = next(
            e for e in events
            if e["job"]["name"] == "deploy.STD.Customer"
        )
        assert cust_event["inputs"] == []

        # VIW.Customer has one input (STD.Customer)
        viw_event = next(
            e for e in events
            if e["job"]["name"] == "deploy.VIW.Customer"
        )
        assert len(viw_event["inputs"]) == 1
        assert viw_event["inputs"][0]["name"] == "Customer"
        assert "STD" in viw_event["inputs"][0]["namespace"]

    def test_output_dataset(self, simple_result):
        """Each event has exactly one output dataset."""
        events = self._parse_ndjson(export_openlineage(simple_result))
        for event in events:
            assert len(event["outputs"]) == 1

    def test_custom_namespace(self, simple_result):
        """Custom namespace URI flows through to datasets."""
        events = self._parse_ndjson(export_openlineage(
            simple_result,
            namespace="teradata://prod-server:1025",
        ))
        for event in events:
            ns = event["outputs"][0]["namespace"]
            assert ns.startswith("teradata://prod-server:1025")

    def test_empty_graph(self, empty_result):
        """Empty graph produces an empty array."""
        events = self._parse_ndjson(export_openlineage(empty_result))
        assert events == []


# ---------------------------------------------------------------
# export_all batch tests
# ---------------------------------------------------------------

class TestExportAll:
    """Tests for batch export to all formats."""

    def test_creates_all_files(self, simple_result, tmp_path):
        """export_all creates all five output files."""
        paths = export_all(simple_result, str(tmp_path))
        assert len(paths) == 5
        for fmt, filepath in paths.items():
            assert os.path.isfile(filepath), f"Missing: {filepath}"

    def test_file_extensions(self, simple_result, tmp_path):
        """Output files have correct extensions."""
        paths = export_all(simple_result, str(tmp_path))
        assert paths["dot"].endswith(".gv")
        assert paths["mermaid"].endswith(".mmd")
        assert paths["json"].endswith(".json")
        assert paths["csv"].endswith(".csv")
        assert paths["openlineage"].endswith(".openlineage.json")

    def test_custom_base_name(self, simple_result, tmp_path):
        """Custom base name is used in filenames."""
        paths = export_all(
            simple_result, str(tmp_path),
            base_name="my_project",
        )
        for filepath in paths.values():
            assert "my_project" in os.path.basename(filepath)

    def test_creates_output_dir(self, simple_result, tmp_path):
        """Output directory is created if it does not exist."""
        out_dir = str(tmp_path / "nested" / "output")
        paths = export_all(simple_result, out_dir)
        assert os.path.isdir(out_dir)

    def test_files_not_empty(self, simple_result, tmp_path):
        """All output files have content."""
        paths = export_all(simple_result, str(tmp_path))
        for fmt, filepath in paths.items():
            size = os.path.getsize(filepath)
            assert size > 0, f"{fmt} file is empty"
