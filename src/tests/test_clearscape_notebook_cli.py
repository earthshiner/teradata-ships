"""End-to-end test for `ships notebook`.

Builds a minimal scaffolded project on disk, invokes the CLI handler
directly, and asserts the produced .ipynb has the expected shape. Direct
invocation (rather than subprocess) keeps the test fast and lets pytest
capture stdout naturally.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from td_release_packager.cli import _cmd_notebook


def _write_min_project(root: Path) -> Path:
    """Create the smallest project layout that ``analyse_project`` accepts."""
    payload = root / "payload" / "database" / "DDL" / "tables"
    payload.mkdir(parents=True)
    (payload / "demo_db.foo.tbl").write_text(
        "CREATE TABLE {{DEMO_DB}}.foo (id INTEGER NOT NULL) PRIMARY INDEX(id);\n",
        encoding="utf-8",
    )

    env_dir = root / "config" / "env"
    env_dir.mkdir(parents=True)
    env_path = env_dir / "DEV.conf"
    env_path.write_text(
        "SHIPS_ENV=DEV\nENV_PREFIX=DEV\nDEMO_DB=DevDemo\n",
        encoding="utf-8",
    )
    return env_path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    _write_min_project(tmp_path)
    return tmp_path


def _args(project: Path, env_config: Path, **overrides) -> argparse.Namespace:
    base = dict(
        project=str(project),
        env_config=str(env_config),
        output=None,
        name=None,
        env_name="DEV",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_notebook_writes_default_output(project_dir: Path, capsys):
    env_config = project_dir / "config" / "env" / "DEV.conf"
    exit_code = _cmd_notebook(_args(project_dir, env_config, name="DemoPkg"))

    assert exit_code == 0
    written = project_dir / "output" / "DemoPkg.clearscape.ipynb"
    assert written.is_file()

    notebook = json.loads(written.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    assert notebook["metadata"]["ships"]["target"] == "clearscape-notebook"
    assert notebook["metadata"]["ships"]["package_name"] == "DemoPkg"

    intro = "".join(notebook["cells"][0]["source"])
    assert "DemoPkg" in intro

    # The one wave's code cell must contain resolved DDL.
    code_cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    all_code = "\n".join("".join(c["source"]) for c in code_cells)
    assert "DevDemo.foo" in all_code
    assert "{{DEMO_DB}}" not in all_code

    summary = capsys.readouterr().out
    assert "SHIPS Clearscape Notebook" in summary


def test_cmd_notebook_respects_explicit_output(project_dir: Path, tmp_path: Path):
    env_config = project_dir / "config" / "env" / "DEV.conf"
    target = tmp_path / "elsewhere" / "custom.ipynb"

    exit_code = _cmd_notebook(
        _args(project_dir, env_config, name="DemoPkg", output=str(target))
    )

    assert exit_code == 0
    assert target.is_file()
    assert not (project_dir / "output").exists()


def test_cmd_notebook_errors_on_missing_project(tmp_path: Path, capsys):
    bogus = tmp_path / "does-not-exist"
    env_config = tmp_path / "env.conf"
    env_config.write_text("DEMO_DB=X\n", encoding="utf-8")

    exit_code = _cmd_notebook(_args(bogus, env_config))

    assert exit_code == 1
    assert "Project directory not found" in capsys.readouterr().err


def test_cmd_notebook_errors_on_missing_env_config(project_dir: Path, capsys):
    bogus = project_dir / "missing.conf"
    exit_code = _cmd_notebook(_args(project_dir, bogus))

    assert exit_code == 1
    assert "Env config file not found" in capsys.readouterr().err


def test_cmd_notebook_defaults_name_to_project_basename(project_dir: Path):
    env_config = project_dir / "config" / "env" / "DEV.conf"
    exit_code = _cmd_notebook(_args(project_dir, env_config))

    assert exit_code == 0
    # Project dir was created by tmp_path — use its basename for the file.
    written = project_dir / "output" / f"{project_dir.name}.clearscape.ipynb"
    assert written.is_file()
