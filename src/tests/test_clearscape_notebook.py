"""Tests for the Clearscape notebook renderer.

Exercises :mod:`td_release_packager.clearscape_notebook` against a small
synthetic :class:`AnalysisResult` rather than wiring through the full
ingest pipeline — keeps the test focused on the renderer's contract
(cell ordering, token resolution, JSON validity).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from td_release_packager.analyser import AnalysisResult, IndexedObject
from td_release_packager.clearscape_notebook import (
    NBFORMAT_MAJOR,
    NBFORMAT_MINOR,
    render_notebook,
    write_notebook,
)


@pytest.fixture
def two_wave_analysis() -> AnalysisResult:
    """Synthetic analysis: two waves, one database, two tokenised objects."""
    objects = {
        "{{DEMO_DB}}.Customer_T": IndexedObject(
            qualified_name="{{DEMO_DB}}.Customer_T",
            object_type="TABLE",
            file_path="DDL/tables/customer_t.tbl",
            ddl_text=(
                "CREATE TABLE {{DEMO_DB}}.Customer_T (\n"
                "    customer_id INTEGER NOT NULL,\n"
                "    customer_name VARCHAR(100)\n"
                ") PRIMARY INDEX(customer_id);"
            ),
        ),
        "{{DEMO_DB}}.Customer_V": IndexedObject(
            qualified_name="{{DEMO_DB}}.Customer_V",
            object_type="VIEW",
            file_path="DDL/views/customer_v.tbl",
            ddl_text=(
                "REPLACE VIEW {{DEMO_DB}}.Customer_V AS\n"
                "LOCKING ROW FOR ACCESS\n"
                "SELECT customer_id, customer_name FROM {{DEMO_DB}}.Customer_T;"
            ),
        ),
    }
    return AnalysisResult(
        objects=objects,
        waves=[
            ["{{DEMO_DB}}.Customer_T"],
            ["{{DEMO_DB}}.Customer_V"],
        ],
    )


@pytest.fixture
def env_values() -> dict[str, str]:
    return {"DEMO_DB": "ClearscapeDemo"}


def test_render_produces_valid_nbformat_envelope(two_wave_analysis, env_values):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )

    assert notebook["nbformat"] == NBFORMAT_MAJOR
    assert notebook["nbformat_minor"] == NBFORMAT_MINOR
    assert notebook["metadata"]["kernelspec"]["language"] == "python"
    assert notebook["metadata"]["ships"]["target"] == "clearscape-notebook"
    assert notebook["metadata"]["ships"]["package_name"] == "ClearscapeDemo"
    assert notebook["metadata"]["ships"]["wave_count"] == 2
    assert notebook["metadata"]["ships"]["object_count"] == 2

    # Round-trip through JSON to prove the dict is serialisable.
    serialised = json.dumps(notebook)
    assert json.loads(serialised) == notebook


def test_cell_ordering_intro_install_connect_then_waves_then_verify(
    two_wave_analysis, env_values
):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )
    cell_types = [cell["cell_type"] for cell in notebook["cells"]]

    # intro(md) + install(code) + connect(code) + per-wave(md+code)*2 + verify(md+code)
    assert cell_types == [
        "markdown",
        "code",
        "code",
        "markdown",
        "code",
        "markdown",
        "code",
        "markdown",
        "code",
    ]

    intro_source = "".join(notebook["cells"][0]["source"])
    assert "ClearscapeDemo" in intro_source
    assert "Clearscape" in intro_source

    install_source = "".join(notebook["cells"][1]["source"])
    assert "%pip install" in install_source
    assert "teradatasql" in install_source

    connect_source = "".join(notebook["cells"][2]["source"])
    assert "import teradatasql" in connect_source
    assert "getpass" in connect_source
    assert "teradatasql.connect" in connect_source


def test_wave_cells_inline_resolved_ddl(two_wave_analysis, env_values):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )

    # Cell 4 is wave 1 code (cells 3 is wave-1 markdown).
    wave_one_code = "".join(notebook["cells"][4]["source"])
    assert "CREATE TABLE ClearscapeDemo.Customer_T" in wave_one_code
    assert "{{DEMO_DB}}" not in wave_one_code  # tokens resolved
    assert "cursor.execute(sql)" in wave_one_code

    wave_two_code = "".join(notebook["cells"][6]["source"])
    assert "REPLACE VIEW ClearscapeDemo.Customer_V" in wave_two_code
    assert "FROM ClearscapeDemo.Customer_T" in wave_two_code


def test_wave_code_is_syntactically_valid_python(two_wave_analysis, env_values):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        # Skip the install cell — `%pip` is a Jupyter line magic and
        # not valid pure Python by itself.
        if source.lstrip().startswith("%") or "\n%" in source:
            continue
        ast.parse(source)  # raises SyntaxError on failure


def test_verification_cell_lists_resolved_databases(two_wave_analysis, env_values):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )
    verify_source = "".join(notebook["cells"][-1]["source"])
    assert "'ClearscapeDemo'" in verify_source
    assert "DBC.TablesV" in verify_source


def test_unresolved_token_emits_comment_not_exception(two_wave_analysis):
    # Env config deliberately missing DEMO_DB.
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values={},
    )
    wave_one_code = "".join(notebook["cells"][4]["source"])
    assert "Unresolved token" in wave_one_code
    assert "DEMO_DB" in wave_one_code


def test_write_notebook_round_trips_through_disk(
    two_wave_analysis, env_values, tmp_path: Path
):
    notebook = render_notebook(
        two_wave_analysis,
        package_name="ClearscapeDemo",
        env_values=env_values,
    )
    target = tmp_path / "subdir" / "demo.ipynb"
    written = write_notebook(notebook, target)

    assert written == target
    assert target.is_file()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == notebook


def test_markdown_labels_resolve_tokens_and_translate_synthetic_prefixes():
    objects = {
        "{{DEMO_DB}}.Customer_T": IndexedObject(
            qualified_name="{{DEMO_DB}}.Customer_T",
            object_type="TABLE",
            file_path="t.tbl",
            ddl_text="CREATE TABLE {{DEMO_DB}}.Customer_T (id INTEGER);",
        ),
        "$DATABASE.{{DEMO_DB}}": IndexedObject(
            qualified_name="$DATABASE.{{DEMO_DB}}",
            object_type="DATABASE",
            file_path="d.db",
            ddl_text="CREATE DATABASE {{DEMO_DB}} FROM DataProducts AS PERM = 0;",
        ),
        "$FILE:payload/database/DCL/inter_db/CallCentre.dcl": IndexedObject(
            qualified_name="$FILE:payload/database/DCL/inter_db/CallCentre.dcl",
            object_type="GRANT",
            file_path="payload/database/DCL/inter_db/CallCentre.dcl",
            ddl_text="GRANT SELECT ON {{DEMO_DB}} TO PUBLIC;",
        ),
    }
    analysis = AnalysisResult(
        objects=objects,
        waves=[list(objects.keys())],
    )
    notebook = render_notebook(
        analysis,
        package_name="ClearscapeDemo",
        env_values={"DEMO_DB": "ClearscapeDemo"},
    )
    wave_md = "".join(notebook["cells"][3]["source"])

    assert "`ClearscapeDemo.Customer_T`" in wave_md
    assert "`Database: ClearscapeDemo`" in wave_md
    assert "`GRANTs from CallCentre.dcl`" in wave_md
    # Internal markers must not survive into customer-facing markdown.
    assert "$DATABASE." not in wave_md
    assert "$FILE:" not in wave_md
    assert "{{DEMO_DB}}" not in wave_md


def test_long_wave_collapses_into_details_block():
    objects = {
        f"{{{{DEMO_DB}}}}.T_{i:02d}": IndexedObject(
            qualified_name=f"{{{{DEMO_DB}}}}.T_{i:02d}",
            object_type="TABLE",
            file_path=f"t_{i}.tbl",
            ddl_text=f"CREATE TABLE {{{{DEMO_DB}}}}.T_{i:02d} (id INTEGER);",
        )
        for i in range(20)
    }
    analysis = AnalysisResult(objects=objects, waves=[list(objects.keys())])
    notebook = render_notebook(
        analysis,
        package_name="ClearscapeDemo",
        env_values={"DEMO_DB": "ClearscapeDemo"},
    )
    wave_md = "".join(notebook["cells"][3]["source"])
    assert "<details>" in wave_md
    assert "<summary>Show all 20 objects</summary>" in wave_md
    assert "</details>" in wave_md


def test_short_wave_does_not_collapse():
    objects = {
        "{{DEMO_DB}}.T_01": IndexedObject(
            qualified_name="{{DEMO_DB}}.T_01",
            object_type="TABLE",
            file_path="t.tbl",
            ddl_text="CREATE TABLE {{DEMO_DB}}.T_01 (id INTEGER);",
        ),
    }
    analysis = AnalysisResult(objects=objects, waves=[list(objects.keys())])
    notebook = render_notebook(
        analysis,
        package_name="ClearscapeDemo",
        env_values={"DEMO_DB": "ClearscapeDemo"},
    )
    wave_md = "".join(notebook["cells"][3]["source"])
    assert "<details>" not in wave_md


def test_empty_analysis_still_renders_skeleton(env_values, tmp_path: Path):
    notebook = render_notebook(
        AnalysisResult(),
        package_name="EmptyPackage",
        env_values=env_values,
    )
    cell_types = [cell["cell_type"] for cell in notebook["cells"]]
    # intro + install + connect + verify(md+code) — no wave cells.
    assert cell_types == ["markdown", "code", "code", "markdown", "code"]
    verify_source = "".join(notebook["cells"][-1]["source"])
    assert "No databases" in verify_source
